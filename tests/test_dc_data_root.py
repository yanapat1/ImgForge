"""storage.data_root: DATAVIZ_DATA_ROOT moves it, and a root on the container's writable layer is reported."""

from pathlib import Path

from dataviz.core.config import DEFAULT_CONFIG, load_config
from dataviz.datacenter import store

ROOT = Path(__file__).resolve().parents[1]


def test_env_moves_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv('DATAVIZ_DATA_ROOT', str(tmp_path / 'dc'))
    cfg = load_config(DEFAULT_CONFIG)
    assert store.data_root(cfg) == (tmp_path / 'dc').resolve()
    monkeypatch.delenv('DATAVIZ_DATA_ROOT')
    assert load_config(DEFAULT_CONFIG)['storage']['data_root'] == 'data'


def test_on_mount(tmp_path, monkeypatch):
    real_exists = store.os.path.exists
    monkeypatch.setattr(store.os.path, 'exists', lambda p: True if p == '/.dockerenv' else real_exists(p))
    monkeypatch.setattr(store.os.path, 'ismount', lambda p: False)
    assert not store.on_mount(tmp_path)
    monkeypatch.setattr(store.os.path, 'ismount', lambda p: Path(p) == tmp_path)
    assert store.on_mount(tmp_path / 'a' / 'b')


def test_compose_files_mount_the_data_root():
    for name in ('docker-compose.yml', 'docker-compose.server.yml'):
        assert ':/workspace/data' in (ROOT / name).read_text(), name
