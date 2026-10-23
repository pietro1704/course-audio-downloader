"""Course-neutral curriculum, lesson and attachment discovery from authenticated pages."""
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin
import urllib.request
from browser_adapter import SafariAdapter
from pipeline import Candidate


CURRICULUM_SCRIPT=r'''(()=>{
const clean=x=>(x?.textContent||'').replace(/\s+/g,' ').trim();
const own=x=>[...x.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').replace(/\s+/g,' ').trim()||clean(x);
const route=/\/(?:aulas?|lessons?|lectures?|units?|classes|chapters?|mod\/(?:page|resource|url|scorm))(?:\/[^/?#]+|\.php$)/i;
const titleSelector='h1,h2,h3,h4,h5,h6,[class*=title],[class*=titulo],[class*=name],[class*=nome]';
const titleOf=a=>{
 const nodes=[...a.querySelectorAll(titleSelector)].filter(n=>!n.querySelector(titleSelector));
 return clean(nodes[0]||a).replace(/\s+\d{1,2}:\d{2}(?::\d{2})?$/,'');
};
const links=[...document.querySelectorAll('a[href]')].filter(a=>a.origin===location.origin&&route.test(a.pathname)&&titleOf(a));
const all=[...new Map(links.map(a=>{const u=new URL(a.href);u.hash='';return [u.href,a]})).values()];
const containsLink=n=>all.some(a=>n===a||n.contains(a));
const heading=n=>[...n.querySelectorAll(titleSelector)].filter(x=>!x.closest('a[href]')&&!x.querySelector(titleSelector)).map(own).find(t=>t&&t.length<160)||'';
const moduleFor=a=>{
 for(let n=a.parentElement;n&&n!==document.body;n=n.parentElement){
  if(all.every(x=>n.contains(x)))break;
  if([...n.parentElement.children].some(s=>s!==n&&containsLink(s))){const t=heading(n);if(t)return t;}
 }
 return '';
};
const lessons=all.map(a=>{const u=new URL(a.href);u.hash='';return {url:u.href,title:titleOf(a),module:moduleFor(a),duration:clean(a.querySelector('[class*=duration],[class*=duracao],time'))}});
const labels=lessons.map(l=>l.title.replace(/^\d+[.)]\s*/,''));
const valid=t=>t&&t.length<180&&!labels.includes(t.replace(/^\d+[.)]\s*/,''));
let title=!route.test(location.pathname)?document.querySelector('meta[property="og:title"],meta[name="twitter:title"]')?.content||'':'';
const courseSelector='[data-course-title],[data-course-name],[class*=course][class*=title],[class*=course][class*=name],[class*=curso][class*=titulo],[class*=curso][class*=nome]';
if(!title)title=[...document.querySelectorAll(courseSelector+(route.test(location.pathname)?'':',h1,[class*=title],[class*=titulo]'))].filter(x=>!x.closest('a[href],nav,button,[role=dialog],footer')).map(clean).find(valid)||'';
if(!title)title=[...document.querySelectorAll('a[href]')].filter(a=>/\/(?:courses?|cursos?)\//i.test(a.pathname)).map(clean).find(valid)||'';
if(!title){
 const groups=lessons.map(l=>l.module).filter(Boolean);
 title=[...document.querySelectorAll('body *')].filter(n=>!n.children.length&&!['SCRIPT','STYLE','NOSCRIPT'].includes(n.tagName)&&!all.some(a=>a===n||a.contains(n))).map(clean)
  .filter(t=>t.length>=5&&t.length<120&&groups.some(g=>g!==t&&g.includes(t))).sort((a,b)=>b.length-a.length)[0]||'';
}
if(!title&&!route.test(location.pathname))title=document.title;
if(!title)title=location.hostname;
return {url:location.origin+location.pathname+location.search,title,lessons,ready:document.readyState,
 description:document.querySelector('meta[name=description]')?.content||'',
 login:[...document.querySelectorAll('input[type=password]')].some(x=>x.getClientRects().length>0)};
})()'''


