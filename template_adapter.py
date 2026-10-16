"""Resolve HLS templates from a configurable, identity-bound player contract."""
from __future__ import annotations
import json
import re
import subprocess
import time
import urllib.request
from urllib.parse import urljoin,urlsplit
from embedded_adapter import EmbeddedAdapter
from pipeline import Candidate
from site_profile import player_fields,identity_matches


def player_identity(src,profile):
    return tuple(sorted(player_fields(src,profile).items()))


class TemplateAdapter(EmbeddedAdapter):
    def __init__(self,timeout=60,profile=None,encoder='aac_at',max_source_bytes=512*1024**2):
        super().__init__(timeout,max_source_bytes,profile,encoder)

    def page(self,navigate=None):return self._eval(navigate)

    @staticmethod
    def fetch(url,referer):
        try:
            req=urllib.request.Request(url,headers={'Referer':referer,'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req,timeout=40) as response:
                if urlsplit(response.geturl()).scheme!='https':raise RuntimeError('insecure-media')
                raw=response.read(2*1024**2+1)
                if len(raw)>2*1024**2:raise RuntimeError('oversized-manifest')
                return raw.decode('utf-8')
        except Exception:raise RuntimeError('player-fetch-failed') from None

    def resolve(self,lesson):
        self._check_url(lesson['url']);wanted=urlsplit(lesson['url']).path
        transient=(subprocess.TimeoutExpired,RuntimeError,ValueError)
        try:self.page(lesson['url'])
        except transient:pass
        last,stable=None,0;deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            try:page=self.page()
            except transient:
                last,stable=None,0;time.sleep(.5);continue
            if urlsplit(page.get('url','')).path in self.profile.get('login_paths',[]):raise RuntimeError('login-required')
            frames=page.get('frames',[])
            expected=self._title(lesson.get('page_title',lesson['title']))
            valid=(page.get('ready')=='complete' and page.get('url')==lesson['url'] and
                   identity_matches(page,lesson,self.profile) and len(frames)==1 and page.get('titles') and
                   all(self._title(x)==self._title(lesson['title']) for x in page['titles']) and
                   expected==self._title(page.get('title','')))
            key=player_identity(frames[0],self.profile) if valid else None
            if self._last_media and self._last_media[0]!=wanted and self._last_media[1]==key:valid=False
            stable=stable+1 if valid and key==last else 0;last=key
            if stable>=2:
                fields=dict(key);src=frames[0];settings=self.profile['player']
                def address(name):
                    url=settings[name].format_map(fields)
                    if urlsplit(url).scheme!='https':raise RuntimeError('insecure-player-template')
                    return url
                if settings.get('config_url'):
                    config=json.loads(self.fetch(address('config_url'),src))
                    if not isinstance(config,dict) or re.search(r'drm|encrypt',json.dumps(config),re.I):
                        raise RuntimeError('protected-media-not-supported')
                master=address('manifest_url');media=choose_variant(self.fetch(master,src),master)
                playlist=self.fetch(media,src)
                if '#EXT-X-KEY' in playlist or '#EXT-X-ENDLIST' not in playlist:raise RuntimeError('protected-or-unbounded-media')
                candidate=Candidate(media,'hls-audio',thumbnail_url=address('thumbnail_url') if settings.get('thumbnail_url') else None,referer=src)
                self._meta[media]={'referer':src};self._last_media=(wanted,key)
                return [candidate]
            time.sleep(.5)
        raise RuntimeError('lesson-identity-timeout')


from course_catalog import load_lessons


def choose_variant(text, base):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines or lines[0] != '#EXTM3U' or '#EXT-X-KEY' in text or '#EXT-X-SESSION-KEY' in text:
        raise RuntimeError('encrypted-or-invalid-manifest')
    variants = []
    for i, line in enumerate(lines[:-1]):
        if line.startswith('#EXT-X-STREAM-INF:'):
            m = re.search(r'(?:^|,)BANDWIDTH=(\d+)', line.split(':', 1)[1])
            if m and not lines[i+1].startswith('#'):
                variants.append((int(m.group(1)), urljoin(base, lines[i+1])))
    if not variants:
        raise RuntimeError('missing-variants')
    selected = min(variants)[1]
    if urlsplit(selected).scheme != 'https' or urlsplit(selected).netloc != urlsplit(base).netloc:
        raise RuntimeError('unsupported-media-host')
    return selected
