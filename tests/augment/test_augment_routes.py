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
    _, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    return {'c': c, 'sid': sid}


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
