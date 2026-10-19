# `dataviz/ml/` — features and embedding analysis

Pure computation on arrays: turning crops into feature vectors, and feature vectors into 2D maps,
neighbour lists, quality scores and groups. No HTTP, no job queue, no dataset paths beyond the crop
files it is given — which is why `core/workers.py` can run these functions in another process.

## Files

| File | What it does |
|---|---|
| `features.py` | The **feature registry** (`build_registry`): deep models from `config.yaml features.models` (DINOv2, CLIP / OpenCLIP, SigLIP 2 via Hugging Face), your newest YOLO `best.pt` backbones, and fast CPU features (colour histogram, HOG, box geometry). Each `FeatureSpec` has `fn(crop_paths, samples, progress) → (N, D)` and a `metric`. Plus the `.npy` cache helpers. |
| `analysis.py` | `project()` (UMAP / PaCMAP / LocalMAP / t-SNE / PCA), `knn()`, `label_suspicion()`, `outlier_scores()`, `near_duplicates()`, `find_groups()` (HDBSCAN + bubble polygons). `METHODS` = the methods offered in the page. |

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
- UMAP runs unseeded (seeding forces numba single-threaded); PaCMAP / t-SNE use `random_state=0`. A recomputed
  UMAP view therefore looks different — which is why every projection is saved and listed under "Saved views".
