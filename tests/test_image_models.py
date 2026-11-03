"""A model on a one-point-per-image session (no labels to score against): its top class per image, score and count
(GET api/models/{id}/image-classes), two models' agreement + Cohen's kappa (GET api/image-models/agree), the
capability that shows the Model tab in predict mode, and the per-member split of a combined dataset."""

import json

import numpy as np
import pytest

from authutil import account, client, login
from conftest import make_jpeg
from dataviz.models import predict
from vizutil import make_viz_app, union_session, viz_session


def _flat(root, names):
    for k, n in enumerate(names):
        make_jpeg(root / n, 48, 32, color=(30 * k, 80, 140))
    return root


def _fake_preds(st, svc, m, rows, names):
    """Write the prediction cache as predict.run would: rows = [(image, x1, y1, x2, y2, score, cls)]."""
    images = sorted(st.by_image)
    at = {n: i for i, n in enumerate(images)}
    path = predict.cache_path(st, m, svc.fingerprint(m))
    path.parent.mkdir(parents=True, exist_ok=True)
    det = np.array([r[1:] for r in rows], np.float32).reshape(-1, 6)
    np.savez(path, images=np.array(images), img=np.array([at[r[0]] for r in rows], np.int32), det=det,
             names=np.array(json.dumps(names)), ms=np.array(3.0))


@pytest.fixture
def world(pg, tmp_path):
    from dataviz.models.registry import ModelRegistry
    from dataviz.models.service import ModelService
    alice = account('alice')
    sid = viz_session(alice['id'], 'shots', _flat(tmp_path / 'shots', ['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg']))
    from dataviz.server.app import create_app
    ctx, _ = make_viz_app(tmp_path / 'ws')
    ctx.models = ModelRegistry(ctx.datasets.root)                       # the model routes read them at build time
    ctx.model_service = ModelService(ctx.models, ctx.cfg)
    app = create_app(ctx)
    c = client(app)
    assert login(c, 'alice').status_code == 200
    mk = lambda n: ctx.models.upsert({'name': n, 'family': 'yolo', 'format': 'api', 'api': {'url': 'http://fake'}},  # noqa: E731
                                     owner_id=alice['id'], admin=True)
    return {'c': c, 'sid': sid, 'ctx': ctx, 'alice': alice, 'mk': mk, 'tmp': tmp_path}


def test_image_classes_and_agreement(world):
    c, sid, ctx = world['c'], world['sid'], world['ctx']
    caps = c.get(f'/s/{sid}/api/status').json()['caps']
    assert caps['unit'] == 'image' and caps['models'] == 'predict'
    st = ctx.state_for(sid)
    svc = ctx.model_service
    ma, mb = world['mk']('A'), world['mk']('B')
    assert c.get(f'/s/{sid}/api/models/{ma["id"]}/image-classes').status_code == 409    # not run yet
    _fake_preds(st, svc, ma, [('a.jpg', 0, 0, 9, 9, 0.9, 1), ('a.jpg', 0, 0, 5, 5, 0.95, 0),    # top: cat .95
                              ('b.jpg', 0, 0, 9, 9, 0.6, 1),                                   # dog
                              ('c.jpg', 0, 0, 9, 9, 0.1, 1)], ['cat', 'dog'])                  # below conf: nothing
    _fake_preds(st, svc, mb, [('a.jpg', 0, 0, 9, 9, 0.8, 0), ('b.jpg', 0, 0, 9, 9, 0.7, 0),
                              ('d.jpg', 0, 0, 9, 9, 0.7, 2)], ['dog', 'Cat', 'bird'])          # other order
    r = c.get(f'/s/{sid}/api/models/{ma["id"]}/image-classes', params={'conf': 0.25}).json()
    by = {st.samples[i]['image']: (r['pred_cls'][i], r['pred_score'][i], r['n_det'][i]) for i in range(len(st.samples))}
    assert r['names'] == ['cat', 'dog']
    assert by == {'a.jpg': (0, 0.95, 2), 'b.jpg': (1, 0.6, 1), 'c.jpg': (-1, 0.0, 0), 'd.jpg': (-1, 0.0, 0)}
    assert r['images_per_class'] == [1, 1] and r['detections_per_class'] == [1, 2] and r['no_detection'] == 2
    low = c.get(f'/s/{sid}/api/models/{ma["id"]}/image-classes', params={'conf': 0.05}).json()
    assert low['no_detection'] == 1                                     # c.jpg counts now
    g = c.get(f'/s/{sid}/api/image-models/agree', params={'a': ma['id'], 'b': mb['id'], 'conf': 0.25}).json()
    # a: cat, dog, -, -   b (dog, Cat, bird): Cat->? b.jpg 'dog'(0) ... by name: a.jpg cat vs dog, b.jpg dog vs dog,
    # c.jpg nothing vs nothing, d.jpg nothing vs bird -> 2 of 4 agree
    assert g['rows'] == ['cat', 'dog', '(nothing)'] and g['cols'] == ['dog', 'Cat', 'bird', '(nothing)']
    assert g['agree'] == 0.5 and g['n'] == 4 and sum(map(sum, g['table'])) == 4
    assert sorted(st.samples[i]['image'] for i in g['disagree_ids']) == ['a.jpg', 'd.jpg']
    assert -1 <= g['kappa'] <= 1


