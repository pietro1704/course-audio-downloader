import io
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

import transcription as t

FAKE_ENGINE = textwrap.dedent('''
    import sys, wave
    args = sys.argv[1:]
    with wave.open(args[args.index('-f') + 1]) as audio:
        seconds = audio.getnframes() / audio.getframerate()
    log = open(sys.argv[0] + '.calls', 'a')
    log.write(f'{seconds:.1f}\\n')
    tokens = [('\\u2581Olá', True), (',', False), ('\\u2581', True), ('1', False), ('0', False),
              ('%', False), ('\\u2581mund', True), ('o', False), ('.', False)]
    print('Olá, 10% mundo.')
    print('Segments (1):', file=sys.stderr)
    for i, (tok, start) in enumerate(tokens):
        t0 = i * 50
        print(f'  [{i:2d}] id=  1 frame=  1 dur_idx= 1 dur_val= 1 p=1.0 plog=0.0 t0={t0:4d} t1={t0 + 8:4d} '
              f'word_start={"true" if start else "false"} "{tok}"', file=sys.stderr)
''')


class PureFunctionTests(unittest.TestCase):
    def test_detect_silences_pairs_start_and_end(self):
        log = ('[silencedetect @ 0x1] silence_start: -0.01\n'
               '[silencedetect @ 0x1] silence_end: 1.5 | silence_duration: 1.5\n'
               '[silencedetect @ 0x1] silence_start: 10.25\n'
               '[silencedetect @ 0x1] silence_end: 11 | silence_duration: 0.75\n'
               '[silencedetect @ 0x1] silence_start: 20\n')
        self.assertEqual(t.detect_silences(log), [(0.0, 1.5), (10.25, 11.0)])

    def test_cuts_prefer_pause_nearest_target(self):
        cuts = t.choose_cuts(700, [(200, 201), (250, 252), (470, 480)], target=240, slack=60)
        self.assertEqual(cuts, [0.0, 251.0, 475.0, 700.0])

    def test_cuts_fall_back_to_target_without_pause(self):
        self.assertEqual(t.choose_cuts(650, [], target=240, slack=60), [0.0, 240.0, 480.0, 650.0])
        self.assertEqual(t.choose_cuts(290, [(100, 101)], target=240, slack=60), [0.0, 290.0])
        with self.assertRaises(ValueError):
            t.choose_cuts(0, [])

    def test_tokens_rebuild_words_including_detached_word_start(self):
        stderr = '\n'.join(
            f'  [{i}] id= 1 frame=1 dur_idx= 1 dur_val= 1 p=1 plog=0 t0={t0} t1={t0} word_start={s} "{tok}"'
            for i, (t0, s, tok) in enumerate([(100, 'true', '▁forma'), (150, 'true', '▁'), (160, 'false', '1'),
                                              (170, 'false', '0'), (180, 'false', '0'), (190, 'false', '%'),
                                              (250, 'true', '▁"ok"'), (260, 'false', '.')]))
        words = t.words_from_tokens(t.parse_tokens(stderr), offset=60)
        self.assertEqual(words, [(61.0, 'forma'), (61.6, '100%'), (62.5, '"ok".')])

    def test_paragraphs_break_after_sentence_once_soft_limit_passes(self):
        words = [(0, 'Um.'), (10, 'dois'), (50, 'três.'), (55, 'quatro'), (200, 'cinco'), (201, 'seis')]
        self.assertEqual(t.paragraphs(words, soft=45, hard=90),
                         [(0, 'Um. dois três.'), (55, 'quatro cinco'), (201, 'seis')])

    def test_markdown_has_front_matter_and_timestamps(self):
        text = t.render_markdown({'title': 'Aula "1"', 'course': 'Curso', 'source': 'a.m4a', 'duration': '00:01:00',
                                  'support': 'a.pdf', 'engine': 'Engine', 'transcribed': '2026-11-24'},
                                 [(0, 'Olá.'), (3725, 'Fim.')])
        self.assertTrue(text.startswith('---\ntitle: "Aula \\"1\\""\n'))
        self.assertIn('**[00:00:00]** Olá.', text)
        self.assertIn('**[01:02:05]** Fim.', text)
        self.assertIn('Material de apoio: `a.pdf`', text)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.course = self.root / 'Curso X' / 'Módulo'
        self.course.mkdir(parents=True)
        (self.root / 'Curso X' / '_backup').mkdir()
        tone = 'sine=frequency=440:duration=4'
        quiet = 'anullsrc=r=16000:cl=mono'
        self.ffmpeg('-f', 'lavfi', '-i', tone, '-f', 'lavfi', '-t', '2', '-i', quiet, '-f', 'lavfi', '-i', tone,
                    '-filter_complex', '[0:a]aformat=sample_rates=16000:channel_layouts=mono[a];'
                    '[2:a]aformat=sample_rates=16000:channel_layouts=mono[c];[a][1:a][c]concat=n=3:v=0:a=1',
                    str(self.course / '01 - Aula.m4a'))
        (self.course / '01 - Aula.pdf').write_bytes(b'%PDF')
        self.ffmpeg('-f', 'lavfi', '-i', tone, str(self.root / 'Curso X' / '_backup' / 'old.m4a'))
        self.engine = self.root / 'engine.py'
        self.engine.write_text(FAKE_ENGINE)
        self.model = self.root / 'model.bin'
        self.model.write_bytes(b'model')

    def ffmpeg(self, *args):
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *args], check=True, capture_output=True)

    def transcriber(self):
        return t.Transcriber([sys.executable, self.engine], self.model, target=5, log=lambda *_: None)

    def test_course_cli_writes_lesson_and_index_then_skips(self):
        out = self.root / 'out'
        argv = [str(self.root / 'Curso X'), '--output', str(out), '--engine', sys.executable, '--model', str(self.model), '--policy', 'none']
        with redirect_stdout(io.StringIO()):
            duration, count = self.transcriber().transcribe(self.course / '01 - Aula.m4a',
                                                            out / 'Módulo' / '01 - Aula.md', course='Curso X',
                                                            support='01 - Aula.pdf')
        lesson = (out / 'Módulo' / '01 - Aula.md').read_text(encoding='utf-8')
        self.assertAlmostEqual(duration, 10, delta=0.2)
        calls = [float(x) for x in Path(str(self.engine) + '.calls').read_text().split()]
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(sum(calls), 10, delta=0.3)
        self.assertEqual(count, 6)
        self.assertIn('**[00:00:00]** Olá, 10% mundo.', lesson)
        self.assertIn('Material de apoio: `01 - Aula.pdf`', lesson)
        self.assertFalse((out / t.WORK_DIR / 'Módulo').exists() and any((out / t.WORK_DIR).rglob('*.json')))
        self.assertEqual([p.name for p in t.lessons(self.root / 'Curso X')], ['01 - Aula.m4a'])
        stdout = io.StringIO()
        with redirect_stdout(stdout), unittest.mock.patch.object(t.Transcriber, 'check', lambda self: None):
            self.assertEqual(t.main(argv), 0)
        self.assertIn('já transcrita', stdout.getvalue())
        index = (out / 'README.md').read_text(encoding='utf-8')
        self.assertIn('(M%C3%B3dulo/01%20-%20Aula.md)', index)
        with self.assertRaises(SystemExit), unittest.mock.patch.object(t.Transcriber, 'check', lambda self: None):
            t.main([*argv, '--select', '02*'])

    def test_resume_reuses_completed_chunks(self):
        out = self.root / 'out' / 'a.md'
        work = out.parent / t.WORK_DIR / 'a'
        transcriber = self.transcriber()
        original = transcriber.chunk_words
        calls, failed = [], []

        def flaky(audio, start, end, workdir):
            calls.append(start)
            if start > 0 and not failed:
                failed.append(start)
                raise RuntimeError('interrupted')
            return original(audio, start, end, workdir)

        transcriber.chunk_words = flaky
        with self.assertRaises(RuntimeError):
            transcriber.transcribe(self.course / '01 - Aula.m4a', out)
        self.assertTrue((work / '0000.json').is_file())
        calls.clear()
        transcriber.transcribe(self.course / '01 - Aula.m4a', out)
        self.assertEqual(calls, failed)
        self.assertTrue(out.is_file())
        self.assertFalse(work.exists())


if __name__ == '__main__':
    unittest.main()
