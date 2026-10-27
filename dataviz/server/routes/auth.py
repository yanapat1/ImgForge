"""
Signing in and out: the login page, sign-up, login, logout, "sign out other devices", and who am I.

Login answers the same 401 'Invalid username or password' for an unknown login, a wrong password, a locked
account and a deactivated one, and an unknown login still costs one argon2 verification, so neither the text
nor the timing tells which accounts exist. Attempts are limited per IP (token bucket) and per account
(lockout after 5 failures, auth/users.py).
"""

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException

from ...auth import audit, passwords, settings, tiers, tokens, users
from ...auth.deps import current_user
from ...auth.middleware import cookie_secure, cookie_token
from ...auth.ratelimit import FailureLock
from ...paths import STATIC_DIR
from ..static import NO_CACHE, render_index

BAD_LOGIN = 'Invalid username or password'
TOO_MANY = 'Too many attempts: wait a minute and try again'


def client_ip(request):
    return request.client.host if request.client else None


def set_session_cookie(response, request, cfg, token):
    response.set_cookie(tokens.COOKIE, token, max_age=tokens.max_age_seconds(), path='/', httponly=True,
                        samesite='lax', secure=cookie_secure(cfg, request.scope, request.headers))


def clear_session_cookie(response, request, cfg):
    response.delete_cookie(tokens.COOKIE, path='/', httponly=True, samesite='lax',
                           secure=cookie_secure(cfg, request.scope, request.headers))


def _str(body, key, limit=1024):
    v = body.get(key) if isinstance(body, dict) else None
    return v[:limit] if isinstance(v, str) else ''


def router(cfg, limiter):
    r = APIRouter()
    lockout = getattr(limiter, 'lockout', None) or FailureLock()
    limiter.lockout = lockout                    # one per app: tests reset it with the limiter

    def limited(request):
        if not limiter.allow(client_ip(request) or '?'):
            raise HTTPException(429, TOO_MANY)

    def signed_in(request, user, action, status=200):
        token = tokens.create(user['id'], client_ip(request), request.headers.get('user-agent'))
        resp = JSONResponse({'user': user}, status_code=status)
        set_session_cookie(resp, request, cfg, token)
        audit.log(action, target=user['id'], user_id=user['id'], ip=client_ip(request))
        return resp

    @r.get('/login')
    def login_page():
        return HTMLResponse(render_index(STATIC_DIR), headers=NO_CACHE)

    @r.get('/api/auth/config')
    def auth_config():
        return {'signup_enabled': bool(settings.get('signup_enabled')), 'password_min': passwords.MIN_LEN}

    @r.post('/api/auth/login')
    def login(request: Request, body: dict = Body(...)):
        limited(request)
        login_, password = _str(body, 'login', 300), _str(body, 'password', passwords.MAX_LEN * 4)
        ip = client_ip(request)

        def fail(reason, uid=None):
            audit.log('login_failed', target=login_[:100], meta={'reason': reason}, user_id=uid, ip=ip)
            raise HTTPException(401, BAD_LOGIN)

        row = users.by_login(login_) if login_ and password else None
        if row is None:
            passwords.dummy_verify(password)
            fail('unknown')
        key = (row['id'], ip or '?')
        if lockout.locked(key):
            passwords.dummy_verify(password)
            fail('locked', row['id'])
        if not passwords.verify(row['password_hash'], password):
            lockout.fail(key)
            users.record_failure(row['id'])
            fail('password', row['id'])
        if not row['is_active']:
            fail('inactive', row['id'])
        lockout.clear(key)
        users.record_success(row['id'], password, row['password_hash'])
        return signed_in(request, users.public(row), 'login')

    @r.post('/api/auth/signup')
    def signup(request: Request, body: dict = Body(...)):
        if not settings.get('signup_enabled'):
            raise HTTPException(403, 'sign-up is turned off')
        limited(request)
        try:
            user = users.create(_str(body, 'username', 100), _str(body, 'email', 300),
                                _str(body, 'display_name', 200), _str(body, 'password', passwords.MAX_LEN * 4))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return signed_in(request, user, 'signup')

    @r.post('/api/auth/logout')
    def logout(request: Request, user=Depends(current_user)):
        tokens.revoke(cookie_token(request.headers))
        audit.log('logout', target=user['id'], user_id=user['id'], ip=client_ip(request))
        resp = JSONResponse({'ok': True})
        clear_session_cookie(resp, request, cfg)
        return resp

    @r.post('/api/auth/logout-others')
    def logout_others(request: Request, user=Depends(current_user)):
        token = cookie_token(request.headers)
        n = tokens.revoke_user(user['id'], except_digest=tokens.digest(token) if token else None)
        audit.log('logout_others', target=user['id'], meta={'n': n}, user_id=user['id'], ip=client_ip(request))
        return {'ok': True, 'n': n}

    @r.get('/api/auth/me')
    def me(user=Depends(current_user)):
        from .me import get_prefs
        return {'user': {**user, **tiers.describe(user)}, 'prefs': get_prefs(user['id'])}

    return r
