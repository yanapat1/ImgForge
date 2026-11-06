"""
The Data Analysis zone's API (/api/analysis/*): thin over analysis/service.py, which does every check. All of it works
on the caller's own connections and sessions.
"""

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

from ...analysis import service
from ...analysis.service import AnalysisError
from ...auth import deps


def _call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except AnalysisError as e:
        raise HTTPException(e.status, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


def router(state, ctx):
    r = APIRouter(prefix='/api/analysis')
    service.configure(ctx)
    me = Depends(deps.current_user)

    @r.get('/sources')
    def sources(user=me):
        return {'sources': _call(service.sources, user)}

    @r.get('/sessions')
    def sessions(user=me):
        return {'sessions': _call(service.list_sessions, user)}

    @r.post('/sessions')
    def new_session(body: dict = Body(default={}), user=me):
        return _call(service.create_session, user, body.get('name'))

    @r.patch('/sessions/{sid}')
    def change_session(sid: str, body: dict = Body(...), user=me):
        return _call(service.update_session, user, sid, body.get('name'), body.get('state'))

    @r.delete('/sessions/{sid}')
    def drop_session(sid: str, user=me):
        _call(service.delete_session, user, sid)
        return {'ok': True}

    @r.post('/sessions/{sid}/heartbeat')
    def heartbeat(sid: str, user=me):
        return _call(service.heartbeat, user, sid)

    @r.post('/sessions/{sid}/close')
    def close(sid: str, user=me):
        _call(service.close_session, user, sid)
        return {'ok': True}

    @r.get('/tree')
    def tree(connection: str, database: str | None = None, refresh: int = 0, user=me):
        return _call(service.tree, user, connection, database, bool(refresh))

    @r.get('/describe')
    def describe(connection: str, table: str, database: str = '', schema: str = '', user=me):
        return _call(service.describe, user, connection, database, schema, table)

    @r.put('/docs')
    def docs(body: dict = Body(...), user=me):
        return _call(service.set_description, user, body.get('connection'), body.get('database'), body.get('schema'),
                     body.get('table'), body.get('column'), body.get('description'))

    @r.post('/check')
    def check(body: dict = Body(...), user=me):
        return _call(service.check, user, body.get('session'), body.get('connection'), body.get('database'), body.get('sql'))

    @r.post('/run')
    def run(body: dict = Body(...), user=me):
        return _call(service.start, user, body.get('session'), body.get('connection'), body.get('database'), body.get('sql'),
                     body.get('confirm'), body.get('name'))

    @r.get('/run/{rid}')
    def run_state(rid: str, user=me):
        return _call(service.run_state, user, rid)

    @r.post('/run/{rid}/stop')
    def stop(rid: str, user=me):
        return _call(service.stop, user, rid)

    @r.post('/explain')
    def explain(body: dict = Body(...), user=me):
        return _call(service.explain, user, body.get('session'), body.get('connection'), body.get('database'), body.get('sql'))

    @r.post('/model/parse')
    def parse_model(body: dict = Body(...), user=me):
        return _call(service.parse_model, user, body.get('connection'), body.get('sql'))

    @r.post('/model/generate')
    def generate(body: dict = Body(...), user=me):
        return _call(service.generate_sql, user, body.get('model') or {}, body.get('dialect'))

    @r.get('/results/{rid}/page')
    def page(rid: str, offset: int = 0, limit: int = 200, sort: str | None = None, desc: int = 0, q: str = '', user=me):
        return _call(service.page, user, rid, offset, limit, sort, bool(desc), q)

    @r.get('/results/{rid}/stats')
    def stats(rid: str, user=me):
        return _call(service.stats, user, rid)

    @r.get('/results/{rid}/histogram')
    def histogram(rid: str, column: str, bins: int = 12, user=me):
        return _call(service.histogram, user, rid, column, bins)

    @r.get('/results/{rid}/export')
    def export(rid: str, user=me):
        return FileResponse(_call(service.result_path, user, rid), filename=f'result-{rid[:8]}.parquet',
                            media_type='application/octet-stream')

    @r.get('/sessions/{sid}/history')
    def history(sid: str, user=me):
        return {'history': _call(service.history, user, sid)}

    @r.get('/sessions/{sid}/temps')
    def temps(sid: str, user=me):
        return {'temps': _call(service.temps, user, sid)}

    @r.get('/sessions/{sid}/scripts')
    def scripts(sid: str, user=me):
        return {'scripts': _call(service.scripts, user, sid)}

    @r.put('/sessions/{sid}/scripts')
    def save_script(sid: str, body: dict = Body(...), user=me):
        return _call(service.save_script, user, sid, body)

    @r.delete('/sessions/{sid}/scripts/{pid}')
    def drop_script(sid: str, pid: str, user=me):
        _call(service.delete_script, user, sid, pid)
        return {'ok': True}

    @r.get('/sessions/{sid}/dashboards')
    def dashboards(sid: str, user=me):
        return {'dashboards': _call(service.dashboards, user, sid)}

    @r.put('/sessions/{sid}/dashboards')
    def save_dashboard(sid: str, body: dict = Body(...), user=me):
        return _call(service.save_dashboard, user, sid, body)

    @r.delete('/sessions/{sid}/dashboards/{pid}')
    def drop_dashboard(sid: str, pid: str, user=me):
        _call(service.delete_dashboard, user, sid, pid)
        return {'ok': True}

    return r
