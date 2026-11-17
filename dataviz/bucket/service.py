"""
The Bucket page's one entry point (contract: bucket/API.md). The routes (server/routes/bucket.py), the
LLM tools (llm/tools.py) and the CLI (`python -m dataviz bucket`) call only these functions, so a rule is enforced
in one place: who sees a bucket (its owner; admins see all), who may change it (tier permission bucket.manage),
which buckets are read-only (S3 links), and that every change is followed by a format re-check of the bucket
root (tags.revalidate) whose outcome travels back as `retagged`.

Every function takes the acting `user` dict first, raises BucketError(message, status) and returns JSON-able dicts.
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
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from .. import db, zones
from ..auth import tiers as vtiers
from . import cloudiff, formats, s3dir, s3list, store, tags as vtags, video
from .store import BucketError

__all__ = ['BucketError', 'configure', 'list_buckets', 'get_bucket', 'create_bucket', 'update_bucket', 'delete_bucket',
           'summary', 'ls', 'read_file', 'read_text', 'ops', 'undo', 'upload', 'set_tags', 'all_tags', 'detect',
           'stats', 'search', 'send', 'sync', 'export', 'export_check', 'export_to', 'source', 'autolabel', 'write_text',
           'video_info', 'estimate_frames', 'extract_frames', 'set_video_auto', 'fetch_local', 'check_local',
           'drop_local']

KINDS = ('managed', 'local', 's3')
MODES = ('stream', 'local')         # an S3 link: read S3 on every request, or an editable local copy
PERM = 'bucket.manage'
LANE = 'bucket'                        # job lane of the bulk work: fair per account, never in the way of opening a session
S3_TTL = 3600                       # an S3 detection is fresh this long (no cheap fingerprint for a prefix)
SEARCH_SCAN = 300_000               # entries walked by one search
LS_WAIT = 0.8                       # s an S3 folder listing is waited for before ls answers with what it has
CHECK_TTL = 600                     # s a downloaded folder's comparison with S3 is trusted (ls re-checks after)
SCAN_RETRY = 300                    # s a failed / cancelled background S3 scan is not queued again by ls
_JSONB = bindparam('result', type_=JSONB)
_MANIFEST = bindparam('manifest', type_=JSONB)

_conf = {'ctx': None, 'cfg': None, 'datasets': None, 'labeling': None}
_quiet = {}     # ('scan', bid, rel) | ('check', bid) -> monotonic time ls may queue that job again (it failed or was
                # cancelled: ls is polled every second while a folder lists, it must not re-list S3 on each poll)
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
            from ..image.connections import Registry
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
        raise BucketError('sign in first', 401)
    if not vtiers.has(user, perm):
        t = vtiers.tier_of(user)
        raise BucketError(f'your tier ({t["name"] if t else "none"}) cannot {vtiers.DENIED.get(perm, perm)}: '
                      'ask an administrator', 403)


def _epoch(dt):
    return dt.timestamp() if dt is not None else None


# ---------------------------------------------------------------------------------------------- rows
_BUCKET_SQL = """SELECT b.*, u.username, u.display_name FROM app.buckets b
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
        raise BucketError('sign in first', 401)
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
        raise BucketError(f'no bucket {bid!r}', 404)
    return rows[0]


def _writable(r):
    if r.kind == 's3':
        return False
    return os.path.isdir(r.root) and os.access(r.root, os.W_OK)


def _importing(bid):
    jobs = getattr(_conf['ctx'], 'jobs', None) if _conf['ctx'] is not None else None
    return bool(jobs is not None and jobs.busy(f'bk-import:{bid}'))


def _need_write(r):
    if _importing(r.id):
        raise BucketError(f'bucket “{r.name}” is still importing: it can be changed once the download finished', 409)
    if r.kind == 's3' and r.mode == 'local':
        raise BucketError(f'bucket “{r.name}” is still loading from S3: it can be changed once the download finished',
                      409)
    if r.kind == 's3':
        raise BucketError(f'bucket “{r.name}” is streamed from S3 and read-only: switch it to “load into local” '
                      '(Edit) or copy the data into a managed bucket to change it')
    if not _writable(r):
        raise BucketError(f'bucket “{r.name}” cannot be written ({r.root} is missing or read-only)')


class _LocalView:
    """The downloaded folders of a streamed S3 link seen as a local bucket: root = its local copy, which mirrors the
    bucket's layout, so the bucket's own paths, store ops and undo manifests work unchanged. Changes stay local
    (never written back to S3)."""
    local_view = True

    def __init__(self, r):
        self._v = r
        self.kind = 'local'
        self.root = r.local_root

    def __getattr__(self, k):
        return getattr(self._v, k)


def _streamed(r):
    return r.kind == 's3' and not getattr(r, 'local_view', False)


def _outside(r, rel):
    return BucketError(f'{rel or "the bucket root"} of “{r.name}” is streamed from S3 and read-only: download that '
                   'folder first (Download to local) and change it there, or load the whole bucket into local')


def _write_view(r, sources=(), targets=()):
    """The row to write through after the write checks: `r` itself, or for a streamed S3 link its local copy
    (_LocalView) when every path is in a downloaded folder: `sources` (moved, renamed, deleted, rewritten) strictly
    inside one, `targets` (a destination folder, a new file's path) inside or equal to one. A download running into
    the bucket blocks writes (409)."""
    if not _streamed(r):
        _need_write(r)
        return r
    rows = _local_rows(r.id) if r.local_root else {}
    if not rows:
        _need_write(r)                                  # the "streamed and read-only" message
    if _busy(f'bk-fetch:{r.id}:'):
        raise BucketError(f'a download into “{r.name}” is running: it can be changed once it finished', 409)
    _need_inside(r, rows, sources, targets)
    v = _LocalView(r)
    _need_write(v)
    return v


def _need_inside(r, rows, sources=(), targets=()):
    for p in sources:
        p = store.clean_rel(p)
        c = _cover(rows, p)
        if c is None:
            raise _outside(r, p)
        if c['path'] == p:
            raise BucketError(f'{p or "the whole bucket"} is a downloaded folder itself: change what is inside it, or '
                          'use Remove local copy to drop the download')
    for p in targets:
        p = store.clean_rel(p)
        if _cover(rows, p) is None:
            raise _outside(r, p)


def _op_paths(op):
    """(sources, targets) of one file op, for _need_inside."""
    kind = op.get('op')
    if kind in ('mkdir', 'create'):
        return (), (str(op.get('path') or ''),)
    src = (str(op.get('path') or ''),)
    return (src, (str(op.get('to') or ''),)) if kind in ('move', 'copy') else (src, ())


def _location(r, rel=''):
    """Where folder `rel` of a bucket is read from: s3://bucket/prefix/rel for a streamed link, else the local path."""
    rel = store.clean_rel(rel)
    if _streamed(r):
        return _s3_url(r, rel)
    root = r.root or ''
    return os.path.join(root, rel) if rel else root


def _s3_url(r, rel=''):
    return 's3://' + '/'.join(x for x in (r.s3_bucket or '', (r.prefix or '').strip('/'), store.clean_rel(rel)) if x)


def _s3(r, rel='', shared=False):
    """The S3 view of a link (of its sub-folder `rel`); shared: the interactive one (a reused client, short
    timeouts, offline breaker) for listings and reads, else a job's own client."""
    if not r.connection_id:
        raise BucketError(f'the S3 connection of bucket “{r.name}” was deleted', 409)
    try:
        conn = _datasets().connection(r.connection_id, owner_id=r.owner_id)
    except KeyError:
        raise BucketError(f'the S3 connection of bucket “{r.name}” was deleted', 409) from None
    return store.S3View(conn, r.s3_bucket, '/'.join(p for p in ((r.prefix or '').strip('/'), rel) if p),
                        (_cfg().get('s3') or {}), shared=shared)


def _abs(r, path, must_exist=True):
    return store.resolve(r.root, path, _allowed(), must_exist=must_exist)


# ---------------------------------------------------------------------------------------------- detection cache
def _cached(r, rel, force=False, want_tree=False, user=None, wait=True, refresh=True):
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
            raise BucketError(f'{rel or "/"} is not a folder')
        fp = formats.fingerprint(absp)
    with db.connect() as c:
        hit = c.execute(text('SELECT fingerprint, result FROM app.bucket_format_cache WHERE bucket_id = :b AND path = :p'),
                        {'b': r.id, 'p': rel}).first()
    if hit is not None and not force and hit.fingerprint == fp and (hit.result.get('tree') or not (want_tree or not rel)):
        return hit.result
    if r.kind == 's3' and not wait:
        jobs = getattr(_conf['ctx'], 'jobs', None) if _conf['ctx'] is not None else None
        if (refresh and _quiet.get(('scan', r.id, rel), 0) <= time.monotonic()
                and (jobs is None or not jobs.busy(f'bk-scan:{r.id}:'))):      # one scan per bucket at a time on the dc lane
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
        c.execute(text("""INSERT INTO app.bucket_format_cache(bucket_id, path, fingerprint, result, updated_at)
                          VALUES (:b, :p, :fp, :result, now())
                          ON CONFLICT (bucket_id, path) DO UPDATE SET fingerprint = EXCLUDED.fingerprint,
                          result = EXCLUDED.result, updated_at = now()""").bindparams(_JSONB),
                  {'b': r.id, 'p': rel, 'fp': fp, 'result': res})


def _scan_soon(user, r, rel=''):
    """List + detect folder `rel` of S3 link `r` in a bucket-lane job bk-scan:<bid>:<rel> (the running one when it is
    already queued; inline from the CLI): progress shows the object count, Cancel stores nothing. -> job id."""
    rel = store.clean_rel(rel)

    def fn(job=None):
        cancelled = (lambda: job.cancelled.is_set()) if job is not None else None
        try:
            res = _scan(r, rel, True, job.progress if job is not None else None, cancelled)
        except Exception:
            _quiet[('scan', r.id, rel)] = time.monotonic() + SCAN_RETRY
            raise
        if cancelled is not None and cancelled():
            _quiet[('scan', r.id, rel)] = time.monotonic() + SCAN_RETRY
            return {'cancelled': True}
        _quiet.pop(('scan', r.id, rel), None)
        _store_cache(r, rel, _s3_fp(), res)
        retagged = vtags.apply_format(r.id, res['format']) if not rel else None
        return {'format': (res['format'] or {}).get('format'), 'stats': res['tree'], 'retagged': retagged}

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        fn()
        return None
    return jobs.submit(f'bk-scan:{r.id}:{rel}', f'scan {r.name}{"/" + rel if rel else ""} on S3', fn, lane=LANE,
                       public_result=True, owner=(user or {}).get('id') or r.owner_id).id


