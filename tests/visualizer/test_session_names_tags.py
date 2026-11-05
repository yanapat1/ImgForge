"""Session names unique within each list (Visualize / Label, case-insensitive) and user tags on both cards."""

import pytest

from conftest import wait_job

pytest.importorskip('dataviz.labeling.media')
pytestmark = pytest.mark.usefixtures('tmp_roots')

TASKS = [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat']}]


def yolo_dir(path):
    for sub in ('images', 'labels'):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return str(path)


def save_ds(client, path, name=None, **kw):
    return client.post('/api/datasets', json={'kind': 'local', 'path': path, 'name': name, **kw})


def new_label(client, name, **kw):
    return client.post('/api/label/projects', json={'name': name, 'tasks': TASKS, **kw})


# ---------------------------------------------------------------- core rules
def test_clean_tags_and_unique_name():
    from dataviz.core.names import clean_tags, name_taken, unique_name
    assert clean_tags(['  a  b ', 'A B', '', '  ', 'x', 'X']) == ['a b', 'x']
    assert clean_tags(None) == []
    with pytest.raises(ValueError, match='at most 20 tags'):
        clean_tags([f't{i}' for i in range(21)])
    assert len(clean_tags([f't{i}' for i in range(20)] + ['T0'])) == 20     # duplicates do not count
    with pytest.raises(ValueError):
        clean_tags(['x' * 33])
    for bad in ('a,b', [1], {'a': 1}):
        with pytest.raises(ValueError):
            clean_tags(bad)
    assert name_taken('Cars', ['cars']) and not name_taken('Cars 2', ['cars'])
    assert unique_name('data', ['Data', 'data (2)']) == 'data (3)' and unique_name('new', ['data']) == 'new'


# ---------------------------------------------------------------- Visualize sessions
def test_dataset_names_unique(client, tmp_path):
    a, b = yolo_dir(tmp_path / 'a' / 'data'), yolo_dir(tmp_path / 'b' / 'data')
    r = save_ds(client, a, 'Cars')
    assert r.status_code == 200, r.text
    did = r.json()['id']
    for name in ('Cars', 'cARS', '  cars '):
        r = save_ds(client, b, name)
        assert r.status_code == 400 and 'visualize session called' in r.json()['detail'], r.text
    assert save_ds(client, a, 'Cars', id=did).status_code == 200           # an edit keeping its own name
    assert save_ds(client, a, 'CARS', id=did).json()['name'] == 'CARS'      # a case change of its own name
    # blank names are auto names: a unique suffix, never an error
    n1 = save_ds(client, a).json()['name']
    n2 = save_ds(client, b).json()['name']
    assert (n1, n2) == ('data', 'data (2)')
    assert len(client.get('/api/datasets').json()['datasets']) == 3


def test_dataset_tags(client, tmp_path):
    path = yolo_dir(tmp_path / 'ds')
    did = save_ds(client, path, 'Cars').json()['id']
    assert client.get('/api/datasets').json()['datasets'][0].get('tags', []) == []
    r = client.put(f'/api/datasets/{did}/tags', json={'tags': ['  night  shift ', 'Night Shift', 'rain', '']})
    assert r.status_code == 200, r.text
    assert r.json()['tags'] == ['night shift', 'rain'] and r.json()['id'] == did
    assert client.put(f'/api/datasets/{did}/tags', json={'tags': [f't{i}' for i in range(21)]}).status_code == 400
    assert client.put(f'/api/datasets/{did}/tags', json={'tags': 'rain'}).status_code == 400
    assert client.put('/api/datasets/nope/tags', json={'tags': []}).status_code == 404
    # an edit through the session form (no tags in the body) keeps them
    assert save_ds(client, path, 'Cars renamed', id=did).json()['tags'] == ['night shift', 'rain']
    card = client.get('/api/datasets').json()['datasets'][0]
    assert card['name'] == 'Cars renamed' and card['tags'] == ['night shift', 'rain']


# ---------------------------------------------------------------- Label sessions
def test_label_names_unique(client):
    assert new_label(client, 'Birds').status_code == 200
    r = new_label(client, ' birds ')
    assert r.status_code == 400 and 'label session called' in r.json()['detail']
    pid = new_label(client, 'Fish').json()['project']['id']
    r = client.patch(f'/api/label/projects/{pid}', json={'name': 'BIRDS'})
    assert r.status_code == 400 and 'label session called' in r.json()['detail']
    assert client.patch(f'/api/label/projects/{pid}', json={'name': 'Fish'}).status_code == 200
    assert client.patch(f'/api/label/projects/{pid}', json={'name': 'FISH'}).json()['name'] == 'FISH'
    assert len(client.get('/api/label/projects').json()['projects']) == 2


def test_label_tags_and_duplicate(client):
    r = new_label(client, 'Birds', tags=['Sky', 'sky', ' wings '])
    assert r.status_code == 200, r.text
    pid = r.json()['project']['id']
    assert r.json()['project']['tags'] == ['Sky', 'wings']
    r = client.patch(f'/api/label/projects/{pid}', json={'tags': ['a', 'B', 'b']})
    assert r.status_code == 200 and r.json()['tags'] == ['a', 'B']
    assert client.patch(f'/api/label/projects/{pid}', json={'tags': [f't{i}' for i in range(21)]}).status_code == 400
    card = client.get('/api/label/projects').json()['projects'][0]
    assert card['tags'] == ['a', 'B'] and card['name'] == 'Birds'
    assert client.patch(f'/api/label/projects/{pid}', json={'description': 'x'}).json()['tags'] == ['a', 'B']
    # duplicate: blank name -> "(copy)", then a unique suffix; tags copied
    names = []
    for _ in range(2):
        j = wait_job(client, client.post(f'/api/label/projects/{pid}/duplicate', json={}).json()['job_id'])
        assert j['status'] == 'done', j
        names.append(j['result']['name'])
        assert j['result']['tags'] == ['a', 'B']
    assert names == ['Birds (copy)', 'Birds (copy 2)']
    # a typed name that is taken: 400 at once, no job
    r = client.post(f'/api/label/projects/{pid}/duplicate', json={'name': 'birds (COPY)'})
    assert r.status_code == 400 and 'label session called' in r.json()['detail']
    assert len(client.get('/api/label/projects').json()['projects']) == 3
