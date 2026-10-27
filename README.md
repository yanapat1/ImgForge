# Dataset Visualizer

A FiftyOne-style explorer for YOLO datasets. Every bounding box becomes a dot in a 2D projection
(15 methods, grouped Linear: PCA / SVD / ICA / FA · Non-linear: UMAP / PaCMAP / t-SNE / Isomap / LLE / … ·
Other: MDS / random projection / random trees — see [`dataviz/ml/README.md`](dataviz/ml/README.md)) of a feature space you pick (OpenCLIP, SigLIP 2, DINOv2, your own YOLO
weights, colour histogram, HOG, box geometry — or any model you add in Settings › Models with "Use in the plot": your
detector's backbone, a hooked layer, an ONNX output or a Hugging Face backbone). **Find issues** runs cleanlab on k-fold
out-of-fold predicted probabilities (label issues, label quality, out-of-distribution, class confusion, label health). Lasso dots (or describe them in words, for CLIP / SigLIP features) to see their crops, find suspicious labels,
outliers and near-duplicates, measure distances between boxes, and switch between datasets on local
disk or in S3 / RustFS buckets.

Not only images: an **embeddings session** plots vectors you computed anywhere (audio, video, text, sensors,
tables) from a folder of `.npy` files plus a `meta.csv`. Rows show as playable audio cards, videos that play on
hover, text or image thumbnails, and any metadata column colours the plot. Format:
[`dataviz/data/README.md`](dataviz/data/README.md#embeddings-folders-any-modality); try it with
`python examples/make_embeddings_demo.py` and *New visualize session › Embeddings folder*.

```bash
npm install && npm run build                                    # compile static/ts -> static/js (once, and after TS edits; `npm run watch` while developing)
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && set -a && . ./.env && set +a      # POSTGRES_PASSWORD, DATAVIZ_SECRET_KEY, admin
docker compose up -d postgres                # PostgreSQL + pgvector on 127.0.0.1:15432 (POSTGRES_PASSWORD from .env)
export DATABASE_URL=postgresql://dataviz:$POSTGRES_PASSWORD@127.0.0.1:15432/dataviz   # or your own server
.venv/bin/python app.py                      # config.yaml in this folder
.venv/bin/python app.py --dataset dataset --port 5151
.venv/bin/python app.py --list-features      # feature keys
.venv/bin/python app.py --show-config        # effective settings
.venv/bin/python -m dataviz                  # same thing, as a package
```

Settings live in [`config.yaml`](config.yaml) (command-line flags win). Relative paths in it (`storage.allowed_roots`
`'.'`, `dataset.workspace_root`) are relative to the **project root**: this folder when cloned on its own, or two
levels up when it sits in a bigger project as `scripts/image-tools/` (the Docker image); `DATAVIZ_PROJECT_ROOT`
sets it explicitly.

## Accounts, sessions and sharing

Everyone signs in (username or email + password; `/login`). Sign-up is open by default; admins can turn it off
in **Settings › Users**, where they also create accounts, reset passwords, make other people admin, deactivate or
delete users (handing their sessions to someone else) and read the audit log. The first admin is created on the
first start from `DATAVIZ_ADMIN_USERNAME / _EMAIL / _PASSWORD` (an empty password is generated and printed once);
with `DATAVIZ_ADMIN_RESET=true` every start makes that account an active admin with that password again.
Passwords need at least 8 characters.

**Tiers** decide what a non-admin may add to the server. Admins create, rename and edit them in **Settings › Users**
(a checkbox per permission), move users between them, and pick the default tier for sign-ups and users without one:

| permission | lets the user |
|---|---|
| `dataset.create` | add datasets to visualize (local folder, S3, labeling task, embeddings) and browse the server's folders |
| `label.create` | create / duplicate labeling sessions |
| `connection.add` | add S3 / FTP / database / model-server / LLM connections |
| `model.add` | add Hugging Face / API models |
| `model.upload` | upload or point at model files on the server (weights, adapters: loading them runs code) |
| `llm.use` | use the AI assistant |

Built in: **Viewer** (nothing: opens what is shared with them), **Member** (default: everything but `model.upload`),
**Contributor** (everything: adds data and models to label and visualize with). Admins have every permission;
sharing roles (Viewer / Labeler below) still decide what someone may do on another person's session.

The page opens on **Sessions**: one card per session (a dataset to **visualize** — a local folder, an S3 / RustFS
prefix or a labeling task — or a **label** session). Every session has an owner and is **private** (only the owner
and the people they add) or **global** (every user can view it). The owner adds members as **Viewer** (look only)
or **Labeler** (also label, fix boxes, run curation fixes). Admins can open every session. Cards show the owner's
avatar and name and the members' avatars; the filter above them shows All · Mine · Shared with me · Global
(· Everyone's for admins). A visualizer session opens at `s/<id>/`; several sessions are served at once (the
newest `sessions.max_loaded` stay loaded, shared by everyone viewing them).

Connections (S3, FTP / SFTP, databases, model servers), LLM providers + keys, models and every preference belong
to the account that made them: nobody else sees them, and a shared session reads its data with its owner's
connection without revealing it. Secrets are encrypted in the database (`DATAVIZ_SECRET_KEY`). Models that load
files on the server (weights, adapters) can only be added by admins and tiers with `model.upload`; a saved one stops
loading when its owner loses that permission.

**Storage:** PostgreSQL (with pgvector) holds accounts, sessions, sharing, settings, connections, labeling
annotations (schema `lbl`) and every computed feature vector (schema `vec`); `docker compose up` starts it.
Image crops, projections and `.npy` features stay in `.dataviz_cache/` as rebuildable caches. On the first start
the old JSON / SQLite stores (`connections.json`, `llm.json`, `models.json`, `labeling/`) are imported once, owned
by the first admin and private; the old files are left untouched.

## Layout

```
./                      (or scripts/image-tools/ inside a bigger project)
├── app.py              launcher: puts this folder on sys.path, calls dataviz.cli.main()
├── config.yaml         every setting, documented inline
├── requirements.txt
├── dataviz/            Python package (the server)          → dataviz/README.md
│   ├── cli.py          flags + config, port, optional --dataset session, banner, uvicorn
│   ├── state.py        State: the dataset being served (index, features, scores, projections)
│   ├── workspace.py    per-dataset cache folders <name>__<fingerprint>
│   ├── paths.py        every derived path, in one place
│   ├── core/           config, logging, job queue, worker processes  → core/README.md
│   ├── data/           data sources (local / S3), sessions (saved datasets), indexer → data/README.md
│   ├── ml/             feature extractors, projections, k-NN, groups      → ml/README.md
│   ├── models/         detectors: registry, loaders, API client, predict job, evaluation → models/README.md
│   └── server/         FastAPI app, middleware, route modules             → server/README.md
└── static/             the page: TS sources in ts/, built to js/ (npm run build)              → static/README.md
    ├── index.html
    ├── css/            tokens → base → layout → components → plot → settings → responsive
    ├── ts/             TypeScript sources: core/ plot/ views/ settings/ label/ + main.ts
    ├── js/             generated by `npm run build` (gitignored): don't edit
    └── assets/         icons.svg sprite, fonts
```

## How a request flows

```
browser ── GET /api/projection ──▶ server/routes/projections.py
                                     │  state.request_projection()          (state.py)
                                     │    cached? memory LRU → workspace/projections/*.npz
                                     │    no → JobManager.submit()           (core/jobs.py, 'main' lane)
                                     │           features: ml/features.py on the GPU (cached .npy)
                                     │           k-NN + UMAP: core/workers.py process pool → ml/analysis.py
   ◀── {ready:false, job_id} ────────┘
   polls /api/jobs/{id} (progress + ETA)   ── rich progress bar in the terminal, same numbers
   ◀── {ready:true, x, y, …} (orjson bytes, gzip, cached per key)
```

Opening a session builds a new `State` and swaps it into `server.app.Ctx`; every route reads the current
one through a proxy, so the swap needs no restart. With no session open (`ctx.state is None`, the
Sessions home) the proxy answers 409 "no session open"; `/api/status`, `/api/jobs`, `/api/datasets`,
`/api/models` and the S3 / folder browsers work without one.

## Where to change …

| I want to … | Go to |
|---|---|
| add a feature model | `config.yaml` `features.models` (code: `dataviz/ml/features.py` `model_spec`) |
| add a projection method | `dataviz/ml/analysis.py` `METHODS` (with its `category`) + `project()` / `_sklearn_estimator()`; page params: `static/ts/views/controls.ts` `PARAM_INFO` |
| add an API endpoint | the matching `dataviz/server/routes/*.py` (`router(state, ctx)` factory) |
| support another storage backend | `dataviz/data/sources.py` (implement the source interface), `connections.py` `source()` |
| change colours / theme | `static/css/tokens.css`, `static/css/palettes.css` for the named palettes (+ `ui.palette` / `ui.palette_light` in `config.yaml`) |
| change plot behaviour (lasso, pan, zoom) | `static/ts/plot/interactions.ts` |
| change what the plot draws | `static/ts/plot/render.ts`, overlays in `plot/marks.ts` |
| add a settings section | `static/ts/settings/forms.ts` (renderer) + `dialog.ts` (`SECTIONS`, actions) |
| support another model format / family | `dataviz/models/runners.py` (`load_runner`), allowed combos: `models/registry.py` `MATRIX`; page form: `static/ts/settings/models.ts` |
| add a finding / chart to the Evaluation tab | `dataviz/models/insights.py` (`findings()` rules, `dataset_insights()`), `static/ts/views/eval.ts` |
| add a chart to the Model tab | `dataviz/models/evaluate.py` (numbers), `static/ts/views/model.ts` (`renderCharts`), a card in `index.html` `#tab-model` |
| change text search (prompt, tokenizer, k) | `dataviz/ml/features.py` `TextEncoder` / `prompt()`, `state.py` `text_search()`; page: `static/ts/main.ts` (`#text-search`) |
| change train / val leakage detection | `dataviz/data/curation.py` `_leak_items` (`leak_sim`); splits: `models.split_source` → `models/splits.py` |
| change the model A-vs-B compare | `dataviz/models/insights.py` `compare()`, `static/ts/views/eval.ts` (`loadCompare` / `renderCompare`) |
| change how an embeddings row looks (card, hover tip, dialog) or add a preview kind | `static/ts/views/preview.ts`, row dialog `views/modal.ts` `openRow`; server: `dataviz/data/embedding_source.py` (`PREVIEW_COLS`, MIME types), `routes/samples.py` `/api/preview` |
| hide or show a control per session kind | `dataviz/server/deps.py` `capabilities()` → `html.no-boxes` (`static/ts/main.ts`) hides every `.needs-boxes` element (`static/css/states.css`) |
| change the image browser (no selection) | `static/ts/views/image-grid.ts`; server lists / thumbnails: `dataviz/server/routes/samples.py` (`/api/images`, `/api/img/*`) |
| change the image viewer (box toggles, right-click copy) | `static/ts/views/viewer.ts`, the dialog around it `views/modal.ts` |
| change the box editor in the visualizer | `static/ts/views/box-editor.ts`; ops + save path: `dataviz/data/curation.py` (`apply_ops` `set_box` / `add`, `Curation.edit`) |
| support another annotation import format / source | `dataviz/labeling/importers.py` (parsers → Records), routes `dataviz/server/routes/label_import.py`, form `static/ts/label/import.ts` |
| use a Settings › Models model on the label page / gate it | `dataviz/models/hub.py` (adapters), `dataviz/server/routes/labeling.py` (`adapter_for`, `activate_model`), `static/ts/label/model.ts` |
| change the ◆ AI assistant (providers, features, prompts, token budget) | backend `dataviz/llm/` (`store.py` switches + `FEATURES`, `providers.py` OpenAI / compatible / Claude / Gemini, `budget.py`, `context.py`, `tasks.py`) → `dataviz/llm/README.md`, routes `dataviz/server/routes/llm.py`; page `static/ts/core/llm.ts` (visibility), `settings/llm.ts`, `views/assistant.ts`, `label/assist.ts` |
| change the optional review (card bar + checklist, Review button, Review all) | `dataviz/labeling/store.py` `set_review` / `stats`, route `/api/label/projects/{pid}/review`; card `static/ts/views/home.ts` (`taskHTML`, `checklistHTML`, `reviewAll`); label page `static/ts/label/app.ts` (`setReview`, `reviewAll`), `panels.ts` (`renderReview`); colours `--prog-review` / `--prog-bulk` in `css/tokens.css` |
| change terminal logging | `dataviz/core/logging.py` (tags, colours), HTTP lines in `dataviz/server/app.py` |

## Conventions (whole project)

- **Comments say why, not what.** Short, lower-case inline comments next to the line they explain;
  module docstrings / header comments say what a file is for.
- **Measured, not guessed.** Performance choices note the measurement that motivated them (e.g. 4 decode
  threads = 1.8×, 16 = 0.6×). Keep doing that when you change one.
- **Everything slow is a job with progress** (terminal + page), never a request that hangs.
- **Caches are keyed by content fingerprints**, so they are never stale and never need clearing.
- **One build step** for the page: edit `static/ts`, then `npm run build` (or keep `npm run watch` running) and reload.

## Testing

```bash
DATAVIZ_TEST_DATABASE_URL=postgresql://dataviz:$POSTGRES_PASSWORD@127.0.0.1:15432/postgres \
  .venv/bin/python -m pytest -q tests      # ~4 min; without the variable a pgvector container is started via docker
                                           # (on a port docker picks, so it never collides with a running database)
.venv/bin/python -m pytest -q -n auto tests   # ~75 s on 128 cores; auto = 12 workers (~2.5 GiB each), DATAVIZ_TEST_WORKERS=<n> to change
DATAVIZ_TEST_CHROMIUM=/path/to/chrome .venv/bin/python -m pytest -q tests/test_page_smoke.py   # a browser of your own
```

| Tests | Cover |
|---|---|
| `test_auth.py`, `test_admin.py`, `test_sharing.py`, `test_sessions_access.py` | sign-up / login (username or email), lockout per account + address, per-IP limit, cookies, CSRF, logout / password change revoke sessions, admin users + settings + last-admin guard, avatars, sharing + roles |
| `test_acl.py`, `test_acl_routes.py`, `test_multi_session.py`, `test_security.py`, `test_tiers.py` | every `/api` route is guarded; the viewer / labeler / owner matrix; several sessions served at once at `s/<id>/`; open sign-up hardening (no code-running models for non-admins, no SSRF echo, no open redirect) |
| `test_repos.py`, `test_legacy_import.py`, `test_vectors.py`, `test_db_schema.py` | per-user connections / providers / models (secrets encrypted, never shared), the one-time import of the old files, pgvector store, migrations == schema |
| `test_curation.py` | every curation rule (incl. `split_leak`), fix / undo byte round-trip, sha1 conflicts, S3 writes (moto), 20k-image scan speed |
| `test_store.py`, `test_routes.py`, `test_io.py`, `test_formats.py`, `test_colors.py` | labeling: SQLite store, its API routes, upload / local / S3 / FTP / SFTP media and export sinks, YOLO writers, class colours |
| `test_labeling_source.py` | a labeling task opened live as a visualizer session |
| `test_db.py` | PostgreSQL / MSSQL export in docker containers (skips without docker or when a container cannot start) |
| `test_models.py` | model-assisted labeling against two fake model servers (real HTTP), incl. activation |
| `test_hub.py` | Settings › Models models on the label page (a dataviz-label/1 API model, local weights through the shared runner), scored by the visualizer too |
| `test_edits.py` | the image editor: add / set_box ops, edits without a scan, a label file for a background image, conflicts, undo; the image browser routes |
| `test_import.py` | annotation import: YOLO / COCO / CSV / database parsers, image matching, modes, class mapping, uploads, the job |
| `test_llm.py` | the ◆ AI assistant against fake OpenAI-compatible / Anthropic / Gemini servers: key storage + masking, connection test, feature gating (403), NDJSON chat, token budget, box formats, label check / NL filter / auto-tag / voice, label assist + accept |
| `test_filter_js.py`, `test_strip_filter.py` | `static/ts/core/filter.ts` under node (skips without node); the label page's image-strip filter |
| `test_page_smoke.py` | the real `app.py` + headless Chromium: open a session, plot, Curation scan finds a seeded leak; the activity bar groups, the image browser, viewer toggles, right-click copy, drawing a box and saving it; the label page's import dialog and model activation; the ◆ AI assistant appearing only once connected and switched on, a streamed answer; skips without `playwright install chromium` (and its system libraries: `playwright install-deps`) |

Upstream deprecation warnings that are not ours are filtered in `tests/conftest.py`. `bench_labeling.py` is a
benchmark, not a test. `eslint` (no-undef, no-unused-vars) checks the ES modules:

```bash
npx eslint@8 --no-eslintrc --env browser,es2022 --parser-options=ecmaVersion:2022,sourceType:module \
  --global Plotly --rule 'no-undef: error' --rule 'no-unused-vars: [error, {args: none}]' static/js
```

## Deploy with Docker

```bash
cp .env.example .env      # set POSTGRES_PASSWORD, DATAVIZ_SECRET_KEY, DATAVIZ_ADMIN_PASSWORD
docker compose up -d                                              # http://<host>:19151  (CPU)
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d   # with NVIDIA GPU
```

Two containers: `image-tools` and `image-tools-postgres` (pgvector, data in the `pg_data` volume, reachable from
this host only at `127.0.0.1:${PG_PORT:-15432}`, user / database `dataviz`). Back up the database
(`docker exec image-tools-postgres pg_dump -U dataviz dataviz > backup.sql`) **together with** `DATAVIZ_SECRET_KEY`:
without the key the stored passwords and API keys cannot be read. Other settings in `.env`: `DATASET_DIR`,
`DATAVIZ_DEVICE`, `HF_TOKEN`; `COMPOSE_FILE` selects the compose files.

Without docker: run any PostgreSQL ≥ 14 with the `vector` extension and set `DATABASE_URL` (the schema is created /
migrated at start-up, `dataviz/db/migrations`).

**The persistent-path server (only `/home/jovyan/work` survives a reset):** `.env` sets
`COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml:docker-compose.server.yml`. `docker-compose.server.yml`
bind-mounts the caches and the database to `/home/jovyan/work/image-tools-data/` (`postgres/`, `dataviz_cache/`,
`hf_cache/`). After a reset run `./server-setup.sh`, which restores `/etc/docker/daemon.json` from
`/home/jovyan/work/docker-config/`, creates those folders and starts the containers.

## TypeScript

All front-end code is strict TypeScript in `static/ts/`. `npm run typecheck` (tsc), `npm run lint`, `npm run build` (esbuild, per-module output to `static/js/`). Skill scripts in `.claude/skills/*/scripts/*.ts`: `npm run typecheck:tools` / `npm run build:tools` (emits the `.cjs` beside them).
