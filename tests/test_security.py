"""Hardening for open sign-up: a normal account must not run code on the server (file models, adapters, uploads),
browse its folders through the model picker, or probe hosts through the serving-list helper; a file model whose
owner is no longer an admin does not load. (Found by the security review of the multi-user change.)"""

import pytest
from fastapi.testclient import TestClient

from conftest import _TestUserMiddleware, make_user


def _app(tmp_path, user):
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.data import connections
    from dataviz.models.registry import ModelRegistry
    from dataviz.models.service import ModelService
    from dataviz.server.app import Ctx, create_app
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    cfg['logging']['http'] = False
    root = tmp_path / 'ws'
    mr = ModelRegistry(root, allowed_roots=[tmp_path])
    ctx = Ctx(None, datasets=connections.Registry(root), cfg=cfg, jobs=JobManager(), models=mr,
              model_service=ModelService(mr, cfg))
    app = create_app(ctx)
    app.add_middleware(_TestUserMiddleware, user=user)
    return app, mr


def test_normal_user_cannot_run_code_or_browse(pg, tmp_path):
    u = make_user('mallory')
    marker = tmp_path / 'pwned'
    app, _ = _app(tmp_path, u)
    with TestClient(app, headers={'X-Requested-With': 'dataviz'}) as c:
        assert c.get('/api/models/browse', params={'path': '/etc'}).status_code == 403
        assert c.get('/api/models/discover').status_code == 403
        adapter = f"open({str(marker)!r}, 'w').write('x')\n".encode()
        up = c.post('/api/models/upload', files=[('files', ('w.onnx', b'junk')), ('files', ('a.py', adapter))])
        assert up.status_code == 403
        weights = tmp_path / 'w.onnx'
        weights.write_bytes(b'junk')
        s = c.post('/api/models', json={'family': 'custom', 'format': 'onnx', 'path': str(weights)})
        assert s.status_code == 400 and 'only admins' in s.json()['detail']
        r = c.get('/api/models/serving-list', params={'url': 'http://127.0.0.1:1'})
        assert r.status_code == 400 and '127.0.0.1' not in r.json()['detail']    # no remote details echoed back
    assert not marker.exists()


def test_serving_list_obeys_who_can_add_connections(pg, tmp_path):
    from dataviz.auth import settings
    from sqlalchemy import text
    from dataviz import db
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.app_settings(key, value) VALUES ('who_can_add_connections', '"admins"')
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""))
    settings.clear_cache()
    app, _ = _app(tmp_path, make_user('eve'))
    with TestClient(app, headers={'X-Requested-With': 'dataviz'}) as c:
        assert c.get('/api/models/serving-list', params={'url': 'http://127.0.0.1:1'}).status_code == 403
        assert c.post('/api/models', json={'family': 'custom', 'format': 'api',
                                           'api': {'url': 'http://10.0.0.1/'}}).status_code == 403
    settings.clear_cache()


def test_file_model_of_a_demoted_owner_does_not_load(pg, tmp_path):
    from sqlalchemy import text
    from dataviz import db
    from dataviz.models import runners
    admin = make_user('root', admin=True)
    weights = tmp_path / 'w.onnx'
    weights.write_bytes(b'junk')
    _, mr = _app(tmp_path, admin)
    m = mr.upsert({'family': 'custom', 'format': 'onnx', 'path': str(weights)}, owner_id=admin['id'], admin=True)
    with db.begin() as c:
        c.execute(text('UPDATE app.users SET is_admin = false WHERE id = :u'), {'u': admin['id']})
    with pytest.raises(PermissionError):
        runners.load_runner(mr.model(m['id']), tmp_path)
