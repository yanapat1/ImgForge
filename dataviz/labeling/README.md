# `dataviz/labeling/`: labeling sessions

A **labeling session** (a "project" in code) has one image set and N **tasks**. Each task has a `task_type` from
`schema.TASK_TYPES` and its own classes. The page draws annotations per task, and export writes **one
`images/` folder plus one folder per task** (or per-task database runs).

## Files
| File | What it does |
|---|---|
| `schema.py` | The portable schema (SQLAlchemy Core) of the database export targets (PostgreSQL / MSSQL). `GEOMETRY` documents the geometry JSON per type. The app's own copy is `dataviz/db/schema.py` `lbl.*` (same columns + `project_id` everywhere + `model_run_at` / `review`). |
| `projects.py` | `ProjectRegistry(root)`: the project index (`app.sessions` kind `label` + `app.label_projects` + `lbl.projects`), the per-project folders, create / delete / duplicate, card stats. |
| `store.py` | `ProjectStore(pid)`: one project's rows in the shared PostgreSQL schema `lbl` (every query filtered by `project_id`): annotations upsert / diff, status, classes, media pages, export streams. |
| `colors.py` | The colour-blind-safe class colour sequence (greedy max-min CIEDE2000 under normal vision + 3 CVD simulations). |
| `media.py` | Image-only sources: upload folder, local folder, S3, FTP, SFTP; the import job; the remote image cache; thumbnails. |
| `sinks.py` | File export targets: zip, local folder, S3, FTP, SFTP. |
| `formats.py` | YOLO detect / seg / pose / cls writers, `classes.txt`, `data.yaml`. |
| `export.py` | The export job: `images/` once + `<task-slug>/labels/*.txt` + `classes.txt` + `data.yaml` + `manifest.json`. |
| `db_export.py` | `DbSink`: PostgreSQL (`ON CONFLICT`) and MSSQL (`#stage` + `MERGE`) upserts, one run per task. |

## Optional review
A session can switch on `review` (PATCH / create body, `app.label_projects.review`, card `review`). Then every done image
can be approved per task: `manual` (one by one, label page button or Alt+R) or `bulk` (Review all, from the card's ⋯
menu or the label page). The state lives in the column `lbl.media_task_status.review` (not in the portable
schema, so exports are unchanged) and is cleared by a label edit, skip, reopen or class delete. The card shows a
second bar per task (solid = manual, hatched = bulk) and a checklist line; `stats()` returns `review:{tid:{manual,bulk}}`
next to the status counts. All of it is one `UPDATE` per task (10k images: 5 ms).

## Storage
PostgreSQL (`dataviz/db/schema.py`):
```
app.sessions          one row per label session: id = pid, kind 'label', owner_id, name (unique per owner), tags, visibility
app.label_projects    description, source, export_default (JSONB), n_media, task_ids, model_connections, review
lbl.projects          the store's root row (id -> app.sessions.id ON DELETE CASCADE)
lbl.tasks / label_classes / media / annotations / annotation_relations / media_task_status / export_runs
                      every row has project_id (-> lbl.projects, cascade); times epoch seconds; JSON JSONB
```
On disk (`<workspace_root>/labeling/`):
```
<pid>/images/                 uploaded files (UploadSource)
<pid>/cache/                  remote images, sha1(rel).<ext>
<pid>/thumbs/<media_id>.jpg   160 px thumbnails
<pid>/exports/<run>.zip       zip downloads
```
Page settings: `<workspace_root>/label_settings.json`. The old `projects.json` + `<pid>/annotations.db` are read
only by the one-time legacy import (`dataviz/db/legacy.py`).

Entry (`ProjectRegistry.get`): `{id, name, description, created, updated, source, export_default, n_media, task_ids,
model_connections, tags, review, owner_id, visibility}`.
`source` = `{kind:'upload'}` | `{kind:'local', path, recursive}` | `{kind:'s3', connection_id, bucket, prefix}` |
`{kind:'ftp'|'sftp', connection_id, path}`.

### Store concurrency (PostgreSQL)
- `write()` = one transaction holding `pg_advisory_xact_lock(hashtext(pid))`: the writers of one project are serial
  across threads and server processes, so the `base_version` check of a PUT stays exact (two PUTs with the same
  base: exactly one gets 409). A `write()` inside an open one on the same thread joins it; reads made while it is
  open use its connection (they see its rows). `joined(conn)` puts the store inside a caller's transaction
  (registry create / duplicate).
