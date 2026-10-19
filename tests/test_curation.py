"""Data curation: every rule finds its issue, fixes + undo round-trip the label bytes exactly, sha1 conflicts,
the in-memory state patch, S3 write_label (moto) and the scan speed on 20k images."""

import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import make_jpeg, wait_job

from dataviz.data import curation as cur
from dataviz.data.sources import Conflict, LocalSource, sha1_bytes

CLASSES = ['cat', 'dog', 'bird']

LABELS = {
    'ok': '0 0.5 0.5 0.4 0.4\n',
    'oob': '0 0.5 0.5 0.2 0.2\n1 0.95 0.5 0.2 0.2\n',
    'dup': '1 0.5 0.5 0.3 0.3\n1 0.5 0.5 0.3 0.3\n',
    'conflict': '0 0.3 0.3 0.2 0.2\n1 0.3 0.3 0.2 0.2\n',
    'badcls': '7 0.5 0.5 0.2 0.2\n0 0.2 0.2 0.1 0.1\n',
    'malformed': '0 0.5 0.5\r\nabc x y z w\r\n1 0.4 0.4 0.2 0.2\r\n',   # CRLF: undo must give the same bytes back
    'empty': '',
    'tiny': '0 0.5 0.5 0.01 0.3\n0 0.5 0.5 0 0.1\n1 0.3 0.3 0.2 0.2\n',
    'thin': '2 0.5 0.5 0.9 0.04\n',
    'orphan': '0 0.5 0.5 0.2 0.2\n',
}


def make_dataset(root):
    (root / 'labels').mkdir(parents=True)
    (root / 'classes.txt').write_text('\n'.join(CLASSES) + '\n')
    for k, (name, text) in enumerate(LABELS.items()):
        (root / 'labels' / f'{name}.txt').write_bytes(text.encode())
        if name == 'orphan':
            continue
        size = (640, 480) if name == 'thin' else (64, 48)
        make_jpeg(root / 'images' / f'{name}.jpg', *size, color=(10 + k * 20, 80, 40))   # every image different
    make_jpeg(root / 'images' / 'background.jpg', 64, 48, color=(1, 2, 3))
    shutil.copy(root / 'images' / 'ok.jpg', root / 'images' / 'ok_copy.jpg')          # duplicate image file
    (root / 'images' / 'broken.jpg').write_bytes(b'\xff\xd8 this is not a jpeg')       # unreadable image
    return root


class _Job:
    def progress(self, *a, **k):
        pass


def open_state(root, cache):
    from dataviz.state import State
    st = State(LocalSource(root), cache, {'none': None}, default_feature='none')
    st._index_job(_Job())
    return st


@pytest.fixture
def ds(tmp_path):
    return make_dataset(tmp_path / 'ds')


@pytest.fixture
def state(ds, tmp_path):
    return open_state(ds, tmp_path / 'ws')


@pytest.fixture
def scanned(state):
    c = cur.Curation(state)
    c.scan()
    return c


def by_type(c):
    out = {}
    for it in c.all_items():
        out.setdefault(it['type'], []).append(it)
    return out


def test_every_rule_finds_its_issue(scanned):
    t = by_type(scanned)
    assert [it['label'] for it in t['oob']] == ['oob.txt'] and t['oob'][0]['line'] == 1
    assert [f['op'] for f in t['oob'][0]['fixes']] == ['clamp', 'delete']
    assert [(it['label'], it['lines']) for it in t['dup_box']] == [('dup.txt', [0, 1])]
    assert [(it['label'], it['lines']) for it in t['conflict_box']] == [('conflict.txt', [0, 1])]
    assert [(it['label'], it['line']) for it in t['bad_class']] == [('badcls.txt', 0)]
    assert sorted((it['label'], it['line']) for it in t['bad_line']) == [('malformed.txt', 0), ('malformed.txt', 1)]
    assert sorted((it['label'], it['line']) for it in t['degenerate']) == [('tiny.txt', 0), ('tiny.txt', 1)]
    assert [it['label'] for it in t['extreme_aspect']] == ['thin.txt']
    assert [it['label'] for it in t['missing_image']] == ['orphan.txt']
    assert sorted(it['image'] for it in t['empty_label']) == ['background.jpg', 'broken.jpg', 'empty.jpg', 'ok_copy.jpg']
    assert [it['image'] for it in t['unreadable_image']] == ['broken.jpg']
    assert [(it['image'], it['others']) for it in t['dup_image']] == [('ok_copy.jpg', ['ok.jpg'])]
    assert {it['cls'] for it in t['rare_class']} == {0, 1, 2}            # a tiny dataset: every class is rare
    # box-level issues point at the index boxes (plot / filter)
    st = scanned.state
    sid = t['bad_class'][0]['box_ids'][0]
    assert st.samples[sid]['label'] == 'badcls.txt' and st.labels[sid] == 7
    assert len(t['dup_box'][0]['box_ids']) == 2


