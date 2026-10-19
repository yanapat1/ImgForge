"""One model registry for both pages (models/hub.py): a Settings › Models model labels images on the labeling page
(a dataviz-label/1 API model, and local weights through the shared runner), only after it is activated; a
dataviz-label/1 model is also scored by the visualizer (remote.LabelApiRunner); models are tested without a session."""

import uuid

import numpy as np
import pytest
from PIL import Image

from conftest import _TestUserMiddleware, make_jpeg, make_user, wait_job
from test_models import KEY, servers  # noqa: F401 - the fake model servers (module fixture)

from dataviz.models import hub, runners


@pytest.fixture
def mclient(pg, tmp_path):
    from fastapi.testclient import TestClient
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.data import connections
    from dataviz.labeling.projects import ProjectRegistry
    from dataviz.models.registry import ModelRegistry
    from dataviz.models.service import ModelService
    from dataviz.server.app import Ctx, create_app
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    cfg['logging']['http'] = False
    root = tmp_path / 'ws'
    reg = ModelRegistry(root)
    ctx = Ctx(None, datasets=connections.Registry(root), cfg=cfg, jobs=JobManager(), labeling=ProjectRegistry(root / 'labeling'),
              models=reg, model_service=ModelService(reg, cfg.get('models') or {}))
    app = create_app(ctx)
    app.add_middleware(_TestUserMiddleware, user=make_user('admin', admin=True))
    with TestClient(app) as c:
        c.ctx = ctx
        yield c


