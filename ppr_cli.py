from __future__ import annotations
import argparse,json,subprocess,urllib.request,shutil
from pathlib import Path
from pipeline import Pipeline,Candidate
class SafariAdapter:
 def __init__(self): self.js="JSON.stringify({url:location.href,canonical:(document.querySelector('link[rel=canonical]')||{}).href||'',title:document.title,ready:document.readyState,media:[...document.querySelectorAll('audio source,video source,a[href]')].map(e=>e.currentSrc||e.src||e.href||'').filter(Boolean)})"
 def resolve(self,url):
  safe=url.replace('"','\\"'); js=self.js.replace('"','\\"')
  script='tell application "Safari"\nset t to current tab of front window\nset URL of t to "'+safe+'"\ndelay 1\nreturn do JavaScript "'+js+'" in t\nend tell'
  page=json.loads(subprocess.check_output(['osascript','-e',script],text=True,timeout=90))
  return [Candidate(x,'audio' if x.lower().split('?')[0].endswith(('.mp3','.m4a','.aac','.wav')) else 'video') for x in dict.fromkeys(page.get('media',[]))]
 def download(self,candidate,destination):
  with urllib.request.urlopen(candidate.url,timeout=120) as r,destination.open('wb') as f: shutil.copyfileobj(r,f)
  return destination
 def encode(self,source,destination):
  subprocess.run(['ffmpeg','-y','-i',str(source),'-vn','-ac','1','-ar','44100','-c:a','aac','-profile:a','aac_low','-b:a','32k',str(destination)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--catalog',required=True); ap.add_argument('--output-root',required=True); ap.add_argument('--concurrency',type=int,default=2); ap.add_argument('--min-free-gib',type=float,default=4); ap.add_argument('--start',type=int,default=0); ap.add_argument('--limit',type=int); a=ap.parse_args(); data=json.loads(Path(a.catalog).read_text()); lessons=[l['url'] for c in data['courses'] for l in c.get('lessons',[])]; sel=lessons[a.start:(a.start+a.limit if a.limit else None)]; p=Pipeline(a.output_root,SafariAdapter(),a.concurrency,int(a.min_free_gib*1024**3)); print(json.dumps(p.run(sel),ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
