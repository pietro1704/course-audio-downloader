"""CLI regressions: documents are default, backfill never invokes audio."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from course_cli import main
from course_catalog import path_parts


class DocumentDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.catalog = self.base/'catalog.json'
        self.profile = self.base/'profile.json'
        self.lesson = dict(title='Lesson', url='https://learn.example.test/lesson/1',
                           site_path=['Course', 'Module', 'Lesson'])
        self.catalog.write_text(json.dumps(dict(schema='course-catalog/v1', complete=True,
            expected_count=1, collected_count=1, lessons=[self.lesson])))
        self.profile.write_text(json.dumps(dict(schema='course-site-profile/v1', mode='direct',
                                               origin='https://learn.example.test')))
        self.args = ['--catalog',str(self.catalog),'--profile',str(self.profile),
            '--root',str(self.base/'work'),'--destination',str(self.base/'cloud'),
            '--icloud-python','python']

    def test_documents_run_by_default_even_when_audio_already_complete(self):
        with patch('course_cli.Delivery') as audio,patch('course_cli.Progress'), \
             patch('course_cli.NativeCloud'),patch('course_cli.build_adapter'), \
             patch('course_cli.DocumentDelivery',create=True) as documents:
            audio.return_value.journal = {}
            audio.return_value.completed_count.return_value = 1
            self.assertEqual(main(self.args),0)
            documents.return_value.run.assert_called_once()
            selected=documents.return_value.run.call_args.args[0]
            self.assertEqual(selected[0]['url'],self.lesson['url'])

    def test_document_only_backfill_does_not_construct_audio_delivery(self):
        with patch('course_cli.Delivery') as audio,patch('course_cli.Progress'), \
             patch('course_cli.NativeCloud'),patch('course_cli.build_adapter') as adapter, \
             patch('course_cli.DocumentDelivery',create=True) as documents, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self.args+['--documents-only']),0)
            audio.assert_not_called()
            adapter.assert_not_called()
            documents.return_value.run.assert_called_once()

    def test_literal_colon_is_a_native_filename_not_a_directory_separator(self):
        self.assertEqual(path_parts(['Course','Module: Part 1','Lesson']),
                         ['Course','Module: Part 1','Lesson'])
        for bad in ('bad/name','bad\\name','..','\x00'):
            with self.assertRaises(ValueError):path_parts(['Course',bad,'Lesson'])

    def test_document_dry_run_is_read_only_and_never_constructs_delivery(self):
        before={p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with patch('course_cli.DocumentDelivery') as delivery,patch('course_cli.NativeCloud'), \
                patch('course_cli.build_adapter') as audio,contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(self.args+['--documents-only','--dry-run']),0)
            delivery.assert_not_called();audio.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())['total'],1)
        self.assertEqual(before,{p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()})
        self.assertFalse((self.base/'work').exists())

    def test_document_failure_and_cancellation_are_not_success(self):
        for error,code in [(RuntimeError('private URL'),2),(KeyboardInterrupt(),130)]:
            with patch('course_cli.DocumentDelivery') as documents,patch('course_cli.NativeCloud'):
                documents.return_value.run.side_effect=error
                self.assertEqual(main(self.args+['--documents-only']),code)
                if code==130:documents.return_value.cancel.assert_called_once()
