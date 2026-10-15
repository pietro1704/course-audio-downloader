"""Hotmart Club adapter: resolve authenticated iframe HLS audio, then use Pipeline AAC preset."""
from __future__ import annotations
import argparse, json, math, re, subprocess, time
from pathlib import Path
from urllib.parse import urlsplit
from pipeline import Pipeline, Candidate
from ppr_cli import SafariAdapter, normalize_title
import course_audio_bot as audio


def parse_next_data(html: str) -> dict:
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S | re.I)
    if not m:
        raise RuntimeError('missing-next-data')
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise RuntimeError('invalid-next-data') from e


def media_urls(data: dict) -> list[str]:
    app = data.get('props',{}).get('pageProps',{}).get('applicationData',{})
    if app.get('isDrmEnabled'):
        raise RuntimeError('drm-media-not-supported')
    return list(dict.fromkeys(a['url'] for a in app.get('mediaAssets',[]) if isinstance(a,dict) and isinstance(a.get('url'),str) and a['url'].startswith('https://')))


def _lesson_title(page: dict) -> str:
    return normalize_title(page.get('title') or page.get('activeTitle') or '')


class HotmartAdapter(SafariAdapter):
    """Uses the already-authenticated Safari tab; signed URLs stay memory-only."""
    def __init__(self, timeout=60, max_source_bytes=512*1024**2):
        super().__init__(timeout, max_source_bytes)
        self._meta = {}
        self._last_media = None

    @staticmethod
    def _iframe_js():
        return """JSON.stringify({url:location.href,title:document.querySelector('h1,h2,h3')?.innerText||'',ready:document.readyState,iframes:Array.from(document.querySelectorAll('iframe[src]')).map(x=>x.src).filter(u=>{try{return new URL(u).hostname==='cf-embed.play.hotmart.com'}catch{return false}}),active:Array.from(document.querySelectorAll('a[href*=\"/content/\"]')).filter(a=>a.innerText.includes('Tocando agora')).map(a=>({path:new URL(a.href).pathname,title:a.querySelector('span[title]')?.getAttribute('title')||''}))})"""

    def _eval_hotmart(self, navigate=None):
        # Reuse Safari tab discovery but run Hotmart-specific page inspection.
        if self.tab is None: self._discover_hotmart()
        window, index = self.tab
        nav = ('set URL of t to ' + json.dumps(navigate, ensure_ascii=False) + '\n') if navigate else ''
        script = f'''tell application "Safari"
set t to tab {index} of window id {window}
if (URL of t) does not contain "hotmart.com" then error "Provider tab changed"
{nav}return do JavaScript {json.dumps(self._iframe_js(), ensure_ascii=False)} in t
end tell'''
        return json.loads(self._osa(script))

    def _discover_hotmart(self):
        script = '''tell application "Safari"
repeat with w in windows
repeat with i from 1 to count of tabs of w
set t to tab i of w
if (URL of t) contains "hotmart.com/" and (URL of t) contains "/products/539478" then return (id of w as text) & ":" & (i as text)
end repeat
end repeat
error "No Hotmart tab"
end tell'''
        self.tab = tuple(map(int, self._osa(script).split(':')))

    @staticmethod
    def _iframe_html(src: str, referer: str) -> str:
        import urllib.request
        req = urllib.request.Request(src, headers={'User-Agent':'Mozilla/5.0', 'Referer':referer})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(8*1024*1024).decode('utf-8', 'replace')

    def resolve(self, lesson):
        lesson = dict(lesson) if not isinstance(lesson, str) else {'url': lesson}
        if urlsplit(lesson['url']).netloc.lower() != 'hotmart.com': raise RuntimeError('unsupported-provider')
        expected = normalize_title(lesson.get('title',''))
        wanted = urlsplit(lesson['url']).path.rstrip('/')
        last, stable = None, 0
        self._eval_hotmart(lesson['url'])
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                page = self._eval_hotmart()
            except (ValueError, RuntimeError, subprocess.TimeoutExpired):
                time.sleep(.5); continue
            active = page.get('active', [])
            matches = (page.get('ready') == 'complete' and
                       urlsplit(page.get('url','')).path.rstrip('/') == wanted and
                       normalize_title(page.get('title','')) == expected and
                       any(a.get('path','').rstrip('/') == wanted and normalize_title(a.get('title','')) == expected for a in active) and
                       page.get('iframes'))
            src = page['iframes'][0] if page.get('iframes') else ''
            key = (wanted, normalize_title(page.get('title','')), urlsplit(src).path)
            if self._last_media and self._last_media[0] != wanted and self._last_media[1] == urlsplit(src).path:
                matches = False
            stable = stable + 1 if matches and key == last else 0
            last = key
            if stable >= 2:
                urls = [u for u in media_urls(parse_next_data(self._iframe_html(src, lesson['url']))) if '.m3u8' in urlsplit(u).path.lower()]
                if not urls: raise RuntimeError('missing-media-assets')
                candidate = Candidate(urls[0], 'hls-audio')
                self._meta[candidate.url] = {'referer': src}
                self._last_media = (wanted, urlsplit(src).path)
                return [candidate]
            time.sleep(.5)
        raise RuntimeError('hotmart-lesson-identity-timeout')

    def download(self, candidate, destination):
        meta = self._meta.get(candidate.url, {})
        referer = meta.get('referer', '')
        headers = 'Referer: ' + referer + '\r\nUser-Agent: Mozilla/5.0\r\n' if referer else 'User-Agent: Mozilla/5.0\r\n'
        probe = subprocess.run(['ffprobe','-v','error','-headers',headers,'-show_entries','format=duration','-of','json',candidate.url],capture_output=True,timeout=90)
        try: duration = float(json.loads(probe.stdout)['format']['duration'])
        except (ValueError,KeyError): raise RuntimeError('unbounded-or-invalid-hls-duration') from None
        if not math.isfinite(duration) or duration <= 0 or duration * 320000 / 8 > self.max_source_bytes:
            raise RuntimeError('hls-source-budget-exceeded')
        result = subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-headers',headers,'-i',candidate.url,
                                 '-map','0:a:0','-c:a','copy','-fs',str(self.max_source_bytes),str(destination)], capture_output=True, timeout=7200)
        if result.returncode: raise RuntimeError('hls-audio-download-failed')
        actual = audio._probe_duration(destination)
        if destination.stat().st_size >= self.max_source_bytes or actual is None or not math.isfinite(actual) or abs(actual-duration) > max(.25,min(2,duration*.02)):
            raise RuntimeError('incomplete-hls-audio')
        return destination


