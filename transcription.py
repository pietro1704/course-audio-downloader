"""Local lesson transcription into Markdown for search and AI reference.

Audio is split at detected pauses, each chunk is transcribed by a local
Parakeet TDT engine (whisper.cpp ``parakeet-cli``), and token timestamps are
regrouped into timestamped paragraphs. Nothing leaves the machine. Completed
chunks are kept under ``.transcription-work`` so an interrupted run resumes.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from resource_policy import NullPolicy, policy_for

AUDIO_SUFFIXES = {'.m4a', '.mp3', '.aac', '.wav', '.flac', '.ogg', '.opus', '.mp4', '.m4b'}
DATA_HOME = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'course-audio-downloader'
DEFAULT_CLI = DATA_HOME / 'bin' / ('parakeet-cli.exe' if os.name == 'nt' else 'parakeet-cli')
DEFAULT_MODEL = DATA_HOME / 'models' / 'ggml-parakeet-tdt-0.6b-v3-q8_0.bin'
MODEL_URL = 'https://huggingface.co/ggml-org/parakeet-GGUF'
WORK_DIR = '.transcription-work'
FORMAT_VERSION = 1

SILENCE = re.compile(r'silence_(start|end): (-?[\d.]+)')
TOKEN = re.compile(r'^\s*\[\s*\d+\] id=\s*\d+ .*?\bt0=\s*(\d+) t1=\s*(\d+) word_start=(true|false) "(.*)"$')
SENTENCE_END = ('.', '?', '!', '…')


def detect_silences(text):
    """Parse ffmpeg silencedetect output into (start, end) pairs."""
    pairs, start = [], None
    for kind, value in SILENCE.findall(text):
        value = max(0.0, float(value))
        if kind == 'start':
            start = value
        elif start is not None:
            pairs.append((start, value))
            start = None
    return pairs


def choose_cuts(duration, silences, target=240.0, slack=60.0):
    """Chunk boundaries near ``target`` seconds, placed inside pauses when possible."""
    if duration <= 0:
        raise ValueError('empty-audio')
    middles = sorted((a + b) / 2 for a, b in silences if b > a)
    cuts = [0.0]
    while duration - cuts[-1] > target + slack:
        low, ideal, high = cuts[-1] + target - slack, cuts[-1] + target, cuts[-1] + target + slack
        inside = [m for m in middles if low <= m <= high]
        cuts.append(round(min(inside, key=lambda m: abs(m - ideal)) if inside else ideal, 3))
    cuts.append(round(duration, 3))
    return cuts


def parse_tokens(text):
    """Return (seconds, token, word_start) tuples from ``parakeet-cli -ps`` output."""
    tokens = []
    for line in text.splitlines():
        match = TOKEN.match(line)
        if match:
            t0, _, start, token = match.groups()
            tokens.append((int(t0) / 100, token, start == 'true'))
    return tokens


def words_from_tokens(tokens, offset=0.0):
    """Join subword tokens into (seconds, word) pairs, shifted by the chunk offset."""
    words, pending = [], True
    for seconds, token, start in tokens:
        piece = token.replace('▁', ' ')
        pending = pending or start or piece.startswith(' ')
        if not piece.strip():
            continue
        if pending or not words:
            words.append([round(seconds + offset, 2), piece.strip()])
        else:
            words[-1][1] += piece.strip()
        pending = False
    return [(t, w) for t, w in words if w]


def paragraphs(words, soft=45.0, hard=90.0):
    """Group words into paragraphs, ending at sentence boundaries after ``soft`` seconds."""
    result, current = [], []
    for seconds, word in words:
        current.append((seconds, word))
        elapsed = seconds - current[0][0]
        if (elapsed >= soft and word.endswith(SENTENCE_END)) or elapsed >= hard:
            result.append((current[0][0], ' '.join(w for _, w in current)))
            current = []
    if current:
        result.append((current[0][0], ' '.join(w for _, w in current)))
    return result


def clock(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def yaml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def render_markdown(meta, blocks):
    lines = ['---']
    for key in ('title', 'course', 'source', 'duration', 'support', 'engine', 'transcribed'):
        if meta.get(key):
            lines.append(f'{key}: {yaml_string(meta[key])}')
    lines += ['review: "automatic; not reviewed"', '---', '', f'# {meta["title"]}', '']
    if meta.get('course'):
        lines.append(f'Curso: {meta["course"]}  ')
    lines.append(f'Áudio: `{meta["source"]}` ({meta["duration"]})  ')
    if meta.get('support'):
        lines.append(f'Material de apoio: `{meta["support"]}`  ')
    lines += [f'Transcrição automática ({meta["engine"]}), sem revisão humana: nomes próprios, números, '
              'siglas e tickers podem estar errados. Os tempos `[hh:mm:ss]` indicam onde ouvir o trecho.',
              '', '---', '']
    for seconds, text in blocks:
        lines += [f'**[{clock(seconds)}]** {text}', '']
    return '\n'.join(lines).rstrip() + '\n'


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def run(command, timeout):
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout, check=True)


def probe_duration(path):
    out = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)], 120).stdout
    return float(out.strip())


def source_identity(path):
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return {'name': path.name, 'bytes': stat.st_size, 'sha256': digest.hexdigest()}


class EngineKilled(RuntimeError):
    """The engine was killed by a signal, typically the cgroup memory limit."""


class Transcriber:
    def __init__(self, engine, model, threads=4, target=120.0, log=None, policy=None, poll=5.0,
                 min_split=30.0):
        self.engine = [str(part) for part in engine]
        self.model = Path(model)
        self.policy = policy or NullPolicy()
        self.threads = self.policy.threads(threads)
        self.target = target
        self.poll = poll
        self.min_split = min_split
        self.log = log or (lambda message: print(message, flush=True))

    @property
    def engine_label(self):
        return f'Parakeet TDT ({self.model.name}) via parakeet-cli'

    def check(self):
        program = self.engine[0]
        if not (Path(program).is_file() or shutil.which(program)):
            raise SystemExit(f'parakeet-cli não encontrado: {program}. Compile o exemplo parakeet-cli do '
                             'whisper.cpp e informe --engine ou COURSE_PARAKEET_CLI.')
        if not self.model.is_file():
            raise SystemExit(f'Modelo não encontrado: {self.model}. Baixe ggml-parakeet-tdt-0.6b-v3-q8_0.bin '
                             f'de {MODEL_URL} e informe --model ou COURSE_PARAKEET_MODEL.')
        for tool in ('ffmpeg', 'ffprobe'):
            if not shutil.which(tool):
                raise SystemExit(f'{tool} não encontrado no PATH.')

    def wait_for_admission(self):
        """Block until the host policy reports nothing that should take precedence."""
        announced = None
        while True:
            reasons = self.policy.blockers()
            if not reasons:
                if announced:
                    self.log('  liberado; continuando')
                return
            if reasons != announced:
                self.log('  aguardando: ' + '; '.join(reasons))
                announced = reasons
            time.sleep(self.poll)

    def supervised(self, command, timeout):
        """Run a heavy command, pausing it (SIGSTOP/SIGCONT) while the policy reports blockers.

        ``timeout`` counts only running time. Returns a CompletedProcess like ``run``.
        """
        can_pause = hasattr(signal, 'SIGSTOP')
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            # A new session lets wrappers that spawn rather than exec (taskpolicy) be paused as a group.
            process = subprocess.Popen(self.policy.wrap(command), stdout=out, stderr=err,
                                       stdin=subprocess.DEVNULL, start_new_session=can_pause)

            def signal_group(number):
                try:
                    os.killpg(process.pid, number)
                except ProcessLookupError:
                    pass

            active, paused, last = 0.0, None, time.monotonic()
            try:
                while True:
                    try:
                        process.wait(timeout=self.poll)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    now = time.monotonic()
                    if paused is None:
                        active += now - last
                    last = now
                    if active > timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    if not can_pause:
                        continue
                    reasons = self.policy.blockers()
                    if reasons and paused is None:
                        signal_group(signal.SIGSTOP)
                        paused = reasons
                        self.log('  pausado: ' + '; '.join(reasons))
                    elif reasons and reasons != paused:
                        paused = reasons
                        self.log('  ainda pausado: ' + '; '.join(reasons))
                    elif not reasons and paused is not None:
                        signal_group(signal.SIGCONT)
                        paused = None
                        last = time.monotonic()
                        self.log('  retomado')
            except BaseException:
                if can_pause:
                    signal_group(signal.SIGKILL)
                    signal_group(signal.SIGCONT)
                else:
                    process.kill()
                process.wait()
                raise
            out.seek(0)
            err.seek(0)
            stdout = out.read().decode('utf-8', 'replace')
            stderr = err.read().decode('utf-8', 'replace')
        if process.returncode < 0 or process.returncode in (137, 143):
            raise EngineKilled(f'engine killed (exit {process.returncode})')
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, 0, stdout, stderr)

    def chunk_words(self, audio, start, end, workdir):
        self.wait_for_admission()
        with tempfile.TemporaryDirectory(dir=workdir) as temp:
            wav = Path(temp) / 'chunk.wav'
            run(self.policy.wrap(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{start:.3f}',
                                  '-t', f'{end - start:.3f}', '-i', str(audio), '-ac', '1', '-ar', '16000',
                                  '-c:a', 'pcm_s16le', str(wav)]), 300)
            try:
                result = self.supervised([*self.engine, '-m', str(self.model), '-f', str(wav), '-t',
                                          str(self.threads), '-ng', '-ps', '-np'], max(600, int((end - start) * 6)))
            except EngineKilled:
                if end - start < 2 * self.min_split:
                    raise
                middle = round((start + end) / 2, 3)
                self.log(f'  motor encerrado pelo limite de memória em {clock(start)}; dividindo o bloco')
                return (self.chunk_words(audio, start, middle, workdir)
                        + self.chunk_words(audio, middle, end, workdir))
        tokens = parse_tokens(result.stderr + '\n' + result.stdout)
        if not tokens and result.stdout.strip():
            raise RuntimeError(f'engine returned text without token timestamps at {clock(start)}')
        return words_from_tokens(tokens, offset=start)

    def transcribe(self, audio, output, course=None, support=None, title=None):
        audio, output = Path(audio), Path(output)
        workdir = output.parent / WORK_DIR / output.stem
        workdir.mkdir(parents=True, exist_ok=True)
        identity = source_identity(audio)
        plan_file = workdir / 'plan.json'
        plan = json.loads(plan_file.read_text(encoding='utf-8')) if plan_file.is_file() else None
        # The chunk size only shapes new plans; an existing plan keeps its cuts so work resumes.
        expected = {'format': FORMAT_VERSION, 'source': identity, 'model': self.model.name}
        if plan and {k: plan.get(k) for k in expected} == expected:
            for stale in workdir.glob('tmp*'):
                shutil.rmtree(stale, ignore_errors=True)
        else:
            shutil.rmtree(workdir)
            workdir.mkdir(parents=True)
            duration = probe_duration(audio)
            self.wait_for_admission()
            silence = run(self.policy.wrap(['ffmpeg', '-nostdin', '-v', 'info', '-i', str(audio), '-af',
                                            'silencedetect=noise=-35dB:d=0.35', '-f', 'null', '-']), 1800).stderr
            plan = {**expected, 'target': self.target, 'duration': duration,
                    'cuts': choose_cuts(duration, detect_silences(silence), self.target, self.target / 4)}
            write_atomic(plan_file, json.dumps(plan, ensure_ascii=False, indent=1))
        cuts, words = plan['cuts'], []
        started = time.monotonic()
        for index, (start, end) in enumerate(zip(cuts, cuts[1:])):
            part = workdir / f'{index:04d}.json'
            if part.is_file():
                chunk = json.loads(part.read_text(encoding='utf-8'))
            else:
                chunk = self.chunk_words(audio, start, end, workdir)
                write_atomic(part, json.dumps(chunk, ensure_ascii=False))
                self.log(f'  {output.stem}: {clock(end)}/{clock(plan["duration"])} '
                         f'({time.monotonic() - started:.0f} s)')
            words.extend(tuple(w) for w in chunk)
        if not words:
            raise RuntimeError(f'nenhuma fala reconhecida em {audio.name}')
        meta = {'title': title or audio.stem, 'course': course, 'source': audio.name,
                'duration': clock(plan['duration']), 'support': support, 'engine': self.engine_label,
                'transcribed': datetime.date.today().isoformat()}
        write_atomic(output, render_markdown(meta, paragraphs(words)))
        shutil.rmtree(workdir)
        return plan['duration'], len(words)


def lessons(source):
    return sorted(p for p in Path(source).rglob('*')
                  if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file()
                  and not any(part.startswith(('.', '_')) for part in p.relative_to(source).parts))


def render_index(course, rows):
    lines = [f'# {course} — transcrições', '',
             'Transcrições automáticas locais, uma por aula, com marcações `[hh:mm:ss]`. '
             'Não revisadas: confira nomes, números e tickers no áudio ou no material de apoio.', '',
             '| Aula | Duração | Palavras |', '| --- | ---: | ---: |']
    for relative, duration, count in rows:
        lines.append(f"| [{Path(relative).stem}]({quote(relative)}) | {clock(duration)} | {count} |")
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='course-transcribe', description=(
        'Transcreve localmente as aulas em áudio de uma pasta de curso para Markdown com tempos.'))
    parser.add_argument('source', type=Path, help='pasta do curso (procura áudios recursivamente)')
    parser.add_argument('--output', type=Path, help='pasta de saída (padrão: <source>/Transcrições)')
    parser.add_argument('--course', help='nome do curso no cabeçalho (padrão: nome da pasta)')
    parser.add_argument('--engine', default=os.environ.get('COURSE_PARAKEET_CLI', str(DEFAULT_CLI)))
    parser.add_argument('--model', type=Path, default=Path(os.environ.get('COURSE_PARAKEET_MODEL', DEFAULT_MODEL)))
    parser.add_argument('--threads', type=int, default=4,
                        help='threads do motor (as políticas mac/linux limitam a 2)')
    parser.add_argument('--chunk-seconds', type=float, default=120.0,
                        help='duração alvo dos blocos; menor usa menos RAM (~1,2 GB a 120 s)')
    parser.add_argument('--policy', choices=('auto', 'mac', 'linux', 'none'), default='auto',
                        help='limites de recursos do host (padrão: pelo sistema operacional)')
    parser.add_argument('--force', action='store_true', help='refaz aulas já transcritas')
    parser.add_argument('--select', action='append', metavar='GLOB',
                        help='transcreve só os áudios cujo nome casa com o padrão (repetível)')
    args = parser.parse_args(argv)
    source = args.source.expanduser().resolve()
    output = (args.output or source / 'Transcrições').expanduser().resolve()
    course = args.course or source.name
    policy = policy_for(args.policy)
    transcriber = Transcriber([args.engine], args.model.expanduser(), args.threads,
                              target=args.chunk_seconds, policy=policy)
    transcriber.check()
    print(f'política: {policy.name}; {transcriber.threads} threads; blocos de ~{args.chunk_seconds:.0f} s', flush=True)
    found = [p for p in lessons(source) if output not in p.parents
             and (not args.select or any(fnmatch.fnmatch(p.name, g) for g in args.select))]
    if not found:
        raise SystemExit(f'Nenhum áudio encontrado em {source}')
    rows, failures = [], 0
    for number, audio in enumerate(found, 1):
        relative = audio.relative_to(source).with_suffix('.md')
        target = output / relative
        support = next((audio.with_suffix(s).name for s in ('.pdf', '.PDF') if audio.with_suffix(s).is_file()), None)
        if target.is_file() and not args.force:
            print(f'[{number}/{len(found)}] já transcrita: {relative}', flush=True)
            text = target.read_text(encoding='utf-8')
            rows.append((relative.as_posix(), probe_duration(audio), len(text.split('\n---\n', 2)[-1].split())))
            continue
        print(f'[{number}/{len(found)}] transcrevendo: {audio.name}', flush=True)
        try:
            duration, count = transcriber.transcribe(audio, target, course=course, support=support,
                                                     title=audio.stem)
        except (subprocess.SubprocessError, RuntimeError, ValueError, OSError) as error:
            failures += 1
            print(f'  falhou ({type(error).__name__}): {error}; blocos concluídos preservados', file=sys.stderr, flush=True)
            continue
        rows.append((relative.as_posix(), duration, count))
        print(f'  ok: {target}', flush=True)
    write_atomic(output / 'README.md', render_index(course, rows))
    work = output / WORK_DIR
    if work.is_dir() and not any(work.iterdir()):
        work.rmdir()
    print(f'{len(rows)}/{len(found)} aulas transcritas em {output}', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
