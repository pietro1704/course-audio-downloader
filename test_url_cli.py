import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import course_cli
from local_course import main

class URLCLITests(unittest.TestCase):
    def test_public_entry_accepts_only_url_and_enables_removal_by_default(self):
        with tempfile.TemporaryDirectory() as root,patch.dict('os.environ',{'COURSE_AUDIO_ROOT':root}),patch('course_discovery.discover',return_value=({'title':'Example','lessons':[{'id':'lesson'}]},{})) as discover,patch('local_course.LocalCourse') as factory,contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.docs=Path(root)
            self.assertEqual(course_cli.main(['https://example.test/course']),0)
            discover.assert_called_once_with('https://example.test/course')
            factory.assert_called_once_with(Path(root),{},protected_pauses=True)
            factory.return_value.run.assert_called_once_with([{'id':'lesson'}])
    def test_opt_out_uses_separate_root_without_editing_previous_receipts(self):
        with tempfile.TemporaryDirectory() as root,patch.dict('os.environ',{'COURSE_AUDIO_ROOT':root}),patch('course_discovery.discover',return_value=({'title':'Example','lessons':[]},{})),patch('local_course.LocalCourse') as factory,contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.docs=Path(root)
            self.assertEqual(course_cli.main(['--no-silence-removal','https://example.test/course']),0)
            factory.assert_called_once_with(Path(root)/'Pausas originais',{},protected_pauses=False)
    def test_legacy_catalog_equals_syntax_keeps_the_existing_delivery_route(self):
        with patch('course_cli.load_profile',side_effect=ValueError('legacy-route')) as load,patch('local_course.main') as download:
            with self.assertRaisesRegex(ValueError,'legacy-route'):
                course_cli.main(['--catalog=old.json','--profile','profile.json','--root','work','--destination','cloud','--icloud-python','python','--dry-run'])
            load.assert_called_once_with('profile.json');download.assert_not_called()
    def test_inspection_is_read_only_and_closes_private_browser_session(self):
        from unittest.mock import Mock
        runtime=Mock()
        with tempfile.TemporaryDirectory() as root,patch.dict('os.environ',{'COURSE_AUDIO_ROOT':str(Path(root)/'absent')}),patch('course_discovery.discover',return_value=({'title':'Example','lessons':[]},{'_browser_runtime':runtime})),patch('local_course.LocalCourse') as factory,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--inspect','https://example.test/course']),0)
            factory.assert_not_called();self.assertFalse((Path(root)/'absent').exists());runtime.close.assert_called_once()
    def test_discovery_failure_or_cancel_never_starts_downloader(self):
        for error,code in [(ValueError('unsupported-course-url'),2),(KeyboardInterrupt(),130)]:
            with self.subTest(code=code),patch('course_discovery.discover',side_effect=error),patch('local_course.LocalCourse') as factory,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['https://example.test/course']),code);factory.assert_not_called()
    def test_missing_url_and_manual_unknown_options_are_rejected(self):
        for args in [[],['https://example.test/course','--profile','manual.json']]:
            with self.subTest(args=args),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as error:main(args)
            self.assertEqual(error.exception.code,2)

if __name__=='__main__':unittest.main()
