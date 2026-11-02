---
title: The Data page: menus, reload, copy path, resizing
summary: right-click menus on files, folders, empty space, tree rows and bucket cards; keyboard in the tree and grid; drag-and-drop upload; the Reload and Copy path buttons; resizing the tree and the assistant
---
- **Right-click** works like a desktop file manager. On a file or folder (it becomes the selection, or joins it):
  Open, Open in a new tab, Rename (F2), Move to, Copy to, Tags, Copy path, Send to Visualize / Label, Extract
  frames (videos), Export (zip), Download to local / Remove local copy (streamed S3), Delete (Del). On the empty
  space of a folder: New folder, Upload files, Refresh, Select all, Thumbnails / Names only, sort, Copy path, Edit
  tags, Export, Send this folder, Download this folder to local / Update from S3. On a tree row or bucket card:
  Open, Refresh, Copy path, Send, Edit (name, tags, load mode), Rename, Label with model, Re-detect, Delete / Unlink.
  Items that would change a read-only folder are greyed out with the reason. Keyboard: the ContextMenu key or
  Shift+F10.
- **Keyboard**: the tree and the file grid are one Tab stop each. In the tree, Up / Down move, Right opens a folder,
  Left closes it (or goes to its parent), Enter opens. In the grid / list, the arrow keys move, Shift+arrows extend
  the selection, Space selects, Enter opens, Ctrl+A selects all, Del deletes (with Undo), F2 renames. `/` jumps to
  search. **?** shows every shortcut of the page (on every page of the app).
- **Drag files or folders from the desktop** onto the grid (or onto a folder tile) to upload them.
- **Reload** (the circular-arrows button in the folder toolbar and the overview): lists the buckets, the tree and
  the open folder again; an S3 folder is listed from S3 again instead of the copy kept for a minute.
- **Copy path** (the link icon next to the breadcrumb, or right-click › Copy path): copies where the folder is read
  from: `s3://bucket/prefix/folder` for a streamed S3 bucket, the folder on the server otherwise (a downloaded S3
  folder: its local copy).
- **Resize**: drag the right edge of the bucket tree, or the top edge of the open assistant (arrow keys on the
  focused edge; double-click resets). The sizes are remembered per user.
