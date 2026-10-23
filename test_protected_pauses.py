"""Public synthetic PCM regressions; no course recordings."""
import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

import protected_pauses as pauses
try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None or importlib.util.find_spec("onnxruntime") is None or importlib.util.find_spec("webrtcvad") is None, "install the silence extra")
class PlanTests(unittest.TestCase):
    def test_exact_conservation_stereo_short_quiet_and_edges(self):
        rate = 44100
        pcm = np.zeros((rate * 5, 2), dtype='<i2')
        pcm[rate*2:rate*2+100] = [104, -104]
        pcm[rate*3:rate*3+100] = [103, -103]
        # A short, low-volume utterance vetoed by either VAD must survive.
        speech = np.zeros(500, dtype=bool)
        speech[300] = True
        plan = pauses.plan(pcm, rate, speech)
        self.assertTrue(plan['removed'])
        for a, b in plan['removed']:
            self.assertGreaterEqual(a, rate)
            self.assertLessEqual(b, 4*rate)
            self.assertLessEqual(np.abs(pcm[a:b].astype('i4')).max(), 103)
            self.assertFalse(a < rate*3+100 and b > rate*3)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'out.wav'
            pauses.render(pcm, rate, plan, out, threading.Event())
            with wave.open(str(out)) as f:
                edited = np.frombuffer(f.readframes(f.getnframes()), '<i2').reshape(-1, 2)
            self.assertEqual(len(edited), plan['output_frames'])
            loud = np.any(np.abs(pcm.astype('i4')) > 103, axis=1)
            loud_out = np.any(np.abs(edited.astype('i4')) > 103, axis=1)
            np.testing.assert_array_equal(pcm[loud], edited[loud_out])
            np.testing.assert_array_equal(edited[:rate], pcm[:rate])
            np.testing.assert_array_equal(edited[-rate:], pcm[-rate:])

    def test_detector_projection_any_overlap_and_hysteresis(self):
        flags = pauses.project([.4, .21, .19], [False]*10, 10)
        self.assertTrue(all(flags[:7]))  # frame 60–70ms overlaps Silero 32–64ms
        self.assertFalse(flags[7])

    def test_short_pause_and_identity(self):
        pcm = np.full((48000*3, 1), 104, dtype='<i2')
        pcm[48000:48000+4800] = 0
        plan = pauses.plan(pcm, 48000, np.zeros(300, bool))
        self.assertEqual(plan['removed'], [])
        self.assertEqual(plan['output_frames'], len(pcm))

    def test_literal_160ms_boundary_and_int16_minimum(self):
        for milliseconds, expected in [(150, []), (160, [[32480,34080]])]:
            pcm = np.full((16000*5, 2), [104, -32768], dtype='<i2')
            pcm[32000:32000+milliseconds*16] = [103, -103]
            receipt = pauses.plan(pcm, 16000, np.zeros(500, bool))
            self.assertEqual(receipt['removed'], expected)

    def test_short_clip_preserved_without_dilation_shape_error(self):
        pcm = np.zeros((320, 1), '<i2')
        receipt = pauses.plan(pcm, 16000, np.ones(2, bool))
        self.assertEqual(receipt['removed'], [])
        self.assertEqual(receipt['output_frames'], 320)

    def test_model_hash_and_cancellation(self):
        with tempfile.TemporaryDirectory() as d:
            model = Path(d)/'bad.onnx'; model.write_bytes(b'bad')
            with self.assertRaisesRegex(RuntimeError, 'silence-model-hash-mismatch'):
                pauses.Detectors(model)
        stop = threading.Event(); stop.set()
        with self.assertRaisesRegex(RuntimeError, 'delivery-cancelled'):
            pauses.check_cancel(stop)




