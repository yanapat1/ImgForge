"""
Saved S3 connections and datasets. Each saved dataset is a **session**: a card on the page's first
screen (Sessions home) that opens the visualizer on it. There is no built-in dataset: a fresh checkout
has no connections.json, so it starts with 0 sessions.

Stored in <workspace_root>/connections.json, readable only by the owner (chmod 600) because it
holds S3 secret keys. Secrets never leave the server: public() masks them for the page.

    {"connections": [{id, kind, name, ...per-kind fields (CONN_KIND_FIELDS)}],
     "datasets":    [{id, name, kind: local|s3|labeling, path | connection_id, bucket, prefix, mode: stream|cache
                      | project_id, task_id, only_done, created, tags}],
     "active": "<dataset id>"}

Connection kinds: s3 (the default: files written before kinds existed have no `kind`), ftp, sftp (labeling
image sources / export targets), postgres, mssql (labeling database export), model (a self-hosted model
server the labeling page sends images to: url, optional api_key sent as a Bearer token, timeout_s). A
connection used by a saved dataset or a labeling session (app.label_projects, including a task's
model link) cannot be deleted.
"""

import os
import json
import time
import uuid
import threading
from pathlib import Path

from . import sources
from ..core.names import clean_tags, name_taken, unique_name

CONN_FIELDS = ('name', 'endpoint', 'region', 'access_key', 'secret_key', 'verify_ssl')
_DB_FIELDS = ('name', 'host', 'port', 'database', 'username', 'secret_key', 'schema', 'table_prefix', 'encrypt')
CONN_KIND_FIELDS = {
    's3': CONN_FIELDS,
    'ftp': ('name', 'host', 'port', 'username', 'secret_key', 'tls', 'passive', 'base_path'),
    'sftp': ('name', 'host', 'port', 'username', 'secret_key', 'private_key', 'base_path'),
    'postgres': _DB_FIELDS,
    'mssql': _DB_FIELDS,
    'model': ('name', 'url', 'api_key', 'timeout_s'),
}
CONN_KINDS = tuple(CONN_KIND_FIELDS)
DEFAULT_PORTS = {'ftp': 21, 'sftp': 22, 'postgres': 5432, 'mssql': 1433}
SECRET_FIELDS = ('secret_key', 'access_key', 'private_key', 'api_key')      # blank in an update = keep the stored one
_BOOL_FIELDS = ('verify_ssl', 'tls', 'passive', 'encrypt')
DS_FIELDS = ('name', 'kind', 'path', 'connection_id', 'bucket', 'prefix', 'mode', 'project_id', 'task_id', 'only_done')
DS_KINDS = ('local', 's3', 'labeling')          # labeling: one task of a labeling session, read live (labeling_source)
_DS_KIND_FIELDS = {'local': ('path',), 's3': ('connection_id', 'bucket', 'prefix', 'mode'),
                   'labeling': ('project_id', 'task_id', 'only_done')}


def _mask(key):
    key = key or ''
    return (key[:2] + '•' * max(0, len(key) - 6) + key[-4:]) if len(key) > 6 else '•' * len(key)


