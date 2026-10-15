from __future__ import annotations
import hashlib,json,shutil,threading,tempfile,subprocess,os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit,urlunsplit
@dataclass(frozen=True)
class Candidate: url:str; kind:str
class ProviderAdapter(Protocol):
 def resolve(self,page_url:str)->list[Candidate]: ...
 def download(self,candidate:Candidate,destination:Path)->Path: ...
 def encode(self,source:Path,destination:Path)->None: ...
class Pipeline:
 def __init__(self,root,adapter,concurrency=2,reserve_bytes=4*1024**3):
  self.root=Path(root); self.adapter=adapter; self.concurrency=concurrency; self.reserve_bytes=reserve_bytes
  self.manifest=self.root/"manifest.jsonl"; self.root.mkdir(parents=True,exist_ok=True); (self.root/"originals").mkdir(exist_ok=True); (self.root/".staging").mkdir(exist_ok=True)
  self._resolve_lock=threading.Lock(); self._manifest_lock=threading.Lock()
 @staticmethod
 def identity(url):
  p=urlsplit(url); return hashlib.sha256(urlunsplit((p.scheme,p.netloc,p.path,"","")).encode()).hexdigest()
 @staticmethod
 def sha256(p):
  h=hashlib.sha256();
  with open(p,"rb") as f:
   for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
  return h.hexdigest()
 @staticmethod
 def rank(cs): return sorted(cs,key=lambda c:(0 if c.kind.lower() in {"audio","m4a","mp3","aac"} else 1,c.url))
 def records(self):
  out={}
  if self.manifest.exists():
   for line in self.manifest.read_text().splitlines():
    try:
     r=json.loads(line); out[r.get("source_identity")]=r
    except json.JSONDecodeError: pass
  return out
 def write(self,r):
  with self._manifest_lock:
   with self.manifest.open("a") as f: f.write(json.dumps(r,sort_keys=True)+"\n")
 def _valid(self,p):
  if not p.is_file() or p.stat().st_size==0:return False
  try: subprocess.run(["ffprobe","-v","error",str(p)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); return True
  except (OSError,subprocess.CalledProcessError): return False
 def one(self,url):
  ident=self.identity(url); old=self.records().get(ident)
  if old and old.get("status")=="ok":
   final=Path(old.get("final","")); original=Path(old.get("original",""))
   if final.is_file() and original.is_file() and self.sha256(original)==old.get("source_hash") and self.sha256(final)==old.get("final_hash") and self._valid(final): return {**old,"resume":True}
  if shutil.disk_usage(self.root).free<self.reserve_bytes:return {"status":"failed","reason":"disk-reserve","source_identity":ident}
  with self._resolve_lock: cs=self.rank(self.adapter.resolve(url))
  if not cs:return {"status":"failed","reason":"no-candidate","source_identity":ident}
  c=cs[0]; stem=ident[:16]; staging=self.root/".staging"; source_tmp=staging/(stem+Path(urlsplit(c.url).path).suffix); original=self.root/"originals"/(stem+".original"+Path(urlsplit(c.url).path).suffix); final=self.root/(stem+".m4a")
  try:
   got=Path(self.adapter.download(c,source_tmp)); shutil.copy2(got,original)
   source_hash=self.sha256(original); tmp=staging/(stem+".tmp.m4a"); self.adapter.encode(original,tmp)
   if not self._valid(tmp): raise RuntimeError("encoded audio failed ffprobe validation")
   final_hash=self.sha256(tmp); os.replace(tmp,final)
   r={"status":"ok","source_identity":ident,"original":str(original),"final":str(final),"source_hash":source_hash,"final_hash":final_hash,"params":{"codec":"aac","profile":"LC","channels":1,"sample_rate":44100,"bitrate":32000}}; self.write(r); return r
  except Exception as e:
   (staging/(stem+".tmp.m4a")).unlink(missing_ok=True)
   return {"status":"failed","source_identity":ident,"reason":str(e)}
 def run(self,urls):
  with ThreadPoolExecutor(max_workers=self.concurrency) as x:return list(x.map(self.one,urls))
