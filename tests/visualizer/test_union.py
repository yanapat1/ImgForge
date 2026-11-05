"""Combined datasets (data/union.py): members assembled from their own workspaces (ids offset, names prefixed,
classes unified by name, crops hard-linked, features concatenated), writes routed back to the owning member with
its own class ids, access needs every member, a member in use cannot be deleted, and the index lock."""

import os
import threading

import numpy as np
import pytest

from authutil import account, client, login
from vizutil import _Job, geom_registry, make_viz_app, make_yolo, share, union_session, viz_session


@pytest.fixture
def world(pg, tmp_path):
    a = make_yolo(tmp_path / 'dsA', classes=('cat', 'dog'),
                  boxes=(('a1', '0 0.5 0.5 0.4 0.4\n1 0.3 0.3 0.2 0.2\n'), ('a2', '1 0.5 0.5 0.2 0.2\n')))
    b = make_yolo(tmp_path / 'dsB', classes=('Dog', 'bird'), boxes=(('b1', '0 0.5 0.5 0.3 0.3\n1 0.2 0.2 0.1 0.1\n'),))
    users = {n: account(n) for n in ('alice', 'bob', 'carol')}
    A = viz_session(users['alice']['id'], 'A', a)
    B = viz_session(users['alice']['id'], 'B', b)
    U = union_session(users['alice']['id'], 'A vs B', [(A, 'A'), (B, 'B')])
    return {'tmp': tmp_path, 'users': users, 'A': A, 'B': B, 'U': U, 'a': a, 'b': b}


def signed(app, name):
    c = client(app)
    assert login(c, name).status_code == 200
    return c