def load_lessons(path):
    data=json.loads(Path(path).read_text()); out=[]
    for mod in data.get('modules', []):
        for i,item in enumerate(mod.get('lessons',[]),1):
            if not item.get('url'): raise ValueError('catalogue contains unresolved lesson URL')
            x=dict(item, course_title=mod['title'], stem=f"{mod['order']:02d}-{item.get('number',i):03d}-{audio.slugify(item['title'])}")
            out.append(x)
    return out


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--catalog',required=True); p.add_argument('--output-root',required=True); p.add_argument('--start',type=int,default=0); p.add_argument('--limit',type=int); p.add_argument('--smoke-module',action='store_true'); p.add_argument('--concurrency',type=int,default=1); p.add_argument('--min-free-gib',type=float,default=4)
    a=p.parse_args(argv); lessons=load_lessons(a.catalog)
    if a.start < 0 or (a.limit is not None and a.limit < 1) or a.min_free_gib < 4:
        p.error('start must be nonnegative, limit positive, and minimum free space at least 4 GiB')
    if a.smoke_module: lessons=[x for x in lessons if x.get('course_title') and x['course_title'].startswith('Módulo 3.II')][:2]
    else: lessons=lessons[a.start:a.start+a.limit if a.limit is not None else None]
    if not lessons: p.error('no lessons selected')
    results=Pipeline(a.output_root,HotmartAdapter(),concurrency=a.concurrency,reserve_bytes=int(a.min_free_gib*1024**3),task_budget_bytes=512*1024**2).run(lessons)
    print(json.dumps({'selected':len(lessons),'ok':sum(r.get('status')=='ok' for r in results),'failed':sum(r.get('status')!='ok' for r in results)},ensure_ascii=False)); return 0 if len(results)==len(lessons) and all(r.get('status')=='ok' for r in results) else 2
if __name__=='__main__': raise SystemExit(main())
