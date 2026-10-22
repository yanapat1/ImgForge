"""
Benchmark of the cleanlab label-quality check (dataviz/ml/label_quality.run) on synthetic embeddings (not a test;
run by hand):

    .venv/bin/python tests/bench_label_quality.py [n ...] [--dim 768] [--classes 20] [--classifier logreg]
    (default n: 50000 150000)

The k-NN the page already computes for every feature space (analysis.knn) is timed apart and handed in, as
State.get_label_quality does. Prints the seconds spent in each stage the job reports (PCA, the k folds, cleanlab scores, label issues, OOD, class
statistics) and the total, plus how many of the 2 % flipped labels rank in the worst 2 % (a sanity check that a
faster path still finds them).
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from dataviz.ml import analysis, label_quality as lq  # noqa: E402


def data(n, dim, n_cls, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(n_cls, dim)).astype(np.float32) * 0.6
    true = rng.integers(0, n_cls, n)
    x = centers[true] + rng.normal(size=(n, dim)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    given = true.copy()
    flip = rng.choice(n, n // 50, replace=False)
    given[flip] = (given[flip] + 1 + rng.integers(0, n_cls - 1, len(flip))) % n_cls
    return x, given, flip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('n', nargs='*', type=int, default=[50000, 150000])
    ap.add_argument('--dim', type=int, default=768)
    ap.add_argument('--classes', type=int, default=20)
    ap.add_argument('--classifier', default='logreg')
    ap.add_argument('--fake-knn', action='store_true', help='random k-NN similarities instead of the exact k-NN '
                    '(times the check alone; the exact k-NN of 300k x 768 takes ~45 min on 4 CPU cores)')
    a = ap.parse_args()
    for n in a.n:
        x, y, flip = data(n, a.dim, a.classes)
        stages, last = {}, [time.perf_counter(), 'start']

        def progress(done=None, total=None, stage=None):
            if stage and stage.split(':')[-1].split('(')[0].strip() != last[1]:
                now = time.perf_counter()
                stages[last[1]] = stages.get(last[1], 0) + now - last[0]
                last[:] = [now, stage.split(':')[-1].split('(')[0].strip()]
        t0 = time.perf_counter()
        if a.fake_knn:
            nbr_sim = np.random.default_rng(1).uniform(0.5, 1, (n, 10)).astype(np.float32)
        else:
            _, nbr_sim = analysis.knn(x, k=10)          # the page's k-NN scores: computed for every feature anyway
        print(f'\n(k-NN of the page, not part of the check: {time.perf_counter() - t0:.1f} s)')
        last[0] = t0 = time.perf_counter()
        per, summary = lq.run(x, y, {'classifier': a.classifier}, progress, nbr_sim=nbr_sim)
        now = time.perf_counter()
        stages[last[1]] = stages.get(last[1], 0) + now - last[0]
        worst = set(np.argsort(np.nan_to_num(per['cl_quality'], nan=2))[:len(flip)])
        print(f'\nn={n:,} dim={a.dim} classes={a.classes} classifier={a.classifier}: {now - t0:.1f} s total, '
              f'held-out accuracy {summary["oof_accuracy"]:.3f}, flipped found {len(worst & set(flip)) / len(flip):.0%}')
        for k, v in stages.items():
            if v >= 0.05:
                print(f'  {v:7.1f} s  {k}')


if __name__ == '__main__':
    main()
