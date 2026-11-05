"""Label-all inference in its own process (models/proc.py): the worker reads and decodes the files itself, answers
in the labeling predictions shape (normalised boxes), fails one unreadable image alone, keeps the model's class
names, and goes away when idle; the Data Center's autolabel POST returns before the model is even looked at."""

import time

import pytest

from conftest import make_jpeg

from test_dc_routes import dc, new_bucket, upload_zip  # noqa: F401 - the Data Center fixture

from dataviz.models import proc

ADAPTER = '''
import numpy as np
CLASS_NAMES = ['cat', 'dog']

def build_model(path, device):
    return lambda x: x

def preprocess(images, meta):
    return np.zeros((len(images), 3, 8, 8), np.float32)

def postprocess(out, meta):
    # one box over the top-left quarter of every image, class 1, score 0.9; one below the threshold
    return [np.array([[0, 0, w / 2, h / 2, 0.9, 1], [0, 0, 1, 1, 0.01, 0]], np.float32) for w, h in meta['orig_sizes']]
'''


@pytest.fixture
def stub(tmp_path):
    ad = tmp_path / 'stub_adapter.py'
    ad.write_text(ADAPTER)
    w = tmp_path / 'w.pt'
    w.write_bytes(b'')
    return {'id': 'stub', 'name': 'stub', 'family': 'custom', 'format': 'pt', 'path': str(w), 'adapter': str(ad),
            'imgsz': 64}


def test_worker_process_labels_files(stub, tmp_path):
    paths = []
    for i in range(5):
        p = tmp_path / f'{i}.jpg'
        make_jpeg(p, 80, 40)
        paths.append(str(p))
    bad = tmp_path / 'bad.jpg'
    bad.write_bytes(b'not an image')
    mp = proc.ModelProcess(idle_s=60)
    try:
        assert mp.names(stub, 'fp1', str(tmp_path)) == ['cat', 'dog']
        out = mp.predict_files(stub, 'fp1', str(tmp_path), paths + [str(bad)], 0.25)
        assert len(out) == 6
        for preds in out[:5]:
            assert len(preds) == 1 and preds[0]['class_name'] == 'dog'
            g = preds[0]['geometry']
            assert abs(g['x']) < 1e-6 and abs(g['w'] - 0.5) < 1e-3 and abs(g['h'] - 0.5) < 1e-3
        assert isinstance(out[5], Exception) and 'cannot read' in str(out[5])
    finally:
        mp.shutdown()


def test_idle_worker_goes_away(stub, tmp_path):
    p = tmp_path / 'a.jpg'
    make_jpeg(p, 32, 32)
    mp = proc.ModelProcess(idle_s=1)
    try:
        mp.predict_files(stub, 'fp', str(tmp_path), [str(p)])
        assert mp._pool is not None
        t0 = time.time()
        while mp._pool is not None and time.time() - t0 < 10:
            time.sleep(0.2)
        assert mp._pool is None
        assert mp.predict_files(stub, 'fp', str(tmp_path), [str(p)])[0]      # starts again on the next call
    finally:
        mp.shutdown()


def test_autolabel_post_does_not_wait_for_the_model(dc, monkeypatch):  # noqa: F811
    """The model may take seconds to load: the POST must not wait for it (the page froze while it did)."""
    from conftest import wait_job
    from dataviz.datacenter import service
    _, client = dc
    b = new_bucket(client)
    upload_zip(client, b['id'])

    def slow_info(link, owner):
        time.sleep(3)
        raise service.DCError('the model is broken')
    monkeypatch.setattr(service._hooks(), 'model_info', slow_info)
    t0 = time.time()
    r = client.post('/api/dc/autolabel', json={'bucket': b['id'], 'model': {'registry_id': 'x', 'name': 'Slow'}})
    took = time.time() - t0
    assert r.status_code == 200, r.text
    assert took < 1.5, took
    j = wait_job(client, r.json()['job_id'])
    assert j['status'] == 'error' and 'broken' in j['error']
