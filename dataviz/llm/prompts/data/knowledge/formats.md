---
title: Dataset formats (what detect_format reports)
summary: yolo-detect / seg / pose / obb, coco, voc, classification, images, embeddings, other, empty
core: true
---
- `yolo-detect` / `yolo-seg` / `yolo-pose` / `yolo-obb`: `images/` and a mirrored `labels/` (one `.txt` per image),
  usually a `data.yaml` with class names; label lines have 5 columns (detect), 5 + 3k (pose), 9 (obb) or an odd
  number ≥ 7 of polygon coordinates (seg). Splits are sub-folders `train/`, `val/`, `test/` under images/ and labels/.
- `coco`: a JSON with `images`, `annotations` and `categories`. `voc`: `Annotations/*.xml` next to the images.
- `classification`: one sub-folder of images per class. `images`: images with no labels. `embeddings`: `.npy`
  vector files. `other`: none of these. `empty`: nothing inside.
