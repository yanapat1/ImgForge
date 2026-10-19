# Connecting your own model

> A model added in **Settings › Models** (local weights of any format, or an API: this repo's serving app, any JSON
> endpoint, or a server speaking the protocol below, preset *Labeling model server*) can be linked to a task too; it
> is the same model the visualizer's Model tab scores (`dataviz/models/hub.py`).
>
> **Nothing runs until it is activated.** A linked model labels images (one image, label-all, auto-label on open)
> only after **Activate** in the label page's model menu (Ctrl+M), and only for the running server: a restart, or
> saving the link again, turns it off. The server enforces it (409 otherwise).

The labeling page sends images to a model **you host** and draws what it returns. The app never runs the model. Add a
connection of kind **Model** (Settings › Connections, or inline from the label page's model menu) with the server's
URL and an optional API key (sent as `Authorization: Bearer <key>`). **Test** detects which API the server speaks.

Two APIs are supported; the app tries them in this order.

## 1. Generic protocol (`dataviz-label/1`): any task type
### `GET /info`
```json
{"protocol": "dataviz-label/1",
 "models": [{"id": "det-v3", "name": "Pets detector", "task_type": "object_detection", "classes": ["cat", "dog"]},
            {"id": "pose", "name": "Body pose", "task_type": "keypoints", "classes": ["person"],
             "keypoints": ["nose", "left_eye", "right_eye"]}]}
```
`task_type`: `object_detection` | `segmentation` | `keypoints` | `classification`.

### `POST /predict?model=<id>&conf=<0..1>`
Body: multipart `file` = the image bytes. Response:
```json
{"predictions": [
  {"class_name": "cat", "score": 0.91, "geometry_type": "bbox", "geometry": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25}},
  {"class_name": "house", "score": 0.8, "geometry_type": "polygon", "geometry": {"points": [[0.1, 0.1], [0.4, 0.1], [0.3, 0.5]]}},
  {"class_name": "person", "score": 0.7, "geometry_type": "keypoints", "geometry": {"points": [[0.5, 0.2, 2], [0.48, 0.18, 2], [0, 0, 0]]}},
  {"class_name": "outdoor", "score": 0.66, "geometry_type": "tag", "geometry": {}}
], "coords": "norm"}
```
- Coordinates are normalised 0-1 (`x, y` = top-left). Send `"coords": "px"` to give pixels instead; the app divides
  by the image size.
- Keypoint visibility: 0 = not labeled, 1 = occluded, 2 = visible.
- Shapes are clamped to the image; invalid ones are dropped; at most 1000 per image.

A complete reference server wrapping an Ultralytics `.pt` (detect / segment / pose / classify) is in
`examples/model_server_generic.py`.

## 2. The YOLO serving API (`serving/` in this project)
Detected when `GET /` returns a list of `{route, versions, classes, ...}`. Each route `<provider>/<type>/<name>` is one
model; `<type>` `obd`/`det` = object detection (`seg`, `pose`/`kpt`, `cls` map to the other task types). The app calls
`POST /<version>/<route>/predict?conf=` with the raw image body and reads
`detections[].{box:[x1,y1,x2,y2] px, score, class_name}`; the label-all job uses `/predict/batch` (8 images per call).

## Classes
When you link a model to a task, each model class is mapped to a task class (same names are matched automatically),
**added as a new class**, or **ignored**. Predictions of ignored classes are dropped.

## Model labels in the page
Model shapes are drawn dashed and their tag shows `🤖 <class> <score>`. Editing one makes it yours (manual).
**Remove model labels** deletes only model shapes, on this image or every image.
