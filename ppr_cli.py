"""Authenticated Safari PPR catalogue entrypoint. No cookie export is required."""
from __future__ import annotations
import argparse
import json
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from pipeline import Pipeline, Candidate
import course_audio_bot as audio

JS = """JSON.stringify({url:location.href,canonical:document.querySelector('link[rel=canonical]')?.href||'',title:document.title,ready:document.readyState,auth:(document.body?.innerText||'').slice(0,400).includes('Olá,'),links:Array.from(document.querySelectorAll('a[href],audio, audio source')).map(e=>({url:e.currentSrc||e.src||e.href||'',label:e.innerText||e.getAttribute('aria-label')||''}))})"""

def normalize_title(value):
    value = re.sub(r'\s+', ' ', value).strip().casefold()
    return re.sub(r'\s*[|–—-]\s*padre paulo ricardo$', '', value).strip()

def canonical(value):
    p = urlsplit(value)
    return p.netloc.lower(), p.path.rstrip('/')

class SafariAdapter:
    def __init__(self, timeout=60, max_source_bytes=256*1024**2):
        self.timeout = timeout
        self.max_source_bytes = max_source_bytes
        self.tab = None

    @staticmethod
    def _osa(script):
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=90)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError('Safari-unavailable')
        return result.stdout.strip()

    def _discover(self):
        script = '''tell application "Safari"
repeat with w in windows
repeat with i from 1 to count of tabs of w
set t to tab i of w
if (URL of t) contains "padrepauloricardo.org" then
try
set a to do JavaScript "(document.body?.innerText||'').slice(0,400).includes('Olá,')" in t
if a is true then return (id of w as text) & ":" & (i as text)
end try
end if
end repeat
end repeat
error "No authenticated provider tab"
end tell'''
        self.tab = tuple(map(int, self._osa(script).split(':')))

    def _eval(self, navigate=None):
        if self.tab is None:
            self._discover()
        window, index = self.tab
        nav = ('if (URL of t) does not contain "padrepauloricardo.org" then error "Provider tab changed"\n'
               'set authenticated to do JavaScript "(document.body?.innerText||\'\').slice(0,400).includes(\'Olá,\')" in t\n'
               'if authenticated is not true then error "Provider tab is not authenticated"\n'
               'set URL of t to ' + json.dumps(navigate, ensure_ascii=False) + '\n') if navigate else ''
        script = f'''tell application "Safari"
set t to tab {index} of window id {window}
{nav}return do JavaScript {json.dumps(JS, ensure_ascii=False)} in t
end tell'''
        return json.loads(self._osa(script))

    @staticmethod
    def candidates(page):
        explicit, other = [], []
        for entry in page.get('links', []):
            url = entry.get('url', '')
            path = urlsplit(url).path.lower()
            label = normalize_title(entry.get('label', ''))
            if not url.startswith(('https://', 'http://')):
                continue
            if path.endswith(('.mp3', '.m4a', '.aac', '.wav', '.ogg')):
                (explicit if ('áudio' in label or 'mp3' in label) else other).append(Candidate(url, 'audio'))
        # Never silently fall back to downloading a multi-GB movie.
        return list({c.url: c for c in explicit + other}.values())

    def resolve(self, value):
        lesson = {'url': value} if isinstance(value, str) else value
        if canonical(lesson['url'])[0] != 'padrepauloricardo.org':
            raise RuntimeError('unsupported-provider')
        expected = normalize_title(lesson.get('title', ''))
        for attempt in range(2):
            try:
                self._eval(lesson['url'])
            except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                self.tab = None
                if attempt:
                    raise RuntimeError('Safari-navigation-failed') from None
                time.sleep(1)
                continue
            deadline, previous, stable = time.monotonic() + self.timeout, None, 0
            while time.monotonic() < deadline:
                try:
                    page = self._eval()
                except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                    time.sleep(.5)
                    continue
                title = normalize_title(page.get('title', ''))
                urls = {canonical(page.get('url', '')), canonical(page.get('canonical', ''))}
                candidates = self.candidates(page)
                matches = (canonical(lesson['url']) in urls and page.get('auth') and
                           page.get('ready') == 'complete' and title and
                           (not expected or title == expected) and candidates)
                key = (title, tuple(canonical(c.url) for c in candidates))
                stable = stable + 1 if matches and key == previous else 0
                previous = key
                if stable >= 2:
                    return candidates
                time.sleep(.5)
        raise RuntimeError('Safari-lesson-identity-timeout')

    def download(self, candidate, destination):
        req = urllib.request.Request(candidate.url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=90) as response, destination.open('wb') as output:
                if response.headers.get_content_type() in {'text/html', 'application/json'}:
                    raise RuntimeError('unexpected-media-content-type')
                length = int(response.headers.get('Content-Length') or 0)
                if length > self.max_source_bytes:
                    raise RuntimeError('source-size-budget-exceeded')
                size = 0
                while chunk := response.read(1024*1024):
                    size += len(chunk)
                    if size > self.max_source_bytes:
                        raise RuntimeError('source-size-budget-exceeded')
                    output.write(chunk)
                if length and size != length:
                    raise RuntimeError('incomplete-download')
            return destination
        except RuntimeError as error:
            if str(error) in {'source-size-budget-exceeded', 'incomplete-download'}:
                raise
            raise RuntimeError('media-download-failed') from None
        except Exception:
            raise RuntimeError('media-download-failed') from None

    def encode(self, source, destination):
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
                                 '-vn', '-ac', '1', '-ar', '44100', '-c:a', 'aac',
                                 '-profile:a', 'aac_low', '-b:a', '32k', str(destination)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if result.returncode:
            raise RuntimeError('audio-encode-failed')


def load_lessons(path):
    data = json.loads(Path(path).read_text())
    lessons = []
    for ci, course in enumerate(data['courses'], 1):
        for index, item in enumerate(course.get('lessons', []), 1):
            if not item.get('url'):
                raise ValueError('catalogue contains an unresolved lesson URL')
            lesson = dict(item, course_title=course['title'])
            lesson['stem'] = f"{ci:02d}-{item.get('number', index):03d}-{audio.slugify(item['title'])}"
            lessons.append(lesson)
    if len({Pipeline.identity(x['url']) for x in lessons}) != len(lessons):
        raise ValueError('duplicate catalogue identities')
    return lessons


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--min-free-gib', type=float, default=4)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args(argv)
    lessons = load_lessons(args.catalog)
    selected = lessons[args.start:args.start + args.limit if args.limit is not None else None]
    pipeline = Pipeline(args.output_root, SafariAdapter(), args.concurrency,
                        int(args.min_free_gib * 1024**3), task_budget_bytes=512*1024**2)
    results = pipeline.run(selected)
    return 0 if all(r['status'] == 'ok' for r in results) else 2

if __name__ == '__main__':
    raise SystemExit(main())
