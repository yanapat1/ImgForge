"""
The Data Center's one entry point (contract: datacenter/API.md). The routes (server/routes/datacenter.py), the
LLM tools (llm/tools.py) and the CLI (`python -m dataviz dc`) call only these functions, so a rule is enforced
in one place: who sees a bucket (its owner; admins see all), who may change it (tier permission data.manage),
which buckets are read-only (S3 links), and that every change is followed by a format re-check of the bucket
root (tags.revalidate) whose outcome travels back as `retagged`.

Every function takes the acting `user` dict first, raises DCError(message, status) and returns JSON-able dicts.
The server wires its Ctx in with configure(ctx=ctx) (the router does it); the CLI calls configure(cfg) and the
registries it needs are then built from the config.
"""

import fnmatch
import mimetypes
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from .. import db
from ..auth import tiers as vtiers
from . import formats, store, tags as vtags
from .store import DCError

__all__ = ['DCError', 'configure', 'list_buckets', 'get_bucket', 'create_bucket', 'update_bucket', 'delete_bucket',
           'summary', 'ls', 'read_file', 'read_text', 'ops', 'undo', 'upload', 'set_tags', 'all_tags', 'detect',
           'stats', 'search', 'send']

KINDS = ('managed', 'local', 's3')
PERM = 'data.manage'
S3_TTL = 600                        # an S3 detection is reused this long (no cheap fingerprint for a prefix)
SEARCH_SCAN = 300_000               # entries walked by one search
_JSONB = bindparam('result', type_=JSONB)
_MANIFEST = bindparam('manifest', type_=JSONB)

_conf = {'ctx': None, 'cfg': None, 'datasets': None, 'labeling': None}
_lock = threading.Lock()


def configure(cfg=None, ctx=None):
    """Point the service at the server's Ctx (its cfg, connections registry, labeling registry, jobs) or, without
    one (CLI), at a config dict."""
    with _lock:
        _conf.update(ctx=ctx, cfg=cfg, datasets=None, labeling=None)


def _cfg():
    ctx = _conf['ctx']
    if ctx is not None and getattr(ctx, 'cfg', None) is not None:
        return ctx.cfg
    if _conf['cfg'] is None:
        from ..core import config as vconfig
        _conf['cfg'] = vconfig.load_config()
    return _conf['cfg']


def _workspace():
    from ..core.config import resolve
    return resolve(str((_cfg().get('dataset') or {}).get('workspace_root') or '.dataviz_cache'))


def _datasets():
    ctx = _conf['ctx']
    if ctx is not None and getattr(ctx, 'datasets', None) is not None:
        return ctx.datasets
    with _lock:
        if _conf['datasets'] is None:
            from ..data.connections import Registry
            _conf['datasets'] = Registry(_workspace())
        return _conf['datasets']


def _labeling():
    ctx = _conf['ctx']
    if ctx is not None and getattr(ctx, 'labeling', None) is not None:
        return ctx.labeling
    with _lock:
        if _conf['labeling'] is None:
            from ..labeling.projects import ProjectRegistry
            _conf['labeling'] = ProjectRegistry(_workspace() / 'labeling')
        return _conf['labeling']


def _allowed():
    return store.roots(_cfg())


def _need(user, perm=PERM):
    if user is None:
        raise DCError('sign in first', 401)
    if not vtiers.has(user, perm):
        t = vtiers.tier_of(user)
        raise DCError(f'your tier ({t["name"] if t else "none"}) cannot {vtiers.DENIED.get(perm, perm)}: '
                      'ask an administrator', 403)


def _epoch(dt):
    return dt.timestamp() if dt is not None else None


# ---------------------------------------------------------------------------------------------- rows
_BUCKET_SQL = """SELECT b.*, u.username, u.display_name FROM app.dc_buckets b
                 JOIN app.users u ON u.id = b.owner_id"""


def _visible_rows(user, conn, bid=None):
    if user is None:
        raise DCError('sign in first', 401)
    q, a = _BUCKET_SQL + ' WHERE true', {}
    if not user.get('is_admin'):
        q += ' AND b.owner_id = :u'
        a['u'] = user['id']
    if bid is not None:
        q += ' AND b.id = :b'
        a['b'] = bid
    return conn.execute(text(q + ' ORDER BY lower(b.name), b.created_at'), a).all()


def _row(user, bid, conn=None):
    """The bucket row `user` may see (404 otherwise: another user's bucket does not even exist for them)."""
    if conn is None:
        with db.connect() as c:
            return _row(user, bid, c)
    rows = _visible_rows(user, conn, str(bid or ''))
    if not rows:
        raise DCError(f'no bucket {bid!r}', 404)
    return rows[0]


