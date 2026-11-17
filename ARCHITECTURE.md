# Architecture: where things live

This is a one-page map of the code. Each folder has its own `README.md` with the details; this page tells you **which folder to open**.

## Names

The product is **DataViz**. Its pages, in the header's fixed order, and their names in the code:

| UI | Code (packages, zone, URL, API, CSS prefix) |
|---|---|
| **Home** (the logo: one card each for the pages below) | `views/home.ts`, screen `home`, URL `/` |
| **Bucket** (was Data Center) | `dataviz/bucket/`, zone `bucket`, `?bucket[=<id>&path=]`, `/api/bucket/*`, `bk-`, assistant role `bucket` |
| **Image** (was Sessions): tabs **Visualize** and **Label** | `dataviz/image/` (sessions, sources, curation …), `views/image.ts`, `?image[=viz\|label]`; zones `visualizer` and `labeling`; a single visualize / label item is still a *session* (`/s/<id>/`, `?label=<pid>`) |
| **Data** (was Analysis) | `dataviz/data/`, zone `data`, `?data[=<session>]`, `/api/data/*`, `dt-`, assistant role `data` |

Not part of this naming: `dataviz/ml/analysis.py` and the config key `analysis:` are the visualizer's projection
settings. Migration 0016 renamed the tables and the stored names; an old `?analysis` link redirects to `?data`.

## The app at a glance

```
browser (one index.html)                               server (FastAPI, package dataviz/)
──────────────────────────────────────                 ───────────────────────────────────
 /login            views/login.ts                       routes/auth.py
 /                 views/home.ts       Home     ──▶     the routes below (counts and recent items)
 /?bucket          bucket/*            Bucket   ──▶     routes/bucket.py ─▶ bucket/service.py
 /?image           views/image.ts      Image    ──▶     routes/datasets.py, routes/labeling.py
 /s/<id>/          main.ts + views/*   Visualize ──▶    routes/projections, samples, curation, models, compare …
 /?label=<pid>     label/*             Label    ──▶     routes/labeling.py, label_import.py ─▶ labeling/*
 /?data            data/*              Data     ──▶     routes/data.py ─▶ data/service.py
 ◆ assistant       core/dock.ts, bucket/ai.ts, data/dock.ts ─▶ routes/llm.py, routes/agent.py ─▶ llm/*
                                                         PostgreSQL + pgvector (db/), caches in .dataviz_cache/
```

Data flows **Bucket → Image sessions**. Everything you import lands in a bucket. From there you **send** folders to a Visualize session (embeddings plot, curation, models) or a Label session (annotation). A session can still point at its own folder or S3 prefix directly.

## Zones: what an install contains

The product is being split into zones, so an analyst can install Bucket + Data without the visualizer and its GPU libraries. `dataviz/zones.py` is the list: `core` (always), `bucket`, `visualizer`, `labeling`, `data`. An install picks them with `DATAVIZ_ZONES=core,bucket` or `zones:` in the config; the default is every zone. Each `server/routes` module belongs to one zone (`zones.ROUTES`), and only the routes of the selected zones are loaded. `cli.py` starts core; what only the visualizer needs at start-up (feature spaces, worker pools, opening a dataset) is in `visualizer_boot.py`, imported only when that zone is selected. The rule: core never needs another zone, and `tests/zones/` proves a partial install runs with the visualizer libraries blocked.

**Installing fewer zones.** `requirements/<zone>.txt` holds each zone's packages (`requirements.txt` = all of them). `zones.FILES` lists the files only a zone uses; `python -m dataviz.zones prune` deletes those of the zones an install leaves out, and the Docker image does both with `--build-arg ZONES=bucket,data` (no torch, no visualizer code). Kept code reaches the visualizer only through `static/ts/core/hooks.ts` (theme repaint, status refresh, model list, display marks) or a zone-guarded dynamic import, never a static one; the visualizer screen is `static/ts/viz/boot.ts`, loaded by `main.ts` only on `/s/<id>/`. Markup of one zone sits between `<!-- zone:x -->` and `<!-- /zone:x -->` in `index.html`, and `server/static.py` leaves it out when that zone is off. `tests/zones/test_install.py` checks the import rules and runs a pruned analyst copy in a browser. Most of `dataviz/labeling/` stays in every install because the Bucket page reuses it.

## Find the code for…