def test_union_of_image_sessions_splits_by_member(world):
    c, ctx = world['c'], world['ctx']
    alice = world['alice']
    other = viz_session(alice['id'], 'more', _flat(world['tmp'] / 'more', ['x.jpg', 'y.jpg']))
    U = union_session(alice['id'], 'both', [(world['sid'], 'A'), (other, 'B')])
    st = ctx.state_for(U)
    m = world['mk']('M')
    _fake_preds(st, ctx.model_service, m, [('A/a.jpg', 0, 0, 9, 9, 0.9, 0), ('B/x.jpg', 0, 0, 9, 9, 0.9, 1),
                                           ('B/y.jpg', 0, 0, 9, 9, 0.9, 1)], ['cat', 'dog'])
    r = c.get(f'/s/{U}/api/models/{m["id"]}/image-classes').json()
    assert [o['alias'] for o in r['by_origin']] == ['A', 'B']
    assert r['by_origin'][0]['images_per_class'] == [1, 0] and r['by_origin'][0]['no_detection'] == 3
    assert r['by_origin'][1]['images_per_class'] == [0, 2] and r['by_origin'][1]['n_images'] == 2


def test_union_box_models_per_member(pg, tmp_path):
    """A combined labelled dataset: Model tab scores per member (by_origin), the leaderboard's model x dataset
    numbers, and model A vs B restricted to one member, with McNemar's test."""
    from dataviz.models.registry import ModelRegistry
    from dataviz.models.service import ModelService
    from dataviz.server.app import create_app
    from vizutil import make_yolo
    alice = account('alice')
    a = make_yolo(tmp_path / 'A', classes=('cat', 'dog'), boxes=(('a1', '0 0.5 0.5 0.4 0.4\n'), ('a2', '1 0.5 0.5 0.4 0.4\n')))
    b = make_yolo(tmp_path / 'B', classes=('cat', 'dog'), boxes=(('b1', '0 0.5 0.5 0.4 0.4\n'),))
    A, B = viz_session(alice['id'], 'A', a), viz_session(alice['id'], 'B', b)
    U = union_session(alice['id'], 'A vs B', [(A, 'A'), (B, 'B')])
    ctx, _ = make_viz_app(tmp_path / 'ws')
    ctx.models = ModelRegistry(ctx.datasets.root)
    ctx.model_service = svc = ModelService(ctx.models, ctx.cfg)
    app = create_app(ctx)
    c = client(app)
    assert login(c, 'alice').status_code == 200
    st = ctx.state_for(U)
    mk = lambda n: ctx.models.upsert({'name': n, 'family': 'yolo', 'format': 'api', 'api': {'url': 'http://fake'}},  # noqa: E731
                                     owner_id=alice['id'], admin=True)
    good, half = mk('good'), mk('half')
    box = lambda s: list(s['bbox'])  # noqa: E731
    gt = {s['image']: s for s in st.samples}
    # good finds every label; half finds only A's (misses B/b1)
    _fake_preds(st, svc, good, [(n, *box(s), 0.9, s['cls']) for n, s in gt.items()], ['cat', 'dog'])
    _fake_preds(st, svc, half, [(n, *box(s), 0.9, s['cls']) for n, s in gt.items() if n.startswith('A/')], ['cat', 'dog'])
    ev = c.get(f'/s/{U}/api/models/{half["id"]}/eval', params={'split': 'all'}).json()
    side = {o['alias']: o for o in ev['by_origin']}
    assert side['A']['r'] == 1.0 and side['B']['r'] == 0.0 and side['A']['labels'] == 2 and side['B']['labels'] == 1
    assert side['A']['map50'] > 0.9 and side['B']['map50'] == 0.0
    ov = c.get(f'/s/{U}/api/eval/overview', params={'split': 'all'}).json()
    rows = {r['name']: {o['alias']: o['map50'] for o in r['by_origin']} for r in ov['leaderboard']['rows']}
    assert rows['good']['B'] > 0.9 and rows['half']['B'] == 0.0
    cmp_b = c.get(f'/s/{U}/api/eval/compare', params={'a': good['id'], 'b': half['id'], 'split': 'all', 'origin': 'B'}).json()
    assert cmp_b['origin'] == 'B' and cmp_b['labels'] == 1 and cmp_b['counts']['only_a'] == 1
    assert cmp_b['map50_b'] == 0.0 and cmp_b['test']['test'] == 'McNemar (exact)' and cmp_b['test']['p'] == 1.0
    both = c.get(f'/s/{U}/api/eval/compare', params={'a': good['id'], 'b': half['id'], 'split': 'all'}).json()
    assert both['origin'] is None and both['labels'] == 3
