---
name: datacenter
description: "Drive the Data Center of the dataviz app (image dataset buckets: managed folders, linked local folders, read-only S3 links): list, search, inspect, detect formats, and propose moves / copies / renames / deletes / tags / sends to the visualizer or a labeling project. Use when asked to find, organise, tag, inspect or open datasets kept in Data Center buckets, through the CLI (`python -m dataviz dc`), the HTTP API (/api/dc/*, /api/llm/dc/*), or as the in-app ◆ agent."
---

# Data Center

The Data Center keeps a user's image datasets in **buckets**. Three kinds:

- `managed`: a folder under `storage.data_root` (default `data/`). Imports land here.
- `local`: a linked folder inside `storage.allowed_roots`, used in place.
- `s3`: a linked bucket + prefix through one of the owner's S3 connections. **Read-only**: you can list, view,
  detect, tag and send it, never change its files.

Buckets are private to their owner (admins see every one). Paths are relative to the bucket root, POSIX, no
leading slash; `''` is the root. Changing data needs the tier permission `data.manage`.

One tool registry (`dataviz/llm/tools.py`) backs the in-app agent, the CLI and this skill, and every tool goes
through `dataviz/datacenter/service.py`. Contract: `dataviz/datacenter/API.md`.

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
```

Output is JSON: `{ok, summary, result}` or `{ok: false, error, status}`. Exit codes: `0` ok, `1` tool / service
error, `2` usage (unknown tool, bad `--args`, no `--user`), `3` a change not confirmed (run it again with `--yes`).
As Claude Code: show the user the `confirm` line and only re-run with `--yes` after they agree.

## HTTP

- Direct API, `/api/dc/*` (JSON in and out, errors `{detail}`): `GET /summary`, `GET|POST /buckets`,
  `GET|PATCH|DELETE /buckets/{bid}`, `GET /buckets/{bid}/ls?path=`, `GET .../file?path=&w=`, `GET .../text?path=`,
  `POST .../ops {ops}`, `POST .../undo {undo_id}`, `POST .../upload?path=`, `PUT .../tags {path, add, remove}`,
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
   formats or images to video, editing pixels or labels, training, anything outside the user's buckets, and changing
   an S3 bucket (offer copying into a managed bucket).
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

**Read-only bucket.** "Delete the blurry images in the cloud bucket" (an `s3` bucket) → `cannot_do` (S3 links are
read-only), alternative: copy them into a managed bucket, or tag them `blurry`.

## Format detection (`detect_format`)

Each folder gets one format with a confidence, the checks that decided it and a reason; the bucket carries it as
the automatic tag `format:<key>`.

| format | what it looks like |
|---|---|
| `yolo-detect` / `yolo-seg` / `yolo-pose` / `yolo-obb` | `images/` + mirrored `labels/` (one `.txt` per image), usually `data.yaml`; label lines of 5 columns (detect), 5 + 3k (pose), 9 (obb), odd ≥ 7 polygon values (seg) |
| `coco` | a JSON with `images`, `annotations`, `categories` |
| `voc` | `Annotations/*.xml` next to the images |
| `classification` | one sub-folder of images per class |
| `images` | images with no labels |
| `embeddings` | `.npy` vector files |
| `other` / `empty` | none of these / nothing inside |
