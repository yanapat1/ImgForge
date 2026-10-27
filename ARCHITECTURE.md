# Architecture: where things live

This is a one-page map of the code. Each folder has its own `README.md` with the details; this page tells you **which folder to open**.

## The app at a glance

```
browser (one index.html, five screens)                 server (FastAPI, package dataviz/)
──────────────────────────────────────                 ───────────────────────────────────
 /login            views/login.ts                       routes/auth.py
 /                 views/home.ts       Sessions  ──▶    routes/datasets.py, routes/labeling.py
 /?data            datacenter/*        Data Center ─▶   routes/datacenter.py ─▶ datacenter/service.py
 /s/<id>/          main.ts + views/*   Visualizer ──▶   routes/projections, samples, curation, models, compare …
 /?label=<pid>     label/*             Labeling   ──▶   routes/labeling.py, label_import.py ─▶ labeling/*
 ◆ assistant       views/assistant.ts, datacenter/ai.ts ─▶ routes/llm.py ─▶ llm/* (providers, tools, agent)
                                                         PostgreSQL + pgvector (db/), caches in .dataviz_cache/
```

Data flows **Data Center → sessions**. Everything you import lands in a Data Center bucket. From there you **send** folders to a Visualize session (embeddings plot, curation, models) or a Label session (annotation). A session can still point at its own folder or S3 prefix directly.

## Find the code for…

| I want to change… | Backend (`dataviz/`) | Frontend (`static/`) |
|---|---|---|
| **Data Center**: buckets, file ops, tags, format detection, Send to | `datacenter/` (`service.py` is the only entry point; contract in `datacenter/API.md`), `server/routes/datacenter.py` | `ts/datacenter/`, `css/datacenter.css` |
| AI assistant: tools the LLM can call, confirm-a-plan loop | `llm/tools.py`, `llm/agent.py`, `llm/prompts/`, `llm/providers.py` | `ts/datacenter/ai.ts`, `ts/views/assistant.ts` |
| AI skill (how an agent drives the Data Center) | `.claude/skills/datacenter/SKILL.md` | |
| Sessions home (cards, tags, sharing) | `data/sessions.py`, `server/routes/datasets.py` | `ts/views/home.ts`, `ts/views/share.ts`, `css/home.css` |
| Where datasets are read from (local / S3 / FTP) | `data/sources.py`, `data/connections.py`, `labeling/media.py` | `ts/settings/forms.ts` |
| The plot, projections, features | `ml/`, `state.py`, `server/routes/projections.py` | `ts/plot/`, `ts/views/controls.ts` |
| Curation scans and fixes | `data/curation.py`, `server/routes/curation.py` | `ts/views/curation.ts` |
| Models, predictions, evaluation | `models/`, `server/routes/models.py` | `ts/views/model.ts`, `ts/views/eval.ts` |
| Labeling tool | `labeling/`, `server/routes/labeling.py` | `ts/label/` |
| Import / export of annotations (YOLO, COCO, CSV, DB) | `labeling/importers.py`, `labeling/formats.py`, `labeling/export.py` | `ts/label/import.ts`, `ts/label/export.ts` |
| Accounts, tiers, permissions | `auth/` (`tiers.py` = the permission list) | `ts/settings/users.ts`, `ts/core/auth.ts` |
| Database tables / migrations | `db/schema.py`, `db/migrations/versions/` | |
| Config keys | `config.yaml`, `core/config.py` | |
| Colours, fonts, looks | | `css/tokens.css` (tokens only), `css/<look>.css` |
| Routing between screens | `server/routing.py`, `server/routes/system.py` | `ts/theme-boot.ts`, `ts/core/nav.ts`, `ts/main.ts` |

## Rules that keep it easy to work in

- **One entry point per feature on the server.** Routes, the LLM tools and the CLI all call the same service functions (e.g. `datacenter/service.py`), so a behaviour is fixed in one place.
- **Dependencies point one way:** `server → (state, models, datacenter, llm) → (data, labeling, ml, core) → db`. Nothing imports `server` or `cli` back.
- **Frontend:** one module per concern, loaded lazily per screen (`main.ts` imports `label/app.js` / `datacenter/app.js` on demand). One `innerHTML` render per change plus delegated listeners. Colours exist only as CSS tokens.
- **Slow work is a job** with progress (`core/jobs.py` ↔ `ts/core/jobs.ts`); no request blocks on it.
- **Tests** live in `tests/`, named after the area (`test_dc_*.py` for the Data Center). Run them with `pytest tests` against PostgreSQL (see `tests/conftest.py`).
- **Design references** live in `design/` (rendered mockups and their sources).
