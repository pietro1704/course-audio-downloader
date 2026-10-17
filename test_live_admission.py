from pathlib import Path
import tempfile,threading,time,unittest
from cloud_delivery import Delivery


class LiveAdmissionTests(unittest.TestCase):
    def work(self,root):
        return [({'title':name,'url':'https://portal.example/'+name,'site_path':['Course',name]},root/(name+'.m4a')) for name in ['A','B','C']]

    def test_grows_during_a_long_download_then_shrinks_without_cancelling_inflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);limit=[1];started={n:threading.Event() for n in ['A','B','C']};release={n:threading.Event() for n in started};errors=[];finished=[]
            class Observed(Delivery):
                def process(self,lesson,target,index=None,**kwargs):
                    name=lesson['title'];started[name].set()
                    if not release[name].wait(3):raise RuntimeError('test-release-timeout')
                    finished.append(name);return name
            d=Observed(root/'work',root/'cloud',None,concurrency=1,max_concurrency=2,worker_limit=lambda:limit[0],admission_poll_seconds=.02)
            def run():
                try:d.run(self.work(root/'cloud'))
                except BaseException as error:errors.append(error)
            thread=threading.Thread(target=run);thread.start()
            try:
                self.assertTrue(started['A'].wait(1));self.assertFalse(started['B'].is_set())
                limit[0]=2;self.assertTrue(started['B'].wait(1))
                limit[0]=1;release['A'].set();time.sleep(.08)
                self.assertFalse(started['C'].is_set());self.assertFalse(d._stop.is_set())
                release['B'].set();self.assertTrue(started['C'].wait(1));release['C'].set()
            finally:
                for event in release.values():event.set()
                thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertCountEqual(finished,['A','B','C'])

    def test_invalid_live_limit_stops_new_work_and_drains_the_existing_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);limit=[1];started=[];errors=[];first=threading.Event()
            class Observed(Delivery):
                def process(self,lesson,target,index=None,**kwargs):
                    started.append(lesson['title']);first.set();self._stop.wait(2);return 'preserved'
            d=Observed(root/'work',root/'cloud',None,concurrency=1,max_concurrency=2,worker_limit=lambda:limit[0],admission_poll_seconds=.02)
            def run():
                try:d.run(self.work(root/'cloud'))
                except BaseException as error:errors.append(error)
            thread=threading.Thread(target=run);thread.start();self.assertTrue(first.wait(1));limit[0]=0;thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(started,['A']);self.assertEqual(len(errors),1)
            self.assertEqual(str(errors[0]),'invalid-worker-limit');self.assertTrue(d._stop.is_set())

    def test_duplicate_identity_rejects_before_admission_or_source_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[]
            d=Delivery(root/'work',root/'cloud',None,concurrency=1,max_concurrency=2,worker_limit=lambda:calls.append(True) or 2)
            work=self.work(root/'cloud')
            with self.assertRaisesRegex(ValueError,'duplicate input lesson identities'):d.run([work[0],work[0]])
            self.assertEqual(calls,[])
