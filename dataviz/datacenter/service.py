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
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from .. import db
from ..auth import tiers as vtiers
from . import formats, store, tags as vtags, video
from .store import DCError

__all__ = ['DCError', 'configure', 'list_buckets', 'get_bucket', 'create_bucket', 'update_bucket', 'delete_bucket',
           'summary', 'ls', 'read_file', 'read_text', 'ops', 'undo', 'upload', 'set_tags', 'all_tags', 'detect',
           'stats', 'search', 'send', 'sync', 'export', 'export_check', 'export_to', 'source', 'autolabel', 'write_text',
           'video_info', 'estimate_frames', 'extract_frames', 'set_video_auto']

KINDS = ('managed', 'local', 's3')
MODES = ('stream', 'local')         # an S3 link: read S3 on every request, or an editable local copy
PERM = 'data.manage'
LANE = 'dc'                         # job lane of the bulk work: fair per account, never in the way of opening a session
S3_TTL = 3600                       # an S3 detection is fresh this long (no cheap fingerprint for a prefix)
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


class _Row:
    """A bucket row. An S3 link loaded into a local folder (mode local, pulled at least once) reads and writes
    that folder, so for every rule below it is `kind` local with `root` = the copy; `link` stays s3 and the S3
    columns stay readable for the re-sync."""

    def __init__(self, row):
        self._r = row
        self.link = row.kind
        self.local_copy = row.kind == 's3' and row.mode == 'local' and row.synced_at is not None
        self.kind = 'local' if self.local_copy else row.kind
        self.root = row.local_root if self.local_copy else row.root

    def __getattr__(self, k):
        return getattr(self._r, k)


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
    return [_Row(r) for r in conn.execute(text(q + ' ORDER BY lower(b.name), b.created_at'), a).all()]


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


def _importing(bid):
    jobs = getattr(_conf['ctx'], 'jobs', None) if _conf['ctx'] is not None else None
    return bool(jobs is not None and jobs.busy(f'dc-import:{bid}'))


def _need_write(r):
    if _importing(r.id):
        raise DCError(f'bucket “{r.name}” is still importing: it can be changed once the download finished', 409)
    if r.kind == 's3' and r.mode == 'local':
        raise DCError(f'bucket “{r.name}” is still loading from S3: it can be changed once the download finished',
                      409)
    if r.kind == 's3':
        raise DCError(f'bucket “{r.name}” is streamed from S3 and read-only: switch it to “load into local” '
                      '(Edit) or copy the data into a managed bucket to change it')
    if not _writable(r):
        raise DCError(f'bucket “{r.name}” cannot be written ({r.root} is missing or read-only)')


def _s3(r, rel=''):
    """The S3 view of a link (of its sub-folder `rel`)."""
    if not r.connection_id:
        raise DCError(f'the S3 connection of bucket “{r.name}” was deleted', 409)
    try:
        conn = _datasets().connection(r.connection_id, owner_id=r.owner_id)
    except KeyError:
        raise DCError(f'the S3 connection of bucket “{r.name}” was deleted', 409) from None
    return store.S3View(conn, r.s3_bucket, '/'.join(p for p in ((r.prefix or '').strip('/'), rel) if p),
                        (_cfg().get('s3') or {}))


def _abs(r, path, must_exist=True):
    return store.resolve(r.root, path, _allowed(), must_exist=must_exist)


# ---------------------------------------------------------------------------------------------- detection cache
def _cached(r, rel, force=False, want_tree=False, user=None, wait=True):
    """The cache entry {format, tree} of folder `rel` of bucket row `r`, detected again when its fingerprint
    changed (or force). tree ({images, labels, size}) is filled for the root, or when want_tree. An S3 link with
    wait=False never lists inline: a stale entry is returned as is (None when there is none) while a scan job
    (_scan_soon) refreshes it."""
    rel = store.clean_rel(rel)
    if r.kind == 's3':
        fp = _s3_fp()
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
    if r.kind == 's3' and not wait:
        jobs = getattr(_conf['ctx'], 'jobs', None) if _conf['ctx'] is not None else None
        if jobs is None or not jobs.busy(f'dc-scan:{r.id}:'):      # one scan per bucket at a time on the dc lane
            _scan_soon(user, r, rel)
        return hit.result if hit is not None else None
    res = _scan(r, rel, want_tree or not rel)
    _store_cache(r, rel, fp, res)
    return res


def _s3_fp():
    return f's3:{int(time.time() // S3_TTL)}'


def _scan(r, rel, want_tree, progress=None, cancelled=None):
    """Detect the format of folder `rel` (and its {images, labels, size} when want_tree) -> {format, tree}."""
    view = None
    try:
        if r.kind == 's3':
            view = _s3(r)
            keys = view.keys(rel, progress=progress, cancelled=cancelled)
            if progress is not None:
                progress(stage='detecting the format')
            tree = formats.KeyTree(keys, lambda p, n: view.read(store.join(rel, p), n or None)[0])
        else:
            tree = formats.LocalTree(_abs(r, rel))
        fmt = formats.detect(tree)
        ts = formats.tree_stats(tree) if want_tree else None
    finally:
        if view is not None:
            view.close()
    return {'format': fmt, 'tree': ts}


