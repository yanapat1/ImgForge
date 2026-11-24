"""New file… on the Bucket page: POST …/ops {op: create, path, text} with any extension, a starter text, undo
removes it, an existing name is refused (409 in the op's error), a hidden name is refused."""

import pytest

from test_bucket_routes import dc, new_bucket  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures('tmp_roots')


def test_new_file_any_extension_and_undo(dc):  # noqa: F811 - the fixture
    _, c = dc
    bid = new_bucket(c)['id']
    r = c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': [
        {'op': 'create', 'path': 'notes.md', 'text': ''},
        {'op': 'create', 'path': 'data.yaml', 'text': 'names:\n  0: object\n'},
        {'op': 'create', 'path': 'run.weird-ext'}]}).json()
    assert len(r['applied']) == 3 and not r['errors'] and r['undo_id']
    names = {e['name'] for e in c.get(f'/api/bucket/buckets/{bid}/ls').json()['entries']}
    assert {'notes.md', 'data.yaml', 'run.weird-ext'} <= names
    assert c.get(f'/api/bucket/buckets/{bid}/text', params={'path': 'data.yaml'}).json()['text'] == 'names:\n  0: object\n'
    again = c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': [{'op': 'create', 'path': 'notes.md'},
                                                               {'op': 'create', 'path': '.hidden'}]}).json()
    assert len(again['errors']) == 2 and 'already exists' in again['errors'][0]['error']
    assert 'hidden' in again['errors'][1]['error']
    u = c.post(f'/api/bucket/buckets/{bid}/undo', json={'undo_id': r['undo_id']}).json()
    assert u['restored'] == 3
    names = {e['name'] for e in c.get(f'/api/bucket/buckets/{bid}/ls').json()['entries']}
    assert not names & {'notes.md', 'data.yaml', 'run.weird-ext'}
