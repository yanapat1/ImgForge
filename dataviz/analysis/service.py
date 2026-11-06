"""
The Data Analysis zone's one entry point: routes, assistant tools and tests call these functions.

Every call names the signed-in user, and connections are looked up as that user's own: nobody can run SQL with someone
else's credentials, so analysis sessions are private and never shared.

    Flow of a query: check() classifies the text and, when something is not a plain read, returns a single-use token
    bound to this user, session and exact text. start() refuses a non-read without that token. The run happens on a
    worker thread; the page polls run(), and stop() cancels it. Rows go to a spool file (spool.py), never into memory.

    Native temp tables live on one database connection, so a statement that makes or reads one runs on the session's
    pinned connection; other queries get their own, and a long query does not block another tab.
"""

import hashlib
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import text

from .. import db
from ..auth import audit, tiers
from ..core import logging as vlog
from . import adapters, classify, spool

KINDS = ('postgres', 'mssql', 'mongo')
TOKEN_S = 300
MAX_TEXT = 200000
_log = vlog.get_logger('analysis')


class AnalysisError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _Conf:
    ctx = None
    root = None


_lock = threading.Lock()
_runs = {}                 # run id -> Run
_results = {}              # result id -> {user, session, path, columns, rows, stats, ...}
_tokens = {}               # token -> (user id, session id, text hash, expiry)
_tree_cache = {}           # (user, connection, database) -> (time, value)
_live = {}                 # session id -> {seen, pinned: {(conn, db): (adapter, lock)}, temps: [...]}
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix='dataviz-analysis')


def configure(ctx):
    _Conf.ctx = ctx


def _cfg():
    return (_Conf.ctx.cfg or {}).get('data_analysis') or {}


def _root():
    return Path(_Conf.ctx.datasets.root)


def _use(user):
    tiers.check(user, 'analysis.use')


def _conn(user, cid):
    """The caller's own saved connection, with its secret. Someone else's is simply not found."""
    try:
        c = _Conf.ctx.datasets.connection(cid, owner_id=user['id'])
    except KeyError:
        raise AnalysisError('no such connection', 404) from None
    if c.get('kind') not in KINDS:
        raise AnalysisError(f'a {c.get("kind")} connection cannot be queried')
    return c


def _clean(e, conn):
    """Driver messages can quote a connection string: the page and the AI only get this."""
    msg = str(e).strip() or type(e).__name__
    for secret in (conn.get('secret_key'),):
        if secret:
            msg = msg.replace(str(secret), '***')
    return msg[:1000]


def _dialect(conn):
    return conn['kind']


# ---------------------------------------------------------------- sessions
def _session(user, sid):
    with db.connect() as c:
        r = c.execute(text('SELECT * FROM app.an_sessions WHERE id = :i AND owner_id = :o'),
                      {'i': sid, 'o': user['id']}).first()
    if r is None:
        raise AnalysisError('no such session', 404)
    return r


def _shape(r):
    return {'id': r.id, 'name': r.name, 'state': r.state or {}, 'updated_at': r.updated_at.isoformat()}


def list_sessions(user):
    _use(user)
    with db.connect() as c:
        rows = c.execute(text('SELECT * FROM app.an_sessions WHERE owner_id = :o ORDER BY updated_at DESC'),
                         {'o': user['id']}).all()
    return [_shape(r) for r in rows]


def create_session(user, name):
    _use(user)
    name = str(name or '').strip()[:200] or 'Untitled analysis'
    sid = uuid.uuid4().hex
    with db.begin() as c:
        r = c.execute(text('INSERT INTO app.an_sessions (id, owner_id, name) VALUES (:i, :o, :n) RETURNING *'),
                      {'i': sid, 'o': user['id'], 'n': name}).first()
    return _shape(r)


