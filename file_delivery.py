"""Shared native iCloud file publication, independent of audio conversion."""
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid
from pipeline import Pipeline
from icloud_bridge import uploaded


class FilePublisher:
    def __init__(self, root, destination, cloud, reserve_bytes=4*1024**3,
                 upload_timeout=900, poll_seconds=3, event=None):
        self.root, self.destination = Path(root).resolve(), Path(destination).resolve()
        if (self.root == self.destination or self.root.is_relative_to(self.destination)
                or self.destination.is_relative_to(self.root)):
            raise ValueError('overlapping-working-and-cloud-roots')
        self.root.mkdir(parents=True, exist_ok=True)
        self.cloud, self.reserve_bytes = cloud, reserve_bytes
        self.upload_timeout, self.poll_seconds = upload_timeout, poll_seconds
        self._stop = threading.Event()
        self.event = event or (lambda *args, **kwargs: None)

    def cancel(self):
        self._stop.set()

    def publish(self, receipt):
        final, target = Path(receipt['final']), Path(receipt['cloud'])
        self.event('copy-to-icloud')
        if target.exists():
            if Pipeline.sha256(target) != receipt['final_hash']:
                raise RuntimeError('cloud-destination-conflict')
            return
        if not final.is_file() or Pipeline.sha256(final) != receipt['final_hash']:
            raise RuntimeError('local-final-hash-mismatch')
        if shutil.disk_usage(self.root).free - receipt['size'] < self.reserve_bytes:
            raise RuntimeError('disk-reserve')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name('.course-' + uuid.uuid4().hex + '.partial')
        try:
            with final.open('rb') as source, temporary.open('xb') as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            if Pipeline.sha256(temporary) != receipt['final_hash']:
                raise RuntimeError('cloud-copy-hash-mismatch')
            if sys.platform == 'darwin':
                import ctypes
                native = ctypes.CDLL(None, use_errno=True)
                if native.renamex_np(os.fsencode(temporary), os.fsencode(target), 4):
                    raise RuntimeError('cloud-exclusive-promotion-failed')
            else:
                os.link(temporary, target)  # Refuse replacing an existing destination.
                temporary.unlink()
        finally:
            temporary.unlink(missing_ok=True)

    def confirm(self, receipt):
        target = Path(receipt['cloud'])
        self.event('wait-for-upload')
        deadline = time.monotonic() + self.upload_timeout
        while True:
            status = self.cloud.status(target)
            if status.get('upload_error') or status.get('conflicts'):
                raise RuntimeError('icloud-upload-error-or-conflict')
            if uploaded(status):
                if status['size'] != receipt['size']:
                    raise RuntimeError('cloud-size-mismatch')
                if status.get('materialized'):
                    if Pipeline.sha256(target) != receipt['final_hash']:
                        raise RuntimeError('cloud-final-hash-mismatch')
                # iCloud round-trips timestamps through floating-point dates;
                # eviction on the live Mac shifted nanoseconds without changing bytes.
                elif abs(receipt.get('uploaded_mtime_ns', receipt.get('published_mtime_ns', -10**30))
                         - status['mtime_ns']) > 1000:
                    raise RuntimeError('cloud-receipt-metadata-mismatch')
                return status
            if self._stop.is_set():
                raise RuntimeError('delivery-cancelled')
            if time.monotonic() >= deadline:
                raise RuntimeError('icloud-upload-timeout')
            time.sleep(self.poll_seconds)
