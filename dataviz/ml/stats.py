"""
Two-group statistical tests for the visualizer's comparisons (Compare tab, Model tab, Evaluation). Each returns a
plain dict the page shows as one chip: {test, stat, p, effect, effect_name, n_a, n_b, verdict}. Vectorised numpy /
scipy.stats; random parts (permutations, subsampling) use a fixed seed, so a number never changes on reload.

    contingency(a, b)          chi-square of homogeneity of two count vectors (class mix A vs B) + Cramér's V,
                               per-category two-proportion z with Benjamini-Hochberg q
    bowker(m)                  symmetry of a square paired table (class changes A -> B): generalised McNemar
    two_sample(x, y)           KS (exact / asymptotic p) + Mann-Whitney U + Cliff's delta + Wasserstein-1
    mmd(x, y)                  kernel two-sample test (RBF, median heuristic) on embeddings, permutation p
    mcnemar(b, c)              paired binary outcomes (only-A vs only-B): exact binomial p, odds ratio
    two_prop(k1, n1, k2, n2)   two-proportion z test (e.g. images with no detection)
    kappa(m)                   Cohen's kappa of an agreement table (two models' top class per image)

ALPHA is the level the verdicts use. Effect sizes come with every p: a big dataset makes tiny differences
"significant", so the verdict also needs the effect past a small threshold to say "differs".
"""

import numpy as np

ALPHA = 0.05
SEED = 0


def _verdict(p, effect=None, small=None):
    if p is None or not np.isfinite(p):
        return 'n/a'
    if p >= ALPHA:
        return 'no evidence of a difference'
    if effect is not None and small is not None and abs(effect) < small:
        return 'significant but negligible'
    return 'differs'


def _f(v, nd=4):
    return None if v is None or not np.isfinite(v) else round(float(v), nd)


def bh(p):
    """Benjamini-Hochberg q values (same order as p; NaN stays NaN)."""
    p = np.asarray(p, float)
    q = np.full_like(p, np.nan)
    ok = np.isfinite(p)
    if not ok.any():
        return q
    pv = p[ok]
    o = np.argsort(pv)
    m = len(pv)
    r = pv[o] * m / np.arange(1, m + 1)
    r = np.minimum.accumulate(r[::-1])[::-1]
    out = np.empty(m)
    out[o] = np.minimum(r, 1.0)
    q[ok] = out
    return q


def contingency(a, b):
    """A vs B category counts (same order): chi-square of homogeneity over the categories present in either, Cramér's
    V, and per category a two-proportion z test (its share in A vs in B) with BH q."""
    from scipy import stats
    a, b = np.asarray(a, float), np.asarray(b, float)
    na, nb = a.sum(), b.sum()
    keep = (a + b) > 0
    out = {'test': 'chi-square', 'n_a': int(na), 'n_b': int(nb)}
    if na == 0 or nb == 0 or keep.sum() < 2:
        return {**out, 'stat': None, 'p': None, 'df': 0, 'effect': None, 'effect_name': "Cramér's V",
                'verdict': 'n/a', 'per': [None] * len(a)}
    t = np.vstack([a[keep], b[keep]])
    chi2, p, df, _ = stats.chi2_contingency(t, correction=False)
    v = np.sqrt(chi2 / (t.sum() * (min(t.shape) - 1)))
    # per category: share in A vs share in B
    pa, pb = a / na, b / nb
    pool = (a + b) / (na + nb)
    se = np.sqrt(pool * (1 - pool) * (1 / na + 1 / nb))
    with np.errstate(divide='ignore', invalid='ignore'):
        z = np.where(se > 0, (pb - pa) / se, np.nan)
    pz = 2 * stats.norm.sf(np.abs(z))
    q = bh(pz)
    per = [None if not np.isfinite(qq) else {'z': _f(zz, 3), 'p': _f(pp, 6), 'q': _f(qq, 6), 'sig': bool(qq < ALPHA)}
           for zz, pp, qq in zip(z, pz, q)]
    return {**out, 'stat': _f(chi2, 3), 'p': _f(p, 8), 'df': int(df), 'effect': _f(v, 4),
            'effect_name': "Cramér's V", 'verdict': _verdict(p, v, 0.1), 'per': per}


