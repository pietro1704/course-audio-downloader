"""Bounded supervision of a fixed, privately configured delivery partition."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from cloud_delivery import NativeCloud,atomic_json
from course_cli import preflight
from course_catalog import load_lessons
from site_profile import load_profile
from pipeline import Pipeline


def read(path,default):
    try:return json.loads(Path(path).read_text())
    except FileNotFoundError:return default


def process_state(pid):
    result=subprocess.run(['ps','-p',str(pid),'-o','stat=','-o','lstart='],capture_output=True,text=True,timeout=5)
    if result.returncode not in {0,1}:raise RuntimeError('process-inspection-failed')
    parts=result.stdout.strip().split(None,1)
    return (parts[0],parts[1] if len(parts)>1 else '') if parts else ('','')


class ProcessRef:
    def __init__(self,pid,owned=None):
        if type(pid) is not int or pid<1:raise ValueError('invalid-worker-pid')
        self.pid,self.owned=pid,owned
        self.birth=process_state(pid)[1] if owned is None else None

    def running(self):
        if self.owned is not None:
            return self.owned.poll() is None  # Reap our child; kill -0 accepts zombies.
        state,birth=process_state(self.pid)
        if state and not state.startswith('Z') and birth!=self.birth:
            raise RuntimeError('process-identity-changed')
        return bool(state and not state.startswith('Z'))


def awake(pid):
    if sys.platform!='darwin':return None
    return subprocess.Popen(['caffeinate','-i','-w',str(pid)],stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)


class Supervisor:
    def __init__(self,spec,poll_seconds=15,max_no_progress=3,max_resumes=100,hours=12):
        self.spec=dict(spec);self.root=Path(spec['root']);self.docs=self.root/'Documentacao'
        self.stop=threading.Event();self.child=None;self.awake_child=None;self.helpers=[];self.attempts=0
        if poll_seconds<=0 or max_no_progress<1 or max_resumes<1 or hours<=0:raise ValueError('invalid-monitor-boundary')
        self.poll_seconds,self.max_no_progress,self.max_resumes=poll_seconds,max_no_progress,max_resumes
        self.hours=hours;self.no_progress=0;self.pid=spec['initial_worker_pid']
        if type(self.pid) is not int or self.pid<1:raise ValueError('invalid-worker-pid')
        if type(spec['start']) is not int or spec['start']<0 or spec['concurrency'] not in range(1,9):raise ValueError('invalid-queue-boundary')
        upload_timeout=spec.get('upload_timeout',900)
        if type(upload_timeout) not in (int,float) or not 1<=upload_timeout<=21600:
            raise ValueError('invalid-upload-timeout')
        self.spec['upload_timeout']=upload_timeout

    def queue(self):
        lessons=load_lessons(self.spec['catalog'])[self.spec['start']:]
        if len(lessons)!=self.spec['expected_partition_count'] or not lessons:raise ValueError('queue-scope-mismatch')
        identities={Pipeline.identity(x['url']) for x in lessons}
        journal=read(self.docs/'delivery-journal.json',{})
        if not set(journal)<=identities:raise ValueError('foreign-journal-identity')
        return lessons,journal

    def engine(self):
        root=Path(self.spec['engine']).resolve();manifest=read(root/'engine.json',{})
        files=manifest.get('files',{})
        if 'course_cli.py' not in files:raise ValueError('missing-engine-manifest')
        for name,digest in files.items():
            file=root/name
            if Path(name).name!=name or file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest()!=digest:
                raise ValueError('engine-identity-mismatch')
        return root

    def verify(self):
        self.engine();lessons,journal=self.queue()
        rows=preflight(lessons,load_profile(self.spec['profile']),self.root,
                       Path(self.spec['destination']),NativeCloud(self.spec['icloud_python']))
        return rows,journal

    def dry_run(self):
        rows,_=self.verify()
        return dict(ok=True,total=len(rows),complete=sum(x['state']=='complete' for x in rows),
                    pending=sum(x['state']!='complete' for x in rows),concurrency=self.spec['concurrency'])

    def save(self,phase,**fields):
        atomic_json(self.docs/'resume-monitor-status.json',dict(at=time.time(),phase=phase,
                    worker_pid=self.pid,attempts=self.attempts,consecutive_no_progress=self.no_progress,
                    expected=self.spec['expected_partition_count'],**fields))

    def reap_helpers(self):
        self.helpers=[child for child in self.helpers if child.poll() is None]

    def launch(self):
        rows,_=self.verify()
        atomic_json(self.docs/'resume-preflight.json',dict(ok=True,total=len(rows),at=time.time(),
                    states={s:sum(x['state']==s for x in rows) for s in {x['state'] for x in rows}}))
        with (self.docs/'delivery.lock').open('a') as check:fcntl.flock(check,fcntl.LOCK_EX|fcntl.LOCK_NB)
        spec=self.spec
        command=[spec['python'],str(self.engine()/'course_cli.py'),'--catalog',spec['catalog'],
                 '--profile',spec['profile'],'--root',spec['root'],'--destination',spec['destination'],
                 '--icloud-python',spec['icloud_python'],'--concurrency',str(spec['concurrency']),
                 '--start',str(spec['start']),'--upload-timeout',str(spec['upload_timeout'])]
        with (self.docs/'delivery.log').open('a') as log:
            child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        # Retain ownership before anything else can fail; never lose the live handle.
        self.child=child;previous=self.pid;self.pid=child.pid;self.attempts+=1
        self.reap_helpers();self.awake_child=awake(child.pid)
        if self.awake_child is not None:self.helpers.append(self.awake_child)
        meta=read(self.docs/'worker.json',{});meta.update(pid=self.pid,started_at=time.time(),
                    previous_pid=previous,selected=len(rows),start=spec['start'],concurrency=spec['concurrency'],
                    upload_timeout=spec['upload_timeout'],commit=spec.get('commit'),
                    isolated_engine=True,resume_monitor=True,
                    awake_pid=self.awake_child.pid if self.awake_child else None)
        atomic_json(self.docs/'worker.json',meta)
        return ProcessRef(child.pid,child)

    def run(self):
        self.docs.mkdir(parents=True,exist_ok=True)
        try:
            with (self.docs/'supervisor.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return 2
                return self._run()
        except KeyboardInterrupt:
            self.stop.set();self.save('monitor-cancelled-worker-kept-running');return 130
        except Exception as error:
            self.save('stopped-verification-or-launch-error',error_type=type(error).__name__);return 2
        finally:
            if self.child is not None:self.child.poll()
            self.reap_helpers()

    def _run(self):
        _,journal=self.queue();last_count=sum(x['state']=='complete' for x in journal.values())
        ref=ProcessRef(self.pid);deadline=time.monotonic()+self.hours*3600
        while time.monotonic()<deadline:
            self.reap_helpers()
            if self.stop.is_set():self.save('monitor-cancelled-worker-kept-running');return 130
            if ref.running():
                self.save('monitoring-live-worker');self.stop.wait(self.poll_seconds);continue
            status=read(self.docs/'delivery-status.json',{})
            if status.get('pid')!=self.pid:raise ValueError('worker-status-identity-mismatch')
            _,journal=self.queue();current=sum(x['state']=='complete' for x in journal.values())
            if status.get('stage')=='finished':
                rows,_=self.verify()
                if current!=self.spec['expected_partition_count'] or any(x['state']!='complete' for x in rows):
                    raise ValueError('incomplete-terminal-queue')
                self.save('complete',complete=current);return 0
            if status.get('stage')=='interrupted':self.save('interrupted-worker-kept-stopped');return 130
            if status.get('stage')!='failed':raise ValueError('missing-terminal-worker-status')
            if self.attempts:
                self.no_progress=self.no_progress+1 if current<=last_count else 0
            if self.no_progress>=self.max_no_progress or self.attempts>=self.max_resumes:
                self.save('stopped-retry-limit',complete=current);return 2
            last_count=current;self.save('preflight',complete=current)
            if self.attempts and self.stop.wait(self.poll_seconds):continue
            ref=self.launch()
        self.save('monitor-deadline-worker-kept-running');return 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--spec',required=True)
    parser.add_argument('--dry-run',action='store_true');args=parser.parse_args(argv)
    supervisor=Supervisor(json.loads(Path(args.spec).read_text()))
    if args.dry_run:print(json.dumps(supervisor.dry_run()));return 0
    return supervisor.run()


if __name__=='__main__':raise SystemExit(main())
