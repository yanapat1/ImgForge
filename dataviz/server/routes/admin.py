"""
The admin page's API (admins only): accounts (list, create, edit, reset password, deactivate, delete with or
without handing their sessions to someone), server settings, and the audit log. Every change is audited.
"""

from fastapi import APIRouter, Body, Depends, Request
from starlette.exceptions import HTTPException

from ... import db
from ...auth import audit, settings, tokens, users
from ...auth.deps import admin_user
from .auth import client_ip

EDITABLE = ('is_admin', 'is_active', 'display_name', 'email', 'username')


def router(cfg, limiter):
    r = APIRouter(dependencies=[Depends(admin_user)])

    def note(request, admin, action, target=None, meta=None):
        audit.log(action, target=target, meta=meta, user_id=admin['id'], ip=client_ip(request))

    @r.get('/api/admin/users')
    def list_users(admin=Depends(admin_user)):
        return {'users': users.list_all()}

    @r.post('/api/admin/users')
    def create_user(request: Request, body: dict = Body(...), admin=Depends(admin_user)):
        try:
            u = users.create(body.get('username'), body.get('email'), body.get('display_name'),
                             body.get('password'), is_admin=bool(body.get('is_admin')))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        note(request, admin, 'user_create', u['id'], {'username': u['username'], 'is_admin': u['is_admin']})
        return {'user': u}

    @r.patch('/api/admin/users/{uid}')
    def update_user(uid: str, request: Request, body: dict = Body(...), admin=Depends(admin_user)):
        fields = {k: body[k] for k in EDITABLE if body.get(k) is not None}
        for k in ('is_admin', 'is_active'):
            if k in fields and not isinstance(fields[k], bool):
                raise HTTPException(400, f'{k} must be true or false')
        password = body.get('password')
        try:
            with db.begin() as c:                   # all or nothing: a bad password keeps the other fields too
                u = users.update(uid, conn=c, **fields)
                if password:
                    users.set_password(uid, password, conn=c)
            if password:
                tokens.revoke_user(uid)
        except KeyError as e:
            raise HTTPException(404, 'no such user') from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        meta = dict(fields)
        if password:
            meta['password'] = 'reset'
        note(request, admin, 'user_update', uid, meta)
        return {'user': u}

    @r.delete('/api/admin/users/{uid}')
    def delete_user(uid: str, request: Request, transfer_to: str = '', admin=Depends(admin_user)):
        try:
            out = users.delete(uid, transfer_to or None)
        except KeyError as e:
            raise HTTPException(404, 'no such user') from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        note(request, admin, 'user_delete', uid, {'transfer_to': transfer_to or None, **out})
        return {'ok': True, **out}

    @r.get('/api/admin/settings')
    def get_settings(admin=Depends(admin_user)):
        return settings.all_settings()

    @r.put('/api/admin/settings')
    def put_settings(request: Request, body: dict = Body(...), admin=Depends(admin_user)):
        try:
            out = settings.update(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        note(request, admin, 'settings', None, body)
        return out

    @r.get('/api/admin/audit')
    def get_audit(limit: int = 200, admin=Depends(admin_user)):
        return {'events': audit.list_events(limit)}

    return r
