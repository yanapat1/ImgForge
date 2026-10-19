"""Opening a session whose files changed after the cache was built shows that cache (no re-index) until refreshed."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_curation import _Job, make_dataset                   # noqa: E402
from dataviz.data.sources import LocalSource                    # noqa: E402
from dataviz.state import State                                 # noqa: E402


def _open(ds, ws, **kw):
    st = State(LocalSource(ds), None, {'none': None}, default_feature='none', workspace_root=ws, **kw)
    st._index_job(_Job())
    return st


def test_changed_files_show_last_cache_then_refresh(tmp_path):
    ds, ws = make_dataset(tmp_path / 'ds'), tmp_path / 'ws'
    first = _open(ds, ws)
    assert first.stale is None and first.samples
    built = first.cache_dir

    lab = next((ds / 'labels').glob('*.txt'))                  # a file is touched: the fingerprint (mtime) changes
    os.utime(lab, (lab.stat().st_atime, lab.stat().st_mtime + 100))

    again = _open(ds, ws)
    assert again.cache_dir == built, 'must reuse the cached workspace, not create a new folder'
    assert again.stale and again.stale['cached'] != again.stale['current']
    assert len(again.samples) == len(first.samples)
    assert len(list(ws.glob('ds__*'))) == 1

    fresh = _open(ds, ws, refresh=True)                        # "Update cache"
    assert fresh.stale is None and fresh.cache_dir != built
    assert len(list(ws.glob('ds__*'))) == 2

    assert _open(ds, ws).stale is None                         # now up to date: plain cache hit


def test_unchanged_files_are_a_plain_cache_hit(tmp_path):
    ds, ws = make_dataset(tmp_path / 'ds'), tmp_path / 'ws'
    a = _open(ds, ws)
    b = _open(ds, ws)
    assert b.stale is None and b.cache_dir == a.cache_dir
