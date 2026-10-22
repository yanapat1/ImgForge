"""cleanlab label quality (dataviz/ml/label_quality.py): k-fold out-of-fold class probabilities, cleanlab's per-box
scores and dataset summary, the object-detection report on detector predictions, the State cache (keyed by the
settings and the labels) and the /api/label-quality routes."""

import numpy as np
import pytest

pytest.importorskip('cleanlab')

from dataviz.ml import label_quality as lq  # noqa: E402


def _noisy(n_per=100, n_cls=4, dim=32, flips=20, seed=0):
    """Gaussian clusters (unit rows) with `flips` labels moved to the next class -> (x, given labels, flipped ids)."""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(n_cls, dim)) * 3
    true = np.repeat(np.arange(n_cls), n_per)
    x = centers[true] + rng.normal(size=(len(true), dim))
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    given = true.copy()
    flip = rng.choice(len(true), flips, replace=False)
    given[flip] = (given[flip] + 1) % n_cls
    return x.astype(np.float32), given, flip


def test_oof_probs_are_distributions_and_out_of_fold():
    x, y, _ = _noisy()
    probs, fit = lq.oof_pred_probs(x, y, folds=5)
    assert probs.shape == (len(y), 4) and probs.dtype == np.float32
    np.testing.assert_allclose(probs.sum(1), 1, atol=1e-4)
    assert fit['folds'] == 5 and fit['accuracy'] > 0.8
    assert lq.oof_pred_probs(x[:4], y[:4])[1]['folds'] == 0      # too small: nothing to cross-validate


@pytest.mark.parametrize('classifier', lq.CLASSIFIERS)
def test_flipped_labels_rank_first(classifier):
    x, y, flip = _noisy()
    per, summary = lq.run(x, y, {'classifier': classifier}, class_names=list('abcd'))
    worst = set(np.argsort(per['cl_quality'])[:len(flip)])
    assert len(worst & set(flip)) >= 0.8 * len(flip)
    issues = np.flatnonzero(per['cl_issue'])
    assert len(set(issues) & set(flip)) >= 0.7 * len(flip)
    assert sorted(per['cl_rank'][issues]) == list(range(len(issues)))       # 0 = most likely wrong
    assert (per['cl_suggested'][flip] != y[flip]).mean() > 0.7              # cleanlab's guess is the true class
    assert 0 < summary['health_score'] <= 1 and summary['n_issues'] == len(issues)
    assert {r['name'] for r in summary['class_ranking']} == set('abcd')
    assert np.asarray(summary['confident_joint']).shape == (4, 4)
    assert np.isfinite(per['cl_ood_feat']).all() and np.isfinite(per['cl_ood_prob']).all()


def test_small_classes_and_removed_boxes_are_left_out():
    x, y, _ = _noisy()
    y = y.copy()
    y[:3] = 6                                              # a 3-box class 6; class 4 and 5 have no boxes at all
    keep = np.ones(len(y), bool)
    keep[10:15] = False                                    # removed by a curation fix
    per, summary = lq.run(x, y, keep=keep)
    assert per['pred_probs'].shape == (len(y), 7)
    for ids in (np.arange(3), np.arange(10, 15)):
        assert np.isnan(per['cl_quality'][ids]).all() and not per['cl_issue'][ids].any()
        assert (per['cl_rank'][ids] == -1).all() and not per['cl_scored'][ids].any()
    assert summary['left_out_classes'] == [6] and 6 not in summary['class_ids']
    assert np.isfinite(per['cl_quality'][15:]).all()


def test_settings_validate_and_hash():
    s = lq.settings({'folds': 3, 'nonsense': 1}, classifier='knn', folds=None)
    assert s['folds'] == 3 and s['classifier'] == 'knn' and 'nonsense' not in s
    assert lq.settings_hash(s) != lq.settings_hash(lq.settings())
    with pytest.raises(ValueError):
        lq.settings(classifier='svm')


