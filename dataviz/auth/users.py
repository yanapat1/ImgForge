"""
The user repository (app.users): create, look up, edit, list, search, delete (with or without handing the
user's sessions to someone else), passwords and the per-account login lockout.

Usernames are stored lower-case; usernames and emails are unique ignoring case. ValueError carries a message
meant for the person (the routes answer 400 with it). "At least one active admin" is enforced here, inside the
transaction that would break it, with the admin rows locked (SELECT ... FOR UPDATE) so two concurrent
demotions cannot both pass.
"""

import re
import uuid
from contextlib import nullcontext

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .. import db
from ..core.names import unique_name
from . import passwords, tokens

USERNAME_RE = re.compile(r'^[a-z0-9._-]{3,32}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+$')
LOCK_AFTER = 5                 # consecutive failures before the account locks
LOCK_MAX_MIN = 15
LAST_ADMIN = 'at least one active admin must remain'


def _conn(conn):
    return nullcontext(conn) if conn is not None else db.begin()


# ------------------------------------------------------------------------------------------ shapes
def public(r, email=True):
    """The user dict the app passes around: {id, username, email, display_name, is_admin, avatar_ver}."""
    m = r if isinstance(r, dict) else r._mapping
    out = {'id': m['id'], 'username': m['username'], 'email': m['email'], 'display_name': m['display_name'],
           'is_admin': bool(m['is_admin']), 'avatar_ver': int(m['avatar_ver'] or 0)}
    if not email:
        del out['email']
    return out


def card(r):
    """What other users may see of someone (search results, member lists): never the email."""
    m = r if isinstance(r, dict) else r._mapping
    return {'id': m['id'], 'username': m['username'], 'name': m['display_name'],
            'avatar_ver': int(m['avatar_ver'] or 0)}


def _epoch(dt):
    return dt.timestamp() if dt is not None else None


# ------------------------------------------------------------------------------------------ validation
def clean_username(v):
    v = (v or '').strip().lower() if isinstance(v, str) else ''
    if not USERNAME_RE.match(v):
        raise ValueError('a username has 3 to 32 characters: lower-case letters, digits, dot, dash or underscore')
    return v


def clean_email(v):
    v = (v or '').strip() if isinstance(v, str) else ''
    if len(v) > 254 or not EMAIL_RE.match(v):
        raise ValueError('enter a valid email address')
    return v


def clean_display_name(v):
    v = ' '.join(v.split()) if isinstance(v, str) else ''
    if not 1 <= len(v) <= 80:
        raise ValueError('the display name must have 1 to 80 characters')
    return v


def _check_unique(c, username=None, email=None, exclude=None):
    if username is not None and c.execute(text(
            'SELECT 1 FROM app.users WHERE lower(username) = lower(:v) AND id <> :x'),
            {'v': username, 'x': exclude or ''}).first():
        raise ValueError(f'the username “{username}” is taken')
    if email is not None and c.execute(text(
            'SELECT 1 FROM app.users WHERE lower(email) = lower(:v) AND id <> :x'),
            {'v': email, 'x': exclude or ''}).first():
        raise ValueError('an account with this email already exists')


# ------------------------------------------------------------------------------------------ reads
def row(uid, conn=None):
    """The full row (password hash, lockout, is_active ...) as a mapping, or None."""
    if not uid:
        return None
    with _conn(conn) as c:
        r = c.execute(text('SELECT * FROM app.users WHERE id = :id'), {'id': uid}).first()
    return dict(r._mapping) if r else None


def get(uid, conn=None):
    r = row(uid, conn)
    return public(r) if r else None


def by_login(login, conn=None):
    """The full row whose username or email equals `login` (ignoring case), or None."""
    login = (login or '').strip() if isinstance(login, str) else ''
    if not login or len(login) > 254:
        return None
    with _conn(conn) as c:
        r = c.execute(text("""SELECT * FROM app.users WHERE lower(username) = lower(:l) OR lower(email) = lower(:l)
                              ORDER BY (lower(username) = lower(:l)) DESC LIMIT 1"""), {'l': login}).first()
    return dict(r._mapping) if r else None


def any_admin(conn=None):
    with _conn(conn) as c:
        return c.execute(text('SELECT 1 FROM app.users WHERE is_admin AND is_active LIMIT 1')).first() is not None


def list_all(conn=None):
    """Every user for the admin page, with how many sessions each owns."""
    with _conn(conn) as c:
        rows = c.execute(text("""
            SELECT u.*, (SELECT count(*) FROM app.sessions s WHERE s.owner_id = u.id) AS n_sessions
            FROM app.users u ORDER BY lower(u.username)""")).all()
    return [{'id': r.id, 'username': r.username, 'email': r.email, 'name': r.display_name,
             'is_admin': bool(r.is_admin), 'is_active': bool(r.is_active), 'avatar_ver': int(r.avatar_ver or 0),
             'created': _epoch(r.created_at), 'last_login': _epoch(r.last_login_at),
             'n_sessions': int(r.n_sessions)} for r in rows]