def _writable(r):
    if r.kind == 's3':
        return False
    return os.path.isdir(r.root) and os.access(r.root, os.W_OK)


def _need_write(r):
    if r.kind == 's3':
        raise DCError(f'bucket “{r.name}” is an S3 link and read-only: copy the data into a managed bucket to '
                      'change it')
    if not _writable(r):
        raise DCError(f'bucket “{r.name}” cannot be written ({r.root} is missing or read-only)')


def _s3(r):
    if not r.connection_id:
        raise DCError(f'the S3 connection of bucket “{r.name}” was deleted', 409)
    try:
        conn = _datasets().connection(r.connection_id, owner_id=r.owner_id)
    except KeyError:
        raise DCError(f'the S3 connection of bucket “{r.name}” was deleted', 409) from None
    return store.S3View(conn, r.s3_bucket, r.prefix, (_cfg().get('s3') or {}))


def _abs(r, path, must_exist=True):
    return store.resolve(r.root, path, _allowed(), must_exist=must_exist)


# ---------------------------------------------------------------------------------------------- detection cache
def _cached(r, rel, force=False, want_tree=False):
    """The cache entry {format, tree} of folder `rel` of bucket row `r`, detected again when its fingerprint
    changed (or force). tree ({images, labels, size}) is filled for the root, or when want_tree."""
    rel = store.clean_rel(rel)
    view = None
    if r.kind == 's3':
        fp = f's3:{int(time.time() // S3_TTL)}'
    else:
        absp = _abs(r, rel)
        if not os.path.isdir(absp):
            raise DCError(f'{rel or "/"} is not a folder')
        fp = formats.fingerprint(absp)
    with db.connect() as c:
        hit = c.execute(text('SELECT fingerprint, result FROM app.dc_format_cache WHERE bucket_id = :b AND path = :p'),
                        {'b': r.id, 'p': rel}).first()
    if hit is not None and not force and hit.fingerprint == fp and (hit.result.get('tree') or not (want_tree or not rel)):
        return hit.result
    try:
        if r.kind == 's3':
            view = _s3(r)
            keys = view.keys(rel)
            tree = formats.KeyTree(keys, lambda p, n: view.read(store.join(rel, p), n or None)[0])
        else:
            tree = formats.LocalTree(absp)
        fmt = formats.detect(tree)
        ts = formats.tree_stats(tree) if (want_tree or not rel) else None
    finally:
        if view is not None:
            view.close()
    res = {'format': fmt, 'tree': ts}
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.dc_format_cache(bucket_id, path, fingerprint, result, updated_at)
                          VALUES (:b, :p, :fp, :result, now())
                          ON CONFLICT (bucket_id, path) DO UPDATE SET fingerprint = EXCLUDED.fingerprint,
                          result = EXCLUDED.result, updated_at = now()""").bindparams(_JSONB),
                  {'b': r.id, 'p': rel, 'fp': fp, 'result': res})
    return res


def _revalidate(r):
    """Re-detect the bucket root (forced) and swap its auto tag -> retagged or None."""
    try:
        res = _cached(r, '', force=True)
    except DCError:
        return None
    return vtags.apply_format(r.id, res['format'])


# ---------------------------------------------------------------------------------------------- bucket shape
def _sessions_for(rows, conn):
    """{bucket id: [{id, kind, name, url}]}: the owner's sessions whose source lies inside the bucket (cheap:
    two queries for every bucket at once, matched by path prefix)."""
    out = {r.id: [] for r in rows}
    owners = list({r.owner_id for r in rows})
    if not owners:
        return out
    viz = conn.execute(text("""SELECT s.id, s.name, s.owner_id, v.source_kind, v.path, v.bucket, v.prefix
                               FROM app.sessions s JOIN app.viz_sessions v ON v.session_id = s.id
                               WHERE s.owner_id = ANY(:o)"""), {'o': owners}).all()
    lab = conn.execute(text("""SELECT s.id, s.name, s.owner_id, p.source FROM app.sessions s
                               JOIN app.label_projects p ON p.session_id = s.id WHERE s.owner_id = ANY(:o)"""),
                       {'o': owners}).all()

    def inside(path, root):
        return bool(path) and (path == root or path.startswith(root.rstrip('/') + '/'))

    for r in rows:
        for v in viz:
            if v.owner_id != r.owner_id:
                continue
            hit = inside(v.path, r.root) if r.kind != 's3' else (
                v.source_kind == 's3' and v.bucket == r.s3_bucket and
                (not r.prefix or inside((v.prefix or '').strip('/'), r.prefix.strip('/'))))
            if hit:
                out[r.id].append({'id': v.id, 'kind': 'visualize', 'name': v.name, 'url': f's/{v.id}/'})
        for p in lab:
            src = p.source or {}
            if p.owner_id != r.owner_id:
                continue
            hit = (src.get('kind') == 'local' and inside(src.get('path'), r.root)) if r.kind != 's3' else (
                src.get('kind') == 's3' and src.get('bucket') == r.s3_bucket)
            if hit:
                out[r.id].append({'id': p.id, 'kind': 'label', 'name': p.name, 'url': f'?label={p.id}'})
    return out


def _shape(rows, conn):
    if not rows:
        return []
    ids = [r.id for r in rows]
    cache = {c.bucket_id: c.result for c in conn.execute(
        text("SELECT bucket_id, result FROM app.dc_format_cache WHERE bucket_id = ANY(:b) AND path = ''"),
        {'b': ids}).all()}
    tg = vtags.for_buckets(ids, conn)
    ses = _sessions_for(rows, conn)
    out = []
    for r in rows:
        res = cache.get(r.id) or {}
        fmt = res.get('format')
        tree = res.get('tree') or {}
        out.append({
            'id': r.id, 'name': r.name, 'kind': r.kind, 'root': r.root, 'writable': _writable(r),
            'owner': {'id': r.owner_id, 'username': r.username, 'display_name': r.display_name},
            'created_at': _epoch(r.created_at), 'updated_at': _epoch(r.updated_at),
            'format': fmt, 'tags': tg.get(r.id, []),
            'stats': {'images': tree.get('images'), 'labels': tree.get('labels'),
                      'classes': (fmt or {}).get('classes'), 'size': tree.get('size')},
            'sessions': ses.get(r.id, []),
            **({'connection_id': r.connection_id, 's3_bucket': r.s3_bucket, 'prefix': r.prefix}
               if r.kind == 's3' else {}),
        })
    return out


# ---------------------------------------------------------------------------------------------- buckets
def list_buckets(user, q='', tags=(), formats=()):
    """The buckets `user` sees, filtered by name substring, ALL of `tags`, ANY of `formats`."""
    with db.connect() as c:
        rows = _visible_rows(user, c)
        out = _shape(rows, c)
    qq = (q or '').casefold().strip()
    want = {t.casefold() for t in tags or () if str(t).strip()}
    fmts = {str(f).removeprefix(vtags.AUTO_PREFIX) for f in formats or () if str(f).strip()}
    res = []
    for b in out:
        have = {t['tag'].casefold() for t in b['tags']}
        fmt = next((t['tag'][len(vtags.AUTO_PREFIX):] for t in b['tags'] if t['auto']), None)
        if qq and qq not in b['name'].casefold():
            continue
        if want and not want <= have:
            continue
        if fmts and fmt not in fmts:
            continue
        res.append(b)
    return res


def get_bucket(user, bid):
    r = _row(user, bid)
    if r.kind != 's3':
        try:
            res = _cached(r, '')
            vtags.apply_format(r.id, res['format'])
        except DCError:
            pass
    with db.connect() as c:
        return _shape([r], c)[0]


def _clean_name(name):
    name = ' '.join(str(name or '').split())
    if not 1 <= len(name) <= 200:
        raise DCError('a bucket name needs 1 to 200 characters')
    if '/' in name or '\\' in name:
        raise DCError('a bucket name cannot contain / or \\')
    return name


def _slug(name):
    s = ''.join(ch if ch.isalnum() or ch in '-_.' else '-' for ch in name).strip('-.') or 'bucket'
    return s[:80]


def create_bucket(user, name, kind='managed', path=None, connection_id=None, s3_bucket=None, prefix='', tags=()):
    _need(user)
    name = _clean_name(name)
    if kind not in KINDS:
        raise DCError('kind must be managed, local or s3')
    try:
        utags = vtags.clean_tags(list(tags or []))
    except ValueError as e:
        raise DCError(str(e)) from None
    cfg = _cfg()
    made = None
    s3b, pre, cid = None, '', None
    if kind == 'managed':
        base = store.data_root(cfg)
        root = base / _slug(name)
        i = 2
        while root.exists():
            root, i = base / f'{_slug(name)}-{i}', i + 1
        root.mkdir(parents=True)
        made = root
        root = str(root)
    elif kind == 'local':
        from ..data import connections
        if not str(path or '').strip():
            raise DCError('path is required for a local link')
        try:
            root = connections.check_local(path, connections.allowed_roots(cfg))
        except ValueError as e:
            raise DCError(str(e)) from None
        if not os.path.isdir(root):
            raise DCError(f'not a folder: {root}')
    else:
        if not connection_id or not str(s3_bucket or '').strip():
            raise DCError('an S3 link needs one of your connections and a bucket')
        try:
            cn = _datasets().connection(connection_id, owner_id=user['id'])
        except KeyError:
            raise DCError('pick one of your own S3 connections') from None
        if (cn.get('kind') or 's3') != 's3':
            raise DCError('an S3 link needs an S3 connection')
        s3b, pre, cid = str(s3_bucket).strip(), store.clean_rel(prefix or ''), connection_id
        root = f's3://{s3b}/{pre}'.rstrip('/')
    bid = uuid.uuid4().hex
    try:
        with db.begin() as c:
            c.execute(text("""INSERT INTO app.dc_buckets(id, owner_id, name, kind, root, connection_id, s3_bucket, prefix)
                              VALUES (:id, :o, :n, :k, :r, :cid, :b, :p)"""),
                      {'id': bid, 'o': user['id'], 'n': name, 'k': kind, 'r': root, 'cid': cid, 'b': s3b, 'p': pre})
            for t in utags:
                if t.casefold().startswith(vtags.AUTO_PREFIX):
                    raise DCError(f'“{t}”: format:* tags are set automatically')
                c.execute(text("INSERT INTO app.dc_tags(bucket_id, path, tag, auto) VALUES (:b, '', :t, false)"),
                          {'b': bid, 't': t})
    except IntegrityError:
        if made is not None:
            shutil.rmtree(made, ignore_errors=True)
        raise DCError(f'you already have a bucket called “{name}”: pick another name', 409) from None
    except BaseException:
        if made is not None:
            shutil.rmtree(made, ignore_errors=True)
        raise
    r = _row(user, bid)
    try:
        vtags.apply_format(bid, _cached(r, '', force=True)['format'])
    except Exception:  # noqa: BLE001 - an unreachable S3 bucket is still linked; detect says why later
        pass
    return get_bucket(user, bid)


def update_bucket(user, bid, name=None):
    r = _row(user, bid)
    _need(user)
    if name is not None:
        name = _clean_name(name)
        try:
            with db.begin() as c:
                c.execute(text('UPDATE app.dc_buckets SET name = :n, updated_at = now() WHERE id = :b'),
                          {'n': name, 'b': r.id})
        except IntegrityError:
            raise DCError(f'a bucket called “{name}” already exists: pick another name', 409) from None
    return get_bucket(user, bid)


def delete_bucket(user, bid, delete_files=False):
    """Forget a bucket (tags, cache and undo rows go with it). delete_files: also remove a MANAGED bucket's
    folder; a linked folder / S3 prefix is never deleted (it is not ours)."""
    r = _row(user, bid)
    _need(user)
    if delete_files and r.kind != 'managed':
        raise DCError('only a managed bucket’s files can be deleted: a link is only forgotten')
    with db.begin() as c:
        undo_ids = c.execute(text('SELECT id FROM app.dc_undo WHERE bucket_id = :b'), {'b': r.id}).scalars().all()
        c.execute(text('DELETE FROM app.dc_buckets WHERE id = :b'), {'b': r.id})
    trash = store.data_root(_cfg()) / store.TRASH
    for u in undo_ids:
        shutil.rmtree(trash / u, ignore_errors=True)
    if delete_files:
        base = store.data_root(_cfg())
        root = Path(os.path.realpath(r.root))
        if base in root.parents:
            shutil.rmtree(root, ignore_errors=True)
    return {'ok': True, 'deleted_files': bool(delete_files)}


def summary(user):
    bs = list_buckets(user)
    managed = [b for b in bs if b['kind'] == 'managed']
    try:
        free = shutil.disk_usage(store.data_root(_cfg())).free
    except OSError:
        free = None
    return {'buckets': len(bs), 'images': sum(int(b['stats'].get('images') or 0) for b in bs),
            'size': sum(int(b['stats'].get('size') or 0) for b in bs),
            'managed': {'used': sum(int(b['stats'].get('size') or 0) for b in managed), 'free': free},
            'links': {'local': sum(b['kind'] == 'local' for b in bs), 's3': sum(b['kind'] == 's3' for b in bs)}}


# ---------------------------------------------------------------------------------------------- browsing
def ls(user, bid, path='', offset=0, limit=500, sort='name', q=''):
    r = _row(user, bid)
    rel = store.clean_rel(path)
    if r.kind == 's3':
        view = _s3(r)
        try:
            out = view.list(rel, offset, limit, sort, q)
        finally:
            view.close()
    else:
        out = store.list_local(r.root, rel, _allowed(), offset, limit, sort, q)
    try:
        res = _cached(r, rel)
        out['format'] = res['format']
        if not rel:
            vtags.apply_format(r.id, res['format'])
    except DCError:
        out['format'] = None
    out['tags'] = vtags.get(r.id, rel)
    out['writable'] = _writable(r)
    return out


def _file(r, path):
    rel = store.clean_rel(path)
    if not rel:
        raise DCError('give a file path')
    return rel


def file_path(user, bid, path):
    """(absolute path, media type) of a local file, for streaming the raw bytes; None for an S3 bucket."""
    r = _row(user, bid)
    if r.kind == 's3':
        return None
    p = _abs(r, _file(r, path))
    if not os.path.isfile(p):
        raise DCError(f'{path} is not a file', 404 if not os.path.lexists(p) else 400)
    return p, mimetypes.guess_type(p)[0] or 'application/octet-stream'


def read_file(user, bid, path, w=None):
    """(bytes, media type): the raw file, or with `w` a JPEG thumbnail at most w px (cached on disk)."""
    r = _row(user, bid)
    rel = _file(r, path)
    if r.kind == 's3':
        view = _s3(r)
        try:
            data, etag, size = view.read(rel)
        finally:
            view.close()
        if w:
            return store.thumbnail(data, f'{r.id}|{rel}|{etag}|{size}', w, _cfg()), 'image/jpeg'
        return data, mimetypes.guess_type(rel)[0] or 'application/octet-stream'
    p, mt = file_path(user, bid, rel)
    if w:
        st = os.stat(p)
        return store.thumbnail(p, f'{p}|{st.st_mtime_ns}|{st.st_size}', w, _cfg()), 'image/jpeg'
    with open(p, 'rb') as f:
        return f.read(), mt


def read_text(user, bid, path, limit=65536):
    r = _row(user, bid)
    rel = _file(r, path)
    limit = max(1, min(int(limit or 65536), 4 << 20))
    if r.kind == 's3':
        view = _s3(r)
        try:
            data, _, size = view.read(rel, limit + 1)
        finally:
            view.close()
    else:
        p, _ = file_path(user, bid, rel)
        with open(p, 'rb') as f:
            data = f.read(limit + 1)
    if b'\x00' in data[:8192]:
        raise DCError(f'{rel} is not a text file')
    return {'text': data[:limit].decode('utf-8', 'replace'), 'truncated': len(data) > limit}


# ---------------------------------------------------------------------------------------------- changes
def ops(user, bid, ops):
    """Apply file ops in order (each one may fail on its own) -> {applied, errors, undo_id, retagged}."""
    r = _row(user, bid)
    _need(user)
    _need_write(r)
    if not isinstance(ops, (list, tuple)) or not ops:
        raise DCError('ops must be a non-empty list')
    if len(ops) > 5000:
        raise DCError('at most 5000 ops at once')
    allowed = _allowed()
    undo_id = uuid.uuid4().hex
    trash_root = store.data_root(_cfg()) / store.TRASH
    trash = trash_root / undo_id
    applied, errors, steps = [], [], []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            errors.append({'op': op, 'error': 'an op is an object like {op: "move", path, to}'})
            continue
        try:
            info, step = store.apply_op(r.root, allowed, op, str(trash), i)
        except DCError as e:
            errors.append({'op': op, 'error': e.message})
            continue
        except OSError as e:
            errors.append({'op': op, 'error': f'{e.strerror or e}'})
            continue
        with db.begin() as c:
            if step['undo'] == 'move':
                vtags.move_paths(r.id, step['to'], step['from'], c)
            elif step['undo'] == 'restore':
                step['tags'] = vtags.take_paths(r.id, step['path'], c)
        applied.append(info)
        steps.append(step)
    if steps:
        with db.begin() as c:
            c.execute(text("""INSERT INTO app.dc_undo(id, bucket_id, user_id, manifest)
                              VALUES (:id, :b, :u, :manifest)""").bindparams(_MANIFEST),
                      {'id': undo_id, 'b': r.id, 'u': user['id'],
                       'manifest': {'root': r.root, 'trash': str(trash), 'steps': steps}})
            c.execute(text('UPDATE app.dc_buckets SET updated_at = now() WHERE id = :b'), {'b': r.id})
            keep = set(c.execute(text('SELECT id FROM app.dc_undo')).scalars().all())
        store.purge_trash(str(trash_root), keep)
    return {'applied': applied, 'errors': errors, 'undo_id': undo_id if steps else None,
            'retagged': _revalidate(r) if steps else None}


def undo(user, bid, undo_id):
    r = _row(user, bid)
    _need(user)
    _need_write(r)
    with db.connect() as c:
        u = c.execute(text('SELECT * FROM app.dc_undo WHERE id = :id AND bucket_id = :b'),
                      {'id': str(undo_id or ''), 'b': r.id}).first()
    if u is None:
        raise DCError('nothing to undo with that id (already undone, or expired)', 404)
    if u.user_id != user['id'] and not user.get('is_admin') and r.owner_id != user['id']:
        raise DCError('only who made a change can undo it', 403)
    man = u.manifest or {}
    if os.path.realpath(man.get('root') or '') != os.path.realpath(r.root):
        raise DCError('this change belongs to another folder', 409)
    allowed = _allowed()
    trash_root = str(store.data_root(_cfg()) / store.TRASH)
    restored, errors = [], []
    for step in reversed(man.get('steps') or []):
        try:
            restored.append(store.undo_step(r.root, allowed, step, trash_root))
        except (DCError, OSError) as e:
            errors.append({'step': step.get('undo'), 'path': step.get('path') or step.get('to'), 'error': str(e)})
            continue
        with db.begin() as c:
            if step['undo'] == 'move':
                vtags.move_paths(r.id, step['from'], step['to'], c)
            elif step['undo'] == 'restore':
                vtags.put_back(r.id, step.get('tags'), c)
    with db.begin() as c:
        c.execute(text('DELETE FROM app.dc_undo WHERE id = :id'), {'id': u.id})
    if not errors:
        shutil.rmtree(man.get('trash') or os.path.join(trash_root, u.id), ignore_errors=True)
    return {'restored': len(restored), 'paths': restored, 'errors': errors, 'retagged': _revalidate(r)}


def upload(user, bid, path, files, unzip=True):
    """Stream files into folder `path` (zips unpacked there unless unzip=False) -> {added, skipped, retagged}."""
    r = _row(user, bid)
    _need(user)
    _need_write(r)
    out = store.upload_local(r.root, _allowed(), path, files, unzip=unzip)
    with db.begin() as c:
        c.execute(text('UPDATE app.dc_buckets SET updated_at = now() WHERE id = :b'), {'b': r.id})
    out['retagged'] = _revalidate(r) if out['added'] else None
    return out


def set_tags(user, bid, path='', add=(), remove=()):
    r = _row(user, bid)
    _need(user)
    rel = store.clean_rel(path)
    if rel and r.kind != 's3':
        _abs(r, rel)
    try:
        return vtags.set_user(r.id, rel, add, remove)
    except ValueError as e:
        if isinstance(e, DCError):
            raise
        raise DCError(str(e)) from None


def all_tags(user):
    with db.connect() as c:
        ids = [r.id for r in _visible_rows(user, c)]
    return vtags.all_tags(ids)


def detect(user, bid, path='', force=False):
    r = _row(user, bid)
    rel = store.clean_rel(path)
    res = _cached(r, rel, force=bool(force))
    if not rel:
        vtags.apply_format(r.id, res['format'])
    return res['format']


def _count_lines(p):
    try:
        with open(p, 'rb') as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def stats(user, bid, path=''):
    """{images, labels, boxes, classes, size, splits}: boxes = non-empty lines of the YOLO label files."""
    r = _row(user, bid)
    rel = store.clean_rel(path)
    res = _cached(r, rel, want_tree=True)
    fmt, tree = res['format'], res.get('tree') or {}
    boxes = 0
    if r.kind != 's3' and str(fmt.get('format', '')).startswith('yolo-'):
        base = _abs(r, rel)
        labs = [p for p, _ in formats.LocalTree(base).walk()
                if p.endswith('.txt') and '/labels/' in '/' + p and not p.endswith('classes.txt')]
        with ThreadPoolExecutor(8) as ex:
            boxes = sum(ex.map(lambda p: _count_lines(os.path.join(base, p)), labs))
    splits = fmt.get('splits') or {}
    return {'images': int(tree.get('images') or 0), 'labels': int(tree.get('labels') or 0), 'boxes': boxes,
            'classes': int(fmt.get('classes') or 0), 'size': int(tree.get('size') or 0), 'splits': splits,
            'format': fmt.get('format')}


def search(user, q='', tags=(), formats=(), bid=None, path='', glob=None, limit=200):
    """Buckets matching by name / tags / format, and files / folders matching q (name substring) and glob,
    or carrying the tags. Without q, glob or tags no files are listed."""
    limit = max(1, min(int(limit or 200), 2000))
    q = (q or '').strip()
    tags = tuple(t for t in tags or () if str(t).strip())
    bks = list_buckets(user, q=q, tags=tags, formats=formats) if not bid else []
    with db.connect() as c:
        rows = [_row(user, bid, c)] if bid else _visible_rows(user, c)
    if formats:
        keep = {b['id'] for b in list_buckets(user, formats=formats)}
        rows = [r for r in rows if r.id in keep]
    rel = store.clean_rel(path)
    files = []
    qq, gg = q.casefold(), (glob or '').casefold().strip()
    if tags:
        hits = vtags.paths_with([r.id for r in rows], tags)
        by = {r.id: r for r in rows}
        for b, p in hits:
            if not p or (rel and not (p == rel or p.startswith(rel + '/'))):
                continue
            name = p.rsplit('/', 1)[-1]
            if (qq and qq not in name.casefold()) or (gg and not fnmatch.fnmatchcase(name.casefold(), gg)):
                continue
            files.append(_found(by[b], p))
            if len(files) >= limit:
                break
    elif qq or gg:
        allowed = _allowed()
        for r in rows:
            if r.kind == 's3':
                continue
            try:
                for p, isdir, absp in store.walk_local(r.root, rel, allowed, cap=SEARCH_SCAN):
                    name = p.rsplit('/', 1)[-1].casefold()
                    if (qq and qq not in name) or (gg and not fnmatch.fnmatchcase(name, gg)):
                        continue
                    try:
                        size = 0 if isdir else os.lstat(absp).st_size
                    except OSError:
                        size = 0
                    files.append({'bucket': r.id, 'path': p, 'name': p.rsplit('/', 1)[-1],
                                  'type': 'dir' if isdir else 'file', 'size': size})
                    if len(files) >= limit:
                        break
            except DCError:
                continue
            if len(files) >= limit:
                break
    return {'buckets': bks, 'files': files}


def _found(r, p):
    name = p.rsplit('/', 1)[-1]
    if r.kind == 's3':
        return {'bucket': r.id, 'path': p, 'name': name, 'type': 'file', 'size': None}
    try:
        absp = _abs(r, p)
        isdir = os.path.isdir(absp)
        size = 0 if isdir else os.path.getsize(absp)
    except (DCError, OSError):
        isdir, size = False, None
    return {'bucket': r.id, 'path': p, 'name': name, 'type': 'dir' if isdir else 'file', 'size': size}


# ---------------------------------------------------------------------------------------------- send
TASK_TYPES = {'detect': 'object_detection', 'segment': 'segmentation', 'pose': 'keypoints',
              'obb': 'object_detection', 'classify': 'classification'}
VISUALIZABLE = ('yolo-detect', 'yolo-seg', 'yolo-pose', 'yolo-obb', 'images', 'classification', 'embeddings')


def send(user, bid, paths, target='visualize', name=None, task_type='detect'):
    """Open one folder of a bucket as a visualize session (a saved dataset) or a labeling project (source
    {kind: local, path} / S3) -> {target, id, url} with the page's URL ('s/<id>/' or '?label=<pid>')."""
    r = _row(user, bid)
    _need(user)
    if target not in ('visualize', 'label'):
        raise DCError('target must be visualize or label')
    _need(user, 'dataset.create' if target == 'visualize' else 'label.create')
    paths = [paths] if isinstance(paths, str) else list(paths or [''])
    if len(paths) != 1:
        raise DCError('send one folder at a time (select the folder that holds them)')
    rel = store.clean_rel(paths[0])
    if r.kind != 's3' and not os.path.isdir(_abs(r, rel)):
        raise DCError(f'{rel} is a file: send the folder that holds it')
    fmt = _cached(r, rel)['format']
    base = r.name + (f' · {rel}' if rel else '')
    if target == 'visualize':
        return _send_visualize(user, r, rel, fmt, (name or '').strip())
    return _send_label(user, r, rel, fmt, (name or '').strip() or base, task_type)


