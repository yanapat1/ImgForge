"""
The FastAPI application: Ctx (what is being served), the state proxy that lets "open another dataset"
swap the State under live routes, request logging, and the route modules wired together.
"""

import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from html import escape
from starlette.exceptions import HTTPException

from ..core import logging as vlog
from ..paths import STATIC_DIR
from ..state import State
from .static import JsonGZip, NoCacheStatic
from .routes import system, projections, samples, datasets, models, labeling, label_import, curation, llm


class Ctx:
    """What the app is serving right now. Opening another dataset swaps `state`; routes always see the current one."""

    def __init__(self, state, registry=None, datasets=None, cfg=None, open_dataset=None, models=None,
                 model_service=None, jobs=None, labeling=None, llm=None):
        self.state = state                  # None until a session is opened (the page shows the Sessions home)
        self.registry = registry            # feature registry (shared by every dataset)
        self.datasets = datasets            # connections.Registry (saved S3 connections + datasets)
        self.cfg = cfg or state.cfg
        self.open_dataset = open_dataset    # callable(dataset dict) -> new State, set by main()
        self.models = models                # models.registry.ModelRegistry (saved detectors, shared by every dataset)
        self.model_service = model_service  # models.service.ModelService (predictions + evaluation)
        self.jobs = jobs or state.jobs      # core.jobs.JobManager, shared by every session
        self.labeling = labeling            # labeling.projects.ProjectRegistry (labeling sessions, keyed by pid)
        self.llm = llm                      # llm.store.LLMStore (saved LLM providers + feature switches)
        self.curation = lambda st: None     # State -> its scanned data.curation.Curation or None (routes/curation.py)

    def current(self):
        """The open session's State; 409 when none is open (every dataset route goes through this)."""
        st = self.state
        if st is None:
            raise HTTPException(409, 'no session open')
        return st


class _StateProxy:
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


def create_app(ctx) -> FastAPI:
    if isinstance(ctx, State):               # tests / old callers: a single fixed dataset
        ctx = Ctx(ctx)
    state = _StateProxy(ctx)
    app = FastAPI(title='Dataset Visualizer')
    app.add_middleware(JsonGZip)
    log_http = bool(ctx.cfg.get('logging', {}).get('http', True))
    log_static = bool(ctx.cfg.get('logging', {}).get('static', False))   # also log /static/ files, thumbnails, polling

    @app.middleware('http')
    async def access_log(request: Request, call_next):
        t0 = time.perf_counter()
        path = request.url.path
        try:
            resp = await call_next(request)
        except Exception:
            http_log.exception(f'[bold red]{request.method} {vlog.esc(path)} crashed[/]')
            raise
        ms = (time.perf_counter() - t0) * 1000
        code = resp.status_code
        if not log_http:
            return resp
        who = request.client.host if request.client else '?'
        msg = (f'{request.method:<6} {vlog.esc(path)} [dim]→[/] [{_status_style(code)}]{code}[/] '
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
    return app
