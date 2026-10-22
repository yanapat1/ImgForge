"""
Embedding analysis: 2D projections (linear: PCA / SVD / ICA / FA; non-linear: UMAP / PaCMAP / t-SNE / Isomap / ...;
other: MDS / random projection / random trees), nearest neighbours,
data-quality scores and group (cluster) finding with outliers.

Most feature spaces are L2-normalized ("cosine" metric), so similarity = dot product.
"""

import os
import threading

import numpy as np

from ..core import hardware

# numba's default threading layer crashes the process on concurrent use, so UMAP runs one at a time.
_UMAP_LOCK = threading.Lock()
_PACMAP_LOCK = threading.Lock()   # PaCMAP / LocalMAP also use numba
# The box shows 128 CPUs but is shared: more t-SNE threads made it 20x slower (8 threads: 18 s for 10k points).
TSNE_THREADS = min(8, os.cpu_count() or 1)

# Grouped in the page's Method dropdown by `category` (in this order); `note` is the option's tooltip.
CATEGORIES = ('Linear', 'Non-linear', 'Other')

METHODS = {
    # linear: each 2D axis is a weighted sum of the feature dimensions
    'pca': {'label': 'PCA', 'category': 'Linear', 'params': {},
            'note': 'the two directions of largest variance'},
    'svd': {'label': 'Truncated SVD', 'category': 'Linear', 'params': {},
            'note': 'PCA without centring: the first axis follows the mean direction (LSA)'},
    'ica': {'label': 'ICA', 'category': 'Linear', 'params': {},
            'note': 'FastICA: statistically independent axes instead of largest-variance ones'},
    'fa': {'label': 'Factor Analysis', 'category': 'Linear', 'params': {},
           'note': 'PCA with a separate noise level per dimension (on the first 50 PCs)'},
    # non-linear (manifold learning): keeps neighbourhoods, not straight-line distances
    'umap': {'label': 'UMAP', 'category': 'Non-linear', 'params': {'n_neighbors': 15, 'min_dist': 0.1}},
    'pacmap': {'label': 'PaCMAP', 'category': 'Non-linear', 'params': {'n_neighbors': 10},
               'note': 'keeps global structure (how clusters relate) better than UMAP'},
    'localmap': {'label': 'LocalMAP', 'category': 'Non-linear', 'params': {'n_neighbors': 10},
                 'note': 'PaCMAP variant with sharper separation between clusters'},
    'tsne': {'label': 't-SNE', 'category': 'Non-linear', 'params': {'perplexity': 30}},
    'kpca': {'label': 'Kernel PCA', 'category': 'Non-linear', 'params': {'gamma': 0},
             'note': 'PCA in an RBF kernel space (fit on up to 3000 points, the rest projected)'},
    'isomap': {'label': 'Isomap', 'category': 'Non-linear', 'params': {'n_neighbors': 10},
               'note': 'keeps geodesic (along-the-neighbour-graph) distances (fit on up to 3000 points)'},
    'lle': {'label': 'LLE', 'category': 'Non-linear', 'params': {'n_neighbors': 10},
            'note': 'Locally Linear Embedding: each point stays a mix of its neighbours (fit on up to 3000 points)'},
    'spectral': {'label': 'Spectral Embedding', 'category': 'Non-linear', 'params': {'n_neighbors': 10},
                 'note': 'Laplacian eigenmaps of the k-NN graph (fit on up to 5000 points, the rest placed by '
                         'neighbours)'},
    # other: distance-preserving, data-independent or tree-based
    'mds': {'label': 'MDS', 'category': 'Other', 'params': {},
            'note': 'metric MDS: keeps pairwise distances (fit on up to 1500 points, the rest placed by neighbours)'},
    'random': {'label': 'Random Projection', 'category': 'Other', 'params': {},
               'note': 'Gaussian random matrix, not fitted to the data: a baseline for the other methods'},
    'trees': {'label': 'Random Trees', 'category': 'Other', 'params': {},
              'note': 'totally random trees hash each point to its leaves, then SVD to 2D'},
}


def _noop(*_, **__):
    pass