def _send_visualize(user, r, rel, fmt, name):
    f = fmt.get('format')
    if f not in VISUALIZABLE:
        raise DCError(f'{rel or r.name} looks like “{fmt.get("label")}”: the visualizer opens YOLO folders, plain '
                      'image folders, classification folders and embeddings (.npy + meta.csv). '
                      + ('Convert it to YOLO first.' if f in ('coco', 'voc') else ''))
    body = {'name': name or None}
    if r.kind == 's3':
        body.update(kind='s3', connection_id=r.connection_id, bucket=r.s3_bucket,
                    prefix='/'.join(p for p in (r.prefix.strip('/'), rel) if p), mode='stream')
    else:
        body.update(kind='embeddings' if f == 'embeddings' else 'local', path=_abs(r, rel))
    if f == 'images':
        body['unit'] = 'images'
    elif f == 'classification':
        body.update(unit='images', class_from_folder=True)
    check = None
    if f == 'embeddings':
        from ..data.embedding_source import EmbeddingSource
        check = lambda ds: EmbeddingSource(ds['path']).check()  # noqa: E731
    try:
        ds = _datasets().upsert_dataset(body, user['id'], check, _allowed() if r.kind != 's3' else None)
    except (ValueError, KeyError) as e:
        raise DCError(str(e).strip("'\"")) from None
    return {'target': 'visualize', 'id': ds['id'], 'url': f's/{ds["id"]}/', 'name': ds['name']}


