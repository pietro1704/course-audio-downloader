"""Execute document discovery and source guards in JavaScript, without a live account."""
import json
import subprocess
import unittest
from unittest.mock import patch
from document_browser import DocumentBrowser

class DocumentBrowserTests(unittest.TestCase):
    def browser(self,origin='https://alpha.example.test'):
        p=dict(mode='direct',origin=origin,browser_target=dict(window_id=42,tab_index=7),
               identity_expressions={'lesson':'window.lessonId'},auth_expression='window.authenticated',
               documents=dict(items_expression='window.files',ready_expression='window.panelReady',
                              panel_script="document.querySelector('button').dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0}))"))
        lesson=dict(title='Lesson',url=origin+'/lesson/1',page_identity={'lesson':10})
        page=dict(url=lesson['url'],canonical='',title='Lesson',ready='complete',auth=True,identities={'lesson':10})
        return DocumentBrowser(profile=p,timeout=5),lesson,page
    def evaluate(self,script,page,changed=None):
        env=dict(url=page['url'],title=page['title'],lessonId=10,authenticated=True)
        env.update(changed or {})
        js='const e='+json.dumps(env)+''';global.location={href:e.url};global.window={lessonId:e.lessonId,authenticated:e.authenticated,panelReady:false,files:[{id:'document-1',name:'Original.pdf',url:'https://assets.example.test/original.pdf'}]};global.MouseEvent=class{constructor(type){this.type=type}};global.document={title:e.title,querySelector:s=>s==='button'?{dispatchEvent:event=>{if(event.type==='mousedown')window.panelReady=true}}:null};let output;try{output={value:eval(SCRIPT),panelReady:window.panelReady}}catch(error){output={error:error.message,panelReady:window.panelReady}};process.stdout.write(JSON.stringify(output));'''.replace('SCRIPT',json.dumps(script))
        r=subprocess.run(['node'],input=js,text=True,capture_output=True,timeout=5)
        self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
    def test_two_unrelated_profiles_and_mousedown_panel(self):
        for origin in ['https://alpha.example.test','https://omega.example.test']:
            a,lesson,page=self.browser(origin);scripts=[]
            def run(script):
                scripts.append(script)
                # Each request runs in a new test JS realm; model the panel's mounted state on inventory read.
                if 'items:' in script:script=script.replace('window.panelReady','true')
                out=self.evaluate(script,page);self.assertNotIn('error',out)
                return json.loads(out['value'])
            with patch.object(a,'_eval',side_effect=[{},page,page]),patch.object(a,'_script',side_effect=run),patch('document_browser.time.sleep'):
                rows=a.inventory(lesson)
            self.assertEqual(rows[0]['name'],'Original.pdf');self.assertIn('mousedown',scripts[0])
            self.assertNotIn('.click()',scripts[0])
            self.assertEqual(a.url_for(lesson,rows[0]),rows[0]['url'])
    def test_changed_identity_title_auth_or_url_never_opens_panel(self):
        a,lesson,page=self.browser();scripts=[]
        with patch.object(a,'_eval',side_effect=[{},page,page]),patch.object(a,'_script',side_effect=lambda s:scripts.append(s) or {'ready':True,'items':[]}),patch('document_browser.time.sleep'):
            a.inventory(lesson)
        for changed in [dict(url='https://other.example.test/lesson/1'),dict(lessonId=11),dict(title='Other'),dict(authenticated=False)]:
            out=self.evaluate(scripts[0],page,changed)
            self.assertEqual(out['error'],'document-page-changed');self.assertFalse(out['panelReady'])
    def test_login_and_cancel_never_touch_controls(self):
        a,lesson,page=self.browser();a.profile['login_paths']=['/login']
        with patch.object(a,'_eval',side_effect=[{},dict(page,url=a.origin+'/login')]),patch.object(a,'_script') as script:
            with self.assertRaisesRegex(RuntimeError,'login-required'):a.inventory(lesson)
            script.assert_not_called()
        with patch.object(a,'_eval',side_effect=KeyboardInterrupt()),patch.object(a,'_script') as script:
            with self.assertRaises(KeyboardInterrupt):a.inventory(lesson)
            script.assert_not_called()
    def test_default_anchor_discovery_uses_original_download_name(self):
        a,lesson,page=self.browser();a.profile['documents']={};a.profile['identity_expressions']={}
        def run(script):
            js='global.location={href:'+json.dumps(page['url'])+'};global.window={authenticated:true};global.document={title:"Lesson",querySelector:()=>null,querySelectorAll:()=>[{href:"https://assets.example.test/file.pdf",hasAttribute:()=>true,getAttribute:()=>"Original – File.pdf"}]};process.stdout.write('+script+');'
            r=subprocess.run(['node'],input=js,text=True,capture_output=True,timeout=5);self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
        with patch.object(a,'_eval',side_effect=[{},page,page]),patch.object(a,'_script',side_effect=run),patch('document_browser.time.sleep'):
            self.assertEqual(a.inventory(lesson)[0]['name'],'Original – File.pdf')