# ---------------------------------------------------------------- optional GPU helpers

GPU_MIN_KNN = 4000        # below this the numpy path is as fast (CUDA init + transfer dominate)
GPU_MIN_PCA = 20000       # sklearn's PCA on a few thousand rows is already quick


def _gpu_ok(min_n, n):
    """True when torch+CUDA is usable (hardware self-test passed) and the problem is big enough to benefit."""
    if n < min_n or os.environ.get('DATAVIZ_ANALYSIS_GPU', '1') == '0':
        return False
    try:
        return hardware.device() == 'cuda'
    except Exception:  # noqa: BLE001 - never let a hardware probe break an analysis
        return False


def _gpu_failed(exc):
    """A CUDA failure at run time: free what we can; non-OOM errors demote the whole process to CPU."""
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    if not hardware.is_oom(exc):
        hardware.demote_to_cpu()


def _cuml(module):
    """cuml submodule ('manifold', 'cluster') when RAPIDS is installed and a GPU is in use, else None.
    Disable with DATAVIZ_CUML=0."""
    if os.environ.get('DATAVIZ_CUML', '1') == '0':
        return None
    try:
        if hardware.device() != 'cuda':
            return None
        import importlib
        return importlib.import_module(f'cuml.{module}')
    except Exception:  # noqa: BLE001 - not installed / incompatible CUDA: use the CPU libraries
        return None


def _pca_gpu(x, n_components):
    """Exact PCA on the GPU via the eigendecomposition of the d x d covariance (float64).

    Returns (scores (N, k) float32, explained_variance_ratio (k,)). Components are the same as sklearn's up to
    sign; the sign convention matches sklearn's svd_flip (largest-|.| loading of each component is positive),
    so results agree with the CPU path except for (near-)degenerate eigenvalues.
    """
    import torch
    dev = 'cuda'
    xt = torch.as_tensor(np.ascontiguousarray(x, dtype=np.float32), device=dev).double()
    mean = xt.mean(0, keepdim=True)
    xt -= mean
    cov = (xt.T @ xt) / max(1, xt.shape[0] - 1)
    evals, evecs = torch.linalg.eigh(cov)
    k = min(n_components, evecs.shape[1])
    comps = evecs[:, -k:].flip(1)                             # (d, k), largest variance first
    vals = evals[-k:].flip(0).clamp_min(0)
    flip = torch.sign(comps[comps.abs().argmax(0), torch.arange(k, device=dev)])
    flip[flip == 0] = 1
    comps = comps * flip
    total = evals.clamp_min(0).sum()
    out = (xt @ comps).float().cpu().numpy()
    ratio = (vals / total.clamp_min(1e-30)).cpu().numpy()
    del xt, cov
    return out, ratio


def pca_reduce(x, n_components, seed=None):
    """PCA scores (N, n_components): GPU for large N, sklearn otherwise or on any GPU error.
    Returns (scores, explained_variance_ratio)."""
    n = len(x)
    if _gpu_ok(GPU_MIN_PCA, n) and x.shape[1] > n_components:
        try:
            return _pca_gpu(x, n_components)
        except Exception as e:  # noqa: BLE001 - OOM / driver error
            _gpu_failed(e)
    from sklearn.decomposition import PCA
    pca = PCA(n_components=n_components, random_state=seed)
    return pca.fit_transform(x), pca.explained_variance_ratio_


def configure(projections=None):
    """Apply the `projections` section of the config: default params per method and the t-SNE thread cap."""
    global TSNE_THREADS
    for method, params in (projections or {}).items():
        if method in METHODS and isinstance(params, dict):
            METHODS[method]['params'] = {**METHODS[method]['params'], **params}
    if projections and projections.get('tsne_threads'):
        TSNE_THREADS = max(1, min(int(projections['tsne_threads']), os.cpu_count() or 1))


