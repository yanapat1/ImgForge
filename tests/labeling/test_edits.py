"""The image editor (Explore viewer): add / set_box ops, edits without a scan, a label file made for a background
image, the in-memory State patch (moved boxes, deleted ones), undo of an edit, sha1 conflicts (409), and the
image browser routes (background images, raw image, thumbnails that follow edits)."""

import pytest

from test_curation import LABELS, make_dataset, open_state

from dataviz.data import curation as cur


@pytest.fixture
def ds(tmp_path):
    return make_dataset(tmp_path / 'ds')


@pytest.fixture
def client(ds, tmp_path):
    from fastapi.testclient import TestClient
    from dataviz.server.app import create_app
    st = open_state(ds, tmp_path / 'ws')
    with TestClient(create_app(st)) as c:
        c.st = st
        yield c


def test_apply_ops_add_and_set_box():
    text = '0 0.5 0.5 0.2 0.2 extra\r\n1 0.3 0.3 0.1 0.1'          # CRLF, no newline at the end
    out, lm = cur.apply_ops(text, [{'op': 'set_box', 'line': 0, 'cx': 0.4, 'cy': 0.4, 'w': 0.2, 'h': 0.2},
                                   {'op': 'add', 'cls': 2, 'cx': 0.9, 'cy': 0.9, 'w': 0.4, 'h': 0.4}])
    assert out == '0 0.4 0.4 0.2 0.2 extra\r\n1 0.3 0.3 0.1 0.1\r\n2 0.85 0.85 0.3 0.3\r\n'   # added box clamped
    assert lm == {0: 0, 1: 1}
    assert cur.apply_ops('', [{'op': 'add', 'cls': 0, 'cx': 0.5, 'cy': 0.5, 'w': 0.2, 'h': 0.2}])[0] == '0 0.5 0.5 0.2 0.2\n'
    for bad in ({'op': 'add', 'cls': -1, 'cx': .5, 'cy': .5, 'w': .1, 'h': .1},
                {'op': 'add', 'cls': 0, 'cx': 2, 'cy': 2, 'w': .1, 'h': .1},
                {'op': 'add', 'cls': 0, 'cx': .5, 'cy': .5, 'w': 0, 'h': .1},
                {'op': 'set_box', 'line': 5, 'cx': .5, 'cy': .5, 'w': .1, 'h': .1},
                {'op': 'set_box', 'line': 0, 'cx': 'x', 'cy': .5, 'w': .1, 'h': .1}):
        with pytest.raises(ValueError):
            cur.apply_ops('0 0.5 0.5 0.2 0.2\n', [bad])


def test_edit_without_scan_move_add_delete_undo(client, ds):
    st = client.st
    b = client.get('/api/img/boxes', params={'name': 'oob.jpg'}).json()
    assert b['writable'] and (b['img_w'], b['img_h']) == (64, 48)
    sids = [x['sid'] for x in b['boxes']]
    assert None not in sids and len(sids) == 2
    r = client.post('/api/edits', json={'image': 'oob.jpg', 'expect_sha1': b['sha1'], 'ops': [
        {'op': 'set_box', 'line': 0, 'cx': 0.25, 'cy': 0.25, 'w': 0.2, 'h': 0.2},
        {'op': 'set_class', 'line': 1, 'cls': 2},
        {'op': 'add', 'cls': 1, 'cx': 0.7, 'cy': 0.7, 'w': 0.2, 'h': 0.2}]})
    assert r.status_code == 200, r.text
    r = r.json()
    assert (ds / 'labels' / 'oob.txt').read_text() == '0 0.25 0.25 0.2 0.2\n2 0.95 0.5 0.2 0.2\n1 0.7 0.7 0.2 0.2\n'
    assert r['patch']['moved'] == [sids[0]] and r['patch']['cls'] == {str(sids[1]): 2}
    assert st.samples[sids[0]]['bbox'] == [10, 7, 22, 17] and int(st.labels[sids[1]]) == 2
    assert [x['sid'] for x in r['boxes']] == [sids[0], sids[1], None]          # the added box has no dot yet
    assert r['status'] == {'pending_new': 1, 'moved': 1, 'images': ['oob.jpg']}
    # a second edit on the same file (ids now come from box_idx): delete the moved box
    r2 = client.post('/api/edits', json={'image': 'oob.jpg', 'expect_sha1': r['sha1'],
                                         'ops': [{'op': 'delete', 'line': 0}]}).json()
    assert r2['patch']['deleted'] == [sids[0]] and [x['sid'] for x in r2['boxes']] == [sids[1], None]
    assert st.deleted[sids[0]]
    # undo the second, then the first: bytes and State as they were
    client.post('/api/curation/undo', json={'undo_id': r2['undo_id']})
    assert not st.deleted[sids[0]]
    u = client.post('/api/curation/undo', json={'undo_id': r['undo_id']}).json()
    assert u['restored'] == 1 and sids[0] in u['patch']['moved']
    assert (ds / 'labels' / 'oob.txt').read_text() == LABELS['oob']
    assert int(st.labels[sids[1]]) == 1 and st.samples[sids[0]]['bbox'] != [10, 7, 22, 17]
    assert [x['sid'] for x in client.get('/api/img/boxes', params={'name': 'oob.jpg'}).json()['boxes']] == sids


