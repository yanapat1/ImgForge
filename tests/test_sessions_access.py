"""app.sessions + sharing + the access rule (auth/access.py, data/sessions.py)."""

import pytest
from starlette.exceptions import HTTPException

from conftest import make_user


def test_roles_and_listing(pg):
    from dataviz.auth import access
    from dataviz.data import sessions
    alice, bob, carol, root = make_user('alice'), make_user('bob'), make_user('carol'), make_user('root', admin=True)
    s1 = sessions.create('label', alice['id'], 'Shelf A', tags=['x'])
    s2 = sessions.create('label', bob['id'], 'Shelf A')            # same name, other owner: fine
    with pytest.raises(ValueError):
        sessions.create('label', alice['id'], 'shelf a')            # same owner + kind, any case: taken
    assert sessions.create('label', alice['id'], 'Shelf A', auto_name=True)['name'] == 'Shelf A (2)'
    assert sessions.create('viz', alice['id'], 'Shelf A')['kind'] == 'viz'   # kinds have separate names

    assert access.role(alice, s1['id']) == 'owner'
    assert access.role(bob, s1['id']) is None
    assert access.role(root, s1['id']) == 'owner'
    with pytest.raises(HTTPException) as e:
        access.check(bob, s1['id'])
    assert e.value.status_code == 404

    sessions.set_sharing(s1['id'], 'private', [{'user_id': bob['id'], 'role': 'labeler'},
                                               {'user_id': alice['id'], 'role': 'viewer'}], alice['id'])
    assert access.role(bob, s1['id']) == 'labeler'
    assert access.role(alice, s1['id']) == 'owner'                 # the owner is never demoted to a member
    assert access.role(carol, s1['id']) is None
    with pytest.raises(HTTPException) as e:
        access.check(bob, s1['id'], 'owner')
    assert e.value.status_code == 403

    sessions.set_sharing(s2['id'], 'global', [], bob['id'])
    assert access.role(carol, s2['id']) == 'viewer'

    ids = lambda u, scope='all': {d['id'] for d in sessions.listing(u, 'label', scope)}   # noqa: E731
    assert s1['id'] in ids(bob, 'shared') and s2['id'] not in ids(bob, 'shared')
    assert ids(carol) == {s2['id']} and ids(carol, 'global') == {s2['id']}
    assert s1['id'] not in ids(root)                               # admin: own + shared + global by default
    assert s1['id'] in ids(root, 'everyone')                       # … and every session on request
    assert ids(carol, 'everyone') == ids(carol)                    # 'everyone' is admin-only

    card = sessions.header(bob, s1['id'])
    assert card['owner']['name'] == 'Alice' and card['my_role'] == 'labeler'
    assert [m['id'] for m in card['members']] == [bob['id']]
    with pytest.raises(ValueError):
        sessions.set_sharing(s1['id'], 'private', [{'user_id': 'nope', 'role': 'viewer'}], alice['id'])


def test_transfer_and_delete(pg):
    from dataviz.data import sessions
    alice, bob = make_user('alice'), make_user('bob')
    s = sessions.create('viz', alice['id'], 'D')
    sessions.set_sharing(s['id'], 'private', [{'user_id': bob['id'], 'role': 'viewer'}], alice['id'])
    sessions.update(s['id'], owner_id=bob['id'])
    assert sessions.get(s['id'])['owner_id'] == bob['id'] and sessions.members(s['id']) == []
    sessions.mark_opened(alice['id'], s['id'])
    sessions.delete(s['id'])
    assert sessions.get(s['id']) is None
    with pytest.raises(KeyError):
        sessions.delete(s['id'])
