"""Tiers (auth/tiers.py, the /api/admin/tiers routes): the seeded tiers, admin CRUD, moving users between tiers,
each permission refusing a tier without it, Contributor's model files loading, and the .env admin sync
(auth/bootstrap.sync_env_admin)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from authutil import PW, client, login, make_app, signed_in
from conftest import _TestUserMiddleware, make_user
from test_security import _app

XRW = {'X-Requested-With': 'dataviz'}


def _set_tier(user, tid):
    from dataviz import db
    from dataviz.auth import tokens
    with db.begin() as c:
        c.execute(text('UPDATE app.users SET tier_id = :t WHERE id = :u'), {'t': tid, 'u': user['id']})
    tokens.clear_cache()
    return {**user, 'tier_id': tid}


def test_seeded_tiers_and_defaults(pg):
    from dataviz.auth import tiers
    tiers.clear_cache()
    by = {t['id']: t for t in tiers.list_all()}
    assert set(by) == {'viewer', 'member', 'contributor'}
    assert by['member']['is_default'] and by['viewer']['permissions'] == []
    assert set(by['contributor']['permissions']) == set(by['member']['permissions']) | {'model.upload'}
    u = make_user('newbie')                                       # no tier: the default one (today's rights)
    assert tiers.tier_of(u)['id'] == 'member' and not tiers.has(u, 'model.upload')
    assert tiers.has({'id': 'x', 'is_admin': True}, 'model.upload')     # admins: everything


def test_admin_tier_crud(pg):
    app = make_app()
    adm, _ = signed_in(app, 'root', admin=True)
    r = adm.get('/api/admin/tiers').json()
    assert {p['key'] for p in r['permissions']} >= {'dataset.create', 'model.upload'}
    t = adm.post('/api/admin/tiers', json={'name': 'Annotators', 'description': 'label only',
                                           'permissions': ['label.create', 'label.create']})
    assert t.status_code == 200, t.text
    t = t.json()['tier']
    assert t['permissions'] == ['label.create']
    assert adm.post('/api/admin/tiers', json={'name': 'annotators'}).status_code == 400      # name taken
    assert adm.post('/api/admin/tiers', json={'name': 'X', 'permissions': ['root']}).status_code == 400
    r = adm.patch(f'/api/admin/tiers/{t["id"]}', json={'name': 'Labelers', 'permissions': ['llm.use']})
    assert r.json()['tier']['name'] == 'Labelers' and r.json()['tier']['permissions'] == ['llm.use']
    assert adm.patch('/api/admin/tiers/nope', json={'name': 'Y'}).status_code == 404

    # a user moved to the tier, then the tier deleted: they fall back to the default tier
    from dataviz.auth import users
    bob = users.create('bob', 'bob@x.y', 'Bob', PW, tier_id=t['id'])
    assert bob['tier_id'] == t['id']
    assert adm.delete('/api/admin/tiers/member').status_code == 400                       # the default stays
    r = adm.delete(f'/api/admin/tiers/{t["id"]}')
    assert r.status_code == 200 and r.json()['moved_users'] == 1
    assert users.get(bob['id'])['tier_id'] is None

    # make another tier the default; unsetting one directly is refused
    assert adm.patch('/api/admin/tiers/member', json={'is_default': False}).status_code == 400
    assert adm.patch('/api/admin/tiers/viewer', json={'is_default': True}).json()['tier']['is_default']
    from dataviz.auth import tiers
    assert tiers.default_tier()['id'] == 'viewer'
    events = [e['action'] for e in adm.get('/api/admin/audit').json()['events']]
    assert {'tier_create', 'tier_update', 'tier_delete'} <= set(events)


def test_admin_sets_user_tier(pg):
    app = make_app()
    adm, _ = signed_in(app, 'root', admin=True)
    r = adm.post('/api/admin/users', json={'username': 'carol', 'email': 'c@x.y', 'display_name': 'Carol',
                                           'password': PW, 'tier_id': 'contributor'})
    assert r.status_code == 200 and r.json()['user']['tier_id'] == 'contributor'
    cid = r.json()['user']['id']
    cc = client(app)
    assert login(cc, 'carol').status_code == 200
    assert cc.get('/api/ping').json()['user']['tier_id'] == 'contributor'
    assert adm.patch(f'/api/admin/users/{cid}', json={'tier_id': 'viewer'}).json()['user']['tier_id'] == 'viewer'
    assert cc.get('/api/ping').json()['user']['tier_id'] == 'viewer'                       # no stale cache
    assert adm.patch(f'/api/admin/users/{cid}', json={'tier_id': 'nope'}).status_code == 400
    assert adm.patch(f'/api/admin/users/{cid}', json={'tier_id': ''}).json()['user']['tier_id'] is None


def test_viewer_tier_is_refused_member_allowed(pg, tmp_path):
    viewer = _set_tier(make_user('vic'), 'viewer')
    app, _ = _app(tmp_path, viewer)
    with TestClient(app, headers=XRW) as c:
        for method, path, body in (('post', '/api/datasets', {'kind': 'local', 'path': str(tmp_path)}),
                                   ('post', '/api/label/projects', {'name': 'p', 'tasks': []}),
                                   ('post', '/api/connections', {'kind': 's3', 'host': 'x'}),
                                   ('post', '/api/models', {'family': 'hf', 'format': 'hf', 'path': 'a/b'}),
                                   ('get', '/api/fs/browse', None)):
            r = getattr(c, method)(path, **({'json': body} if body is not None else {}))
            assert r.status_code == 403 and 'your tier (Viewer)' in r.json()['detail'], (path, r.text)
    member = make_user('meg')
    app, _ = _app(tmp_path, member)
    with TestClient(app, headers=XRW) as c:
        r = c.post('/api/models', json={'name': 'b', 'family': 'hf', 'format': 'hf', 'path': 'a/b'})
        assert r.status_code == 200, r.text                       # a hub id: Member may
        assert c.get('/api/models/discover').status_code == 403  # files: model.upload only


def test_contributor_may_add_and_run_file_models(pg, tmp_path):
    from dataviz.models import runners
    contrib = _set_tier(make_user('cora'), 'contributor')
    app, mr = _app(tmp_path, contrib)
    weights = tmp_path / 'w.onnx'
    weights.write_bytes(b'junk')
    with TestClient(app, headers=XRW) as c:
        assert c.get('/api/models/browse', params={'path': str(tmp_path)}).status_code != 403   # 400: not a root here
        r = c.post('/api/models', json={'name': 'det', 'family': 'custom', 'format': 'onnx', 'path': str(weights)})
        assert r.status_code == 200, r.text
    m = mr.model(r.json()['id'], owner_id=contrib['id'])
    assert runners._trusted_owner(m)
    _set_tier(contrib, 'member')                                  # moved down: the saved model stops loading
    assert not runners._trusted_owner(m)


def test_env_admin_sync(pg):
    from dataviz.auth import bootstrap, passwords, users
    env = {'DATAVIZ_ADMIN_USERNAME': 'admin', 'DATAVIZ_ADMIN_PASSWORD': 'admin123', 'DATAVIZ_ADMIN_RESET': 'true'}
    assert bootstrap.sync_env_admin({**env, 'DATAVIZ_ADMIN_RESET': 'false'}, log=lambda m: None) is None
    u = bootstrap.sync_env_admin(env, log=lambda m: None)                         # created
    assert u['is_admin'] and passwords.verify(users.by_login('admin')['password_hash'], 'admin123')
    users.set_password(u['id'], 'changed in the UI')
    users.update(u['id'], is_admin=True)
    u2 = bootstrap.sync_env_admin(env, log=lambda m: None)                        # existing: password back
    assert u2['id'] == u['id'] and passwords.verify(users.by_login('admin')['password_hash'], 'admin123')
    app = make_app()
    assert login(client(app), 'admin', 'admin123').status_code == 200
    with pytest.raises(ValueError):
        passwords.check_policy('x' * 7)
    passwords.check_policy('admin123', 'admin', 'admin@localhost')
