"""Supervise trusted private commands with bounded retries and durable progress.

Commands never enter the repository. Each lane owns one browser lock; jobs in a
lane run sequentially, while separate lanes may use different existing tabs.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from cloud_delivery import atomic_json


def safe_reason(error):
    value=str(error)
    return value if re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value) else type(error).__name__


class JobSupervisor:
    def __init__(self,spec,poll_seconds=15,retry_seconds=15):
        self.spec=spec
        self.state=Path(spec['state'])
        self.stop=threading.Event()
        self.child=None
        self.poll_seconds,self.retry_seconds=poll_seconds,retry_seconds
        self.limit=spec.get('max_no_progress',3)
        self.max_attempts=spec.get('max_attempts',100)
        if type(self.limit) is not int or self.limit<1 or type(self.max_attempts) is not int or self.max_attempts<1:
            raise ValueError('invalid-retry-boundary')
        names=[]
        for job in spec['jobs']:
            names.append(job['name'])
            for key in ('command','preflight','verify'):
                command=job[key]
                if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x for x in command):
                    raise ValueError('invalid-job-command')
        if not names or len(set(names))!=len(names):raise ValueError('invalid-job-identities')
        self.fingerprint=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
        self.report=dict(schema='course-job-supervision/v1',spec_hash=self.fingerprint,jobs={})

    def save(self,phase,**fields):
        self.report.update(phase=phase,pid=os.getpid(),at=time.time(),
                           free_gib=round(shutil.disk_usage(self.state.parent).free/1024**3,3),**fields)
        atomic_json(self.state,self.report)

    def verify_bindings(self):
        for binding in self.spec.get('bindings',[]):
            path=Path(binding['path'])
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=binding['sha256']:
                raise ValueError('supervisor-input-identity-changed')

    def progress(self,job):
        count=0
        for item in job.get('progress',[]):
            try:rows=json.loads(Path(item['path']).read_text()).values()
            except FileNotFoundError:continue
            for row in rows:
                value=row
                for key in item['field'].split('.'):
                    value=value.get(key) if isinstance(value,dict) else None
                count+=value in item['values']
        return count

    def execute(self,command,log):
        # Keep the handle even if writing status fails; poll always reaps children.
        self.child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,
                                    stderr=subprocess.STDOUT,start_new_session=True)
        self.save('running-command',worker_pid=self.child.pid)
        while self.child.poll() is None:
            if self.stop.wait(self.poll_seconds):return None
            self.save('running-command',worker_pid=self.child.pid)
        return self.child.returncode

    def run_job(self,job):
        entry=self.report['jobs'].setdefault(job['name'],dict(attempts=0,no_progress=0))
        if entry.get('phase') in {'complete','blocked','cancelled'}:return
        last=self.progress(job)
        while entry['attempts']<self.max_attempts and entry['no_progress']<self.limit:
            if self.stop.is_set():return
            self.verify_bindings()
            entry['attempts']+=1
            self.save('preflight',job=job['name'])
            with Path(job['log']).open('a') as log:
                code=self.execute(job['preflight'],log)
                if code==0:
                    code=self.execute(job['command'],log)
                    if code==0:code=self.execute(job['verify'],log)
            if code is None:
                self.save('monitor-cancelled-worker-kept-running');return
            if code in {130,-2,-15}:
                entry.update(phase='cancelled',exit_code=code)
                self.save('cancelled-worker-kept-stopped');return
            current=self.progress(job)
            entry.update(progress=current,exit_code=code,
                         no_progress=entry['no_progress']+1 if current<=last else 0)
            last=current
            if code==0:
                entry['phase']='complete';self.save('job-complete');return
            self.save('retry-wait')
            if self.stop.wait(self.retry_seconds):return
        entry.update(phase='blocked',reason='retry-limit-without-verified-completion')
        self.save('job-blocked')

    def run(self):
        self.state.parent.mkdir(parents=True,exist_ok=True)
        with Path(self.spec['lock']).open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return 2
            if self.state.exists():
                saved=json.loads(self.state.read_text())
                if saved.get('spec_hash')!=self.fingerprint:raise ValueError('supervisor-spec-changed')
                # Do not launch over a worker left running by a cancelled monitor.
                pid=saved.get('worker_pid')
                if pid:
                    from queue_supervisor import process_state
                    state,_=process_state(pid)
                    if state and not state.startswith('Z'):raise RuntimeError('previous-worker-still-live')
                self.report=saved
            try:
                for job in self.spec['jobs']:
                    self.run_job(job)
                    if self.stop.is_set():return 130
                blocked=any(x.get('phase')!='complete' for x in self.report['jobs'].values())
                self.save('blocked' if blocked else 'complete')
                return 2 if blocked else 0
            except KeyboardInterrupt:
                self.stop.set();self.save('monitor-cancelled-worker-kept-running');return 130
            except Exception as error:
                self.save('blocked',reason=safe_reason(error));return 2
            finally:
                if self.child is not None:self.child.poll()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',required=True)
    args=parser.parse_args(argv)
    return JobSupervisor(json.loads(Path(args.spec).read_text())).run()


if __name__=='__main__':raise SystemExit(main())