def _like(q):
    return q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def search(q, limit=10, conn=None):
    """Active users whose username or display name contains `q` (ignoring case); card form, never the email."""
    q = (q or '').strip() if isinstance(q, str) else ''
    if len(q) < 2:
        return []
    pat = '%' + _like(q[:80]) + '%'
    with _conn(conn) as c:
        rows = c.execute(text(r"""
            SELECT id, username, display_name, avatar_ver FROM app.users
            WHERE is_active AND (username ILIKE :p ESCAPE '\' OR display_name ILIKE :p ESCAPE '\')
            ORDER BY (lower(username) LIKE lower(:pre) ESCAPE '\') DESC, lower(username)
            LIMIT :n"""), {'p': pat, 'pre': _like(q[:80]) + '%', 'n': int(min(limit, 10))}).all()
    return [card(r) for r in rows]


# ------------------------------------------------------------------------------------------ writes
def create(username, email, display_name, password, is_admin=False, conn=None):
    """A new active account; returns its public dict. ValueError (human message) when anything is invalid."""
    username, email = clean_username(username), clean_email(email)
    display_name = clean_display_name(display_name)
    passwords.check_policy(password, username, email)
    h = passwords.hash_password(password)
    uid = uuid.uuid4().hex
    try:
        with _conn(conn) as c:
            _check_unique(c, username, email)
            r = c.execute(text("""INSERT INTO app.users(id, username, email, display_name, password_hash, is_admin)
                                  VALUES (:id, :u, :e, :d, :h, :a) RETURNING *"""),
                          {'id': uid, 'u': username, 'e': email, 'd': display_name, 'h': h,
                           'a': bool(is_admin)}).first()
    except IntegrityError as e:          # a concurrent sign-up won the name (unique index)
        raise ValueError('that username or email is already taken') from e
    return public(r)


def _lock_admins(c):
    """Lock the active admin rows; returns their ids."""
    return set(c.execute(text('SELECT id FROM app.users WHERE is_admin AND is_active FOR UPDATE')).scalars().all())


def _guard_last_admin(c, uid):
    admins = _lock_admins(c)
    if uid in admins and not (admins - {uid}):
        raise ValueError(LAST_ADMIN)


def update(uid, *, username=None, email=None, display_name=None, is_admin=None, is_active=None, conn=None):
    """Change the given fields (None = keep); returns the public dict. KeyError for an unknown user."""
    revoke = False
    try:
        with _conn(conn) as c:
            cur = c.execute(text('SELECT * FROM app.users WHERE id = :id FOR UPDATE'), {'id': uid}).first()
            if cur is None:
                raise KeyError(f'no user {uid!r}')
            sets = {}
            if username is not None:
                sets['username'] = clean_username(username)
            if email is not None:
                sets['email'] = clean_email(email)
            if display_name is not None:
                sets['display_name'] = clean_display_name(display_name)
            _check_unique(c, sets.get('username'), sets.get('email'), exclude=uid)
            if is_admin is not None:
                sets['is_admin'] = bool(is_admin)
            if is_active is not None:
                sets['is_active'] = bool(is_active)
            if cur.is_admin and cur.is_active and (sets.get('is_admin') is False or sets.get('is_active') is False):
                _guard_last_admin(c, uid)
            if sets.get('is_active') is False and cur.is_active:
                revoke = True
            if sets.get('is_active') is True:
                sets.update(failed_logins=0, locked_until=None)
            if sets:
                assign = ', '.join(f'{k} = :{k}' for k in sets)
                cur = c.execute(text(f'UPDATE app.users SET {assign} WHERE id = :id RETURNING *'),
                                {**sets, 'id': uid}).first()
            if revoke:
                tokens.revoke_user(uid, conn=c)
    except IntegrityError as e:
        raise ValueError('that username or email is already taken') from e
    tokens.evict_user(uid)              # cached copies carry the old name / admin flag
    return public(cur)


def set_password(uid, password, conn=None):
    """Policy-checked new password (does not revoke sessions: the caller decides which go)."""
    with _conn(conn) as c:
        cur = c.execute(text('SELECT username, email FROM app.users WHERE id = :id'), {'id': uid}).first()
        if cur is None:
            raise KeyError(f'no user {uid!r}')
        passwords.check_policy(password, cur.username, cur.email)
        c.execute(text("""UPDATE app.users SET password_hash = :h, failed_logins = 0, locked_until = NULL
                          WHERE id = :id"""), {'h': passwords.hash_password(password), 'id': uid})


def set_avatar(uid, data, mime='image/webp', conn=None):
    """Store (or with data=None remove) the avatar; bumps avatar_ver so the versioned URL changes."""
    with _conn(conn) as c:
        r = c.execute(text("""UPDATE app.users SET avatar = :a, avatar_mime = :m, avatar_ver = avatar_ver + 1
                              WHERE id = :id RETURNING *"""),
                      {'a': data, 'm': mime if data is not None else None, 'id': uid}).first()
    if r is None:
        raise KeyError(f'no user {uid!r}')
    tokens.evict_user(uid)
    return public(r)


