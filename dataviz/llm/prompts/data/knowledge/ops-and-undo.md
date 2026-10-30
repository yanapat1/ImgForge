---
title: File operations, undo and the trash
summary: create folder, move, copy, rename, delete, write a text file, upload; undo ids; the trash and how long it keeps files
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
- Upload (page only): files, folders or archives (zip / tar / tar.gz / 7z, unpacked) into the open folder.
