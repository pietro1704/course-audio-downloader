"""In-memory browser cookies and a platform-neutral renderer owned by the CLI."""
import atexit
from concurrent.futures import Future
import http.cookiejar
import json
import multiprocessing
import signal
import urllib.request
import os
import queue
import re
import threading
from urllib.parse import urlsplit

BROWSERS=('chrome','chromium','edge','brave','firefox','librewolf','opera','opera_gx','vivaldi','safari')


def _read_cookies(name,domain,connection):
    if os.name=='posix':os.setsid()
    try:
        import browser_cookie3
        jar=getattr(browser_cookie3,name)(domain_name=domain)
        connection.send(list(jar))
    except Exception:connection.send([])
    finally:connection.close()


def read_cookies(name,domain,timeout=6):
    # Bound OS keychain prompts and locked profile stores. No application cookie exports are written.
    context=multiprocessing.get_context('spawn')
    receiver,sender=context.Pipe(duplex=False)
    process=context.Process(target=_read_cookies,args=(name,domain,sender),daemon=True)
    process.start();sender.close()
    try:
        if receiver.poll(timeout):
            try:return receiver.recv()
            except EOFError:return []
        return []
    finally:
        receiver.close()
        process.join(timeout=.2)
        if process.is_alive():
            if os.name=='posix':
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
            process.terminate();process.join(timeout=2)


def cookie_sessions(url,readers=None):
    """Read only the requested site's cookie domain, never serialize credentials."""
    import browser_cookie3
    host=urlsplit(url).hostname
    if not host:raise ValueError('invalid-course-url')
    # Parent-domain cookies are also valid for a site's subdomain.
    from publicsuffix2 import get_sld
    domain=get_sld(host) or host
    selected=os.environ.get('COURSE_BROWSER')
    import sys
    names=(selected,) if selected else (('safari',)+BROWSERS[:-1] if sys.platform=='darwin' else BROWSERS)
    for name in names:
        reader=(readers or {}).get(name) if readers is not None else getattr(browser_cookie3,name,None)
        if reader is None:continue
        try:jar=reader(domain_name=domain) if readers is not None else read_cookies(name,domain)
        except Exception:continue
        cookies=[]
        for cookie in jar:
            owner=cookie.domain.lstrip('.').lower()
            if not owner or not (host==owner or host.endswith('.'+owner)) or cookie.is_expired():continue
            value=dict(name=cookie.name,value=cookie.value,domain=cookie.domain,path=cookie.path or '/',secure=cookie.secure)
            if cookie.expires is not None:value['expires']=cookie.expires
            value['httpOnly']=any(k.lower()=='httponly' for k in cookie._rest)
            cookies.append(value)
        if cookies:yield name,cookies


class CookieBrowser:
    """All Playwright operations stay on one owner thread, including web requests."""
    def __init__(self,origin,cookies,*,engine='chromium'):
        self.origin=origin;self.cookies=cookies;self.engine=engine
        self.commands=queue.Queue();self.closed=False
        jar=http.cookiejar.CookieJar()
        for c in cookies:
            jar.set_cookie(http.cookiejar.Cookie(0,c['name'],c['value'],None,False,c['domain'],
                c['domain'].startswith('.'),c['domain'].startswith('.'),c['path'],True,c.get('secure',False),
                c.get('expires'),c.get('expires') is None,None,None,{},False))
        self.jar=jar
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.thread=threading.Thread(target=self._worker,daemon=True);self.thread.start()
        atexit.register(self.close)

    def _worker(self):
        manager=browser=context=page=None
        try:
            while True:
                command,args,future=self.commands.get()
                if command=='close':break
                try:
                    if page is None:
                        from playwright.sync_api import sync_playwright
                        manager=sync_playwright().start()
                        browser=getattr(manager,self.engine).launch(headless=True,args=['--disable-gpu'] if self.engine=='chromium' else [])
                        context=browser.new_context(accept_downloads=False);context.add_cookies(self.cookies)
                        context.route('**/*',lambda route:route.abort() if (
                            route.request.resource_type in {'media','font','image'} or
                            re.search(r'\.(?:mp[34]|m4[asv]|m3u8|ts|aac|ogg|webm)$',urlsplit(route.request.url).path,re.I))
                            else route.continue_())
                        self.cookies=[]
                        page=context.new_page()
                    if command=='navigate':
                        parsed=urlsplit(args[0])
                        if parsed.scheme+'://'+parsed.netloc!=self.origin:raise ValueError('foreign-course-lesson')
                        page.goto(args[0],wait_until='domcontentloaded',timeout=45000)
                        result=None
                    elif command=='evaluate':
                        parsed=urlsplit(page.url)
                        if parsed.scheme+'://'+parsed.netloc!=self.origin:raise RuntimeError('login-required')
                        result=page.evaluate(args[0])
                    else:raise ValueError('invalid-browser-command')
                    future.set_result(result)
                except Exception as error:
                    # Browser errors can contain cookies, page content or signed URLs.
                    if isinstance(error,(RuntimeError,ValueError)) and str(error) in {'login-required','foreign-course-lesson'}:
                        future.set_exception(error)
                    else:future.set_exception(RuntimeError('authenticated-browser-failed'))
        finally:
            if context is not None:context.close()
            if browser is not None:browser.close()
            if manager is not None:manager.stop()

    def call(self,command,*args):
        if self.closed:raise RuntimeError('authenticated-browser-closed')
        result=Future();self.commands.put((command,args,result));
        try:return result.result(timeout=90)
        except TimeoutError:raise RuntimeError('authenticated-browser-timeout') from None

    def urlopen(self,*args,**kwargs):return self.opener.open(*args,**kwargs)

    def navigate(self,url):return self.call('navigate',url)
    def evaluate(self,script):return self.call('evaluate',script)

    def close(self):
        if self.closed:return
        self.closed=True;self.cookies=[];self.jar.clear();self.commands.put(('close',(),None));self.thread.join(timeout=10)
        atexit.unregister(self.close)


class CookieAdapterMixin:
    def resolve(self,value):
        try:return super().resolve(value)
        except RuntimeError as error:
            if str(error)=='Safari-lesson-identity-timeout':raise RuntimeError('lesson-identity-timeout') from None
            if str(error) in {'Safari-unavailable','Safari-navigation-failed'}:raise RuntimeError('authenticated-browser-failed') from None
            raise

    def urlopen(self,*args,**kwargs):return self.profile['_browser_runtime'].urlopen(*args,**kwargs)

    def _eval(self,navigate=None,**kwargs):
        from site_profile import inspection_script
        runtime=self.profile['_browser_runtime']
        if navigate:
            runtime.navigate(navigate);return {}
        return self._script(inspection_script(self.profile))

    def _script(self,script):
        value=self.profile['_browser_runtime'].evaluate(script)
        return json.loads(value) if isinstance(value,str) else value


def browser_documents(profile):
    if profile.get('mode')=='automatic':
        from automatic_site import AutomaticDocuments
        return AutomaticDocuments(profile)
    from document_browser import DocumentBrowser
    class Documents(CookieAdapterMixin,DocumentBrowser):pass
    return Documents(profile=profile)


def cookie_adapter(cls,profile,encoder):
    class Adapter(CookieAdapterMixin,cls):pass
    return Adapter(profile=profile,encoder=encoder)
