"""
Login sessions (app.auth_sessions): the random token in the `dv_session` cookie -> the signed-in user.

Only sha256(token) is stored, so a database leak does not hand out live sessions. A session dies after
`auth.session_idle_days` without a request (sliding; last_seen_at is written at most once a minute) or
`auth.session_max_days` after sign-in, whichever comes first.

resolve() answers from an in-process cache for 30 s so a page asking for 60 thumbnails does not cost 60
queries; anything that must take effect at once (logout, password change, deactivation, admin rights,
profile edits) evicts the user's entries explicitly.
"""

import hashlib
import secrets
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .. import db

COOKIE = 'dv_session'
CACHE_TTL = 30.0
CACHE_MAX = 10000
TOUCH_EVERY = 60          # seconds between last_seen_at writes

_cfg = {'idle_days': 7, 'max_days': 30}
_cache = {}               # digest -> (monotonic expiry, user dict)
_lock = threading.Lock()


def configure(cfg):
    a = (cfg or {}).get('auth', {}) or {}
    _cfg['idle_days'] = float(a.get('session_idle_days', 7))
    _cfg['max_days'] = float(a.get('session_max_days', 30))


def max_age_seconds():
    return int(_cfg['max_days'] * 86400)


def digest(token):
    return hashlib.sha256(token.encode('utf-8', 'replace')).digest()


def _user(r):
    return {'id': r.id, 'username': r.username, 'email': r.email, 'display_name': r.display_name,
            'is_admin': bool(r.is_admin), 'avatar_ver': int(r.avatar_ver or 0)}


# ------------------------------------------------------------------------------------------ cache
def _cache_put(d, user):
    with _lock:
        if len(_cache) >= CACHE_MAX:
            now = time.monotonic()
            for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                del _cache[k]
            while len(_cache) >= CACHE_MAX:              # still full: drop the oldest inserted
                del _cache[next(iter(_cache))]
        _cache[d] = (time.monotonic() + CACHE_TTL, user)


def cached(token):
    """The cached user for `token` (no database access), or None when not cached."""
    if not token:
        return None
    d = digest(token)
    with _lock:
        hit = _cache.get(d)
        if hit is None:
            return None
        if hit[0] <= time.monotonic():
            del _cache[d]
            return None
        return dict(hit[1])


def evict(d):
    with _lock:
        _cache.pop(d, None)


def evict_user(uid):
    with _lock:
        for k in [k for k, (_, u) in _cache.items() if u['id'] == uid]:
            del _cache[k]


def clear_cache():
    with _lock:
        _cache.clear()


# ------------------------------------------------------------------------------------------ sessions
def create(uid, ip=None, user_agent=None, conn=None):
    """A new login session for `uid`; returns the token (the cookie value). Expired rows of the user go."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    ctx = db.begin() if conn is None else nullcontext(conn)
    with ctx as c:
        c.execute(text("""DELETE FROM app.auth_sessions WHERE user_id = :u
                          AND (expires_at <= now() OR last_seen_at < now() - make_interval(secs => :idle))"""),
                  {'u': uid, 'idle': _cfg['idle_days'] * 86400})
        c.execute(text("""INSERT INTO app.auth_sessions(token_hash, user_id, expires_at, ip, user_agent)
                          VALUES (:h, :u, :exp, :ip, :ua)"""),
                  {'h': digest(token), 'u': uid, 'exp': now + timedelta(days=_cfg['max_days']),
                   'ip': (ip or '')[:64] or None, 'ua': (user_agent or '')[:300] or None})
    return token


def resolve(token):
    """The user dict behind `token`, or None (unknown, expired, idle too long, or the account is deactivated)."""
    if not token or len(token) > 200:
        return None
    hit = cached(token)
    if hit is not None:
        return hit
    d = digest(token)
    with db.begin() as c:
        r = c.execute(text("""
            SELECT u.id, u.username, u.email, u.display_name, u.is_admin, u.avatar_ver, u.is_active,
                   s.last_seen_at, s.expires_at, now() AS now
            FROM app.auth_sessions s JOIN app.users u ON u.id = s.user_id
            WHERE s.token_hash = :h"""), {'h': d}).first()
        if r is None:
            return None
        idle = timedelta(days=_cfg['idle_days'])
        if not r.is_active or r.expires_at <= r.now or r.last_seen_at + idle <= r.now:
            c.execute(text('DELETE FROM app.auth_sessions WHERE token_hash = :h'), {'h': d})
            return None
        if (r.now - r.last_seen_at).total_seconds() >= TOUCH_EVERY:
            c.execute(text('UPDATE app.auth_sessions SET last_seen_at = now() WHERE token_hash = :h'), {'h': d})
    user = _user(r)
    _cache_put(d, user)
    return dict(user)


def revoke(token):
    """Delete the session of `token` (logout)."""
    if not token:
        return 0
    d = digest(token)
    evict(d)
    with db.begin() as c:
        n = c.execute(text('DELETE FROM app.auth_sessions WHERE token_hash = :h'), {'h': d}).rowcount
    evict(d)
    return n


def revoke_user(uid, except_digest=None, conn=None):
    """Delete every session of `uid` but the one with `except_digest`; returns how many went."""
    evict_user(uid)
    ctx = db.begin() if conn is None else nullcontext(conn)
    with ctx as c:
        n = c.execute(text('DELETE FROM app.auth_sessions WHERE user_id = :u AND token_hash <> :keep'),
                      {'u': uid, 'keep': except_digest or b''}).rowcount
    evict_user(uid)
    return n
