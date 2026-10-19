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


def allows(have, need):
    return have is not None and RANK[have] >= RANK[need]


def check(user, sid, need='viewer', conn=None, kind=None, what='session'):
    """The caller's role, or HTTPException: 401 signed out, 404 no access (or unknown), 403 role too low."""
    if user is None:
        raise HTTPException(401, 'sign in first')
    have = role(user, sid, conn, kind)
    if have is None:
        raise HTTPException(404, f'no {what} {sid!r}')
    if not allows(have, need):
        raise HTTPException(403, {'viewer': 'you can only view this session',
                                  'labeler': 'only the owner of this session can do that'}.get(have,
                                                                                               'not allowed'))
    return have
