# `dataviz/ml/` — features and embedding analysis

Pure computation on arrays: turning crops into feature vectors, and feature vectors into 2D maps,
neighbour lists, quality scores and groups. No HTTP, no job queue, no dataset paths beyond the crop
files it is given — which is why `core/workers.py` can run these functions in another process.

## Files

| File | What it does |
|---|---|
| `features.py` | The **feature registry** (`build_registry`): deep models from `config.yaml features.models` (DINOv2, CLIP / OpenCLIP, SigLIP 2 via Hugging Face), your newest YOLO `best.pt` backbones, and fast CPU features (colour histogram, HOG, box geometry). Each `FeatureSpec` has `fn(crop_paths, samples, progress) → (N, D)` and a `metric`. Plus the `.npy` cache helpers. |
| `analysis.py` | `project()` (the 15 dimensionality-reduction methods below), `knn()`, `label_suspicion()`, `outlier_scores()`, `near_duplicates()`, `find_groups()` (HDBSCAN + bubble polygons). `METHODS` = the methods offered in the page. |

## Dimensionality reduction methods

`METHODS` in `analysis.py` lists every method; its `category` groups the page's **Method** dropdown
into Linear / Non-linear / Other, and `note` is the option's tooltip. All return `(N, 2)` float32.

### Linear

Each 2D axis is a weighted sum of the feature dimensions: distances along the map are honest, but
curved structure (clusters on a manifold) is flattened onto each other.

| Key | Method | Library | Notes |
|---|---|---|---|
| `pca` | PCA | sklearn / torch on GPU (≥ 20k points) | Two directions of largest variance; the page shows their explained variance. |
| `svd` | Truncated SVD | sklearn | PCA without centring (LSA): the first axis follows the mean direction. |
| `ica` | ICA (FastICA) | sklearn | Statistically independent axes instead of largest-variance ones. |
| `fa` | Factor Analysis | sklearn | PCA with a separate noise level per dimension; run on the first 50 PCs (full 512-d took 43 s for 6k points). |

### Non-linear (manifold learning)

Keep each point's neighbourhood rather than straight-line distances: clusters separate well, but
distances *between* clusters are only loosely meaningful (PaCMAP / Isomap keep them best).

| Key | Method | Library | Params | Notes |
|---|---|---|---|---|
| `umap` | UMAP | umap-learn / cuML | `n_neighbors`, `min_dist` | The default. |
| `pacmap` | PaCMAP | pacmap | `n_neighbors` | Better global structure than UMAP. |
| `localmap` | LocalMAP | pacmap | `n_neighbors` | PaCMAP with sharper cluster separation. |
| `tsne` | t-SNE | openTSNE / cuML / sklearn | `perplexity` | PCA to 50 dims first. |
| `kpca` | Kernel PCA (RBF) | sklearn | `gamma` (0 = 1 / dims) | Fitted on 3000 points, the rest `transform`ed. |
| `isomap` | Isomap | sklearn | `n_neighbors` | Geodesic distances on the k-NN graph; fitted on 3000, rest `transform`ed. |
| `lle` | Locally Linear Embedding | sklearn | `n_neighbors` | Each point stays the same mix of its neighbours; fitted on 3000, rest `transform`ed. |
| `spectral` | Spectral Embedding (Laplacian eigenmaps) | sklearn | `n_neighbors` | Fitted on 5000; no `transform`, so the rest are placed by their neighbours. |

### Other

Neither a fitted linear map nor neighbourhood-based manifold learning.

| Key | Method | Library | Notes |
|---|---|---|---|
| `mds` | Metric MDS (SMACOF) | sklearn | Keeps pairwise distances; O(N²), so fitted on 1500 points and the rest placed by their neighbours. |
| `random` | Gaussian random projection | sklearn | A random matrix not fitted to the data (linear, but data-independent): a baseline that shows how much structure the other methods add. |
| `trees` | Random trees embedding | sklearn | 100 totally random trees hash each point into its leaves; Truncated SVD of that sparse code gives 2D. |

Every sklearn method except PCA, SVD, ICA and random projection runs on the first 50 PCs. Methods that
cannot handle every point (`FIT_MAX`) are fitted on a fixed random subset (seed 0); points outside it are
`transform`ed when the estimator supports it, else placed at the inverse-distance mean of their 5 nearest
fitted points (`_place_by_neighbours`). Timings for 6000 points × 512 dims on CPU: under 5 s each.

To add one: an entry in `METHODS` (label, category, params, note) and a branch in `project()` or
`_sklearn_estimator()`; a new parameter also needs a hint in `PARAM_INFO` (`static/ts/views/controls.ts`)
and its type in `PARAM_TYPES` (`dataviz/state.py`).

## How the code is written

- Functions take arrays and return arrays (or `(xy, info)`); every long one accepts a
  `progress(done, total, stage)` callback with a no-op default.
- Heavy libraries (`torch`, `transformers`, `umap`, `pacmap`, `ultralytics`) are imported **inside**
  the function that needs them: the server starts in a second and a missing optional package only
  disables that one option.
- `metric='cosine'` means rows are L2-normalized, so cosine similarity = dot product and
  `1 − d²/2` everywhere.

## Optimizations & techniques

| Technique | Where | Why |
|---|---|---|
| fp16 on GPU, batched, `inference_mode` | `_batched_torch` | Batch size per model in config (64–128 fits a 35 GB MIG slice). |
| Crop loading in 8 threads overlapping the GPU | `_batched_torch` (`ThreadPoolExecutor(8)`) | The GPU never waits on JPEG decode. |
| Model freed after use | `del model; torch.cuda.empty_cache()` | Switching feature spaces doesn't accumulate VRAM. |
| Chunked exact k-NN | `knn()` (2048-row blocks, `argpartition`) | Exact neighbours with bounded memory: O(chunk × N) instead of an N × N matrix; `argpartition` then sorting only k items. |
| Real UMAP progress | `_epoch_reporter` | Replaces the `tqdm` UMAP wraps its epoch loop in, so the page shows epochs instead of a spinner. |
| numba locks | `_UMAP_LOCK`, `_PACMAP_LOCK` | numba's default threading layer crashes on concurrent use. |
| t-SNE thread cap | `TSNE_THREADS` (config `projections.tsne_threads`) | 8 threads: 18 s for 10k points; more was 20× slower on the shared machine. |
| Group repair | `_merge_touching`, `_attach_fringe` | HDBSCAN splits compact blobs and calls their thin edges "noise"; these passes merge touching groups and re-attach fringe points, so "outlier" really means isolated. |
| Rounded bubbles | `_bubble` | Hull of every hull vertex grown by a 12-point circle: smooth outlines from a cheap convex hull. |
| Content-keyed feature cache | `load_cached` / `save_cached` | `.npy` + `.json {fingerprint, n, dim}`; a cache file is used only if it matches the dataset exactly. |

## Gotchas

- Feature `key`s name the cache files: renaming a key in `config.yaml` recomputes that feature.
- UMAP runs unseeded (seeding forces numba single-threaded); PaCMAP / t-SNE / the sklearn methods use `random_state=0`. A recomputed
  UMAP view therefore looks different — which is why every projection is saved and listed under "Saved views".