def update_session(user, sid, name=None, state=None):
    _session(user, sid)
    sets, args = ['updated_at = now()'], {'i': sid}
    if name is not None:
        sets.append('name = :n')
        args['n'] = str(name).strip()[:200] or 'Untitled analysis'
    if state is not None:
        import orjson
        sets.append('state = CAST(:s AS JSONB)')
        args['s'] = orjson.dumps(state).decode()
    with db.begin() as c:
        r = c.execute(text(f'UPDATE app.an_sessions SET {", ".join(sets)} WHERE id = :i RETURNING *'), args).first()
    return _shape(r)


def delete_session(user, sid):
    _session(user, sid)
    close_session(user, sid)
    with db.begin() as c:
        c.execute(text('DELETE FROM app.an_sessions WHERE id = :i'), {'i': sid})
    shutil.rmtree(_root() / 'analysis' / str(user['id']) / sid, ignore_errors=True)


def _state(sid):
    with _lock:
        return _live.setdefault(sid, {'seen': time.time(), 'pinned': {}, 'temps': []})


def heartbeat(user, sid):
    _session(user, sid)
    _state(sid)['seen'] = time.time()
    reap()
    return {'temps': temps(user, sid)}


def close_session(user, sid):
    """Leaving: temp tables go with their connection, and the result files of this session too."""
    _session(user, sid)
    _forget(sid)
    with _lock:
        gone = [k for k, v in _results.items() if v['session'] == sid and v['user'] == user['id']]
    for rid in gone:
        _drop_result(rid)


def _forget(sid):
    with _lock:
        st = _live.pop(sid, None)
    for adapter, _ in (st or {}).get('pinned', {}).values():
        try:
            adapter.close()
        except Exception:  # noqa: BLE001 - it is going away anyway
            pass


def reap():
    """Sessions with no page open past the grace time lose their temp tables. A page refresh loses nothing."""
    limit = time.time() - float(_cfg().get('temp_grace_s', 900))
    with _lock:
        gone = [sid for sid, st in _live.items() if st['seen'] < limit and st['pinned']]
    for sid in gone:
        _forget(sid)


def temps(user, sid):
    return [dict(t) for t in _state(sid)['temps']]


# ---------------------------------------------------------------- what can be queried
def sources(user):
    _use(user)
    out = []
    for c in _Conf.ctx.datasets.connections(user['id']):
        if c.get('kind') in KINDS:
            out.append({'id': c['id'], 'name': c.get('name') or c.get('host'), 'kind': c['kind'], 'host': c.get('host'),
                        'database': c.get('database') or ''})
    return out


def tree(user, cid, database=None, refresh=False):
    """database None: the databases of this connection; else its tables."""
    _use(user)
    conn = _conn(user, cid)
    key = (user['id'], cid, database)
    ttl = float(_cfg().get('tree_cache_s', 120))
    with _lock:
        hit = _tree_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < ttl:
        return hit[1]
    a = None
    try:
        a = adapters.adapter(conn, database)
        value = {'tables': a.tables()} if database or conn['kind'] == 'mongo' and conn.get('database') else \
            {'databases': a.databases(), 'version': a.version()}
    except AnalysisError:
        raise
    except Exception as e:  # noqa: BLE001 - a database's own error: say what it said
        raise AnalysisError(_clean(e, conn)) from None
    finally:
        if a is not None:
            a.close()
    with _lock:
        _tree_cache[key] = (time.time(), value)
    return value


def describe(user, cid, database, schema, table):
    _use(user)
    conn = _conn(user, cid)
    a = None
    try:
        a = adapters.adapter(conn, database)
        out = a.describe(schema, table)
    except Exception as e:  # noqa: BLE001
        raise AnalysisError(_clean(e, conn)) from None
    finally:
        if a is not None:
            a.close()
    with db.connect() as c:
        rows = c.execute(text("""SELECT column_name, description, source FROM app.an_column_docs
                                 WHERE connection_id = :c AND database = :d AND schema_name = :s AND table_name = :t"""),
                         {'c': cid, 'd': database or '', 's': schema or '', 't': table}).all()
    docs = {r.column_name: r for r in rows}
    for col in out['columns']:
        col['from'] = 'database' if col['description'] else ''
        d = docs.get(col['name'])
        if d:
            col['description'], col['from'] = d.description, d.source
    if '' in docs:
        out['description'] = docs[''].description
    return out


