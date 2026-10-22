"""
One model registry for the visualizer and the labeling page.

The models saved in Settings › Models (models/registry.py: local weights of every family / format, and HTTP APIs)
can label images on the labeling page too. adapter(m) returns an object with the labeling adapters' interface
(labeling/models.py), so the label routes treat a registry model and a model-server connection the same way:

    predict(image bytes, conf, (w, h), name) -> [{class_name, score, geometry_type, geometry (normalised)}]
    predict_batch([(image bytes, (w, h), name)], conf) -> [predictions | Exception]   (one per image)

    local weights          runners.get_runner (ONE loaded runner, shared with the visualizer's predict job: every
                           call holds runners._lock, so neither side ever sees its runner closed mid-batch)
    API serving / generic  remote.ApiRunner, one per model, kept here (not in the single-runner slot: a network
                           model never unloads the GPU one)
    API label              a dataviz-label/1 server (labeling/models.py GenericAdapter): every task type

Images are EXIF-transposed before a runner sees them: labeling coordinates are relative to the oriented image.
"""

import io
import threading

from PIL import Image, ImageOps

from . import runners

_api = {}                        # (model id, fingerprint) -> ApiRunner
_api_lock = threading.Lock()


def is_label_api(m):
    return m.get('format') == 'api' and (m.get('api') or {}).get('preset') == 'label'


def label_conn(m):
    a = m.get('api') or {}
    return {'url': str(a.get('url') or '').rstrip('/'), 'api_key': a.get('api_key') or '',
            'timeout_s': float(a.get('timeout') or 60)}


def label_model_id(m):
    """The model on a dataviz-label/1 server: the saved one, else the server's only model."""
    from ..labeling import models as lm
    mid = (m.get('api') or {}).get('label_model')
    if mid:
        return mid
    ms = lm.detect(label_conn(m))['models']
    if len(ms) != 1:
        raise lm.ModelError(f'this server has {len(ms)} models: name one in Settings › Models › {m.get("name")} '
                            f'({", ".join(x["id"] for x in ms) or "none"})')
    return ms[0]['id']


def _api_runner(m, fp):
    from .remote import ApiRunner
    key = (m['id'], fp)
    with _api_lock:
        r = _api.get(key)
        if r is None:
            for k in [k for k in _api if k[0] == m['id']]:      # an edited model: drop its old client
                try:
                    _api.pop(k).close()
                except Exception:  # noqa: BLE001 - best effort
                    pass
            r = _api[key] = ApiRunner(m)
        return r


def forget(mid):
    """A model was edited or deleted: drop its cached API client."""
    with _api_lock:
        for k in [k for k in _api if k[0] == mid]:
            try:
                _api.pop(k).close()
            except Exception:  # noqa: BLE001
                pass


class RunnerAdapter:
    """Boxes from a Runner ((N, 6) x1 y1 x2 y2 score class in pixels) as labeling predictions."""
    protocol = 'registry'

    def __init__(self, m, fp, workdir):
        self.m, self.fp, self.workdir = m, fp, workdir
        self.local = m.get('format') != 'api'

    def _run(self, images):
        if self.local:
            with runners._lock:
                r = runners.get_runner(self.m, self.fp, self.workdir)
                return r.predict(images), list(r.names or [])
        r = _api_runner(self.m, self.fp)
        return r.predict(images), list(r.names or [])

    def names(self):
        if self.local:
            with runners._lock:
                return list(runners.get_runner(self.m, self.fp, self.workdir).names or [])
        return list(_api_runner(self.m, self.fp).names or [])

    def predict(self, image, conf=None, size=None, name='image.jpg'):
        res = self.predict_batch([(image, size, name)], conf)[0]
        if isinstance(res, Exception):
            raise res
        return res

    def predict_batch(self, items, conf=None):
        from ..labeling import models as lm
        out, ims, at = [None] * len(items), [], []
        for i, (data, _, _) in enumerate(items):
            try:
                with Image.open(io.BytesIO(data)) as im:
                    ims.append(ImageOps.exif_transpose(im).convert('RGB'))
                at.append(i)
            except Exception as e:  # noqa: BLE001 - an unreadable image fails alone
                out[i] = lm.ModelError(f'cannot read the image: {e}', 502)
        if ims:
            try:
                dets, names = self._run(ims)
            except Exception as e:  # noqa: BLE001 - the model failed on the batch: every image of it
                err = e if isinstance(e, lm.ModelError) else lm.ModelError(f'{self.m.get("name")}: {type(e).__name__}: {e}', 502)
                for i in at:
                    out[i] = err
                return out
            thr = float(conf if conf is not None else self.m.get('conf') or 0.25)
            for i, im, det in zip(at, ims, dets):
                w, h = im.size
                preds = []
                for x1, y1, x2, y2, score, c in det.tolist():
                    if score < thr:
                        continue
                    c = int(c)
                    preds.append({'class_name': names[c] if 0 <= c < len(names) else str(c), 'score': float(score),
                                  'geometry_type': 'bbox',
                                  'geometry': {'x': x1 / w, 'y': y1 / h, 'w': (x2 - x1) / w, 'h': (y2 - y1) / h}})
                out[i] = lm.clean_predictions(preds, None, 'norm')
        return out


def adapter(registry, m, workdir):
    """The labeling-style adapter of registry model `m`."""
    if is_label_api(m):
        from ..labeling import models as lm
        return lm.adapter(label_conn(m), label_model_id(m))
    return RunnerAdapter(m, registry.fingerprint(m), workdir)


def info(registry, m, workdir):
    """What the label page's link dialog shows for a registry model: {id, name, task_type, geometry, classes,
    keypoints?, versions}. Local weights are loaded to read their class names (the next call reuses them)."""
    if is_label_api(m):
        from ..labeling import models as lm
        _, mm = lm.find_model(label_conn(m), label_model_id(m), True)
        return {**mm, 'id': m['id'], 'name': m['name'], 'versions': [], 'remote_id': mm['id']}
    names = RunnerAdapter(m, registry.fingerprint(m), workdir).names()
    return {'id': m['id'], 'name': m['name'], 'task_type': 'object_detection', 'geometry': 'bbox', 'classes': names,
            'versions': []}


def task_types(m):
    """Task types a registry model can label without asking it (label-protocol models: the one it declares)."""
    if is_label_api(m):
        return [m.get('task_type') or 'object_detection']
    if m.get('family') == 'hf':
        return []                      # a backbone that only embeds (the plot's feature list): labels nothing
    return ['object_detection']
