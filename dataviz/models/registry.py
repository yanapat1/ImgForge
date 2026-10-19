"""
Saved models (Settings › Models in the page), per user, in app.models (PostgreSQL).

    row: {id, owner_id, name, config: {family: yolo|rfdetr|custom, format: pt|pth|pkl|safetensors|onnx|openvino|api,
          path, variant, arch, adapter, imgsz, conf, iou, task_type, api: {...minus secrets}, created},
          secrets: Fernet-encrypted {api_key, headers}}      (the format db/legacy.py imports models.json into)

A model dict (models() / model()) is the old models.json entry: config + id / name / owner_id, the secrets put
back into `api`. Every user sees and uses only their own models (owner_id=...); owner_id None is an internal
lookup (a labeling task runs its project owner's model, ModelService scores a model by id). public() masks the
secrets. `env:` in an API key or header is refused unless the acting user is an admin: otherwise anyone could
make the server send its environment secrets to a URL they control.

API presets: serving (this repo's serving/ app), generic (any JSON endpoint), label (a dataviz-label/1 server, the
labeling page's protocol: any task type; api.label_model names the model on it). The same models label images on
the labeling page (models/hub.py).

Uploaded files go to <root>/models/<id>/ (on disk). Model files must lie in allowed_roots or the upload folder.
"""

import json
import time
import uuid
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from .. import db

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


SECRET_API = ('api_key', 'headers')
_UPSERT_ARGS = (bindparam('cfg', type_=JSONB),)


def _mask(key):
    key = key or ''
    return (key[:2] + '•' * max(0, len(key) - 6) + key[-4:]) if len(key) > 6 else '•' * len(key)


def guess_format(path):
    p = Path(path)
    if p.is_dir() and any(p.glob('*.xml')):
        return 'openvino'
    return EXT.get(p.suffix.lower())


def _mask_headers(h):
    """'Name: value' lines with every value masked (the page shows which headers are set, never their values)."""
    out = []
    for line in str(h or '').splitlines():
        k, sep, _ = line.partition(':')
        if sep and k.strip():
            out.append(f'{k.strip()}: ••••')
    return '\n'.join(out)


def _acting_admin():
    """Whether env: secrets may be saved: the signed-in user is an admin; no user = an internal caller (scripts)."""
    from ..auth import context
    u = context.user()
    return u is None or bool(u.get('is_admin'))


def _row(r):
    from ..db import crypto
    m = dict(r.config or {})
    sec = crypto.decrypt_json(r.secrets) if r.secrets else {}
    if isinstance(m.get('api'), dict) or sec:
        api = dict(m.get('api') or {})
        api.update({k: v for k, v in (sec or {}).items() if k in SECRET_API})
        m['api'] = api
    m.update(id=r.id, name=r.name, owner_id=r.owner_id)
    return m


