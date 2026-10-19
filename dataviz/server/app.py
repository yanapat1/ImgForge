"""
The FastAPI application: Ctx (what is being served: the loaded visualizer sessions and the shared services),
the state proxy the routes read the request's session through, request logging, and the route modules wired
together with the sign-in layer (auth/middleware.py) and the /s/<id>/ routing (server/routing.py).

Middleware, outermost first: AuthMiddleware (who is asking) -> SessionRouting (/s/<id>/ -> context.BOUND) ->
access log -> gzip -> routes.
"""

import re
import threading
import time
from collections import OrderedDict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from html import escape
from sqlalchemy import text
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware

from ..auth import access, context
from ..core import logging as vlog
from ..paths import STATIC_DIR
from ..state import State
from .routing import FixedSession, SessionRouting
from .static import JsonGZip, NoCacheStatic
from .routes import system, projections, samples, datasets, models, labeling, label_import, curation, llm

_DS_FIELDS = ('path', 'connection_id', 'bucket', 'prefix', 'mode', 'project_id', 'task_id', 'only_done')


class Ctx:
    """What the app serves: an LRU of loaded sessions (`states`: session id -> State, shared by every user who
    opens it) plus the services every session shares. A request reaches its State through context.bound()
    (set by server/routing.py); `state` / `current()` read that.

    Ctx(state) / create_app(State) is the TEST-ONLY single-dataset mode: every request is bound to that State
    and there is no sign-in layer (tests/test_curation.py, test_edits.py, test_llm.py)."""

    def __init__(self, state=None, registry=None, datasets=None, cfg=None, open_dataset=None, models=None,
                 model_service=None, jobs=None, labeling=None, llm=None):
        from ..core.jobs import JobManager
        self.fixed = state                  # test-only: the one State every request is bound to
        self.registry = registry            # feature registry (shared by every dataset)
        self.datasets = datasets            # connections.Registry (saved connections + datasets)
        if cfg is None:
            from ..core import config as vconfig
            cfg = state.cfg if state is not None else vconfig.load_config()
        self.cfg = cfg
        self.build = open_dataset           # callable(dataset dict, cache_dir=, rebuild=, refresh=) -> new State
        self.models = models                # models.registry.ModelRegistry (saved detectors)
        self.model_service = model_service  # models.service.ModelService (predictions + evaluation)
        self.jobs = jobs or (state.jobs if state is not None else JobManager())   # shared by every session
        self.labeling = labeling            # labeling.projects.ProjectRegistry (labeling sessions, keyed by pid)
        self.llm = llm                      # llm.store.LLMStore (saved LLM providers + feature switches)
        self.curation = lambda st: None     # State -> its scanned data.curation.Curation or None (routes/curation.py)
        self.states = OrderedDict()         # session id -> State, least recently used first
        self.max_loaded = max(1, int((cfg.get('sessions') or {}).get('max_loaded', 4)))
        self.close_hooks = []               # fn(tag): per-State caches of the route modules, dropped on unload
        self._lock = threading.Lock()
        self._open_locks = {}               # session id -> Lock: two first requests build ONE State

    # ------------------------------------------------------------ the request's session
    @property
    def state(self):
        """The State of this request's /s/<id>/ session; None on the home page (and outside a request)."""
        b = context.bound()
        return b.state if b is not None else self.fixed

    @state.setter
    def state(self, st):                    # old callers: "this State is now loaded" (None: nothing to do)
        if st is None:
            return
        if self.fixed is not None:
            self.fixed = st
        elif st.dataset_id:
            self._register(st.dataset_id, st)

    def current(self):
        """The request's State; 409 outside /s/<id>/ (every dataset route goes through this)."""
        st = self.state
        if st is None:
            raise HTTPException(409, 'open the session first')
        return st

    # ------------------------------------------------------------ loading / unloading
    @property
    def open_dataset(self):
        """callable(ds, **kw) -> State: build a State for a saved dataset and keep it loaded under ds['id']."""
        if self.build is None:
            return None

        def open_(ds, **kw):
            st = self.build(ds, **kw)
            self._register(ds['id'], st)
            return st
        return open_

    @open_dataset.setter
    def open_dataset(self, fn):
        self.build = fn

    def dataset(self, sid):
        """The saved dataset dict of visualizer session `sid` (KeyError when there is none)."""
        if self.datasets is not None:
            try:
                return self.datasets.dataset(sid)
            except KeyError:
                pass
        from .. import db
        with db.connect() as c:
            r = c.execute(text("""SELECT s.id, s.name, s.owner_id, s.tags, v.* FROM app.sessions s
                                  JOIN app.viz_sessions v ON v.session_id = s.id
                                  WHERE s.id = :sid AND s.kind = 'viz'"""), {'sid': sid}).mappings().first()
        if r is None:
            raise KeyError(f'no visualize session {sid!r}')
        ds = {'id': r['id'], 'name': r['name'], 'kind': r['source_kind'], 'owner_id': r['owner_id'],
              'tags': list(r['tags'] or [])}
        ds.update({k: r[k] for k in _DS_FIELDS if r[k] is not None})
        return ds

    def state_for(self, sid, user=None):
        """The loaded State of session `sid`, opening it on first use (blocking: call it off the event loop).
        Access is the caller's business (server/routing.py checks it first)."""
        if self.fixed is not None:
            return self.fixed
        with self._lock:
            st = self.states.get(sid)
            if st is not None:
                self.states.move_to_end(sid)
                return st
            lock = self._open_locks.setdefault(sid, threading.Lock())
        with lock:
            with self._lock:
                st = self.states.get(sid)
                if st is not None:
                    self.states.move_to_end(sid)
                    return st
            if self.build is None:
                raise HTTPException(501, 'opening sessions is not enabled in this server')
            try:
                ds = self.dataset(sid)
            except KeyError as e:
                raise HTTPException(404, str(e).strip("'\""))
            st = self.build(ds)
            self._register(sid, st)
        with self._lock:
            self._open_locks.pop(sid, None)
        return st

    def loaded(self, sid):
        """True while session `sid` is loaded on the server (the home cards' "open now")."""
        return sid in self.states

    def _register(self, sid, st):
        with self._lock:
            old = self.states.pop(sid, None)
            self.states[sid] = st
            drop = [old] if old is not None and old is not st else []
            # least recently used first; a State with a running job stays (its job holds it anyway)
            for k in list(self.states):
                if len(self.states) <= self.max_loaded:
                    break
                if k != sid and not self.states[k].busy():
                    drop.append(self.states.pop(k))
        for d in drop:
            self.close_state(d)

    def evict(self, sid):
        """Unload session `sid` (deleted, re-indexed): the next request opens it again."""
        with self._lock:
            st = self.states.pop(sid, None)
        if st is not None:
            self.close_state(st)

    def close_state(self, st):
        """Free an unloaded State and the caches other modules keep for it. A State that still has a job keeps
        its thread pool (the job holds the object; it is collected when the job ends)."""
        if st is self.fixed:
            return
        if not st.busy():
            st.close()
        if self.model_service is not None:
            self.model_service.drop_state(st.tag)
        for hook in list(self.close_hooks):
            try:
                hook(st.tag)
            except Exception:  # noqa: BLE001 - unloading never fails
                pass

    # ------------------------------------------------------------ jobs
    def job_filter(self, user):
        """job -> bool: the jobs `user` may see (own jobs, jobs of sessions they can open; admins: all)."""
        if self.fixed is not None or (user and user.get('is_admin')):
            return None
        if user is None:
            return lambda j: False
        roles = {}

        def ok(j):
            if j.owner and j.owner == user['id']:
                return True
            if not j.session_id:
                return False
            if j.session_id not in roles:
                roles[j.session_id] = access.role(user, j.session_id)
            return roles[j.session_id] is not None
        return ok

    def can_cancel(self, user, job):
        """The job's owner, an admin or the owner of its session; else 403 (404 when it is not even visible)."""
        if self.fixed is not None or user is None:
            return self.fixed is not None
        if user.get('is_admin') or (job.owner and job.owner == user['id']):
            return True
        have = access.role(user, job.session_id) if job.session_id else None
        if have == 'owner':
            return True
        if have is None:
            raise HTTPException(404, f'no job {job.id}')
        raise HTTPException(403, 'only who started this job or the owner of its session can cancel it')


