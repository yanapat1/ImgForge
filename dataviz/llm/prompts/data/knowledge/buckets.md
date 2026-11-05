---
title: Buckets (kinds, paths, settings)
summary: managed / local / S3 buckets, who sees them, paths, bucket settings (rename, load mode), removing a bucket
core: true
---
- A **bucket** is one root of data, private to its owner (admins see all; another user's bucket answers "not found").
  - `managed`: a folder kept by the Data Center under the data root; imports and `create_bucket` make these.
  - `local`: a linked folder on the server (inside the allowed roots), used in place.
  - `s3`: a linked S3 / RustFS bucket + prefix through one of the owner's connections; `mode` stream (read-only, except
    folders downloaded with `download_s3_folders`, which are editable in place) or local (the whole bucket
    downloaded, editable). See `s3-modes`.
- **Paths** are relative to the bucket root, `/`-separated, no leading slash; `''` is the root. `..`, absolute paths
  and symlinks leaving the bucket are rejected. Dot names are hidden (except config files like `.env`, `.gitignore`).
- **Settings** (`update_bucket`; the page: the ✎ on a bucket card / list row opens its Edit dialog): rename, tags; for an S3
  link, the load mode. The dialog's Actions tiles: Export (zip), Export to…, Re-detect format, Label with model…; then
  a red Delete… / Unlink… strip.
  Managed and local buckets have no mode.
- **Removing** (`delete_bucket`): without delete_files the bucket is only forgotten (files stay; tags, the format
  cache and its undo history go). delete_files=true deletes a managed bucket's folder (or an S3 link's local copy)
  **permanently, no trash**. A linked local folder or S3 prefix is never deleted. On the page: the 🗑 beside the ✎ on a
  card / list row, the Edit dialog's Delete strip or the details menu; each asks first (with the delete-files box).
- The page: All buckets overview (cards, filter, Tags), a folder view (Thumbnails / Names only, sort, New folder,
  Upload, Send to…), the details pane (format, checks, Re-detect, tags, split counts, sessions), and the ◆ assistant dock on the right
  (shows what it sees: bucket, folder, selection; "Tools" lists which tools run on their own / need an OK).