def set_description(user, cid, database, schema, table, column, description, source='user'):
    _use(user)
    _conn(user, cid)
    keys = {'c': cid, 'd': database or '', 's': schema or '', 't': table, 'col': column or ''}
    with db.begin() as c:
        if not str(description or '').strip():
            c.execute(text("""DELETE FROM app.an_column_docs WHERE connection_id = :c AND database = :d
                              AND schema_name = :s AND table_name = :t AND column_name = :col"""), keys)
            return {'ok': True}
        c.execute(text("""
            INSERT INTO app.an_column_docs (connection_id, database, schema_name, table_name, column_name, description,
                                            source, updated_by)
            VALUES (:c, :d, :s, :t, :col, :text, :src, :u)
            ON CONFLICT (connection_id, database, schema_name, table_name, column_name)
            DO UPDATE SET description = :text, source = :src, updated_by = :u, updated_at = now()"""),
                  {**keys, 'text': str(description).strip()[:4000], 'src': 'ai' if source == 'ai' else 'user', 'u': user['id']})
    return {'ok': True}


# ---------------------------------------------------------------- the gate
def _classify(conn, sql, sid=None):
    if not str(sql or '').strip():
        raise AnalysisError('the query is empty')
    if len(sql) > MAX_TEXT:
        raise AnalysisError('the query is too long')
    if conn['kind'] == 'mongo':
        return classify.classify_mongo(sql)
    names = [t['name'].lstrip('#') for t in (_state(sid)['temps'] if sid else ())]
    return classify.classify(sql, _dialect(conn), names)


def _digest(sql):
    return hashlib.sha256(sql.encode()).hexdigest()


def check(user, sid, cid, database, sql):
    """What the text would do. confirm: a single-use token, present when the user must agree first."""
    _session(user, sid)
    conn = _conn(user, cid)
    st = _classify(conn, sql, sid)
    public = [{k: s.get(k) for k in ('sql', 'kind', 'op', 'tables', 'reason')} for s in st]
    need = classify.needs_confirm(st)
    if not need:
        return {'statements': public, 'confirm': None}
    if any(s['kind'] == classify.WRITE for s in need):
        tiers.check(user, 'analysis.write')
    token = secrets.token_urlsafe(24)
    with _lock:
        for k in [k for k, v in _tokens.items() if v[3] < time.time()]:
            del _tokens[k]
        _tokens[token] = (user['id'], sid, _digest(sql), time.time() + TOKEN_S)
    return {'statements': public, 'confirm': token}


def _spend(user, sid, sql, token):
    with _lock:
        got = _tokens.pop(token or '', None)
    if got is None or got[3] < time.time() or got[:3] != (user['id'], sid, _digest(sql)):
        raise AnalysisError('this needs your confirmation first', 409)


# ---------------------------------------------------------------- running
class Run:
    def __init__(self, user, sid, cid, database, name, sql):
        self.id = uuid.uuid4().hex
        self.user = user
        self.sid = sid
        self.cid = cid
        self.database = database
        self.name = name
        self.sql = sql
        self.status = 'running'
        self.t0 = time.time()
        self.ms = None
        self.statement = 0
        self.total = 0
        self.results = []
        self.error = None
        self.stopped = threading.Event()
        self.active = None

    def public(self):
        return {'id': self.id, 'status': self.status, 'ms': self.ms if self.ms is not None else round((time.time() - self.t0) * 1000),
                'statement': self.statement, 'total': self.total, 'results': self.results, 'error': self.error}


