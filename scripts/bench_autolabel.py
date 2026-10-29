"""
Does the page stay responsive while "Label with model" runs? Starts its own server on a fresh database, makes a
managed bucket of N synthetic images, registers a YOLO .pt and labels the bucket, while a probe loop hits
GET /api/jobs and a thumbnail every 100 ms. Prints the POST time and the probe p50 / p95 / max before and during
the job. Nothing outside its scratch folder and database is touched.

    .venv/bin/python scripts/bench_autolabel.py --images 2000
    .venv/bin/python scripts/bench_autolabel.py --no-model-process      # the in-process path, for comparison

PostgreSQL: --pg / DATAVIZ_TEST_DATABASE_URL (a server where the user may CREATE DATABASE), else the test
container's default (postgresql://postgres:pw@127.0.0.1:32771/postgres).
"""

import argparse
import io
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

import httpx
import psycopg
import yaml

HERE = Path(__file__).resolve().parent.parent
SECRET = 'q2Xb0hJb7fWm1Xr3d6Y0cS9Zp8Lk4Nv2Tg5Ue7Ia1Ro='
ADMIN = ('bench', 'bench-password-123')
H = {'X-Requested-With': 'dataviz'}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def images_zip(n, w=640, h=480):
    from PIL import Image, ImageDraw
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as z:
        for i in range(n):
            im = Image.new('RGB', (w, h), ((i * 37) % 255, (i * 91) % 255, (i * 13) % 255))
            d = ImageDraw.Draw(im)
            d.rectangle([w // 4, h // 4, w // 2 + i % 100, h // 2 + i % 80], fill=(240, 240, 240))
            b = io.BytesIO()
            im.save(b, 'JPEG', quality=85)
            z.writestr(f'{i:05d}.jpg', b.getvalue())     # flat: the bucket root holds the images
    return buf.getvalue()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else float('nan')


def summary(name, xs):
    if not xs:
        return f'{name:<22} (no samples)'
    return (f'{name:<22} n={len(xs):<5} p50={statistics.median(xs):7.1f} ms  p95={pct(xs, .95):7.1f} ms  '
            f'max={max(xs):7.1f} ms')


class Probe(threading.Thread):
    """GET /api/jobs and one thumbnail every `every` s; latencies (ms) go to the current phase."""

    def __init__(self, c, thumb, every=0.1):
        super().__init__(daemon=True)
        self.c, self.thumb, self.every = c, thumb, every
        self.phase, self.stop = 'idle', False
        self.lat = {}

    def run(self):
        while not self.stop:
            t0 = time.perf_counter()
            for key, url in (('jobs', '/api/jobs'), ('thumb', self.thumb)):
                s = time.perf_counter()
                try:
                    self.c.get(url, timeout=30)
                except httpx.HTTPError:
                    pass
                self.lat.setdefault((self.phase, key), []).append((time.perf_counter() - s) * 1000)
            time.sleep(max(0.0, self.every - (time.perf_counter() - t0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--images', type=int, default=2000)
    ap.add_argument('--weights', default=str(HERE.parent.parent / 'weights' / 'yolo11n.pt'))
    ap.add_argument('--pg', default=os.environ.get('DATAVIZ_TEST_DATABASE_URL')
                    or 'postgresql://postgres:pw@127.0.0.1:32771/postgres')
    ap.add_argument('--no-model-process', action='store_true', help='workers.model_process: false (in-process)')
    ap.add_argument('--app', default=str(HERE), help='the checkout to run (e.g. a worktree of an older commit)')
    ap.add_argument('--keep', action='store_true', help='keep the scratch folder and database')
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix='bench_autolabel_'))
    dbname = f'bench_{uuid.uuid4().hex[:10]}'
    with psycopg.connect(a.pg, autocommit=True) as c:
        c.execute(f'CREATE DATABASE {dbname}')
    dburl = a.pg.rsplit('/', 1)[0] + '/' + dbname
    cfg = yaml.safe_load((Path(a.app) / 'config.yaml').read_text())
    cfg.setdefault('database', {})['url'] = dburl
    cfg.setdefault('storage', {})['data_root'] = str(tmp / 'data')
    cfg.setdefault('dataset', {})['workspace_root'] = str(tmp / 'cache')
    cfg.setdefault('workers', {})['model_process'] = not a.no_model_process
    cfg.setdefault('startup', {})['precompute'] = []
    cfg['workers']['warmup'] = False
    cfg.setdefault('models', {})['allowed_roots'] = [str(Path(a.weights).resolve().parent)]
    (tmp / 'config.yaml').write_text(yaml.safe_dump(cfg))
    port = free_port()
    env = {**os.environ, 'DATAVIZ_SECRET_KEY': SECRET, 'DATAVIZ_ADMIN_USERNAME': ADMIN[0],
           'DATAVIZ_ADMIN_EMAIL': 'bench@example.com', 'DATAVIZ_ADMIN_PASSWORD': ADMIN[1], 'DATABASE_URL': dburl}
    log = open(tmp / 'server.log', 'w')
    srv = subprocess.Popen([sys.executable, 'app.py', '--config', str(tmp / 'config.yaml'), '--host', '127.0.0.1',
                            '--port', str(port)], cwd=a.app, env=env, stdout=log, stderr=subprocess.STDOUT)
    probe = None
    try:
        c = httpx.Client(base_url=f'http://127.0.0.1:{port}', headers=H, timeout=120)
        for _ in range(240):
            try:
                if c.get('/api/auth/config').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if srv.poll() is not None:
                raise SystemExit(f'the server exited: see {tmp / "server.log"}')
            time.sleep(0.5)
        c.post('/api/auth/login', json={'login': ADMIN[0], 'password': ADMIN[1]}).raise_for_status()

        b = c.post('/api/dc/buckets', json={'name': 'bench', 'kind': 'managed'}).raise_for_status().json()
        bid = b.get('id') or b['bucket']['id']
        print(f'uploading {a.images} images…', flush=True)
        c.post(f'/api/dc/buckets/{bid}/upload', params={'path': '', 'unzip': 1},
               files={'files': ('imgs.zip', images_zip(a.images), 'application/zip')}).raise_for_status()
        m = c.post('/api/models', json={'name': 'yolo11n', 'family': 'yolo', 'format': 'pt',
                                        'path': a.weights}).raise_for_status().json()

        probe = Probe(c, f'/api/dc/buckets/{bid}/file?path=00000.jpg&w=256')
        probe.start()
        time.sleep(3)
        probe.phase = 'labeling'
        t0 = time.perf_counter()
        r = c.post('/api/dc/autolabel', json={'bucket': bid, 'path': '', 'model': {'registry_id': m['id']},
                                              'target': 'inplace', 'conf': 0.25})
        post_ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            raise SystemExit(f'autolabel: {r.status_code} {r.text}')
        jid = r.json()['job_id']
        t_job = time.perf_counter()
        last = ''
        while True:
            j = c.get(f'/api/jobs/{jid}').json()
            st = f'{j.get("status")} · {j.get("stage")} · {j.get("done")}/{j.get("total")}'
            if st != last:
                print('  ', st, flush=True)
                last = st
            if j.get('status') in ('done', 'error', 'cancelled'):
                break
            time.sleep(1)
        job_s = time.perf_counter() - t_job
        probe.stop = True
        probe.join()

        print(f'\nmodel process: {"off" if a.no_model_process else "on"} · {a.images} images · job {j["status"]} '
              f'in {job_s:.1f} s ({a.images / job_s:.0f} img/s)')
        if j.get('status') == 'error':
            print('   error:', j.get('error'))
        print(f'POST /api/dc/autolabel   {post_ms:.0f} ms')
        for phase in ('idle', 'labeling'):
            for key in ('jobs', 'thumb'):
                print(summary(f'{phase} {key}', probe.lat.get((phase, key), [])))
    finally:
        if probe:
            probe.stop = True
        srv.terminate()
        try:
            srv.wait(20)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        if a.keep:
            print(f'kept {tmp} and database {dbname}')
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            with psycopg.connect(a.pg, autocommit=True) as cc:
                cc.execute(f'DROP DATABASE IF EXISTS {dbname} WITH (FORCE)')


if __name__ == '__main__':
    main()
