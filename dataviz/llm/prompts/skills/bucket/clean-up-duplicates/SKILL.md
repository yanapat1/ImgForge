---
name: clean-up-duplicates
description: find duplicate files / copied folders in buckets and remove them safely (to the trash)
when: the user asks to find or delete duplicates, copies, "(1)" files or repeated datasets
---
1. Find candidates by name: `search` with glob patterns such as `* (1).*`, `*copy*`, `*_dup*`, or the same file name
   across folders; `list_folder` with sort size to spot equal sizes; `list_buckets` for buckets with the same
   counts (repeated imports).
2. Names and sizes only suggest a copy: say so. Identical pixels or near-identical frames are found in the
   visualizer (Curation › Duplicate image file, Find issues › Near-duplicates): offer `send_to_session` for that.
3. Show the groups as a table (keep / remove, path, size) and ask_user which copy to keep when it is not obvious
   (keep the one inside the dataset's `images/`, the older one, or the one with a label file).
4. When removing images of a YOLO dataset, remove their label files (`labels/` same stem) in the same `delete` call.
5. `delete` goes to the trash and is confirmed by the user; report the undo id. Never `delete_bucket` with
   delete_files for a duplicate bucket without saying it is permanent.
6. `detect_format` afterwards if a dataset folder was touched.