PAGE_SCRIPT=r'''(()=>{
const clean=x=>(x?.textContent||'').replace(/\s+/g,' ').trim();
const absolute=value=>{try{return new URL(value,location.href).href}catch{return ''}};
const related=a=>{for(let n=a,i=0;n&&i<3;n=n.parentElement,i++){if(/download|attach|anexo|material|resource/i.test(n.className+' '+n.id))return true}return false};
const links=[...document.querySelectorAll('a[href],audio[src],audio source[src]')].map(a=>({attachment:related(a),url:absolute(a.currentSrc||a.src||a.href),label:clean(a),download:a.getAttribute('download'),media:['AUDIO','SOURCE'].includes(a.tagName)}));
const audio=/\.(?:mp3|m4a|aac|wav|ogg|opus)(?:$|[?#])/i;
const direct=links.filter(a=>a.media||audio.test(a.url)||(/mp3/i.test(a.label))||(/(?:audio|áudio)/i.test(a.label)&&a.download!==null));
const documents=links.filter(a=>a.url.startsWith('https://')&&a.url.split('#')[0]!==location.href.split('#')[0]&&!direct.includes(a)&&((a.attachment||/^(?:pdf|download|baixar|anexo|material|notes|handout)\b/i.test(a.label))&&/\.(?:pdf|docx?|xlsx?|pptx?|zip|txt|csv|epub)(?:$|[?#])/i.test(a.url)||a.download!==null||/\/(?:attachment|anexo|arquivo|file|download)(?:\/|[?])/i.test(a.url))).map(a=>{
 const u=new URL(a.url);const cd=u.searchParams.get('response-content-disposition')||'';
 const filename=cd.match(/filename="([^"]+)"/)?.[1]||a.download||'';
 const n=u.searchParams.get('n')||'';
 const name=filename||(/\.(?:pdf|docx?|xlsx?|pptx?|zip|txt|csv|epub)$/i.test(n)?n:null);
 const identity=u.origin+u.pathname+'?'+[...u.searchParams].filter(([k])=>! /^(?:signature|expires|t|x-amz-.*)$/i.test(k)).sort().map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(v)).join('&');
 return {id:identity,url:a.url,name,label:a.label.replace(/\s+[\d.,]+\s*[kmg]?b$/i,'')};
});
return {url:location.origin+location.pathname+location.search,title:document.title,ready:document.readyState,
 headings:[...document.querySelectorAll('h1,h2,[role=heading]')].filter(x=>!x.closest('a[href],nav,aside')).map(clean),
 direct,documents,frames:[...document.querySelectorAll('iframe[src]')].map(x=>absolute(x.src)),
 poster:document.querySelector('video[poster]')?.poster||null,
 login:[...document.querySelectorAll('input[type=password]')].some(x=>x.getClientRects().length>0)};
})()'''


def normalized(value):
    return re.sub(r'^\d+[.)]\s*','',re.sub(r'\s+',' ',value).strip()).casefold()


def matches_title(page,lesson):
    expected={normalized(lesson[k]) for k in ('title','display_title','page_title','provider_title') if lesson.get(k)}
    actual=[normalized(page.get('title','')),*map(normalized,page.get('headings',[]))]
    return any(a==e or any(a.startswith(e+separator) for separator in (' - ',' | ',' – ',' — ')) for a in actual for e in expected if e)


class AutomaticDocuments:
    def __init__(self,profile,timeout=45):self.profile=profile;self.runtime=profile['_browser_runtime'];self.timeout=timeout
    def _eval(self,navigate=None):
        if navigate:self.runtime.navigate(navigate);return {}
        return self._script(PAGE_SCRIPT)
    def _script(self,script):
        value=self.runtime.evaluate(script);return json.loads(value) if isinstance(value,str) else value
    def urlopen(self,*args,**kwargs):return self.runtime.urlopen(*args,**kwargs)
    def page(self,lesson,need_audio=False):
        self._eval(lesson['url']);deadline=time.monotonic()+self.timeout;previous=None;stable=0
        while time.monotonic()<deadline:
            page=self._script(PAGE_SCRIPT)
            if page.get('login'):raise RuntimeError('login-required')
            if page.get('url')==lesson['url'].split('#')[0] and page.get('ready')=='complete' and matches_title(page,lesson):
                if not need_audio or page.get('direct') or page.get('frames'):
                    key=json.dumps([page.get('title'),page.get('direct'),page.get('documents'),page.get('frames')],sort_keys=True)
                    stable=stable+1 if key==previous else 0;previous=key
                    if stable>=1:return page
            else:stable=0;previous=None
            time.sleep(.25)
        raise RuntimeError('lesson-identity-timeout')
    def inventory(self,lesson):return self.page(lesson)['documents']
    def url_for(self,lesson,resource):return resource['url']


