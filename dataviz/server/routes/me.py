"""
The signed-in user's own account: profile (display name, username, email), password, avatar, and the
per-user preferences blob (app.user_prefs: theme, home filter ... whatever the page keeps; merged by top-level key).
"""

import json

from fastapi import APIRouter, Body, Depends, Request, UploadFile
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from ... import db
from ...auth import audit, avatars, passwords, tokens, users
from ...auth.deps import current_user
from ...auth.middleware import cookie_token
from .auth import client_ip

PREFS_MAX = 64 * 1024
CHUNK = 64 * 1024


def get_prefs(uid):
    with db.connect() as c:
        v = c.execute(text('SELECT data FROM app.user_prefs WHERE user_id = :u'), {'u': uid}).scalar()
    return v or {}


def _put_prefs(uid, patch):
    """Shallow merge `patch` into the stored prefs (a None value removes the key); 413 past PREFS_MAX."""
    with db.begin() as c:
        cur = c.execute(text('SELECT data FROM app.user_prefs WHERE user_id = :u FOR UPDATE'), {'u': uid}).scalar()
        data = dict(cur or {})
        for k, v in patch.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        raw = json.dumps(data)
        if len(raw.encode()) > PREFS_MAX:
            raise HTTPException(413, 'preferences are limited to 64 KB')
        c.execute(text("""INSERT INTO app.user_prefs(user_id, data) VALUES (:u, CAST(:d AS jsonb))
                          ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()"""),
                  {'u': uid, 'd': raw})
    return data


def router(cfg, limiter):
    r = APIRouter()

    @r.patch('/api/me')
    def update_me(request: Request, body: dict = Body(...), user=Depends(current_user)):
        fields = {k: body.get(k) for k in ('display_name', 'username', 'email') if body.get(k) is not None}
        try:
            u = users.update(user['id'], **fields)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if fields:
            audit.log('profile', target=user['id'], meta={'fields': sorted(fields)}, user_id=user['id'],
                      ip=client_ip(request))
        return {'user': u}

    @r.put('/api/me/password')
    def change_password(request: Request, body: dict = Body(...), user=Depends(current_user)):
        current, new = body.get('current'), body.get('password')
        row = users.row(user['id'])
        if not isinstance(current, str) or not passwords.verify(row['password_hash'], current):
            raise HTTPException(400, 'the current password is not right')
        try:
            users.set_password(user['id'], new)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        token = cookie_token(request.headers)
        n = tokens.revoke_user(user['id'], except_digest=tokens.digest(token) if token else None)
        audit.log('password_change', target=user['id'], meta={'revoked': n}, user_id=user['id'],
                  ip=client_ip(request))
        return {'ok': True}

    @r.put('/api/me/avatar')
    async def put_avatar(request: Request, file: UploadFile, user=Depends(current_user)):
        try:
            declared = int(request.headers.get('content-length') or 0)
        except ValueError:
            declared = 0
        if declared > avatars.MAX_BYTES + 64 * 1024:              # multipart overhead allowance
            raise HTTPException(413, 'the image is larger than 5 MB')
        buf = bytearray()
        while True:
            chunk = await file.read(CHUNK)
            if not chunk:
                break
            buf += chunk
            if len(buf) > avatars.MAX_BYTES:
                raise HTTPException(413, 'the image is larger than 5 MB')
        try:
            webp = await run_in_threadpool(avatars.process, bytes(buf))
        except avatars.TooLarge as e:
            raise HTTPException(413, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        u = await run_in_threadpool(users.set_avatar, user['id'], webp, avatars.MIME)
        return {'user': u}

    @r.delete('/api/me/avatar')
    def delete_avatar(user=Depends(current_user)):
        return {'user': users.set_avatar(user['id'], None)}

    @r.get('/api/me/prefs')
    def read_prefs(user=Depends(current_user)):
        return {'prefs': get_prefs(user['id'])}

    @r.put('/api/me/prefs')
    async def write_prefs(request: Request, user=Depends(current_user)):
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > PREFS_MAX + 1024:
                raise HTTPException(413, 'preferences are limited to 64 KB')
        try:
            body = json.loads(raw or b'{}')
        except ValueError as e:
            raise HTTPException(400, 'send JSON: {"prefs": {...}}') from e
        patch = body.get('prefs') if isinstance(body, dict) else None
        if not isinstance(patch, dict):
            raise HTTPException(400, 'send JSON: {"prefs": {...}}')
        return {'prefs': await run_in_threadpool(_put_prefs, user['id'], patch)}

    return r
