"""
Database export: copy a labeling session into PostgreSQL or MSSQL, into the same global schema the local
SQLite store uses (schema.make_metadata(table_prefix, schema)), so an export is a row copy, never a
translation.

    engine_for(conn)        postgresql+psycopg:// | mssql+pymssql:// (URL.create escapes the credentials)
    test(conn)              SELECT 1 + whether the tables exist and their schema_version
    export_to_db(...)       ensure_schema, then upsert project -> tasks -> classes -> media -> annotations
                            (soft-deleted ones too, is_deleted=1, so the remote mirrors deletes) -> statuses,
                            one export_runs row per task, optional per-task views <prefix>v_<task_slug>

Upserts are idempotent (keyed on the primary key; running twice gives the same rows):
    PostgreSQL  INSERT .. ON CONFLICT (pk) DO UPDATE, executemany in 1000-row batches
    MSSQL       multi-row INSERT into #stage_<table> (chunked under the 2100-parameter cap), then one MERGE
                per table and task (pymssql has no fast_executemany: row-by-row executemany is ~50x slower)
"""

import re
import time
import uuid
from datetime import datetime, timezone

import orjson
from sqlalchemy import Boolean, DateTime, create_engine, inspect, or_, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL

from ..core import logging as vlog
from . import schema as S

BATCH = 1000
MSSQL_PARAMS = 2000                     # stay under MSSQL's 2100 parameters per statement
DEFAULT_PREFIX = 'lbl_'
_log = vlog.get_logger('db')


# ---------------------------------------------------------------- engine

def _kind(conn):
    k = (conn.get('kind') or '').lower()
    if k in ('postgres', 'postgresql', 'pg'):
        return 'postgres'
    if k in ('mssql', 'sqlserver'):
        return 'mssql'
    raise ValueError(f'not a database connection: {k or "s3"}')


def _prefix(conn):
    p = conn.get('table_prefix')
    p = DEFAULT_PREFIX if p is None else str(p)
    if p and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,30}', p):
        raise ValueError('table prefix may only hold letters, digits and _')
    return p


def _schema(conn):
    s = (conn.get('schema') or '').strip()
    if s and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', s):
        raise ValueError('schema may only hold letters, digits and _')
    return s or None


def engine_for(conn):
    kind = _kind(conn)
    common = dict(username=conn.get('username') or None, password=conn.get('secret_key') or None,
                  host=conn.get('host') or 'localhost', database=conn.get('database') or None)
    if kind == 'postgres':
        url = URL.create('postgresql+psycopg', port=int(conn.get('port') or 5432), **common)
        return create_engine(
            url, pool_pre_ping=True, insertmanyvalues_page_size=BATCH,
            json_serializer=lambda o: orjson.dumps(o).decode(),
            connect_args={'connect_timeout': 10, 'application_name': 'dataviz-labeling',
                          'sslmode': 'require' if conn.get('encrypt') else 'prefer'})
    url = URL.create('mssql+pymssql', port=int(conn.get('port') or 1433), **common)
    # pymssql (FreeTDS) negotiates TLS itself when the server requires it; `encrypt` has no switch here
    return create_engine(url, pool_pre_ping=True,
                         connect_args={'login_timeout': 15, 'charset': 'UTF-8', 'appname': 'dataviz-labeling'})


def _metadata(conn):
    prefix = _prefix(conn)
    return S.make_metadata(prefix, _schema(conn)), prefix


def test(conn):
    """{ok, ms, server_version, tables_exist, schema_version}; raises on a connection / login error."""
    t0 = time.time()
    md, prefix = _metadata(conn)
    tbl = S.tables(md, prefix)
    eng = engine_for(conn)
    try:
        with eng.connect() as c:
            c.execute(text('SELECT 1')).scalar()
            ver = '.'.join(str(v) for v in (eng.dialect.server_version_info or ()))
            insp = inspect(c)
            exists = insp.has_table(tbl['projects'].name, schema=_schema(conn))
            sv = None
            if insp.has_table(tbl['schema_info'].name, schema=_schema(conn)):
                si = tbl['schema_info']
                sv = c.execute(select(si.c.value).where(si.c.key == 'schema_version')).scalar()
        return {'ok': True, 'ms': round((time.time() - t0) * 1000), 'server_version': ver,
                'tables_exist': bool(exists), 'schema_version': int(sv) if sv is not None else None}
    finally:
        eng.dispose()


