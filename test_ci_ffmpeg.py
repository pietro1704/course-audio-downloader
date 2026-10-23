"""Exercise the Linux CI installer in an isolated PATH without root or downloads."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == 'posix', 'Linux CI shell installer')
class CIFFmpegTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.mirror = self.root / 'mirrors'
        self.mirror.write_text('http://azure.archive.ubuntu.com/ubuntu/\nhttps://security.ubuntu.com/ubuntu/\n')
        self.log = self.root / 'calls'
        self.env = dict(os.environ, PATH=str(self.bin), CI_TEST_ROOT=str(self.root),
                        COURSE_CI_APT_MIRROR_FILE=str(self.mirror))
        self.bash = shutil.which('bash')
        for name in ('sed',):
            (self.bin / name).symlink_to(shutil.which(name))
        self.write('sudo', '#!/bin/sh\nexec "$@"\n')
        self.tool = f'#!{sys.executable}\n' + '''import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['CI_TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
with (root / 'calls').open('a') as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'apt-get':
    stage = 'install' if 'install' in sys.argv else 'update'
    if os.environ.get('CI_TEST_BLOCK') == stage:
        (root / 'blocked').touch()
        time.sleep(60)
    if os.environ.get('CI_TEST_FAIL') == stage:
        sys.exit(42)
    if stage == 'install':
        for tool in ('ffmpeg', 'ffprobe'):
            destination = root / 'bin' / tool
            destination.write_bytes(pathlib.Path(__file__).read_bytes())
            destination.chmod(0o755)
elif os.environ.get('CI_TEST_FAIL') == name:
    sys.exit(43)
'''
        self.write('apt-get', self.tool)
        self.script = Path(os.environ.get('COURSE_TEST_INSTALLER',
                                         Path(__file__).parent / '.github/scripts/install-ffmpeg.sh'))

    def write(self, name, source):
        target = self.bin / name
        target.write_text(source)
        target.chmod(0o755)

    def run_installer(self, **env):
        return subprocess.run([self.bash, str(self.script)], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_install_uses_official_mirror_and_checks_both_tools(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mirror.read_text(),
                         'https://archive.ubuntu.com/ubuntu/\nhttps://security.ubuntu.com/ubuntu/\n')
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ['apt-get', 'apt-get', 'ffmpeg', 'ffprobe'])
        self.assertIn('--no-install-recommends', calls[1])

    def test_already_installed_checks_tools_without_touching_mirrors(self):
        for name in ('ffmpeg', 'ffprobe'):
            self.write(name, self.tool)
        before = self.mirror.read_bytes()
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.mirror.read_bytes(), before)
        self.assertEqual([call[0] for call in self.calls()], ['ffmpeg', 'ffprobe'])

    def test_package_failure_stops_before_reporting_success(self):
        for stage in ('update', 'install'):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.run_installer(CI_TEST_FAIL=stage).returncode, 42)
                self.assertTrue(all(call[0] == 'apt-get' for call in self.calls()))
                self.assertFalse((self.bin / 'ffmpeg').exists())

    def test_broken_ffprobe_is_a_failure(self):
        self.assertEqual(self.run_installer(CI_TEST_FAIL='ffprobe').returncode, 43)

    def test_cancellation_does_not_install_or_report_success(self):
        process = subprocess.Popen([self.bash, str(self.script)],
                                   env=dict(self.env, CI_TEST_BLOCK='update'),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'blocked').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.root / 'blocked').exists())
            os.killpg(process.pid, signal.SIGTERM)
            self.assertNotEqual(process.wait(timeout=5), 0)
            self.assertFalse((self.bin / 'ffmpeg').exists())
            self.assertEqual(len(self.calls()), 1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
