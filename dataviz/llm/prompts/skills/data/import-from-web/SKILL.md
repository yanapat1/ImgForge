---
name: import-from-web
description: find and import a public dataset (Kaggle, Hugging Face, Roboflow, a URL, Zenodo, OpenML) into a bucket
when: the user asks to download / import / get a dataset from the internet
---
1. If the user gave no exact ref and web search is on, use `web_search` with general words only (task + object, e.g.
   "YOLO dataset traffic signs") and pick 2-3 candidates; otherwise ask_user for the link or ref.
2. `inspect_dataset_source` each candidate: title, size, files, license, versions. Show a short comparison and
   ask_user which one (size and license matter).
3. A required token missing (Kaggle, Roboflow): say which connection to add in Settings › Connections. Never ask for
   the token in the chat.
4. `import_dataset` with `source`, `ref`, a `name` for the new bucket (or `bucket` + `path` to import into an existing
   writable folder), `version` / `revision` / `patterns` / `format` as needed (Roboflow: a YOLO format such as
   yolov8 for detection).
5. It runs as a job: report the bucket it lands in. Afterwards `detect_format` and `get_stats`; offer
   `organise-yolo-dataset` if the format is not YOLO, `add_tags` (e.g. `source:kaggle`), or `send_to_session`.
