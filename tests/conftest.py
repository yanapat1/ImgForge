"""Shared fixtures: a temp workspace root, a FastAPI TestClient on it, small generated JPEGs."""

import io
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # makes `dataviz` importable


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


@pytest.fixture
def store(tmp_path):
    from dataviz.labeling.store import ProjectStore
    s = ProjectStore(tmp_path / 'annotations.db')
    s.init_project('p' * 32, 'test')
    yield s
    s.close()


@pytest.fixture
def app_ctx(tmp_path):
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
    return ctx, create_app(ctx)


@pytest.fixture
def client(app_ctx):
    from fastapi.testclient import TestClient
    with TestClient(app_ctx[1]) as c:
        c.ctx = app_ctx[0]
        yield c


def wait_job(client, job_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f'/api/jobs/{job_id}').json()
        if j.get('status') in ('done', 'error'):
            return j
        time.sleep(0.05)
    raise TimeoutError(f'job {job_id} did not finish')
