"""
Local copy vs. the cloud: what the user changed in the downloaded copy of an S3 link, and what S3 changed since.

A download sets each file's mtime to its object's LastModified (store.S3View.pull / service._fetch_file) and stamps
the folder with the end of the download (bucket_s3_local.fetched_at; a whole bucket loaded into local: synced_at) = T.
So, per file, with SLACK seconds of clock tolerance:

    same            size equal and mtime == LastModified                      (as downloaded)
    added_local     only here, mtime > T                                      (the user made it)
    removed_cloud   only here, mtime <= T                                     (S3 deleted it after the download)
    deleted_local   only in S3, LastModified <= T                             (the user deleted it)
    new_cloud       only in S3, LastModified > T                              (added to S3 after the download)
    edited_local    both, different, touched here (mtime > T), S3 not since T (the user changed the file)
    changed_cloud   both, different, untouched here (mtime <= T)              (S3 has a newer version)
    both            both, different, touched here and S3 newer than T         (changed on both sides)

verify: also read the files whose size and time say same / changed and compare their MD5 with the S3 ETag (single-
part uploads only; a multipart ETag is not an MD5 of the file): a match is same, a mismatch is a real edit.

The result goes to one SQLite file per compared folder (<data_root>/.compare/<bid>/<hash>.sqlite, written aside
and swapped in), so a listing of millions of files is paged and filtered with an index, never held in memory.
"""

import hashlib
import os
import sqlite3
import stat as stat_
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from . import s3list, store

STATUSES = ('added_local', 'deleted_local', 'edited_local', 'both', 'new_cloud', 'changed_cloud', 'removed_cloud',
            'same')
LABEL = {'same': 'Same', 'added_local': 'Added in local', 'deleted_local': 'Deleted in local',
         'edited_local': 'Edited in local', 'new_cloud': 'New in cloud', 'changed_cloud': 'Changed in cloud',
         'removed_cloud': 'Removed from cloud', 'both': 'Changed on both sides'}
RANK = {s: i for i, s in enumerate(STATUSES)}          # stored as its rank: the index orders a page by status, path
LOCAL_SIDE = ('added_local', 'deleted_local', 'edited_local', 'both')       # what the user did in the copy
SLACK = 2.0                     # s of clock difference between this server and S3
BATCH = 5000
MAX_PAGE = 1000
VERIFY_WORKERS = 8
_CHUNK = 1 << 20


UNKNOWN = object()               # no manifest of the download: the times decide


def _eq(a, b):
    return a is not None and b is not None and a[0] == b[0] and b[1] is not None and abs(a[1] - b[1]) < 1e-3


def status_of(local, cloud, t_sync, was=UNKNOWN):
    """local (size, mtime) | None, cloud (size, last_modified) | None, t_sync: the end of the download; was: what
    the download's manifest holds for the file ((size, last_modified), None = not in it, UNKNOWN = no manifest)."""
    if was is not UNKNOWN and (was is not None or not (local and cloud)):
        if local and cloud:
            if _eq(local, cloud):
                return 'same'
            mine, theirs = not _eq(local, was), not _eq(cloud, was)
            return 'both' if mine and theirs else 'edited_local' if mine else 'changed_cloud'
        if local:
            return 'removed_cloud' if was is not None else 'added_local'
        return 'deleted_local' if was is not None else 'new_cloud'
    if local and cloud:
        if local[0] == cloud[0] and cloud[1] is not None and abs(local[1] - cloud[1]) < 1e-3:
            return 'same'
        if local[1] <= t_sync + SLACK:      # untouched here since the download, yet different: S3 has a new version
            return 'changed_cloud'
        return 'both' if cloud[1] is not None and cloud[1] > t_sync else 'edited_local'
    if local:
        return 'added_local' if local[1] > t_sync + SLACK else 'removed_cloud'
    # S3's LastModified is cut to the second and S3's clock is not ours: anything after the download's end is new
    return 'new_cloud' if cloud[1] is not None and cloud[1] > t_sync else 'deleted_local'


def single_md5(etag):
    """The MD5 an ETag stands for ('' for a multipart / encrypted one, which is not the file's MD5)."""
    e = str(etag or '').strip().strip('"').lower()
    return e if len(e) == 32 and all(c in '0123456789abcdef' for c in e) else ''


def md5_of(path, cancelled=None):
    h = hashlib.md5(usedforsecurity=False)
    with open(path, 'rb') as f:
        while chunk := f.read(_CHUNK):
            if cancelled and cancelled():
                return ''
            h.update(chunk)
    return h.hexdigest()


def db_path(root, bid, rel):
    return Path(root) / '.compare' / bid / f'{hashlib.sha1(rel.encode()).hexdigest()[:16]}.sqlite'


def manifest_path(root, bid):
    return Path(root) / '.compare' / bid / 'manifest.sqlite'