def bowker(m):
    """Bowker's test of symmetry of a square table of paired outcomes (row = A's class, column = B's): do boxes change
    class A -> B as often as B -> A? Pairs with no off-diagonal count are skipped."""
    from scipy import stats
    m = np.asarray(m, float)
    up, lo = np.triu(m, 1), np.tril(m, -1).T
    s = up + lo
    keep = s > 0
    n = int(m.sum())
    changed = float(s.sum())
    base = {'test': 'Bowker symmetry', 'n_a': n, 'n_b': n, 'effect': _f(changed / n if n else np.nan, 4),
            'effect_name': 'share changed'}
    if not keep.any():
        return {**base, 'stat': None, 'p': None, 'df': 0, 'verdict': 'n/a'}
    stat = float((((up - lo) ** 2)[keep] / s[keep]).sum())
    df = int(keep.sum())
    p = float(stats.chi2.sf(stat, df))
    return {**base, 'stat': _f(stat, 3), 'p': _f(p, 8), 'df': df, 'verdict': _verdict(p)}


def cliffs_delta(x, y, u=None):
    """P(y > x) - P(y < x), from the Mann-Whitney U of y (no n*m loop)."""
    from scipy import stats
    if u is None:
        u = stats.mannwhitneyu(y, x, alternative='two-sided').statistic
    return 2 * u / (len(x) * len(y)) - 1


def two_sample(x, y):
    """Two numeric samples (a box statistic in A vs B): KS + Mann-Whitney U, Cliff's delta (B vs A) and W1."""
    from scipy import stats
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    x, y = x[np.isfinite(x)], y[np.isfinite(y)]
    base = {'test': 'KS + Mann-Whitney', 'n_a': int(len(x)), 'n_b': int(len(y)), 'effect_name': "Cliff's δ"}
    if len(x) < 2 or len(y) < 2:
        return {**base, 'stat': None, 'p': None, 'effect': None, 'verdict': 'n/a'}
    ks = stats.ks_2samp(x, y)
    mw = stats.mannwhitneyu(y, x, alternative='two-sided')
    d = cliffs_delta(x, y, mw.statistic)
    w1 = stats.wasserstein_distance(x, y)
    return {**base, 'stat': _f(ks.statistic, 4), 'p': _f(ks.pvalue, 8), 'p_mw': _f(mw.pvalue, 8),
            'effect': _f(d, 4), 'w1': _f(w1, 6), 'verdict': _verdict(min(ks.pvalue, mw.pvalue), d, 0.147)}