def test_parse_and_apply_ops_keep_other_bytes():
    text = '0 0.5 0.5 0.2 0.2\r\n  1 1.1 0.5 0.4 0.2 extra\n2 0.1 0.1 0.1 0.1'
    out, lm = cur.apply_ops(text, [{'op': 'set_class', 'line': 0, 'cls': 2}, {'op': 'delete', 'line': 2}])
    assert out == '2 0.5 0.5 0.2 0.2\r\n  1 1.1 0.5 0.4 0.2 extra\n'
    assert lm == {0: 0, 1: 1, 2: None}
    out, _ = cur.apply_ops('1 1.1 0.5 0.4 0.2\n', [{'op': 'clamp', 'line': 0}])
    assert out == '1 0.95 0.5 0.1 0.2\n'
    out, _ = cur.apply_ops('1 3 3 0.4 0.2\n', [{'op': 'clamp', 'line': 0}])     # nothing inside: removed
    assert out == ''
    with pytest.raises(ValueError):
        cur.apply_ops('0 0.5 0.5 0.1 0.1\n', [{'op': 'delete', 'line': 5}])
    with pytest.raises(ValueError):
        cur.apply_ops('0 0.5 0.5 0.1 0.1\n', [{'op': 'rm -rf', 'line': 0}])


def test_local_write_label_atomic_and_conflict(ds):
    src = LocalSource(ds)
    assert src.label_writable
    before = src.read_label_bytes('ok.txt')
    new = src.write_label('ok.txt', '1 0.5 0.5 0.4 0.4\n', sha1_bytes(before))
    assert (ds / 'labels' / 'ok.txt').read_bytes() == b'1 0.5 0.5 0.4 0.4\n' and new == sha1_bytes(b'1 0.5 0.5 0.4 0.4\n')
    with pytest.raises(Conflict):
        src.write_label('ok.txt', 'x', sha1_bytes(before))
    assert not list((ds / 'labels').glob('.*tmp'))
    with pytest.raises(ValueError):
        src.write_label('../evil.txt', 'x')


def test_fix_undo_round_trip_bytes_and_state_patch(scanned, ds):
    st, c = scanned.state, scanned
    orig = {p.name: p.read_bytes() for p in (ds / 'labels').iterdir()}
    t = by_type(c)
    dup = t['dup_box'][0]
    gone = dup['box_ids'][1]
    sha = c._public(dup)['sha1']
    res = c.fix([{'label': 'dup.txt', 'expect_sha1': sha, 'ops': [dup['fixes'][0]]}], 'dedupe')
    assert res['applied'] == 1 and res['conflicts'] == [] and res['patch']['deleted'] == [gone]
    assert (ds / 'labels' / 'dup.txt').read_bytes() == b'1 0.5 0.5 0.3 0.3\n'
    assert st.deleted[gone] and gone not in st.by_image['dup.jpg']
    assert 'dup_box' not in by_type(c)
    # change the class of the unknown-class box
    bad = by_type(c)['bad_class'][0]
    sid = bad['box_ids'][0]
    r2 = c.fix([{'label': 'badcls.txt', 'expect_sha1': c._public(bad)['sha1'],
                 'ops': [{'op': 'set_class', 'line': bad['line'], 'cls': 2}]}])
    assert r2['patch']['cls'] == {sid: 2} and st.labels[sid] == 2 and st.samples[sid]['cls'] == 2
    assert (ds / 'labels' / 'badcls.txt').read_bytes() == b'2 0.5 0.5 0.2 0.2\n0 0.2 0.2 0.1 0.1\n'
    assert c.state_patch() == {'cls': {sid: 2}, 'deleted': [gone]}
    assert c.n_fixes() == 2 and len(c.history()) == 2
    # fix all malformed lines (CRLF file), then undo everything: identical bytes, state restored
    fx = c.default_fix_items('bad_line')
    assert [x['label'] for x in fx] == ['malformed.txt'] and len(fx[0]['ops']) == 2
    r3 = c.fix(fx, 'all bad lines')
    assert (ds / 'labels' / 'malformed.txt').read_bytes() == b'1 0.4 0.4 0.2 0.2\r\n'
    for r in (r3, r2, res):
        u = c.undo(r['undo_id'])
        assert u['restored'] == 1 and u['conflicts'] == []
    assert {p.name: p.read_bytes() for p in (ds / 'labels').iterdir()} == orig
    assert not st.deleted[gone] and gone in st.by_image['dup.jpg'] and st.labels[sid] == 7
    assert len(by_type(c)['dup_box']) == 1 and c.history() == []
    with pytest.raises(KeyError):
        c.undo(res['undo_id'])


