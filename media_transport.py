"""Configurable embedded player and bounded stream transport; signed addresses remain memory-only."""
import re
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from pipeline import Candidate

class Player(HTMLParser):
    def __init__(self, element):
        super().__init__(); self.players=[]; self.element=element
    def handle_starttag(self,tag,attrs):
        if tag==self.element: self.players.append(dict(attrs))

def get_text(url, referer):
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':referer})
    try:
        with urllib.request.urlopen(request,timeout=60) as response:
            raw=response.read(8*1024**2+1)
            if len(raw)>8*1024**2: raise RuntimeError('player-response-too-large')
            return raw.decode('utf-8')
    except Exception as error:
        from urllib.error import HTTPError
        if isinstance(error,HTTPError): error.close()
        raise RuntimeError('player-fetch-failed') from None

def attributes(value):
    return {k:v.strip('"') for k,v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)',value)}

def select_rendition(master, text, audio_only=False):
    if not text.lstrip().startswith('#EXTM3U'): raise RuntimeError('invalid-player-playlist')
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    audio=[];variants=[]
    for i,line in enumerate(lines):
        if line.startswith('#EXT-X-MEDIA:'):
            attrs=attributes(line.split(':',1)[1])
            if attrs.get('TYPE')=='AUDIO' and attrs.get('URI'):
                audio.append((attrs.get('DEFAULT')!='YES',urljoin(master,attrs['URI'])))
        if line.startswith('#EXT-X-STREAM-INF:'):
            attrs=attributes(line.split(':',1)[1])
            if i+1>=len(lines) or lines[i+1].startswith('#'): raise RuntimeError('invalid-player-variant')
            codecs=attrs.get('CODECS','').split(',')
            if not any(c.startswith(('mp4a','ac-3','ec-3')) for c in codecs): continue
            only_audio=all(c.startswith(('mp4a','ac-3','ec-3')) for c in codecs)
            variants.append((only_audio,int(attrs.get('BANDWIDTH','0')),urljoin(master,lines[i+1])))
    if audio: selected=sorted(audio)[0][1]
    elif any(v[0] for v in variants): selected=max((v for v in variants if v[0]),key=lambda v:v[1])[2]
    elif audio_only: return None
    elif variants:
        # Muxed resolutions can carry different audio bitrates: preserve the
        # highest advertised source quality, then discard video during remux.
        selected=max(variants,key=lambda v:v[1])[2]
    elif '#EXTINF:' in text and '#EXT-X-ENDLIST' in text: selected=master
    else: raise RuntimeError('missing-player-vod-audio')
    if urlsplit(selected).scheme!='https': raise RuntimeError('unsafe-player-media-url')
    return selected


def shared_muxed_audio(master,text):
    """Use less video only when every variant carries one declared audio rendition.

    RFC 8216 section 4.3.4.2.1 places an AUDIO rendition without a URI in
    every referencing video variant. Missing/ambiguous groups or different
    audio codecs provide no such selection contract and retain the caller's
    original source policy.
    """
    if not text.lstrip().startswith('#EXTM3U'):raise RuntimeError('invalid-player-playlist')
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    groups=[attributes(line.split(':',1)[1]) for line in lines if line.startswith('#EXT-X-MEDIA:')]
    groups=[group for group in groups if group.get('TYPE')=='AUDIO']
    if len(groups)!=1 or groups[0].get('URI') or not groups[0].get('GROUP-ID'):return None
    group=groups[0]['GROUP-ID'];variants=[];audio_codec=None
    for i,line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF:'):continue
        info=attributes(line.split(':',1)[1])
        codecs=tuple(c for c in info.get('CODECS','').split(',') if c.startswith(('mp4a','ac-3','ec-3')))
        if info.get('AUDIO')!=group or len(codecs)!=1:return None
        if audio_codec is not None and codecs!=audio_codec:return None
        audio_codec=codecs
        try:bandwidth=int(info.get('BANDWIDTH','0'))
        except ValueError:return None
        if bandwidth<=0 or i+1>=len(lines) or lines[i+1].startswith('#'):return None
        url=urljoin(master,lines[i+1])
        if urlsplit(url).scheme!='https':raise RuntimeError('unsafe-player-media-url')
        variants.append((bandwidth,url))
    return min(variants,key=lambda item:item[0])[1] if variants else None

def _player(frame, lesson_url, profile):
    from site_profile import player_fields
    fields=player_fields(frame,profile)
    parser=Player(profile.get('element','media-player'));parser.feed(get_text(frame,lesson_url))
    identities=profile.get('identity_attributes',{'video':'video-id','library':'library-id'})
    players=[p for p in parser.players if all(p.get(attr)==fields.get(key) for key,attr in identities.items())]
    if len(players)!=1: raise RuntimeError('player-identity-mismatch')
    return players[0]

