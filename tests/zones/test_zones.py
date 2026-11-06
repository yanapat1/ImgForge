"""Zones: which parts an install has (dataviz/zones.py), and that a partial install stays free of the visualizer."""

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import TEST_SECRET_KEY
from dataviz import zones

APP_DIR = Path(__file__).resolve().parents[2]
SIGN_IN_LAYER = {'__init__', 'auth', 'users', 'admin', 'me', 'sharing'}     # installed by auth/middleware.py
VISUALIZER_LIBS = ('torch', 'torchvision', 'transformers', 'ultralytics', 'cv2', 'sklearn', 'scipy', 'umap', 'pacmap',
                   'openTSNE', 'plotly', 'cleanlab', 'rfdetr', 'onnxruntime', 'openvino')


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv('DATAVIZ_ZONES', raising=False)


def test_every_zone_by_default():
    assert zones.selected({}) == tuple(zones.ZONES)
    assert zones.selected(None) == tuple(zones.ZONES)


def test_config_list_and_core_is_always_there():
    assert zones.selected({'zones': ['datacenter']}) == ('core', 'datacenter')
    assert zones.on('core', {'zones': ['datacenter']})
    assert not zones.on('visualizer', {'zones': ['datacenter']})


def test_the_environment_wins_over_the_config(monkeypatch):
    monkeypatch.setenv('DATAVIZ_ZONES', ' core , datacenter,labeling ')
    assert zones.selected({'zones': ['datacenter', 'visualizer']}) == ('core', 'datacenter', 'labeling')


def test_empty_environment_means_not_set(monkeypatch):
    monkeypatch.setenv('DATAVIZ_ZONES', '  ')
    assert zones.selected({'zones': ['datacenter']}) == ('core', 'datacenter')


def test_unknown_zone_is_refused():
    with pytest.raises(ValueError, match='unknown zone nope'):
        zones.selected({'zones': ['nope']})


def test_missing_requirement_is_refused():
    with pytest.raises(ValueError, match='zone visualizer needs datacenter'):
        zones.selected({'zones': ['visualizer']})


def test_every_route_module_belongs_to_one_zone():
    files = {p.stem for p in (APP_DIR / 'dataviz/server/routes').glob('*.py')} - SIGN_IN_LAYER
    listed = [name for name, _ in zones.ROUTES]
    assert sorted(listed) == sorted(files)
    assert {zone for _, zone in zones.ROUTES} <= set(zones.ZONES)


# the app is built in a new process: only there can we prove what a partial install imports
PROBE = '''
import json, sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in LIBS:
            raise ImportError('blocked in this test: ' + name)
LIBS = set(sys.argv[1].split(','))
sys.meta_path.insert(0, Block())
from dataviz.core import config as vconfig
from dataviz.server.app import Ctx, create_app
cfg = vconfig.load_config()
paths = {getattr(r, 'path', '') for r in create_app(Ctx(None, cfg=cfg)).routes}
print(json.dumps({'paths': sorted(paths), 'mods': sorted(m for m in sys.modules if m.startswith('dataviz.'))}))
'''


