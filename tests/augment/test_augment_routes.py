"""The Augment tab's API on a small YOLO session."""

import sys

import pytest

from authutil import account, client, login
from vizutil import make_viz_app, make_yolo, viz_session

pytest.importorskip('albumentations')


@pytest.fixture
def world(pg, tmp_path):
    ds = make_yolo(tmp_path / 'ds', classes=('cat', 'dog'), boxes=(
        ('a', '0 0.5 0.5 0.4 0.4\n'), ('b', '1 0.3 0.3 0.2 0.2\n'), ('c', '0 0.6 0.6 0.3 0.3\n1 0.2 0.2 0.1 0.1\n')))
    alice = account('alice')
    sid = viz_session(alice['id'], 'ds', ds)
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    return {'c': c, 'sid': sid, 'ctx': ctx}


def test_catalog_preview_and_errors(world):
    c, sid = world['c'], world['sid']
    cat = c.get(f'/s/{sid}/api/augment/catalog').json()
    assert {'groups', 'transforms', 'presets'} <= set(cat) and len(cat['transforms']) > 30
    flip = {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}
    r = c.post(f'/s/{sid}/api/augment/preview', json={'recipe': flip, 'n_images': 2, 'variants': 3, 'seed': 1, 'size': 160})
    assert r.status_code == 200, r.text
    items = r.json()['items']
    assert len(items) == 2 and all(len(it['variants']) == 3 for it in items)
    v = items[0]['variants'][0]
    assert v['img'].startswith('data:image/jpeg;base64,') and v['applied'] == ['HorizontalFlip']
    assert len(v['boxes']) == len(items[0]['orig']['boxes'])
    per = c.post(f'/s/{sid}/api/augment/preview', json={'recipe': flip, 'pick': 'per_class', 'n_images': 2}).json()['items']
    assert len({it['name'] for it in per}) == 2
    named = c.post(f'/s/{sid}/api/augment/preview', json={'recipe': flip, 'pick': 'names', 'names': ['c.jpg'], 'n_images': 3}).json()['items']
    assert [it['name'] for it in named] == ['c.jpg']
    assert c.post(f'/s/{sid}/api/augment/preview', json={'recipe': {'steps': [{'t': 'Nope'}]}}).status_code == 400
    one = c.post(f'/s/{sid}/api/augment/one', json={'recipe': flip, 'name': 'a.jpg', 'seed': 2})
    assert one.status_code == 200 and one.headers['content-type'] == 'image/jpeg'
    assert c.post(f'/s/{sid}/api/augment/one', json={'recipe': flip, 'name': 'zzz.jpg'}).status_code == 404


def test_saved_recipes_export_and_import(world):
    c, sid = world['c'], world['sid']
    assert c.get(f'/s/{sid}/api/augment/recipes').json() == {'recipes': []}
    rec = {'steps': [{'t': 'MotionBlur', 'p': 0.4}]}
    got = c.put(f'/s/{sid}/api/augment/recipes', json={'recipes': [{'name': 'blurry', 'recipe': rec}]}).json()['recipes']
    assert got[0]['name'] == 'blurry' and got[0]['recipe']['steps'][0]['args']['blur_limit'] == [3, 9]
    assert c.get(f'/s/{sid}/api/augment/recipes').json()['recipes'][0]['name'] == 'blurry'
    assert c.put(f'/s/{sid}/api/augment/recipes', json={'recipes': [{'name': '', 'recipe': rec}]}).status_code == 400
    js = c.post(f'/s/{sid}/api/augment/export', json={'recipe': rec, 'format': 'albumentations'}).json()
    back = c.post(f'/s/{sid}/api/augment/import', json={'data': __import__('json').loads(js['text'])}).json()
    assert back['recipe']['steps'][0]['t'] == 'MotionBlur' and back['skipped'] == []
    assert 'A.MotionBlur' in c.post(f'/s/{sid}/api/augment/export', json={'recipe': rec, 'format': 'python'}).json()['text']


def test_without_albumentations_the_tab_says_how_to_get_it(world, monkeypatch):
    monkeypatch.setitem(sys.modules, 'albumentations', None)
    r = world['c'].get(f'/s/{world["sid"]}/api/augment/catalog')
    assert r.status_code == 503 and 'albumentations' in r.text


def test_box_check_runs_as_a_job_and_is_cached(world):
    from conftest import wait_job
    c, sid = world['c'], world['sid']
    body = {'recipe': {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}, 'm': 10, 'k': 1}
    r = c.post(f'/s/{sid}/api/augment/boxes', json=body).json()
    assert r['ready'] is False
    assert wait_job(c, r['job_id'])['status'] == 'done'
    res = c.get(f'/s/{sid}/api/augment/boxes/result', params={'key': r['key']}).json()['result']
    assert res['boxes_in'] == 4 and res['lost'] == 0 and res['images'] == 3
    again = c.post(f'/s/{sid}/api/augment/boxes', json=body).json()
    assert again['ready'] is True and again['result'] == res
    assert c.get(f'/s/{sid}/api/augment/boxes/result', params={'key': 'nope'}).status_code == 404


