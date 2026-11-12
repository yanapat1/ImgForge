---
name: split-train-val-test
description: split a YOLO dataset into train / val / test folders (images and labels moved together)
when: the user asks to split, make a validation set, or re-balance splits
---
1. `detect_format` and `get_stats` on the dataset: confirm a YOLO layout and see the current splits and counts.
2. Agree the ratio (default 80 / 10 / 10) with ask_user when not given; for small classes say that a random split can
   leave a class out of val.
3. `list_folder` `images/` (page through with offset) to get every image path. Group frames of the same scene or
   camera (shared name prefix, e.g. "cam1_000123") so near-identical frames stay on one side: a split by group avoids
   train / val leakage.
4. `todo` the moves. `create_folder` `images/train/`, `images/val/`, `images/test/` and the same under `labels/`.
5. `move` each image batch to its split folder and its label files (same stems) to the mirrored `labels/` folder in
   the same step. Background images (no label) go too.
6. Update `data.yaml` (`read_text`, then `write_text_file` with `train:` / `val:` / `test:` paths).
7. `get_stats` again and report images per split; offer `undo` ids. Suggest checking leakage in the visualizer
   (Curation › Train / val leakage) after `send_to_session`.
