import contextlib
import http.server
import os
import socketserver
import tempfile
import threading
import unittest
import wave
from pathlib import Path

import course_audio_bot as bot


class TestHelpers(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(bot.slugify('Aula 01: Fé & Razão'), 'aula-01-fé-razão')

    def test_media_candidates_absolute_and_relative(self):
        html = '''<html><head><title>Aula X</title></head>
        <body><audio src="/media/aula 1.mp3"></audio>
        <script>{"url":"https://cdn.example/aula2.m4a?token=abc"}</script></body></html>'''
        urls = bot.media_candidates(html, 'https://course.example/aulas/1')
        self.assertIn('https://course.example/media/aula 1.mp3', urls)
        self.assertIn('https://cdn.example/aula2.m4a?token=abc', urls)

    def test_signed_audio_query_candidate(self):
        signed = 'https://files.example/object?id=abc&response-content-type=audio%2Fmpeg&response-content-disposition=attachment%3B%20filename%3Dlesson.mp3'
        html = f'<a href="{signed}">audio</a>'
        self.assertIn(signed, bot.media_candidates(html, 'https://course.example/aulas/1'))
        self.assertEqual(bot.infer_suffix_from_url(signed), '.mp3')

    def test_generic_embed_is_preserved_without_inventing_media_url(self):
        frame = 'https://player.example.test/embed/video-1'
        urls = bot.media_candidates(f'<iframe src="{frame}"></iframe>', 'https://course.example/lesson/1')
        self.assertEqual(urls, [frame])

    def test_page_links_same_site_course_paths_only(self):
        html = '''
        <a href="/aulas/aula-1">ok</a>
        <a href="/cursos/curso-x">ok</a>
        <a href="/account/logout">skip</a>
        <a href="https://evil.example/aulas/x">skip</a>
        '''
        links = bot.page_links(html, 'https://course.example/cursos/main')
        self.assertEqual(links, ['https://course.example/aulas/aula-1', 'https://course.example/cursos/curso-x'])

    def test_drm_hint(self):
        self.assertTrue(bot.looks_drm('<script>widevine licenseUrl</script>'))


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@contextlib.contextmanager
def local_server(root: Path):
    old = os.getcwd()
    os.chdir(root)
    srv = socketserver.TCPServer(('127.0.0.1', 0), QuietHandler)
    try:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield f'http://127.0.0.1:{srv.server_address[1]}'
    finally:
        srv.shutdown()
        srv.server_close()
        os.chdir(old)


def write_wav(path: Path, seconds: int = 1):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b'\x00\x00' * 22050 * seconds)


