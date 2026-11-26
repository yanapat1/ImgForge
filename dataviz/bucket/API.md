# Bucket: API contract

This is the shared contract between `dataviz/bucket/` (the backend), `dataviz/llm/tools.py` (the AI tools) and `static/ts/bucket/` (the page). If you change it, change all three sides.

## Concepts
- **Bucket**: one root of data. Each bucket is private to its owner, and admins see every bucket. There are three kinds:
  - `managed`: a folder under `storage.data_root` (config; default `data/` under the project root, always allowed). Imports land here.
  - `local`: a linked folder anywhere inside `storage.allowed_roots`. It is used in place.
  - `s3`: a linked bucket and prefix that goes through one of the owner's S3 connections. It has a load `mode`:
    - `stream` (default): read from S3 on every request. It is **read-only**: you can list, view, detect, tag and send it, but not change it. Linking returns at once: a bucket-lane job `bk-scan:<bid>:` lists the objects (in parallel, `s3list.py`) and detects the format, with the object count as progress and Cancel. `ls` never lists the whole prefix inline: a stale detection (older than `S3_TTL`, 1 h) is returned as it is while a scan job refreshes it, and `format` is null until the first scan is done. A scan that failed or was cancelled is not queued again by `ls` for `SCAN_RETRY` (5 min), and a failed / cancelled check of the downloaded folders not for `CHECK_TTL` (10 min): the page polls `ls` every second while a folder lists, and must not re-list S3 on each poll.
    - `local`: a pull job downloads every object into `local_root` (`<data_root>/.s3/<name>-<id>`). Once the first pull has finished (`synced_at`), the bucket reads and writes that copy, so it is editable, undoable and exportable like a local bucket. Its `kind` stays `s3` and `root` is the copy.
    - Re-sync (`sync`) downloads again. An object replaces the local file with the same name, unless that file is unchanged (same size, and the mtime the pull set to the object's LastModified), which is skipped; files only the copy has are kept, and nothing is deleted. Downloads start while the listing still runs (`extra` shows `listing… n found`; `total` is set once the listing ends); objects of 64 MB or more are fetched in parallel ranges.
    - Switching back to `stream` keeps the copy on disk.
    - **Per-folder local copy** (stream mode): `fetch_local` downloads only the picked folders / files into `local_root` (table `app.dc_s3_local`, one row per downloaded path, `''` = all after a full pull). The bucket stays streamed and read-only **outside** the downloaded folders. A downloaded folder is **edited in place**: `ls` lists it from the copy (`writable: true`, entries `local: fresh`), and `ops`, `undo`, `write_text`, `upload` / `upload_part`, `convert_vectors` work inside it on the copy only (`_write_view` → `_LocalView`, root = `local_root`; S3 is written only by `push`). Refused: a source equal to the downloaded folder itself (use `drop_local`), a destination in a folder that is not downloaded, any write while a `bk-fetch` job runs (409). A change clears `checked_at`, so the next `ls` queues a comparison with S3. Reads of a downloaded file come from disk. Elsewhere `ls` gives each entry `local` (`fresh | outdated | new | gone | partial | fetching | error`, null = stream only), `here` (the row holding the folder) and `local_folders`. A `bk-check:<bid>` job compares the rows with S3 every `CHECK_TTL` (10 min) while browsing (`check_local` runs it at once).
    - **Progressive listing** (`s3dir.py`): an S3 folder is listed in the background. `ls` answers after `LS_WAIT` (0.8 s) with what it has (`partial: true`, `loaded`) and later pages come from the same listing. At most 4 folders list at once, and 16 listings / 1M entries are kept 60 s.
    - **Offline**: an unreachable endpoint (connect / read timeout, refused) opens a 30 s circuit breaker per connection. `ls` then lists only the downloaded folders (`offline: true`, `error`), other folders give 503, and `ls?retry=1` asks again at once. `ls?refresh=1` (the Reload button) drops the kept listing of that folder and lists S3 again. Every `ls` answers `location` (where it is read from: `s3://bucket/prefix/path` or a local path) and, for an S3 link, `s3_url`; buckets carry `location` too. `folder_state(user, bid, path)` gives the assistant {location, s3_url, mode, writable, local, downloaded_in, editable}. Interactive reads share one client per connection (3 s connect timeout, 2 attempts).
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

## Python service: `dataviz/bucket/service.py`
Every function takes `user` (the acting user dict) first, raises `DCError(message, status)` on failure, and returns JSON-able dicts. Both the routes and the LLM tools call **only** these functions.

```
list_buckets(user, q='', tags=(), formats=())                  -> [Bucket]
get_bucket(user, bid)                                          -> Bucket
create_bucket(user, name, kind='managed', path=None, connection_id=None, s3_bucket=None, prefix='', tags=(), mode='stream')
    -> Bucket (+ job_id when an s3 link starts with mode='local'; + scan_job_id, the bk-scan job, for a streamed one)
update_bucket(user, bid, name=None, mode=None)                 -> Bucket (+ job_id when switched to local)
fetch_local(user, bid, paths, mirror=False)                    -> {job_id, paths, result}   # stream: download folders (again = update)
check_local(user, bid)                                         -> {job_id}   # compare the downloaded folders with S3
session_check(user, bid, path='')                             -> {format, label, images, labels, classes, streamed, can: {visualize, label, linked}, label_import, note, convert_to, default} | {scanning: True, job_id}   # what a session from this folder can do; a streamed S3 folder not read yet: its scan job (send / create_session / reformat / source answer 409 meanwhile)
create_session(user, bid, path='', visualize=True, label=True, name=None, task_type='detect') -> {name, linked, visualize?: {id, url, job_id?}, label?: {id, url, job_id, import_job_id?}}   # both = linked: the labeling project owns the labels (the folder's are imported), Visualize is a `labeling` dataset on it
reformat(user, bid, path='', to='coco')                        -> {job_id, bucket, bucket_name, path}   # a new folder <path>-<to> of the managed bucket "<name> · formats": labels re-written (labeling writers); local: images hard-linked; streamed S3: labels + images.txt + source.json only (session_check format s3-labels: Visualize as an S3 session with labels_dir)
job_status(user, job_id, wait_s=0)                             -> {id, label, state, stage, pct, eta_s, elapsed_s, error?, result?}   # one of the caller's own jobs; wait_s <= 60 waits for it
compare_cloud(user, bid, path='', verify=False)                -> {job_id, path}   # local copy vs S3, file by file
compare_result(user, bid, path='', status=None, q='', offset=0, limit=200)   -> {ready, running, counts, rows ...}
    # status: added_local deleted_local edited_local both new_cloud changed_cloud removed_cloud same (cloudiff.py)
drop_local(user, bid, paths)                                   -> {removed}  # delete their local copy
sync(user, bid)                                                -> {job_id, result}   # s3 + mode local: (re)pull
    # result {downloaded, skipped (unchanged), bytes, failed: [{path, error}] (first 50), total}
export(user, bid, path='')                                     -> (zip name, chunk iterator)
    # a folder (or one file) as a stored zip, names relative to that folder (a file: its own name). Local files or a
    # streamed s3 link alike (objects are read one by one while the zip streams). Hidden names and the trash are left
    # out, .env files unless bucket.manage; symlinks and files that cannot be read are skipped (never the stream);
    # pre-1980 / 0 mtimes are clamped. The download's Content-Disposition carries filename*=UTF-8'' (Thai names).
export_check(user, bid, path='')                               -> {name, kind: 'file'|'dir'}
    # only the checks export runs before streaming (bucket access, path, .env, S3 reachable): the page calls it
    # first and toasts the error instead of starting a download that fails
export_to(user, bid, path='', target={kind, connection_id, bucket?, prefix?, path?}, create_bucket=False, overwrite=False)
    -> {job_id, result}   # result (the job's, or inline from the CLI): {files, bytes, uri, created, cancelled}
    # bucket.manage. Copies a folder (or file) to S3 / FTP / SFTP through one of the CALLER's own connections
    # (labeling/sinks.make_sink). Layout: destination = prefix (S3) or path (FTP / SFTP) + the path RELATIVE TO THE
    # BUCKET ROOT, so data/image/im1.png exported with bucket Dataset, prefix 'dev/v1/new folder/' lands at
    # s3://Dataset/dev/v1/new folder/data/image/im1.png whether data/, data/image/ or the whole bucket is exported.
    # Same filtering as export. create_bucket (S3): the bucket is made when missing (sinks.ensure_bucket; S3 naming
    # rules, else 400). A non-empty destination without overwrite, a missing bucket, an unreachable target: 400
    # before the job starts (S3 unreadable for export_check / export: 502);
    # a second export of the same bucket while one runs: 409. A bucket-lane job bk-export:<bid>, cancellable (stops
    # queueing files; what is already uploaded stays).
source(user, bid, path='')                                     -> {source, format, classes, bucket, path, name}
    # the POST /api/label/projects `source` of a bucket folder (New label › Bucket), classes from its data.yaml
autolabel(user, bid, path='', model={registry_id | connection_id + model_id, version}, target='inplace'|'copy',
          new_name=None, conf=None, write_yolo=True, name=None, formats=['yolo']) -> {job_id, bucket, result}
    # one model-lane job:
    #   1. copy: into the new managed bucket new_name (a streamed s3 bucket can only be copied)
    #   2. make a labeling session; its classes are the model's
    #   3. link + activate the model, then label all
    #   4. write each of `formats` (labeling/writers; the ones the model's task cannot use are skipped and listed in
    #      formats_skipped). yolo / yolo_obb: loose images move into images/ first (in place: through ops, so
    #      undoable), then labels/*.txt, data.yaml and classes.txt. Layouts, relative to the picked folder:
    #        yolo       labels/ mirroring images/, data.yaml, classes.txt (detect / seg / pose / cls)
    #        yolo_obb   labels/ (labels-obb/ when yolo is picked too): cls x1 y1 .. x4 y4
    #        coco       annotations/instances_<split>.json (train / val / test from the path, else default)
    #        voc        Annotations/<stem>.xml          (sub-folders below images/ kept)
    #        labelme    labelme/<stem>.json, imagePath relative to the json
    #        createml   createml.json
    #        csv        labels.csv, one row per shape
    #        imagefolder imagefolder/<class>/<image> (copies; classification)
    #      write_yolo=False (the older flag) = formats=[] (label the session only)
    # job result: {bucket, session: {id, name, url}, labeled, written: {labels, classes, data_yaml?, formats: {key:
    #   {files, n_media, ...}}}, copied, restructured: {moved, undo_id}, formats_skipped?}
convert_vectors(user, bid, path='', to='parquet'|'csv'|'npy') -> {job_id, path, result}
    # an embeddings folder (meta.csv + .npy, embeddings.parquet or embeddings.csv) into another layout, in a new
    # sibling folder <name>-<to> (labeling/writers/vector.py); a bucket-lane job, key bk-convert:<bid>
video_info(user, bid, path) -> {path, w, h, fps, frames, duration, codec, rotation, size, decoder}
    # w / h as shown (rotation applied); a video streamed from S3 only {path, size, streamed: true}. PyAV probe
    # (bucket/video.py), OpenCV fallback (decoder 'opencv'), cached per path + mtime + size
estimate_frames(user, bid, paths, opts) -> {frames, approx, bytes, times: [≤ 8 s], videos: [...], describe, limit}
    # what extract_frames would write: the same pickers walk the nominal frame grid (keyframes: the packet index),
    # scene / smart are guesses (approx); S3 streamed: unknown (frames null)
extract_frames(user, bid, paths, opts=None, target='inplace'|'copy', new_name=None, out=None, remember=False)
                                                               -> {job_id, bucket, path, estimate, result}
    # videos (files, or folders: the videos directly inside) cut into image frames. opts = video.options:
    #   which:  fps (shorthand) | mode all | every (n) | pattern (every_s, window_s, take, pick spread|consecutive)
    #           | count (n) | keyframes | scene (threshold) | smart (n: farthest-point picks, 2 passes)
    #   range:  start, end (s)      skip: dedupe 0..0.5 (mean abs diff of a 64×36 gray vs the last written)
    #   output: format jpg|png|webp, quality, max_side, layout yolo|yolo-split|per-video|flat, split, block_s,
    #           max_frames (≤ 200 000; a non-approx estimate over the hard limit is refused up front)
    # into a new folder `out` (default <video>-frames; several videos <folder>-frames; a free name) next to the
    # first video, or at the root of a new managed bucket new_name (target copy: the only way for a streamed S3
    # bucket, each video downloaded first). Written as .frames-<hex>/ and renamed when complete (removed on error or
    # cancel, the copy bucket too). yolo*: images/ (+ train|val|test by time block), empty labels/, data.yaml with
    # nc: 0; every layout: frames.csv (file, video, frame, time_s, split). A bucket-lane job bk-frames:<bid>:<path>.
    # result {bucket, path, frames, bytes, videos: [{path, frames, skipped_dupes, errors, decoder, duration}],
    #   layout, format, describe, capped, splits?, retagged}. remember=True also runs set_video_auto(opts)
set_video_auto(user, bid, opts|None) -> {video_auto}
    # the bucket's auto-extract (dc_buckets.video_auto, migration 0010): upload(), import_source and sync() start one
    # extract_frames job per new video with these options; their result carries frames: {jobs, errors}. None = off
read_file(user, bid, path, w=None, t=None)                     # a video with w: its frame at t s (a poster when None)
list_sources(user)                                             -> {sources: [{key, label, needs, token, fields, examples, help, connections}]}
inspect_source(user, source, ref, connection_id=None, opts={}) -> {title, size, files, license, versions, name, ref, ...}
import_source(user, source, ref, name=None, connection_id=None, opts={}, bucket=None, path='') -> {job_id, bucket, result}
    # source: kaggle | huggingface | roboflow | url | zenodo | openml (bucket/sources). Into a new managed bucket
    # (named from the ref) or folder `path` of a writable `bucket`. opts: the source's fields (version, revision,
    # patterns, format) + extract (default true), nested, keep_archive. A bucket-lane job bk-import:<bid>: download
    # (SSRF-guarded, resumable, ranged parallel parts), unpack (zip / tar.* / gz / 7z with py7zr), detect the format.
    # A new bucket whose import fails or is cancelled is removed; while it runs the bucket is busy (409 on writes).
    # The token comes from the picked connection, else the caller's first one of that kind (kaggle / huggingface /
    # roboflow / zenodo connections: Settings › Connections, encrypted).
reimport(user, bid)                                            -> {job_id, bucket}   # the last import again
delete_bucket(user, bid, delete_files=False)                   -> {ok}
summary(user)                                                  -> {buckets, images, size, managed: {used, free}, links: {local, s3}}
ls(user, bid, path='', offset=0, limit=500, sort='name'|'-mtime'|'size', q='', retry=False)   # s3 stream: + partial, loaded, here, offline; entries + local
    -> {path, total, entries: [Entry], format: Format|None, tags: [Tag], writable}
read_file(user, bid, path, w=None) -> (bytes, media_type)      # w: scaled JPEG thumbnail (cached), else the raw file
read_text(user, bid, path, limit=65536) -> {text, truncated, size, mtime_ns, encoding, newline, editable, reason?}
    # limit up to 8 MB; text has '\n' line ends, no BOM (encoding 'utf-8-sig' when the file has one), newline is the
    # file's own style; mtime_ns is a string (local files; None on S3). editable is false (with a reason) when
    # truncated, not strict UTF-8, read-only bucket or no bucket.manage. .env files need bucket.manage to read at all.
write_text(user, bid, path, text, expect_mtime_ns=None, create=False) -> {path, size, mtime_ns, undo_id, created, retagged}
    # 409 when the file's mtime is not expect_mtime_ns (changed since it was read; None overwrites); keeps the mode,
    # BOM and newline style; the old content goes to the trash (undo step 'replace'), a new file undoes as 'remove'
ops(user, bid, ops) -> {applied: [..], errors: [{op, error}], undo_id, retagged: {...}|None}
    op: {op:'mkdir', path} | {op:'create', path} (empty file) | {op:'move', path, to} | {op:'copy', path, to} | {op:'rename', path, name} | {op:'delete', path}
    `to` is a destination folder; delete moves into `<data_root>/.trash/<undo_id>/`, and undo restores it.
session_info(user, did) -> {bucket, bucket_name, path, format, label, task_type, formats:[{key,label}], dest:{bucket, path, name}, streamed, can_export, can_label, note}
    # GET /api/bucket/session-folder/{did}: the bucket folder a Visualize session reads (locate: path / S3 prefix; the
    # owner only; 409 for a labeling / combined session or a folder outside the user's buckets)
export_session(user, did, fmt='coco', dest_bucket=None, dest_path=None) -> {job_id, bucket, bucket_name, path}
    # POST …/session-folder/{did}/export {format, to_bucket?, to_path?}: reformat() of that folder into any bucket
    # (default <folder>/exports/<name>-<fmt>; a new or empty folder). A streamed S3 destination: staged under
    # <data_root>/.export-staging, uploaded to <prefix>/<path> (images of a streamed source on the same connection:
    # copy_object), the staging removed. Job key bk-reformat:<src>:<path>:<fmt>:<dest>:<dest path>.
session_to_label(user, did, name=None, task_type=None) -> send(label) + the label-import job (import_job_id)
    # POST …/session-folder/{did}/label {name, task_type}: a labeling project on the SAME folder (in place)
push(user, bid, paths, overwrite=()) -> {job_id, result}        # POST …/push {paths, overwrite}: Upload to S3
    # the local copy of an S3 link (downloaded folders; mode local: all) back to <prefix>/<path>: job bk-push:<bid>:<hash8>.
    # One S3 listing per path, then per file: unchanged vs the manifest -> skipped; S3 changed since the download (or an
    # object never downloaded) -> conflicts[] unless in overwrite; else put_object (< 16 MB) / upload_file multipart,
    # 16 at a time, bytes progress. An uploaded file gets S3's LastModified as mtime + its manifest row (fresh) and loses
    # its change mark. result {uploaded, bytes, unchanged, conflicts:[{path, local_size, s3_size, s3_modified}],
    # deleted_here, failed:[{path, error}], cancelled}. Not an S3 link / outside the downloaded folders: 400.
list_changes(user, bid, prefix='', offset=0, limit=200) -> {total, items: [{path, kind, source, at}]}   # GET …/changes
clear_changes(user, bid, paths=(), prefix=None) -> {cleared}      # POST …/changes/clear {paths} | {prefix}: Mark as seen
    # bucket/changes.py, table app.bucket_changes (one row per path): kind edited|created|copied|moved|deleted|uploaded|
    # external, source bucket|visual|label|external. ls adds entries[].changed {kind, source, at, n, self} (one range
    # scan per page) and `changes` (the total under the folder). Written by ops / undo (clears) / write_text / upload /
    # copy_to, image/sources.py WRITE_HOOKS (Visualize's label writes, in a background thread), the bk-check job
    # (external: a downloaded file that left the manifest); a download (fetch) clears the files it replaced.
copy_to(user, src_bid, paths, dst_bid, dst_rel='') -> {job_id, result}     # POST /api/bucket/copy {from_bucket, paths, to_bucket, to_path}
    # job bk-copy:<dst>:<hash8> (lane bucket; the same request twice: one job). Any source (local, downloaded, streamed
    # S3: objects read in 1 MB chunks), 8 files at a time, each through <name>.part then os.replace; progress in bytes
    # (ETA), cancel between files. A clashing top-level name -> "name (copy)". result: {files, bytes, to, undo_id,
    # bucket, cancelled}; undo_id (on the destination) removes the copies. Into itself: 400; destination not writable
    # (a streamed link outside its downloaded folders): 400.
undo(user, bid, undo_id) -> {restored, errors, retagged}
upload(user, bid, path, files: [(name, fileobj)], unzip=True) -> {added, skipped, retagged}   # zip / tar / tar.gz / tar.bz2 / tar.xz / 7z unpacked
upload_part(user, bid, path, name, uid, offset, total, data: bytes, unzip=True) -> {received, ranges} | {added, skipped, retagged, done: True}
    # one chunk of a big file (the page sends files over its chunk size this way, so a proxy that refuses large
    # requests still lets them through). Chunks may come in any order, in parallel and more than once: written in
    # place into the hidden .upload-<uid>.part of the folder, the ranges held in .upload-<uid>.json beside it; the
    # file is placed like upload() once all `total` bytes are in (exactly once). A name upload() would skip (dot
    # part, file already there) answers done before any bytes are kept. Parts left for a day are removed.
    # The page's uid: the first 32 hex of SHA-256(bid|path|name|size|lastModified), so a re-upload resumes.
part_status(user, bid, path, name, uid, total, unzip=True) -> {received, ranges} | {skipped: 1, done: True}
set_tags(user, bid, path='', add=(), remove=()) -> [Tag]
all_tags(user) -> [{tag, count, auto}]
detect(user, bid, path='', force=False) -> Format
stats(user, bid, path='') -> {images, labels, boxes, classes, size, splits: {train: n, ...}}
search(user, q='', tags=(), formats=(), bid=None, path='', glob=None, limit=200)
    -> {buckets: [Bucket], files: [{bucket, path, name, type, size}]}   # q matches names (case-insensitive substring); glob like '*night*.jpg'
send(user, bid, paths, target='visualize'|'label', name=None, task_type='detect')
    -> {target, id, url}   # creates a visualize session (dataset upsert) or a label project (source {kind:'local', path})
```

`Bucket = {id, name, kind, root, writable, owner, created_at, updated_at, format, tags, stats: {images, labels, classes, size}, sessions: [{id, kind:'visualize'|'label', name, url}]}`. An s3 bucket also has `{connection_id, s3_bucket, prefix, mode, local_ready, synced_at, s3_root}`. A bucket imported from the web has `source: {kind, ref, imported_at, title, version, importing}`.
`Entry = {name, path, type: 'dir'|'file', size, mtime, kind: 'image'|'label'|'yaml'|'json'|'xml'|'text'|'code'|'table'|'audio'|'video'|'npy'|'archive'|'other', items?: n (dir), w?, h?, labels?: n (an image with a YOLO label file)}`
`Tag = {tag, auto}`

Dot names are hidden everywhere (listing, search, export, S3) except config files: `.env`, `.env.*`, `.gitignore`, `.gitattributes`, `.editorconfig`, `.dockerignore`, `.npmrc`, `.prettierrc*`, `.eslintrc*` (store.visible). Dot folders, the trash, `.s3`, `.upload-*` and `*.part-*` stay hidden.

## HTTP: `dataviz/server/routes/bucket.py`
All routes are under `/api/bucket`. JSON goes in and out; errors are `{detail}`. Permission: the new tier permission `bucket.manage` is needed to create buckets, run ops, upload, tag and send. Reading needs only bucket access.

| method | path | maps to |
|---|---|---|
| GET | `/api/bucket/summary` | summary |
| GET | `/api/bucket/buckets?q=&tag=a&tag=b&format=` | list_buckets |
| POST | `/api/bucket/buckets` | create_bucket |
| GET / PATCH / DELETE | `/api/bucket/buckets/{bid}` | get / update / delete (`?delete_files=1`) |
| GET | `/api/bucket/buckets/{bid}/ls?path=&offset=&limit=&sort=&q=` | ls |
| GET | `/api/bucket/buckets/{bid}/file?path=&w=&t=` | read_file (Cache-Control private, max-age=300; a video with w: its frame at t s) |
| GET | `/api/bucket/buckets/{bid}/text?path=&limit=` | read_text |
| PUT | `/api/bucket/buckets/{bid}/text` `{path, text, expect_mtime_ns, create}` | write_text |
| POST | `/api/bucket/buckets/{bid}/ops` `{ops}` | ops |
| POST | `/api/bucket/buckets/{bid}/undo` `{undo_id}` | undo |
| POST | `/api/bucket/buckets/{bid}/upload?path=` multipart `files` | upload |
| POST | `/api/bucket/buckets/{bid}/upload/part?path=&name=&id=&offset=&total=` raw body (≤ 64 MB) | upload_part |
| GET | `/api/bucket/buckets/{bid}/upload/part?path=&name=&id=&total=` | part_status (what a resume still has to send) |
| PUT | `/api/bucket/buckets/{bid}/tags` `{path, add, remove}` | set_tags |
| GET | `/api/bucket/buckets/{bid}/detect?path=&force=` | detect |
| GET | `/api/bucket/buckets/{bid}/stats?path=` | stats |
| GET | `/api/bucket/tags` | all_tags |
| GET | `/api/bucket/search?q=&tag=&format=&bucket=&path=&glob=` | search |
| POST | `/api/bucket/send` `{bucket, paths, target, name, task_type}` | send |
| POST | `/api/bucket/buckets/{bid}/sync` | sync |
| POST | `/api/bucket/buckets/{bid}/local` `{paths, mirror}` | fetch_local (bucket.manage) |
| POST | `/api/bucket/buckets/{bid}/local/check` | check_local (bucket.manage) |
| POST | `/api/bucket/buckets/{bid}/compare` | compare_cloud `{path, verify?}` -> {job_id, path} (a viewer may: read-only) |
| GET | `/api/bucket/buckets/{bid}/compare` | compare_result `?path&status=a,b&q&offset&limit` -> summary + one page of files |
| POST | `/api/bucket/buckets/{bid}/local/remove` `{paths}` | drop_local (bucket.manage) |
| GET | `/api/bucket/buckets/{bid}/export?path=` | export (a streamed zip) |
| GET | `/api/bucket/buckets/{bid}/export/check?path=` | export_check |
| POST | `/api/bucket/buckets/{bid}/export` `{path, target, create_bucket, overwrite}` | export_to (bucket.manage) |
| POST | `/api/connections/create-bucket` `{id, bucket}` | not the service: `{ok, bucket, created}`, the caller's own S3 connection (routes/datasets.py) |
| GET | `/api/bucket/buckets/{bid}/source?path=` | source |
| POST | `/api/bucket/autolabel` `{bucket, path, model, target, new_name, conf, formats, write_yolo, name}` | autolabel |
| POST | `/api/bucket/buckets/{bid}/convert` `{path, to}` | convert_vectors |
| GET | `/api/bucket/buckets/{bid}/video?path=` | video_info |
| POST | `/api/bucket/frames/estimate` `{bucket, paths, opts}` | estimate_frames |
| POST | `/api/bucket/frames` `{bucket, paths, opts, target, new_name, out, remember}` | extract_frames (bucket.manage) |
| POST | `/api/bucket/buckets/{bid}/video-auto` `{opts \| null}` | set_video_auto (bucket.manage) |
| GET | `/api/bucket/sources` | list_sources |
| POST | `/api/bucket/sources/inspect` `{source, ref, connection_id, opts}` | inspect_source |
| POST | `/api/bucket/import` `{source, ref, name, connection_id, opts, bucket, path}` | import_source |
| POST | `/api/bucket/buckets/{bid}/reimport` | reimport |
| GET | `/api/label/formats?task_type=&media_type=` | the label writers: {formats: [{key, label, tasks, media, layout, export_layout, note}], default} |

## AI assistant: `/api/llm/bucket/*` (`dataviz/llm/agent.py`, `tools.py`)
`POST /api/llm/bucket/chat` `{messages: [{role, text}], scope: {bucket, path, selection: [paths]}}` streams NDJSON, one event per line:
- `{"type":"meta","model":..,"in_est":..}`
- `{"type":"delta","text":..}`: assistant text.
- `{"type":"tool","id":..,"name":..,"args":{..},"summary":"→ 38 matches","ok":true}`: a read-only tool the agent already ran.
- `{"type":"plan","plan_id":..,"actions":[{"id":..,"tool":..,"args":{..},"summary":".."}]}`: mutating tools, waiting for the user.
- `{"type":"ask","question":..,"options":[..]}`: a clarifying question, sent instead of guessing.
- `{"type":"cannot","reason":..,"alternative":..|null}`
- `{"type":"done","usage":{..}}` / `{"type":"error","detail":..}`

`POST /api/llm/bucket/confirm` `{plan_id, action_ids}` runs only the checked actions (in order, through `service`) and returns `{results: [{id, ok, summary, error?}], retagged: [..]}`. A plan expires after 15 min and belongs to its user.