class Manifest:
    """What S3 held at the last download of each folder: {path: (size, last_modified)}, written while a pull lists
    S3 (store.S3View.pull's `listed`). With it, a file only here is told apart exactly: in the manifest = S3 removed
    it, else the user added it; one only in S3: in the manifest = the user deleted it, else it is new.

        with Manifest(path, rel) as m: view.pull(..., listed=m.add)     # replaces the rows below rel
    """

    def __init__(self, path, rel):
        self.path, self.rel, self.rows, self.con = Path(path), rel, [], None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=30)
        self.con.execute('CREATE TABLE IF NOT EXISTS m (path TEXT PRIMARY KEY, size INTEGER, lm REAL)')
        _drop_under(self.con, self.rel)
        return self

    def add(self, k, size, lm):
        self.rows.append((k, size, lm))
        if len(self.rows) >= BATCH:
            self._flush()

    def _flush(self):
        self.con.executemany('INSERT OR REPLACE INTO m VALUES (?, ?, ?)', self.rows)
        self.rows.clear()

    def __exit__(self, kind, exc, tb):
        try:
            if kind is None:
                self._flush()
                self.con.commit()
            else:                               # a failed / stopped download: what it saw is not the whole folder
                self.con.rollback()
                _drop_under(self.con, self.rel)
                self.con.commit()
        finally:
            self.con.close()
        return False


def _drop_under(con, rel):
    if rel:
        esc = rel.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        con.execute("DELETE FROM m WHERE path = ? OR path LIKE ? ESCAPE '\\'", (rel, esc + '/%'))
    else:
        con.execute('DELETE FROM m')


def rows_under(path, rel):
    """(path, size, lm) of the manifest rows below `rel` ('' = all), or [] without a manifest."""
    p = Path(path)
    if not p.exists():
        return []
    with closing(sqlite3.connect(p, timeout=30)) as con:
        if rel:
            esc = rel.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            return con.execute("SELECT path, size, lm FROM m WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                               (rel, esc + '/%')).fetchall()
        return con.execute('SELECT path, size, lm FROM m').fetchall()


def manifest_put(path, rows):
    """Upsert (path, size, lm) rows: what S3 holds now for files just uploaded (service.push)."""
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(p, timeout=30)) as con:
        con.execute('CREATE TABLE IF NOT EXISTS m (path TEXT PRIMARY KEY, size INTEGER, lm REAL)')
        con.executemany('INSERT OR REPLACE INTO m VALUES (?, ?, ?)', rows)
        con.commit()


def forget(root, bid, rel):
    """Drop the manifest rows below `rel` (its local copy was removed)."""
    p = manifest_path(root, bid)
    if p.exists():
        with closing(sqlite3.connect(p, timeout=30)) as con:
            _drop_under(con, rel)
            con.commit()


def _known(manifest):
    """(path) -> (size, lm) | None of a manifest file (None when there is none: the times decide)."""
    if not manifest or not Path(manifest).exists():
        return None
    con = sqlite3.connect(f'file:{manifest}?mode=ro', uri=True, check_same_thread=False)

    def get(k):
        r = con.execute('SELECT size, lm FROM m WHERE path = ?', (k,)).fetchone()
        return (r[0], r[1]) if r else None
    get.close = con.close
    return get


_SCHEMA = """CREATE TABLE f (path TEXT PRIMARY KEY, st INTEGER NOT NULL, ls INTEGER, lm REAL, cs INTEGER, cm REAL,
                             etag TEXT, verified TEXT);
             CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"""


