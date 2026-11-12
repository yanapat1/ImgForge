---
title: Importing datasets from the web
summary: Kaggle, Hugging Face, Roboflow, a URL, Zenodo, OpenML: refs, tokens, options, what the import job does
---
Always `inspect_dataset_source` first (title, size, files, license, versions), then `import_dataset`.
| source | ref | token (Settings › Connections) | options |
|---|---|---|---|
| `kaggle` | owner/dataset (or its URL), owner/dataset/versions/3, competitions/name | required | version |
| `huggingface` | user/dataset, user/dataset@main, the link | optional (gated / private) | revision, patterns |
| `roboflow` | workspace/project/version or a Universe link | required | format (yolov11, yolov8, yolov5pytorch, yolov8-obb, coco, voc, createml, darknet, folder …) |
| `url` | a direct link to a file or archive (Google Drive, Dropbox, GitHub links work) | none | |
| `zenodo` | a record id, its DOI or link | optional (restricted) | patterns (e.g. *.zip) |
| `openml` | a dataset id or its link | none | format (parquet, else ARFF) |
- The job (activity strip): download (resumable, parallel parts, internal addresses blocked), unpack archives (zip,
  tar.*, gz, 7z; `extract` default true, `nested` unpacks archives inside), detect the format and tag it.
- While a Bucket job (import, frames, sync, export, …) runs, sessions still open at once (their own job
  lane). Each account's Bucket jobs run one after another; other accounts' jobs do not wait for them.
- Into a **new managed bucket** (named from the ref, or `name`) or into a folder of a writable `bucket` (`path`).
  A new bucket whose import fails or is cancelled is removed; while it runs the bucket is busy (writes answer 409).
- A missing / wrong token: the error says which connection kind to add (Settings › Connections). Never ask the user
  to paste a token into the chat.
- Check the license before using a dataset commercially; Roboflow exports in the format you pick, so choose a YOLO
  one for training with ultralytics.
