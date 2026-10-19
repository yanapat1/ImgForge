"""
Feature vectors in PostgreSQL (pgvector): the durable copy of every computed feature space of a dataset
version. The workspace `.npy` (ml/features.py load_cached / save_cached) stays the fast memory-mapped cache;
when it is missing it is rebuilt from here instead of re-running the GPU model.

    save(fingerprint, feature_key, X, ids=None, hnsw_min_rows=50_000) -> {id, n, dim, type, has_index}
    load(fingerprint, feature_key) -> float32 ndarray (n, dim) ordered by sample_id, or None
    load_ids(fingerprint, feature_key) -> int ndarray of the sample ids (same order as load), or None
    info(fingerprint, feature_key) -> {id, n, dim, complete, has_index} or None
    exists(fingerprint, feature_key) -> bool (a complete set is stored)
    delete(fingerprint, feature_key) -> bool (something was deleted)
    knn(fingerprint, feature_key, query_vec, k) -> (ids int64[k'], sims float32[k'])  cosine similarity, best first

Layout: one registry row in vec.feature_sets per (fingerprint, feature_key) and one table vec.emb_<id>
(sample_id int primary key, v halfvec(dim)) per set, so every column has a fixed dimension and can carry an
HNSW index. Table names are built ONLY from the integer id, never from user text.

Storage type: halfvec (fp16, half the bytes) when dim <= 16000 and every value fits fp16 (|x| <= 65504);
otherwise `vector` (fp32: box geometry in pixels squared can overflow fp16). halfvec costs ~1e-3 relative
precision, which cosine neighbours / UMAP do not notice. dim > 16000 is a ValueError (pgvector's limit).

Index: HNSW (halfvec_cosine_ops) only when n > hnsw_min_rows (config vectors.hnsw_min_rows; below that an exact
scan / numpy is faster) and dim <= 4000 (pgvector's HNSW limit for halfvec); fp32 sets are never indexed. knn()
works with or without it (`<=>` cosine distance); with an index it is approximate (ef_search = max(40, k)).

Writes go through binary COPY built with numpy (no per-row Python), all in one transaction: the registry row
is upserted with complete=false, the table (re)created / truncated, the rows copied, then complete=true with
n and dim; a reader sees the old set or the new one, never half of it. The index is built after that commit.

measured (20 000 x 1024 float32, local docker pgvector 0.8.0 / pg17): save 0.53 s, load 0.20 s, exact knn 0.1 s;
HNSW build on 10 000 x 1024 7.3 s (so 50 000+ rows: about a minute, after the data is committed), knn with it 10 ms.
"""

import struct

import numpy as np

from .. import db

HALF_MAX = 65504.0              # largest finite fp16
MAX_DIM = 16000                 # pgvector: halfvec / vector columns
HNSW_MAX_DIM = 4000             # pgvector: HNSW on halfvec
_COPY_HEADER = b'PGCOPY\n\xff\r\n\x00' + struct.pack('>ii', 0, 0)
_COPY_TRAILER = struct.pack('>h', -1)
_CHUNK = 8192                   # rows per COPY write: bounded memory for big sets


def _table(set_id):
    return f'vec.emb_{int(set_id)}'


def _row_dtype(dim, fp16):
    # one binary COPY tuple: field count, (len, int4 id), (len, pgvector recv format: dim, unused, values)
    return np.dtype([('nf', '>i2'), ('l1', '>i4'), ('id', '>i4'), ('l2', '>i4'), ('dim', '>i2'), ('unused', '>i2'),
                     ('v', '>f2' if fp16 else '>f4', (dim,))])


class _Raw:
    """The raw psycopg connection of a pooled SQLAlchemy connection (COPY needs it); back to the pool on exit."""

    def __enter__(self):
        self.pooled = db.engine().raw_connection()
        self.conn = self.pooled.driver_connection
        return self.conn

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.pooled.close()


def _registry(cur, fingerprint, feature_key):
    cur.execute('SELECT id, n, dim, complete, has_index FROM vec.feature_sets WHERE fingerprint = %s AND feature_key = %s',
                (fingerprint, feature_key))
    r = cur.fetchone()
    return None if r is None else {'id': r[0], 'n': r[1], 'dim': r[2], 'complete': r[3], 'has_index': r[4]}


def _col_type(cur, set_id):
    """'halfvec' | 'vector' | None (no table)."""
    cur.execute("SELECT t.typname FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid "
                "WHERE a.attrelid = to_regclass(%s) AND a.attname = 'v'", (_table(set_id),))
    r = cur.fetchone()
    return r[0] if r else None


def info(fingerprint, feature_key):
    with _Raw() as conn, conn.cursor() as cur:
        return _registry(cur, fingerprint, feature_key)


def exists(fingerprint, feature_key):
    r = info(fingerprint, feature_key)
    return bool(r and r['complete'])


