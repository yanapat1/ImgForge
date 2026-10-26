"""
Saved connections and visualize sessions, per user, in PostgreSQL. Each saved dataset is a **session**: a card
on the page's first screen (Sessions home) that opens the visualizer on it. There is no built-in dataset: a
fresh database has no sessions.

    app.connections   one row per connection, owned by one user: config = the public fields (CONN_KIND_FIELDS
                      minus SECRET_FIELDS), secrets = Fernet-encrypted {secret_key, access_key, private_key,
                      api_key} (db/crypto.py). Names are unique per owner. Nobody else ever sees or uses them,
                      admins included: public_connection() is the only shape the page gets (secrets masked).
    app.sessions (kind 'viz') + app.viz_sessions
                      a dataset: {id, name, kind: local|s3|labeling, path | connection_id, bucket, prefix,
                      mode: stream|cache | project_id, task_id, only_done, created, updated, tags, owner_id,
                      visibility}. Who may see / open / change it is data/sessions.py + auth/access.py.

The formats are the ones db/legacy.py writes when it imports the old connections.json.

Connection kinds: s3, ftp, sftp (labeling image sources / export targets), postgres, mssql (labeling database
export), model (a self-hosted model server the labeling page sends images to: url, optional api_key sent as
a Bearer token, timeout_s). A connection used by a visualize session or a labeling session (app.label_projects,
including a task's model link) cannot be deleted.

A session's source is always built with **its owner's** connection (source(), scoped()): members read the S3
data without ever seeing the credentials. Local folders must lie inside storage.allowed_roots (check_local).
"""

import uuid
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from . import sources
from .. import db
from ..core.names import name_taken, unique_name

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
# labeling: one task of a labeling session, read live (labeling_source); embeddings: a folder of .npy vectors + a
# metadata table (embedding_source)
DS_KINDS = ('local', 's3', 'labeling', 'embeddings')
_DS_KIND_FIELDS = {'local': ('path',), 's3': ('connection_id', 'bucket', 'prefix', 'mode'),
                   'labeling': ('project_id', 'task_id', 'only_done'), 'embeddings': ('path',)}
_SRC_COLS = ('path', 'connection_id', 'bucket', 'prefix', 'mode', 'project_id', 'task_id', 'only_done')
_JSONB_CFG = bindparam('cfg', type_=JSONB)
OUTSIDE = 'that folder is outside the folders this server allows (storage.allowed_roots)'


def _mask(key):
    key = key or ''
    return (key[:2] + '•' * max(0, len(key) - 6) + key[-4:]) if len(key) > 6 else '•' * len(key)


# ------------------------------------------------------------------------------------------ local folders
def allowed_roots(cfg):
    """storage.allowed_roots as resolved folders (relative = to the project root; symlinks followed)."""
    from ..paths import PROJECT_ROOT
    roots = ((cfg or {}).get('storage') or {}).get('allowed_roots')
    if roots is None:
        roots = ['.']
    out = []
    for r in roots:
        p = Path(str(r)).expanduser()
        out.append((p if p.is_absolute() else PROJECT_ROOT / p).resolve())
    return out


def inside(path, roots):
    rp = Path(path)
    return any(rp == r or r in rp.parents for r in roots)


def check_local(path, roots):
    """`path` resolved (relative = to the project root, symlinks and '..' resolved first) when it lies inside
    one of `roots`; ValueError(OUTSIDE) otherwise, so a link pointing out of a root does not get through."""
    from ..paths import PROJECT_ROOT
    raw = str(path or '').strip()
    if not raw:
        raise ValueError('path is required')
    p = Path(raw).expanduser()
    rp = (p if p.is_absolute() else PROJECT_ROOT / p).resolve()
    if not inside(rp, roots):
        raise ValueError(OUTSIDE)
    return str(rp)


# ------------------------------------------------------------------------------------------ rows
def _conn_dict(r):
    """A connection row -> the dict every caller uses: config + decrypted secrets + id / kind / name / owner_id."""
    from ..db import crypto
    out = dict(r.config or {})
    if r.secrets:
        out.update(crypto.decrypt_json(r.secrets) or {})
    out.update(id=r.id, kind=r.kind, name=r.name, owner_id=r.owner_id)
    return out


_DS_SQL = """
    SELECT s.id, s.name, s.owner_id, s.visibility, s.tags, s.created_at, s.updated_at, v.source_kind, v.path,
           v.connection_id, v.bucket, v.prefix, v.mode, v.project_id, v.task_id, v.only_done
    FROM app.sessions s JOIN app.viz_sessions v ON v.session_id = s.id
    WHERE s.kind = 'viz'"""


