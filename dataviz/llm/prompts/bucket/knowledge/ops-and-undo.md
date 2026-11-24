---
title: File operations, undo and the trash
summary: create folder, move, copy, copy / cut / paste between buckets, rename, delete, write a text file, upload; undo ids; the trash and how long it keeps files
---
- Operations run as one batch per call and return an `undo_id`: `create_folder`, `move` / `copy` (into a destination
  folder of the same bucket), `rename` (a new name, not a path), `delete`, `write_text_file`. Errors are per item
  (`errors`), the rest still applies.
- **Delete** moves files into the trash (`<data_root>/.trash/<undo_id>/`); `undo` with that id puts them back.
  `write_text_file` keeps the old content in the trash too (undo restores it; a new file undoes as a removal).
- **How long**: a trash folder stays as long as its undo entry exists (until it is undone, or its bucket is removed).
  Folders no undo entry points at are purged after 7 days. Undo history is per bucket; removing a bucket drops it
  and its trash. `delete_bucket` with delete_files=true never goes through the trash.
- Undo fails with "no longer in the trash" when the trash folder is gone; ask before retrying anything else.
- After every change the bucket root is re-detected: a result with `retagged` means the format changed (e.g.
  yolo-detect → other because labels/ moved). Tell the user and offer `undo`.
- The page: drag onto a folder moves (Ctrl / Alt copies), F2 renames, Delete deletes, and every action toast has Undo.
- **Copy / Cut / Paste** (page): select, then Ctrl+C / Ctrl+X (or right-click › Copy / Cut), open any folder of any
  bucket and Ctrl+V (or the toolbar's **Paste …** pill, right-click › Paste on the empty space, a tree row or a
  folder). Same bucket: Cut is a move, a small Copy is instant; a folder or a big copy, and any paste into another
  bucket, runs as a background job `bk-copy` (bytes, ETA and Cancel in the status bar). Cut across buckets deletes
  the originals (to the trash) once all was copied. A clashing name gets "(copy)"; Undo removes the copies. The
  source may be streamed S3 (read from S3); the destination must be writable (a streamed link: inside a downloaded
  folder). The assistant uses `copy_to_bucket` for another bucket, `copy` inside one bucket.
- Upload (page only): files, folders (with their sub-folders) or archives (zip / tar / tar.gz / 7z, unpacked) into the
  open folder, or a new bucket from Import data. Dropping files or folders from the desktop onto the file grid
  uploads them too (onto a folder tile: into that folder; folders keep their sub-folders). It runs in the background: the dialog closes at once and the user
  keeps working in the Bucket page while the status bar at the bottom of the page shows files done, MB/s,
  bytes, time left and the finish clock, with Cancel. Several uploads queue one after another.
  - Speed: 4 requests in flight (small files in batches, a big file in chunks sent in parallel). No request is
    bigger than 8 MB; the size halves (down to 256 KB) when a proxy in front of the app (JupyterHub, nginx) cuts or
    refuses one, and the browser keeps the size that worked.
  - The browser tab sends the bytes: closing or reloading it asks first and stops the upload. Send to opens the new
    session in a new tab while an upload runs. Uploading the same files again resumes: files already there are
    skipped and a half-sent big file continues from what the server holds (Retry on a failed row does the same).
  - "Upload stopped after N files: the connection was cut" means even the smallest chunk failed: check the proxy or
    the network, then Retry.
- **Background jobs.** Import, export, video → frames, an S3 download / sync / compare return a `job_id` and go
  on in the background (the status bar at the bottom shows them: one small line for the task in front with its stage,
  %, count, MB/s and time left, a thin bar and Cancel; "+N more" lists every task. With no task it names the page's
  own work: "Listing … on S3…", "Detecting the format…", "Loading previews · n left", else "Ready". Video → frames
  from an S3 link first shows the video's download %, MB and MB/s, then frames n/total). `job_status` job_id tells queued |
  running | done | error | cancelled, the stage, percent and seconds left; `wait_s` (up to 60) waits for it. Check
  it before a step that needs the job's files; a job of many minutes: say it continues and stop, do not poll for long.