def _store_cache(r, rel, fp, res):
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.dc_format_cache(bucket_id, path, fingerprint, result, updated_at)
                          VALUES (:b, :p, :fp, :result, now())
                          ON CONFLICT (bucket_id, path) DO UPDATE SET fingerprint = EXCLUDED.fingerprint,
                          result = EXCLUDED.result, updated_at = now()""").bindparams(_JSONB),
                  {'b': r.id, 'p': rel, 'fp': fp, 'result': res})


def _scan_soon(user, r, rel=''):
    """List + detect folder `rel` of S3 link `r` in a dc-lane job dc-scan:<bid>:<rel> (the running one when it is
    already queued; inline from the CLI): progress shows the object count, Cancel stores nothing. -> job id."""
    rel = store.clean_rel(rel)

    def fn(job=None):
        cancelled = (lambda: job.cancelled.is_set()) if job is not None else None
        res = _scan(r, rel, True, job.progress if job is not None else None, cancelled)
        if cancelled is not None and cancelled():
            return {'cancelled': True}
        _store_cache(r, rel, _s3_fp(), res)
        retagged = vtags.apply_format(r.id, res['format']) if not rel else None
        return {'format': (res['format'] or {}).get('format'), 'stats': res['tree'], 'retagged': retagged}

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        fn()
        return None
    return jobs.submit(f'dc-scan:{r.id}:{rel}', f'scan {r.name}{"/" + rel if rel else ""} on S3', fn, lane=LANE,
                       public_result=True, owner=(user or {}).get('id') or r.owner_id).id


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
            'id': r.id, 'name': r.name, 'kind': r.link, 'root': r.root, 'writable': _writable(r),
            'owner': {'id': r.owner_id, 'username': r.username, 'display_name': r.display_name},
            'created_at': _epoch(r.created_at), 'updated_at': _epoch(r.updated_at),
            'format': fmt, 'tags': tg.get(r.id, []),
            'stats': {'images': tree.get('images'), 'labels': tree.get('labels'),
                      'classes': (fmt or {}).get('classes'), 'size': tree.get('size')},
            'sessions': ses.get(r.id, []),
            'video_auto': getattr(r._r, 'video_auto', None),
            **({'source': {'kind': r.source_kind, 'ref': r.source_ref, 'imported_at': _epoch(r.imported_at),
                           'title': (r.source_meta or {}).get('title'), 'version': (r.source_meta or {}).get('version'),
                           'importing': _importing(r.id)}}
               if getattr(r._r, 'source_kind', None) else {}),
            **({'connection_id': r.connection_id, 's3_bucket': r.s3_bucket, 'prefix': r.prefix, 'mode': r.mode,
                'local_ready': r.local_copy, 'synced_at': _epoch(r.synced_at), 's3_root': r._r.root}
               if r.link == 's3' else {}),
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


def create_bucket(user, name, kind='managed', path=None, connection_id=None, s3_bucket=None, prefix='', tags=(),
                  mode='stream'):
    """A new bucket. An S3 link with mode='local' is created streamed and its first pull is queued (sync): the
    result carries that `job_id`."""
    _need(user)
    name = _clean_name(name)
    if kind not in KINDS:
        raise DCError('kind must be managed, local or s3')
    mode = mode or 'stream'
    if mode not in MODES:
        raise DCError('mode must be stream or local')
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
    if kind == 's3':                     # listing a big bucket takes long: a job (a pull, or a scan) does it
        if mode == 'local':
            return update_bucket(user, bid, mode='local')
        try:
            scan = _scan_soon(user, r)
        except Exception:  # noqa: BLE001 - an unreachable S3 bucket is still linked; detect says why later
            scan = None
        return {**get_bucket(user, bid), 'scan_job_id': scan}
    try:
        vtags.apply_format(bid, _cached(r, '', force=True)['format'])
    except Exception:  # noqa: BLE001 - a folder that cannot be read is still linked; detect says why later
        pass
    return get_bucket(user, bid)


def update_bucket(user, bid, name=None, mode=None):
    """Rename, and/or switch an S3 link between stream and local. Switching to local queues a pull into the
    bucket's local folder (the result carries `job_id`); switching back to stream keeps that folder on disk, so a
    later switch to local only downloads again over it."""
    r = _row(user, bid)
    _need(user)
    job_id = None
    if mode is not None:
        if mode not in MODES:
            raise DCError('mode must be stream or local')
        if r.link != 's3':
            raise DCError('only an S3 link has a load mode: managed and local buckets are always local')
        with db.begin() as c:
            c.execute(text('UPDATE app.dc_buckets SET mode = :m, updated_at = now() WHERE id = :b'),
                      {'m': mode, 'b': r.id})
        if mode == 'local':
            job_id = sync(user, bid)['job_id']
    if name is not None:
        name = _clean_name(name)
        try:
            with db.begin() as c:
                c.execute(text('UPDATE app.dc_buckets SET name = :n, updated_at = now() WHERE id = :b'),
                          {'n': name, 'b': r.id})
        except IntegrityError:
            raise DCError(f'a bucket called “{name}” already exists: pick another name', 409) from None
    out = get_bucket(user, bid)
    if job_id is not None:
        out['job_id'] = job_id
    return out


def _local_root(r):
    """The folder an S3 link is loaded into: its saved local_root, else a new one under the data root."""
    if r.local_root:
        return r.local_root
    base = store.data_root(_cfg()) / '.s3'
    root = base / f'{_slug(r.name)}-{r.id[:6]}'
    with db.begin() as c:
        c.execute(text('UPDATE app.dc_buckets SET local_root = :p WHERE id = :b'), {'p': str(root), 'b': r.id})
    return str(root)


def sync(user, bid):
    """(Re-)load an S3 link (mode local) into its local folder: every object is downloaded, replacing the local
    file of the same name; files only the local copy has are kept. A job on the dc lane (inline from the CLI)
    -> {job_id, result}."""
    r = _row(user, bid)
    _need(user)
    if r.link != 's3':
        raise DCError('only an S3 link can be synced')
    if r.mode != 'local':
        raise DCError('switch the bucket to “load into local” first (Edit › Load mode)')
    dest = _local_root(r)

    def fn(job=None):
        auto = getattr(r._r, 'video_auto', None)
        before = _videos_in(dest) if auto else None
        view = _s3(r)
        try:
            res = view.pull(dest, job.progress if job is not None else None,
                            (lambda: job.cancelled.is_set()) if job is not None else None)
        finally:
            view.close()
        if job is None or not job.cancelled.is_set():
            with db.begin() as c:
                c.execute(text('UPDATE app.dc_buckets SET synced_at = now(), updated_at = now() WHERE id = :b'),
                          {'b': r.id})
            _revalidate(_row(user, bid))
            if auto:
                res['frames'] = _auto_frames(user, _row(user, bid), sorted(_videos_in(dest) - before))
        return res

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        return {'job_id': None, 'result': fn()}
    return {'job_id': jobs.submit(f'dc-sync:{r.id}', f'load {r.name} from S3', fn, lane=LANE,
                                  public_result=True, owner=user['id']).id, 'result': None}


def convert_vectors(user, bid, path='', to='parquet'):
    """Convert the embeddings folder `path` (npy + meta.csv, embeddings.parquet or embeddings.csv) into layout `to`
    (labeling/writers/vector.py), in a new sibling folder <name>-<to>. A job on the dc lane -> {job_id, path}."""
    from ..labeling.writers import vector
    r = _row(user, bid)
    _need(user)
    _need_write(r)
    rel = store.clean_rel(path)
    src = _abs(r, rel)
    if not os.path.isdir(src):
        raise DCError(f'{rel or "/"} is not a folder')
    if to not in vector.WRITERS:
        raise DCError(f'unknown layout {to!r} (one of {", ".join(vector.WRITERS)})')
    frm = vector.detect(src)
    if frm is None:
        raise DCError('not an embeddings folder: meta.csv + .npy files, embeddings.parquet or embeddings.csv')
    if frm == to:
        raise DCError(f'the folder is already {vector.LAYOUTS[to]["label"]}')
    base = os.path.basename(src.rstrip(os.sep)) if rel else _slug(r.name)
    dest = store._unique(os.path.join(os.path.dirname(src) if rel else src, f'{base}-{to}'))
    out = store.join(store.posix_parent(rel), os.path.basename(dest)) if rel else os.path.basename(dest)

    def fn(job=None):
        try:
            res = vector.convert(src, dest, to, (lambda n: job.progress(done=n)) if job is not None else None)
        except ValueError as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise DCError(str(e)) from None
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        _revalidate(_row(user, bid))
        return {**res, 'path': out}

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        return {'job_id': None, 'path': out, 'result': fn()}
    return {'job_id': jobs.submit(f'dc-convert:{r.id}', f'convert {base} to {vector.LAYOUTS[to]["label"]}', fn,
                                  lane=LANE, public_result=True, owner=user['id']).id, 'path': out, 'result': None}


# ---------------------------------------------------------------------------------------------- video -> frames
FRAMES_MAX_VIDEOS = 500             # videos one extraction takes
ESTIMATE_PROBES = 100               # videos probed for an estimate (the rest is scaled from them)


def _videos_in(absdir, base_rel=''):
    """The rel paths (below the bucket root, `base_rel` being absdir's) of every video under folder `absdir`."""
    out = set()
    if not os.path.isdir(absdir):
        return out
    for d, dirs, files in os.walk(absdir):
        dirs[:] = [x for x in dirs if store.visible(x, True)]
        sub = os.path.relpath(d, absdir).replace(os.sep, '/')
        folder = '/'.join(x for x in (base_rel, '' if sub == '.' else sub) if x)
        for f in files:
            if store.visible(f) and store.kind_of(f) == 'video':
                out.add(store.join(folder, f))
    return out


def _videos_below(r, rel):
    try:
        return _videos_in(_abs(r, store.clean_rel(rel), must_exist=False), store.clean_rel(rel))
    except DCError:
        return set()


def _video_rels(r, paths):
    """The videos `paths` name: a file must be a video; a folder gives the videos directly inside it.
    -> [rel], in order and without repeats."""
    if isinstance(paths, str):
        paths = [paths]
    out = []
    view = _s3(r) if r.kind == 's3' else None
    try:
        for p in paths or []:
            rel = store.clean_rel(p)
            if view is not None:
                what = view.what(rel)
                if what is None:
                    raise DCError(f'no file {rel}', 404)
                isdir = what == 'dir'
            else:
                a = _abs(r, rel)
                isdir = os.path.isdir(a)
                if not isdir and not os.path.isfile(a):
                    raise DCError(f'no file {rel or "/"}', 404)
            if isdir:
                if view is not None:
                    names = [e['name'] for e in view.list(rel, limit=5000)['entries'] if e['type'] == 'file']
                else:
                    with os.scandir(_abs(r, rel)) as it:
                        names = sorted(e.name for e in it if e.is_file() and store.visible(e.name))
                found = [store.join(rel, n) for n in names if store.kind_of(n) == 'video']
                if not found:
                    raise DCError(f'{rel or "/"} holds no video (mp4, mov, avi, mkv, webm, …)')
                out += found
            elif store.kind_of(rel.rsplit('/', 1)[-1]) != 'video':
                raise DCError(f'{rel} is not a video (mp4, mov, avi, mkv, webm, …)')
            else:
                out.append(rel)
    finally:
        if view is not None:
            view.close()
    out = list(dict.fromkeys(out))
    if not out:
        raise DCError('pick a video, or a folder that holds videos')
    if len(out) > FRAMES_MAX_VIDEOS:
        raise DCError(f'{len(out)} videos: one extraction takes at most {FRAMES_MAX_VIDEOS}')
    return out


def _info(r, rel):
    i = video.probe(_abs(r, rel))
    i.pop('time_base', None)
    return i


def video_info(user, bid, path):
    """A video's {path, w, h, fps, frames, duration, codec, rotation, size, decoder} (w / h as shown, rotation
    applied); a video streamed from S3 only has {path, size, streamed: true}."""
    r = _row(user, bid)
    rel = _file(r, path)
    if store.kind_of(rel.rsplit('/', 1)[-1]) != 'video':
        raise DCError(f'{rel} is not a video (mp4, mov, avi, mkv, webm, …)')
    if r.kind == 's3':
        view = _s3(r)
        try:
            size = view.size(rel)
        finally:
            view.close()
        if size is None:
            raise DCError(f'no file {rel}', 404)
        return {'path': rel, 'size': size, 'streamed': True}
    if not os.path.isfile(_abs(r, rel)):
        raise DCError(f'no file {rel}', 404)
    return {'path': rel, **_info(r, rel)}


def _estimate(r, rels, o):
    """{frames, approx, bytes, times, videos}: what options `o` take from the videos `rels` (local: the probes and
    the same pickers the extraction runs; S3 streamed: unknown until downloaded)."""
    if r.kind == 's3':
        return {'frames': None, 'approx': True, 'bytes': None, 'times': [], 'videos': [{'path': x} for x in rels]}
    vids, frames, size, approx, times = [], 0, 0, False, []
    for rel in rels[:ESTIMATE_PROBES]:
        info = _info(r, rel)
        kf = video.keyframes(_abs(r, rel)) if o['mode'] == 'keyframes' and info['decoder'] == 'pyav' else None
        e = video.estimate(info, o, kf)
        vids.append({'path': rel, 'frames': e['frames'], **{k: info[k] for k in ('w', 'h', 'fps', 'duration')}})
        if not times:
            times = e['times']
        if e['frames'] is None:
            frames = None
        elif frames is not None:
            frames += e['frames']
        size += e['bytes'] or 0
        approx = approx or e['approx']
    if len(rels) > ESTIMATE_PROBES and frames is not None:
        k = len(rels) / ESTIMATE_PROBES
        frames, size, approx = int(frames * k), int(size * k), True
    if frames is not None and frames > o['max_frames']:
        frames = o['max_frames']
    return {'frames': frames, 'approx': approx, 'bytes': size, 'times': times, 'videos': vids}


def estimate_frames(user, bid, paths, opts=None):
    """How many frames (and roughly how many bytes) extract_frames would write, plus up to 8 of the first video's
    frame times for a film strip: {frames, approx, bytes, times, videos: [{path, frames, w, h, fps, duration}],
    describe, limit}."""
    r = _row(user, bid)
    o = video.options(opts)
    rels = _video_rels(r, paths)
    est = _estimate(r, rels, o)
    return {**est, 'describe': video.describe(o), 'limit': o['max_frames']}


def _frames_name(r, rels, out):
    if out is not None:
        name = ' '.join(str(out).split())
        if not 1 <= len(name) <= 200 or '/' in name or '\\' in name or name.startswith('.'):
            raise DCError('the output folder name needs 1 to 200 characters, no / or \\, not starting with a dot')
        return name
    if len(rels) == 1:
        return f'{video.stems(rels)[0]}-frames'
    parent = store.posix_parent(rels[0])
    return f'{parent.rsplit("/", 1)[-1] if parent else _slug(r.name)}-frames'


def extract_frames(user, bid, paths, opts=None, target='inplace', new_name=None, out=None, remember=False):
    """Cut the videos `paths` (video files, or folders: the videos directly inside) into image frames picked by
    `opts` (video.options: mode all | every | pattern | count | keyframes | scene | smart, the rate pattern
    every_s / window_s / take / pick, or fps=…; start / end, dedupe, max_side, format, quality, layout, split,
    max_frames). The frames go into a new folder `out` (default <video>-frames, or <folder>-frames for several
    videos) next to the first video ('inplace', the bucket must be writable) or at the root of a new managed bucket
    `new_name` ('copy'; the only way for a bucket streamed from S3: each video is downloaded first). The folder is
    written hidden and appears in one rename when complete; layout yolo / yolo-split also writes labels/ and
    data.yaml, every layout writes frames.csv. remember=True saves `opts` as the bucket's auto-extract settings.
    One dc-lane job -> {job_id, bucket, path, estimate, result}."""
    r = _row(user, bid)
    _need(user)
    o = video.options(opts)
    if target not in ('inplace', 'copy'):
        raise DCError('target must be inplace or copy')
    if target == 'inplace':
        if r.kind == 's3':
            raise DCError(f'bucket “{r.name}” is streamed from S3 and read-only: extract into a new bucket '
                          '(target copy), or switch it to “load into local”')
        _need_write(r)
    rels = _video_rels(r, paths)
    est = _estimate(r, rels, o)
    if est['frames'] is not None and not est['approx'] and est['frames'] >= o['max_frames'] and \
            o['max_frames'] == video.MAX_FRAMES:
        raise DCError(f'these settings take ≈{est["frames"]:,} frames, the limit is {video.MAX_FRAMES:,}: pick a '
                      'lower rate or a shorter range')
    name = _frames_name(r, rels, out)
    if remember:
        set_video_auto(user, bid, opts)
    return _frames_job(user, r, rels, o, est, name, target, new_name)


def _frames_job(user, r, rels, o, est, name, target='inplace', new_name=None):
    """Queue (or, without a job runner, run) the extraction; the checks are the caller's."""
    if target == 'copy':
        dest = _row(user, create_bucket(user, new_name or f'{r.name}-frames', 'managed')['id'])
        parent_rel = ''
    else:
        dest, parent_rel = r, store.posix_parent(rels[0])
    parent = _abs(dest, parent_rel)
    out_rel = store.join(parent_rel, os.path.basename(store._unique(os.path.join(parent, name))))
    total = est['frames']
    label = f'cut {", ".join(x.rsplit("/", 1)[-1] for x in rels[:2])}{f" +{len(rels) - 2}" if len(rels) > 2 else ""}' \
            f' · {video.describe(o)}'

    def fn(job=None):
        cancelled = (lambda: job.cancelled.is_set()) if job is not None else (lambda: False)
        tmp = os.path.join(parent, f'.frames-{uuid.uuid4().hex[:10]}')
        os.makedirs(tmp)
        done = [0]

        def prog(d=None, t=None, stage=None, extra=''):
            if job is not None:
                job.progress(done=done[0] + (d or 0), total=total, stage=stage, extra=extra)
        try:
            vids, per, nbytes, capped = [], [], 0, False
            for i, (rel, stem) in enumerate(zip(rels, video.stems(rels))):
                if cancelled():
                    raise store.Cancelled()
                shown = rel.rsplit('/', 1)[-1] + (f' ({i + 1}/{len(rels)})' if len(rels) > 1 else '')
                src, dl = None, None
                if r.kind == 's3':
                    prog(stage=f'downloading {shown}')
                    dl = os.path.join(tmp, f'.src-{i}{os.path.splitext(rel)[1]}')
                    view = _s3(r)
                    try:
                        view.download(rel, dl)
                    finally:
                        view.close()
                    src = dl
                else:
                    src = _abs(r, rel)
                try:
                    info = video.probe(src)
                    res = video.extract(src, video.image_folder(tmp, o['layout'], stem), stem, o, info, prog,
                                        cancelled, shown, start_count=done[0])
                finally:
                    if dl is not None:
                        try:
                            os.remove(dl)
                        except OSError:
                            pass
                done[0] += res['frames']
                nbytes += res['bytes']
                per.append({'path': rel, 'frames': res['frames'], 'skipped_dupes': res['skipped_dupes'],
                            'errors': res['errors'], 'decoder': res['decoder'], 'duration': info['duration']})
                vids.append((rel, stem, res['rows'], info['duration']))
                if res['capped']:
                    capped = True
                    break
            if not done[0]:
                raise DCError('no frame matched these settings: check the range and the mode')
            prog(stage='laying out the folder')
            lay = video.finish(tmp, o['layout'], vids, o)
            final = store._unique(os.path.join(parent, os.path.basename(out_rel)))
            os.replace(tmp, final)
        except BaseException as e:
            shutil.rmtree(tmp, ignore_errors=True)
            if target == 'copy':
                try:
                    delete_bucket(user, dest.id, delete_files=True)
                except Exception:  # noqa: BLE001 - best effort: the error below is what matters
                    pass
            if isinstance(e, store.Cancelled):
                return {'cancelled': True, 'bucket': None if target == 'copy' else dest.id}
            raise
        path = store.join(parent_rel, os.path.basename(final))
        with db.begin() as c:
            c.execute(text('UPDATE app.dc_buckets SET updated_at = now() WHERE id = :b'), {'b': dest.id})
        return {'bucket': dest.id, 'path': path, 'frames': done[0], 'bytes': nbytes, 'videos': per,
                'layout': o['layout'], 'format': o['format'], 'describe': video.describe(o),
                'capped': capped,
                **lay, 'retagged': _revalidate(_row(user, dest.id))}

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    base = {'bucket': dest.id, 'path': out_rel, 'estimate': {k: est[k] for k in ('frames', 'approx', 'bytes')}}
    if jobs is None:
        return {'job_id': None, **base, 'result': fn()}
    job = jobs.submit(f'dc-frames:{dest.id}:{out_rel}', label, fn, lane=LANE, public_result=True, owner=user['id'])
    return {'job_id': job.id, **base, 'result': None}


def set_video_auto(user, bid, opts=None):
    """Save `opts` (video.options) as the bucket's auto-extract settings: every video that lands in it later (upload,
    import, S3 load) is cut into <video>-frames by itself. opts None turns it off. -> {video_auto}."""
    r = _row(user, bid)
    _need(user)
    if r.kind == 's3':
        raise DCError(f'bucket “{r.name}” is streamed from S3 and read-only: nothing new lands in it')
    o = None
    if opts is not None:
        o = video.options(opts)
        o['split'] = list(o['split'])
    with db.begin() as c:
        c.execute(text('UPDATE app.dc_buckets SET video_auto = :v, updated_at = now() WHERE id = :b')
                  .bindparams(bindparam('v', type_=JSONB)), {'v': o, 'b': r.id})
    return {'video_auto': o}


def _auto_frames(user, r, rels):
    """Start the auto-extract jobs (one per new video, so each gets its own <video>-frames) -> {jobs, errors} or None."""
    auto = getattr(r._r, 'video_auto', None)
    if not auto or not rels or not _writable(r):
        return None
    try:
        o = video.options(auto)
    except DCError as e:
        return {'jobs': [], 'errors': [{'path': '', 'error': f'the saved auto-extract settings: {e}'}]}
    jobs, errors = [], []
    for rel in rels[:FRAMES_MAX_VIDEOS]:
        try:
            est = _estimate(r, [rel], o)
            res = _frames_job(user, r, [rel], o, est, _frames_name(r, [rel], None))
            jobs.append({'path': rel, 'job_id': res['job_id'], 'out': res['path']})
        except DCError as e:
            errors.append({'path': rel, 'error': e.message})
    return {'jobs': jobs, 'errors': errors}


# ---------------------------------------------------------------------------------------------- web sources
def _source_conn(user, src, connection_id):
    """The decrypted token connection for `src`: the one picked, else the user's only / first one of that kind;
    None when the source needs none (DCError when it is required and there is none)."""
    from ..data.connections import TOKEN_KINDS
    if not src.needs:
        return None
    reg = _datasets()
    if connection_id:
        try:
            c = reg.connection(connection_id, owner_id=user['id'])
        except KeyError:
            raise DCError('pick one of your own connections') from None
        if c.get('kind') != src.needs:
            raise DCError(f'that connection is not a {src.label} token')
        return c
    mine = [c for c in reg.connections(user['id']) if c.get('kind') == src.needs and src.needs in TOKEN_KINDS]
    if mine:
        return reg.connection(mine[0]['id'], owner_id=user['id'])
    if src.token == 'required':
        raise DCError(f'{src.label} needs your token: add it in Settings › Connections ({src.label}), then try again')
    return None


def list_sources(user):
    """The web sources and, for each, the user's token connections -> {sources: [...]}."""
    from . import sources
    _need(user)
    conns = [c for c in _datasets().connections(user['id'])]
    out = []
    for d in sources.describe():
        d['connections'] = [{'id': c['id'], 'name': c.get('name')} for c in conns if d['needs'] and c.get('kind') == d['needs']]
        out.append(d)
    return {'sources': out}


def inspect_source(user, source, ref, connection_id=None, opts=None):
    """A cheap preview of what an import would fetch -> {title, size, files, license, versions, name, ref, ...}."""
    from . import sources
    _need(user)
    src = sources.get(source)
    pref = src.parse_ref(ref)
    o = src.clean_opts(opts)
    return {**src.inspect(_source_conn(user, src, connection_id), pref, o), 'name': pref['name'], 'ref': pref['ref'],
            'source': src.key}


IMPORT_FLAGS = ('extract', 'keep_archive', 'nested')


def import_source(user, source, ref, name=None, connection_id=None, opts=None, bucket=None, path=''):
    """Download a dataset from the web (Kaggle, Hugging Face, Roboflow, a link, Zenodo, OpenML) into a new managed
    bucket (named from the ref, or `name`), or into folder `path` of an existing writable `bucket`. Archives are
    unpacked (opts.extract, default on; opts.nested: archives inside too; opts.keep_archive keeps the download), files
    of the same name are replaced, then the format is detected. A dc-lane job keyed dc-import:<bid> (inline from
    the CLI); a new bucket whose import fails or is cancelled is removed again. -> {job_id, bucket, result}"""
    from . import sources
    _need(user)
    src = sources.get(source)
    pref = src.parse_ref(ref)
    raw = dict(opts or {})
    flags = {'extract': raw.get('extract', True) is not False, 'keep_archive': bool(raw.get('keep_archive')),
             'nested': bool(raw.get('nested'))}
    o = src.clean_opts({k: v for k, v in raw.items() if k not in IMPORT_FLAGS})
    conn = _source_conn(user, src, connection_id)
    created = False
    if bucket:
        r = _row(user, bucket)
        _need_write(r)
        rel = store.clean_rel(path)
        target = _abs(r, rel, must_exist=False)
        os.makedirs(target, exist_ok=True)
        if not os.path.isdir(target):
            raise DCError(f'{rel} is not a folder')
    else:
        base = _clean_name(name or pref['name'])
        for i in range(1, 50):
            try:
                b = create_bucket(user, base if i == 1 else f'{base}-{i}', 'managed')
                break
            except DCError as e:
                if e.status != 409 or name:
                    raise
        else:
            raise DCError(f'pick a name: “{base}” and its numbered variants are taken', 409)
        r, rel, created = _row(user, b['id']), '', True
        target = r.root
    meta = {'connection_id': (conn or {}).get('id'), 'opts': {**o, **flags}, 'path': rel}
    with db.begin() as c:
        c.execute(text("""UPDATE app.dc_buckets SET source_kind = :k, source_ref = :ref, source_meta = :m,
                          updated_at = now() WHERE id = :b""").bindparams(bindparam('m', type_=JSONB)),
                  {'k': src.key, 'ref': pref['ref'], 'm': meta, 'b': r.id})

    def fn(job=None):
        auto = getattr(_row(user, r.id)._r, 'video_auto', None)
        before = _videos_below(r, rel) if auto else None
        tmp = os.path.join(r.root, f'.import-{uuid.uuid4().hex[:10]}')
        os.makedirs(tmp)
        cancelled = (lambda: job.cancelled.is_set()) if job is not None else None

        def prog(done=None, total=None, stage=None, extra=''):
            if job is not None:
                job.progress(done=done, total=total, stage=stage, extra=extra)
        try:
            prog(stage=f'asking {src.label}')
            got = src.download(conn, pref, o, tmp, prog, cancelled)
            res = {'files': 0, 'bytes': got.get('bytes', 0), 'added': 0, 'replaced': 0, 'skipped': 0, 'archives': 0}
            for f in got['files']:
                kind = store.archive_kind(f) if flags['extract'] else None
                if kind:
                    prog(done=0, total=None, stage=f'unpacking {os.path.basename(f)}')
                    x = store.extract_into(f, target, r.root, _allowed(),
                                           lambda d, t: prog(done=d, total=t, stage=f'unpacking {os.path.basename(f)}'),
                                           cancelled, overwrite=True, nested=flags['nested'])
                    res['archives'] += 1
                    for k in ('added', 'replaced', 'skipped'):
                        res[k] += x[k]
                    if not flags['keep_archive']:
                        continue
                dest = os.path.join(target, os.path.relpath(f, tmp))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.isdir(dest):
                    raise DCError(f'{os.path.relpath(f, tmp)} is a folder in the bucket already', 409)
                os.replace(f, dest)
                res['files'] += 1
            prog(stage='detecting the format')
            done_meta = {**meta, **(got.get('meta') or {}), 'bytes': res['bytes']}
            with db.begin() as c:
                c.execute(text("""UPDATE app.dc_buckets SET imported_at = now(), updated_at = now(), source_meta = :m
                                  WHERE id = :b""").bindparams(bindparam('m', type_=JSONB)), {'m': done_meta, 'b': r.id})
            res['retagged'] = _revalidate(_row(user, r.id))
            if auto:
                res['frames'] = _auto_frames(user, _row(user, r.id), sorted(_videos_below(r, rel) - before))
            return {**res, 'bucket': r.id, 'source': src.key, 'ref': pref['ref']}
        except BaseException as e:
            if created:
                try:
                    delete_bucket(user, r.id, delete_files=True)
                except Exception:  # noqa: BLE001 - best effort: the error below is what matters
                    pass
            if isinstance(e, store.Cancelled):
                return {'cancelled': True, 'bucket': None if created else r.id}
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        return {'job_id': None, 'bucket': r.id, 'result': fn()}
    return {'job_id': jobs.submit(f'dc-import:{r.id}', f'import {pref["ref"]} from {src.label}', fn, lane=LANE,
                                  public_result=True, owner=user['id']).id, 'bucket': r.id, 'result': None}


def reimport(user, bid):
    """Run the bucket's last web import again into the same folder (newer files replace older ones)."""
    r = _row(user, bid)
    if not getattr(r._r, 'source_kind', None):
        raise DCError(f'bucket “{r.name}” was not imported from the web')
    m = r.source_meta or {}
    return import_source(user, r.source_kind, r.source_ref, connection_id=m.get('connection_id'),
                         opts=m.get('opts') or {}, bucket=r.id, path=m.get('path') or '')


def _export_check(user, bid, path):
    """What every export checks before a byte moves (export_check runs only this, so the page can say why instead
    of a download that fails silently) -> (row, rel, is one file)."""
    r = _row(user, bid)
    rel = store.clean_rel(path)
    if store.is_secret(rel):
        _need_secret(user, rel)
    if r.kind == 's3':                                  # streamed (or still loading): read straight from S3
        view = _s3(r)
        try:
            what = view.what(rel)
        except Exception as e:  # noqa: BLE001 - botocore: endpoint down, no access ...
            raise DCError(f'cannot read bucket “{r.name}” from S3: {e}', 502) from None
        finally:
            view.close()
        if what is None:
            raise DCError(f'nothing at {rel or "/"}', 404)
        return r, rel, what == 'file'
    return r, rel, os.path.isfile(_abs(r, rel))


def _export_items(user, r, rel):
    """The files of folder (or file) `rel`, paths below the bucket root (store.Item), local or S3 alike: what the
    zip and export_to both read. .env files only for data.manage; the S3 client closes when the walk ends."""
    secrets = vtiers.has(user, PERM)
    if r.kind != 's3':
        yield from store.local_items(r.root, rel, _allowed(), secrets)
        return
    view = _s3(r)
    try:
        yield from view.items(rel, secrets)
    finally:
        view.close()


def export_check(user, bid, path=''):
    """{name, kind} of what GET export would zip, or the DCError it would fail with."""
    r, rel, single = _export_check(user, bid, path)
    return {'name': _zip_name(r, rel), 'kind': 'file' if single else 'dir'}


def _zip_name(r, rel):
    # like _slug, but combining marks stay: a Thai name keeps its vowels (the header sends it as filename*)
    name = rel.rsplit('/', 1)[-1] if rel else r.name
    s = ''.join(ch if ch.isalnum() or ch in '-_.' or unicodedata.category(ch)[0] == 'M' else '-' for ch in name)
    return (s.strip('-.') or 'bucket')[:80] + '.zip'


def export(user, bid, path=''):
    """(zip file name, chunk iterator) of a folder (or one file) of a bucket: local files or a streamed S3 link.
    Names in the zip are relative to that folder (a file: just its name)."""
    r, rel, single = _export_check(user, bid, path)
    return _zip_name(r, rel), store.zip_items(_export_items(user, r, rel), store.posix_parent(rel) if single else rel)


EXPORT_TARGETS = ('s3', 'ftp', 'sftp')


def export_to(user, bid, path='', target=None, create_bucket=False, overwrite=False):
    """Copy a folder (or one file) of a bucket to S3 / FTP / SFTP through one of the user's connections
    (labeling/sinks). Layout: destination = target prefix (S3) or path (FTP / SFTP) + the path RELATIVE TO THE
    BUCKET ROOT, so exporting data/image or all of the bucket puts data/image/im1.png at the same key.
    create_bucket (S3): make the target bucket when missing. A non-empty destination needs overwrite.
    A job on the dc lane dc-export:<bid> (inline from the CLI) -> {job_id, result: {files, bytes, uri, created}}."""
    _need(user)
    r, rel, _ = _export_check(user, bid, path)
    t = dict(target or {})
    kind = t.get('kind')
    if kind not in EXPORT_TARGETS:
        raise DCError(f'export to one of {", ".join(EXPORT_TARGETS)} (not {kind!r})')
    if not t.get('connection_id'):
        raise DCError('pick one of your connections for the export')
    conns = _datasets().scoped(user['id'])              # the caller's own connections only
    try:
        conns.connection(t['connection_id'])
    except KeyError:
        raise DCError('pick one of your own connections for the export') from None
    tgt = {'kind': kind, 'connection_id': t['connection_id']}
    if kind == 's3':
        tgt.update(bucket=str(t.get('bucket') or '').strip(), prefix=t.get('prefix') or '',
                   create_bucket=bool(create_bucket))
        if not tgt['bucket']:
            raise DCError('pick the S3 bucket to export into')
        from ..data.sources import _norm_prefix
        src = _norm_prefix('/'.join(p for p in ((r.prefix or '').strip('/'), rel) if p))     # '' = the whole bucket
        dst = _norm_prefix('/'.join(p for p in (tgt['prefix'].strip('/'), rel) if p))
        if r.kind == 's3' and r.connection_id == tgt['connection_id'] and r.s3_bucket == tgt['bucket'] and \
                (dst.startswith(src) or src.startswith(dst)):     # the listing would meet its own copies
            raise DCError('the export target overlaps the folder being exported: pick another prefix')
    else:
        tgt['path'] = t.get('path') or t.get('prefix') or None
    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is not None and jobs.busy(f'dc-export:{r.id}'):     # submit would hand back that job, not this target
        raise DCError(f'bucket “{r.name}” is already being exported: wait for that job (or cancel it)', 409)
    from ..labeling import sinks
    sink = None
    try:
        if kind == 's3' and create_bucket:
            sinks.check_bucket_name(tgt['bucket'])
        sink = sinks.make_sink(tgt, conns, _cfg().get('s3') or {}, None)
        sink.check_target(bool(overwrite))
    except Exception as e:  # noqa: BLE001 - ValueError (bad name, not empty) or botocore / ftplib / paramiko
        if sink is not None:
            store._try(sink.close)                      # FTP / SFTP: the pooled logins
        if isinstance(e, ValueError):
            raise DCError(str(e)) from None
        raise DCError(f'cannot use the export target: {type(e).__name__}: {e}') from None

    def fn(job=None):
        stop = (lambda: job.cancelled.is_set()) if job is not None else (lambda: False)
        n, size = 0, 0
        if job is not None:
            job.progress(stage='listing files')
        try:
            for it in _export_items(user, r, rel):
                if stop():
                    break
                if it.path is not None:
                    sink.copy_file(it.rel, it.path)
                else:
                    body = it.open()
                    try:
                        sink.write(it.rel, body.read())     # one object in memory at a time per upload slot
                    finally:
                        body.close()
                n, size = n + 1, size + it.size
                if job is not None and n % 50 == 0:
                    job.progress(n, None, 'uploading', extra=f'{size / 1e6:.1f} MB')
        except BaseException:
            store._try(sink.close)                      # waits for the queued uploads, frees the connections
            raise
        try:
            res = sink.close()
        except ValueError as e:
            raise DCError(str(e)) from None
        if job is not None:
            job.progress(n, n, 'done')
        return {**res, 'created': bool(getattr(sink, 'created', False)), 'cancelled': stop()}

    if jobs is None:
        return {'job_id': None, 'result': fn()}
    return {'job_id': jobs.submit(f'dc-export:{r.id}', f'export {r.name} to {sink.uri}', fn, lane=LANE,
                                  public_result=True, owner=user['id']).id, 'result': None}


def delete_bucket(user, bid, delete_files=False):
    """Forget a bucket (tags, cache and undo rows go with it). delete_files: also remove a MANAGED bucket's
    folder; a linked folder / S3 prefix is never deleted (it is not ours)."""
    r = _row(user, bid)
    _need(user)
    if delete_files and r.kind != 'managed' and not (r.link == 's3' and r.local_root):
        raise DCError('only a managed bucket’s files can be deleted: a link is only forgotten')
    with db.begin() as c:
        undo_ids = c.execute(text('SELECT id FROM app.dc_undo WHERE bucket_id = :b'), {'b': r.id}).scalars().all()
        c.execute(text('DELETE FROM app.dc_buckets WHERE id = :b'), {'b': r.id})
    trash = store.data_root(_cfg()) / store.TRASH
    for u in undo_ids:
        shutil.rmtree(trash / u, ignore_errors=True)
    if delete_files:
        base = store.data_root(_cfg())
        root = Path(os.path.realpath(r.local_root if r.link == 's3' else r.root))  # an S3 link: only its copy
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
        res = _cached(r, rel, user=user, wait=False)          # S3: never a full listing inside the request
        out['format'] = res['format'] if res else None
        if res and not rel:
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
    _need_secret(user, path)
    if r.kind == 's3':
        return None
    p = _abs(r, _file(r, path))
    if not os.path.isfile(p):
        raise DCError(f'{path} is not a file', 404 if not os.path.lexists(p) else 400)
    return p, mimetypes.guess_type(p)[0] or 'application/octet-stream'


def read_file(user, bid, path, w=None, t=None):
    """(bytes, media type): the raw file, or with `w` a JPEG thumbnail at most w px (cached on disk); for a video
    the thumbnail is its frame at `t` seconds (default: a poster frame 10 % in)."""
    r = _row(user, bid)
    rel = _file(r, path)
    _need_secret(user, rel)
    is_video = store.kind_of(rel.rsplit('/', 1)[-1]) == 'video'
    if r.kind == 's3' and w and is_video:
        raise DCError('a video streamed from S3 has no preview: load the bucket locally or copy it', 415)
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
        if is_video:
            return store.video_thumbnail(p, f'{p}|{st.st_mtime_ns}|{st.st_size}', w, t, _cfg()), 'image/jpeg'
        return store.thumbnail(p, f'{p}|{st.st_mtime_ns}|{st.st_size}', w, _cfg()), 'image/jpeg'
    with open(p, 'rb') as f:
        return f.read(), mt


TEXT_MAX = 8 << 20                  # the text editor reads and saves at most this much
_BOM = b'\xef\xbb\xbf'


def _need_secret(user, rel):
    """A .env file holds secrets: reading it needs data.manage, like changing data."""
    if store.is_secret(rel):
        _need(user)


def _newline(text):
    crlf = text.count('\r\n')
    cr = text.count('\r') - crlf
    lf = text.count('\n') - crlf
    return '\r\n' if crlf > max(cr, lf) else '\r' if cr > lf else '\n'


def _decode(data, truncated):
    """(text, strict): UTF-8 without its BOM; strict is False when the bytes are not valid UTF-8 (a multi-byte
    character cut by the read limit does not count)."""
    body = data[3:] if data.startswith(_BOM) else data
    try:
        return body.decode('utf-8'), True
    except UnicodeDecodeError as e:
        if truncated and e.start >= len(body) - 3:
            return body[:e.start].decode('utf-8', 'replace'), True
        return body.decode('utf-8', 'replace'), False


def read_text(user, bid, path, limit=65536):
    """-> {text, truncated, size, mtime_ns, encoding, newline, editable, reason?}. text has '\\n' line ends (the
    file's own style is `newline`, kept on save) and no BOM (encoding 'utf-8-sig' when there was one). mtime_ns is
    a string (it does not fit a JS number) and goes back as write_text's expect_mtime_ns."""
    r = _row(user, bid)
    rel = _file(r, path)
    _need_secret(user, rel)
    limit = max(1, min(int(limit or 65536), TEXT_MAX))
    mtime_ns = None
    if r.kind == 's3':
        view = _s3(r)
        try:
            data, _, size = view.read(rel, limit + 1)
            if len(data) > limit:
                size = view.size(rel) or size
        finally:
            view.close()
    else:
        p, _ = file_path(user, bid, rel)
        with open(p, 'rb') as f:
            st = os.fstat(f.fileno())
            data = f.read(limit + 1)
        size, mtime_ns = st.st_size, str(st.st_mtime_ns)
    if b'\x00' in data[:8192]:
        raise DCError(f'{rel} is not a text file')
    truncated = len(data) > limit
    data = data[:limit]
    text_, strict = _decode(data, truncated)
    nl = _newline(text_)
    if nl != '\n':
        text_ = text_.replace('\r\n', '\n').replace('\r', '\n')
    reason = None
    if truncated:
        reason = f'only the first {limit // 1024} KB are shown' + (
            f' (the editor opens files up to {TEXT_MAX >> 20} MB)' if size and size <= TEXT_MAX else '')
    elif not strict:
        reason = 'not UTF-8 text: saving would change its bytes'
    elif not vtiers.has(user, PERM):
        reason = 'your tier cannot change data'
    else:
        try:
            _need_write(r)
        except DCError as e:
            reason = e.message
    out = {'text': text_, 'truncated': truncated, 'size': size, 'mtime_ns': mtime_ns,
           'encoding': 'utf-8-sig' if data.startswith(_BOM) else 'utf-8', 'newline': nl, 'editable': reason is None}
    if reason is not None:
        out['reason'] = reason
    return out


def write_text(user, bid, path, text, expect_mtime_ns=None, create=False):
    """Save a text file (or create one) -> {path, size, mtime_ns, undo_id, retagged}. 409 when the file changed
    since it was read (its mtime is not expect_mtime_ns; None overwrites). The file keeps its mode, BOM and
    newline style; the old content goes to the trash and undo puts it back."""
    import io
    from sqlalchemy import text as sql                  # `text` is the file content here
    r = _row(user, bid)
    _need(user)
    _need_write(r)
    rel = _file(r, path)
    if not isinstance(text, str):
        raise DCError('text must be a string')
    p = os.path.realpath(_abs(r, rel, must_exist=not create))
    base = os.path.realpath(r.root)
    rel = os.path.relpath(p, base).replace(os.sep, '/')
    if not store.visible(os.path.basename(p)):
        raise DCError(f'{rel} is a hidden file')
    exists = os.path.lexists(p)
    if create and exists:
        raise DCError(f'{rel} already exists', 409)
    body = text.replace('\r\n', '\n').replace('\r', '\n')
    old = mode = None
    if exists:
        if not os.path.isfile(p):
            raise DCError(f'{rel} is not a file')
        st = os.stat(p)
        if expect_mtime_ns not in (None, '') and str(expect_mtime_ns) != str(st.st_mtime_ns):
            raise DCError(f'{rel} was changed by someone else since you opened it', 409)
        if st.st_size > TEXT_MAX:
            raise DCError(f'{rel} is larger than {TEXT_MAX >> 20} MB: it cannot be edited here', 413)
        with open(p, 'rb') as f:
            old = f.read()
        if b'\x00' in old[:8192]:
            raise DCError(f'{rel} is not a text file')
        prev, strict = _decode(old, False)
        if not strict:
            raise DCError(f'{rel} is not UTF-8 text: saving would change its bytes', 415)
        nl = _newline(prev)
        if nl != '\n':
            body = body.replace('\n', nl)
        mode = st.st_mode & 0o7777
    elif not os.path.isdir(os.path.dirname(p)):
        raise DCError(f'{store.posix_parent(rel) or "/"} is not a folder', 404)
    data = ((_BOM if old is not None and old.startswith(_BOM) else b'') + body.encode('utf-8'))
    if len(data) > TEXT_MAX:
        raise DCError(f'at most {TEXT_MAX >> 20} MB of text', 413)
    undo_id = uuid.uuid4().hex
    trash_root = store.data_root(_cfg()) / store.TRASH
    trash = trash_root / undo_id
    if old is not None:
        slot = trash / '0'
        slot.mkdir(parents=True, exist_ok=True)
        keep = slot / os.path.basename(p)
        shutil.copy2(p, keep)
        step = {'undo': 'replace', 'trash': str(keep), 'path': rel}
    else:
        step = {'undo': 'remove', 'path': rel}
    store._write_stream(io.BytesIO(data), p)
    if mode is not None:
        try:
            os.chmod(p, mode)
        except OSError:
            pass
    st = os.stat(p)
    with db.begin() as c:
        c.execute(sql("""INSERT INTO app.dc_undo(id, bucket_id, user_id, manifest)
                         VALUES (:id, :b, :u, :manifest)""").bindparams(_MANIFEST),
                  {'id': undo_id, 'b': r.id, 'u': user['id'],
                   'manifest': {'root': r.root, 'trash': str(trash), 'steps': [step]}})
        c.execute(sql('UPDATE app.dc_buckets SET updated_at = now() WHERE id = :b'), {'b': r.id})
    return {'path': rel, 'size': st.st_size, 'mtime_ns': str(st.st_mtime_ns), 'undo_id': undo_id,
            'created': old is None, 'retagged': _revalidate(r)}


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
    auto = getattr(r._r, 'video_auto', None)
    before = _videos_below(r, path) if auto else None
    out = store.upload_local(r.root, _allowed(), path, files, unzip=unzip)
    with db.begin() as c:
        c.execute(text('UPDATE app.dc_buckets SET updated_at = now() WHERE id = :b'), {'b': r.id})
    out['retagged'] = _revalidate(r) if out['added'] else None
    if auto and out['added']:
        out['frames'] = _auto_frames(user, r, sorted(_videos_below(r, path) - before))
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
    names, kpt = _yaml_names(r, rel)
    task = {'name': {'object_detection': 'Detect', 'segmentation': 'Segment', 'keypoints': 'Pose',
                     'classification': 'Classify'}[tt], 'task_type': tt, 'classes': names[:500]}
    if tt == 'keypoints':
        k = int(kpt[0]) if isinstance(kpt, (list, tuple)) and kpt else 17
        task['config'] = {'keypoints': [f'kp{i + 1}' for i in range(k)]}
    source = _label_source(r, rel)
    reg = _labeling()
    try:
        entry = reg.create({'name': sessions.free_name(user['id'], 'label', name[:200]), 'tasks': [task],
                            'source': source}, user['id'])
    except (ValueError, KeyError) as e:
        raise DCError(str(e).strip("'\"")) from None
    job_id = _submit_label_import(entry['id'], entry['source'], entry['owner_id'], entry['name'])
    return {'target': 'label', 'id': entry['id'], 'url': f'?label={entry["id"]}', 'name': entry['name'],
            'job_id': job_id}


def _yaml_names(r, rel):
    """(class names, kpt_shape) from the data YAML of a local folder; ([], None) for S3 or without one."""
    if r.kind == 's3':
        return [], None
    try:
        tree = formats.LocalTree(_abs(r, rel))
        names, kpt, _ = formats._yaml_info(tree, '', [n for n, d in tree.scan() if not d])
        return list(names or []), kpt
    except DCError:
        return [], None


def _label_source(r, rel):
    """The labeling-project source of folder `rel` (images are referenced in place, never copied)."""
    if r.kind == 's3':
        return {'kind': 's3', 'connection_id': r.connection_id, 'bucket': r.s3_bucket,
                'prefix': '/'.join(p for p in (r.prefix.strip('/'), rel) if p)}
    return {'kind': 'local', 'path': _abs(r, rel)}


def source(user, bid, path=''):
    """What a new labeling session needs to read folder `path` of a bucket (New label › Data Center):
    {source, format, classes, bucket, path} -- source is the POST /api/label/projects `source`."""
    r = _row(user, bid)
    rel = store.clean_rel(path)
    if r.kind != 's3' and not os.path.isdir(_abs(r, rel)):
        raise DCError(f'{rel} is a file: pick the folder that holds it')
    fmt = _cached(r, rel)['format']
    names, _ = _yaml_names(r, rel)
    return {'source': _label_source(r, rel), 'format': fmt, 'classes': names[:500], 'bucket': r.id, 'path': rel,
            'name': r.name + (f' · {rel}' if rel else '')}


def _submit_label_import(pid, source, owner_id, name):
    """List the folder's images into the new project (= the import job of POST /api/label/projects): a job when
    the server's job manager is wired in, else inline (CLI)."""
    fn = _label_import_fn(pid, source, owner_id)
    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        fn()
        return None
    return jobs.submit(f'label-import:{pid}', f'import images · {name}', fn, lane='io', session_id=pid).id


PROBE_THREADS = 8                   # label import: image headers read side by side (I/O bound)


def _label_import_fn(pid, source, owner_id):
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

            def probe(rel):
                try:
                    return media.probe(src, rel)
                except Exception:  # noqa: BLE001 - one unreadable file must not stop the import
                    return None

            with ThreadPoolExecutor(PROBE_THREADS) as pool:       # header reads are I/O: probe them side by side
                for i in range(0, len(new), 500):
                    rows = []
                    chunk = new[i:i + 500]
                    for rel, nm, info in zip(chunk, names[i:i + 500], pool.map(probe, chunk)):
                        if info is None:
                            continue
                        rows.append({'file_name': nm, 'uri': rel, 'mime': info.get('mime'),
                                     'size_bytes': info.get('size_bytes'), 'width': info.get('width'),
                                     'height': info.get('height'),
                                     'meta': {'rel_path': rel, 'orientation': info.get('orientation')}})
                    added += st.add_media(rows)
                    if job is not None:
                        job.progress(done=i + len(chunk), total=len(new), extra=f'{added} added')
            reg.set_counts(pid)
            return {'added': added}
        finally:
            src.close()
    return fn


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


# ---------------------------------------------------------------------------------------------- label with a model
AUTO_TASKS = {'object_detection': 'Detect', 'segmentation': 'Segment', 'keypoints': 'Pose',
              'classification': 'Classify'}


def _hooks():
    hooks = getattr(_conf['ctx'], 'label_auto', None) if _conf['ctx'] is not None else None
    if hooks is None:
        raise DCError('labeling with a model runs in the server (the labeling routes are not loaded here)', 501)
    return hooks


def _http(fn, *a, **kw):
    """Call a labeling-route helper; its HTTPException / ModelError / ValueError becomes a DCError."""
    from starlette.exceptions import HTTPException

    from ..labeling.models import ModelError
    try:
        return fn(*a, **kw)
    except HTTPException as e:
        d = e.detail.get('detail') if isinstance(e.detail, dict) else e.detail
        raise DCError(str(d), e.status_code) from None
    except ModelError as e:
        raise DCError(str(e), getattr(e, 'status', 400)) from None
    except (ValueError, KeyError) as e:
        raise DCError(str(e).strip("'\""), 404 if isinstance(e, KeyError) else 400) from None


def _link_of(model):
    model = dict(model or {})
    rid, cid = model.get('registry_id') or None, model.get('connection_id') or None
    if not rid and not (cid and model.get('model_id')):
        raise DCError('pick a model: one of Settings › Models, or a model server and one of its models')
    return {'registry_id': rid, 'connection_id': None if rid else cid,
            'model_id': rid or str(model['model_id']), 'version': model.get('version') or None}


def autolabel(user, bid, path='', model=None, target='inplace', new_name=None, conf=None, write_yolo=True,
              name=None, formats=None):
    """Label every image of folder `path` with a model: a labeling session is made (its classes = the model's
    classes), the model is linked and run over every image (the label-all job), and the result is written into
    the bucket in each of `formats` (labeling/writers keys, default ['yolo']: labels/ mirroring images/,
    data.yaml + classes.txt; coco: annotations/instances_<split>.json; voc: Annotations/; labelme: labelme/;
    createml.json; labels.csv; ...). write_yolo=False is the older spelling of formats=[] (write nothing).

    target 'inplace' labels the bucket itself (it must be writable); 'copy' first copies the folder into a new
    managed bucket `new_name` and labels that (a streamed S3 bucket can only be copied).
    -> {job_id, bucket (the labeled one), result?}: one job on the model lane; its result carries the session."""
    r = _row(user, bid)
    _need(user)
    _need(user, 'label.create')
    hooks = _hooks()
    link = _link_of(model)
    if conf is not None and not 0 <= float(conf) <= 1:
        raise DCError('confidence must be between 0 and 1')
    rel = store.clean_rel(path)
    if r.kind != 's3' and not os.path.isdir(_abs(r, rel)):
        raise DCError(f'{rel} is a file: pick the folder that holds the images')
    if target not in ('inplace', 'copy'):
        raise DCError('target must be inplace or copy')
    if target == 'inplace':
        _need_write(r)
    from ..labeling import writers as lw
    try:
        fmts = [] if write_yolo is False and formats is None else lw.check(lw.DEFAULT if formats is None else formats)
    except ValueError as e:
        raise DCError(str(e)) from None
    shown = str((model or {}).get('name') or link['model_id'])     # the job label only; the job reads the model

    def check_model():
        # in the job, never in the request: a registry model may have to load (torch) to report its classes
        info = _http(hooks.model_info, link, user['id'])
        tt = info.get('task_type') or 'object_detection'
        classes = [str(c) for c in info.get('classes') or []]
        if tt not in AUTO_TASKS:
            raise DCError(f'{info.get("name") or link["model_id"]} is a {tt} model: it cannot label images')
        if not classes and tt != 'classification':
            raise DCError(f'{info.get("name") or link["model_id"]} reports no classes')
        return info, tt, classes

    dest = r
    if target == 'copy':                       # an empty managed bucket: one insert, no walk
        dest = _row(user, create_bucket(user, new_name or f'{r.name}-labeled', 'managed')['id'])

    def fn(job=None):
        if job is not None:
            job.progress(stage='checking the model', done=0, total=None)
        try:
            info, tt, classes = check_model()
        except Exception:
            if target == 'copy':               # nothing was copied yet: do not leave an empty bucket behind
                delete_bucket(user, dest.id, delete_files=True)
            raise
        session_name = (name or '').strip() or f'{dest.name}{f" · {rel}" if rel and target == "inplace" else ""} · ' \
                                                 f'{info.get("name") or link["model_id"]}'
        out = {'bucket': dest.id, 'copied': None}
        use = [k for k in fmts if lw.supports(k, tt)]
        if len(use) < len(fmts):
            out['formats_skipped'] = [k for k in fmts if k not in use]
        src_rel = rel
        if target == 'copy':
            out['copied'] = _copy_folder(r, rel, dest.root, job)
            src_rel = ''
        if {'yolo', 'yolo_obb'} & set(use):    # loose images -> images/ first, so the result is a YOLO layout
            out['restructured'] = _to_yolo_layout(user, dest, src_rel, undoable=target == 'inplace')
        if target == 'copy':
            _revalidate(dest)
        if job is not None:
            job.progress(stage='making the labeling session', done=0, total=None)
        from ..data import sessions
        task = {'name': AUTO_TASKS[tt], 'task_type': tt, 'classes': [], 'config': {}}
        if tt == 'keypoints':
            task['config'] = {'keypoints': list(info.get('keypoints') or [f'kp{i + 1}' for i in range(17)])}
        reg = _labeling()
        try:
            entry = reg.create({'name': sessions.free_name(user['id'], 'label', session_name[:200]), 'tasks': [task],
                                'source': _label_source(dest, src_rel)}, user['id'])
        except (ValueError, KeyError) as e:
            raise DCError(str(e).strip("'\"")) from None
        pid = entry['id']
        out['session'] = {'id': pid, 'name': entry['name'], 'url': f'?label={pid}'}
        if job is not None:
            job.progress(stage='listing the images')
        _label_import_fn(pid, entry['source'], user['id'])(job)
        st = _http(hooks.store, pid)
        tid = reg.project(pid)['tasks'][0]['id']
        body = SimpleNamespace(registry_id=link['registry_id'], connection_id=link['connection_id'],
                               model_id=link['model_id'], version=link['version'],
                               conf=float(conf if conf is not None else 0.25), auto_on_open=False,
                               class_map={c: 'new' for c in classes})
        _http(hooks.link, pid, tid, body)
        hooks.activate(pid, tid, True)
        task = st.task(tid)
        run = _http(hooks.label_all, pid, reg.get(pid), task, task['config']['model'], 'all', body.conf)
        out['labeled'] = run(job) if job is not None else run(_InlineJob())
        if use and not out['labeled'].get('cancelled'):
            if job is not None:
                job.progress(stage=f'writing {", ".join(lw.get(k).label for k in use)} labels', done=0, total=None)
            out['written'] = _write_labels(dest, src_rel, st, st.task(tid), use)
            _cached(_row(user, dest.id), src_rel, force=True)
            out['retagged'] = _revalidate(_row(user, dest.id))
        return out

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None)
    if jobs is None:
        return {'job_id': None, 'bucket': dest.id, 'result': fn()}
    job = jobs.submit(f'dc-autolabel:{dest.id}:{rel}', f'label {dest.name} with {shown}',
                      fn, lane='model', public_result=True, owner=user['id'])
    return {'job_id': job.id, 'bucket': dest.id, 'result': None}


