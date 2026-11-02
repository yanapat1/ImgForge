---
name: s3-local-and-sync
description: work on an S3 bucket: stream vs load into local, re-sync, publishing changes back
when: the user wants to edit, move or delete in an S3 bucket, asks why it is read-only, or wants to sync
---
1. `list_buckets` (or the scope) and check the bucket's kind and `mode`. Stream = read-only by design. A just-linked
   bucket with no format yet is still being scanned (activity strip): wait for it rather than re-linking.
2. To work with only part of a big bucket (fast, offline), propose `download_s3_folders` for those folders: the
   bucket keeps streaming, the copy is read-only. To update one later: the same tool again (`check_s3_local`
   first if you need to know what changed; `mirror` only when the user wants files S3 dropped removed).
   To edit: propose `set_s3_mode` mode local. Explain: a job downloads every object into a local copy (time and disk
   grow with the bucket; `get_stats` shows the size); once synced, the bucket is editable and undoable.
3. After the pull finished, do the requested changes with the normal tools on the same bucket.
4. Re-sync (`sync_from_s3`) only when the user wants the S3 version again: S3 files **replace** local files with the
   same name (local edits to them are lost), unchanged files are skipped, local-only files stay. It is always confirmed.
5. Local changes are never written back to S3. To publish them, `export_to` the user's S3 connection (prefix =
   where it should land; the layout is kept from the bucket root; `overwrite` only when asked).
6. Back to stream (`set_s3_mode` mode stream) keeps the local copy on disk.