def _send_label(user, r, rel, fmt, name, task_type):
    from ..data import sessions
    tt = TASK_TYPES.get(task_type or 'detect')
    if tt is None:
        raise DCError(f'task_type must be one of {", ".join(TASK_TYPES)}')
    names = []
    if r.kind != 's3':
        try:
            tree = formats.LocalTree(_abs(r, rel))
            names, kpt, _ = formats._yaml_info(tree, '', [n for n, d in tree.scan() if not d])
        except DCError:
            kpt = None
    else:
        kpt = None
    task = {'name': {'object_detection': 'Detect', 'segmentation': 'Segment', 'keypoints': 'Pose',
                     'classification': 'Classify'}[tt], 'task_type': tt, 'classes': names[:500]}
    if tt == 'keypoints':
        k = int(kpt[0]) if isinstance(kpt, (list, tuple)) and kpt else 17
        task['config'] = {'keypoints': [f'kp{i + 1}' for i in range(k)]}
    if r.kind == 's3':
        source = {'kind': 's3', 'connection_id': r.connection_id, 'bucket': r.s3_bucket,
                  'prefix': '/'.join(p for p in (r.prefix.strip('/'), rel) if p)}
    else:
        source = {'kind': 'local', 'path': _abs(r, rel)}
    reg = _labeling()
    try:
        entry = reg.create({'name': sessions.free_name(user['id'], 'label', name[:200]), 'tasks': [task],
                            'source': source}, user['id'])
    except (ValueError, KeyError) as e:
        raise DCError(str(e).strip("'\"")) from None
    job_id = _submit_label_import(entry['id'], entry['source'], entry['owner_id'], entry['name'])
    return {'target': 'label', 'id': entry['id'], 'url': f'?label={entry["id"]}', 'name': entry['name'],
            'job_id': job_id}


