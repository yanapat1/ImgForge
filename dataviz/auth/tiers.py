"""
Tiers (app.tiers): named permission sets admins define on the Users & admin page. Every account is in one tier
(users.tier_id; NULL = the default tier). A tier decides what a user may ADD to the server; what they may do on
someone else's session still comes from sharing (auth/access.py). Admins have every permission.

    dataset.create   add visualize datasets (local / S3 / combined / embeddings), browse the server's folders
    label.create     create / duplicate labeling projects
    connection.add   add S3 / FTP / database / model-server / LLM connections (and the server-wide
                     who_can_add_connections = admins still keeps them to admins)
    model.add        register Hugging Face / API / serving models
    model.upload     upload or register model FILES and folders: loading them runs code on the server
    llm.use          the LLM features (chat, assist, pre-label)

Reads are cached for a few seconds in-process like auth/settings.py; every write clears the cache.
"""

import json
import threading
import time
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from .. import db

PERMS = {
    'dataset.create': 'Add datasets to visualize',
    'label.create': 'Create labeling projects',
    'connection.add': 'Add connections (S3, FTP, databases, model servers, LLM providers)',
    'model.add': 'Add Hugging Face / API models',
    'model.upload': 'Upload model files (runs their code on the server)',
    'llm.use': 'Use the LLM assistant',
}
DENIED = {
    'dataset.create': 'add datasets', 'label.create': 'create labeling projects', 'connection.add': 'add connections',
    'model.add': 'add models', 'model.upload': 'upload model files', 'llm.use': 'use the LLM assistant',
}
CACHE_TTL = 5.0

_lock = threading.Lock()
_cache = {'at': 0.0, 'data': None}


def _shape(r):
    return {'id': r.id, 'name': r.name, 'description': r.description or '',
            'permissions': [p for p in (r.permissions or []) if p in PERMS], 'is_default': bool(r.is_default)}


def _all():
    """{id: tier} (cached)."""
    with _lock:
        if _cache['data'] is not None and time.monotonic() - _cache['at'] < CACHE_TTL:
            return _cache['data']
    with db.connect() as c:
        rows = c.execute(text('SELECT * FROM app.tiers ORDER BY created_at, lower(name)')).all()
    data = {r.id: _shape(r) for r in rows}
    with _lock:
        _cache.update(at=time.monotonic(), data=data)
    return data


def clear_cache():
    with _lock:
        _cache.update(at=0.0, data=None)


def list_all():
    """Every tier with how many users are in it (the default tier counts the users without one)."""
    with db.connect() as c:
        n = dict(c.execute(text('SELECT tier_id, count(*) FROM app.users GROUP BY tier_id')).all())
    out = []
    for t in _all().values():
        out.append({**t, 'n_users': int(n.get(t['id'], 0)) + (int(n.get(None, 0)) if t['is_default'] else 0)})
    return out


def default_tier():
    return next((t for t in _all().values() if t['is_default']), None)


def tier_of(user):
    """The tier dict of `user` (their own, else the default), None when there are no tiers at all."""
    if user is None:
        return None
    t = _all().get(user.get('tier_id')) if user.get('tier_id') else None
    return t or default_tier()


def perms_for(user):
    """The permission keys `user` has: every one for an admin (and for internal callers without a user)."""
    if user is None or user.get('is_admin'):
        return list(PERMS)
    t = tier_of(user)
    return list(t['permissions']) if t else []


def has(user, perm):
    return perm in perms_for(user)


def check(user, perm):
    """403 with the tier's name when `user` lacks `perm`."""
    if not has(user, perm):
        t = tier_of(user)
        raise HTTPException(403, f'your tier ({t["name"] if t else "none"}) cannot {DENIED.get(perm, perm)}: '
                                 'ask an administrator')


def describe(user):
    """{tier: {id, name}, perms: [...]} for /api/auth/me and the status line."""
    t = tier_of(user)
    return {'tier': {'id': t['id'], 'name': t['name']} if t else None, 'perms': perms_for(user)}


