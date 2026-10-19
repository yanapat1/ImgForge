"""
Sharing of one session (either kind): visibility (private | global) and the members with their roles. Only the
owner or an admin may see or change it: a labeler gets 403, anyone without access 404 (auth/access.py).
"""

from fastapi import APIRouter, Body, Depends, Request
from starlette.exceptions import HTTPException

from ...auth import access, audit
from ...auth.deps import current_user
from ...data import sessions
from .auth import client_ip


def _shape(user, sid):
    h = sessions.header(user, sid)
    if h is None:
        raise HTTPException(404, f'no session {sid!r}')
    return {'visibility': h['visibility'], 'owner': h['owner'], 'members': h['members'], 'my_role': h['my_role']}


def router(cfg, limiter):
    r = APIRouter()

    @r.get('/api/sessions/{sid}/sharing')
    def get_sharing(sid: str, user=Depends(current_user)):
        access.check(user, sid, 'owner')
        return _shape(user, sid)

    @r.put('/api/sessions/{sid}/sharing')
    def put_sharing(sid: str, request: Request, body: dict = Body(...), user=Depends(current_user)):
        access.check(user, sid, 'owner')
        members = body.get('members', [])
        if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
            raise HTTPException(400, 'members must be a list of {user_id, role}')
        try:
            sessions.set_sharing(sid, body.get('visibility'), members, user['id'])
        except KeyError as e:
            raise HTTPException(404, f'no session {sid!r}') from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        out = _shape(user, sid)
        audit.log('sharing', target=sid, user_id=user['id'], ip=client_ip(request),
                  meta={'visibility': out['visibility'],
                        'members': [{'id': m['id'], 'role': m['role']} for m in out['members']]})
        return out

    return r
