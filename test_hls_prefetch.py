import subprocess,tempfile,threading,time,unittest,urllib.error,urllib.request
from pathlib import Path
from unittest.mock import patch
from hls_prefetch import PrefetchedPlaylist,copy_prefetched
from pipeline import Candidate
from embedded_adapter import EmbeddedAdapter


class HLSPrefetchTests(unittest.TestCase):
    def fixture(self,root):
        key=root/'key.bin';key.write_bytes(bytes(range(16)))
        info=root/'key-info';info.write_text('https://cdn.example/key?secret=KEY\n'+str(key)+'\n')
        result=subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=duration=3:frequency=440',
            '-c:a','aac','-b:a','64k','-f','hls','-hls_time','0.4','-hls_list_size','0',
            '-hls_key_info_file',str(info),'-hls_segment_filename',str(root/'part%03d.ts'),str(root/'source.m3u8')],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        return (root/'source.m3u8').read_text()

    def test_encrypted_audio_roundtrip_prefetches_in_parallel_and_preserves_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);text=self.fixture(root);active=0;maximum=0;lock=threading.Lock();calls=[]
            def fetch(url,referer):
                nonlocal active,maximum
                self.assertEqual(referer,'https://portal.example/lesson')
                with lock:active+=1;maximum=max(maximum,active);calls.append(url)
                try:
                    time.sleep(.025)
                    return (root/('key.bin' if '/key?' in url else url.rsplit('/',1)[1])).read_bytes()
                finally:
                    with lock:active-=1
            output=root/'out.m4a'
            with patch('media_transport.get_text',return_value=text),patch('media_transport.read_fragment',side_effect=fetch):
                result=copy_prefetched(Candidate('https://cdn.example/source.m3u8?secret=SOURCE','hls-audio'),output,10*1024**2,'https://portal.example/lesson',4)
            self.assertEqual(result,output);self.assertGreaterEqual(maximum,2);self.assertLessEqual(maximum,4)
            self.assertEqual(active,0)
            original=text.replace('https://cdn.example/key?secret=KEY',str(root/'key.bin'))
            (root/'local.m3u8').write_text(original)
            def packets(path):
                return subprocess.run(['ffmpeg','-nostdin','-v','error','-allowed_extensions','ALL','-i',str(path),
                    '-map','0:a:0','-c:a','copy','-f','adts','pipe:1'],check=True,capture_output=True).stdout
            # Input-only allowed_extensions is meaningful for HLS, not an MP4 demuxer.
            before=packets(root/'local.m3u8')
            after=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(output),'-map','0:a:0','-c:a','copy','-f','adts','pipe:1'],check=True,capture_output=True).stdout
            self.assertEqual(before,after)

    def test_proxy_routes_are_identity_bound_and_do_not_publish_remote_urls(self):
        text='#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttps://cdn.example/secret.ts?token=PRIVATE\n#EXT-X-ENDLIST'
        owner=PrefetchedPlaylist(text,'https://cdn.example/list.m3u8','https://portal.example',2,fetch=lambda *_:b'fragment')
        with owner:
            with urllib.request.urlopen(owner.url) as response:body=response.read().decode()
            self.assertNotIn('PRIVATE',body);self.assertNotIn('cdn.example',body)
            for url in [owner.url.replace(owner.token,'wrong-token'),owner.url.rsplit('/',1)[0]+'/segment/999.ts',owner.url+'?other=true']:
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(url)
                self.assertEqual(error.exception.code,404);error.exception.close()
        self.assertFalse(owner.thread.is_alive());self.assertEqual(owner.futures,{})

    def test_failed_fetch_is_bounded_sanitized_and_partial_output_survives(self):
        text='#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\none.ts\n#EXT-X-ENDLIST'
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'partial.m4a';output.write_bytes(b'pending')
            def failed(*_):raise OSError('PRIVATE signed address')
            with patch('media_transport.get_text',return_value=text),patch('media_transport.read_fragment',side_effect=failed):
                with self.assertRaisesRegex(RuntimeError,'^hls-audio-download-failed$'):
                    copy_prefetched(Candidate('https://cdn.example/list.m3u8','hls-audio'),output,100000,'https://portal.example',2)
            self.assertTrue(output.exists())
            self.assertFalse(any(t.name.startswith('hls-prefetch') for t in threading.enumerate()))

    def test_cancellation_closes_proxy_and_preserves_pending_output(self):
        text='#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\none.ts\n#EXT-X-ENDLIST'
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'pending.m4a';output.write_bytes(b'pending')
            with patch('media_transport.get_text',return_value=text),patch('media_transport.read_fragment',return_value=b'fragment'),patch('hls_prefetch.subprocess.run',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):copy_prefetched(Candidate('https://cdn.example/list.m3u8','hls-audio'),output,100000,'https://portal.example',2)
            self.assertEqual(output.read_bytes(),b'pending')
            self.assertFalse(any(t.name.startswith('hls-prefetch') for t in threading.enumerate()))

    def test_non_identity_encryption_and_unsafe_segment_addresses_are_not_prefetched(self):
        for line in ['#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"',
                     '#EXT-X-KEY:METHOD=AES-128,KEYFORMAT="com.example.drm",URI="key"',
                     '#EXT-X-KEY:METHOD=AES-128,URI="http://cdn.example/key"']:
            with self.subTest(line=line),self.assertRaises(ValueError):
                PrefetchedPlaylist('#EXTM3U\n'+line+'\n#EXTINF:1,\none.ts\n#EXT-X-ENDLIST','https://cdn.example/list.m3u8','',2)
        with self.assertRaisesRegex(ValueError,'unsafe-prefetch-segment'):
            PrefetchedPlaylist('#EXTM3U\n#EXTINF:1,\nfile:///private/segment.ts\n#EXT-X-ENDLIST','https://cdn.example/list.m3u8','',2)

    def test_unsupported_layout_retains_existing_transport_and_invalid_width_rejects(self):
        unsupported='#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\none.m4s\n#EXT-X-ENDLIST'
        with patch('media_transport.get_text',return_value=unsupported):
            self.assertIsNone(copy_prefetched(Candidate('https://cdn.example/list.m3u8','hls-audio'),Path('unused'),100000,'https://portal.example',2))
        for width in [True,5,-1]:
            with self.subTest(width=width),self.assertRaisesRegex(ValueError,'invalid-hls-prefetch-width'):
                PrefetchedPlaylist(unsupported,'https://cdn.example/list.m3u8','',width)

    def test_adapter_opt_in_reaches_transport_with_same_candidate_and_referer(self):
        adapter=EmbeddedAdapter(profile={'hls_prefetch_fragments':4});candidate=Candidate('https://cdn.example/list.m3u8','hls-audio')
        adapter._meta[candidate.url]={'referer':'https://player.example/embed/id'};destination=Path('pending.m4a')
        with patch('hls_prefetch.copy_prefetched',return_value=destination) as copy:
            self.assertEqual(adapter.download(candidate,destination),destination)
        copy.assert_called_once_with(candidate,destination,adapter.max_source_bytes,'https://player.example/embed/id',4)