def test_expect_sha1_conflict(scanned, ds):
    c = scanned
    oob = by_type(c)['oob'][0]
    sha = c._public(oob)['sha1']
    (ds / 'labels' / 'oob.txt').write_text('0 0.5 0.5 0.2 0.2\n1 0.95 0.5 0.2 0.2\n0 0.1 0.1 0.1 0.1\n')   # edited elsewhere
    res = c.fix([{'label': 'oob.txt', 'expect_sha1': sha, 'ops': [oob['fixes'][0]]}])
    assert res['conflicts'] == ['oob.jpg'] and res['applied'] == 0 and res['undo_id'] is None
    assert (ds / 'labels' / 'oob.txt').read_text().count('\n') == 3


def test_backups_pruned(scanned, ds):
    c = scanned
    c.cfg['keep_backups'] = 3
    for k in range(5):
        it = by_type(c)['bad_class'][0]
        r = c.fix([{'label': 'badcls.txt', 'expect_sha1': c._public(it)['sha1'],
                    'ops': [{'op': 'set_class', 'line': 0, 'cls': 7 + (k + 1) % 2}]}])
        assert r['undo_id']
    assert len(list(c.backup_dir.iterdir())) == 3


def test_routes(ds, tmp_path):
    from fastapi.testclient import TestClient
    from dataviz.server.app import create_app
    st = open_state(ds, tmp_path / 'ws2')
    with TestClient(create_app(st)) as client:
        r = client.post('/api/curation/scan', json={}).json()
        assert r['ready'] is False
        assert wait_job(client, r['job_id'])['status'] == 'done'
        s = client.get('/api/curation/summary').json()
        assert s['ready'] and s['summary']['oob']['n'] == 1 and s['writable']
        page = client.get('/api/curation/issues', params={'type': 'dup_box'}).json()
        assert page['total'] == 1
        item = page['items'][0]
        assert item['sha1'] and item['wh'] == [64, 48]
        ids = client.get('/api/curation/ids', params={'types': 'dup_box,missing_image'}).json()
        assert ids == {'images': [], 'boxes': item['box_ids']}
        fx = client.post('/api/curation/fix', json={'items': [{'label': item['label'], 'expect_sha1': item['sha1'],
                                                                'ops': [item['fixes'][0]]}]}).json()
        assert fx['applied'] == 1 and fx['patch']['deleted'] == [item['box_ids'][1]] and fx['n_fixes'] == 1
        assert 'dup_box' not in fx['summary']
        stale = client.post('/api/curation/fix', json={'items': [{'label': item['label'], 'expect_sha1': item['sha1'],
                                                                   'ops': [item['fixes'][0]]}]}).json()
        assert stale['conflicts'] == ['dup.jpg']
        assert client.get('/api/curation/patch').json()['deleted'] == [item['box_ids'][1]]
        all_oob = client.post('/api/curation/fix', json={'type': 'oob'}).json()
        assert all_oob['applied'] == 1
        assert client.post('/api/curation/undo', json={'undo_id': all_oob['undo_id']}).json()['restored'] == 1
        assert client.post('/api/curation/undo', json={'undo_id': fx['undo_id']}).json()['patch']['restored'] == \
            [item['box_ids'][1]]
        assert (ds / 'labels' / 'dup.txt').read_text() == LABELS['dup']
        # ignore hides an issue from the counts, un-ignore brings it back
        oob = client.get('/api/curation/issues', params={'type': 'oob'}).json()['items'][0]
        client.post('/api/curation/ignore', json={'ids': [oob['id']]})
        assert 'oob' not in client.get('/api/curation/summary').json()['summary']
        assert client.get('/api/curation/issues', params={'type': 'oob', 'ignored': 1}).json()['total'] == 1
        client.post('/api/curation/ignore', json={'ids': [oob['id']], 'ignore': False})
        assert client.get('/api/curation/summary').json()['summary']['oob']['n'] == 1
        img = client.get('/api/curation/image', params={'name': 'thin.jpg', 'max_w': 200})
        assert img.status_code == 200 and img.headers['content-type'] == 'image/jpeg'
        assert client.get('/api/curation/image', params={'name': '../x.jpg'}).status_code == 404
        assert client.post('/api/curation/undo', json={'undo_id': '../../etc'}).status_code == 404