def _pca_fallback(emb):
    centred = emb - emb.mean(0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    out = centred @ vt[:2].T
    return np.pad(out, ((0, 0), (0, 2 - out.shape[1]))).astype(np.float32)


def _epoch_reporter(progress, stage):
    """Stand-in for the tqdm that UMAP wraps its epoch loop in: reports every epoch to `progress`."""
    def tq(iterable, **_):
        total = len(iterable)
        progress(0, total, stage)
        for i, item in enumerate(iterable):
            yield item
            progress(i + 1, total)
    return tq


def run_umap(emb, n_neighbors=15, min_dist=0.1, seed=None, metric='cosine', progress=_noop):
    """Project embeddings to 2D with UMAP. Handles tiny inputs."""
    n = len(emb)
    if n == 0:
        return np.zeros((0, 2), np.float32)
    if n < 5:
        # UMAP needs a handful of points; fall back to a spread on the first 2 PCs.
        return _pca_fallback(emb)

    cu = _cuml('manifold') if n >= 10000 else None
    if cu is not None:                                  # RAPIDS UMAP (same seed semantics: random_state=seed)
        try:
            progress(stage='UMAP (cuML GPU)')
            return np.asarray(cu.UMAP(n_components=2, n_neighbors=max(2, min(int(n_neighbors), n - 1)),
                                      min_dist=float(min_dist), metric=metric, random_state=seed,
                                      init='spectral' if n > 50 else 'random').fit_transform(
                np.ascontiguousarray(emb, np.float32)), np.float32)
        except Exception:  # noqa: BLE001 - fall back to the CPU implementation
            pass
    import umap
    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=max(2, min(int(n_neighbors), n - 1)),
        min_dist=float(min_dist),
        metric=metric,
        random_state=seed,
        init='spectral' if n > 50 else 'random',
    )
    import umap.layouts
    with _UMAP_LOCK:
        progress(stage='UMAP: finding neighbours + spectral layout')
        original = umap.layouts.tqdm
        umap.layouts.tqdm = _epoch_reporter(progress, 'UMAP: optimizing layout (epochs)')
        try:
            return reducer.fit_transform(emb).astype(np.float32)
        finally:
            umap.layouts.tqdm = original


# ---------------------------------------------------------------- scikit-learn projections

# Points the O(n^2)+ methods are fitted on; the others are projected (transform) or placed by their neighbours.
FIT_MAX = {'kpca': 3000, 'isomap': 3000, 'lle': 3000, 'spectral': 5000, 'mds': 1500}


def _place_by_neighbours(x, fit_idx, fit_xy, k=5):
    """Out-of-sample positions for methods without transform(): inverse-distance mean of the k nearest fitted points."""
    from sklearn.neighbors import NearestNeighbors
    out = np.empty((len(x), 2), np.float32)
    nn = NearestNeighbors(n_neighbors=min(k, len(fit_idx))).fit(x[fit_idx])
    dist, idx = nn.kneighbors(x)
    w = 1.0 / np.maximum(dist, 1e-9)
    out[:] = (fit_xy[idx] * w[..., None]).sum(1) / w.sum(1, keepdims=True)
    out[fit_idx] = fit_xy
    return out


def _sklearn_estimator(method, params, n):
    """Unfitted estimator producing 2 components for one of the scikit-learn methods."""
    from sklearn import decomposition, manifold, random_projection
    nn = max(2, min(int(params.get('n_neighbors', 10)), n - 2))
    if method == 'svd':
        return decomposition.TruncatedSVD(n_components=2, random_state=0)
    if method == 'ica':
        return decomposition.FastICA(n_components=2, whiten='unit-variance', max_iter=400, random_state=0)
    if method == 'fa':
        return decomposition.FactorAnalysis(n_components=2, random_state=0)
    if method == 'kpca':
        return decomposition.KernelPCA(n_components=2, kernel='rbf', gamma=float(params.get('gamma') or 0) or None,
                                       eigen_solver='randomized', random_state=0)
    if method == 'isomap':
        return manifold.Isomap(n_components=2, n_neighbors=nn)
    if method == 'lle':
        return manifold.LocallyLinearEmbedding(n_components=2, n_neighbors=max(nn, 3), eigen_solver='dense',
                                               random_state=0)
    if method == 'spectral':
        return manifold.SpectralEmbedding(n_components=2, affinity='nearest_neighbors', n_neighbors=nn,
                                          random_state=0)
    if method == 'mds':
        return manifold.MDS(n_components=2, n_init=1, init='random', max_iter=300, random_state=0)
    if method == 'random':
        return random_projection.GaussianRandomProjection(n_components=2, random_state=0)
    raise ValueError(f'unknown projection method {method!r}')


