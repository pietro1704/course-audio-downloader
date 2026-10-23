import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch
import numpy as np
from browser_adapter import SafariAdapter
from local_course import LocalCourse,main
from pipeline import Candidate

class PDF(io.BytesIO):
 def __init__(self):
  super().__init__(b'%PDF-1.4\nsynthetic fixture\n%%EOF\n')
  from email.message import Message
  self.headers=Message();self.headers['Content-Type']='application/pdf'
  self.headers['Content-Length']=str(len(self.getvalue()))
 def geturl(self):return 'https://files.example.test/notes.pdf'

class Documents:
 def inventory(self,lesson):return [{'id':'notes','name':'notes.pdf','url':'https://files.example.test/notes.pdf'}]
 def url_for(self,lesson,resource):return resource['url']

class LocalCourseTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
  self.lesson={'url':'https://school.example.test/lesson/1','title':'One','site_path':['Course','01. One']}
  wav=self.base/'fixture.wav';rate=16000
  pcm=np.zeros(rate*6,dtype='<i2');voice=(np.sin(np.arange(rate)*2*np.pi*180/rate)*8000).astype('<i2')
  pcm[:rate]=voice;pcm[-rate:]=voice
  with wave.open(str(wav),'wb') as out:out.setnchannels(1);out.setsampwidth(2);out.setframerate(rate);out.writeframes(pcm.tobytes())
  self.source=self.base/'fixture.mp3'
  subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-i',str(wav),'-c:a','libmp3lame',str(self.source)],check=True,capture_output=True)
  source=self.source
  class Adapter(SafariAdapter):
   def resolve(self,lesson):return [Candidate('https://files.example.test/opaque','mp3')]
   def download(self,candidate,dest):shutil.copyfile(source,dest);return dest
  self.adapter=Adapter(encoder='aac')
 def job(self,**kwargs):return LocalCourse(self.base/'out',{},encoder='aac',adapter=self.adapter,documents=Documents(),reserve_bytes=0,event=lambda _:None,**kwargs)
 def test_default_removal_real_codec_pdf_and_resume_without_double_editing(self):
  published=[];job=self.job(on_artifact=lambda record,kind:published.append((dict(record),kind)))
  with patch('documents.urllib.request.urlopen',side_effect=lambda *a,**k:PDF()):records=job.run([self.lesson])
  self.assertEqual(job.phase,'complete');self.assertEqual(job.attachments,1)
  self.assertEqual([kind for record,kind in published],['audio','document'])
  self.assertEqual(published[0][0]['final_hash'],records[0]['final_hash'])
  self.assertTrue(published[0][0]['silence']['removed'])
  self.assertTrue(records[0]['silence']['removed'])
  self.assertLess(records[0]['silence']['output_frames'],records[0]['silence']['input_frames'])
  with patch.object(self.adapter,'resolve',side_effect=AssertionError('download repeated')):
   resumed=self.job().run([self.lesson])
  self.assertTrue(resumed[0]['resume']);self.assertEqual(resumed[0]['final_hash'],records[0]['final_hash'])
 def test_explicit_opt_out_and_invalid_pdf_preserve_audio(self):
  job=self.job(protected_pauses=False)
  bad=PDF();bad.seek(0);bad.write(b'ERROR');bad.seek(0)
  with patch('documents.urllib.request.urlopen',return_value=bad),self.assertRaisesRegex(RuntimeError,'invalid-document-format'):
   job.run([self.lesson])
  self.assertEqual(job.phase,'failed');self.assertTrue((job.root/'Course/01. One.m4a').exists())
  records=job.pipeline.records();self.assertNotIn('silence',next(iter(records.values())))
 def test_cancelled_encode_does_not_publish_and_preserves_original(self):
  job=self.job();encode=self.adapter.encode
  def cancel(source,dest):encode(source,dest);job.cancel()
  with patch.object(self.adapter,'encode',side_effect=cancel),self.assertRaises(RuntimeError):job.run([self.lesson])
  self.assertEqual(job.phase,'cancelled')
  self.assertFalse((job.root/'Course/01. One.m4a').exists());self.assertTrue(list((job.root/'Course/originals').glob('*')))
 def test_pdf_identity_change_is_refused(self):
  job=self.job(protected_pauses=False)
  with patch('documents.urllib.request.urlopen',side_effect=lambda *a,**k:PDF()):job.run([self.lesson])
  (job.root/'Course/01. One/notes.pdf').write_bytes(b'changed')
  with self.assertRaisesRegex(RuntimeError,'document-source-hash-mismatch'):self.job(protected_pauses=False).run([self.lesson])
 def test_mp3_direct_precedes_video_even_with_an_opaque_download_url(self):
  page=dict(url=self.lesson['url'],canonical='',ready='complete',auth=True,title='One',
   links=[{'url':'https://files.example.test/opaque?signature=private','label':'Áudio da aula (mp3)'}],
   frames=['https://video.example.test/player'])
  adapter=SafariAdapter(profile={'origin':'https://school.example.test'})
  adapter._eval=lambda navigate=None:{} if navigate else page
  with patch('browser_adapter.time.sleep'),patch('media_transport.resolve_player',side_effect=AssertionError('video selected')):
   candidates=adapter.resolve(self.lesson)
  self.assertEqual(candidates[0].kind,'mp3')
 def test_default_encoder_matches_the_operating_system(self):
  for platform,encoder in [('win32','aac'),('linux','aac'),('darwin','aac_at')]:
   with self.subTest(platform=platform),patch('sys.platform',platform),patch('local_course.build_adapter',return_value=self.adapter) as build:
    LocalCourse(self.base/platform,{},protected_pauses=False,documents=Documents(),reserve_bytes=0)
    build.assert_called_once_with({},encoder)
 def test_direct_mp3_wins_over_embedded_video_with_full_page_guards(self):
  from embedded_adapter import EmbeddedAdapter
  page=dict(url=self.lesson['url'],canonical='',ready='complete',auth=True,title='One',
   links=[{'url':'https://files.example.test/opaque','label':'Áudio da aula (mp3)'}],
   frames=['https://video.example.test/player'],iframes=['https://video.example.test/player'])
  adapter=EmbeddedAdapter(profile={'origin':'https://school.example.test'})
  adapter._eval=lambda navigate=None:{} if navigate else page
  with patch('browser_adapter.time.sleep'),patch.object(adapter,'_iframe_html',side_effect=AssertionError('video selected')):
   candidates=adapter.resolve(self.lesson)
  self.assertEqual(candidates[0].kind,'mp3')

if __name__=='__main__':unittest.main()