def test_s3_write_label(monkeypatch, tmp_path):
    moto = pytest.importorskip('moto')
    import boto3
    from dataviz.data.sources import S3Source
    for k, v in {'AWS_ACCESS_KEY_ID': 'x', 'AWS_SECRET_ACCESS_KEY': 'x', 'AWS_DEFAULT_REGION': 'us-east-1'}.items():
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='bkt-test')
        c = boto3.client('s3', region_name='us-east-1')
        c.put_object(Bucket='bkt-test', Key='ds/labels/a.txt', Body=b'0 0.5 0.5 0.1 0.1\r\n')
        c.put_object(Bucket='bkt-test', Key='ds/images/a.jpg', Body=b'x')
        src = S3Source({'id': 'c1'}, 'bkt-test', 'ds', mode='stream')
        assert src.label_writable
        before = src.read_label_bytes('a.txt')
        assert before == b'0 0.5 0.5 0.1 0.1\r\n' and src.read_label_bytes('missing.txt') == b''
        sha = src.write_label('a.txt', b'1 0.5 0.5 0.1 0.1\r\n', sha1_bytes(before))
        assert c.get_object(Bucket='bkt-test', Key='ds/labels/a.txt')['Body'].read() == b'1 0.5 0.5 0.1 0.1\r\n'
        assert sha == sha1_bytes(b'1 0.5 0.5 0.1 0.1\r\n')
        with pytest.raises(Conflict):
            src.write_label('a.txt', 'x', sha1_bytes(before))
        # cache mode keeps the local mirror (and its manifest) in step
        cached = S3Source({'id': 'c1'}, 'bkt-test', 'ds', mode='cache', mirror_root=tmp_path / 'mirror')
        cached.sync()
        cached.write_label('a.txt', '2 0.5 0.5 0.1 0.1\n', sha)
        assert (cached.mirror / 'labels' / 'a.txt').read_bytes() == b'2 0.5 0.5 0.1 0.1\n'
        assert c.get_object(Bucket='bkt-test', Key='ds/labels/a.txt')['Body'].read() == b'2 0.5 0.5 0.1 0.1\n'


def test_scan_speed_20k_images(tmp_path):
    root = tmp_path / 'big'
    (root / 'labels').mkdir(parents=True)
    (root / 'images').mkdir()
    (root / 'classes.txt').write_text('a\nb\nc\nd\n')
    rng = np.random.default_rng(0)
    n_img = 20000
    samples, by_image = [], {}
    jpeg = make_jpeg(tmp_path / 'one.jpg').read_bytes()
    for i in range(n_img):
        k = int(rng.integers(1, 9))
        b = rng.uniform(0.1, 0.9, (k, 2))
        wh = rng.uniform(0.02, 0.2, (k, 2))
        cls = rng.integers(0, 4, k)
        lines = [f'{c} {x:.5f} {y:.5f} {w:.5f} {h:.5f}' for c, (x, y), (w, h) in zip(cls, b, wh)]
        if i % 50 == 0:
            lines.append(lines[0])               # a duplicate box
        name = f'img_{i:05d}'
        (root / 'labels' / f'{name}.txt').write_text('\n'.join(lines) + '\n')
        (root / 'images' / f'{name}.jpg').write_bytes(jpeg + i.to_bytes(4, 'little'))
        ids = []
        for ln in lines:
            c = int(ln.split()[0])
            ids.append(len(samples))
            samples.append({'id': len(samples), 'image': f'{name}.jpg', 'label': f'{name}.txt', 'cls': c,
                            'img_w': 640, 'img_h': 480, 'bbox': [0, 0, 1, 1]})
        by_image[f'{name}.jpg'] = ids
    st = SimpleNamespace(source=LocalSource(root), cache_dir=tmp_path / 'ws', samples=samples, by_image=by_image,
                         labels=np.array([s['cls'] for s in samples]), classes=['a', 'b', 'c', 'd'], fingerprint='f' * 40,
                         scores={}, registry={}, default_feature=None, ready=True, payloads={})
    st.cache_dir.mkdir()
    # best of two runs: the first one also pays for cold file reads, and a busy test machine (other suites in
    # parallel) can stall a single run; the budget is for the scan itself
    took = float('inf')
    for _ in range(2):
        c = cur.Curation(st)
        t0 = time.perf_counter()
        summ = c.scan()
        took = min(took, time.perf_counter() - t0)
    assert summ['dup_box']['n'] == n_img // 50
    assert 'unreadable_image' not in summ and 'dup_image' not in summ
    assert took < 5, f'scan of {n_img} images took {took:.2f}s'
    print(f'\nscan of {n_img:,} images / {len(samples):,} boxes: {took:.2f}s')


