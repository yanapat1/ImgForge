"""
Embeddings folders between their three layouts (the Data Center's Convert… on a bucket detected as embeddings):

    npy       meta.csv + <space>.npy (N, D) per feature space    the app's embeddings folder (data/README.md)
    parquet   embeddings.parquet: the meta columns + one list<float32> column per space (needs pyarrow)
    csv       embeddings.csv: the meta columns + <space>_0 .. <space>_<D-1> per space (wide; fine up to ~1M cells)

Not a Writer: it reads a folder, not annotation rows. Every layout converts into every other; the source folder is
left as it is and the result goes into a new folder. Rows stream in blocks of BLOCK, so memory stays flat (the
.npy files are memory-mapped).
"""

import csv
import os
import re

import numpy as np

LAYOUTS = {
    'npy': {'label': 'npy + meta.csv', 'layout': '├── meta.csv\n└── <space>.npy        (N, D) float32, one per feature space'},
    'parquet': {'label': 'Parquet', 'layout': '└── embeddings.parquet  meta columns + one list<float32> column per space'},
    'csv': {'label': 'CSV (wide)', 'layout': '└── embeddings.csv     meta columns + <space>_0 … <space>_<D-1>'},
}
BLOCK = 4096
_WIDE = re.compile(r'^(.+)_(\d+)$')


def _pq():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        raise ValueError('Parquet needs pyarrow (pip install pyarrow): pick npy or CSV instead') from None
    return pa, pq


def detect(folder):
    """The layout of `folder`: 'npy', 'parquet', 'csv' or None."""
    names = set(os.listdir(folder))
    if ({'meta.csv', 'meta.parquet'} & names) and any(n.endswith('.npy') for n in names):
        return 'npy'
    if 'embeddings.parquet' in names:
        return 'parquet'
    if 'embeddings.csv' in names:
        return 'csv'
    return None


# ---------------------------------------------------------------- reading: (meta columns, meta rows, {space: array})
def _read_npy(folder):
    if os.path.isfile(os.path.join(folder, 'meta.csv')):
        with open(os.path.join(folder, 'meta.csv'), newline='', encoding='utf-8-sig') as f:
            rd = csv.reader(f)
            cols = next(rd, [])
            rows = list(rd)
    else:
        _, pq = _pq()
        t = pq.read_table(os.path.join(folder, 'meta.parquet'))
        cols = list(t.column_names)
        d = t.to_pydict()
        rows = [['' if d[c][i] is None else str(d[c][i]) for c in cols] for i in range(t.num_rows)]
    spaces = {}
    for n in sorted(os.listdir(folder)):
        if n.endswith('.npy'):
            a = np.load(os.path.join(folder, n), mmap_mode='r')
            if a.ndim != 2:
                raise ValueError(f'{n} is not a (N, D) array')
            if len(a) != len(rows):
                raise ValueError(f'{n} has {len(a)} rows, meta has {len(rows)}')
            spaces[n[:-4]] = a
    return cols, rows, spaces


def _split_wide(cols):
    """(meta column indexes, {space: [column index of dim 0..D-1]}) of a wide CSV header."""
    groups = {}
    for i, c in enumerate(cols):
        m = _WIDE.match(c)
        if m:
            groups.setdefault(m.group(1), {})[int(m.group(2))] = i
    spaces = {s: [g[k] for k in range(len(g))] for s, g in groups.items()
              if len(g) >= 2 and set(g) == set(range(len(g)))}
    used = {i for v in spaces.values() for i in v}
    return [i for i in range(len(cols)) if i not in used], spaces


def _read_csv(folder):
    with open(os.path.join(folder, 'embeddings.csv'), newline='', encoding='utf-8-sig') as f:
        rd = csv.reader(f)
        head = next(rd, [])
        meta_ix, sp_ix = _split_wide(head)
        if not sp_ix:
            raise ValueError('embeddings.csv has no vector columns (<space>_0, <space>_1, …)')
        rows, vecs = [], {s: [] for s in sp_ix}
        for r in rd:
            rows.append([r[i] for i in meta_ix])
            for s, ix in sp_ix.items():
                vecs[s].append([float(r[i] or 'nan') for i in ix])
    return [head[i] for i in meta_ix], rows, {s: np.asarray(v, np.float32) for s, v in vecs.items()}


