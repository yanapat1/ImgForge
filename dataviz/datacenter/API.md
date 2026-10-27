# Data Center: API contract

This is the shared contract between `dataviz/datacenter/` (the backend), `dataviz/llm/tools.py` (the AI tools) and `static/ts/datacenter/` (the page). If you change it, change all three sides.

## Concepts
- **Bucket**: one root of data. Each bucket is private to its owner, and admins see every bucket. There are three kinds:
  - `managed`: a folder under `storage.data_root` (config; default `data/` under the project root, always allowed). Imports land here.
  - `local`: a linked folder anywhere inside `storage.allowed_roots`. It is used in place.
  - `s3`: a linked bucket and prefix that goes through one of the owner's S3 connections. It is **read-only** in v1: you can list, view, detect, tag and send it, but not change it.
- **Path**: relative to the bucket root, POSIX, no leading slash; `''` is the root. Every resolved local path is checked with `connections.check_local`; `..`, absolute paths and symlinks that escape are rejected with 400.
- **Tags**: each one is on `(bucket, path)`, where `path=''` is the bucket itself. Tags are `{tag, auto}`. **Auto** tags are written only by format detection, as `format:<key>`. Users cannot remove them, and they are swapped when the format changes. User tags are cleaned with `core/names.clean_tags`.

## Format result (`formats.detect`)
```json
{"format": "yolo-detect", "label": "YOLO detect", "confidence": 0.98,
 "checks": [{"name": "images/", "ok": true, "detail": "train · val · test", "count": 12480}],
 "classes": 9, "reason": "images/ + labels/ mirror, data.yaml resolves, 5-column label lines"}
```
- `format` is one of `yolo-detect | yolo-seg | yolo-pose | yolo-obb | coco | voc | classification | images | embeddings | other | empty`.
- Auto tag = `format:<format>`.
- If a change turns a recognised format into another one, the op response carries `"retagged": {"path": "", "from": "yolo-detect", "to": "other", "reason": "labels/ missing"}`.

## Python service: `dataviz/datacenter/service.py`
Every function takes `user` (the acting user dict) first, raises `DCError(message, status)` on failure, and returns JSON-able dicts. Both the routes and the LLM tools call **only** these functions.

```
list_buckets(user, q='', tags=(), formats=())                  -> [Bucket]
get_bucket(user, bid)                                          -> Bucket
create_bucket(user, name, kind='managed', path=None, connection_id=None, s3_bucket=None, prefix='', tags=())
update_bucket(user, bid, name=None)                            -> Bucket
delete_bucket(user, bid, delete_files=False)                   -> {ok}
summary(user)                                                  -> {buckets, images, size, managed: {used, free}, links: {local, s3}}
ls(user, bid, path='', offset=0, limit=500, sort='name'|'-mtime'|'size', q='')
    -> {path, total, entries: [Entry], format: Format|None, tags: [Tag], writable}
read_file(user, bid, path, w=None) -> (bytes, media_type)      # w: scaled JPEG thumbnail (cached), else the raw file
read_text(user, bid, path, limit=65536) -> {text, truncated}
ops(user, bid, ops) -> {applied: [..], errors: [{op, error}], undo_id, retagged: {...}|None}
    op: {op:'mkdir', path} | {op:'move', path, to} | {op:'copy', path, to} | {op:'rename', path, name} | {op:'delete', path}
    `to` is a destination folder; delete moves into `<data_root>/.trash/<undo_id>/`, and undo restores it.
undo(user, bid, undo_id) -> {restored, errors, retagged}
upload(user, bid, path, files: [(name, fileobj)], unzip=True) -> {added, skipped, retagged}
set_tags(user, bid, path='', add=(), remove=()) -> [Tag]
all_tags(user) -> [{tag, count, auto}]
detect(user, bid, path='', force=False) -> Format
stats(user, bid, path='') -> {images, labels, boxes, classes, size, splits: {train: n, ...}}
search(user, q='', tags=(), formats=(), bid=None, path='', glob=None, limit=200)
    -> {buckets: [Bucket], files: [{bucket, path, name, type, size}]}   # q matches names (case-insensitive substring); glob like '*night*.jpg'
send(user, bid, paths, target='visualize'|'label', name=None, task_type='detect')
    -> {target, id, url}   # creates a visualize session (dataset upsert) or a label project (source {kind:'local', path})
```