def start(user, sid, cid, database, sql, token=None, name=''):
    _session(user, sid)
    conn = _conn(user, cid)
    st = _classify(conn, sql, sid)
    need = classify.needs_confirm(st)
    if need:
        if any(s['kind'] == classify.WRITE for s in need):
            tiers.check(user, 'analysis.write')
        _spend(user, sid, sql, token)
    with _lock:
        mine = sum(1 for r in _runs.values() if r.user['id'] == user['id'] and r.status == 'running')
    if mine >= int(_cfg().get('runs_per_user', 4)):
        raise AnalysisError('you already have the most queries running at once: wait or stop one', 429)
    run = Run(user, sid, cid, database, str(name or '')[:200], sql)
    run.total = len(st)
    with _lock:
        _runs[run.id] = run
        for k in [k for k, r in _runs.items() if r.status != 'running' and r.t0 < time.time() - 3600]:
            del _runs[k]
    _pool.submit(_execute, run, conn, st)
    return run.public()


def _pinned(sid, conn, database):
    st = _state(sid)
    key = (conn['id'], database or '')
    with _lock:
        hit = st['pinned'].get(key)
    if hit:
        return hit
    made = adapters.adapter(conn, database, _cfg().get('statement_timeout_s', 0)), threading.Lock()
    with _lock:
        return st['pinned'].setdefault(key, made)


def _uses_temp(sid, s):
    if s['kind'] == classify.TEMP:
        return True
    names = {t['name'].lstrip('#').lower() for t in _state(sid)['temps']}
    return any(t.split('.')[-1].lower() in names for t in s.get('tables') or [])


def _note_temp(sid, conn, database, s):
    target = s.get('target') or ''
    if not target:
        return
    bare = target.split('.')[-1].lstrip('#')
    label = f'#{bare}' if conn['kind'] == 'mssql' else bare
    st = _state(sid)
    with _lock:
        st['temps'] = [t for t in st['temps'] if t['name'].lstrip('#').lower() != bare.lower()]
        if s['op'] != 'DROP':
            st['temps'].append({'name': label, 'connection': conn['id'], 'database': database or '',
                                'source': conn['kind'], 'source_name': conn.get('name') or ''})


def _execute(run, conn, statements):
    total_rows, kind = 0, classify.READ
    pinned_lock = None
    try:
        for i, s in enumerate(statements):
            if run.stopped.is_set():
                break
            run.statement = i + 1
            read = s['kind'] == classify.READ
            if not read:
                kind = classify.WRITE if s['kind'] == classify.WRITE else (classify.TEMP if kind == classify.READ else kind)
            pin = _uses_temp(run.sid, s)
            if pin:
                a, pinned_lock = _pinned(run.sid, conn, run.database)
                pinned_lock.acquire()
            else:
                a = adapters.adapter(conn, run.database, _cfg().get('statement_timeout_s', 0))
            run.active = a
            try:
                total_rows += _one(run, conn, a, s, read)
            finally:
                run.active = None
                if pin:
                    pinned_lock.release()
                else:
                    a.close()
            if s['kind'] == classify.TEMP:
                _note_temp(run.sid, conn, run.database, s)
            if s['kind'] == classify.WRITE:
                audit.log('analysis.change', target=conn['id'], user_id=run.user['id'],
                          meta={'database': run.database, 'op': s['op'], 'sql': s['sql'][:2000]})
        run.status = 'stopped' if run.stopped.is_set() else 'done'
    except Exception as e:  # noqa: BLE001 - the database's own error, shown in Messages
        if run.stopped.is_set():
            run.status = 'stopped'
        else:
            run.status = 'error'
            run.error = _clean(e, conn)
            _log.info(f'query failed: {vlog.esc(run.error[:200])}')
    run.ms = round((time.time() - run.t0) * 1000)
    _remember(run, kind, total_rows)


