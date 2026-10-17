import json
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import Candidate, Pipeline
from browser_adapter import SafariAdapter
from cloud_delivery import Delivery


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cover = self.root / 'cover.jpg'
        self.ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=48x48',
                    '-frames:v', '1', str(self.cover))
        self.source = self.root / 'source.mp3'
        self.ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.6',
                    '-i', str(self.cover), '-map', '0:a', '-map', '1:v',
                    '-c:a', 'libmp3lame', '-c:v', 'copy',
                    '-disposition:v', 'attached_pic', str(self.source))

    def ffmpeg(self, *args):
        return subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *args],
                              check=True, capture_output=True).stdout

    def pipeline(self, source=None, output=None, **candidate_fields):
        source = source or self.source
        class LocalAdapter(SafariAdapter):
            def resolve(self, lesson):
                return [Candidate('https://cdn.test/lesson.mp3', 'mp3', **candidate_fields)]
            def download(self, candidate, destination):
                shutil.copyfile(source, destination)
                return destination
        return Pipeline(output or self.root / 'output', LocalAdapter(), reserve_bytes=0)

    def cover_bytes(self, path):
        return self.ffmpeg('-i', str(path), '-map', '0:v:0', '-c:v', 'copy',
                           '-frames:v', '1', '-f', 'image2pipe', 'pipe:1')

    def test_embedded_mp3_cover_survives_public_pipeline_exactly(self):
        result = self.pipeline().one({'url': 'https://test/lesson', 'title': 'Lesson'})
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.cover_bytes(result['final']), self.cover.read_bytes())
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of',
                                'json', result['final']], check=True, capture_output=True)
        streams = json.loads(probe.stdout)['streams']
        self.assertEqual([s['codec_type'] for s in streams], ['audio', 'video'])
        self.assertEqual(streams[1]['disposition']['attached_pic'], 1)

    def test_video_thumbnail_fills_missing_cover(self):
        plain = self.root / 'plain.mp3'
        self.ffmpeg('-i', str(self.source), '-map', '0:a', '-c', 'copy', str(plain))
        class Response(io.BytesIO):
            headers = {'Content-Length': str(self.cover.stat().st_size)}
            def geturl(self):
                return 'https://cdn.test/poster.jpg'
        with patch('browser_adapter.urllib.request.urlopen',
                   return_value=Response(self.cover.read_bytes())):
            result = self.pipeline(plain, thumbnail_url='https://cdn.test/poster.jpg').one(
                {'url': 'https://test/lesson', 'title': 'Lesson'})
        self.assertEqual(result['status'], 'ok', result)
        self.assertTrue(self.cover_bytes(result['final']))
        self.assertEqual(result['artwork']['origin'], 'video')

    def test_embedded_json_resolver_artwork_reaches_real_conversion(self):
        from test_profiles import ProfiledEmbeddedAdapter
        plain=self.root/'plain-source.mp3'
        self.ffmpeg('-i',str(self.source),'-map','0:a','-c','copy',str(plain))
        lesson={'url':'https://portal.example.test/product/content/ABC','title':'Lesson'}
        page={'url':lesson['url'],'title':lesson['title'],'ready':'complete','active':[{'path':'/product/content/ABC','title':lesson['title']}],'iframes':['https://embed.example.test/embed/clip']}
        payload={'visual':{'cover':'https://cdn.test/artwork.jpg'},'props':{'pageProps':{'applicationData':{'mediaAssets':[{'url':'https://cdn.test/audio.m3u8'}]}}}}
        class Response(io.BytesIO):
            headers={}
            def geturl(self):return 'https://cdn.test/artwork.jpg'
        for embedded,source in [(False,plain),(True,self.source)]:
            with self.subTest(embedded=embedded):
                adapter=ProfiledEmbeddedAdapter(encoder='aac');adapter.profile['thumbnail_path']=['visual','cover']
                def download(candidate,destination):shutil.copyfile(source,destination);return destination
                with patch.object(adapter,'_eval_embedded',return_value=page),patch.object(adapter,'_iframe_html',return_value='<script id="__NEXT_DATA__">'+json.dumps(payload)+'</script>'),patch.object(adapter,'download',side_effect=download),patch('embedded_adapter.time.sleep'),patch('browser_adapter.urllib.request.urlopen',return_value=Response(self.cover.read_bytes())) as image:
                    result=Pipeline(self.root/str(embedded),adapter,reserve_bytes=0).one(lesson)
                self.assertEqual(result['status'],'ok',result)
                self.assertEqual(result['artwork']['origin'],'embedded' if embedded else 'video')
                self.assertTrue(self.cover_bytes(result['final']))
                self.assertEqual(image.call_count,0 if embedded else 1)

    def test_original_cover_has_priority_without_any_thumbnail_request(self):
        with patch('browser_adapter.urllib.request.urlopen', side_effect=AssertionError('unneeded fetch')) as fetch:
            result = self.pipeline(thumbnail_url='https://cdn.test/other.jpg').one('https://test/lesson')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.cover_bytes(result['final']), self.cover.read_bytes())
        self.assertEqual(result['artwork']['origin'], 'embedded')
        fetch.assert_not_called()

    def test_missing_or_unavailable_video_image_keeps_valid_audio(self):
        plain = self.root / 'plain.mp3'
        self.ffmpeg('-i', str(self.source), '-map', '0:a', '-c', 'copy', str(plain))
        for index, fields in enumerate([{}, {'thumbnail_url': 'https://cdn.test/missing.jpg'},
                                       {'thumbnail_url': 'file:///private/image.jpg'}]):
            with self.subTest(fields=fields), patch('browser_adapter.urllib.request.urlopen', side_effect=OSError('offline')):
                pipe = self.pipeline(plain, output=self.root / str(index), **fields)
                result = pipe.one('https://test/lesson')
                self.assertEqual(result['status'], 'ok', result)
                self.assertEqual(result['artwork']['origin'], 'none')

    def test_attaching_cover_failure_preserves_original_and_existing_final(self):
        pipeline = self.pipeline()
        final = pipeline.root / 'lesson.m4a'
        final.write_bytes(b'previous final')
        real_run = subprocess.run
        def run(args, **kwargs):
            if any('with-cover.m4a' in str(arg) for arg in args):
                raise OSError('attachment failed')
            return real_run(args, **kwargs)
        with patch('artwork.subprocess.run', side_effect=run):
            result = pipeline.one({'url': 'https://test/lesson', 'stem': 'lesson'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(final.read_bytes(), b'previous final')
        originals = list((pipeline.root / 'originals').glob('*.mp3'))
        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0].read_bytes(), self.source.read_bytes())

    def test_video_player_thumbnail_requires_matching_video_and_library(self):
        from test_profiles import thumbnail_player
        frame = 'https://player.example.test/embed/123/video-id'
        for video_id, library_id, expected in [('video-id', '123', 'https://cdn.test/poster.jpg'),
                                                ('different', '123', None), ('video-id', '456', None)]:
            html = (f'<media-player video-id="{video_id}" library-id="{library_id}" '
                    'cast-poster="https://cdn.test/poster.jpg"></media-player>')
            with self.subTest(video_id=video_id, library_id=library_id), patch('media_transport.get_text', return_value=html):
                self.assertEqual(thumbnail_player(frame, 'https://test/lesson'), expected)

    def test_candidate_captures_video_thumbnail_without_logging_signed_url(self):
        page = {'url': 'https://lessons.example.test/aulas/a',
                'posters': ['https://cdn.test/poster.jpg?token=PRIVATE'],
                'frames': ['https://player.example.test/embed/123/video-id'],
                'links': [{'url': 'https://cdn.test/audio.mp3', 'label': 'Áudio da aula (mp3)'}]}
        candidate = SafariAdapter.candidates(page)[0]
        self.assertEqual(candidate.thumbnail_url, page['posters'][0])
        self.assertEqual(candidate.player_url, page['frames'][0])
        self.assertNotIn('PRIVATE', repr(candidate))

    def test_png_cover_bytes_are_preserved(self):
        png = self.root / 'cover.png'
        self.ffmpeg('-i', str(self.cover), str(png))
        source = self.root / 'png.mp3'
        self.ffmpeg('-i', str(self.source), '-i', str(png), '-map', '0:a',
                    '-map', '1:v', '-c', 'copy', '-disposition:v', 'attached_pic', str(source))
        result = self.pipeline(source).one('https://test/lesson')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.cover_bytes(result['final']), png.read_bytes())

    def test_cover_survives_delivery_pending_upload_and_resume(self):
        class Cloud:
            ready = False
            def status(self, path):
                stat = path.stat()
                return dict(ubiquitous=True, uploaded=self.ready, uploading=not self.ready,
                            conflicts=False, upload_error=False, size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns, materialized=True)
            def evict(self, path):
                pass
        cloud = Cloud()
        pipe = self.pipeline()
        destination = self.root / 'cloud'
        target = destination / 'lesson.m4a'
        lesson = {'url': 'https://test/lesson', 'title': 'Lesson', 'stem': 'lesson'}
        delivery = Delivery(pipe.root, destination, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        with self.assertRaisesRegex(RuntimeError, 'icloud-upload-timeout'):
            delivery.process(lesson, target)
        receipt = next(iter(delivery.journal.values()))
        self.assertEqual(receipt['artwork']['origin'], 'embedded')
        self.assertTrue(Path(receipt['original']).is_file())
        self.assertEqual(self.cover_bytes(target), self.cover.read_bytes())
        cloud.ready = True
        delivery = Delivery(pipe.root, destination, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        with patch.object(pipe.adapter, 'resolve', side_effect=AssertionError('unneeded resolve')):
            receipt = delivery.process(lesson, target)
        self.assertEqual(receipt['state'], 'complete')
        self.assertFalse(Path(receipt['original']).exists())
        self.assertFalse(Path(receipt['final']).exists())
        self.assertEqual(self.cover_bytes(target), self.cover.read_bytes())

    def old_pending(self):
        from test_cloud_delivery import Cloud
        cloud = Cloud()
        cloud.ready = False
        pipe = self.pipeline()
        target = self.root / 'cloud/lesson.m4a'
        lesson = {'url': 'https://test/lesson', 'title': 'Lesson', 'stem': 'lesson'}
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        with self.assertRaisesRegex(RuntimeError, 'icloud-upload-timeout'):
            delivery.process(lesson, target)
        receipt = next(iter(delivery.journal.values()))
        local = Path(receipt['final'])
        stripped = self.root / 'stripped.m4a'
        self.ffmpeg('-i', str(local), '-map', '0:a:0', '-c', 'copy', str(stripped))
        shutil.copyfile(stripped, local)
        shutil.copyfile(stripped, target)
        receipt.update(final_hash=Pipeline.sha256(local), size=local.stat().st_size,
                       published_mtime_ns=target.stat().st_mtime_ns)
        receipt.pop('artwork')
        delivery.save(receipt)
        cloud.ready = True
        return pipe, cloud, target, lesson

    def completed_without_cover(self):
        from test_cloud_delivery import Cloud
        plain = self.root / 'plain.mp3'
        self.ffmpeg('-i', str(self.source), '-map', '0:a', '-c', 'copy', str(plain))
        pipe = self.pipeline(plain)
        cloud = Cloud()
        target = self.root / 'cloud/lesson.m4a'
        lesson = {'url': 'https://test/lesson', 'title': 'Lesson', 'stem': 'lesson'}
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter, reserve_bytes=0, upload_timeout=0)
        receipt = delivery.process(lesson, target)
        self.assertEqual(receipt['artwork']['origin'], 'none')
        self.assertFalse(Path(receipt['original']).exists())
        return pipe, cloud, target, lesson, receipt

    def test_completed_missing_cover_refresh_preserves_audio_and_source_identity(self):
        pipe, cloud, target, lesson, old = self.completed_without_cover()
        before = self.ffmpeg('-i', str(target), '-map', '0:a:0', '-c:a', 'copy', '-f', 'adts', 'pipe:1')
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter, reserve_bytes=0, upload_timeout=0)
        with patch.object(pipe.adapter, 'thumbnail', return_value=self.cover), \
             patch.object(pipe.adapter, 'encode', side_effect=AssertionError('reencode')):
            receipt = delivery.refresh_completed_artwork(lesson, target)
        self.assertEqual(receipt['state'], 'complete')
        self.assertEqual(receipt['artwork']['origin'], 'video')
        self.assertEqual(receipt['source_hash'], old['source_hash'])
        self.assertEqual(receipt['source_identity'], old['source_identity'])
        self.assertEqual(self.cover_bytes(target), self.cover.read_bytes())
        self.assertEqual(self.ffmpeg('-i', str(target), '-map', '0:a:0', '-c:a', 'copy', '-f', 'adts', 'pipe:1'), before)
        self.assertFalse(Path(receipt['final']).exists())

    def legacy_completed(self):
        pipe,cloud,target,lesson=self.old_pending()
        delivery=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0,upload_timeout=0)
        # Model an old native receipt whose source and local encoding were cleaned
        # before the attached-artwork preservation policy existed.
        receipt=dict(delivery.journal[Pipeline.identity(lesson['url'])])
        receipt.update(state='complete',uploaded_mtime_ns=target.stat().st_mtime_ns)
        delivery.save(receipt)
        Path(receipt['original']).unlink();Path(receipt['final']).unlink()
        return pipe,cloud,target,lesson,receipt

    def test_original_artwork_reconciliation_preserves_audio_and_cleans_after_upload(self):
        pipe,cloud,target,lesson,old=self.legacy_completed()
        before=self.ffmpeg('-i',str(target),'-map','0:a:0','-c:a','copy','-f','adts','pipe:1')
        delivery=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0,upload_timeout=0)
        with patch.object(pipe.adapter,'encode',side_effect=AssertionError('must-not-reencode')):
            receipts=delivery.run([(lesson,target)],reconcile_original_artwork=True)
        receipt=receipts[0]
        self.assertEqual(receipt['state'],'complete');self.assertEqual(receipt['artwork']['origin'],'embedded')
        self.assertEqual(receipt['source_hash'],old['source_hash'])
        self.assertEqual(self.cover_bytes(target),self.cover.read_bytes())
        self.assertEqual(self.ffmpeg('-i',str(target),'-map','0:a:0','-c:a','copy','-f','adts','pipe:1'),before)
        self.assertFalse(Path(receipt['original']).exists());self.assertFalse(Path(receipt['final']).exists())

    def test_original_reconciliation_rejects_changed_source_without_mutating_cloud(self):
        pipe,cloud,target,lesson,old=self.legacy_completed()
        before=target.read_bytes();delivery=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0)
        def wrong(candidate,path):path.write_bytes(b'wrong-source');return path
        with patch.object(pipe.adapter,'download',side_effect=wrong):
            with self.assertRaisesRegex(RuntimeError,'artwork-original-content-changed'):
                delivery.run([(lesson,target)],reconcile_original_artwork=True)
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(delivery.journal[Pipeline.identity(lesson['url'])],old)
        self.assertTrue(any(p.read_bytes()==b'wrong-source' for p in pipe.root.rglob('source.mp3')))

    def test_original_reconciliation_cancellation_preserves_old_receipt_and_resumes(self):
        pipe,cloud,target,lesson,old=self.legacy_completed()
        before=target.read_bytes();delivery=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0)
        download=pipe.adapter.download
        def cancel(candidate,path):
            result=download(candidate,path);delivery.cancel();return result
        with patch.object(pipe.adapter,'download',side_effect=cancel):
            with self.assertRaisesRegex(RuntimeError,'delivery-cancelled'):
                delivery.run([(lesson,target)],reconcile_original_artwork=True)
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(delivery.journal[Pipeline.identity(lesson['url'])],old)
        resumed=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0,upload_timeout=0)
        self.assertEqual(resumed.run([(lesson,target)],reconcile_original_artwork=True)[0]['artwork']['origin'],'embedded')

    def test_original_reconciliation_rejects_identity_before_source_download(self):
        pipe,cloud,target,lesson,old=self.legacy_completed()
        delivery=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0)
        with patch.object(pipe.adapter,'download') as download:
            with self.assertRaisesRegex(RuntimeError,'delivery-identity-mismatch'):
                delivery.run([(lesson,target.with_name('other.m4a'))],reconcile_original_artwork=True)
            download.assert_not_called()

    def test_original_reconciliation_waits_for_native_upload_before_cleanup(self):
        pipe,cloud,target,lesson,old=self.legacy_completed()
        calls=[]
        def upload_pending(path):
            calls.append(path)
            if len(calls)>1:cloud.ready=False
        cloud.on_status=upload_pending
        delivery=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0,upload_timeout=0)
        with self.assertRaisesRegex(RuntimeError,'icloud-upload-timeout'):
            delivery.run([(lesson,target)],reconcile_original_artwork=True)
        self.assertTrue(Path(old['original']).is_file());self.assertTrue(Path(old['final']).is_file())
        cloud.on_status=None;cloud.ready=True
        resumed=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0,upload_timeout=0)
        self.assertEqual(resumed.run([(lesson,target)],reconcile_original_artwork=True)[0]['state'],'complete')
        self.assertFalse(Path(old['original']).exists());self.assertFalse(Path(old['final']).exists())

    def test_original_reconciliation_records_no_cover_after_original_and_video_attempts(self):
        pipe,cloud,target,lesson,old=self.completed_without_cover()
        delivery=Delivery(pipe.root,target.parent,cloud,pipe.adapter,reserve_bytes=0,upload_timeout=0)
        old=dict(old);old.pop('artwork');delivery.save(old)
        with patch.object(pipe.adapter,'thumbnail',return_value=None) as thumbnail:
            receipt=delivery.run([(lesson,target)],reconcile_original_artwork=True)[0]
        self.assertEqual(receipt['artwork']['origin'],'none')
        self.assertIn('original_artwork_recovered_at',receipt)
        thumbnail.assert_called_once()

    def test_completed_cover_refresh_recovers_cancellation_without_original(self):
        pipe, cloud, target, lesson, old = self.completed_without_cover()
        before = target.read_bytes()
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter, reserve_bytes=0, upload_timeout=0)
        replace = __import__('os').replace
        def interrupt(source, dest):
            if Path(dest) == target: raise KeyboardInterrupt()
            return replace(source, dest)
        with patch.object(pipe.adapter, 'thumbnail', return_value=self.cover), patch('cloud_delivery.os.replace', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                delivery.refresh_completed_artwork(lesson, target)
        self.assertEqual(target.read_bytes(), before)
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter, reserve_bytes=0, upload_timeout=0)
        with patch.object(pipe.adapter, 'resolve', side_effect=AssertionError('repeat fetch')):
            receipt = delivery.process(lesson, target)
        self.assertEqual(receipt['state'], 'complete')
        self.assertEqual(self.cover_bytes(target), self.cover.read_bytes())
        self.assertEqual(receipt['source_hash'], old['source_hash'])

    def test_completed_cover_refresh_rejects_identity_and_cloud_conflict(self):
        pipe, cloud, target, lesson, old = self.completed_without_cover()
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter, reserve_bytes=0, upload_timeout=0)
        with self.assertRaisesRegex(RuntimeError, 'identity-mismatch'):
            delivery.refresh_completed_artwork(lesson, target.with_name('other.m4a'))
        cloud.conflicts = True
        with self.assertRaisesRegex(RuntimeError, 'upload-error-or-conflict'):
            delivery.refresh_completed_artwork(lesson, target)
        self.assertFalse(Path(old['final']).exists())
        self.assertEqual(Pipeline.sha256(target), old['final_hash'])

    def test_old_pending_delivery_gets_cover_without_reencoding_audio(self):
        pipe, cloud, target, lesson = self.old_pending()
        before = self.ffmpeg('-i', str(target), '-map', '0:a:0', '-c:a', 'copy',
                             '-f', 'adts', 'pipe:1')
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        with patch.object(pipe.adapter, 'encode', side_effect=AssertionError('audio reencode')):
            result = delivery.process(lesson, target)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['artwork']['origin'], 'embedded')
        self.assertEqual(self.cover_bytes(target), self.cover.read_bytes())
        self.assertEqual(self.ffmpeg('-i', str(target), '-map', '0:a:0', '-c:a', 'copy',
                                     '-f', 'adts', 'pipe:1'), before)

    def test_pending_cover_replacement_resumes_after_cancellation(self):
        pipe, cloud, target, lesson = self.old_pending()
        previous = target.read_bytes()
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        real_replace = __import__('os').replace
        def interrupt(source, destination):
            if Path(destination) == target:
                raise KeyboardInterrupt()
            return real_replace(source, destination)
        with patch('cloud_delivery.os.replace', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                delivery.process(lesson, target)
        self.assertEqual(target.read_bytes(), previous)
        receipt = next(iter(delivery.journal.values()))
        self.assertIn('artwork_update', receipt)
        self.assertTrue(Path(receipt['original']).is_file())
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        with patch.object(pipe.adapter, 'resolve', side_effect=AssertionError('repeated resolution')):
            receipt = delivery.process(lesson, target)
        self.assertEqual(receipt['state'], 'complete')
        self.assertNotIn('artwork_update', receipt)
        self.assertEqual(self.cover_bytes(target), self.cover.read_bytes())

    def test_pending_cover_replacement_refuses_changed_cloud_file(self):
        pipe, cloud, target, lesson = self.old_pending()
        target.write_bytes(b'unrelated cloud file')
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        with self.assertRaisesRegex(RuntimeError, 'cloud-destination-conflict'):
            delivery.process(lesson, target)
        self.assertEqual(target.read_bytes(), b'unrelated cloud file')
        receipt = next(iter(delivery.journal.values()))
        self.assertTrue(Path(receipt['original']).is_file())

    def test_pending_cover_transaction_rejects_unbound_staging_path(self):
        pipe, cloud, target, lesson = self.old_pending()
        delivery = Delivery(pipe.root, target.parent, cloud, pipe.adapter,
                            reserve_bytes=0, upload_timeout=0)
        receipt = next(iter(delivery.journal.values()))
        unrelated = Path(receipt['final']).parent / '.staging/encoded.m4a'
        unrelated.parent.mkdir(exist_ok=True)
        unrelated.write_bytes(b'unrelated staging file')
        receipt['artwork_update'] = dict(previous_hash=receipt['final_hash'],
            final_hash=receipt['final_hash'], size=receipt['size'], staged=str(unrelated),
            artwork={'version': 1, 'origin': 'none'})
        delivery.save(receipt)
        previous = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'delivery-identity-mismatch'):
            delivery.process(lesson, target)
        self.assertEqual(unrelated.read_bytes(), b'unrelated staging file')
        self.assertEqual(target.read_bytes(), previous)
        self.assertTrue(Path(receipt['original']).is_file())


if __name__ == '__main__':
    unittest.main()
