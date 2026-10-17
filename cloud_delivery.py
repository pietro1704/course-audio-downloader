"""Bounded course downloads, confirmed iCloud delivery and scoped local cleanup."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import course_audio_bot as audio
import artwork
from icloud_bridge import uploaded
from file_delivery import FilePublisher
from pipeline import Pipeline, PRESET
from browser_adapter import SafariAdapter, load_lessons, normalize_title


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class NativeCloud:
    def __init__(self, python):
        self.python = str(python)

    def call(self, operation, path):
        result = subprocess.run([self.python, str(Path(__file__).with_name('icloud_bridge.py')),
                                 operation, str(path)], capture_output=True, text=True, timeout=60)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError('icloud-bridge-unavailable') from None
        if result.returncode or value.get('error'):
            raise RuntimeError('icloud-native-query-failed')
        return value

    def status(self, path):
        return self.call('status', path)

    def evict(self, path):
        return self.call('evict', path)


class ObservedAdapter:
    def __init__(self, adapter, event):
        self.adapter, self.event = adapter, event

    def resolve(self, lesson):
        self.event('resolve')
        return self.adapter.resolve(lesson)

    def download(self, candidate, destination):
        self.event('download')
        return self.adapter.download(candidate, destination)

    def encode(self, source, destination):
        self.event('encode')
        self.adapter.encode(source, destination)
        self.event('validate')

    def thumbnail(self, candidate, folder):
        fallback = getattr(self.adapter, 'thumbnail', None)
        return fallback(candidate, folder) if fallback else None


class Delivery(FilePublisher):
    def __init__(self, root, destination, cloud, adapter=None, reserve_bytes=4 * 1024**3,
                 upload_timeout=900, poll_seconds=3, event=None, concurrency=1):
        self.root = Path(root).resolve()
        self.destination = Path(destination).resolve()
        if self.destination == self.root or self.destination.is_relative_to(self.root):
            raise ValueError('cloud destination must be outside the working root')
        self.root.mkdir(parents=True, exist_ok=True)
        self.docs = self.root / 'Documentacao'
        self.docs.mkdir(exist_ok=True)
        self.journal_path = self.docs / 'delivery-journal.json'
        self.journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
        self._journal_lock = threading.RLock()
        self._context = threading.local()
        self._inflight = set()
        self._stop = threading.Event()
        self._failure = None
        self.concurrency = concurrency
        self.cloud, self.upload_timeout, self.poll_seconds = cloud, upload_timeout, poll_seconds
        self.event_callback = event or (lambda stage, **fields: None)
        self.adapter = ObservedAdapter(adapter or SafariAdapter(), self.event)
        self.pipeline = Pipeline(self.root, self.adapter, concurrency=concurrency,
                                 reserve_bytes=reserve_bytes, task_budget_bytes=512 * 1024**2)
        self.reserve_bytes = reserve_bytes
        self.legacy = {}
        legacy_path = self.root / 'worker-manifest.jsonl'
        if legacy_path.exists():
            for line in legacy_path.read_text().splitlines():
                record = json.loads(line)
                if record.get('status') == 'ok' and record.get('url'):
                    self.legacy[Pipeline.identity(record['url'])] = record

    def event(self, stage, **fields):
        self.event_callback(stage, **{**getattr(self._context, 'fields', {}), **fields})

    def save(self, receipt):
        with self._journal_lock:
            # Worker-owned receipts are copied so another worker cannot mutate a
            # dictionary while the shared journal is being serialized.
            self.journal[receipt['source_identity']] = dict(receipt)
            atomic_json(self.journal_path, self.journal)

    def completed_count(self):
        with self._journal_lock:
            return sum(r.get('state') == 'complete' for r in self.journal.values())

    def cancel(self):
        self._stop.set()
        self.pipeline._stop.set()

    def paths(self, lesson):
        folder, stem = self.pipeline._paths(lesson, Pipeline.identity(lesson['url']))
        return folder.resolve(), folder / (stem + '.m4a')

    def check_receipt(self, receipt, lesson, target):
        folder, final = self.paths(lesson)
        original = Path(receipt['original'])
        if (receipt.get('schema') != 1 or receipt.get('params') != PRESET
                or receipt.get('source_identity') != Pipeline.identity(lesson['url'])
                or receipt.get('title') != lesson['title']
                or Path(receipt['cloud']).resolve() != target.resolve()
                or Path(receipt['final']).resolve() != final.resolve()
                or original.parent.resolve() != folder / 'originals'
                or original.stem not in {final.stem, final.stem + '-' + receipt['source_hash'][:12]}
                or original.is_symlink() or final.is_symlink() or target.is_symlink()
                or not target.resolve().is_relative_to(self.destination)):
            raise RuntimeError('delivery-identity-mismatch')

    def prepare(self, lesson, target):
        ident = Pipeline.identity(lesson['url'])
        folder, final = self.paths(lesson)
        record = self.pipeline._records.get(ident)
        if record and self.pipeline._resume(record, lesson):
            if Path(record['final']).resolve() != final.resolve():
                raise RuntimeError('delivery-identity-mismatch')
        elif final.exists():
            self.event('validate-existing')
            old = self.legacy.get(ident, {})
            original = Path(old.get('original', folder / 'missing'))
            audit = old.get('audit', {})
            if (not old.get('validated') or not old.get('identity_audited')
                    or old.get('title') != lesson['title']
                    or Path(old.get('final', folder / 'missing')).resolve() != final.resolve()
                    or original.parent.resolve() != folder / 'originals'
                    or original.is_symlink() or final.is_symlink() or not original.is_file()):
                raise RuntimeError('untrusted-existing-final')
            source_hash = Pipeline.sha256(original)
            if source_hash != (audit.get('source_sha256') or old.get('sha256')):
                raise RuntimeError('legacy-source-hash-mismatch')
            if audit:
                if (Pipeline.identity(audit.get('url', '')) != ident
                        or normalize_title(audit.get('title', '')) != normalize_title(lesson['title'])):
                    raise RuntimeError('legacy-identity-mismatch')
            else:
                # Legacy flags alone do not prove identity: resolve the current lesson,
                # fetch its source and compare bytes before reusing the old encoding.
                self.event('audit-existing-source')
                with self.pipeline._resolve_lock:
                    candidates = self.pipeline.rank(self.adapter.resolve(lesson))
                if not candidates:
                    raise RuntimeError('no-audio-candidate')
                temporary = folder / ('.identity-' + uuid.uuid4().hex + original.suffix)
                try:
                    self.adapter.download(candidates[0], temporary)
                    if Pipeline.sha256(temporary) != source_hash:
                        raise RuntimeError('legacy-current-source-mismatch')
                finally:
                    temporary.unlink(missing_ok=True)
            valid, _ = audio.verify_audio_output(final, source=original)
            if not valid:
                raise RuntimeError('existing-final-validation-failed')
            record = dict(original=str(original), final=str(final), source_hash=source_hash,
                          final_hash=Pipeline.sha256(final), timings={})
        else:
            record = self.pipeline.one(lesson)
            if record['status'] != 'ok':
                raise RuntimeError('lesson-pipeline-failed')
        receipt = {'schema': 1, 'state': 'prepared', 'source_identity': ident,
                   'title': lesson['title'], 'params': PRESET, 'cloud': str(target),
                   **{key: record[key] for key in ('original', 'final', 'source_hash', 'final_hash')},
                   'size': final.stat().st_size, 'pipeline_timings': record.get('timings', {})}
        receipt['audio_seconds'] = record.get('duration_seconds')
        if record.get('artwork'):
            receipt['artwork'] = dict(record['artwork'])
        self.check_receipt(receipt, lesson, target)
        self.save(receipt)
        return receipt



    def cleanup(self, receipt):
        self.event('cleanup-local')
        # Never unlink the iCloud destination. Each local path and hash was bound to
        # this lesson, and the uploaded receipt was fsynced before reaching here.
        for key, hash_key in [('original', 'source_hash'), ('final', 'final_hash')]:
            path = Path(receipt[key])
            if path.exists():
                if path.is_symlink() or Pipeline.sha256(path) != receipt[hash_key]:
                    raise RuntimeError('cleanup-hash-mismatch')
                size = path.stat().st_size
                path.unlink()
                receipt['freed_bytes'] = receipt.get('freed_bytes', 0) + size
                self.save(receipt)

    def process(self, lesson, target, index=None, *, reconcile_original_artwork=False):
        ident = Pipeline.identity(lesson['url'])
        with self._journal_lock:
            if ident in self._inflight:
                raise ValueError('duplicate input lesson identities')
            self._inflight.add(ident)
        self._context.fields = dict(source_identity=ident, title=lesson['title'])
        if index is not None:
            self._context.fields['index'] = index
        try:
            self.event('start-lesson')
            if self._stop.is_set():
                raise RuntimeError('delivery-cancelled')
            if reconcile_original_artwork:
                self.recover_original_artwork(lesson, target)
            return self._process(lesson, target)
        except BaseException as error:
            with self._journal_lock:
                if self._failure is None and str(error) != 'delivery-cancelled':
                    self._failure = error
            self.cancel()
            self.event('cancelled' if str(error) == 'delivery-cancelled' else 'failed',
                       error_type=type(error).__name__)
            raise
        finally:
            with self._journal_lock:
                self._inflight.discard(ident)
            self._context.fields = {}

    def recover_original_artwork(self, lesson, target):
        """Recover pre-policy originals before upgrading a confirmed receipt.

        Existing source hashes remain authoritative. Never substitute a new
        recording, re-encode the delivered audio, or discard failed downloads.
        The normal prepared-artwork transaction handles publication and cleanup.
        """
        ident=Pipeline.identity(lesson['url'])
        with self._journal_lock:
            receipt=dict(self.journal[ident])
        self.check_receipt(receipt,lesson,Path(target))
        if receipt.get('artwork') or receipt.get('artwork_update') or receipt['state']=='prepared':return
        if receipt['state'] not in {'complete','uploaded'}:raise RuntimeError('invalid-artwork-recovery-state')
        self.confirm(receipt)
        original,final=Path(receipt['original']),Path(receipt['final'])
        maximum=getattr(self.adapter.adapter,'max_source_bytes',512*1024**2)
        budget=maximum+3*receipt['size']+16*1024**2
        if not self.pipeline._reserve(budget):raise RuntimeError('disk-reserve')
        try:
            folder=final.parent/'.staging'/('recover-artwork-'+uuid.uuid4().hex)
            folder.mkdir(parents=True)
            if not original.exists():
                with self.pipeline._resolve_lock:
                    if self._stop.is_set():raise RuntimeError('delivery-cancelled')
                    candidates=self.pipeline.rank(self.adapter.resolve(lesson))
                if not candidates:raise RuntimeError('no-audio-candidate')
                got=Path(self.adapter.download(candidates[0],folder/('source'+original.suffix)))
                if got.stat().st_size>maximum:raise RuntimeError('source-size-budget-exceeded')
                if Pipeline.sha256(got)!=receipt['source_hash']:
                    raise RuntimeError('artwork-original-content-changed')
                if self._stop.is_set():raise RuntimeError('delivery-cancelled')
                original.parent.mkdir(parents=True,exist_ok=True)
                os.link(got,original);got.unlink()
            if Pipeline.sha256(original)!=receipt['source_hash']:
                raise RuntimeError('artwork-original-content-changed')
            if not final.exists():
                staged=folder/'previous.m4a'
                with Path(target).open('rb') as source,staged.open('xb') as output:
                    shutil.copyfileobj(source,output);output.flush();os.fsync(output.fileno())
                if Pipeline.sha256(staged)!=receipt['final_hash']:
                    raise RuntimeError('local-final-hash-mismatch')
                os.link(staged,final);staged.unlink()
            if Pipeline.sha256(final)!=receipt['final_hash']:
                raise RuntimeError('local-final-hash-mismatch')
            if self._stop.is_set():raise RuntimeError('delivery-cancelled')
            receipt['state']='prepared';receipt.pop('uploaded_mtime_ns',None)
            receipt['original_artwork_recovered_at']=time.time()
            self.save(receipt)
            folder.rmdir()
        finally:
            with self.pipeline._state_lock:self.pipeline._reserved-=budget

    def _process(self, lesson, target):
        start = time.monotonic()
        target = Path(target)
        ident = Pipeline.identity(lesson['url'])
        with self._journal_lock:
            stored = self.journal.get(ident)
            receipt = dict(stored) if stored else None
        if receipt is None:
            receipt = self.prepare(lesson, target)
        self.check_receipt(receipt, lesson, target)
        if self._stop.is_set():
            raise RuntimeError('delivery-cancelled')
        if receipt.get('artwork_update') or (receipt['state'] == 'prepared' and not receipt.get('artwork')):
            self.upgrade_pending_artwork(receipt, lesson)
        if receipt['state'] == 'prepared':
            self.publish(receipt)
            receipt['published_mtime_ns'] = target.stat().st_mtime_ns
            self.save(receipt)
        status = self.confirm(receipt)
        receipt.update(state='uploaded', uploaded_mtime_ns=status['mtime_ns'],
                       upload_confirmed_at=time.time())
        self.save(receipt)
        self.cleanup(receipt)
        receipt.update(state='complete', last_cycle_seconds=round(time.monotonic() - start, 3))
        self.save(receipt)
        if status.get('materialized'):
            self.event('evict-icloud-cache')
            try:
                self.cloud.evict(target)
            except RuntimeError:
                self.event('icloud-cache-eviction-deferred')
        self.event('complete', freed_bytes=receipt.get('freed_bytes', 0),
                   cycle_seconds=receipt['last_cycle_seconds'],
                   delivered=self.completed_count())
        return receipt

    def refresh_completed_artwork(self, lesson, target):
        """Explicitly retry missing artwork on a confirmed, already cleaned delivery."""
        ident = Pipeline.identity(lesson['url'])
        receipt = dict(self.journal[ident])
        target = Path(target)
        self.check_receipt(receipt, lesson, target)
        if receipt.get('artwork_update'):
            return self.process(lesson, target)
        if receipt['state'] != 'complete' or receipt.get('artwork', {}).get('origin') != 'none':
            raise RuntimeError('artwork-refresh-not-applicable')
        self.confirm(receipt)
        final = Path(receipt['final'])
        if not final.exists():
            if shutil.disk_usage(self.root).free - 3 * receipt['size'] - 16 * 1024**2 < self.reserve_bytes:
                raise RuntimeError('disk-reserve')
            final.parent.mkdir(parents=True, exist_ok=True)
            with target.open('rb') as source, final.open('xb') as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
        if Pipeline.sha256(final) != receipt['final_hash']:
            raise RuntimeError('local-final-hash-mismatch')
        self.upgrade_pending_artwork(receipt, lesson, completed=True)
        return self.process(lesson, target)

    def upgrade_pending_artwork(self, receipt, lesson, completed=False):
        """Resume pre-policy pending deliveries, preserving audio and crash recovery."""
        final, target, original = map(Path, (receipt['final'], receipt['cloud'], receipt['original']))
        update = receipt.get('artwork_update')
        if update is None:
            if shutil.disk_usage(self.root).free - 2 * receipt['size'] - 16 * 1024**2 < self.reserve_bytes:
                raise RuntimeError('disk-reserve')
            source = final if completed else original
            if (Pipeline.sha256(final) != receipt['final_hash']
                    or (not completed and Pipeline.sha256(original) != receipt['source_hash'])):
                raise RuntimeError('cleanup-hash-mismatch')
            self.event('preserve-pending-artwork')
            folder = final.parent / '.staging' / ('artwork-' + uuid.uuid4().hex)
            folder.mkdir(parents=True)
            staged = folder / 'encoded.m4a'
            shutil.copyfile(final, staged)
            # Fetch player metadata only if the original has no attached cover.
            candidate = None
            if artwork.embedded(source, folder) is None:
                with self.pipeline._resolve_lock:
                    candidates = self.pipeline.rank(self.adapter.resolve(lesson))
                candidate = candidates[0] if candidates else None
            cover = artwork.apply(source, staged, candidate, self.adapter, folder)
            if not audio.verify_audio_output(staged, source=source)[0]:
                raise RuntimeError('existing-final-validation-failed')
            update = dict(previous_hash=receipt['final_hash'], final_hash=Pipeline.sha256(staged),
                          size=staged.stat().st_size, staged=str(staged), artwork=cover)
            receipt['artwork_update'] = update
            if completed:
                receipt['state'] = 'prepared'
                receipt.pop('uploaded_mtime_ns', None)
            self.save(receipt)  # Intent precedes either local or cloud replacement.
        staged = Path(update['staged'])
        if (update['previous_hash'] != receipt['final_hash'] or staged.is_symlink()
                or not staged.resolve().is_relative_to(final.parent / '.staging')
                or staged.parent.is_symlink() or staged.parent.parent != final.parent / '.staging'
                or not staged.parent.name.startswith('artwork-') or staged.name != 'encoded.m4a'):
            raise RuntimeError('delivery-identity-mismatch')
        allowed = {update['previous_hash'], update['final_hash']}
        local_hash = Pipeline.sha256(final)
        if local_hash not in allowed or (target.exists() and Pipeline.sha256(target) not in allowed):
            raise RuntimeError('cloud-destination-conflict')
        if local_hash != update['final_hash']:
            if Pipeline.sha256(staged) != update['final_hash']:
                raise RuntimeError('local-final-hash-mismatch')
            os.replace(staged, final)
        if target.exists() and Pipeline.sha256(target) != update['final_hash']:
            temporary = target.with_name('.course-artwork-' + uuid.uuid4().hex + '.partial')
            try:
                with final.open('rb') as source, temporary.open('xb') as output:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                if Pipeline.sha256(temporary) != update['final_hash']:
                    raise RuntimeError('cloud-copy-hash-mismatch')
                if target.is_symlink() or Pipeline.sha256(target) != update['previous_hash']:
                    raise RuntimeError('cloud-destination-conflict')
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        receipt.update(final_hash=update['final_hash'], size=update['size'], artwork=update['artwork'])
        if target.exists():
            receipt['published_mtime_ns'] = target.stat().st_mtime_ns
        receipt.pop('artwork_update')
        self.save(receipt)
        shutil.rmtree(staged.parent)

    def run(self, work, start_index=0, *, reconcile_original_artwork=False):
        work = list(work)
        identities = [Pipeline.identity(lesson['url']) for lesson, _ in work]
        if len(set(identities)) != len(identities):
            raise ValueError('duplicate input lesson identities')
        local_paths = [str(self.paths(lesson)[1].resolve()) for lesson, _ in work]
        cloud_paths = [str(Path(target).resolve()) for _, target in work]
        if len(set(local_paths)) != len(work) or len(set(cloud_paths)) != len(work):
            raise ValueError('duplicate output paths')
        iterator = iter(enumerate(work, start_index))
        results = []
        pool = ThreadPoolExecutor(max_workers=self.concurrency)
        pending = set()
        def submit():
            if self._stop.is_set():
                return False
            try:
                index, (lesson, target) = next(iterator)
            except StopIteration:
                return False
            kwargs={'reconcile_original_artwork':True} if reconcile_original_artwork else {}
            pending.add(pool.submit(self.process, lesson, target, index, **kwargs))
            return True
        try:
            for _ in range(self.concurrency):
                submit()
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        results.append(future.result())
                    except BaseException:
                        self.cancel()
                if not self._stop.is_set():
                    for _ in done:
                        submit()
        except BaseException:
            self.cancel()
            for future in pending:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if self._failure is not None:
            raise self._failure
        if self._stop.is_set():
            raise RuntimeError('delivery-cancelled')
        return results


def destinations(catalog, root):
    from course_catalog import relative_audio_path
    return [(lesson, Path(root) / relative_audio_path(lesson)) for lesson in load_lessons(catalog)]


class Progress:
    """One atomic status snapshot with independent state for each active lesson."""
    def __init__(self, root, **initial):
        self.root = Path(root)
        self.path = self.root / 'Documentacao/delivery-status.json'
        self.lock = threading.RLock()
        self.current = {'pid': os.getpid(), 'index': 0, 'delivered': 0, 'concurrency': 1, **initial}
        self.active = {}
        self.run_stage = 'running'
        self.maximum = 0

    def event(self, stage=None, **fields):
        with self.lock:
            now = time.time()
            ident = fields.get('source_identity')
            if ident:
                row = dict(self.active.get(ident, {}))
                if row.get('stage') != stage:
                    row['stage_started_at'] = now
                row.update(fields, stage=stage)
                for key in ('index', 'title', 'source_identity'):
                    if key in fields:
                        self.current[key] = fields[key]
                if stage in {'complete', 'failed', 'cancelled'}:
                    self.active.pop(ident, None)
                else:
                    self.active[ident] = row
            elif stage is not None:
                self.run_stage = stage
            if 'delivered' in fields:
                self.current['delivered'] = max(self.current['delivered'], fields['delivered'])
            slow_after = {'resolve': 90, 'download': 180, 'encode': 180,
                          'validate': 60, 'validate-existing': 60, 'wait-for-upload': 180}
            active = []
            for row in self.active.values():
                row = dict(row)
                row['stage_seconds'] = round(now - row['stage_started_at'], 1)
                row['slow_stage'] = row['stage_seconds'] > slow_after.get(row['stage'], float('inf'))
                active.append(row)
            self.maximum = max(self.maximum, len(active))
            overall = self.run_stage
            if overall == 'running' and active:
                overall = active[0]['stage'] if self.current['concurrency'] == 1 else 'parallel'
            report = dict(self.current, stage=overall, observed_at=now,
                          heartbeat=fields.get('heartbeat', False), active=active,
                          active_count=len(active), max_observed_active=self.maximum,
                          stage_seconds=max((r['stage_seconds'] for r in active), default=0),
                          slow_stage=any(r['slow_stage'] for r in active),
                          free_gib=round(shutil.disk_usage(self.root).free / 2**30, 3))
            if stage is not None:
                report['event'] = stage
                if ident:
                    report['task'] = fields
                elif fields:
                    report.update(fields)
            atomic_json(self.path, report)
            print(json.dumps(report, ensure_ascii=False), flush=True)


def main(argv=None):
    from course_cli import main as run
    return run(argv)


if __name__ == '__main__':
    raise SystemExit(main())
