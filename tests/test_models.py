"""Model-assisted labeling against two fake model servers (real HTTP on threads, free ports):
a generic dataviz-label/1 server and a fake of the YOLO serving API."""

import io
import socket
import threading
import time
import uuid

import pytest
import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile

from conftest import make_jpeg, wait_job

pytest.importorskip('dataviz.labeling.media')

KEY = 'sekrit-key-123'


def _size(data):
    from PIL import Image
    return Image.open(io.BytesIO(data)).size


def generic_app():
    app = FastAPI()
    app.state.delay = 0.0
    app.state.calls = 0
    app.state.fail = False

    def auth(h):
        if h != f'Bearer {KEY}':
            raise HTTPException(401, 'bad key')

    @app.get('/info')
    def info(authorization: str | None = Header(None)):
        auth(authorization)
        return {'protocol': 'dataviz-label/1', 'models': [
            {'id': 'det', 'name': 'Pets', 'task_type': 'object_detection', 'classes': ['cat', 'dog', 'horse']},
            {'id': 'seg', 'name': 'Houses', 'task_type': 'segmentation', 'classes': ['house']}]}

    @app.post('/predict')
    def predict(file: UploadFile = File(...), model: str = Query(...), conf: float = Query(0.25),
                authorization: str | None = Header(None)):
        auth(authorization)
        app.state.calls += 1
        if app.state.fail:
            raise HTTPException(500, 'model crashed')
        if app.state.delay:
            time.sleep(app.state.delay)
        w, h = _size(file.file.read())
        if model == 'seg':
            return {'predictions': [{'class_name': 'house', 'score': 0.8, 'geometry_type': 'polygon',
                                     'geometry': {'points': [[0.1, 0.1], [0.4, 0.1], [0.3, 0.5]]}}]}
        preds = [
            {'class_name': 'cat', 'score': 0.91, 'geometry_type': 'bbox', 'geometry': {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.25}},
            {'class_name': 'dog', 'score': 0.5, 'geometry_type': 'bbox', 'geometry': {'x': 0.5, 'y': 0.5, 'w': 0.9, 'h': 0.9}},
            {'class_name': 'horse', 'score': 0.7, 'geometry_type': 'bbox', 'geometry': {'x': 0.2, 'y': 0.2, 'w': 0.1, 'h': 0.1}},
            {'class_name': 'cat', 'score': 0.4, 'geometry_type': 'bbox', 'geometry': {'x': 0.2, 'y': 0.2, 'w': 0, 'h': 0.1}},
            {'class_name': 'zebra', 'score': 0.9, 'geometry_type': 'bbox', 'geometry': {'x': 0.0, 'y': 0.0, 'w': 0.1, 'h': 0.1}},
        ]
        return {'predictions': [p for p in preds if p['score'] >= conf]}

    return app


def serving_app():
    app = FastAPI()
    app.state.single = 0
    app.state.batch = 0

    @app.get('/')
    def listing():
        return [{'route': 'yolo/obd/fake-640', 'versions': ['v1'], 'imgsz': 640, 'backend': 'pt', 'precision': 'fp16',
                 'end2end_nms': False, 'classes': {'0': 'cat', '1': 'dog'}}]

    def dets(w, h):
        return [{'box': [w * 0.1, h * 0.2, w * 0.5, h * 0.6], 'score': 0.9, 'class_id': 0, 'class_name': 'cat'},
                {'box': [w * 0.6, h * 0.6, w * 1.2, h * 0.9], 'score': 0.6, 'class_id': 1, 'class_name': 'dog'}]

    @app.post('/{version}/yolo/obd/fake-640/predict')
    async def predict(version: str, request: Request):
        if version != 'v1':
            raise HTTPException(404, 'version')
        app.state.single += 1
        w, h = _size(await request.body())
        d = dets(w, h)
        return {'model': 'yolo/obd/fake-640', 'version': version, 'image': {'width': w, 'height': h},
                'count': len(d), 'detections': d}

    @app.post('/{version}/yolo/obd/fake-640/predict/batch')
    async def batch(version: str, files: list[UploadFile] = File(...)):
        app.state.batch += 1
        out = []
        for f in files:
            w, h = _size(await f.read())
            out.append({'filename': f.filename, 'count': 2, 'detections': dets(w, h)})
        return {'model': 'yolo/obd/fake-640', 'version': version, 'images': len(files), 'results': out}

    return app


