"""Discover a provider course automatically, independently of local/manual catalogues."""
import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit
from course_catalog import path_parts
from documents import document_resource,validate_document_paths
from pipeline import Pipeline
from local_paths import component


def automatic_profile(url):
    value=urlsplit(url)
    if value.scheme!='https' or value.username or value.password or not value.hostname:
        raise ValueError('invalid-course-url')
    return dict(origin=value.scheme+'://'+value.netloc,mode='automatic',generic=True,
                lesson_path_pattern=r'/.*',course=dict(entry_path_pattern=r'/.*',automatic=True))


def parse_course(page,profile):
    origin=profile['origin'].rstrip('/')
    title=page.get('title','').strip()
    if not title:raise ValueError('missing-course-title')
    lessons=[];seen=set();modules=[];groups={}
    for raw in page.get('lessons',[]):
        url=raw.get('url','');p=urlsplit(url)
        if p.scheme+'://'+p.netloc!=origin or p.username or p.password:
            raise ValueError('foreign-course-lesson')
        if not re.fullmatch(profile['lesson_path_pattern'],p.path):continue
        ident=Pipeline.identity(url)
        if ident in seen:continue
        seen.add(ident)
        label=raw.get('title','').strip()
        if not label:raise ValueError('missing-lesson-title')
        match=re.match(r'^(\d+)\.\s*(.+)$',label)
        lesson_title=match[2] if match else label
        module=(raw.get('module') or '').strip()
        raw_parts=path_parts([title,*([module] if module else []),label])
        parts=[component(p) for p in raw_parts]
        lesson=dict(id=ident,url=url,title=lesson_title,display_title=label,
            number=int(match[1]) if match else len(lessons)+1,site_path=parts,duration=raw.get('duration',''))
        if module:
            module_id=hashlib.sha256((origin+'\n'+title+'\n'+module).encode()).hexdigest()
            lesson['module_id']=module_id
            if module_id not in groups:
                entry=dict(id=module_id,title=module,lesson_ids=[]);groups[module_id]=entry;modules.append(entry)
            groups[module_id]['lesson_ids'].append(ident)
        lessons.append(lesson)
    if not lessons:raise ValueError('no-course-lessons-found')
    paths=[unicodedata.normalize('NFD',str(path_parts(x['site_path']))).casefold() for x in lessons]
    if len(set(paths))!=len(paths):raise ValueError('duplicate-course-path')
    course_id=hashlib.sha256((origin+'\n'+'\n'.join(sorted(x['url'] for x in lessons))).encode()).hexdigest()
    return dict(schema='course-catalog/v1',id=course_id,title=title,url=page['url'],
        complete=True,expected_count=len(lessons),collected_count=len(lessons),lessons=lessons,
        modules_info=modules,description=page.get('description',''))


def collect_attachments(course,browser):
    """Expose metadata only; signed resource URLs stay in the browser/download lane."""
    for lesson in course['lessons']:
        if len(course['lessons'])>12 and lesson['url']!=course['url']:
            lesson['attachments']=[];lesson['attachments_state']='pending';continue
        resources=[r for raw in browser.inventory(lesson) if (r:=document_resource(raw)) is not None]
        validate_document_paths(resources)
        lesson['attachments']=[dict(id=hashlib.sha256(r['id'].encode()).hexdigest(),
            name=r.get('name') or r.get('label') or 'Anexo '+str(i),mime=r.get('mime',''),size=r.get('size'))
            for i,r in enumerate(resources,1)]
    return course


def discover(url,*,profile=None,browser=None,timeout=30):
    profile=dict(profile or automatic_profile(url))
    parsed=urlsplit(url)
    if (parsed.scheme+'://'+parsed.netloc!=profile['origin'].rstrip('/') or parsed.username or parsed.password
            or not re.fullmatch(profile['course']['entry_path_pattern'],parsed.path)):
        raise ValueError('unsupported-course-url')
    if browser is None:
        from authenticated_browser import CookieBrowser,cookie_sessions,browser_documents
        from itertools import chain
        last_error=None
        for source,cookies in chain(cookie_sessions(url),[('public',[])]):
            runtime=CookieBrowser(profile['origin'],cookies)
            attempt=dict(profile,_browser_runtime=runtime,_browser_source=source)
            try:return discover(url,profile=attempt,browser=browser_documents(attempt),timeout=timeout)
            except BaseException as error:
                runtime.close()
                if isinstance(error,RuntimeError) and str(error) in {'login-required','document-page-identity-timeout'}:
                    last_error=error;continue
                raise
        raise RuntimeError('login-required') from last_error
    browser._eval(url)
    settings=json.dumps(dict(profile['course'],generic=profile.get('generic',False),lesson_pattern=profile['lesson_path_pattern']),ensure_ascii=False)
    script=r'''(()=>{const p=SETTINGS;
const text=x=>x?.textContent?.trim().replace(/\s+/g,' ')||'';
const ownText=x=>x?[...x.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim()||text(x):'';
let title=text(document.querySelector(p.title_selector));
if(!title&&p.title_from_module_pattern){
 const heading=ownText(document.querySelector(p.module_title_selector));
 title=heading.match(new RegExp(p.title_from_module_pattern))?.[1]||'';
}
if(!title)title=p.fallback_to_origin?location.hostname:document.title;

return JSON.stringify({url:location.origin+location.pathname,title,ready:document.readyState,login:[...document.querySelectorAll('input[type=password]')].some(x=>x.getClientRects().length>0),
description:document.querySelector('meta[name="description"]')?.content||'',
lessons:[...document.querySelectorAll(p.links_selector)].filter(a=>!p.generic||(a.origin===location.origin&&new RegExp(p.lesson_pattern).test(a.pathname))).map(a=>{const group=p.module_selector?a.closest(p.module_selector):null;
return {url:a.href,title:text(p.lesson_title_selector?a.querySelector(p.lesson_title_selector):a),
duration:(p.duration_selector?a.querySelector(p.duration_selector)?.textContent?.trim():'')||'',
module:group&&p.module_title_selector?ownText(group.querySelector(p.module_title_selector)):''}})});})()'''.replace('SETTINGS',settings)
    if profile['course'].get('automatic'):
        from automatic_site import CURRICULUM_SCRIPT
        script=CURRICULUM_SCRIPT
    deadline=time.monotonic()+timeout;previous=None;stable=0;wrong_page=False
    while time.monotonic()<deadline:
        page=browser._script(script)
        if page.get('login'):raise RuntimeError('login-required')
        if page.get('ready')=='complete' and page.get('title') and page.get('lessons'):
            expected=urlsplit(url)
            actual=urlsplit(page['url'])
            if actual.netloc!=expected.netloc or actual.path.rstrip('/')!=expected.path.rstrip('/'):
                wrong_page=True;stable=0;previous=None;time.sleep(.25);continue
            key=json.dumps(page['lessons'],sort_keys=True)
            stable=stable+1 if key==previous else 0;previous=key
            if stable>=1:return collect_attachments(parse_course(page,profile),browser),profile
        time.sleep(.25)
    raise RuntimeError('course-page-identity-mismatch' if wrong_page else 'course-discovery-timeout')
