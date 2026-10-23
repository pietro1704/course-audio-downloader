import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from local_portal import Portal,Server
from pipeline import Pipeline

LESSONS=[dict(id=Pipeline.identity('https://example.test/'+str(i)),url='https://example.test/'+str(i),title=str(i),module_id='module') for i in range(2)]
COURSE=dict(id='course',title='Example',lessons=LESSONS)

class Engine:
    def __init__(self,root,profile,protected_pauses,event,on_artifact=None):
        self.root=Path(root);self.root.mkdir(parents=True);self.docs=self.root/'docs';self.docs.mkdir()
        self.remove=protected_pauses;self.event=event;self.phase=None;self.on_artifact=on_artifact or (lambda *args:None)
    def run(self,lessons):
        self.lessons=lessons;records=[]
        for lesson in lessons:
            path=self.root/(lesson['title']+'.m4a');path.write_bytes(b'audio fixture')
            records.append(dict(final=str(path),final_hash=Pipeline.sha256(path),source_identity=lesson['id']))
        return records
    def cancel(self):self.phase='cancelled'

class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.portal=Portal(self.temp.name,discovery=lambda url:(COURSE,{}),factory=Engine)
        self.portal.inspect('https://example.test/course')
        self.addCleanup(self.portal.close)
    def start(self,scope='course',ident='course',**kw):
        run=self.portal.start(dict(scope=scope,id=ident,request_id='request',**kw))
        self.portal.thread.join(3);self.assertFalse(self.portal.thread.is_alive());return run
    def test_selection_default_optout_idempotency_and_archive(self):
        run=self.start();self.assertTrue(self.portal.engine.remove)
        self.assertEqual(len(self.portal.engine.lessons),2)
        self.assertEqual(self.portal.start(dict(scope='course',id='course',request_id='request')),run)
        path,_=self.portal.artifact(run,archive=True)
        with zipfile.ZipFile(path) as z:self.assertEqual(z.namelist(),['0.m4a','1.m4a'])
        for scope,ident,size in [('lesson',LESSONS[0]['id'],1),('module','module',2)]:
            self.portal.start(dict(scope=scope,id=ident,request_id=scope,silence_removal=False));self.portal.thread.join(3)
            self.assertFalse(self.portal.engine.remove);self.assertEqual(len(self.portal.engine.lessons),size)
    def test_unknown_selection_and_request_identity_fail_closed(self):
        for scope,ident in [('course','wrong'),('lesson','wrong'),('module','wrong'),('module',None)]:
            with self.assertRaises(ValueError):self.start(scope,ident)
        self.start()
        with self.assertRaisesRegex(ValueError,'request-identity-mismatch'):
            self.portal.start(dict(scope='course',id='course',request_id='request',silence_removal=False))
    def test_modified_file_old_run_and_archive_are_rejected(self):
        run=self.start();entry=self.portal.state()['job']['files'][0]
        path,_=self.portal.artifact(entry['id']);path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'identity-mismatch'):self.portal.artifact(entry['id'])
        archive,_=self.portal.artifact(run,archive=True);archive.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'identity-mismatch'):self.portal.artifact(run,archive=True)
        self.portal.start(dict(scope='course',id='course',request_id='new'));self.portal.thread.join(3)
        with self.assertRaises(ValueError):self.portal.artifact(entry['id'])
        with self.assertRaises(ValueError):self.portal.artifact(run,archive=True)
    def test_cancel_uses_exact_run_and_blocks_parallel_discovery(self):
        entered=threading.Event();released=threading.Event()
        class Blocking(Engine):
            def run(self,lessons):
                entered.set();released.wait(3);raise KeyboardInterrupt()
            def cancel(self):super().cancel();released.set()
        self.portal.factory=Blocking
        run=self.portal.start(dict(scope='course',id='course',request_id='request'));self.assertTrue(entered.wait(3))
        with self.assertRaises(ValueError):self.portal.inspect('https://example.test/other')
        with self.assertRaises(ValueError):self.portal.cancel('wrong')
        self.portal.cancel(run);self.portal.thread.join(3)
        self.assertEqual(self.portal.state()['job']['phase'],'cancelled')
        with self.assertRaises(ValueError):self.portal.artifact(run,archive=True)
    def test_initialization_failure_is_reported_and_can_retry(self):
        def broken(*args,**kwargs):raise RuntimeError('model-unavailable')
        self.portal.factory=broken
        with self.assertRaises(RuntimeError):self.start()
        self.assertEqual(self.portal.state()['job']['phase'],'failed')
        self.portal.factory=Engine
        self.portal.start(dict(scope='course',id='course',request_id='retry'));self.portal.thread.join(3)
        self.assertTrue(self.portal.state()['job']['ready'])
    def test_cancel_after_last_audio_cannot_publish_zip(self):
        entered=threading.Event();released=threading.Event()
        class LateCancel(Engine):
            def run(self,lessons):
                records=super().run(lessons);entered.set();released.wait(3)
                self.event(dict(phase='complete'))
                return records
            def cancel(self):released.set()
        self.portal.factory=LateCancel
        run=self.portal.start(dict(scope='course',id='course',request_id='request'));self.assertTrue(entered.wait(3))
        self.portal.cancel(run);self.portal.thread.join(3)
        self.assertEqual(self.portal.state()['job']['phase'],'cancelled')
        with self.assertRaises(ValueError):self.portal.artifact(run,archive=True)
    def test_engine_failure_does_not_publish_artifacts(self):
        class Broken(Engine):
            def run(self,lessons):raise RuntimeError('download-failed')
        self.portal.factory=Broken;run=self.start()
        self.assertEqual(self.portal.state()['job']['phase'],'failed')
        with self.assertRaises(ValueError):self.portal.artifact(run,archive=True)
    def test_processed_audio_streams_while_rest_is_downloading(self):
        entered=threading.Event();released=threading.Event()
        class Streaming(Engine):
            def run(self,lessons):
                records=super().run(lessons[:1]);self.on_artifact(records[0],'audio')
                entered.set();released.wait(3)
                return records
            def cancel(self):super().cancel();released.set()
        self.portal.factory=Streaming
        run=self.portal.start(dict(scope='course',id='course',request_id='request',silence_removal=True))
        self.addCleanup(released.set);self.assertTrue(entered.wait(3))
        state=self.portal.state()['job'];self.assertFalse(state['ready'])
        self.assertEqual(len(state['files']),1)
        path,_=self.portal.artifact(state['files'][0]['id']);self.assertEqual(path.read_bytes(),b'audio fixture')
        self.assertTrue(self.portal.engine.remove)
        with self.assertRaises(ValueError):self.portal.artifact(run,archive=True)
        released.set();self.portal.thread.join(3)
    def test_http_session_origin_cookie_and_audio_range(self):
        run=self.start();server=Server(0,self.portal)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port);self.addCleanup(connection.close)
        def request(method,path,body=None,headers=None):
            connection.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers or {})
            response=connection.getresponse();return response.status,dict(response.getheaders()),response.read()
        self.assertEqual(request('GET','/api/state')[0],401)
        self.assertEqual(request('GET','/api/state',headers={'Host':'evil.test'})[0],403)
        headers={'Origin':server.origin,'Content-Type':'application/json'}
        status,response,_=request('POST','/api/session',{'session':server.bootstrap},headers)
        self.assertEqual(status,200);cookie=response['Set-Cookie'];self.assertIn('HttpOnly',cookie)
        headers['Cookie']=cookie.split(';')[0]
        self.assertEqual(request('POST','/api/session',{'session':server.bootstrap},headers)[0],401)
        self.assertEqual(request('GET','/api/state',headers=headers)[0],200)
        evil={**headers,'Origin':'https://evil.test'}
        self.assertEqual(request('POST','/api/download',{},evil)[0],403)
        ident=self.portal.state()['job']['files'][0]['id']
        status,response,data=request('GET','/api/file?id='+ident,headers={**headers,'Range':'bytes=2-5'})
        self.assertEqual((status,data),(206,b'dio '));self.assertEqual(response['Content-Range'],'bytes 2-5/13')
        status,_,data=request('GET','/api/archive?id='+run,headers=headers)
        self.assertEqual(status,200);self.assertTrue(data.startswith(b'PK'))

if __name__=='__main__':unittest.main()
