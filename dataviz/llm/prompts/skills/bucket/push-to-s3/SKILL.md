---
name: push-to-s3
description: upload the edited files of a downloaded S3 folder back to S3 (same keys), handling conflicts
when: the user wants to upload, push, publish or save local changes of an S3 bucket back to S3, or asks why a file was not uploaded
---
1. Find what changed: `list_changes {bucket, path}` (edits here, label fixes in Visualize, new / copied files) or
   the scope's selection. Only files of a downloaded folder (or a bucket loaded into local) can go up: `list_folder`
   says so (its local copy, the entries' local state). Nothing downloaded = nothing to push (S3 already has it).
2. `push_to_s3 {bucket, paths}` (the folders or files; the whole downloaded folder is fine: unchanged files are
   skipped). It runs as a job: `job_status` with its job_id (wait_s 60) until done; the status bar shows bytes and an ETA.
3. Read the result: uploaded, unchanged, conflicts (S3 changed since the download: NOT uploaded), failed
   (an access error: the connection cannot write), deleted_here (deleted locally: S3 keeps them; say so).
4. Conflicts: show them (path, local size, S3 size / time) and ask_user per group: overwrite S3 with the local file
   (`push_to_s3` again with `overwrite` = those paths: always confirmed, it replaces someone's change), or take S3's
   version (`download_s3_folders` with those paths: the local edit is lost), or leave them.
5. After an upload the files read as fresh and their change marks go. Never push a whole bucket when the user named
   one folder.
