"""Change marks of the Bucket page: which files of a bucket changed since they were downloaded (or marked seen), and
by what (the Bucket page, a fix in Visualize, a labeling export, an edit outside the app). One row per path in
app.bucket_changes (an upsert: the table never holds more rows than changed paths); a folder's mark is the count of
changed rows below it, so `marks` answers one listing page with one indexed range scan, not a query per row.

Writers: service.ops / write_text / upload / copy_to (source bucket), the label writes of image/sources.py through
`note_local` / `note_s3` (source visual: a background thread, the save never waits for it), the bk-check job
(source external: a downloaded file whose size / mtime left the manifest's). Upload to S3 (service.push) and Mark
as seen clear them.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from .. import db
from ..core import logging as vlog

_log = vlog.get_logger('bucket')

KINDS = ('edited', 'created', 'copied', 'moved', 'deleted', 'uploaded', 'external')
SOURCES = ('bucket', 'visual', 'label', 'external')
KEEP = {'created', 'copied', 'uploaded'}       # an edit of a new file keeps it "new"
ROOTS_TTL = 30                                  # s: the root map (which bucket a path belongs to) is read again after
_pool = ThreadPoolExecutor(1, thread_name_prefix='bk-changes')
_roots = {'at': 0.0, 'local': [], 's3': {}}
_roots_lock = threading.Lock()


def _like(p):
    return p.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _clean(p):
    return str(p or '').strip('/')


# ---------------------------------------------------------------------------------------------- write
def record(bid, paths, kind, source='bucket', by=None, conn=None, keep=False):
    """Mark `paths` of bucket `bid` as changed (one batched upsert). A created / copied / uploaded path stays so when
    it is edited later; keep: a path marked already keeps its mark (the background check)."""
    rows = [{'b': bid, 'p': _clean(p), 'k': kind, 's': source, 'u': by} for p in dict.fromkeys(paths or ()) if _clean(p)]
    if not rows:
        return 0
    sql = text("""INSERT INTO app.bucket_changes AS c (bucket_id, path, kind, source, user_id, at)
                  VALUES (:b, :p, :k, :s, :u, now())
                  ON CONFLICT (bucket_id, path) DO """ + ('NOTHING' if keep else """UPDATE SET
                    kind = CASE WHEN c.kind IN ('created', 'copied', 'uploaded') AND EXCLUDED.kind IN ('edited', 'external')
                                THEN c.kind ELSE EXCLUDED.kind END,
                    source = EXCLUDED.source, user_id = EXCLUDED.user_id, at = now()"""))
    if conn is not None:
        conn.execute(sql, rows)
    else:
        with db.begin() as c:
            c.execute(sql, rows)
    return len(rows)


def moved(bid, old, new, by=None, conn=None):
    """`old` (a file or folder) became `new`: its marks (and those below it) move with it, and `new` is marked moved."""
    old, new = _clean(old), _clean(new)
    if not old or not new or old == new:
        return

    def run(c):
        c.execute(text("""DELETE FROM app.bucket_changes WHERE bucket_id = :b AND (path = :n OR path LIKE :np ESCAPE '\\')"""),
                  {'b': bid, 'n': new, 'np': _like(new) + '/%'})            # what was there before (an undo's target)
        c.execute(text("""UPDATE app.bucket_changes SET path = :n || substr(path, :k)
                          WHERE bucket_id = :b AND (path = :o OR path LIKE :op ESCAPE '\\')"""),
                  {'b': bid, 'n': new, 'k': len(old) + 1, 'o': old, 'op': _like(old) + '/%'})
        record(bid, [new], 'moved', 'bucket', by, conn=c)

    if conn is not None:
        run(conn)
    else:
        with db.begin() as c:
            run(c)


def clear(bid, paths=(), prefix=None, conn=None):
    """Forget the marks of `paths` (each with everything below it), or of every path under `prefix` ('' = all)
    -> how many were cleared."""
    q, args = [], {'b': bid}
    if prefix is not None:
        p = _clean(prefix)
        if p:
            q.append("path = :x OR path LIKE :xp ESCAPE '\\'")
            args.update(x=p, xp=_like(p) + '/%')
        else:
            q.append('true')
    for i, p in enumerate(dict.fromkeys(_clean(x) for x in paths or ())):
        if not p:
            q.append('true')
            continue
        q.append(f"path = :p{i} OR path LIKE :q{i} ESCAPE '\\'")
        args[f'p{i}'], args[f'q{i}'] = p, _like(p) + '/%'
    if not q:
        return 0
    sql = text(f'DELETE FROM app.bucket_changes WHERE bucket_id = :b AND ({" OR ".join(f"({x})" for x in q)})')
    if conn is not None:
        return conn.execute(sql, args).rowcount
    with db.begin() as c:
        return c.execute(sql, args).rowcount



def clear_exact(bid, paths, conn=None):
    """Forget the marks of exactly these paths (files a download just replaced with S3's version)."""
    ps = [p for p in dict.fromkeys(_clean(x) for x in paths or ()) if p]
    if not ps:
        return 0
    sql = text('DELETE FROM app.bucket_changes WHERE bucket_id = :b AND path = ANY(:ps)')
    if conn is not None:
        return conn.execute(sql, {'b': bid, 'ps': ps}).rowcount
    with db.begin() as c:
        return c.execute(sql, {'b': bid, 'ps': ps}).rowcount

# ---------------------------------------------------------------------------------------------- read
def marks(bid, rel=''):
    """{name: {kind, source, at, n, self}} of the direct children of folder `rel` that changed (themselves, or
    something below them), and the total under `rel`: ({...}, total). One range scan of the prefix index."""
    rel = _clean(rel)
    start = len(rel) + 2 if rel else 1
    with db.connect() as c:
        rows = c.execute(text("""
            SELECT split_part(substr(path, :s), '/', 1) AS name, count(*) AS n, max(at) AS at,
                   (array_agg(kind ORDER BY at DESC))[1] AS kind, (array_agg(source ORDER BY at DESC))[1] AS source,
                   bool_or(strpos(substr(path, :s), '/') = 0) AS self
            FROM app.bucket_changes WHERE bucket_id = :b AND path LIKE :pre ESCAPE '\\'
            GROUP BY 1"""), {'b': bid, 's': start, 'pre': (_like(rel) + '/%') if rel else '%'}).mappings().all()
    out = {r['name']: {'kind': r['kind'], 'source': r['source'], 'at': r['at'].isoformat() if r['at'] else None,
                       'n': int(r['n']), 'self': bool(r['self'])} for r in rows if r['name']}
    return out, sum(v['n'] for v in out.values())


def listing(bid, prefix='', offset=0, limit=200):
    """One page of the changed paths under `prefix`, newest first -> {total, items: [{path, kind, source, at}]}."""
    p = _clean(prefix)
    args = {'b': bid, 'x': p, 'xp': (_like(p) + '/%') if p else '%', 'o': max(0, int(offset)),
            'l': max(1, min(1000, int(limit)))}
    where = "bucket_id = :b AND (path = :x OR path LIKE :xp ESCAPE '\\')"
    with db.connect() as c:
        total = c.execute(text(f'SELECT count(*) FROM app.bucket_changes WHERE {where}'), args).scalar() or 0
        rows = c.execute(text(f"""SELECT path, kind, source, at FROM app.bucket_changes WHERE {where}
                                  ORDER BY at DESC, path LIMIT :l OFFSET :o"""), args).mappings().all()
    return {'total': int(total), 'items': [{'path': r['path'], 'kind': r['kind'], 'source': r['source'],
                                            'at': r['at'].isoformat() if r['at'] else None} for r in rows]}


# ---------------------------------------------------------------------------------------------- which bucket a path is in
def _load_roots():
    with db.connect() as c:
        rows = c.execute(text("""SELECT id, kind, root, local_root, connection_id, s3_bucket, prefix
                                 FROM app.buckets""")).mappings().all()
    local, s3 = [], {}
    for r in rows:
        for root in (r['root'] if r['kind'] != 's3' else None, r['local_root']):
            if root:
                local.append((os.path.realpath(root).rstrip(os.sep) + os.sep, r['id']))
        if r['kind'] == 's3' and r['connection_id'] and r['s3_bucket']:
            pre = _clean(r['prefix'])
            s3.setdefault((r['connection_id'], r['s3_bucket']), []).append((pre + '/' if pre else '', r['id']))
    local.sort(key=lambda x: -len(x[0]))         # the longest root first: a bucket inside another's folder wins
    for v in s3.values():
        v.sort(key=lambda x: -len(x[0]))
    return local, s3


def roots(force=False):
    with _roots_lock:
        if force or time.monotonic() - _roots['at'] > ROOTS_TTL:
            _roots['local'], _roots['s3'] = _load_roots()
            _roots['at'] = time.monotonic()
        return _roots['local'], _roots['s3']


def forget_roots():
    """A bucket was created, changed or removed: the next lookup reads the map again."""
    _roots['at'] = 0.0


MISS_RELOAD = 2.0                               # s: a miss reads the map again (a bucket made since), at most this often


def _find(match):
    for i in range(2):
        hit = match(*roots())
        if hit or i or time.monotonic() - _roots['at'] < MISS_RELOAD:
            return hit
        forget_roots()
    return None


def resolve_local(path):
    """(bucket id, path in it) of an absolute local path, or None."""
    p = os.path.realpath(str(path))

    def match(local, _s3):
        for root, bid in local:
            if p.startswith(root):
                return bid, p[len(root):].replace(os.sep, '/')
        return None
    return _find(match)


def resolve_s3(connection_id, bucket, key):
    """(bucket id, path in it) of an object of an S3 link, or None."""
    def match(_local, s3):
        for pre, bid in s3.get((connection_id, bucket), ()):
            if key.startswith(pre):
                return bid, key[len(pre):]
        return None
    return _find(match)


# ---------------------------------------------------------------------------------------------- hooks (never block)
def _later(fn, *a):
    def run():
        try:
            fn(*a)
        except Exception as e:  # noqa: BLE001 - a mark is a hint: it never fails the write it follows
            _log.debug(f'change mark skipped: {vlog.esc(str(e))}')
    try:
        _pool.submit(run)
    except RuntimeError:                         # interpreter shutting down
        pass


def note_local(path, kind='edited', source='visual', by=None):
    """A file at absolute `path` was written outside the Bucket page (a label fix): mark it when it is in a bucket."""
    def go():
        hit = resolve_local(path)
        if hit:
            record(hit[0], [hit[1]], kind, source, by)
    _later(go)


def note_s3(connection_id, bucket, key, kind='edited', source='visual', by=None):
    def go():
        hit = resolve_s3(connection_id, bucket, key)
        if hit:
            record(hit[0], [hit[1]], kind, source, by)
    _later(go)


def flush(timeout=5.0):
    """Wait for the queued marks (tests)."""
    _pool.submit(lambda: None).result(timeout)


def _on_label_write(path=None, connection_id=None, bucket=None, key=None, **_):
    if path:
        note_local(path)
    elif connection_id and bucket and key:
        note_s3(connection_id, bucket, key)


def install():
    """Mark the label files Visualize writes (image/sources.py WRITE_HOOKS); once."""
    from ..image import sources
    if _on_label_write not in sources.WRITE_HOOKS:
        sources.WRITE_HOOKS.append(_on_label_write)
