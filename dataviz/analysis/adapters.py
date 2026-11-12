"""
One interface over the user's databases. Every adapter is one open connection, used by one run at a time.

    adapter(conn, database=None)   -> PostgresAdapter | MssqlAdapter | MongoAdapter; conn = a saved connection (with secret)
    .databases()                   -> [name]
    .tables()                      -> [{schema, name, kind}]   kind: table, view, system, file, external, graph
    .describe(schema, name)        -> {columns: [{name, type, nullable, key, description}], description, rows, keys}
    .run(statement, chunk, read)   -> (columns, batches): streams rows, so a big result never sits in memory
    .explain(statement)            -> {nodes: [{label, kind, cost, rows}], total, estimated}
    .set_description(...)          writes a description back to the source database
    .cancel()                      stops the running statement from another thread
    .close()

Nothing here decides what may run: classify.py does, before an adapter is touched.
"""

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter

CHUNK = 5000
TIMEOUT_S = 10


class Unsupported(Exception):
    """This database cannot do that."""


def _quote_mssql(n):
    return '[' + str(n).replace(']', ']]') + ']'


def _quote_pg(n):
    return '"' + str(n).replace('"', '""') + '"'


# ---------------------------------------------------------------- PostgreSQL
class PostgresAdapter:
    dialect = 'postgres'

    def __init__(self, conn, database=None, statement_timeout_s=0):
        import psycopg
        self.conn_cfg = conn
        self.database = database or conn.get('database') or 'postgres'
        self.c = psycopg.connect(host=conn.get('host') or 'localhost', port=int(conn.get('port') or 5432),
                                 dbname=self.database, user=conn.get('username') or None,
                                 password=conn.get('secret_key') or None, connect_timeout=TIMEOUT_S, autocommit=True,
                                 sslmode='require' if conn.get('encrypt') else 'prefer', application_name='dataviz-analysis')
        if statement_timeout_s:
            self.c.execute(f'SET statement_timeout = {int(statement_timeout_s) * 1000}')

    def version(self):
        return self.c.execute('SHOW server_version').fetchone()[0]

    def databases(self):
        rows = self.c.execute("SELECT datname FROM pg_database WHERE NOT datistemplate AND has_database_privilege(datname, "
                              "'CONNECT') ORDER BY datname").fetchall()
        return [r[0] for r in rows]

    def tables(self):
        rows = self.c.execute("""
            SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND n.nspname !~ '^pg_toast'
              AND has_schema_privilege(n.oid, 'USAGE') ORDER BY n.nspname, c.relname""").fetchall()
        out = []
        for schema, name, kind in rows:
            system = schema in ('pg_catalog', 'information_schema')
            out.append({'schema': schema, 'name': name, 'kind': 'system' if system else {'v': 'view', 'm': 'view',
                                                                                         'f': 'external'}.get(kind, 'table')})
        return out

    def describe(self, schema, name):
        rel = f'{_quote_pg(schema)}.{_quote_pg(name)}'
        oid = self.c.execute('SELECT %s::regclass::oid', [rel]).fetchone()[0]
        cols = self.c.execute("""
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, col_description(a.attrelid, a.attnum)
            FROM pg_attribute a WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
                              [oid]).fetchall()
        keys = dict(self.c.execute("""
            SELECT a.attname, CASE c.contype WHEN 'p' THEN 'PK' ELSE 'FK' END FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
            WHERE c.conrelid = %s AND c.contype IN ('p', 'f') ORDER BY c.contype DESC""", [oid]).fetchall())
        fks = self.c.execute("""
            SELECT a.attname, fn.nspname, f.relname, fa.attname FROM pg_constraint c
            JOIN pg_class f ON f.oid = c.confrelid JOIN pg_namespace fn ON fn.oid = f.relnamespace
            JOIN LATERAL unnest(c.conkey, c.confkey) AS k(col, fcol) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.col
            JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = k.fcol
            WHERE c.conrelid = %s AND c.contype = 'f'""", [oid]).fetchall()
        # columns unique on their own (PK, UNIQUE, a one-column unique index without WHERE / expression): the builder's 1 vs N
        uniq = [r[0] for r in self.c.execute("""
            SELECT DISTINCT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
            WHERE i.indrelid = %s AND i.indisunique AND i.indnatts = 1 AND i.indpred IS NULL AND i.indkey[0] <> 0""", [oid]).fetchall()]
        info = self.c.execute('SELECT obj_description(%s), reltuples FROM pg_class WHERE oid = %s', [oid, oid]).fetchone()
        return {'columns': [{'name': n, 'type': t, 'nullable': nl, 'key': keys.get(n, ''), 'description': d or ''}
                            for n, t, nl, d in cols],
                'description': info[0] or '', 'rows': max(int(info[1] or 0), 0), 'unique': uniq,
                'keys': [{'column': a, 'schema': b, 'table': c, 'to': d} for a, b, c, d in fks]}

    def run(self, statement, chunk=CHUNK, read=True):
        if not read:
            cur = self.c.execute(statement)
            return self._all(cur)
        self.c.execute('BEGIN TRANSACTION READ ONLY')     # second guard: the database itself refuses a write
        cur = self.c.cursor(name='dataviz_run', scrollable=False)
        cur.itersize = chunk
        try:
            cur.execute(statement)
        except Exception:
            self.c.execute('ROLLBACK')
            raise
        first = cur.fetchmany(chunk)
        columns = [d.name for d in cur.description or []]

        def batches():
            try:
                rows = first
                while rows:
                    yield [list(r) for r in rows]
                    rows = cur.fetchmany(chunk)
            finally:
                cur.close()
                self.c.execute('ROLLBACK')
        return columns, batches()

    @staticmethod
    def _all(cur):
        if cur.description is None:
            return [], iter(())
        return [d.name for d in cur.description], iter([[list(r) for r in cur.fetchall()]])

    def explain(self, statement):
        self.c.execute('BEGIN TRANSACTION READ ONLY')
        try:
            plan = self.c.execute(f'EXPLAIN (FORMAT JSON) {statement}').fetchone()[0]
        finally:
            self.c.execute('ROLLBACK')
        if isinstance(plan, str):
            plan = json.loads(plan)
        nodes = []

        def walk(n):
            kids = n.get('Plans') or []
            own = max(float(n.get('Total Cost') or 0) - sum(float(k.get('Total Cost') or 0) for k in kids), 0)
            rel = n.get('Relation Name') or n.get('Index Name') or ''
            cond = n.get('Hash Cond') or n.get('Merge Cond') or n.get('Join Filter') or n.get('Filter') or ''
            nodes.append({'label': ' '.join(x for x in (n.get('Node Type'), rel, cond) if x), 'kind': n.get('Node Type'),
                          'cost': own, 'rows': n.get('Plan Rows')})
            for k in kids:
                walk(k)
        walk(plan[0]['Plan'])
        return {'nodes': nodes, 'total': sum(x['cost'] for x in nodes), 'estimated': True}

    def set_description(self, schema, name, column, text):
        target = f'{_quote_pg(schema)}.{_quote_pg(name)}'
        what = f'COLUMN {target}.{_quote_pg(column)}' if column else f'TABLE {target}'
        self.c.execute(f'COMMENT ON {what} IS %s'.replace('%s', self._literal(text)))

    def _literal(self, text):
        from psycopg import sql
        return sql.Literal(text).as_string(self.c)

    def cancel(self):
        self.c.cancel()

    def close(self):
        self.c.close()


# ---------------------------------------------------------------- SQL Server
class MssqlAdapter:
    dialect = 'mssql'

    def __init__(self, conn, database=None, statement_timeout_s=0):
        import pymssql
        self.conn_cfg = conn
        self.database = database or conn.get('database') or 'master'
        self.c = pymssql.connect(server=conn.get('host') or 'localhost', port=str(conn.get('port') or 1433),
                                 user=conn.get('username') or None, password=conn.get('secret_key') or None,
                                 database=self.database, login_timeout=TIMEOUT_S, timeout=int(statement_timeout_s or 0),
                                 appname='dataviz-analysis', autocommit=True, charset='UTF-8')

    def _q(self, sql, *args):
        cur = self.c.cursor()
        cur.execute(sql, args or None)
        return cur.fetchall()

    def version(self):
        return self._q("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(40))")[0][0]

    def databases(self):
        return [r[0] for r in self._q("SELECT name FROM sys.databases WHERE state = 0 AND HAS_DBACCESS(name) = 1 ORDER BY name")]

    def tables(self):
        rows = self._q("""
            SELECT s.name, t.name, t.is_ms_shipped, t.is_filetable, t.is_external, t.is_node, t.is_edge, 'U'
            FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            UNION ALL
            SELECT s.name, v.name, v.is_ms_shipped, 0, 0, 0, 0, 'V' FROM sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id
            ORDER BY 1, 2""")
        out = []
        for schema, name, shipped, file_, ext, node, edge, kind in rows:
            k = 'system' if shipped else 'view' if kind == 'V' else 'file' if file_ else 'external' if ext else \
                'graph' if node or edge else 'table'
            out.append({'schema': schema, 'name': name, 'kind': k})
        return out

    def describe(self, schema, name):
        oid = self._q('SELECT OBJECT_ID(%s)', f'{_quote_mssql(schema)}.{_quote_mssql(name)}')[0][0]
        if oid is None:
            raise ValueError(f'no table {schema}.{name}')
        cols = self._q("""
            SELECT c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable, CAST(p.value AS nvarchar(4000))
            FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            LEFT JOIN sys.extended_properties p ON p.major_id = c.object_id AND p.minor_id = c.column_id
                 AND p.class = 1 AND p.name = 'MS_Description'
            WHERE c.object_id = %s ORDER BY c.column_id""", oid)
        pk = {r[0] for r in self._q("""
            SELECT c.name FROM sys.indexes i JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.is_primary_key = 1 AND i.object_id = %s""", oid)}
        fks = self._q("""
            SELECT pc.name, OBJECT_SCHEMA_NAME(fk.referenced_object_id), OBJECT_NAME(fk.referenced_object_id), rc.name
            FROM sys.foreign_key_columns fk
            JOIN sys.columns pc ON pc.object_id = fk.parent_object_id AND pc.column_id = fk.parent_column_id
            JOIN sys.columns rc ON rc.object_id = fk.referenced_object_id AND rc.column_id = fk.referenced_column_id
            WHERE fk.parent_object_id = %s""", oid)
        fk_cols = {r[0] for r in fks}
        uniq = [r[0] for r in self._q("""
            SELECT MIN(c.name) FROM sys.indexes i JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.object_id = %s AND i.is_unique = 1 AND i.has_filter = 0 AND ic.is_included_column = 0
            GROUP BY i.index_id HAVING COUNT(*) = 1""", oid)]
        note = self._q("""SELECT CAST(value AS nvarchar(4000)) FROM sys.extended_properties
                          WHERE major_id = %s AND minor_id = 0 AND class = 1 AND name = 'MS_Description'""", oid)
        rows = self._q('SELECT SUM(rows) FROM sys.partitions WHERE object_id = %s AND index_id IN (0, 1)', oid)

        def typ(t, length, prec, scale):
            if t in ('varchar', 'char', 'varbinary', 'binary'):
                return f'{t}({"max" if length == -1 else length})'
            if t in ('nvarchar', 'nchar'):
                return f'{t}({"max" if length == -1 else length // 2})'
            return f'{t}({prec},{scale})' if t in ('decimal', 'numeric') else t
        return {'columns': [{'name': n, 'type': typ(t, ln, p, s), 'nullable': bool(nl),
                             'key': 'PK' if n in pk else 'FK' if n in fk_cols else '', 'description': d or ''}
                            for n, t, ln, p, s, nl, d in cols],
                'description': (note[0][0] if note else '') or '', 'rows': int((rows[0][0] if rows else 0) or 0),
                'unique': sorted(set(uniq)), 'keys': [{'column': a, 'schema': b, 'table': c, 'to': d} for a, b, c, d in fks]}

    def run(self, statement, chunk=CHUNK, read=True):
        cur = self.c.cursor()
        cur.execute(statement)
        while cur.description is None and cur.nextset():
            pass
        if cur.description is None:
            return [], iter(())
        columns = [d[0] for d in cur.description]

        def batches():
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    return
                yield [list(r) for r in rows]
        return columns, batches()

    def explain(self, statement):
        cur = self.c.cursor()
        cur.execute('SET SHOWPLAN_XML ON')
        try:
            cur.execute(statement)
            xml = cur.fetchall()[0][0]
        finally:
            cur.execute('SET SHOWPLAN_XML OFF')
        return plan_nodes(xml)

    def set_description(self, schema, name, column, text):
        args = ['MS_Description', text, 'SCHEMA', schema, 'TABLE', name] + (['COLUMN', column] if column else [])
        marks = ', '.join(['%s'] * len(args))
        names = '@name, @value, @level0type, @level0name, @level1type, @level1name' + (', @level2type, @level2name' if column else '')
        declared = ', '.join(f'{n} = {m}' for n, m in zip(names.split(', '), marks.split(', ')))
        target = 'sp_updateextendedproperty' if self._has_note(schema, name, column) else 'sp_addextendedproperty'
        self.c.cursor().execute(f'EXEC sys.{target} {declared}', tuple(args))

    def _has_note(self, schema, name, column):
        q = f'{_quote_mssql(schema)}.{_quote_mssql(name)}'
        return bool(self._q("""SELECT 1 FROM sys.extended_properties p WHERE p.major_id = OBJECT_ID(%s) AND p.class = 1
                               AND p.name = 'MS_Description' AND p.minor_id = ISNULL(COLUMNPROPERTY(OBJECT_ID(%s), %s, 'ColumnId'), 0)""",
                            q, q, column or ''))

    def cancel(self):
        self.c._conn.cancel()

    def close(self):
        self.c.close()


_SHOWPLAN = '{http://schemas.microsoft.com/sqlserver/2004/07/showplan}'


def plan_nodes(xml):
    """Showplan XML -> each operator's own share: its subtree cost minus its children's."""
    root = ET.fromstring(xml)
    nodes = []

    def kids(op):
        found, stack = [], list(op)
        while stack:
            e = stack.pop()
            if e.tag == f'{_SHOWPLAN}RelOp':
                found.append(e)
            else:
                stack.extend(list(e))
        return found

    def walk(op):
        under = kids(op)
        total = float(op.get('EstimatedTotalSubtreeCost') or 0)
        own = max(total - sum(float(k.get('EstimatedTotalSubtreeCost') or 0) for k in under), 0)
        obj = next((e for e in op.iter(f'{_SHOWPLAN}Object')), None) if not under else None
        rel = obj.get('Table', '').strip('[]') if obj is not None else ''
        nodes.append({'label': ' '.join(x for x in (op.get('LogicalOp') or op.get('PhysicalOp'), rel) if x),
                      'kind': op.get('PhysicalOp'), 'cost': own, 'rows': float(op.get('EstimateRows') or 0)})
        for k in under:
            walk(k)

    for top in root.iter(f'{_SHOWPLAN}QueryPlan'):
        for op in top:
            if op.tag == f'{_SHOWPLAN}RelOp':
                walk(op)
    return {'nodes': nodes, 'total': sum(x['cost'] for x in nodes), 'estimated': True}


# ---------------------------------------------------------------- MongoDB
SAMPLE = 1000


def _flat(doc, prefix=''):
    """Nested documents become dotted columns; arrays stay one JSON cell."""
    out = {}
    for k, v in doc.items():
        name = f'{prefix}{k}'
        if isinstance(v, dict):
            out.update(_flat(v, f'{name}.'))
        else:
            out[name] = json.dumps(v, default=str) if isinstance(v, list) else v
    return out


class MongoAdapter:
    dialect = 'mongo'

    def __init__(self, conn, database=None, statement_timeout_s=0):
        import pymongo
        self.conn_cfg = conn
        self.database = database or conn.get('database') or None
        kw = {'username': conn.get('username') or None, 'password': conn.get('secret_key') or None,
              'serverSelectionTimeoutMS': TIMEOUT_S * 1000, 'appname': 'dataviz-analysis', 'tls': bool(conn.get('encrypt'))}
        self.c = pymongo.MongoClient(conn.get('host') or 'localhost', int(conn.get('port') or 27017), **kw)
        self.max_ms = int(statement_timeout_s or 0) * 1000
        self.cursor = None
        self.stopped = threading.Event()

    def version(self):
        return self.c.server_info()['version']

    def _db(self):
        if not self.database:
            raise ValueError('choose a database first')
        return self.c[self.database]

    def databases(self):
        return sorted(d for d in self.c.list_database_names() if d not in ('admin', 'config', 'local'))

    def tables(self):
        return [{'schema': '', 'name': n, 'kind': 'table'} for n in sorted(self._db().list_collection_names())]

    def describe(self, schema, name):
        col = self._db()[name]
        seen, types, total = Counter(), {}, 0
        for doc in col.aggregate([{'$sample': {'size': SAMPLE}}]):
            total += 1
            for k, v in _flat(doc).items():
                seen[k] += 1
                types.setdefault(k, Counter())[type(v).__name__] += 1
        columns = [{'name': k, 'type': types[k].most_common(1)[0][0], 'nullable': seen[k] < total,
                    'key': 'PK' if k == '_id' else '', 'description': '', 'present': round(seen[k] / max(total, 1), 3)}
                   for k in seen]
        return {'columns': columns, 'description': '', 'rows': col.estimated_document_count(), 'keys': [], 'sampled': total,
                'unique': ['_id']}

    def run(self, statement, chunk=CHUNK, read=True):
        """statement: the command dict from classify.parse_mongo."""
        col = self._db()[statement['collection']]
        method, args, mods = statement['method'], statement['args'], statement.get('modifiers') or {}
        if method in ('find', 'findOne'):
            cur = col.find(args[0] if args else {}, args[1] if len(args) > 1 else None, max_time_ms=self.max_ms or None)
            if method == 'findOne':
                cur = cur.limit(1)
            for k in ('sort', 'skip', 'limit'):
                if k in mods:
                    cur = getattr(cur, k)(list(mods[k].items()) if k == 'sort' else mods[k])
        elif method == 'aggregate':
            kw = {'maxTimeMS': self.max_ms} if self.max_ms else {}
            cur = col.aggregate(args[0] if args else [], **kw)
        elif method in ('countDocuments', 'count'):
            return ['count'], iter([[[col.count_documents(args[0] if args else {})]]])
        elif method == 'estimatedDocumentCount':
            return ['count'], iter([[[col.estimated_document_count()]]])
        elif method == 'distinct':
            return [str(args[0])], iter([[[v] for v in col.distinct(args[0], *args[1:])]])
        else:
            return self._change(col, method, args)
        self.cursor = cur
        first = list(_take(cur, min(chunk, SAMPLE)))
        rows = [_flat(d) for d in first]
        columns = list(dict.fromkeys(k for r in rows for k in r))

        def batches():
            page = rows
            while page:
                yield [[r.get(c) for c in columns] for r in page]
                if self.stopped.is_set():
                    return
                page = [_flat(d) for d in _take(cur, chunk)]
        return columns, batches()

    @staticmethod
    def _change(col, method, args):
        """A confirmed write: the driver's own method by name; the result is one row."""
        fn = getattr(col, re.sub(r'[A-Z]', lambda m: '_' + m.group(0).lower(), method), None)
        if fn is None:
            raise Unsupported(f'{method} is not a collection method')
        res = fn(*args)
        info = {k: getattr(res, k) for k in ('matched_count', 'modified_count', 'deleted_count', 'inserted_id')
                if hasattr(res, k)}
        return list(info) or ['ok'], iter([[list(info.values()) or [True]]])

    def explain(self, statement):
        col = statement['collection']
        method = statement['method']
        if method == 'aggregate':
            cmd = {'aggregate': col, 'pipeline': statement['args'][0] if statement['args'] else [], 'cursor': {}}
        elif method in ('find', 'findOne'):
            cmd = {'find': col, 'filter': statement['args'][0] if statement['args'] else {}}
        else:
            raise Unsupported(f'{method} has no plan')
        got = self._db().command('explain', cmd, verbosity='executionStats')
        nodes = []

        def walk(stage):
            if not isinstance(stage, dict):
                return
            ms = stage.get('executionTimeMillisEstimate') or 0
            kids = [stage.get('inputStage')] + list(stage.get('inputStages') or [])
            kids = [k for k in kids if k]
            own = max(ms - sum(k.get('executionTimeMillisEstimate') or 0 for k in kids), 0)
            nodes.append({'label': ' '.join(x for x in (stage.get('stage'), stage.get('indexName')) if x), 'kind': stage.get('stage'),
                          'cost': own, 'rows': stage.get('nReturned')})
            for k in kids:
                walk(k)
        walk((got.get('executionStats') or {}).get('executionStages') or {})
        return {'nodes': nodes, 'total': sum(x['cost'] for x in nodes), 'estimated': False}

    def set_description(self, schema, name, column, text):
        raise Unsupported('MongoDB has no place for column descriptions: they stay in this app')

    def cancel(self):
        self.stopped.set()
        if self.cursor is not None:
            self.cursor.close()

    def close(self):
        self.c.close()


def _take(cur, n):
    for i, doc in enumerate(cur):
        yield doc
        if i + 1 >= n:
            return


ADAPTERS = {'postgres': PostgresAdapter, 'mssql': MssqlAdapter, 'mongo': MongoAdapter}


def ping(conn):
    """The connection form's Test button: {ok, ms, server_version}. Raises on a connection or login error."""
    t0 = time.time()
    a = adapter(conn)
    try:
        return {'ok': True, 'ms': round((time.time() - t0) * 1000), 'server_version': a.version()}
    finally:
        a.close()


def adapter(conn, database=None, statement_timeout_s=0):
    cls = ADAPTERS.get(conn.get('kind'))
    if cls is None:
        raise ValueError(f'a {conn.get("kind")} connection cannot be queried')
    return cls(conn, database, statement_timeout_s)
