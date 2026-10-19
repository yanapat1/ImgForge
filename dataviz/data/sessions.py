"""
The sessions table (app.sessions): one row per card of the home page, for both kinds — 'viz' (a dataset
opened in the visualizer; its source in app.viz_sessions, data/connections.py) and 'label' (a labeling
project; app.label_projects, labeling/projects.py). Ownership, visibility, tags, members and "last opened by
me" live here, so both kinds share one access model (auth/access.py) and one card header.

Names are unique per (owner, kind), case-insensitively. Times leave this module as epoch seconds (what the
page and the old JSON files used).
"""

import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .. import db
from ..auth.access import role_for
from ..core.names import clean_tags, name_taken, unique_name

KINDS = ('viz', 'label')
VISIBILITY = ('private', 'global')
ROLES = ('viewer', 'labeler')
SCOPES = ('all', 'mine', 'shared', 'global', 'everyone')
MAX_MEMBERS = 200
KIND_WORD = {'viz': 'visualize', 'label': 'label'}


def epoch(dt):
    return dt.timestamp() if dt is not None else None


def _row(r):
    return {'id': r.id, 'kind': r.kind, 'owner_id': r.owner_id, 'name': r.name, 'visibility': r.visibility,
            'tags': list(r.tags or []), 'created': epoch(r.created_at), 'updated': epoch(r.updated_at)}


def _conn(conn):
    """A transaction of our own unless the caller passed one (so a caller can make several changes atomic)."""
    if conn is not None:
        from contextlib import nullcontext
        return nullcontext(conn)
    return db.begin()


# ------------------------------------------------------------------------------------------ rows
def get(sid, conn=None):
    """The session row as a dict, or None."""
    with _conn(conn) as c:
        r = c.execute(text('SELECT * FROM app.sessions WHERE id = :id'), {'id': sid}).first()
    return _row(r) if r else None


def names(owner_id, kind, conn=None, exclude=None):
    with _conn(conn) as c:
        rows = c.execute(text('SELECT name FROM app.sessions WHERE owner_id = :o AND kind = :k AND id <> :x'),
                         {'o': owner_id, 'k': kind, 'x': exclude or ''}).scalars().all()
    return list(rows)


def check_name(owner_id, kind, name, sid=None, conn=None):
    """ValueError when this owner already has a session of this kind called `name` (ignoring case)."""
    if name_taken(name, names(owner_id, kind, conn, exclude=sid)):
        raise ValueError(f'a {KIND_WORD[kind]} session called “{name.strip()}” already exists: pick another name')


def free_name(owner_id, kind, base, conn=None, nth=None):
    """`base`, or `base (2)`, `base (3)` … : the first name this owner does not use yet (auto-generated names)."""
    others = names(owner_id, kind, conn)
    return unique_name(base, others, nth) if nth else unique_name(base, others)


def create(kind, owner_id, name, *, sid=None, tags=(), visibility='private', conn=None, auto_name=False):
    """Insert a session row; returns it. auto_name: a taken name becomes "name (2)" instead of a ValueError."""
    if kind not in KINDS:
        raise ValueError(f'unknown session kind {kind!r}')
    if visibility not in VISIBILITY:
        raise ValueError('visibility must be private or global')
    name = (name or '').strip()
    if not name:
        raise ValueError('give the session a name')
    tags = clean_tags(list(tags or []))
    sid = sid or uuid.uuid4().hex
    with _conn(conn) as c:
        if auto_name:
            name = free_name(owner_id, kind, name, c)
        else:
            check_name(owner_id, kind, name, conn=c)
        try:
            r = c.execute(text("""INSERT INTO app.sessions(id, kind, owner_id, name, visibility, tags)
                                  VALUES (:id, :kind, :owner, :name, :vis, :tags) RETURNING *"""),
                          {'id': sid, 'kind': kind, 'owner': owner_id, 'name': name, 'vis': visibility,
                           'tags': tags}).first()
        except IntegrityError as e:        # a concurrent create won the name (unique index)
            raise ValueError(f'a {KIND_WORD[kind]} session called “{name}” already exists: pick another name') from e
    return _row(r)


