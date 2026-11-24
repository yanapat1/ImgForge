---
title: Change marks: which files and folders changed
summary: the amber dots on changed files and the counts on their parent folders (edited on the Bucket page, fixed in Visualize, uploaded, copied, moved, deleted, edited outside the app), the "N changed" list, Mark as seen, what clears them
---
- A file that changed since it was **downloaded** (or last marked seen) has an **amber dot** on its tile, list row and
  tree row; every **parent folder** shows a dot with the **count** of changed files below it, up to the bucket. The
  tooltip says what and where: "edited in Visualize · 3 min ago", "new on the Bucket page", "uploaded", "copied
  here", "moved here", "deleted" (red: the parent keeps the count), "changed outside the app" (grey).
- What marks: text edits, New file / New folder, uploads (an archive: the folder it unpacked into), Copy / Paste,
  moves and renames (the marks move along), deletes; **label fixes saved in Visualize** (box edits, curation fixes:
  the label file, local or in S3, of a session that reads a bucket folder); in a downloaded S3 folder, the background
  check (every 10 min while browsing, or Check for updates) also marks files whose size / time left what was
  downloaded (edited by another tool).
- What clears: **Mark as seen** (right-click a marked item, or the folder toolbar's **N changed** › Mark all as
  seen), Undo of that change, **Update from S3** (those files are S3's version again), **Upload to S3** (once uploaded).
- The folder toolbar's **N changed** button lists the changed paths under the open folder, newest first; a click
  opens that file's folder.
- The assistant: `list_changes {bucket, path}` (read) and `mark_seen {bucket, paths | path}`; `ls` entries carry
  `changed: {kind, source, at, n, self}` (n = changed files below a folder, self = the folder itself changed) and the
  listing `changes` (the total under the open folder).
