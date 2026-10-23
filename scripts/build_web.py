"""Prepare only public UI assets for Cloudflare Pages; exclude tests and local data."""
from pathlib import Path
import shutil
ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ('index.html', 'style.css', 'app.js', 'model.js', '_headers')

def build():
    output = ROOT/'.web-dist'
    output.mkdir(exist_ok=True)
    for old in output.iterdir():
        if old.is_dir():
            raise RuntimeError('unexpected-directory-in-web-output')
        old.unlink()
    for name in PUBLIC:
        shutil.copyfile(ROOT/'web'/name, output/name)
    return output

if __name__ == '__main__':print(build())
