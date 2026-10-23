"""Course-neutral contracts and read-only delivery preflight regression."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from course_cli import main, preflight, build_adapter
from course_catalog import load_lessons
from pipeline import Pipeline, PRESET
from site_profile import load_profile, identity_matches


class CourseContractsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def inputs(self, name, mode='direct'):
        catalog = self.base / (name + '-catalog.json')
        profile = self.base / (name + '-profile.json')
        origin = 'https://' + name + '.example.test'
        lesson = dict(title='2. Aula – Áudio', url=origin+'/lesson/1',
                      site_path=[name, 'Módulo 02', '2. Aula – Áudio'])
        catalog.write_text(json.dumps(dict(schema='course-catalog/v1', complete=True,
              expected_count=1, collected_count=1, lessons=[lesson])))
        profile.write_text(json.dumps(dict(schema='course-site-profile/v1', mode=mode, origin=origin)))
        return catalog, profile, lesson

    def test_same_cli_two_unrelated_courses_no_media_or_files_written(self):
        for name in ['alpha', 'omega']:
            catalog, profile, lesson = self.inputs(name)
            root, cloud = self.base/(name+'-work'), self.base/(name+'-cloud')
            args = ['--catalog',str(catalog),'--profile',str(profile),'--root',str(root),
                    '--destination',str(cloud),'--icloud-python','python','--dry-run','--probe','1','--concurrency','8']
            before = {str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
            output = io.StringIO()
            with patch('course_cli.Delivery', side_effect=AssertionError('dry run creates delivery')), \
                 patch('course_cli.Progress', side_effect=AssertionError('dry run writes progress')), \
                 patch('course_cli.build_adapter') as adapter, patch('course_cli.NativeCloud'), \
                 contextlib.redirect_stdout(output):
                adapter.return_value.resolve.return_value = [object()]
                self.assertEqual(main(args),0)
                adapter.return_value.resolve.assert_called_once_with(lesson)
                adapter.return_value.download.assert_not_called()
                adapter.return_value.encode.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertEqual((report['ok'], report['pending'], report['probed'],report['concurrency']),(True,1,1,8))
            self.assertEqual(before,{str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()})
            self.assertFalse(root.exists()); self.assertFalse(cloud.exists())

    def test_upload_timeout_reaches_delivery_worker(self):
        catalog, profile, _ = self.inputs('slow-cloud')
        root, cloud = self.base/'slow-cloud-work', self.base/'slow-cloud-drive'
        args = ['--catalog',str(catalog),'--profile',str(profile),'--root',str(root),
                '--destination',str(cloud),'--icloud-python','python','--upload-timeout','3600']
        with patch('course_cli.Delivery') as delivery, \
             patch('course_cli.DocumentDelivery'), patch('course_cli.NativeCloud'), \
             patch('course_cli.DocumentBrowser'), patch('course_cli.build_adapter'), \
             contextlib.redirect_stdout(io.StringIO()):
            delivery.return_value.journal = {}
            delivery.return_value.completed_count.return_value = 0
            self.assertEqual(main(args),0)
        self.assertEqual(delivery.call_args.kwargs['upload_timeout'],3600)

    def test_worker_admission_uses_free_space_before_submitting_parallel_sources(self):
        import shutil
        catalog,profile,_=self.inputs('limited-space')
        args=['--catalog',str(catalog),'--profile',str(profile),'--root',str(self.base/'work'),
              '--destination',str(self.base/'cloud'),'--icloud-python','python','--concurrency','8']
        for free,expected in [(5*1024**3,1),(int(6.25*1024**3),4),(int(8.25*1024**3),8)]:
            with self.subTest(free=free),patch('course_cli.Delivery') as delivery,\
                 patch('course_cli.DocumentDelivery') as documents,patch('course_cli.NativeCloud'),\
                 patch('course_cli.DocumentBrowser'),patch('course_cli.build_adapter'),\
                 patch('course_cli.shutil.disk_usage',return_value=shutil._ntuple_diskusage(20*1024**3,0,free)),\
                 contextlib.redirect_stdout(io.StringIO()):
                delivery.return_value.journal={};delivery.return_value.completed_count.return_value=0
                self.assertEqual(main(args),0)
                self.assertEqual(delivery.call_args.kwargs['concurrency'],expected)
                self.assertEqual(delivery.call_args.kwargs['max_concurrency'],8)
                self.assertEqual(delivery.call_args.kwargs['worker_limit'](),expected)
                self.assertEqual(documents.call_args.kwargs['concurrency'],expected)
                status=json.loads((self.base/'work/Documentacao/delivery-status.json').read_text())
                self.assertEqual(status['concurrency'],expected)


    def test_audio_failure_status_retains_actionable_identity_code(self):
        catalog,profile,_=self.inputs('identity-failure')
        root=self.base/'identity-failure-work'
        args=['--catalog',str(catalog),'--profile',str(profile),'--root',str(root),
              '--destination',str(self.base/'cloud'),'--icloud-python','python']
        with patch('course_cli.Delivery') as delivery,patch('course_cli.DocumentDelivery'),\
             patch('course_cli.NativeCloud'),patch('course_cli.DocumentBrowser'),patch('course_cli.build_adapter'),\
             contextlib.redirect_stdout(io.StringIO()):
            delivery.return_value.journal={};delivery.return_value.completed_count.return_value=0
            delivery.return_value.run.side_effect=RuntimeError('embedded-lesson-identity-timeout')
            self.assertEqual(main(args),2)
        status=json.loads((root/'Documentacao/delivery-status.json').read_text())
        self.assertEqual(status['reason'],'embedded-lesson-identity-timeout')

    def test_original_artwork_mode_reaches_bounded_delivery_without_document_navigation(self):
        catalog,profile,_=self.inputs('artwork')
        args=['--catalog',str(catalog),'--profile',str(profile),'--root',str(self.base/'work'),
              '--destination',str(self.base/'cloud'),'--icloud-python','python','--reconcile-original-artwork']
        with patch('course_cli.Delivery') as delivery,patch('course_cli.DocumentDelivery') as documents,\
             patch('course_cli.NativeCloud'),patch('course_cli.DocumentBrowser'),patch('course_cli.build_adapter'),\
             contextlib.redirect_stdout(io.StringIO()):
            delivery.return_value.completed_count.return_value=0
            self.assertEqual(main(args),0)
            self.assertTrue(delivery.return_value.run.call_args.kwargs['reconcile_original_artwork'])
            documents.return_value.run.assert_not_called()

    def test_upload_timeout_rejects_nonpositive_nonfinite_and_over_bound_values(self):
        catalog, profile, _ = self.inputs('bad-timeout')
        args = ['--catalog',str(catalog),'--profile',str(profile),'--root',str(self.base/'work'),
                '--destination',str(self.base/'cloud'),'--icloud-python','python','--dry-run']
        for value in ['0','nan','21601']:
            with self.subTest(value=value), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(args+['--upload-timeout',value])

    def test_identity_origin_and_unsafe_paths_rejected_before_media(self):
        catalog, profile, lesson = self.inputs('alpha')
        rules=load_profile(profile); lessons=load_lessons(catalog)
        rules['origin']='https://other.example.test'
        with self.assertRaisesRegex(ValueError,'origin-mismatch'):
            preflight(lessons,rules,self.base/'work',self.base/'cloud',Mock())
        rules['origin']='https://alpha.example.test';rules['identity_expressions']={'collection':'window.collection'}
        with self.assertRaisesRegex(ValueError,'missing-catalogue-page-identity'):
            preflight(lessons,rules,self.base/'work',self.base/'cloud',Mock())
        self.assertFalse(identity_matches({'identities':{'collection':2}},{'page_identity':{'collection':1}},rules))
        data=json.loads(catalog.read_text());data['lessons'][0]['site_path']=['..','escape'];catalog.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'invalid-site-path'):load_lessons(catalog)

    def test_unknown_schema_is_not_silently_accepted(self):
        catalog,profile,_=self.inputs('alpha')
        data=json.loads(catalog.read_text());data['schema']='unknown/v9';catalog.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'unsupported-catalogue'):load_lessons(catalog)
        data=json.loads(profile.read_text());data['mode']='unknown';profile.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'unsupported-profile-mode'):load_profile(profile)

    def test_cancelled_probe_propagates_without_creating_run(self):
        catalog,profile,_=self.inputs('alpha');root=self.base/'work'
        with patch('course_cli.build_adapter') as adapter,patch('course_cli.NativeCloud'):
            adapter.return_value.resolve.side_effect=KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                main(['--catalog',str(catalog),'--profile',str(profile),'--root',str(root),
                      '--destination',str(self.base/'cloud'),'--icloud-python','python','--dry-run','--probe','1'])
        self.assertFalse(root.exists())

    def test_symlink_parent_and_untracked_output_refused(self):
        catalog,profile,_=self.inputs('alpha');lessons=load_lessons(catalog);rules=load_profile(profile)
        cloud=self.base/'cloud';cloud.mkdir();victim=self.base/'victim';victim.mkdir()
        (cloud/'alpha').symlink_to(victim,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'unsafe-output-symlink'):
            preflight(lessons,rules,self.base/'work',cloud,Mock())

    def test_bounded_concurrency_accepts_eight_and_refuses_nine(self):
        with self.assertRaisesRegex(ValueError,'between 1 and 8'):
            Pipeline(self.base/'work',Mock(),concurrency=9)
        self.assertFalse((self.base/'work').exists())

    def test_prepared_and_complete_receipts_are_read_only_and_fail_closed(self):
        catalog, profile, lesson = self.inputs('alpha')
        lessons, rules = load_lessons(catalog), load_profile(profile)
        root, cloud = self.base/'work', self.base/'cloud'
        final = root.joinpath(*lesson['site_path'][:-1],lesson['title']+'.m4a')
        original = final.parent/'originals'/(final.stem+'.mp3')
        original.parent.mkdir(parents=True);original.write_bytes(b'original source')
        final.write_bytes(b'converted output')
        target = cloud.joinpath(*lesson['site_path'][:-1],lesson['title']+'.m4a')
        ident = Pipeline.identity(lesson['url'])
        receipt = dict(schema=1,state='prepared',source_identity=ident,title=lesson['title'],params=PRESET,
                       original=str(original),final=str(final),cloud=str(target),source_hash=Pipeline.sha256(original),
                       final_hash=Pipeline.sha256(final),size=final.stat().st_size)
        docs=root/'Documentacao';docs.mkdir();journal=docs/'delivery-journal.json'
        journal.write_text(json.dumps({ident:receipt}));native=Mock()
        before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(preflight(lessons,rules,root,cloud,native)[0]['state'],'prepared')
        native.status.assert_not_called()
        self.assertEqual(before,{str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()})
        original.write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError,'prepared-receipt-hash-mismatch'):
            preflight(lessons,rules,root,cloud,native)
        receipt.update(state='complete',uploaded_mtime_ns=1000)
        journal.write_text(json.dumps({ident:receipt}))
        native.status.return_value=dict(ubiquitous=True,uploaded=True,uploading=False,conflicts=False,
                                       upload_error=False,size=receipt['size'],mtime_ns=1100,materialized=False)
        with patch('course_cli.Pipeline.sha256',side_effect=AssertionError('placeholder hydrated')):
            self.assertEqual(preflight(lessons,rules,root,cloud,native)[0]['state'],'complete')
        native.status.return_value['uploaded']=False
        with self.assertRaisesRegex(ValueError,'unconfirmed-cloud-receipt'):
            preflight(lessons,rules,root,cloud,native)
        self.assertTrue(original.exists());self.assertTrue(final.exists())

    def test_uploaded_receipt_can_resume_cleanup_without_missing_original(self):
        catalog,profile,lesson=self.inputs('alpha');root=self.base/'work';docs=root/'Documentacao';docs.mkdir(parents=True)
        final=root.joinpath(*lesson['site_path'][:-1],lesson['title']+'.m4a')
        target=(self.base/'cloud').joinpath(*lesson['site_path'][:-1],lesson['title']+'.m4a')
        receipt=dict(schema=1,state='uploaded',source_identity=Pipeline.identity(lesson['url']),params=PRESET,
                     title=lesson['title'],original=str(final.parent/'originals'/(final.stem+'.mp3')),
                     final=str(final),cloud=str(target),source_hash='a'*64,final_hash='b'*64,size=20,uploaded_mtime_ns=1000)
        (docs/'delivery-journal.json').write_text(json.dumps({receipt['source_identity']:receipt}))
        native=Mock();native.status.return_value=dict(ubiquitous=True,uploaded=True,uploading=False,conflicts=False,
                       upload_error=False,size=20,mtime_ns=1000,materialized=False)
        self.assertEqual(preflight(load_lessons(catalog),load_profile(profile),root,self.base/'cloud',native)[0]['state'],'uploaded')


class CompletedArtworkPolicyTests(unittest.TestCase):
    setUp = CourseContractsTests.setUp
    inputs = CourseContractsTests.inputs
    def invoke(self, refresh=False, failure=None):
        catalog,profile,lesson=self.inputs('resume')
        root=self.base/'work';events=[];ident=Pipeline.identity(lesson['url'])
        with patch('course_cli.Progress') as progress,patch('course_cli.Delivery') as cls,patch('course_cli.build_adapter'),patch('course_cli.NativeCloud'),patch('course_cli.DocumentDelivery'):
            delivery=cls.return_value
            delivery.journal={ident:dict(state='complete',artwork={'origin':'none'})}
            delivery.completed_count.return_value=1
            delivery.refresh_completed_artwork.side_effect=failure
            delivery.run.side_effect=lambda *a,**k:events.append('run')
            args=['--catalog',str(catalog),'--profile',str(profile),'--root',str(root),'--destination',str(self.base/'cloud'),'--icloud-python','python']
            if refresh:args.append('--refresh-completed-artwork')
            result=main(args)
            return result,delivery,events

    def test_default_resume_does_not_retry_completed_artwork(self):
        result,delivery,events=self.invoke(failure=RuntimeError('embedded-lesson-identity-timeout'))
        self.assertEqual(result,0);self.assertEqual(events,['run'])
        delivery.refresh_completed_artwork.assert_not_called()
        delivery.cancel.assert_not_called()

    def test_explicit_refresh_is_still_available(self):
        result,delivery,events=self.invoke(refresh=True)
        self.assertEqual(result,0);delivery.refresh_completed_artwork.assert_called_once()
        self.assertEqual(events,['run'])

    def test_requested_refresh_failure_and_cancellation_are_not_hidden(self):
        for error,code in [(RuntimeError('delivery-identity-mismatch'),2),(KeyboardInterrupt(),130)]:
            result,delivery,events=self.invoke(refresh=True,failure=error)
            self.assertEqual(result,code);self.assertEqual(events,[])
            if code==130:delivery.cancel.assert_called_once()

    def test_actual_refresh_rejects_wrong_receipt_before_touching_cloud(self):
        from cloud_delivery import Delivery
        catalog,profile,lesson=self.inputs('identity')
        root=self.base/'work';cloud=self.base/'cloud';native=Mock()
        d=Delivery(root,cloud,native,adapter=Mock())
        ident=Pipeline.identity(lesson['url']);d.journal[ident]=dict(schema=1,state='complete',source_identity=ident,title='Wrong lesson',params=PRESET,artwork={'origin':'none'},original=str(root/'originals/source.mp3'),final=str(root/'out.m4a'),cloud=str(cloud/'out.m4a'),source_hash='a'*64)
        with self.assertRaisesRegex(RuntimeError,'delivery-identity-mismatch'):
            d.refresh_completed_artwork(lesson,cloud/'out.m4a')
        native.status.assert_not_called()
