"""Resumable original-document delivery, independent of audio completion."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from email.message import Message
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import unquote, urlsplit
import urllib.request
import unicodedata
import uuid
import zipfile
from icloud_bridge import uploaded
from cloud_delivery import atomic_json
from course_catalog import path_parts
from file_delivery import FilePublisher
from pipeline import Pipeline
from transfer_errors import failure_reason

MEDIA_SUFFIXES = {'.mp3','.m4a','.aac','.wav','.ogg','.opus','.mp4','.mkv','.webm','.m3u8'}


def document_resource(raw):
    if not isinstance(raw,dict):raise ValueError('invalid-document-resource')
    value=dict(raw)
    name=value.get('name')
    mime=value.get('mime') or ''
    if not isinstance(mime,str) or (name is not None and not isinstance(name,str)):
        raise ValueError('invalid-document-resource')
    if mime.startswith(('audio/','video/')) or (name and Path(name).suffix.lower() in MEDIA_SUFFIXES):return None
    if name is not None:path_parts(['resource',name])
    url=value.get('url')
    if url:
        p=urlsplit(url)
        if p.scheme!='https' or not p.hostname or p.username or p.password:raise ValueError('invalid-document-url')
    ident=value.get('id')
    if not ident:
        if not url:raise ValueError('missing-document-identity')
        ident=hashlib.sha256(url.encode()).hexdigest()
    if not isinstance(ident,str) or not ident:raise ValueError('invalid-document-identity')
    value['id']=ident
    if 'shared_path_with' in value or 'shared_file_sha256' in value:
        peers=value.get('shared_path_with');digest=value.get('shared_file_sha256')
        if (not isinstance(peers,list) or not peers or
                any(not isinstance(peer,str) or not peer or peer==ident for peer in peers) or
                len(set(peers))!=len(peers) or not isinstance(digest,str) or
                not re.fullmatch('[0-9a-f]{64}',digest)):
            raise ValueError('invalid-shared-document-permission')
    size=value.get('size')
    if size is not None and (type(size) is not int or size<0):raise ValueError('invalid-document-size')
    return value


def validate_document_paths(resources):
    ids=[r['id'] for r in resources]
    if len(set(ids))!=len(ids):raise ValueError('ambiguous-document-identity-or-path')
    names={}
    for resource in resources:
        if resource.get('name'):
            key=unicodedata.normalize('NFD',resource['name']).casefold()
            names.setdefault(key,[]).append(resource)
    for group in names.values():
        if len(group)==1 and 'shared_path_with' not in group[0]:continue
        peers={r['id'] for r in group};hashes={r.get('shared_file_sha256') for r in group}
        if (len(group)<2 or len({r['name'] for r in group})!=1 or None in hashes or len(hashes)!=1 or
                any(set(r.get('shared_path_with',[]))!=peers-{r['id']} for r in group)):
            raise ValueError('ambiguous-document-identity-or-path')
    if any('shared_path_with' in r and not r.get('name') for r in resources):
        raise ValueError('invalid-shared-document-permission')


def document_preflight(lessons,profile,root,destination,cloud):
    """Validate saved document transactions without creating files or hydrating placeholders."""
    root,destination=Path(root).resolve(),Path(destination).resolve()
    if root==destination or root.is_relative_to(destination) or destination.is_relative_to(root):
        raise ValueError('overlapping-working-and-cloud-roots')
    path=root/'Documentacao/documents-journal.json'
    journal=json.loads(path.read_text()) if path.exists() else {}
    rows=[]
    for lesson in lessons:
        u=urlsplit(lesson['url'])
        if u.scheme+'://'+u.netloc!=profile['origin'].rstrip('/'):
            raise ValueError('catalogue-profile-origin-mismatch')
        if not set(profile.get('identity_expressions',{}))<=lesson.get('page_identity',{}).keys():
            raise ValueError('missing-catalogue-page-identity')
        source=Pipeline.identity(lesson['url']);count=0
        for receipt in journal.values():
            if receipt.get('source_identity')!=source:continue
            local=root.joinpath(*path_parts(lesson['site_path']),receipt['name'])
            target=destination.joinpath(*path_parts(lesson['site_path']),receipt['name'])
            path_parts(['resource',receipt['name']])
            if (local.resolve()!=local or target.resolve()!=target or receipt.get('schema')!='course-document/v1'
                    or receipt.get('source_url')!=lesson['url'] or receipt.get('final')!=str(local)
                    or receipt.get('cloud')!=str(target) or receipt.get('site_path')!=lesson['site_path']+[receipt['name']]
                    or receipt.get('source_hash')!=receipt.get('final_hash')):
                raise ValueError('document-receipt-identity-mismatch')
            if receipt['state'] in {'uploaded','complete'}:
                native=cloud.status(target)
                if (not uploaded(native) or native['size']!=receipt['size'] or
                        abs(native['mtime_ns']-receipt.get('uploaded_mtime_ns',-1))>1000):
                    raise ValueError('unconfirmed-cloud-document')
                if native.get('materialized') and Pipeline.sha256(target)!=receipt['final_hash']:
                    raise ValueError('cloud-document-hash-mismatch')
            elif receipt['state']!='prepared':raise ValueError('invalid-document-receipt-state')
            if local.exists() and (local.stat().st_size!=receipt['size'] or Pipeline.sha256(local)!=receipt['source_hash']):
                raise ValueError('document-source-hash-mismatch')
            if receipt['state']=='prepared' and not local.is_file():raise ValueError('missing-prepared-document')
            if receipt['state']=='complete' and local.exists():raise ValueError('unexpected-complete-document-source')
            count+=1
        rows.append(dict(source_identity=source,title=lesson['title'],receipts=count))
    return rows


class DocumentDelivery:
    def __init__(self, root, destination, cloud, adapter, concurrency=3,
                 reserve_bytes=4*1024**3, max_bytes=512*1024**2, event=None,
                 upload_timeout=900, poll_seconds=3):
        if not 1<=concurrency<=8:raise ValueError('invalid-document-concurrency')
        self.root,self.destination=Path(root).resolve(),Path(destination).resolve()
        self.publisher=FilePublisher(self.root,self.destination,cloud,reserve_bytes,
                                      upload_timeout,poll_seconds,event)
        self.adapter,self.concurrency,self.max_bytes=adapter,concurrency,max_bytes
        self.docs=self.root/'Documentacao';self.docs.mkdir(exist_ok=True)
        if self.docs.resolve()!=self.docs:raise ValueError('unsafe-document-path')
        self.journal_path=self.docs/'documents-journal.json'
        self.pages_path=self.docs/'documents-pages.json'
        self.journal=json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
        self.pages=json.loads(self.pages_path.read_text()) if self.pages_path.exists() else {}
        self.lock=threading.RLock();self.stop=self.publisher._stop
        self._shared_path_locks={}
        self.event=event or (lambda *args,**kwargs:None)
        self.selected=[];self.failures=[];self.total_lessons=0

    def status(self,stage,**extra):
        with self.lock:
            report=dict(pid=os.getpid(),at=time.time(),stage=stage,lessons=self.total_lessons,
                inventoried=sum(p.get('inventory_complete',False) for p in self.pages.values()),
                documents=len(self.selected),complete=sum(self.journal.get(i,{}).get('state')=='complete' for i in self.selected),
                failed=len(self.failures),free_gib=round(shutil.disk_usage(self.root).free/1024**3,3),**extra)
            atomic_json(self.docs/'documents-status.json',report)

    def cancel(self):self.publisher.cancel()

    def save(self, receipt):
        with self.lock:
            self.journal[receipt['identity']]=dict(receipt)
            atomic_json(self.journal_path,self.journal)

    def identity(self,lesson,resource):
        return Pipeline.identity(lesson['url'])+':'+hashlib.sha256(resource['id'].encode()).hexdigest()

    def paths(self,lesson,name):
        parts=path_parts(lesson['site_path'])+[path_parts(['resource',name])[-1]]
        local,target=self.root.joinpath(*parts),self.destination.joinpath(*parts)
        if (local.resolve()!=local or target.resolve()!=target or local.is_symlink() or target.is_symlink()):
            raise ValueError('unsafe-document-path')
        return parts,local,target

    def check(self,receipt,lesson,resource):
        parts,local,target=self.paths(lesson,receipt['name'])
        if (receipt.get('schema')!='course-document/v1' or
                receipt['identity']!=self.identity(lesson,resource) or
                receipt['resource_id']!=hashlib.sha256(resource['id'].encode()).hexdigest() or
                receipt['source_identity']!=Pipeline.identity(lesson['url']) or
                receipt['source_url']!=lesson['url'] or receipt['site_path']!=parts or
                receipt['final']!=str(local) or receipt['cloud']!=str(target) or
                (resource.get('name') is not None and resource['name']!=receipt['name']) or
                (resource.get('size') is not None and resource['size']!=receipt['size']) or
                receipt['source_hash']!=receipt['final_hash']):
            raise ValueError('document-receipt-identity-mismatch')
        peers=resource.get('shared_path_with',[])
        if (peers or receipt.get('shared_resource_ids')) and (
                receipt.get('shared_resource_ids')!=sorted(hashlib.sha256(p.encode()).hexdigest() for p in peers)
                or receipt['source_hash']!=resource.get('shared_file_sha256')):
            raise ValueError('document-receipt-identity-mismatch')
        return local,target

    def download(self,lesson,resource):
        if self.stop.is_set():raise RuntimeError('document-cancelled')
        url=self.adapter.url_for(lesson,resource)
        p=urlsplit(url)
        if p.scheme!='https' or not p.hostname or p.username or p.password:raise ValueError('invalid-document-url')
        staging=self.root/'.document-staging';staging.mkdir(exist_ok=True)
        if staging.resolve()!=staging:raise ValueError('unsafe-document-path')
        temporary=staging/(uuid.uuid4().hex+'.partial');total=0;digest=hashlib.sha256()
        expected=resource.get('size')
        if expected is not None and expected>self.max_bytes:raise RuntimeError('document-size-budget-exceeded')
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':lesson['url']})
            with urllib.request.urlopen(request,timeout=60) as response,temporary.open('xb') as output:
                end=urlsplit(response.geturl())
                if end.scheme!='https' or end.username or end.password:raise RuntimeError('insecure-document-redirect')
                mime=response.headers.get_content_type()
                if mime in ('text/html','application/xhtml+xml'):raise RuntimeError('document-login-or-html-response')
                if mime.startswith(('audio/','video/')):raise RuntimeError('document-is-media')
                declared=int(response.headers.get('Content-Length') or 0)
                if declared>self.max_bytes:raise RuntimeError('document-size-budget-exceeded')
                name=resource.get('name')
                if not name:
                    header=Message();header['Content-Disposition']=response.headers.get('Content-Disposition','')
                    name=header.get_filename() or unquote(urlsplit(url).path.rsplit('/',1)[-1])
                if not name:raise RuntimeError('missing-document-filename')
                parts,local,target=self.paths(lesson,name)
                if local.exists():raise RuntimeError('untracked-local-document')
                while True:
                    if self.stop.is_set():raise RuntimeError('document-cancelled')
                    chunk=response.read(65536)
                    if not chunk:break
                    total+=len(chunk)
                    if total>self.max_bytes:raise RuntimeError('document-size-budget-exceeded')
                    if shutil.disk_usage(self.root).free-len(chunk)<self.publisher.reserve_bytes:
                        raise RuntimeError('disk-reserve')
                    digest.update(chunk);output.write(chunk)
                output.flush();os.fsync(output.fileno())
            if (not total or (declared and total!=declared) or
                    (expected is not None and total!=expected)):raise RuntimeError('document-size-mismatch')
            if Path(name).suffix.lower()=='.pdf':
                with temporary.open('rb') as stream:
                    if stream.read(5)!=b'%PDF-':raise RuntimeError('invalid-document-format')
                    stream.seek(max(0,total-4096))
                    if b'%%EOF' not in stream.read():raise RuntimeError('invalid-document-format')
            required={'.docx':'word/document.xml','.xlsx':'xl/workbook.xml','.pptx':'ppt/presentation.xml'}.get(Path(name).suffix.lower())
            if required:
                with zipfile.ZipFile(temporary) as archive:
                    if (sum(i.file_size for i in archive.infolist())>1024**3 or
                            not {'[Content_Types].xml',required}<=set(archive.namelist()) or
                            archive.testzip() is not None):raise RuntimeError('invalid-document-format')
            if resource.get('shared_file_sha256') and digest.hexdigest()!=resource['shared_file_sha256']:
                raise RuntimeError('document-source-hash-mismatch')
            local.parent.mkdir(parents=True,exist_ok=True)
            if local.resolve()!=local or local.exists():raise RuntimeError('unsafe-document-path')
            # Exclusive link promotion cannot replace a concurrently created file.
            os.link(temporary,local);temporary.unlink()
            receipt=dict(schema='course-document/v1',identity=self.identity(lesson,resource),state='prepared',
                source_identity=Pipeline.identity(lesson['url']),source_url=lesson['url'],resource_id=hashlib.sha256(resource['id'].encode()).hexdigest(),
                name=name,site_path=parts,final=str(local),cloud=str(target),size=total,
                source_hash=digest.hexdigest(),final_hash=digest.hexdigest(),
                params={'mode':'passthrough','content_type':mime},prepared_at=time.time())
            if resource.get('shared_path_with'):
                receipt['shared_resource_ids']=sorted(hashlib.sha256(p.encode()).hexdigest() for p in resource['shared_path_with'])
            return receipt
        except (KeyboardInterrupt,SystemExit):raise
        except Exception as error:
            allowed={'document-cancelled','document-size-budget-exceeded','disk-reserve','document-size-mismatch',
                'invalid-document-format','document-login-or-html-response','document-is-media',
                'insecure-document-redirect','missing-document-filename','untracked-local-document','unsafe-document-path',
                'document-source-hash-mismatch'}
            raise RuntimeError(str(error) if str(error) in allowed else 'document-download-failed') from None

    def one(self,lesson,resource):
        if self.stop.is_set():raise RuntimeError('document-cancelled')
        if resource.get('shared_path_with'):
            resource=document_resource(resource)
            key=str(self.paths(lesson,resource['name'])[1])
            with self.lock:path_lock=self._shared_path_locks.setdefault(key,threading.Lock())
            with path_lock:
                if self.stop.is_set():raise RuntimeError('document-cancelled')
                return self._one(lesson,resource)
        return self._one(lesson,resource)

    def _one(self,lesson,resource):
        self.status('delivering',title=lesson['title'])
        ident=self.identity(lesson,resource)
        with self.lock:receipt=dict(self.journal[ident]) if ident in self.journal else None
        if receipt is None:receipt=self.download(lesson,resource);self.save(receipt)
        local,target=self.check(receipt,lesson,resource)
        if receipt['state']=='complete':
            self.publisher.confirm(receipt)
            if local.exists():raise RuntimeError('unexpected-complete-document-source')
            return receipt
        if receipt['state'] not in {'prepared','uploaded'}:raise ValueError('invalid-document-receipt-state')
        if receipt['state']=='prepared':
            if not local.is_file() or Pipeline.sha256(local)!=receipt['final_hash']:
                raise RuntimeError('document-source-hash-mismatch')
            self.publisher.publish(receipt);receipt['published_mtime_ns']=target.stat().st_mtime_ns;self.save(receipt)
        native=self.publisher.confirm(receipt)
        receipt.update(state='uploaded',uploaded_mtime_ns=native['mtime_ns'],native=native,upload_confirmed_at=time.time());self.save(receipt)
        if local.exists():
            if local.resolve()!=local or Pipeline.sha256(local)!=receipt['source_hash']:
                raise RuntimeError('document-cleanup-hash-mismatch')
            local.unlink()
        receipt.update(state='complete',completed_at=time.time());self.save(receipt)
        self.status('delivering')
        if native.get('materialized'):
            try:self.publisher.cloud.evict(target)
            except RuntimeError:receipt['eviction_deferred']=True;self.save(receipt)
        return receipt

    def run(self,lessons):
        self.selected=[];self.failures=[]
        failures=self.failures;futures={};selected=self.selected;self.total_lessons=len(lessons)
        consecutive_inventory_failures=0
        def collect(done):
            for f in done:
                owner=futures.pop(f)
                try:f.result()
                except Exception as error:
                    failures.append(owner);self.status('delivering',reason=failure_reason(error),failure_source_identity=owner)
        with (self.docs/'documents.lock').open('a') as runlock:
            try:fcntl.flock(runlock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('another-document-run-is-active') from None
            # A separate backfill may have finished since this object was constructed.
            self.journal=json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
            self.pages=json.loads(self.pages_path.read_text()) if self.pages_path.exists() else {}
            self.status('starting')
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                try:
                    for lesson in lessons:
                        if self.stop.is_set():raise KeyboardInterrupt()
                        source=Pipeline.identity(lesson['url'])
                        self.status('inventory',title=lesson['title'])
                        try:
                            resources=[r for raw in self.adapter.inventory(lesson) if (r:=document_resource(raw)) is not None]
                            ids=[self.identity(lesson,r) for r in resources]
                            validate_document_paths(resources)
                            consecutive_inventory_failures=0
                            selected.extend(ids);self.pages[source]={'source_url':lesson['url'],'resources':ids,'inventory_complete':True}
                            atomic_json(self.pages_path,self.pages)
                            for resource in resources:
                                while len(futures)>=self.concurrency:collect(wait(futures,return_when=FIRST_COMPLETED)[0])
                                if self.stop.is_set():raise KeyboardInterrupt()
                                futures[pool.submit(self.one,lesson,resource)]=source
                        except KeyboardInterrupt:raise
                        except Exception as error:
                            consecutive_inventory_failures+=1
                            failures.append(source);self.pages[source]={'source_url':lesson['url'],'inventory_complete':False,
                                'reason':failure_reason(error),'failed_at':time.time()};atomic_json(self.pages_path,self.pages)
                            self.status('inventory-failed',title=lesson['title'],reason=failure_reason(error))
                            if str(error)=='login-required':self.cancel();raise RuntimeError('login-required') from None
                            if consecutive_inventory_failures>=3:
                                self.cancel();raise RuntimeError('repeated-document-inventory-failure') from None
                    if futures:collect(wait(futures)[0])
                except KeyboardInterrupt:self.cancel();self.status('interrupted');raise
                except Exception as error:self.cancel();self.status('failed',reason=failure_reason(error));raise
        report={'ok':not failures,'lessons':len(lessons),'inventoried':sum(self.pages.get(Pipeline.identity(l['url']),{}).get('inventory_complete',False) for l in lessons),
                'documents':len(selected),'complete':sum(self.journal.get(i,{}).get('state')=='complete' for i in selected),'failed':len(failures)}
        atomic_json(self.docs/'documents-status.json',dict(report,at=time.time()))
        if failures:raise RuntimeError('document-delivery-incomplete')
        return report