def update(sid, *, name=None, tags=None, visibility=None, owner_id=None, conn=None):
    """Change the given fields (None = keep); returns the row. KeyError for an unknown session."""
    with _conn(conn) as c:
        cur = get(sid, c)
        if cur is None:
            raise KeyError(f'no session {sid!r}')
        sets = {}
        new_owner = owner_id or cur['owner_id']
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError('give the session a name')
            if name.casefold() != cur['name'].casefold() or new_owner != cur['owner_id']:
                check_name(new_owner, cur['kind'], name, sid=sid, conn=c)
            sets['name'] = name
        if owner_id is not None and owner_id != cur['owner_id']:
            if name is None:
                check_name(owner_id, cur['kind'], cur['name'], sid=sid, conn=c)
            sets['owner_id'] = owner_id
            # the new owner is no longer a member of their own session
            c.execute(text('DELETE FROM app.session_members WHERE session_id = :s AND user_id = :u'),
                      {'s': sid, 'u': owner_id})
        if tags is not None:
            sets['tags'] = clean_tags(tags)
        if visibility is not None:
            if visibility not in VISIBILITY:
                raise ValueError('visibility must be private or global')
            sets['visibility'] = visibility
        if not sets:
            return cur
        assign = ', '.join(f'{k} = :{k}' for k in sets)
        try:
            r = c.execute(text(f'UPDATE app.sessions SET {assign}, updated_at = now() WHERE id = :id RETURNING *'),
                          {**sets, 'id': sid}).first()
        except IntegrityError as e:
            raise ValueError('that name is already used by another session of the same owner') from e
    return _row(r)


def touch(sid, conn=None):
    """updated_at = now (a label session was edited: the card sorts by it)."""
    with _conn(conn) as c:
        c.execute(text('UPDATE app.sessions SET updated_at = now() WHERE id = :id'), {'id': sid})


def delete(sid, conn=None):
    """Delete the row; members, opens, prefs, chats and (for label sessions) every lbl row cascade."""
    with _conn(conn) as c:
        n = c.execute(text('DELETE FROM app.sessions WHERE id = :id'), {'id': sid}).rowcount
    if not n:
        raise KeyError(f'no session {sid!r}')


def mark_opened(user_id, sid, conn=None):
    with _conn(conn) as c:
        c.execute(text("""INSERT INTO app.session_opens(user_id, session_id) VALUES (:u, :s)
                          ON CONFLICT (user_id, session_id) DO UPDATE SET opened_at = now()"""),
                  {'u': user_id, 's': sid})


# ------------------------------------------------------------------------------------------ sharing
def members(sid, conn=None):
    """[{id, username, display_name, avatar_ver, role}] in the order they were added."""
    with _conn(conn) as c:
        rows = c.execute(text("""
            SELECT u.id, u.username, u.display_name, u.avatar_ver, m.role
            FROM app.session_members m JOIN app.users u ON u.id = m.user_id
            WHERE m.session_id = :s ORDER BY m.added_at, u.username"""), {'s': sid}).all()
    return [dict(r._mapping) for r in rows]


def set_sharing(sid, visibility, member_list, by_user_id, conn=None):
    """Replace visibility + the member list ([{user_id, role}]) in one transaction. The owner cannot be a
    member, unknown / deactivated users are a ValueError. Returns {visibility, members}."""
    if visibility not in VISIBILITY:
        raise ValueError('visibility must be private or global')
    wanted = {}
    for m in member_list or []:
        uid, r = (m or {}).get('user_id'), (m or {}).get('role')
        if not uid or r not in ROLES:
            raise ValueError('each member needs a user and a role (viewer or labeler)')
        wanted[uid] = r
    if len(wanted) > MAX_MEMBERS:
        raise ValueError(f'a session can have at most {MAX_MEMBERS} members')
    with _conn(conn) as c:
        cur = c.execute(text('SELECT owner_id FROM app.sessions WHERE id = :s FOR UPDATE'), {'s': sid}).first()
        if cur is None:
            raise KeyError(f'no session {sid!r}')
        wanted.pop(cur.owner_id, None)                 # the owner already has every right
        if wanted:
            ok = set(c.execute(text('SELECT id FROM app.users WHERE id = ANY(:ids) AND is_active'),
                               {'ids': list(wanted)}).scalars().all())
            missing = set(wanted) - ok
            if missing:
                raise ValueError(f'unknown or deactivated user(s): {", ".join(sorted(missing))}')
        c.execute(text('UPDATE app.sessions SET visibility = :v, updated_at = now() WHERE id = :s'),
                  {'v': visibility, 's': sid})
        c.execute(text('DELETE FROM app.session_members WHERE session_id = :s AND NOT (user_id = ANY(:keep))'),
                  {'s': sid, 'keep': list(wanted)})
        for uid, r in wanted.items():
            c.execute(text("""INSERT INTO app.session_members(session_id, user_id, role, added_by)
                              VALUES (:s, :u, :r, :by)
                              ON CONFLICT (session_id, user_id) DO UPDATE SET role = EXCLUDED.role"""),
                      {'s': sid, 'u': uid, 'r': r, 'by': by_user_id})
        return {'visibility': visibility, 'members': members(sid, c)}