def _serve(app):
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False, ws='none'))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    t0 = time.time()
    while not server.started:
        if time.time() - t0 > 10:
            raise RuntimeError('fake model server did not start')
        time.sleep(0.02)
    return server, f'http://127.0.0.1:{port}'


@pytest.fixture(scope='module')
def servers():
    g, s = generic_app(), serving_app()
    (gs, gurl), (ss, surl) = _serve(g), _serve(s)
    yield {'generic': (g, gurl), 'serving': (s, surl)}
    gs.should_exit = ss.should_exit = True
    from dataviz.labeling import models
    models.close_clients()


# ---------------------------------------------------------------- adapters
def test_detect_both_protocols_and_errors(servers):
    from dataviz.labeling import models
    gurl, surl = servers['generic'][1], servers['serving'][1]
    g = models.detect({'url': gurl, 'api_key': KEY}, fresh=True)
    assert g['protocol'] == 'generic'
    assert [(m['id'], m['task_type'], m['geometry']) for m in g['models']] == \
        [('det', 'object_detection', 'bbox'), ('seg', 'segmentation', 'polygon')]
    assert g['models'][0]['classes'] == ['cat', 'dog', 'horse']
    s = models.detect({'url': surl}, fresh=True)
    assert s == {'protocol': 'serving', 'models': [{'id': 'yolo/obd/fake-640', 'name': 'yolo/obd/fake-640',
                                                     'task_type': 'object_detection', 'geometry': 'bbox',
                                                     'route_type': 'obd', 'classes': ['cat', 'dog'], 'versions': ['v1'],
                                                     'imgsz': 640}]}
    with pytest.raises(models.ModelError, match='API key'):
        models.detect({'url': gurl, 'api_key': 'wrong'}, fresh=True)
    with pytest.raises(models.ModelError, match='not a supported model API'):
        models.detect({'url': surl + '/nothing'}, fresh=True)
    s_ = socket.socket()
    s_.bind(('127.0.0.1', 0))
    dead = s_.getsockname()[1]
    s_.close()
    with pytest.raises(models.ModelError, match='cannot reach'):
        models.detect({'url': f'http://127.0.0.1:{dead}', 'timeout_s': 2}, fresh=True)


def test_serving_models_are_always_boxes():
    """The serving API answers with boxes only, so a seg / pose / cls route is still a bbox detector (the route
    type stays visible as route_type)."""
    from dataviz.labeling import models
    out = models._serving_models([{'route': r, 'versions': ['v1'], 'classes': ['a']}
                                  for r in ('yolo/seg/x', 'yolo/pose/y', 'yolo/cls/z', '/plain/')])
    assert [(m['id'], m['task_type'], m['geometry'], m['route_type']) for m in out] == [
        ('yolo/seg/x', 'object_detection', 'bbox', 'seg'), ('yolo/pose/y', 'object_detection', 'bbox', 'pose'),
        ('yolo/cls/z', 'object_detection', 'bbox', 'cls'), ('plain', 'object_detection', 'bbox', '')]


def test_adapters_predict(servers):
    from dataviz.labeling import models
    from conftest import jpeg_bytes
    img = jpeg_bytes(200, 100)
    g = models.adapter({'url': servers['generic'][1], 'api_key': KEY}, 'det')
    preds = g.predict(img, 0.3)
    # the zero-width box is dropped, the overflowing dog box is clamped to the image
    assert [(p['class_name'], p['score']) for p in preds] == [('cat', 0.91), ('dog', 0.5), ('horse', 0.7), ('zebra', 0.9)]
    assert preds[1]['geometry'] == {'x': 0.5, 'y': 0.5, 'w': 0.5, 'h': 0.5}
    s = models.adapter({'url': servers['serving'][1]}, 'yolo/obd/fake-640')
    p = s.predict(img)
    assert p[0]['geometry'] == pytest.approx({'x': 0.1, 'y': 0.2, 'w': 0.4, 'h': 0.4})
    assert p[1]['geometry'] == pytest.approx({'x': 0.6, 'y': 0.6, 'w': 0.4, 'h': 0.3})     # clamped at the right edge
    out = s.predict_batch([(img, (200, 100), 'a.jpg'), (jpeg_bytes(50, 50), (50, 50), 'b.jpg')])
    assert len(out) == 2 and out[1][0]['geometry'] == pytest.approx({'x': 0.1, 'y': 0.2, 'w': 0.4, 'h': 0.4})