def avatar(uid, conn=None):
    """(bytes, mime) or None."""
    with _conn(conn) as c:
        r = c.execute(text('SELECT avatar, avatar_mime FROM app.users WHERE id = :id'), {'id': uid}).first()
    if r is None or r.avatar is None:
        return None
    return bytes(r.avatar), r.avatar_mime or 'image/webp'


# ------------------------------------------------------------------------------------------ login bookkeeping
def is_locked(r):
    """True while the account (full row) is locked after repeated failures."""
    from datetime import datetime, timezone
    lu = r.get('locked_until')
    return lu is not None and lu > datetime.now(timezone.utc)


def record_failure(uid, conn=None):
    """One more failed password; from the 5th in a row the account locks 1, 2, 4, 8, then 15 minutes."""
    with _conn(conn) as c:
        c.execute(text("""
            UPDATE app.users SET failed_logins = failed_logins + 1,
                locked_until = CASE WHEN failed_logins + 1 >= :n
                    THEN now() + make_interval(mins => LEAST(:mx, power(2, LEAST(failed_logins + 1 - :n, 4))::int))
                    ELSE locked_until END
            WHERE id = :id"""), {'id': uid, 'n': LOCK_AFTER, 'mx': LOCK_MAX_MIN})


def record_success(uid, password=None, stored_hash=None, conn=None):
    """Reset the failure count, stamp last_login_at, and re-hash when the argon2 parameters changed."""
    new_hash = None
    if password is not None and stored_hash and passwords.needs_rehash(stored_hash):
        new_hash = passwords.hash_password(password)
    with _conn(conn) as c:
        c.execute(text("""UPDATE app.users SET failed_logins = 0, locked_until = NULL, last_login_at = now(),
                              password_hash = COALESCE(:h, password_hash) WHERE id = :id"""),
                  {'h': new_hash, 'id': uid})
    return new_hash is not None


# ------------------------------------------------------------------------------------------ delete
def _move_owned(c, table, uid, to, unique):
    """Hand every `table` row of `uid` to `to`, renaming "name (2)" ... where `to` already uses the name."""
    rows = c.execute(text(f'SELECT id, name FROM app.{table} WHERE owner_id = :u ORDER BY created_at'),
                     {'u': uid}).all()
    if not rows:
        return 0
    taken = list(c.execute(text(f'SELECT name FROM app.{table} WHERE owner_id = :t'), {'t': to}).scalars().all())
    for r in rows:
        name = unique_name(r.name, taken) if unique else r.name
        taken.append(name)
        c.execute(text(f'UPDATE app.{table} SET owner_id = :t, name = :n, updated_at = now() WHERE id = :id'),
                  {'t': to, 'n': name, 'id': r.id})
    return len(rows)


def delete(uid, transfer_to=None, conn=None):
    """Delete a user. transfer_to: another user who becomes the owner of their sessions (and of the connections,
    models and LLM providers those sessions may use); without it their sessions are deleted.
    Returns {transferred, deleted} (session counts)."""
    from ..data import sessions
    out = {'transferred': 0, 'deleted': 0}
    with _conn(conn) as c:
        cur = c.execute(text('SELECT * FROM app.users WHERE id = :id FOR UPDATE'), {'id': uid}).first()
        if cur is None:
            raise KeyError(f'no user {uid!r}')
        if cur.is_admin and cur.is_active:
            _guard_last_admin(c, uid)
        owned = c.execute(text('SELECT id, kind, name FROM app.sessions WHERE owner_id = :u ORDER BY created_at'),
                          {'u': uid}).all()
        if transfer_to:
            if transfer_to == uid:
                raise ValueError('pick another user to receive the sessions')
            to = c.execute(text('SELECT id FROM app.users WHERE id = :id'), {'id': transfer_to}).first()
            if to is None:
                raise ValueError('the user to transfer the sessions to does not exist')
            # connections first: the moved viz sessions point at them (FK RESTRICT on the old owner's rows)
            _move_owned(c, 'connections', uid, transfer_to, True)
            _move_owned(c, 'models', uid, transfer_to, True)
            _move_owned(c, 'llm_providers', uid, transfer_to, False)
            for s in owned:
                name = sessions.free_name(transfer_to, s.kind, s.name, c)
                sessions.update(s.id, name=name, owner_id=transfer_to, conn=c)
            out['transferred'] = len(owned)
        else:
            for s in owned:
                sessions.delete(s.id, conn=c)
            out['deleted'] = len(owned)
        try:
            with c.begin_nested():
                c.execute(text('DELETE FROM app.users WHERE id = :id'), {'id': uid})
        except IntegrityError as e:     # e.g. someone else's session still uses one of their connections
            raise ValueError('this user still owns something other sessions depend on; transfer it instead') from e
    tokens.evict_user(uid)
    return out
