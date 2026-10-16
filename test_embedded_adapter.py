import json
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
from embedded_adapter import parse_next_data, media_urls, load_lessons, Candidate
from test_profiles import ProfiledEmbeddedAdapter as EmbeddedAdapter

class EmbeddedTests(unittest.TestCase):
    def test_parse(self):
        data={'props':{'pageProps':{'applicationData':{'mediaAssets':[{'url':'https://cdn/x.m3u8'}]}}}}
        self.assertEqual(media_urls(parse_next_data('<script id="__NEXT_DATA__">'+json.dumps(data)+'</script>')),['https://cdn/x.m3u8'])

    def test_missing_media(self):
        self.assertEqual(media_urls({}),[])
        with self.assertRaisesRegex(RuntimeError,'missing-next-data'): parse_next_data('<html>')
        with self.assertRaisesRegex(RuntimeError,'invalid-next-data'): parse_next_data('<script id="__NEXT_DATA__">oops</script>')

    def test_signed_values_remain_usable_in_memory(self):
        token='https://cdn/x.m3u8?token=SECRETJWT'
        self.assertEqual(media_urls({'props':{'pageProps':{'applicationData':{'mediaAssets':[{'url':token}]}}}}),[token])

    def test_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'c.json'
            p.write_text(json.dumps({'modules':[{'order':8,'title':'Módulo','lessons':[{'number':1,'title':'Aula','url':'https://portal.example.test/x'}]}]}))
            got=load_lessons(p)
            self.assertEqual(got[0]['title'],'Aula')
            self.assertEqual(got[0]['site_path'], ['Módulo', 'Aula'])

    def test_waits_without_repeated_navigation(self):
        lesson={'url':'https://portal.example.test/product/content/ABC','title':'Aula'}
        adapter=EmbeddedAdapter(timeout=1)
        good={'url':lesson['url'],'title':'Aula','ready':'complete','active':[{'path':'/product/content/ABC','title':'Aula'}],'iframes':['https://embed.example.test/embed/vid?jwt=ROTATING']}
        stale=dict(good,title='Previous')
        with patch.object(adapter,'_eval_embedded',side_effect=[stale,stale,good,good,good]) as ev, patch.object(adapter,'_iframe_html',return_value='<script id="__NEXT_DATA__">{"props":{"pageProps":{"applicationData":{"mediaAssets":[{"url":"https://cdn/x.m3u8?token=SECRET"}]}}}}</script>'), patch('embedded_adapter.time.sleep'):
            result=adapter.resolve(lesson)
        self.assertEqual(result[0].kind,'hls-audio')
        self.assertEqual(sum(bool(c.args) for c in ev.call_args_list),1)

    def resolve_with_artwork(self, image, thumbnail_path=None, profile_overrides=None, **overrides):
        lesson={'url':'https://portal.example.test/product/content/ABC','title':'Aula'}
        adapter=EmbeddedAdapter(timeout=1)
        adapter.profile['thumbnail_path']=thumbnail_path if thumbnail_path is not None else ['visual','cover']
        adapter.profile.update(profile_overrides or {})
        payload={'visual':{'cover':image},'visuals':[{}, {'cover':image}],'props':{'pageProps':{'applicationData':{'mediaAssets':[{'url':'https://cdn.test/audio.m3u8?token=PRIVATE'}]}}}}
        page={'url':lesson['url'],'title':'Aula','ready':'complete','active':[{'path':'/product/content/ABC','title':'Aula'}],'iframes':['https://embed.example.test/embed/clip']}
        page.update(overrides)
        with patch.object(adapter,'_eval_embedded',return_value=page),patch.object(adapter,'_iframe_html',return_value='<script id="__NEXT_DATA__">'+json.dumps(payload)+'</script>') as fetch,patch('embedded_adapter.time.sleep'):
            result=adapter.resolve(lesson)
        fetch.assert_called_once()
        return result[0]

    def test_configured_artwork_is_bound_to_resolved_embedded_media(self):
        candidate=self.resolve_with_artwork('https://cdn.test/artwork.jpg?token=PRIVATE')
        self.assertEqual(candidate.thumbnail_url,'https://cdn.test/artwork.jpg?token=PRIVATE')
        self.assertEqual(candidate.referer,'https://embed.example.test/embed/clip')
        self.assertNotIn('PRIVATE',repr(candidate).replace(candidate.url,''))

    def test_missing_or_unsafe_embedded_artwork_does_not_reject_audio(self):
        for image in [None,{},[],17,'http://cdn.test/artwork.jpg','file:///private/artwork.jpg','https:///missing-host','https://[invalid/artwork.jpg','https://user:pass@cdn.test/artwork.jpg']:
            with self.subTest(image=image):
                candidate=self.resolve_with_artwork(image)
                self.assertIsNone(candidate.thumbnail_url)
                self.assertEqual(candidate.kind,'hls-audio')

    def test_configured_array_path_and_invalid_indices(self):
        image='https://cdn.test/artwork.jpg'
        self.assertEqual(self.resolve_with_artwork(image,thumbnail_path=['visuals',1,'cover']).thumbnail_url,image)
        for path in [[],['visuals',3,'cover'],['visuals',-1,'cover'],['visuals',True,'cover'],['absent','cover']]:
            with self.subTest(path=path):self.assertIsNone(self.resolve_with_artwork(image,thumbnail_path=path).thumbnail_url)

    def test_cancelled_embedded_metadata_fetch_propagates(self):
        adapter=EmbeddedAdapter(timeout=1)
        lesson={'url':'https://portal.example.test/product/content/ABC','title':'Aula'}
        page={'url':lesson['url'],'title':'Aula','ready':'complete','active':[{'path':'/product/content/ABC','title':'Aula'}],'iframes':['https://embed.example.test/embed/clip']}
        with patch.object(adapter,'_eval_embedded',return_value=page),patch.object(adapter,'_iframe_html',side_effect=KeyboardInterrupt()),patch('embedded_adapter.time.sleep'):
            with self.assertRaises(KeyboardInterrupt):adapter.resolve(lesson)

    def test_opted_in_audio_rendition_preserves_artwork_and_referer(self):
        master='#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,DEFAULT=YES,URI="track.m3u8?token=PRIVATE"\n#EXT-X-STREAM-INF:BANDWIDTH=1000,CODECS="mp4a.40.2,avc1"\nvideo.m3u8'
        with patch('media_transport.get_text',return_value=master) as fetch:
            candidate=self.resolve_with_artwork('https://cdn.test/cover.jpg',profile_overrides={'prefer_audio_rendition':True})
        self.assertEqual(candidate.url,'https://cdn.test/track.m3u8?token=PRIVATE')
        self.assertEqual(candidate.thumbnail_url,'https://cdn.test/cover.jpg')
        self.assertEqual(candidate.referer,'https://embed.example.test/embed/clip')
        fetch.assert_called_once_with('https://cdn.test/audio.m3u8?token=PRIVATE',candidate.referer)

    def test_audio_rendition_falls_back_to_master_without_changing_muxed_quality(self):
        master='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,CODECS="mp4a.40.2,avc1"\nvideo.m3u8'
        with patch('media_transport.get_text',return_value=master):
            candidate=self.resolve_with_artwork(None,profile_overrides={'prefer_audio_rendition':True})
        self.assertEqual(candidate.url,'https://cdn.test/audio.m3u8?token=PRIVATE')
        with patch('media_transport.get_text',side_effect=AssertionError('opt-in required')):
            self.resolve_with_artwork(None)

    def test_audio_rendition_invalid_manifest_and_cancellation_are_not_suppressed(self):
        for manifest,error in [('not a playlist',RuntimeError),('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,DEFAULT=YES,URI="http://cdn.test/unsafe.m3u8"',RuntimeError),(None,KeyboardInterrupt)]:
            with self.subTest(manifest=manifest),patch('media_transport.get_text',return_value=manifest,side_effect=KeyboardInterrupt() if manifest is None else None):
                with self.assertRaises(error) as failure:
                    self.resolve_with_artwork(None,profile_overrides={'prefer_audio_rendition':True})
                self.assertNotIn('PRIVATE',str(failure.exception))

    def test_rejects_lookalike_host(self):
        with self.assertRaisesRegex(RuntimeError,'unsupported-provider'):
            EmbeddedAdapter().resolve({'url':'https://portal.example.test.evil/x','title':'Aula'})

    def test_ignores_unrelated_media_assets(self):
        self.assertEqual(media_urls({'mediaAssets':[{'url':'https://wrong/x.m3u8'}]}),[])

    def test_drm_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError,'drm-media-not-supported'):
            media_urls({'props':{'pageProps':{'applicationData':{'isDrmEnabled':True}}}})

    def test_old_player_is_not_accepted_for_new_lesson(self):
        adapter=EmbeddedAdapter(timeout=1)
        adapter._last_media=('/content/OLD','/embed/old')
        adapter.profile['prefer_audio_rendition']=True
        lesson={'url':'https://portal.example.test/content/NEW','title':'New'}
        page={'url':lesson['url'],'title':'New','ready':'complete','active':[{'path':'/content/NEW','title':'New'}],'iframes':['https://embed.example.test/embed/old?jwt=hidden']}
        with patch.object(adapter,'_eval_embedded',return_value=page),patch.object(adapter,'_iframe_html') as fetch, patch('embedded_adapter.time.monotonic',side_effect=[0,.1,.2,2]),patch('embedded_adapter.time.sleep'):
            with self.assertRaisesRegex(RuntimeError,'embedded-lesson-identity-timeout'):
                adapter.resolve(lesson)
        fetch.assert_not_called()

    def _download(self, remote=100, local=100, max_bytes=10000000, ffmpeg_code=0):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest=Path(tmp.name)/'source.m4a'
        candidate=Candidate('https://cdn/x.m3u8?token=SECRET','hls-audio')
        def run(cmd,**kwargs):
            if cmd[0]=='ffprobe':
                return subprocess.CompletedProcess(cmd,0,json.dumps({'format':{'duration':str(remote)}}).encode(),b'')
            dest.write_bytes(b'original stream bytes')
            self.assertEqual(cmd[cmd.index('-c:a')+1],'copy')
            self.assertEqual(cmd[cmd.index('-map')+1],'0:a:0')
            self.assertEqual(cmd[cmd.index('-fs')+1],str(max_bytes))
            return subprocess.CompletedProcess(cmd,ffmpeg_code,b'',b'')
        with patch('embedded_adapter.subprocess.run',side_effect=run),patch('embedded_adapter.audio._probe_duration',return_value=local):
            return EmbeddedAdapter(max_source_bytes=max_bytes).download(candidate,dest)

    def test_original_download_is_stream_copy_with_size_cap(self):
        self.assertEqual(self._download().read_bytes(),b'original stream bytes')

    def test_truncated_source_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'incomplete-hls-audio'): self._download(local=95)

    def test_source_budget_rejected_before_ffmpeg(self):
        with self.assertRaisesRegex(RuntimeError,'hls-source-budget-exceeded'): self._download(max_bytes=100)

    def test_ffmpeg_failure_is_not_promoted(self):
        with self.assertRaisesRegex(RuntimeError,'hls-audio-download-failed'): self._download(ffmpeg_code=1)

    def test_nonfinite_source_duration_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'hls-source-budget-exceeded'): self._download(remote=float('nan'))

if __name__=='__main__': unittest.main()
