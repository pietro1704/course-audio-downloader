"""Source-bound document discovery driven entirely by trusted site profiles."""
import json
import subprocess
import time
from urllib.parse import urlsplit
from browser_adapter import SafariAdapter, canonical
from site_profile import identity_matches


class DocumentBrowser(SafariAdapter):
    def _script(self, script):
        target=self.profile.get('browser_target')
        if (not isinstance(target,dict) or any(type(target.get(k)) is not int or target[k]<1
                for k in ('window_id','tab_index'))):raise ValueError('documents-require-explicit-browser-target')
        command=('tell application "Safari"\nset t to tab '+str(target['tab_index'])+
                 ' of window id '+str(target['window_id'])+'\nif (URL of t) starts with '+
                 json.dumps(self.origin.rstrip('/')+'/')+' then\n'+self._activation_script()+'return do JavaScript '+
                 json.dumps(script,ensure_ascii=False)+' in t\nend if\nerror "Browser target changed"\nend tell')
        return json.loads(self._osa(command))

    def _matches(self,page,lesson):
        titles={self._title(lesson[k]) for k in ('title','page_title','provider_title') if lesson.get(k)}
        if type(lesson.get('number')) is int and lesson['number']>0:
            titles.add(self._title(str(lesson['number'])+'. '+lesson['title']))
        ready_states={'complete','interactive'} if self.profile.get('documents',{}).get('ready_expression') else {'complete'}
        return (canonical(page.get('url',''),self.profile)==canonical(lesson['url'],self.profile) and
                (not page.get('canonical') or canonical(page['canonical'],self.profile)==canonical(lesson['url'],self.profile)) and
                page.get('auth') and page.get('ready') in ready_states and
                self._title(page.get('title','')) in titles and identity_matches(page,lesson,self.profile))

    def inventory(self,lesson):
        self._check_url(lesson['url']);self._eval(lesson['url'])
        deadline=time.monotonic()+self.timeout;stable=0
        while time.monotonic()<deadline:
            try:
                page=self._eval()
                if not isinstance(page,dict):raise ValueError('invalid-document-page')
            except (RuntimeError,ValueError,subprocess.TimeoutExpired):
                stable=0;time.sleep(.5);continue
            if urlsplit(page.get('url','')).path in self.profile.get('login_paths',[]):raise RuntimeError('login-required')
            stable=stable+1 if self._matches(page,lesson) else 0
            if stable>=2:break
            time.sleep(.5)
        else:raise RuntimeError('document-page-identity-timeout')
        contract=self.profile.get('documents',{})
        # Bind the exact inspected page again inside the script, before accessing controls.
        guard='if(location.href!=='+json.dumps(page['url'])+')throw Error("document-page-changed");'
        guard+='if(!('+self.profile.get('auth_expression','true')+'))throw Error("document-page-changed");'
        guard+='if((document.querySelector(\'link[rel=canonical]\')?.href||\'\')!=='+json.dumps(page.get('canonical',''))+')throw Error("document-page-changed");'
        for key,expression in self.profile.get('identity_expressions',{}).items():
            guard+='if(JSON.stringify(('+expression+'))!=='+json.dumps(json.dumps(lesson['page_identity'][key],separators=(',',':')))+')throw Error("document-page-changed");'
        title_expression=('document.querySelector('+json.dumps(self.profile['title_selector'])+')?.textContent||\'\'' if self.profile.get('title_selector') else 'document.title')
        guard+='if(('+title_expression+')!=='+json.dumps(page['title'])+')throw Error("document-page-changed");'
        items=contract.get('items_expression')
        if not items:
            selector=json.dumps(contract.get('links_selector','a[download],a[href]'))
            items='''[...document.querySelectorAll(SELECTOR)].filter(a=>a.hasAttribute('download')||/\\.(pdf|docx?|xlsx?|pptx?|zip|txt|csv|epub)(?:$|[?#])/i.test(a.href)).map(a=>({id:new URL(a.href).origin+new URL(a.href).pathname,url:a.href,name:a.getAttribute('download')||decodeURIComponent(new URL(a.href).pathname.split('/').pop())}))'''.replace('SELECTOR',selector)
        panel=contract.get('panel_script','')
        if panel:self._script('(()=>{'+guard+panel+';return JSON.stringify(true)})()')
        ready=contract.get('ready_expression','true')
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            result=self._script('(()=>{'+guard+'return JSON.stringify({ready:!!('+ready+'),items:('+items+')})})()')
            if result['ready']:
                if not isinstance(result['items'],list):raise RuntimeError('invalid-document-inventory')
                return result['items']
            time.sleep(.5)
        raise RuntimeError('document-inventory-timeout')

    def url_for(self,lesson,resource):
        # Inventories resolve links before the next page is visited; never inspect another lesson here.
        url=resource.get('url')
        if not url:raise RuntimeError('missing-document-download-url')
        return url
