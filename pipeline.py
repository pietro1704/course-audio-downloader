"""Bounded course-audio pipeline. Resolvers are serialized; media work overlaps."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import course_audio_bot as audio

PRESET = {"codec": "aac", "profile": "LC", "channels": 1, "sample_rate": 44100, "bitrate": 32000}

@dataclass(frozen=True)
class Candidate:
    url: str
    kind: str
    size: int | None = None

class Pipeline:
    def __init__(self, root, adapter, concurrency=2, reserve_bytes=4*1024**3,
                 task_budget_bytes=128*1024**2):
        if not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be between 1 and 4")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.adapter = adapter
        self.concurrency = concurrency
        self.reserve_bytes = reserve_bytes
        self.task_budget_bytes = task_budget_bytes
        self.manifest = self.root / "pipeline-manifest.jsonl"
        self._resolve_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._reserved = 0
        self._stop = threading.Event()
        self._records = self.records()
        self._source_owners = {r['source_hash']: key for key, r in self._records.items()
                               if r.get('status') == 'ok' and r.get('source_hash')}

    @staticmethod
    def identity(url):
        p = urlsplit(url)
        return hashlib.sha256(urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), '', '')).encode()).hexdigest()

    @staticmethod
    def sha256(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024*1024), b''):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def rank(candidates):
        return sorted(candidates, key=lambda c: (0 if c.kind.lower() in {'audio', 'mp3', 'm4a', 'aac', 'wav', 'hls-audio'} else 1))

    def records(self):
        out = {}
        if self.manifest.exists():
            for line in self.manifest.read_text().splitlines():
                try:
                    record = json.loads(line)
                    key = record['source_identity']
                    if record.get('status') == 'ok' or out.get(key, {}).get('status') != 'ok':
                        out[key] = record
                except (ValueError, KeyError):
                    continue
        return out

    def write(self, record):
        with self._state_lock:
            with self.manifest.open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
            key = record['source_identity']
            if record.get('status') == 'ok' or self._records.get(key, {}).get('status') != 'ok':
                self._records[key] = record

    @staticmethod
    def _lesson(value):
        return {'url': value} if isinstance(value, str) else dict(value)

    def _paths(self, lesson, ident):
        folder = self.root
        if lesson.get('course_title'):
            folder /= audio.slugify(lesson['course_title'])
        stem = lesson.get('stem') or ident[:16]
        # Explicit stems originate from catalogue titles, never media URLs.
        stem = audio.slugify(stem)
        return folder, stem

    def _resume(self, old, lesson):
        if not old or old.get('status') != 'ok' or old.get('params') != PRESET:
            return False
        if old.get('validation_version') != 2 or not old.get('provider_identity_verified'):
            return False
        if old.get('title', '') != lesson.get('title', ''):
            return False
        try:
            return (self.sha256(Path(old['original'])) == old['source_hash'] and
                    self.sha256(Path(old['final'])) == old['final_hash'])
        except (KeyError, OSError):
            return False

    def _reserve(self, budget):
        with self._state_lock:
            if shutil.disk_usage(self.root).free - self._reserved - budget < self.reserve_bytes:
                return False
            self._reserved += budget
            return True

    def one(self, value):
        lesson = self._lesson(value)
        ident = self.identity(lesson['url'])
        with self._state_lock:
            old = self._records.get(ident)
        if self._resume(old, lesson):
            return {**old, 'resume': True}
        if self._stop.is_set():
            return {'status': 'cancelled', 'source_identity': ident}
        budget = self.task_budget_bytes
        if not self._reserve(budget):
            self._stop.set()
            return {'status': 'failed', 'reason': 'disk-reserve', 'source_identity': ident}
        start = time.monotonic()
        timings = {}
        work = None
        source_claimed = None
        try:
            with self._resolve_lock:
                if self._stop.is_set():
                    return {'status': 'cancelled', 'source_identity': ident}
                t = time.monotonic()
                candidates = self.rank(self.adapter.resolve(lesson))
                timings['resolve'] = time.monotonic() - t
            if not candidates:
                raise RuntimeError('no-audio-candidate')
            candidate = candidates[0]
            if candidate.size and candidate.size * 2 > budget:
                extra = candidate.size * 2 - budget
                if not self._reserve(extra):
                    raise RuntimeError('disk-reserve')
                budget += extra
            folder, stem = self._paths(lesson, ident)
            folder.mkdir(parents=True, exist_ok=True)
            work = folder / '.staging' / (stem + '-' + uuid.uuid4().hex)
            work.mkdir(parents=True)
            originals = folder / 'originals'
            originals.mkdir(exist_ok=True)
            suffix = Path(urlsplit(candidate.url).path).suffix.lower()
            if suffix not in audio.AUDIO_EXTS + audio.VIDEO_EXTS:
                suffix = '.m4a' if candidate.kind == 'hls-audio' else '.mp3' if candidate.kind == 'mp3' else '.media'
            t = time.monotonic()
            got = Path(self.adapter.download(candidate, work / ('source' + suffix)))
            timings['download'] = time.monotonic() - t
            source_hash = self.sha256(got)
            with self._state_lock:
                owner = self._source_owners.get(source_hash)
                if owner and owner != ident:
                    raise RuntimeError('duplicate-source-different-lesson')
                self._source_owners[source_hash] = ident
                source_claimed = source_hash
            # Move immutable bytes, not a second multi-gigabyte copy.
            original = originals / (stem + suffix)
            if original.exists():
                if self.sha256(original) == source_hash:
                    got.unlink()
                else:
                    original = originals / (stem + '-' + source_hash[:12] + suffix)
                    os.replace(got, original)
            else:
                os.replace(got, original)
            duration = audio._probe_duration(original)
            if duration is None or duration <= 0:
                raise RuntimeError('invalid-original-audio')
            required = original.stat().st_size + int(duration * 32000 / 8 * 1.25) + 8*1024**2
            if required > budget:
                extra = required - budget
                if not self._reserve(extra):
                    raise RuntimeError('disk-reserve')
                budget += extra
            encoded = work / 'encoded.m4a'
            t = time.monotonic()
            self.adapter.encode(original, encoded)
            timings['encode'] = time.monotonic() - t
            t = time.monotonic()
            valid, _ = audio.verify_audio_output(encoded, source=original)
            if not valid:
                raise RuntimeError('output-validation-failed')
            timings['validate'] = time.monotonic() - t
            final_hash = self.sha256(encoded)
            final = folder / (stem + '.m4a')
            if final.exists() and self.sha256(final) != final_hash:
                backup = folder / 'quarantine' / ('replaced-' + uuid.uuid4().hex + '.m4a')
                backup.parent.mkdir(exist_ok=True)
                # Same-filesystem hard link preserves old bytes without another audio copy.
                # If unsupported, fail safely; do not consume an unreserved full-file copy.
                os.link(final, backup)
            os.replace(encoded, final)
            timings['total'] = time.monotonic() - start
            record = {'status': 'ok', 'source_identity': ident,
                      'title': lesson.get('title', ''), 'original': str(original), 'final': str(final),
                      'source_hash': source_hash, 'final_hash': final_hash,
                      'media_identity': self.identity(candidate.url), 'params': PRESET,
                      'provider_identity_verified': True, 'validation_version': 2,
                      'timings': timings}
            self.write(record)
            shutil.rmtree(work)
            return record
        except Exception as error:
            self._stop.set()
            # Preserve failed sources/temporaries for diagnosis, never erase valid finals.
            if work and work.exists():
                quarantine = self.root / 'quarantine' / ('failed-' + uuid.uuid4().hex)
                quarantine.parent.mkdir(exist_ok=True)
                os.replace(work, quarantine)
            with self._state_lock:
                if source_claimed and self._source_owners.get(source_claimed) == ident:
                    self._source_owners.pop(source_claimed, None)
            reason = str(error) if str(error) in {'disk-reserve', 'duplicate-source-different-lesson', 'no-audio-candidate', 'output-validation-failed'} else type(error).__name__
            record = {'status': 'failed', 'source_identity': ident, 'reason': reason, 'timings': timings}
            self.write(record)
            return record
        finally:
            with self._state_lock:
                self._reserved -= budget

    def run(self, values):
        values = list(values)
        identities = [self.identity(self._lesson(value)['url']) for value in values]
        if len(set(identities)) != len(identities):
            raise ValueError('duplicate input lesson identities')
        # At most concurrency tasks submitted: executor.map eagerly queues a full library.
        iterator = iter(values)
        results = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            pending = set()
            def submit():
                if self._stop.is_set():
                    return False
                try:
                    pending.add(pool.submit(self.one, next(iterator)))
                    return True
                except StopIteration:
                    return False
            for _ in range(self.concurrency):
                submit()
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    results.append(result)
                    print(json.dumps({k: result[k] for k in ('status', 'source_identity', 'title', 'resume', 'reason', 'timings') if k in result}), flush=True)
                    submit()
        return results