def test_object_detection_report():
    # image a: box 0 matches its prediction, box 1 is a class-1 label the model calls class 0, plus an unlabelled
    # object; image b: the label sits where the model sees nothing, the model's box is elsewhere
    gts = [[(0, 0, 10, 10, 50, 50), (1, 1, 60, 60, 90, 90)], [(2, 0, 0, 0, 20, 20)]]
    preds = [[(0, 0, .99, 11, 11, 51, 49), (1, 0, .99, 60, 60, 90, 90), (2, 1, .99, 100, 100, 150, 150)],
             [(3, 0, .99, 30, 30, 60, 60), (4, 0, .6, 2, 2, 22, 22)]]
    per, summary = lq.od_report(['a', 'b'], gts, preds, 4, 2)
    assert per['cl_swap'][1] < 0.1 < per['cl_swap'][0]
    assert per['cl_badloc'][2] < per['cl_badloc'][1]
    assert np.isnan(per['cl_swap'][3])                                       # box 3 is in no scored image
    assert {m['pid'] for m in summary['overlooked'] if m['quality'] < 0.1} == {2, 3}
    assert [i['image'] for i in summary['images']] == ['a', 'b']             # worst first


# ---------------------------------------------------------------- State + routes

@pytest.fixture
def world(pg, tmp_path):
    from authutil import account
    from vizutil import make_yolo, viz_session
    rng = np.random.default_rng(1)
    boxes = []
    for i in range(40):                     # class 0: small boxes, class 1: large ones (geometry separates them)
        c = i % 2
        w = 0.1 + 0.02 * rng.random() if c == 0 else 0.5 + 0.02 * rng.random()
        boxes.append((f'im{i}', f'{c} 0.5 0.5 {w:.4f} {w:.4f}\n'))
    ds = make_yolo(tmp_path / 'ds', boxes=tuple(boxes))
    alice = account('alice')
    return {'tmp': tmp_path, 'alice': alice, 'S': viz_session(alice['id'], 'S', ds)}


def test_state_caches_by_settings_and_labels(world):
    from dataviz.data.sources import LocalSource
    from dataviz.state import State
    from vizutil import _Job, geom_registry
    st = State(LocalSource(world['tmp'] / 'ds'), None, geom_registry(), default_feature='geom',
               workspace_root=world['tmp'] / 'ws')
    st._index_job(_Job())
    assert st.cached_label_quality('geom') is None
    per, summary = st.get_label_quality('geom', _Job())
    assert len(per['cl_quality']) == len(st.samples) == 40 and summary['feature'] == 'geom'
    st.label_quality.clear()
    assert st.cached_label_quality('geom') is not None                       # from disk
    assert st.cached_label_quality('geom', classifier='knn') is None          # another setting
    st.labels = st.labels.copy()
    st.labels[0] = 1 - st.labels[0]                                           # a curation relabel
    assert st.cached_label_quality('geom') is None


def test_routes(world):
    from authutil import client, login
    from vizutil import make_viz_app
    from conftest import wait_job
    _, app = make_viz_app(world['tmp'] / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    base = f'/s/{world["S"]}/api'
    assert c.get(f'{base}/label-quality').json() == {'ready': False, 'feature': 'geom'}
    assert c.post(f'{base}/label-quality', json={'classifier': 'svm'}).status_code == 400
    r = c.post(f'{base}/label-quality', json={}).json()
    assert not r['ready']
    assert wait_job(c, r["job_id"])["status"] == "done"
    d = c.get(f'{base}/label-quality').json()
    assert d['ready'] and len(d['quality']) == 40 and set(d['issue']) <= {0, 1}
    assert d['summary']['folds_used'] == 5 and 'health_score' in d['summary']
    s = c.get(f'{base}/sample/0').json()
    assert 0 <= s['label_quality']['quality'] <= 1 and s['label_quality']['top'][0]['p'] > 0
    csv = c.post(f'{base}/export', json={'ids': [0, 1]}).text
    assert 'cl_quality' in csv.splitlines()[0]
