"""Loopback web adapter. Trusted local catalogues; execution belongs to course-deliver."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
from cloud_delivery import atomic_json
from course_catalog import load_lessons
from site_profile import load_profile

OPERATIONS = {'check': '--dry-run', 'deliver': None, 'documents': '--documents-only',
              'reconcile-artwork': '--reconcile-original-artwork',
              'refresh-artwork': '--refresh-completed-artwork'}
RANGES = {'concurrency': (1, 8), 'start': (0, 1_000_000), 'limit': (1, 1_000_000),
          'probe': (0, 32), 'min_free_gib': (4, 1_000_000), 'upload_timeout': (1, 21600)}
IDENTIFIER = re.compile(r'[a-zA-Z0-9_-]{1,64}\Z')


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment
            or (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost'})):
        raise ValueError('invalid-site-origin')
    return value


def options(values):
    if not isinstance(values, dict) or set(values)-set(RANGES)-{'encoder', 'protected_pauses'}:
        raise ValueError('invalid-options')
    result = []
    for key, value in values.items():
        if key in RANGES:
            numeric = key in {'min_free_gib', 'upload_timeout'}
            if (type(value) not in ((int, float) if numeric else (int,)) or
                    not math.isfinite(value) or not RANGES[key][0] <= value <= RANGES[key][1]):
                raise ValueError('invalid-option-value')
            result.extend(['--'+key.replace('_', '-'), str(value)])
        elif key == 'encoder':
            if value not in {'aac', 'aac_at'}:
                raise ValueError('invalid-encoder')
            result.extend(['--encoder', value])
        else:
            if type(value) is not bool:
                raise ValueError('invalid-option-value')
            if value:
                result.append('--protected-pauses')
    return result


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_jobs(path):
    config = json.loads(Path(path).read_text())
    if config.get('schema') != 'course-web/v1' or not isinstance(config.get('jobs'), list):
        raise ValueError('invalid-web-config')
    jobs = {}
    for item in config['jobs']:
        ident, label = item.get('id'), item.get('label')
        if (not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident) or ident in jobs
                or not isinstance(label, str) or not label.strip() or len(label) > 120):
            raise ValueError('invalid-job-identity')
        job = {'id': ident, 'label': label}
        for key in ['catalog', 'profile', 'root', 'destination', 'icloud_python']:
            value = item.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError('absolute-local-path-required')
            job[key] = str(Path(value).resolve())
        root, destination = Path(job['root']), Path(job['destination'])
        if root == destination or root.is_relative_to(destination) or destination.is_relative_to(root):
            raise ValueError('overlapping-working-and-cloud-roots')
        job['total'] = len(load_lessons(job['catalog']))
        load_profile(job['profile'])  # Validate trusted local code before accepting remote actions.
        job['bindings'] = {key: digest(job[key]) for key in ['catalog', 'profile']}
        jobs[ident] = job
    return jobs


class Runner:
    """One owned CLI child; retries and cancellation are bound to a run identity."""
    def __init__(self, jobs, state_dir, command=None):
        self.jobs, self.directory = jobs, Path(state_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.child = None
        self.worker_lock = None
        self.command = command or [sys.executable, str(Path(__file__).with_name('course_cli.py'))]
        self.state_path = self.directory/'current-run.json'
        self.current = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        self.requests = {}
        self.instance_lock = (self.directory/'bridge.lock').open('a')
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.instance_lock.close()
            raise ValueError('another-web-bridge-is-active') from None

    def _save(self):
        atomic_json(self.state_path, self.current)

    def _worker(self):
        handle = (self.directory/'worker.lock').open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    def _refresh(self):
        if self.child is not None:
            code = self.child.poll()
            if code is not None and self.current['status'] in {'running', 'cancelling'}:
                self.current.update(status='cancelled' if self.current['status']=='cancelling' and code != 0
                                    else 'succeeded' if code == 0 else 'failed',
                                    exit_code=code, finished_at=time.time())
                self.worker_lock.close(); self.worker_lock = None
                self._save()
        elif self.current and self.current['status'] in {'running', 'cancelling', 'detached'}:
            handle = self._worker()
            self.current['status'] = 'detached' if handle is None else 'unknown'
            if handle:
                handle.close()
            self._save()

    def state(self):
        with self.lock:
            self._refresh()
            run = dict(self.current) if self.current else None
            if run:
                run.pop('signature', None)
                run.pop('request_id', None)
                job = self.jobs.get(run['job_id'])
                run['can_cancel'] = bool(self.child is not None and self.child.poll() is None)
                if job and self.child is not None and run['operation'] != 'check':
                    path = Path(job['root'])/'Documentacao/delivery-status.json'
                    try:
                        if path.stat().st_size <= 256*1024:
                            progress = json.loads(path.read_text())
                            if progress.get('pid') == self.child.pid:
                                run['progress'] = {k: progress[k] for k in ['stage','delivered','selected','title',
                                    'concurrency','active_count','free_gib','slow_stage','reason'] if k in progress}
                    except (OSError, ValueError):
                        pass
                if run['operation'] == 'check' and run['status'] == 'succeeded':
                    try:
                        path = self.directory/(run['id']+'.log')
                        if path.stat().st_size <= 4*1024**2:
                            result = json.loads(path.read_text().strip().splitlines()[-1])
                            run['result'] = {k: result[k] for k in ['ok','total','complete','pending','prepared','uploaded','probed'] if k in result}
                    except (OSError, ValueError, IndexError):
                        pass
            return {'schema': 'course-web-state/v1', 'jobs': [
                {k: job[k] for k in ['id','label','total']} for job in self.jobs.values()], 'run': run}

    def start(self, request):
        if not isinstance(request, dict) or set(request) != {'job_id','operation','options','request_id'}:
            raise ValueError('invalid-run-request')
        job_id, operation, request_id = (request[k] for k in ['job_id','operation','request_id'])
        if (not isinstance(job_id, str) or job_id not in self.jobs or not isinstance(operation, str)
                or operation not in OPERATIONS or not isinstance(request_id, str)
                or not IDENTIFIER.fullmatch(request_id)):
            raise ValueError('invalid-run-identity')
        arguments = options(request['options'])
        if ('--protected-pauses' in arguments and operation in {'documents','reconcile-artwork','refresh-artwork'}) or (
                request['options'].get('probe', 0) and operation != 'check'):
            raise ValueError('incompatible-options')
        signature = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.lock:
            self._refresh()
            previous = self.requests.get(request_id)
            if previous is None and self.current and self.current.get('request_id') == request_id:
                previous = self.current
            if previous:
                if previous['signature'] != signature:
                    raise ValueError('request-identity-conflict')
                return dict(previous)
            if self.current and self.current['status'] in {'running','cancelling','detached'}:
                raise ValueError('a-run-is-active')
            job = self.jobs[job_id]
            if any(digest(job[k]) != v for k,v in job['bindings'].items()):
                raise ValueError('registered-input-changed')
            handle = self._worker()
            if handle is None:
                raise ValueError('previous-cli-still-active')
            ident = uuid.uuid4().hex
            run = dict(id=ident, job_id=job_id, operation=operation, request_id=request_id,
                       signature=signature, status='running', started_at=time.time())
            command = list(self.command)
            for key in ['catalog','profile','root','destination','icloud_python']:
                command.extend(['--'+key.replace('_','-'), job[key]])
            if OPERATIONS[operation]:
                command.append(OPERATIONS[operation])
            command.extend(arguments)
            try:
                # The child inherits the worker lease. A bridge crash cannot admit a second CLI.
                fd = os.open(self.directory/(ident+'.log'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as log:
                    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(handle.fileno(),))
            except BaseException:
                handle.close()
                raise
            self.child, self.worker_lock, self.current = child, handle, run
            self.requests[request_id] = run
            if len(self.requests) > 128:
                self.requests.pop(next(iter(self.requests)))
            self._save()
            return dict(run)

    def cancel(self, ident):
        with self.lock:
            self._refresh()
            if not self.current or ident != self.current['id']:
                raise ValueError('run-identity-mismatch')
            if self.child is None:
                raise ValueError('run-not-owned-by-this-bridge')
            if self.child.poll() is None and self.current['status'] != 'cancelling':
                self.child.send_signal(signal.SIGINT)
                self.current['status'] = 'cancelling'
                self._save()
            return self.state()['run']

    def close(self):
        # The CLI owns graceful drain and artifact safety; never SIGKILL its descendants.
        if self.child is not None and self.child.poll() is None:
            self.cancel(self.current['id'])
            self.child.wait()
        with self.lock:
            self._refresh()
            self.instance_lock.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port, runner, site_origin, token):
        self.runner, self.site_origin, self.token = runner, origin(site_origin), token
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Neither bearer tokens, paths nor catalogue contents belong in access logs.

    def _headers(self, code):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if self.headers.get('Origin') == self.server.site_origin:
            self.send_header('Access-Control-Allow-Origin', self.server.site_origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Connection', 'close')

    def reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self._headers(code); self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def trusted(self, authenticate=True):
        port = self.server.server_address[1]
        if (self.headers.get('Host') not in {f'127.0.0.1:{port}', f'localhost:{port}'} or
                self.headers.get('Origin') != self.server.site_origin):
            self.reply(403, {'error': 'untrusted-origin-or-host'}); return False
        if authenticate and not hmac.compare_digest(self.headers.get('Authorization','').encode(), ('Bearer '+self.server.token).encode()):
            self.reply(401, {'error': 'pairing-required'}); return False
        return True

    def do_OPTIONS(self):
        if not self.trusted(False):return
        requested = {h.strip().lower() for h in self.headers.get('Access-Control-Request-Headers','').split(',') if h.strip()}
        if (self.path not in {'/api/state','/api/runs','/api/cancel'} or
                self.headers.get('Access-Control-Request-Method') not in {'GET','POST'} or
                not requested <= {'authorization','content-type'}):
            self.reply(403, {'error': 'invalid-preflight'});return
        self._headers(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Length', '0'); self.end_headers()

    def do_GET(self):
        if not self.trusted():return
        if self.path != '/api/state':self.reply(404, {'error': 'not-found'});return
        self.reply(200, self.server.runner.state())

    def do_POST(self):
        if not self.trusted():return
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if (not 0 <= size <= 16384 or self.headers.get('Transfer-Encoding') or
                    self.headers.get_content_type() != 'application/json'):
                raise ValueError('invalid-request-body')
            self.connection.settimeout(5)
            request = json.loads(self.rfile.read(size))
            if self.path == '/api/runs':
                result = self.server.runner.start(request)
                # Return the public projection rather than internal identity fingerprints.
                self.reply(202, {'id': result['id'], 'status': result['status']})
            elif self.path == '/api/cancel' and isinstance(request, dict) and set(request)=={'id'}:
                self.reply(200, self.server.runner.cancel(request['id']))
            else:self.reply(404, {'error': 'not-found'})
        except (ValueError, TypeError):
            self.reply(409, {'error': 'invalid-or-conflicting-request'})
        except Exception:
            self.reply(500, {'error': 'cli-operation-failed'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Trusted local course-web/v1 registry')
    parser.add_argument('--origin', required=True, help='Exact published website origin')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--state-dir', default=str(Path.home()/'.local/state/course-audio-downloader/web'))
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:parser.error('invalid-port')
    site = origin(args.origin)
    runner = Runner(load_jobs(args.config), args.state_dir)
    token = secrets.token_urlsafe(32)
    try:
        server = Server(args.port, runner, site, token)
        print(f'Open this private pairing link on this Mac:\n{site}/#token={token}&port={args.port}', flush=True)
        print('Closing this terminal cancels the owned CLI safely and waits for its active work.', flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()
    finally:runner.close()
    return 0


if __name__ == '__main__':raise SystemExit(main())