def _read_parquet(folder):
    pa, pq = _pq()
    t = pq.read_table(os.path.join(folder, 'embeddings.parquet'))
    cols, spaces = [], {}
    for name in t.column_names:
        col = t.column(name)
        if pa.types.is_list(col.type) or pa.types.is_fixed_size_list(col.type) or pa.types.is_large_list(col.type):
            spaces[name] = np.asarray(col.to_pylist(), np.float32)
        else:
            cols.append(name)
    if not spaces:
        raise ValueError('embeddings.parquet has no list (vector) columns')
    d = t.select(cols).to_pydict()
    rows = [['' if d[c][i] is None else str(d[c][i]) for c in cols] for i in range(t.num_rows)]
    return cols, rows, spaces


READERS = {'npy': _read_npy, 'csv': _read_csv, 'parquet': _read_parquet}


# ---------------------------------------------------------------- writing
def _write_npy(dest, cols, rows, spaces, progress):
    with open(os.path.join(dest, 'meta.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    for k, (s, a) in enumerate(spaces.items()):
        out = np.lib.format.open_memmap(os.path.join(dest, f'{s}.npy'), 'w+', np.float32, a.shape)
        for i in range(0, len(a), BLOCK):
            out[i:i + BLOCK] = a[i:i + BLOCK]
        out.flush()
        del out
        progress(k + 1)
    return ['meta.csv'] + [f'{s}.npy' for s in spaces]


def _write_csv(dest, cols, rows, spaces, progress):
    head = list(cols) + [f'{s}_{i}' for s, a in spaces.items() for i in range(a.shape[1])]
    with open(os.path.join(dest, 'embeddings.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(head)
        for i0 in range(0, len(rows), BLOCK):
            blocks = [np.asarray(a[i0:i0 + BLOCK], np.float32) for a in spaces.values()]
            for j, r in enumerate(rows[i0:i0 + BLOCK]):
                w.writerow(list(r) + [repr(float(v)) for b in blocks for v in b[j]])
            progress(min(len(rows), i0 + BLOCK))
    return ['embeddings.csv']


def _write_parquet(dest, cols, rows, spaces, progress):
    pa, pq = _pq()
    fields = [pa.field(c, pa.string()) for c in cols] + \
        [pa.field(s, pa.list_(pa.float32(), a.shape[1])) for s, a in spaces.items()]
    schema = pa.schema(fields)
    with pq.ParquetWriter(os.path.join(dest, 'embeddings.parquet'), schema) as w:
        for i0 in range(0, len(rows), BLOCK):
            part = rows[i0:i0 + BLOCK]
            arrs = [pa.array([r[k] for r in part], pa.string()) for k in range(len(cols))]
            for s, a in spaces.items():
                b = np.ascontiguousarray(a[i0:i0 + BLOCK], np.float32)
                arrs.append(pa.FixedSizeListArray.from_arrays(pa.array(b.ravel()), a.shape[1]))
            w.write_table(pa.Table.from_arrays(arrs, schema=schema))
            progress(min(len(rows), i0 + BLOCK))
    return ['embeddings.parquet']


WRITERS = {'npy': _write_npy, 'csv': _write_csv, 'parquet': _write_parquet}


def convert(src, dest, to, progress=None):
    """Convert the embeddings folder `src` into `dest` (created; must not exist) in layout `to`.
    -> {from, to, rows, spaces: {name: dim}, files}."""
    if to not in WRITERS:
        raise ValueError(f'unknown layout {to!r} (one of {", ".join(WRITERS)})')
    frm = detect(src)
    if frm is None:
        raise ValueError('not an embeddings folder: meta.csv + .npy files, embeddings.parquet or embeddings.csv')
    if frm == to:
        raise ValueError(f'the folder is already {LAYOUTS[to]["label"]}')
    cols, rows, spaces = READERS[frm](src)
    os.makedirs(dest)
    files = WRITERS[to](dest, cols, rows, spaces, progress or (lambda n: None))
    return {'from': frm, 'to': to, 'rows': len(rows), 'spaces': {s: int(a.shape[1]) for s, a in spaces.items()},
            'files': files}