def _session(client, tmp_path, n=3):
    src = tmp_path / f'pics{uuid.uuid4().hex[:6]}'
    for i in range(n):
        make_jpeg(src / f'{i:04d}.jpg', 40, 30)
    out = client.post('/api/label/projects', json={'name': 'H', 'source': {'kind': 'local', 'path': str(src)},
                                                   'tasks': [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat']}]}).json()
    assert wait_job(client, out['job_id'])['status'] == 'done'
    pid = out['project']['id']
    return pid, out['project']['tasks'][0], client.get(f'/api/label/projects/{pid}/media-ids').json()['ids']


def test_label_api_model_from_the_registry(mclient, servers, tmp_path):  # noqa: F811
    gurl = servers['generic'][1]
    r = mclient.post('/api/models', json={'name': 'Pets API', 'family': 'custom', 'format': 'api', 'task_type': 'segmentation',
                                          'api': {'preset': 'label', 'url': gurl, 'api_key': KEY, 'label_model': 'det'}})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m['api']['preset'] == 'label' and m['task_type'] == 'segmentation' and KEY not in r.text
    # tested with no session open: it answers and lists its classes
    t = mclient.post(f"/api/models/{m['id']}/test").json()
    assert t['ok'] and t['no_session'] and t['names'] == ['cat', 'dog', 'horse']
    d = mclient.post('/api/label/models/detect', json={'registry_id': m['id']}).json()
    assert d['protocol'] == 'registry' and d['models'][0]['id'] == m['id'] and d['models'][0]['task_type'] == 'object_detection'
    pid, det, ids = _session(mclient, tmp_path)
    base = f'/api/label/projects/{pid}'
    cat = det['classes'][0]
    link = {'registry_id': m['id'], 'model_id': m['id'], 'conf': 0.3, 'class_map': {'cat': cat['id'], 'dog': 'ignore', 'horse': 'new'}}
    t = mclient.put(f"{base}/tasks/{det['id']}/model", json=link)
    assert t.status_code == 200, t.text
    assert t.json()['config']['model']['registry_id'] == m['id']
    assert mclient.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']}).status_code == 409
    mclient.post(f"{base}/tasks/{det['id']}/model/activate", json={'on': True})
    p = mclient.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']}).json()
    assert p['n_model'] == 2                                         # cat + horse (new); dog ignored
    j = wait_job(mclient, mclient.post(f'{base}/predict-all', json={'task_id': det['id']}).json()['job_id'])
    assert j['status'] == 'done' and j['result']['n_images'] == 2, j
    mclient.post(f"{base}/tasks/{det['id']}/model/activate", json={'on': False})
    assert mclient.post(f'{base}/predict/{ids[1]}', json={'task_id': det['id']}).status_code == 409
    # the visualizer scores the same model: (N, 6) pixel boxes, class names learnt from the server
    r = runners.load_runner(mclient.ctx.models.model(m['id']), tmp_path)
    det6 = r.predict([Image.new('RGB', (100, 50))])[0]
    assert det6.shape[1] == 6 and r.names[:3] == ['cat', 'dog', 'horse']
    x1, y1, x2, y2, sc, c = det6[det6[:, 4] > 0.9][0]
    assert (round(x1), round(y1), round(x2), round(y2), int(c)) == (10, 10, 40, 22, 0)
    r.close()
    # deleting the model: a link to it now says so
    mclient.delete(f"/api/models/{m['id']}")
    mclient.post(f"{base}/tasks/{det['id']}/model/activate", json={'on': True})
    r = mclient.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']})
    assert r.status_code == 400 and 'no longer' in r.json()['detail']


class FakeRunner(runners.Runner):
    names = ['cat', 'dog']

    def __init__(self):
        self.seen = []

    def predict(self, images):
        self.seen.append([im.size for im in images])
        out = []
        for im in images:                                       # a confident cat + a weak dog per image
            w, h = im.size
            out.append(np.array([[0.1 * w, 0.1 * h, 0.5 * w, 0.5 * h, 0.9, 0], [0, 0, 1, 1, 0.1, 1]], np.float32))
        return out


def test_local_weights_label_through_the_shared_runner(mclient, tmp_path, monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(runners, 'load_runner', lambda m, workdir: fake)
    runners.release()
    w = mclient.ctx.models.store / 'x' / 'best.pt'
    w.parent.mkdir(parents=True)
    w.write_bytes(b'not really weights')
    m = mclient.post('/api/models', json={'name': 'local', 'family': 'yolo', 'format': 'pt', 'path': str(w)}).json()
    assert m['task_type'] == 'object_detection'
    d = mclient.post('/api/label/models/detect', json={'registry_id': m['id']}).json()
    assert d['models'][0]['classes'] == ['cat', 'dog']
    pid, det, ids = _session(mclient, tmp_path, n=10)
    base = f'/api/label/projects/{pid}'
    assert mclient.put(f"{base}/tasks/{det['id']}/model", json={'registry_id': m['id'], 'model_id': m['id'],
                                                                'class_map': {'cat': det['classes'][0]['id'], 'dog': 'ignore'}}).status_code == 200
    mclient.post(f"{base}/tasks/{det['id']}/model/activate", json={})
    p = mclient.post(f'{base}/predict/{ids[0]}', json={'task_id': det['id']}).json()
    a = [x for x in p['annotations'] if x['source'] == 'model']
    assert len(a) == 1 and a[0]['geometry'] == pytest.approx({'x': 0.1, 'y': 0.1, 'w': 0.4, 'h': 0.4})
    j = wait_job(mclient, mclient.post(f'{base}/predict-all', json={'task_id': det['id']}).json()['job_id'])
    assert j['result']['n_images'] == 9
    assert max(len(b) for b in fake.seen) == 8                       # label-all batches reach the runner as batches
    runners.release()


def test_hub_adapter_reports_unreadable_images(tmp_path, monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(runners, 'get_runner', lambda m, fp, wd: fake)
    ad = hub.RunnerAdapter({'id': 'x', 'name': 'x', 'format': 'pt'}, 'fp', tmp_path)
    import io
    buf = io.BytesIO()
    Image.new('RGB', (20, 10)).save(buf, 'PNG')
    out = ad.predict_batch([(b'garbage', (1, 1), 'a'), (buf.getvalue(), (20, 10), 'b')], 0.5)
    assert isinstance(out[0], Exception) and len(out[1]) == 1 and out[1][0]['class_name'] == 'cat'
