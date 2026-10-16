import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from template_adapter import choose_variant
from course_catalog import load_lessons
from course_cli import main
from test_profiles import ProfiledTemplateAdapter as TemplateAdapter,player_identity,TEMPLATE_PROFILE
from pipeline import Pipeline

PLAYER = 'https://player-vz-demo1234-001.tv.media.example.test/embed/?v=12345678-1234-1234-1234-123456789abc&color=red'
URL = 'https://lessons.example.test/area/conteudo/aula/1001'
TITLE = '1. Introdução aos estudos'


class TemplateTests(unittest.TestCase):
    def catalog(self, root):
        p = root / 'catalog.json'
        d = dict(schema='course-catalog/v1',product_id=101, title='Example Formation', complete=True,
                 responsive_menus_match=True, expected_count=1, collected_count=1,
                 modules=[dict(title='Course introduction', expected_count=1,
                               lessons=[dict(title=TITLE, url=URL)])])
        d['modules'][0]['lessons'][0].update(page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001})
        p.write_text(json.dumps(d))
        (root/'profile.json').write_text(json.dumps(TEMPLATE_PROFILE))
        return p, d

    def test_catalog_literal_paths_and_rejects_count_and_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            p, data = self.catalog(Path(td))
            self.assertEqual(load_lessons(p)[0]['site_path'],
                             ['Example Formation', 'Course introduction', TITLE])
            data['modules'][0]['lessons'] *= 2
            data['modules'][0]['expected_count'] = 2
            p.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'duplicate-catalogue'):
                load_lessons(p)
            data['modules'][0]['lessons'].pop()
            p.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'count-mismatch'):
                load_lessons(p)

    def test_player_rejects_other_host_and_protected_parameters(self):
        self.assertEqual(dict(player_identity(PLAYER))['library'], 'vz-demo1234-001')
        for value in [PLAYER.replace('https:', 'http:'), PLAYER+'&drm_group_id=private',
                      PLAYER.replace('media.example.test', 'example.test')]:
            with self.assertRaises(RuntimeError): player_identity(value)

    def test_lowest_variant_and_reject_encryption_or_foreign_host(self):
        manifest = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000\n720/video.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n360/video.m3u8'
        self.assertEqual(choose_variant(manifest, 'https://example.test/a/playlist.m3u8'),
                         'https://example.test/a/360/video.m3u8')
        for h in [manifest+'\n#EXT-X-KEY:METHOD=AES-128', manifest.replace('360/video.m3u8', 'https://other.test/a')]:
            with self.assertRaises(RuntimeError): choose_variant(h, 'https://example.test/a/playlist.m3u8')

    def page(self):
        return dict(url=URL, ready='complete', title='Introdução aos estudos',
                    identities={'collection':101,'lesson':1001}, frames=[PLAYER], titles=[TITLE, TITLE])

    def test_resolver_requires_stable_lesson_and_returns_bound_cover(self):
        adapter = TemplateAdapter()
        manifest = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n360/video.m3u8'
        with patch.object(adapter, 'page', return_value=self.page()), \
             patch.object(adapter, 'fetch', side_effect=['{}', manifest, '#EXTM3U\n#EXT-X-ENDLIST']), \
             patch('template_adapter.time.sleep'):
            candidate, = adapter.resolve(dict(url=URL, title=TITLE, page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001}))
        self.assertIn('/360/video.m3u8', candidate.url)
        self.assertEqual(candidate.thumbnail_url,
                         'https://cdn.media.example.test/vz-demo1234-001/12345678-1234-1234-1234-123456789abc/thumbnail.jpg')
        self.assertEqual(adapter._meta[candidate.url]['referer'], PLAYER)

    def test_navigation_never_evaluates_javascript_during_url_assignment(self):
        adapter = TemplateAdapter()
        with patch.object(adapter, '_osa', return_value='navigation-requested') as osa:
            self.assertEqual(adapter.page(URL), {})
            script = osa.call_args.args[0]
            self.assertIn('set URL of t to', script)
            self.assertNotIn('do JavaScript', script)
        with patch.object(adapter, '_osa', return_value=json.dumps(self.page())) as osa:
            self.assertEqual(adapter.page(), self.page())
            self.assertIn('do JavaScript', osa.call_args.args[0])
            self.assertNotIn('set URL of t to', osa.call_args.args[0])

    def test_transient_navigation_and_read_timeouts_require_fresh_stability(self):
        adapter = TemplateAdapter()
        error = subprocess.TimeoutExpired('osascript', 15)
        pages = [error, self.page(), self.page(), error, self.page(), self.page(), self.page()]
        manifest = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n360/video.m3u8'
        with patch.object(adapter, 'page', side_effect=pages) as page, \
             patch.object(adapter, 'fetch', side_effect=['{}', manifest, '#EXTM3U\n#EXT-X-ENDLIST']), patch('template_adapter.time.sleep'):
            candidate, = adapter.resolve(dict(url=URL, title=TITLE, page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001}))
        self.assertEqual(page.call_count, 7)
        self.assertIn('/360/video.m3u8', candidate.url)

    def test_expired_session_and_persistent_timeout_never_fetch_media(self):
        for failure in [dict(url='https://lessons.example.test/auth/login'),
                        subprocess.TimeoutExpired('osascript', 15)]:
            adapter = TemplateAdapter(timeout=.002)
            with patch.object(adapter, 'page', side_effect=[{}, *([failure]*10000)]) as page, \
                 patch.object(adapter, 'fetch') as fetch, patch('template_adapter.time.sleep'):
                with self.assertRaisesRegex(RuntimeError, 'login-required|identity-timeout'):
                    adapter.resolve(dict(url=URL, title=TITLE, page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001}))
                fetch.assert_not_called()

    def test_safari_observation_is_bounded_and_keyboard_interrupt_propagates(self):
        with patch('browser_adapter.subprocess.run', side_effect=subprocess.TimeoutExpired('osascript', 15)) as run:
            with self.assertRaises(subprocess.TimeoutExpired): TemplateAdapter._osa('read')
            self.assertEqual(run.call_args.kwargs['timeout'], 15)
        adapter = TemplateAdapter()
        with patch.object(adapter, 'page', side_effect=KeyboardInterrupt), patch.object(adapter, 'fetch') as fetch:
            with self.assertRaises(KeyboardInterrupt): adapter.resolve(dict(url=URL, title=TITLE, page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001}))
            fetch.assert_not_called()

    def test_wrong_product_lesson_or_stale_video_never_fetches_media(self):
        for changed in [dict(identities={'collection':99,'lesson':1001}), dict(identities={'collection':101,'lesson':1}), dict(title='Outra aula'), dict(titles=['Outra'])]:
            adapter = TemplateAdapter(timeout=.002)
            page = dict(self.page(), **changed)
            with patch.object(adapter, 'page', return_value=page), patch.object(adapter, 'fetch') as fetch, patch('template_adapter.time.sleep'):
                with self.assertRaisesRegex(RuntimeError, 'identity-timeout'):
                    adapter.resolve(dict(url=URL, title=TITLE, page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001}))
                fetch.assert_not_called()
        adapter = TemplateAdapter(timeout=.002)
        adapter._last_media = ('/other/99', player_identity(PLAYER))
        with patch.object(adapter, 'page', return_value=self.page()), patch.object(adapter, 'fetch') as fetch, patch('template_adapter.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'identity-timeout'):
                adapter.resolve(dict(url=URL, title=TITLE, page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001}))
            fetch.assert_not_called()

    def test_protected_or_live_playlist_rejected_before_download(self):
        for playlist in ['#EXTM3U\n#EXT-X-KEY:METHOD=AES-128\n#EXT-X-ENDLIST', '#EXTM3U\n#EXTINF:10\na.ts']:
            adapter = TemplateAdapter()
            with patch.object(adapter, 'page', return_value=self.page()), \
                 patch.object(adapter, 'fetch', side_effect=['{}', '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n360/video.m3u8', playlist]), patch('template_adapter.time.sleep'):
                with self.assertRaisesRegex(RuntimeError, 'protected-or-unbounded'):
                    adapter.resolve(dict(url=URL, title=TITLE, page_title='Introdução aos estudos',page_identity={'collection':101,'lesson':1001}))

    def test_cli_exact_destination_success_failure_and_cancel_status(self):
        for failure, expected in [(None, 0), (RuntimeError('secret URL'), 2), (KeyboardInterrupt(), 130)]:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td); cat, _ = self.catalog(root)
                args = ['--profile', str(root/'profile.json'), '--catalog', str(cat), '--root', str(root/'work'), '--destination', str(root/'cloud'), '--icloud-python', 'python']
                with patch('course_cli.Delivery') as cls, patch('course_cli.NativeCloud'), patch('builtins.print'):
                    cls.return_value.completed_count.return_value = 0
                    cls.return_value.run.side_effect = failure
                    self.assertEqual(main(args), expected)
                    work = cls.return_value.run.call_args.args[0]
                    self.assertEqual(work[0][1], root/'cloud'/'Example Formation'/'Course introduction'/(TITLE+'.m4a'))
                    state = json.loads((root/'work/Documentacao/delivery-status.json').read_text())
                    self.assertEqual(state['stage'], {0:'finished',2:'failed',130:'interrupted'}[expected])
                    self.assertNotIn('secret URL', json.dumps(state))
                    if expected == 130: cls.return_value.cancel.assert_called_once()

    def test_cli_returns_monitor_to_running_after_cover_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); cat, _ = self.catalog(root)
            args = ['--profile', str(root/'profile.json'), '--catalog', str(cat), '--root', str(root/'work'), '--destination', str(root/'cloud'), '--icloud-python', 'python', '--refresh-completed-artwork']
            with patch('course_cli.Delivery') as cls, patch('course_cli.NativeCloud'), patch('builtins.print'):
                cls.return_value.completed_count.return_value = 1
                cls.return_value.journal = {Pipeline.identity(URL): dict(state='complete', artwork={'origin':'none'})}
                def refresh(*args): cls.call_args.kwargs['event']('wait-for-upload')
                cls.return_value.refresh_completed_artwork.side_effect = refresh
                def run(work, **kwargs):
                    state = json.loads((root/'work/Documentacao/delivery-status.json').read_text())
                    self.assertEqual(state['stage'], 'running')
                cls.return_value.run.side_effect = run
                self.assertEqual(main(args), 0)
                cls.return_value.refresh_completed_artwork.assert_called_once()
