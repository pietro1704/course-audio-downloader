"""Optional bounded, memory-only prefetch for ordinary VOD HLS fragments.

FFmpeg retains responsibility for demuxing and AES-128. Remote addresses and
keys stay in memory; the loopback server exposes only random, indexed routes.
"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urljoin,urlsplit
import math,re,secrets,subprocess,threading


class PrefetchedPlaylist:
    def __init__(self,text,base,referer,width,fetch=None):
        from media_transport import read_fragment
        if type(width) is not int or not 1<=width<=4:raise ValueError('invalid-hls-prefetch-width')
        self.width=width;self.referer=referer;self.fetch=fetch or read_fragment
        self.token=secrets.token_hex(24);self.segments=[];self.lines=[];self.keys=[]
        self.duration=0.;durations=[];self.futures={};self.key_cache={};self.failed=False
        if not text.lstrip().startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in text:raise ValueError('unsupported-prefetch-playlist')
        for raw in text.splitlines():
            line=raw.strip()
            if not line:continue
            if line.startswith(('#EXT-X-MAP:','#EXT-X-BYTERANGE:','#EXT-X-STREAM-INF:','#EXT-X-SESSION-KEY:')):
                raise ValueError('unsupported-prefetch-playlist')
            if line.startswith('#EXTINF:'):
                value=float(line.split(':',1)[1].split(',')[0])
                if not math.isfinite(value) or value<=0:raise ValueError('invalid-prefetch-duration')
                durations.append(value)
            if line.startswith('#EXT-X-KEY:'):
                from media_transport import attributes
                fields=attributes(line.split(':',1)[1])
                if fields.get('METHOD')=='NONE':self.lines.append(line);continue
                if fields.get('METHOD')!='AES-128' or fields.get('KEYFORMAT','identity')!='identity':
                    raise ValueError('unsupported-prefetch-encryption')
                address=urljoin(base,fields.get('URI',''))
                if not fields.get('URI') or urlsplit(address).scheme!='https':raise ValueError('unsafe-prefetch-key')
                if address not in self.keys:self.keys.append(address)
                route=f'key/{self.keys.index(address)}'
                line=re.sub(r'URI="[^"]*"','URI="'+route+'"',line,count=1)
                if 'URI="'+route+'"' not in line:raise ValueError('unsupported-prefetch-key')
            elif line.startswith('#'):
                if re.search(r'\bURI=',line):raise ValueError('unsupported-prefetch-uri')
            else:
                address=urljoin(base,line)
                if urlsplit(address).scheme!='https':raise ValueError('unsafe-prefetch-segment')
                line=f'segment/{len(self.segments)}.ts';self.segments.append(address)
            self.lines.append(line)
        if not self.segments or len(durations)!=len(self.segments):raise ValueError('invalid-prefetch-segments')
        self.duration=sum(durations)
        self.server=None;self.pool=None;self.thread=None

    def __enter__(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.connection.settimeout(15)
                prefix='/'+owner.token+'/'
                if not self.path.startswith(prefix):self.send_error(404);return
                route=self.path[len(prefix):];segment=None
                try:
                    if route=='playlist.m3u8':
                        body=('\n'.join(owner.lines)+'\n').encode();kind='application/vnd.apple.mpegurl'
                    elif re.fullmatch(r'segment/\d+\.ts',route):
                        segment=int(route.split('/')[1][:-3])
                        if not 0<=segment<len(owner.segments):self.send_error(404);return
                        future=owner.futures.get(segment)
                        body=(future or owner.pool.submit(owner.fetch,owner.segments[segment],owner.referer)).result()
                        kind='video/mp2t'
                    elif re.fullmatch(r'key/\d+',route):
                        index=int(route.split('/')[1])
                        if not 0<=index<len(owner.keys):self.send_error(404);return
                        body=owner.key_cache.get(index)
                        if body is None:
                            body=owner.pool.submit(owner.fetch,owner.keys[index],owner.referer).result()
                            if len(body)!=16:raise RuntimeError('invalid-prefetch-key-size')
                            if len(owner.key_cache)>=4:owner.key_cache.pop(next(iter(owner.key_cache)))
                            owner.key_cache[index]=body
                        kind='application/octet-stream'
                    else:self.send_error(404);return
                    if len(body)>16*1024**2:raise RuntimeError('prefetch-fragment-too-large')
                    self.send_response(200);self.send_header('Content-Type',kind)
                    self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
                    self.end_headers();self.wfile.write(body)
                    if segment is not None:
                        owner.futures.pop(segment,None)
                        # At most width prefetched fragments plus the current response.
                        following=segment+owner.width
                        if following<len(owner.segments) and following not in owner.futures and len(owner.futures)<owner.width:
                            owner.futures[following]=owner.pool.submit(owner.fetch,owner.segments[following],owner.referer)
                except (BrokenPipeError,ConnectionResetError):pass
                except Exception:
                    owner.failed=True
                    try:self.send_error(503)
                    except OSError:pass
        try:
            self.server=HTTPServer(('127.0.0.1',0),Handler)
            self.pool=ThreadPoolExecutor(max_workers=self.width,thread_name_prefix='hls-prefetch')
            self.futures={i:self.pool.submit(self.fetch,url,self.referer) for i,url in enumerate(self.segments[:self.width])}
            self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.__exit__(None,None,None);raise

    @property
    def url(self):return f'http://127.0.0.1:{self.server.server_port}/{self.token}/playlist.m3u8'

    def __exit__(self,*args):
        if self.server:
            if self.thread and self.thread.is_alive():self.server.shutdown();self.thread.join()
            self.server.server_close()
        if self.pool:self.pool.shutdown(wait=True,cancel_futures=True)
        self.futures.clear();self.key_cache.clear()


def copy_prefetched(candidate,destination,max_bytes,referer,width):
    from media_transport import get_text
    import course_audio_bot as audio
    try:playlist=PrefetchedPlaylist(get_text(candidate.url,referer),candidate.url,referer,width)
    except ValueError:return None  # Retain the established transport for unsupported layouts.
    if playlist.duration*320000/8>max_bytes:raise RuntimeError('hls-source-budget-exceeded')
    with playlist:
        result=subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',playlist.url,
            '-map','0:a:0','-c:a','copy','-fs',str(max_bytes),str(destination)],capture_output=True,timeout=7200)
        if result.returncode or playlist.failed:raise RuntimeError('hls-audio-download-failed')
    actual=audio._probe_duration(destination)
    if (not Path(destination).exists() or Path(destination).stat().st_size>=max_bytes or actual is None
            or not math.isfinite(actual) or abs(actual-playlist.duration)>max(.25,min(2,playlist.duration*.02))):
        raise RuntimeError('incomplete-hls-audio')
    return destination