# ------------------------------------------------------------------------------------------ listing
_LIST_SQL = """
    SELECT s.*, u.username AS owner_username, u.display_name AS owner_name, u.avatar_ver AS owner_avatar_ver,
           m.role AS member_role, o.opened_at
    FROM app.sessions s
    JOIN app.users u ON u.id = s.owner_id
    LEFT JOIN app.session_members m ON m.session_id = s.id AND m.user_id = :uid
    LEFT JOIN app.session_opens o ON o.session_id = s.id AND o.user_id = :uid
    WHERE s.kind = :kind AND {where}
    ORDER BY COALESCE(o.opened_at, s.updated_at) DESC"""

_SCOPE_WHERE = {
    'all': "(s.owner_id = :uid OR m.user_id IS NOT NULL OR s.visibility = 'global')",
    'mine': 's.owner_id = :uid',
    'shared': 'm.user_id IS NOT NULL',
    'global': "s.visibility = 'global'",
    'everyone': 'TRUE',                          # admins only (checked below)
}


def listing(user, kind, scope='all', conn=None, ids=None):
    """The sessions of `kind` this user sees in `scope`, newest-opened first, each with the card header:
    {..row, owner: {id, username, name, avatar_ver}, members: [...], my_role, opened_at}.
    scope: all (mine + shared with me + global) | mine | shared | global | everyone (admins: every session).
    ids: only these session ids (still filtered by access)."""
    if scope not in SCOPES:
        raise ValueError(f'scope must be one of {", ".join(SCOPES)}')
    if scope == 'everyone' and not user.get('is_admin'):
        scope = 'all'
    where = _SCOPE_WHERE[scope]
    params = {'uid': user['id'], 'kind': kind}
    if ids is not None:
        where += ' AND s.id = ANY(:ids)'
        params['ids'] = list(ids)
    with _conn(conn) as c:
        rows = c.execute(text(_LIST_SQL.format(where=where)), params).all()
        mem = {}
        if rows:
            for m in c.execute(text("""
                    SELECT m.session_id, u.id, u.username, u.display_name, u.avatar_ver, m.role
                    FROM app.session_members m JOIN app.users u ON u.id = m.user_id
                    WHERE m.session_id = ANY(:ids) ORDER BY m.added_at, u.username"""),
                    {'ids': [r.id for r in rows]}).all():
                mem.setdefault(m.session_id, []).append({'id': m.id, 'username': m.username,
                                                         'name': m.display_name, 'avatar_ver': m.avatar_ver,
                                                         'role': m.role})
    out = []
    for r in rows:
        d = _row(r)
        d['owner'] = {'id': r.owner_id, 'username': r.owner_username, 'name': r.owner_name,
                      'avatar_ver': r.owner_avatar_ver}
        d['members'] = mem.get(r.id, [])
        d['my_role'] = role_for(user, r.owner_id, r.visibility, r.member_role)
        d['opened_at'] = epoch(r.opened_at)
        out.append(d)
    return out


def header(user, sid, conn=None):
    """The card header (listing() entry) of one session, or None when the user cannot see it."""
    with _conn(conn) as c:
        r = get(sid, c)
        if r is None:
            return None
        rows = listing(user, r['kind'], 'everyone' if user.get('is_admin') else 'all', c, ids=[sid])
    return rows[0] if rows else None
