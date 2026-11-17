---
name: s3-local-and-sync
description: work on an S3 bucket: stream vs load into local, re-sync, publishing changes back
when: the user wants to edit, move or delete in an S3 bucket, asks why it is read-only, or wants to sync
---
1. `list_buckets` (or the scope) and check the bucket's kind and `mode`. Stream = read-only, except its downloaded folders (editable in place). A just-linked
   bucket with no format yet is still being scanned (activity strip): wait for it rather than re-linking.
2. To change one folder (prefix) of a streamed bucket: if the scope / `list_folder` says it is downloaded, just do
   the changes (create_folder / move / rename / delete / upload) on the same bucket: they land in the local copy.
   If not, propose `download_s3_folders` for **only that folder** first (only that prefix is downloaded; the rest
   keeps streaming), then, after the job (`job_status` with its job_id, wait_s 60: state done), the changes inside it. Never ask to load the whole bucket for one folder.
   To update a downloaded folder later: the same tool again (`check_s3_local` first if you need to know what
   changed; S3 versions replace local edits of the same name; `mirror` only when the user wants files S3 dropped
   removed).
   Only when the user wants to edit the whole bucket: propose `set_s3_mode` mode local. Explain: a job downloads
   every object (time and disk grow with the bucket; `get_stats` shows the size); once synced it is editable.
3. After the download finished, do the requested changes with the normal tools on the same bucket.
4. Re-sync (`sync_from_s3`) only when the user wants the S3 version again: S3 files **replace** local files with the
   same name (local edits to them are lost), unchanged files are skipped, local-only files stay. It is always confirmed.
5. Local changes are never written back to S3. To publish them, `export_to` the user's S3 connection (prefix =
   where it should land; the layout is kept from the bucket root; `overwrite` only when asked).
6. Back to stream (`set_s3_mode` mode stream) keeps the local copy on disk.
