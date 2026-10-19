"""
The outermost ASGI layer: who is asking (cookie `dv_session` -> context.USER), CSRF checks, the sign-in wall and
two security headers on every response.

    CSRF        a non-GET/HEAD/OPTIONS request to /api or /s/<id>/api must carry X-Requested-With: dataviz
                (another site cannot add it without a CORS preflight, and no CORS is enabled), and an Origin
                header, when present, must name this host
    sign-in     everything but PUBLIC needs a session: /api -> 401 JSON, a page -> 302 to login?next=...
                with a RELATIVE Location (the app often sits behind a path-prefix proxy: JupyterHub, VS Code
                port forwarding), so an absolute "/login" would leave the prefix
    headers     X-Content-Type-Options: nosniff, Referrer-Policy: same-origin

install_auth(app, cfg) adds the middleware and the auth / me / users / admin / sharing routers.
"""

import json
import re
from http.cookies import SimpleCookie
from urllib.parse import quote, urlsplit

from anyio import to_thread
from starlette.datastructures import Headers, MutableHeaders

from . import context, tokens

PUBLIC = frozenset({'/login', '/api/auth/config', '/api/auth/login', '/api/auth/signup', '/plotly.min.js',
                    '/favicon.ico'})
PUBLIC_PREFIXES = ('/static/',)
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
CSRF_HEADER = ('x-requested-with', 'dataviz')
_S_API = re.compile(r'^/s/[^/]+/api(?:/|$)')
SECURITY_HEADERS = (('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'same-origin'))


def is_api(path):
    return path == '/api' or path.startswith('/api/') or bool(_S_API.match(path))


def is_public(path):
    return path in PUBLIC or path.startswith(PUBLIC_PREFIXES)


def login_location(path, query=''):
    """login?next=<path>, relative to `path` so a path prefix in front of the app survives."""
    target = path + ('?' + query if query else '')
    return '../' * max(path.count('/') - 1, 0) + 'login?next=' + quote(target, safe='')


def cookie_token(headers):
    raw = headers.get('cookie')
    if not raw:
        return None
    try:
        c = SimpleCookie()
        c.load(raw)
    except Exception:            # noqa: BLE001 - a malformed Cookie header is just "no session"
        return None
    m = c.get(tokens.COOKIE)
    return m.value if m else None


def is_https(scope, headers):
    proto = (headers.get('x-forwarded-proto') or '').split(',')[0].strip().lower()
    return scope.get('scheme') in ('https', 'wss') or proto == 'https'


def cookie_secure(cfg, scope, headers):
    mode = str(((cfg or {}).get('auth') or {}).get('cookie_secure', 'auto')).strip().lower()
    if mode in ('true', '1', 'yes', 'on'):
        return True
    if mode == 'auto':
        return is_https(scope, headers)
    return False


def origin_ok(headers):
    origin = headers.get('origin')
    if origin is None:
        return True
    try:
        host = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    if not host:                                     # "null" (sandboxed iframe, file://): never ours
        return False
    allowed = {(headers.get('host') or '').lower()}
    fwd = (headers.get('x-forwarded-host') or '').split(',')[0].strip().lower()
    if fwd:
        allowed.add(fwd)
    return host in allowed


async def _json(send, status, detail, extra=()):
    body = json.dumps({'detail': detail}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()),
                            (b'cache-control', b'no-store'), *extra]})
    await send({'type': 'http.response.body', 'body': body})


class AuthMiddleware:
    def __init__(self, app, cfg=None):
        self.app = app
        self.cfg = cfg or {}

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path = scope.get('path', '')

        async def send_secured(msg):
            if msg['type'] == 'http.response.start':
                h = MutableHeaders(scope=msg)
                for k, v in SECURITY_HEADERS:
                    if k not in h:
                        h.append(k, v)
            await send(msg)

        method = scope.get('method', 'GET').upper()
        if scope['type'] == 'http' and is_api(path) and method not in SAFE_METHODS:
            if headers.get(CSRF_HEADER[0], '').strip().lower() != CSRF_HEADER[1]:
                return await _json(send_secured, 403, 'missing X-Requested-With header')
            if not origin_ok(headers):
                return await _json(send_secured, 403, 'cross-site request refused')

        token = cookie_token(headers)
        user = None
        if token:
            user = tokens.cached(token)
            if user is None:
                user = await to_thread.run_sync(tokens.resolve, token)
        scope.setdefault('state', {})['user'] = user

        if user is None and not is_public(path):
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 4401})
            if is_api(path) or method not in ('GET', 'HEAD'):
                return await _json(send_secured, 401, 'sign in first')
            q = scope.get('query_string', b'').decode('latin-1')
            loc = login_location(path, q).encode('latin-1')
            await send_secured({'type': 'http.response.start', 'status': 302,
                                'headers': [(b'location', loc), (b'content-length', b'0'),
                                            (b'cache-control', b'no-store')]})
            return await send_secured({'type': 'http.response.body', 'body': b''})

        reset = context.USER.set(user)
        try:
            await self.app(scope, receive, send_secured if scope['type'] == 'http' else send)
        finally:
            context.USER.reset(reset)


def install_auth(app, cfg):
    """Add the sign-in middleware and the account routers to `app` (server/app.py create_app calls this)."""
    from ..server.routes import admin, auth, me, sharing, users as users_routes
    from . import settings
    from .ratelimit import RateLimiter
    tokens.configure(cfg)
    tokens.clear_cache()
    settings.clear_cache()
    limiter = RateLimiter(rate=20, per=60, burst=20)
    app.state.login_limiter = limiter
    for module in (auth, me, users_routes, admin, sharing):
        app.include_router(module.router(cfg, limiter))
    app.add_middleware(AuthMiddleware, cfg=cfg)
    return app
