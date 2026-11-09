"""
Result spool: a query's rows go to a Parquet file, never into memory or to the AI. The grid, statistics, charts and
tests all read this file through DuckDB, so paging, sorting and filtering never touch the user's database again.

    Spool(path).write(columns, batches, limit)   -> {rows, columns, truncated, bytes}
    page(path, offset, limit, sort, desc, q)     -> {rows, total}
    stats(path)                                  -> {column: {type, nulls, distinct, min, max, mean}}

Files live in .dataviz_cache/analysis/<user>/<session>/ and leave with the session.
"""

import datetime as dt
import decimal
import uuid
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

PAGE_MAX = 1000
NUMERIC = ('TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT', 'FLOAT', 'DOUBLE', 'DECIMAL', 'UTINYINT', 'USMALLINT',
           'UINTEGER', 'UBIGINT')


def unique_names(names):
    """Duplicate or empty column names (SELECT a.id, b.id) stay distinct in the file: id, id_2."""
    seen, out = {}, []
    for i, n in enumerate(names):
        n = str(n) if n not in (None, '') else f'column{i + 1}'
        k = n.lower()
        seen[k] = seen.get(k, 0) + 1
        out.append(n if seen[k] == 1 else f'{n}_{seen[k]}')
    return out


def _plain(v):
    """A value Arrow can store: exotic driver types (ObjectId, UUID, memoryview ...) become text."""
    if v is None or isinstance(v, (bool, int, float, str, bytes, decimal.Decimal, dt.date, dt.time)):
        return v
    if isinstance(v, memoryview):
        return bytes(v)
    return str(v)


def _column(values):
    values = [_plain(v) for v in values]
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, OverflowError):
        return pa.array([None if v is None else str(v) for v in values], type=pa.string())


class Spool:
    def __init__(self, path):
        self.path = Path(path)

    def write(self, columns, batches, limit):
        """columns: names; batches: iterable of row lists. Stops after `limit` rows (truncated: true)."""
        names = unique_names(columns)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.part')
        writer, schema, rows, truncated = None, None, 0, False
        try:
            for batch in batches:
                if not batch:
                    continue
                room = limit - rows
                if len(batch) >= room:
                    truncated = len(batch) > room
                    batch = batch[:room]
                cols = [_column(c) for c in zip(*batch)]
                table = pa.Table.from_arrays(cols, names=names)
                if writer is None:
                    schema = pa.schema([f.with_type(pa.string()) if pa.types.is_null(f.type) else f for f in table.schema])
                    writer = pq.ParquetWriter(tmp, schema, compression='zstd')
                writer.write_table(_fit(table, schema))
                rows += len(batch)
                if rows >= limit:
                    truncated = True
                    break
            if writer is None:
                pq.write_table(pa.table({n: pa.array([], type=pa.string()) for n in names}), tmp)
        finally:
            if writer is not None:
                writer.close()
        tmp.replace(self.path)
        return {'rows': rows, 'columns': names, 'truncated': truncated, 'bytes': self.path.stat().st_size}


