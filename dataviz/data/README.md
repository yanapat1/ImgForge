# `dataviz/data/` — datasets: where they live and how they are indexed

## Files

| File | What it does |
|---|---|
| `sources.py` | The **source interface** and its two implementations. `LocalSource(path)` reads a YOLO folder in place. `S3Source(conn, bucket, prefix, mode)` reads an S3 / RustFS / MinIO prefix, in `stream` or `cache` mode. Also `browse()` (buckets / folders with a YOLO flag), `browse_local()`, `make_client()`. |
| `labeling_source.py` | `LabelingSource(labeling, pid, task_id, only_done)`: a **live** visualizer source over a labeling session's task (`kind='labeling'`, `uri=label://pid/task`). Labels are built in one `iter_task_export` pass cached per `fingerprint()` (changes on any edit), images come from the project's `ImageCache` EXIF-transposed, and `write_label` diffs YOLO text back into the store through `put_annotations` (curation fixes). |
| `connections.py` | `Registry`, in PostgreSQL: each user's own connections (`app.connections`, secrets Fernet-encrypted; nobody else lists or uses them, admins included) and the visualize sessions (`app.sessions` kind `viz` + `app.viz_sessions`; `datasets(user, scope)` = the cards with owner / members / my role). `source(ds)` builds the right source object **with the session owner's connection** (members never see it); `scoped(owner_id)` is the connection lookup handed to the labeling sources and export sinks. `check_local()` keeps local folders inside `storage.allowed_roots` (symlinks and `..` resolved first). `find_local(path, owner_id)` matches `--dataset` to an existing session. `public_connection()` is the only shape the page ever sees (secret → `has_secret`, access key masked). |
| `embedding_source.py` | `EmbeddingSource(path)`: an **embeddings session** (`kind='embeddings'`): vectors computed anywhere plus a table of labels and metadata, read in place. `check()` validates the folder and returns the summary the session form shows; `feature_registry()` offers one feature space per `.npy`; `build_index()` writes `samples.json` (one record per row) and image previews as `crops/` thumbnails, so the page's crop URLs work unchanged. |
| `indexer.py` | `build_index(source, cache_dir)`: one record per box (`samples.json`) + a square 224 px crop per box (`crops/000123.jpg`) + brightness / blur metrics. Reused while the fingerprint matches. |

### The source interface

```
name, uri, kind ('local' | 's3'), root (local folder or None)
fingerprint()            content hash: changes when any label/image changes
list_labels(), list_images(), read_label(name), read_classes()   # classes.txt, else data.yaml names
open_image(name) -> PIL.Image
image_ref(name), label_ref(name)                                   # for the CSV export
```

A new backend (GCS, HTTP, a zip file) only has to implement this; nothing else changes.

### Embeddings folders (any modality)

An embeddings session plots vectors you computed elsewhere (audio, video, text, sensors, tables), with
everything that works on vectors: the 15 projections and their map quality, k-NN label suspicion and
outliers, groups, lasso, tags, saved views, CSV export. There are no boxes, so curation, models and the
image viewer are off (the server answers 409; the page hides them through `api/status` `caps`).

```
my-set/
  meta.csv  (or meta.parquet, needs pyarrow)    one row per sample
  clip.npy, wav2vec.npy, ...                    (N, D) float arrays; row i = meta row i; each file = one feature space
```

| Column of `meta` | Meaning |
|---|---|
| `label` | the class (optional; without it every row is in the class `all`) |
| `id` | the row's display id (optional; else its row number) |
| `image`, `audio`, `video` | a path inside the folder: the row's preview. Paths that leave the folder are refused, both when checking and when serving (`api/preview/<id>/<kind>`, with Range support so media can seek). An http(s) URL is allowed in the table but **not shown** unless `embeddings.allow_url_previews` is on in the config: every viewer's browser would fetch from that outside host |
| `text` | the row's text, shown on its card and in the dialog |
| anything else | metadata: **num** when every value is a number (colour on a scale, sort), **cat** with ≤ 50 distinct values (colour with a legend), else free **text** (shown, not coloured) |

- A `.npy` whose rows are already unit length is compared by cosine; any other by Euclidean distance (it is not
  normalized: the magnitude you computed is kept). k-NN is exact squared-Euclidean either way.
- The fingerprint is the names, sizes and mtimes of `meta.*` and the `.npy` files: editing any of them makes a new
  workspace, as for a YOLO folder.
- `python examples/make_embeddings_demo.py` writes a ready folder from scikit-learn's digits (image previews, two
  feature spaces, a numeric and a categorical column).

## How the code is written

- Sources never cache dataset content beyond what they need (`S3Source` keeps the bucket listing;
  `LocalSource` the image-name set). Caching is the workspace's job.
- Secrets: Fernet-encrypted in the `secrets` column (db/crypto.py), never in `config`; a dump alone does not leak
  them. The layout is the one db/legacy.py imports the old connections.json into.
  world-readable, even for a moment. A blank secret in an update means "keep the stored one".
- The indexer consumes results **in label-file order**, so sample ids are identical on every run and
  every machine (feature / projection caches stay valid).

## Optimizations & techniques

| Technique | Where | Measured / why |
|---|---|---|
| Bounded pipeline | `indexer.build_index` (`deque` window) | Images are decoded a fixed window ahead of the writer: memory stays flat for any dataset size. |
| 4 decode threads | `INDEX_WORKERS = 4` | PIL decode + crop partly holds the GIL. On this machine: 4 threads = 1.8× faster, 16 threads = 0.6× (slower than serial). Local indexing 47 s → 20 s. |
| Download ≠ decode | stream mode | Images are fetched `s3.concurrency` (32) at a time on the source's pool, while only 4 threads decode. |
| Streamed GETs | `S3Source.sync` | `download_file()` starts its own 10-thread transfer pool per file; 32 of those starved the GIL so progress froze until the end. `get_object` + `iter_chunks` is **3.3× faster** (27 s → 8 s for 1.26 GB) with smooth progress. |
| Incremental sync | `.manifest.json` (key → size, ETag) | Re-sync downloads only new/changed files, deletes removed ones, and saves the manifest every 5 s so an interrupted sync resumes. Files land as `.part` then `os.replace`. |
| Mode-independent fingerprint | `S3Source.fingerprint` | Hash of the bucket listing (not mirror mtimes): stream and cache mode share one workspace, so switching modes recomputes nothing. |
| Listing reuse | `sync(refresh=False)` on open | `check()` just listed the bucket; opening doesn't list it twice. |
| Path-style addressing, s3v4 | `make_client` | Required by RustFS / MinIO; adaptive retries and timeouts from `config.yaml s3:`. |
| Parallel label reads | `S3Source.read_labels` | All small label files fetched concurrently before decoding starts. |

## Gotchas

- `LocalSource.fingerprint()` must stay byte-identical to the old `dataset_fingerprint()`, or every
  existing workspace looks like a new dataset (everything recomputes).
- Sample `id`s are positions; never reorder `samples` without rebuilding the workspace.
