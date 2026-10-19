"""
Reference model server for the labeling page's generic protocol (dataviz-label/1, see
dataviz/labeling/MODEL_API.md). It wraps Ultralytics .pt files (detect / segment / pose / classify).

    pip install ultralytics fastapi uvicorn python-multipart
    python examples/model_server_generic.py yolo11n.pt yolo11n-seg.pt --port 8010 [--api-key SECRET]

Then add a Model connection with URL http://<host>:8010 in the labeling page.
"""

import argparse
import io
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from PIL import Image, ImageOps
from ultralytics import YOLO

TASK_TYPES = {'detect': 'object_detection', 'segment': 'segmentation', 'pose': 'keypoints',
              'classify': 'classification', 'obb': 'object_detection'}
app = FastAPI(title='dataviz-label/1 reference server')
MODELS, API_KEY = {}, None


def check_key(authorization):
    if API_KEY and authorization != f'Bearer {API_KEY}':
        raise HTTPException(401, 'bad or missing API key')


@app.get('/info')
def info(authorization: str | None = Header(None)):
    check_key(authorization)
    out = []
    for mid, m in MODELS.items():
        d = {'id': mid, 'name': mid, 'task_type': TASK_TYPES.get(m.task, 'object_detection'),
             'classes': [m.names[i] for i in sorted(m.names)]}
        kpt = getattr(m.model, 'kpt_shape', None) or (m.model.yaml.get('kpt_shape') if hasattr(m.model, 'yaml') else None)
        if m.task == 'pose' and kpt:
            d['keypoints'] = [f'kp{i}' for i in range(kpt[0])]
        out.append(d)
    return {'protocol': 'dataviz-label/1', 'models': out}


@app.post('/predict')
def predict(file: UploadFile = File(...), model: str = Query(...), conf: float = Query(0.25, ge=0, le=1),
            authorization: str | None = Header(None)):
    check_key(authorization)
    m = MODELS.get(model)
    if m is None:
        raise HTTPException(404, f'no model {model!r}')
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(file.file.read()))).convert('RGB')   # same as the page
    r = m.predict(img, conf=conf, verbose=False)[0]
    names, preds = r.names, []
    if m.task == 'classify':
        for i in r.probs.top5:
            if float(r.probs.data[i]) >= conf:
                preds.append({'class_name': names[i], 'score': float(r.probs.data[i]), 'geometry_type': 'tag',
                              'geometry': {}})
        return {'predictions': preds}
    boxes = r.boxes if r.boxes is not None else []
    for k, b in enumerate(boxes):
        cls, score = names[int(b.cls)], float(b.conf)
        if m.task == 'segment' and r.masks is not None:
            pts = r.masks.xyn[k].tolist()
            if len(pts) >= 3:
                preds.append({'class_name': cls, 'score': score, 'geometry_type': 'polygon', 'geometry': {'points': pts}})
        elif m.task == 'pose' and r.keypoints is not None:
            xy, cf = r.keypoints.xyn[k].tolist(), r.keypoints.conf
            vis = cf[k].tolist() if cf is not None else [1.0] * len(xy)
            preds.append({'class_name': cls, 'score': score, 'geometry_type': 'keypoints',
                          'geometry': {'points': [[x, y, 2 if v >= 0.5 else (1 if v > 0 else 0)]
                                                  for (x, y), v in zip(xy, vis)]}})
        else:
            x1, y1, x2, y2 = b.xyxyn[0].tolist()
            preds.append({'class_name': cls, 'score': score, 'geometry_type': 'bbox',
                          'geometry': {'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1}})
    return {'predictions': preds, 'coords': 'norm'}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('weights', nargs='+', help='Ultralytics .pt files (the file stem is the model id)')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8010)
    ap.add_argument('--api-key', default=None, help='require Authorization: Bearer <key>')
    a = ap.parse_args()
    MODELS.update({Path(w).stem: YOLO(w) for w in a.weights})
    API_KEY = a.api_key
    uvicorn.run(app, host=a.host, port=a.port)
