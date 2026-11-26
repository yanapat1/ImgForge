---
title: Dataset formats (what detect_format reports)
summary: yolo-detect / seg / pose / obb, coco, voc, classification, images, embeddings, other, empty
core: true
---
- `yolo-detect` / `yolo-seg` / `yolo-pose` / `yolo-obb`: `images/` and a mirrored `labels/` (one `.txt` per image),
  usually a `data.yaml` with class names; label lines have 5 columns (detect), 5 + 3k (pose), 9 (obb) or an odd
  number ≥ 7 of polygon coordinates (seg). Splits are sub-folders `train/`, `val/`, `test/` under images/ and labels/.
- `coco`: a JSON with `images`, `annotations` and `categories`. `voc`: `Annotations/*.xml` next to the images.
- `classification`: one sub-folder of images per class. `images`: images with no labels (stray non-image
  files up to 1 % of the images are ignored with a warning check, more makes it `other`). `embeddings`: `.npy`
  vector files. `other`: none of these (its reason names the first file that broke the layout; sending it to the
  visualizer fails with that reason). `empty`: nothing inside.
- `detect_format` returns the format, a confidence, the checks that decided it (images/, labels/ mirror, data.yaml,
  ≤ 50 label files sampled for their column counts) and the class count; results are cached (force re-detects;
  the page: Re-detect). The bucket root is re-detected after every change and tagged `format:<format>`.
- **Re-format** (`reformat_folder` bucket, path, to: yolo | coco | voc | labelme | createml | csv; the Bucket and Create
  session pages offer it too): writes the folder again in another label format as a new folder `<path>-<to>` of the
  managed bucket "<bucket> · formats"; the original is never changed. It reads YOLO detect / segment / pose and COCO
  labels (images only, VOC, OBB, classification: nothing to convert). A local or downloaded folder gets a complete
  dataset (images hard-linked, no extra disk). A **streamed S3** folder gets the labels only plus `images.txt` (the
  s3:// address of each image) and `source.json`: nothing is downloaded and nothing is written to S3, so a training
  tool needs the images downloaded first (`download_s3_folders`). Re-formatted to YOLO, that folder opens in
  Visualize, Label and the linked pair (session check format `s3-labels`, "YOLO labels · images in S3"): the images
  stream from S3; Visualize reads and fixes the local labels, a Label session imports them. S3 is never written. It
  is a job: `job_status`.
