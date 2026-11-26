---
name: bucket
description: "Drive the Bucket page of the dataviz app (image dataset buckets: managed folders, linked local folders, S3 links: streamed read-only except downloaded folders): list, search, inspect, detect formats, and propose moves / copies / renames / deletes / tags / sends to the visualizer or a labeling project. Use when asked to find, organise, tag, inspect or open datasets kept in buckets, through the CLI (`python -m dataviz bucket`), the HTTP API (/api/bucket/*, /api/llm/bucket/*), or as the in-app ◆ agent."
---

# Bucket

The Bucket page keeps a user's image datasets in **buckets**. Three kinds:

- `managed`: a folder under `storage.data_root` (default `data/`). Imports land here.
- `local`: a linked folder inside `storage.allowed_roots`, used in place.
- `s3`: a linked bucket + prefix through one of the owner's S3 connections. Streamed it is read-only (list, view,
  detect, tag, send); a **downloaded folder** (or the whole bucket in mode local) is an editable local copy, and
  `push_to_s3` uploads its changes back to the same keys (conflicts when S3 changed since the download). Nothing
  writes S3 by itself: a Visualize session of a streamed YOLO folder downloads only its `labels/` (+ data.yaml), so
  label fixes stay in that local copy until `push_to_s3`; a downloaded folder is sent from its local copy; updating
  a downloaded folder keeps the files edited there.

Buckets are private to their owner (admins see every one). Paths are relative to the bucket root, POSIX, no
leading slash; `''` is the root. Changing data needs the tier permission `bucket.manage`.

