"""
Saved ◆ chats (/api/llm/sessions*, llm/sessions.py): the History panel of both assistants. Owner only: another
user's chat, like a deleted one, answers 404. Every route needs sign-in and the tier permission llm.use.

    GET    /api/llm/sessions?role=&scope=&bucket_id=&dataset_id=&q=&cursor=&limit=
    POST   /api/llm/sessions                 {role, scope?, title?}
    GET    /api/llm/sessions/{id}            the chat + its messages (and traces); opening it counts as opened
    PATCH  /api/llm/sessions/{id}            {title?, pinned?}
    DELETE /api/llm/sessions/{id}
    POST   /api/llm/sessions/delete          {ids} | {role, scope?, include_pinned?}
    POST   /api/llm/sessions/restore         {ids}   (Undo)
"""

import json

from fastapi import APIRouter, Depends, HTTPException

from ...auth import deps, tiers
from ...auth import context as auth_context
from ...llm import sessions


def _user():
    u = auth_context.user()
    tiers.check(u, 'llm.use')
    return u


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except sessions.SessionError as e:
        raise HTTPException(e.status, str(e))


def _scope_arg(scope, bucket_id, dataset_id):
    """?scope= is a JSON object ({"bucket_id": "..."}) or key:value; ?bucket_id= / ?dataset_id= work too."""
    out = {}
    if scope:
        try:
            v = json.loads(scope)
        except ValueError:
            k, _, val = scope.partition(':')
            v = {k: val} if val else {}
        if not isinstance(v, dict):
            raise HTTPException(400, 'scope must be a JSON object')
        out.update(v)
    if bucket_id:
        out['bucket_id'] = bucket_id
    if dataset_id:
        out['dataset_id'] = dataset_id
    return out


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.current_user)])

    @r.get('/api/llm/sessions')
    def list_sessions(role: str, scope: str = '', bucket_id: str = '', dataset_id: str = '', q: str = '',
                      cursor: str = '', limit: int = 50):
        return _call(sessions.list_, _user(), role, _scope_arg(scope, bucket_id, dataset_id), q, cursor or None,
                     limit)

    @r.post('/api/llm/sessions')
    def create_session(body: dict):
        return _call(sessions.create, _user(), body.get('role'), body.get('scope') or {}, body.get('title') or '')

    @r.post('/api/llm/sessions/delete')
    def delete_sessions(body: dict):
        u = _user()
        if body.get('ids') is not None:
            return _call(sessions.delete, u, ids=body.get('ids'))
        return _call(sessions.delete, u, role=body.get('role'), scope=body.get('scope') or {},
                     include_pinned=bool(body.get('include_pinned')))

    @r.post('/api/llm/sessions/restore')
    def restore_sessions(body: dict):
        return _call(sessions.restore, _user(), body.get('ids'))

    @r.get('/api/llm/sessions/{sid}')
    def get_session(sid: str):
        return _call(sessions.get, _user(), sid)

    @r.patch('/api/llm/sessions/{sid}')
    def patch_session(sid: str, body: dict):
        return {'session': _call(sessions.update, _user(), sid, title=body.get('title'), pinned=body.get('pinned'))}

    @r.delete('/api/llm/sessions/{sid}')
    def delete_session(sid: str):
        return _call(sessions.delete_one, _user(), sid)

    return r
