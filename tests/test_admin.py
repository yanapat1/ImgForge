"""Admin API (server/routes/admin.py) and the user repository rules behind it: admin-only access, the
last-admin guard, deleting users with / without a transfer, server settings, the audit log."""

import pytest
from sqlalchemy import text

from authutil import PW, account, client, login, make_app, signed_in
from conftest import make_user


@pytest.fixture
def app(pg):
    return make_app()


def test_admin_only(app):
    c, _ = signed_in(app, 'alice')
    for method, path in (('get', '/api/admin/users'), ('post', '/api/admin/users'),
                         ('patch', '/api/admin/users/x'), ('delete', '/api/admin/users/x'),
                         ('get', '/api/admin/settings'), ('put', '/api/admin/settings'),
                         ('get', '/api/admin/audit')):
        kw = {'json': {}} if method in ('post', 'patch', 'put') else {}
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 403 and r.json()['detail'] == 'admins only', (method, path)
    assert client(app).get('/api/admin/users').status_code == 401


def test_create_list_update(app):
    adm, root = signed_in(app, 'root', admin=True)
    r = adm.post('/api/admin/users', json={'username': 'Bob', 'email': 'bob@x.y', 'display_name': 'Bob',
                                           'password': PW})
    assert r.status_code == 200
    bob = r.json()['user']
    assert adm.post('/api/admin/users', json={'username': 'bob', 'email': 'b2@x.y', 'display_name': 'B',
                                              'password': PW}).status_code == 400
    users = {u['username']: u for u in adm.get('/api/admin/users').json()['users']}
    assert set(users) == {'root', 'bob'}
    assert set(users['bob']) == {'id', 'username', 'email', 'name', 'is_admin', 'is_active', 'avatar_ver',
                                 'created', 'last_login', 'n_sessions'}
    assert users['root']['last_login'] is not None and users['bob']['last_login'] is None

    cb = client(app)
    assert login(cb, 'bob').status_code == 200
    r = adm.patch(f'/api/admin/users/{bob["id"]}', json={'is_admin': True, 'display_name': 'Robert'})
    assert r.json()['user']['is_admin'] is True and r.json()['user']['display_name'] == 'Robert'
    assert cb.get('/api/ping').json()['user']['is_admin'] is True        # no stale cache
    assert adm.patch(f'/api/admin/users/{bob["id"]}', json={'is_admin': 'yes'}).status_code == 400
    assert adm.patch('/api/admin/users/nope', json={'is_admin': True}).status_code == 404

    # password reset: the new password works and every session of that user ends
    r = adm.patch(f'/api/admin/users/{bob["id"]}', json={'password': 'reset by admin 1', 'display_name': 'Bobby'})
    assert r.status_code == 200
    assert cb.get('/api/ping').status_code == 401
    assert login(client(app), 'bob', 'reset by admin 1').status_code == 200
    # a bad password changes nothing (one transaction)
    assert adm.patch(f'/api/admin/users/{bob["id"]}', json={'password': 'short', 'display_name': 'X'}).status_code == 400
    assert {u['username']: u for u in adm.get('/api/admin/users').json()['users']}['bob']['name'] == 'Bobby'

    # deactivate: sessions revoked, login refused
    cb = client(app)
    login(cb, 'bob', 'reset by admin 1')
    assert adm.patch(f'/api/admin/users/{bob["id"]}', json={'is_active': False}).status_code == 200
    assert cb.get('/api/ping').status_code == 401
    assert login(client(app), 'bob', 'reset by admin 1').status_code == 401


def test_last_admin_guard(app):
    from dataviz.auth import users
    adm, root = signed_in(app, 'root', admin=True)
    for body in ({'is_admin': False}, {'is_active': False}):
        r = adm.patch(f'/api/admin/users/{root["id"]}', json=body)
        assert r.status_code == 400 and r.json()['detail'] == 'at least one active admin must remain'
    r = adm.delete(f'/api/admin/users/{root["id"]}')
    assert r.status_code == 400 and 'active admin' in r.json()['detail']
    other = account('other', admin=True)
    users.update(other['id'], is_active=False)                    # an inactive admin does not count
    with pytest.raises(ValueError):
        users.update(root['id'], is_admin=False)
    users.update(other['id'], is_active=True)
    assert adm.patch(f'/api/admin/users/{root["id"]}', json={'is_admin': False}).status_code == 200
    with pytest.raises(ValueError):
        users.delete(other['id'])


