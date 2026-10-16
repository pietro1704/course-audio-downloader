import unittest
from unittest.mock import patch
from pipeline import Candidate
from browser_adapter import SafariAdapter
from media_transport import select_rendition
from test_profiles import resolve_player,HTML_PLAYER

FRAME='https://player.example.test/embed/12/lesson-uuid'
MASTER='https://media.example/lesson-uuid/playlist.m3u8?signature=PRIVATE'
HTML='<media-player library-id="12" video-id="lesson-uuid" drm-version="0" content-src="'+MASTER+'"></media-player>'
MUX='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,CODECS="avc1.4d401e,mp4a.40.2"\n720p.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200000,CODECS="avc1.4d4015,mp4a.40.2"\n240p.m3u8\n'
class MediaPlayerTests(unittest.TestCase):
    def test_mp3_precedes_player(self):
        page={'links':[{'url':'https://cdn.example/opaque','label':'Áudio da aula (mp3)'}],'frames':[FRAME]}
        self.assertEqual(SafariAdapter.candidates(page),[Candidate('https://cdn.example/opaque','mp3')])
    def test_player_only_when_no_audio_control(self):
        self.assertEqual(SafariAdapter.candidates({'frames':[FRAME]}, {'player':HTML_PLAYER}),[Candidate(FRAME,'player-embed')])
        self.assertEqual(SafariAdapter.candidates({'frames':['https://player.example.test.evil/embed/12/abc']}, {'player':HTML_PLAYER}),[])
    def test_dedicated_audio_precedes_video(self):
        text=MUX+'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",DEFAULT=YES,URI="audio.m3u8?token=PRIVATE"\n'
        self.assertEqual(select_rendition(MASTER,text),'https://media.example/lesson-uuid/audio.m3u8?token=PRIVATE')
    def test_audio_only_variant_precedes_muxed(self):
        text=MUX+'#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"\naudio.m3u8\n'
        self.assertEqual(select_rendition(MASTER,text),'https://media.example/lesson-uuid/audio.m3u8')
    def test_highest_audio_only_bitrate(self):
        text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\n64.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"\n128.m3u8'
        self.assertTrue(select_rendition(MASTER,text).endswith('/128.m3u8'))
    def test_muxed_fallback_preserves_highest_source_quality(self):
        self.assertEqual(select_rendition(MASTER,MUX),'https://media.example/lesson-uuid/720p.m3u8')
    def test_direct_vod_playlist(self):
        text='#EXTM3U\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST'
        self.assertEqual(select_rendition(MASTER,text),MASTER)
    def test_missing_and_unsafe_media_rejected(self):
        for text in ['not a playlist','#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2,CODECS="mp4a.40.2"\nhttp://bad.example/a.m3u8']:
            with self.assertRaises(RuntimeError):select_rendition(MASTER,text)
    def test_video_only_variant_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'missing-player-vod-audio'):
            select_rendition(MASTER,'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=20000,CODECS="avc1.4d4015"\nvideo.m3u8')
    def test_html_entity_player_parse_and_identity(self):
        with patch('media_transport.get_text',side_effect=[HTML.replace('&','&amp;'),MUX]):
            c=resolve_player(FRAME,'https://lessons.example.test/aulas/test')
        self.assertEqual(c.kind,'hls-audio')
        self.assertTrue(c.url.endswith('/720p.m3u8'))
    def test_wrong_video_and_drm_are_rejected(self):
        for html in [HTML.replace('video-id="lesson-uuid"','video-id="wrong"'),HTML.replace('drm-version="0"','drm-version="1"')]:
            with patch('media_transport.get_text',return_value=html):
                with self.assertRaises(RuntimeError):resolve_player(FRAME,'https://lessons.example.test/aulas/test')
    def test_frame_allowlist(self):
        with self.assertRaisesRegex(RuntimeError,'unsupported-player-frame'):
            resolve_player('https://evil.example/embed/12/lesson-uuid','https://lessons.example.test/')

if __name__=='__main__':unittest.main()
