"""Zones: which parts an install has (dataviz/zones.py), and that a partial install stays free of the visualizer."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
