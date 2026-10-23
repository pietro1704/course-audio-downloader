"""Authenticated browser adapter driven by a local website profile."""
from __future__ import annotations
import json
import re
import subprocess
import time
import urllib.request
from dataclasses import replace
from urllib.parse import urlsplit
from pipeline import Candidate
from site_profile import inspection_script, identity_matches, navigation_script, normalized_url_path


def normalize_title(value, suffix_pattern=None):
    value = re.sub(r'\s+', ' ', value).strip()
    if suffix_pattern: value = re.sub(suffix_pattern, '', value, flags=re.I).strip()
    return value.casefold()


def canonical(value, profile=None):
    p = urlsplit(value)
    path = normalized_url_path(p.path, profile) if profile else p.path
    return p.netloc.lower(), path.rstrip('/')


class SafariAdapter:
    def __init__(self, timeout=60, max_source_bytes=256*1024**2, encoder='aac', profile=None):
        if encoder not in {'aac', 'aac_at'}: raise ValueError('unsupported audio encoder')
        self.encoder, self.timeout, self.max_source_bytes = encoder, timeout, max_source_bytes
        self.profile = dict(profile or {})
        self.origin = self.profile.get('origin')
        self.tab = None
        self._hls_meta = {}

    @staticmethod
    def _osa(script):
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=15)
        if result.returncode or not result.stdout.strip(): raise RuntimeError('Safari-unavailable')
        return result.stdout.strip()

    def _title(self, value):
        return normalize_title(value, self.profile.get('title_suffix_pattern'))

    def _check_url(self, url):
        p = urlsplit(url)
        origin = p.scheme + '://' + p.netloc
        if p.scheme != 'https' or (self.origin and origin != self.origin.rstrip('/')):
            raise RuntimeError('unsupported-provider')
        if self.profile.get('lesson_path_pattern') and not re.fullmatch(self.profile['lesson_path_pattern'], p.path):
            raise RuntimeError('unsupported-lesson')
        if self.origin is None: self.origin = origin

    def _take_link_navigation_context(self):
        return None

    def _activation_script(self):
        activate = self.profile.get('activate_browser_tab', False)
        if type(activate) is not bool:
            raise ValueError('invalid-browser-activation')
        if not activate:
            return ''
        target = self.profile.get('browser_target')
        if target is None:
            raise ValueError('activation-requires-explicit-browser-target')
        if (not isinstance(target, dict) or
                any(type(target.get(key)) is not int or target[key] < 1
                    for key in ('window_id', 'tab_index'))):
            raise ValueError('invalid-browser-target')
        # Insert only inside the existing target's origin guard. Never activate
        # Safari itself or create a replacement window/tab.
        return 'set current tab of window id ' + str(target['window_id']) + ' to t\n'

    def _eval(self, navigate=None, *, force_reload=False):
        if not self.origin: raise RuntimeError('missing-profile-origin')
        activation = self._activation_script()
        command = ('set URL of t to ' + json.dumps(navigate, ensure_ascii=False) + '\nreturn "navigation-requested"') if navigate else ('return do JavaScript ' + json.dumps(inspection_script(self.profile), ensure_ascii=False) + ' in t')
        context=None
        if navigate and not force_reload and self.profile.get('navigation_mode')=='matching-link':
            context=self._take_link_navigation_context()
        fast=context is not None
        if fast:
            command='return do JavaScript '+json.dumps(navigation_script(navigate,self.profile,context),ensure_ascii=False)+' in t'
        script = '''tell application "Safari"
repeat with w in windows
repeat with t in tabs of w
if (URL of t) starts with ''' + json.dumps(self.origin.rstrip('/') + '/') + ''' then
''' + command + '''
end if
end repeat
end repeat
error "No matching browser tab"
end tell'''
        target = self.profile.get('browser_target')
        if target is not None:
            if (not isinstance(target, dict) or
                    any(type(target.get(key)) is not int or target[key] < 1
                        for key in ('window_id', 'tab_index'))):
                raise ValueError('invalid-browser-target')
            script = ('tell application "Safari"\nset t to tab ' + str(target['tab_index']) +
                      ' of window id ' + str(target['window_id']) +
                      '\nif (URL of t) starts with ' + json.dumps(self.origin.rstrip('/') + '/') +
                      ' then\n' + activation + command + '\nend if\nerror "Browser target changed"\nend tell')
        result = self._osa(script)
        if fast:
            if result=='reload-required':return self._eval(navigate,force_reload=True)
            if result!='link-clicked':raise RuntimeError('Safari-navigation-failed')
        return {} if navigate else json.loads(result)

    @staticmethod
    def candidates(page, profile=None):
        profile = profile or {}
        explicit, other = [], []
        pattern = profile.get('audio_label_pattern', r'(?=.*(?:audio|áudio))(?=.*mp3)')
        for entry in page.get('links', []):
            url = entry.get('url', ''); label = normalize_title(entry.get('label', ''))
            if urlsplit(url).scheme != 'https': continue
            control = bool(re.search(pattern, label))
            if control or urlsplit(url).path.lower().endswith(('.mp3','.m4a','.aac','.wav','.ogg')):
                (explicit if control else other).append(Candidate(url, 'mp3' if control else 'audio'))
        direct = list({c.url:c for c in explicit + other}.values())
        host = profile.get('player', {}).get('host_pattern')
        frames = [x for x in page.get('frames', []) if urlsplit(x).scheme == 'https' and
                  (not host or re.fullmatch(host, urlsplit(x).hostname or ''))]
        posters = [x for x in page.get('posters', []) if urlsplit(x).scheme == 'https']
        fields = dict(thumbnail_url=posters[0] if posters else None,
                      player_url=frames[0] if frames else None, referer=page.get('url'))
        if direct: return [replace(c, **fields) for c in direct]
        return [Candidate(x, 'player-embed', player_url=x, thumbnail_url=fields['thumbnail_url'], referer=fields['referer']) for x in frames]

    def resolve(self, value):
        lesson = {'url':value} if isinstance(value,str) else value
        self._check_url(lesson['url'])
        expected = self._title(lesson.get('title',''))
        titles = {expected} if expected else set()
        alias = lesson.get('provider_title')
        if alias is not None:
            if not isinstance(alias,str) or not self._title(alias): raise ValueError('provider_title must be a nonempty string')
            titles.add(self._title(alias))
        if lesson.get('page_title'): titles.add(self._title(lesson['page_title']))
        if expected and type(lesson.get('number')) is int and lesson['number']>0:
            titles.add(f"{lesson['number']}. {expected}")
        for attempt in range(2):
            try: self._eval(lesson['url'])
            except (RuntimeError,ValueError,subprocess.TimeoutExpired):
                if attempt: raise RuntimeError('Safari-navigation-failed') from None
                time.sleep(1);continue
            deadline, previous, stable = time.monotonic()+self.timeout,None,0
            while time.monotonic()<deadline:
                try: page=self._eval()
                except (RuntimeError,ValueError,subprocess.TimeoutExpired):
                    previous,stable=None,0;time.sleep(.5);continue
                if urlsplit(page.get('url','')).path in self.profile.get('login_paths',[]): raise RuntimeError('login-required')
                title=self._title(page.get('title',''));candidates=self.candidates(page,self.profile)
                exact=canonical(page.get('url',''),self.profile)==canonical(lesson['url'],self.profile)
                canon=not page.get('canonical') or canonical(page['canonical'],self.profile)==canonical(lesson['url'],self.profile)
                interactive=(page.get('ready')=='interactive' and titles and exact and canon and
                             len(candidates)==1 and candidates[0].kind=='mp3')
                active=not self.profile.get('active_selector') or any(self._title(x)==self._title(lesson.get('title','')) for x in page.get('titles',[]))
                matches=(exact and canon and page.get('auth') and title and (not titles or title in titles) and
                         (page.get('ready')=='complete' or interactive) and identity_matches(page,lesson,self.profile) and active and candidates)
                key=(title,tuple(canonical(c.url) for c in candidates))
                stable=stable+1 if matches and key==previous else 0;previous=key if matches else None
                if stable>=2:
                    if candidates[0].kind=='player-embed':
                        from media_transport import resolve_player
                        frame=candidates[0].url;candidate=resolve_player(frame,lesson['url'],self.profile.get('player',{}))
                        self._hls_meta[candidate.url]=frame
                        return [replace(candidate,player_url=frame,referer=lesson['url'],thumbnail_url=candidates[0].thumbnail_url)]
                    return candidates
                time.sleep(.5)
        raise RuntimeError('Safari-lesson-identity-timeout')

    def download(self, candidate, destination):
        if candidate.kind == 'hls-audio':
            from media_transport import copy_hls_audio
            return copy_hls_audio(candidate,destination,self.max_source_bytes,self._hls_meta.get(candidate.url, candidate.referer or ""))
        req = urllib.request.Request(candidate.url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with getattr(self,'urlopen',urllib.request.urlopen)(req, timeout=90) as response, destination.open('wb') as output:
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
        # AudioToolbox requires numeric AAC-LC profile 1; it does not accept
        # the native AAC encoder's `aac_low` alias. Quality 0 maps to high.
        options = ['-aac_at_quality', '0', '-aac_at_mode', 'cbr'] if self.encoder == 'aac_at' else []
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
                                 '-vn', '-ac', '1', '-ar', '44100', '-c:a', self.encoder,
                                 '-profile:a', '1' if self.encoder == 'aac_at' else 'aac_low',
                                 '-b:a', '32k', *options, str(destination)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if result.returncode:
            raise RuntimeError('audio-encode-failed')

    def thumbnail(self, candidate, folder):
        # Optional enrichment: unavailable artwork must not discard valid audio.
        try:
            url = candidate.thumbnail_url
            if not url and candidate.player_url:
                from media_transport import thumbnail_player
                url = thumbnail_player(candidate.player_url, candidate.referer, self.profile.get("player", {}))
            if not url or urlsplit(url).scheme != 'https':
                return None
            request = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0', 'Referer': candidate.referer or ''})
            path = folder / 'video-thumbnail.input'
            with urllib.request.urlopen(request, timeout=20) as response:
                if urlsplit(response.geturl()).scheme != 'https':
                    return None
                if int(response.headers.get('Content-Length') or 0) > 8 * 1024**2:
                    return None
                raw = response.read(8 * 1024**2 + 1)
                if not raw or len(raw) > 8 * 1024**2:
                    return None
                path.write_bytes(raw)
            cover = folder / 'video-cover.jpg'
            result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y',
                                     '-i', str(path), '-map', '0:v:0', '-frames:v', '1',
                                     '-vf', 'scale=w=min(1600\\,iw):h=min(1600\\,ih):force_original_aspect_ratio=decrease',
                                     str(cover)], capture_output=True, timeout=30)
            return cover if not result.returncode and cover.is_file() and cover.stat().st_size else None
        except Exception:
            return None



from course_catalog import load_lessons
