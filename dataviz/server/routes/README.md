# `dataviz/server/routes/` — the JSON API, by area

Each module exports `router(state, ctx) -> APIRouter`, included by `server/app.py`.

| Module | Endpoints |
|---|---|
| `system.py` | `GET /` (the page), `/plotly.min.js`, `/api/status` (startup polling, features ✓, active dataset + its index job), `GET/POST /api/settings` (remembered UI), `/api/jobs`, `/api/jobs/{id}`, `/api/meta`, `/api/points` (columnar, every box), `/api/workspace` |
| `projections.py` | `/api/projections` (saved views), `/api/scores`, `POST /api/projection`, `POST /api/groups` |
| `samples.py` | `/api/images` (background image names for the image browser), `/api/img/raw?name=` (the plain image), `/api/img/thumb?name=&size=` (boxes drawn, cached on disk), `/api/crop/{id}`, `/api/image/{id}` (full image with boxes drawn), `/api/sample/{id}`, `/api/similar/{id}`, `/api/duplicates`, `POST /api/distances`, `POST /api/export` (CSV), `GET/POST /api/tags` |
| `models.py` | `GET/POST/DELETE /api/models`, `/api/models/discover` (trained runs in the project), `/api/models/spec` (format × family matrix, Custom output spec), `/api/models/adapter_template.py`, `/api/models/browse` (folders + weight files), `/api/models/serving-list`, `POST /api/models/upload`, `POST /api/models/{id}/test`, `POST /api/models/{id}/predict` (job), `/api/models/{id}/eval?conf&iou&split`, `/api/models/{id}/pred/{pid}` (+ `/image`, `/crop`), `/api/models/{id}/image-preds?name=&conf=` (one image's predictions as data, for the viewer), `/api/eval/overview?split&conf_mode&conf&focus` (the Evaluation tab) |
| `curation.py` | the Curation view (`/api/curation/*`, see its docstring) and the image editor: `GET /api/img/boxes?name=` (an image's boxes from its label file + sha1), `POST /api/edits` (set_box / add / set_class / delete ops through the curation backup + undo), `GET /api/edits/status` |
| `labeling.py` | labeling sessions (`/api/label/*`, `labeling/README.md`), incl. model links (a Settings › Models model or a model server of the project's owner) and `POST /api/label/projects/{pid}/tasks/{tid}/model/activate`. Per-route role guards: viewer reads, labeler labels / zip-exports / duplicates, owner changes the structure and exports to folders / connections; `GET /api/label/exports/{run}` only for who started it or the owner; `GET/PUT /api/label/settings` = the caller's `user_prefs.label` |
| `label_import.py` | annotation import: `POST /api/label/annotations/upload`, `/preview`, `POST /api/label/projects/{pid}/import-annotations` (job, labeler); sources read with the caller's own connections, local paths inside `storage.allowed_roots` |
| `llm.py` | the ◆ AI assistant (`dataviz/llm/README.md`): `GET /api/llm/state`, `GET/PUT /api/llm/config`, `POST/DELETE /api/llm/providers`, `POST /api/llm/test`, `POST /api/llm/models`, `POST /api/llm/chat` (NDJSON stream; `task: 'report'`), `POST /api/llm/label-check`, `/filter`, `/tag` (job), `/voice`, `POST /api/llm/label/{pid}/assist`, `/prelabel`, `/accept`; every feature route 403s when its switch is off. Everything runs on the caller's own providers; dataset features need the bound `/s/<id>/` session (auto-tag: labeler), label features the label session (assist: viewer, prelabel / accept: labeler) |
| `datasets.py` | `GET /api/datasets?scope=` (cards + owner / members / my_role / opened_at / loaded), `POST /api/datasets` (owner = caller), `PUT …/tags`, `DELETE`, `POST …/refresh`, `…/sync` (owner), `POST /api/datasets/{id}/open` (viewer: loads it, marks it opened), `GET/POST/DELETE /api/connections` (the caller's own; create / test / browse need `can_add_connections`), `POST /api/connections/test`, `POST /api/connections/browse`, `GET /api/fs/browse` (inside `storage.allowed_roots` only) |

## Patterns

- **Compute-or-job**: `POST /api/projection` and `GET /api/scores` answer `{ready: true, …data}` when
  cached, otherwise `{ready: false, job_id}`; the page polls `/api/jobs/{id}` and asks again.
- **Columnar payloads**: `/api/points` sends `{id: [...], cls: [...], image: [...], …}` — one array per
  field instead of one object per box: no repeated keys, and the page indexes them by box id directly.
- **Rounded floats**: coordinates and scores are rounded (`np.round`, `_r`) before serialization.
- **Full-image LRU**: `/api/image` keeps 64 rendered JPEGs keyed by `(state.tag, id, width, boxes, model, conf)`
  (`?model=&conf=` draws that model's predictions dashed over the solid label boxes);
  in S3 stream mode this also saves the S3 GET when you step back and forth in the modal.
- **Secrets never leave**: connection endpoints return `public_connection()` shapes; tests and edits
  take `{id}` and the server merges the stored secret (`Registry.merged_connection`).
- **Distances**: `1 − E·Eᵀ` for cosine features (rows are normalized), euclidean otherwise; capped at
  12 ids.
