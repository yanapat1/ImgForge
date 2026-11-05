"""dataviz/ml/stats.py: the two-group tests behind the Compare / Model / Evaluation chips, checked against scipy and
known answers; deterministic (fixed seed)."""

import numpy as np
from scipy import stats as ss

from dataviz.ml import stats


def test_contingency_matches_scipy_and_per_class():
    a, b = [50, 30, 20, 0], [20, 30, 50, 0]
    r = stats.contingency(a, b)
    chi2, p, df, _ = ss.chi2_contingency(np.array([a[:3], b[:3]]), correction=False)
    assert abs(r['stat'] - chi2) < 1e-3 and abs(r['p'] - p) < 1e-8 and r['df'] == df == 2
    assert 0.3 < r['effect'] < 0.4 and r['verdict'] == 'differs'
    assert r['per'][3] is None                                   # absent from both: not tested
    assert r['per'][0]['sig'] and not r['per'][1]['sig'] and r['per'][2]['z'] > 0
    same = stats.contingency([100, 100], [101, 99])
    assert same['p'] > 0.5 and same['verdict'] == 'no evidence of a difference'
    assert stats.contingency([5, 0], [0, 0])['p'] is None        # B empty: n/a


def test_bowker_symmetry():
    sym = [[50, 5, 2], [5, 40, 3], [2, 3, 30]]
    assert stats.bowker(sym)['stat'] == 0 and stats.bowker(sym)['p'] == 1.0
    skew = [[50, 20, 0], [2, 40, 0], [0, 0, 30]]                  # class 0 -> 1 far more than 1 -> 0
    r = stats.bowker(skew)
    assert abs(r['stat'] - (18 ** 2) / 22) < 1e-3 and r['df'] == 1 and r['p'] < 0.001 and r['verdict'] == 'differs'
    assert stats.bowker([[3, 0], [0, 4]])['p'] is None            # nothing changed


def test_two_sample_ks_mw_cliff():
    rng = np.random.default_rng(1)
    x, y = rng.normal(0, 1, 400), rng.normal(0.5, 1, 300)
    r = stats.two_sample(x, y)
    assert abs(r['stat'] - ss.ks_2samp(x, y).statistic) < 1e-4
    assert abs(r['p_mw'] - ss.mannwhitneyu(y, x).pvalue) < 1e-6
    # Cliff's delta from U equals the direct pairwise definition
    direct = (np.sign(y[:, None] - x[None, :])).mean()
    assert abs(r['effect'] - direct) < 1e-4 and r['effect'] > 0.147 and r['verdict'] == 'differs'
    assert stats.two_sample([1.0], [2.0, 3.0])['p'] is None
    same = stats.two_sample(rng.normal(0, 1, 300), rng.normal(0, 1, 300))
    assert same['p'] > 0.01


def test_mmd_detects_shift_and_is_calibrated():
    rng = np.random.default_rng(2)
    x = rng.normal(0, 1, (200, 8))
    shifted = stats.mmd(x, rng.normal(0.6, 1, (200, 8)), perms=100)
    assert shifted['p'] < 0.02 and shifted['verdict'] == 'differs'
    ps = [stats.mmd(rng.normal(0, 1, (60, 4)), rng.normal(0, 1, (60, 4)), perms=50, seed=k)['p'] for k in range(10)]
    assert np.mean(ps) > 0.25                                    # alike: p is not small on average
    r1, r2 = stats.mmd(x, x + 0.5, perms=50), stats.mmd(x, x + 0.5, perms=50)
    assert r1 == r2                                              # deterministic


def test_mcnemar_two_prop_kappa_bh():
    r = stats.mcnemar(30, 10)
    assert abs(r['p'] - ss.binomtest(30, 40, 0.5).pvalue) < 1e-7 and r['effect'] > 2
    assert stats.mcnemar(0, 0)['p'] is None and stats.mcnemar(5, 5)['p'] == 1.0
    t = stats.two_prop(10, 100, 30, 100)
    assert t['p'] < 0.001 and abs(t['effect'] - 0.2) < 1e-9
    k = stats.kappa([[20, 5], [10, 15]])
    assert abs(k['agree'] - 0.7) < 1e-9 and abs(k['effect'] - 0.4) < 1e-9
    q = stats.bh([0.01, 0.04, 0.03, np.nan])
    assert np.allclose(q[:3], [0.03, 0.04, 0.04]) and np.isnan(q[3])
