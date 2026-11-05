"""The label page's image-strip filter: GET /api/label/projects/{pid}/media with q / cls / cls_mode / min_ann /
max_ann (store.media_page -> _media_filtered)."""

import os
import uuid

import pytest

from conftest import make_jpeg, wait_job

pytest.importorskip('dataviz.labeling.media')
pytestmark = pytest.mark.usefixtures('tmp_roots')     # the test images live in tmp_path

# a parallel run (pytest -n, xdist) shares the CPUs: the budgets catch seconds-long backtracking, not load
SLOW = 3 if os.environ.get('PYTEST_XDIST_WORKER') else 1


def hid():
    return uuid.uuid4().hex


def box(cls):
    return {'id': hid(), 'class_id': cls, 'geometry_type': 'bbox', 'geometry': {'x': .1, 'y': .1, 'w': .2, 'h': .2}}


NAMES = ['cam1_001.jpg', 'cam1_002.jpg', 'cam2_001.jpg', 'sub/cam1_003.jpg', 'other_50%.jpg', 'x_y.jpg']


@pytest.fixture
def fstore(store):
    det = store.add_task('Detect', 'object_detection', None, ['cat', 'dog', 'bird'])
    store.add_media([{'file_name': n, 'uri': n, 'width': 10, 'height': 10} for n in NAMES])
    ids = dict(zip(NAMES, store.media_ids()))
    cat, dog, bird = (c['id'] for c in det['classes'])
    store.put_annotations(ids['cam1_001.jpg'], det['id'], [box(cat), box(dog)], 0)
    store.put_annotations(ids['cam1_002.jpg'], det['id'], [box(cat)], 0)
    store.put_annotations(ids['cam2_001.jpg'], det['id'], [box(dog), box(dog), box(bird)], 0)
    store.set_status(ids['cam1_002.jpg'], [det['id']], 'commit')
    return store, det['id'], (cat, dog, bird)


def names(page):
    return [m['file_name'] for m in page['items']]


def test_name_substring_wildcard_regex(fstore):
    s, _, _ = fstore
    assert names(s.media_page(q='cam1')) == ['cam1_001.jpg', 'cam1_002.jpg', 'sub/cam1_003.jpg']
    assert names(s.media_page(q='CAM2')) == ['cam2_001.jpg']                    # case-insensitive
    # wildcard: anchored on the whole name or its base name
    assert names(s.media_page(q='cam1*')) == ['cam1_001.jpg', 'cam1_002.jpg', 'sub/cam1_003.jpg']
    assert names(s.media_page(q='cam?_001.jpg')) == ['cam1_001.jpg', 'cam2_001.jpg']
    # LIKE metacharacters are literal
    assert names(s.media_page(q='50%')) == ['other_50%.jpg']
    assert names(s.media_page(q='x_y')) == ['x_y.jpg']
    assert names(s.media_page(q='/^cam\\d_00[12]/')) == ['cam1_001.jpg', 'cam1_002.jpg', 'cam2_001.jpg']
    p = s.media_page(offset=1, limit=1, q='/cam/')
    assert p['total'] == 4 and names(p) == ['cam1_002.jpg']
    with pytest.raises(ValueError, match='regular expression'):
        s.media_page(q='/(/')


def test_class_modes_counts_and_status(fstore):
    s, tid, (cat, dog, bird) = fstore
    assert names(s.media_page(cls=[cat, bird], cls_mode='any')) == ['cam1_001.jpg', 'cam1_002.jpg', 'cam2_001.jpg']
    assert names(s.media_page(cls=[cat, dog], cls_mode='all')) == ['cam1_001.jpg']
    assert names(s.media_page(cls=[cat], cls_mode='none')) == ['cam2_001.jpg', 'sub/cam1_003.jpg', 'other_50%.jpg',
                                                                'x_y.jpg']
    assert names(s.media_page(task_id=tid, min_ann=2)) == ['cam1_001.jpg', 'cam2_001.jpg']
    assert s.media_page(task_id=tid, max_ann=0)['total'] == 3
    assert names(s.media_page(min_ann=3, max_ann=3)) == ['cam2_001.jpg']        # no task: every live annotation
    # combined with the status filter (todo = no status row or 'todo')
    assert names(s.media_page(task_id=tid, status='done', q='cam1')) == ['cam1_002.jpg']
    assert names(s.media_page(task_id=tid, status='todo', q='cam1')) == ['sub/cam1_003.jpg']
    assert names(s.media_page(task_id=tid, status='in_progress', cls=[dog])) == ['cam1_001.jpg', 'cam2_001.jpg']
    with pytest.raises(ValueError):
        s.media_page(cls=[cat], cls_mode='some')
    # items carry status / counts like the unfiltered page
    it = s.media_page(q='cam2')['items'][0]
    assert it['n'][tid] == 3 and it['status'][tid] == 'in_progress'