# ---------------------------------------------------------------- partial batches, persistence, deleted boxes

def test_partial_batch_failure_keeps_undo(scanned, ds, monkeypatch):
    """One file of a batch fails (bad op, write error): the others are written, backed up and undoable, the
    state is patched and re-derived, and the failures come back per file."""
    c, st = scanned, scanned.state
    before = {p.name: p.read_bytes() for p in (ds / 'labels').iterdir()}
    dup = by_type(c)['dup_box'][0]
    gone = dup['box_ids'][1]
    seq = st.edit_seq
    st.payloads['points'] = b'x'
    real = st.source.write_label

    def flaky(name, data, expect):
        if name == 'thin.txt':
            raise OSError('disk full')
        return real(name, data, expect)

    monkeypatch.setattr(st.source, 'write_label', flaky)
    items = [{'label': 'dup.txt', 'expect_sha1': c.files['dup.txt']['sha1'], 'ops': [{'op': 'dedupe', 'line': 1}]},
             {'label': 'oob.txt', 'expect_sha1': c.files['oob.txt']['sha1'], 'ops': [{'op': 'delete', 'line': 99}]},
             {'label': 'thin.txt', 'expect_sha1': c.files['thin.txt']['sha1'], 'ops': [{'op': 'delete', 'line': 0}]},
             {'label': 'nope.txt', 'ops': [{'op': 'delete', 'line': 0}]}]
    res = c.fix(items, 'partial')
    assert res['applied'] == 1 and res['files'] == 1 and res['undo_id']
    assert sorted(e['file'] for e in res['errors']) == ['nope.txt', 'oob.jpg', 'thin.jpg']
    assert 'out of range' in next(e['error'] for e in res['errors'] if e['file'] == 'oob.jpg')
    assert (ds / 'labels' / 'dup.txt').read_bytes() == b'1 0.5 0.5 0.3 0.3\n'
    assert (ds / 'labels' / 'thin.txt').read_bytes() == before['thin.txt']
    assert st.deleted[gone] and res['patch']['deleted'] == [gone]
    assert 'points' not in st.payloads and st.edit_seq > seq and 'dup_box' not in by_type(c)
    files = sorted(p.name for p in (c.backup_dir / res['undo_id']).iterdir())
    assert files == ['0.bin', 'meta.json']                          # the failed files left no backup behind
    h = c.history()
    assert len(h) == 1 and h[0]['files'] == 1
    u = c.undo(res['undo_id'])
    assert u['restored'] == 1 and u['errors'] == [] and not st.deleted[gone]
    assert {p.name: p.read_bytes() for p in (ds / 'labels').iterdir()} == before