def test_clean_predictions_validation_and_cap():
    from dataviz.labeling import models
    raw = [{'class_name': 'a', 'score': 0.5, 'geometry_type': 'bbox', 'geometry': {'x': -0.2, 'y': 0.1, 'w': 0.5, 'h': 2}},
           {'class_name': 'a', 'score': float('nan'), 'geometry_type': 'bbox', 'geometry': {'x': float('nan'), 'y': 0, 'w': 1, 'h': 1}},
           {'class_name': 'a', 'geometry_type': 'polygon', 'geometry': {'points': [[0, 0], [1, 1]]}},
           {'class_name': '', 'geometry_type': 'tag'},
           {'class_name': 'b', 'geometry_type': 'cube', 'geometry': {}},
           {'class_name': 'c', 'score': 3, 'geometry_type': 'tag', 'geometry': None},
           {'class_name': 'px', 'score': 0.2, 'geometry_type': 'bbox', 'geometry': {'x': 10, 'y': 10, 'w': 20, 'h': 40}}]
    out = models.clean_predictions(raw[:6])
    assert out == [{'class_name': 'a', 'score': 0.5, 'geometry_type': 'bbox', 'geometry': {'x': 0.0, 'y': 0.1, 'w': 0.3, 'h': 0.9}},
                   {'class_name': 'c', 'score': 1.0, 'geometry_type': 'tag', 'geometry': {}}]
    px = models.clean_predictions(raw[6:], (100, 200), 'px')
    assert px[0]['geometry'] == {'x': 0.1, 'y': 0.05, 'w': 0.2, 'h': 0.2}
    many = [{'class_name': 'a', 'score': i / 2000, 'geometry_type': 'bbox', 'geometry': {'x': 0, 'y': 0, 'w': .1, 'h': .1}}
            for i in range(1500)]
    out = models.clean_predictions(many)
    assert len(out) == models.MAX_SHAPES and min(p['score'] for p in out) >= 0.25


# ---------------------------------------------------------------- routes
def _session(client, tmp_path, n=3, tasks=None):
    src = tmp_path / f'pics{uuid.uuid4().hex[:6]}'
    for i in range(n):
        make_jpeg(src / f'{i:04d}.jpg', 40 + i % 7, 30)
    tasks = tasks or [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['Cat', 'dog']},
                      {'name': 'Segment', 'task_type': 'segmentation', 'classes': ['house']}]
    out = client.post('/api/label/projects', json={'name': 'M', 'source': {'kind': 'local', 'path': str(src)},
                                                   'tasks': tasks}).json()
    assert wait_job(client, out['job_id'])['status'] == 'done'
    pid = out['project']['id']
    return pid, out['project']['tasks'], client.get(f'/api/label/projects/{pid}/media-ids').json()['ids']


def _activate(client, pid, tid, on=True):
    r = client.post(f'/api/label/projects/{pid}/tasks/{tid}/model/activate', json={'on': on})
    assert r.status_code == 200, r.text
    assert r.json()['active'] is on