def _one(run, conn, a, s, read):
    stmt = s['command'] if conn['kind'] == 'mongo' else s['sql']
    t0 = time.time()
    columns, batches = a.run(stmt, read=read)
    if not columns:
        for _ in batches:
            pass
        return 0
    path = spool.new_path(_root(), run.user['id'], run.sid)

    def until_stopped():
        try:
            for b in batches:
                yield b
                if run.stopped.is_set():
                    return
        except Exception:
            if not run.stopped.is_set():    # a cancelled driver raises: what arrived before it is kept
                raise

    info = spool.Spool(path).write(columns, until_stopped(), int(_cfg().get('max_rows', 1000000)))
    rid = uuid.uuid4().hex
    entry = {'user': run.user['id'], 'session': run.sid, 'path': path, 'stats': None, 'at': time.time(), **info}
    with _lock:
        _results[rid] = entry
    run.results.append({'id': rid, 'rows': info['rows'], 'columns': info['columns'], 'truncated': info['truncated'],
                        'ms': round((time.time() - t0) * 1000), 'statement': s['sql'][:500], 'stopped': run.stopped.is_set()})
    _trim(run.user['id'])
    return info['rows']


def _trim(user_id):
    """Oldest results go first once a user's files pass the quota."""
    limit = float(_cfg().get('max_spool_mb', 4096)) * 1e6
    with _lock:
        mine = sorted(((v['at'], k, v['bytes']) for k, v in _results.items() if v['user'] == user_id))
    used = sum(x[2] for x in mine)
    for _, rid, size in mine:
        if used <= limit:
            break
        _drop_result(rid)
        used -= size


def _drop_result(rid):
    with _lock:
        v = _results.pop(rid, None)
    if v:
        Path(v['path']).unlink(missing_ok=True)


def _remember(run, kind, rows):
    try:
        with db.begin() as c:
            c.execute(text("""INSERT INTO app.an_history (session_id, connection_id, database, name, sql, kind, status, rows, ms, error)
                              VALUES (:s, :c, :d, :n, :q, :k, :st, :r, :m, :e)"""),
                      {'s': run.sid, 'c': run.cid, 'd': run.database, 'n': run.name, 'q': run.sql, 'k': kind,
                       'st': run.status, 'r': rows, 'm': run.ms, 'e': run.error})
    except Exception as e:  # noqa: BLE001 - history is a nicety: the result is there
        _log.warning(f'history not saved: {vlog.esc(e)}')


def _run(user, rid):
    with _lock:
        r = _runs.get(rid)
    if r is None or r.user['id'] != user['id']:
        raise AnalysisError('no such run', 404)
    return r


def run_state(user, rid):
    return _run(user, rid).public()


def stop(user, rid):
    r = _run(user, rid)
    r.stopped.set()
    a = r.active
    if a is not None:
        try:
            a.cancel()
        except Exception as e:  # noqa: BLE001 - the flag still ends it at the next batch
            _log.info(f'cancel: {vlog.esc(e)}')
    return r.public()


def explain(user, sid, cid, database, sql):
    """The plan of one read: each step's share of the cost, estimated unless it says otherwise."""
    _session(user, sid)
    conn = _conn(user, cid)
    st = [s for s in _classify(conn, sql, sid) if s['kind'] == classify.READ]
    if not st:
        raise AnalysisError('only a read has a plan here')
    a = adapters.adapter(conn, database, _cfg().get('statement_timeout_s', 0))
    try:
        plan = a.explain(st[0]['command'] if conn['kind'] == 'mongo' else st[0]['sql'])
    except Exception as e:  # noqa: BLE001
        raise AnalysisError(_clean(e, conn)) from None
    finally:
        a.close()
    total = plan['total'] or 1
    for n in plan['nodes']:
        n['pct'] = round(n['cost'] / total * 100, 1)
    return plan


# ---------------------------------------------------------------- results
def _result(user, rid):
    with _lock:
        r = _results.get(rid)
    if r is None or r['user'] != user['id']:
        raise AnalysisError('that result is gone: run the query again', 404)
    return r


def page(user, rid, offset=0, limit=200, sort=None, desc=False, q=''):
    return spool.page(_result(user, rid)['path'], offset, limit, sort, desc, q)