class _StateProxy:
    """What the route modules call `state`: the State of the request's session (409 when there is none)."""

    def __init__(self, ctx):
        object.__setattr__(self, '_ctx', ctx)

    def __getattr__(self, name):
        return getattr(self._ctx.current(), name)


QUIET_PATHS = ('/api/crop/', '/api/cover/', '/api/jobs', '/static/', '/api/image/', '/plotly.min.js', '/api/status')
# labeling thumbnails / images / autosaves: a filmstrip asks for dozens per second
QUIET_LABEL = re.compile(r'^/api/label/projects/[0-9a-f]+/(thumb|image|annotations)/')
http_log = vlog.get_logger('http')


def _status_style(code):
    return 'green' if code < 300 else 'cyan' if code < 400 else 'yellow' if code < 500 else 'bold red'


class _AuthUnlessPreset:
    """The sign-in layer, skipped when an outer layer already set context.USER. Only tests do that
    (tests/conftest.py _TestUserMiddleware runs every request as one user without a cookie); in the server
    nothing sits outside this layer, so USER is always unset here and every request goes through AuthMiddleware."""

    def __init__(self, app, cfg=None):
        from ..auth.middleware import AuthMiddleware
        self.app = app
        self.auth = AuthMiddleware(app, cfg=cfg)

    async def __call__(self, scope, receive, send):
        u = context.user()
        if u is not None and scope['type'] == 'http':
            scope.setdefault('state', {})['user'] = u
            return await self.app(scope, receive, send)
        return await self.auth(scope, receive, send)


