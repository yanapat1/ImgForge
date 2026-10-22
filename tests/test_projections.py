"""Every projection method offered in the page, and the map-quality metrics (CPU only, small synthetic data)."""
import numpy as np
import pytest

from dataviz.ml import analysis

NEEDS_PACKAGE = {'umap': 'umap', 'pacmap': 'pacmap', 'localmap': 'pacmap'}


def _data(n, d=64, seed=0, spread=0.5):
    rng = np.random.default_rng(seed)
    c = rng.normal(size=(5, d))
    x = (c[rng.integers(0, 5, n)] + spread * rng.normal(size=(n, d))).astype(np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def test_every_method_has_a_category():
    assert {m['category'] for m in analysis.METHODS.values()} == set(analysis.CATEGORIES)


@pytest.mark.parametrize('method', sorted(analysis.METHODS))
@pytest.mark.parametrize('n', [3, 60])
def test_project_shape(method, n):
    if method in NEEDS_PACKAGE:
        pytest.importorskip(NEEDS_PACKAGE[method])
    xy, info = analysis.project(_data(n), method)
    assert xy.shape == (n, 2) and xy.dtype == np.float32 and np.isfinite(xy).all()
    assert isinstance(info, dict)


def test_subset_fit_covers_every_point(monkeypatch):
    # isomap has transform(), spectral does not (placed by neighbours): both must place all points
    monkeypatch.setitem(analysis.FIT_MAX, 'isomap', 50)
    monkeypatch.setitem(analysis.FIT_MAX, 'spectral', 50)
    x = _data(200, spread=2.0)   # overlapping clusters: spectral collapses fully separate ones to a point each
    for method in ('isomap', 'spectral'):
        xy, _ = analysis.project(x, method)
        assert xy.shape == (200, 2) and np.isfinite(xy).all()
        assert len(np.unique(xy.round(5), axis=0)) > 150


def test_projection_quality():
    x = _data(400)
    good = analysis.projection_quality(x, analysis.project(x, 'pca')[0])
    assert set(good) == {'trustworthiness', 'continuity', 'knn_preserved', 'k', 'n'}
    assert all(0 <= good[k] <= 1 for k in ('trustworthiness', 'continuity', 'knn_preserved'))
    noise = analysis.projection_quality(x, np.random.default_rng(1).normal(size=(400, 2)))
    assert good['trustworthiness'] > noise['trustworthiness']
    assert analysis.projection_quality(x[:2], x[:2, :2]) == {}
