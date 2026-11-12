"""Projection › Dimensions = 3D cloud: POST /api/projection {dims: 3} computes a 3-component projection under its own
key (2D keys stay as they were, so saved maps remain valid), answers with z, lists it in Saved views as 3D, and the
2D-only consumers (density groups, the live feed, Compare's member maps) leave it alone."""

import json
import time

import numpy as np

from authutil import account, client, login
from dataviz.state import projection_dims, projection_key
from vizutil import make_viz_app, make_yolo, viz_session


def _boxes(n=12):
    return tuple((f'i{k:02d}', f'{k % 2} 0.5 0.5 {0.1 + 0.03 * k:.2f} {0.4 - 0.02 * k:.2f}\n'
                  f'{(k + 1) % 2} 0.3 0.3 {0.05 + 0.01 * k:.2f} 0.1\n') for k in range(n))


def _projection(c, sid, body, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.post(f'/s/{sid}/api/projection', json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        if out['ready']:
            return out
        time.sleep(0.1)
    raise TimeoutError('projection did not finish')


def test_projection_key_dims():
    k2 = projection_key('geom', 'pca', {}, None)
    k3 = projection_key('geom', 'pca', {}, None, 3)
    assert k2 == 'geom|pca|{}|all' and k3 == k2 + '|3d'                  # 2D keys unchanged
    assert projection_dims(k2) == 2 and projection_dims(k3) == 3
    assert projection_key('geom', 'pca', {}, [1, 0], 3) == 'geom|pca|{}|0,1|3d'


def test_3d_projection_route(pg, tmp_path):
    alice = account('alice')
    sid = viz_session(alice['id'], 'D', make_yolo(tmp_path / 'd', boxes=_boxes()))
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200

    flat = _projection(c, sid, {'feature': 'geom', 'method': 'pca'})
    assert 'z' not in flat and not flat['key'].endswith('|3d')
    cloud = _projection(c, sid, {'feature': 'geom', 'method': 'pca', 'dims': 3})
    n = len(cloud['id'])
    assert cloud['key'] == flat['key'] + '|3d' and n == 24
    assert len(cloud['x']) == len(cloud['y']) == len(cloud['z']) == n
    assert len(cloud['info']['explained_variance']) == 3                  # 2 feature dims: PC3 padded with 0
    assert np.isfinite(cloud['z']).all()
    assert c.post(f'/s/{sid}/api/projection', json={'feature': 'geom', 'method': 'pca', 'dims': 4}).status_code == 422

    # Saved views: both, the 3D one marked
    views = {v['key']: v for v in c.get(f'/s/{sid}/api/projections').json()['views']}
    assert views[cloud['key']]['dims'] == 3 and views[cloud['key']]['label'].endswith('3D')
    assert views[cloud['key']]['classes'] is None and views[cloud['key']]['feature'] == 'geom'
    assert views[flat['key']]['dims'] == 2 and '3D' not in views[flat['key']]['label']

    # density groups are 2D only
    body = {'feature': 'geom', 'method': 'pca', 'min_cluster_size': 3, 'per_class': True}
    assert c.post(f'/s/{sid}/api/groups', json={**body, 'dims': 3}).status_code == 409
    assert c.post(f'/s/{sid}/api/groups', json=body).status_code == 200
    st = ctx.states[sid]
    assert st.groups(cloud['key'], 3, True) is None

    # the saved 3D cloud reloads from disk with its 3 columns
    st.projections.clear()
    assert st.get_projection(cloud['key'])['xy'].shape == (n, 3)
    idx = json.loads((st.proj_dir / 'index.json').read_text())
    assert idx[cloud['key']]['dims'] == 3


def test_live_feed_skips_3d(pg, tmp_path):
    from dataviz.image import live
    alice = account('alice')
    sid = viz_session(alice['id'], 'D', make_yolo(tmp_path / 'd', boxes=_boxes(4)))
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    cloud = _projection(c, sid, {'feature': 'geom', 'method': 'pca', 'dims': 3})
    feed = live.LiveFeed.__new__(live.LiveFeed)            # only _place's early checks run
    feed.state, feed.proj = ctx.states[sid], cloud['key']
    assert feed._place({'ann': {'image': 'i00.jpg', 'xywhn': (0.5, 0.5, 0.1, 0.1)}}, None) == 0