def stats(user, rid):
    r = _result(user, rid)
    if r['stats'] is None:
        r['stats'] = {'columns': spool.stats(r['path']), 'duplicates': spool.duplicate_rows(r['path'])}
    return r['stats']


def histogram(user, rid, column, bins=12):
    return spool.histogram(_result(user, rid)['path'], column, bins)


def result_path(user, rid):
    return _result(user, rid)['path']


# ---------------------------------------------------------------- saved things
def history(user, sid, limit=200):
    _session(user, sid)
    with db.connect() as c:
        rows = c.execute(text('SELECT * FROM app.an_history WHERE session_id = :s ORDER BY at DESC LIMIT :l'),
                         {'s': sid, 'l': max(1, min(int(limit), 1000))}).all()
    return [{'id': r.id, 'connection': r.connection_id, 'database': r.database, 'name': r.name, 'sql': r.sql, 'kind': r.kind,
             'status': r.status, 'rows': r.rows, 'ms': r.ms, 'error': r.error, 'at': r.at.isoformat()} for r in rows]


def scripts(user, sid):
    _session(user, sid)
    with db.connect() as c:
        rows = c.execute(text('SELECT * FROM app.an_scripts WHERE session_id = :s ORDER BY position, updated_at'), {'s': sid}).all()
    return [{'id': r.id, 'name': r.name, 'position': r.position, 'connection': r.connection_id, 'database': r.database,
             'body': r.body} for r in rows]


def save_script(user, sid, body):
    _session(user, sid)
    pid = str(body.get('id') or uuid.uuid4().hex)
    args = {'i': pid, 's': sid, 'n': str(body.get('name') or 'command').strip()[:200] or 'command',
            'p': int(body.get('position') or 0), 'c': body.get('connection'), 'd': body.get('database'),
            'b': str(body.get('body') or '')[:MAX_TEXT]}
    with db.begin() as c:
        got = c.execute(text("""
            INSERT INTO app.an_scripts (id, session_id, name, position, connection_id, database, body)
            VALUES (:i, :s, :n, :p, :c, :d, :b)
            ON CONFLICT (id) DO UPDATE SET name = :n, position = :p, connection_id = :c, database = :d, body = :b, updated_at = now()
            WHERE app.an_scripts.session_id = :s RETURNING id"""), args).first()
    if got is None:
        raise AnalysisError('no such script', 404)
    return {'id': pid}


def delete_script(user, sid, pid):
    _session(user, sid)
    with db.begin() as c:
        c.execute(text('DELETE FROM app.an_scripts WHERE id = :i AND session_id = :s'), {'i': pid, 's': sid})


def dashboards(user, sid):
    _session(user, sid)
    with db.connect() as c:
        rows = c.execute(text('SELECT * FROM app.an_dashboards WHERE session_id = :s ORDER BY updated_at DESC'), {'s': sid}).all()
    return [{'id': r.id, 'name': r.name, 'layout': r.layout} for r in rows]


def save_dashboard(user, sid, body):
    import orjson
    _session(user, sid)
    pid = str(body.get('id') or uuid.uuid4().hex)
    args = {'i': pid, 's': sid, 'n': str(body.get('name') or 'Dashboard').strip()[:200], 'l': orjson.dumps(body.get('layout') or []).decode()}
    with db.begin() as c:
        got = c.execute(text("""
            INSERT INTO app.an_dashboards (id, session_id, name, layout) VALUES (:i, :s, :n, CAST(:l AS JSONB))
            ON CONFLICT (id) DO UPDATE SET name = :n, layout = CAST(:l AS JSONB), updated_at = now()
            WHERE app.an_dashboards.session_id = :s RETURNING id"""), args).first()
    if got is None:
        raise AnalysisError('no such dashboard', 404)
    return {'id': pid}


def delete_dashboard(user, sid, pid):
    _session(user, sid)
    with db.begin() as c:
        c.execute(text('DELETE FROM app.an_dashboards WHERE id = :i AND session_id = :s'), {'i': pid, 's': sid})