def test_coverage_against_an_uploaded_target_set(pg, tmp_path):
    import io
    import numpy as np
    from PIL import Image
    from conftest import wait_job
    rng = np.random.default_rng(3)
    root = tmp_path / 'scenes'
    (root / 'images').mkdir(parents=True)
    (root / 'labels').mkdir()
    (root / 'classes.txt').write_text('part\n')
    bright = []
    for k in range(14):
        a = np.full((96, 128, 3), 170, np.uint8)
        for _ in range(6):
            x, y = int(rng.integers(0, 100)), int(rng.integers(0, 70))
            a[y:y + 20, x:x + 24] = rng.integers(60, 255, 3)
        Image.fromarray(a).save(root / 'images' / f's{k:02d}.jpg', quality=92)
        (root / 'labels' / f's{k:02d}.txt').write_text('0 0.5 0.5 0.3 0.3\n')
        bright.append(a)
    alice = account('alice')
    sid = viz_session(alice['id'], 'scenes', root)
    _, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    # the "new camera": the same kind of scenes, much darker
    files = []
    for k, a in enumerate(bright[:8]):
        buf = io.BytesIO()
        Image.fromarray((a * 0.35).astype(np.uint8)).save(buf, 'JPEG', quality=90)
        files.append(('files', (f'night{k}.jpg', buf.getvalue(), 'image/jpeg')))
    files.append(('files', ('notes.txt', b'hello', 'text/plain')))
    t = c.post(f'/s/{sid}/api/augment/targets', files=files, data={'name': 'night camera'}).json()
    assert t['n'] == 8 and t['skipped'] == ['notes.txt'] and t['name'] == 'night camera'
    assert c.get(f'/s/{sid}/api/augment/targets').json()['targets'][0]['id'] == t['id']
    dark = {'steps': [{'t': 'RandomBrightnessContrast', 'p': 1, 'args': {'brightness_limit': [-0.6, -0.5], 'contrast_limit': [-0.4, -0.3]}}]}
    flip = {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}
    out = {}
    for name, recipe in (('flip', flip), ('dark', dark)):
        r = c.post(f'/s/{sid}/api/augment/coverage', json={'recipe': recipe, 'target': {'kind': 'upload', 'id': t['id']}, 'm': 14, 'k': 2}).json()
        j = wait_job(c, r['job_id'], timeout=180)
        assert j['status'] == 'done', j
        out[name] = c.get(f'/s/{sid}/api/augment/coverage/result', params={'key': r['key']}).json()['result']
    # darkening the dataset covers the dark camera better than mirroring it; the gap names brightness
    assert out['dark']['coverage']['after'] > out['flip']['coverage']['after']
    assert out['flip']['coverage']['before'] == out['dark']['coverage']['before']           # same train + target parts
    assert any(g['key'] == 'brightness' for g in out['flip'].get('gap') or [])
    assert out['dark']['credit'] and out['dark']['credit'][0]['t'] == 'RandomBrightnessContrast'
    latest = c.get(f'/s/{sid}/api/augment/coverage/result', params={'latest': 1}).json()
    assert latest['key'] == out['dark']['key']
    th = c.get(f'/s/{sid}/api/augment/thumb', params={'part': out['dark']['parts']['target'], 'i': 0})
    assert th.status_code == 200 and th.headers['content-type'] == 'image/jpeg'
    assert c.get(f'/s/{sid}/api/augment/thumb', params={'part': '../x', 'i': 0}).status_code == 404
    # the search, from an empty recipe: it adds a step that darkens, and the coverage rises
    r = c.post(f'/s/{sid}/api/augment/autotune', json={'recipe': {'steps': []}, 'target': {'kind': 'upload', 'id': t['id']}, 'm': 14}).json()
    assert wait_job(c, r['job_id'], timeout=300)['status'] == 'done'
    tune = c.get(f'/s/{sid}/api/augment/coverage/result', params={'key': r['key']}).json()['result']
    assert tune['history'] and tune['after'] > tune['start']
    assert tune['recipe']['steps'] and tune['history'][0]['step'] in {s['t'] for s in tune['recipe']['steps']}
    assert c.delete(f'/s/{sid}/api/augment/targets/{t["id"]}').json() == {'ok': True}
    assert c.post(f'/s/{sid}/api/augment/coverage', json={'recipe': flip, 'target': {'kind': 'upload', 'id': t['id']}}).status_code == 404


def test_augmented_copy_lands_in_a_new_data_center_bucket(world, tmp_path):
    from conftest import wait_job
    world['ctx'].cfg['storage']['data_root'] = str(tmp_path / 'dc')    # never the real data folder
    c, sid = world['c'], world['sid']
    assert c.post(f'/s/{sid}/api/augment/copy', json={'recipe': {'steps': []}}).status_code == 400
    r = c.post(f'/s/{sid}/api/augment/copy', json={'recipe': {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}, 'k': 1, 'name': 'flipped'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['images'] == 6 and wait_job(c, body['job_id'])['status'] == 'done'
    root = next((tmp_path / 'dc').iterdir())
    assert len(list((root / 'images').iterdir())) == 6 and (root / 'recipe.json').exists()


def test_model_check_needs_a_model(world):
    c, sid = world['c'], world['sid']
    r = c.post(f'/s/{sid}/api/augment/model', json={'recipe': {'steps': [{'t': 'HorizontalFlip'}]}, 'model': 'nope'})
    assert r.status_code in (404, 409) and ('model' in r.text)