def test_assemble(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    alice = signed(app, 'alice')
    U = world['U']
    meta = alice.get(f'/s/{U}/api/meta').json()
    assert meta['classes'] == ['cat', 'dog', 'bird']                   # 'Dog' of B is A's 'dog'
    assert [m['alias'] for m in meta['members']] == ['A', 'B']
    pts = alice.get(f'/s/{U}/api/points').json()
    assert pts['image'] == ['A/a1.jpg', 'A/a1.jpg', 'A/a2.jpg', 'B/b1.jpg', 'B/b1.jpg']
    assert pts['cls'] == [0, 1, 1, 1, 2] and pts['origin'] == [0, 0, 0, 1, 1]
    assert pts['id'] == [0, 1, 2, 3, 4]
    st = ctx.state_for(U)
    assert [s['src_id'] for s in st.samples] == [0, 1, 2, 0, 1]
    # crops are hard links into the members' own workspaces (= the ones their sessions use)
    mws = world['tmp'] / 'ws' / st.members[1]['ws']
    assert os.stat(st.cache_dir / 'crops' / '000003.jpg').st_ino == os.stat(mws / 'crops' / '000000.jpg').st_ino
    # every image hashed (images.json of the union), same content -> same group id
    from dataviz.data import imagehash
    imgs = imagehash.load(st.cache_dir)['images']
    assert set(imgs) == {'A/a1.jpg', 'A/a2.jpg', 'B/b1.jpg'} and all(e['q'] for e in imgs.values())


def test_features_concatenate(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    calls = []
    st = ctx.state_for(world['U'])
    st.registry = __import__('dataviz.ml.features', fromlist=['FeatureRegistry']).FeatureRegistry(geom_registry(calls))
    emb = st.get_features('geom', _Job())
    assert calls == [3, 2]                                            # computed per member, in its workspace
    want = np.asarray([[s['area'], s['aspect']] for s in st.samples], np.float32)
    assert np.allclose(np.asarray(emb), want)
    st.features.clear()
    calls.clear()
    st2 = ctx.state_for(world['A'])                                   # the member's own session: a cache hit
    st2.registry = st.registry
    assert st2.get_features('geom', _Job()) is not None and calls == []


def test_write_routes_to_member(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    st = ctx.state_for(world['U'])
    src = st.source
    text = src.read_label('B/b1.txt')
    assert text.split('\n')[0].split()[0] == '1' and text.split('\n')[1].split()[0] == '2'
    from dataviz.data.sources import Conflict, sha1_bytes
    sha = sha1_bytes(src.read_label_bytes('B/b1.txt'))
    src.write_label('B/b1.txt', '2 0.5 0.5 0.3 0.3\n', sha)            # union 'bird' = B's class 1
    assert (world['b'] / 'labels' / 'b1.txt').read_text() == '1 0.5 0.5 0.3 0.3\n'
    with pytest.raises(ValueError, match='cat'):                       # 'cat' is not one of B's classes
        src.write_label('B/b1.txt', '0 0.5 0.5 0.3 0.3\n')
    with pytest.raises(Conflict):
        src.write_label('B/b1.txt', '2 0.5 0.5 0.3 0.3\n', 'stale')


def test_access_needs_every_member(world):
    u = world['users']
    share(world['U'], u['alice']['id'], **{u['bob']['id']: 'viewer'})
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    bob = signed(app, 'bob')
    assert bob.get(f'/s/{world["U"]}/api/meta').status_code == 404      # cannot see A or B
    share(world['A'], u['alice']['id'], **{u['bob']['id']: 'viewer'})
    share(world['B'], u['alice']['id'], **{u['bob']['id']: 'viewer'})
    assert bob.get(f'/s/{world["U"]}/api/meta').status_code == 200


def test_member_in_use_cannot_be_deleted(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    alice = signed(app, 'alice')
    r = alice.delete(f'/api/datasets/{world["A"]}')
    assert r.status_code == 400 and 'A vs B' in r.json()['detail']


def test_create_via_api_reuses(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    alice, carol = signed(app, 'alice'), signed(app, 'carol')
    body = {'kind': 'union', 'reuse': True, 'parts': [{'id': world['A']}, {'id': world['B']}]}
    r = alice.post('/api/datasets', json=body)
    assert r.status_code == 200 and r.json()['id'] == world['U']
    r = carol.post('/api/datasets', json={**body, 'reuse': False})
    assert r.status_code == 400                                       # carol sees neither member
    r = alice.post('/api/datasets', json={'kind': 'union', 'parts': [{'id': world['U']}, {'id': world['A']}]})
    assert r.status_code == 400 and 'combined' in r.json()['detail']


def test_stale_when_member_changes(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    st = ctx.state_for(world['U'])
    fp = st.fingerprint
    lbl = world['b'] / 'labels' / 'b1.txt'
    lbl.write_text('0 0.5 0.5 0.3 0.3\n')
    os.utime(lbl, (1, 1))
    assert st.source.fingerprint() != fp


def test_index_lock_serializes(tmp_path):
    from dataviz.data.indexer import index_lock
    order = []

    def hold(tag):
        with index_lock(tmp_path / 'ws'):
            order.append(f'{tag}+')
            threading.Event().wait(0.05)
            order.append(f'{tag}-')
    ts = [threading.Thread(target=hold, args=(t,)) for t in 'ab']
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert order in (['a+', 'a-', 'b+', 'b-'], ['b+', 'b-', 'a+', 'a-'])
    with index_lock(tmp_path / 'ws'):                                  # re-entrant on one thread
        with index_lock(tmp_path / 'ws'):
            pass


def _union(tmp_path, *members):
    from dataviz.data.sources import LocalSource
    from dataviz.data.union import Member, UnionSource
    ms = [Member(a, f'sid{i}', a, LocalSource(p), i) for i, (a, p) in enumerate(members)]
    return UnionSource({'id': 'u1', 'name': 'u'}, ms, tmp_path / 'ws')


def test_writes_never_change_untouched_classes(tmp_path):
    """A member with classes that fold alike ('Car', 'car') keeps both apart: moving one box through the union
    leaves every other line exactly as it was (review finding: they used to collapse into one class)."""
    a = make_yolo(tmp_path / 'a', classes=('Car', 'car', 'person'),
                  boxes=(('x', '0 0.5 0.5 0.1 0.1\n1 0.2 0.2 0.1 0.1\n2 0.3 0.3 0.1 0.1\n'),))
    b = make_yolo(tmp_path / 'b', classes=('car',), boxes=(('y', '0 0.5 0.5 0.2 0.2\n'),))
    u = _union(tmp_path, ('A', a), ('B', b))
    assert u.read_classes() == ['Car', 'car', 'person']
    text = u.read_label('A/x.txt')
    moved = text.replace('0.3 0.3', '0.35 0.3')
    u.write_label('A/x.txt', moved)
    assert (a / 'labels' / 'x.txt').read_text() == '0 0.5 0.5 0.1 0.1\n1 0.2 0.2 0.1 0.1\n2 0.35 0.3 0.1 0.1\n'
    assert u.read_label('B/y.txt').split()[0] == '0'            # B's 'car' is A's 'Car' (first of that name)


def test_class_ids_past_the_list_each_get_their_own_class(tmp_path):
    a = make_yolo(tmp_path / 'a', classes=('cat', 'dog'), boxes=(('x', '5 0.5 0.5 0.1 0.1\n3 0.2 0.2 0.1 0.1\n'),))
    b = make_yolo(tmp_path / 'b', classes=('cat',), boxes=(('y', '0 0.5 0.5 0.2 0.2\n'),))
    u = _union(tmp_path, ('A', a), ('B', b))
    text = u.read_label('A/x.txt')
    cls = [int(ln.split()[0]) for ln in text.splitlines()]
    names = u.read_classes()
    assert [names[c] for c in cls] == ['class_5', 'class_3']
    u.write_label('A/x.txt', text)
    assert (a / 'labels' / 'x.txt').read_text() == '5 0.5 0.5 0.1 0.1\n3 0.2 0.2 0.1 0.1\n'


def test_hidden_members_hide_the_union_everywhere(world):
    """A viewer of the union who cannot see its members gets no card, no cover crop (review finding)."""
    u = world['users']
    share(world['U'], u['alice']['id'], **{u['bob']['id']: 'viewer'})
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    alice, bob = signed(app, 'alice'), signed(app, 'bob')
    assert alice.get(f'/s/{world["U"]}/api/meta').status_code == 200         # builds the workspace (covers)
    assert alice.get(f'/api/cover/{world["U"]}/0').status_code == 200
    assert bob.get(f'/api/cover/{world["U"]}/0').status_code == 404
    assert world['U'] not in [d['id'] for d in bob.get('/api/datasets').json()['datasets']]


def test_another_users_union_never_blocks_a_delete(world):
    """bob combines alice's shared dataset A with his own: alice can still delete A; bob's union reports it."""
    u = world['users']
    share(world['A'], u['alice']['id'], **{u['bob']['id']: 'viewer'})
    C = viz_session(u['bob']['id'], 'C', world['b'])
    BU = union_session(u['bob']['id'], 'A vs C', [(world['A'], 'A'), (C, 'C')])
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    alice, bob = signed(app, 'alice'), signed(app, 'bob')
    r = alice.delete(f'/api/datasets/{world["A"]}')
    assert r.status_code == 400 and 'A vs B' in r.json()['detail'] and 'A vs C' not in r.json()['detail']
    assert alice.delete(f'/api/datasets/{world["U"]}').status_code == 200          # her own union first
    assert alice.delete(f'/api/datasets/{world["A"]}').status_code == 200
    r = bob.get(f'/s/{BU}/api/meta')
    assert r.status_code == 409 and 'deleted' in r.json()['detail']


def test_deleting_a_user_with_unions(world):
    from dataviz.auth import users
    users.delete(world['users']['alice']['id'])                       # owns A, B and the union over them
