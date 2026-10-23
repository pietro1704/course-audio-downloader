"""Document failures retain sanitized, source-bound evidence across retries."""
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from test_documents import Response, PDF
import test_documents as fixtures
from documents import DocumentDelivery
from pipeline import Pipeline


class DocumentFailureTests(unittest.TestCase):
    setUp = fixtures.DocumentsTests.setUp
    transfer = fixtures.DocumentsTests.transfer

    def records(self):
        path = self.d.docs / 'documents-failures.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def http_failure(self, code):
        return HTTPError(self.resource['url'], code, 'private signed URL must stay private', {}, None)

    def test_permanent_http_failure_is_durable_source_bound_and_redacted(self):
        with patch('documents.urllib.request.urlopen', side_effect=self.http_failure(410)):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                self.d.run([self.lesson])
        key = self.d.identity(self.lesson, self.resource)
        record = self.records()[key]
        self.assertEqual(record['identity'], key)
        self.assertEqual(record['source_identity'], Pipeline.identity(self.lesson['url']))
        self.assertEqual(record['name'], self.resource['name'])
        self.assertEqual((record['state'], record['reason'], record['http_status']), ('failed', 'document-http-410', 410))
        self.assertFalse(record['retryable'])
        self.assertEqual(record['attempts'], 1)
        saved = json.dumps(self.records())
        self.assertNotIn('private', saved)
        self.assertNotIn('https://', saved)
        self.assertFalse(self.d.journal)
        self.assertFalse(self.d.destination.exists())

    def test_recoverable_http_failures_count_restarts_then_resolve_after_native_upload(self):
        key = self.d.identity(self.lesson, self.resource)
        for number, code in enumerate([429, 503], 1):
            with patch('documents.urllib.request.urlopen', side_effect=self.http_failure(code)):
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    self.d.run([self.lesson])
            record = self.records()[key]
            self.assertTrue(record['retryable'])
            self.assertEqual(record['attempts'], number)
            self.d = DocumentDelivery(self.d.root, self.d.destination, self.cloud, self.adapter,
                                      reserve_bytes=0, upload_timeout=0, poll_seconds=0)
        self.transfer()
        self.assertEqual(self.records()[key]['state'], 'resolved')
        self.assertEqual(self.records()[key]['attempts'], 2)
        self.assertEqual(Path(self.d.journal[key]['cloud']).read_bytes(), PDF)
        self.assertFalse(Path(self.d.journal[key]['final']).exists())

    def test_same_resource_on_two_lessons_has_separate_failure_identity(self):
        other = dict(self.lesson, url='https://omega.example.test/lesson/2', site_path=['Omega', 'Lesson'])
        with patch('documents.urllib.request.urlopen', side_effect=self.http_failure(404)):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                self.d.run([self.lesson, other])
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual({r['source_identity'] for r in records.values()},
                         {Pipeline.identity(l['url']) for l in [self.lesson, other]})
        self.assertTrue(all(r['reason'] == 'document-http-404' and not r['retryable'] for r in records.values()))

    def test_cancelled_stream_preserves_partial_without_persistent_failure(self):
        delivery = self.d
        class CancelResponse(Response):
            def read(self, size):
                chunk = super().read(size)
                delivery.cancel()
                return chunk
        with patch('documents.urllib.request.urlopen', return_value=CancelResponse()):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                delivery.one(self.lesson, self.resource)
        self.assertFalse(self.records())
        self.assertFalse(delivery.journal)
        self.assertTrue(list((delivery.root / '.document-staging').glob('*.partial')))

    def test_foreign_receipt_identity_cannot_mark_confirmed_document_failed(self):
        self.transfer()
        before = json.loads(self.d.journal_path.read_text())
        changed = dict(self.resource, name='Different.pdf')
        with self.assertRaisesRegex(ValueError, 'identity-mismatch'):
            self.d.one(self.lesson, changed)
        self.assertFalse(self.records())
        self.assertEqual(json.loads(self.d.journal_path.read_text()), before)

    def test_pending_native_upload_is_recorded_without_deleting_original(self):
        self.cloud.ready = False
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            self.transfer()
        key = self.d.identity(self.lesson, self.resource)
        record = self.records()[key]
        self.assertEqual(record['reason'], 'icloud-upload-timeout')
        self.assertTrue(record['retryable'])
        self.assertTrue(Path(self.d.journal[key]['final']).exists())
        self.cloud.ready = True
        with patch('documents.urllib.request.urlopen', side_effect=AssertionError('must resume prepared data')):
            self.d.run([self.lesson])
        self.assertEqual(self.records()[key]['state'], 'resolved')
        self.assertFalse(Path(self.d.journal[key]['final']).exists())
