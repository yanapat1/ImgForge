"""
Models behind an HTTP API.

    serving   this repo's serving/ app: POST {url}/{version}/{provider}/{model_type}/{model_name}/predict
              (multipart "file") -> {"detections": [{"box": [x1,y1,x2,y2], "score", "class_id", "class_name"}]}
    generic   any endpoint: URL, headers / API key, how the image is sent, and where the boxes are in the JSON
              (mapping: dotted paths + box format), see schema.CUSTOM_SPEC['api_generic']
    label     a dataviz-label/1 server (the labeling page's protocol, labeling/MODEL_API.md): its boxes (and the
              extent of its polygons) are scored like any detector
"""

import io
import base64
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .runners import Runner

SERVING_MAPPING = {'detections': 'detections', 'box': 'box', 'score': 'score', 'class_id': 'class_id',
                   'class_name': 'class_name', 'box_format': 'xyxy', 'normalized': False}


def dig(obj, path):
    """'result.objects' / 'data.0.boxes' -> the nested value ('' = obj itself)."""
    for part in [p for p in (path or '').split('.') if p]:
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            if part not in obj:
                raise KeyError(f'"{part}" not found (keys: {", ".join(list(obj)[:12])})')
            obj = obj[part]
        else:
            raise KeyError(f'cannot read "{part}" from {type(obj).__name__}')
    return obj


def parse(payload, mapping, w, h, name_to_id=None):
    """JSON response -> (N, 6) in image pixels + the class names it mentioned {id: name}."""
    mp = {**SERVING_MAPPING, **{k: v for k, v in (mapping or {}).items() if v not in (None, '')}}
    items = dig(payload, mp['detections'])
    if not isinstance(items, list):
        raise ValueError(f'"{mp["detections"]}" is not a list')
    rows, names = [], {}
    for it in items:
        b = [float(v) for v in dig(it, mp['box'])][:4]
        if mp['box_format'] == 'xywh':
            b = [b[0], b[1], b[0] + b[2], b[1] + b[3]]
        elif mp['box_format'] == 'cxcywh':
            b = [b[0] - b[2] / 2, b[1] - b[3] / 2, b[0] + b[2] / 2, b[1] + b[3] / 2]
        if str(mp.get('normalized')).lower() in ('true', '1'):
            b = [b[0] * w, b[1] * h, b[2] * w, b[3] * h]
        score = float(dig(it, mp['score'])) if mp.get('score') else 1.0
        cname = None
        if mp.get('class_name'):
            try:
                cname = str(dig(it, mp['class_name']))
            except KeyError:
                cname = None
        cid = None
        if mp.get('class_id'):
            try:
                cid = int(dig(it, mp['class_id']))
            except (KeyError, TypeError, ValueError):
                cid = None
        if cid is None and cname is not None:
            cid = (name_to_id or {}).setdefault(cname, len(name_to_id or {})) if name_to_id is not None else -1
        if cid is None:
            raise ValueError('neither class_id nor class_name found in a detection')
        if cname is not None:
            names[cid] = cname
        rows.append(b + [score, cid])
    return np.asarray(rows, np.float32).reshape(-1, 6), names


