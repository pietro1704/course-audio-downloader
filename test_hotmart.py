import json
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
from hotmart_cli import parse_next_data, media_urls, load_lessons, HotmartAdapter, Candidate

class HotmartTests(unittest.TestCase):
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
            p.write_text(json.dumps({'modules':[{'order':8,'title':'Módulo','lessons':[{'number':1,'title':'Aula','url':'https://hotmart.com/x'}]}]}))
            got=load_lessons(p)
            self.assertEqual(got[0]['title'],'Aula')
            self.assertTrue(got[0]['stem'].startswith('08-001-'))

    def test_waits_without_repeated_navigation(self):
        lesson={'url':'https://hotmart.com/product/content/ABC','title':'Aula'}
        adapter=HotmartAdapter(timeout=1)
        good={'url':lesson['url'],'title':'Aula','ready':'complete','active':[{'path':'/product/content/ABC','title':'Aula'}],'iframes':['https://cf-embed.play.hotmart.com/embed/vid?jwt=ROTATING']}
        stale=dict(good,title='Previous')
        with patch.object(adapter,'_eval_hotmart',side_effect=[stale,stale,good,good,good]) as ev, patch.object(adapter,'_iframe_html',return_value='<script id="__NEXT_DATA__">{"props":{"pageProps":{"applicationData":{"mediaAssets":[{"url":"https://cdn/x.m3u8?token=SECRET"}]}}}}</script>'), patch('hotmart_cli.time.sleep'):
            result=adapter.resolve(lesson)
        self.assertEqual(result[0].kind,'hls-audio')
        self.assertEqual(sum(bool(c.args) for c in ev.call_args_list),1)

    def test_rejects_lookalike_host(self):
        with self.assertRaisesRegex(RuntimeError,'unsupported-provider'):
            HotmartAdapter().resolve({'url':'https://hotmart.com.evil/x','title':'Aula'})

    def test_ignores_unrelated_media_assets(self):
        self.assertEqual(media_urls({'mediaAssets':[{'url':'https://wrong/x.m3u8'}]}),[])

    def test_drm_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError,'drm-media-not-supported'):
            media_urls({'props':{'pageProps':{'applicationData':{'isDrmEnabled':True}}}})

    def test_old_player_is_not_accepted_for_new_lesson(self):
        adapter=HotmartAdapter(timeout=1)
        adapter._last_media=('/content/OLD','/embed/old')
        lesson={'url':'https://hotmart.com/content/NEW','title':'New'}
        page={'url':lesson['url'],'title':'New','ready':'complete','active':[{'path':'/content/NEW','title':'New'}],'iframes':['https://cf-embed.play.hotmart.com/embed/old?jwt=hidden']}
        with patch.object(adapter,'_eval_hotmart',return_value=page),patch.object(adapter,'_iframe_html') as fetch, patch('hotmart_cli.time.monotonic',side_effect=[0,.1,.2,2]),patch('hotmart_cli.time.sleep'):
            with self.assertRaisesRegex(RuntimeError,'hotmart-lesson-identity-timeout'):
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
        with patch('hotmart_cli.subprocess.run',side_effect=run),patch('hotmart_cli.audio._probe_duration',return_value=local):
            return HotmartAdapter(max_source_bytes=max_bytes).download(candidate,dest)

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