class ModelRegistry:
    def __init__(self, root, allowed_roots=()):
        self.root = Path(root)
        self.store = self.root / 'models'
        self.allowed_roots = [Path(r).resolve() for r in allowed_roots] + [self.store.resolve()]

    # ------------------------------------------------------------ rows
    def models(self, owner_id=None):
        """owner_id's models (oldest first); None = every model (internal)."""
        q, a = 'SELECT * FROM app.models', {}
        if owner_id is not None:
            q += ' WHERE owner_id = :o'
            a['o'] = owner_id
        with db.connect() as c:
            return [_row(r) for r in c.execute(text(q + ' ORDER BY created_at, name'), a)]

    def model(self, mid, owner_id=None):
        """One model with its secrets (server side only); KeyError when there is none or owner_id does not own it."""
        with db.connect() as c:
            r = c.execute(text('SELECT * FROM app.models WHERE id = :id'), {'id': mid or ''}).first()
        if r is None or (owner_id is not None and r.owner_id != owner_id):
            raise KeyError(f'no model {mid!r}')
        return _row(r)

    @staticmethod
    def public(m):
        out = {k: v for k, v in m.items() if k not in ('api', 'owner_id')}
        if m.get('api'):
            a = dict(m['api'])
            a['has_key'] = bool(a.get('api_key'))
            a['api_key'] = _mask(a.get('api_key'))
            a['has_headers'] = bool(a.get('headers'))
            a['headers'] = _mask_headers(a.get('headers'))
            out['api'] = a
        return out

    def check_path(self, p):
        """Only files under the allowed folders or the upload store: .pkl / full torch.load run code."""
        rp = Path(p).expanduser().resolve()
        if not any(rp == r or r in rp.parents for r in self.allowed_roots):
            raise ValueError(f'{rp} is outside the folders this server allows (storage.allowed_roots)')
        if not rp.exists():
            raise ValueError(f'{rp} does not exist')
        return str(rp)

    def upsert(self, body, owner_id=None, admin=None):
        """Save a model of owner_id (new, or one of theirs: KeyError for someone else's id). An edit keeps its
        owner; owner_id None only edits (internal). admin: may `env:` secrets be saved (None = the signed-in user
        is an admin, or there is no user)."""
        base = dict(self.model(body['id'], owner_id=owner_id)) if body.get('id') else {}
        owner = base.get('owner_id') or owner_id
        if not owner:
            raise ValueError('a model needs an owner (sign in first)')
        admin = _acting_admin() if admin is None else admin
        m = {k: v for k, v in base.items() if k not in ('owner_id',)}
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
            hdr = api.get('headers')
            if not str(hdr or '').strip() or hdr == _mask_headers(old.get('headers')):
                api['headers'] = old.get('headers', '')     # blank / the masked lines back = keep the saved ones
            if not admin and ('env:' in str(api.get('api_key') or '') or 'env:' in str(api.get('headers') or '')):
                raise ValueError('only admins can use env: secrets (server environment variables)')
            api['preset'] = api.get('preset') if api.get('preset') in PRESETS else 'generic'
            if not api.get('url'):
                raise ValueError('API URL is required')
            m['api'] = api
            m.pop('path', None)
        else:
            # weights / adapters run code in the server process (pickle, torch.load, custom .py): admins only,
            # because anyone can sign up
            if not admin:
                raise ValueError('only admins can add models that load files on this server (weights, adapters): '
                                 'add an API model instead')
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
        m['name'] = ((m.get('name') or '').strip() or (Path(m.get('path', '')).stem if m.get('path') else 'api model'))[:200]
        m['id'] = base.get('id') or uuid.uuid4().hex[:10]
        m.setdefault('created', time.strftime('%Y-%m-%d %H:%M:%S'))
        self._write(m, owner, new=not base)
        return {**m, 'owner_id': owner}

    def _write(self, m, owner, new):
        from ..db import crypto
        config = {k: v for k, v in m.items() if k not in ('id', 'name')}
        secrets = {}
        if isinstance(config.get('api'), dict):
            api = dict(config['api'])
            for k in SECRET_API:
                v = api.pop(k, None)
                if v:
                    secrets[k] = v
            config['api'] = api
        args = {'id': m['id'], 'o': owner, 'n': m['name'], 'cfg': config, 's': crypto.encrypt_json(secrets)}
        sql = ("INSERT INTO app.models(id, owner_id, name, config, secrets) VALUES (:id, :o, :n, :cfg, :s)" if new else
               "UPDATE app.models SET name = :n, config = :cfg, secrets = :s, updated_at = now() "
               "WHERE id = :id AND owner_id = :o")
        try:
            with db.begin() as c:
                c.execute(text(sql).bindparams(*_UPSERT_ARGS), args)
        except IntegrityError:
            raise ValueError(f'a model named {m["name"]!r} already exists') from None

    def delete(self, mid, owner_id=None):
        """Delete a model (owner_id: only theirs; KeyError otherwise). Uploaded weights stay on disk."""
        q, a = 'DELETE FROM app.models WHERE id = :id', {'id': mid}
        if owner_id is not None:
            q += ' AND owner_id = :o'
            a['o'] = owner_id
        with db.begin() as c:
            if not c.execute(text(q), a).rowcount:
                raise KeyError(f'no model {mid!r}')

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
