# `dataviz/datacenter/`: the Data Center backend

Buckets of data (managed folders under `storage.data_root`, linked local folders inside `storage.allowed_roots`,
read-only S3 links), their files, tags and detected formats. **`service.py` is the only entry point**: the routes
(`server/routes/datacenter.py`), the LLM tools (`llm/tools.py`) and the CLI (`python -m dataviz dc`) all call it.
The contract shared with the page and the tools is [`API.md`](API.md).

| File | What it does |
|---|---|
| `service.py` | The public functions of API.md. Visibility (owner only; admins see all → another user's bucket is a 404), the `data.manage` tier permission on every change (plus `dataset.create` / `label.create` for Send), S3 buckets read-only, the detection cache (`app.dc_format_cache`, keyed by `formats.fingerprint`, S3 entries reused for 10 min), the undo log (`app.dc_undo`), Send to a visualize session (`connections.Registry.upsert_dataset`) or a labeling project (`ProjectRegistry.create` + the image import as a job). `configure(ctx=)` is called by the router; the CLI calls `configure(cfg)`. |
| `store.py` | Filesystem layer, no users or DB. `clean_rel` / `resolve` (rejects `..`, absolute paths, drive letters, symlinks leaving the bucket; every path also through `connections.check_local`), `list_local` (one `os.scandir`, only the returned page is stat'ed when sorting by name), `apply_op` / `undo_step` (mkdir, move, copy, rename, delete → `<data_root>/.trash/<undo_id>/`), streamed uploads and zip-slip-safe unzip, cached JPEG thumbnails (`<workspace_root>/dc_thumbs/`), `S3View` (listing / reads through the owner's connection). `DCError(message, status)` lives here. |
| `formats.py` | `detect(tree)`: YOLO detect / seg / pose / OBB (images/ + labels/ or train/images …; ≤ 50 label files sampled for their column counts; data.yaml `kpt_shape` → pose), COCO, Pascal VOC, classification folders, images, embeddings, empty, other, each with checks, confidence, reason and class count. Runs on `LocalTree` (os.scandir) or `KeyTree` (an S3 key listing). `tree_stats` (images / labels / size), `fingerprint` (top two levels, files not stat'ed). Pure: `tests/test_dc_formats.py`. |
| `tags.py` | `app.dc_tags` on (bucket, path). User tags via `core/names.clean_tags`; the auto tag `format:<key>` on the bucket root is written only from detection and cannot be removed. `apply_format` swaps it and returns `retagged {path, from, to, reason}`; tags follow moves / renames and come back with an undone delete. |

Rules worth knowing:
- After every change (ops, upload, undo) the bucket root is re-detected (forced) and `retagged` is returned whenever the format changed (also from `empty`).
- `delete_bucket(delete_files=True)` only ever removes a **managed** folder; a link is just forgotten.
- Entries carry `labels` (lines of the mirrored YOLO label file) but no `w` / `h`: reading 500 image headers per page would cost more than the whole listing.

Tests: `tests/test_dc_formats.py`, `tests/test_dc_routes.py`. Tables: migration `db/migrations/versions/0006_datacenter.py`.