def test_route(client, tmp_path):
    src = tmp_path / 'pics'
    for n in ('cam1_a.jpg', 'cam1_b.jpg', 'cam2_a.jpg'):
        make_jpeg(src / n)
    r = client.post('/api/label/projects', json={'name': 'F', 'source': {'kind': 'local', 'path': str(src)},
                                                  'tasks': [{'name': 'D', 'task_type': 'object_detection',
                                                             'classes': ['a', 'b']}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert wait_job(client, out['job_id'])['status'] == 'done'
    pid = out['project']['id']
    task = out['project']['tasks'][0]
    url = f'/api/label/projects/{pid}/media'
    assert client.get(url, params={'q': 'cam1*'}).json()['total'] == 2
    assert client.get(url, params={'q': '/_a\\./'}).json()['total'] == 2
    assert client.get(url, params={'q': '/[/'}).status_code == 400
    a = task['classes'][0]['id']
    mid = client.get(url, params={'q': 'cam2'}).json()['items'][0]['id']
    r = client.put(f'/api/label/projects/{pid}/annotations/{mid}',
                   json={'task_id': task['id'], 'base_version': 0, 'annotations': [box(a)]})
    assert r.status_code == 200, r.text
    d = client.get(url, params={'cls': a, 'cls_mode': 'any'}).json()
    assert [m['id'] for m in d['items']] == [mid]
    assert client.get(url, params={'cls': a, 'cls_mode': 'none'}).json()['total'] == 2
    assert client.get(url, params={'task_id': task['id'], 'min_ann': 1}).json()['total'] == 1
    assert client.get(url, params={'cls': a, 'cls_mode': 'bogus'}).status_code == 400


def test_catastrophic_regex_is_refused_fast(store, client):
    """A regex that backtracks exponentially would hold the GIL (the whole server freezes): refused (400) up front
    when it nests repetitions, cut off by the time budget otherwise; never seconds of work."""
    import time
    from dataviz.labeling import store as store_mod
    long_names = ['a' * 40 + f'{k}!.jpg' for k in range(50)]
    store.add_media([{'file_name': n, 'uri': n, 'width': 10, 'height': 10} for n in long_names])
    for pat in ('/(a+)+$/', '/(\\w*)*!x/', '/(?:a{2,})+$/'):
        t0 = time.perf_counter()
        with pytest.raises(ValueError, match='regular expression'):
            store.media_page(q=pat)
        assert time.perf_counter() - t0 < 1 * SLOW
    assert store.media_page(q='/^a+0!/')['total'] == 1                          # a plain one still works
    if store_mod._regex is not None:                                          # overlapping alternation: the timeout
        t0 = time.perf_counter()
        with pytest.raises(ValueError, match='too long'):
            store.media_page(q='/(a|aa)+$/')
        assert time.perf_counter() - t0 < 1.5 * SLOW
    # through the route: 400, quickly
    r = client.post('/api/label/projects', json={'name': 'R', 'source': {'kind': 'upload'},
                                                  'tasks': [{'name': 'D', 'task_type': 'object_detection', 'classes': ['a']}]})
    pid = r.json()['project']['id']
    client.ctx.labeling.store(pid).add_media([{'file_name': n, 'uri': n, 'width': 10, 'height': 10} for n in long_names])
    t0 = time.perf_counter()
    r = client.get(f'/api/label/projects/{pid}/media', params={'q': '/(a+)+$/'})
    assert r.status_code == 400 and 'regular expression' in r.json()['detail'] and time.perf_counter() - t0 < 1 * SLOW
