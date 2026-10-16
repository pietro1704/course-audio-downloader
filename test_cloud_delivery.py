import json
import contextlib
import io
import fcntl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import Candidate, Pipeline
from browser_adapter import SafariAdapter, load_lessons
from cloud_delivery import Delivery, Progress, destinations


class MediaAdapter(SafariAdapter):
    def __init__(self, encoder='aac'):
        super().__init__(encoder=encoder)
        self.downloads = self.encodes = 0

    def resolve(self, lesson):
        return [Candidate('https://media.test/' + lesson['title'] + '.mp3', 'mp3')]

    def download(self, candidate, destination):
        self.downloads += 1
        frequency = 440 + sum(candidate.url.encode()) % 300
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        f'sine=frequency={frequency}:duration=0.3', '-c:a', 'pcm_s16le',
                        '-f', 'wav', str(destination)], check=True, capture_output=True)
        return destination

    def encode(self, source, destination):
        self.encodes += 1
        return super().encode(source, destination)


class Cloud:
    def __init__(self):
        self.ready = True
        self.error = self.conflicts = False
        self.evicted = set()
        self.on_status = None

    def status(self, path):
        if self.on_status:
            self.on_status(path)
        stat = path.stat()
        return dict(ubiquitous=True, uploaded=self.ready, uploading=not self.ready,
                    conflicts=self.conflicts, upload_error=self.error, size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns, materialized=path not in self.evicted)

    def evict(self, path):
        self.evicted.add(path)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'work'
        self.cloud_root = Path(self.temp.name) / 'cloud'
        self.root.mkdir()
        self.cloud_root.mkdir()
        self.catalog = self.root / 'catalog.json'
        self.catalog.write_text(json.dumps({'courses': [{'title': 'Course', 'lessons': [
            {'title': 'A', 'url': 'https://lessons.example.test/aulas/a'},
            {'title': 'B', 'url': 'https://lessons.example.test/aulas/b'}]}]}))
        self.lessons = load_lessons(self.catalog)
        self.cloud = Cloud()
        self.adapter = MediaAdapter()

    def delivery(self, **kwargs):
        return Delivery(self.root, self.cloud_root, self.cloud, self.adapter,
                        reserve_bytes=0, upload_timeout=0, **kwargs)

    def target(self, index=0):
        return destinations(self.catalog, self.cloud_root)[index][1]

    def assert_sources_preserved(self, delivery):
        receipt = next(iter(delivery.journal.values()))
        self.assertTrue(Path(receipt['original']).is_file())
        self.assertTrue(Path(receipt['final']).is_file())

    def test_success_uses_real_media_and_cleans_only_after_native_confirmation(self):
        delivery = self.delivery()
        observed = []
        def checking(path):
            self.assert_sources_preserved(delivery)
            observed.append(path)
        self.cloud.on_status = checking
        receipt = delivery.process(self.lessons[0], self.target())
        self.assertEqual(receipt['state'], 'complete')
        self.assertEqual(len(observed), 1)
        self.assertFalse(Path(receipt['original']).exists())
        self.assertFalse(Path(receipt['final']).exists())
        self.assertEqual(Pipeline.sha256(self.target()), receipt['final_hash'])
        self.assertIn(self.target(), self.cloud.evicted)
        self.assertGreater(receipt['freed_bytes'], 0)

    def test_run_accepts_literal_colons_in_site_output_paths(self):
        lesson = dict(self.lessons[0], title='Aula: 3',
                      site_path=['Course', 'Módulo: Parte 1', 'Aula: 3'])
        delivery = self.delivery()
        target = self.cloud_root / 'Course' / 'Módulo: Parte 1' / 'Aula: 3.m4a'
        called = []
        delivery.process = lambda item, path, index=None: called.append((item, path, index))

        delivery.run([(lesson, target)])

        self.assertEqual(called, [(lesson, target, 0)])

    def test_pending_upload_preserves_sources_and_retry_does_not_reencode(self):
        delivery = self.delivery()
        self.cloud.ready = False
        with self.assertRaisesRegex(RuntimeError, 'icloud-upload-timeout'):
            delivery.process(self.lessons[0], self.target())
        self.assert_sources_preserved(delivery)
        self.cloud.ready = True
        self.delivery().process(self.lessons[0], self.target())
        self.assertEqual((self.adapter.downloads, self.adapter.encodes), (1, 1))

    def test_icloud_can_evict_just_published_file_before_confirmation(self):
        self.cloud.on_status = lambda path: self.cloud.evicted.add(path)
        receipt = self.delivery().process(self.lessons[0], self.target())
        self.assertEqual(receipt['state'], 'complete')
        self.assertEqual(self.adapter.downloads, 1)

    def test_upload_error_preserves_sources(self):
        self.cloud.error = True
        delivery = self.delivery()
        with self.assertRaisesRegex(RuntimeError, 'icloud-upload-error-or-conflict'):
            delivery.process(self.lessons[0], self.target())
        self.assert_sources_preserved(delivery)

    def test_cloud_conflict_preserves_sources_and_destination(self):
        target = self.target()
        target.parent.mkdir()
        target.write_bytes(b'existing different media')
        delivery = self.delivery()
        with self.assertRaisesRegex(RuntimeError, 'cloud-destination-conflict'):
            delivery.process(self.lessons[0], target)
        self.assert_sources_preserved(delivery)
        self.assertEqual(target.read_bytes(), b'existing different media')

    def test_restart_after_cleanup_does_not_require_deleted_source_or_materialize_cloud(self):
        receipt = self.delivery().process(self.lessons[0], self.target())
        with patch.object(Pipeline, 'sha256', side_effect=AssertionError('must not hydrate cloud cache')):
            result = self.delivery().process(self.lessons[0], self.target())
        self.assertEqual(result['final_hash'], receipt['final_hash'])
        self.assertEqual((self.adapter.downloads, self.adapter.encodes), (1, 1))

    def test_icloud_eviction_timestamp_rounding_does_not_trigger_redownload(self):
        receipt = self.delivery().process(self.lessons[0], self.target())
        original_status = self.cloud.status
        def rounded_status(path):
            result = original_status(path)
            result['mtime_ns'] += 100
            return result
        self.cloud.status = rounded_status
        self.delivery().process(self.lessons[0], self.target())
        self.assertEqual(self.adapter.downloads, 1)

    def test_evicted_cloud_with_changed_metadata_is_not_trusted(self):
        self.delivery().process(self.lessons[0], self.target())
        original_status = self.cloud.status
        def changed_status(path):
            result = original_status(path)
            result['mtime_ns'] += 1_000_000_000
            return result
        self.cloud.status = changed_status
        with self.assertRaisesRegex(RuntimeError, 'cloud-receipt-metadata-mismatch'):
            self.delivery().process(self.lessons[0], self.target())

    def test_interrupt_after_uploaded_journal_recovers_without_download(self):
        delivery = self.delivery()
        with patch.object(delivery, 'cleanup', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                delivery.process(self.lessons[0], self.target())
        self.assertEqual(next(iter(delivery.journal.values()))['state'], 'uploaded')
        self.assert_sources_preserved(delivery)
        self.delivery().process(self.lessons[0], self.target())
        self.assertEqual((self.adapter.downloads, self.adapter.encodes), (1, 1))

    def test_wrong_title_in_receipt_prevents_cleanup(self):
        delivery = self.delivery()
        self.cloud.ready = False
        with self.assertRaises(RuntimeError):
            delivery.process(self.lessons[0], self.target())
        receipt = next(iter(delivery.journal.values()))
        receipt['title'] = 'Wrong lesson'
        delivery.save(receipt)
        self.cloud.ready = True
        with self.assertRaisesRegex(RuntimeError, 'delivery-identity-mismatch'):
            self.delivery().process(self.lessons[0], self.target())
        self.assert_sources_preserved(delivery)

    def test_original_changed_during_upload_is_not_deleted(self):
        delivery = self.delivery()
        def change_source(path):
            original = Path(next(iter(delivery.journal.values()))['original'])
            original.write_bytes(b'changed while uploading')
        self.cloud.on_status = change_source
        with self.assertRaisesRegex(RuntimeError, 'cleanup-hash-mismatch'):
            delivery.process(self.lessons[0], self.target())
        self.assert_sources_preserved(delivery)

    def test_receipt_cannot_delete_another_original_in_same_course(self):
        delivery = self.delivery()
        self.cloud.ready = False
        with self.assertRaises(RuntimeError):
            delivery.process(self.lessons[0], self.target())
        receipt = next(iter(delivery.journal.values()))
        protected = Path(receipt['original']).with_name('another-lesson.mp3')
        protected.write_bytes(b'another lesson original')
        receipt.update(original=str(protected), source_hash=Pipeline.sha256(protected))
        delivery.save(receipt)
        self.cloud.ready = True
        with self.assertRaisesRegex(RuntimeError, 'delivery-identity-mismatch'):
            self.delivery().process(self.lessons[0], self.target())
        self.assertTrue(protected.exists())

    def test_materialized_cloud_corruption_prevents_cleanup(self):
        delivery = self.delivery()
        def change_cloud(path):
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
        self.cloud.on_status = change_cloud
        with self.assertRaisesRegex(RuntimeError, 'cloud-final-hash-mismatch'):
            delivery.process(self.lessons[0], self.target())
        self.assert_sources_preserved(delivery)

    def test_second_lesson_is_not_downloaded_while_first_awaits_upload(self):
        delivery = self.delivery()
        self.cloud.ready = False
        for lesson, target in destinations(self.catalog, self.cloud_root):
            with self.assertRaisesRegex(RuntimeError, 'icloud-upload-timeout'):
                delivery.process(lesson, target)
            break
        self.assertEqual((self.adapter.downloads, self.adapter.encodes), (1, 1))

    def test_symlink_original_is_never_deleted(self):
        delivery = self.delivery()
        self.cloud.ready = False
        with self.assertRaises(RuntimeError):
            delivery.process(self.lessons[0], self.target())
        receipt = next(iter(delivery.journal.values()))
        original = Path(receipt['original'])
        victim = self.root / 'protected'
        victim.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(victim)
        self.cloud.ready = True
        with self.assertRaisesRegex(RuntimeError, 'delivery-identity-mismatch'):
            self.delivery().process(self.lessons[0], self.target())
        self.assertTrue(victim.exists())

    def test_cli_refuses_second_process_before_touching_media(self):
        docs = self.root / 'Documentacao'
        docs.mkdir()
        profile = self.root / 'profile.json'
        profile.write_text(json.dumps({'schema':'course-site-profile/v1','mode':'direct','origin':'https://example.test'}))
        with (docs / 'delivery.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('cloud_delivery.py')),
                                     '--profile', str(profile), '--catalog', str(self.catalog), '--output-root', str(self.root),
                                     '--destination', str(self.cloud_root), '--icloud-python', sys.executable],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['reason'], 'another-run-is-active')
        self.assertEqual(list(self.cloud_root.iterdir()), [])

    def test_encode_failure_preserves_original_and_does_not_publish(self):
        with patch.object(self.adapter, 'encode', side_effect=RuntimeError('signed SECRET media URL')):
            with self.assertRaisesRegex(RuntimeError, 'lesson-pipeline-failed'):
                self.delivery().process(self.lessons[0], self.target())
        self.assertEqual(len(list(self.root.glob('*/originals/*'))), 1)
        self.assertEqual(list(self.cloud_root.iterdir()), [])
        self.assertNotIn('SECRET', (self.root / 'pipeline-manifest.jsonl').read_text())

    def legacy_output(self, audit=True):
        record = Pipeline(self.root, self.adapter, reserve_bytes=0).one(self.lessons[0])
        self.assertEqual(record['status'], 'ok')
        (self.root / 'pipeline-manifest.jsonl').unlink()
        old = dict(record, url=self.lessons[0]['url'], validated=True, identity_audited=True,
                   sha256=record['source_hash'])
        if audit:
            old['audit'] = dict(url=self.lessons[0]['url'], title=self.lessons[0]['title'],
                                source_sha256=record['source_hash'])
        (self.root / 'worker-manifest.jsonl').write_text(json.dumps(old) + '\n')
        return old

    def test_audited_legacy_output_is_delivered_without_download_or_encode(self):
        self.legacy_output()
        receipt = self.delivery().process(self.lessons[0], self.target())
        self.assertEqual(receipt['state'], 'complete')
        self.assertEqual((self.adapter.downloads, self.adapter.encodes), (1, 1))

    def test_legacy_flags_without_audit_require_current_source_comparison(self):
        self.legacy_output(audit=False)
        receipt = self.delivery().process(self.lessons[0], self.target())
        self.assertEqual(receipt['state'], 'complete')
        self.assertEqual((self.adapter.downloads, self.adapter.encodes), (2, 1))

    def test_changed_current_source_does_not_delete_or_publish_legacy_media(self):
        old = self.legacy_output(audit=False)
        download = self.adapter.download
        def changed_source(candidate, destination):
            return download(Candidate('https://media.test/different.mp3', 'mp3'), destination)
        with patch.object(self.adapter, 'download', side_effect=changed_source):
            with self.assertRaisesRegex(RuntimeError, 'legacy-current-source-mismatch'):
                self.delivery().process(self.lessons[0], self.target())
        self.assertTrue(Path(old['original']).exists())
        self.assertTrue(Path(old['final']).exists())
        self.assertEqual(list(self.cloud_root.iterdir()), [])

    def test_wrong_legacy_audit_identity_preserves_source(self):
        old = self.legacy_output()
        old['audit']['url'] = self.lessons[1]['url']
        (self.root / 'worker-manifest.jsonl').write_text(json.dumps(old) + '\n')
        with self.assertRaisesRegex(RuntimeError, 'legacy-identity-mismatch'):
            self.delivery().process(self.lessons[0], self.target())
        self.assertTrue(Path(old['original']).exists())
        self.assertTrue(Path(old['final']).exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'AudioToolbox requires macOS')
    def test_audiotoolbox_real_delivery_has_same_aac32_format(self):
        self.adapter = MediaAdapter(encoder='aac_at')
        receipt = self.delivery().process(self.lessons[0], self.target())
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                                   'stream=codec_name,profile,channels,sample_rate',
                                                   '-of', 'json', str(self.target())], text=True))
        self.assertEqual(probe['streams'][0], dict(codec_name='aac', profile='LC', channels=1, sample_rate='44100'))
        self.assertEqual(receipt['state'], 'complete')
        self.assertFalse(Path(receipt['original']).exists())

    def test_unknown_encoder_is_rejected_before_touching_media(self):
        with self.assertRaisesRegex(ValueError, 'unsupported audio encoder'):
            SafariAdapter(encoder='unexpected')

    def parallel_catalog(self, count=6):
        self.catalog.write_text(json.dumps({'courses': [{'title': 'Course', 'lessons': [
            {'title': f'Lesson {i}', 'url': f'https://lessons.example.test/aulas/lesson-{i}'}
            for i in range(count)]}]}))
        return destinations(self.catalog, self.cloud_root)

    def test_parallel_cycles_overlap_uploads_keep_resolver_serial_and_persist_every_receipt(self):
        work = self.parallel_catalog(10)
        barrier = threading.Barrier(8)
        resolving = 0
        maximum_resolving = 0
        lock = threading.Lock()
        resolver = self.adapter.resolve
        def slow_resolve(lesson):
            nonlocal resolving, maximum_resolving
            with lock:
                resolving += 1
                maximum_resolving = max(maximum_resolving, resolving)
            try:
                time.sleep(.02)
                return resolver(lesson)
            finally:
                with lock:
                    resolving -= 1
        upload_count = 0
        def overlap_uploads(path):
            nonlocal upload_count
            with lock:
                upload_count += 1
                number = upload_count
            if number <= 8:
                barrier.wait(timeout=20)
        self.cloud.on_status = overlap_uploads
        with patch.object(self.adapter, 'resolve', side_effect=slow_resolve):
            results = self.delivery(concurrency=8).run(work)
        self.assertEqual(len(results), 10)
        self.assertEqual(maximum_resolving, 1)
        stored = json.loads((self.root / 'Documentacao/delivery-journal.json').read_text())
        self.assertEqual(len(stored), 10)
        self.assertTrue(all(r['state'] == 'complete' for r in stored.values()))
        for lesson, target in work:
            receipt = stored[Pipeline.identity(lesson['url'])]
            self.assertEqual(receipt['title'], lesson['title'])
            self.assertAlmostEqual(receipt['audio_seconds'], .3, places=2)
            self.assertEqual(Pipeline.sha256(target), receipt['final_hash'])
            self.assertFalse(Path(receipt['original']).exists())

    def test_parallel_failure_stops_submissions_and_preserves_pending_sources(self):
        work = self.parallel_catalog(8)
        rendezvous = threading.Barrier(2)
        def failed_upload(path):
            rendezvous.wait(timeout=20)
            raise RuntimeError('icloud-upload-error-or-conflict')
        self.cloud.on_status = failed_upload
        delivery = self.delivery(concurrency=2)
        with self.assertRaisesRegex(RuntimeError, 'icloud-upload-error-or-conflict'):
            delivery.run(work)
        self.assertEqual(self.adapter.downloads, 2)
        self.assertEqual(len(delivery.journal), 2)
        for receipt in delivery.journal.values():
            self.assertTrue(Path(receipt['original']).exists())
            self.assertTrue(Path(receipt['final']).exists())
            self.assertNotEqual(receipt['state'], 'complete')

    def test_duplicate_parallel_lesson_identity_is_rejected_before_download(self):
        pair = destinations(self.catalog, self.cloud_root)[0]
        with self.assertRaisesRegex(ValueError, 'duplicate input lesson identities'):
            self.delivery(concurrency=4).run([pair, pair])
        self.assertEqual(self.adapter.downloads, 0)

    def test_parallel_lessons_cannot_share_an_output_path(self):
        work = destinations(self.catalog, self.cloud_root)
        work[1] = (work[1][0], work[0][1])
        with self.assertRaisesRegex(ValueError, 'duplicate output paths'):
            self.delivery(concurrency=4).run(work)
        self.assertEqual(self.adapter.downloads, 0)

    def test_progress_separates_overlapping_lessons_and_never_decreases_completed_count(self):
        (self.root / 'Documentacao').mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            progress = Progress(self.root, concurrency=4)
            progress.event('start-lesson', source_identity='a', title='A', index=0)
            progress.event('start-lesson', source_identity='b', title='B', index=1)
            progress.event('encode', source_identity='a', title='A', index=0)
            state = json.loads(progress.path.read_text())
            self.assertEqual(state['active_count'], 2)
            self.assertEqual({r['source_identity']: r['stage'] for r in state['active']},
                             {'a': 'encode', 'b': 'start-lesson'})
            progress.event('complete', source_identity='a', delivered=2)
            progress.event('complete', source_identity='b', delivered=1)
            progress.event(heartbeat=True)
            state = json.loads(progress.path.read_text())
        self.assertEqual((state['delivered'], state['active_count'], state['max_observed_active']), (2, 0, 2))

    def test_parallel_cancel_preserves_sources_and_does_not_start_more_lessons(self):
        work = self.parallel_catalog(8)
        rendezvous = threading.Barrier(3)
        release = threading.Event()
        self.cloud.ready = False
        def wait_upload(path):
            rendezvous.wait(timeout=20)
            release.wait(timeout=20)
        self.cloud.on_status = wait_upload
        delivery = self.delivery(concurrency=2)
        errors = []
        def run():
            try:
                delivery.run(work)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            rendezvous.wait(timeout=20)
            delivery.cancel()
        finally:
            release.set()
            thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(str(errors[0]), 'delivery-cancelled')
        self.assertEqual(self.adapter.downloads, 2)
        for receipt in delivery.journal.values():
            self.assertTrue(Path(receipt['original']).exists())
            self.assertTrue(Path(receipt['final']).exists())


if __name__ == '__main__':
    unittest.main()
