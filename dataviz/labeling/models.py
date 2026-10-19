"""
Model adapters: the labeling page sends images to a model server the user hosts and stores what comes back.
The app never runs a model itself. Two APIs are spoken (MODEL_API.md), chosen by detect():

    generic   GET /info -> {protocol: 'dataviz-label/1', models}; POST /predict?model=&conf= (multipart `file`)
    serving   the YOLO serving API in <repo>/serving: GET / -> [{route, versions, classes, ...}];
              POST /{version}/{route}/predict (raw body), POST .../predict/batch (repeated `files`)

Every prediction leaves an adapter in one normalised form, validated with the store's own geometry rules
(clamped to 0..1, NaN / empty shapes dropped, at most MAX_SHAPES per image, highest scores kept):
    {class_name, score, geometry_type, geometry}

One httpx.Client per connection (keep-alive, pooled), shared by every request and thread: a label-all job
keeps IN_FLIGHT batch calls open on it, so the TCP / TLS handshake is paid once.
"""

import threading
import time

import httpx
import orjson

from ..core import logging as vlog
from .store import normalize_geometry

_log = vlog.get_logger('model')

PROTOCOL = 'dataviz-label/1'
MAX_SHAPES = 1000
BATCH = 8                       # images per /predict/batch call (label-all)
IN_FLIGHT = 4                   # batch calls open at once
DETECT_TTL = 300                # s: detect results are reused by predict calls (version / protocol lookup)

TASK_GEOMETRY = {'object_detection': 'bbox', 'segmentation': 'polygon', 'keypoints': 'keypoints',
                 'classification': 'tag'}
_GEOMS = ('bbox', 'polygon', 'keypoints', 'tag')

NOT_SUPPORTED = ('not a supported model API. Expected either GET {url}/info returning '
                 '{"protocol": "dataviz-label/1", "models": [...]} (see MODEL_API.md), or the YOLO serving API: '
                 'GET {url}/ returning a list of {"route", "versions", "classes"}')