def owner_has(uid, perm, conn=None):
    """Whether the ACTIVE account `uid` has `perm` now (admin, or their tier): the trust check of a stored model,
    evaluated when it loads so a demoted owner's model stops loading."""
    if conn is None:
        with db.connect() as c:
            return owner_has(uid, perm, c)
    r = conn.execute(text('SELECT id, is_admin, is_active, tier_id FROM app.users WHERE id = :u'), {'u': uid}).first()
    if r is None or not r.is_active:
        return False
    return has({'id': r.id, 'is_admin': bool(r.is_admin), 'tier_id': r.tier_id}, perm)


# ------------------------------------------------------------------------------------------ writes (admins)
def _clean(body, partial=False):
    out = {}
    if 'name' in body or not partial:
        name = ' '.join(str(body.get('name') or '').split())
        if not 1 <= len(name) <= 40:
            raise ValueError('a tier name needs 1 to 40 characters')
        out['name'] = name
    if 'description' in body:
        out['description'] = str(body.get('description') or '').strip()[:300]
    if 'permissions' in body or not partial:
        perms = body.get('permissions') or []
        if not isinstance(perms, list) or any(p not in PERMS for p in perms):
            raise ValueError(f'permissions must be a list of: {", ".join(PERMS)}')
        out['permissions'] = [p for p in PERMS if p in perms]          # catalog order, no duplicates
    if body.get('is_default') is not None:
        if not isinstance(body['is_default'], bool):
            raise ValueError('is_default must be true or false')
        out['is_default'] = body['is_default']
    return out


def _write(fn):
    try:
        with db.begin() as c:
            return fn(c)
    except IntegrityError as e:
        raise ValueError('a tier with that name already exists') from e
    finally:
        clear_cache()


def _make_default(c, tid):
    c.execute(text('UPDATE app.tiers SET is_default = false WHERE is_default AND id <> :id'), {'id': tid})


def create(body):
    f = _clean(body)
    tid = uuid.uuid4().hex

    def go(c):
        if f.get('is_default'):
            _make_default(c, tid)
        c.execute(text("""INSERT INTO app.tiers(id, name, description, permissions, is_default)
                          VALUES (:id, :n, :d, CAST(:p AS jsonb), :dflt)"""),
                  {'id': tid, 'n': f['name'], 'd': f.get('description', ''), 'p': json.dumps(f['permissions']),
                   'dflt': bool(f.get('is_default'))})
    _write(go)
    return _all()[tid]


def update(tid, body):
    """Change name / description / permissions / is_default (only true: another tier becomes the default by
    being set, never by unsetting this one). KeyError for an unknown tier."""
    f = _clean(body, partial=True)
    if f.get('is_default') is False:
        raise ValueError('make another tier the default instead')

    def go(c):
        if c.execute(text('SELECT 1 FROM app.tiers WHERE id = :id FOR UPDATE'), {'id': tid}).first() is None:
            raise KeyError(tid)
        if f.get('is_default'):
            _make_default(c, tid)
        sets = dict(f)
        if 'permissions' in sets:
            sets['permissions'] = json.dumps(sets['permissions'])
        if sets:
            assign = ', '.join(f'{k} = CAST(:{k} AS jsonb)' if k == 'permissions' else f'{k} = :{k}' for k in sets)
            c.execute(text(f'UPDATE app.tiers SET {assign} WHERE id = :id'), {**sets, 'id': tid})
    _write(go)
    return _all()[tid]


def delete(tid):
    """Remove a tier; its users fall back to the default tier. The default tier itself cannot go.
    -> how many users moved."""
    def go(c):
        r = c.execute(text('SELECT is_default FROM app.tiers WHERE id = :id FOR UPDATE'), {'id': tid}).first()
        if r is None:
            raise KeyError(tid)
        if r.is_default:
            raise ValueError('the default tier cannot be deleted: make another tier the default first')
        n = c.execute(text('UPDATE app.users SET tier_id = NULL WHERE tier_id = :id'), {'id': tid}).rowcount
        c.execute(text('DELETE FROM app.tiers WHERE id = :id'), {'id': tid})
        return n
    return _write(go)


def exists(tid, conn=None):
    if conn is None:
        with db.connect() as c:
            return exists(tid, c)
    return conn.execute(text('SELECT 1 FROM app.tiers WHERE id = :id'), {'id': tid}).first() is not None
