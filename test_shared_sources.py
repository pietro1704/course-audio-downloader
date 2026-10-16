"""Real conversion coverage for explicitly shared media and default identity guards."""
import json
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from course_catalog import load_lessons
from pipeline import Candidate, Pipeline
from test_pipeline import Adapter


class SharedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root/'source.wav'
        Adapter().download(Candidate('https://assets.example.test/source', 'audio'), self.raw)
        self.adapter = Adapter()
        self.adapter.download = lambda candidate, destination: Path(shutil.copy2(self.raw, destination))
        self.first = dict(url='https://portal.example.test/lesson/A', title='Shared lesson',
                          site_path=['Formation', 'Module A', 'Shared lesson'])
        self.second = dict(url='https://portal.example.test/lesson/B', title='Shared lesson',
                           site_path=['Formation', 'Module B', 'Shared lesson'])
        self.first['shared_source_with'] = [self.second['url']]
        self.second['shared_source_with'] = [self.first['url']]

    def pipeline(self):
        return Pipeline(self.root/'work', self.adapter, reserve_bytes=0)

    def test_declared_shared_bytes_keep_distinct_verified_artifacts(self):
        pipeline = self.pipeline()
        first = pipeline.one(self.first)
        second = pipeline.one(self.second)
        self.assertEqual(first['status'], 'ok', first)
        self.assertEqual(second['status'], 'ok', second)
        self.assertEqual(first['source_hash'], second['source_hash'])
        self.assertNotEqual(first['source_identity'], second['source_identity'])
        self.assertNotEqual(first['final'], second['final'])
        self.assertTrue(first['provider_identity_verified'])
        self.assertTrue(second['provider_identity_verified'])
        self.assertEqual(self.adapter.resolved, 2)
        self.assertEqual(self.adapter.encoded, 2)
        self.assertEqual(second['shared_source_identities'], [Pipeline.identity(self.first['url'])])

    def test_sharing_survives_restart_and_reverse_input_order(self):
        second = self.pipeline().one(self.second)
        first = self.pipeline().one(self.first)
        self.assertEqual((first['status'], second['status']), ('ok', 'ok'))
        pipeline = self.pipeline()
        self.assertEqual(pipeline._source_owners[first['source_hash']],
                         {first['source_identity'], second['source_identity']})
        before = self.adapter.resolved
        self.assertTrue(pipeline.one(self.first)['resume'])
        self.assertEqual(self.adapter.resolved, before)

    def test_concurrent_peers_register_both_owners(self):
        pipeline = Pipeline(self.root/'work', self.adapter, reserve_bytes=0, concurrency=2)
        with contextlib.redirect_stdout(io.StringIO()):
            results = pipeline.run([self.first, self.second])
        self.assertEqual([r['status'] for r in results], ['ok', 'ok'])
        self.assertEqual(pipeline._source_owners[results[0]['source_hash']],
                         {r['source_identity'] for r in results})

    def test_all_existing_owners_must_be_explicitly_permitted(self):
        self.pipeline().one(self.first)
        self.pipeline().one(self.second)
        third = dict(url='https://portal.example.test/lesson/C', title='Other lesson',
                     shared_source_with=[self.first['url']])
        result = self.pipeline().one(third)
        self.assertEqual(result['reason'], 'duplicate-source-different-lesson')
        third.pop('shared_source_with')
        self.assertEqual(self.pipeline().one(third)['reason'], 'duplicate-source-different-lesson')

    def test_failed_shared_conversion_preserves_the_successful_owner(self):
        pipeline = self.pipeline(); first = pipeline.one(self.first)
        self.adapter.fail = True
        failed = pipeline.one(self.second)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(pipeline._source_owners[first['source_hash']], {first['source_identity']})
        self.assertTrue(Path(first['original']).is_file())
        self.assertTrue(Path(first['final']).is_file())
        self.assertNotIn('SECRET', pipeline.manifest.read_text())

    def test_cancellation_does_not_resolve_or_remove_shared_owners(self):
        pipeline = self.pipeline(); first = pipeline.one(self.first)
        pipeline._stop.set(); before = self.adapter.resolved
        self.assertEqual(pipeline.one(self.second)['status'], 'cancelled')
        self.assertEqual(self.adapter.resolved, before)
        self.assertEqual(pipeline._source_owners[first['source_hash']], {first['source_identity']})
        self.assertTrue(Path(first['original']).is_file())

    def test_peer_tracking_parameters_are_not_persisted(self):
        self.first['shared_source_with'] = [self.second['url']+'?signature=SECRET']
        pipeline = self.pipeline(); result = pipeline.one(self.first)
        self.assertEqual(result['status'], 'ok', result)
        self.assertNotIn('SECRET', pipeline.manifest.read_text())
        self.assertEqual(result['shared_source_identities'], [Pipeline.identity(self.second['url'])])

    def test_catalogue_requires_known_reciprocal_non_self_https_peers(self):
        path = self.root/'catalog.json'
        def write(first, second):
            path.write_text(json.dumps(dict(schema='course-catalog/v1', complete=True,
                expected_count=2, collected_count=2, lessons=[first, second])))
        write(self.first, self.second)
        self.assertEqual(len(load_lessons(path)), 2)
        invalid = ['not-a-list', [self.first['url']], ['http://portal.example.test/lesson/B'],
                   ['https://user:password@portal.example.test/lesson/B'],
                   [self.second['url']+'\n'],
                   ['https://portal.example.test/unknown'],
                   [self.second['url'], self.second['url']+'?tracking=1']]
        for references in invalid:
            with self.subTest(references=references):
                write({**self.first, 'shared_source_with': references}, self.second)
                with self.assertRaisesRegex(ValueError, 'shared-source'): load_lessons(path)
        write(self.first, {k:v for k,v in self.second.items() if k!='shared_source_with'})
        with self.assertRaisesRegex(ValueError, 'asymmetric-shared-source'): load_lessons(path)
