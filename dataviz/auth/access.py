"""
Who may do what with a session (a card of the home page, either kind):

    owner    the session's owner, or any admin       everything (rename, tags, sharing, delete, structure)
    labeler  a member added as labeler                view + label / fix boxes / curation fixes / predict
    viewer   a member added as viewer, or ANY user    view only (visualizer, images, label page read-only)
             when the session is global
    None     nobody else: the API answers 404, so a private session's id does not leak

The rank order is viewer < labeler < owner; check(user, sid, 'labeler') passes for labelers and owners.
"""

from sqlalchemy import text
from starlette.exceptions import HTTPException

from .. import db

RANK = {'viewer': 1, 'labeler': 2, 'owner': 3}
LEVELS = tuple(RANK)


def role_for(user, owner_id, visibility, member_role):
    """The role from one session row; the pure rule every query below uses."""
    if user is None:
        return None
    if user.get('is_admin') or owner_id == user['id']:
        return 'owner'
    if member_role in ('viewer', 'labeler'):
        return member_role
    if visibility == 'global':
        return 'viewer'
    return None


_ROLE_SQL = text("""
    SELECT s.owner_id, s.visibility, s.kind, m.role
    FROM app.sessions s
    LEFT JOIN app.session_members m ON m.session_id = s.id AND m.user_id = :uid
    WHERE s.id = :sid""")


def role(user, sid, conn=None, kind=None):
    """The caller's role on session `sid` (None: no access or no such session / not of `kind`)."""
    if user is None or not sid:
        return None
    if conn is None:
        with db.connect() as c:
            return role(user, sid, c, kind)
    row = conn.execute(_ROLE_SQL, {'sid': sid, 'uid': user['id']}).first()
    if row is None or (kind is not None and row.kind != kind):
        return None
    return role_for(user, row.owner_id, row.visibility, row.role)


_PARTS_SQL = text('SELECT member_id FROM app.union_members WHERE union_id = :sid')


def viz_role(user, sid, conn=None):
    """role() of a visualize session; a combined dataset (data/union.py) also needs access to EVERY member: a
    union shared with someone who cannot see one of its datasets is a 404 to them (no member leaks through it).
    Writes through a union are checked per member again (UnionSource.check_write)."""
    if conn is None:
        with db.connect() as c:
            return viz_role(user, sid, c)
    have = role(user, sid, conn, 'viz')
    if have is None:
        return None
    for mid in conn.execute(_PARTS_SQL, {'sid': sid}).scalars():
        if role(user, mid, conn, 'viz') is None:
            return None
    return have


def member_writer(user_fn):
    """check_write for a UnionSource: PermissionError unless the current user (user_fn()) may label that member.
    No user (scripts, the single-dataset test app): allowed."""
    def check(member_id):
        user = user_fn()
        if user is None or user.get('id') is None:
            return
        have = role(user, member_id, kind='viz')
        if not allows(have, 'labeler'):
            raise PermissionError('you cannot change the labels of this member dataset: ask its owner for '
                                  'labeler access')
    return check


def allows(have, need):
    return have is not None and RANK[have] >= RANK[need]


def check(user, sid, need='viewer', conn=None, kind=None, what='session'):
    """The caller's role, or HTTPException: 401 signed out, 404 no access (or unknown), 403 role too low."""
    if user is None:
        raise HTTPException(401, 'sign in first')
    # seeing a visualize session (its cover crops too) needs every member of a combined dataset; managing it
    # (rename, delete) stays its owner's even after a member was unshared from them
    have = viz_role(user, sid, conn) if kind == 'viz' and need == 'viewer' else role(user, sid, conn, kind)
    if have is None:
        raise HTTPException(404, f'no {what} {sid!r}')
    if not allows(have, need):
        raise HTTPException(403, {'viewer': 'you can only view this session',
                                  'labeler': 'only the owner of this session can do that'}.get(have,
                                                                                               'not allowed'))
    return have