def _project_sklearn(emb, method, params, progress):
    label = METHODS[method]['label']
    n = len(emb)
    x = np.asarray(emb, np.float32)
    if method not in ('pca', 'svd', 'ica', 'random') and x.shape[1] > 50 and n > 50:
        progress(stage=f'{label}: PCA to 50 dims')
        x = np.ascontiguousarray(pca_reduce(x, 50)[0], np.float32)
    if method == 'trees':
        from sklearn.decomposition import TruncatedSVD
        from sklearn.ensemble import RandomTreesEmbedding
        progress(stage=f'{label} on {n} points: hashing into leaves')
        trees = RandomTreesEmbedding(n_estimators=100, max_depth=5, random_state=0, n_jobs=TSNE_THREADS)
        leaves = trees.fit_transform(x)
        progress(stage=f'{label}: SVD of the leaf codes')
        return TruncatedSVD(n_components=2, random_state=0).fit_transform(leaves).astype(np.float32)
    est = _sklearn_estimator(method, params, min(n, FIT_MAX.get(method, n)))
    fit_max = FIT_MAX.get(method)
    if not fit_max or n <= fit_max:
        progress(stage=f'{label} on {n} points')
        return np.asarray(est.fit_transform(x), np.float32)
    fit_idx = np.sort(np.random.default_rng(0).choice(n, fit_max, replace=False))
    progress(stage=f'{label}: fitting {fit_max} of {n} points')
    fit_xy = np.asarray(est.fit_transform(x[fit_idx]), np.float32)
    if hasattr(est, 'transform'):
        progress(stage=f'{label}: projecting the other {n - fit_max} points')
        out = np.concatenate([est.transform(x[i:i + 10000]) for i in range(0, n, 10000)]).astype(np.float32)
        out[fit_idx] = fit_xy
        return out
    progress(stage=f'{label}: placing the other {n - fit_max} points by their neighbours')
    return _place_by_neighbours(x, fit_idx, fit_xy)


