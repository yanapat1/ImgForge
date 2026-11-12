---
title: S3 buckets: stream vs local, and Sync
summary: what stream (read-only) and local (downloaded copy) allow, per-folder downloads (editable in place) and their badges, Compare with cloud (file by file: added / deleted / edited here, new / changed / removed in S3), offline, re-sync / update rules, when to use which
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
- **Download only some folders** (stream mode; selection bar or right-click › Download to local, tool
  `download_s3_folders`): a local copy of just those folders (only that prefix is downloaded, never the whole
  bucket), in the same place as a full load. Downloaded files open from disk (fast, video previews work).
- **A downloaded folder is editable in place**: it lists from the local copy, and create folder / move / copy /
  rename / delete / write a text file / upload inside it work (undoable) on the copy only, never on S3. Not allowed:
  a move into a folder that is not downloaded, renaming or deleting the downloaded folder itself (use Remove local
  copy), any change while a download into the bucket runs (409). The rest of the bucket keeps streaming, read-only.
  To change a folder that is not downloaded: `download_s3_folders` it first, then the changes. Export and Send to
  read S3, so to publish local edits use `export_to` from a fully loaded copy, or re-upload them.
- **Badges** (`local` on each listed entry): stream only (cloud, none) · `fresh` (downloaded, same as S3) ·
  `outdated` (S3 changed it) · `new` (S3 has it, the copy not yet) · `gone` (only the copy has it: S3 removed it) ·
  `partial` (some folders inside are downloaded) · `fetching`. A downloaded folder is compared with S3
  automatically every 10 minutes while browsing, or with `check_s3_local`; `list_folder` gives its
  `local_copy` (state, diff {new, changed, removed}).
- **Compare with cloud** (strip button "Compare with cloud" on a downloaded folder or a bucket loaded into local,
  right-click › Compare with cloud…, tool `compare_s3_local`): every file of the local copy against S3, in a list the
  user can filter and search. Statuses: **Added in local**, **Deleted in local**, **Edited in local** (what the user
  did in the copy), **New in cloud**, **Changed in cloud**, **Removed from cloud** (what S3 did since the download),
  **Changed on both sides**, **Same**. Each download keeps a manifest of what S3 held, so added vs. removed is exact;
  a copy downloaded before that is judged by the file times. **Verify contents** also compares the bytes (MD5 vs. a
  single-part ETag; slow, it reads every file): a re-saved file with the same bytes then reads as Same. Rows can be
  ticked and **Get cloud version** downloads S3's file over the local one (streamed links); a path opens its folder.
  Read-only: it never writes S3 or the copy. The last comparison is kept; Compare again / `refresh` redoes it.
- **Update** an outdated folder: `download_s3_folders` on it again. New and changed files are fetched (an S3 file
  replaces a local edit of the same name; files deleted locally come back), unchanged skipped. Files S3 no longer has stay unless `mirror` (the dialog's opt-in checkbox): only when the user asks.
  `remove_local_copy` deletes a downloaded folder's copy (S3 untouched; it streams again).
- **Offline**: when S3 cannot be reached the bucket lists only its downloaded folders (`offline`), with a
  "Retry S3" button; other folders answer "S3 is unreachable". It is not an error of the data.
- **Reload** (toolbar refresh button, `ls` with `refresh`) lists the open folder from S3 again; otherwise a listing
  is reused for a minute.
- Big folders list progressively: the first entries show at once and the rest fill in (`partial` while it runs).
- Switching back to stream keeps the copy on disk; a later switch to local downloads again over it.
- Changes made in mode local stay local: they are **not** written back to S3. To publish them, use `export_to` to S3.
- Use stream to browse / tag / send big buckets; local to reorganise, fix labels in place, or work offline.
