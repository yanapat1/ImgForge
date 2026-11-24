"""Change marks of the Bucket page (bucket/changes.py): a file the Bucket page or Visualize changed carries `changed`
in ls, each parent folder the count below it; a move carries its marks along, undo clears them, Mark as seen (POST
…/changes/clear) forgets them, GET …/changes lists them newest first, and the table schema matches the migration."""

import pytest

from test_bucket_routes import dc, new_bucket, upload_zip  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures('tmp_roots')


def _ls(c, bid, path=''):
    j = c.get(f'/api/bucket/buckets/{bid}/ls', params={'path': path}).json()
    return {e['name']: e.get('changed') for e in j['entries']}, j.get('changes')


def test_marks_roll_up_move_undo_and_seen(dc):  # noqa: F811 - the fixture
    _, c = dc
    bid = new_bucket(c)['id']
    upload_zip(c, bid)
    from dataviz.bucket import changes
    changes.clear(bid, prefix='')                       # the upload itself marks what came in: start clean
    assert _ls(c, bid)[1] == 0
    # a text edit on the page
    r = c.put(f'/api/bucket/buckets/{bid}/text', json={'path': 'labels/train/im0.txt', 'text': '0 0.5 0.5 0.1 0.1\n'})
    assert r.status_code == 200, r.text
    root, n = _ls(c, bid)
    assert n == 1 and root['labels']['n'] == 1 and not root['labels']['self'] and root['images'] is None
    files, n = _ls(c, bid, 'labels/train')
    assert files['im0.txt']['kind'] == 'edited' and files['im0.txt']['source'] == 'bucket' and files['im1.txt'] is None
    # a label fix from Visualize (the source's write hook, in the background)
    from dataviz.image import sources
    src = sources.LocalSource(c.get(f'/api/bucket/buckets/{bid}').json()['root'])
    src.write_label('im1.txt', '1 0.2 0.2 0.1 0.1\n')
    changes.flush()
    files, _ = _ls(c, bid, 'labels')                    # this source's labels/ is the bucket's labels/
    assert files['im1.txt']['source'] == 'visual' and files['im1.txt']['kind'] == 'edited'
    assert _ls(c, bid)[0]['labels']['n'] == 2
    # a move carries the marks; undo clears them
    r = c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': [{'op': 'rename', 'path': 'labels', 'name': 'lbl'}]}).json()
    root, n = _ls(c, bid)
    assert 'labels' not in root and root['lbl']['n'] == 3 and root['lbl']['self'], 'two edits + the rename'
    c.post(f'/api/bucket/buckets/{bid}/undo', json={'undo_id': r['undo_id']})
    root, n = _ls(c, bid)
    assert root['labels'] is None and n == 0, 'undone: as it was'
    # new folder + file, list newest first, then Mark as seen
    c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': 'extra'},
                                                       {'op': 'create', 'path': 'extra/a.txt'}]})
    lst = c.get(f'/api/bucket/buckets/{bid}/changes', params={'prefix': 'extra'}).json()
    assert lst['total'] == 2 and {i['path'] for i in lst['items']} == {'extra', 'extra/a.txt'}
    assert c.post(f'/api/bucket/buckets/{bid}/changes/clear', json={'paths': ['extra']}).json()['cleared'] == 2
    assert _ls(c, bid)[1] == 0
    assert c.post(f'/api/bucket/buckets/{bid}/changes/clear', json={}).status_code == 400


def test_delete_marks_the_parent_and_upload_marks_files(dc):  # noqa: F811 - the fixture
    _, c = dc
    bid = new_bucket(c)['id']
    c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': 'in'}]})
    up = upload_zip(c, bid, 'in')                       # an archive: the folder it unpacked into is marked
    assert up['added'] == 7
    root, n = _ls(c, bid)
    assert root['in']['kind'] == 'uploaded' and root['in']['self']
    r = c.post(f'/api/bucket/buckets/{bid}/upload', params={'path': 'in'}, files=[('files', ('note.txt', b'hi', 'text/plain'))])
    assert r.status_code == 200, r.text
    files, _ = _ls(c, bid, 'in')
    assert files['note.txt']['kind'] == 'uploaded'
    from dataviz.bucket import changes
    changes.clear(bid, prefix='')
    upload_zip(c, bid)
    changes.clear(bid, prefix='')
    c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': [{'op': 'delete', 'path': 'images/train/im2.jpg'}]})
    root, n = _ls(c, bid)
    assert n == 1 and root['images']['kind'] == 'deleted'
