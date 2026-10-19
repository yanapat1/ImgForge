# `dataviz/server/` — the web server

FastAPI app that serves the page (`static/`) and the JSON API the page uses.

## Files

| File | What it does |
|---|---|
| `app.py` | `Ctx` (the loaded sessions `states` + shared services: feature registry, dataset registry, config, models, jobs, `open_dataset`), `_StateProxy`, `create_app(ctx, auth=True)`: sign-in layer, `/s/<id>/` routing, access-log middleware, the `HTTPException` handler, the route modules, and the `/static` mount. |
| `routing.py` | `SessionRouting` (pure ASGI, just inside the sign-in layer): `/s/<id>/…` → access check (404 when none), the State loaded once, `context.BOUND`, prefix stripped; `/s/<id>` → 308 `<id>/`. `FixedSession`: the test-only single-State mode. |
| `deps.py` | `guards(state)` → `need_index` (503 while indexing), `sample_or_404`, `feature_or_400`, `scores_or_409` (409 = "compute it first"); `owned(method, …)` (per-user model registry calls); `json_bytes`, `_r`. |
| `models.py` | pydantic request bodies (`ProjectionRequest`, `GroupsRequest`, `IdsRequest`, `DistancesRequest`, `TagRequest`). |
| `static.py` | `JsonGZip` (gzip for JSON / text, never for JPEGs), `NoCacheStatic` (`Cache-Control: no-cache`) and `render_index(static_dir, prefix='')` (the page shell, every asset URL hash-stamped; `prefix='../../'` under `/s/<id>/`). |
| [`routes/`](routes/README.md) | One module per area of the API. |

## Serving many sessions

| URL | What |
|---|---|
| `/` | the home page (session cards); `/?session=<id>` (old links) → 302 `s/<id>/` |
| `/s/<id>/` | the visualizer of session `<id>`; its relative `api/…` calls hit `/s/<id>/api/…` |
| `/api/…` outside `/s/` | account, home, jobs, models, labeling: not bound to a visualizer session |

- **Middleware, outermost first**: `AuthMiddleware` (cookie → `context.USER`, CSRF, sign-in wall) →
  `SessionRouting` → access log → gzip → routes. The access log prints the original path (with `/s/<id>`).
- **`Ctx.states`**: an LRU `session id → State`, shared by every user of the session, at most
  `sessions.max_loaded` (default 4). `state_for(sid, user)` opens one on first use under a per-session
  lock (concurrent first requests build one State); eviction skips States with a queued / running job
  (`JobManager.busy(tag)`), and `close_state` frees the S3 thread pool, `ModelService._splits[tag]` and
  every `ctx.close_hooks` cache. `evict(sid)` after a delete / re-index.
- **`create_app(State)` / `Ctx(state)`** is test-only: no sign-in, every request bound to that one State.
- **Guards**: visualizer routers carry `Depends(require_bound('viewer'))`; box tags, curation fix / undo /
  ignore, `/api/edits` and model predict need `labeler`; the rest of `/api/*` at least `current_user`.
  `tests/test_acl.py::test_every_route_guarded` fails on an `/api` route without one.
- **Per user**: `/api/settings` = `app.user_session_prefs` (UI + filter); shared by the session: the curation
  ignore list (`app.dataset_state`) and box tags (`app.box_tags`, per dataset fingerprint).
- **Jobs** record `owner` + `session_id` (defaults: the request's user and bound session); `/api/jobs`
  lists only mine and those of sessions I can open; cancel = the job's owner, the session's owner or an admin.

## How the code is written

- Each route module exports `router(state, ctx) -> APIRouter`. `state` is the **proxy**: routes
  write `state.samples` and always get the State of the request's `/s/<id>/` session (409 outside one).
- Handlers are plain `def` (FastAPI runs them in its thread pool) unless they `await` something;
  network / disk work in `async` handlers goes through `run_io()` = `asyncio.to_thread` (it carries the
  contextvars; `loop.run_in_executor` would drop the user and the bound session), so the event loop
  never blocks.
- Errors are `HTTPException` with a human message — the page shows the `detail` text as-is.

## Optimizations & techniques

| Technique | Where | Why |
|---|---|---|
| One State per session, resolved once | `SessionRouting` → `context.BOUND`, `_StateProxy` | Each request sees its own session for its whole length; an evicted State finishes in-flight requests and jobs. |
| One browser cache for every session | `render_index(prefix='../../')` | Assets are fetched from `/static/` whatever the session, not re-downloaded per `/s/<id>/`. |
| Bytes, not dicts | `json_bytes(state.payload(...))` | Large payloads (points, scores, projections) are serialized once with orjson and reused. |
| Selective gzip | `JsonGZip` | Number-heavy JSON compresses well (a big win over a VS Code port forward); JPEG crops are already compressed, gzipping them only costs CPU. |
| Revalidate, don't expire | `NoCacheStatic` | Browsers keep the files but check the ETag: edited page code is never stale, unchanged files cost a 304. |
| Quiet access log | `QUIET_PATHS`, `access_log` | Thumbnails / polling log at DEBUG; slow (> 1 s) requests are WARN, 4xx WARN, 5xx ERROR with traceback. |
| Expected-status silence | `http_error` | 409 / 503 are the normal "not computed / not ready yet" handshake; not logged as problems. |