def _to_yolo_layout(user, r, rel, undoable):
    """A folder with images but no images/ sub-folder: move its images (and sub-folders holding images) into
    images/, the layout YOLO tools and format detection expect (labels/ then mirrors it). In place this goes
    through ops(), so it has an undo id; a fresh copy is moved directly. None when nothing had to move."""
    base = _abs(r, rel)
    if os.path.basename(base).lower() in ('images', 'labels'):
        return None
    entries = [e for e in os.scandir(base) if not e.name.startswith('.')]
    if any(e.is_dir() and e.name.lower() == 'images' for e in entries):
        return None
    skip = {'labels', 'annotations', 'data.yaml', 'classes.txt'}

    def holds_images(path):
        return any(store.is_image(n) for _, _, fs in os.walk(path) for n in fs)

    moves = [e.name for e in entries if e.name.lower() not in skip and
             (store.is_image(e.name) if e.is_file() else e.is_dir() and holds_images(e.path))]
    if not moves:
        return None
    if not undoable:
        os.makedirs(os.path.join(base, 'images'))
        for n in moves:
            shutil.move(os.path.join(base, n), os.path.join(base, 'images', n))
        return {'moved': len(moves), 'undo_id': None}
    res = ops(user, r.id, [{'op': 'mkdir', 'path': store.join(rel, 'images')}] +
              [{'op': 'move', 'path': store.join(rel, n), 'to': store.join(rel, 'images')} for n in moves])
    if res['errors']:
        raise DCError(f'could not move the images into images/: {res["errors"][0]["error"]}')
    return {'moved': len(moves), 'undo_id': res['undo_id']}