class TestDownload(unittest.TestCase):
    def test_direct_download_defaults_to_m4a(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_wav(root / 'lesson.wav')
            (root / 'page.html').write_text('<title>Aula Teste</title><audio src="lesson.wav"></audio>')
            out = root / 'out'
            with local_server(root) as base:
                course = bot.Course('test-provider', 'Local Course', None, [base + '/page.html'])
                rec = bot.process_url(course, out, base + '/page.html', 1, prefer_ytdlp=False)
            self.assertEqual(rec['status'], 'ok', rec)
            files = list((out / 'test-provider' / 'local-course').glob('*.m4a'))
            self.assertEqual(len(files), 1)
            self.assertGreater(files[0].stat().st_size, 0)
            self.assertFalse(list((out / 'test-provider' / 'local-course').glob('*.mp3')))

    def test_mp3_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_wav(root / 'lesson.wav')
            (root / 'page.html').write_text('<title>Aula Teste</title><audio src="lesson.wav"></audio>')
            out = root / 'out'
            with local_server(root) as base:
                course = bot.Course('test-provider', 'Local Course', None, [base + '/page.html'])
                rec = bot.process_url(course, out, base + '/page.html', 1, prefer_ytdlp=False, output_format='mp3')
            self.assertEqual(rec['status'], 'ok', rec)
            files = list((out / 'test-provider' / 'local-course').glob('*.mp3'))
            self.assertEqual(len(files), 1)
            self.assertFalse(list((out / 'test-provider' / 'local-course').glob('*.m4a')))

    def test_invalid_existing_m4a_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / 'out' / 'test-provider' / 'local-course'
            out_dir.mkdir(parents=True)
            existing = out_dir / '001-aula-teste.m4a'
            existing.write_bytes(b'existing')
            ok, detail = bot.direct_download('https://files.example/lesson.wav', out_dir, '001-aula-teste', None)
            self.assertFalse(ok)
            self.assertNotIn('já existe verificado', detail)


class TestRegressionMedia(unittest.TestCase):
    def variable_bitrate_fixture(self, root):
        parts = []
        for index, bitrate in enumerate(['64k', '192k']):
            part = root / f'part-{index}.mp3'
            rc, detail = bot.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                 'sine=frequency=440:duration=2', '-c:a', 'libmp3lame',
                                 '-b:a', bitrate, '-write_xing', '0', '-id3v2_version', '0', str(part)])
            self.assertEqual(rc, 0, detail)
            parts.append(part.read_bytes())
        source, output = root / 'source.mp3', root / 'output.m4a'
        source.write_bytes(b''.join(parts))
        rc, detail = bot.run(['ffmpeg', '-v', 'error', '-i', str(source), '-ac', '1',
                             '-ar', '44100', '-c:a', 'aac', '-b:a', '32k', str(output)])
        self.assertEqual(rc, 0, detail)
        return source, output

    def test_estimated_mp3_duration_uses_decoded_audio_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = self.variable_bitrate_fixture(Path(td))
            self.assertGreater(abs(bot._probe_duration(source) - bot._probe_duration(output)), 2)
            valid, detail = bot.verify_audio_output(output, source)
            self.assertTrue(valid, detail)

    def test_estimated_duration_does_not_accept_truncated_audio(self):
        with tempfile.TemporaryDirectory() as td:
            source, _ = self.variable_bitrate_fixture(Path(td))
            truncated = Path(td) / 'truncated.m4a'
            rc, detail = bot.run(['ffmpeg', '-v', 'error', '-i', str(source), '-t', '1',
                                 '-ac', '1', '-ar', '44100', '-c:a', 'aac', '-b:a', '32k', str(truncated)])
            self.assertEqual(rc, 0, detail)
            self.assertFalse(bot.verify_audio_output(truncated, source)[0])

    def test_unavailable_decoded_duration_keeps_validation_closed(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            source, output = self.variable_bitrate_fixture(Path(td))
            original_run = bot.run
            for failed_path in [source, output]:
                with self.subTest(path=failed_path.name):
                    def run(cmd, timeout=600):
                        if 'stream=sample_rate:frame=nb_samples' in cmd and cmd[-1] == str(failed_path):
                            return 1, 'decode failed'
                        return original_run(cmd, timeout)
                    with patch('course_audio_bot.run', side_effect=run):
                        self.assertFalse(bot.verify_audio_output(output, source)[0])

    def test_video_source_is_converted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); wav = root / "source.wav"; write_wav(wav)
            video = root / "source.mp4"
            rc, _ = bot.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:s=16x16:d=1", "-i", str(wav), "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(video)])
            self.assertEqual(rc, 0)
            converted, detail = bot.compress_audio_file(video)
            self.assertIsNotNone(converted, detail)
            self.assertTrue(bot.verify_audio_output(converted)[0])

    def test_duration_tolerance_rejects_large_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); a = root / "a.wav"; b = root / "b.wav"
            write_wav(a, 1); write_wav(b, 2)
            converted, detail = bot.compress_audio_file(a)
            self.assertIsNotNone(converted, detail)
            self.assertFalse(bot.verify_audio_output(converted, b)[0])


class TestRepositoryHygiene(unittest.TestCase):
    def test_gitignore_blocks_private_patterns(self):
        gitignore = Path('.gitignore').read_text()
        for pattern in ['config.*.toml', '*.cookies.txt', 'logs/', '*.mp3', '*.m4a', 'apple_complete_pipeline.py']:
            self.assertIn(pattern, gitignore)

    def test_public_examples_are_generic(self):
        public_text = Path('README.md').read_text() + '\n' + Path('config.example.toml').read_text()
        forbidden_patterns = [
            r'192\.168\.',
            r'gho_[A-Za-z0-9_]+',
            r'cookies_file\s*=\s*"[^"]*(real|private|personal|prod)[^"]*"',
        ]
        for pattern in forbidden_patterns:
            self.assertIsNone(__import__('re').search(pattern, public_text.lower()), pattern)


if __name__ == '__main__':
    unittest.main()
