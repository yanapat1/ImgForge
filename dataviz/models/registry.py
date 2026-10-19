"""
Saved models (Settings › Models in the page).

Stored in <workspace_root>/models.json (chmod 600: API keys live here; public() masks them).

    {"models": [{id, name, family: yolo|rfdetr|custom, format: pt|pth|pkl|safetensors|onnx|openvino|api,
                 path, variant, arch, adapter, imgsz, conf, iou, task_type, api: {...}, created}]}

API presets: serving (this repo's serving/ app), generic (any JSON endpoint), label (a dataviz-label/1 server, the
labeling page's protocol: any task type; api.label_model names the model on it). The same models label images on
the labeling page (models/hub.py).

Uploaded files go to <workspace_root>/models/<id>/.
"""

import os
import json
import time
import uuid
import threading
from pathlib import Path

FAMILIES = ('yolo', 'rfdetr', 'custom')
FORMATS = ('pt', 'pth', 'pkl', 'safetensors', 'onnx', 'openvino', 'api')
FIELDS = ('name', 'family', 'format', 'path', 'variant', 'arch', 'adapter', 'imgsz', 'conf', 'iou', 'api', 'task_type')
TASK_TYPES = ('object_detection', 'segmentation', 'keypoints', 'classification')
PRESETS = ('serving', 'generic', 'label')
API_FIELDS = ('preset', 'url', 'version', 'provider', 'model_type', 'model_name', 'headers', 'api_key', 'auth_header',
              'send', 'field', 'mapping', 'timeout', 'label_model')
RFDETR_VARIANTS = ('nano', 'small', 'medium', 'base', 'large', 'xlarge', '2xlarge')

# which formats each family accepts (the page greys out the rest)
MATRIX = {
    'yolo': ['pt', 'pth', 'pkl', 'safetensors', 'onnx', 'openvino', 'api'],
    'rfdetr': ['pt', 'pth', 'pkl', 'safetensors', 'onnx', 'openvino', 'api'],
    'custom': ['pt', 'pth', 'pkl', 'safetensors', 'onnx', 'openvino', 'api'],
}
EXT = {'.pt': 'pt', '.pth': 'pth', '.pkl': 'pkl', '.pickle': 'pkl', '.safetensors': 'safetensors', '.onnx': 'onnx',
       '.xml': 'openvino', '.torchscript': 'pt'}


def _mask(key):
    key = key or ''
    return (key[:2] + '•' * max(0, len(key) - 6) + key[-4:]) if len(key) > 6 else '•' * len(key)


def guess_format(path):
    p = Path(path)
    if p.is_dir() and any(p.glob('*.xml')):
        return 'openvino'
    return EXT.get(p.suffix.lower())