def mmd(x, y, n_max=1500, perms=200, seed=SEED):
    """Kernel two-sample test: MMD² (unbiased) with an RBF kernel at the median pairwise distance, p from `perms`
    label permutations of the pooled sample. Each side is subsampled to n_max (the Gram matrix is (2 n_max)²)."""
    rng = np.random.default_rng(seed)
    x, y = np.asarray(x, np.float32), np.asarray(y, np.float32)
    base = {'test': 'MMD (RBF) permutation', 'n_a': int(len(x)), 'n_b': int(len(y)), 'effect_name': 'MMD²'}
    if len(x) < 5 or len(y) < 5:
        return {**base, 'stat': None, 'p': None, 'effect': None, 'verdict': 'n/a'}
    if len(x) > n_max:
        x = x[rng.choice(len(x), n_max, replace=False)]
    if len(y) > n_max:
        y = y[rng.choice(len(y), n_max, replace=False)]
    z = np.vstack([x, y]).astype(np.float64)
    sq = (z * z).sum(1)
    d2 = np.maximum(sq[:, None] + sq[None, :] - 2 * z @ z.T, 0)
    iu = np.triu_indices(len(z), 1)
    med = np.median(d2[iu])
    k = np.exp(-d2 / (med if med > 0 else 1.0))
    np.fill_diagonal(k, 0)
    nx, ny = len(x), len(y)

    def stat(lab):                                   # lab: bool, True = group x
        a = lab.astype(np.float64)
        b = 1 - a
        kxx = a @ k @ a / (nx * (nx - 1))
        kyy = b @ k @ b / (ny * (ny - 1))
        kxy = a @ k @ b / (nx * ny)
        return kxx + kyy - 2 * kxy
    lab = np.r_[np.ones(nx, bool), np.zeros(ny, bool)]
    obs = stat(lab)
    # permutations in blocks: (perms, n) @ K is one BLAS call instead of `perms` matrix-vector products
    P = np.stack([rng.permutation(lab) for _ in range(perms)]).astype(np.float64)
    Q = 1 - P
    KP, KQ = P @ k, Q @ k
    null = ((KP * P).sum(1) / (nx * (nx - 1)) + (KQ * Q).sum(1) / (ny * (ny - 1)) - 2 * (KP * Q).sum(1) / (nx * ny))
    p = (1 + int((null >= obs).sum())) / (perms + 1)
    return {**base, 'stat': _f(obs, 6), 'p': _f(p, 6), 'effect': _f(obs, 6), 'perms': perms,
            'verdict': _verdict(p)}


def mcnemar(b, c):
    """Paired binary outcomes: b = cases only A got right, c = only B. Exact two-sided binomial p, odds ratio b / c."""
    from scipy import stats
    b, c = int(b), int(c)
    base = {'test': 'McNemar (exact)', 'n_a': b, 'n_b': c, 'effect_name': 'odds ratio A:B'}
    if b + c == 0:
        return {**base, 'stat': None, 'p': None, 'effect': None, 'verdict': 'n/a'}
    p = float(stats.binomtest(b, b + c, 0.5).pvalue)
    ratio = (b + 0.5) / (c + 0.5)                     # Haldane: finite with a zero cell
    return {**base, 'stat': b + c, 'p': _f(p, 8), 'effect': _f(ratio, 3), 'verdict': _verdict(p)}


def two_prop(k1, n1, k2, n2):
    """Two-proportion z test: k1 of n1 (A) vs k2 of n2 (B). Effect: the difference of the shares (B - A)."""
    from scipy import stats
    base = {'test': 'two-proportion z', 'n_a': int(n1), 'n_b': int(n2), 'effect_name': 'Δ share'}
    if n1 == 0 or n2 == 0:
        return {**base, 'stat': None, 'p': None, 'effect': None, 'verdict': 'n/a'}
    p1, p2 = k1 / n1, k2 / n2
    pool = (k1 + k2) / (n1 + n2)
    se = np.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return {**base, 'stat': 0.0, 'p': 1.0, 'effect': _f(p2 - p1, 4), 'verdict': _verdict(1.0)}
    z = (p2 - p1) / se
    p = float(2 * stats.norm.sf(abs(z)))
    return {**base, 'stat': _f(z, 3), 'p': _f(p, 8), 'effect': _f(p2 - p1, 4), 'verdict': _verdict(p, p2 - p1, 0.02)}


def kappa(m):
    """Cohen's kappa of a square agreement table (rows: rater A, columns: rater B, same categories)."""
    m = np.asarray(m, float)
    n = m.sum()
    if n == 0:
        return {'test': "Cohen's κ", 'n_a': 0, 'n_b': 0, 'agree': None, 'effect': None, 'effect_name': 'κ'}
    po = np.trace(m) / n
    pe = float((m.sum(0) * m.sum(1)).sum()) / (n * n)
    k = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return {'test': "Cohen's κ", 'n_a': int(n), 'n_b': int(n), 'agree': _f(po, 4), 'effect': _f(k, 4),
            'effect_name': 'κ'}