class Registry:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'connections.json'
        self._lock = threading.Lock()

    # ------------------------------------------------------------ storage
    def load(self):
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
        except ValueError:
            data = {}
        data.setdefault('connections', [])
        data.setdefault('datasets', [])
        data.setdefault('active', None)
        return data

    def save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # never world-readable, even briefly
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ------------------------------------------------------------ connections
    def connection(self, cid):
        for c in self.load()['connections']:
            if c['id'] == cid:
                return c
        raise KeyError(f'no connection {cid!r}')

    @staticmethod
    def public_connection(c):
        kind = c.get('kind') or 's3'
        if kind == 's3':
            return {'id': c['id'], 'kind': 's3', 'name': c.get('name') or c.get('endpoint'), 'endpoint': c.get('endpoint', ''),
                    'region': c.get('region', ''), 'access_key': _mask(c.get('access_key')),
                    'has_secret': bool(c.get('secret_key')), 'verify_ssl': c.get('verify_ssl', True)}
        if kind == 'model':
            return {'id': c['id'], 'kind': 'model', 'name': c.get('name') or c.get('url'), 'url': c.get('url', ''),
                    'has_secret': bool(c.get('api_key')), 'timeout_s': c.get('timeout_s') or 60}
        out = {'id': c['id'], 'kind': kind, 'name': c.get('name') or c.get('host'), 'host': c.get('host', ''),
               'port': c.get('port') or DEFAULT_PORTS[kind], 'username': c.get('username', ''),
               'has_secret': bool(c.get('secret_key'))}
        if kind == 'ftp':
            out.update(tls=bool(c.get('tls', False)), passive=c.get('passive', True) is not False,
                       base_path=c.get('base_path', ''))
        elif kind == 'sftp':
            out.update(base_path=c.get('base_path', ''), has_private_key=bool(c.get('private_key')))
        else:
            out.update(database=c.get('database', ''), schema=c.get('schema', ''),
                       table_prefix=c.get('table_prefix', 'lbl_') if c.get('table_prefix') is not None else 'lbl_',
                       encrypt=bool(c.get('encrypt', False)))
        return out

    def merged_connection(self, body):
        """A connection from the page: a blank secret / access key / private key means 'keep the saved one'."""
        base = {}
        if body.get('id'):
            try:
                base = dict(self.connection(body['id']))
            except KeyError:
                pass
        kind = body.get('kind') or base.get('kind') or 's3'
        if kind not in CONN_KIND_FIELDS:
            raise ValueError(f'unknown connection kind {kind!r} (one of {", ".join(CONN_KINDS)})')
        out = dict(base) if (base.get('kind') or 's3') == kind else {'id': base['id']} if base else {}
        if kind != 's3' or body.get('kind') or base.get('kind'):
            out['kind'] = kind                        # s3 connections saved without a kind stay byte-identical
        for k in CONN_KIND_FIELDS[kind]:
            v = body.get(k)
            if k in SECRET_FIELDS and not v:
                continue
            if v is None:
                continue
            if k == 'port':
                v = int(v) if str(v).strip() else DEFAULT_PORTS[kind]
            elif k == 'timeout_s':
                v = max(1.0, float(v)) if str(v).strip() else 60
            elif k == 'url':
                v = str(v).strip().rstrip('/')
            elif k in _BOOL_FIELDS and isinstance(v, str):
                v = v.lower() in ('1', 'true', 'yes', 'on')
            out[k] = v
        if kind in DEFAULT_PORTS:
            out.setdefault('port', DEFAULT_PORTS[kind])
        if kind in ('postgres', 'mssql'):
            out.setdefault('table_prefix', 'lbl_')
        if kind == 'model':
            if not str(out.get('url') or '').startswith(('http://', 'https://')):
                raise ValueError('a model connection needs the server URL (http:// or https://)')
            out.setdefault('timeout_s', 60)
        out.setdefault('id', body.get('id') or uuid.uuid4().hex[:10])
        return out

    def upsert_connection(self, body):
        with self._lock:
            data = self.load()
            conn = self.merged_connection(body)
            data['connections'] = [c for c in data['connections'] if c['id'] != conn['id']] + [conn]
            self.save(data)
            return conn

    def delete_connection(self, cid):
        with self._lock:
            data = self.load()
            used = [d['name'] for d in data['datasets'] if d.get('connection_id') == cid] + self._labeling_users(cid)
            if used:
                raise ValueError(f'connection is used by: {", ".join(used)}')
            data['connections'] = [c for c in data['connections'] if c['id'] != cid]
            self.save(data)

    def _labeling_users(self, cid):
        """Labeling sessions whose image source, default export target or a task's model link is this connection."""
        from sqlalchemy import text
        from .. import db
        try:
            with db.connect() as c:
                rows = c.execute(text("""
                    SELECT s.name FROM app.label_projects lp JOIN app.sessions s ON s.id = lp.session_id
                    WHERE lp.source->>'connection_id' = :cid OR lp.export_default->>'connection_id' = :cid
                       OR lp.export_default->'target'->>'connection_id' = :cid OR :cid = ANY(lp.model_connections)
                    ORDER BY s.name"""), {'cid': cid}).scalars().all()
        except RuntimeError:                         # no database (a bare Registry in a script)
            return []
        return [f'labeling session {n}' for n in rows]

    # ------------------------------------------------------------ datasets
    def datasets(self):
        return self.load()['datasets']

    def dataset(self, did):
        for d in self.datasets():
            if d['id'] == did:
                return d
        raise KeyError(f'no dataset {did!r}')

    def upsert_dataset(self, body, check=None):
        """Validate + save a dataset. check(ds): an extra test run before anything is written (raise ValueError)."""
        ds = {k: body[k] for k in DS_FIELDS if body.get(k) is not None}
        kind = ds.get('kind')
        if kind not in DS_KINDS:
            raise ValueError('kind must be local, s3 or labeling')
        for k in set(DS_FIELDS) - {'name', 'kind', *_DS_KIND_FIELDS[kind]}:
            ds.pop(k, None)                              # a kind's fields only (an edit may switch kinds)
        if kind == 'local':
            if not ds.get('path'):
                raise ValueError('path is required')
            ds['path'] = str(Path(ds['path']).expanduser().resolve())
        elif kind == 's3':
            if not ds.get('connection_id') or not ds.get('bucket'):
                raise ValueError('connection and bucket are required')
            if (self.connection(ds['connection_id']).get('kind') or 's3') != 's3':
                raise ValueError('a dataset needs an S3 connection')
            ds['prefix'] = (ds.get('prefix') or '').strip('/')
            ds['mode'] = ds.get('mode') if ds.get('mode') in ('stream', 'cache') else 'stream'
        else:
            if not ds.get('project_id') or not ds.get('task_id'):
                raise ValueError('pick the labeling session and its task')
            ds['only_done'] = ds.get('only_done') in (True, 1, '1', 'true', 'on')
        ds['name'] = (ds.get('name') or '').strip() or (Path(ds['path']).name if kind == 'local' else
                                                         'labeling session' if kind == 'labeling' else
                                                         (ds['prefix'].rsplit('/', 1)[-1] or ds['bucket']))
        typed = ds['name']
        if check is not None:
            check(ds)
        auto = not (body.get('name') or '').strip() or ds['name'] != typed     # a default, not what the user typed
        with self._lock:
            data = self.load()
            ds['id'] = body.get('id') or uuid.uuid4().hex[:10]
            old = next((d for d in data['datasets'] if d['id'] == ds['id']), {})
            others = [d.get('name') for d in data['datasets'] if d['id'] != ds['id']]
            if ds['name'].casefold() != (old.get('name') or '').casefold() and name_taken(ds['name'], others):
                if not auto:
                    raise ValueError(f'a visualize session called “{ds["name"]}” already exists: pick another name')
                ds['name'] = unique_name(ds['name'], others)
            ds['created'] = old.get('created') or time.time()     # an edit keeps the creation time
            ds['tags'] = old.get('tags', [])                     # the forms never send tags: keep them
            data['datasets'] = [d for d in data['datasets'] if d['id'] != ds['id']] + [ds]
            self.save(data)
        return ds

    def set_dataset_tags(self, did, tags):
        tags = clean_tags(tags)
        with self._lock:
            data = self.load()
            ds = next((d for d in data['datasets'] if d['id'] == did), None)
            if ds is None:
                raise KeyError(f'no dataset {did!r}')
            ds['tags'] = tags
            self.save(data)
        return ds

    def find_local(self, path):
        """The saved local dataset reading `path`, or None."""
        path = str(Path(path).expanduser().resolve())
        return next((d for d in self.datasets() if d['kind'] == 'local' and d.get('path') == path), None)

    def delete_dataset(self, did):
        with self._lock:
            data = self.load()
            data['datasets'] = [d for d in data['datasets'] if d['id'] != did]
            if data['active'] == did:
                data['active'] = None
            self.save(data)

    def set_active(self, did):
        with self._lock:
            data = self.load()
            data['active'] = did
            self.save(data)

    def active(self):
        return self.load()['active']

    # ------------------------------------------------------------ sources
    def mirror_root(self):
        return self.root / '_s3'

    def source(self, ds, s3cfg=None, labeling=None):
        """The source object of a saved dataset. labeling: the ProjectRegistry (ctx.labeling), for kind labeling."""
        if ds['kind'] == 'local':
            return sources.LocalSource(ds['path'], name=ds.get('name'))
        if ds['kind'] == 'labeling':
            if labeling is None:
                raise ValueError('labeling sessions are not enabled in this server')
            from .labeling_source import LabelingSource
            return LabelingSource(labeling, ds['project_id'], ds['task_id'], only_done=ds.get('only_done', False),
                                  name=ds.get('name'), connections=self, s3cfg=s3cfg)
        conn = self.connection(ds['connection_id'])
        return sources.S3Source(conn, ds['bucket'], ds.get('prefix', ''), name=ds.get('name'), mode=ds.get('mode', 'stream'),
                                mirror_root=self.mirror_root(), s3cfg=s3cfg)
