# `dataviz/server/` — the web server

FastAPI app that serves the page (`static/`) and the JSON API the page uses.

## Files

| File | What it does |
|---|---|
| `app.py` | `Ctx` (what is served now: `state`, feature registry, dataset registry, config, `open_dataset`), `_StateProxy`, `create_app(ctx)`: access-log middleware, the `HTTPException` handler, the route modules, and the `/static` mount. |
| `deps.py` | `guards(state)` → `need_index` (503 while indexing), `sample_or_404`, `feature_or_400`, `scores_or_409` (409 = "compute it first"); `json_bytes`, `_r`. |
| `models.py` | pydantic request bodies (`ProjectionRequest`, `GroupsRequest`, `IdsRequest`, `DistancesRequest`, `TagRequest`). |
| `static.py` | `JsonGZip` (gzip for JSON / text, never for JPEGs) and `NoCacheStatic` (`Cache-Control: no-cache`). |
| [`routes/`](routes/README.md) | One module per area of the API. |

## How the code is written

- Each route module exports `router(state, ctx) -> APIRouter`. `state` is the **proxy**: routes
  write `state.samples` and always get the dataset being served now.
- Handlers are plain `def` (FastAPI runs them in its thread pool) unless they `await` something;
  network / disk work in `async` handlers goes through `run_io()` (the default executor), so the event
  loop never blocks.
- Errors are `HTTPException` with a human message — the page shows the `detail` text as-is.

## Optimizations & techniques

| Technique | Where | Why |
|---|---|---|
| Hot swap | `_StateProxy.__getattr__` → `ctx.state` | Opening another dataset needs no restart; in-flight requests finish on the old state. |
| Bytes, not dicts | `json_bytes(state.payload(...))` | Large payloads (points, scores, projections) are serialized once with orjson and reused. |
| Selective gzip | `JsonGZip` | Number-heavy JSON compresses well (a big win over a VS Code port forward); JPEG crops are already compressed, gzipping them only costs CPU. |
| Revalidate, don't expire | `NoCacheStatic` | Browsers keep the files but check the ETag: edited page code is never stale, unchanged files cost a 304. |
| Quiet access log | `QUIET_PATHS`, `access_log` | Thumbnails / polling log at DEBUG; slow (> 1 s) requests are WARN, 4xx WARN, 5xx ERROR with traceback. |
| Expected-status silence | `http_error` | 409 / 503 are the normal "not computed / not ready yet" handshake; not logged as problems. |