| I want to change… | Backend (`dataviz/`) | Frontend (`static/`) |
|---|---|---|
| **Data**: queries, catalog, statistics, forecasts, dashboards | `data/` (`service.py`), `server/routes/data.py` | `ts/data/`, `css/data.css` |
| **Bucket**: buckets, file ops, tags, format detection, Send to | `bucket/` (`service.py` is the only entry point; contract in `bucket/API.md`), `server/routes/bucket.py` | `ts/bucket/`, `css/bucket.css` |
| Video → frames (Extract frames, auto-extract) | `bucket/video.py`, `service.extract_frames` | `ts/bucket/frames.ts` |
| AI assistant: one chat on every page (the page picks the specialist), tools the LLM can call, confirm-a-plan loop | `llm/tools/`, `llm/engine/` (ReAct loop, router roles, confirm, chat compaction), `llm/sessions.py`, `llm/prompts/`, `llm/providers.py` | `ts/core/dock.ts` (right-side dock, every screen), `ts/core/chat.ts`, `ts/views/assistant.ts`, `ts/bucket/ai.ts`, `ts/views/dock-page.ts`, `css/dock.css` |
| AI skill (how an agent drives the Bucket page) | `.claude/skills/bucket/SKILL.md` | |
| Home page (the overview cards) | the existing routes | `ts/views/home.ts`, `css/home.css` |
| Image page (session cards, Visualize / Label tabs, tags, sharing) | `image/sessions.py`, `server/routes/datasets.py` | `ts/views/image.ts`, `ts/views/share.ts`, `css/image.css` |
| Create session (one dialog: a bucket folder → Visualize, Label or both linked; format check) | `bucket/service.py` `create_session`, `session_check`, `bucket/formats.py` `SESSION_SUPPORT` | `ts/views/session-new.ts` |
| Where datasets are read from (local / S3 / FTP) | `image/sources.py`, `image/connections.py`, `labeling/media.py` | `ts/settings/forms.ts` |
| The plot, projections, features | `ml/`, `state.py`, `server/routes/projections.py` | `ts/plot/`, `ts/views/controls.ts` |
| Curation scans and fixes | `image/curation.py`, `server/routes/curation.py` | `ts/views/curation.ts` |
| Models, predictions, evaluation | `models/`, `server/routes/models.py` | `ts/views/model.ts`, `ts/views/eval.ts` |
| Labeling tool | `labeling/`, `server/routes/labeling.py` | `ts/label/` |
| Import / export of annotations (YOLO, COCO, CSV, DB) | `labeling/importers.py`, `labeling/formats.py`, `labeling/export.py` | `ts/label/import.ts`, `ts/label/export.ts` |
| Accounts, tiers, permissions | `auth/` (`tiers.py` = the permission list) | `ts/settings/users.ts`, `ts/core/auth.ts` |
| Database tables / migrations | `db/schema.py`, `db/migrations/versions/` | |
| Config keys | `config.yaml`, `core/config.py` | |
| Which zones exist, which routes each owns | `zones.py` | |
| Colours, fonts, looks | | `css/tokens.css` (tokens only), `css/<look>.css` |
| Routing between screens | `server/routing.py`, `server/routes/system.py` | `ts/theme-boot.ts`, `ts/core/nav.ts`, `ts/main.ts` |

## Rules that keep it easy to work in

- **One entry point per feature on the server.** Routes, the LLM tools and the CLI all call the same service functions (e.g. `bucket/service.py`), so a behaviour is fixed in one place.
- **Dependencies point one way:** `server → (state, models, bucket, data, llm) → (image, labeling, ml, core) → db`. Nothing imports `server` or `cli` back.
- **Frontend:** one module per concern, loaded lazily per screen (`main.ts` imports `label/app.js` / `bucket/app.js` on demand). One `innerHTML` render per change plus delegated listeners. Colours exist only as CSS tokens.
- **Slow work is a job** with progress (`core/jobs.py` ↔ `ts/core/jobs.ts`); no request blocks on it.
- **Tests** live in `tests/<feature>/` (`auth`, `bucket`, `data`, `labeling`, `models`, `visualizer`, `compare`, `ml`, `llm`, `storage`, `ui`, `zones`, `bench`; see the README's Testing section); a new test goes in the folder of its feature. Shared fixtures stay in `tests/` (`conftest.py`, `conftest_io.py`, `authutil.py`, `vizutil.py`), and file names stay unique across folders. Run them with `pytest tests` against PostgreSQL (see `tests/conftest.py`).
- **Design references** live in `design/` (rendered mockups and their sources).
