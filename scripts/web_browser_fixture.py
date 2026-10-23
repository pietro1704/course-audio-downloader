"""Private synthetic browser QA: real CLI preflight, controlled child for cancellation."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_dashboard

ROOT = Path(__file__).resolve().parents[1]
class Static(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT/'web'),**kwargs)
    def log_message(self,*args):pass
    def end_headers(self):
        for line in (ROOT/'web/_headers').read_text().splitlines()[1:]:
            name,value=line.strip().split(':',1);self.send_header(name,value.strip())
        super().end_headers()

with tempfile.TemporaryDirectory() as directory:
    base=Path(directory);catalog,profile=base/'catalog.json',base/'profile.json'
    lessons=[dict(url=f'https://courses.example.test/lesson/{i}',title=f'Example {i}',site_path=['Example course',f'Lesson {i}']) for i in range(3)]
    catalog.write_text(json.dumps(dict(schema='course-catalog/v1',complete=True,expected_count=3,collected_count=3,lessons=lessons)))
    profile.write_text(json.dumps(dict(schema='course-site-profile/v1',mode='direct',origin='https://courses.example.test')))
    config=base/'web.json';config.write_text(json.dumps(dict(schema='course-web/v1',jobs=[dict(id='example',label='Curso de exemplo',catalog=str(catalog),profile=str(profile),root=str(base/'work'),destination=str(base/'cloud'),icloud_python=sys.executable)])))
    fixture=base/'cli-fixture.py'
    fixture.write_text('''import json,os,signal,sys,time
from pathlib import Path
if '--dry-run' in sys.argv:
 os.execv(sys.executable,[sys.executable,CLI,*sys.argv[1:]])
root=Path(sys.argv[sys.argv.index('--root')+1]);folder=root/'Documentacao';folder.mkdir(parents=True,exist_ok=True)
path=folder/'delivery-status.json'
def stop(*_):
 path.write_text(json.dumps(dict(pid=os.getpid(),stage='interrupted',selected=3,delivered=1)))
 sys.exit(130)
signal.signal(signal.SIGINT,stop)
path.write_text(json.dumps(dict(pid=os.getpid(),stage='wait-for-upload',selected=3,delivered=1,title='Example lesson',free_gib=12.5)))
while True:time.sleep(.05)
'''.replace('CLI',repr(str(ROOT/'course_cli.py'))))
    static=ThreadingHTTPServer(('127.0.0.1',0),Static)
    site=f'http://127.0.0.1:{static.server_address[1]}'
    runner=web_dashboard.Runner(web_dashboard.load_jobs(config),base/'state',command=[sys.executable,str(fixture)])
    bridge=web_dashboard.Server(0,runner,site,'browser-fixture-token')
    workers=[threading.Thread(target=s.serve_forever,daemon=True) for s in [static,bridge]]
    for worker in workers:worker.start()
    print(json.dumps(dict(site=site,port=bridge.server_address[1],token='browser-fixture-token')),flush=True)
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while True:signal.pause()
    except KeyboardInterrupt:pass
    finally:
        for server in [static,bridge]:server.shutdown();server.server_close()
        runner.close()
        for worker in workers:worker.join()
