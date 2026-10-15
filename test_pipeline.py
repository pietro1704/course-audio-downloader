import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from pipeline import Pipeline, Candidate
from ppr_cli import SafariAdapter

class Adapter:
    def __init__(self, fail=False, wrong=False):
        self.resolved = self.encoded = self.active = self.max_active = 0
        self.fail, self.wrong = fail, wrong
        self.lock = threading.Lock()
    def resolve(self, lesson):
        with self.lock:
            self.resolved += 1
        return [Candidate(lesson['url'] + '.mp3?sig=SECRET', 'audio')]
    def download(self, candidate, destination):
        freq = 440 + sum(candidate.url.encode()) % 300
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'sine=frequency={freq}:duration=0.5','-c:a','pcm_s16le','-f','wav',str(destination)], check=True, capture_output=True)
        return destination
    def encode(self, source, destination):
        self.encoded += 1
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.fail:
                raise RuntimeError('signed https://example.test/a?sig=SECRET')
            time.sleep(.05)
            codec = 'libmp3lame' if self.wrong else 'aac'
            args = ['ffmpeg','-v','error','-y','-i',str(source),'-ac','1','-ar','44100','-c:a',codec,'-b:a','32k']
            if self.wrong:
                args += ['-f','mp3']
            subprocess.run(args + [str(destination)], check=True, capture_output=True)
        finally:
            with self.lock:
                self.active -= 1

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
    def pipe(self, adapter=None, **kwargs):
        return Pipeline(self.root, adapter or Adapter(), reserve_bytes=0, **kwargs)
    def test_real_encode_validation_resume_no_resolver_or_decode(self):
        adapter = Adapter(); pipe = self.pipe(adapter)
        result = pipe.one('https://test/a')
        self.assertEqual(result['status'], 'ok', result)
        with patch('course_audio_bot.verify_audio_output', side_effect=AssertionError('duplicate decode')):
            self.assertTrue(pipe.one('https://test/a')['resume'])
        self.assertEqual(adapter.resolved, 1)
        self.assertNotIn('SECRET', pipe.manifest.read_text())
        self.assertNotIn('?sig=', pipe.manifest.read_text())
    def test_full_validation_exactly_once_before_promotion(self):
        import course_audio_bot as audio
        original = audio.verify_audio_output
        with patch('course_audio_bot.verify_audio_output', wraps=original) as verify:
            result = self.pipe().one('https://test/a')
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual(verify.call_count, 1)
    def test_wrong_codec_not_promoted(self):
        result = self.pipe(Adapter(wrong=True)).one('https://test/a')
        self.assertEqual(result['status'], 'failed', result)
        self.assertFalse(list(self.root.glob('*.m4a')))
    def test_failed_encode_preserves_existing_final_and_originals(self):
        first = self.pipe().one('https://test/a')
        final = Path(first['final']); previous = final.read_bytes()
        final.write_bytes(previous + b'change')
        existing = final.read_bytes()
        result = self.pipe(Adapter(fail=True)).one('https://test/a')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(final.read_bytes(), existing)
        self.assertTrue(Path(first['original']).exists())
        self.assertNotIn('SECRET', (self.root/'pipeline-manifest.jsonl').read_text())
    def test_source_hash_change_invalidates_resume(self):
        result = self.pipe().one('https://test/a')
        original = Path(result['original']); original.write_bytes(original.read_bytes()+b'changed')
        adapter = Adapter()
        result2 = self.pipe(adapter).one('https://test/a')
        self.assertEqual(result2['status'], 'ok', result2)
        self.assertEqual(adapter.resolved, 1)
    def test_disk_reserve_precedes_resolver(self):
        adapter = Adapter(); pipe = Pipeline(self.root,adapter,reserve_bytes=10**30)
        self.assertEqual(pipe.one('https://test/a')['reason'], 'disk-reserve')
        self.assertEqual(adapter.resolved, 0)
    def test_concurrency_bounded_and_resolver_serial(self):
        adapter = Adapter(); pipe = self.pipe(adapter, concurrency=2)
        active, peak = [0], [0]
        original = adapter.resolve
        def resolve(lesson):
            active[0] += 1; peak[0] = max(peak[0],active[0]); time.sleep(.03)
            try: return original(lesson)
            finally: active[0] -= 1
        adapter.resolve = resolve
        with contextlib.redirect_stdout(io.StringIO()):
            results = pipe.run(['https://test/a','https://test/b','https://test/c'])
        self.assertTrue(all(r['status']=='ok' for r in results),results)
        self.assertEqual(peak[0], 1)
        self.assertLessEqual(adapter.max_active, 2)
    def test_ranking_audio_before_video(self):
        self.assertEqual(Pipeline.rank([Candidate('video','video'),Candidate('audio','audio')])[0].url,'audio')
    def test_signed_query_rotation_has_same_identity(self):
        self.assertEqual(Pipeline.identity('https://test/a?sig=1'),Pipeline.identity('https://test/a?sig=2'))
    def test_duplicate_source_across_lessons_stops(self):
        adapter = Adapter()
        raw = self.root/'source.wav'
        adapter.download(Candidate('https://test/source','audio'),raw)
        def download(candidate,dest): shutil.copy2(raw,dest); return dest
        adapter.download = download
        pipe = self.pipe(adapter)
        self.assertEqual(pipe.one('https://test/a')['status'],'ok')
        result = pipe.one('https://test/b')
        self.assertEqual(result['reason'],'duplicate-source-different-lesson')
    def test_provider_filters_unrelated_links_and_prefers_audio_control(self):
        page={'links':[{'url':'https://site/course','label':'Course'},{'url':'https://site/huge.mp4','label':'Video'},{'url':'https://site/lesson.mp3?sig=SECRET','label':'Áudio da aula (mp3)'}]}
        candidates = SafariAdapter.candidates(page)
        self.assertEqual(len(candidates),1)
        self.assertIn('lesson.mp3',candidates[0].url)
    def test_legacy_metadata_does_not_enable_resume(self):
        result=self.pipe().one('https://test/a')
        result.pop('provider_identity_verified')
        self.assertFalse(self.pipe()._resume(result, {'url':'https://test/a'}))

    def test_duplicate_run_input_is_rejected_before_download(self):
        adapter = Adapter()
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.pipe(adapter).run(['https://test/a', 'https://test/a?sig=new'])
        self.assertEqual(adapter.resolved, 0)
    def test_failure_record_retains_good_unchanged_resume(self):
        pipe = self.pipe()
        result = pipe.one('https://test/a')
        pipe.write({'status':'failed','source_identity':result['source_identity'],'reason':'transient'})
        adapter = Adapter()
        resumed = self.pipe(adapter).one('https://test/a')
        self.assertTrue(resumed['resume'])
        self.assertEqual(adapter.resolved, 0)
    def test_replacement_quarantine_uses_hardlink_without_copy(self):
        pipe = self.pipe()
        result = pipe.one('https://test/a')
        final = Path(result['final'])
        final.write_bytes(final.read_bytes()+b'old-version')
        old_bytes = final.read_bytes()
        with patch('pipeline.shutil.copy2', side_effect=AssertionError('unbudgeted copy')):
            replacement = self.pipe().one('https://test/a')
        self.assertEqual(replacement['status'],'ok',replacement)
        backups = list((self.root/'quarantine').glob('replaced-*'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),old_bytes)
    def test_title_suffix_is_normalized_but_other_lesson_not_accepted(self):
        from ppr_cli import normalize_title
        self.assertEqual(normalize_title('Aula X | Padre Paulo Ricardo'),normalize_title('Aula X'))
        self.assertNotEqual(normalize_title('Aula anterior'),normalize_title('Aula X'))

    def test_safari_waits_for_expected_title_and_signed_query_stability(self):
        adapter=SafariAdapter(timeout=2)
        lesson={'url':'https://padrepauloricardo.org/aulas/new','title':'Nova aula'}
        pages=[{'url':lesson['url'],'title':title,'ready':'complete','auth':True,
                'links':[{'url':'https://cdn.test/new.mp3?sig='+str(i),'label':'Áudio da aula (mp3)'}]}
               for i,title in enumerate(['Aula anterior','Nova aula','Nova aula','Nova aula'])]
        def evaluate(navigate=None):
            return {} if navigate else pages.pop(0)
        adapter._eval=evaluate
        with patch('ppr_cli.time.sleep'):
            candidates=adapter.resolve(lesson)
        self.assertEqual(candidates[0].url,'https://cdn.test/new.mp3?sig=3')
        self.assertEqual(len(pages),0)
    def test_missing_safari_session_fails_without_download(self):
        adapter=SafariAdapter(timeout=0)
        adapter._eval=lambda navigate=None: (_ for _ in ()).throw(RuntimeError('Safari-unavailable'))
        with patch('ppr_cli.time.sleep'), self.assertRaisesRegex(RuntimeError,'navigation'):
            adapter.resolve({'url':'https://padrepauloricardo.org/aulas/new','title':'Nova aula'})

if __name__=='__main__':unittest.main()
