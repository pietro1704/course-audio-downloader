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
from browser_adapter import SafariAdapter

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
    def test_site_names_preserved_in_real_output_and_identity(self):
        pipe = self.pipe()
        first = {'url': 'https://test/course/1', 'title': '2. Aula 08 – Prosa – Parte II',
                 'site_path': ['Example Formation', 'Semana 36', '2. Aula 08 – Prosa – Parte II']}
        result = pipe.one(first)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(Path(result['final']).relative_to(self.root).parts,
                         (*first['site_path'][:-1], first['site_path'][-1] + '.m4a'))
        second = dict(first, url='https://test/course/2',
                      site_path=['Example Formation', 'Semana 37', first['title']])
        other = pipe.one(second)
        self.assertEqual(other['status'], 'ok', other)
        self.assertNotEqual(result['source_identity'], other['source_identity'])
        self.assertNotEqual(result['final'], other['final'])
    def test_literal_colon_is_preserved_in_real_output_path(self):
        lesson = {'url': 'https://test/course/colon', 'title': 'Aula: 3',
                  'site_path': ['Example Formation', 'Módulo: Parte 1', 'Aula: 3']}
        result = self.pipe().one(lesson)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(Path(result['final']).relative_to(self.root).parts,
                         ('Example Formation', 'Módulo: Parte 1', 'Aula: 3.m4a'))
    def test_unsafe_site_names_fail_without_download_or_escape(self):
        for parts in [['..', 'aula'], ['Semana 01', '../aula'],
                      ['Semana 01', 'a\\b'], ['Semana 01', ''], 'Semana 01']:
            with self.subTest(parts=parts):
                adapter = Adapter()
                result = self.pipe(adapter).one({'url': 'https://test/invalid', 'site_path': parts})
                self.assertEqual(result['status'], 'failed', result)
                self.assertEqual(adapter.encoded, 0)
                self.assertFalse(list(self.root.rglob('*.m4a')))
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
        from browser_adapter import normalize_title
        self.assertEqual(normalize_title('Aula X | Example Academy', r'\s*[|]\s*Example Academy$'),normalize_title('Aula X'))
        self.assertNotEqual(normalize_title('Aula anterior'),normalize_title('Aula X'))

    def test_safari_waits_for_expected_title_and_signed_query_stability(self):
        adapter=SafariAdapter(timeout=2)
        lesson={'url':'https://lessons.example.test/aulas/new','title':'Nova aula'}
        pages=[{'url':lesson['url'],'title':title,'ready':'complete','auth':True,
                'links':[{'url':'https://cdn.test/new.mp3?sig='+str(i),'label':'Áudio da aula (mp3)'}]}
               for i,title in enumerate(['Aula anterior','Nova aula','Nova aula','Nova aula'])]
        def evaluate(navigate=None):
            return {} if navigate else pages.pop(0)
        adapter._eval=evaluate
        with patch('browser_adapter.time.sleep'):
            candidates=adapter.resolve(lesson)
        self.assertEqual(candidates[0].url,'https://cdn.test/new.mp3?sig=3')
        self.assertEqual(len(pages),0)

    def test_safari_resolves_stable_named_mp3_when_document_is_interactive(self):
        import itertools
        lesson = {'url': 'https://lessons.example.test/aulas/ready', 'title': 'Ready lesson'}
        page = {'url': lesson['url'], 'canonical': lesson['url'], 'title': lesson['title'],
                'auth': True, 'ready': 'interactive',
                'links': [{'url': 'https://cdn.test/ready.mp3', 'label': 'Áudio da aula (mp3)'}]}
        adapter = SafariAdapter(timeout=6)
        polls = []
        def evaluate(navigate=None):
            if navigate:
                return {}
            polls.append(1)
            return dict(page)
        adapter._eval = evaluate
        with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
            candidates = adapter.resolve(lesson)
        self.assertEqual(candidates[0].url, 'https://cdn.test/ready.mp3')
        self.assertEqual(len(polls), 3)

    def test_interactive_mp3_keeps_url_title_auth_and_canonical_guards(self):
        import itertools
        lesson = {'url': 'https://lessons.example.test/aulas/ready', 'title': 'Ready lesson'}
        base = {'url': lesson['url'], 'canonical': lesson['url'], 'title': lesson['title'],
                'auth': True, 'ready': 'interactive',
                'links': [{'url': 'https://cdn.test/ready.mp3', 'label': 'Áudio da aula (mp3)'}]}
        for changes in [{'auth': False}, {'title': 'Other lesson'},
                        {'url': 'https://lessons.example.test/aulas/other'},
                        {'canonical': 'https://lessons.example.test/aulas/other'}, {'ready': 'loading'}]:
            with self.subTest(changes=changes):
                page = dict(base, **changes)
                adapter = SafariAdapter(timeout=6)
                adapter._eval = lambda navigate=None: {} if navigate else dict(page)
                with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
                    with self.assertRaisesRegex(RuntimeError, 'Safari-lesson-identity-timeout'):
                        adapter.resolve(lesson)

    def test_interactive_generic_audio_still_waits_for_complete_document(self):
        import itertools
        lesson = {'url': 'https://lessons.example.test/aulas/ready', 'title': 'Ready lesson'}
        page = {'url': lesson['url'], 'title': lesson['title'], 'auth': True,
                'links': [{'url': 'https://cdn.test/ready.mp3', 'label': 'Audio'}]}
        calls = []
        def evaluate(navigate=None):
            if navigate:
                return {}
            calls.append(1)
            return dict(page, ready='interactive' if len(calls) <= 3 else 'complete')
        adapter = SafariAdapter(timeout=12)
        adapter._eval = evaluate
        with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
            result = adapter.resolve(lesson)
        self.assertEqual(result[0].kind, 'audio')
        self.assertEqual(len(calls), 6)

    def test_interactive_mp3_needs_three_authenticated_matching_observations(self):
        import itertools
        lesson = {'url': 'https://lessons.example.test/aulas/ready', 'title': 'Ready lesson'}
        page = {'url': lesson['url'], 'title': lesson['title'], 'ready': 'interactive',
                'links': [{'url': 'https://cdn.test/ready.mp3', 'label': 'Áudio da aula (mp3)'}]}
        calls = []
        def evaluate(navigate=None):
            if navigate:
                return {}
            calls.append(1)
            return dict(page, auth=len(calls) > 1)
        adapter = SafariAdapter(timeout=12)
        adapter._eval = evaluate
        with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
            result = adapter.resolve(lesson)
        self.assertEqual(result[0].kind, 'mp3')
        self.assertEqual(len(calls), 4)
    def test_safari_accepts_only_catalogued_number_prefix(self):
        import itertools
        lesson = {'url': 'https://lessons.example.test/aulas/navegacoes',
                  'title': 'Como organizar uma seção de exemplo?', 'number': 1}
        page = {'url': lesson['url'], 'title': '1. ' + lesson['title'],
                'auth': True, 'ready': 'complete',
                'links': [{'url': 'https://cdn.test/a.mp3', 'label': 'Áudio da aula (mp3)'}]}
        adapter = SafariAdapter(timeout=4)
        adapter._eval = lambda navigate=None: {} if navigate else dict(page)
        with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
            candidates = adapter.resolve(lesson)
        self.assertEqual(candidates[0].url, 'https://cdn.test/a.mp3')

    def test_safari_number_prefix_keeps_identity_checks(self):
        import itertools
        base = {'url': 'https://lessons.example.test/aulas/navegacoes',
                'title': 'Como organizar uma seção de exemplo?', 'number': 1}
        for title, url, number in [
                ('2. ' + base['title'], base['url'], 1),
                ('1. Aula diferente', base['url'], 1),
                ('1. ' + base['title'], 'https://lessons.example.test/aulas/other', 1),
                ('1. ' + base['title'], base['url'], None)]:
            with self.subTest(title=title, url=url, number=number):
                lesson = dict(base, number=number)
                page = {'url': url, 'title': title, 'auth': True, 'ready': 'complete',
                        'links': [{'url': 'https://cdn.test/a.mp3', 'label': 'Áudio da aula (mp3)'}]}
                adapter = SafariAdapter(timeout=4)
                adapter._eval = lambda navigate=None: {} if navigate else dict(page)
                with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
                    with self.assertRaisesRegex(RuntimeError, 'Safari-lesson-identity-timeout'):
                        adapter.resolve(lesson)

    def test_explicit_provider_title_keeps_catalogue_name_and_stem(self):
        import itertools
        from browser_adapter import load_lessons
        catalogue = self.root / 'catalogue.json'
        item = {'url': 'https://lessons.example.test/aulas/example',
                'title': 'Título curto', 'provider_title': 'Título editorial longo'}
        catalogue.write_text(json.dumps({'courses': [{'title': 'Course', 'lessons': [item]}]}))
        lesson = load_lessons(catalogue)[0]
        page = {'url': lesson['url'], 'title': lesson['provider_title'],
                'auth': True, 'ready': 'complete',
                'links': [{'url': 'https://cdn.test/a.mp3', 'label': 'Áudio da aula (mp3)'}]}
        adapter = SafariAdapter(timeout=4)
        adapter._eval = lambda navigate=None: {} if navigate else dict(page)
        with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
            self.assertEqual(adapter.resolve(lesson)[0].url, 'https://cdn.test/a.mp3')
        self.assertEqual(lesson['title'], 'Título curto')
        self.assertEqual(lesson['stem'], '01-001-título-curto')

    def test_provider_title_requires_exact_alias_url_and_authentication(self):
        import itertools
        item = {'url': 'https://lessons.example.test/aulas/example',
                'title': 'Título curto', 'provider_title': 'Título editorial longo'}
        for alias, title, url, auth in [
                (None, item['provider_title'], item['url'], True),
                (item['provider_title'], 'Outra aula', item['url'], True),
                (item['provider_title'], item['provider_title'], 'https://lessons.example.test/aulas/other', True),
                (item['provider_title'], item['provider_title'], item['url'], False)]:
            with self.subTest(alias=alias, title=title, url=url, auth=auth):
                lesson = dict(item, provider_title=alias)
                page = {'url': url, 'title': title, 'auth': auth, 'ready': 'complete',
                        'links': [{'url': 'https://cdn.test/a.mp3', 'label': 'Áudio da aula (mp3)'}]}
                adapter = SafariAdapter(timeout=4)
                adapter._eval = lambda navigate=None: {} if navigate else dict(page)
                with patch('browser_adapter.time.sleep'), patch('browser_adapter.time.monotonic', side_effect=itertools.count()):
                    with self.assertRaisesRegex(RuntimeError, 'Safari-lesson-identity-timeout'):
                        adapter.resolve(lesson)

    def test_invalid_provider_title_is_rejected_before_safari(self):
        for alias in ['', [], 42]:
            with self.subTest(alias=alias):
                adapter = SafariAdapter()
                with patch.object(adapter, '_eval', side_effect=AssertionError('touched Safari')):
                    with self.assertRaisesRegex(ValueError, 'provider_title'):
                        adapter.resolve({'url': 'https://lessons.example.test/aulas/example',
                                         'title': 'Título curto', 'provider_title': alias})

    def test_missing_safari_session_fails_without_download(self):
        adapter=SafariAdapter(timeout=0)
        adapter._eval=lambda navigate=None: (_ for _ in ()).throw(RuntimeError('Safari-unavailable'))
        with patch('browser_adapter.time.sleep'), self.assertRaisesRegex(RuntimeError,'navigation'):
            adapter.resolve({'url':'https://lessons.example.test/aulas/new','title':'Nova aula'})

if __name__=='__main__':unittest.main()