def built_app(chosen, libs=VISUALIZER_LIBS):
    env = {**os.environ, 'DATAVIZ_ZONES': chosen, 'PYTHONPATH': str(APP_DIR)}
    out = subprocess.run([sys.executable, '-c', PROBE, ','.join(libs)], capture_output=True, text=True, env=env,
                         cwd=APP_DIR, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_data_center_install_runs_without_the_visualizer_libraries():
    got = built_app('core,datacenter')
    assert {'/api/dc/summary', '/api/connections', '/api/fs/browse', '/api/status', '/api/jobs'} <= set(got['paths'])
    assert '/api/points' not in got['paths'] and '/api/datasets' not in got['paths']
    assert not any(p.startswith('/api/label/') for p in got['paths'])
    assert 'dataviz.state' not in got['mods']
    assert not any(m.startswith('dataviz.server.routes.') and m.rsplit('.', 1)[1] in ('models', 'curation', 'labeling')
                   for m in got['mods'])


def test_full_install_keeps_every_route():
    got = built_app('core,datacenter,labeling,visualizer', libs=())
    assert {'/api/points', '/api/meta', '/api/settings', '/api/workspace', '/api/datasets', '/api/fs/embeddings',
            '/api/connections', '/api/fs/browse', '/api/dc/summary'} <= set(got['paths'])


# the real start-up (dataviz/cli.py) with the visualizer libraries blocked: database, sign-in and Data Center all work
SERVE = '''
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in LIBS:
            raise ImportError('blocked in this test: ' + name)
LIBS = set(sys.argv[1].split(','))
sys.meta_path.insert(0, Block())
sys.argv = ['app.py', *sys.argv[2:]]
from dataviz.cli import main
main()
'''


def test_data_center_server_starts_and_serves_without_the_visualizer(pg, tmp_path):
    jar = http.cookiejar.CookieJar()
    http_ = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {'Content-Type': 'application/json', 'X-Requested-With': 'dataviz'}

    def get(path):
        return json.loads(http_.open(urllib.request.Request(base + path, headers=headers), timeout=10).read())

    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(json.dumps({'dataset': {'workspace_root': str(tmp_path / 'ws')}, 'workers': {'enabled': False},
                               'logging': {'level': 'WARNING'}}))
    env = {**os.environ, 'DATAVIZ_ZONES': 'core,datacenter', 'DATABASE_URL': pg, 'DATAVIZ_SECRET_KEY': TEST_SECRET_KEY,
           'DATAVIZ_ADMIN_USERNAME': 'admin', 'DATAVIZ_ADMIN_PASSWORD': 'zone-test-password-1', 'PYTHONPATH': str(APP_DIR)}
    proc = subprocess.Popen([sys.executable, '-c', SERVE, ','.join(VISUALIZER_LIBS), '--config', str(cfg),
                             '--host', '127.0.0.1', '--port', str(port)], cwd=APP_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.fail(f'the server exited:\n{proc.stdout.read()[-3000:]}')
            try:
                get('/api/auth/config')
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail('the server did not answer in 60 s')
        body = json.dumps({'login': 'admin', 'password': 'zone-test-password-1'}).encode()
        http_.open(urllib.request.Request(base + '/api/auth/login', data=body, headers=headers), timeout=10)
        assert get('/api/status')['methods'] == []
        assert isinstance(get('/api/dc/summary'), dict)
        assert get('/api/dc/buckets') == []
        assert get('/api/connections') == {'connections': []}
        assert b'<html' in http_.open(base + '/', timeout=10).read().lower()
        with pytest.raises(urllib.error.HTTPError) as e:
            get('/api/datasets')
        assert e.value.code == 404
        made = http_.open(urllib.request.Request(base + '/api/dc/buckets', data=json.dumps({'name': 'x'}).encode(),
                                                 headers=headers), timeout=10)
        bid = json.loads(made.read())['id']
        send = {'bucket': bid, 'paths': [''], 'target': 'visualize'}
        with pytest.raises(urllib.error.HTTPError) as e:          # hiding the button is not enough: the server refuses
            http_.open(urllib.request.Request(base + '/api/dc/send', data=json.dumps(send).encode(), headers=headers),
                       timeout=10)
        assert 'no visualizer zone' in e.value.read().decode()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def data_center_install(monkeypatch):
    monkeypatch.setenv('DATAVIZ_ZONES', 'core,datacenter')


def test_tier_permissions_follow_the_zones(data_center_install):
    assert zones.perms_off() == {'dataset.create', 'model.add', 'model.upload', 'label.create', 'analysis.use',
                                 'analysis.write'}


def test_a_shared_permission_stays_while_one_of_its_zones_is_there(monkeypatch):
    monkeypatch.setenv('DATAVIZ_ZONES', 'core,datacenter,labeling')
    assert zones.perms_off() == {'dataset.create', 'analysis.use', 'analysis.write'}


def test_the_assistant_only_knows_the_installed_specialists(data_center_install):
    from dataviz.llm.engine import roles
    assert set(roles.installed()) == {'data'}
    card = roles.other_pages('data')
    assert 'no Dataset Visualizer, Data Analysis, Labeling' in card and 'screen viz' not in card and 'consult' not in card


def test_full_install_card_is_unchanged():
    from dataviz.llm.engine import roles
    assert set(roles.installed()) == {'data', 'visual', 'analysis'}
    card = roles.other_pages('data')
    assert 'screen viz' in card and 'This install has no' not in card


def test_analyst_install_has_no_visualizer_code_loaded():
    got = built_app('core,datacenter,analysis')
    assert {'/api/analysis/run', '/api/analysis/tree', '/api/dc/summary'} <= set(got['paths'])
    assert 'dataviz.state' not in got['mods'] and '/api/points' not in got['paths']


def test_the_assistant_cannot_go_to_a_missing_zone(data_center_install):
    from types import SimpleNamespace
    from dataviz.llm.tools import ToolError, nav
    ctx = SimpleNamespace(role='data', user={'id': 'u'}, depth=0)
    with pytest.raises(ToolError, match='no Visualizer'):
        nav._check_nav(ctx, {'screen': 'viz', 'goal': 'open it', 'session_id': 'x'})
    with pytest.raises(ToolError, match='no Labeling'):
        nav._check_nav(ctx, {'screen': 'label', 'goal': 'open it', 'session_id': 'x'})
    with pytest.raises(ToolError, match='no Visualizer'):
        nav._consult(ctx, {'page': 'viz', 'question': 'how many boxes?'})
