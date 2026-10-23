"""Same-origin local website embedding the URL-only Python CLI engine."""
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib
import hmac
import json
import mimetypes
from pathlib import Path
import re
import secrets
import threading
import uuid
from urllib.parse import parse_qs,quote,urlsplit
import webbrowser
import zipfile
from course_discovery import discover
from local_course import LocalCourse
from pipeline import Pipeline
from transfer_errors import failure_reason


class Portal:
    def __init__(self,root,discovery=discover,factory=LocalCourse):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.discovery,self.factory=discovery,factory
        self.lock=threading.RLock();self.course=None;self.profile=None
        self.job=None;self.engine=None;self.thread=None

    def inspect(self,url):
        with self.lock:
            if self.thread and self.thread.is_alive():raise ValueError('a-download-is-active')
            course,profile=self.discovery(url)
            if self.profile and '_browser_runtime' in self.profile:self.profile['_browser_runtime'].close()
            self.course,self.profile=course,profile
            self.job=None;self.engine=None
            return course

    def state(self):
        with self.lock:
            course=self.course
            return dict(course=course,job={k:v for k,v in self.job.items() if k not in {'root','archive_path','archive_hash'}} if self.job else None)

    def start(self,request):
        if not isinstance(request,dict) or set(request)-{'scope','id','silence_removal','request_id'}:
            raise ValueError('invalid-download-request')
        scope=request.get('scope');target=request.get('id');remove=request.get('silence_removal',True)
        nonce=request.get('request_id')
        if not isinstance(target,str) or not target or type(remove) is not bool or not isinstance(nonce,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}',nonce):
            raise ValueError('invalid-download-request')
        with self.lock:
            if not self.course:raise ValueError('load-a-course-first')
            if scope=='course' and target==self.course['id']:selected=self.course['lessons']
            elif scope=='lesson':selected=[x for x in self.course['lessons'] if x['id']==target]
            elif scope=='module':selected=[x for x in self.course['lessons'] if x.get('module_id')==target]
            else:raise ValueError('invalid-download-selection')
            if not selected:raise ValueError('invalid-download-selection')
            signature=hashlib.sha256(json.dumps([self.course['id'],scope,target,remove]).encode()).hexdigest()
            if self.job and self.job['request_id']==nonce:
                if self.job['signature']!=signature:raise ValueError('request-identity-mismatch')
                return self.job['id']
            if self.thread and self.thread.is_alive():raise ValueError('a-download-is-active')
            run_id=uuid.uuid4().hex;root=self.root/run_id
            self.job=dict(id=run_id,request_id=nonce,signature=signature,phase='starting',scope=scope,
                          course_title=self.course['title'],total=len(selected),audios_complete=0,attachments_complete=0,files=[],ready=False,cancel_requested=False,root=str(root))
            def event(row):
                with self.lock:
                    self.job.update(row)
                    print(json.dumps(dict(run_id=run_id,**row),ensure_ascii=False),flush=True)
                    if row['phase']=='complete':self.job['phase']='verifying'
            def publish(record,kind):
                path=Path(record['final'])
                if path.resolve()!=path or not path.is_relative_to(root) or Pipeline.sha256(path)!=record['final_hash']:
                    raise RuntimeError('local-artifact-identity-mismatch')
                ident=hashlib.sha256((run_id+'\n'+str(path.relative_to(root))).encode()).hexdigest()
                entry=dict(id=ident,name=path.name,kind=kind,lesson_id=record['source_identity'],
                           relative=str(path.relative_to(root)),sha256=record['final_hash'],attachment_id=record.get('resource_identity'))
                with self.lock:
                    if self.job['cancel_requested']:raise KeyboardInterrupt()
                    if not any(x['id']==ident for x in self.job['files']):self.job['files'].append(entry)
            try:self.engine=self.factory(root,self.profile,protected_pauses=remove,event=event,on_artifact=publish)
            except Exception:
                self.job.update(phase='failed',reason='engine-initialization-failed')
                raise
            def execute():
                try:
                    records=self.engine.run(selected)
                    entries=[]
                    for record in records:
                        entries.append(dict(path=Path(record['final']),sha256=record['final_hash'],
                            kind='audio',lesson_id=record['source_identity']))
                    document_path=self.engine.docs/'local-documents.json'
                    if document_path.exists():
                        for record in json.loads(document_path.read_text(encoding='utf-8')).values():
                            entries.append(dict(path=Path(record['final']),sha256=record['final_hash'],
                                kind='document',lesson_id=record['source_identity'],attachment_id=record.get('resource_identity')))
                    archive=root/'course.zip';public=[]
                    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as output:
                        for entry in entries:
                            with self.lock:
                                if self.job['cancel_requested']:raise KeyboardInterrupt()
                            path=entry['path']
                            if path.resolve()!=path or not path.is_relative_to(root) or Pipeline.sha256(path)!=entry['sha256']:
                                raise RuntimeError('local-artifact-identity-mismatch')
                            ident=hashlib.sha256((run_id+'\n'+str(path.relative_to(root))).encode()).hexdigest()
                            output.write(path,str(path.relative_to(root)))
                            public.append(dict(id=ident,name=path.name,kind=entry['kind'],lesson_id=entry['lesson_id'],
                                relative=str(path.relative_to(root)),sha256=entry['sha256'],attachment_id=entry.get('attachment_id')))
                    with self.lock:
                        if self.job['cancel_requested']:raise KeyboardInterrupt()
                        self.job.update(phase='complete',ready=True,files=public,archive_path=str(archive),
                                        archive_hash=Pipeline.sha256(archive))
                except BaseException as error:
                    with self.lock:
                        self.job.update(phase='cancelled' if self.job['cancel_requested'] or getattr(self.engine,'phase',None)=='cancelled' or isinstance(error,KeyboardInterrupt)
                                        else 'failed',reason=failure_reason(error),ready=False)
            self.thread=threading.Thread(target=execute,daemon=False);self.thread.start()
            return run_id

    def cancel(self,ident):
        with self.lock:
            if not self.job or ident!=self.job['id']:raise ValueError('download-identity-mismatch')
            if self.thread and self.thread.is_alive():
                self.job.update(phase='cancelling',cancel_requested=True);self.engine.cancel()

    def artifact(self,ident,archive=False):
        with self.lock:
            if not self.job:raise ValueError('download-not-ready')
            if archive:
                if not self.job['ready']:raise ValueError('download-not-ready')
                if ident!=self.job['id']:raise ValueError('download-identity-mismatch')
                path=Path(self.job['archive_path'])
                if path.resolve()!=path or Pipeline.sha256(path)!=self.job['archive_hash']:
                    raise ValueError('local-artifact-identity-mismatch')
                return path,self.job['course_title']+'.zip'
            entry=next((x for x in self.job['files'] if x['id']==ident),None)
            if not entry:raise ValueError('unknown-file')
            path=Path(self.job['root'])/entry['relative']
            if path.resolve()!=path or not path.is_relative_to(Path(self.job['root'])) or Pipeline.sha256(path)!=entry['sha256']:
                raise ValueError('local-artifact-identity-mismatch')
            return path,entry['name']

    def close(self):
        if self.thread and self.thread.is_alive():
            self.cancel(self.job['id']);self.thread.join()
        if self.profile and '_browser_runtime' in self.profile:self.profile['_browser_runtime'].close()


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,port,portal):
        self.portal=portal;self.bootstrap=secrets.token_urlsafe(32);self.session=secrets.token_urlsafe(32)
        self.bootstrapped=False
        super().__init__(('127.0.0.1',port),Handler)
        self.origin='http://127.0.0.1:'+str(self.server_address[1])

    @property
    def open_url(self):return self.origin+'/?launch='+uuid.uuid4().hex+'#session='+self.bootstrap


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def trusted_origin(self):
        return self.headers.get('Host')==urlsplit(self.server.origin).netloc and self.headers.get('Origin') in {None,self.server.origin}

    def authorized(self):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return False
        value=cookie.get('course_session')
        return self.trusted_origin() and value is not None and hmac.compare_digest(value.value.encode(),self.server.session.encode())

    def asset_headers(self,code,kind,length,**extra):
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        for key,value in extra.items():self.send_header(key,value)
        self.end_headers()

    def reply(self,code,value,**extra):
        raw=json.dumps(value,ensure_ascii=False).encode();self.asset_headers(code,'application/json; charset=utf-8',len(raw),**extra)
        self.wfile.write(raw)

    def do_GET(self):
        parsed=urlsplit(self.path)
        if not self.trusted_origin():self.reply(403,{'error':'untrusted-origin'});return
        assets={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if parsed.path in assets:
            file=Path(__file__).parent/'assets'/assets[parsed.path];raw=file.read_bytes()
            self.asset_headers(200,mimetypes.guess_type(str(file))[0] or 'text/plain',len(raw));self.wfile.write(raw);return
        if not self.authorized():self.reply(401,{'error':'open-the-application'});return
        if parsed.path=='/api/state':self.reply(200,self.server.portal.state());return
        if parsed.path in {'/api/file','/api/archive'}:
            query=parse_qs(parsed.query)
            try:
                path,name=self.server.portal.artifact(query.get('id',[''])[0],archive=parsed.path=='/api/archive')
                total=path.stat().st_size;start,end=0,total-1;code=200;extra={}
                value=self.headers.get('Range')
                if value:
                    match=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
                    if not match or not any(match.groups()):raise ValueError('invalid-range')
                    if match[1]:start=int(match[1]);end=min(end,int(match[2])) if match[2] else end
                    else:start=max(0,total-int(match[2]))
                    if start<0 or start>end or start>=total:raise ValueError('invalid-range')
                    code=206;extra['Content-Range']=f'bytes {start}-{end}/{total}'
                extra['Accept-Ranges']='bytes'
                if parsed.path=='/api/archive' or path.suffix.lower()=='.pdf':
                    extra['Content-Disposition']="attachment; filename*=UTF-8''"+quote(name,safe='')
                self.asset_headers(code,mimetypes.guess_type(str(path))[0] or 'application/octet-stream',end-start+1,**extra)
                with path.open('rb') as stream:
                    stream.seek(start);remaining=end-start+1
                    while remaining:
                        chunk=stream.read(min(65536,remaining))
                        if not chunk:break
                        self.wfile.write(chunk);remaining-=len(chunk)
            except (BrokenPipeError,ConnectionResetError):pass
            except (ValueError,OSError):self.reply(409,{'error':'artifact-not-available'})
            return
        self.reply(404,{'error':'not-found'})

    def do_POST(self):
        if not self.trusted_origin() or self.headers.get('Origin')!=self.server.origin:
            self.reply(403,{'error':'untrusted-origin'});return
        try:
            length=int(self.headers.get('Content-Length','-1'))
            if not 0<=length<=8192 or self.headers.get_content_type()!='application/json' or self.headers.get('Transfer-Encoding'):
                raise ValueError('invalid-request')
            self.connection.settimeout(5);body=json.loads(self.rfile.read(length))
            if self.path=='/api/session':
                if self.server.bootstrapped or not isinstance(body,dict) or not hmac.compare_digest(str(body.get('session','')),self.server.bootstrap):
                    self.reply(401,{'error':'open-the-application'});return
                self.server.bootstrapped=True
                self.reply(200,{'ok':True},**{'Set-Cookie':'course_session='+self.server.session+'; HttpOnly; SameSite=Strict; Path=/'});return
            if not self.authorized():self.reply(401,{'error':'open-the-application'});return
            if self.path=='/api/course' and isinstance(body,dict) and set(body)=={'url'}:
                self.reply(200,self.server.portal.inspect(body['url']));return
            if self.path=='/api/download':
                self.reply(202,{'id':self.server.portal.start(body)});return
            if self.path=='/api/cancel' and isinstance(body,dict) and set(body)=={'id'}:
                self.server.portal.cancel(body['id']);self.reply(200,{'ok':True});return
            raise ValueError('invalid-request')
        except (ValueError,TypeError) as error:self.reply(409,{'error':failure_reason(error)})
        except Exception as error:
            print(json.dumps(dict(error_type=type(error).__name__,reason=failure_reason(error))),flush=True)
            self.reply(500,{'error':failure_reason(error)})


def main(argv=None):
    p=argparse.ArgumentParser(description='Local course website with the Python CLI embedded.')
    p.add_argument('--port',type=int,default=8788);p.add_argument('--root',default=str(Path.home()/'Downloads/Course Audio/site'))
    args=p.parse_args(argv)
    if not 1024<=args.port<=65535:p.error('invalid-port')
    portal=Portal(args.root);server=Server(args.port,portal)
    print('Local website: '+server.origin,flush=True)
    webbrowser.open(server.open_url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();portal.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