def _fit(table, schema):
    """A later batch may infer other types (all NULL first, ints then text): cast it, or store text."""
    if table.schema.equals(schema):
        return table
    arrays = []
    for f, col in zip(schema, table.columns):
        try:
            arrays.append(col.cast(f.type))
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError):
            arrays.append(pa.chunked_array([[None] * len(col)], type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def new_path(root, user_id, session_id):
    return Path(root) / 'analysis' / str(user_id) / str(session_id) / f'{uuid.uuid4().hex}.parquet'


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _connect():
    return duckdb.connect(':memory:', config={'threads': 2})


def _names(c, path):
    return [r[0] for r in c.execute('DESCRIBE SELECT * FROM read_parquet(?)', [str(path)]).fetchall()]


def page(path, offset=0, limit=200, sort=None, desc=False, q=''):
    """One page of the saved result. sort: a column name of the file; q: text searched in every column."""
    limit = max(1, min(int(limit), PAGE_MAX))
    offset = max(0, int(offset))
    with _connect() as c:
        names = _names(c, path)
        where, args = '', [str(path)]
        if q:
            cond = ' OR '.join(f'contains(lower(CAST({_q(n)} AS VARCHAR)), ?)' for n in names)
            where = f' WHERE {cond}'
            args += [str(q).lower()] * len(names)
        order = ''
        if sort is not None:
            if sort not in names:
                raise ValueError(f'no column {sort!r}')
            order = f' ORDER BY {_q(sort)} {"DESC" if desc else "ASC"} NULLS LAST'
        total = c.execute(f'SELECT count(*) FROM read_parquet(?){where}', args).fetchone()[0]
        cur = c.execute(f'SELECT * FROM read_parquet(?){where}{order} LIMIT {limit} OFFSET {offset}', args)
        rows = [[_json(v) for v in r] for r in cur.fetchall()]
    return {'columns': names, 'rows': rows, 'total': total, 'offset': offset}


def _json(v):
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=' ') if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return f'0x{bytes(v[:32]).hex()}'
    return v


EXACT_DISTINCT = 20_000_000   # cells (rows x columns): up to here stats() counts distinct values exactly


def stats(path):
    """Per column: type, NULL count, distinct estimate, min, max and (numbers) mean, computed once by the caller."""
    with _connect() as c:
        rows = c.execute('SELECT column_name, column_type, min, max, approx_unique, avg, null_percentage, count '
                         'FROM (SUMMARIZE SELECT * FROM read_parquet(?))', [str(path)]).fetchall()
        # approx_unique is a HyperLogLog guess: 507 distinct in 500 rows, a unique id at 495. Exact counts are cheap at
        # result-spool sizes, a batch of columns per pass (one pass over 1500 columns runs out of memory); past
        # EXACT_DISTINCT cells the guess stays, capped at the non-NULL count.
        exact = None
        n = rows[0][7] if rows else 0
        if n and n * len(rows) <= EXACT_DISTINCT:
            exact = []
            for i in range(0, len(rows), 64):
                cols = ', '.join(f'count(DISTINCT {_q(r[0])})' for r in rows[i:i + 64])
                exact += c.execute(f'SELECT {cols} FROM read_parquet(?)', [str(path)]).fetchone()
    out = {}
    for i, (name, typ, mn, mx, distinct, avg, null_pct, count) in enumerate(rows):
        numeric = typ.split('(')[0] in NUMERIC
        nulls = round(float(null_pct or 0) * count / 100)
        distinct = exact[i] if exact is not None else min(int(distinct or 0), count - nulls)
        out[name] = {'type': typ, 'nulls': nulls, 'null_pct': float(null_pct or 0),
                     'distinct': int(distinct), 'min': mn, 'max': mx, 'numeric': numeric,
                     'mean': float(avg) if numeric and avg is not None else None}
    return out


def histogram(path, column, bins=12):
    """Counts for a tiny chart: equal-width bins for numbers, the most common values otherwise."""
    bins = max(2, min(int(bins), 40))
    with _connect() as c:
        col = _q(column)
        if column not in _names(c, path):
            raise ValueError(f'no column {column!r}')
        typ = c.execute(f'SELECT typeof({col}) FROM read_parquet(?) WHERE {col} IS NOT NULL LIMIT 1', [str(path)]).fetchone()
        if typ and typ[0].split('(')[0] in NUMERIC:
            lo, hi = c.execute(f'SELECT min({col})::DOUBLE, max({col})::DOUBLE FROM read_parquet(?)', [str(path)]).fetchone()
            if lo is None or lo == hi:
                n = c.execute(f'SELECT count({col}) FROM read_parquet(?)', [str(path)]).fetchone()[0]
                return {'kind': 'num', 'lo': lo, 'hi': hi, 'counts': [n]}
            step = (hi - lo) / bins
            got = dict(c.execute(f'SELECT LEAST(floor(({col}::DOUBLE - ?) / ?)::INT, ?) b, count(*) FROM read_parquet(?) '
                                 f'WHERE {col} IS NOT NULL GROUP BY b', [lo, step, bins - 1, str(path)]).fetchall())
            return {'kind': 'num', 'lo': lo, 'hi': hi, 'counts': [got.get(i, 0) for i in range(bins)]}
        rows = c.execute(f'SELECT CAST({col} AS VARCHAR) v, count(*) n FROM read_parquet(?) WHERE {col} IS NOT NULL '
                         f'GROUP BY v ORDER BY n DESC, v LIMIT {bins}', [str(path)]).fetchall()
    return {'kind': 'top', 'labels': [r[0] for r in rows], 'counts': [r[1] for r in rows]}


def duplicate_rows(path):
    with _connect() as c:
        total = c.execute('SELECT count(*) FROM read_parquet(?)', [str(path)]).fetchone()[0]
        distinct = c.execute('SELECT count(*) FROM (SELECT DISTINCT * FROM read_parquet(?))', [str(path)]).fetchone()[0]
    return total - distinct
