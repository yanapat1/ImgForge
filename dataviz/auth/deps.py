"""
FastAPI dependencies that guard routes. Every one carries `__dataviz_auth__ = True`: the route-guard test
(tests/test_acl.py) walks each /api route's dependency tree and fails when none of them has it.

    current_user                   401 unless signed in; -> user dict
    admin_user                     403 'admins only'
    require_session(need, param)   access.check on the session id in path parameter `param` -> role
    require_bound(need)            the /s/<id>/ session this request is bound to (context.bound()) -> Bound
    can_add_connections            403 when admins locked outbound connections to themselves
"""

from fastapi import Depends, Request
from starlette.exceptions import HTTPException

from . import access, context, settings


def _guard(f):
    f.__dataviz_auth__ = True
    return f


@_guard
def current_user(request: Request):
    u = context.user()
    if u is None:                                    # the middleware also stores it: thread-pool safety net
        u = (request.scope.get('state') or {}).get('user')
    if u is None:
        raise HTTPException(401, 'sign in first')
    return u


@_guard
def admin_user(user=Depends(current_user)):
    if not user.get('is_admin'):
        raise HTTPException(403, 'admins only')
    return user


def require_session(need='viewer', param='sid', kind=None, what='session'):
    """Dependency: the caller's role on the session named by path parameter `param` (401 / 404 / 403)."""
    @_guard
    def dep(request: Request, user=Depends(current_user)):
        return access.check(user, request.path_params.get(param), need, kind=kind, what=what)
    dep.__name__ = f'require_session_{need}'
    return dep


_TOO_LOW = {'viewer': 'you can only view this session', 'labeler': 'only the owner of this session can do that'}


def require_bound(need='viewer'):
    """Dependency for the visualizer routes under /s/<id>/: the Bound session, its role high enough."""
    @_guard
    def dep(user=Depends(current_user)):
        b = context.bound()
        if b is None:
            raise HTTPException(404, 'open the session first')
        if not access.allows(b.role, need):
            raise HTTPException(403, _TOO_LOW.get(b.role, 'not allowed'))
        return b
    dep.__name__ = f'require_bound_{need}'
    return dep


@_guard
def can_add_connections(user=Depends(current_user)):
    if settings.get('who_can_add_connections') == 'admins' and not user.get('is_admin'):
        raise HTTPException(403, 'only admins can add connections on this server')
    return user