def project(emb, method='umap', params=None, metric='cosine', progress=_noop):
    """2D projection. Returns (xy, info) where info holds e.g. PCA explained variance."""
    params = {**METHODS[method]['params'], **(params or {})}
    n = len(emb)
    if n < 5:
        return (_pca_fallback(emb) if n else np.zeros((0, 2), np.float32)), {}
    if method == 'umap':
        progress(stage=f'UMAP on {n} points (n_neighbors={params["n_neighbors"]}, min_dist={params["min_dist"]})')
        return run_umap(emb, params['n_neighbors'], params['min_dist'], metric=metric, progress=progress), {}
    if method == 'pca':
        progress(stage=f'PCA on {n} points')
        xy, ratio = pca_reduce(emb, 2)
        return xy.astype(np.float32), {'explained_variance': [round(float(v), 4) for v in ratio]}
    if method in ('pacmap', 'localmap'):
        import pacmap
        nn = max(2, min(int(params['n_neighbors']), n - 2))
        label = METHODS[method]['label']
        progress(stage=f'{label} on {n} points (n_neighbors={nn})')
        x = np.ascontiguousarray(emb, dtype=np.float32)
        cls = pacmap.LocalMAP if method == 'localmap' else pacmap.PaCMAP
        reducer = cls(n_components=2, n_neighbors=nn, random_state=0, apply_pca=x.shape[1] > 100, verbose=True)
        total = int(sum(reducer.num_iters))
        import re
        import io
        import contextlib
        import pacmap.pacmap as pm

        def report(msg, verbose, **_):
            m = re.match(r'\s*Iteration:\s*(\d+)', str(msg))
            if m:
                progress(int(m.group(1)), total, f'{label}: optimizing layout (iterations)')
            elif 'nearest neighbor' in str(msg):
                progress(stage=f'{label}: neighbours found, building pairs')
        with _PACMAP_LOCK:
            progress(stage=f'{label}: finding nearest neighbours')
            original = pm.print_verbose
            pm.print_verbose = report
            try:
                with contextlib.redirect_stdout(io.StringIO()):   # its own 'Initial Loss' print
                    xy = reducer.fit_transform(x, init='pca')
            finally:
                pm.print_verbose = original
        return np.asarray(xy, np.float32), {}
    if method == 'tsne':
        x = emb
        if x.shape[1] > 50 and n > 50:
            progress(stage='t-SNE: PCA to 50 dims')
            x = pca_reduce(x, 50)[0]
        perplexity = float(min(params['perplexity'], max(2.0, (n - 1) / 3)))
        cu = _cuml('manifold') if n >= 10000 else None
        if cu is not None:
            try:
                progress(stage='t-SNE (cuML GPU)')
                xy = np.asarray(cu.TSNE(n_components=2, perplexity=perplexity, random_state=0).fit_transform(
                    np.ascontiguousarray(x, np.float32)))
                return xy.astype(np.float32), {}
            except Exception:  # noqa: BLE001 - fall back to openTSNE / sklearn
                pass
        try:
            from openTSNE import TSNE as OpenTSNE   # FFT-accelerated, several times faster than sklearn
            progress(stage=f't-SNE: finding neighbours (perplexity={perplexity:g})')
            est = OpenTSNE(n_components=2, perplexity=perplexity, initialization='pca', random_state=0,
                           n_jobs=TSNE_THREADS, neighbors='exact' if n < 1000 else 'auto')
            total = est.early_exaggeration_iter + est.n_iter
            done = [0]

            def tick(iteration, error, embedding):
                done[0] += 10
                progress(min(done[0], total), total, 't-SNE: optimizing layout (iterations)')
            est.callbacks, est.callbacks_every_iters = [tick], 10
            xy = np.asarray(est.fit(np.asarray(x, np.float64)))
        except ImportError:
            from sklearn.manifold import TSNE
            progress(stage=f't-SNE on {n} points (perplexity={perplexity:g}, ~1 min for 10k)')
            xy = TSNE(n_components=2, perplexity=perplexity, init='pca', random_state=0,
                      n_jobs=TSNE_THREADS).fit_transform(x)
        return xy.astype(np.float32), {}
    if method in METHODS:
        return _project_sklearn(emb, method, params, progress), {}
    raise ValueError(f'unknown projection method {method!r}')


