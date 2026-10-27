"""Shared fixtures: a PostgreSQL database per test, a temp workspace root, a FastAPI TestClient on it, small
generated JPEGs.

PostgreSQL: DATAVIZ_TEST_DATABASE_URL (a server where the user may CREATE DATABASE), else a pgvector container
started through docker (tests/conftest_io.docker_db; reused across runs while it is up). The schema is migrated
once into a template database; every test gets a fresh copy of it (CREATE DATABASE ... TEMPLATE, ~50 ms).
"""

import io
import os
import sys
import time
import uuid
from pathlib import Path

# pytest -n <workers> (pytest-xdist): numpy / BLAS / numba / torch start one thread per core in EVERY worker (and in every
# app.py a smoke test launches), so 16 workers on 128 cores ran 2000 threads and the suite got slower than serial. Worse,
# every index build spawns hardware.workers('proc') decode processes (16 on 128 cores): 32 workers x 16 = 560 pythons
# and the 64 GiB container was OOM-killed. Each worker is pinned to its own slice of the cores instead (sched_setaffinity
# is what hardware._cpu_count reads, and children -- decode pools, app.py -- inherit it), with BLAS threads to match.
# Set before anything imports numpy. A serial run is left as it is.
if os.environ.get('PYTEST_XDIST_WORKER_COUNT'):
    _cores = sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else list(range(os.cpu_count() or 2))
    _n = int(os.environ['PYTEST_XDIST_WORKER_COUNT'])
    _share = max(2, len(_cores) // _n)
    _i = int(os.environ.get('PYTEST_XDIST_WORKER', 'gw0').lstrip('gw') or 0)
    if hasattr(os, 'sched_setaffinity'):
        _start = (_i * _share) % max(1, len(_cores) - _share + 1)
        os.sched_setaffinity(0, _cores[_start:_start + _share])
    for _var in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMBA_NUM_THREADS',
                 'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ.setdefault(_var, str(_share))

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # makes `dataviz` importable


def pytest_xdist_auto_num_workers(config):
    """-n auto. xdist's default is one worker per core (128 here) at ~2 GiB each, which OOM-kills a 64 GiB container.
    Measured on 128 cores / 64 GiB: -n 8 89 s (22 GiB peak), 12 75 s (30 GiB), 16 70 s (32 GiB, a timing test flaked);
    the floor is the UMAP numba compile (~30 s) every worker pays. DATAVIZ_TEST_WORKERS overrides."""
    if os.environ.get('DATAVIZ_TEST_WORKERS'):
        return int(os.environ['DATAVIZ_TEST_WORKERS'])
    n = min(12, max(1, len(os.sched_getaffinity(0)) // 4))
    try:                                                    # a visible cgroup v2 limit: half of it at ~2.5 GiB a worker
        limit = open('/sys/fs/cgroup/memory.max').read().strip()
        if limit != 'max':
            n = min(n, max(1, int(limit) // 2 // (5 << 29)))
    except (OSError, ValueError):
        pass
    return n


def pytest_configure(config):
    # upstream: starlette's TestClient (fastapi 0.115.6) still names anyio.abc.BlockingPortal; not our code
    config.addinivalue_line('filterwarnings', 'ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning')


def make_jpeg(path, w=64, h=48, color=(200, 80, 40)):
    from PIL import Image
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (w, h), color).save(path, 'JPEG')
    return path


def jpeg_bytes(w=64, h=48, color=(10, 120, 200)):
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', (w, h), color).save(b, 'JPEG')
    return b.getvalue()


PG_IMAGE = 'pgvector/pgvector:0.8.0-pg17'
TEST_SECRET_KEY = 'q2Xb0hJb7fWm1Xr3d6Y0cS9Zp8Lk4Nv2Tg5Ue7Ia1Ro='   # Fernet key for tests only


def _admin_url(base, dbname):
    from sqlalchemy.engine import make_url
    return make_url(base).set(drivername='postgresql', database=dbname).render_as_string(hide_password=False)


@pytest.fixture(scope='session')
def pg_server():
    """Base URL (database 'postgres') of a server the tests may create databases on, with a migrated template."""
    import psycopg
    url = os.environ.get('DATAVIZ_TEST_DATABASE_URL')
    if url:
        yield from _with_template(_admin_url(url, 'postgres'))
        return
    from conftest_io import docker_db, pg_ready
    # every worker holds up to 2 x pool_size (5) connections: the default 100 ran out at -n 16
    with docker_db('dataviz-pgtest', PG_IMAGE, {'POSTGRES_PASSWORD': 'pw'}, 5432, pg_ready, 120,
                   args=('-c', 'max_connections=1000')) as (host, port):
        yield from _with_template(f'postgresql://postgres:pw@{host}:{port}/postgres')
    del psycopg


def _with_template(base):
    import psycopg
    from dataviz import db
    from dataviz.db import migrate
    tmpl = f'dataviz_tmpl_{os.getpid()}'
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS {tmpl} WITH (FORCE)')
        c.execute(f'CREATE DATABASE {tmpl}')
    db.init(_admin_url(base, tmpl))
    migrate.upgrade()
    db.dispose()                                   # a template must have no open connections
    yield {'base': base, 'template': tmpl}
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS {tmpl} WITH (FORCE)')


@pytest.fixture
def pg(pg_server):
    """A fresh, migrated database for this test; the app's engine (dataviz.db) points at it."""
    import psycopg
    from dataviz import db
    from dataviz.db import crypto
    name = f't_{uuid.uuid4().hex[:12]}'
    with psycopg.connect(pg_server['base'], autocommit=True) as c:
        c.execute(f'CREATE DATABASE {name} TEMPLATE {pg_server["template"]}')
    url = _admin_url(pg_server['base'], name)
    db.init(url, pool_size=5)
    crypto.init(TEST_SECRET_KEY)
    from dataviz.auth import tiers
    tiers.clear_cache()                            # the in-process tier cache belongs to the previous database
    yield url
    db.dispose()
    with psycopg.connect(pg_server['base'], autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def make_user(username, admin=False, display_name=None, active=True):
    """Insert a user straight into the database (no password login: the hash is a placeholder) -> user dict."""
    from sqlalchemy import text
    from dataviz import db
    uid = uuid.uuid4().hex
    u = {'id': uid, 'username': username, 'email': f'{username}@example.test',
         'display_name': display_name or username.title(), 'is_admin': admin, 'avatar_ver': 0}
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.users(id, username, email, display_name, password_hash, is_admin, is_active)
                          VALUES (:id, :username, :email, :display_name, 'x', :is_admin, :active)"""),
                  {**u, 'active': active})
    return u


@pytest.fixture
def store(pg):
    """A ProjectStore on an empty project 'ppp…' owned by a fresh user (store.owner)."""
    from dataviz.data import sessions
    from dataviz.labeling.store import ProjectStore
    owner = make_user('owner')
    pid = 'p' * 32
    sessions.create('label', owner['id'], 'test', sid=pid)
    s = ProjectStore(pid)
    s.init_project(pid, 'test')
    s.owner = owner
    yield s
    s.close()


def new_store(owner_id, name, pid=None):
    """Another ProjectStore (session row + project row) in the test database."""
    from dataviz.data import sessions
    from dataviz.labeling.store import ProjectStore
    pid = pid or uuid.uuid4().hex
    sessions.create('label', owner_id, name, sid=pid)
    s = ProjectStore(pid)
    s.init_project(pid, name)
    return s


class _TestUserMiddleware:
    """TEST ONLY: every request runs as `user` (dataviz.auth.context.USER), so routes that read
    context.user_id() work without a login. Phase 2 replaces it with a real session cookie."""

    def __init__(self, app, user):
        self.app = app
        self.user = user

    async def __call__(self, scope, receive, send):
        from dataviz.auth import context
        token = context.USER.set(self.user)
        try:
            await self.app(scope, receive, send)
        finally:
            context.USER.reset(token)


@pytest.fixture
def app_ctx(pg, tmp_path):
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.data import connections
    from dataviz.labeling.projects import ProjectRegistry
    from dataviz.llm.store import LLMStore
    from dataviz.server.app import Ctx, create_app
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    cfg['logging']['http'] = False
    root = tmp_path / 'ws'
    ctx = Ctx(None, datasets=connections.Registry(root), cfg=cfg, jobs=JobManager(),
              labeling=ProjectRegistry(root / 'labeling'), llm=LLMStore(root))
    app = create_app(ctx)
    ctx.user = make_user('admin', admin=True)
    app.add_middleware(_TestUserMiddleware, user=ctx.user)
    return ctx, app


@pytest.fixture
def client(app_ctx):
    from fastapi.testclient import TestClient
    with TestClient(app_ctx[1]) as c:
        c.ctx = app_ctx[0]
        c.user = app_ctx[0].user
        yield c


def wait_job(client, job_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f'/api/jobs/{job_id}').json()
        if j.get('status') in ('done', 'error'):
            return j
        time.sleep(0.05)
    raise TimeoutError(f'job {job_id} did not finish')


# ---------------------------------------------------------------- multi-user helpers (added for the per-user repositories)
@pytest.fixture
def tmp_roots(monkeypatch, tmp_path):
    """The folders local paths may use (data/connections.py allowed_roots, read by the routes on every request)
    also include tmp_path, where the test folders live. Use with pytestmark = pytest.mark.usefixtures('tmp_roots')."""
    from dataviz.data import connections
    orig = connections.allowed_roots
    monkeypatch.setattr(connections, 'allowed_roots', lambda cfg: [*orig(cfg), tmp_path.resolve()])
    return tmp_path


class _SwitchUserMiddleware:
    """TEST ONLY: each request runs as the user its `x-test-user` header names (an id in `users`)."""

    def __init__(self, app, users):
        self.app = app
        self.users = users

    async def __call__(self, scope, receive, send):
        from dataviz.auth import context
        uid = dict(scope.get('headers') or []).get(b'x-test-user', b'').decode()
        token = context.USER.set(self.users.get(uid))
        try:
            await self.app(scope, receive, send)
        finally:
            context.USER.reset(token)


@pytest.fixture
def client_as(pg, tmp_path):
    """client_as(user) -> a TestClient running as `user`; every client shares ONE app and Ctx (like the server:
    same jobs, same export runs, same model activations). client_as.ctx is that Ctx."""
    from fastapi.testclient import TestClient
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.data import connections
    from dataviz.labeling.projects import ProjectRegistry
    from dataviz.llm.store import LLMStore
    from dataviz.models.registry import ModelRegistry
    from dataviz.server.app import Ctx, create_app
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    cfg['logging']['http'] = False
    root = tmp_path / 'ws'
    ctx = Ctx(None, datasets=connections.Registry(root), cfg=cfg, jobs=JobManager(),
              labeling=ProjectRegistry(root / 'labeling'), llm=LLMStore(root),
              models=ModelRegistry(root, allowed_roots=[tmp_path]))
    users = {}
    app = create_app(ctx)
    app.add_middleware(_SwitchUserMiddleware, users=users)
    opened = []

    def make(user):
        users[user['id']] = user
        c = TestClient(app, headers={'x-test-user': user['id']})
        c.__enter__()
        c.ctx, c.user = ctx, user
        opened.append(c)
        return c
    make.ctx = ctx
    yield make
    for c in opened:
        c.__exit__(None, None, None)
