from pathlib import Path
import subprocess
import unittest

class PortalUITests(unittest.TestCase):
    def test_attachments_early_streaming_and_player_identity_in_javascript(self):
        result=subprocess.run(['node','tests/local_portal_ui.cjs'],cwd=Path(__file__).parent,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class BrowserPortalUITests(unittest.TestCase):
    def test_real_website_lists_attachments_and_streams_before_course_finishes(self):
        import os
        for engine in os.environ.get('COURSE_TEST_WEB_ENGINES','chromium').split(','):
            with self.subTest(engine=engine):self.exercise(engine)

    def exercise(self,engine):
        import tempfile
        import threading
        import shutil
        from playwright.sync_api import sync_playwright
        from local_portal import Portal,Server
        from pipeline import Pipeline
        entered=threading.Event();released=threading.Event()
        lessons=[dict(id='one',title='First',display_title='01. First',url='https://fixture.example/lesson/1',attachments=[dict(id='notes',name='notes.pdf')])]
        course=dict(id='course',title='Fixture',lessons=lessons)
        class Engine:
            def __init__(self,root,profile,protected_pauses,event,on_artifact):
                self.root=Path(root);self.root.mkdir(parents=True);self.docs=self.root/'docs';self.docs.mkdir()
                self.publish=on_artifact;self.remove=protected_pauses
            def run(self,selected):
                path=self.root/'first.m4a';shutil.copyfile(audio_source,path)
                record=dict(final=str(path),final_hash=Pipeline.sha256(path),source_identity='one')
                self.publish(record,'audio');entered.set();released.wait(10);return [record]
            def cancel(self):released.set()
        with tempfile.TemporaryDirectory() as root:
            audio_source=Path(root)/'synthetic.m4a'
            subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','anullsrc=r=44100:cl=mono','-t','1','-c:a','aac',str(audio_source)],check=True,capture_output=True)
            portal=Portal(root,discovery=lambda url:(course,{}),factory=Engine);server=Server(0,portal)
            thread=threading.Thread(target=server.serve_forever);thread.start()
            try:
                with sync_playwright() as playwright:
                    browser=getattr(playwright,engine).launch(headless=True,args=['--disable-gpu'] if engine=='chromium' else [])
                    try:
                        page=browser.new_page();page.goto(server.open_url)
                        page.locator('#course-url').fill('https://fixture.example/course')
                        page.locator('#load-course').click()
                        page.locator('#course-title').wait_for(state='visible')
                        self.assertIn('notes.pdf',page.locator('#lesson-list').inner_text())
                        self.assertTrue(page.locator('#silence-removal').is_checked())
                        page.locator('#download-course').click();self.assertTrue(entered.wait(5))
                        page.locator('audio').wait_for(state='visible')
                        page.locator('audio').evaluate("audio=>{audio.preload='auto';audio.load()}")
                        page.locator('audio').evaluate('''audio=>new Promise((resolve,reject)=>{
                            const timer=setTimeout(()=>reject(Error('audio-timeout')),8000);
                            const ready=()=>{clearTimeout(timer);resolve(true)};
                            const failed=()=>{clearTimeout(timer);reject(Error('audio-decode-failed'))};
                            if(audio.error)failed();else if(audio.readyState>=3)ready();
                            else{audio.addEventListener('canplay',ready,{once:true});audio.addEventListener('error',failed,{once:true});}
                        })''')
                        self.assertGreater(page.locator('audio').evaluate('audio=>audio.duration'),.9)
                        self.assertFalse(portal.state()['job']['ready']);self.assertTrue(portal.engine.remove)
                        self.assertTrue(page.locator('#archive').is_hidden())
                        result=page.evaluate("""async()=>{const r=await fetch(document.querySelector('audio').src,{headers:{Range:'bytes=0-7'}});return {status:r.status,bytes:(await r.arrayBuffer()).byteLength}}""")
                        self.assertEqual(result,dict(status=206,bytes=8))
                        page.evaluate('window.playingNode=document.querySelector("audio")')
                        released.set();portal.thread.join(5)
                        page.locator('#archive').wait_for(state='visible')
                        self.assertTrue(page.evaluate('window.playingNode===document.querySelector("audio")'))
                    finally:browser.close()
            finally:
                released.set();portal.close();server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