def projection_quality(emb, xy, k=10, sample=2000):
    """How faithful a 2D map is, on a fixed random subset (trustworthiness is O(n^2)).

    trustworthiness: are 2D neighbours real neighbours (1 = no false neighbours appear in the map)?
    continuity: are real neighbours kept close in 2D (1 = none torn apart)?
    knn_preserved: mean share of each point's k nearest neighbours that are also its k nearest in 2D.
    """
    n = len(emb)
    k = min(k, (n - 1) // 2)
    if k < 1:
        return {}
    from sklearn.manifold import trustworthiness
    from sklearn.neighbors import NearestNeighbors
    idx = np.sort(np.random.default_rng(0).choice(n, sample, replace=False)) if n > sample else np.arange(n)
    x = np.asarray(emb[idx], np.float32)
    y = np.asarray(xy[idx], np.float32)
    hi = NearestNeighbors(n_neighbors=k + 1).fit(x).kneighbors(x, return_distance=False)[:, 1:]
    lo = NearestNeighbors(n_neighbors=k + 1).fit(y).kneighbors(y, return_distance=False)[:, 1:]
    kept = np.mean([len(np.intersect1d(a, b, assume_unique=True)) for a, b in zip(hi, lo)]) / k
    return {'trustworthiness': round(float(trustworthiness(x, y, n_neighbors=k)), 4),
            'continuity': round(float(trustworthiness(y, x, n_neighbors=k)), 4),
            'knn_preserved': round(float(kept), 4), 'k': int(k), 'n': int(len(idx))}


def _knn_gpu(emb, k, progress):
    """Exact k-NN on the GPU: chunked fp32 matmul + topk. Same formula as the numpy path (TF32 is not used), so
    neighbour sets are identical up to ties / last-ulp rounding. Chunk rows are sized from the free VRAM and
    halved on OOM."""
    import torch
    n = len(emb)
    dev = 'cuda'
    prev = torch.backends.cuda.matmul.allow_tf32
    torch.backends.cuda.matmul.allow_tf32 = False
    try:
        e = torch.as_tensor(np.ascontiguousarray(emb, dtype=np.float32), device=dev)
        sq = (e * e).sum(1)
        free = torch.cuda.mem_get_info()[0]
        # per row: s (n floats) + matmul temp + topk workspace ~ 3 x n x 4 bytes
        chunk = int(max(64, min(16384, free * (1 - hardware.HEADROOM) // (12 * n))))
        idx = np.zeros((n, k), np.int64)
        sim = np.zeros((n, k), np.float32)
        progress(0, n, 'k-NN (GPU): nearest neighbours of every box')
        start = 0
        while start < n:
            stop = min(n, start + chunk)
            try:
                s = 1.0 - (sq[start:stop, None] + sq[None, :] - 2.0 * (e[start:stop] @ e.T)) / 2.0
                rows = torch.arange(stop - start, device=dev)
                s[rows, start + rows] = -float('inf')
                v, i = torch.topk(s, k, dim=1)                # sorted, largest first
                idx[start:stop] = i.cpu().numpy()
                sim[start:stop] = v.cpu().numpy()
            except Exception as ex:  # noqa: BLE001
                if hardware.is_oom(ex) and chunk > 64:
                    s = v = i = None
                    torch.cuda.empty_cache()
                    chunk = max(64, chunk // 2)
                    continue
                raise
            start = stop
            progress(start, n)
        return idx, sim
    finally:
        torch.backends.cuda.matmul.allow_tf32 = prev


def knn(emb, k=10, chunk=2048, progress=_noop):
    """Exact k-NN (squared euclidean) excluding self. Returns (indices, similarities), each (N, k).

    similarity = 1 - d^2 / 2, which equals cosine similarity for L2-normalized rows.
    Runs on the GPU (torch) when one is usable and N is large, else (or on any GPU error) in numpy.
    """
    n = len(emb)
    k = max(0, min(k, n - 1))
    if k > 0 and _gpu_ok(GPU_MIN_KNN, n):
        try:
            return _knn_gpu(emb, k, progress)
        except Exception as e:  # noqa: BLE001 - fall back to the CPU path
            _gpu_failed(e)
    return _knn_numpy(emb, k, chunk, progress)


def _knn_numpy(emb, k, chunk=2048, progress=_noop):
    n = len(emb)
    idx = np.zeros((n, k), np.int64)
    sim = np.zeros((n, k), np.float32)
    if k == 0:
        return idx, sim
    sq = (emb * emb).sum(1)
    progress(0, n, 'k-NN: nearest neighbours of every box')
    for start in range(0, n, chunk):
        progress(start, n)
        a = emb[start:start + chunk]
        s = 1.0 - (sq[start:start + chunk, None] + sq[None, :] - 2.0 * (a @ emb.T)) / 2.0
        rows = np.arange(s.shape[0])
        s[rows, start + rows] = -np.inf
        part = np.argpartition(-s, k - 1, axis=1)[:, :k]
        part_sim = np.take_along_axis(s, part, axis=1)
        order = np.argsort(-part_sim, axis=1)
        idx[start:start + chunk] = np.take_along_axis(part, order, axis=1)
        sim[start:start + chunk] = np.take_along_axis(part_sim, order, axis=1)
    progress(n, n)
    return idx, sim


def label_suspicion(labels, nbr_idx):
    """Per sample: (fraction of neighbours with a different class, majority neighbour class)."""
    labels = np.asarray(labels)
    if nbr_idx.shape[1] == 0:
        return np.zeros(len(labels), np.float32), labels.copy()
    nbr_labels = labels[nbr_idx]
    disagree = (nbr_labels != labels[:, None]).mean(1).astype(np.float32)
    n_cls = int(labels.max()) + 1
    counts = np.zeros((len(labels), n_cls), np.int32)
    np.add.at(counts, (np.repeat(np.arange(len(labels)), nbr_idx.shape[1]), nbr_labels.ravel()), 1)
    return disagree, counts.argmax(1)


def outlier_scores(emb, labels, metric='cosine'):
    """Distance of each sample to its own class centre (cosine distance, or z-scored euclidean)."""
    labels = np.asarray(labels)
    scores = np.zeros(len(labels), np.float32)
    for c in np.unique(labels):
        mask = labels == c
        centroid = emb[mask].mean(0)
        if metric == 'cosine':
            centroid /= max(np.linalg.norm(centroid), 1e-12)
            scores[mask] = 1.0 - emb[mask] @ centroid
        else:
            scores[mask] = np.linalg.norm(emb[mask] - centroid, axis=1)
    return scores


def near_duplicates(nbr_idx, nbr_sim, threshold=0.97, limit=500, exclude=None):
    """Unique (i, j, sim) pairs with similarity >= threshold, most similar first. exclude: bool per point (pairs
    touching one are dropped before the limit)."""
    seen, pairs = set(), []
    rows, cols = np.nonzero(nbr_sim >= threshold)
    if exclude is not None and len(rows):
        keep = ~exclude[rows] & ~exclude[nbr_idx[rows, cols]]
        rows, cols = rows[keep], cols[keep]
    for r, c in zip(rows, cols):
        j = int(nbr_idx[r, c])
        key = (min(int(r), j), max(int(r), j))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((key[0], key[1], float(nbr_sim[r, c])))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:limit]


# ---------------------------------------------------------------- groups (bubbles)

def _bubble(pts, pad, n_circle=12):
    """Closed, rounded polygon around pts: the convex hull of every hull vertex grown by a circle of
    radius `pad` (a rounded offset of the hull). Smooth without spline drawing, and always encloses pts."""
    from scipy.spatial import ConvexHull
    a = np.linspace(0, 2 * np.pi, n_circle, endpoint=False)
    ring = pad * np.stack([np.cos(a), np.sin(a)], 1)
    core = pts
    if len(pts) >= 3:
        try:
            core = pts[ConvexHull(pts).vertices]
        except Exception:  # noqa: BLE001 - collinear / duplicate points
            core = pts
    grown = (core[:, None, :] + ring[None, :, :]).reshape(-1, 2)
    poly = grown[ConvexHull(grown).vertices]
    return np.vstack([poly, poly[:1]])


def _spacing(pts, lab, k):
    """Typical neighbour spacing inside groups: 2 x the 90th percentile of the k-th neighbour distance."""
    from sklearn.neighbors import NearestNeighbors
    member = lab >= 0
    kd = NearestNeighbors(n_neighbors=k + 1).fit(pts[member]).kneighbors(pts[member])[0][:, -1]
    return 2.0 * float(np.percentile(kd, 90))


def _merge_touching(pts, lab, eps):
    """HDBSCAN can split one compact blob into sub-groups. Merge groups whose closest points are within
    the normal neighbour spacing; clusters that are really apart stay separate."""
    from scipy.spatial import cKDTree
    ids = sorted(set(lab.tolist()) - {-1})
    if len(ids) < 2:
        return lab
    parent = {i: i for i in ids}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    members = {i: pts[lab == i] for i in ids}
    trees = {i: cKDTree(members[i]) for i in ids}
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            if find(a) != find(b) and trees[a].query(members[b], k=1, distance_upper_bound=eps)[0].min() <= eps:
                parent[find(b)] = find(a)
    out = lab.copy()
    for i in ids:
        out[lab == i] = find(i)
    return out


def _attach_fringe(pts, lab, k, rounds=3, eps=None):
    """HDBSCAN calls the thin edge of a blob 'noise'. Re-attach a noise point to the nearest group
    when it is within that group's usual neighbour spacing, so only truly isolated points stay outliers."""
    from sklearn.neighbors import NearestNeighbors
    lab = lab.copy()
    if (lab >= 0).sum() <= k or (lab < 0).sum() == 0:
        return lab
    if eps is None:
        eps = _spacing(pts, lab, k)
    for _ in range(rounds):
        noise, member = np.nonzero(lab < 0)[0], np.nonzero(lab >= 0)[0]
        if not len(noise):
            break
        d, j = NearestNeighbors(n_neighbors=1).fit(pts[member]).kneighbors(pts[noise])
        close = d[:, 0] <= eps
        if not close.any():
            break
        lab[noise[close]] = lab[member[j[close, 0]]]
    return lab


class _CuGuard:
    """fit_predict on the cuML estimator, falling back to the sklearn one on any error."""

    def __init__(self, gpu, cpu):
        self.gpu, self.cpu = gpu, cpu

    def fit_predict(self, x):
        try:
            return np.asarray(self.gpu.fit_predict(np.ascontiguousarray(x, np.float32)))
        except Exception:  # noqa: BLE001
            return self.cpu.fit_predict(x)


def find_groups(xy, labels, min_cluster_size=15, per_class=True, pad_frac=0.015):
    """Cluster 2D points with HDBSCAN. Outliers get group -1.

    per_class=True clusters each class separately, so a group is always one class and a
    point that does not fit any group of its own class is an outlier.
    Returns (group ids per point, list of group dicts with a bubble polygon).
    """
    from sklearn.cluster import HDBSCAN as SkHDBSCAN
    xy = np.asarray(xy, np.float64)

    def HDBSCAN(**kw):                      # cuML only for big parts (GPU launch cost dominates small ones)
        cu = _cuml('cluster') if len(pts) >= 50000 else None
        if cu is not None:
            kw.pop('copy', None)
            return _CuGuard(cu.HDBSCAN(**kw), SkHDBSCAN(**kw, copy=True))
        return SkHDBSCAN(**kw)
    labels = np.asarray(labels)
    n = len(xy)
    group = np.full(n, -1, np.int64)
    if n == 0:
        return group, []
    mcs = max(2, int(min_cluster_size))
    extent = float(max(np.ptp(xy[:, 0]), np.ptp(xy[:, 1]), 1e-9))
    pad = pad_frac * extent

    parts = [(c, np.nonzero(labels == c)[0]) for c in np.unique(labels)] if per_class else [(-1, np.arange(n))]
    next_id = 0
    for c, idx in parts:
        if len(idx) < mcs:
            continue
        pts = xy[idx]
        ms = min(mcs, 5)
        lab = HDBSCAN(min_cluster_size=mcs, min_samples=ms, copy=True).fit_predict(pts)
        if lab.max() < 0:
            # one compact blob: plain HDBSCAN finds no split, so allow a single group. Using this always
            # merged far-apart clusters of a class into one huge bubble.
            lab = HDBSCAN(min_cluster_size=mcs, min_samples=ms, allow_single_cluster=True, copy=True).fit_predict(pts)
        if (lab >= 0).sum() > ms:
            eps = _spacing(pts, lab, ms)
            lab = _attach_fringe(pts, lab, ms, eps=eps)
            lab = _merge_touching(pts, lab, eps)
            # renumber 0..n-1
            uniq = sorted(set(lab.tolist()) - {-1})
            remap = {u: i for i, u in enumerate(uniq)}
            lab = np.array([remap.get(v, -1) for v in lab.tolist()], dtype=np.int64)
        for g in sorted(set(lab) - {-1}):
            group[idx[lab == g]] = next_id + g
        next_id += max(lab.max() + 1, 0)

    groups = []
    for g in range(next_id):
        members = np.nonzero(group == g)[0]
        if not len(members):
            continue
        cls_counts = np.bincount(labels[members])
        poly = _bubble(xy[members], pad)
        groups.append({
            'id': g, 'n': int(len(members)), 'cls': int(cls_counts.argmax()),
            'pure': bool(cls_counts.max() == len(members)),
            'x': [round(float(v), 4) for v in poly[:, 0]], 'y': [round(float(v), 4) for v in poly[:, 1]],
        })
    return group, groups
