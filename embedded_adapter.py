"""Profile-driven embedded JSON media and bounded HLS audio copy."""
from __future__ import annotations
import json,math,re,subprocess,time
from urllib.parse import urlsplit
from pipeline import Candidate
from browser_adapter import SafariAdapter,normalize_title,canonical
from site_profile import normalized_url_path
import course_audio_bot as audio


def parse_next_data(html, script_id='__NEXT_DATA__'):
    m=re.search(r'<script[^>]+id=["\x27]'+re.escape(script_id)+r'["\x27][^>]*>(.*?)</script>',html,re.S|re.I)
    if not m:raise RuntimeError('missing-next-data')
    try:return json.loads(m.group(1))
    except json.JSONDecodeError:raise RuntimeError('invalid-next-data') from None


def media_urls(data, profile=None):
    profile=profile or {}
    app=data
    for key in profile.get('data_path',['props','pageProps','applicationData']):
        app=app.get(key,{}) if isinstance(app,dict) else {}
    if app.get(profile.get('protected_field','isDrmEnabled')):raise RuntimeError('drm-media-not-supported')
    return list(dict.fromkeys(a[profile.get('url_key','url')] for a in app.get(profile.get('assets_field','mediaAssets'),[])
        if isinstance(a,dict) and isinstance(a.get(profile.get('url_key','url')),str) and a[profile.get('url_key','url')].startswith('https://')))


def media_thumbnail(data, profile):
    path = profile.get('thumbnail_path')
    if not isinstance(path, list) or not path:
        return None
    value = data
    for key in path:
        if isinstance(value, dict) and isinstance(key, str):
            value = value.get(key)
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            return None
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    return value if url.scheme == 'https' and url.hostname and not url.username else None


class EmbeddedAdapter(SafariAdapter):
    def __init__(self, timeout=60, max_source_bytes=512*1024**2, profile=None, encoder='aac'):
        super().__init__(timeout,max_source_bytes,encoder,profile)
        self._meta={};self._last_media=None;self._link_context=None

    def _take_link_navigation_context(self):
        context=self._link_context
        self._link_context=None  # A failed navigation/resolve cannot authorize another click.
        return context

    def _eval_embedded(self,navigate=None):return self._eval(navigate)

    @staticmethod
    def _iframe_html(src,referer):
        from media_transport import get_text
        return get_text(src,referer)

    def resolve(self,lesson):
        self._check_url(lesson['url'])
        expected=self._title(lesson.get('title',''))
        wanted=normalized_url_path(urlsplit(lesson['url']).path,self.profile).rstrip('/')
        self._eval_embedded(lesson['url']);last,stable=None,0;deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            try:page=self._eval_embedded()
            except (ValueError,RuntimeError,subprocess.TimeoutExpired):time.sleep(.5);continue
            direct=[c for c in self.candidates(page,self.profile) if c.kind in {'mp3','audio'}]
            if direct and page.get('auth') and canonical(page.get('url',''),self.profile)==canonical(lesson['url'],self.profile):
                # Reuse the direct adapter's full title, page and candidate stability guards.
                return SafariAdapter.resolve(self,lesson)
            frames=page.get('iframes',[]);src=frames[0] if frames else '';player=self.profile.get('player',{})
            allowed=(urlsplit(src).scheme=='https' and re.fullmatch(player.get('host_pattern',r'(?!)'),urlsplit(src).hostname or ''))
            matches=(page.get('ready')=='complete' and canonical(page.get('url',''),self.profile)==canonical(lesson['url'],self.profile) and
                     self._title(page.get('title',''))==expected and allowed and
                     any(normalized_url_path(a.get('path',''),self.profile).rstrip('/')==wanted and
                         self._title(a.get('title',''))==expected for a in page.get('active',[])))
            key=(wanted,self._title(page.get('title','')),urlsplit(src).path)
            if self._last_media and self._last_media[0]!=wanted and self._last_media[1]==urlsplit(src).path:matches=False
            stable=stable+1 if matches and key==last else 0;last=key
            if stable>=2:
                payload=parse_next_data(self._iframe_html(src,lesson['url']),self.profile.get('script_id','__NEXT_DATA__'))
                urls=[u for u in media_urls(payload,self.profile) if '.m3u8' in urlsplit(u).path.lower()]
                if not urls:raise RuntimeError('missing-media-assets')
                media=urls[0]
                if self.profile.get('prefer_audio_rendition') is True:
                    from media_transport import get_text,select_rendition,shared_muxed_audio
                    playlist=get_text(media,src)
                    media=(select_rendition(media,playlist,audio_only=True)
                           or shared_muxed_audio(media,playlist) or media)
                candidate=Candidate(media,'hls-audio',referer=src,thumbnail_url=media_thumbnail(payload,self.profile));self._meta[candidate.url]={'referer':src};self._last_media=(wanted,urlsplit(src).path)
                frame=urlsplit(src)
                origin=frame.scheme+'://'+(frame.hostname or '')
                if frame.port and frame.port!=443:origin+=':'+str(frame.port)
                self._link_context=dict(page_path=urlsplit(page['url']).path.rstrip('/'),player=origin+frame.path)
                return [candidate]
            time.sleep(.5)
        raise RuntimeError('embedded-lesson-identity-timeout')

    def download(self, candidate, destination):
        meta = self._meta.get(candidate.url, {})
        referer = meta.get('referer', '')
        width=self.profile.get('hls_prefetch_fragments',0)
        if width:
            if type(width) is not int or not 1<=width<=4:raise ValueError('invalid-hls-prefetch-width')
            from hls_prefetch import copy_prefetched
            result=copy_prefetched(candidate,destination,self.max_source_bytes,referer,width)
            if result is not None:return result
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



from course_catalog import load_lessons