`Bucket = {id, name, kind, root, writable, owner, created_at, updated_at, format, tags, stats: {images, labels, classes, size}, sessions: [{id, kind:'visualize'|'label', name, url}]}`
`Entry = {name, path, type: 'dir'|'file', size, mtime, kind: 'image'|'label'|'yaml'|'json'|'xml'|'text'|'npy'|'archive'|'other', items?: n (dir), w?, h?, labels?: n (an image with a YOLO label file)}`
`Tag = {tag, auto}`

## HTTP: `dataviz/server/routes/datacenter.py`
All routes are under `/api/dc`. JSON goes in and out; errors are `{detail}`. Permission: the new tier permission `data.manage` is needed to create buckets, run ops, upload, tag and send. Reading needs only bucket access.

| method | path | maps to |
|---|---|---|
| GET | `/api/dc/summary` | summary |
| GET | `/api/dc/buckets?q=&tag=a&tag=b&format=` | list_buckets |
| POST | `/api/dc/buckets` | create_bucket |
| GET / PATCH / DELETE | `/api/dc/buckets/{bid}` | get / update / delete (`?delete_files=1`) |
| GET | `/api/dc/buckets/{bid}/ls?path=&offset=&limit=&sort=&q=` | ls |
| GET | `/api/dc/buckets/{bid}/file?path=&w=` | read_file (Cache-Control private, max-age=300) |
| GET | `/api/dc/buckets/{bid}/text?path=` | read_text |
| POST | `/api/dc/buckets/{bid}/ops` `{ops}` | ops |
| POST | `/api/dc/buckets/{bid}/undo` `{undo_id}` | undo |
| POST | `/api/dc/buckets/{bid}/upload?path=` multipart `files` | upload |
| PUT | `/api/dc/buckets/{bid}/tags` `{path, add, remove}` | set_tags |
| GET | `/api/dc/buckets/{bid}/detect?path=&force=` | detect |
| GET | `/api/dc/buckets/{bid}/stats?path=` | stats |
| GET | `/api/dc/tags` | all_tags |
| GET | `/api/dc/search?q=&tag=&format=&bucket=&path=&glob=` | search |
| POST | `/api/dc/send` `{bucket, paths, target, name, task_type}` | send |

## AI assistant: `/api/llm/dc/*` (`dataviz/llm/agent.py`, `tools.py`)
`POST /api/llm/dc/chat` `{messages: [{role, text}], scope: {bucket, path, selection: [paths]}}` streams NDJSON, one event per line:
- `{"type":"meta","model":..,"in_est":..}`
- `{"type":"delta","text":..}`: assistant text.
- `{"type":"tool","id":..,"name":..,"args":{..},"summary":"→ 38 matches","ok":true}`: a read-only tool the agent already ran.
- `{"type":"plan","plan_id":..,"actions":[{"id":..,"tool":..,"args":{..},"summary":".."}]}`: mutating tools, waiting for the user.
- `{"type":"ask","question":..,"options":[..]}`: a clarifying question, sent instead of guessing.
- `{"type":"cannot","reason":..,"alternative":..|null}`
- `{"type":"done","usage":{..}}` / `{"type":"error","detail":..}`

`POST /api/llm/dc/confirm` `{plan_id, action_ids}` runs only the checked actions (in order, through `service`) and returns `{results: [{id, ok, summary, error?}], retagged: [..]}`. A plan expires after 15 min and belongs to its user.
