"""Many visualizer sessions served at once under /s/<id>/: each request sees its own session's State, access is
checked per session (404 for strangers, 403 below the needed role), the LRU of loaded States, the page and its
asset URLs, old links, per-user settings, and which jobs a user sees / may cancel."""

import threading

import numpy as np

import pytest

from authutil import account, client, login
from vizutil import _Job, geom_registry, make_viz_app, make_yolo, share, viz_session

HTML = {'accept': 'text/html'}


@pytest.fixture
def world(pg, tmp_path):
    """alice owns A (2 images, 3 boxes), bob owns B (1 image, 1 box); carol is nobody's member."""
    a = make_yolo(tmp_path / 'dsA', boxes=(('a1', '0 0.5 0.5 0.4 0.4\n1 0.3 0.3 0.2 0.2\n'), ('a2', '1 0.5 0.5 0.2 0.2\n')))
    b = make_yolo(tmp_path / 'dsB', classes=('car',), boxes=(('b1', '0 0.5 0.5 0.3 0.3\n'),))
    users = {n: account(n) for n in ('alice', 'bob', 'carol')}
    sid_a = viz_session(users['alice']['id'], 'A', a)
    sid_b = viz_session(users['bob']['id'], 'B', b)
    return {'tmp': tmp_path, 'users': users, 'A': sid_a, 'B': sid_b}


def signed(app, name):
    c = client(app)
    assert login(c, name).status_code == 200
    return c


