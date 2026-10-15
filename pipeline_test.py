import tempfile,unittest,subprocess
from pathlib import Path
from pipeline import Pipeline,Candidate
class A:
 def __init__(self): self.resolve_calls=0; self.encode_calls=0
 def resolve(self,u): self.resolve_calls+=1; return [Candidate(u+".m4a?sig=secret","audio")]
 def download(self,c,d): subprocess.run(["ffmpeg","-y","-f","lavfi","-i","sine=frequency=440:duration=0.2","-c:a","pcm_s16le","-f","wav",str(d)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); return d
 def encode(self,s,d): self.encode_calls+=1; subprocess.run(["ffmpeg","-y","-i",str(s),"-ac","1","-ar","44100","-c:a","aac","-b:a","32k",str(d)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
class T(unittest.TestCase):
 def test_real_validation_resume_and_secret_free_manifest(self):
  with tempfile.TemporaryDirectory() as x:
   a=A(); p=Pipeline(x,a,reserve_bytes=0); r=p.one("https://x/a"); self.assertEqual(r["status"],"ok"); self.assertTrue(p.one("https://x/a")["resume"]); self.assertEqual(a.resolve_calls,1); self.assertEqual(a.encode_calls,1); self.assertNotIn("sig",Path(x,"manifest.jsonl").read_text())
 def test_disk_reserve(self):
  with tempfile.TemporaryDirectory() as x: self.assertEqual(Pipeline(x,A(),reserve_bytes=10**30).one("https://x/a")["reason"],"disk-reserve")
if __name__=="__main__": unittest.main()