- Reads outside a write take a pooled connection. Streams (`iter_rows`, `iter_task_export`, `predict_targets`) use a
  private REPEATABLE READ connection with server-side cursors, closed in `finally`.
- `put_annotations(..., user=)` / `set_status(..., user=)` take the user id: `annotations.created_by` /
  `media_task_status.committed_by` (routes pass `auth.context.user_id()`).
- Rows inserted in one batch get `created_at` 1 µs apart, so `ORDER BY z_order, created_at` keeps the insertion order
  (SQLite's rowid did it before).

## API contract (all JSON; errors are `{"detail": "<human message>"}` with 4xx)
Ids are 32-char hex strings (`uuid4().hex`; the page creates annotation ids with `crypto.randomUUID().replaceAll('-','')`).
Times are epoch seconds (float) in JSON.

Shared shapes:
```
Task   {id, name, task_type, position, config:{keypoints?:[names], skeleton?:[[i,j]], flip_idx?:[..], multi_label?:bool},
        classes:[Class]}
Class  {id, task_id, name, color:'#rrggbb', position, hotkey:null|'ctrl+1'}
Media  {id, file_name, width, height, position, status:{<task_id>: 'todo'|'in_progress'|'done'|'skipped'}, n:{<task_id>: int}}
Ann    {id, task_id, class_id, geometry_type, geometry, attributes:{}, parent_id:null, z_order:0, version?, score?, source?}
Job    {job_id}   -> poll the existing GET /api/jobs/{id}
```

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/api/label/projects` | | `{projects:[{id,name,created,updated,source:{kind,…},n_media,cover:[media_id ≤4],tasks:[{id,name,task_type,n_classes,done,in_progress,skipped,todo}]}]}` |
| POST | `/api/label/projects` | `{name, description?, source, tasks:[{name, task_type, config?, classes:[{name, color?}]}], export_default?}` | `{project: Project, job_id: id\|null}` (job = import; null for `upload` until finalize) |
| GET | `/api/label/projects/{pid}` | | `Project = {id,name,description,created,updated,source,export_default,n_media,tasks:[Task]}` |
| PATCH | `/api/label/projects/{pid}` | `{name?, description?, export_default?}` | Project |
| DELETE | `/api/label/projects/{pid}?delete_files=0\|1` | | `{ok:true}` |
| POST | `/api/label/projects/{pid}/tasks` | `{name, task_type, config?, classes?:[{name,color?}]}` | Task |
| PATCH | `/api/label/projects/{pid}/tasks/{tid}` | `{name?, config?}` | Task |
| DELETE | `/api/label/projects/{pid}/tasks/{tid}` | | `{ok:true}` (soft delete) |
| POST | `/api/label/projects/{pid}/tasks/reorder` | `{ids:[tid…]}` | `{ok:true}` |
| POST | `/api/label/projects/{pid}/tasks/{tid}/classes` | `{names:[str…]}` or `{classes:[{name,color?}]}` | `{classes:[Class]}` (new ones, auto colours) |
| PATCH | `/api/label/projects/{pid}/classes/{cid}` | `{name?, color?, hotkey?, position?}` | Class |
| DELETE | `/api/label/projects/{pid}/classes/{cid}?reassign_to=<cid>&delete_annotations=0\|1` | | `{ok:true}`; 409 if annotations exist and neither is given |
| POST | `/api/label/projects/{pid}/upload` | multipart: repeated `files` + parallel repeated `rel_paths` | `{saved:int, skipped:int}` |
| POST | `/api/label/projects/{pid}/import` | `{source?}` (omitted = finalize the uploads / rescan the project source) | `{job_id}` |
| GET | `/api/label/projects/{pid}/media?offset=0&limit=200&task_id=&status=&review=` | `review` = `none` (done, not reviewed) \| `manual` \| `bulk`, needs `task_id` | `{total, items:[Media]}`; a Media also has `review:{<task_id>: 'manual'\|'bulk'}` (done images only) |
| GET | `/api/label/projects/{pid}/media-ids` | | `{ids:[media_id…]}` in position order |
| GET | `/api/label/projects/{pid}/image/{mid}?max=` | | image bytes (`Cache-Control: private, max-age=86400`); prefetches the next 3 remote images |
| GET | `/api/label/projects/{pid}/thumb/{mid}` | | 160 px JPEG |
| GET | `/api/label/projects/{pid}/annotations/{mid}` | | `{media: Media, versions:{<task_id>: int}, annotations:[Ann]}` |
| PUT | `/api/label/projects/{pid}/annotations/{mid}` | `{task_id, base_version:int, annotations:[Ann]}` (the full live set of that task on that image) | `{version:int, status:str, n:int}`; **409** `{detail, version, annotations}` when base_version is stale |
| POST | `/api/label/projects/{pid}/commit/{mid}` | `{task_ids:[…]}` | `{status:{<task_id>: 'done'}}` |
| POST | `/api/label/projects/{pid}/skip/{mid}` / `reopen/{mid}` | `{task_ids:[…]}` | same |
| POST | `/api/label/projects/{pid}/review` | `{task_ids:[…], mode:'manual'\|'bulk'\|'clear', media_ids?:[…]}` (the optional review: `manual` = a person approved the done image, `bulk` = Review all, only done images not reviewed yet; `media_ids` omitted = every image) | `{changed:{<task_id>: int}}` |
| POST | `/api/label/projects/{pid}/export` | `{target:{kind:'zip'\|'local'\|'s3'\|'ftp'\|'sftp'\|'db', path?, connection_id?, bucket?, prefix?, create_views?}, task_ids?:[…], include_images:true, only_done:false, overwrite:false}` | `{job_id, run_id}` |
| GET | `/api/label/exports/{run_id}` | | `{run_id, status, kind, summary, download?:url}` |
| GET | `/api/label/exports/{run_id}/download` | | zip file |
| GET | `/api/label/colors?n=64` | | `{colors:['#…'], min_de:[float]}` |
| GET / PUT | `/api/label/settings` | the settings object | the settings object |

### Annotation import (`server/routes/label_import.py`, parsers `labeling/importers.py`)
| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/label/annotations/upload` | multipart `files` (+ `rel_paths`): .json, .csv / .tsv / .txt, .zip of YOLO labels, classes.txt / data.yaml | `{upload_id, files, bytes}` (kept a day) |
| POST | `/api/label/annotations/preview` | `{format: auto\|yolo\|coco\|text\|db, source, task_type, n_kpt?, coords?, project_id?, task_id?}` | `{format, records, images, classes:[{name, n, match}], matched?, unmatched_sample?, skipped, errors}` |
| POST | `/api/label/projects/{pid}/import-annotations` | `{task_id, format, source, mode: merge\|replace\|skip_labeled, class_map?: {name: class_id\|'new'\|'ignore'}, add_classes?, coords?}` | `{job_id}`; public result `{annotations, images, unmatched, unmatched_sample, classes_added, skipped}` |

`source`: `{kind: upload, upload_id}` · `{kind: local, path}` · `{kind: s3, connection_id, bucket, prefix}` · `{kind: ftp|sftp, connection_id, path}`
(a path / prefix may name one file) · `{kind: db, connection_id, mode: dataviz|sql, sql?, task_name?}` (PostgreSQL / SQL Server:
the tables the DB export writes, or one read-only SELECT whose columns are named image, class, x1 y1 x2 y2 | cx cy w h | x y w h, score).
Images are matched by relative path, then file name, then stem (case-insensitive); YOLO class indices without classes.txt map to the
task's classes in order. Rows are stored with `source='import'` (`ProjectStore.import_annotations`, one transaction per 2000 images).

### Models on the label page
A task's `config.model` is `{registry_id, model_id, name, conf, auto_on_open, class_map}` (a Settings › Models model, `models/hub.py`) or
`{connection_id, model_id, version, conf, auto_on_open, class_map}` (a model server connection). `POST /api/label/models/detect`
takes `{registry_id}` too. `POST /api/label/projects/{pid}/tasks/{tid}/model/activate {on}` lets it run in this server session;
predict / predict-all answer 409 until then, and `GET /api/label/projects/{pid}` lists the active task ids in `model_active`.

Existing routes that get extended:
- `GET /api/fs/browse?path=&images=1`: each folder also gets `images:int` (direct image files, capped count) when `images=1`.
- `POST /api/connections` / `GET /api/connections`: `kind` = `s3` (default) | `ftp` | `sftp` | `postgres` | `mssql`.
- `POST /api/connections/test`: per kind (ftp/sftp list the base path; db runs `SELECT 1` + reports whether the tables exist).
- `POST /api/connections/browse`: ftp/sftp `{connection, path}` → `{path, parent, folders:[{name, images}], images:int}`.

Connection fields per kind (the secret is masked by `public_connection`; a blank secret keeps the stored one):
- s3: existing.
- ftp: `name, host, port=21, username, secret_key, tls:false, passive:true, base_path`.
- sftp: `name, host, port=22, username, secret_key, private_key, base_path`.
- postgres / mssql: `name, host, port (5432 / 1433), database, username, secret_key, schema, table_prefix='lbl_', encrypt`.

## Export layout (every file target)
```
images/<file_name>
<task-slug>/labels/<stem>.txt        YOLO detect / seg / pose / cls (formats.py)
<task-slug>/classes.txt
<task-slug>/data.yaml
manifest.json
```

## Python interfaces between modules (the contract between `media`/`sinks`/`db_export` and the rest)
```python
# media.py
make_source(source: dict, project_dir: Path, connections: data.connections.Registry, s3cfg: dict) -> ImageSource
class ImageSource:                       # UploadSource, LocalImageSource, S3ImageSource, FTPImageSource, SFTPImageSource
    kind: str; uri: str
    def list_images(self) -> list[tuple[str, int]]       # (rel path with '/', size bytes), sorted by rel
    def read_bytes(self, rel: str) -> bytes
    def read_head(self, rel: str, n: int = 65536) -> bytes  # enough for PIL to read the size (Range GET on S3)
    def local_path(self, rel: str) -> Path | None          # set for upload / local: served directly, never copied
    def close(self) -> None                                # pooled FTP/SFTP connections
probe(source, rel) -> dict  # {width, height, mime, size_bytes, orientation}; EXIF-rotated width/height
class ImageCache:            # one per project; remote bytes in <pid>/cache/, local sources pass through
    def __init__(self, project_dir: Path, source: ImageSource)
    def path(self, rel: str) -> Path                       # fetch on miss (tmp -> replace), thread-safe per key
    def prefetch(self, rels: list[str]) -> None            # background, 4 threads, never raises
    def thumb(self, media_id: str, rel: str, size=160) -> Path  # <pid>/thumbs/<media_id>.jpg, lazily
    def preview(self, rel: str, max_side: int) -> Path     # downscaled JPEG for huge images
scan_folder_images(path) -> int                            # direct image files in a local folder (browse helper)

# sinks.py
make_sink(target: dict, connections, s3cfg, run_dir: Path) -> Sink   # kind zip | local | s3 | ftp | sftp
class Sink:
    kind: str; uri: str
    def check_target(self, overwrite: bool) -> None        # ValueError if the target is not empty and not overwrite
    def write(self, rel: str, data: bytes) -> None         # rel uses '/', thread-safe
    def copy_file(self, rel: str, src: Path) -> None       # images: copyfile / upload / ZIP_STORED
    def close(self) -> dict                                 # {files, bytes, uri, path? (zip file path)}

# db_export.py
engine_for(conn: dict)                                     # postgresql+psycopg / mssql+pymssql, pool_pre_ping
test(conn: dict) -> dict                                   # {ok, ms, server_version, tables_exist, schema_version}
export_to_db(conn: dict, store: ProjectStore, project: dict, task_ids: list[str], options: dict, progress) -> dict
    # ensure_schema, upsert project/tasks/classes/media/annotations/statuses in 1000-row batches,
    # one export_runs row per task; returns {tasks: {tid: {n_media, n_annotations}}, ms}

# store.py (what media / export / db_export may call)
ProjectStore.iter_rows(table: str, where: dict | None = None, batch=1000)  # yields lists of dict rows (raw columns)
ProjectStore.iter_task_export(task_id, only_done) # yields (media_row, [ann rows]) in media position order
ProjectStore.add_media(rows: list[dict])          # batch insert, skips existing file_name
```
`progress` is `job.progress(done=, total=, stage=, extra=)` from `core/jobs.py`.

## Implementation notes (backend core)
- `store.py` keeps times as epoch seconds (double precision), JSON as JSONB, booleans as booleans. `iter_rows()`
  converts each row for SQLAlchemy inserts: `*_at` -> timezone-aware UTC `datetime` (rows also carry the app-only
  columns `project_id` / `model_run_at` / `review`; the export's row converter keeps only the target's columns). `iter_task_export()` rows have `geometry` / `attributes` / `meta` decoded and the media
  row carries the task's `status`.
- `media.uri` holds the source-relative path (`rel`); `media.file_name` is the flattened unique name
  (`sub__name.jpg` when two files share a base name, case-insensitive) used for `images/<file_name>` and the
  label stem; `media.meta.rel_path` keeps the original.
- `export.run_export(job, project, store, target, options, image_path=None)`: `target` is a `Sink`, or
  `{'kind': 'db', 'conn': <connection dict>}` for `db_export.export_to_db`. `image_path(media_row) -> Path`.
  A zip export lands in `<pid>/exports/<run_id>/<run_id>.zip`.
- `GET /api/label/colors`: `min_de[0]` is `null` (the first colour has nothing to be compared with).
- Geometry types per task: detection `bbox`; segmentation `polygon` (+ `mask_rle`); keypoints `keypoints`
  (+ `bbox` parents, linked by `parent_id`, used as the pose box); classification `tag` (one per image unless
  `config.multi_label`). A keypoints task needs `config.keypoints` (names).
- PUT with an unchanged set returns the current version without bumping it (idempotent autosave).

## Measured
`tests/bench_labeling.py 10000` (this machine; 10k 64x48 JPEGs in a local folder, 2 tasks). PostgreSQL 17 in docker on
the same host (2026-10-19); the earlier SQLite numbers in brackets:

| Area | Budget | Measured |
|---|---|---|
| autosave PUT, 200 shapes (store, 10 changed) | < 30 ms | median 12.4 ms, p95 25 ms; 200 new shapes 20 ms [1.5 / 3.3 / 2.9 ms] |
| autosave PUT, 200 shapes (HTTP route, TestClient incl. JSON) | | median 24 ms, p95 32 ms [8.4 / 26 ms] |
| media page of 200, 10k images | < 20 ms | 11.8 ms (first), 13.6 ms (offset 9800), 16.6 ms (filtered `done`), 24 ms over HTTP [0.8 / 1.1 / 5.3 / 9-11 ms] |
| import scan, 10k local images | < 15 s | 3.9 s (list + header probe on 8 threads + 500-row batches) [3.4 s] |
| export 10k images x 2 tasks, local folder | < 30 s, flat memory | 17.8 s, Python heap peak 8.8 MB [14.1 s, 5.2 MB] |
| export 10k images x 2 tasks, zip | | 11.4 s, heap peak 24 MB [11.0 s, 22 MB] |
| colour sequence (64) | ~50 ms planned | ~300 ms once, lazily, warmed in a thread at server start |

Colours: first 25 have min pairwise CIEDE2000 = 8.22 under the worst of normal / protan / deutan / tritan (the
12 target is not reachable for 25 colours under all three dichromacies; see `colors.py`). Tests assert >= 8.

## Measured (I/O)
This machine: 128 cores, local disk, warm page cache. The numbers come from one-off scripts that make the same calls as
the import / export jobs (`media.probe`, `ImageCache.thumb`, `ZipSink` / `LocalDirSink`, `db_export.export_to_db`).
PostgreSQL 16 runs in docker on the same host.

| What | Result | Notes |
|---|---|---|
| `probe()` 1000 local 1920×1080 JPEGs (1.57 GB) | **0.08 s, 12.7k img/s** on 1 thread; 0.19 s on 8 threads | header only (lazy `Image.open` + EXIF), pixels never decoded. Warm local files are GIL-bound, so 8 threads lose here; the import job's 8 threads pay off on remote / cold storage, where each probe waits on the network (64 KB Range GET on S3). Listing 1000 files: 4 ms (`os.scandir`). |
| Thumbnails 160 px from 1920×1080 JPEGs | **4.4 ms each** (229/s on 4 threads) | `draft()` decodes the JPEG at 1/8 scale first. |
| Zip export: 1000 images + 2 tasks × 1000 label files | **2.5 s, 637 MB/s** (3000 entries, 1.57 GB zip) | images `ZIP_STORED` (no re-deflating JPEGs), text `ZIP_DEFLATED`. |
| Local-folder export, same content | **1.5 s, 1.08 GB/s** | `shutil.copyfile` (kernel copy) + tmp → `os.replace`. |
| PostgreSQL upsert 100k annotations (+ 5k media, 5k statuses) | **6.5 s first export, 8.5 s re-export** (budget 60 s) | `INSERT … ON CONFLICT DO UPDATE` executemany in 1000-row batches; psycopg 3 sends them in pipeline mode, so the time is mostly server-side. Per-row client work (`_row_converter`) ≈ 1.2 s of it. |
| MSSQL | not measured | SQL Server 2022 crashes at startup in this host's docker (`RETAIL ASSERT drtl.cpp:1552`, errno 13, even with `--privileged`), so `tests/test_db.py` skips its MSSQL cases with that reason. The MSSQL path (`#stage` + `MERGE`, `N''` literals, DATETIMEOFFSET strings) was checked offline against the compiled SQL only. |

I/O implementation notes:
- FTP / SFTP use a checkout pool (FTP 4, SFTP 8 connections), not one client per thread: the import and prefetch pools
  come and go, and a pool caps the number of server sessions. A connection that raised is dropped and the call is
  retried twice on a fresh one. `close()` drops connections; a later call reconnects lazily.
- FTP `read_head` closes the data socket after 64 KB and swallows the expected `426`; `NLST` fallback (servers
  without `MLSD`) uses `SIZE` to tell files from folders and switches back to binary mode first (`SIZE` is refused in ASCII mode).
- SFTP host keys are not pinned (`AutoAddPolicy`): the connection is set up by the server's owner in Settings.
- pymssql has no `encrypt` switch: FreeTDS negotiates TLS itself when the server requires it.
- `ImageCache.thumb(media_id, rel, size)` with a size other than 160 writes `thumbs/<media_id>_<size>.jpg`.
- `probe()` returns `size_bytes` from the listing for remote sources (None if `list_images()` was not called).

## Measured (label page, headless Chromium, 2026-10-08)
| Area | Budget | Measured |
|---|---|---|
| pointermove + crosshair, 500 shapes | < 4 ms script / frame, 60 fps | handler p95 0.4 ms, max 0.7 ms; frames at 16.7 ms |
| drag a box among 500 shapes | < 8 ms / frame | 60 fps (16.7 ms between frames, max 16.8) |
| image switch, prefetched | < 50 ms | 14-21 ms typical; **~60 ms on an image with 500 shapes** (the canvas draw of 500 shapes; the objects list renders 60 rows, the rest on scroll) |
| heap after 200 switches | flat | 10 MB -> 10 MB |
| long tasks > 50 ms | none | none in a 10-switch run including the 500-shape image; one 52-57 ms task in some runs (same cause) |

End-to-end flow (§10.3 steps 1-7) scripted in Playwright: 14/14 checks, 0 page errors.

## v2 contract (model-assisted labeling, export class filter, duplicate, bulk class delete)
Ann gains nothing new: `score` (float|null) and `source` ('manual'|'model'|'import') already travel in every Ann.
A model shape the user moves / resizes / reclassifies becomes `source:'manual', score:null` (the page does it in the
edit command; `put_annotations` also enforces it when a 'model' row arrives with a changed geometry or class_id).

Task config gains `model`: `{connection_id, model_id, version?:str, conf:0.25, auto_on_open:false,
class_map:{<model class name>: <class_id> | 'ignore'}}` (absent / null = no model linked).
Media gains `model_run:{<task_id>: epoch|null}` (media_task_status.model_run_at: the model already ran on it).

Connection kind `model`: `{kind:'model', name, url, api_key?(secret), timeout_s:60}`. `POST /api/connections/test`
for it returns the detect result below.

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/label/models/detect` | `{connection_id}` or an unsaved `{connection:{url, api_key?}}` | `{protocol:'serving'\|'generic', models:[{id, name, task_type, geometry, classes:[str], versions:[str], keypoints?:[str]}]}`; 400 `{detail}` when not a supported API |
| PUT | `/api/label/projects/{pid}/tasks/{tid}/model` | `{connection_id, model_id, version?, conf, auto_on_open, class_map:{name: class_id\|'ignore'\|'new'}}` or `null` (unlink) | `Task` (`'new'` entries are created as classes and replaced by their ids; 400 when the model type does not fit the task) |
| POST | `/api/label/projects/{pid}/predict/{mid}` | `{task_id, conf?}` | `{version:int, annotations:[Ann] (the task's full live set on that image), n_model:int, ms:float}` |
| POST | `/api/label/projects/{pid}/predict-all` | `{task_id, scope:'unlabeled'\|'not_done'\|'all', conf?}` | `{job_id}` (lane 'model'; job.result `{n_images, n_shapes, failed, cancelled}`) |
| POST | `/api/jobs/{jid}/cancel` | | `{ok:true}` (the job stops between batches, keeps what it wrote, ends with status 'done' + result.cancelled) |
| POST | `/api/label/projects/{pid}/remove-model-labels` | `{task_id, media_ids?:[mid]}` (omitted = every image) | `{removed:int, images:int}` (versions bumped) |
| GET | `/api/label/projects/{pid}/class-stats` | | `{stats:{<class_id>: {n_annotations, n_images}}}` |
| POST | `/api/label/projects/{pid}/classes/delete` | `{class_ids:[cid]}` | `{deleted:int, annotations:int, images:int}`; all or nothing |
| POST | `/api/label/projects/{pid}/duplicate` | `{name?}` | `{job_id}` (job.result = the new Project card; every id is new) |

Export request gains `classes:{<task_id>: [class_id]}` (a task not listed keeps all its classes). Kept classes are
renumbered 0..N-1 in position order; classes.txt / data.yaml list only them; manifest.json gets
`excluded_classes:{<task slug>: [names]}`. `export_default` stores `{target, task_ids, classes, include_images,
only_done}` so a re-export repeats it.

### v2 contract notes (as built)
- Becomes manual: forced only when the **stored** row is `model` and its geometry / class changed; a row sent back
  as `model` (the page's undo of an edit) is kept.
- Serving models are always `task_type:'object_detection'` (the serving API only returns boxes); the route's type
  segment is in `route_type` ('obd', 'seg', ...). A model link whose class_map names a class the task no longer has
  saves it as `'ignore'` (deleting a class also rewrites its class_map entries to `'ignore'`).
- Server-side deletes (model re-run, remove model labels, class delete) unlink children (`parent_id` → null); a class
  delete returns emptied `in_progress` images to `todo`. predict-all re-checks the scope at write time.
- DB export with a class filter soft-deletes the task's remote rows of excluded classes, keeps the parent boxes of kept
  keypoints, and writes the filtered `n_annotations`.
- Detect results for serving models carry `imgsz`; `POST /api/connections/test` (kind model) → `{ok, protocol, models, ms}`.
- Jobs: `cancelled` (bool) in the job JSON; `result` is public for predict-all (`{n_images, n_shapes, failed,
  cancelled, seconds}`) and duplicate (the new card). A job cancelled while queued ends `done` + `result.cancelled`.
- predict-all scopes: `unlabeled` = no shapes in the task and not done / skipped; `not_done` = any status but done;
  `all` = every image. It stops with an error when the first 3 batches all fail.
- Classification tasks: at most one model tag per image (the top score), none when the user already picked a tag.
- Errors: model server failure → 502; bad API key / unknown model → 400.
- `export_default` is saved by the server on every export (`{...target fields, target, task_ids, classes,
  include_images, only_done}`). Connections used by a model link or the saved export target cannot be deleted.

### Measured (v2 backend)
| What | Result |
|---|---|
| predict-all, fake serving server (`/predict/batch`, 640×480) | ~771 img/s (200), ~822 img/s (1000) |
| predict-all, fake generic server (single calls, 4 in flight) | ~246 img/s (200), ~238 img/s (1000) |
| predict one image over HTTP | median 5.8 ms, p95 8-17 ms |
| duplicate 1000 images + 10k annotations + thumbs | 0.29-0.31 s |
| **real `serving/`** (yolo11n-640, MIG GPU): one 1280×720 image | 12 boxes, 37 ms through the app |
| **real `serving/`**: predict-all 20 images | 152 shapes, 0 failed, 1.64 s |