def test_fix_route_partial_200_and_undo_errors(ds, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dataviz.server.app import create_app
    st = open_state(ds, tmp_path / 'ws3')
    with TestClient(create_app(st)) as client:
        assert wait_job(client, client.post('/api/curation/scan', json={}).json()['job_id'])['status'] == 'done'
        sha = {n: client.get('/api/curation/issues', params={'type': t}).json()['items'][0]['sha1']
               for n, t in (('dup.txt', 'dup_box'), ('oob.txt', 'oob'))}
        r = client.post('/api/curation/fix', json={'items': [
            {'label': 'dup.txt', 'expect_sha1': sha['dup.txt'], 'ops': [{'op': 'dedupe', 'line': 1}]},
            {'label': 'oob.txt', 'expect_sha1': sha['oob.txt'], 'ops': [{'op': 'bogus', 'line': 0}]}]})
        assert r.status_code == 200
        body = r.json()
        assert body['applied'] == 1 and body['undo_id'] and [e['file'] for e in body['errors']] == ['oob.jpg']
        # undo: a failing write stays in the backup (retry later), no 500
        real = st.source.write_label
        monkeypatch.setattr(st.source, 'write_label', lambda *a: (_ for _ in ()).throw(ValueError('nope')))
        u = client.post('/api/curation/undo', json={'undo_id': body['undo_id']})
        assert u.status_code == 200 and u.json()['restored'] == 0 and u.json()['errors'][0]['file'] == 'dup.jpg'
        monkeypatch.setattr(st.source, 'write_label', real)
        u = client.post('/api/curation/undo', json={'undo_id': body['undo_id']}).json()
        assert u['restored'] == 1 and u['errors'] == []
        assert (ds / 'labels' / 'dup.txt').read_text() == LABELS['dup']


def test_undo_history_and_ignores_survive_reindex(ds, tmp_path):
    """Backups, the ignore list and the filter live per dataset id (not per workspace fingerprint): after the
    fix changed the files and the dataset was re-indexed into a new workspace folder, they are all still there."""
    from dataviz.state import State
    root = tmp_path / 'wsroot'

    def open_ds(cache):
        st = State(LocalSource(ds), root / cache, {'none': None}, default_feature='none', workspace_root=root,
                   dataset_id='ds1')
        st._index_job(_Job())
        return st

    st = open_ds('v1')
    c = cur.Curation(st)
    c.scan()
    st.save_settings({**st.load_settings(), 'curation_ignored': ['oob|oob.txt|1'], 'filter': {'scope': 'box'},
                      'filter_presets': {'p': {}}, 'thumb-size': '96'})
    dup = by_type(c)['dup_box'][0]
    res = c.fix([{'label': 'dup.txt', 'expect_sha1': c._public(dup)['sha1'], 'ops': [dup['fixes'][0]]}], 'dedupe')
    assert (root / 'curation' / 'ds1' / 'curation_backup' / res['undo_id'] / 'meta.json').exists()
    assert not (root / 'v1' / 'curation_backup').exists()
    # re-index: a new State over the changed files, its own workspace folder
    st2 = open_ds('v2')
    assert st2.fingerprint != st.fingerprint
    s2 = st2.load_settings()
    assert s2['curation_ignored'] == ['oob|oob.txt|1'] and s2['filter'] == {'scope': 'box'} and 'p' in s2['filter_presets']
    assert 'thumb-size' not in s2                                   # per index version, as before
    c2 = cur.Curation(st2)
    c2.scan()
    h = c2.history()
    assert [x['undo_id'] for x in h] == [res['undo_id']] and h[0]['current'] is False and c2.n_fixes() == 0
    u = c2.undo(res['undo_id'])
    assert u['restored'] == 1 and u['stale'] is True and u['conflicts'] == []
    assert (ds / 'labels' / 'dup.txt').read_text() == LABELS['dup']
    assert len(by_type(c2)['dup_box']) == 1 and c2.history() == []


def test_backups_move_from_the_workspace_folder(ds, tmp_path):
    from dataviz.state import State
    root = tmp_path / 'wsroot'
    st = State(LocalSource(ds), root / 'v1', {'none': None}, default_feature='none')     # no dataset id: as before
    st._index_job(_Job())
    c = cur.Curation(st)
    c.scan()
    dup = by_type(c)['dup_box'][0]
    res = c.fix([{'label': 'dup.txt', 'expect_sha1': c._public(dup)['sha1'], 'ops': [dup['fixes'][0]]}])
    assert (root / 'v1' / 'curation_backup' / res['undo_id']).is_dir()
    st.workspace_root, st.dataset_id = root, 'ds1'
    c2 = cur.Curation(st)
    assert (root / 'curation' / 'ds1' / 'curation_backup' / res['undo_id'] / 'meta.json').exists()
    assert c2.history()[0]['undo_id'] == res['undo_id']


def test_deleted_boxes_left_out_of_server_results(state, monkeypatch):
    """Boxes a curation fix removed: not neighbours, near-duplicates, text-search hits, group members or eval
    ground truth."""
    from fastapi.testclient import TestClient
    from dataviz.ml import features as feats
    from dataviz.models import evaluate
    from dataviz.server.app import create_app
    st = state
    n = len(st.samples)
    gone = 1
    others = [i for i in range(n) if i != gone]
    nbr_idx = np.asarray([[gone if i != gone else 0] + [j for j in others if j != i][:3] for i in range(n)])
    nbr_sim = np.tile(np.asarray([0.999, 0.5, 0.4, 0.3], np.float32), (n, 1))
    st.scores['none'] = {'nbr_idx': nbr_idx, 'nbr_sim': nbr_sim, 'suspicion': np.zeros(n), 'suggested': st.labels.copy(),
                         'outlier': np.zeros(n)}
    st.deleted = np.zeros(n, bool)
    with TestClient(create_app(st)) as client:
        assert gone in client.get('/api/similar/0', params={'k': 2, 'feature': 'none'}).json()['id']
        assert client.get('/api/duplicates', params={'feature': 'none', 'threshold': 0.99,
                                                     'cross_image_only': False}).json()['pairs']
        st.deleted[gone] = True
        sim = client.get('/api/similar/0', params={'k': 2, 'feature': 'none'}).json()
        assert gone not in sim['id'] and len(sim['id']) == 2
        assert client.get('/api/duplicates', params={'feature': 'none', 'threshold': 0.99,
                                                     'cross_image_only': False}).json()['pairs'] == []
    # text search: the removed box is the best match, and is not returned
    emb = np.eye(n, dtype=np.float32)
    monkeypatch.setattr(st, 'get_features', lambda key, job=None: emb)
    monkeypatch.setattr(st, 'registry', {'none': SimpleNamespace(text_model='m', kind='clip')})
    monkeypatch.setattr(feats.TEXT, 'encode', lambda *a, **k: emb[gone] + 0.1 * emb[0])
    res = st.text_search('none', 'a cat', 3)
    assert gone not in res['id'] and res['id'][0] == 0
    # density groups: the removed box is in no group and no outlier (-2)
    st.projections['k'] = {'id': np.arange(n), 'xy': np.random.default_rng(0).normal(size=(n, 2)), 'info': {}}
    group, _ = st.groups('k', 2, False)
    assert len(group) == n and group[gone] == -2 and (np.delete(group, gone) >= -1).all()
    # evaluation: not ground truth
    preds = {'images': sorted(st.by_image), 'det': np.zeros((0, 6), np.float32), 'img': np.zeros(0, np.int64)}
    d = evaluate.Data(st.samples, len(st.classes), preds, np.zeros(0, np.int64), {}, st.deleted_mask())
    assert gone not in [s for ids in d.gt_by_img for s in ids]
    out = evaluate.outcome(d, 0.25, 0.5, 'all')
    assert out['status'][gone] == -1 and (np.delete(out['status'], gone) == 3).all()


def test_box_idx_follows_the_label_lines_through_fix_and_undo(scanned, ds):
    """box_idx is the box's 0-based line in its label file ("label file · line N", CSV export): it moves when a fix
    removes lines above it, is None for a removed box, and goes back to the original line on undo."""
    st, c = scanned.state, scanned
    line_of = lambda img: [st.samples[i]['box_idx'] for i in st.by_image[img]]   # noqa: E731
    assert line_of('malformed.jpg') == [2] and line_of('tiny.jpg') == [0, 2]       # bad lines are skipped, not counted
    a, b = st.by_image['dup.jpg']
    assert (st.samples[a]['box_idx'], st.samples[b]['box_idx']) == (0, 1)
    res = c.fix([{'label': 'dup.txt', 'expect_sha1': None, 'ops': [{'op': 'delete', 'line': 0}]}])
    assert res['patch']['deleted'] == [a]
    assert st.samples[a]['box_idx'] is None and st.samples[b]['box_idx'] == 0
    r2 = c.fix(c.default_fix_items('bad_line'))
    assert line_of('malformed.jpg') == [0]
    for r in (r2, res):
        c.undo(r['undo_id'])
    assert line_of('malformed.jpg') == [2]
    assert (st.samples[a]['box_idx'], st.samples[b]['box_idx']) == (0, 1)


def _fake_scores(st, pairs):
    """k-NN scores with one neighbour per box: pairs = {box id: (neighbour id, cosine)}, the rest point at
    themselves with similarity 0."""
    n = len(st.samples)
    nbr_idx = np.arange(n)[:, None].copy()
    nbr_sim = np.zeros((n, 1), np.float32)
    for i, (j, s) in pairs.items():
        nbr_idx[i, 0], nbr_sim[i, 0] = j, s
    st.scores['none'] = {'nbr_idx': nbr_idx, 'nbr_sim': nbr_sim, 'suspicion': np.zeros(n),
                         'suggested': st.labels.copy(), 'outlier': np.zeros(n)}


def test_split_leak_exact_and_embedding(state):
    st = state
    sid = {st.samples[i]['image']: i for i in range(len(st.samples))}
    # oob (val) ~ dup (train): a leak; thin (val) ~ tiny (val): same split; conflict ~ badcls: badcls has no split
    _fake_scores(st, {sid['oob.jpg']: (sid['dup.jpg'], 0.96), sid['thin.jpg']: (sid['tiny.jpg'], 0.99),
                      sid['conflict.jpg']: (sid['badcls.jpg'], 0.99)})
    splits = {'ok.jpg': 'train', 'ok_copy.jpg': 'val', 'dup.jpg': 'train', 'oob.jpg': 'val', 'thin.jpg': 'val',
              'tiny.jpg': 'val', 'conflict.jpg': 'test'}
    c = cur.Curation(st)
    c.scan(splits=splits)
    leaks = {it['id']: it for it in by_type(c).get('split_leak', [])}
    assert set(leaks) == {'split_leak|ok_copy.jpg|ok.jpg', 'split_leak|oob.jpg|dup.jpg'}
    assert leaks['split_leak|ok_copy.jpg|ok.jpg']['others'] == ['ok.jpg']
    emb = leaks['split_leak|oob.jpg|dup.jpg']
    assert emb['image'] == 'oob.jpg' and emb['others'] == ['dup.jpg'] and emb['severity'] == 'error'
    assert sid['oob.jpg'] in emb['box_ids'] and sid['dup.jpg'] in emb['box_ids'] and len(emb['boxes']) == 1
    # below the threshold or a removed box: no leak
    c2 = cur.Curation(st, {'leak_sim': 0.97})
    c2.scan(splits=splits)
    assert [it['id'] for it in by_type(c2)['split_leak']] == ['split_leak|ok_copy.jpg|ok.jpg']
    st.deleted = np.zeros(len(st.samples), bool)
    st.deleted[sid['dup.jpg']] = True
    c3 = cur.Curation(st)
    c3.scan(splits=splits)
    assert 'split_leak|oob.jpg|dup.jpg' not in {it['id'] for it in by_type(c3)['split_leak']}
    # a hand-crafted feature (box geometry, HOG, colour) is no evidence of a copy: only the exact copy is left
    st.deleted[:] = False
    st.registry = {'none': SimpleNamespace(group='simple')}
    c5 = cur.Curation(st)
    c5.scan(splits=splits)
    assert [it['id'] for it in by_type(c5)['split_leak']] == ['split_leak|ok_copy.jpg|ok.jpg']
    # no split known: the rule stays silent
    c4 = cur.Curation(st)
    c4.scan()
    assert 'split_leak' not in by_type(c4)


def test_split_leak_survives_cache_and_reaches_findings(state):
    from dataviz.models import insights
    c = cur.Curation(state)
    c.scan(splits={'ok.jpg': 'train', 'ok_copy.jpg': 'val'})
    again = cur.Curation(state)
    assert again.load_cached() and [it['id'] for it in by_type(again)['split_leak']] == ['split_leak|ok_copy.jpg|ok.jpg']
    lb = {'rows': [], 'best': None}
    f = insights.findings(lb, None, CLASSES, 'split.json', again)
    assert f[0]['severity'] == 3 and 'leak' in f[0]['title'] and f[0]['action']['kind'] == 'select'
    assert not [x for x in insights.findings(lb, None, CLASSES, 'split.json', None) if 'leak' in x['title']]


def test_split_leak_route_reads_the_split_folders(ds, tmp_path):
    from fastapi.testclient import TestClient
    from dataviz.core import config as vconfig
    from dataviz.server.app import create_app
    split = tmp_path / 'split'
    make_jpeg(split / 'train' / 'images' / 'ok.jpg')
    make_jpeg(split / 'val' / 'images' / 'ok_copy.jpg')
    cfg = vconfig.load_config()
    cfg['models']['split_source'] = str(split)
    from dataviz.state import State
    st = State(LocalSource(ds), tmp_path / 'ws3', {'none': None}, default_feature='none', cfg=cfg)
    st._index_job(_Job())
    with TestClient(create_app(st)) as client:
        r = client.post('/api/curation/scan', json={}).json()
        assert wait_job(client, r['job_id'])['status'] == 'done'
        s = client.get('/api/curation/summary').json()
        assert s['summary']['split_leak'] == {'n': 1, 'severity': 'error', 'group': 'splits',
                                              'label': 'Train / val leakage', 'bulk': False}
        page = client.get('/api/curation/issues', params={'type': 'split_leak'}).json()
        assert page['items'][0]['image'] == 'ok_copy.jpg'