class _InlineJob:
    """A stand-in job for running the label-all body without the job manager."""

    def __init__(self):
        self.cancelled = threading.Event()

    def progress(self, *a, **k):
        pass


def _copy_folder(r, rel, dest, job=None):
    """Copy folder `rel` of bucket `r` into folder `dest` (an S3 one is downloaded) -> {files, bytes}."""
    progress = job.progress if job is not None else (lambda *a, **k: None)
    cancelled = (lambda: job.cancelled.is_set()) if job is not None else None
    if r.kind == 's3':
        view = _s3(r, rel)
        try:
            res = view.pull(dest, progress, cancelled)
        finally:
            view.close()
        return {'files': res['downloaded'], 'bytes': res['bytes']}
    allowed = _allowed()
    todo = [(p, a) for p, isdir, a in store.walk_local(r.root, rel, allowed, cap=10_000_000) if not isdir]
    progress(0, len(todo), 'copying')
    n = size = 0
    for p, a in todo:
        if cancelled is not None and cancelled():
            raise DCError('cancelled', 499)
        sub = p[len(rel) + 1:] if rel else p
        out = os.path.join(dest, sub)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(a, out)
        n += 1
        size += os.path.getsize(out)
        if n % 200 == 0:
            progress(n, len(todo), 'copying')
    progress(n, len(todo), 'copying')
    return {'files': n, 'bytes': size}