One tool registry (`dataviz/llm/tools/`, the Bucket page's are `tools/bucket.py`) backs the in-app agent, the
CLI and this skill, and every tool goes through `dataviz/bucket/service.py`. Contract:
`dataviz/bucket/API.md`. In the app the ◆ agent lives in the right-side dock of every page; besides these
tools it has the common `list_sessions` and `navigate` (open Home, the Image page, the Bucket page, a visualize or labeling
session; the same chat continues there, answered by that page's specialist). `consult` asks the Visualizer's specialist a read-only question about a session without leaving.
`list_sessions bucket path` finds
the visualize sessions made from a folder; when there is none, `send_to_session` (confirmed in ask; target label
always confirmed) makes one and `navigate` opens it. It never creates sessions on its own initiative otherwise.

## Tools

`read` tools run at once. `change` tools are only **proposed**: in the app they become a plan the user checks and
confirms; on the CLI they need `--yes`. `control` tools end the in-app agent's turn (the page shows a question
or a refusal). The table is `tools.catalog()` (regenerate it with `python -m dataviz bucket list` after changing
tools.py). `?` marks an optional argument.

| tool | kind | arguments | what it does |
|---|---|---|---|
| `list_buckets` | read | q?, tags?, formats? | List the buckets (data roots) the user can see, with format, tags and image counts. |
| `list_folder` | read | bucket, path?, offset?, limit?, sort?, q? | List one folder of a bucket (paged), with its detected format and tags. |
| `search` | read | q?, tags?, formats?, bucket?, path?, glob? | Find buckets and files by name (case-insensitive substring), glob (e.g. "*night*.jpg"), tags or format. Optional bucket / path narrow it. |
| `detect_format` | read | bucket, path?, force? | Detect the dataset format of a folder (yolo-detect, coco, voc, classification ...) with the checks that decided it. |
| `get_stats` | read | bucket, path? | Counts of a folder: images, labels, boxes, classes, size, images per split. |
| `read_text` | read | bucket, path | Read a text file (yaml, txt, json, xml ...), the first 8000 characters. |
| `create_folder` | change | bucket, path | Create a folder. |
| `move` | change | bucket, paths, to | Move files / folders into another folder of the same bucket. |
| `copy` | change | bucket, paths, to | Copy files / folders into another folder of the same bucket. |
| `copy_to_bucket` | change | bucket, paths, to_bucket, to | Copy files / folders into a folder of ANOTHER bucket (or a big copy in the same one) as a background job with progress (the status bar; task_status / job_status). Any source (local, downloaded or streamed S3); the destination must be writable. A clashing name gets "(copy)"; the job result has an undo_id. |
| `push_to_s3` | change | bucket, paths, overwrite? | Upload to S3: files / folders of the local copy of an S3 link (a downloaded folder, or a bucket loaded into local) go back to the SAME keys (<prefix>/<path>) as a background job. Unchanged files are skipped; a file S3 changed since the download is a conflict (listed, not uploaded) unless its path is in overwrite. Files deleted here stay in S3. |
| `list_changes` | read | bucket, path? | What changed in a bucket (under a folder) since it was downloaded or marked seen: edits on the Bucket page, label fixes in Visualize (source visual), labeling exports, edits outside the app (external), newest first. The page shows them as dots; ls entries carry `changed` too. |
| `mark_seen` | change | bucket, paths?, path? | Clear change marks ("Mark as seen"): of `paths` (each with what is below), or of everything under `path` when no paths. Nothing on disk changes. |
| `rename` | change | bucket, path, name | Rename one file or folder (a new name, not a path). |
| `delete` | change | bucket, paths | Delete files / folders (they go to the trash and can be undone). |
| `add_tags` | change | bucket, path?, tags | Add user tags to a bucket ('' path) or a folder / file. format:* tags are automatic. |
| `remove_tags` | change | bucket, path?, tags | Remove user tags (automatic format:* tags cannot be removed). |
| `session_check` | read | bucket, path? | Before a session is made from a folder: its detected format, image / label counts, and whether Visualize, Label and the linked pair work on it (and why not), whether its labels are imported into the Label session, and the formats it can be re-formatted to. A streamed S3 folder read for the first time answers scanning + job_id: job_status it, then call session_check again. |
| `reformat_folder` | change | bucket, path?, to | Write a folder again in another label format (yolo, coco, voc, labelme, createml, csv) as a NEW folder of the bucket "<bucket> · formats"; the original is not changed. Reads YOLO detect / segment / pose and COCO labels. A local folder gets its images linked beside the new labels; a streamed S3 folder gets the labels only, with images.txt and source.json (the images stay in S3; to yolo it opens in Visualize and Label, images streamed). A background job: job_status says when it is done. |
| `send_to_session` | change | bucket, paths, target, name?, task_type? | Make a session from one folder: target both (the default choice: one linked session, the Label side owns the labels, the folder's own are imported, Visualize reads them live), visualize (the visualizer alone) or label (a labeling project alone). |
| `set_s3_mode` | change | bucket, mode | Choose how a WHOLE S3 bucket is read: stream (from S3 on every request) or local (every object downloaded into an editable local folder that can be re-synced and exported). Not needed to change one folder: download_s3_folders that folder instead. |
| `sync_from_s3` | change | bucket | Re-sync an S3 bucket that is loaded locally: files with the same name are replaced by the S3 version (unchanged files are skipped), local-only files are kept. |
| `download_s3_folders` | change | bucket, paths, mirror? | Download folders (or files) of a STREAMED S3 bucket into its local copy: fast to open, readable while S3 is unreachable, and EDITABLE (create_folder / move / rename / delete / upload inside it change only the local copy, never S3); the rest keeps streaming and stays read-only. Run it again on a downloaded folder to update it (new + changed files; unchanged skipped; files edited here are kept, see the result's kept / conflicts). mirror also deletes local files S3 no longer has: only when the user asks. |
| `check_s3_local` | read | bucket | Compare the downloaded folders of a streamed S3 bucket with S3 now: new / changed / removed files per folder (a job; list_folder then shows local_copy.state outdated). |
| `job_status` | read | job_id, wait_s? | How a background job is doing (the job_id an import, export, frames, S3 download / sync / compare tool returned): state queued | running | done | error | cancelled, stage, percent, seconds left. Call it before a step that needs the job finished; wait_s waits for it first. |
| `compare_s3_local` | read | bucket, path?, status?, q?, offset?, limit?, refresh?, verify? | File by file, the local copy of a folder of an S3 bucket against S3 (a streamed link's downloaded folder, or a bucket loaded into local): what the user added, deleted or edited here, and what is new, changed or removed in S3 since the download. Read-only. Reuses the last comparison unless refresh; verify also compares the bytes (MD5 vs ETag; slow). The user sees the same in the Bucket page (Compare with cloud). |
| `remove_local_copy` | change | bucket, paths | Delete the downloaded copy of folders of a streamed S3 bucket (S3 is not touched; they stream again). Only whole downloaded folders (or folders holding them). |
| `export_to` | change | bucket, path?, to, connection, s3_bucket?, prefix?, create_bucket?, overwrite? | Copy a folder (or file) of a bucket to S3, FTP or SFTP through one of the user's connections as a background job. Paths are kept from the bucket root: prefix + data/image/im1.png. |
| `extract_frames` | change | bucket, paths, fps?, mode?, n?, take?, window_s?, every_s?, pick?, start?, end?, dedupe?, format?, max_side?, layout?, target?, new_name?, out? | Cut videos (a video file, or a folder: its videos) into image frames to label or visualize: a background job writing a new folder <video>-frames (YOLO-ready by default). Pick frames with fps, or mode + its numbers; bursts = take frames from window_s every every_s. |
| `set_video_auto` | change | bucket, fps?, mode?, n?, layout?, off? | Make a bucket cut every new video (upload, import, S3 load) into frames by itself, with extract_frames-style settings; off=true turns it off. |
| `inspect_dataset_source` | read | source, ref, version?, revision?, patterns?, format? | Preview a dataset on the web before importing it: Kaggle (owner/dataset, competitions/<name>), Hugging Face (user/dataset), Roboflow (workspace/project/version), a direct link (url), Zenodo (record id / DOI) or OpenML (dataset id): title, size, files, license, versions. |
| `import_dataset` | change | source, ref, name?, bucket?, path?, version?, revision?, patterns?, format?, extract?, nested? | Download a dataset from the web into a new bucket (or a folder of a writable bucket) as a background job; archives are unpacked and the format is detected. The user needs the source's token in Settings › Connections (Kaggle, Roboflow; Hugging Face / Zenodo only for gated / restricted data). |
| `write_text_file` | change | bucket, path, text | Write (replace or create) a text file such as data.yaml, classes.txt, a .json or .env. Read it first with read_text and send the whole new content. Undoable. |
| `all_tags` | read |  | Every tag used in the buckets the user can see, with counts (to reuse existing tag names). |
| `create_bucket` | change | name, tags? | Create a new empty managed bucket (a folder kept by the Bucket page). |
| `update_bucket` | change | bucket, name?, mode? | Change a bucket's settings: its name, and for an S3 link its load mode (stream | local). |
| `delete_bucket` | change | bucket, delete_files? | Remove a bucket from the Bucket page. delete_files=true also deletes a managed bucket's files PERMANENTLY (no trash); without it the files stay and only the link is forgotten. |
| `undo` | change | bucket, undo_id | Undo one earlier change (move, copy, rename, delete, write) by the undo_id its result returned. |
| `ask_user` | control | question, options | Ask the user a clarifying question with 2-5 concrete options instead of guessing. Ends your turn. |
| `cannot_do` | control | reason, alternative? | Say the request cannot be done with these tools, why, and the closest thing you can do. Ends your turn. |
| `enter_plan_mode` | control | reason | Auto mode: switch to plan mode BEFORE acting when the request is big (several steps or pages), unclear, or would change or delete a lot. Then look things up read-only, ask_user when a choice is the user's, and submit_plan; nothing changes until they approve. Not for small, clear requests. |

`bucket` is a bucket id or its exact name (a unique case-insensitive match also works). A name shared by several
buckets is an error that lists their ids: ask which one, never pick one yourself. `move` / `copy` take `to` as
a destination **folder** in the same bucket. `delete` moves into the trash and returns an `undo_id`.

## CLI

Run from `scripts/image-tools/` (it needs the app's database: `DATABASE_URL` or `database.url` in config.yaml):

```bash
python -m dataviz bucket list                                                   # the catalogue above
python -m dataviz bucket list_buckets --user alice
python -m dataviz bucket search --args '{"glob": "*night*.jpg", "bucket": "cars"}' --user alice
python -m dataviz bucket move --args '{"bucket": "cars", "paths": ["train/a.jpg"], "to": "val"}' --user alice
#   -> exit 3 + {"ok": false, "confirm": "Move 1 item (train/a.jpg) → val/ in 'cars'", "args": {...}}
python -m dataviz bucket move --args '{"bucket": "cars", "paths": ["train/a.jpg"], "to": "val"}' --user alice --yes
python -m dataviz bucket export_to --args '{"bucket": "cars", "path": "data/image", "to": "s3", "connection": "minio",
  "s3_bucket": "dataset", "prefix": "dev/v1", "create_bucket": true}' --user alice --yes   # -> s3://dataset/dev/v1/data/image/...
```

Output is JSON: `{ok, summary, result}` or `{ok: false, error, status}`. Exit codes: `0` ok, `1` tool / service
error, `2` usage (unknown tool, bad `--args`, no `--user`), `3` a change not confirmed (run it again with `--yes`).
As Claude Code: show the user the `confirm` line and only re-run with `--yes` after they agree.

## HTTP

- Direct API, `/api/bucket/*` (JSON in and out, errors `{detail}`): `GET /summary`, `GET|POST /buckets`,
  `GET|PATCH|DELETE /buckets/{bid}`, `GET /buckets/{bid}/ls?path=&refresh=` (answers `location`, `s3_url`, `writable`), `GET .../file?path=&w=`, `GET .../text?path=`,
  `POST .../ops {ops}`, `POST .../undo {undo_id}`, `POST .../upload?path=` (+ `POST|GET .../upload/part`: raw chunks of one big file, any order, resumable), `PUT .../tags {path, add, remove}`,
  `GET .../detect?path=&force=`, `GET .../stats?path=`, `GET /tags`, `GET /search?q=&tag=&format=&bucket=&path=&glob=`,
  `POST /send {bucket, paths, target, name, task_type}`. Full table in `dataviz/bucket/API.md`.
- The agent: `POST /api/llm/bucket/chat {messages: [{role, text}], scope: {bucket, path, selection}}` streams NDJSON,
  one event per line, keyed by `type`: `meta {model, in_est}`, `delta {text}`, `tool {id, name, args, summary, ok}`
  (a read-only tool it ran), `plan {plan_id, actions: [{id, tool, args, summary}]}`, `ask {question, options}`,
  `cannot {reason, alternative}`, then `done {usage}` or `error {detail}`.
  `POST /api/llm/bucket/confirm {plan_id, action_ids}` runs only the checked actions, in plan order, and returns
  `{results: [{id, ok, summary, error?, undo_id?}], retagged: [..]}`. A plan lives 15 minutes, runs once and belongs
  to the user who asked. Both need the `bucket_data` AI feature switched on (else 403).

## Rules (the in-app agent follows the same ones: `dataviz/llm/prompts/bucket-agent.md`)

1. **Never guess** a bucket, path or file name. Look it up (`list_buckets`, `list_folder`, `search`) and reuse
   the exact `path` values returned.
2. **Ambiguous → ask.** "Clean up", "the old ones", "fix the dataset", a name matching several buckets: look first,
   then `ask_user` with 2-5 concrete options built from what you found (real names, real counts). Do not ask when
   the request is clear.
3. **Impossible → refuse** with `cannot_do`, the reason and the closest alternative. Out of reach: converting
   formats, making a video from images (cutting a video into frames is `extract_frames`), editing pixels or labels, training, anything outside the user's buckets, and changing
   a streamed S3 bucket outside its downloaded folders (offer `download_s3_folders` for just that folder, or
   copying into a managed bucket; inside a downloaded folder changes are allowed and stay local).
4. **Changes are proposals.** Put every change of the request into one plan, after the lookups. Never claim a
   change is done before it was confirmed.
5. **Never touch `format:*` tags** (automatic). Warn when a plan removes `images/`, `labels/` or `data.yaml`: the
   bucket gets re-tagged (`retagged {from, to, reason}`).
6. **Be short**: one or two sentences, or a small table for numbers.

## Worked examples

**Clear request.** "Move the night images of cars into a night/ folder."
1. `list_buckets {q: "cars"}` → one bucket `b1`.
2. `search {bucket: "b1", glob: "*night*.jpg"}` → 38 matches (exact paths).
3. One plan: `create_folder {bucket: "b1", path: "night"}` + `move {bucket: "b1", paths: [...38 paths], to: "night"}`.
4. Reply: "38 night images found; confirm the plan to move them into night/." Nothing runs until confirm.

**Ambiguous.** "Clean up the dupe bucket."
1. `list_buckets {q: "dupe"}` → two buckets named `dupe` (managed `b2`, local `b3`).
2. `ask_user {question: "Which dupe, and what should go?", options: ["dupe (managed, 1,204 images): delete empty folders", "dupe (local, 88 images): delete empty folders", "Show me what is in both first"]}`.

**Impossible.** "Turn the cars bucket into an mp4."
- `cannot_do {reason: "The Bucket page cannot render images into a video.", alternative: "I can open the cars bucket in the visualizer to page through it."}`

**Video → frames.** "Cut drive.mp4 into 15 frames every 2 seconds and get it ready to label."
1. `search {glob: "drive.mp4"}` → bucket `b4`, path `raw/drive.mp4`.
2. One plan: `extract_frames {bucket: "b4", paths: ["raw/drive.mp4"], mode: "pattern", take: 15, window_s: 1, every_s: 2}`
   (layout yolo by default: `raw/drive-frames/images/` + empty `labels/` + `data.yaml` + `frames.csv`).
3. After the job: `send_to_session {bucket: "b4", paths: ["raw/drive-frames"], target: "label"}`. "Every new video like
   this" → `set_video_auto` with the same settings.

**Streamed S3 bucket.** "Delete the blurry images in cam3/" of a streamed `s3` bucket: if `cam3` is downloaded (the
scope says "downloaded and editable", or `list_folder` gives `local_copy`), propose the `delete` directly (it changes
only the local copy). If not, propose `download_s3_folders {paths: ["cam3"]}` first (only that prefix, never the whole
bucket), then the delete once it finished. Outside the downloaded folders the bucket stays read-only.

## Format detection (`detect_format`)

Each folder gets one format with a confidence, the checks that decided it and a reason; the bucket carries it as
the automatic tag `format:<key>`.

| format | what it looks like |
|---|---|
| `yolo-detect` / `yolo-seg` / `yolo-pose` / `yolo-obb` | `images/` + mirrored `labels/` (one `.txt` per image), usually `data.yaml`; label lines of 5 columns (detect), 5 + 3k (pose), 9 (obb), odd ≥ 7 polygon values (seg) |
| `coco` | a JSON with `images`, `annotations`, `categories` |
| `voc` | `Annotations/*.xml` next to the images |
| `classification` | one sub-folder of images per class |
| `images` | images with no labels (stray non-image files ≤ 1 % of the images are ignored, with a warning) |
| `embeddings` | `.npy` vector files |
| `other` / `empty` | none of these / nothing inside |