def resolve_player(frame, lesson_url, profile=None):
    profile=profile or {}
    player=_player(frame,lesson_url,profile)
    policy=profile.get('unprotected_attributes')
    if not policy or any(player.get(key) not in allowed for key,allowed in policy.items()):
        raise RuntimeError('drm-media-not-supported')
    master=player.get(profile.get('media_attribute','content-src'),'')
    if urlsplit(master).scheme!='https' or '.m3u8' not in urlsplit(master).path:
        raise RuntimeError('missing-player-hls')
    return Candidate(select_rendition(master,get_text(master,frame)),'hls-audio')

def thumbnail_player(frame, lesson_url, profile=None):
    profile=profile or {}
    try:player=_player(frame,lesson_url,profile)
    except RuntimeError:return None
    url=player.get(profile.get('poster_attribute','poster'),'')
    return url if urlsplit(url).scheme=='https' else None

def plain_segments(url,text):
    import math
    if not text.lstrip().startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in text:
        raise RuntimeError('missing-player-vod-audio')
    # Unsupported container/encryption modes retain the existing guarded FFmpeg path.
    if any(tag in text for tag in ('#EXT-X-MAP:', '#EXT-X-BYTERANGE:')): return None
    if any(attributes(line.split(':',1)[1]).get('METHOD')!='NONE'
           for line in text.splitlines() if line.startswith('#EXT-X-KEY:')): return None
    durations=[];segments=[]
    for line in text.splitlines():
        line=line.strip()
        if line.startswith('#EXTINF:'):
            try: duration=float(line.split(':',1)[1].split(',')[0])
            except ValueError: raise RuntimeError('invalid-player-segment-duration') from None
            if not math.isfinite(duration) or duration<=0: raise RuntimeError('invalid-player-segment-duration')
            durations.append(duration)
        elif line and not line.startswith('#'):
            segment=urljoin(url,line)
            if urlsplit(segment).scheme!='https': raise RuntimeError('unsafe-player-media-url')
            segments.append(segment)
    if not segments or len(segments)!=len(durations): raise RuntimeError('invalid-player-segment-list')
    return segments,sum(durations)

def read_fragment(url,referer):
    # Four in-flight fragments, each capped at 16 MiB; no movie spool on disk.
    import time
    for attempt in range(3):
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':referer})
            with urllib.request.urlopen(request,timeout=30) as response:
                if response.headers.get_content_type() in {'text/html','application/json'}:
                    raise RuntimeError('invalid-player-segment-content')
                length=int(response.headers.get('Content-Length') or 0)
                if length>16*1024**2: raise RuntimeError('player-segment-size-budget')
                chunks=[];size=0;start=time.monotonic()
                while chunk:=response.read(1024**2):
                    size+=len(chunk)
                    if size>16*1024**2: raise RuntimeError('player-segment-size-budget')
                    if time.monotonic()-start>90: raise RuntimeError('player-segment-timeout')
                    chunks.append(chunk)
                if not size or (length and length!=size): raise OSError('incomplete-segment')
                return b''.join(chunks)
        except RuntimeError: raise
        except Exception as error:
            from urllib.error import HTTPError
            if isinstance(error,HTTPError): error.close()
            if attempt==2: raise RuntimeError('player-segment-fetch-failed') from None

def copy_hls_audio(candidate,destination,max_bytes,referer):
    import subprocess,time
    from concurrent.futures import ThreadPoolExecutor
    import course_audio_bot as audio
    parsed=plain_segments(candidate.url,get_text(candidate.url,referer))
    if parsed is None:
        from embedded_adapter import EmbeddedAdapter
        adapter=EmbeddedAdapter(max_source_bytes=max_bytes)
        adapter._meta[candidate.url]={'referer':referer}
        return adapter.download(candidate,destination)
    segments,expected=parsed
    process=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-y','-f','mpegts','-i','pipe:0',
                              '-map','0:a:0','-c:a','copy','-fs',str(max_bytes),str(destination)],
                             stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        start=time.monotonic()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i in range(0,len(segments),4):
                if time.monotonic()-start>7200: raise RuntimeError('player-download-timeout')
                # Batch submission prevents eager whole-movie buffering by executor.map.
                futures=[pool.submit(read_fragment,url,referer) for url in segments[i:i+4]]
                for future in futures: process.stdin.write(future.result())
        process.stdin.close()
        if process.wait(timeout=120): raise RuntimeError('player-remux-failed')
        if not destination.exists() or destination.stat().st_size>=max_bytes:
            raise RuntimeError('source-size-budget-exceeded')
        duration=audio._probe_duration(destination)
        tolerance=max(.25,min(2,expected*.02))
        if duration is None or abs(duration-expected)>tolerance:
            raise RuntimeError('player-source-duration-mismatch')
        return destination
    except Exception:
        raise RuntimeError('player-audio-download-failed') from None
    finally:
        if process.poll() is None:
            process.kill();process.wait()
        if not process.stdin.closed:
            try: process.stdin.close()
            except OSError: pass
