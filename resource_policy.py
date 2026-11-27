"""Host-aware admission and throttling for long background jobs (transcription).

A policy answers three questions: how to wrap a heavy command (QoS, priority,
cgroup limits), how many threads it may use, and what currently blocks it.
Blocked work is paused with SIGSTOP and resumed with SIGCONT instead of being
killed, so no completed computation is lost. Policies never signal, lock or
otherwise touch processes they did not start.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

GIB = 1 << 30
EPUB_LEASE = Path('/tmp/epub2mp3.heavy-job.lock')
MAC_BLOCKING_PROCESSES = frozenset({
    'xcodebuild', 'Simulator', 'launchd_sim', 'update_dyld_sim_shared_cache'})
LINUX_ACTIVE_CPU_SHARE = 0.10  # one tenth of a core, summed over matching processes


def parse_swap_used(text):
    """Bytes of swap in use from ``sysctl vm.swapusage``."""
    match = re.search(r'used = ([\d.]+)([KMG])', text)
    if not match:
        return None
    return float(match.group(1)) * {'K': 1 << 10, 'M': 1 << 20, 'G': GIB}[match.group(2)]


def parse_mem_available(text):
    """Bytes of ``MemAvailable`` from /proc/meminfo."""
    match = re.search(r'^MemAvailable:\s+(\d+) kB', text, re.M)
    return int(match.group(1)) * 1024 if match else None


def lease_held(path=EPUB_LEASE):
    """True when another process holds the flock lease; probes without keeping it."""
    try:
        import fcntl
    except ImportError:
        return False
    try:
        handle = open(path, 'r')
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _output(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


class NullPolicy:
    name = 'none'
    max_threads = None

    def wrap(self, command):
        return list(command)

    def threads(self, requested):
        return requested if self.max_threads is None else max(1, min(requested, self.max_threads))

    def blockers(self):
        return []


class MacPolicy(NullPolicy):
    """Intel Mac shared with Xcode/Simulator work: yield to it completely."""
    name = 'mac'
    max_threads = 2

    def __init__(self, max_swap=3 * GIB, lease=EPUB_LEASE, probe=None):
        self.max_swap = max_swap
        self.lease = lease
        self.probe = probe or _output

    def wrap(self, command):
        return ['taskpolicy', '-b', *command] if shutil.which('taskpolicy') else list(command)

    def blockers(self):
        reasons = []
        if lease_held(self.lease):
            reasons.append(f'lease {self.lease} ocupado')
        running = {Path(line.strip()).name for line in self.probe(['ps', '-Ao', 'comm=']).splitlines()}
        reasons += [f'{name} ativo' for name in sorted(running & MAC_BLOCKING_PROCESSES)]
        swap = parse_swap_used(self.probe(['sysctl', '-n', 'vm.swapusage']))
        if swap is not None and swap >= self.max_swap:
            reasons.append(f'swap {swap / GIB:.1f} GiB')
        level = self.probe(['sysctl', '-n', 'kern.memorystatus_vm_pressure_level']).strip()
        if level and level != '1':
            reasons.append(f'pressão de memória nível {level}')
        return reasons


class LinuxPolicy(NullPolicy):
    """Linux host shared with Flutter/Gradle/emulator work: lowest priority, capped cgroup."""
    name = 'linux'
    max_threads = 2

    def __init__(self, min_available=int(1.5 * GIB), memory_max='2G', cpu_quota='200%',
                 proc=Path('/proc'), sample_seconds=1.0):
        self.min_available = min_available
        self.memory_max = memory_max
        self.cpu_quota = cpu_quota
        self.proc = proc
        self.sample_seconds = sample_seconds
        self._scope = None

    def scope_available(self):
        if self._scope is None:
            self._scope = bool(shutil.which('systemd-run')) and subprocess.run(
                ['systemd-run', '--user', '--scope', '--quiet', 'true'],
                capture_output=True, timeout=30).returncode == 0
        return self._scope

    def wrap(self, command):
        wrapped = ['nice', '-n', '19']
        if shutil.which('ionice'):
            wrapped += ['ionice', '-c3']
        wrapped += list(command)
        if self.scope_available():
            wrapped = ['systemd-run', '--user', '--scope', '--quiet', '--collect',
                       '-p', f'MemoryMax={self.memory_max}', '-p', 'MemorySwapMax=0',
                       '-p', f'CPUQuota={self.cpu_quota}', *wrapped]
        return wrapped

    @staticmethod
    def is_android_build(comm, cmdline):
        if comm in {'flutter', 'dart', 'dartaotruntime', 'gradle', 'emulator'}:
            return True
        if comm.startswith(('qemu-system', 'emulator')):
            return True
        lowered = cmdline.lower()
        return comm == 'java' and ('gradle' in lowered or 'kotlin' in lowered)

    def _build_cpu_ticks(self):
        ticks = {}
        for entry in self.proc.glob('[0-9]*'):
            try:
                comm = (entry / 'comm').read_text().strip()
                cmdline = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                if not self.is_android_build(comm, cmdline):
                    continue
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                ticks[(entry.name, comm)] = int(fields[11]) + int(fields[12])
            except (OSError, IndexError, ValueError):
                continue
        return ticks

    def active_builds(self):
        before = self._build_cpu_ticks()
        if not before:
            return []
        time.sleep(self.sample_seconds)
        after = self._build_cpu_ticks()
        hertz = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
        busy = {}
        for key, value in after.items():
            busy[key[1]] = busy.get(key[1], 0) + max(0, value - before.get(key, value))
        total = sum(busy.values()) / hertz / self.sample_seconds
        return sorted(name for name, used in busy.items() if used) if total >= LINUX_ACTIVE_CPU_SHARE else []

    def blockers(self):
        reasons = []
        try:
            available = parse_mem_available((self.proc / 'meminfo').read_text())
        except OSError:
            available = None
        if available is not None and available < self.min_available:
            reasons.append(f'MemAvailable {available / GIB:.1f} GiB')
        active = self.active_builds()
        if active:
            reasons.append('build ativo: ' + ', '.join(active))
        return reasons


def policy_for(name='auto'):
    if name == 'auto':
        name = {'darwin': 'mac', 'linux': 'linux'}.get(sys.platform, 'none')
    return {'mac': MacPolicy, 'linux': LinuxPolicy, 'none': NullPolicy}[name]()