def test_conflict_and_bad_requests(client, ds):
    b = client.get('/api/img/boxes', params={'name': 'ok.jpg'}).json()
    (ds / 'labels' / 'ok.txt').write_text('1 0.5 0.5 0.1 0.1\n')                # changed on disk meanwhile
    r = client.post('/api/edits', json={'image': 'ok.jpg', 'expect_sha1': b['sha1'],
                                        'ops': [{'op': 'delete', 'line': 0}]})
    assert r.status_code == 409
    d = r.json()['detail']
    assert d['boxes'][0]['cls'] == 1 and d['sha1'] != b['sha1']
    assert (ds / 'labels' / 'ok.txt').read_text() == '1 0.5 0.5 0.1 0.1\n'
    assert client.post('/api/edits', json={'image': 'nope.jpg', 'ops': [{'op': 'delete', 'line': 0}]}).status_code == 404
    assert client.post('/api/edits', json={'image': 'ok.jpg', 'ops': []}).status_code == 400
    assert client.post('/api/edits', json={'image': 'ok.jpg', 'ops': [{'op': 'delete', 'line': 9}]}).status_code == 400


def test_background_image_gets_a_label_file(client, ds):
    imgs = client.get('/api/images').json()
    assert 'background.jpg' in imgs['bg'] and 'ok.jpg' not in imgs['bg'] and imgs['total'] >= 10
    b = client.get('/api/img/boxes', params={'name': 'background.jpg'}).json()
    assert b['boxes'] == [] and b['label'] == 'background.txt'
    t1 = client.get('/api/img/thumb', params={'name': 'background.jpg', 'size': 200})
    assert t1.status_code == 200 and t1.headers['content-type'] == 'image/jpeg'
    r = client.post('/api/edits', json={'image': 'background.jpg', 'expect_sha1': b['sha1'],
                                        'ops': [{'op': 'add', 'cls': 0, 'cx': 0.5, 'cy': 0.5, 'w': 0.5, 'h': 0.5}]})
    assert r.status_code == 200, r.text
    assert (ds / 'labels' / 'background.txt').read_text() == '0 0.5 0.5 0.5 0.5\n'
    assert client.get('/api/images').json()['pending'] == {'background.jpg': 1}
    t2 = client.get('/api/img/thumb', params={'name': 'background.jpg', 'size': 200})
    assert t2.content != t1.content                                          # the thumbnail shows the new box
    raw = client.get('/api/img/raw', params={'name': 'background.jpg'})
    assert raw.status_code == 200 and raw.headers['content-type'] == 'image/jpeg'
    assert client.get('/api/img/raw', params={'name': '../classes.txt'}).status_code == 404