class ModelError(ValueError):
    """A model server problem with a human message (unreachable, wrong API, bad answer). status: HTTP code the
    route returns (400 for a setup problem, 502 when the server failed a request)."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- clients
_clients = {}
_clients_lock = threading.Lock()
_detected = {}                  # client key -> (time, detect result)


def _key(conn):
    return (str(conn.get('url') or '').rstrip('/'), conn.get('api_key') or '', float(conn.get('timeout_s') or 60))


def client_for(conn):
    """The shared keep-alive client of a connection (a changed url / key / timeout gets a new one)."""
    k = _key(conn)
    if not k[0].startswith(('http://', 'https://')):
        raise ModelError('the model connection has no http(s) URL')
    c = _clients.get(k)
    if c is None:
        with _clients_lock:
            c = _clients.get(k)
            if c is None:
                headers = {'Authorization': f'Bearer {k[1]}'} if k[1] else {}
                c = _clients[k] = httpx.Client(
                    base_url=k[0], headers=headers, follow_redirects=True,
                    timeout=httpx.Timeout(k[2], connect=min(10.0, k[2])),
                    limits=httpx.Limits(max_connections=2 * IN_FLIGHT, max_keepalive_connections=2 * IN_FLIGHT))
    return c


def close_clients():
    with _clients_lock:
        for c in _clients.values():
            c.close()
        _clients.clear()
        _detected.clear()


def _get(conn, path):
    try:
        return client_for(conn).get(path)
    except httpx.HTTPError as e:
        raise ModelError(f'cannot reach the model server at {_key(conn)[0]}: {type(e).__name__}: {e}') from None


def _json(r):
    try:
        return orjson.loads(r.content)
    except orjson.JSONDecodeError:
        return None


def _check(r, what):
    if r.status_code >= 400:
        body = _json(r)
        detail = body.get('detail') if isinstance(body, dict) else None
        raise ModelError(f'{what}: the model server answered {r.status_code}'
                         + (f': {detail}' if detail else ''), 400 if r.status_code in (401, 403, 404) else 502)
    body = _json(r)
    if body is None:
        raise ModelError(f'{what}: the model server did not answer JSON', 502)
    return body


# ---------------------------------------------------------------- detect
def _generic_models(info):
    out = []
    for m in info.get('models') or []:
        if not isinstance(m, dict) or not m.get('id'):
            continue
        tt = m.get('task_type') if m.get('task_type') in TASK_GEOMETRY else 'object_detection'
        out.append({'id': str(m['id']), 'name': str(m.get('name') or m['id']), 'task_type': tt,
                    'geometry': TASK_GEOMETRY[tt], 'classes': [str(c) for c in m.get('classes') or []],
                    'versions': [str(v) for v in m.get('versions') or []],
                    **({'keypoints': [str(k) for k in m['keypoints']]} if m.get('keypoints') else {})})
    return out


def _serving_classes(classes):
    if isinstance(classes, dict):
        items = []
        for k, v in classes.items():
            try:
                items.append((int(k), str(v)))
            except (TypeError, ValueError):
                continue
        return [v for _, v in sorted(items)]
    return [str(c) for c in classes or []]


def _serving_models(listing):
    out = []
    for m in listing:
        route = str(m['route']).strip('/')
        parts = route.split('/')
        # route = <provider>/<type>/<name>, but the serving API only ever answers with boxes (_serving_preds), so
        # every serving model is a detector; the type segment travels as route_type for display only
        out.append({'id': route, 'name': route, 'task_type': 'object_detection', 'geometry': 'bbox',
                    'route_type': parts[1].lower() if len(parts) > 1 else '',
                    'classes': _serving_classes(m.get('classes')), 'versions': [str(v) for v in m.get('versions') or []],
                    **({'imgsz': m['imgsz']} if m.get('imgsz') else {})})
    return out


def detect(conn, fresh=False):
    """{protocol: 'generic'|'serving', models:[{id, name, task_type, geometry, classes, versions, keypoints?,
    route_type? (serving: the route's type segment; serving models are always object_detection / bbox)}]}.
    Tries the generic GET /info first, then the serving GET /. ModelError when neither answers as expected."""
    k = _key(conn)
    hit = _detected.get(k)
    if hit and not fresh and time.time() - hit[0] < DETECT_TTL:
        return hit[1]
    r = _get(conn, '/info')
    body = _json(r) if r.status_code < 400 else None
    if isinstance(body, dict) and body.get('protocol') == PROTOCOL:
        res = {'protocol': 'generic', 'models': _generic_models(body)}
    else:
        if r.status_code in (401, 403):
            raise ModelError(f'the model server refused the request ({r.status_code}): check the API key')
        r = _get(conn, '/')
        if r.status_code in (401, 403):
            raise ModelError(f'the model server refused the request ({r.status_code}): check the API key')
        body = _json(r) if r.status_code < 400 else None
        if isinstance(body, list) and body and all(isinstance(m, dict) and m.get('route') and 'classes' in m
                                                   for m in body):
            res = {'protocol': 'serving', 'models': _serving_models(body)}
        else:
            raise ModelError(NOT_SUPPORTED.replace('{url}', k[0]))
    _detected[k] = (time.time(), res)
    return res


def find_model(conn, model_id, fresh=False):
    """(protocol, model info) of `model_id` on this connection; ModelError when the server no longer has it."""
    info = detect(conn, fresh)
    for m in info['models']:
        if m['id'] == model_id:
            return info['protocol'], m
    if not fresh:
        return find_model(conn, model_id, True)
    raise ModelError(f'the model server has no model {model_id!r} (it has: '
                     f'{", ".join(m["id"] for m in info["models"]) or "none"})')


# ---------------------------------------------------------------- normalisation
def _num(v):
    v = float(v)
    if v != v or v in (float('inf'), float('-inf')):
        raise ValueError('not a finite number')
    return v


def _to_norm(gtype, g, w, h):
    """Pixel geometry -> normalised (generic `"coords": "px"`)."""
    if gtype == 'bbox':
        return {'x': _num(g['x']) / w, 'y': _num(g['y']) / h, 'w': _num(g['w']) / w, 'h': _num(g['h']) / h}
    if gtype in ('polygon', 'keypoints'):
        pts = [[_num(p[0]) / w, _num(p[1]) / h, *p[2:3]] for p in g.get('points') or []]
        out = {'points': pts}
        if g.get('holes'):
            out['holes'] = [[[_num(p[0]) / w, _num(p[1]) / h] for p in hole] for hole in g['holes']]
        return out
    return g


def clean_predictions(preds, size=None, coords='norm', n_keypoints=None):
    """Validated predictions: [{class_name, score, geometry_type, geometry}] with geometry normalised and
    clamped by store.normalize_geometry; invalid ones are dropped (counted in the log), at most MAX_SHAPES
    (highest scores first). size = (w, h) in pixels, needed when coords == 'px'."""
    out, bad = [], 0
    task = {'config': {'keypoints': [''] * n_keypoints} if n_keypoints else {}}
    for p in preds or []:
        try:
            gtype = p.get('geometry_type') or 'bbox'
            if gtype not in _GEOMS:
                raise ValueError(f'geometry type {gtype!r}')
            name = p.get('class_name')
            if name is None or str(name) == '':
                raise ValueError('no class name')
            g = p.get('geometry') if gtype != 'tag' else {}
            if not isinstance(g, dict):
                raise ValueError('geometry is not an object')
            if coords == 'px' and gtype != 'tag':
                if not size or not size[0] or not size[1]:
                    raise ValueError('pixel coordinates without an image size')
                g = _to_norm(gtype, g, float(size[0]), float(size[1]))
            geom, _ = normalize_geometry(gtype, g, task)
            score = p.get('score')
            try:
                score = None if score is None else round(min(1.0, max(0.0, _num(score))), 4)
            except (TypeError, ValueError):
                score = None
            out.append({'class_name': str(name), 'score': score, 'geometry_type': gtype, 'geometry': geom})
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            bad += 1
    if bad:
        _log.debug(f'{bad} invalid prediction(s) dropped')
    if len(out) > MAX_SHAPES:
        out.sort(key=lambda p: -(p['score'] or 0))
        out = out[:MAX_SHAPES]
    return out


def _serving_preds(dets, w, h):
    out = []
    for d in dets or []:
        try:
            x1, y1, x2, y2 = (float(v) for v in d['box'][:4])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({'class_name': d.get('class_name', d.get('class_id')), 'score': d.get('score'),
                    'geometry_type': 'bbox', 'geometry': {'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1}})
    return clean_predictions(out, (w, h), 'px')


# ---------------------------------------------------------------- adapters
class GenericAdapter:
    protocol = 'generic'

    def __init__(self, conn, model):
        self.conn, self.model = conn, model
        self.c = client_for(conn)
        self.n_kpt = len(model.get('keypoints') or []) or None

    def predict(self, image, conf=None, size=None, name='image.jpg'):
        params = {'model': self.model['id']}
        if conf is not None:
            params['conf'] = conf
        try:
            r = self.c.post('/predict', params=params, files={'file': (name, image, 'application/octet-stream')})
        except httpx.HTTPError as e:
            raise ModelError(f'model request failed: {type(e).__name__}: {e}', 502) from None
        body = _check(r, 'predict')
        if not isinstance(body, dict) or not isinstance(body.get('predictions'), list):
            raise ModelError('predict: the answer has no "predictions" list', 502)
        return clean_predictions(body['predictions'], size, 'px' if body.get('coords') == 'px' else 'norm',
                                 self.n_kpt)

    def predict_batch(self, items, conf=None):
        """items: [(image bytes, (w, h), name)] -> [predictions | Exception] (the generic API has no batch call)."""
        out = []
        for image, size, name in items:
            try:
                out.append(self.predict(image, conf, size, name))
            except Exception as e:  # noqa: BLE001 - one bad image must not fail its batch
                out.append(e)
        return out


class ServingAdapter:
    protocol = 'serving'

    def __init__(self, conn, model, version=None):
        self.conn, self.model = conn, model
        self.c = client_for(conn)
        versions = model.get('versions') or ['v1']
        self.version = version if version in versions else versions[0]
        self.base = f'/{self.version}/{model["id"]}'

    def predict(self, image, conf=None, size=None, name='image.jpg'):
        params = {} if conf is None else {'conf': conf}
        try:
            r = self.c.post(f'{self.base}/predict', params=params, content=image,
                            headers={'Content-Type': 'application/octet-stream'})
        except httpx.HTTPError as e:
            raise ModelError(f'model request failed: {type(e).__name__}: {e}', 502) from None
        body = _check(r, 'predict')
        im = body.get('image') or {}
        w, h = im.get('width') or (size or (0, 0))[0], im.get('height') or (size or (0, 0))[1]
        return _serving_preds(body.get('detections'), w, h)

    def predict_batch(self, items, conf=None):
        """One /predict/batch call for up to BATCH images; the batch answer has no image size, so the stored
        (EXIF-oriented) media size converts the pixel boxes, as cv2.imdecode applies the EXIF rotation too."""
        params = {} if conf is None else {'conf': conf}
        files = [('files', (name, image, 'application/octet-stream')) for image, _, name in items]
        try:
            r = self.c.post(f'{self.base}/predict/batch', params=params, files=files)
        except httpx.HTTPError as e:
            raise ModelError(f'model request failed: {type(e).__name__}: {e}', 502) from None
        body = _check(r, 'predict batch')
        results = body.get('results') if isinstance(body, dict) else None
        if not isinstance(results, list) or len(results) != len(items):
            raise ModelError('predict batch: the answer does not have one result per image', 502)
        out = []
        for res, (_, size, _) in zip(results, items):
            if not isinstance(res, dict) or res.get('error'):
                out.append(ModelError(f'model failed on this image: {(res or {}).get("error")}', 502))
            elif not size or not size[0] or not size[1]:
                out.append(ModelError('the image size is unknown', 502))
            else:
                out.append(_serving_preds(res.get('detections'), *size))
        return out


def adapter(conn, model_id, version=None):
    """The adapter for a linked model (detects the protocol, cached DETECT_TTL)."""
    protocol, model = find_model(conn, model_id)
    if protocol == 'generic':
        return GenericAdapter(conn, model)
    return ServingAdapter(conn, model, version)
