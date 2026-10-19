# `dataviz/models/` — detectors scored against the dataset

You add a model in the page under **Settings › Models**. Use it in the **Model** tab: it runs once over
every image of the open dataset, and the results are cached in that dataset's workspace. After that,
every chart for any confidence threshold, match IoU or split is computed in well under a second.

| File | What it does |
|---|---|
| `registry.py` | `ModelRegistry`: the saved models, per user in `app.models` (API key + headers Fernet-encrypted; `env:` in them admin-only). `models(owner_id=)` / `model(mid, owner_id=)` / `upsert(body, owner_id)` / `delete(mid, owner_id)`; owner_id None = an internal lookup (a labeling task runs its project owner's model). `public()` masks the key and header values. Uploads stay on disk in `models/<id>/`. `check_path()` only accepts files under the allowed folders given to it or the upload folder. `MATRIX` lists which formats each family accepts. `discover()` finds trained runs and served models in the project. |
| `runners.py` | `load_runner(model)` returns a `Runner` whose `predict([PIL]) -> [(N,6) x1 y1 x2 y2 score cls]` gives boxes in original-image pixels. Covers every family × format pair (matrix below). Only one runner is kept loaded, to bound GPU memory. |
| `remote.py` | `ApiRunner`. Two kinds: this repo's `serving/` app (class names come from `GET /`), or a generic endpoint (image sent as multipart, raw or JSON base64; dotted paths say where boxes, scores and classes are in the reply). `LabelApiRunner`: a dataviz-label/1 server (preset `label`) scored like a detector (its boxes, and the extent of its polygons). |
| `schema.py` | `CUSTOM_SPEC` (shown in the form), `ADAPTER_TEMPLATE`, `to_detections()` (with precise errors such as "looks like a raw YOLO head"), and `clean()`. |
| `predict.py` | The prediction job. It reads ahead while the model runs, keeps detections down to conf 0.001 so the PR curves work, and caches them in `<workspace>/predictions/<id>_<fingerprint>.npz`. It also maps classes **by name** (`mapping()`), so a different class order (`lb_dataset` swaps 13 and 14) is still scored correctly. |
| `evaluate.py` | Pure numpy. `ap_stats` (AP50, AP50-95, PR / P / R / F1 vs confidence, TP/FP histograms) matches ultralytics `val`. `outcome` gives each label a status of found, wrong class, badly placed or missed, and each prediction a kind of tp, wrong_cls, fp_loc, fp_dup or fp_bg. Also `summary` (per-class table, confusion matrix) and `label_issues` (likely label errors). |
| `splits.py` | Maps each image to train or val, using the split dataset the models were trained on (`models.split_source`, default `dataset_split/`). |
| `insights.py` | The Evaluation tab. `leaderboard()` scores every model that ran (best-F1 or fixed confidence) and finds the per-class winners and the fastest model near the best. `dataset_insights()` explains one model: training labels vs AP, train/val gap, sessions, worst images, box IoU, calibration (ECE and bias), COCO size buckets, and agreement with the kNN label check. `findings()` turns these into ranked plain-language findings with click-through actions. |
| `hub.py` | **One registry for both pages.** `adapter(registry, m, workdir)` gives the labeling adapters' interface (`predict` / `predict_batch` → normalised shapes) for any saved model: local weights through the shared runner (every call holds `runners._lock`, so the visualizer's predict job and the label page never close each other's runner), serving / generic APIs through their own `ApiRunner`, a `label` API (dataviz-label/1) through `labeling/models.py`. `info()` gives the link dialog its classes. |
| `service.py` | `ModelService`: what the routes call. It caches predictions and AP in memory per `State.tag`, submits the job, runs the one-image test and computes `evaluate()`. |

## Family × format

| | YOLO | RF-DETR | Custom |
|---|---|---|---|
| `.pt` | `ultralytics.YOLO` | `RFDETR<size>(num_classes, resolution)` + state dict; size, resolution and class names come from this repo's checkpoints | TorchScript, or a pickled module |
| `.pth` / `.pkl` | a checkpoint or model is re-saved as `.pt`; a state dict needs `arch` | as `.pt` | a module, or a state dict plus adapter `build_model()` |
| `.safetensors` | `arch` (yaml or base .pt) + `load_state_dict` | size + state dict | adapter `build_model()` |
| ONNX | `ultralytics.YOLO(x.onnx)` | onnxruntime + sigmoid top-300 | onnxruntime, output A or B |
| OpenVINO | `ultralytics.YOLO(dir)` | OpenVINO + top-300 | OpenVINO, output A or B |
| API | serving app / generic | same | same |

Custom output: **A** is `(N,6)` or `(B,N,6)` `[x1,y1,x2,y2,score,class_id]` in letterboxed-input pixels.
**B** is `{boxes, scores, labels}`, or a list of those. An optional adapter `.py` can supply
`build_model`, `preprocess` and `postprocess`. The page shows all of this and offers the template for
download.

## Checked against the training logs (`dataset/`, val split)

| model | mAP50 here | mAP50 in `results.csv` | mAP50-95 here | mAP50-95 in log |
|---|---|---|---|---|
| wd-yolo11n-21 `.pt` | 0.979 | 0.980 | 0.898 | 0.891 |
| same, ONNX / OpenVINO / safetensors / adapter | 0.980 / 0.980 / 0.979 / 0.981 | | | |
| wd-rfdetr-l-3 `.pt` | 0.984 | 0.984 | 0.884 | 0.875 |

On **all** images the same YOLO model scores 0.992 / 0.931. About 90% of `dataset/` is in the training
split, which is why the tab defaults to **val**.
