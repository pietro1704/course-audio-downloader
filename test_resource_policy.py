import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import resource_policy as rp
import transcription as t

POSIX = hasattr(os, 'fork')


class ParsingTests(unittest.TestCase):
    def test_swap_usage(self):
        self.assertEqual(rp.parse_swap_used('total = 7168.00M  used = 3072.00M  free = 4096.00M  (encrypted)'),
                         3 * rp.GIB)
        self.assertEqual(rp.parse_swap_used('total = 0.00M  used = 0.00M  free = 0.00M'), 0)
        self.assertIsNone(rp.parse_swap_used(''))

    def test_mem_available(self):
        text = 'MemTotal:        8000000 kB\nMemFree:          200000 kB\nMemAvailable:    1572864 kB\n'
        self.assertEqual(rp.parse_mem_available(text), int(1.5 * rp.GIB))
        self.assertIsNone(rp.parse_mem_available('MemTotal: 1 kB\n'))


@unittest.skipUnless(POSIX, 'flock lease is POSIX-only')
class LeaseTests(unittest.TestCase):
    def test_probe_reports_holder_without_keeping_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temp:
            lease = Path(temp) / 'heavy.lock'
            self.assertFalse(rp.lease_held(lease))  # missing file is free
            lease.touch()
            self.assertFalse(rp.lease_held(lease))
            with open(lease, 'a+') as holder:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(rp.lease_held(lease))
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            with open(lease, 'a+') as other:  # the probe released its own shared lock
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class MacPolicyTests(unittest.TestCase):
    def policy(self, ps='', swap='used = 100.00M', level='1', lease=None):
        answers = {'ps': ps, 'vm.swapusage': swap, 'kern.memorystatus_vm_pressure_level': level}
        probe = lambda command: next(v for k, v in answers.items() if k in command)
        return rp.MacPolicy(lease=lease or Path('/nonexistent/lease'), probe=probe)

    def test_clear_host_has_no_blockers_and_caps_threads(self):
        policy = self.policy(ps='/usr/sbin/cfprefsd\n/bin/zsh\n')
        self.assertEqual(policy.blockers(), [])
        self.assertEqual(policy.threads(8), 2)

    def test_each_condition_blocks(self):
        policy = self.policy(ps='/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild\nlaunchd_sim\n',
                             swap='total = 7168.00M  used = 3200.00M', level='2')
        self.assertEqual(policy.blockers(), ['launchd_sim ativo', 'xcodebuild ativo', 'swap 3.1 GiB',
                                             'pressão de memória nível 2'])

    def test_wrap_uses_background_qos_when_available(self):
        wrapped = rp.MacPolicy().wrap(['engine', '-x'])
        self.assertEqual(wrapped[-2:], ['engine', '-x'])
        if sys.platform == 'darwin':
            self.assertEqual(wrapped[:2], ['taskpolicy', '-b'])