def _revalidate(r):
    """Re-detect the bucket root (forced) and swap its auto tag -> retagged or None. A change in the downloaded
    folders of a streamed link only asks for a new comparison with S3 (the copy is partial: the format is S3's)."""
    if getattr(r, 'local_view', False):
        with db.begin() as c:
            c.execute(text('UPDATE app.bucket_s3_local SET checked_at = NULL WHERE bucket_id = :b'), {'b': r.id})
        return None
    try:
        res = _cached(r, '', force=True)
    except BucketError:
        return None
    return vtags.apply_format(r.id, res['format'])


# ---------------------------------------------------------------------------------------------- bucket shape
def _sessions_for(rows, conn):
    """{bucket id: [{id, kind, name, url}]}: the owner's sessions whose source lies inside the bucket. Each zone
    with sessions lists its own (zones.bucket_session_finders)."""
    out = {r.id: [] for r in rows}
    if rows:
        for find in zones.bucket_session_finders(_cfg()):
            find(rows, conn, out)
    return out


def _shape(rows, conn):
    if not rows:
        return []
    ids = [r.id for r in rows]
    cache = {c.bucket_id: c.result for c in conn.execute(
        text("SELECT bucket_id, result FROM app.bucket_format_cache WHERE bucket_id = ANY(:b) AND path = ''"),
        {'b': ids}).all()}
    tg = vtags.for_buckets(ids, conn)
    ses = _sessions_for(rows, conn)
    loc = {x.bucket_id: x for x in conn.execute(text(
        """SELECT bucket_id, count(*) AS n, count(*) FILTER (WHERE state IN ('outdated', 'error')) AS old
           FROM app.bucket_s3_local WHERE bucket_id = ANY(:b) GROUP BY bucket_id"""), {'b': ids}).all()}
    out = []
    for r in rows:
        res = cache.get(r.id) or {}
        fmt = res.get('format')
        tree = res.get('tree') or {}
        out.append({
            'id': r.id, 'name': r.name, 'kind': r.link, 'root': r.root, 'writable': _writable(r),
            'location': _location(r),
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
                'local_ready': r.local_copy, 'synced_at': _epoch(r.synced_at), 's3_root': r._r.root,
                'local_folders': loc[r.id].n if r.id in loc else 0, 'local_outdated': loc[r.id].old if r.id in loc else 0}
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
        except BucketError:
            pass
    with db.connect() as c:
        return _shape([r], c)[0]


def _clean_name(name):
    name = ' '.join(str(name or '').split())
    if not 1 <= len(name) <= 200:
        raise BucketError('a bucket name needs 1 to 200 characters')
    if '/' in name or '\\' in name:
        raise BucketError('a bucket name cannot contain / or \\')
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
        raise BucketError('kind must be managed, local or s3')
    mode = mode or 'stream'
    if mode not in MODES:
        raise BucketError('mode must be stream or local')
    try:
        utags = vtags.clean_tags(list(tags or []))
    except ValueError as e:
        raise BucketError(str(e)) from None
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
        from ..image import connections
        if not str(path or '').strip():
            raise BucketError('path is required for a local link')
        try:
            root = connections.check_local(path, connections.allowed_roots(cfg))
        except ValueError as e:
            raise BucketError(str(e)) from None
        if not os.path.isdir(root):
            raise BucketError(f'not a folder: {root}')
    else:
        if not connection_id or not str(s3_bucket or '').strip():
            raise BucketError('an S3 link needs one of your connections and a bucket')
        try:
            cn = _datasets().connection(connection_id, owner_id=user['id'])
        except KeyError:
            raise BucketError('pick one of your own S3 connections') from None
        if (cn.get('kind') or 's3') != 's3':
            raise BucketError('an S3 link needs an S3 connection')
        s3b, pre, cid = str(s3_bucket).strip(), store.clean_rel(prefix or ''), connection_id
        root = f's3://{s3b}/{pre}'.rstrip('/')
    bid = uuid.uuid4().hex
    try:
        with db.begin() as c:
            c.execute(text("""INSERT INTO app.buckets(id, owner_id, name, kind, root, connection_id, s3_bucket, prefix)
                              VALUES (:id, :o, :n, :k, :r, :cid, :b, :p)"""),
                      {'id': bid, 'o': user['id'], 'n': name, 'k': kind, 'r': root, 'cid': cid, 'b': s3b, 'p': pre})
            for t in utags:
                if t.casefold().startswith(vtags.AUTO_PREFIX):
                    raise BucketError(f'“{t}”: format:* tags are set automatically')
                c.execute(text("INSERT INTO app.bucket_tags(bucket_id, path, tag, auto) VALUES (:b, '', :t, false)"),
                          {'b': bid, 't': t})
    except IntegrityError:
        if made is not None:
            shutil.rmtree(made, ignore_errors=True)
        raise BucketError(f'you already have a bucket called “{name}”: pick another name', 409) from None
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
            raise BucketError('mode must be stream or local')
        if r.link != 's3':
            raise BucketError('only an S3 link has a load mode: managed and local buckets are always local')
        with db.begin() as c:
            c.execute(text('UPDATE app.buckets SET mode = :m, updated_at = now() WHERE id = :b'),
                      {'m': mode, 'b': r.id})
        if mode == 'local':
            job_id = sync(user, bid)['job_id']
    if name is not None:
        name = _clean_name(name)
        try:
            with db.begin() as c:
                c.execute(text('UPDATE app.buckets SET name = :n, updated_at = now() WHERE id = :b'),
                          {'n': name, 'b': r.id})
        except IntegrityError:
            raise BucketError(f'a bucket called “{name}” already exists: pick another name', 409) from None
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
        c.execute(text('UPDATE app.buckets SET local_root = :p WHERE id = :b'), {'p': str(root), 'b': r.id})
    return str(root)


def sync(user, bid):
    """(Re-)load an S3 link (mode local) into its local folder: every object is downloaded, replacing the local
    file of the same name; files only the local copy has are kept. A job on the dc lane (inline from the CLI)
    -> {job_id, result}."""
    r = _row(user, bid)
    _need(user)
    if r.link != 's3':
        raise BucketError('only an S3 link can be synced')
    if r.mode != 'local':
        raise BucketError('switch the bucket to “load into local” first (Edit › Load mode)')
    dest = _local_root(r)

    def fn(job=None):
        auto = getattr(r._r, 'video_auto', None)
        before = _videos_in(dest) if auto else None
        view = _s3(r)
        try:
            with cloudiff.Manifest(_manifest(r), '') as m:     # what S3 held: Compare with cloud reads it
                res = view.pull(dest, job.progress if job is not None else None,
                                (lambda: job.cancelled.is_set()) if job is not None else None, listed=m.add)
                if job is not None and job.cancelled.is_set():
                    raise _Stopped
        except _Stopped:
            pass
        finally:
            view.close()
        if job is None or not job.cancelled.is_set():
            with db.begin() as c:
                c.execute(text('UPDATE app.buckets SET synced_at = now(), updated_at = now() WHERE id = :b'),
                          {'b': r.id})
                c.execute(text("DELETE FROM app.bucket_s3_local WHERE bucket_id = :b AND path <> ''"), {'b': r.id})
            _set_local(r.id, '', state='fresh', objects=res['total'], bytes=res['size'], diff=None, fetched=True,
                       checked=True)                     # streamed again later: the whole copy reads as downloaded
            s3dir.invalidate(r.id)
            _revalidate(_row(user, bid))
            if auto:
                res['frames'] = _auto_frames(user, _row(user, bid), sorted(_videos_in(dest) - before))
        return res

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        return {'job_id': None, 'result': fn()}
    return {'job_id': jobs.submit(f'bk-sync:{r.id}', f'load {r.name} from S3', fn, lane=LANE,
                                  public_result=True, owner=user['id']).id, 'result': None}


def convert_vectors(user, bid, path='', to='parquet'):
    """Convert the embeddings folder `path` (npy + meta.csv, embeddings.parquet or embeddings.csv) into layout `to`
    (labeling/writers/vector.py), in a new sibling folder <name>-<to>. A job on the dc lane -> {job_id, path}."""
    from ..labeling.writers import vector
    r = _row(user, bid)
    _need(user)
    rel = store.clean_rel(path)
    r = _write_view(r, sources=(rel,))                  # the new sibling folder lands next to it
    src = _abs(r, rel)
    if not os.path.isdir(src):
        raise BucketError(f'{rel or "/"} is not a folder')
    if to not in vector.WRITERS:
        raise BucketError(f'unknown layout {to!r} (one of {", ".join(vector.WRITERS)})')
    frm = vector.detect(src)
    if frm is None:
        raise BucketError('not an embeddings folder: meta.csv + .npy files, embeddings.parquet or embeddings.csv')
    if frm == to:
        raise BucketError(f'the folder is already {vector.LAYOUTS[to]["label"]}')
    base = os.path.basename(src.rstrip(os.sep)) if rel else _slug(r.name)
    dest = store._unique(os.path.join(os.path.dirname(src) if rel else src, f'{base}-{to}'))
    out = store.join(store.posix_parent(rel), os.path.basename(dest)) if rel else os.path.basename(dest)

    def fn(job=None):
        try:
            res = vector.convert(src, dest, to, (lambda n: job.progress(done=n)) if job is not None else None)
        except ValueError as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise BucketError(str(e)) from None
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        _revalidate(_row(user, bid))
        return {**res, 'path': out}

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is None:
        return {'job_id': None, 'path': out, 'result': fn()}
    return {'job_id': jobs.submit(f'bk-convert:{r.id}', f'convert {base} to {vector.LAYOUTS[to]["label"]}', fn,
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
    except BucketError:
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
                    raise BucketError(f'no file {rel}', 404)
                isdir = what == 'dir'
            else:
                a = _abs(r, rel)
                isdir = os.path.isdir(a)
                if not isdir and not os.path.isfile(a):
                    raise BucketError(f'no file {rel or "/"}', 404)
            if isdir:
                if view is not None:
                    names = [e['name'] for e in view.list(rel, limit=5000)['entries'] if e['type'] == 'file']
                else:
                    with os.scandir(_abs(r, rel)) as it:
                        names = sorted(e.name for e in it if e.is_file() and store.visible(e.name))
                found = [store.join(rel, n) for n in names if store.kind_of(n) == 'video']
                if not found:
                    raise BucketError(f'{rel or "/"} holds no video (mp4, mov, avi, mkv, webm, …)')
                out += found
            elif store.kind_of(rel.rsplit('/', 1)[-1]) != 'video':
                raise BucketError(f'{rel} is not a video (mp4, mov, avi, mkv, webm, …)')
            else:
                out.append(rel)
    finally:
        if view is not None:
            view.close()
    out = list(dict.fromkeys(out))
    if not out:
        raise BucketError('pick a video, or a folder that holds videos')
    if len(out) > FRAMES_MAX_VIDEOS:
        raise BucketError(f'{len(out)} videos: one extraction takes at most {FRAMES_MAX_VIDEOS}')
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
        raise BucketError(f'{rel} is not a video (mp4, mov, avi, mkv, webm, …)')
    if r.kind == 's3':
        view = _s3(r)
        try:
            size = view.size(rel)
        finally:
            view.close()
        if size is None:
            raise BucketError(f'no file {rel}', 404)
        return {'path': rel, 'size': size, 'streamed': True}
    if not os.path.isfile(_abs(r, rel)):
        raise BucketError(f'no file {rel}', 404)
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
            raise BucketError('the output folder name needs 1 to 200 characters, no / or \\, not starting with a dot')
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
    One bucket-lane job -> {job_id, bucket, path, estimate, result}."""
    r = _row(user, bid)
    _need(user)
    o = video.options(opts)
    if target not in ('inplace', 'copy'):
        raise BucketError('target must be inplace or copy')
    if target == 'inplace':
        if r.kind == 's3':
            raise BucketError(f'bucket “{r.name}” is streamed from S3 and read-only: extract into a new bucket '
                          '(target copy), or switch it to “load into local”')
        _need_write(r)
    rels = _video_rels(r, paths)
    est = _estimate(r, rels, o)
    if est['frames'] is not None and not est['approx'] and est['frames'] >= o['max_frames'] and \
            o['max_frames'] == video.MAX_FRAMES:
        raise BucketError(f'these settings take ≈{est["frames"]:,} frames, the limit is {video.MAX_FRAMES:,}: pick a '
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
                raise BucketError('no frame matched these settings: check the range and the mode')
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
            c.execute(text('UPDATE app.buckets SET updated_at = now() WHERE id = :b'), {'b': dest.id})
        return {'bucket': dest.id, 'path': path, 'frames': done[0], 'bytes': nbytes, 'videos': per,
                'layout': o['layout'], 'format': o['format'], 'describe': video.describe(o),
                'capped': capped,
                **lay, 'retagged': _revalidate(_row(user, dest.id))}

    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    base = {'bucket': dest.id, 'path': out_rel, 'estimate': {k: est[k] for k in ('frames', 'approx', 'bytes')}}
    if jobs is None:
        return {'job_id': None, **base, 'result': fn()}
    job = jobs.submit(f'bk-frames:{dest.id}:{out_rel}', label, fn, lane=LANE, public_result=True, owner=user['id'])
    return {'job_id': job.id, **base, 'result': None}


def set_video_auto(user, bid, opts=None):
    """Save `opts` (video.options) as the bucket's auto-extract settings: every video that lands in it later (upload,
    import, S3 load) is cut into <video>-frames by itself. opts None turns it off. -> {video_auto}."""
    r = _row(user, bid)
    _need(user)
    if r.kind == 's3':
        raise BucketError(f'bucket “{r.name}” is streamed from S3 and read-only: nothing new lands in it')
    o = None
    if opts is not None:
        o = video.options(opts)
        o['split'] = list(o['split'])
    with db.begin() as c:
        c.execute(text('UPDATE app.buckets SET video_auto = :v, updated_at = now() WHERE id = :b')
                  .bindparams(bindparam('v', type_=JSONB)), {'v': o, 'b': r.id})
    return {'video_auto': o}


def _auto_frames(user, r, rels):
    """Start the auto-extract jobs (one per new video, so each gets its own <video>-frames) -> {jobs, errors} or None."""
    auto = getattr(r._r, 'video_auto', None)
    if not auto or not rels or not _writable(r):
        return None
    try:
        o = video.options(auto)
    except BucketError as e:
        return {'jobs': [], 'errors': [{'path': '', 'error': f'the saved auto-extract settings: {e}'}]}
    jobs, errors = [], []
    for rel in rels[:FRAMES_MAX_VIDEOS]:
        try:
            est = _estimate(r, [rel], o)
            res = _frames_job(user, r, [rel], o, est, _frames_name(r, [rel], None))
            jobs.append({'path': rel, 'job_id': res['job_id'], 'out': res['path']})
        except BucketError as e:
            errors.append({'path': rel, 'error': e.message})
    return {'jobs': jobs, 'errors': errors}


# ---------------------------------------------------------------------------------------------- web sources
def _source_conn(user, src, connection_id):
    """The decrypted token connection for `src`: the one picked, else the user's only / first one of that kind;
    None when the source needs none (BucketError when it is required and there is none)."""
    from ..image.connections import TOKEN_KINDS
    if not src.needs:
        return None
    reg = _datasets()
    if connection_id:
        try:
            c = reg.connection(connection_id, owner_id=user['id'])
        except KeyError:
            raise BucketError('pick one of your own connections') from None
        if c.get('kind') != src.needs:
            raise BucketError(f'that connection is not a {src.label} token')
        return c
    mine = [c for c in reg.connections(user['id']) if c.get('kind') == src.needs and src.needs in TOKEN_KINDS]
    if mine:
        return reg.connection(mine[0]['id'], owner_id=user['id'])
    if src.token == 'required':
        raise BucketError(f'{src.label} needs your token: add it in Settings › Connections ({src.label}), then try again')
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
    of the same name are replaced, then the format is detected. A bucket-lane job keyed bk-import:<bid> (inline from
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
            raise BucketError(f'{rel} is not a folder')
    else:
        base = _clean_name(name or pref['name'])
        for i in range(1, 50):
            try:
                b = create_bucket(user, base if i == 1 else f'{base}-{i}', 'managed')
                break
            except BucketError as e:
                if e.status != 409 or name:
                    raise
        else:
            raise BucketError(f'pick a name: “{base}” and its numbered variants are taken', 409)
        r, rel, created = _row(user, b['id']), '', True
        target = r.root
    meta = {'connection_id': (conn or {}).get('id'), 'opts': {**o, **flags}, 'path': rel}
    with db.begin() as c:
        c.execute(text("""UPDATE app.buckets SET source_kind = :k, source_ref = :ref, source_meta = :m,
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
                    raise BucketError(f'{os.path.relpath(f, tmp)} is a folder in the bucket already', 409)
                os.replace(f, dest)
                res['files'] += 1
            prog(stage='detecting the format')
            done_meta = {**meta, **(got.get('meta') or {}), 'bytes': res['bytes']}
            with db.begin() as c:
                c.execute(text("""UPDATE app.buckets SET imported_at = now(), updated_at = now(), source_meta = :m
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
    return {'job_id': jobs.submit(f'bk-import:{r.id}', f'import {pref["ref"]} from {src.label}', fn, lane=LANE,
                                  public_result=True, owner=user['id']).id, 'bucket': r.id, 'result': None}


def reimport(user, bid):
    """Run the bucket's last web import again into the same folder (newer files replace older ones)."""
    r = _row(user, bid)
    if not getattr(r._r, 'source_kind', None):
        raise BucketError(f'bucket “{r.name}” was not imported from the web')
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
            raise BucketError(f'cannot read bucket “{r.name}” from S3: {e}', 502) from None
        finally:
            view.close()
        if what is None:
            raise BucketError(f'nothing at {rel or "/"}', 404)
        return r, rel, what == 'file'
    return r, rel, os.path.isfile(_abs(r, rel))


def _export_items(user, r, rel):
    """The files of folder (or file) `rel`, paths below the bucket root (store.Item), local or S3 alike: what the
    zip and export_to both read. .env files only for bucket.manage; the S3 client closes when the walk ends."""
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
    """{name, kind} of what GET export would zip, or the BucketError it would fail with."""
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
    A job on the dc lane bk-export:<bid> (inline from the CLI) -> {job_id, result: {files, bytes, uri, created}}."""
    _need(user)
    r, rel, _ = _export_check(user, bid, path)
    t = dict(target or {})
    kind = t.get('kind')
    if kind not in EXPORT_TARGETS:
        raise BucketError(f'export to one of {", ".join(EXPORT_TARGETS)} (not {kind!r})')
    if not t.get('connection_id'):
        raise BucketError('pick one of your connections for the export')
    conns = _datasets().scoped(user['id'])              # the caller's own connections only
    try:
        conns.connection(t['connection_id'])
    except KeyError:
        raise BucketError('pick one of your own connections for the export') from None
    tgt = {'kind': kind, 'connection_id': t['connection_id']}
    if kind == 's3':
        tgt.update(bucket=str(t.get('bucket') or '').strip(), prefix=t.get('prefix') or '',
                   create_bucket=bool(create_bucket))
        if not tgt['bucket']:
            raise BucketError('pick the S3 bucket to export into')
        from ..image.sources import _norm_prefix
        src = _norm_prefix('/'.join(p for p in ((r.prefix or '').strip('/'), rel) if p))     # '' = the whole bucket
        dst = _norm_prefix('/'.join(p for p in (tgt['prefix'].strip('/'), rel) if p))
        if r.kind == 's3' and r.connection_id == tgt['connection_id'] and r.s3_bucket == tgt['bucket'] and \
                (dst.startswith(src) or src.startswith(dst)):     # the listing would meet its own copies
            raise BucketError('the export target overlaps the folder being exported: pick another prefix')
    else:
        tgt['path'] = t.get('path') or t.get('prefix') or None
    ctx = _conf['ctx']
    jobs = getattr(ctx, 'jobs', None) if ctx is not None else None
    if jobs is not None and jobs.busy(f'bk-export:{r.id}'):     # submit would hand back that job, not this target
        raise BucketError(f'bucket “{r.name}” is already being exported: wait for that job (or cancel it)', 409)
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
            raise BucketError(str(e)) from None
        raise BucketError(f'cannot use the export target: {type(e).__name__}: {e}') from None

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
            raise BucketError(str(e)) from None
        if job is not None:
            job.progress(n, n, 'done')
        return {**res, 'created': bool(getattr(sink, 'created', False)), 'cancelled': stop()}

    if jobs is None:
        return {'job_id': None, 'result': fn()}
    return {'job_id': jobs.submit(f'bk-export:{r.id}', f'export {r.name} to {sink.uri}', fn, lane=LANE,
                                  public_result=True, owner=user['id']).id, 'result': None}


def delete_bucket(user, bid, delete_files=False):
    """Forget a bucket (tags, cache and undo rows go with it). delete_files: also remove a MANAGED bucket's
    folder; a linked folder / S3 prefix is never deleted (it is not ours)."""
    r = _row(user, bid)
    _need(user)
    if delete_files and r.kind != 'managed' and not (r.link == 's3' and r.local_root):
        raise BucketError('only a managed bucket’s files can be deleted: a link is only forgotten')
    with db.begin() as c:
        undo_ids = c.execute(text('SELECT id FROM app.bucket_undo WHERE bucket_id = :b'), {'b': r.id}).scalars().all()
        c.execute(text('DELETE FROM app.buckets WHERE id = :b'), {'b': r.id})
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
def ls(user, bid, path='', offset=0, limit=500, sort='name', q='', retry=False, refresh=False):
    """One page of folder `path`. A streamed S3 link answers with what its listing holds after LS_WAIT s
    (partial: true while it runs; the page asks again), every entry carries `local` (its downloaded copy's state)
    and `here` is the downloaded folder holding `path`; when S3 cannot be reached the downloaded folders are listed
    from the local copy (offline: true). A downloaded folder (not being downloaded) is listed from its copy and is
    writable: it is edited in place. retry: ask S3 again at once after it was found unreachable; refresh: list
    the folder from S3 again (the Reload button) instead of the listing kept for a minute. `location`: where the
    folder is read from (s3://… or a local path)."""
    r = _row(user, bid)
    rel = store.clean_rel(path)
    offline = False
    if refresh and r.kind == 's3':
        s3dir.invalidate(r.id, rel)
    if r.kind == 's3':
        out = _s3_ls(user, r, rel, offset, limit, sort, q, retry)
        offline = bool(out.get('offline'))
    else:
        out = store.list_local(r.root, rel, _allowed(), offset, limit, sort, q)
    try:
        res = _cached(r, rel, user=user, wait=False, refresh=not offline)  # S3: never a full listing in the request
        out['format'] = res['format'] if res else None
        if res and not rel:
            vtags.apply_format(r.id, res['format'])
    except BucketError:
        out['format'] = None
    out['tags'] = vtags.get(r.id, rel)
    edit = out.pop('editable', False)
    out['writable'] = edit or _writable(r)
    out['location'] = os.path.join(r.local_root, rel) if edit and rel else r.local_root if edit else _location(r, rel)
    if r.link == 's3':
        out['s3_url'] = _s3_url(r, rel)
    return out



def image_files(user, bid, path='', limit=2000):
    """The images in folder `path` of a bucket and its subfolders (hidden ones left out), sorted, at most `limit`
    -> [absolute path]. Local and managed buckets, and the downloaded folders of an S3 link; a part of an S3 link
    that is only streamed raises BucketError: download it first. (The visualizer's Augment › Coverage reads its target.)"""
    r = _row(user, bid)
    rel = store.clean_rel(path)
    if r.kind == 's3':
        local = os.path.join(r.local_root or '', rel) if r.local_root else ''
        if not local or not os.path.isdir(local):
            raise BucketError(f'{rel or "this bucket"} of “{r.name}” is streamed from S3: download that folder first (Download to local)')
        r = _LocalView(r)
    base = _abs(r, rel)
    out = []
    for dp, dn, fn in os.walk(base):
        dn[:] = sorted(d for d in dn if not d.startswith('.'))
        out += [os.path.join(dp, f) for f in sorted(fn) if not f.startswith('.') and f.lower().endswith(store.IMAGE_EXTS)]
        if len(out) >= limit:
            break
    return out[:limit]

# ---------------------------------------------------------------------------------------------- S3 local copies
def _jobs():
    ctx = _conf['ctx']
    return getattr(ctx, 'jobs', None) if ctx is not None else None


JOB_WAIT_MAX = 60


def job_status(user, job_id, wait_s=0):
    """One background job the user started (import, export, frames, S3 download / sync / compare, label with model)
    -> {id, label, state: queued | running | done | error | cancelled, stage, pct, eta_s, elapsed_s, error?, result?}.
    wait_s (<= JOB_WAIT_MAX) waits that long for it to finish first. Jobs of other users do not exist here."""
    _need(user)
    jobs = _jobs()
    try:
        j = jobs.get(int(job_id)) if jobs is not None else None
    except (TypeError, ValueError):
        raise BucketError(f'job_id must be the number a tool returned, not {job_id!r}') from None
    if j is None or (j.owner != user.get('id') and not user.get('is_admin')):
        raise BucketError(f'no job {job_id} of yours (finished jobs are kept for a while, then forgotten)')
    if wait_s and j.status in ('queued', 'running'):
        j.wait(max(0.0, min(float(wait_s), JOB_WAIT_MAX)))
    d = j.to_dict()
    state = 'cancelled' if d['cancelled'] and d['status'] != 'done' else d['status']
    out = {'id': d['id'], 'label': d['label'], 'state': state, 'stage': d['stage'], 'pct': d['pct'],
           'eta_s': d['eta'], 'elapsed_s': d['elapsed']}
    if d.get('error'):
        out['error'] = str(d['error'])[:500]
    if 'result' in d and isinstance(d['result'], (dict, list, str, int, float)):
        out['result'] = d['result']
    return out


def _busy(key):
    jobs = _jobs()
    return bool(jobs is not None and jobs.busy(key))


def _local_rows(bid):
    """{path: {path, state, objects, bytes, diff, fetched_at, checked_at}} of the downloaded folders of an S3 link
    ('' = all). A download that is no longer running (cancelled, a restart) reads as outdated."""
    with db.connect() as c:
        rows = c.execute(text("""SELECT path, state, objects, bytes, diff, fetched_at, checked_at FROM app.bucket_s3_local
                                 WHERE bucket_id = :b"""), {'b': bid}).all()
    fetching = _busy(f'bk-fetch:{bid}:') if rows else False
    return {x.path: {'path': x.path, 'state': 'outdated' if x.state == 'fetching' and not fetching else x.state,
                     'objects': x.objects, 'bytes': x.bytes, 'diff': x.diff, 'fetched_at': _epoch(x.fetched_at),
                     'checked_at': _epoch(x.checked_at)} for x in rows}


def _cover(rows, rel):
    """The downloaded folder (or file) holding `rel`: rel itself or its nearest downloaded parent; None."""
    p = rel
    while True:
        if p in rows:
            return rows[p]
        if not p:
            return None
        p = store.posix_parent(p)


def _holds(rows, rel):
    """Whether a folder below `rel` is downloaded (rel itself is not)."""
    pre = rel + '/' if rel else ''
    return any(p.startswith(pre) and p != rel for p in rows)


def _entry_state(rows, e, root):
    """`local` of one S3 entry: fresh | outdated | new (S3 has it, the copy not) | fetching | partial (a folder with
    downloaded sub-folders) | None (stream only)."""
    c = _cover(rows, e['path'])
    if c is None:
        return 'partial' if e['type'] == 'dir' and _holds(rows, e['path']) else None
    if c['state'] in ('fetching', 'error'):
        return c['state']
    p = root / e['path']
    if e['type'] == 'dir':
        return c['state'] if p.is_dir() else 'new'
    if store._unchanged(p, e['size'], e['mtime']):
        return 'fresh'
    return 'outdated' if p.exists() else 'new'


def _local_entries(r, rel):
    """The entries of folder `rel` of the local copy (one scandir, hidden names left out)."""
    d = os.path.join(r.local_root, rel) if rel else r.local_root
    out = []
    try:
        it = os.scandir(d)
    except OSError:
        return out
    with it:
        for e in it:
            isdir = e.is_dir(follow_symlinks=False)
            if not store.visible(e.name, isdir) or not (isdir or e.is_file(follow_symlinks=False)):
                continue
            st = e.stat(follow_symlinks=False)
            out.append({'name': e.name, 'path': store.join(rel, e.name), 'type': 'dir' if isdir else 'file',
                        'size': 0 if isdir else st.st_size, 'mtime': st.st_mtime,
                        **({} if isdir else {'kind': store.kind_of(e.name, rel)})})
    return out


def _s3_ls(user, r, rel, offset, limit, sort, q, retry):
    rows = _local_rows(r.id)
    cover = _cover(rows, rel)
    if (cover is not None and cover['state'] != 'fetching' and r.local_root and not _busy(f'bk-fetch:{r.id}:')
            and os.path.isdir(os.path.join(r.local_root, rel) if rel else r.local_root)):
        out = store.list_local(r.local_root, rel, _allowed(), offset, limit, sort, q)   # edited in place
        st = 'error' if cover['state'] == 'error' else 'fresh'
        out['entries'] = [{**e, 'local': st} for e in out['entries']]
        out.update(partial=False, loaded=out['total'], here=cover, local_folders=len(rows), editable=True)
        _check_soon(user, r, rows)
        return out
    try:
        view = _s3(r, shared=True)
        view.online(retry)
        x = s3dir.get((r.id, rel, r.connection_id, r.s3_bucket, r.prefix), lambda: view.dir_pages(rel))
        x.wait(LS_WAIT)
        dirs, files, done, err = x.snapshot()
        if err is not None and not (dirs or files):
            if isinstance(err, store.Offline):
                raise err
            raise BucketError(f'cannot list S3: {err}', 502)
    except store.Offline as e:
        if rows and r.local_root:
            return _offline_ls(r, rel, rows, offset, limit, sort, q, e)
        raise
    if cover is not None and done and err is None and r.local_root:     # only a complete listing can tell "gone"
        names = {e['name'] for e in dirs} | {e['name'] for e in files}
        for e in _local_entries(r, rel):
            if e['name'] not in names:
                (dirs if e['type'] == 'dir' else files).append({**e, 'local': 'gone'})
    total, page = store.page_rows(dirs, files, offset, limit, sort, q)
    root = Path(r.local_root) if r.local_root else None
    page = [e if 'local' in e else {**e, 'local': _entry_state(rows, e, root) if root else None} for e in page]
    out = {'path': rel, 'total': total, 'entries': page, 'partial': not done, 'loaded': len(dirs) + len(files),
           'here': cover, 'local_folders': len(rows)}
    if err is not None:
        out['error'] = f'the S3 listing stopped early: {err}'
    _check_soon(user, r, rows)
    return out


def _offline_ls(r, rel, rows, offset, limit, sort, q, e):
    """ls from the local copy while S3 is unreachable: a downloaded folder lists its files; the root (or a folder
    above downloaded ones) only the folders that lead to them. Elsewhere the Offline error stands."""
    cover = _cover(rows, rel)
    if cover is not None:
        if not os.path.isdir(os.path.join(r.local_root, rel) if rel else r.local_root):
            raise e
        ents = [{**x, 'local': cover['state']} for x in _local_entries(r, rel)]
        dirs, files = [x for x in ents if x['type'] == 'dir'], [x for x in ents if x['type'] == 'file']
    else:
        pre = rel + '/' if rel else ''
        kids = sorted({p[len(pre):].split('/')[0] for p in rows if p.startswith(pre) and p != rel})
        if not kids:
            raise e
        have = {x['name']: x for x in _local_entries(r, rel)}
        dirs = [{**have[k], 'local': _cover(rows, store.join(rel, k))['state'] if _cover(rows, store.join(rel, k))
                 else 'partial'} for k in kids if k in have and have[k]['type'] == 'dir']
        files = [{**have[k], 'local': rows[store.join(rel, k)]['state']} for k in kids
                 if k in have and have[k]['type'] == 'file' and store.join(rel, k) in rows]
    total, page = store.page_rows(dirs, files, offset, limit, sort, q)
    return {'path': rel, 'total': total, 'entries': page, 'partial': False, 'loaded': total, 'here': cover,
            'offline': True, 'error': e.message, 'local_folders': len(rows)}


def folder_state(user, bid, path=''):
    """What the assistant needs about the open folder: {name, kind, mode, location, s3_url, writable, local,
    downloaded_in, editable}. local: the folder's downloaded state on a streamed S3 link (fresh | outdated |
    error | fetching | partial | None); editable: changes here are allowed (they land in the local copy)."""
    r = _row(user, bid)
    rel = store.clean_rel(path)
    out = {'name': r.name, 'kind': r.link, 'mode': r.mode if r.link == 's3' else None,
           'location': _location(r, rel), 's3_url': _s3_url(r, rel) if r.link == 's3' else None,
           'writable': _writable(r), 'local': None, 'downloaded_in': None, 'editable': _writable(r)}
    if _streamed(r) and r.local_root:
        rows = _local_rows(r.id)
        c = _cover(rows, rel)
        busy = _busy(f'bk-fetch:{r.id}:')
        if c is not None:
            out.update(local='fetching' if busy else c['state'], downloaded_in=c['path'], editable=not busy)
            out['location'] = os.path.join(r.local_root, rel) if rel else r.local_root
        elif _holds(rows, rel):
            out['local'] = 'partial'
        out['downloaded'] = sorted(rows)[:20]
    return out


def _local_file(r, rel):
    """(path of the downloaded copy of file `rel`, whether to read it rather than S3) for a streamed S3 link, else
    (None, False). A downloaded folder is edited in place, so its copy is what the user sees (outdated or not)."""
    if r.kind != 's3' or not r.local_root:
        return None, False
    c = _cover(_local_rows(r.id), rel)
    if c is None or c['state'] == 'fetching':
        return None, False
    p = os.path.join(r.local_root, rel)
    return (p, True) if os.path.isfile(p) else (None, False)


def _set_local(bid, path, **cols):
    """Upsert the bucket_s3_local row of (bid, path) with `cols` (state, objects, bytes, diff, fetched, checked)."""
    vals = {'b': bid, 'p': path, 'state': cols.get('state', 'fetching'), 'objects': cols.get('objects', 0),
            'bytes': cols.get('bytes', 0), 'diff': cols.get('diff')}
    stamp = ', '.join(f'{k}_at = now()' for k in ('fetched', 'checked') if cols.get(k))
    sets = ['state = EXCLUDED.state'] + [f'{k} = EXCLUDED.{k}' for k in ('objects', 'bytes', 'diff') if k in cols]
    with db.begin() as c:
        c.execute(text(f"""INSERT INTO app.bucket_s3_local(bucket_id, path, state, objects, bytes, diff
                           {''.join(f', {k}_at' for k in ('fetched', 'checked') if cols.get(k))})
                           VALUES (:b, :p, :state, :objects, :bytes, :diff
                           {''.join(', now()' for k in ('fetched', 'checked') if cols.get(k))})
                           ON CONFLICT (bucket_id, path) DO UPDATE SET {', '.join(sets)}
                           {(', ' + stamp) if stamp else ''}""").bindparams(bindparam('diff', type_=JSONB)), vals)


def _need_stream(user, bid):
    r = _row(user, bid)
    _need(user)
    if r.link != 's3':
        raise BucketError('only a streamed S3 link downloads folders: this bucket is local already')
    if r.kind != 's3':
        raise BucketError(f'bucket “{r.name}” is loaded into local as a whole: use Re-sync from S3')
    return r


def _top_paths(paths):
    """The cleaned paths without those inside another one of them."""
    rels = sorted({store.clean_rel(p) for p in paths or ()})
    out = []
    for p in rels:
        if not any(p == o or p.startswith(o + '/') or not o for o in out):
            out.append(p)
    return out


def fetch_local(user, bid, paths, mirror=False):
    """Download folders (or files) `paths` of a streamed S3 link into its local copy (read-only; the bucket keeps
    streaming the rest). Downloading again updates: new and changed objects are fetched, unchanged ones skipped;
    mirror also removes local files S3 no longer has. A path inside an already downloaded folder refreshes only
    that part; a folder above downloaded ones takes them over. A bucket-lane job bk-fetch:<bid>:<hash> -> {job_id,
    paths, result}."""
    r = _need_stream(user, bid)
    rels = _top_paths(paths)
    if not rels:
        raise BucketError('pick the folders to download')
    dest = _local_root(r)
    rows = _local_rows(r.id)
    own = [p for p in rels if _cover(rows, p) is None or _cover(rows, p)['path'] == p]   # rows this job writes
    for p in own:
        _set_local(r.id, p, state='fetching')
    s3dir.invalidate(r.id)

    def fn(job=None):
        cancelled = (lambda: job.cancelled.is_set()) if job is not None else (lambda: False)
        view = _s3(r)
        out = {'downloaded': 0, 'skipped': 0, 'bytes': 0, 'removed': 0, 'failed': [], 'paths': rels}
        try:
            for i, p in enumerate(rels):
                if cancelled():
                    break

                def prog(d=None, t=None, stage=None, extra=None, p=p, i=i):
                    if job is not None:
                        job.progress(d, t, f'{p or "all"} ({i + 1}/{len(rels)}): {stage}' if stage else None, extra)
                try:
                    with cloudiff.Manifest(_manifest(r), p) as m:     # what S3 held: Compare with cloud reads it
                        if p and view.what(p) == 'file':
                            res = _fetch_file(view, dest, p, m.add)
                        else:
                            res = view.pull(dest, prog, cancelled, rel=p, mirror=mirror, listed=m.add)
                        if cancelled():
                            raise _Stopped
                except _Stopped:
                    pass
                for k in ('downloaded', 'skipped', 'bytes', 'removed'):
                    out[k] += res[k]
                out['failed'] += res['failed']
                if p in own:
                    _set_local(r.id, p, state='outdated' if cancelled() or res['failed'] else 'fresh',
                               objects=res['total'], bytes=res['size'], diff=None, fetched=True, checked=True)
                    with db.begin() as c:                      # a folder above downloaded ones takes them over
                        c.execute(text("""DELETE FROM app.bucket_s3_local WHERE bucket_id = :b AND path <> :p
                                          AND (:p = '' OR path LIKE :pre)"""),
                                  {'b': r.id, 'p': p, 'pre': p.replace('%', r'\%').replace('_', r'\_') + '/%'})
        except BaseException as e:
            for p in own:
                _set_local(r.id, p, state='error', diff={'error': str(e)})
            raise
        finally:
            view.close()
            s3dir.invalidate(r.id)
        out['failed'] = out['failed'][:50]
        out['cancelled'] = cancelled()
        return out

    jobs = _jobs()
    if jobs is None:
        return {'job_id': None, 'paths': rels, 'result': fn()}
    key = f'bk-fetch:{r.id}:{uuid.uuid4().hex[:8]}'
    return {'job_id': jobs.submit(key, f'download {", ".join(p or "all" for p in rels[:3])}'
                                       f'{"…" if len(rels) > 3 else ""} of {r.name}', fn, lane=LANE,
                                  public_result=True, owner=user['id']).id, 'paths': rels, 'result': None}


class _Stopped(Exception):
    """A cancelled download: its manifest rows are dropped (what it listed is not the whole folder)."""


def _manifest(r):
    return cloudiff.manifest_path(store.data_root(_cfg()), r.id)


def _fetch_file(view, dest, rel, listed=None):
    """Download one object as a pull would (mtime = its LastModified, so it reads as unchanged later)."""
    h = view.client.head_object(Bucket=view.bucket, Key=view.key(rel))
    size, lm = int(h.get('ContentLength') or 0), h.get('LastModified')
    if listed is not None:
        listed(rel, size, lm.timestamp() if lm is not None else None)
    dst = os.path.join(dest, rel)
    if store._unchanged(dst, size, lm.timestamp() if lm is not None else None):
        return {'downloaded': 0, 'skipped': 1, 'bytes': 0, 'size': size, 'removed': 0, 'failed': [], 'total': 1}
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    view.download(rel, dst)
    if lm is not None:
        os.utime(dst, (lm.timestamp(), lm.timestamp()))
    return {'downloaded': 1, 'skipped': 0, 'bytes': size, 'size': size, 'removed': 0, 'failed': [], 'total': 1}


def _compare(view, dest, rel, cancelled):
    """{new, changed, removed, sample} of the local copy of folder (or file) `rel` against S3."""
    pre = view.base + (rel + '/' if rel else '')
    seen, diff, sample = set(), {'new': 0, 'changed': 0, 'removed': 0}, []
    with closing(s3list.objects(view.client, view.bucket, pre, cancelled=cancelled)) as objs:
        for o in objs:
            sub = o['Key'][len(pre):]
            if not store._shown(sub):
                continue
            k = store.join(rel, sub)
            seen.add(k)
            lm = o.get('LastModified')
            p = Path(dest) / k
            if not store._unchanged(p, int(o.get('Size') or 0), lm.timestamp() if lm is not None else None):
                kind = 'changed' if p.exists() else 'new'
                diff[kind] += 1
                if len(sample) < 20:
                    sample.append({'path': k, 'what': kind})
    top = Path(dest) / rel if rel else Path(dest)
    if top.is_dir() and not (cancelled and cancelled()):
        for k, _, _ in store.walk_files(top, rel):
            if k not in seen:
                diff['removed'] += 1
                if len(sample) < 20:
                    sample.append({'path': k, 'what': 'removed'})
    return {**diff, 'sample': sample}


def _check_soon(user, r, rows):
    """Queue the comparison of the downloaded folders with S3 when the last one is older than CHECK_TTL (one
    check per bucket at a time)."""
    now = time.time()
    if (not rows or _quiet.get(('check', r.id), 0) > time.monotonic() or _busy(f'bk-check:{r.id}')
            or _busy(f'bk-fetch:{r.id}:')):
        return None
    if all(x['checked_at'] and now - x['checked_at'] < CHECK_TTL for x in rows.values() if x['state'] != 'fetching'):
        return None
    return _check_job(user, r)


def _check_job(user, r):
    def fn(job=None):
        cancelled = (lambda: job.cancelled.is_set()) if job is not None else None
        out = {}
        try:
            view = _s3(r)
            try:
                for p, row in _local_rows(r.id).items():
                    if row['state'] == 'fetching' or (cancelled and cancelled()):
                        continue
                    if job is not None:
                        job.progress(stage=f'comparing {p or "all"} with S3')
                    d = _compare(view, r.local_root, p, cancelled)
                    if cancelled and cancelled():
                        break
                    state = 'outdated' if d['new'] or d['changed'] or d['removed'] else 'fresh'
                    _set_local(r.id, p, state=state, diff=d, checked=True)
                    out[p] = {'state': state, **{k: d[k] for k in ('new', 'changed', 'removed')}}
            finally:
                view.close()
        except Exception:
            _quiet[('check', r.id)] = time.monotonic() + CHECK_TTL        # ls does not queue it again at once
            raise
        if cancelled and cancelled():
            _quiet[('check', r.id)] = time.monotonic() + CHECK_TTL
        else:
            _quiet.pop(('check', r.id), None)
        return {'folders': out}          # S3 itself did not change: its listings stay (badges are read per ls)

    jobs = _jobs()
    if jobs is None:
        fn()
        return None
    return jobs.submit(f'bk-check:{r.id}', f'check {r.name} against S3', fn, lane=LANE, public_result=True,
                       owner=(user or {}).get('id') or r.owner_id).id


def check_local(user, bid):
    """Compare every downloaded folder of a streamed S3 link with S3 now (new / changed / removed files; the
    folder turns outdated when any). A bucket-lane job bk-check:<bid> -> {job_id}."""
    r = _need_stream(user, bid)
    if not _local_rows(r.id):
        raise BucketError(f'no folder of “{r.name}” is downloaded')
    return {'job_id': _check_job(user, r)}


# ---------------------------------------------------------------- local copy vs. the cloud (bucket/cloudiff.py)
def _compare_targets(r, rel):
    """(dest, [(folder, end of its download)]) to compare for `rel` of an S3 link with a local copy."""
    if r.local_copy:                                  # the whole bucket loaded into local (mode local)
        return r.local_root, [(rel, _epoch(r.synced_at) or 0.0)]
    rows = _local_rows(r.id)
    c = _cover(rows, rel)
    picked = [c] if c is not None else [x for p, x in rows.items() if not rel or p.startswith(rel + '/')]
    if not picked:
        raise BucketError(f'nothing of “{rel or r.name}” is downloaded: there is no local copy to compare with S3')
    if any(x['state'] == 'fetching' or not x['fetched_at'] for x in picked):
        raise BucketError('a download of this folder is still running: compare when it is done', 409)
    return _local_root(r), [(rel if c is not None else x['path'], float(x['fetched_at'])) for x in picked]


def _compare_file(r, rel):
    return cloudiff.db_path(store.data_root(_cfg()), r.id, rel)


def compare_cloud(user, bid, path='', verify=False):
    """Compare the local copy of folder `path` of an S3 link with S3: every file added, deleted or edited here,
    and new, changed or removed in S3 since the download. verify: also compare the bytes (MD5 against the ETag).
    A bucket-lane job bk-compare:<bid>:<hash> -> {job_id, path}; compare_result reads it."""
    r = _row(user, bid)
    if r.link != 's3':
        raise BucketError('only an S3 link has a cloud copy to compare with')
    rel = store.clean_rel(path)
    dest, targets = _compare_targets(r, rel)
    out = _compare_file(r, rel)
    key = f'bk-compare:{r.id}:{out.stem}'
    if _busy(key):
        raise BucketError('this folder is being compared already', 409)

    def fn(job=None):
        cancelled = (lambda: job.cancelled.is_set()) if job is not None else None
        prog = (lambda n, stage: job.progress(n, None, stage)) if job is not None else None
        view = _s3(r)
        try:
            return {'path': rel, **cloudiff.run(view, dest, targets, out, verify=bool(verify), cancelled=cancelled,
                                                progress=prog, manifest=_manifest(r))}
        finally:
            view.close()

    jobs = _jobs()
    if jobs is None:
        return {'job_id': None, 'path': rel, 'result': fn()}
    return {'job_id': jobs.submit(key, f'compare {rel or r.name} with S3', fn, lane=LANE, public_result=True,
                                  owner=user['id']).id, 'path': rel, 'result': None}


def compare_result(user, bid, path='', status=None, q='', offset=0, limit=200):
    """The last comparison of `path` -> {ready, running, path, counts, total, finished_at, verify, complete,
    matched, offset, rows: [{path, status, local: {size, mtime}, cloud: {size, mtime}, verified?}]}; ready false
    when it was never compared. status: one status or several, comma-separated."""
    r = _row(user, bid)
    if r.link != 's3':
        raise BucketError('only an S3 link has a cloud copy to compare with')
    rel = store.clean_rel(path)
    out = _compare_file(r, rel)
    running = _busy(f'bk-compare:{r.id}:{out.stem}')
    if not out.exists():
        return {'ready': False, 'running': running, 'path': rel, 'labels': cloudiff.LABEL}
    sts = [s for s in (status.split(',') if isinstance(status, str) else status or ()) if s]
    bad = [s for s in sts if s not in cloudiff.RANK]
    if bad:
        raise BucketError(f'unknown status {bad[0]!r}: one of {", ".join(cloudiff.STATUSES)}')
    return {'ready': True, 'running': running, 'path': rel, 'labels': cloudiff.LABEL, **cloudiff.summary(out),
            **cloudiff.page(out, sts, str(q or '')[:200], offset, limit)}


def drop_local(user, bid, paths):
    """Remove the local copy of downloaded folders `paths` (or of every downloaded folder below them); S3 is not
    touched and they stream again. -> {removed: [paths]}"""
    r = _need_stream(user, bid)
    if _busy(f'bk-fetch:{r.id}:'):
        raise BucketError(f'a download into “{r.name}” is running: wait for it (or cancel it)', 409)
    rows = _local_rows(r.id)
    gone = []
    for p in _top_paths(paths):
        c = _cover(rows, p)
        if c is not None and c['path'] != p:
            raise BucketError(f'{p} is part of the downloaded folder {c["path"] or "(whole bucket)"}: remove that one')
        hit = [x for x in rows if x == p or not p or x.startswith(p + '/')]
        if not hit:
            raise BucketError(f'{p or "/"} is not downloaded')
        for x in hit:
            target = Path(os.path.realpath(os.path.join(r.local_root, x) if x else r.local_root))
            base = Path(os.path.realpath(r.local_root))
            for e in ((list(base.iterdir()) if base.is_dir() else []) if target == base
                      else [target] if base in target.parents else []):
                if e.is_dir() and not e.is_symlink():
                    shutil.rmtree(e, ignore_errors=True)
                else:
                    e.unlink(missing_ok=True)
            with db.begin() as c:
                c.execute(text('DELETE FROM app.bucket_s3_local WHERE bucket_id = :b AND path = :p'), {'b': r.id, 'p': x})
            cloudiff.forget(store.data_root(_cfg()), r.id, x)
            gone.append(x)
    s3dir.invalidate(r.id)
    return {'removed': gone}


def _file(r, path):
    rel = store.clean_rel(path)
    if not rel:
        raise BucketError('give a file path')
    return rel


def file_path(user, bid, path):
    """(absolute path, media type) of a local file, for streaming the raw bytes; for a streamed S3 link the
    downloaded copy when its folder is fresh, else None (read from S3)."""
    r = _row(user, bid)
    _need_secret(user, path)
    if r.kind == 's3':
        p, fresh = _local_file(r, _file(r, path))
        return (p, mimetypes.guess_type(p)[0] or 'application/octet-stream') if p and fresh else None
    p = _abs(r, _file(r, path))
    if not os.path.isfile(p):
        raise BucketError(f'{path} is not a file', 404 if not os.path.lexists(p) else 400)
    return p, mimetypes.guess_type(p)[0] or 'application/octet-stream'


def read_file(user, bid, path, w=None, t=None):
    """(bytes, media type): the raw file, or with `w` a JPEG thumbnail at most w px (cached on disk); for a video
    the thumbnail is its frame at `t` seconds (default: a poster frame 10 % in)."""
    r = _row(user, bid)
    rel = _file(r, path)
    _need_secret(user, rel)
    is_video = store.kind_of(rel.rsplit('/', 1)[-1]) == 'video'
    if r.kind == 's3':
        local, fresh = _local_file(r, rel)
        if local is None or not fresh:                  # an outdated copy is only the fallback while offline
            try:
                if w and is_video:
                    raise BucketError('a video streamed from S3 has no preview: download its folder (or load the '
                                  'bucket locally)', 415)
                view = _s3(r, shared=True)
                data, etag, size = view.read(rel)
                if w:
                    return store.thumbnail(data, f'{r.id}|{rel}|{etag}|{size}', w, _cfg()), 'image/jpeg'
                return data, mimetypes.guess_type(rel)[0] or 'application/octet-stream'
            except (store.Offline, BucketError) as e:
                if local is None or (not isinstance(e, store.Offline) and e.status != 415):
                    raise
        p, mt = local, mimetypes.guess_type(local)[0] or 'application/octet-stream'
    else:
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
    """A .env file holds secrets: reading it needs bucket.manage, like changing data."""
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
    local, fresh = _local_file(r, rel)
    if r.kind == 's3' and not fresh:
        try:
            view = _s3(r, shared=True)
            data, _, size = view.read(rel, limit + 1)
            if len(data) > limit:
                size = view.size(rel) or size
        except store.Offline:
            if local is None:
                raise
            fresh = True                                 # offline: the (older) downloaded copy
    if r.kind == 's3' and fresh:
        with open(local, 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            data = f.read(limit + 1)
    elif r.kind != 's3':
        p, _ = file_path(user, bid, rel)
        with open(p, 'rb') as f:
            st = os.fstat(f.fileno())
            data = f.read(limit + 1)
        size, mtime_ns = st.st_size, str(st.st_mtime_ns)
    if b'\x00' in data[:8192]:
        raise BucketError(f'{rel} is not a text file')
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
            _write_view(r, targets=(rel,))
        except BucketError as e:
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
    rel = _file(r, path)
    r = _write_view(r, targets=(rel,))
    if not isinstance(text, str):
        raise BucketError('text must be a string')
    p = os.path.realpath(_abs(r, rel, must_exist=not create))
    base = os.path.realpath(r.root)
    rel = os.path.relpath(p, base).replace(os.sep, '/')
    if not store.visible(os.path.basename(p)):
        raise BucketError(f'{rel} is a hidden file')
    exists = os.path.lexists(p)
    if create and exists:
        raise BucketError(f'{rel} already exists', 409)
    body = text.replace('\r\n', '\n').replace('\r', '\n')
    old = mode = None
    if exists:
        if not os.path.isfile(p):
            raise BucketError(f'{rel} is not a file')
        st = os.stat(p)
        if expect_mtime_ns not in (None, '') and str(expect_mtime_ns) != str(st.st_mtime_ns):
            raise BucketError(f'{rel} was changed by someone else since you opened it', 409)
        if st.st_size > TEXT_MAX:
            raise BucketError(f'{rel} is larger than {TEXT_MAX >> 20} MB: it cannot be edited here', 413)
        with open(p, 'rb') as f:
            old = f.read()
        if b'\x00' in old[:8192]:
            raise BucketError(f'{rel} is not a text file')
        prev, strict = _decode(old, False)
        if not strict:
            raise BucketError(f'{rel} is not UTF-8 text: saving would change its bytes', 415)
        nl = _newline(prev)
        if nl != '\n':
            body = body.replace('\n', nl)
        mode = st.st_mode & 0o7777
    elif not os.path.isdir(os.path.dirname(p)):
        raise BucketError(f'{store.posix_parent(rel) or "/"} is not a folder', 404)
    data = ((_BOM if old is not None and old.startswith(_BOM) else b'') + body.encode('utf-8'))
    if len(data) > TEXT_MAX:
        raise BucketError(f'at most {TEXT_MAX >> 20} MB of text', 413)
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
        c.execute(sql("""INSERT INTO app.bucket_undo(id, bucket_id, user_id, manifest)
                         VALUES (:id, :b, :u, :manifest)""").bindparams(_MANIFEST),
                  {'id': undo_id, 'b': r.id, 'u': user['id'],
                   'manifest': {'root': r.root, 'trash': str(trash), 'steps': [step]}})
        c.execute(sql('UPDATE app.buckets SET updated_at = now() WHERE id = :b'), {'b': r.id})
    return {'path': rel, 'size': st.st_size, 'mtime_ns': str(st.st_mtime_ns), 'undo_id': undo_id,
            'created': old is None, 'retagged': _revalidate(r)}


# ---------------------------------------------------------------------------------------------- changes
def ops(user, bid, ops):
    """Apply file ops in order (each one may fail on its own) -> {applied, errors, undo_id, retagged}."""
    r = _row(user, bid)
    _need(user)
    if not isinstance(ops, (list, tuple)) or not ops:
        raise BucketError('ops must be a non-empty list')
    rows = _local_rows(r.id) if _streamed(r) else None    # a streamed link: each op must stay in its downloads
    r = _write_view(r)
    if len(ops) > 5000:
        raise BucketError('at most 5000 ops at once')
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
            if rows is not None:
                _need_inside(r, rows, *_op_paths(op))
            info, step = store.apply_op(r.root, allowed, op, str(trash), i)
        except BucketError as e:
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
            c.execute(text("""INSERT INTO app.bucket_undo(id, bucket_id, user_id, manifest)
                              VALUES (:id, :b, :u, :manifest)""").bindparams(_MANIFEST),
                      {'id': undo_id, 'b': r.id, 'u': user['id'],
                       'manifest': {'root': r.root, 'trash': str(trash), 'steps': steps}})
            c.execute(text('UPDATE app.buckets SET updated_at = now() WHERE id = :b'), {'b': r.id})
            keep = set(c.execute(text('SELECT id FROM app.bucket_undo')).scalars().all())
        store.purge_trash(str(trash_root), keep)
    return {'applied': applied, 'errors': errors, 'undo_id': undo_id if steps else None,
            'retagged': _revalidate(r) if steps else None}


def undo(user, bid, undo_id):
    r = _row(user, bid)
    _need(user)
    r = _write_view(r)
    with db.connect() as c:
        u = c.execute(text('SELECT * FROM app.bucket_undo WHERE id = :id AND bucket_id = :b'),
                      {'id': str(undo_id or ''), 'b': r.id}).first()
    if u is None:
        raise BucketError('nothing to undo with that id (already undone, or expired)', 404)
    if u.user_id != user['id'] and not user.get('is_admin') and r.owner_id != user['id']:
        raise BucketError('only who made a change can undo it', 403)
    man = u.manifest or {}
    if os.path.realpath(man.get('root') or '') != os.path.realpath(r.root):
        raise BucketError('this change belongs to another folder', 409)
    allowed = _allowed()
    trash_root = str(store.data_root(_cfg()) / store.TRASH)
    restored, errors = [], []
    for step in reversed(man.get('steps') or []):
        try:
            restored.append(store.undo_step(r.root, allowed, step, trash_root))
        except (BucketError, OSError) as e:
            errors.append({'step': step.get('undo'), 'path': step.get('path') or step.get('to'), 'error': str(e)})
            continue
        with db.begin() as c:
            if step['undo'] == 'move':
                vtags.move_paths(r.id, step['from'], step['to'], c)
            elif step['undo'] == 'restore':
                vtags.put_back(r.id, step.get('tags'), c)
    with db.begin() as c:
        c.execute(text('DELETE FROM app.bucket_undo WHERE id = :id'), {'id': u.id})
    if not errors:
        shutil.rmtree(man.get('trash') or os.path.join(trash_root, u.id), ignore_errors=True)
    return {'restored': len(restored), 'paths': restored, 'errors': errors, 'retagged': _revalidate(r)}


def upload(user, bid, path, files, unzip=True):
    """Stream files into folder `path` (zips unpacked there unless unzip=False) -> {added, skipped, retagged}."""
    r = _row(user, bid)
    _need(user)
    r = _write_view(r, targets=(str(path or ''),))
    auto = getattr(r._r, 'video_auto', None) and not getattr(r, 'local_view', False)
    before = _videos_below(r, path) if auto else None
    return _after_upload(user, r, path, store.upload_local(r.root, _allowed(), path, files, unzip=unzip), before)


def _after_upload(user, r, path, out, before):
    """The bucket's touch, format re-check and auto-extract of the new videos after files landed in `path`."""
    with db.begin() as c:
        c.execute(text('UPDATE app.buckets SET updated_at = now() WHERE id = :b'), {'b': r.id})
    out['retagged'] = _revalidate(r) if out['added'] else None
    if before is not None and out['added']:
        out['frames'] = _auto_frames(user, r, sorted(_videos_below(r, path) - before))
    return out


def upload_part(user, bid, path, name, uid, offset, total, data, unzip=True):
    """One chunk of a big file uploaded in parts (store.upload_part; any order, in parallel) -> {received, ranges}
    until the file is whole, then what upload() answers plus done: True."""
    r = _row(user, bid)
    _need(user)
    r = _write_view(r, targets=(str(path or ''),))
    out = store.upload_part(r.root, _allowed(), path, name, uid, offset, total, data, unzip=unzip)
    if not out.get('ready'):
        if out.get('done'):
            out['retagged'] = None
        return out
    before = (_videos_below(r, path) if getattr(r._r, 'video_auto', None) and not getattr(r, 'local_view', False)
              else None)
    out = store.finish_part(r.root, _allowed(), path, name, uid, out['path'], unzip=unzip)
    if not out['added']:
        out['retagged'] = None
        return out
    return _after_upload(user, r, path, out, before)


def part_status(user, bid, path, name, uid, total, unzip=True):
    """The ranges the server holds of a file uploaded in parts (resume), or skipped: True-ish SKIP."""
    r = _row(user, bid)
    _need(user)
    r = _write_view(r, targets=(str(path or ''),))
    out = store.part_status(r.root, _allowed(), path, name, uid, total, unzip=unzip)
    if out.get('done'):
        out['retagged'] = None
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
        if isinstance(e, BucketError):
            raise
        raise BucketError(str(e)) from None


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
            except BucketError:
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
    except (BucketError, OSError):
        isdir, size = False, None
    return {'bucket': r.id, 'path': p, 'name': name, 'type': 'dir' if isdir else 'file', 'size': size}


# ---------------------------------------------------------------------------------------------- send
TASK_TYPES = {'detect': 'object_detection', 'segment': 'segmentation', 'pose': 'keypoints',
              'obb': 'object_detection', 'classify': 'classification'}
VISUALIZABLE = ('yolo-detect', 'yolo-seg', 'yolo-pose', 'yolo-obb', 'images', 'classification', 'embeddings')


def _parents(rel):
    """'a/b/c' -> ['a/b', 'a', ''] (the folders above, nearest first: "../.." is parents[1])."""
    parts = [p for p in rel.split('/') if p]
    return ['/'.join(parts[:k]) for k in range(len(parts) - 1, -1, -1)]


def locate(user, path=None, s3_bucket=None, prefix=None):
    """The Bucket page folder that holds a dataset (a visualize session's source) -> {bucket, bucket_name, path,
    parents} or None when no bucket the user sees contains it. A local `path` matches a bucket whose folder (or
    the local copy of a downloaded S3 link) contains it; an S3 source (s3_bucket, prefix) an S3 link of that
    bucket whose prefix contains it. The deepest bucket wins (a bucket inside another bucket's folder)."""
    with db.connect() as c:
        rows = _visible_rows(user, c)
    want = os.path.realpath(path) if path else None
    best = None
    for r in rows:
        rel = None
        if want:
            for root in (r._r.root if r._r.kind != 's3' else None, r._r.local_root):
                if not root:
                    continue
                root = os.path.realpath(root)
                if want == root or want.startswith(root.rstrip(os.sep) + os.sep):
                    rel = os.path.relpath(want, root).replace(os.sep, '/')
                    break
        elif s3_bucket and r._r.kind == 's3' and r._r.s3_bucket == s3_bucket:
            bp, px = (r._r.prefix or '').strip('/'), (prefix or '').strip('/')
            if not bp or px == bp or px.startswith(bp + '/'):
                rel = px[len(bp):].strip('/')
        if rel is None:
            continue
        rel = '' if rel == '.' else rel
        depth = len([p for p in rel.split('/') if p])
        if best is None or depth < best[0]:
            best = (depth, rel, r)
    if best is None:
        return None
    _, rel, r = best
    return {'bucket': r.id, 'bucket_name': r.name, 'path': rel, 'parents': _parents(rel)}


def folder_source(user, bid, rel):
    """Where a bucket folder lives, in the terms a visualize session's source uses: {path} (local, absolute) or
    {s3_bucket, prefix} (an S3 link) -> matched against the sessions (send() makes the same source)."""
    r = _row(user, bid)
    rel = store.clean_rel(rel or '')
    if r.kind == 's3':
        return {'s3_bucket': r.s3_bucket, 'prefix': '/'.join(p for p in (r.prefix.strip('/'), rel) if p)}
    return {'path': os.path.realpath(_abs(r, rel, must_exist=False))}


def send(user, bid, paths, target='visualize', name=None, task_type='detect'):
    """Open one folder of a bucket as a visualize session (a saved dataset) or a labeling project (source
    {kind: local, path} / S3) -> {target, id, url} with the page's URL ('s/<id>/' or '?label=<pid>')."""
    r = _row(user, bid)
    _need(user)
    if target not in ('visualize', 'label'):
        raise BucketError('target must be visualize or label')
    if target not in zones.send_targets(_cfg()):
        raise BucketError(f'this install has no {"visualizer" if target == "visualize" else "labeling"} zone')
    _need(user, 'dataset.create' if target == 'visualize' else 'label.create')
    paths = [paths] if isinstance(paths, str) else list(paths or [''])
    if len(paths) != 1:
        raise BucketError('send one folder at a time (select the folder that holds them)')
    rel = store.clean_rel(paths[0])
    if r.kind != 's3' and not os.path.isdir(_abs(r, rel)):
        raise BucketError(f'{rel} is a file: send the folder that holds it')
    fmt = _cached(r, rel)['format']
    base = r.name + (f' · {rel}' if rel else '')
    if target == 'visualize':
        return _send_visualize(user, r, rel, fmt, (name or '').strip())
    return _send_label(user, r, rel, fmt, (name or '').strip() or base, task_type)


def _send_visualize(user, r, rel, fmt, name):
    f = fmt.get('format')
    if f not in VISUALIZABLE:
        why = f' ({fmt["reason"]})' if fmt.get('reason') else ''
        raise BucketError(f'{rel or r.name} looks like “{fmt.get("label")}”{why}: the visualizer opens YOLO folders, '
                      'plain image folders, classification folders and embeddings (.npy + meta.csv). '
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
        from ..image.embedding_source import EmbeddingSource
        check = lambda ds: EmbeddingSource(ds['path']).check()  # noqa: E731
    try:
        ds = _datasets().upsert_dataset(body, user['id'], check, _allowed() if r.kind != 's3' else None)
    except (ValueError, KeyError) as e:
        raise BucketError(str(e).strip("'\"")) from None
    return {'target': 'visualize', 'id': ds['id'], 'url': f's/{ds["id"]}/', 'name': ds['name']}


def _send_label(user, r, rel, fmt, name, task_type):
    from ..image import sessions
    tt = TASK_TYPES.get(task_type or 'detect')
    if tt is None:
        raise BucketError(f'task_type must be one of {", ".join(TASK_TYPES)}')
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
        raise BucketError(str(e).strip("'\"")) from None
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
    except BucketError:
        return [], None


def _label_source(r, rel):
    """The labeling-project source of folder `rel` (images are referenced in place, never copied)."""
    if r.kind == 's3':
        return {'kind': 's3', 'connection_id': r.connection_id, 'bucket': r.s3_bucket,
                'prefix': '/'.join(p for p in (r.prefix.strip('/'), rel) if p)}
    return {'kind': 'local', 'path': _abs(r, rel)}


def source(user, bid, path=''):
    """What a new labeling session needs to read folder `path` of a bucket (New label › Bucket):
    {source, format, classes, bucket, path} -- source is the POST /api/label/projects `source`."""
    r = _row(user, bid)
    rel = store.clean_rel(path)
    if r.kind != 's3' and not os.path.isdir(_abs(r, rel)):
        raise BucketError(f'{rel} is a file: pick the folder that holds it')
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
        raise BucketError('labeling with a model runs in the server (the labeling routes are not loaded here)', 501)
    return hooks


def _http(fn, *a, **kw):
    """Call a labeling-route helper; its HTTPException / ModelError / ValueError becomes a BucketError."""
    from starlette.exceptions import HTTPException

    from ..labeling.models import ModelError
    try:
        return fn(*a, **kw)
    except HTTPException as e:
        d = e.detail.get('detail') if isinstance(e.detail, dict) else e.detail
        raise BucketError(str(d), e.status_code) from None
    except ModelError as e:
        raise BucketError(str(e), getattr(e, 'status', 400)) from None
    except (ValueError, KeyError) as e:
        raise BucketError(str(e).strip("'\""), 404 if isinstance(e, KeyError) else 400) from None


def _link_of(model):
    model = dict(model or {})
    rid, cid = model.get('registry_id') or None, model.get('connection_id') or None
    if not rid and not (cid and model.get('model_id')):
        raise BucketError('pick a model: one of Settings › Models, or a model server and one of its models')
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
        raise BucketError('confidence must be between 0 and 1')
    rel = store.clean_rel(path)
    if r.kind != 's3' and not os.path.isdir(_abs(r, rel)):
        raise BucketError(f'{rel} is a file: pick the folder that holds the images')
    if target not in ('inplace', 'copy'):
        raise BucketError('target must be inplace or copy')
    if target == 'inplace':
        _need_write(r)
    from ..labeling import writers as lw
    try:
        fmts = [] if write_yolo is False and formats is None else lw.check(lw.DEFAULT if formats is None else formats)
    except ValueError as e:
        raise BucketError(str(e)) from None
    shown = str((model or {}).get('name') or link['model_id'])     # the job label only; the job reads the model

    def check_model():
        # in the job, never in the request: a registry model may have to load (torch) to report its classes
        info = _http(hooks.model_info, link, user['id'])
        tt = info.get('task_type') or 'object_detection'
        classes = [str(c) for c in info.get('classes') or []]
        if tt not in AUTO_TASKS:
            raise BucketError(f'{info.get("name") or link["model_id"]} is a {tt} model: it cannot label images')
        if not classes and tt != 'classification':
            raise BucketError(f'{info.get("name") or link["model_id"]} reports no classes')
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
        from ..image import sessions
        task = {'name': AUTO_TASKS[tt], 'task_type': tt, 'classes': [], 'config': {}}
        if tt == 'keypoints':
            task['config'] = {'keypoints': list(info.get('keypoints') or [f'kp{i + 1}' for i in range(17)])}
        reg = _labeling()
        try:
            entry = reg.create({'name': sessions.free_name(user['id'], 'label', session_name[:200]), 'tasks': [task],
                                'source': _label_source(dest, src_rel)}, user['id'])
        except (ValueError, KeyError) as e:
            raise BucketError(str(e).strip("'\"")) from None
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
    job = jobs.submit(f'bk-autolabel:{dest.id}:{rel}', f'label {dest.name} with {shown}',
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
        raise BucketError(f'could not move the images into images/: {res["errors"][0]["error"]}')
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
            raise BucketError('cancelled', 499)
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
        lines = [f'# written by the Bucket page from a {task["task_type"].replace("_", " ")} model', 'path: .',
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
