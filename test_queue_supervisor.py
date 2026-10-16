import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from pipeline import Pipeline
from queue_supervisor import ProcessRef,Supervisor

class SupervisorTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name);self.root=self.base/'work';self.docs=self.root/'Documentacao';self.docs.mkdir(parents=True);self.engine=self.base/'engine';self.engine.mkdir();self.url='https://portal.example.test/lesson/one';self.ident=Pipeline.identity(self.url)
  self.catalog=self.base/'catalog.json';self.catalog.write_text(json.dumps(dict(schema='course-catalog/v1',complete=True,expected_count=1,collected_count=1,lessons=[dict(url=self.url,title='Aula',site_path=['Course','Aula'])])))
  self.profile=self.base/'profile.json';self.profile.write_text(json.dumps(dict(schema='course-site-profile/v1',mode='direct',origin='https://portal.example.test')))
  self.spec=dict(root=str(self.root),engine=str(self.engine),python=sys.executable,catalog=str(self.catalog),profile=str(self.profile),destination=str(self.base/'cloud'),icloud_python=sys.executable,initial_worker_pid=99999999,start=0,concurrency=2,expected_partition_count=1)
  self.write('delivery-status.json',dict(pid=99999999,stage='failed'));self.write('delivery-journal.json',{})
  self.set_engine('pass\n')
 def write(self,name,value):(self.docs/name).write_text(json.dumps(value))
 def set_engine(self,source):
  p=self.engine/'course_cli.py';p.write_text(source);(self.engine/'engine.json').write_text(json.dumps(dict(files={'course_cli.py':hashlib.sha256(p.read_bytes()).hexdigest()})))
 def preflight(self,*args,**kwargs):
  j=json.loads((self.docs/'delivery-journal.json').read_text());return [dict(state='complete' if j.get(self.ident,{}).get('state')=='complete' else 'pending')]
 def supervisor(self,**kwargs):return Supervisor(self.spec,poll_seconds=.01,**kwargs)
 def test_owned_exited_child_is_reaped_instead_of_reported_live(self):
  child=subprocess.Popen([sys.executable,'-c','pass']);self.addCleanup(lambda:child.wait())
  # Let it exit without poll/wait: on POSIX it is still a kernel zombie.
  for _ in range(100):
   state=subprocess.run(['ps','-p',str(child.pid),'-o','stat='],capture_output=True,text=True).stdout.strip()
   if state.startswith('Z'):break
   time.sleep(.01)
  self.assertTrue(state.startswith('Z'),state)
  ref=ProcessRef(child.pid,child);self.assertFalse(ref.running());self.assertEqual(child.returncode,0)
 def test_adopted_zombie_is_not_live_and_pid_reuse_is_detected(self):
  with patch('queue_supervisor.process_state',return_value=('Z','birth')):self.assertFalse(ProcessRef(123).running())
  with patch('queue_supervisor.process_state',side_effect=[('S','first'),('S','second')]):
   ref=ProcessRef(123)
   with self.assertRaisesRegex(RuntimeError,'process-identity-changed'):ref.running()
 def test_real_child_failure_then_success_resumes_and_finishes(self):
  self.set_engine("""import sys,json,pathlib,os,hashlib
root=pathlib.Path(sys.argv[sys.argv.index('--root')+1]);d=root/'Documentacao';counter=d/'attempt.txt';n=int(counter.read_text())+1 if counter.exists() else 1;counter.write_text(str(n));ident=hashlib.sha256(b'https://portal.example.test/lesson/one').hexdigest();(d/'delivery-status.json').write_text(json.dumps(dict(pid=os.getpid(),stage='failed' if n==1 else 'finished')))
if n>1:(d/'delivery-journal.json').write_text(json.dumps({ident:dict(state='complete')}))
raise SystemExit(2 if n==1 else 0)
""")
  with patch('queue_supervisor.preflight',side_effect=self.preflight),patch('queue_supervisor.awake',return_value=None):
   s=self.supervisor();self.assertEqual(s.run(),0)
  self.assertEqual((self.docs/'attempt.txt').read_text(),'2');self.assertEqual(s.child.poll(),0)
 def test_no_progress_is_bounded_and_interruption_is_not_restarted(self):
  self.set_engine("import sys,json,pathlib,os\nd=pathlib.Path(sys.argv[sys.argv.index('--root')+1])/'Documentacao'\n(d/'delivery-status.json').write_text(json.dumps(dict(pid=os.getpid(),stage='failed')))\nraise SystemExit(2)\n")
  with patch('queue_supervisor.preflight',side_effect=self.preflight),patch('queue_supervisor.awake',return_value=None):
   s=self.supervisor(max_no_progress=2);self.assertEqual(s.run(),2);self.assertEqual(s.attempts,2)
  self.write('delivery-status.json',dict(pid=99999999,stage='interrupted'))
  with patch('queue_supervisor.process_state',return_value=('','')),patch('queue_supervisor.subprocess.Popen') as launch:self.assertEqual(self.supervisor().run(),130);launch.assert_not_called()
 def test_cancel_monitor_does_not_stop_running_worker(self):
  child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);self.addCleanup(lambda:(child.terminate(),child.wait()))
  spec={**self.spec,'initial_worker_pid':child.pid};self.write('delivery-status.json',dict(pid=child.pid,stage='parallel'));s=Supervisor(spec,poll_seconds=.01);timer=threading.Timer(.05,s.stop.set);timer.start()
  try:self.assertEqual(s.run(),130);self.assertIsNone(child.poll())
  finally:timer.join()
 def test_bad_engine_queue_and_preflight_fail_before_launch(self):
  for kind in ['engine','queue','preflight','pid','scope']:
   with self.subTest(kind=kind):
    self.set_engine('pass\n');self.write('delivery-journal.json',{});self.write('delivery-status.json',dict(pid=99999999,stage='failed'));spec=dict(self.spec)
    if kind=='engine':(self.engine/'course_cli.py').write_text('changed\n')
    if kind=='queue':self.write('delivery-journal.json',{'foreign':dict(state='complete')})
    if kind=='pid':self.write('delivery-status.json',dict(pid=123,stage='failed'))
    if kind=='scope':spec['expected_partition_count']=2
    with patch('queue_supervisor.process_state',return_value=('','')),patch('queue_supervisor.preflight',side_effect=ValueError('invalid-receipt') if kind=='preflight' else self.preflight),patch('queue_supervisor.subprocess.Popen') as launch:
     self.assertEqual(Supervisor(spec,poll_seconds=.01).run(),2);launch.assert_not_called()
 def test_dry_run_never_creates_directories_or_launches(self):
  spec={**self.spec,'root':str(self.base/'absent')}
  with patch('queue_supervisor.preflight',return_value=[dict(state='pending')]),patch('queue_supervisor.subprocess.Popen') as launch:
   result=Supervisor(spec).dry_run();self.assertTrue(result['ok']);self.assertEqual(result['total'],1);launch.assert_not_called()
  self.assertFalse(Path(spec['root']).exists())

 def test_duplicate_supervisor_does_not_overwrite_active_monitor(self):
  import fcntl
  self.write('resume-monitor-status.json',dict(phase='owner-active'))
  with (self.docs/'supervisor.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   self.assertEqual(self.supervisor().run(),2)
  self.assertEqual(json.loads((self.docs/'resume-monitor-status.json').read_text()),dict(phase='owner-active'))
 def test_finished_subset_is_not_claimed_complete(self):
  self.write('delivery-status.json',dict(pid=99999999,stage='finished'))
  with patch('queue_supervisor.preflight',side_effect=self.preflight):self.assertEqual(self.supervisor().run(),2)

 def test_monitor_reaps_exited_awake_helpers_without_stopping_live_ones(self):
  exited=Mock();exited.poll.return_value=0;running=Mock();running.poll.return_value=None
  s=self.supervisor();s.helpers=[exited,running];s.reap_helpers()
  self.assertEqual(s.helpers,[running]);running.terminate.assert_not_called()
