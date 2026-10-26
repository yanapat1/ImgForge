# `dataviz/` — the server package

Everything that runs in Python: reading the config, indexing a dataset, computing features and
projections, and serving the page + JSON API. The `app.py` launcher one folder up puts its own folder on
`sys.path` and calls `dataviz.cli.main()`; `python -m dataviz` does the same.

## Files

| File | What it does |
|---|---|
| `cli.py` | Command line → config (flags win over `config.yaml`) → bind the port **before** touching any workspace → PostgreSQL (`start_database`: `db.init`, migrate, `secret.key`, default settings, the first admin — its generated password shown once in the banner — and the one-time legacy import) → worker pools → `--dataset` (saved as a session of the first admin and loaded) → banner → uvicorn. Also defines `open_dataset()`, the State builder `Ctx` loads sessions with. |
| `state.py` | `State`: one loaded visualizer session. Index, feature matrices (also kept in pgvector), k-NN scores, projections and groups, each with an in-memory LRU and a copy in the workspace folder. `request_*` methods return the cached value or queue a job. UI settings / filter per user, the curation ignore list per session and box tags per dataset version are database rows (JSON files without a database). `close()` frees it when unloaded. |
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
- **State lives in exactly one object per loaded session** (`State`; several are loaded at once, see
  `server/README.md`). Routes never cache dataset data themselves; anything a route remembers (e.g. the
  full-image LRU) is keyed by `state.tag`, so one session can never serve another's bytes, and is
  bounded / dropped on unload (`Ctx.close_hooks`).
- **Slow work is a job**: `state.submit(key, label, fn)` returns at once; the page polls
  `/api/jobs/{id}`. Job keys include `state.tag` so a new dataset never attaches to an old job; the job
  records who started it and its session (users only see / cancel jobs of sessions they can open).
- **Disk caches are content-addressed**: workspace folder = dataset fingerprint; feature / score
  files carry the fingerprint and `n`; projection files are `sha1(key|fingerprint)`. Changing the
  dataset changes the key — nothing is ever invalidated by hand.
- Atomic writes for anything another thread may read (`write tmp → replace`).

## Optimizations & techniques

| Technique | Where | Why |
|---|---|---|
| Lazy workspace resolution | `State._index_job` | For S3 the fingerprint needs a bucket listing (and in cache mode a sync); doing it in the job keeps startup instant and shows progress. |
| Many sessions at once | `server/app.py` `Ctx.states` + `_StateProxy` | An LRU of loaded States (`sessions.max_loaded`); `/s/<id>/` binds each request to its own, routes read it through the proxy. |
| Feature vectors in PostgreSQL | `ml/features.py` `vectors_*` | Every computed matrix is also saved to pgvector; a missing `.npy` is rebuilt from it instead of re-running the GPU model; old `.npy` files are backfilled in the background. |
| Three-level cache per result | `State.get_projection` / `get_scores` / `get_features` | memory LRU (48 projections) → workspace `.npz/.npy` → compute as a job. |
| Pre-serialized JSON | `State.payload()` | Big arrays are `orjson`-dumped once (numpy-native) and served as bytes from a 64-entry LRU; repeat requests cost a dict lookup. |
| Per-key locks | `State._feature_locks` | Two requests for the same feature space compute it once; different spaces don't block each other. |
| Heavy CPU off the server process | `core/workers.py` | UMAP / HDBSCAN hold the GIL for seconds; in worker processes they never stall a thumbnail request. |
| Port bound before workspace touched | `cli.py` | A second instance fails fast instead of migrating a workspace the first one is using. |

## Gotchas

- contextvars (the user, the bound session) follow a request into FastAPI's thread pool and
  `asyncio.to_thread`, **not** into `loop.run_in_executor` or job threads: resolve what a job needs in the
  handler.

- `State.cache_dir` is `None` until the index job has run (S3 datasets). Code that needs it after
  startup should go through `need_index()` / `state.ready`.
- Worker processes are **spawned**: they import `dataviz.core.workers` by name. `multiprocessing`
  passes the parent's `sys.path`, so it works from the launcher and from `python -m dataviz`; it would
  not work if the package were imported from elsewhere without the app folder on the path.