def _write_labels(r, rel, st, task, formats=('yolo',)):
    """Write the session's labels into folder `rel` of bucket `r` in each of `formats` (labeling/writers keys),
    through a LocalDirSink rooted at the folder. YOLO: each image's .txt goes in the labels/ folder that mirrors
    its images/ folder (next to the image when there is no images/ level, which is where ultralytics looks then),
    plus data.yaml and classes.txt at the folder root. When the picked folder is images/ itself the files go one
    level up (labels/ beside it). -> {labels, data_yaml?, classes, formats: {key: summary}, warnings?}."""
    from ..labeling import formats as lfmt, writers as lw
    from ..labeling.sinks import LocalDirSink
    base, root_rel, prefix = _abs(r, rel), rel, ''
    if rel and os.path.basename(base).lower() == 'images':                  # the picked folder is images/ itself
        base, root_rel, prefix = os.path.dirname(base), posix_dir(rel), os.path.basename(base) + '/'
    index, names = lfmt.class_map(task['classes'])

    def label_rel(img):
        folder, stem = posix_dir(img), os.path.splitext(img.rsplit('/', 1)[-1])[0]
        lab_dir = store.label_dir_for(folder)
        return store.join(lab_dir if lab_dir is not None else folder, stem + '.txt')

    def data_yaml(task, names):
        splits = [d for d in ('train', 'val', 'valid', 'test') if os.path.isdir(os.path.join(base, 'images', d))]
        if splits:
            train = 'images/train' if 'train' in splits else f'images/{splits[0]}'
            val = next((f'images/{d}' for d in ('val', 'valid') if d in splits), train)
        else:
            train = val = 'images' if os.path.isdir(os.path.join(base, 'images')) else '.'
        lines = [f'# written by the Data Center from a {task["task_type"].replace("_", " ")} model', 'path: .',
                 f'train: {train}', f'val: {val}', f'nc: {len(names)}', 'names:']
        lines += [f'  {i}: {_yaml_str(nm)}' for i, nm in enumerate(names)]
        if task['task_type'] == 'keypoints':
            k = len((task.get('config') or {}).get('keypoints') or [])
            lines.append(f'kpt_shape: [{k}, 3]')
        return '\n'.join(lines) + '\n'

    keys = [k for k in formats if lw.supports(k, task['task_type'])]
    sink = LocalDirSink(base)
    ctx = lw.bucket_ctx(base, index, label_rel, data_yaml, keys=keys, image_prefix=prefix)
    ws = []
    try:
        for k in keys:
            w = lw.get(k)()
            w.begin(sink, task, names, ctx)
            ws.append(w)
        for m, rows in st.iter_task_export(task['id']):
            if not rows and m['status'] != 'done':
                continue
            meta = m.get('meta') if isinstance(m.get('meta'), dict) else {}
            if not (meta.get('rel_path') or m.get('uri')):
                continue
            for w in ws:
                w.add(m, rows)
        done = {w.key: w.finish() for w in ws}
    except BaseException:
        for w in ws:
            w.abort()
        raise
    yolo = done.get('yolo') or done.get('yolo_obb')
    out = {'labels': (yolo or max(done.values(), key=lambda d: d.get('n_media', 0), default={})).get('n_media', 0),
           'classes': names, 'formats': done}
    if yolo:
        out['data_yaml'] = store.join(root_rel, 'data.yaml')
    if ctx['warnings']:
        out['warnings'] = ctx['warnings']
    return out


_write_yolo = _write_labels           # the older name


def _yaml_str(s):
    import json
    return json.dumps(str(s), ensure_ascii=False)


def posix_dir(rel):
    return rel.rsplit('/', 1)[0] if '/' in rel else ''
