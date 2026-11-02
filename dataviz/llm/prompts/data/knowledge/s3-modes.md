---
title: S3 buckets: stream vs local, and Sync
summary: what stream (read-only) and local (downloaded copy) allow, per-folder downloads and their badges, offline, re-sync / update rules, when to use which
---
- **stream** (default): every listing and file read goes to S3. Read-only: list, view, detect format, tag, send to a
  session, export (zip or Export to…). No move / copy / rename / delete / write / upload.
- **Linking** (Import data › S3 bucket; a blank prefix links the whole bucket) returns at once. A **scan** job lists the
  objects and detects the format: it shows in the activity strip ("Scanning", object count, Cancel) and on the card.
  Until it ends the format and stats are empty; that is normal for a big bucket, not an error.
- **local**: a pull job downloads every object into a local copy (`<data_root>/.s3/<name>-<id>`). Once the first pull
  has finished (synced), the bucket reads and writes that copy: editable, undoable, exportable like a local bucket.
  Its kind stays `s3`. Switch with `set_s3_mode` / `update_bucket` mode local (starts the pull job).
- **Sync** (`sync_from_s3`, only in mode local): downloads again. An S3 object **replaces** the local file with the
  same name (local edits to it are lost); files left unchanged since the last pull (same size and time) are skipped,
  so a re-sync is fast; files only the copy has are kept; nothing is deleted. Dangerous: always confirmed.
- A pull starts downloading while it is still listing ("listing… n found" in the activity strip, then n/total).
- **Download only some folders** (stream mode; selection bar › Download to local, tool `download_s3_folders`): a
  read-only local copy of just those folders, in the same place as a full load. The bucket keeps streaming the rest
  and stays read-only. Downloaded files open from disk (fast, video previews work).
- **Badges** (`local` on each listed entry): stream only (cloud, none) · `fresh` (downloaded, same as S3) ·
  `outdated` (S3 changed it) · `new` (S3 has it, the copy not yet) · `gone` (only the copy has it: S3 removed it) ·
  `partial` (some folders inside are downloaded) · `fetching`. A downloaded folder is compared with S3
  automatically every 10 minutes while browsing, or with `check_s3_local`; `list_folder` gives its
  `local_copy` (state, diff {new, changed, removed}).
- **Update** an outdated folder: `download_s3_folders` on it again. New and changed files are fetched, unchanged
  skipped. Files S3 no longer has stay unless `mirror` (the dialog's opt-in checkbox): only when the user asks.
  `remove_local_copy` deletes a downloaded folder's copy (S3 untouched; it streams again).
- **Offline**: when S3 cannot be reached the bucket lists only its downloaded folders (`offline`), with a
  "Retry S3" button; other folders answer "S3 is unreachable". It is not an error of the data.
- Big folders list progressively: the first entries show at once and the rest fill in (`partial` while it runs).
- Switching back to stream keeps the copy on disk; a later switch to local downloads again over it.
- Changes made in mode local stay local: they are **not** written back to S3. To publish them, use `export_to` to S3.
- Use stream to browse / tag / send big buckets; local to reorganise, fix labels in place, or work offline.