class LinuxPolicyTests(unittest.TestCase):
    def test_android_build_processes(self):
        build = rp.LinuxPolicy.is_android_build
        self.assertTrue(build('dart', '/opt/flutter/bin/cache/dart-sdk/bin/dart flutter_tools.snapshot run'))
        self.assertTrue(build('java', '/usr/lib/jvm/bin/java -cp gradle-launcher.jar org.gradle.launcher.daemon'))
        self.assertTrue(build('qemu-system-x86_64', 'qemu-system-x86_64 -avd Pixel'))
        self.assertFalse(build('java', 'java -jar some-server.jar'))
        self.assertFalse(build('adb', 'adb -P 5073 fork-server server'))

    def fake_proc(self, root, available_kb, processes):
        (root / 'meminfo').write_text(f'MemTotal: 8000000 kB\nMemAvailable: {available_kb} kB\n')
        for pid, (comm, cmdline, ticks) in processes.items():
            entry = root / str(pid)
            entry.mkdir()
            (entry / 'comm').write_text(comm + '\n')
            (entry / 'cmdline').write_bytes(cmdline.replace(' ', '\0').encode())
            (entry / 'stat').write_text(f'{pid} ({comm}) S ' + ' '.join(['0'] * 10) + f' {ticks} 0 0 0')

    def test_idle_daemons_do_not_block_but_busy_builds_and_low_memory_do(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fake_proc(root, 4_000_000, {10: ('java', 'java org.gradle.launcher.daemon', 500),
                                             11: ('bash', 'bash', 9)})
            policy = rp.LinuxPolicy(proc=root, sample_seconds=0.01)
            self.assertEqual(policy.blockers(), [])
            samples = iter([{('10', 'java'): 500}, {('10', 'java'): 520}])
            policy._build_cpu_ticks = lambda: next(samples)
            (root / 'meminfo').write_text('MemAvailable: 1000000 kB\n')
            self.assertEqual(policy.blockers(), ['MemAvailable 1.0 GiB', 'build ativo: java'])

    def test_wrap_stacks_scope_nice_and_ionice(self):
        policy = rp.LinuxPolicy()
        policy._scope = True
        wrapped = policy.wrap(['engine'])
        self.assertEqual(wrapped[:4], ['systemd-run', '--user', '--scope', '--quiet'])
        for option in ('MemoryMax=2G', 'CPUQuota=200%'):
            self.assertIn(option, wrapped)
        self.assertEqual(wrapped[wrapped.index('nice'):wrapped.index('nice') + 3], ['nice', '-n', '19'])
        self.assertEqual(wrapped[-1], 'engine')
        policy._scope = False
        self.assertEqual(policy.wrap(['engine'])[:3], ['nice', '-n', '19'])


class ScriptedPolicy(rp.NullPolicy):
    def __init__(self, answers):
        self.answers = list(answers)

    def blockers(self):
        return self.answers.pop(0) if self.answers else []


@unittest.skipUnless(POSIX, 'SIGSTOP/SIGCONT are POSIX-only')
class SupervisionTests(unittest.TestCase):
    def test_engine_is_paused_and_resumed_not_killed(self):
        logs = []
        policy = ScriptedPolicy([[], ['xcodebuild ativo'], ['xcodebuild ativo'], []])
        transcriber = t.Transcriber(['unused'], 'model', policy=policy, poll=0.2, log=logs.append)
        code = 'import time\nfor _ in range(10): time.sleep(0.1)\nprint("feito")'
        result = transcriber.supervised([sys.executable, '-c', code], timeout=30)
        self.assertEqual(result.stdout.strip(), 'feito')
        self.assertEqual(logs, ['  pausado: xcodebuild ativo', '  retomado'])

    def test_pause_reaches_engine_behind_spawning_wrapper(self):
        class Wrapping(ScriptedPolicy):
            def wrap(self, command):  # like taskpolicy: a parent that spawns the real engine
                return [sys.executable, '-c', 'import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))',
                        *command]
        logs = []
        policy = Wrapping([[]] + [['swap 4.0 GiB']] * 15)
        transcriber = t.Transcriber(['unused'], 'model', policy=policy, poll=0.1, log=logs.append)
        code = 'import time\nfor _ in range(10): time.sleep(0.1)\nprint(1)'
        started = time.monotonic()
        result = transcriber.supervised([sys.executable, '-c', code], timeout=30)
        self.assertGreater(time.monotonic() - started, 1.6)
        self.assertEqual(result.stdout.strip(), '1')

    def test_paused_time_does_not_count_against_timeout(self):
        # 1.5 s paused + ~1 s of work exceeds the 1.8 s budget only if paused time were counted.
        policy = ScriptedPolicy([['swap 4.0 GiB']] * 15)
        transcriber = t.Transcriber(['unused'], 'model', policy=policy, poll=0.1, log=lambda _: None)
        code = 'import time\nfor _ in range(10): time.sleep(0.1)\nprint(1)'
        started = time.monotonic()
        result = transcriber.supervised([sys.executable, '-c', code], timeout=1.8)
        self.assertGreater(time.monotonic() - started, 1.8)
        self.assertEqual(result.stdout.strip(), '1')

    def test_admission_waits_until_clear(self):
        logs = []
        transcriber = t.Transcriber(['unused'], 'model', poll=0.01, log=logs.append,
                                    policy=ScriptedPolicy([['lease ocupado'], ['lease ocupado'], []]))
        transcriber.wait_for_admission()
        self.assertEqual(logs, ['  aguardando: lease ocupado', '  liberado; continuando'])

    def test_memory_kill_splits_chunk_and_keeps_all_words(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'a.wav'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=8',
                            '-ar', '16000', '-ac', '1', str(audio)], check=True)
            engine = root / 'engine.py'
            engine.write_text(textwrap.dedent('''
                import os, signal, sys, wave
                args = sys.argv[1:]
                with wave.open(args[args.index('-f') + 1]) as audio:
                    seconds = audio.getnframes() / audio.getframerate()
                if seconds > 5:
                    os.kill(os.getpid(), signal.SIGKILL)
                print('  [0] id= 1 frame= 1 dur_idx= 1 dur_val= 1 p=1 plog=0 t0=   0 t1=   8 '
                      'word_start=true "\\u2581parte"', file=sys.stderr)
            '''))
            logs = []
            transcriber = t.Transcriber([sys.executable, engine], root / 'model', poll=0.1, min_split=2,
                                        log=logs.append)
            words = transcriber.chunk_words(audio, 0, 8, root)
        self.assertEqual(words, [(0.0, 'parte'), (4.0, 'parte')])
        self.assertTrue(any('dividindo' in line for line in logs))


if __name__ == '__main__':
    unittest.main()