class ModelRegistry:
    def __init__(self, root, allowed_roots=()):
        self.root = Path(root)
        self.path = self.root / 'models.json'
        self.store = self.root / 'models'
        self.allowed_roots = [Path(r).resolve() for r in allowed_roots] + [self.store.resolve()]
        self._lock = threading.Lock()

    # ------------------------------------------------------------ storage
    def load(self):
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
        except ValueError:
            data = {}
        data.setdefault('models', [])
        return data

    def save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds API keys: never world-readable
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def models(self):
        return self.load()['models']

    def model(self, mid):
        for m in self.models():
            if m['id'] == mid:
                return m
        raise KeyError(f'no model {mid!r}')

    @staticmethod
    def public(m):
        out = {k: v for k, v in m.items() if k != 'api'}
        if m.get('api'):
            a = dict(m['api'])
            a['has_key'] = bool(a.get('api_key'))
            a['api_key'] = _mask(a.get('api_key'))
            out['api'] = a
        return out

    def check_path(self, p):
        """Only files under the project root or the upload store: .pkl / full torch.load run code."""
        rp = Path(p).expanduser().resolve()
        if not any(rp == r or r in rp.parents for r in self.allowed_roots):
            raise ValueError(f'{rp} is outside the allowed folders ({", ".join(map(str, self.allowed_roots))})')
        if not rp.exists():
            raise ValueError(f'{rp} does not exist')
        return str(rp)

    def upsert(self, body):
        base = {}
        if body.get('id'):
            try:
                base = dict(self.model(body['id']))
            except KeyError:
                pass
        m = dict(base)
        for k in FIELDS:
            if k in body and body[k] is not None:
                m[k] = body[k]
        if m.get('family') not in FAMILIES:
            raise ValueError(f'family must be one of {", ".join(FAMILIES)}')
        if m.get('format') not in FORMATS:
            raise ValueError(f'format must be one of {", ".join(FORMATS)}')
        if m['format'] not in MATRIX[m['family']]:
            raise ValueError(f'{m["family"]} models cannot be {m["format"]}')
        if m['format'] == 'api':
            api = {k: v for k, v in (body.get('api') or {}).items() if k in API_FIELDS}
            old = base.get('api') or {}
            if not api.get('api_key'):                      # blank key in the form = keep the saved one
                api['api_key'] = old.get('api_key', '')
            api['preset'] = api.get('preset') if api.get('preset') in PRESETS else 'generic'
            if not api.get('url'):
                raise ValueError('API URL is required')
            m['api'] = api
            m.pop('path', None)
        else:
            if not m.get('path'):
                raise ValueError('model file path is required')
            m['path'] = self.check_path(m['path'])
            if m.get('adapter'):
                m['adapter'] = self.check_path(m['adapter'])
            if m.get('arch') and Path(m['arch']).suffix:   # a file (yaml / base .pt); otherwise a name
                m['arch'] = self.check_path(m['arch'])
            m.pop('api', None)
        # only a dataviz-label/1 server labels other task types; weights and the other APIs give boxes
        if m['format'] == 'api' and m['api']['preset'] == 'label':
            m['task_type'] = m.get('task_type') if m.get('task_type') in TASK_TYPES else 'object_detection'
        else:
            m['task_type'] = 'object_detection'
        if m['family'] == 'rfdetr' and m.get('variant') and m['variant'] not in RFDETR_VARIANTS:
            raise ValueError(f'RF-DETR variant must be one of {", ".join(RFDETR_VARIANTS)}')
        for k, typ in (('imgsz', int), ('conf', float), ('iou', float)):
            if m.get(k) in ('', None):
                m.pop(k, None)
            else:
                m[k] = typ(m[k])
        m['name'] = (m.get('name') or '').strip() or (Path(m.get('path', '')).stem if m.get('path') else 'api model')
        with self._lock:
            data = self.load()
            m['id'] = body.get('id') or uuid.uuid4().hex[:10]
            m.setdefault('created', time.strftime('%Y-%m-%d %H:%M:%S'))
            if any(o['name'] == m['name'] and o['id'] != m['id'] for o in data['models']):
                raise ValueError(f'a model named {m["name"]!r} already exists')
            data['models'] = [o for o in data['models'] if o['id'] != m['id']] + [m]
            self.save(data)
        return m

    def delete(self, mid):
        with self._lock:
            data = self.load()
            data['models'] = [m for m in data['models'] if m['id'] != mid]
            self.save(data)

    def upload_dir(self):
        d = self.store / uuid.uuid4().hex[:10]
        d.mkdir(parents=True, exist_ok=True)
        return d

    def fingerprint(self, m):
        """Changes when the weights or anything that changes the predictions changes."""
        parts = [m['family'], m['format'], str(m.get('imgsz')), str(m.get('variant')), str(m.get('arch'))]
        if m['format'] == 'api':
            a = m.get('api') or {}
            parts += [a.get('url', ''), a.get('model_name', ''), json.dumps(a.get('mapping') or {}, sort_keys=True),
                      a.get('preset', ''), a.get('label_model', '')]
        else:
            for p in (m.get('path'), m.get('adapter')):
                if p and Path(p).exists():
                    pp = Path(p)
                    files = [pp] if pp.is_file() else sorted(pp.iterdir())
                    parts += [f'{f.name}:{f.stat().st_size}:{int(f.stat().st_mtime)}' for f in files if f.is_file()]
        import hashlib
        return hashlib.sha1('|'.join(parts).encode()).hexdigest()[:10]


def discover(project_root):
    """Trained / served weights in the project, for one-click 'Add' in Settings › Models."""
    root = Path(project_root)
    out = []

    def add(p, family, name):
        fmt = guess_format(p)
        if fmt:
            out.append({'path': str(p), 'family': family, 'format': fmt, 'name': name,
                        'mtime': p.stat().st_mtime, 'size': p.stat().st_size if p.is_file() else None})

    runs = root / 'runs'
    for p in runs.glob('**/weights/best*.pt'):
        run = p.parent.parent.name
        add(p, 'rfdetr' if 'rfdetr' in str(p).lower() else 'yolo', run)
    for ext in ('*.onnx', '*.safetensors', '*.pkl'):
        for p in runs.glob(f'**/weights/{ext}'):
            add(p, 'rfdetr' if 'rfdetr' in str(p).lower() else 'yolo', f'{p.parent.parent.name} ({p.suffix[1:]})')
    for p in runs.glob('**/*_openvino_model'):
        add(p, 'rfdetr' if 'rfdetr' in str(p).lower() else 'yolo', f'{p.parent.parent.name} (openvino)')
    serving = root / 'serving' / 'models'
    for p in serving.glob('*/*/*/model.*'):
        prov, typ, name = p.parent.parent.parent.name, p.parent.parent.name, p.parent.name
        add(p, 'rfdetr' if 'rfdetr' in (prov + name).lower() else 'yolo', f'serving {prov}/{typ}/{name}')
    return sorted(out, key=lambda d: d['mtime'], reverse=True)