def _submit_label_import(pid, source, owner_id, name):
    """List the folder's images into the new project (= the import job of POST /api/label/projects): a job when
    the server's job manager is wired in, else inline (CLI)."""
    reg = _labeling()

    def fn(job=None):
        from ..labeling import media
        src = media.make_source(source, reg.project_dir(pid), _datasets().scoped(owner_id), _cfg().get('s3') or {})
        st = reg.store(pid)
        try:
            items = src.list_images()
            known = st.media_names()
            known_uris = set(known.values())
            new = [rel for rel, _ in items if rel not in known_uris]
            names = _label_names(new, known)
            added = 0
            for i in range(0, len(new), 500):
                rows = []
                for rel, nm in zip(new[i:i + 500], names[i:i + 500]):
                    try:
                        info = media.probe(src, rel)
                    except Exception:  # noqa: BLE001 - one unreadable file must not stop the import
                        continue
                    rows.append({'file_name': nm, 'uri': rel, 'mime': info.get('mime'),
                                 'size_bytes': info.get('size_bytes'), 'width': info.get('width'),
                                 'height': info.get('height'),
                                 'meta': {'rel_path': rel, 'orientation': info.get('orientation')}})
                added += st.add_media(rows)
                if job is not None:
                    job.progress(done=i + len(rows), total=len(new), extra=f'{added} added')
            reg.set_counts(pid)
            return {'added': added}
        finally:
            src.close()

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        fn()
        return None
    return jobs.submit(f'label-import:{pid}', f'import images · {name}', fn, lane='io', session_id=pid).id


def _label_names(rels, taken):
    """Unique file names for the project (= routes/labeling.assign_names: base names, flattened on clashes)."""
    stem = lambda n: os.path.splitext(n)[0].lower()  # noqa: E731
    used = {stem(t) for t in taken}
    count = {}
    for r in rels:
        count[stem(r.rsplit('/', 1)[-1])] = count.get(stem(r.rsplit('/', 1)[-1]), 0) + 1
    out = []
    for r in rels:
        b = r.rsplit('/', 1)[-1]
        nm = b if count[stem(b)] == 1 and stem(b) not in used else r.replace('/', '__')
        s, ext = os.path.splitext(nm)
        k = 2
        while stem(nm) in used:
            nm, k = f'{s}_{k}{ext}', k + 1
        used.add(stem(nm))
        out.append(nm)
    return out
