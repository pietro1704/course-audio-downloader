import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from job_supervisor import JobSupervisor


class JobSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.progress=self.root/'receipts.json';self.progress.write_text('{}')
        self.job=dict(name='one',command=[sys.executable,'-c','pass'],
                      preflight=[sys.executable,'-c','pass'],verify=[sys.executable,'-c','pass'],
                      log=str(self.root/'private.log'),
                      progress=[dict(path=str(self.progress),field='state',values=['complete'])])
        self.spec=dict(state=str(self.root/'state.json'),lock=str(self.root/'lane.lock'),
                       max_no_progress=2,max_attempts=6,jobs=[self.job])

    def supervisor(self):return JobSupervisor(self.spec,poll_seconds=.005,retry_seconds=.005)

    def test_real_worker_crash_without_terminal_status_is_retried_and_reaped(self):
        marker=self.root/'attempt'
        self.job['command']=[sys.executable,'-c',
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); raise SystemExit(2 if n==1 else 0)",str(marker)]
        supervisor=self.supervisor()
        self.assertEqual(supervisor.run(),0)
        self.assertEqual(marker.read_text(),'2')
        self.assertEqual(supervisor.child.poll(),0)
        self.assertEqual(supervisor.report['jobs']['one']['attempts'],2)

    def test_failed_preflight_is_bounded_without_launching_worker(self):
        self.job['preflight']=[sys.executable,'-c','raise SystemExit(2)']
        self.job['command']=[sys.executable,'-c',"raise RuntimeError('must-not-launch')"]
        supervisor=self.supervisor();self.assertEqual(supervisor.run(),2)
        self.assertEqual(supervisor.report['jobs']['one']['attempts'],2)
        self.assertEqual(supervisor.report['jobs']['one']['phase'],'blocked')
        self.assertNotIn('must-not-launch',Path(self.job['log']).read_text())
        # Restarting the monitor cannot reset the retry boundary.
        restarted=self.supervisor();self.assertEqual(restarted.run(),2)
        self.assertEqual(restarted.report['jobs']['one']['attempts'],2)

    def test_exit_zero_requires_verification_and_does_not_block_other_jobs(self):
        self.job['verify']=[sys.executable,'-c','raise SystemExit(2)']
        self.spec['jobs'].append({**self.job,'name':'two','verify':[sys.executable,'-c','pass']})
        supervisor=self.supervisor();self.assertEqual(supervisor.run(),2)
        self.assertEqual(supervisor.report['jobs']['one']['phase'],'blocked')
        self.assertEqual(supervisor.report['jobs']['two']['phase'],'complete')

    def test_cancelled_worker_is_not_restarted(self):
        self.job['command']=[sys.executable,'-c','raise SystemExit(130)']
        supervisor=self.supervisor();self.assertEqual(supervisor.run(),2)
        self.assertEqual(supervisor.report['jobs']['one']['phase'],'cancelled')
        self.assertEqual(supervisor.report['jobs']['one']['attempts'],1)

    def test_monitor_cancel_keeps_worker_and_restart_refuses_overlap(self):
        self.job['command']=[sys.executable,'-c','import time; time.sleep(30)']
        supervisor=self.supervisor()
        original=supervisor.execute
        def execute(command,log):
            if command==self.job['command']:
                timer=threading.Timer(.05,supervisor.stop.set);timer.start()
                try:return original(command,log)
                finally:timer.join()
            return original(command,log)
        with patch.object(supervisor,'execute',side_effect=execute):
            self.assertEqual(supervisor.run(),130)
        child=supervisor.child
        try:
            self.assertIsNone(child.poll())
            with self.assertRaisesRegex(RuntimeError,'previous-worker-still-live'):self.supervisor().run()
        finally:child.terminate();child.wait()

    def test_progress_counts_receipts_instead_of_error_timestamps(self):
        self.progress.write_text(json.dumps({'one':{'state':'complete'},'two':{'state':'failed','at':100}}))
        self.assertEqual(self.supervisor().progress(self.job),1)

    def test_changed_spec_and_duplicate_browser_lane_fail_closed(self):
        self.assertEqual(self.supervisor().run(),0)
        self.job['command']=[sys.executable,'-c','print(123)']
        with self.assertRaisesRegex(ValueError,'supervisor-spec-changed'):self.supervisor().run()
        import fcntl
        before=Path(self.spec['state']).read_bytes()
        with Path(self.spec['lock']).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.supervisor().run(),2)
        self.assertEqual(Path(self.spec['state']).read_bytes(),before)

    def test_changed_bound_input_never_launches(self):
        self.spec['bindings']=[dict(path=str(self.progress),sha256='0'*64)]
        with patch('job_supervisor.subprocess.Popen') as launch:
            supervisor=self.supervisor();self.assertEqual(supervisor.run(),2)
            launch.assert_not_called()
        self.assertEqual(supervisor.report['reason'],'supervisor-input-identity-changed')
