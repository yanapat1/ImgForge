"""GPU vs CPU equivalence for the analysis helpers (GPU tests skip without a usable CUDA device)."""
import numpy as np
import pytest

from dataviz.core import hardware
from dataviz.ml import analysis

needs_cuda = pytest.mark.skipif(hardware.device() != 'cuda', reason='no usable CUDA device')


def _data(n=3000, d=64, seed=0):
    rng = np.random.default_rng(seed)
    c = rng.normal(size=(8, d))
    x = (c[rng.integers(0, 8, n)] + 0.7 * rng.normal(size=(n, d))).astype(np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def test_knn_numpy_structure():
    x = _data(300)
    idx, sim = analysis.knn(x, k=5)
    assert idx.shape == (300, 5) and sim.shape == (300, 5)
    assert idx.dtype == np.int64 and sim.dtype == np.float32
    assert not (idx == np.arange(300)[:, None]).any()
    assert (np.diff(sim, axis=1) <= 1e-6).all()


def test_knn_tiny():
    idx, sim = analysis.knn(_data(1), k=5)
    assert idx.shape == (1, 0)


@needs_cuda
def test_knn_gpu_matches_numpy():
    x = _data(3000)
    i0, s0 = analysis._knn_numpy(x, 10)
    i1, s1 = analysis._knn_gpu(x, 10, lambda *a, **k: None)
    assert i1.shape == i0.shape and i1.dtype == np.int64 and s1.dtype == np.float32
    np.testing.assert_allclose(s0, s1, atol=1e-5)
    # identical neighbour sets except where the k-th/(k+1)-th similarities tie
    same = [set(a) == set(b) for a, b in zip(i0, i1)]
    assert np.mean(same) > 0.995


@needs_cuda
def test_knn_gpu_small_chunks(monkeypatch):
    x = _data(1500)
    monkeypatch.setattr(hardware, 'HEADROOM', 0.999999)          # forces the minimum chunk size
    i0, s0 = analysis._knn_numpy(x, 5)
    i1, s1 = analysis._knn_gpu(x, 5, lambda *a, **k: None)
    np.testing.assert_allclose(s0, s1, atol=1e-5)


@needs_cuda
def test_pca_gpu_matches_sklearn():
    from sklearn.decomposition import PCA
    x = _data(2500, 96)
    p = PCA(5, svd_solver='full')
    ref = p.fit_transform(x)
    out, ratio = analysis._pca_gpu(x, 5)
    np.testing.assert_allclose(ratio, p.explained_variance_ratio_, atol=1e-5)
    np.testing.assert_allclose(np.abs(out), np.abs(ref), atol=1e-4)      # sign is fixed by svd_flip convention
    np.testing.assert_allclose(out, ref, atol=1e-4)


def test_pca_reduce_cpu_path(monkeypatch):
    monkeypatch.setattr(analysis, '_gpu_ok', lambda *a: False)
    out, ratio = analysis.pca_reduce(_data(500, 32), 4)
    assert out.shape == (500, 4) and len(ratio) == 4


def test_knn_falls_back_on_gpu_error(monkeypatch):
    monkeypatch.setattr(analysis, '_gpu_ok', lambda *a: True)
    monkeypatch.setattr(analysis, '_knn_gpu', lambda *a: (_ for _ in ()).throw(RuntimeError('CUDA out of memory')))
    monkeypatch.setattr(analysis, '_gpu_failed', lambda e: None)
    x = _data(200)
    i, s = analysis.knn(x, 3)
    i0, s0 = analysis._knn_numpy(x, 3)
    np.testing.assert_array_equal(i, i0)
