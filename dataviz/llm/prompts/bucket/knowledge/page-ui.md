---
title: The Data page: menus, reload, copy path, resizing
summary: right-click menus on files, folders, empty space, tree rows and bucket cards; keyboard in the tree and grid; drag-and-drop upload; the Reload and Copy path buttons; resizing the tree and the assistant
---
- **Right-click** works like a Windows right-click menu: the common actions at the top, the rest in sub-menus
  (a ▸ row opens beside it on hover or with ArrowRight; ArrowLeft / Escape go back). On a file or folder (it becomes
  the selection, or joins it): Open, Open in a new tab · Cut (Ctrl+X), Copy (Ctrl+C), Paste into (a folder), Rename
  (F2) · **Organize ▸** Move to, Copy to, Tags, Mark as seen · **Send to ▸** Visualize, Label, Create session… (one
  folder: the Image page's dialog, both linked) · **Export ▸** Download (zip), Export to… (S3, FTP, SFTP), Re-format
  to… (a folder) · **Cloud (S3) ▸** (S3 buckets) Download to local, Upload to S3, Check for updates, Compare with
  cloud…, Remove local copy · **Tools ▸** Extract frames (videos), Label with model (a folder) · Copy path · Delete
  (Del). On the empty space of a folder: **New ▸** Folder, File, Upload files · Paste (Ctrl+V), Select all, Refresh ·
  **View ▸** Thumbnails / Names only, sort · Tags · Send to ▸ · Export ▸ · Cloud (S3) ▸ Update from S3 / Check for
  updates / Compare with cloud / Download this folder / Upload this folder · **Tools ▸** Label with model, Extract
  frames, Re-detect format · Copy path. On a bucket (tree row or card): Open, Refresh · Send to ▸ · Cloud (S3) ▸ Load
  the whole bucket into local · Tools ▸ · Copy path · Edit (name, tags, load mode), Rename · Delete / Unlink. A tree
  folder: Open, Refresh · New ▸ (the open folder) · Paste here · Tags · Send to ▸ · Export ▸ · Cloud (S3) ▸ · Copy
  path. Items that would change a read-only folder are greyed out with the reason (a sub-menu still opens to show
  it); a sub-menu with nothing to offer is left out. Keyboard: the ContextMenu key or Shift+F10. When you point the
  user to an item, name its path, e.g. "right-click › Cloud (S3) › Upload to S3".
- **Keyboard**: the tree and the file grid are one Tab stop each. In the tree, Up / Down move, Right opens a folder,
  Left closes it (or goes to its parent), Enter opens. In the grid / list, the arrow keys move, Shift+arrows extend
  the selection, Space selects, Enter opens, Ctrl+A selects all, Del deletes (with Undo), F2 renames. `/` jumps to
  search. **?** shows every shortcut of the page (on every page of the app).
- **New file…** (folder toolbar, the empty-space menu, a tree row of the open folder): a name plus an extension
  picked from a list (.txt .md .json .yaml .yml .csv .py .sh .xml .ini .toml) or typed (any); a name with its own
  extension (data.yaml) keeps it. data.yaml and classes.txt start with a template; a text file opens in the editor.
  Undo removes it; an existing name is refused. Only where the folder is writable (not a streamed S3 folder). The
  assistant itself creates a file with `write_text_file`.
- **Drag files or folders from the desktop** onto the grid (or onto a folder tile) to upload them.
- **Reload** (the circular-arrows button in the folder toolbar and the overview): lists the buckets, the tree and
  the open folder again; an S3 folder is listed from S3 again instead of the copy kept for a minute.
- **Copy path** (the link icon next to the breadcrumb, or right-click › Copy path): copies where the folder is read
  from: `s3://bucket/prefix/folder` for a streamed S3 bucket, the folder on the server otherwise (a downloaded S3
  folder: its local copy).
- **Resize**: drag the right edge of the bucket tree, or the top edge of the open assistant (arrow keys on the
  focused edge; double-click resets). The sizes are remembered per user.
