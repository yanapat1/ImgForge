# `dataviz/core/` — infrastructure

Generic building blocks with no knowledge of datasets or HTTP: settings, terminal logging, the
background job queue and the CPU worker processes.

## Files

| File | What it does |
|---|---|
| `config.py` | `DEFAULTS` (every key) merged with `config.yaml`; unknown keys are reported as likely typos and ignored, so an old or partial config keeps working. `resolve()` makes config paths relative to the project root. |
| `logging.py` | One `rich` console for the whole app. `get_logger(tag)` → coloured subsystem tag (`index`, `s3`, `sync`, `jobs`, `http`, …). `log_fn(tag)` adapts code that takes a `log=print` callable. `esc()` escapes rich markup in user data. `banner()` = the startup panel. Imported as `vlog`. |
| `jobs.py` | `JobManager`: two lanes (threads) — `main` for GPU / numba work (one at a time) and `io` for network work (S3 sync, dataset open). `Job.progress(done, total, stage, extra)` feeds both the live terminal bar and `/api/jobs`. |
| `workers.py` | Two persistent single-process pools (`heavy`: projections + k-NN scores; `fast`: density groups). Pure functions (`do_project`, `do_scores`, `do_groups`) run there; progress comes back over a queue. `InlinePools` = same interface in-process, for debugging. |

## How the code is written

- No imports from `data/`, `server/` or `state.py` (only `workers.py` imports `ml.analysis`, the code
  it runs). Anything here can be reused by another tool.
- Logging: always `vlog.get_logger('<tag>').info(...)`, and wrap user-controlled text (paths, error
  messages) in `vlog.esc()` — it may contain `[`, which rich would read as markup.
- `Job.progress()` is the one progress API everywhere (indexer, features, workers, S3 sync). Pass
  `stage=` to start a new phase (resets the bar), `total=None` for an indeterminate (pulsing) bar,
  `extra=` for a short rate text such as `12.4 MB/s`.

## Optimizations & techniques

| Technique | Where | Why |
|---|---|---|
| Spawned, warm worker processes | `workers.Pools` | numba (UMAP / PaCMAP / HDBSCAN) holds the GIL for seconds; in the server process every request stalled. Workers are started at launch and JIT-compile the startup method on fake data (`warmup`), so the first real projection is ~20 s faster. |
| Two pools | `heavy` / `fast` | Group bubbles must not wait behind a 60 s UMAP. |
| Progress over a queue | `workers._report` → `Pools.run` | Workers stream `(token, done, total, stage)`; the waiting job thread forwards only its own token's messages. |
| Crash recovery | `Pools._submit`, `BrokenProcessPool` | A worker killed by OOM is restarted; the job fails with a clear message instead of hanging. |
| Watchdog | `_worker_init` | Workers exit when the server dies (parent pid changes): no orphan processes holding GPU / RAM. |
| Thread cap in workers | `OMP_NUM_THREADS` | The machine reports 128 CPUs but is shared; more threads made t-SNE 20× slower. |
| Job lanes | `jobs.JobManager` (`main`, `io`) | A 1 GB S3 sync runs while UMAP computes; GPU jobs stay strictly sequential. |
| Deduplicated jobs | `JobManager.submit` | Same key while queued/running → the existing job is returned (double clicks, several tabs). |
| One live display | `jobs._progress` (shared rich `Progress`, ref-counted start/stop) | Log lines print *above* the bars instead of tearing them; the display only exists while a job runs. |
| Typo-tolerant config | `config._merge` | Missing keys fall back to defaults, unknown keys are warned about. |

## Gotchas

- `rich.Progress.update(total=None)` means "unchanged", so `Job.progress` resets the task total
  directly when a new stage is indeterminate.
- Do not log from inside a worker process with `vlog`; report progress with the `progress` callback
  (workers have no console of their own).
