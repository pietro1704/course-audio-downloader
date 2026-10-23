from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
from automatic_site import AutomaticAdapter,AutomaticDocuments,structured_media
from authenticated_browser import CookieBrowser
from course_discovery import automatic_profile,discover

class AutomaticSiteTests(unittest.TestCase):
    def setUp(self):
        class Page(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=='/course':
                    body='''<title>Program | Site</title><meta property="og:title" content="Example program">
                    <a href="/lessons">Lesson listing</a><a href="https://external.example/lesson/9">Other</a>
                    <section class=module><h3>Week A</h3>
                    <a href="/lesson/1"><span class=title>01. First</span><span>02:00</span></a>
                    <a href="/lesson/2"><span class=title>02. Second</span></a></section>
                    <section class=module><h3>Week B</h3><a href="/lesson/3"><span class=title>01. Third</span></a></section>'''
                elif self.path=='/lesson/entry':
                    body='<title>First | Site</title><a href="/program"><span>Example program</span></a><section><h3>About Example program</h3><a href="/lesson/entry">01. First</a></section><section><h3>Week B</h3><a href="/lesson/2">02. Second</a></section>'
                else:
                    title={'/lesson/1':'First','/lesson/2':'Second','/lesson/3':'Third'}[self.path]
                    body=f'''<title>{title} | Site</title><h1>{title}</h1>
                    <a class=downloads href="https://files.example/audio/opaque">Audio lesson (MP3)</a>
                    <a class=attachment download="notes.pdf" href="https://files.example/notes.pdf">Notes</a>
                    <a href="https://files.example/privacy.pdf">Privacy policy</a>
                    <a download href="javascript:void(0)">Placeholder</a><a download href="#">Empty</a>'''
                raw=body.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Page)
        thread=threading.Thread(target=self.server.serve_forever);thread.start()
        self.addCleanup(thread.join);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        origin='http://127.0.0.1:'+str(self.server.server_port)
        self.runtime=CookieBrowser(origin,[]);self.addCleanup(self.runtime.close)
        self.profile=automatic_profile('https://courses.example/course');self.profile.update(origin=origin,_browser_runtime=self.runtime)
        self.url=origin+'/course'
    def test_real_generic_curriculum_modules_metadata_and_only_lesson_attachments(self):
        with patch('browser_adapter.SafariAdapter._osa',side_effect=AssertionError('native browser used')):
            course,_=discover(self.url,profile=self.profile,browser=AutomaticDocuments(self.profile))
        self.assertEqual(course['title'],'Example program');self.assertEqual(len(course['lessons']),3)
        self.assertEqual([m['title'] for m in course['modules_info']],['Week A','Week B'])
        for lesson in course['lessons']:
            self.assertEqual([a['name'] for a in lesson['attachments']],['notes.pdf'])
    def test_lesson_entry_infers_course_from_navigation_and_curriculum_structure(self):
        from automatic_site import CURRICULUM_SCRIPT
        self.runtime.navigate(self.profile['origin']+'/lesson/entry')
        page=self.runtime.evaluate(CURRICULUM_SCRIPT)
        self.assertEqual(page['title'],'Example program');self.assertEqual(len(page['lessons']),2)
    def test_direct_mp3_is_preferred_to_video_without_fetching_the_player(self):
        adapter=AutomaticAdapter(self.profile)
        with patch.object(adapter.browser,'page',return_value=dict(direct=[dict(url='https://files.example/opaque',label='Audio (MP3)')],frames=['https://video.example/embed'])),patch.object(adapter,'_text',side_effect=AssertionError('video fetched')):
            candidates=adapter.resolve(dict(url='https://courses.example/lesson/1',title='First'))
        self.assertEqual(candidates[0].kind,'mp3')
    def test_structured_player_media_is_course_neutral_and_rejects_drm(self):
        payload={'props':{'assets':[{'url':'https://media.example/audio.m3u8'}],'isDrmEnabled':False}}
        html='<script type="application/json">'+json.dumps(payload)+'</script>'
        self.assertEqual(structured_media(html),['https://media.example/audio.m3u8'])
        self.assertEqual(structured_media('<audio src="/audio/lesson.mp3"></audio>'),['/audio/lesson.mp3'])
        payload['props']['isDrmEnabled']=True
        with self.assertRaisesRegex(RuntimeError,'drm-media-not-supported'):structured_media('<script type="application/json">'+json.dumps(payload)+'</script>')
        with self.assertRaisesRegex(RuntimeError,'drm-media-not-supported'):structured_media('<media-player drm-enabled="true" content-src="https://media.example/audio.m3u8">')

if __name__=='__main__':unittest.main()
