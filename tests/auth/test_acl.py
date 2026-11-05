"""Access control: every /api route carries an auth dependency (the route guard)."""

import pytest
from fastapi.routing import APIRoute

PUBLIC = {'/api/auth/config', '/api/auth/login', '/api/auth/signup'}
# route modules still being moved to per-user repositories + access checks (phase 2); strict once they are
PENDING = {'dataviz.server.routes.datasets', 'dataviz.server.routes.labeling', 'dataviz.server.routes.label_import',
           'dataviz.server.routes.llm'}


def _guarded(dependant):
    for d in dependant.dependencies:
        if getattr(d.call, '__dataviz_auth__', False) or _guarded(d):
            return True
    return False


@pytest.fixture
def full_app(pg, tmp_path):
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.data import connections
    from dataviz.labeling.projects import ProjectRegistry
    from dataviz.llm.store import LLMStore
    from dataviz.server.app import Ctx, create_app
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    root = tmp_path / 'ws'
    ctx = Ctx(None, datasets=connections.Registry(root), cfg=cfg, jobs=JobManager(),
              labeling=ProjectRegistry(root / 'labeling'), llm=LLMStore(root))
    return create_app(ctx)


def test_every_route_guarded(full_app):
    missing = {}
    paths = set()
    for r in full_app.routes:
        if not isinstance(r, APIRoute) or not r.path.startswith('/api'):
            continue
        paths.add(r.path)
        if r.path in PUBLIC or _guarded(r.dependant):
            continue
        for m in r.methods:
            missing.setdefault(r.endpoint.__module__, []).append(f'{m} {r.path}')
    assert PUBLIC <= paths and '/api/points' in paths and '/api/auth/me' in paths
    strict = {k: v for k, v in missing.items() if k not in PENDING}
    assert not strict, f'unguarded /api routes: {strict}'
    pending = sorted(x for k in PENDING for x in missing.get(k, ()))
    if pending:
        pytest.xfail(f'{len(pending)} routes not guarded yet: ' + ', '.join(pending))
