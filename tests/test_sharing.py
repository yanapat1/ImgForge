"""Sharing API (server/routes/sharing.py) and user search (server/routes/users.py)."""

import pytest

from authutil import client, make_app, signed_in


@pytest.fixture
def app(pg):
    return make_app()


def test_sharing_owner_only(app):
    from dataviz.data import sessions
    ca, alice = signed_in(app, 'alice')
    cb, bob = signed_in(app, 'bob')
    cc, carol = signed_in(app, 'carol')
    cr, root = signed_in(app, 'root', admin=True)
    s = sessions.create('label', alice['id'], 'Shelf')
    url = f'/api/sessions/{s["id"]}/sharing'

    r = ca.get(url).json()
    assert r == {'visibility': 'private', 'members': [], 'my_role': 'owner',
                 'owner': {'id': alice['id'], 'username': 'alice', 'name': 'Alice', 'avatar_ver': 0}}
    assert cb.get(url).status_code == 404                       # stranger: does not even learn it exists
    assert cb.put(url, json={'visibility': 'global', 'members': []}).status_code == 404

    r = ca.put(url, json={'visibility': 'global', 'members': [{'user_id': bob['id'], 'role': 'labeler'},
                                                              {'user_id': carol['id'], 'role': 'viewer'}]})
    assert r.status_code == 200
    assert r.json()['visibility'] == 'global'
    assert [(m['username'], m['role']) for m in r.json()['members']] == [('bob', 'labeler'), ('carol', 'viewer')]
    assert 'email' not in r.json()['members'][0]

    r = cb.get(url)                                             # a labeler member is not the owner
    assert r.status_code == 403
    assert cb.put(url, json={'visibility': 'private', 'members': []}).status_code == 403
    assert cr.get(url).json()['my_role'] == 'owner'             # admins may

    row = sessions.listing(bob, 'label', 'shared')[0]
    assert row['my_role'] == 'labeler' and [m['username'] for m in row['members']] == ['bob', 'carol']

    for bad in ({'visibility': 'public', 'members': []}, {'visibility': 'private', 'members': 'x'},
                {'visibility': 'private', 'members': [{'user_id': bob['id'], 'role': 'owner'}]},
                {'visibility': 'private', 'members': [{'user_id': 'nobody', 'role': 'viewer'}]}):
        assert ca.put(url, json=bad).status_code == 400, bad
    assert client(app).get(url).status_code == 401
    assert ca.get('/api/sessions/nope/sharing').status_code == 404

    from dataviz.auth import audit
    ev = [e for e in audit.list_events(50) if e['action'] == 'sharing']
    assert ev and ev[0]['target'] == s['id']


def test_user_search(app):
    from dataviz.auth import users
    c, alice = signed_in(app, 'alice')
    for name, dn in (('bob', 'Bob 100% Real'), ('bo_b', 'Under Score'), ('carl', 'Carl'), ('bobo', 'Inactive')):
        users.create(name, f'{name}@example.test', dn, 'correct horse battery')
    users.update(users.by_login('bobo')['id'], is_active=False)
    assert c.get('/api/users/search', params={'q': 'b'}).json() == {'users': []}         # < 2 characters
    found = c.get('/api/users/search', params={'q': 'bo'}).json()['users']
    assert [u['username'] for u in found] == ['bo_b', 'bob']
    assert all(set(u) == {'id', 'username', 'name', 'avatar_ver'} for u in found)    # never the email
    assert [u['username'] for u in c.get('/api/users/search', params={'q': '0%'}).json()['users']] == ['bob']
    assert c.get('/api/users/search', params={'q': '%%'}).json()['users'] == []
    assert [u['username'] for u in c.get('/api/users/search', params={'q': 'o_'}).json()['users']] == ['bo_b']
    assert c.get('/api/users/search', params={'q': 'example'}).json()['users'] == []   # not by email
    for i in range(15):
        users.create(f'user{i:02d}', f'u{i}@example.test', 'Someone', 'correct horse battery')
    assert len(c.get('/api/users/search', params={'q': 'user'}).json()['users']) == 10
