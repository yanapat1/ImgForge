"""
Benchmark of the labeling backend against the plan's budgets (not a test; run by hand):

    DATAVIZ_TEST_DATABASE_URL=postgresql://user:pw@host:port/postgres .venv/bin/python tests/bench_labeling.py [n]
    (default 10000; a throw-away database is created on that server, migrated, and dropped at the end)

Measures: autosave PUT of 200 shapes (store + HTTP route), a media page of a 10k project (plain and filtered),
the import scan of n local images, and the export of n images x 2 tasks to a local folder (time + Python heap
peak via tracemalloc, to show memory stays flat). Numbers go into dataviz/labeling/README.md "Measured".
"""

import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # conftest helpers

from PIL import Image  # noqa: E402

from dataviz.labeling.projects import ProjectRegistry  # noqa: E402


def ms(t0):
    return (time.perf_counter() - t0) * 1000


def stats(xs):
    xs = sorted(xs)
    return f'median {xs[len(xs) // 2]:.1f} ms, p95 {xs[int(len(xs) * 0.95)]:.1f} ms'


def boxes(cls, n):
    return [{'id': uuid.uuid4().hex, 'class_id': cls, 'geometry_type': 'bbox',
             'geometry': {'x': (i % 20) / 21, 'y': (i // 20) / 11, 'w': 0.04, 'h': 0.05}} for i in range(n)]


def _database():
    """A fresh migrated database on DATAVIZ_TEST_DATABASE_URL (the engine points at it); returns a drop()."""
    import os
    import psycopg
    from sqlalchemy.engine import make_url
    from dataviz import db
    from dataviz.db import migrate
    base = make_url(os.environ['DATAVIZ_TEST_DATABASE_URL']).set(drivername='postgresql')
    name = f'bench_{uuid.uuid4().hex[:8]}'
    admin = base.set(database='postgres').render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE {name}')
    db.init(base.set(database=name).render_as_string(hide_password=False))
    migrate.upgrade()

    def drop():
        db.dispose()
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
    return drop


def main(n):
    drop = _database()
    try:
        _main(n)
    finally:
        drop()


def _main(n):
    from conftest import _TestUserMiddleware, make_user
    from dataviz.labeling import media, sinks
    from dataviz.labeling.export import run_export

    tmp = Path(tempfile.mkdtemp(prefix='label-bench-'))
    src = tmp / 'src'
    t0 = time.perf_counter()
    im = Image.new('RGB', (64, 48), (120, 80, 40))
    for i in range(n):
        d = src / f'd{i % 10}'
        d.mkdir(parents=True, exist_ok=True)
        im.save(d / f'{i:06d}.jpg', 'JPEG', quality=70)
    print(f'generated {n} jpegs in {ms(t0) / 1000:.1f} s')

    reg = ProjectRegistry(tmp / 'ws' / 'labeling')
    user = make_user('bench', admin=True)
    e = reg.create({'name': 'bench', 'source': {'kind': 'local', 'path': str(src)},
                    'tasks': [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['a', 'b']},
                              {'name': 'Segment', 'task_type': 'segmentation', 'classes': ['c']}]}, user['id'])
    pid = e['id']
    st = reg.store(pid)
    det, seg = st.tasks()

    # ---- import scan (same steps as the route's job: list, probe on 8 threads, batches of 500)
    from concurrent.futures import ThreadPoolExecutor
    from dataviz.server.routes.labeling import assign_names
    source = media.make_source(e['source'], reg.project_dir(pid), None)
    t0 = time.perf_counter()
    items = source.list_images()
    rels = [r for r, _ in items]
    names = assign_names(rels, set())
    with ThreadPoolExecutor(8) as pool:
        for i in range(0, len(rels), 500):
            chunk = rels[i:i + 500]
            rows = [{'file_name': nm, 'uri': r, 'width': p['width'], 'height': p['height'], 'mime': p['mime'],
                     'size_bytes': p['size_bytes']}
                    for r, nm, p in zip(chunk, names[i:i + 500], pool.map(lambda r: media.probe(source, r), chunk))]
            st.add_media(rows)
    print(f'import scan of {n} local images: {ms(t0) / 1000:.2f} s  (budget < 15 s)')
    ids = st.media_ids()

    # ---- autosave PUT, 200 shapes
    cls = det['classes'][0]['id']
    times, v = [], 0
    shapes = boxes(cls, 200)
    for k in range(30):
        for s in shapes[:10]:                         # a drag moved 10 shapes
            s['geometry'] = dict(s['geometry'], x=round(s['geometry']['x'] + 0.001, 7))
        t0 = time.perf_counter()
        v = st.put_annotations(ids[0], det['id'], shapes, v)['version']
        times.append(ms(t0))
    first = st.put_annotations(ids[1], det['id'], boxes(cls, 200), 0)
    t0 = time.perf_counter()
    st.put_annotations(ids[2], det['id'], boxes(cls, 200), 0)
    print(f'autosave PUT 200 shapes (store, 10 changed): {stats(times)}; 200 new: {ms(t0):.1f} ms  (budget < 30 ms)'
          if first else '')

    from fastapi.testclient import TestClient
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.server.app import Ctx, create_app
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    cfg['logging']['http'] = False
    app = create_app(Ctx(None, cfg=cfg, jobs=JobManager(), labeling=reg))
    app.add_middleware(_TestUserMiddleware, user=user)
    client = TestClient(app)
    times = []
    for k in range(30):
        for s in shapes[:10]:
            s['geometry'] = dict(s['geometry'], y=round(s['geometry']['y'] + 0.001, 7))
        t0 = time.perf_counter()
        r = client.put(f'/api/label/projects/{pid}/annotations/{ids[0]}',
                       json={'task_id': det['id'], 'base_version': v, 'annotations': shapes})
        times.append(ms(t0))
        v = r.json()['version']
    print(f'autosave PUT 200 shapes (HTTP route incl. client + JSON): {stats(times)}')

    # ---- labels everywhere (for the export), then media pages
    t0 = time.perf_counter()
    house = seg['classes'][0]['id']
    for i, mid in enumerate(ids):
        st.put_annotations(mid, det['id'], boxes(cls, 3), 0) if i > 2 else None
        st.put_annotations(mid, seg['id'], [{'id': uuid.uuid4().hex, 'class_id': house, 'geometry_type': 'polygon',
                                             'geometry': {'points': [[.1, .1], [.5, .1], [.4, .6], [.1, .5]]}}], 0)
        if i % 2:
            st.set_status(mid, [det['id'], seg['id']], 'commit')
    print(f'labeled {n} images x 2 tasks in {ms(t0) / 1000:.1f} s')
    for label, kw in (('first page', {}), ('page at offset n-200', {'offset': n - 200}),
                      ('filtered done', {'task_id': det['id'], 'status': 'done', 'offset': n // 4})):
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            st.media_page(limit=200, **kw)
            times.append(ms(t0))
        print(f'media page 200 of {n} ({label}): {stats(times)}  (budget < 20 ms)')
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        client.get(f'/api/label/projects/{pid}/media', params={'limit': 200, 'offset': n // 2})
        times.append(ms(t0))
    print(f'media page via HTTP: {stats(times)}')

    # ---- export, local folder, with images
    proj = reg.project(pid)
    cache = media.ImageCache(reg.project_dir(pid), source)
    tracemalloc.start()
    t0 = time.perf_counter()
    res = run_export(None, proj, st, sinks.make_sink({'kind': 'local', 'path': str(tmp / 'out')}, None, {}, tmp),
                     {'include_images': True}, image_path=lambda m: cache.path(m['uri']))
    took = ms(t0) / 1000
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    n_txt = sum(t['n_media'] for t in res['tasks'].values())
    print(f'export {n} images x 2 tasks to a local folder: {took:.2f} s, {n_txt} label files, '
          f'python heap peak {peak / 1e6:.1f} MB  (budget < 30 s, flat memory)')
    tracemalloc.start()
    t0 = time.perf_counter()
    res = run_export(None, proj, st, sinks.make_sink({'kind': 'zip'}, None, {}, tmp / 'zip'),
                     {'include_images': True}, image_path=lambda m: cache.path(m['uri']))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f'export {n} images x 2 tasks to a zip: {ms(t0) / 1000:.2f} s, {res["bytes"] / 1e6:.1f} MB, '
          f'python heap peak {peak / 1e6:.1f} MB')
    print(f'(temp files left in {tmp})')


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10000)
