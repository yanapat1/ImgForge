"""
The visualizer's URL space: /s/<session_id>/... is one loaded session. A pure ASGI middleware, just inside the
sign-in layer (auth/middleware.py runs first, so context.user() is known here):

    /s/<sid>          308 to <sid>/ (relative: a path prefix in front of the app survives)
    /s/<sid>/...      no access (or not a visualizer session) -> 404, never 403, so private ids do not leak;
                      else the session's State is resolved once (loaded on first use, off the event loop) and
                      context.BOUND = Bound(sid, role, state) for the whole request; the prefix is stripped
                      from scope['path'] / ['raw_path'], so every route below is written as /api/...
                      (root_path is left alone). The original path stays in scope['dataviz_path'] for the
                      access log.

Errors while opening: 404 unknown, 409 the dataset cannot be read (folder gone, labeling task deleted), 500
anything else; JSON for /api, the error page for a browser.
"""

import re

from anyio import to_thread
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from ..auth import access, context
from ..core import logging as vlog

_S = re.compile(r'^/s/([^/]+)(/.*)?$')
log = vlog.get_logger('sessions')


def strip_raw(raw):
    """b'/s/<sid>/rest' -> b'/rest' (the raw path is percent-encoded: cut at the third slash, not by the sid)."""
    if not raw:
        return raw
    parts = raw.split(b'/', 3)
    return b'/' + (parts[3] if len(parts) == 4 else b'')


class SessionRouting:
    def __init__(self, app, ctx, error_page=None):
        self.app = app
        self.ctx = ctx
        self.error_page = error_page        # (code, title, message) -> Response, for browsers

    async def _fail(self, scope, receive, send, code, detail, rest):
        if self.error_page is not None and not rest.startswith('/api/') and \
                'text/html' in dict(scope.get('headers') or []).get(b'accept', b'').decode('latin-1'):
            title = 'Page not found' if code == 404 else 'Something went wrong'
            resp = self.error_page(code, title, 'This address does not exist.' if code == 404 else detail)
        else:
            resp = JSONResponse({'detail': detail}, status_code=code, headers={'Cache-Control': 'no-store'})
        await resp(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        m = _S.match(path)
        if m is None:
            return await self.app(scope, receive, send)
        sid, rest = m.group(1), m.group(2)
        if rest is None:                            # /s/<sid> -> /s/<sid>/
            q = scope.get('query_string', b'')
            loc = sid.encode() + b'/' + (b'?' + q if q else b'')
            await send({'type': 'http.response.start', 'status': 308,
                        'headers': [(b'location', loc), (b'content-length', b'0'), (b'cache-control', b'no-store')]})
            return await send({'type': 'http.response.body', 'body': b''})
        user = context.user()
        if user is None:
            return await self._fail(scope, receive, send, 401, 'sign in first', rest)
        role = await to_thread.run_sync(lambda: access.viz_role(user, sid))
        if role is None:
            return await self._fail(scope, receive, send, 404, f'no session {sid!r}', rest)
        try:
            st = await to_thread.run_sync(self.ctx.state_for, sid, user)
        except HTTPException as e:
            return await self._fail(scope, receive, send, e.status_code, e.detail, rest)
        except (ValueError, KeyError, FileNotFoundError, NotADirectoryError, PermissionError) as e:
            return await self._fail(scope, receive, send, 409, f'cannot open this session: {str(e).strip(chr(39))}', rest)
        except Exception as e:  # noqa: BLE001 - surface it, keep serving
            log.exception(f'opening session {vlog.esc(sid)} failed')
            return await self._fail(scope, receive, send, 500, f'cannot open this session: {type(e).__name__}: {e}', rest)
        inner = dict(scope)
        inner['path'] = rest
        inner['raw_path'] = strip_raw(scope.get('raw_path'))
        inner['dataviz_path'] = path
        inner['dataviz_sid'] = sid
        token = context.BOUND.set(context.Bound(sid, role, st))
        try:
            await self.app(inner, receive, send)
        finally:
            context.BOUND.reset(token)


class FixedSession:
    """TEST ONLY (create_app(State) / Ctx(state)): every request is bound to that one State as its owner, as a
    local user without an account; there is no sign-in layer. Never used by the server (cli.py)."""

    LOCAL_USER = {'id': None, 'username': 'local', 'email': '', 'display_name': 'Local', 'is_admin': True,
                  'avatar_ver': 0}

    def __init__(self, app, ctx):
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        u = context.USER.set(context.user() or self.LOCAL_USER)
        b = context.BOUND.set(context.Bound('', 'owner', self.ctx.fixed))
        try:
            await self.app(scope, receive, send)
        finally:
            context.BOUND.reset(b)
            context.USER.reset(u)
