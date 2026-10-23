import http.cookiejar
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from authenticated_browser import CookieBrowser,cookie_sessions,browser_documents
from course_discovery import discover
from portable_lock import flock,LOCK_EX,LOCK_NB
from local_paths import component


def cookie(name,value,domain):
    return http.cookiejar.Cookie(0,name,value,None,False,domain,True,domain.startswith('.'),'/',True,False,None,True,None,None,{},False)

class SessionTests(unittest.TestCase):
    def test_cookie_access_is_scoped_to_course_and_parent_domain(self):
        seen=[]
        def reader(domain_name):
            seen.append(domain_name)
            return [cookie('session','private','school.example.com'),cookie('shared','private','.example.com'),cookie('other','private','.unrelated.com')]
        with patch.dict(os.environ,{'COURSE_BROWSER':'chrome'}):sessions=list(cookie_sessions('https://school.example.com/course',readers={'chrome':reader}))
        self.assertEqual(seen,['example.com']);self.assertEqual([v['name'] for v in sessions[0][1]],['session','shared'])
    def test_reader_failure_does_not_export_credentials_or_invent_session(self):
        def broken(**kwargs):raise RuntimeError('private credential error')
        with patch.dict(os.environ,{'COURSE_BROWSER':'firefox'}):self.assertEqual(list(cookie_sessions('https://school.example.com/course',readers={'firefox':broken})),[])
    def test_utf8_metadata_roundtrip_on_every_platform(self):
        from cloud_delivery import atomic_json
        from pipeline import Pipeline
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.json';value=dict(title='課程 — ação',source_identity='one',status='ok')
            atomic_json(path,value)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),value)
            manifest=Path(root)/'pipeline-manifest.jsonl';manifest.write_text(json.dumps(value,ensure_ascii=False)+'\n',encoding='utf-8')
            self.assertEqual(Pipeline.records(SimpleNamespace(manifest=manifest))['one']['title'],value['title'])
    def test_portable_paths_and_exclusive_lock(self):
        self.assertEqual(component('07. Voice: Why?'),'07. Voice_ Why_')
        self.assertEqual(component('CON'),'_CON');self.assertLessEqual(len(component('á'*250).encode()),180)
        with tempfile.TemporaryDirectory() as root:
            with open(Path(root)/'lock','a') as first,open(Path(root)/'lock','a') as second:
                flock(first,LOCK_EX|LOCK_NB)
                with self.assertRaises(BlockingIOError):flock(second,LOCK_EX|LOCK_NB)

class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        class Page(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.server.received.append(self.headers.get('Cookie',''));self.server.paths.append(self.path)
                if self.path=='/course':
                    body='<h1>Fixture</h1><img src="/tracking.png"><a href="https://other.example/lesson/999">External</a><a href="/logout">Logout</a><input type=password style="display:none"><a class=lesson href="/lesson/1"><span class=title>01. First</span></a>'
                elif self.path=='/login':body='<input type=password><h1>Login</h1>'
                else:body='<h1>First</h1><a download="notes.pdf" href="https://files.example.test/notes.pdf">PDF</a>'
                raw=body.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Page);self.server.received=[];self.server.paths=[]
        thread=threading.Thread(target=self.server.serve_forever);thread.start()
        self.addCleanup(thread.join);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.runtime=CookieBrowser(self.origin,[dict(name='session',value='synthetic',domain='127.0.0.1',path='/')])
        self.addCleanup(self.runtime.close)
        self.profile=dict(origin=self.origin,mode='direct',title_selector='h1',lesson_path_pattern=r'/lesson/\d+',
            _browser_runtime=self.runtime,course=dict(entry_path_pattern=r'/(?:course|login)',title_selector='h1',links_selector='a.lesson',lesson_title_selector='.title',duration_selector='.duration'))
    def test_real_renderer_discovers_with_cookies_across_threads_without_safari(self):
        # The local HTTP fixture isolates renderer/session behavior from HTTPS validation.
        with patch('browser_adapter.SafariAdapter._osa',side_effect=AssertionError('Safari used')),patch('browser_adapter.SafariAdapter._check_url'):
            course,_=discover(self.origin+'/course',profile=self.profile,browser=browser_documents(self.profile))
            self.assertEqual(len(course['lessons']),1)
            self.assertEqual(course['lessons'][0]['attachments'][0]['name'],'notes.pdf')
            result=[]
            worker=threading.Thread(target=lambda:result.append(self.runtime.evaluate('document.title')))
            worker.start();worker.join(5);self.assertFalse(worker.is_alive());self.assertEqual(result,[''])
        self.assertTrue(any('session=synthetic' in h for h in self.server.received))
        self.assertNotIn('/tracking.png',self.server.paths)
        with self.runtime.urlopen(self.origin+'/lesson/1') as response:self.assertEqual(response.status,200)
        self.assertIn('session=synthetic',self.server.received[-1])
    def test_generic_curriculum_ignores_unrelated_navigation_and_foreign_links(self):
        self.profile['generic']=True;self.profile['course']['links_selector']='a[href]';self.profile['course']['lesson_title_selector']=''
        with patch('browser_adapter.SafariAdapter._check_url'):
            course,_=discover(self.origin+'/course',profile=self.profile,browser=browser_documents(self.profile))
        self.assertEqual(len(course['lessons']),1)
        self.assertEqual(course['lessons'][0]['title'],'First')
    def test_visible_login_and_foreign_navigation_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError,'login-required'):
            discover(self.origin+'/login',profile=self.profile,browser=browser_documents(self.profile),timeout=2)
        with self.assertRaisesRegex(ValueError,'foreign-course-lesson'):self.runtime.navigate('https://unrelated.example/course')
        self.runtime.close()
        with self.assertRaisesRegex(RuntimeError,'closed'):self.runtime.evaluate('1')

if __name__=='__main__':unittest.main()