def _conn(client, url, **kw):
    r = client.post('/api/connections', json={'kind': 'model', 'name': 'm', 'url': url + '/', **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_connection_kind_model(client, servers):
    gurl = servers['generic'][1]
    c = _conn(client, gurl, api_key=KEY)
    assert c == {'id': c['id'], 'kind': 'model', 'name': 'm', 'url': gurl, 'has_secret': True, 'timeout_s': 60}
    assert KEY not in client.get('/api/connections').text
    t = client.post('/api/connections/test', json={'id': c['id'], 'kind': 'model', 'api_key': ''}).json()
    assert t['ok'] and t['protocol'] == 'generic' and len(t['models']) == 2
    bad = client.post('/api/connections/test', json={'kind': 'model', 'url': gurl, 'api_key': 'nope'})
    assert bad.status_code == 400 and 'API key' in bad.json()['detail']
    assert client.post('/api/connections', json={'kind': 'model', 'url': 'ftp://x'}).status_code == 400
    d = client.post('/api/label/models/detect', json={'connection': {'url': servers['serving'][1]}}).json()
    assert d['protocol'] == 'serving'
    d = client.post('/api/label/models/detect', json={'connection_id': c['id']}).json()
    assert d['protocol'] == 'generic'
    assert client.post('/api/label/models/detect', json={}).status_code == 400


def test_link_predict_edit_remove(client, servers, tmp_path):
    pid, (det, seg), ids = _session(client, tmp_path)
    c = _conn(client, servers['generic'][1], api_key=KEY)
    base = f'/api/label/projects/{pid}'
    cat, dog = det['classes']
    # a segmentation model cannot feed a detection task
    r = client.put(f"{base}/tasks/{det['id']}/model", json={'connection_id': c['id'], 'model_id': 'seg', 'class_map': {}})
    assert r.status_code == 400 and 'segmentation' in r.json()['detail']
    # predicting before a link is a 400
    assert client.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']}).status_code == 400
    link = {'connection_id': c['id'], 'model_id': 'det', 'conf': 0.3, 'auto_on_open': True,
            'class_map': {'cat': cat['id'], 'dog': 'ignore', 'horse': 'new'}}
    t = client.put(f"{base}/tasks/{det['id']}/model", json=link).json()
    horse = [x for x in t['classes'] if x['name'] == 'horse'][0]
    assert t['config']['model']['class_map'] == {'cat': cat['id'], 'dog': 'ignore', 'horse': horse['id']}
    assert t['config']['model']['auto_on_open'] is True
    # the connection is now in use by the session
    assert client.delete(f"/api/connections/{c['id']}").status_code == 400
    # linked is not enough: nothing runs until the model is activated for this session
    r = client.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']})
    assert r.status_code == 409 and 'activate' in r.json()['detail']
    assert client.post(f'{base}/predict-all', json={'task_id': det['id']}).status_code == 409
    assert client.get(base).json()['model_active'] == []
    _activate(client, pid, det['id'])
    assert client.get(base).json()['model_active'] == [det['id']]

    # a manual box first, then the model
    manual = {'id': uuid.uuid4().hex, 'class_id': dog['id'], 'geometry_type': 'bbox',
              'geometry': {'x': .6, 'y': .1, 'w': .2, 'h': .2}}
    assert client.put(f'{base}/annotations/{ids[0]}', json={'task_id': det['id'], 'base_version': 0,
                                                            'annotations': [manual]}).json()['version'] == 1
    p = client.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']}).json()
    assert p['version'] == 2 and p['n_model'] == 2 and p['ms'] > 0
    model_rows = [a for a in p['annotations'] if a['source'] == 'model']
    # cat (mapped) + horse (new class); dog ignored; zebra unknown; the zero-width cat dropped
    assert sorted((a['class_id'], a['score']) for a in model_rows) == sorted([(cat['id'], 0.91), (horse['id'], 0.7)])
    assert [a['id'] for a in p['annotations'] if a['source'] == 'manual'] == [manual['id']]
    # again: model rows are replaced, not added
    p2 = client.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id'], 'conf': 0.8}).json()
    assert p2['version'] == 3 and p2['n_model'] == 1 and len(p2['annotations']) == 2
    got = client.get(f'{base}/annotations/{ids[0]}').json()
    assert got['versions'][det['id']] == 3 and got['media']['model_run'][det['id']] > 0
    assert got['media']['model_run'][seg['id']] is None and got['media']['status'][det['id']] == 'in_progress'

    # editing a model shape makes it manual (server safety net); an untouched one stays a model shape
    rows = p2['annotations']
    m = [a for a in rows if a['source'] == 'model'][0]
    moved = dict(m, geometry={'x': .3, 'y': .3, 'w': .1, 'h': .1})
    r = client.put(f'{base}/annotations/{ids[0]}', json={'task_id': det['id'], 'base_version': 3,
                                                         'annotations': [moved, *[a for a in rows if a is not m]]})
    assert r.status_code == 200
    now = {a['id']: a for a in client.get(f'{base}/annotations/{ids[0]}').json()['annotations']}
    assert now[m['id']]['source'] == 'manual' and now[m['id']]['score'] is None

    # remove model labels: the image's model rows go (none left here), manual rows stay
    p3 = client.post(f'{base}/predict/{ids[1]}', json={'task_id': det['id']}).json()
    assert p3['n_model'] == 2
    r = client.post(f'{base}/remove-model-labels', json={'task_id': det['id'], 'media_ids': [ids[1]]}).json()
    assert r == {'removed': 2, 'images': 1}
    item = client.get(f'{base}/annotations/{ids[1]}').json()
    assert item['annotations'] == [] and item['media']['status'][det['id']] == 'todo'
    assert item['versions'][det['id']] == 2
    client.post(f'{base}/predict/{ids[2]}', json={'task_id': det['id']})
    r = client.post(f'{base}/remove-model-labels', json={'task_id': det['id']}).json()
    assert r == {'removed': 2, 'images': 1}
    assert sorted(a['id'] for a in client.get(f'{base}/annotations/{ids[0]}').json()['annotations']) == \
        sorted([manual['id'], m['id']])

    # unlink
    t = client.put(f"{base}/tasks/{det['id']}/model", content=b'null', headers={'content-type': 'application/json'})
    assert t.status_code == 200 and 'model' not in t.json()['config']


def test_predict_all_serving_batches(client, servers, tmp_path):
    app = servers['serving'][0]
    pid, (det, _), ids = _session(client, tmp_path, n=21)
    c = _conn(client, servers['serving'][1])
    base = f'/api/label/projects/{pid}'
    cat, dog = det['classes']
    r = client.put(f"{base}/tasks/{det['id']}/model", json={'connection_id': c['id'], 'model_id': 'yolo/obd/fake-640',
                                                           'class_map': {'cat': cat['id'], 'dog': dog['id']}})
    assert r.status_code == 200, r.text
    _activate(client, pid, det['id'])
    # image 0 is done (skipped by not_done), image 1 has a manual shape (skipped by unlabeled)
    client.post(f'{base}/commit/{ids[0]}', json={'task_ids': [det['id']]})
    manual = {'id': uuid.uuid4().hex, 'class_id': dog['id'], 'geometry_type': 'bbox',
              'geometry': {'x': .6, 'y': .1, 'w': .2, 'h': .2}}
    client.put(f'{base}/annotations/{ids[1]}', json={'task_id': det['id'], 'base_version': 0, 'annotations': [manual]})
    b0, s0 = app.state.batch, app.state.single
    j = wait_job(client, client.post(f'{base}/predict-all', json={'task_id': det['id']}).json()['job_id'])
    assert j['status'] == 'done', j
    assert j['result'] == {'n_images': 19, 'n_shapes': 38, 'failed': 0, 'cancelled': False,
                           'seconds': j['result']['seconds']}
    assert app.state.batch - b0 == 3 and app.state.single == s0              # 19 images = 8 + 8 + 3, batch calls only
    j = wait_job(client, client.post(f'{base}/predict-all', json={'task_id': det['id'], 'scope': 'not_done'}).json()['job_id'])
    assert j['result']['n_images'] == 20
    one = client.get(f'{base}/annotations/{ids[1]}').json()['annotations']
    assert sum(a['source'] == 'manual' for a in one) == 1 and sum(a['source'] == 'model' for a in one) == 2
    j = wait_job(client, client.post(f'{base}/predict-all', json={'task_id': det['id'], 'scope': 'all'}).json()['job_id'])
    assert j['result']['n_images'] == 21
    st = client.get(f'{base}/media', params={'limit': 50}).json()['items']
    assert st[0]['status'][det['id']] == 'done' and all(m['model_run'][det['id']] for m in st)
    assert client.post(f'{base}/predict-all', json={'task_id': det['id'], 'scope': 'x'}).status_code == 400


def test_predict_all_cancel_keeps_what_it_wrote(client, servers, tmp_path):
    app = servers['generic'][0]
    pid, (det, _), ids = _session(client, tmp_path, n=120)
    c = _conn(client, servers['generic'][1], api_key=KEY)
    base = f'/api/label/projects/{pid}'
    client.put(f"{base}/tasks/{det['id']}/model", json={'connection_id': c['id'], 'model_id': 'det', 'class_map': {}})
    _activate(client, pid, det['id'])
    app.state.delay = 0.02
    try:
        jid = client.post(f'{base}/predict-all', json={'task_id': det['id']}).json()['job_id']
        t0 = time.time()
        while client.get(f'/api/jobs/{jid}').json()['done'] < 20 and time.time() - t0 < 20:
            time.sleep(0.02)
        assert client.post(f'/api/jobs/{jid}/cancel').json() == {'ok': True}
        j = wait_job(client, jid)
    finally:
        app.state.delay = 0.0
    assert j['status'] == 'done' and j['cancelled'] and j['result']['cancelled'] is True
    n = j['result']['n_images']
    assert 20 <= n < 120
    items = client.get(f'{base}/media', params={'limit': 200}).json()['items']
    ran = [m for m in items if m['model_run'][det['id']]]
    assert len(ran) == n and all(m['n'][det['id']] == 2 for m in ran)       # 'Cat' (any case) + dog, by name
    assert client.post('/api/jobs/999999/cancel').status_code == 404


def test_predict_all_server_failing_fails_fast(client, servers, tmp_path):
    app = servers['generic'][0]
    pid, (det, _), ids = _session(client, tmp_path, n=200)
    c = _conn(client, servers['generic'][1], api_key=KEY)
    base = f'/api/label/projects/{pid}'
    client.put(f"{base}/tasks/{det['id']}/model", json={'connection_id': c['id'], 'model_id': 'det', 'class_map': {}})
    _activate(client, pid, det['id'])
    app.state.fail = True
    try:
        calls = app.state.calls
        j = wait_job(client, client.post(f'{base}/predict-all', json={'task_id': det['id']}).json()['job_id'])
        assert j['status'] == 'error' and 'every image' in j['error'] and '500' in j['error']
        assert app.state.calls - calls < 100                 # gave up early, not 200 failed calls
        r = client.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']})
        assert r.status_code == 502 and 'model crashed' in r.json()['detail']
    finally:
        app.state.fail = False
    # the key changes after linking: detect is refused, a clear 400
    client.post('/api/connections', json={'id': c['id'], 'kind': 'model', 'api_key': 'wrong'})
    r = client.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']})
    assert r.status_code == 400 and 'API key' in r.json()['detail']
    j = wait_job(client, client.post(f'{base}/predict-all', json={'task_id': det['id']}).json()['job_id'])
    assert j['status'] == 'error' and 'API key' in j['error']


def test_predict_all_unlabeled_skips_images_labeled_after_listing(client, servers, tmp_path, monkeypatch):
    """The job lists its images up front; one the user labels / skips meanwhile must not get model shapes.
    Simulated with a listing that ignores the scope (as if every image was still unlabeled when listed)."""
    from dataviz.labeling.store import ProjectStore
    pid, (det, _), ids = _session(client, tmp_path, n=3)
    c = _conn(client, servers['generic'][1], api_key=KEY)
    base = f'/api/label/projects/{pid}'
    cat, dog = det['classes']
    client.put(f"{base}/tasks/{det['id']}/model", json={'connection_id': c['id'], 'model_id': 'det', 'class_map': {}})
    _activate(client, pid, det['id'])
    manual = {'id': uuid.uuid4().hex, 'class_id': dog['id'], 'geometry_type': 'bbox',
              'geometry': {'x': .6, 'y': .1, 'w': .2, 'h': .2}}
    client.put(f'{base}/annotations/{ids[0]}', json={'task_id': det['id'], 'base_version': 0, 'annotations': [manual]})
    client.post(f'{base}/skip/{ids[1]}', json={'task_ids': [det['id']]})
    orig = ProjectStore.predict_targets
    monkeypatch.setattr(ProjectStore, 'predict_targets', lambda self, tid, scope='unlabeled', batch=500:
                        orig(self, tid, 'all', batch))
    j = wait_job(client, client.post(f'{base}/predict-all', json={'task_id': det['id']}).json()['job_id'])
    assert j['status'] == 'done' and j['result']['n_images'] == 1, j
    assert [a['id'] for a in client.get(f'{base}/annotations/{ids[0]}').json()['annotations']] == [manual['id']]
    assert client.get(f'{base}/annotations/{ids[1]}').json()['annotations'] == []


def test_deleted_class_in_model_map_is_ignored(client, servers, tmp_path):
    """Deleting a mapped class maps it to 'ignore'; a later link save that re-sends the stale map (toggling
    auto_on_open) is accepted with the stale id as 'ignore' instead of a 400."""
    pid, (det, _), _ = _session(client, tmp_path)
    c = _conn(client, servers['generic'][1], api_key=KEY)
    base = f'/api/label/projects/{pid}'
    cat, dog = det['classes']
    link = {'connection_id': c['id'], 'model_id': 'det', 'class_map': {'cat': cat['id'], 'dog': dog['id']}}
    assert client.put(f"{base}/tasks/{det['id']}/model", json=link).status_code == 200
    assert client.post(f'{base}/classes/delete', json={'class_ids': [dog['id']]}).status_code == 200
    t = [x for x in client.get(f'/api/label/projects/{pid}').json()['tasks'] if x['id'] == det['id']][0]
    assert t['config']['model']['class_map'] == {'cat': cat['id'], 'dog': 'ignore'}
    _activate(client, pid, det['id'])
    r = client.put(f"{base}/tasks/{det['id']}/model", json={**link, 'auto_on_open': True})
    assert r.status_code == 200, r.text
    assert client.get(base).json()['model_active'] == []          # a saved link must be activated again
    assert r.json()['config']['model']['class_map'] == {'cat': cat['id'], 'dog': 'ignore'}
