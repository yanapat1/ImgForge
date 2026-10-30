---
name: organise-yolo-dataset
description: turn a folder of images + label files into a clean YOLO layout (images/, labels/, data.yaml)
when: the user wants a folder to become a YOLO dataset, or detect_format says other / images for a set with labels
---
1. `detect_format` on the folder and `get_stats`; `list_folder` the root (and one level down) to see what is there:
   loose images, `.txt` labels, a `classes.txt`, a `data.yaml`, split folders.
2. If it is already `yolo-detect` (or seg / pose / obb) with a valid `data.yaml`, say so and stop.
3. `todo` the plan. Target layout: `images/` and `labels/` (each with `train/`, `val/`, `test/` when there are splits),
   one `.txt` per image with the same stem, `data.yaml` at the root.
4. `create_folder` the missing folders, then `move` images into `images/...` and label files into the mirrored
   `labels/...` (same bucket; one call per destination; never `copy` unless the user wants to keep the original).
5. Class names: `read_text` an existing `classes.txt` / `data.yaml`. Write `data.yaml` with `write_text_file`:
   `path: .`, `train: images/train`, `val: images/val` (and `test:`), `names:` as a list in class-id order. Never
   invent class names: ask_user when they are unknown.
6. `detect_format` again (force) and report the format, counts per split and any retagged note. Mention `undo` with
   the undo ids if the user wants the old layout back.