class MediaData(HTMLParser):
    def __init__(self):super().__init__();self.scripts=[];self.urls=[];self.capture=False;self.data=[];self.protected=False
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if any('drm' in key.casefold() and value in {'true','1'} for key,value in attrs.items()):self.protected=True
        if tag=='script' and (attrs.get('type') in {'application/json','application/ld+json'} or attrs.get('id')=='__NEXT_DATA__'):
            self.capture=True;self.data=[]
        for key in ('src','content-src'):
            if attrs.get(key) and re.search(r'\.(?:mp3|m4a|aac|ogg|m3u8)(?:$|[?#])',attrs[key],re.I):self.urls.append(attrs[key])
    def handle_data(self,data):
        if self.capture:self.data.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.capture:
            self.scripts.append(''.join(self.data));self.capture=False


def structured_media(html):
    parser=MediaData();parser.feed(html)
    if parser.protected:raise RuntimeError('drm-media-not-supported')
    urls=list(parser.urls);stack=[]
    for script in parser.scripts:
        try:stack.append(json.loads(script))
        except ValueError:continue
    visited=0
    while stack:
        value=stack.pop();visited+=1
        if visited>50000:raise RuntimeError('oversized-manifest')
        if isinstance(value,dict):
            if any('drm' in key.casefold() and item is True for key,item in value.items()):raise RuntimeError('drm-media-not-supported')
            stack.extend(reversed(list(value.values())))
        elif isinstance(value,list):stack.extend(reversed(value))
        elif isinstance(value,str) and (value.startswith('https://') or value.startswith('/')) and re.search(r'\.(?:mp3|m4a|aac|ogg|m3u8)(?:$|[?#])',value,re.I):urls.append(value)
    return list(dict.fromkeys(urls))


class AutomaticAdapter(SafariAdapter):
    def __init__(self,profile,encoder='aac',**kwargs):
        super().__init__(profile=profile,encoder=encoder,**kwargs);self.browser=AutomaticDocuments(profile)
    def urlopen(self,*args,**kwargs):return self.browser.urlopen(*args,**kwargs)
    def _text(self,url,referer):
        if urlsplit(url).scheme!='https':raise RuntimeError('unsafe-player-media-url')
        request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':referer})
        try:
            with self.urlopen(request,timeout=45) as response:
                raw=response.read(8*1024**2+1)
                if len(raw)>8*1024**2:raise RuntimeError('oversized-manifest')
                return raw.decode('utf-8')
        except Exception:raise RuntimeError('player-fetch-failed') from None
    def resolve(self,value):
        lesson={'url':value} if isinstance(value,str) else value
        page=self.browser.page(lesson,need_audio=True)
        direct=[x for x in page['direct'] if urlsplit(x['url']).scheme=='https']
        if direct:
            direct.sort(key=lambda x:not bool(re.search(r'mp3',x['label']+' '+urlsplit(x['url']).path,re.I)))
            return [Candidate(x['url'],'mp3' if re.search('mp3',x['label']+' '+x['url'],re.I) else 'audio',referer=lesson['url'],thumbnail_url=page.get('poster')) for x in direct]
        from media_transport import select_rendition,shared_muxed_audio
        for frame in page['frames']:
            if urlsplit(frame).scheme!='https':continue
            try:urls=[urljoin(frame,u) for u in structured_media(self._text(frame,lesson['url']))]
            except RuntimeError as error:
                if str(error)=='player-fetch-failed':continue
                raise
            urls.sort(key=lambda u:not urlsplit(u).path.lower().endswith('.mp3'))
            if not urls:continue
            url=urls[0]
            if '.m3u8' in urlsplit(url).path.lower():
                playlist=self._text(url,frame)
                url=select_rendition(url,playlist,audio_only=True) or shared_muxed_audio(url,playlist) or url
                self._hls_meta[url]=frame
                return [Candidate(url,'hls-audio',referer=frame,player_url=frame,thumbnail_url=page.get('poster'))]
            return [Candidate(url,'mp3' if '.mp3' in urlsplit(url).path.lower() else 'audio',referer=frame,player_url=frame)]
        raise RuntimeError('no-audio-candidate')
