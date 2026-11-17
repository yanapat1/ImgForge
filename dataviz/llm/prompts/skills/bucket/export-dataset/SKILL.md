---
name: export-dataset
description: export a folder or bucket as a zip, or copy it to S3 / RustFS, FTP or SFTP
when: the user asks to export, download, back up, upload elsewhere or hand a dataset over
---
1. Resolve the folder (`list_buckets`, `list_folder` or `search`); `get_stats` for the size and `detect_format` to
   confirm it is the dataset they mean.
2. A zip download is the page's button (Send to… › Export to… › Download zip): tell the user, you cannot download.
3. Remote copy: ask_user for the target when unclear (which connection, S3 bucket, prefix or FTP / SFTP path).
   Connections are the user's own (Settings › Connections); without one, say how to add it.
4. Explain the layout before running: destination = prefix / path + the path from the bucket root (exporting
   `data/images/` with prefix `v1/` gives `v1/data/images/...`).
5. `export_to` with `connection`, `s3_bucket` (S3), `prefix` or `to`, `create_bucket` when the bucket must be made.
   Use `overwrite` only when the user says so (it is dangerous and confirmed).
6. It runs as a job: report that it started and where the files will land; `job_status` with its job_id shows the
   progress and, when done, the result (files, bytes, URI).
