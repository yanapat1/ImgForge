# `dataviz/` — the server package

Everything that runs in Python: reading the config, indexing a dataset, computing features and
projections, and serving the page + JSON API. The `app.py` launcher one folder up puts
`scripts/image-tools/` on `sys.path` and calls `dataviz.cli.main()`; `python -m dataviz` does the same.

## Files

| File | What it does |
|---|---|
| `cli.py` | Command line → config (flags win over `config.yaml`) → bind the port **before** touching any workspace → worker pools → pick the start dataset (`--dataset`, else the one open last time, else config's) → banner → uvicorn. Also defines `open_dataset()`, which the Settings "Open" button calls through `Ctx`. |
| `state.py` | `State`: the dataset being served. Index, feature matrices, k-NN scores, projections and groups, each with an in-memory LRU and a copy in the workspace folder. `request_*` methods return the cached value or queue a job. |
| `workspace.py` | `resolve_workspace()` names the cache folder `<dataset>__<fingerprint[:8]>` (and migrates the old un-suffixed folder); `list_workspaces()` lists other versions of the same dataset. |
| `paths.py` | `PKG_DIR`, `APP_DIR`, `PROJECT_ROOT`, `STATIC_DIR`, `DEFAULT_CONFIG`, `DATA_PREP_DIR`. The only file that knows where the package sits. |
| `__main__.py` | `python -m dataviz`. |

| Sub-package | |
|---|---|
| [`core/`](core/README.md) | config, logging, job queue, worker processes — no dataset knowledge |
| [`data/`](data/README.md) | where datasets live (local / S3), saved datasets & connections, the indexer |
| [`ml/`](ml/README.md) | feature extractors, projections, k-NN scores, density groups — pure numpy/torch |
| [`models/`](models/README.md) | saved detectors (files or HTTP APIs), running them over a dataset, scoring them against its labels |
| [`server/`](server/README.md) | FastAPI app, middleware, route modules |

Dependency direction: `server → (state, models) → (data, ml, core)`, `data → core`, `core.workers → ml`.
Nothing imports `server` or `cli` back.

## How the code is written

- **Relative imports inside the package** (`from ..core import logging as vlog`). No `sys.path` hacks
  except the one in the launcher. `vlog` is the import alias for `core/logging.py`, so it never
  shadows the standard library `logging`.
- **State lives in exactly one object** (`State`). Routes never cache dataset data themselves;
  anything a route remembers (e.g. the full-image LRU) is keyed by `state.tag`, so a dataset switch
  can never serve the previous dataset's bytes.
- **Slow work is a job**: `state.jobs.submit(key, label, fn)` returns at once; the page polls
  `/api/jobs/{id}`. Job keys include `state.tag` so a new dataset never attaches to an old job.
- **Disk caches are content-addressed**: workspace folder = dataset fingerprint; feature / score
  files carry the fingerprint and `n`; projection files are `sha1(key|fingerprint)`. Changing the
  dataset changes the key — nothing is ever invalidated by hand.
- Atomic writes for anything another thread may read (`write tmp → replace`).

## Optimizations & techniques

| Technique | Where | Why |
|---|---|---|
| Lazy workspace resolution | `State._index_job` | For S3 the fingerprint needs a bucket listing (and in cache mode a sync); doing it in the job keeps startup instant and shows progress. |
| Hot-swappable state | `server/app.py` `Ctx` + `_StateProxy` | "Open dataset" replaces `ctx.state`; live routes read through the proxy, no restart. |
| Three-level cache per result | `State.get_projection` / `get_scores` / `get_features` | memory LRU (48 projections) → workspace `.npz/.npy` → compute as a job. |
| Pre-serialized JSON | `State.payload()` | Big arrays are `orjson`-dumped once (numpy-native) and served as bytes from a 64-entry LRU; repeat requests cost a dict lookup. |
| Per-key locks | `State._feature_locks` | Two requests for the same feature space compute it once; different spaces don't block each other. |
| Heavy CPU off the server process | `core/workers.py` | UMAP / HDBSCAN hold the GIL for seconds; in worker processes they never stall a thumbnail request. |
| Port bound before workspace touched | `cli.py` | A second instance fails fast instead of migrating a workspace the first one is using. |

## Gotchas

- `State.cache_dir` is `None` until the index job has run (S3 datasets). Code that needs it after
  startup should go through `need_index()` / `state.ready`.
- Worker processes are **spawned**: they import `dataviz.core.workers` by name. `multiprocessing`
  passes the parent's `sys.path`, so it works from the launcher and from `python -m dataviz`; it would
  not work if the package were imported from elsewhere without `scripts/image-tools` on the path.
