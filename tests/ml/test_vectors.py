"""ml/vectors.py: feature vectors in pgvector (round trip, overwrite, k-NN, HNSW threshold, isolation)."""

import numpy as np
from sqlalchemy import text


def _rand(n, d, seed=0):
    return np.random.default_rng(seed).standard_normal((n, d)).astype(np.float32)


def _close(a, b):
    assert a.shape == b.shape
    np.testing.assert_allclose(a, b, rtol=2e-3, atol=2e-3 * float(np.abs(b).max()))


def test_round_trip(pg):
    from dataviz.ml import vectors
    X = _rand(300, 64)
    r = vectors.save('fp1', 'dinov2', X)
    assert r['type'] == 'halfvec' and r['n'] == 300 and r['dim'] == 64 and not r['has_index']
    assert vectors.exists('fp1', 'dinov2')
    Y = vectors.load('fp1', 'dinov2')
    assert Y.dtype == np.float32
    _close(Y, X)
    assert (vectors.load_ids('fp1', 'dinov2') == np.arange(300)).all()
    assert vectors.load('fp1', 'other') is None and not vectors.exists('fp1', 'other')


def test_large_values_stored_as_fp32(pg):
    from dataviz.ml import vectors
    X = _rand(20, 5) * 1e6                  # beyond fp16: box areas in pixels
    assert vectors.save('fp1', 'geom', X)['type'] == 'vector'
    np.testing.assert_allclose(vectors.load('fp1', 'geom'), X, rtol=1e-6)


def test_overwrite_with_different_n_and_dim(pg):
    from dataviz.ml import vectors
    vectors.save('fp1', 'k', _rand(100, 16))
    X = _rand(37, 8, seed=1)
    r = vectors.save('fp1', 'k', X)
    _close(vectors.load('fp1', 'k'), X)
    i = vectors.info('fp1', 'k')
    assert i['n'] == 37 and i['dim'] == 8 and i['complete'] and i['id'] == r['id']


def test_knn_matches_numpy(pg):
    from dataviz.ml import vectors
    X = _rand(500, 32, seed=2)
    vectors.save('fp1', 'k', X)
    q = _rand(1, 32, seed=3)[0]
    ids, sims = vectors.knn('fp1', 'k', q, 10)
    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    ref = Xn @ (q / np.linalg.norm(q))
    order = np.argsort(-ref)
    assert len(ids) == 10 and (np.diff(sims) <= 1e-6).all()
    np.testing.assert_allclose(sims, ref[ids], atol=3e-3)
    # same top-k up to fp16 ties: every returned id scores at least the 10th best minus the precision
    assert (ref[ids] >= ref[order[9]] - 3e-3).all()
    assert len(set(ids) & set(order[:10])) >= 9
    e_ids, e_sims = vectors.knn('fp1', 'missing', q, 5)
    assert len(e_ids) == 0 and len(e_sims) == 0


def test_hnsw_index_over_threshold(pg):
    from dataviz import db
    from dataviz.ml import vectors
    small = vectors.save('fp1', 'a', _rand(50, 16), hnsw_min_rows=100)
    big = vectors.save('fp1', 'b', _rand(150, 16), hnsw_min_rows=100)
    assert not small['has_index'] and big['has_index']
    assert vectors.info('fp1', 'b')['has_index']
    with db.connect() as c:
        idx = c.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'vec' AND tablename = :t"),
                        {'t': f"emb_{big['id']}"}).scalars().all()
    assert any('hnsw' in d and 'halfvec_cosine_ops' in d for d in idx)
    ids, sims = vectors.knn('fp1', 'b', _rand(1, 16, 9)[0], 60)     # > default ef_search 40
    assert len(ids) == 60


def test_fingerprints_isolated_and_delete(pg):
    from dataviz.ml import vectors
    A, B = _rand(10, 4, 1), _rand(20, 4, 2)
    vectors.save('fpA', 'k', A)
    vectors.save('fpB', 'k', B)
    _close(vectors.load('fpA', 'k'), A)
    _close(vectors.load('fpB', 'k'), B)
    assert vectors.delete('fpA', 'k') and not vectors.delete('fpA', 'k')
    assert vectors.load('fpA', 'k') is None
    _close(vectors.load('fpB', 'k'), B)


def test_incomplete_set_loads_none(pg):
    from dataviz import db
    from dataviz.ml import vectors
    vectors.save('fp1', 'k', _rand(10, 4))
    with db.begin() as c:
        c.execute(text("UPDATE vec.feature_sets SET complete = false WHERE fingerprint = 'fp1'"))
    assert vectors.load('fp1', 'k') is None and not vectors.exists('fp1', 'k')
    assert len(vectors.knn('fp1', 'k', np.ones(4), 3)[0]) == 0


def test_bad_input(pg):
    import pytest
    from dataviz.ml import vectors
    with pytest.raises(ValueError):
        vectors.save('fp', 'k', np.zeros(5))
    with pytest.raises(ValueError):
        vectors.save('fp', 'k', np.full((2, 2), np.nan))
