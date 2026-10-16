"""Course-neutral authenticated audio delivery and read-only preflight."""
from __future__ import annotations
import argparse
import fcntl
import json
import math
import shutil
import threading
from pathlib import Path
from browser_adapter import SafariAdapter
from embedded_adapter import EmbeddedAdapter
from template_adapter import TemplateAdapter
from extractor_adapter import ExtractorAdapter
from course_catalog import load_lessons,relative_audio_path
from site_profile import load_profile
from cloud_delivery import Delivery,NativeCloud,Progress
from pipeline import Pipeline,PRESET
from icloud_bridge import uploaded
from documents import DocumentDelivery, document_preflight
from document_browser import DocumentBrowser


def build_adapter(profile,encoder='aac_at'):
    cls={'direct':SafariAdapter,'embedded-json':EmbeddedAdapter,'template-hls':TemplateAdapter,'extractor':ExtractorAdapter}[profile['mode']]
    return cls(profile=profile,encoder=encoder)


def preflight(lessons,profile,root,destination,cloud):
    root,destination=Path(root).resolve(),Path(destination).resolve()
    if destination==root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('overlapping-working-and-cloud-roots')
    journal_path=root/'Documentacao/delivery-journal.json'
    journal=json.loads(journal_path.read_text()) if journal_path.exists() else {}
    rows=[]
    for lesson in lessons:
        parsed=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(lesson['url'])
        if parsed.scheme+'://'+parsed.netloc!=profile['origin'].rstrip('/'):
            raise ValueError('catalogue-profile-origin-mismatch')
        if not set(profile.get('identity_expressions',{}))<=lesson.get('page_identity',{}).keys():
            raise ValueError('missing-catalogue-page-identity')
        ident=Pipeline.identity(lesson['url']);relative=relative_audio_path(lesson)
        target=destination/relative;final=root/relative;receipt=journal.get(ident);state='pending'
        if target.resolve()!=target or final.resolve()!=final:
            raise ValueError('unsafe-output-symlink')
        if receipt:
            original=Path(receipt['original'])
            if (receipt.get('schema')!=1 or receipt.get('params')!=PRESET or receipt.get('source_identity')!=ident or
                    receipt.get('title')!=lesson['title'] or Path(receipt['cloud']).resolve()!=target or
                    Path(receipt['final']).resolve()!=final or original.is_symlink() or
                    original.parent.resolve()!=final.parent/'originals' or
                    original.stem not in {final.stem,final.stem+'-'+receipt['source_hash'][:12]}):
                raise ValueError('receipt-identity-mismatch')
            if receipt.get('artwork_update'):raise ValueError('unfinished-artwork-transaction')
            if receipt['state'] in {'complete','uploaded'}:
                status=cloud.status(target)
                if (not uploaded(status) or status['size']!=receipt['size'] or
                        abs(status['mtime_ns']-receipt.get('uploaded_mtime_ns',receipt.get('published_mtime_ns',-1)))>1000):
                    raise ValueError('unconfirmed-cloud-receipt')
                if status.get('materialized') and Pipeline.sha256(target)!=receipt['final_hash']:
                    raise ValueError('cloud-receipt-hash-mismatch')
                state=receipt['state']
                if state=='uploaded':
                    for file,key in [(original,'source_hash'),(final,'final_hash')]:
                        if file.exists() and Pipeline.sha256(file)!=receipt[key]:
                            raise ValueError('uploaded-receipt-local-hash-mismatch')
            else:
                if receipt['state']!='prepared':raise ValueError('unsupported-receipt-state')
                if Pipeline.sha256(original)!=receipt['source_hash'] or Pipeline.sha256(final)!=receipt['final_hash']:
                    raise ValueError('prepared-receipt-hash-mismatch')
                state='prepared'
        elif target.exists():raise ValueError('untracked-cloud-output')
        rows.append(dict(source_identity=ident,title=lesson['title'],path=str(relative),state=state))
    return rows


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalog',required=True);p.add_argument('--profile',required=True)
    p.add_argument('--root','--output-root',dest='root',required=True)
    p.add_argument('--destination',required=True);p.add_argument('--icloud-python',required=True)
    p.add_argument('--concurrency',type=int,default=2,choices=range(1,9))
    p.add_argument('--encoder',choices=['aac','aac_at'],default='aac_at')
    p.add_argument('--limit',type=int);p.add_argument('--start',type=int,default=0)
    p.add_argument('--min-free-gib',type=float,default=4)
    p.add_argument('--upload-timeout',type=float,default=900,
                   help='Maximum seconds to wait for iCloud upload confirmation (1–21600)')
    p.add_argument('--dry-run',action='store_true');p.add_argument('--probe',type=int,default=0)
    p.add_argument('--documents-only',action='store_true',help='Backfill original lesson documents without audio processing')
    p.add_argument('--refresh-completed-artwork',action='store_true',
                   help='Explicitly retry missing covers on completed native uploads before delivery')
    args=p.parse_args(argv)
    if (args.start<0 or (args.limit is not None and args.limit<1) or args.min_free_gib<4
            or args.probe<0 or not math.isfinite(args.upload_timeout)
            or not 1<=args.upload_timeout<=21600):
        p.error('invalid numeric boundary')
    profile=load_profile(args.profile);all_lessons=load_lessons(args.catalog)
    lessons=all_lessons[args.start:args.start+args.limit if args.limit is not None else None]
    if not lessons:p.error('no lessons selected')
    root=Path(args.root);destination=Path(args.destination)
    if args.dry_run:
        # No Delivery/Progress construction, mkdir, lock creation, downloads or cleanup.
        cloud=NativeCloud(args.icloud_python)
        document_rows=document_preflight(lessons,profile,root,destination,cloud)
        if args.documents_only:
            browser=DocumentBrowser(profile=profile) if args.probe else None
            for lesson in lessons[:args.probe]:browser.inventory(lesson)
            print(json.dumps(dict(schema='course-documents-dry-run/v1',ok=True,total=len(lessons),
                probed=min(args.probe,len(lessons)),lessons=document_rows),ensure_ascii=False));return 0
        rows=preflight(lessons,profile,root,destination,cloud)
        pending=[lesson for lesson,row in zip(lessons,rows) if row['state']!='complete']
        adapter=build_adapter(profile,args.encoder) if args.probe else None
        for lesson in pending[:args.probe]:
            if not adapter.resolve(lesson):raise RuntimeError('empty-probe-result')
        report=dict(schema='course-dry-run/v1',ok=True,total=len(rows),
                    complete=sum(x['state']=='complete' for x in rows),uploaded=sum(x['state']=='uploaded' for x in rows),prepared=sum(x['state']=='prepared' for x in rows),
                    pending=sum(x['state']=='pending' for x in rows),probed=min(args.probe,len(pending)),
                    concurrency=args.concurrency,lessons=rows)
        report['document_lessons']=document_rows
        print(json.dumps(report,ensure_ascii=False));return 0
    documents=DocumentDelivery(root,destination,NativeCloud(args.icloud_python),
        adapter=DocumentBrowser(profile=profile),concurrency=args.concurrency,
        reserve_bytes=int(args.min_free_gib*1024**3))
    if args.documents_only:
        try:documents.run(lessons);return 0
        except KeyboardInterrupt:documents.cancel();return 130
        except Exception:return 2
    docs=root/'Documentacao';docs.mkdir(parents=True,exist_ok=True)
    with (docs/'delivery.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'reason':'another-run-is-active'}));return 2
        progress=Progress(root,selected=len(lessons),concurrency=args.concurrency)
        delivery=Delivery(root,destination,NativeCloud(args.icloud_python),adapter=build_adapter(profile,args.encoder),
                          reserve_bytes=int(args.min_free_gib*1024**3),concurrency=args.concurrency,
                          upload_timeout=args.upload_timeout,event=progress.event)
        progress.current['delivered']=delivery.completed_count()
        work=[(lesson,destination/relative_audio_path(lesson)) for lesson in lessons]
        stopped=threading.Event()
        def heartbeat():
            while not stopped.wait(15):progress.event(heartbeat=True)
        worker=threading.Thread(target=heartbeat,daemon=True);worker.start()
        try:
            for lesson,target in work:
                receipt=delivery.journal.get(Pipeline.identity(lesson['url']),{})
                if (args.refresh_completed_artwork and receipt.get('state')=='complete'
                        and receipt.get('artwork',{}).get('origin')=='none'):
                    delivery.refresh_completed_artwork(lesson,target)
            progress.event('running');delivery.run(work,start_index=args.start)
            documents.run(lessons)
            progress.event('finished',delivered=delivery.completed_count());return 0
        except KeyboardInterrupt:
            delivery.cancel();documents.cancel();progress.event('interrupted',delivered=delivery.completed_count());return 130
        except Exception:
            progress.event('failed',reason='course-delivery-failed',delivered=delivery.completed_count());return 2
        finally:stopped.set();worker.join(timeout=2)


if __name__=='__main__':raise SystemExit(main())
