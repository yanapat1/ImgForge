---
name: datacenter
description: "Drive the Data Center of the dataviz app (image dataset buckets: managed folders, linked local folders, S3 links: streamed read-only except downloaded folders): list, search, inspect, detect formats, and propose moves / copies / renames / deletes / tags / sends to the visualizer or a labeling project. Use when asked to find, organise, tag, inspect or open datasets kept in Data Center buckets, through the CLI (`python -m dataviz dc`), the HTTP API (/api/dc/*, /api/llm/dc/*), or as the in-app ◆ agent."
---

# Data Center

The Data Center keeps a user's image datasets in **buckets**. Three kinds:

- `managed`: a folder under `storage.data_root` (default `data/`). Imports land here.
- `local`: a linked folder inside `storage.allowed_roots`, used in place.
- `s3`: a linked bucket + prefix through one of the owner's S3 connections. **Read-only**: you can list, view,
  detect, tag and send it, never change its files.

Buckets are private to their owner (admins see every one). Paths are relative to the bucket root, POSIX, no
leading slash; `''` is the root. Changing data needs the tier permission `data.manage`.

One tool registry (`dataviz/llm/tools/`, the Data Center's are `tools/datacenter.py`) backs the in-app agent, the
CLI and this skill, and every tool goes through `dataviz/datacenter/service.py`. Contract:
`dataviz/datacenter/API.md`. In the app the ◆ agent lives in the right-side dock of every page; besides these
tools it has the common `list_sessions` and `navigate` (open the home, the Data Center, a visualize or labeling
session; the same chat continues there, answered by that page's specialist). It never creates sessions on its own initiative
beyond `send_to_session`, which the user confirms.

## Tools

`read` tools run at once. `change` tools are only **proposed**: in the app they become a plan the user checks and
confirms; on the CLI they need `--yes`. `control` tools end the in-app agent's turn (the page shows a question
or a refusal). The table is `tools.catalog()` (regenerate it with `python -m dataviz dc list` after changing
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
| `rename` | change | bucket, path, name | Rename one file or folder (a new name, not a path). |
| `delete` | change | bucket, paths | Delete files / folders (they go to the trash and can be undone). |
| `add_tags` | change | bucket, path?, tags | Add user tags to a bucket ('' path) or a folder / file. format:* tags are automatic. |
| `remove_tags` | change | bucket, path?, tags | Remove user tags (automatic format:* tags cannot be removed). |
| `send_to_session` | change | bucket, paths, target, name?, task_type? | Open folders / files in the visualizer (target visualize) or as a labeling project (target label). |
| `set_s3_mode` | change | bucket, mode | Choose how a WHOLE S3 bucket is read: stream (from S3 on every request) or local (every object downloaded into an editable local folder that can be re-synced and exported). Not needed to change one folder: download_s3_folders that folder instead. |
| `sync_from_s3` | change | bucket | Re-sync an S3 bucket that is loaded locally: files with the same name are replaced by the S3 version (unchanged files are skipped), local-only files are kept. |
| `download_s3_folders` | change | bucket, paths, mirror? | Download folders (or files) of a STREAMED S3 bucket into its local copy: fast to open, readable while S3 is unreachable, and EDITABLE (create_folder / move / rename / delete / upload inside it change only the local copy, never S3); the rest keeps streaming and stays read-only. Run it again on a downloaded folder to update it (new + changed files; unchanged skipped). mirror also deletes local files S3 no longer has: only when the user asks. |
| `check_s3_local` | read | bucket | Compare the downloaded folders of a streamed S3 bucket with S3 now: new / changed / removed files per folder (a job; list_folder then shows local_copy.state outdated). |
| `remove_local_copy` | change | bucket, paths | Delete the downloaded copy of folders of a streamed S3 bucket (S3 is not touched; they stream again). Only whole downloaded folders (or folders holding them). |
| `export_to` | change | bucket, path?, to, connection, s3_bucket?, prefix?, create_bucket?, overwrite? | Copy a folder (or file) of a bucket to S3, FTP or SFTP through one of the user's connections as a background job. Paths are kept from the bucket root: prefix + data/image/im1.png. |
| `extract_frames` | change | bucket, paths, fps?, mode?, n?, take?, window_s?, every_s?, pick?, start?, end?, dedupe?, format?, max_side?, layout?, target?, new_name?, out? | Cut videos (a video file, or a folder: its videos) into image frames to label or visualize: a background job writing a new folder <video>-frames (YOLO-ready by default). Pick frames with fps, or mode + its numbers; bursts = take frames from window_s every every_s. |
| `set_video_auto` | change | bucket, fps?, mode?, n?, layout?, off? | Make a bucket cut every new video (upload, import, S3 load) into frames by itself, with extract_frames-style settings; off=true turns it off. |
| `inspect_dataset_source` | read | source, ref, version?, revision?, patterns?, format? | Preview a dataset on the web before importing it: Kaggle (owner/dataset, competitions/<name>), Hugging Face (user/dataset), Roboflow (workspace/project/version), a direct link (url), Zenodo (record id / DOI) or OpenML (dataset id): title, size, files, license, versions. |
| `import_dataset` | change | source, ref, name?, bucket?, path?, version?, revision?, patterns?, format?, extract?, nested? | Download a dataset from the web into a new bucket (or a folder of a writable bucket) as a background job; archives are unpacked and the format is detected. The user needs the source's token in Settings › Connections (Kaggle, Roboflow; Hugging Face / Zenodo only for gated / restricted data). |
| `write_text_file` | change | bucket, path, text | Write (replace or create) a text file such as data.yaml, classes.txt, a .json or .env. Read it first with read_text and send the whole new content. Undoable. |
| `all_tags` | read |  | Every tag used in the buckets the user can see, with counts (to reuse existing tag names). |
| `create_bucket` | change | name, tags? | Create a new empty managed bucket (a folder kept by the Data Center). |
| `update_bucket` | change | bucket, name?, mode? | Change a bucket's settings: its name, and for an S3 link its load mode (stream | local). |
| `delete_bucket` | change | bucket, delete_files? | Remove a bucket from the Data Center. delete_files=true also deletes a managed bucket's files PERMANENTLY (no trash); without it the files stay and only the link is forgotten. |
| `undo` | change | bucket, undo_id | Undo one earlier change (move, copy, rename, delete, write) by the undo_id its result returned. |
| `ask_user` | control | question, options | Ask the user a clarifying question with 2-5 concrete options instead of guessing. Ends your turn. |
| `cannot_do` | control | reason, alternative? | Say the request cannot be done with these tools, why, and the closest thing you can do. Ends your turn. |

`bucket` is a bucket id or its exact name (a unique case-insensitive match also works). A name shared by several
buckets is an error that lists their ids: ask which one, never pick one yourself. `move` / `copy` take `to` as
a destination **folder** in the same bucket. `delete` moves into the trash and returns an `undo_id`.

## CLI

Run from `scripts/image-tools/` (it needs the app's database: `DATABASE_URL` or `database.url` in config.yaml):

```bash
python -m dataviz dc list                                                   # the catalogue above
python -m dataviz dc list_buckets --user alice
python -m dataviz dc search --args '{"glob": "*night*.jpg", "bucket": "cars"}' --user alice
python -m dataviz dc move --args '{"bucket": "cars", "paths": ["train/a.jpg"], "to": "val"}' --user alice
#   -> exit 3 + {"ok": false, "confirm": "Move 1 item (train/a.jpg) → val/ in 'cars'", "args": {...}}
python -m dataviz dc move --args '{"bucket": "cars", "paths": ["train/a.jpg"], "to": "val"}' --user alice --yes
python -m dataviz dc export_to --args '{"bucket": "cars", "path": "data/image", "to": "s3", "connection": "minio",
  "s3_bucket": "dataset", "prefix": "dev/v1", "create_bucket": true}' --user alice --yes   # -> s3://dataset/dev/v1/data/image/...
```

Output is JSON: `{ok, summary, result}` or `{ok: false, error, status}`. Exit codes: `0` ok, `1` tool / service
error, `2` usage (unknown tool, bad `--args`, no `--user`), `3` a change not confirmed (run it again with `--yes`).
As Claude Code: show the user the `confirm` line and only re-run with `--yes` after they agree.

## HTTP

- Direct API, `/api/dc/*` (JSON in and out, errors `{detail}`): `GET /summary`, `GET|POST /buckets`,
  `GET|PATCH|DELETE /buckets/{bid}`, `GET /buckets/{bid}/ls?path=&refresh=` (answers `location`, `s3_url`, `writable`), `GET .../file?path=&w=`, `GET .../text?path=`,
  `POST .../ops {ops}`, `POST .../undo {undo_id}`, `POST .../upload?path=` (+ `POST|GET .../upload/part`: raw chunks of one big file, any order, resumable), `PUT .../tags {path, add, remove}`,
  `GET .../detect?path=&force=`, `GET .../stats?path=`, `GET /tags`, `GET /search?q=&tag=&format=&bucket=&path=&glob=`,
  `POST /send {bucket, paths, target, name, task_type}`. Full table in `dataviz/datacenter/API.md`.
- The agent: `POST /api/llm/dc/chat {messages: [{role, text}], scope: {bucket, path, selection}}` streams NDJSON,
  one event per line, keyed by `type`: `meta {model, in_est}`, `delta {text}`, `tool {id, name, args, summary, ok}`
  (a read-only tool it ran), `plan {plan_id, actions: [{id, tool, args, summary}]}`, `ask {question, options}`,
  `cannot {reason, alternative}`, then `done {usage}` or `error {detail}`.
  `POST /api/llm/dc/confirm {plan_id, action_ids}` runs only the checked actions, in plan order, and returns
  `{results: [{id, ok, summary, error?, undo_id?}], retagged: [..]}`. A plan lives 15 minutes, runs once and belongs
  to the user who asked. Both need the `data_center` AI feature switched on (else 403).

## Rules (the in-app agent follows the same ones: `dataviz/llm/prompts/datacenter.md`)

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
- `cannot_do {reason: "The Data Center cannot render images into a video.", alternative: "I can open the cars bucket in the visualizer to page through it."}`

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