def _ds_dict(r):
    kind = r.source_kind
    d = {'id': r.id, 'name': r.name, 'kind': kind, 'owner_id': r.owner_id, 'visibility': r.visibility,
         'tags': list(r.tags or []), 'created': r.created_at.timestamp() if r.created_at else None,
         'updated': r.updated_at.timestamp() if r.updated_at else None}
    for k in _DS_KIND_FIELDS[kind]:
        v = getattr(r, k)
        if v is not None:
            d[k] = v
    if kind == 's3':
        d.setdefault('connection_id', None)        # its connection was deleted (or never imported)
        d.setdefault('prefix', '')
    if kind == 'labeling':
        d['only_done'] = bool(r.only_done)
        d.setdefault('project_id', None)           # its labeling session was deleted
    return d


class Scoped:
    """connection(cid) restricted to one owner: what media.make_source / sinks.make_sink get, so a labeling
    session's source / export target can only use its owner's connections."""

    def __init__(self, reg, owner_id):
        self.reg, self.owner_id = reg, owner_id

    def connection(self, cid):
        return self.reg.connection(cid, owner_id=self.owner_id)


class Registry:
    def __init__(self, root):
        self.root = Path(root)               # workspace root: S3 mirrors (_s3/) and the workspace folders

    def scoped(self, owner_id):
        return Scoped(self, owner_id)

    # ------------------------------------------------------------ connections
    def connections(self, owner_id):
        """The connections of one user (oldest first)."""
        with db.connect() as c:
            rows = c.execute(text('SELECT * FROM app.connections WHERE owner_id = :o ORDER BY created_at, name'),
                             {'o': owner_id}).all()
        return [_conn_dict(r) for r in rows]

    def connection(self, cid, owner_id=None):
        """One connection with its secrets (server side only). owner_id: only when that user owns it; None = an
        internal lookup (a session's source, built with its owner's connection). KeyError otherwise."""
        with db.connect() as c:
            r = c.execute(text('SELECT * FROM app.connections WHERE id = :id'), {'id': cid or ''}).first()
        if r is None or (owner_id is not None and r.owner_id != owner_id):
            raise KeyError(f'no connection {cid!r}')
        return _conn_dict(r)

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

    def merged_connection(self, body, owner_id=None):
        """A connection from the page: a blank secret / access key / private key means 'keep the saved one'. The
        saved one is only looked up among owner_id's connections, so nobody can borrow another user's secret."""
        base = {}
        if body.get('id'):
            try:
                base = dict(self.connection(body['id'], owner_id=owner_id))
            except KeyError:
                pass
        kind = body.get('kind') or base.get('kind') or 's3'
        if kind not in CONN_KIND_FIELDS:
            raise ValueError(f'unknown connection kind {kind!r} (one of {", ".join(CONN_KINDS)})')
        out = dict(base) if (base.get('kind') or 's3') == kind else {'id': base['id']} if base else {}
        out['kind'] = kind
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
        out.setdefault('id', body.get('id') or uuid.uuid4().hex)
        return out

    def upsert_connection(self, body, owner_id):
        """Save a connection of owner_id (new, or one of theirs: an id they do not own is a new connection).
        Returns the dict with secrets (server side)."""
        from ..db import crypto
        if not owner_id:
            raise ValueError('a connection needs an owner (sign in first)')
        conn = self.merged_connection(body, owner_id)
        kind = conn['kind']
        with db.begin() as c:
            cur = c.execute(text('SELECT owner_id FROM app.connections WHERE id = :id'), {'id': conn['id']}).first()
            if cur is not None and cur.owner_id != owner_id:
                conn['id'] = uuid.uuid4().hex             # never write over someone else's row
                cur = None
            base = str(conn.get('name') or conn.get('endpoint') or conn.get('host') or conn.get('url') or kind).strip()
            conn['name'] = base[:200]
            config = {k: v for k, v in conn.items() if k not in ('id', 'kind', 'name', 'owner_id', *SECRET_FIELDS)}
            secrets = {k: conn[k] for k in SECRET_FIELDS if conn.get(k)}
            args = {'id': conn['id'], 'o': owner_id, 'k': kind, 'n': conn['name'], 'cfg': config,
                    's': crypto.encrypt_json(secrets)}
            try:
                if cur is None:
                    c.execute(text("""INSERT INTO app.connections(id, owner_id, kind, name, config, secrets)
                                      VALUES (:id, :o, :k, :n, :cfg, :s)""").bindparams(_JSONB_CFG), args)
                else:
                    c.execute(text("""UPDATE app.connections SET kind = :k, name = :n, config = :cfg, secrets = :s,
                                      updated_at = now() WHERE id = :id AND owner_id = :o""").bindparams(_JSONB_CFG), args)
            except IntegrityError:
                raise ValueError(f'you already have a connection called “{conn["name"]}”: pick another name') from None
        conn['owner_id'] = owner_id
        return conn

    def delete_connection(self, cid, owner_id):
        """Delete one of owner_id's connections (KeyError when it is not theirs); ValueError while a session uses it."""
        self.connection(cid, owner_id=owner_id)
        with db.begin() as c:
            used = list(c.execute(text("""SELECT s.name FROM app.viz_sessions v JOIN app.sessions s ON s.id = v.session_id
                                          WHERE v.connection_id = :cid ORDER BY s.name"""), {'cid': cid}).scalars())
            used += self._labeling_users(cid, c)
            if used:
                raise ValueError(f'connection is used by: {", ".join(used)}')
            c.execute(text('DELETE FROM app.connections WHERE id = :id AND owner_id = :o'), {'id': cid, 'o': owner_id})

    def _labeling_users(self, cid, c):
        """Labeling sessions whose image source, default export target or a task's model link is this connection."""
        rows = c.execute(text("""
            SELECT s.name FROM app.label_projects lp JOIN app.sessions s ON s.id = lp.session_id
            WHERE lp.source->>'connection_id' = :cid OR lp.export_default->>'connection_id' = :cid
               OR lp.export_default->'target'->>'connection_id' = :cid OR :cid = ANY(lp.model_connections)
            ORDER BY s.name"""), {'cid': cid}).scalars().all()
        return [f'labeling session {n}' for n in rows]

    # ------------------------------------------------------------ datasets
    def datasets(self, user=None, scope='all'):
        """The visualize sessions `user` sees in `scope` (data/sessions.py listing: newest opened first), each
        dataset dict with the card header {owner, members, visibility, my_role, opened_at}. user None = an
        internal caller: every session."""
        from . import sessions
        if user is None:
            user, scope = {'id': '', 'is_admin': True}, 'everyone'
        heads = sessions.listing(user, 'viz', scope)
        if not heads:
            return []
        with db.connect() as c:
            rows = {r.id: _ds_dict(r) for r in c.execute(text(_DS_SQL + ' AND s.id = ANY(:ids)'),
                                                         {'ids': [h['id'] for h in heads]})}
        out = []
        for h in heads:
            d = rows.get(h['id'])
            if d is None:                          # a viz session row without its source row
                continue
            d.update(owner=h['owner'], members=h['members'], my_role=h['my_role'], opened_at=h['opened_at'])
            out.append(d)
        return out

    def dataset(self, did, conn=None):
        """One saved dataset (internal: no access check; the routes check first). KeyError when there is none."""
        if conn is None:
            with db.connect() as c:
                return self.dataset(did, c)
        r = conn.execute(text(_DS_SQL + ' AND s.id = :id'), {'id': did or ''}).first()
        if r is None:
            raise KeyError(f'no dataset {did!r}')
        return _ds_dict(r)

    def upsert_dataset(self, body, owner_id=None, check=None, roots=None):
        """Validate + save a dataset: a new session owned by owner_id, or (body.id) an edit of an existing one,
        which keeps its owner. check(ds): an extra test run before anything is written (raise ValueError).
        roots: storage.allowed_roots (allowed_roots(cfg)) a local folder must lie in; None = not checked
        (scripts, the --dataset flag). An S3 dataset may only use a connection its owner owns."""
        from . import sessions
        ds = {k: body[k] for k in DS_FIELDS if body.get(k) is not None}
        kind = ds.get('kind')
        if kind not in DS_KINDS:
            raise ValueError('kind must be local, s3, labeling or embeddings')
        for k in set(DS_FIELDS) - {'name', 'kind', *_DS_KIND_FIELDS[kind]}:
            ds.pop(k, None)                              # a kind's fields only (an edit may switch kinds)
        old = None
        if body.get('id'):
            try:
                old = self.dataset(body['id'])
            except KeyError:
                old = None                               # an unknown id: a new session (with a new id)
        owner = old['owner_id'] if old else owner_id
        if not owner:
            raise ValueError('a session needs an owner (sign in first)')
        if kind in ('local', 'embeddings'):
            if not ds.get('path'):
                raise ValueError('path is required')
            ds['path'] = check_local(ds['path'], roots) if roots is not None else \
                str(Path(ds['path']).expanduser().resolve())
        elif kind == 's3':
            if not ds.get('connection_id') or not ds.get('bucket'):
                raise ValueError('connection and bucket are required')
            try:
                cn = self.connection(ds['connection_id'], owner_id=owner)
            except KeyError:
                raise ValueError('pick one of your own S3 connections') from None
            if (cn.get('kind') or 's3') != 's3':
                raise ValueError('a dataset needs an S3 connection')
            ds['prefix'] = (ds.get('prefix') or '').strip('/')
            ds['mode'] = ds.get('mode') if ds.get('mode') in ('stream', 'cache') else 'stream'
        else:
            if not ds.get('project_id') or not ds.get('task_id'):
                raise ValueError('pick the labeling session and its task')
            ds['only_done'] = ds.get('only_done') in (True, 1, '1', 'true', 'on')
        ds['name'] = (ds.get('name') or '').strip() or (Path(ds['path']).name if kind in ('local', 'embeddings') else
                                                         'labeling session' if kind == 'labeling' else
                                                         (ds['prefix'].rsplit('/', 1)[-1] or ds['bucket']))
        typed = ds['name']
        if check is not None:
            check(ds)
        auto = not (body.get('name') or '').strip() or ds['name'] != typed     # a default, not what the user typed
        sid = old['id'] if old else uuid.uuid4().hex
        cols = {k: ds.get(k) for k in _SRC_COLS}
        cols['only_done'] = bool(cols['only_done'])
        with db.begin() as c:
            others = sessions.names(owner, 'viz', c, exclude=sid)
            if ds['name'].casefold() != ((old or {}).get('name') or '').casefold() and name_taken(ds['name'], others):
                if not auto:
                    raise ValueError(f'a visualize session called “{ds["name"]}” already exists: pick another name')
                ds['name'] = unique_name(ds['name'], others)
            if old is None:
                sessions.create('viz', owner, ds['name'], sid=sid, conn=c)
                c.execute(text("""INSERT INTO app.viz_sessions(session_id, source_kind, path, connection_id, bucket,
                                                               prefix, mode, project_id, task_id, only_done)
                                  VALUES (:sid, :k, :path, :connection_id, :bucket, :prefix, :mode, :project_id,
                                          :task_id, :only_done)"""), {'sid': sid, 'k': kind, **cols})
            else:
                sessions.update(sid, name=ds['name'], conn=c)
                c.execute(text("""UPDATE app.viz_sessions SET source_kind = :k, path = :path,
                                  connection_id = :connection_id, bucket = :bucket, prefix = :prefix, mode = :mode,
                                  project_id = :project_id, task_id = :task_id, only_done = :only_done
                                  WHERE session_id = :sid"""), {'sid': sid, 'k': kind, **cols})
            return self.dataset(sid, c)

    def set_dataset_tags(self, did, tags):
        from . import sessions
        with db.begin() as c:
            self.dataset(did, c)                       # KeyError (never a label session here)
            sessions.update(did, tags=tags if tags is not None else [], conn=c)
            return self.dataset(did, c)

    def find_local(self, path, owner_id=None):
        """owner_id's saved local dataset reading `path` (any owner's when None), or None."""
        path = str(Path(path).expanduser().resolve())
        q = _DS_SQL + " AND v.source_kind = 'local' AND v.path = :p"
        a = {'p': path}
        if owner_id is not None:
            q += ' AND s.owner_id = :o'
            a['o'] = owner_id
        with db.connect() as c:
            r = c.execute(text(q + ' ORDER BY s.created_at LIMIT 1'), a).first()
        return _ds_dict(r) if r is not None else None

    def delete_dataset(self, did):
        """Delete a visualize session (its members, opens, prefs cascade). KeyError when there is none."""
        from . import sessions
        with db.begin() as c:
            self.dataset(did, c)
            sessions.delete(did, c)

    # ------------------------------------------------------------ sources
    def mirror_root(self):
        return self.root / '_s3'

    def source(self, ds, s3cfg=None, labeling=None):
        """The source object of a saved dataset, built with its owner's connection. labeling: the ProjectRegistry
        (ctx.labeling), for kind labeling."""
        if ds['kind'] == 'local':
            return sources.LocalSource(ds['path'], name=ds.get('name'))
        if ds['kind'] == 'embeddings':
            from .embedding_source import EmbeddingSource
            return EmbeddingSource(ds['path'], name=ds.get('name'))
        if ds['kind'] == 'labeling':
            if labeling is None:
                raise ValueError('labeling sessions are not enabled in this server')
            if not ds.get('project_id'):
                raise ValueError('its labeling session was deleted')
            from .labeling_source import LabelingSource
            return LabelingSource(labeling, ds['project_id'], ds['task_id'], only_done=ds.get('only_done', False),
                                  name=ds.get('name'), connections=self, s3cfg=s3cfg)
        if not ds.get('connection_id'):
            raise ValueError('its connection was deleted')
        try:
            conn = self.connection(ds['connection_id'], owner_id=ds.get('owner_id'))
        except KeyError:
            raise ValueError('its connection was deleted') from None
        return sources.S3Source(conn, ds['bucket'], ds.get('prefix', ''), name=ds.get('name'), mode=ds.get('mode', 'stream'),
                                mirror_root=self.mirror_root(), s3cfg=s3cfg)