def create_app(ctx, auth=True) -> FastAPI:
    """The app for `ctx`. A bare State (or Ctx(state)) is the TEST-ONLY single-dataset mode: no sign-in layer,
    every request bound to that State. auth=False leaves out the sign-in layer (route tables, tests)."""
    if isinstance(ctx, State):
        ctx = Ctx(ctx)
    state = _StateProxy(ctx)
    app = FastAPI(title='Dataset Visualizer', docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(JsonGZip)
    log_http = bool(ctx.cfg.get('logging', {}).get('http', True))
    log_static = bool(ctx.cfg.get('logging', {}).get('static', False))   # also log /static/ files, thumbnails, polling

    @app.middleware('http')
    async def access_log(request: Request, call_next):
        t0 = time.perf_counter()
        path = request.url.path                 # quiet-path rules: the path the routes see (no /s/<id>)
        shown = request.scope.get('dataviz_path') or path
        try:
            resp = await call_next(request)
        except Exception:
            http_log.exception(f'[bold red]{request.method} {vlog.esc(shown)} crashed[/]')
            raise
        ms = (time.perf_counter() - t0) * 1000
        code = resp.status_code
        if not log_http:
            return resp
        who = request.client.host if request.client else '?'
        msg = (f'{request.method:<6} {vlog.esc(shown)} [dim]→[/] [{_status_style(code)}]{code}[/] '
               f'[dim]{ms:,.0f} ms · from {vlog.esc(who)}[/]')
        if code >= 500:
            http_log.error(msg)
        elif code >= 400:
            http_log.warning(msg)
        elif ms > 1000:
            http_log.warning(msg + ' [yellow](slow)[/]')
        elif log_static or path == '/' or not (path.startswith(QUIET_PATHS) or QUIET_LABEL.search(path)):
            http_log.info(msg)
        else:
            http_log.debug(msg)
        return resp

    def wants_html(request):
        return 'text/html' in request.headers.get('accept', '') and not request.url.path.startswith('/api/')

    def error_page(code, title, message):
        html = (STATIC_DIR / 'error.html').read_text(encoding='utf-8')
        for k, v in (('code', code), ('title', title), ('message', message)):
            html = html.replace('{{%s}}' % k, escape(str(v)))
        return HTMLResponse(html, status_code=code, headers={'Cache-Control': 'no-store'})

    @app.exception_handler(HTTPException)       # starlette's: also covers the router's own 404 / 405
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code >= 400 and exc.status_code not in (409, 503):   # 409/503 = "not computed / not ready yet"
            http_log.warning(f'{request.method} {vlog.esc(request.url.path)}: {vlog.esc(exc.detail)}')
        if wants_html(request):                 # a person typed a bad URL: a page, not JSON
            title = 'Page not found' if exc.status_code == 404 else 'Something went wrong'
            return error_page(exc.status_code, title, exc.detail if exc.status_code != 404 else 'This address does not exist.')
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def crash(request: Request, exc: Exception):    # the access_log middleware already logged the traceback
        if wants_html(request):
            return error_page(500, 'The server hit a problem', 'The request failed. Details are in the server log.')
        return JSONResponse({'detail': 'internal server error'}, status_code=500)

    for module in (system, projections, samples, datasets, models, labeling, label_import, curation, llm):
        app.include_router(module.router(state, ctx))
    app.mount('/static', NoCacheStatic(directory=STATIC_DIR), name='static')
    if ctx.fixed is not None:
        app.add_middleware(FixedSession, ctx=ctx)
        return app
    app.add_middleware(SessionRouting, ctx=ctx, error_page=error_page)    # added before auth: auth is outermost
    if auth:
        from ..auth.middleware import AuthMiddleware, install_auth
        install_auth(app, ctx.cfg)
        app.user_middleware = [Middleware(_AuthUnlessPreset, *m.args, **m.kwargs) if m.cls is AuthMiddleware else m
                               for m in app.user_middleware]
    return app