def run(view, dest, targets, out, *, verify=False, cancelled=None, progress=None, manifest=None):
    """Compare the local copy `dest` with S3 for every (rel, t_sync) of `targets`; writes `out` (sqlite) ->
    {counts, total, cancelled}. `view`: the link's S3View (its base = the link's prefix)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f'.{os.getpid()}.{time.monotonic_ns()}.tmp')
    con = sqlite3.connect(tmp)
    known = _known(manifest)
    was = known if known is not None else (lambda k: UNKNOWN)
    try:
        con.executescript(_SCHEMA)
        n = 0
        rows = []

        def flush():
            con.executemany('INSERT OR REPLACE INTO f VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
            rows.clear()

        for rel, t_sync in targets:
            pre = view.base + (rel + '/' if rel else '')
            seen = set()
            with closing(s3list.objects(view.client, view.bucket, pre, cancelled=cancelled)) as objs:
                for o in objs:
                    if cancelled and cancelled():
                        break
                    sub = o['Key'][len(pre):]
                    if not store._shown(sub):
                        continue
                    k = store.join(rel, sub)
                    seen.add(k)
                    lm = o.get('LastModified')
                    cloud = (int(o.get('Size') or 0), lm.timestamp() if lm is not None else None)
                    try:
                        st = os.stat(os.path.join(dest, k))
                        local = (st.st_size, st.st_mtime) if stat_.S_ISREG(st.st_mode) else None
                    except OSError:
                        local = None
                    rows.append((k, RANK[status_of(local, cloud, t_sync, was(k))], local and local[0], local and local[1],
                                 cloud[0], cloud[1], o.get('ETag'), None))
                    n += 1
                    if len(rows) >= BATCH:
                        flush()
                        if progress:
                            progress(n, f'{n:,} files compared')
            top = Path(dest) / rel if rel else Path(dest)
            if top.is_dir() and not (cancelled and cancelled()):
                for k, _, p in store.walk_files(top, rel):
                    if k in seen:
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    rows.append((k, RANK[status_of((st.st_size, st.st_mtime), None, t_sync, was(k))], st.st_size,
                                 st.st_mtime, None, None, None, None))
                    n += 1
                    if len(rows) >= BATCH:
                        flush()
            elif rel and top.is_file() and rel not in seen:        # a downloaded file S3 lost
                st = top.stat()
                rows.append((rel, RANK[status_of((st.st_size, st.st_mtime), None, t_sync, was(rel))], st.st_size, st.st_mtime,
                             None, None, None, None))
                n += 1
        flush()
        if verify and not (cancelled and cancelled()):
            _verify(con, dest, cancelled, progress)
        con.execute('CREATE INDEX f_st ON f (st, path)')
        done = cancelled is None or not cancelled()
        con.executemany('INSERT INTO meta VALUES (?, ?)', [('finished_at', str(time.time())), ('verify', str(int(verify))),
                                                          ('complete', str(int(done)))])
        con.commit()
    finally:
        con.close()
        if known is not None:
            known.close()
    os.replace(tmp, out)
    return {**summary(out), 'cancelled': not (cancelled is None or not cancelled())}


def _verify(con, dest, cancelled, progress):
    """MD5 of the files whose times say same / changed against a single-part ETag; the status follows the bytes."""
    todo = [(p, single_md5(e)) for p, e in con.execute(
        "SELECT path, etag FROM f WHERE ls IS NOT NULL AND cs IS NOT NULL")]
    todo = [(p, m) for p, m in todo if m]
    if not todo:
        return

    def one(item):
        p, want = item
        try:
            got = md5_of(os.path.join(dest, p), cancelled)
        except OSError:
            return p, None
        return p, (got == want) if got else None

    done = 0
    with ThreadPoolExecutor(max_workers=VERIFY_WORKERS, thread_name_prefix='bk-verify') as ex:
        for p, match in ex.map(one, todo):
            done += 1
            if match is None:
                continue
            if match:                       # the same bytes: a touched or re-saved file is not a change
                con.execute('UPDATE f SET st = ?, verified = ? WHERE path = ?', (RANK['same'], 'match', p))
            else:
                con.execute('UPDATE f SET verified = ?, st = CASE WHEN st = ? THEN ? ELSE st END WHERE path = ?',
                            ('differ', RANK['same'], RANK['edited_local'], p))
            if progress and done % 500 == 0:
                progress(done, f'verified {done:,} of {len(todo):,} files')
    con.execute("UPDATE f SET verified = 'multipart' WHERE verified IS NULL AND ls IS NOT NULL AND cs IS NOT NULL")


def _open(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def summary(path):
    """{counts: {status: n}, total, finished_at, verify, complete} of a stored comparison."""
    with closing(_open(path)) as con:
        counts = {STATUSES[r]: n for r, n in con.execute('SELECT st, count(*) FROM f GROUP BY st').fetchall()}
        meta = dict(con.execute('SELECT k, v FROM meta').fetchall())
    return {'counts': {s: counts.get(s, 0) for s in STATUSES}, 'total': sum(counts.values()),
            'finished_at': float(meta['finished_at']) if meta.get('finished_at') else None,
            'verify': meta.get('verify') == '1', 'complete': meta.get('complete') != '0'}


def page(path, statuses=None, q='', offset=0, limit=200):
    """One page of the stored rows (by status, then path), filtered by `statuses` and a path substring `q`."""
    limit = max(1, min(int(limit or 200), MAX_PAGE))
    where, args = [], []
    sts = sorted({RANK[s] for s in (statuses or ()) if s in RANK})
    if sts:
        where.append(f'st IN ({",".join("?" * len(sts))})')
        args += sts
    if q:
        where.append("path LIKE ? ESCAPE '\\'")
        args.append('%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    w = f' WHERE {" AND ".join(where)}' if where else ''
    with closing(_open(path)) as con:
        total = con.execute(f'SELECT count(*) FROM f{w}', args).fetchone()[0]
        rows = con.execute(f'SELECT path, st, ls, lm, cs, cm, verified FROM f{w} ORDER BY st, path LIMIT ? OFFSET ?',
                           [*args, limit, max(0, int(offset or 0))]).fetchall()
    return {'matched': total, 'offset': max(0, int(offset or 0)),
            'rows': [{'path': p, 'status': STATUSES[s], 'local': {'size': a, 'mtime': b} if a is not None else None,
                      'cloud': {'size': c, 'mtime': d} if c is not None else None, **({'verified': v} if v else {})}
                     for p, s, a, b, c, d, v in rows]}