def test_delete_with_transfer(app):
    from dataviz import db
    from dataviz.data import sessions
    adm, root = signed_in(app, 'root', admin=True)
    alice, bob, carol = make_user('alice'), make_user('bob'), make_user('carol')
    with db.begin() as c:
        for cid, owner, name in (('c1', alice['id'], 'Bucket'), ('c2', bob['id'], 'bucket')):
            c.execute(text("INSERT INTO app.connections(id, owner_id, kind, name) VALUES (:i, :o, 's3', :n)"),
                      {'i': cid, 'o': owner, 'n': name})
        c.execute(text("INSERT INTO app.models(id, owner_id, name) VALUES ('m1', :o, 'Det')"), {'o': alice['id']})
    s1 = sessions.create('viz', alice['id'], 'Shelf')
    s2 = sessions.create('label', alice['id'], 'Shelf')
    sessions.create('viz', bob['id'], 'Shelf')
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.viz_sessions(session_id, source_kind, connection_id, bucket)
                          VALUES (:s, 's3', 'c1', 'b')"""), {'s': s1['id']})
    sessions.set_sharing(s2['id'], 'private', [{'user_id': bob['id'], 'role': 'labeler'},
                                               {'user_id': carol['id'], 'role': 'viewer'}], alice['id'])

    assert adm.delete(f'/api/admin/users/{alice["id"]}', params={'transfer_to': alice['id']}).status_code == 400
    assert adm.delete(f'/api/admin/users/{alice["id"]}', params={'transfer_to': 'nobody'}).status_code == 400
    r = adm.delete(f'/api/admin/users/{alice["id"]}', params={'transfer_to': bob['id']})
    assert r.json() == {'ok': True, 'transferred': 2, 'deleted': 0}
    assert sessions.get(s1['id'])['owner_id'] == bob['id'] and sessions.get(s1['id'])['name'] == 'Shelf (2)'
    assert sessions.get(s2['id'])['name'] == 'Shelf'
    assert [m['username'] for m in sessions.members(s2['id'])] == ['carol']     # bob is the owner now
    with db.connect() as c:
        conns = dict(c.execute(text('SELECT id, name FROM app.connections WHERE owner_id = :o'),
                               {'o': bob['id']}).all())
        assert c.execute(text('SELECT owner_id FROM app.models')).scalar() == bob['id']
        assert c.execute(text('SELECT count(*) FROM app.users WHERE id = :u'), {'u': alice['id']}).scalar() == 0
    assert conns == {'c1': 'Bucket (2)', 'c2': 'bucket'}


def test_delete_without_transfer(app):
    from dataviz import db
    from dataviz.data import sessions
    adm, root = signed_in(app, 'root', admin=True)
    alice = make_user('alice')
    with db.begin() as c:
        c.execute(text("INSERT INTO app.connections(id, owner_id, kind, name) VALUES ('c1', :o, 's3', 'B')"),
                  {'o': alice['id']})
    s1 = sessions.create('viz', alice['id'], 'Shelf')
    sessions.create('label', alice['id'], 'Shelf')
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.viz_sessions(session_id, source_kind, connection_id)
                          VALUES (:s, 's3', 'c1')"""), {'s': s1['id']})
    r = adm.delete(f'/api/admin/users/{alice["id"]}', params={'transfer_to': ''})
    assert r.json() == {'ok': True, 'transferred': 0, 'deleted': 2}
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM app.sessions')).scalar() == 0
        assert c.execute(text('SELECT count(*) FROM app.connections')).scalar() == 0
    assert adm.delete(f'/api/admin/users/{alice["id"]}').status_code == 404


def test_settings_and_can_add_connections(app):
    from fastapi import Depends
    from dataviz.auth import deps
    adm, root = signed_in(app, 'root', admin=True)
    c, _ = signed_in(app, 'alice')

    @app.post('/api/conn-test')
    def add_conn(user=Depends(deps.can_add_connections)):
        return {'ok': True}

    assert adm.get('/api/admin/settings').json() == {'signup_enabled': True, 'who_can_add_connections': 'everyone'}
    assert c.post('/api/conn-test').status_code == 200
    r = adm.put('/api/admin/settings', json={'who_can_add_connections': 'admins'})
    assert r.json() == {'signup_enabled': True, 'who_can_add_connections': 'admins'}
    r = c.post('/api/conn-test')
    assert r.status_code == 403 and r.json()['detail'] == 'only admins can add connections on this server'
    assert adm.post('/api/conn-test').status_code == 200
    assert adm.put('/api/admin/settings', json={'who_can_add_connections': 'nobody'}).status_code == 400
    assert adm.put('/api/admin/settings', json={'other': 1}).status_code == 400
    assert adm.put('/api/admin/settings', json={'signup_enabled': False}).json()['signup_enabled'] is False


def test_audit_endpoint(app):
    adm, root = signed_in(app, 'root', admin=True)
    r = adm.post('/api/admin/users', json={'username': 'bob', 'email': 'bob@x.y', 'display_name': 'Bob',
                                           'password': PW})
    bob = r.json()['user']
    adm.patch(f'/api/admin/users/{bob["id"]}', json={'password': 'reset by admin 1'})
    adm.put('/api/admin/settings', json={'signup_enabled': False})
    adm.delete(f'/api/admin/users/{bob["id"]}')
    ev = adm.get('/api/admin/audit', params={'limit': 50}).json()['events']
    actions = [e['action'] for e in ev]
    assert actions[:4] == ['user_delete', 'settings', 'user_update', 'user_create'] and 'login' in actions
    assert ev[2]['meta'] == {'password': 'reset'}                     # never the password itself
    assert ev[0]['user'] == {'id': root['id'], 'username': 'root'}
    assert set(ev[0]) == {'at', 'user', 'action', 'target', 'meta', 'ip'}


def test_deps_markers():
    from dataviz.auth import deps
    for f in (deps.current_user, deps.admin_user, deps.can_add_connections, deps.require_session('labeler'),
              deps.require_bound('owner')):
        assert getattr(f, '__dataviz_auth__', False) is True


def test_require_bound(app):
    from fastapi import Depends
    from dataviz.auth import context, deps

    @app.get('/api/bound/{need}')
    def bound_route(need: str, b=Depends(deps.require_bound('labeler'))):
        return {'sid': b.session_id}

    c, _ = signed_in(app, 'alice')
    assert c.get('/api/bound/x').json()['detail'] == 'open the session first'
    tok = context.BOUND.set(context.Bound('s1', 'viewer', None))
    try:
        dep = deps.require_bound('labeler')
        with pytest.raises(Exception) as e:
            dep(user={'id': 'u'})
        assert e.value.status_code == 403
        assert deps.require_bound('viewer')(user={'id': 'u'}).session_id == 's1'
    finally:
        context.BOUND.reset(tok)