class ApiRunner(Runner):
    def __init__(self, m):
        import httpx
        self.a = dict(m.get('api') or {})
        self.preset = self.a.get('preset', 'generic')
        self.batch_size = int(self.a.get('concurrency') or 4)
        headers = {}
        for line in (self.a.get('headers') or '').splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip()] = v.strip()
        if self.a.get('api_key'):
            hdr = self.a.get('auth_header') or 'Authorization'
            headers[hdr] = (f'Bearer {self.a["api_key"]}' if hdr.lower() == 'authorization'
                            and not self.a['api_key'].lower().startswith(('bearer ', 'basic ')) else self.a['api_key'])
        self.client = httpx.Client(headers=headers, timeout=float(self.a.get('timeout') or 30), verify=False)
        self.pool = ThreadPoolExecutor(self.batch_size)
        self.name_to_id = {}
        self.seen_names = {}
        self.names = None
        self.server_names = False
        if self.preset == 'serving':
            self.url = (f'{self.a["url"].rstrip("/")}/{self.a.get("version") or "v1"}/{self.a.get("provider")}/'
                        f'{self.a.get("model_type")}/{self.a.get("model_name")}/predict')
            try:                                           # GET / lists the served models with their class names
                for info in self.client.get(self.a['url'].rstrip('/') + '/').json():
                    if info.get('route', '').strip('/').endswith(
                            f'{self.a.get("provider")}/{self.a.get("model_type")}/{self.a.get("model_name")}'):
                        cls = info.get('classes') or {}
                        self.names = [cls[k] for k in sorted(cls, key=int)]
                        self.server_names = True
            except Exception:  # noqa: BLE001 - names then come from class_name in each response
                pass
        else:
            self.url = self.a['url']

    def _one(self, im):
        buf = io.BytesIO()
        im.convert('RGB').save(buf, 'JPEG', quality=95)
        data = buf.getvalue()
        send = self.a.get('send') or 'multipart'
        field = self.a.get('field') or ('image_b64' if send == 'json' else 'file')
        if self.preset == 'serving' or send == 'multipart':
            r = self.client.post(self.url, files={field if self.preset != 'serving' else 'file':
                                                  ('image.jpg', data, 'image/jpeg')})
        elif send == 'raw':
            r = self.client.post(self.url, content=data, headers={'Content-Type': 'image/jpeg'})
        else:
            r = self.client.post(self.url, json={field: base64.b64encode(data).decode()})
        if r.status_code >= 400:
            raise RuntimeError(f'{self.url}: HTTP {r.status_code} {r.text[:200]}')
        return r.json()

    def raw(self, im):
        return self._one(im)

    def predict(self, images):
        payloads = list(self.pool.map(self._one, images))
        mapping = SERVING_MAPPING if self.preset == 'serving' else (self.a.get('mapping') or {})
        out = []
        for im, p in zip(images, payloads):
            det, names = parse(p, mapping, *im.size, name_to_id=self.name_to_id)
            self.seen_names.update(names)
            out.append(det)
        if not self.server_names and self.seen_names:   # names learnt from the responses so far (grows per batch)
            n = max(self.seen_names) + 1
            self.names = [self.seen_names.get(i, f'class_{i}') for i in range(n)]
        return out

    def close(self):
        self.pool.shutdown(wait=False)
        self.client.close()


class LabelApiRunner(Runner):
    """A dataviz-label/1 model scored in the visualizer: normalised shapes back to (N, 6) pixel boxes."""

    def __init__(self, m):
        from ..labeling import models as lm
        from .hub import label_conn, label_model_id
        self.ad = lm.adapter(label_conn(m), label_model_id(m))
        self.batch_size = int((m.get('api') or {}).get('concurrency') or 4)
        self.pool = ThreadPoolExecutor(self.batch_size)
        self.name_to_id = {}
        self.names = list(self.ad.model.get('classes') or []) or None
        if self.names:
            self.name_to_id = {n: i for i, n in enumerate(self.names)}

    def _one(self, im):
        from .runners import LOW_CONF
        buf = io.BytesIO()
        im.convert('RGB').save(buf, 'JPEG', quality=95)
        w, h = im.size
        rows = []
        for p in self.ad.predict(buf.getvalue(), LOW_CONF, (w, h), 'image.jpg'):
            g, t = p['geometry'], p['geometry_type']
            if t == 'bbox':
                x1, y1, x2, y2 = g['x'], g['y'], g['x'] + g['w'], g['y'] + g['h']
            elif t == 'polygon' and g.get('points'):
                xs, ys = [q[0] for q in g['points']], [q[1] for q in g['points']]
                x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            else:
                continue
            name = str(p['class_name'])
            cid = self.name_to_id.setdefault(name, len(self.name_to_id))
            rows.append([x1 * w, y1 * h, x2 * w, y2 * h, float(p.get('score') or 1.0), cid])
        return np.asarray(rows, np.float32).reshape(-1, 6)

    def predict(self, images):
        out = list(self.pool.map(self._one, images))
        if self.name_to_id:
            inv = {i: n for n, i in self.name_to_id.items()}
            self.names = [inv.get(i, f'class_{i}') for i in range(len(inv))]
        return out

    def close(self):
        self.pool.shutdown(wait=False)
