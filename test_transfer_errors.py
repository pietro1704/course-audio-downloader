import errno
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from documents import DocumentDelivery
from pipeline import Pipeline
from transfer_errors import failure_reason


class FailureDiagnosticTests(unittest.TestCase):
    def test_known_codes_and_resource_failures_are_preserved_without_raw_messages(self):
        self.assertEqual(failure_reason(RuntimeError('embedded-lesson-identity-timeout')),'embedded-lesson-identity-timeout')
        self.assertEqual(failure_reason(subprocess.TimeoutExpired(['secret-command'],15)),'timeout-expired')
        self.assertEqual(failure_reason(OSError(errno.ENOSPC,'private-path')),'disk-full')
        for error in [RuntimeError('https://cdn.test/file?token=SECRET'),ValueError('private-user-data')]:
            self.assertEqual(failure_reason(error),type(error).__name__)

    def test_pipeline_manifest_keeps_identity_timeout_and_redacts_unknown_messages(self):
        for message,expected in [('embedded-lesson-identity-timeout','embedded-lesson-identity-timeout'),
                                 ('https://cdn.test/file?token=SECRET','RuntimeError')]:
            with self.subTest(message=expected),tempfile.TemporaryDirectory() as tmp:
                class Adapter:
                    def resolve(self,lesson):raise RuntimeError(message)
                pipeline=Pipeline(tmp,Adapter(),reserve_bytes=0)
                record=pipeline.one({'url':'https://portal.test/lesson','title':'Lesson'})
                self.assertEqual(record['reason'],expected)
                stored=Path(pipeline.manifest).read_text()
                self.assertNotIn('SECRET',stored);self.assertNotIn('cdn.test',stored)

    def test_document_login_failure_is_durable_and_stops_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Adapter:
                def __init__(self):self.calls=0
                def inventory(self,lesson):self.calls+=1;raise RuntimeError('login-required')
            adapter=Adapter();root=Path(tmp)/'work'
            delivery=DocumentDelivery(root,Path(tmp)/'cloud',object(),adapter,reserve_bytes=0)
            lessons=[dict(url='https://portal.test/one',title='One',site_path=['Course','One']),
                     dict(url='https://portal.test/two',title='Two',site_path=['Course','Two'])]
            with self.assertRaisesRegex(RuntimeError,'login-required'):delivery.run(lessons)
            self.assertEqual(adapter.calls,1)
            page=delivery.pages[Pipeline.identity(lessons[0]['url'])]
            self.assertEqual(page['reason'],'login-required');self.assertFalse(page['inventory_complete'])
            status=json.loads((root/'Documentacao/documents-status.json').read_text())
            self.assertEqual((status['stage'],status['reason']),('failed','login-required'))
