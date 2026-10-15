"""Authenticated Bunny player fallback; signed addresses remain memory-only."""
import re
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from pipeline import Candidate

class Player(HTMLParser):
    def __init__(self):
        super().__init__(); self.players=[]
    def handle_starttag(self,tag,attrs):
        if tag=='bunny-stream-video': self.players.append(dict(attrs))

def get_text(url, referer):
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':referer})
    try:
        with urllib.request.urlopen(request,timeout=60) as response:
            raw=response.read(8*1024**2+1)
            if len(raw)>8*1024**2: raise RuntimeError('player-response-too-large')
            return raw.decode('utf-8')
    except Exception:
        raise RuntimeError('player-fetch-failed') from None

def attributes(value):
    return {k:v.strip('"') for k,v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)',value)}

def select_rendition(master, text):
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
    elif variants:
        # Video resolution affects transfer cost, not the requested final AAC settings.
        # Preserve the selected variant's audio stream without another source transcode.
        selected=min(variants,key=lambda v:v[1] if v[1]>0 else float('inf'))[2]
    elif '#EXTINF:' in text and '#EXT-X-ENDLIST' in text: selected=master
    else: raise RuntimeError('missing-player-vod-audio')
    if urlsplit(selected).scheme!='https': raise RuntimeError('unsafe-player-media-url')
    return selected

def resolve_bunny(frame, lesson_url):
    parsed=urlsplit(frame)
    parts=parsed.path.strip('/').split('/')
    if parsed.scheme!='https' or parsed.hostname!='player.mediadelivery.net' or len(parts)!=3 or parts[0]!='embed':
        raise RuntimeError('unsupported-player-frame')
    parser=Player();parser.feed(get_text(frame,lesson_url))
    players=[p for p in parser.players if p.get('video-id')==parts[2] and p.get('library-id')==parts[1]]
    if len(players)!=1: raise RuntimeError('player-identity-mismatch')
    player=players[0]
    if player.get('drm-version')!='0': raise RuntimeError('drm-media-not-supported')
    master=player.get('content-src','')
    if urlsplit(master).scheme!='https' or '.m3u8' not in urlsplit(master).path:
        raise RuntimeError('missing-player-hls')
    selected=select_rendition(master,get_text(master,frame))
    return Candidate(selected,'hls-audio')

def copy_hls_audio(candidate,destination,max_bytes,referer):
    # Shared source-size/duration guards and original stream-copy behavior.
    from hotmart_cli import HotmartAdapter
    adapter=HotmartAdapter(max_source_bytes=max_bytes)
    adapter._meta[candidate.url]={'referer':referer}
    return adapter.download(candidate,destination)
