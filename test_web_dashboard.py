"""Real HTTP/child-process contracts for the web-to-CLI adapter."""
import json
from pathlib import Path
import tempfile
import unittest
import web_dashboard


class RegistryTests(unittest.TestCase):
    def test_web_options_cannot_select_commands_or_paths(self):
        with self.assertRaises(ValueError):
            web_dashboard.options({'command': 'anything'})
        with self.assertRaises(ValueError):
            web_dashboard.options({'concurrency': True})
        self.assertEqual(web_dashboard.options({'concurrency': 2}), ['--concurrency', '2'])

    def test_origin_is_exact_and_https_for_remote_site(self):
        self.assertEqual(web_dashboard.origin('https://panel.example.test'), 'https://panel.example.test')
        for value in ['https://panel.example.test/path', 'https://user@panel.example.test',
                      'http://panel.example.test', 'null', 'https://panel.example.test?x=1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                web_dashboard.origin(value)

import http.client
import os
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.catalog, self.profile = self.base/'catalog.json', self.base/'profile.json'
        self.catalog.write_text(json.dumps(dict(schema='course-catalog/v1', complete=True, expected_count=1,
            collected_count=1, lessons=[dict(url='https://courses.example.test/lesson/1',title='Public lesson',site_path=['Course','Lesson'])])))
        self.profile.write_text(json.dumps(dict(schema='course-site-profile/v1',mode='direct',origin='https://courses.example.test')))
        self.config = self.base/'dashboard.json'
        self.config.write_text(json.dumps(dict(schema='course-web/v1', jobs=[dict(id='one',label='Example course',
            catalog=str(self.catalog),profile=str(self.profile),root=str(self.base/'work'),
            destination=str(self.base/'cloud'),icloud_python=sys.executable)])))
        self.jobs = web_dashboard.load_jobs(self.config)
        self.runner = web_dashboard.Runner(self.jobs,self.base/'state')
        self.addCleanup(self.runner.close)
        self.server = web_dashboard.Server(0,self.runner,'https://panel.example.test','secret-pairing-token')
        self.worker = threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.worker.join,2);self.addCleanup(self.server.shutdown)

    def request(self,method,path,body=None,token='secret-pairing-token',origin='https://panel.example.test',headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=10)
        h={'Origin':origin,'Authorization':'Bearer '+token}
        if body is not None:h['Content-Type']='application/json'
        h.update(headers or {})
        connection.request(method,path,None if body is None else json.dumps(body),h)
        response=connection.getresponse();data=response.read();result=(response.status,response.headers,json.loads(data) if data else None)
        connection.close();return result

    def start(self,request_id='request-one',**overrides):
        return self.request('POST','/api/runs',{**dict(job_id='one',operation='check',options={},request_id=request_id),**overrides})

    def finish(self):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            state=self.runner.state()
            if state['run']['status'] not in {'running','cancelling'}:return state['run']
            time.sleep(.02)
        self.fail('CLI child did not finish')

    def test_real_cli_dry_run_roundtrip_does_not_create_work_or_cloud(self):
        code,headers,state=self.request('GET','/api/state')
        self.assertEqual(code,200);self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(state['jobs'],[dict(id='one',label='Example course',total=1)])
        code,_,started=self.start();self.assertEqual(code,202)
        done=self.finish();self.assertEqual(done['status'],'succeeded',done)
        self.assertEqual(done['result']['pending'],1)
        self.assertFalse((self.base/'work').exists());self.assertFalse((self.base/'cloud').exists())
        self.assertNotIn('secret-pairing-token',json.dumps(self.runner.state()))
        self.assertNotIn('catalog',json.dumps(self.runner.state()))
        again=self.start()[2];self.assertEqual(started['id'],again['id'])
        self.assertEqual(len(list((self.base/'state').glob('*.log'))),1)

    def test_cors_auth_and_host_refuse_remote_control(self):
        for kwargs in [dict(token='wrong'),dict(origin='https://panel.example.test.attacker.test'),
                       dict(origin='null'),dict(headers={'Host':'attacker.test'}),dict(token='não-ascii')]:
            with self.subTest(kwargs=kwargs):
                self.assertIn(self.request('GET','/api/state',**kwargs)[0],{401,403})
        self.assertIsNone(self.runner.child)
        code,headers,_=self.request('OPTIONS','/api/runs',headers={
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization, content-type',
            'Access-Control-Request-Private-Network':'true'})
        self.assertEqual(code,204)
        self.assertEqual(headers['Access-Control-Allow-Origin'],'https://panel.example.test')
        self.assertEqual(headers['Access-Control-Allow-Private-Network'],'true')

    def test_invalid_commands_flags_identity_and_changed_inputs_never_launch(self):
        for request in [dict(job_id='../secret',operation='deliver',options={},request_id='one'),
                        dict(job_id='one',operation='deliver',options={'command':'rm'},request_id='two'),
                        dict(job_id='one',operation='documents',options={'protected_pauses':True},request_id='three')]:
            self.assertEqual(self.request('POST','/api/runs',request)[0],409)
        self.catalog.write_text(self.catalog.read_text()+' ')
        self.assertEqual(self.start()[0],409)
        self.assertIsNone(self.runner.child)

    def test_cli_failure_does_not_claim_success_or_return_private_trace(self):
        self.runner.command=[sys.executable,'-c',"import sys;print('PRIVATE/path?session=SECRET');sys.exit(2)"]
        self.assertEqual(self.start()[0],202)
        done=self.finish();self.assertEqual((done['status'],done['exit_code']),('failed',2))
        self.assertNotIn('SECRET',json.dumps(self.request('GET','/api/state')[2]))

    def test_cancellation_targets_exact_owned_run_and_does_not_restart(self):
        marker=self.base/'ready'
        script=self.base/'child.py';script.write_text("import signal,time,sys\nfrom pathlib import Path\nsignal.signal(signal.SIGINT,lambda *_:sys.exit(130))\nPath("+repr(str(marker))+").write_text('ready')\nwhile True:time.sleep(.05)\n")
        self.runner.command=[sys.executable,str(script)]
        code,_,run=self.start();self.assertEqual(code,202)
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(marker.exists())
        self.assertEqual(self.start('second')[0],409)
        self.assertEqual(self.request('POST','/api/cancel',{'id':'wrong'})[0],409)
        self.assertIsNone(self.runner.child.poll())
        self.assertEqual(self.request('POST','/api/cancel',{'id':run['id']})[0],200)
        self.assertEqual(self.finish()['status'],'cancelled')
        self.assertEqual(self.start()[2]['id'],run['id'])

    def test_stale_progress_is_not_shown_and_profile_identity_is_bound(self):
        folder=self.base/'work/Documentacao';folder.mkdir(parents=True)
        (folder/'delivery-status.json').write_text(json.dumps(dict(pid=-123,delivered=999,title='STALE')))
        self.runner.command=[sys.executable,'-c','import time;time.sleep(.5)']
        run=self.start(operation='deliver')[2]
        self.assertNotIn('progress',self.request('GET','/api/state')[2]['run'])
        self.finish()
        request=dict(job_id='one',operation='deliver',options={'concurrency':2},request_id='request-one')
        self.assertEqual(self.request('POST','/api/runs',request)[0],409)
        self.assertEqual(self.runner.current['id'],run['id'])

    def test_single_bridge_and_inherited_worker_lease(self):
        with self.assertRaisesRegex(ValueError,'another-web-bridge-is-active'):
            web_dashboard.Runner(self.jobs,self.base/'state')
        handle=self.runner._worker();self.assertIsNotNone(handle)
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1)'],pass_fds=(handle.fileno(),))
        handle.close()
        try:
            self.assertEqual(self.start()[0],409)
        finally:child.wait()
        self.assertEqual(self.start()[0],202)
        self.assertEqual(self.finish()['status'],'succeeded')


if __name__ == '__main__':unittest.main()