def ensure_schema(eng, md, conn):
    sch = _schema(conn)
    with eng.begin() as c:
        if sch:
            q = eng.dialect.identifier_preparer.quote(sch)
            if eng.dialect.name == 'postgresql':
                c.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS {q}')
            else:
                c.exec_driver_sql(f"IF SCHEMA_ID(N'{sch}') IS NULL EXEC('CREATE SCHEMA {q}')")
    md.create_all(eng, checkfirst=True)


# ---------------------------------------------------------------- value conversion (local SQLite -> remote)

def to_datetime(v):
    """Epoch seconds / ISO text / datetime -> timezone-aware UTC datetime."""
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    s = str(v).strip()
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except ValueError:
        pass
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _json_obj(v):
    if v is None:
        return None
    if isinstance(v, (bytes, str)):
        return orjson.loads(v) if v else None
    return v


def _json_text(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode()
    return v if isinstance(v, str) else orjson.dumps(v).decode()


def _dt_text(v):
    d = to_datetime(v)
    return None if d is None else d.astimezone(timezone.utc).isoformat(sep=' ')     # DATETIMEOFFSET literal


def _bool_int(v):
    return None if v is None else int(bool(v) if not isinstance(v, str) else v.lower() in ('1', 'true', 't'))


def _bool(v):
    return None if v is None else bool(_bool_int(v))


def _row_converter(table, dialect):
    """row dict (raw local columns) -> dict for every remote column, NOT NULL gaps filled with defaults."""
    pg = dialect == 'postgresql'
    plan = []
    for c in table.columns:
        t = c.type
        if isinstance(t, S.JSONDoc):
            fn = _json_obj if pg else _json_text
        elif isinstance(t, DateTime):
            fn = to_datetime if pg else _dt_text
        elif isinstance(t, Boolean):
            fn = _bool if pg else _bool_int
        else:
            fn = None
        default = None
        if not c.nullable and c.default is not None and c.default.is_scalar:
            default = c.default.arg
            if isinstance(t, Boolean) and not pg:
                default = int(default)
        plan.append((c.name, fn, default))

    def convert(row):
        out = {}
        for name, fn, default in plan:
            v = row.get(name)
            if fn is not None and v is not None:
                v = fn(v)
            out[name] = default if v is None else v
        return out
    return convert


# ---------------------------------------------------------------- upserts

class _PgWriter:
    def __init__(self, c, tbl):
        self.c, self.tbl = c, tbl
        self._stmts = {}

    def upsert(self, name, rows):
        if not rows:
            return
        st = self._stmts.get(name)
        if st is None:
            t = self.tbl[name]
            pk = [col.name for col in t.primary_key.columns]
            ins = pg_insert(t)
            st = self._stmts[name] = ins.on_conflict_do_update(
                index_elements=pk, set_={col.name: ins.excluded[col.name] for col in t.columns if col.name not in pk})
        for i in range(0, len(rows), BATCH):
            self.c.execute(st, rows[i:i + BATCH])

    def drop_excluded(self, task_id, keep, now):
        """Soft-delete the task's remote annotations whose class is not in `keep` (an earlier export without
        the class filter sent them). Rows re-sent after this (kept parents) get their own is_deleted back."""
        t = self.tbl['annotations']
        self.c.execute(_drop_excluded_stmt(t, task_id, keep, now))

    def flush(self):
        pass


class _MsWriter:
    """Rows go to #stage_<table>; flush() merges each staged table into its target once."""

    def __init__(self, c, tbl):
        self.c, self.tbl = c, tbl
        self.q = c.dialect.identifier_preparer
        self._staged = []                             # table names in FK order

    def _stage_name(self, name):
        return f'#stage_{name}'

    def upsert(self, name, rows):
        if not rows:
            return
        t = self.tbl[name]
        cols = [col.name for col in t.columns]
        stage = self._stage_name(name)
        if name not in self._staged:
            self.c.exec_driver_sql(f"IF OBJECT_ID('tempdb..{stage}') IS NOT NULL DROP TABLE {stage}")
            self.c.exec_driver_sql(f'SELECT TOP 0 * INTO {stage} FROM {self.q.format_table(t)}')
            self._staged.append(name)
        col_sql = ', '.join(self.q.quote(n) for n in cols)
        per = max(1, min(BATCH, MSSQL_PARAMS // len(cols)))
        one = '(' + ', '.join(['%s'] * len(cols)) + ')'
        sql_full = f'INSERT INTO {stage} ({col_sql}) VALUES ' + ', '.join([one] * per)
        for i in range(0, len(rows), per):
            chunk = rows[i:i + per]
            sql = sql_full if len(chunk) == per else f'INSERT INTO {stage} ({col_sql}) VALUES ' + ', '.join([one] * len(chunk))
            params = tuple(r[n] for r in chunk for n in cols)
            self.c.exec_driver_sql(sql, params)

    def drop_excluded(self, task_id, keep, now):
        """_PgWriter.drop_excluded; runs at once, so the MERGE at flush() restores the re-sent rows after it."""
        sql, params = _drop_excluded_mssql(self.q, self.tbl['annotations'], task_id, keep, now)
        self.c.exec_driver_sql(sql, params)

    def flush(self):
        for name in self._staged:
            t = self.tbl[name]
            stage = self._stage_name(name)
            pk = [col.name for col in t.primary_key.columns]
            cols = [col.name for col in t.columns]
            qc = self.q.quote
            on = ' AND '.join(f't.{qc(k)} = s.{qc(k)}' for k in pk)
            upd = ', '.join(f't.{qc(n)} = s.{qc(n)}' for n in cols if n not in pk)
            self.c.exec_driver_sql(
                f'MERGE INTO {self.q.format_table(t)} WITH (HOLDLOCK) AS t USING {stage} AS s ON {on} '
                f'WHEN MATCHED THEN UPDATE SET {upd} '
                f'WHEN NOT MATCHED BY TARGET THEN INSERT ({", ".join(qc(n) for n in cols)}) '
                f'VALUES ({", ".join("s." + qc(n) for n in cols)});')
            self.c.exec_driver_sql(f'DROP TABLE {stage}')
        self._staged = []


def _drop_excluded_stmt(t, task_id, keep, now):
    """UPDATE <annotations> SET is_deleted = true, updated_at = now for the task's live rows of other classes."""
    cond = or_(t.c.class_id.is_(None), t.c.class_id.not_in(sorted(keep))) if keep else true()
    return (update(t).where(t.c.task_id == task_id, t.c.is_deleted == False, cond)  # noqa: E712
            .values(is_deleted=True, updated_at=now))


def _drop_excluded_mssql(q, t, task_id, keep, now):
    """(sql, params) of _drop_excluded_stmt for pymssql (%s parameters, DATETIMEOFFSET as text like the
    staged rows; the kept class list stays far under the 2100-parameter cap)."""
    keep = sorted(keep)
    cls = f' AND ({q.quote("class_id")} IS NULL OR {q.quote("class_id")} NOT IN ({", ".join(["%s"] * len(keep))}))' \
        if keep else ''
    sql = (f'UPDATE {q.format_table(t)} SET {q.quote("is_deleted")} = 1, {q.quote("updated_at")} = %s '
           f'WHERE {q.quote("task_id")} = %s AND {q.quote("is_deleted")} = 0{cls}')
    return sql, (_dt_text(now), task_id, *keep)


def _slug(name, used):
    s = re.sub(r'[^a-z0-9]+', '_', (name or '').lower()).strip('_')[:40] or 'task'
    base, k = s, 2
    while s in used:
        s = f'{base}_{k}'
        k += 1
    used.add(s)
    return s


def _create_view(c, tbl, prefix, schema_name, slug, task_id):
    q = c.dialect.identifier_preparer
    view = q.quote(f'{prefix}v_{slug}')
    if schema_name:
        view = f'{q.quote(schema_name)}.{view}'
    a, cl, m = (q.format_table(tbl[n]) for n in ('annotations', 'label_classes', 'media'))
    tid = str(task_id).replace("'", "''")
    false = 'false' if c.dialect.name == 'postgresql' else '0'
    body = (f'SELECT a.*, c.name AS class_name, m.file_name AS file_name, m.width AS image_width, '
            f'm.height AS image_height FROM {a} a JOIN {m} m ON m.id = a.media_id '
            f'LEFT JOIN {cl} c ON c.id = a.class_id WHERE a.task_id = \'{tid}\' AND a.is_deleted = {false}')
    verb = 'CREATE OR REPLACE VIEW' if c.dialect.name == 'postgresql' else 'CREATE OR ALTER VIEW'
    c.exec_driver_sql(f'{verb} {view} AS {body}')
    return f'{prefix}v_{slug}'


def _project_rows(store, project):
    rows = [r for batch in store.iter_rows('projects', {'id': project['id']}) for r in batch]
    if rows:
        return rows
    return [{'id': project['id'], 'name': project.get('name') or project['id'], 'description': project.get('description'),
             'created_at': project.get('created'), 'updated_at': project.get('updated'), 'meta': None}]


def export_to_db(conn, store, project, task_ids, options=None, progress=None):
    """Upsert the session into the remote DB, one export_runs row per task.

    Returns {tasks: {tid: {n_media, n_annotations, n_rows, run_id, view?}}, ms, uri}."""
    options = dict(options or {})
    progress = progress or (lambda *a, **k: None)
    t_all = time.time()
    md, prefix = _metadata(conn)
    tbl = S.tables(md, prefix)
    eng = engine_for(conn)
    dialect = eng.dialect.name
    conv = {name: _row_converter(t, dialect) for name, t in tbl.items()}
    uri = eng.url.render_as_string(hide_password=True)
    pid = project['id']
    run_opts = {k: options.get(k) for k in ('only_done', 'create_views', 'classes') if k in options}
    result = {'tasks': {}, 'uri': uri}
    try:
        progress(stage='database · schema')
        ensure_schema(eng, md, conn)
        with eng.begin() as c:
            w = _PgWriter(c, tbl) if dialect == 'postgresql' else _MsWriter(c, tbl)
            w.upsert('schema_info', [{'key': 'schema_version', 'value': str(S.SCHEMA_VERSION)}])
            w.upsert('projects', [conv['projects'](r) for r in _project_rows(store, project)])
            w.flush()

        # media are shared by every task: sent once, before the per-task runs
        progress(stage='database · media')
        n_media_all = 0
        with eng.begin() as c:
            w = _PgWriter(c, tbl) if dialect == 'postgresql' else _MsWriter(c, tbl)
            for tid in task_ids:
                w.upsert('tasks', [conv['tasks'](r) for b in store.iter_rows('tasks', {'id': tid}) for r in b])
                w.upsert('label_classes', [conv['label_classes'](r) for b in store.iter_rows('label_classes', {'task_id': tid})
                                           for r in b])
            w.flush()       # MSSQL: tasks before media / annotations (FKs)
            for batch in store.iter_rows('media', {'project_id': pid}, batch=BATCH):
                w.upsert('media', [conv['media'](r) for r in batch])
                n_media_all += len(batch)
                progress(done=n_media_all)
            w.flush()

        used_slugs = set()
        task_names = {r['id']: r.get('name') for tid in task_ids for b in store.iter_rows('tasks', {'id': tid}) for r in b}
        for tid in task_ids:
            t0 = time.time()
            run_id = uuid.uuid4().hex
            started = datetime.now(timezone.utc)
            stats = {'n_media': 0, 'n_annotations': 0, 'n_rows': 0, 'run_id': run_id}
            progress(stage=f'database · {task_names.get(tid) or tid}')
            try:
                with eng.begin() as c:
                    w = _PgWriter(c, tbl) if dialect == 'postgresql' else _MsWriter(c, tbl)
                    status = {}
                    for batch in store.iter_rows('media_task_status', {'task_id': tid}, batch=BATCH):
                        for r in batch:
                            status[r['media_id']] = r.get('status')
                    done_ids = {m for m, s in status.items() if s == 'done'} if options.get('only_done') else None
                    # export class filter: only the kept classes' annotation rows (class rows go in full: the
                    # database keeps the real ids, so nothing is renumbered there)
                    keep = (options.get('classes') or {}).get(tid)
                    keep = set(keep) if keep is not None else None
                    parents, n_live = set(), {}
                    if keep is not None:
                        # the boxes kept keypoints sets link to go too, whatever their class (as the file export
                        # does), so no exported parent_id dangles
                        for batch in store.iter_rows('annotations', {'task_id': tid}, batch=BATCH):
                            parents.update(r['parent_id'] for r in batch if r.get('parent_id') and r['class_id'] in keep)
                        # rows of the excluded classes an earlier unfiltered export sent: deleted there now
                        w.drop_excluded(tid, keep, started)
                    for batch in store.iter_rows('annotations', {'task_id': tid}, batch=BATCH):
                        if done_ids is not None:
                            batch = [r for r in batch if r['media_id'] in done_ids]
                        if keep is not None:
                            batch = [r for r in batch if r['class_id'] in keep or r['id'] in parents]
                        rows = [conv['annotations'](r) for r in batch]
                        w.upsert('annotations', rows)
                        for r in rows:
                            if not r['is_deleted']:
                                n_live[r['media_id']] = n_live.get(r['media_id'], 0) + 1
                        stats['n_rows'] += len(rows)
                        stats['n_annotations'] += sum(1 for r in rows if not r['is_deleted'])
                        progress(done=stats['n_rows'], extra=f'{stats["n_rows"]:,} annotations')
                    for batch in store.iter_rows('media_task_status', {'task_id': tid}, batch=BATCH):
                        if done_ids is not None:
                            batch = [r for r in batch if r['media_id'] in done_ids]
                        if keep is not None:              # the count of what this export holds, not the local one
                            batch = [{**r, 'n_annotations': n_live.get(r['media_id'], 0)} for r in batch]
                        w.upsert('media_task_status', [conv['media_task_status'](r) for r in batch])
                    stats['n_media'] = len(done_ids) if done_ids is not None else n_media_all
                    w.flush()
                    if options.get('create_views'):
                        stats['view'] = _create_view(c, tbl, prefix, _schema(conn), _slug(task_names.get(tid), used_slugs), tid)
                    run = {'id': run_id, 'project_id': pid, 'task_id': tid, 'target': 'db', 'target_uri': uri,
                           'options': run_opts, 'started_at': started, 'finished_at': datetime.now(timezone.utc),
                           'n_media': stats['n_media'], 'n_annotations': stats['n_annotations'], 'status': 'done',
                           'error': None}
                    w.upsert('export_runs', [conv['export_runs'](run)])
                    w.flush()
            except Exception as e:
                # the task's transaction rolled back; record the failed run on its own
                try:
                    with eng.begin() as c:
                        w = _PgWriter(c, tbl) if dialect == 'postgresql' else _MsWriter(c, tbl)
                        w.upsert('export_runs', [conv['export_runs'](
                            {'id': run_id, 'project_id': pid, 'task_id': tid, 'target': 'db', 'target_uri': uri,
                             'options': run_opts, 'started_at': started, 'finished_at': datetime.now(timezone.utc),
                             'n_media': 0, 'n_annotations': 0, 'status': 'error', 'error': f'{type(e).__name__}: {e}'})])
                        w.flush()
                except Exception:  # noqa: BLE001 - the original error is the one to report
                    pass
                raise
            stats['ms'] = round((time.time() - t0) * 1000)
            result['tasks'][tid] = stats
            _log.info(f'{vlog.esc(uri)}: task {vlog.esc(task_names.get(tid) or tid)} · {stats["n_rows"]:,} annotation rows '
                      f'[dim]({stats["ms"]} ms)[/]')
    finally:
        eng.dispose()
    result['ms'] = round((time.time() - t_all) * 1000)
    return result