@unittest.skipIf(np is None or importlib.util.find_spec('onnxruntime') is None or importlib.util.find_spec('webrtcvad') is None or not shutil.which('ffmpeg'), 'silence extra and FFmpeg required')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        from pipeline import Candidate
        from browser_adapter import SafariAdapter
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'work'
        self.stop = threading.Event()
        fixture = Path(__file__).parent/'tests/fixtures/public-speech.wav'
        with wave.open(str(fixture)) as f:
            voice = np.frombuffer(f.readframes(f.getnframes()), '<i2')
        # Real public speech, a low/short utterance and long synthetic pauses.
        self.pcm = np.concatenate([np.zeros(16000, '<i2'), voice,
            np.zeros(32000, '<i2'), (voice[:8000]/32).astype('<i2'), np.zeros(32000, '<i2')])[:, None]
        wav = self.base/'source.wav'
        with wave.open(str(wav), 'wb') as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(self.pcm.tobytes())
        self.source = self.base/'source.m4a'
        cover = self.base/'cover.png'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=red:s=32x32',
                        '-frames:v', '1', str(cover)], check=True, capture_output=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(wav), '-i', str(cover),
                        '-map', '0:a:0', '-map', '1:v:0', '-c:a', 'aac', '-b:a', '128k', '-c:v', 'copy',
                        '-disposition:v:0', 'attached_pic', '-metadata', 'title=Public speech',
                        '-metadata', 'artist=Synthetic pause test', '-metadata', 'track=7/9',
                        str(self.source)], check=True, capture_output=True)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        source = self.source
        class Adapter(SafariAdapter):
            def resolve(self, lesson):
                return [Candidate('https://example.test/source.m4a', 'audio')]
            def download(self, candidate, destination):
                shutil.copyfile(source, destination); return destination
        self.adapter = Adapter(encoder='aac')
        self.compactor = pauses.Compactor()
        self.lesson = {'url': 'https://example.test/lesson', 'title': 'Public speech', 'stem': 'lesson'}

    def pipeline(self, **kwargs):
        from pipeline import Pipeline
        return Pipeline(self.root, self.adapter, reserve_bytes=0, silence=self.compactor, **kwargs)

    def test_real_model_compaction_metadata_cover_resume_and_identity(self):
        pipe = self.pipeline(); result = pipe.one(self.lesson)
        self.assertEqual(result['status'], 'ok', result)
        receipt = result['silence']
        self.assertGreater(len(receipt['removed']), 0)
        self.assertLess(receipt['output_frames'], receipt['input_frames'])
        self.assertEqual(receipt['input_frames'], receipt['output_frames']+sum(b-a for a,b in receipt['removed']))
        self.assertEqual(result['artwork']['origin'], 'embedded')
        tags = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-of', 'json', result['final']]))['format']['tags']
        self.assertEqual(tags['title'], 'Public speech'); self.assertEqual(tags['track'], '7/9')
        self.assertEqual(tags['artist'], 'Synthetic pause test')
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        with patch.object(self.adapter, 'resolve', side_effect=AssertionError('double processing')):
            self.assertTrue(self.pipeline().one(self.lesson)['resume'])
            changed = self.pipeline(); changed.silence.identity = {**changed.silence.identity, 'name': 'wrong'}
            self.assertEqual(changed.one(self.lesson)['reason'], 'silence-profile-mismatch')
        from pipeline import Pipeline
        self.assertEqual(Pipeline(self.root, self.adapter, reserve_bytes=0).one(self.lesson)['reason'], 'silence-profile-mismatch')

    def test_inference_failure_preserves_source_and_existing_final(self):
        self.root.mkdir(); final = self.root/'lesson.m4a'; final.write_bytes(b'previous verified output')
        with patch('protected_pauses.Detectors.speech', side_effect=RuntimeError('silence-inference-failed')):
            result = self.pipeline().one(self.lesson)
        self.assertEqual(result['reason'], 'silence-inference-failed')
        self.assertEqual(final.read_bytes(), b'previous verified output')
        self.assertTrue(list((self.root/'originals').iterdir()))
        self.assertEqual(self.source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_cancel_after_encode_never_promotes(self):
        pipe = self.pipeline(); encode = self.adapter.encode
        def cancel(source, destination):
            encode(source, destination); pipe._stop.set()
        with patch.object(self.adapter, 'encode', side_effect=cancel):
            result = pipe.one(self.lesson)
        self.assertEqual(result['reason'], 'delivery-cancelled')
        self.assertFalse((self.root/'lesson.m4a').exists())
        self.assertTrue(list((self.root/'originals').iterdir()))
        self.assertEqual(pipe._reserved, 0)

    def test_shortened_bad_encode_and_codec_failure_do_not_publish(self):
        for mode in ['truncated', 'codec']:
            with self.subTest(mode=mode):
                self.root = self.base/mode
                def encode(source, destination):
                    if mode == 'codec':
                        raise RuntimeError('audio-encode-failed')
                    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-t', '1',
                                    '-ac','1','-ar','44100','-c:a','aac','-b:a','32k',str(destination)], check=True, capture_output=True)
                with patch.object(self.adapter, 'encode', side_effect=encode):
                    result = self.pipeline().one(self.lesson)
                self.assertEqual(result['status'], 'failed', result)
                self.assertFalse((self.root/'lesson.m4a').exists())

    def test_real_detector_state_reset_and_low_voice_protection(self):
        with wave.open(str(Path(__file__).parent/'tests/fixtures/public-speech.wav')) as f:
            voice = np.frombuffer(f.readframes(f.getnframes()), '<i2')
        detector = pauses.Detectors()
        first = detector.speech(voice, len(voice)//160, self.stop)
        self.assertGreater(first.sum(), 0)
        np.testing.assert_array_equal(first, detector.speech(voice, len(voice)//160, self.stop))
        with patch.object(detector.session, 'run', side_effect=ValueError('private model error')):
            with self.assertRaisesRegex(RuntimeError, 'silence-inference-failed'):
                detector.speech(voice, len(voice)//160, self.stop)
        # All samples of a short quiet utterance vetoed by a VAD survive exactly.
        pcm = np.zeros((16000*5, 1), '<i2'); pcm[32000:32800,0] = (voice[:800]/256).astype('<i2')
        speech = np.zeros(500, bool); speech[200:205] = True
        plan = pauses.plan(pcm, 16000, speech)
        for a,b in plan['removed']:
            self.assertFalse(a < 32800 and b > 32000)

    def test_disk_budget_and_invalid_model_fail_closed(self):
        work = self.base/'pcm'; work.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'disk-reserve'):
            self.compactor.prepare(self.source, work, self.stop, lambda _: False)
        self.assertEqual(list(work.iterdir()), [])
        with patch('protected_pauses.MAX_PCM_BYTES', 1):
            with self.assertRaisesRegex(RuntimeError, 'silence-pcm-budget-exceeded'):
                self.compactor.prepare(self.source, work, self.stop, lambda _: True)

    def test_delivery_retains_receipt_and_completed_items_never_reprocess(self):
        from cloud_delivery import Delivery
        from test_cloud_delivery import Cloud
        cloud = Cloud(); target = self.base/'cloud/lesson.m4a'
        worker = Delivery(self.root, target.parent, cloud, self.adapter, reserve_bytes=0,
                          silence=self.compactor)
        result = worker.process(self.lesson, target)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['silence_identity'], self.compactor.identity)
        final_hash = result['final_hash']
        self.assertFalse(Path(result['original']).exists())
        with patch.object(self.adapter, 'resolve', side_effect=AssertionError('reprocessed complete')):
            other = Delivery(self.root, target.parent, cloud, self.adapter, reserve_bytes=0)
            self.assertEqual(other.process(self.lesson, target)['final_hash'], final_hash)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), final_hash)

    def test_pending_profile_mismatch_and_cancellation_keep_local_bytes(self):
        from cloud_delivery import Delivery
        from test_cloud_delivery import Cloud
        target = self.base/'cloud/lesson.m4a'; cloud = Cloud()
        worker = Delivery(self.root, target.parent, cloud, self.adapter, reserve_bytes=0,
                          silence=self.compactor)
        receipt = worker.prepare(self.lesson, target)
        other = Delivery(self.root, target.parent, cloud, self.adapter, reserve_bytes=0)
        with self.assertRaisesRegex(RuntimeError, 'silence-profile-mismatch'):
            other.process(self.lesson, target)
        worker.cancel()
        with self.assertRaisesRegex(RuntimeError, 'delivery-cancelled'):
            worker.process(self.lesson, target)
        self.assertTrue(Path(receipt['original']).is_file())
        self.assertEqual(hashlib.sha256(Path(receipt['final']).read_bytes()).hexdigest(), receipt['final_hash'])
        self.assertFalse(target.exists())

    def test_real_subprocess_cancellation_and_pcm_identity(self):
        import sys
        timer = threading.Timer(.1, self.stop.set); timer.start()
        try:
            with self.assertRaisesRegex(RuntimeError, 'delivery-cancelled'):
                pauses.command([sys.executable, '-c', 'import time; time.sleep(30)'], self.stop)
        finally:
            timer.cancel(); timer.join()
        pcm = np.full((16000*3, 2), [104, -104], '<i2')
        receipt = pauses.plan(pcm, 16000, np.zeros(300, bool))
        self.stop.clear(); dest = self.base/'unchanged.wav'
        pauses.render(pcm, 16000, receipt, dest, self.stop)
        with wave.open(str(dest)) as f:
            np.testing.assert_array_equal(pcm, np.frombuffer(f.readframes(f.getnframes()), '<i2').reshape(-1,2))

    def test_cli_dry_run_advertises_profile_without_creating_outputs(self):
        import contextlib
        import io
        from course_cli import main
        catalog, profile = self.base/'catalog.json', self.base/'profile.json'
        item = {**self.lesson, 'site_path': ['Course', 'Lesson']}
        catalog.write_text(json.dumps(dict(schema='course-catalog/v1', complete=True,
            expected_count=1, collected_count=1, lessons=[item])))
        profile.write_text(json.dumps(dict(schema='course-site-profile/v1', mode='direct', origin='https://example.test')))
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        out = io.StringIO()
        with patch('course_cli.NativeCloud'), patch('course_cli.Delivery', side_effect=AssertionError('writes')), contextlib.redirect_stdout(out):
            self.assertEqual(main(['--catalog',str(catalog),'--profile',str(profile),'--root',str(self.root),
                '--destination',str(self.base/'cloud'),'--icloud-python','python','--protected-pauses','--dry-run']), 0)
        self.assertEqual(json.loads(out.getvalue())['silence_identity'], self.compactor.identity)
        self.assertEqual(before, {p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()})
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