def save(fingerprint, feature_key, X, ids=None, hnsw_min_rows=50_000):
    """Store X (n, dim) as the set (fingerprint, feature_key), replacing what was there. ids: the sample id of
    each row (default 0..n-1). Returns {id, n, dim, type, has_index}."""
    X = np.asarray(X)
    if X.ndim != 2:
        raise ValueError(f'feature vectors must be a 2-D array, got shape {X.shape}')
    n, dim = X.shape
    if not 1 <= dim <= MAX_DIM:
        raise ValueError(f'cannot store {dim}-dimensional vectors (pgvector allows 1..{MAX_DIM})')
    ids = np.arange(n, dtype=np.int64) if ids is None else np.asarray(ids, dtype=np.int64)
    if ids.shape != (n,):
        raise ValueError('ids must have one entry per row')
    X32 = X.astype(np.float32, copy=False)
    if not np.isfinite(X32).all():
        raise ValueError('feature vectors contain NaN or infinite values')
    fp16 = n == 0 or float(np.abs(X32).max()) <= HALF_MAX
    typ = 'halfvec' if fp16 else 'vector'
    with _Raw() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO vec.feature_sets(fingerprint, feature_key, dim, n, complete, has_index)
                       VALUES (%s, %s, %s, %s, false, false)
                       ON CONFLICT (fingerprint, feature_key)
                       DO UPDATE SET dim = EXCLUDED.dim, n = EXCLUDED.n, complete = false, has_index = false,
                                     created_at = now()
                       RETURNING id""", (fingerprint, feature_key, dim, n))
        set_id = cur.fetchone()[0]
        t = _table(set_id)
        cur.execute(f'DROP TABLE IF EXISTS {t}')        # dim / type / index may differ from the old set
        cur.execute(f'CREATE TABLE {t} (sample_id integer PRIMARY KEY, v {typ}({dim}) NOT NULL)')
        dt = _row_dtype(dim, fp16)
        with cur.copy(f'COPY {t} (sample_id, v) FROM STDIN WITH (FORMAT BINARY)') as cp:
            cp.write(_COPY_HEADER)
            for a in range(0, n, _CHUNK):
                b = min(n, a + _CHUNK)
                rows = np.empty(b - a, dtype=dt)
                rows['nf'], rows['l1'], rows['l2'] = 2, 4, 4 + dim * (2 if fp16 else 4)
                rows['id'] = ids[a:b]
                rows['dim'], rows['unused'] = dim, 0
                rows['v'] = X32[a:b]
                cp.write(rows.tobytes())
            cp.write(_COPY_TRAILER)
        cur.execute('UPDATE vec.feature_sets SET complete = true, n = %s, dim = %s WHERE id = %s', (n, dim, set_id))
    has_index = False
    if fp16 and n > hnsw_min_rows and dim <= HNSW_MAX_DIM:
        # after the commit: the data is usable (exact scans) while the index builds
        with _Raw() as conn, conn.cursor() as cur:
            cur.execute(f'CREATE INDEX IF NOT EXISTS emb_{int(set_id)}_hnsw ON {t} USING hnsw (v halfvec_cosine_ops)')
            cur.execute('UPDATE vec.feature_sets SET has_index = true WHERE id = %s', (set_id,))
        has_index = True
    return {'id': set_id, 'n': n, 'dim': dim, 'type': typ, 'has_index': has_index}


def _read(fingerprint, feature_key):
    with _Raw() as conn, conn.cursor() as cur:
        reg = _registry(cur, fingerprint, feature_key)
        if reg is None or not reg['complete']:
            return None
        typ = _col_type(cur, reg['id'])
        if typ is None:
            return None
        buf = bytearray()
        with cur.copy(f"COPY (SELECT sample_id, v FROM {_table(reg['id'])} ORDER BY sample_id) "
                      'TO STDOUT WITH (FORMAT BINARY)') as cp:
            for chunk in cp:
                buf += chunk
    dim = reg['dim']
    dt = _row_dtype(dim, typ == 'halfvec')
    if len(buf) < len(_COPY_HEADER) + 2:
        return None
    ext = struct.unpack_from('>i', buf, 15)[0]                  # header extension area (0 today)
    off = len(_COPY_HEADER) + ext
    n = (len(buf) - off - 2) // dt.itemsize
    rows = np.frombuffer(buf, dtype=dt, count=n, offset=off)
    return rows['id'].astype(np.int64), rows['v'].astype(np.float32)


def load(fingerprint, feature_key):
    """The stored vectors as float32 (n, dim) ordered by sample_id; None when absent or not complete."""
    r = _read(fingerprint, feature_key)
    return None if r is None else r[1]


def load_ids(fingerprint, feature_key):
    r = _read(fingerprint, feature_key)
    return None if r is None else r[0]


def delete(fingerprint, feature_key):
    with _Raw() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM vec.feature_sets WHERE fingerprint = %s AND feature_key = %s RETURNING id',
                    (fingerprint, feature_key))
        r = cur.fetchone()
        if r is None:
            return False
        cur.execute(f'DROP TABLE IF EXISTS {_table(r[0])}')
    return True


def knn(fingerprint, feature_key, query_vec, k):
    """The k nearest stored rows to query_vec by cosine: (sample ids, cosine similarities), best first. Empty
    arrays when the set is absent / incomplete."""
    q = np.asarray(query_vec, dtype=np.float32).ravel()
    k = max(0, int(k))
    with _Raw() as conn, conn.cursor() as cur:
        reg = _registry(cur, fingerprint, feature_key)
        if reg is None or not reg['complete'] or k == 0:
            return np.empty(0, np.int64), np.empty(0, np.float32)
        if q.shape[0] != reg['dim']:
            raise ValueError(f"query has {q.shape[0]} dimensions, the set has {reg['dim']}")
        typ = _col_type(cur, reg['id'])
        if reg['has_index']:
            cur.execute(f'SET LOCAL hnsw.ef_search = {min(1000, max(40, k))}')     # ef_search caps the result count
        lit = '[' + ','.join(repr(float(x)) for x in q) + ']'
        cur.execute(f"SELECT sample_id, 1 - (v <=> %s::{typ}({reg['dim']})) FROM {_table(reg['id'])} "
                    f"ORDER BY v <=> %s::{typ}({reg['dim']}) LIMIT %s", (lit, lit, k))
        rows = cur.fetchall()
    return (np.array([r[0] for r in rows], dtype=np.int64),
            np.array([r[1] for r in rows], dtype=np.float32))
