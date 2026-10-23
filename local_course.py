"""Local audio + original attachments, using the existing guarded CLI modules."""
import argparse
import portable_lock as fcntl
import hashlib
import json
from pathlib import Path
import threading
import time
from cloud_delivery import atomic_json
from course_catalog import path_parts
from course_cli import build_adapter
from document_browser import DocumentBrowser
from documents import document_resource,validate_document_paths,download_document
from pipeline import Pipeline
from transfer_errors import failure_reason
from local_paths import component


class LocalCourse:
    def __init__(self,root,profile,*,protected_pauses=True,encoder=None,event=None,
                 adapter=None,documents=None,reserve_bytes=4*1024**3,on_artifact=None):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.docs=self.root/'Documentacao';self.docs.mkdir(exist_ok=True)
        self.stop=threading.Event();self.event=event or (lambda row:print(json.dumps(row,ensure_ascii=False),flush=True))
        self.on_artifact=on_artifact or (lambda record,kind:None)
        self.reserve_bytes=reserve_bytes
        if encoder is None:
            import sys
            encoder='aac_at' if sys.platform=='darwin' else 'aac'
        silence=None
        if protected_pauses:
            from protected_pauses import Compactor
            silence=Compactor()
        self.pipeline=Pipeline(self.root,adapter or build_adapter(profile,encoder),concurrency=1,
            reserve_bytes=reserve_bytes,silence=silence)
        if documents is not None:self.browser=documents
        elif '_browser_runtime' in profile:
            from authenticated_browser import browser_documents
            self.browser=browser_documents(profile)
        else:self.browser=DocumentBrowser(profile=profile)
        self.journal_path=self.docs/'local-documents.json'
        self.status_path=self.docs/'local-status.json'
        self.completed=0;self.attachments=0

    def status(self,phase,**values):
        self.phase=phase
        row=dict(phase=phase,audios_complete=self.completed,attachments_complete=self.attachments,
                 at=time.time(),**values)
        atomic_json(self.status_path,row);self.event(row)

    def cancel(self):
        self.stop.set();self.pipeline._stop.set()

    def document_path(self,lesson,name):
        path=self.root.joinpath(*path_parts(lesson['site_path']),component(path_parts(['document',name])[-1]))
        if path.resolve()!=path or path.is_symlink():raise ValueError('unsafe-document-path')
        return path

    def run(self,lessons):
        with (self.docs/'local-course.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('another-run-is-active') from None
            journal=json.loads(self.journal_path.read_text(encoding='utf-8')) if self.journal_path.exists() else {}
            results=[]
            try:
                for index,lesson in enumerate(lessons,1):
                    if self.stop.is_set():raise KeyboardInterrupt()
                    self.status('audio',index=index,total=len(lessons),title=lesson['title'])
                    record=self.pipeline.one(lesson)
                    if record['status']!='ok':raise RuntimeError(record.get('reason','audio-download-failed'))
                    self.completed+=1;results.append(record)
                    self.on_artifact(record,'audio')
                    silence=record.get('silence',{})
                    self.status('audio-complete',index=index,total=len(lessons),title=lesson['title'],
                        cuts=len(silence.get('removed',[])),input_seconds=silence.get('input_seconds'),
                        output_seconds=record.get('duration_seconds'),resumed=record.get('resume',False))
                    self.status('attachments',index=index,total=len(lessons),title=lesson['title'])
                    resources=[v for raw in self.browser.inventory(lesson) if (v:=document_resource(raw)) is not None]
                    validate_document_paths(resources)
                    for resource in resources:
                        if self.stop.is_set():raise KeyboardInterrupt()
                        identity=Pipeline.identity(lesson['url'])+':'+hashlib.sha256(resource['id'].encode()).hexdigest()
                        old=journal.get(identity)
                        if old:
                            path=self.document_path(lesson,old['name'])
                            if (old.get('source_identity')!=Pipeline.identity(lesson['url']) or
                                    old.get('status')!='ok' or old.get('final')!=str(path) or
                                    (resource.get('name') and resource['name']!=old['name']) or
                                    Pipeline.sha256(path)!=old['final_hash']):
                                raise RuntimeError('document-source-hash-mismatch')
                            prepared=old
                        else:
                            prepared=download_document(lesson,resource,self.browser.url_for(lesson,resource),self.root,
                                lambda name:self.document_path(lesson,name),self.stop,reserve_bytes=self.reserve_bytes,urlopen=getattr(self.browser,'urlopen',None))
                            prepared.update(schema='course-local-document/v1',status='ok',identity=identity,
                                source_identity=Pipeline.identity(lesson['url']),resource_identity=hashlib.sha256(resource['id'].encode()).hexdigest())
                            journal[identity]=prepared;atomic_json(self.journal_path,journal)
                        self.attachments+=1
                        self.on_artifact(prepared,'document')
                        self.status('attachment-complete',index=index,total=len(lessons),title=lesson['title'],
                            name=prepared['name'],bytes=prepared['size'])
                self.status('complete',total=len(lessons))
                return results
            except KeyboardInterrupt:
                self.cancel();self.status('cancelled',total=len(lessons));raise
            except Exception as error:
                phase='cancelled' if self.stop.is_set() else 'failed'
                self.cancel();self.status(phase,total=len(lessons),reason=failure_reason(error));raise


def main(argv=None):
    from course_discovery import discover
    import os
    p=argparse.ArgumentParser(description='Download course audio and attachments from its URL.')
    p.add_argument('url')
    p.add_argument('--inspect',action='store_true',help='Discover course metadata without downloading audio or attachments')
    p.add_argument('--no-silence-removal',action='store_true',help='Keep the original pauses')
    args=p.parse_args(argv)
    root=Path(os.environ.get('COURSE_AUDIO_ROOT',str(Path.home()/'Downloads/Course Audio')))
    if args.no_silence_removal:root=root/'Pausas originais'
    try:
        print(json.dumps(dict(phase='discovering',url=args.url),ensure_ascii=False),flush=True)
        catalog,profile=discover(args.url)
        print(json.dumps(dict(phase='discovered',title=catalog['title'],lessons=len(catalog['lessons'])),ensure_ascii=False),flush=True)
        if args.inspect:
            print(json.dumps(catalog,ensure_ascii=False),flush=True);return 0
        job=LocalCourse(root,profile,protected_pauses=not args.no_silence_removal)
        atomic_json(job.docs/'automatic-catalog.json',catalog)
        job.run(catalog['lessons']);return 0
    except KeyboardInterrupt:return 130
    except Exception as error:
        print(json.dumps(dict(phase='failed',reason=failure_reason(error))),flush=True)
        return 130 if 'job' in locals() and getattr(job,'phase',None)=='cancelled' else 2
    finally:
        if 'profile' in locals() and '_browser_runtime' in profile:profile['_browser_runtime'].close()


if __name__=='__main__':raise SystemExit(main())