def test_two_users_two_sessions(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    alice, bob = signed(app, 'alice'), signed(app, 'bob')
    A, B = world['A'], world['B']
    pa = alice.get(f'/s/{A}/api/points')
    pb = bob.get(f'/s/{B}/api/points')
    assert pa.status_code == 200 and pb.status_code == 200, (pa.text, pb.text)
    assert len(pa.json()['id']) == 3 and sorted(set(pa.json()['image'])) == ['a1.jpg', 'a2.jpg']
    assert pb.json()['image'] == ['b1.jpg']
    assert alice.get(f'/s/{A}/api/meta').json()['classes'] == ['cat', 'dog']
    assert bob.get(f'/s/{B}/api/meta').json()['classes'] == ['car']
    st = alice.get(f'/s/{A}/api/status').json()
    assert st['dataset']['id'] == A and st['my_role'] == 'owner' and st['user']['username'] == 'alice'
    assert 'email' not in st['user']
    home = alice.get('/api/status').json()                 # the home page: no session bound
    assert home['dataset'] is None and home['my_role'] is None
    assert alice.get('/api/points').status_code == 404     # a dataset route outside /s/<id>/
    assert sorted(ctx.states) == sorted([A, B])


def test_concurrent_first_requests_build_one_state(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    alice = signed(app, 'alice')
    codes = []

    def hit():
        codes.append(alice.get(f'/s/{world["A"]}/api/meta').status_code)
    ts = [threading.Thread(target=hit) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes == [200] * 6 and ctx.builds == [world['A']]


def test_access_per_session(world):
    _, app = make_viz_app(world['tmp'] / 'ws')
    A, u = world['A'], world['users']
    carol = signed(app, 'carol')
    for path in ('api/status', 'api/points', ''):
        r = carol.get(f'/s/{A}/{path}')
        assert r.status_code == 404, path
    assert 'Page not found' in carol.get(f'/s/{A}/', headers=HTML).text
    assert carol.get('/s/nope/api/status').status_code == 404
    share(A, u['alice']['id'], **{u['bob']['id']: 'viewer', u['carol']['id']: 'labeler'})
    bob = signed(app, 'bob')
    assert bob.get(f'/s/{A}/api/points').status_code == 200
    r = bob.post(f'/s/{A}/api/tags', json={'tag': 'x', 'ids': [0]})
    assert r.status_code == 403 and r.json()['detail'] == 'you can only view this session'
    assert bob.post(f'/s/{A}/api/curation/ignore', json={'ids': ['q']}).status_code == 403
    r = carol.post(f'/s/{A}/api/tags', json={'tag': 'x', 'ids': [0, 2]})
    assert r.status_code == 200 and r.json() == {'x': [0, 2]}
    assert bob.get(f'/s/{A}/api/tags').json() == {'x': [0, 2]}           # shared by the session (box_tags)
    assert carol.post(f'/s/{A}/api/curation/ignore', json={'ids': ['q']}).json() == {'ignored': ['q']}
    assert client(app).get(f'/s/{A}/api/points').status_code == 401       # signed out


def test_settings_are_per_user(world):
    _, app = make_viz_app(world['tmp'] / 'ws')
    A, u = world['A'], world['users']
    share(A, u['alice']['id'], **{u['bob']['id']: 'viewer'})
    alice, bob = signed(app, 'alice'), signed(app, 'bob')
    assert alice.post(f'/s/{A}/api/settings', json={'dot': 9, 'filter': {'q': 1}}).json() == {'ok': True}
    assert bob.post(f'/s/{A}/api/settings', json={'dot': 3}).json() == {'ok': True}
    assert alice.get(f'/s/{A}/api/settings').json() == {'dot': 9, 'filter': {'q': 1}}
    assert bob.get(f'/s/{A}/api/settings').json() == {'dot': 3}


def test_lru_eviction(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws', max_loaded=1)
    A, B, u = world['A'], world['B'], world['users']
    share(B, u['bob']['id'], **{u['alice']['id']: 'viewer'})
    alice = signed(app, 'alice')
    assert alice.get(f'/s/{A}/api/meta').status_code == 200
    st_a = ctx.states[A]
    closed = []
    st_a.close = lambda: closed.append(A)
    assert alice.get(f'/s/{B}/api/meta').status_code == 200
    assert list(ctx.states) == [B] and closed == [A]
    # a State with a running job is not evicted
    st_b = ctx.states[B]
    gate = threading.Event()
    job = ctx.jobs.submit(f'hold:{st_b.tag}', 'hold', lambda j: gate.wait(10), lane='io')
    try:
        assert alice.get(f'/s/{A}/api/meta').status_code == 200
        assert sorted(ctx.states) == sorted([A, B])
    finally:
        gate.set()
        job.wait(10)
    assert ctx.builds == [A, B, A]


def test_page_urls_and_redirects(world):
    _, app = make_viz_app(world['tmp'] / 'ws')
    A = world['A']
    alice = signed(app, 'alice')
    r = alice.get(f'/s/{A}/', headers=HTML)
    assert r.status_code == 200 and '../../static/js/theme-boot.js?h=' in r.text
    assert 'src="static/' not in r.text and 'href="static/' not in r.text
    assert '"../../static/js/main.js"' in r.text                          # import map keys too
    home = alice.get('/', headers=HTML).text
    assert 'src="static/js/theme-boot.js?h=' in home and '../../static/' not in home
    r = alice.get(f'/s/{A}', follow_redirects=False)
    assert r.status_code == 308 and r.headers['location'] == f'{A}/'
    r = alice.get(f'/?session={A}', follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'] == f's/{A}/'
    # the page marks the session as opened by me (home sort order)
    from dataviz.data import sessions
    row = next(x for x in sessions.listing(world['users']['alice'], 'viz') if x['id'] == A)
    assert row['opened_at']
    # assets also resolve under the session prefix
    assert alice.get(f'/s/{A}/static/js/theme-boot.js').status_code == 200


def test_jobs_visibility_and_cancel(world):
    ctx, app = make_viz_app(world['tmp'] / 'ws')
    A, u = world['A'], world['users']
    admin = account('root', admin=True)
    share(A, u['alice']['id'], **{u['bob']['id']: 'labeler'})
    gate = threading.Event()
    ctx.jobs.submit('block', 'block the io lane', lambda j: gate.wait(10), lane='io')
    try:
        mine = ctx.jobs.submit('scan-a', 'scan A', lambda j: 1, lane='io', owner=u['alice']['id'], session_id=A)
        other = ctx.jobs.submit('carol', "carol's", lambda j: 1, lane='io', owner=u['carol']['id'])
        alice, bob, carol, root = (signed(app, n) for n in ('alice', 'bob', 'carol', admin['username']))

        def ids(c):
            return {j['id'] for j in c.get('/api/jobs').json()['jobs']}
        assert mine.id in ids(bob) and other.id not in ids(bob)          # bob is a member of A
        assert mine.id not in ids(carol) and other.id in ids(carol)      # carol started her own
        assert {mine.id, other.id} <= ids(root)                          # admins see all
        assert carol.get(f'/api/jobs/{mine.id}').status_code == 404
        assert carol.post(f'/api/jobs/{mine.id}/cancel').status_code == 404
        r = bob.post(f'/api/jobs/{mine.id}/cancel')                       # a labeler, not the job's / session's owner
        assert r.status_code == 403
        assert alice.post(f'/api/jobs/{mine.id}/cancel').json() == {'ok': True}
        assert root.post(f'/api/jobs/{other.id}/cancel').json() == {'ok': True}
        # jobs queued through /s/<id>/ carry the user and the session
        r = bob.post(f'/s/{A}/api/curation/scan', json={})
        j = ctx.jobs.get(r.json()['job_id'])
        assert (j.owner, j.session_id) == (u['bob']['id'], A)
        assert bob.get(f'/s/{A}/api/status').json()['active']
    finally:
        gate.set()


def test_features_kept_in_pgvector(world):
    """A computed feature matrix is also stored in PostgreSQL; a lost .npy is rebuilt from there, not recomputed."""
    from dataviz.data.sources import LocalSource
    from dataviz.ml import features as feats, vectors
    from dataviz.state import State
    calls = []
    reg = geom_registry(calls)

    def fresh():
        st = State(LocalSource(world['tmp'] / 'dsA'), None, reg, default_feature='geom', workspace_root=world['tmp'] / 'ws')
        st._index_job(_Job())
        return st
    st = fresh()
    X = np.array(st.get_features('geom', _Job()))
    assert calls == [3] and vectors.exists(st.fingerprint, 'geom')
    np.testing.assert_allclose(vectors.load(st.fingerprint, 'geom'), X, rtol=1e-3)
    (st.features_dir / f'{feats.cache_name("geom")}.npy').unlink()
    st2 = fresh()
    np.testing.assert_allclose(np.asarray(st2.get_features('geom')), X, rtol=1e-3)      # no job: never computed
    assert calls == [3] and (st2.features_dir / f'{feats.cache_name("geom")}.npy').exists()
