"""The assistant driving the app (llm/engine/react.py run / ui actions, engine/uiwait.py, tools/nav.py,
tools/visual.py run tools): a run action (map, model run, scan, stats) is confirmed in ask mode and runs in auto,
the page reports how it ended and the model reads that (ok, failed, still running), a transient failure is retried
twice, a hard one tells the model to fix it or say it cannot; page actions wait for the page's answer; open_tab /
navigate apply in ask mode too; navigate is validated and ends the turn; plan mode offers none of them; the
ui-result route (the one chat across pages: test_one_chat.py)."""

import json
import threading
from types import SimpleNamespace

import pytest
from test_agent_engine import _connect, script, svc, types  # noqa: F401 - svc is a fixture
from test_llm_tools import ALICE, OPENAI
from test_visual_agent import VIEW, session  # noqa: F401 - session is a fixture

from dataviz.llm import sessions, tools
from dataviz.llm.engine import confirm, react, uiwait
from dataviz.llm.tools import nav, visual

MODELS = [{'id': 'm1', 'name': 'yolov8s', 'family': 'yolo', 'format': 'pt'},
          {'id': 'm2', 'name': 'rtdetr', 'family': 'rfdetr', 'format': 'pth'}]


class _Reg:
    def models(self, owner_id=None):
        return [dict(m) for m in MODELS]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(react, '_sleep', lambda cancel, s: cancel.is_set())     # no real backoff
    monkeypatch.setattr(react, 'PING_S', 0.05)


def _run(session, text, mode, ui_ack=True):  # noqa: F811 - the fixture
    st, c = session
    opts = react.options({'mode': mode}, OPENAI)
    app = SimpleNamespace(curation=lambda s: c, models=_Reg(), model_service=None)
    r = react.prepare(ALICE, 'visual', OPENAI, react.history([{'role': 'user', 'text': text}]), {'view': VIEW},
                      opts, state=st, app=app)
    r.ctx.ui_ack = ui_ack
    return r


def drive(run, answers=(), approve=True):
    """All events; every waiting ui_action gets the next page answer (in a thread, as the page would), every
    confirmation is approved (or rejected)."""
    out, it = [], iter(answers)
    for ev in react.events(run):
        out.append(ev)
        if ev['type'] == 'ui_action' and ev.get('wait'):
            a = next(it, None)
            if a is not None:
                threading.Timer(0.02, lambda e=ev, a=a: uiwait.deliver(ALICE, run.run_id, e['id'], a)).start()
        if ev['type'] == 'confirm':
            ids = [x['id'] for x in ev['actions']]
            threading.Timer(0.02, lambda e=ev, ids=ids: confirm.decide(
                ALICE, e['confirm_id'], ids if approve else [], [] if approve else ids)).start()
    return out


def results(seen, k=1):
    return [p['tool_result'] for p in seen[k]['msgs'][-1]['parts']]


# ---------------------------------------------------------------- run actions
def test_auto_runs_and_reads_the_page_result(monkeypatch, session):  # noqa: F811 - the fixture
    seen = script(monkeypatch, ('', [('set_projection', {'method': 'umap'})]), ('Map ready.', []))
    evs = drive(_run(session, 'make a umap', 'auto'), [{'ok': True, 'detail': 'map on screen: geometry · umap · 2D'}])
    assert 'confirm' not in types(evs) and evs[-1]['type'] == 'done'
    ui = next(e for e in evs if e['type'] == 'ui_action')
    assert ui['action'] == 'set_projection' and ui['applied'] and ui['wait']
    end = next(e for e in evs if e['type'] == 'tool_end')
    assert end['ok'] and end['risk'] == 'run' and 'map on screen' in end['summary']
    assert 'map on screen' in results(seen)[0]['content'] and not results(seen)[0].get('is_error')


def test_ask_confirms_a_run_first(monkeypatch, session):  # noqa: F811 - the fixture
    script(monkeypatch, ('', [('run_model', {'model': 'yolov8s'})]), ('It ran.', []))
    evs = drive(_run(session, 'run yolov8s', 'ask'), [{'ok': True, 'detail': 'yolov8s ran'}])
    kinds = types(evs)
    assert kinds.index('confirm') < kinds.index('ui_action') < kinds.index('confirmed')
    card = next(e for e in evs if e['type'] == 'confirm')['actions'][0]
    assert card['risk'] == 'run' and card['summary'] == 'Run yolov8s on this dataset' and card['args']['model'] == 'm1'
    # declined: nothing reaches the page
    script(monkeypatch, ('', [('run_model', {'model': 'm2'})]), ('OK, not running it.', []))
    evs = drive(_run(session, 'run rtdetr', 'ask'), approve=False)
    assert 'ui_action' not in types(evs) and evs[-1]['type'] == 'done'


def test_transient_failure_is_retried(monkeypatch, session):  # noqa: F811 - the fixture
    seen = script(monkeypatch, ('', [('run_scan', {'kind': 'curation'})]), ('Scanned.', []))
    evs = drive(_run(session, 'scan', 'auto'), [{'ok': False, 'detail': 'busy: 503', 'transient': True},
                                               {'ok': False, 'detail': 'busy: 503', 'transient': True},
                                               {'ok': True, 'detail': 'scan done'}])
    ui = [e for e in evs if e['type'] == 'ui_action']
    assert [u['id'] for u in ui] == ['call_1_0', 'call_1_0.r1', 'call_1_0.r2']
    assert [e['attempt'] for e in evs if e['type'] == 'retry'] == [1, 2]
    assert next(e for e in evs if e['type'] == 'tool_end')['ok'] and 'scan done' in results(seen)[0]['content']


def test_hard_failure_goes_back_to_the_model(monkeypatch, session):  # noqa: F811 - the fixture
    seen = script(monkeypatch, ('', [('compute_stats', {})]), ('It failed.', []))
    evs = drive(_run(session, 'stats', 'auto'), [{'ok': False, 'detail': 'GPU out of memory'}])
    assert 'retry' not in types(evs) and not next(e for e in evs if e['type'] == 'tool_end')['ok']
    r = results(seen)[0]
    assert r['is_error'] and 'GPU out of memory' in r['content'] and 'cannot_do' in r['content']


def test_long_job_reports_still_running(monkeypatch, session):  # noqa: F811 - the fixture
    monkeypatch.setattr(react, 'RUN_WAIT_S', 0.2)
    seen = script(monkeypatch, ('', [('run_eval_all', {})]), ('Still going.', []))
    drive(_run(session, 'evaluate all', 'auto'), [])
    assert 'still running' in results(seen)[0]['content'] and not results(seen)[0].get('is_error')


def test_a_page_that_cannot_answer_starts_without_waiting(monkeypatch, session):  # noqa: F811 - the fixture
    seen = script(monkeypatch, ('', [('compute_stats', {})]), ('Opened.', []))
    evs = drive(_run(session, 'stats', 'auto', ui_ack=False))
    ui = next(e for e in evs if e['type'] == 'ui_action')
    assert 'wait' not in ui and 'started on the page' in results(seen)[0]['content']


def test_run_tool_validation(session):  # noqa: F811 - the fixture
    st, c = session
    ctx = react.Ctx(user=ALICE, role='visual', scope={'view': VIEW}, skills={}, state=st,
                    app=SimpleNamespace(curation=lambda s: c, models=_Reg(), model_service=None))
    assert tools.prepare(ctx, 'run_model', {'model': 'RTDETR'}, tools.REGISTRY)[0] == {'model': 'm2', 'name': 'rtdetr'}
    with pytest.raises(tools.ToolError, match='your models: yolov8s'):
        tools.prepare(ctx, 'run_model', {'model': 'nope'}, tools.REGISTRY)
    with pytest.raises(tools.ToolError, match='only applies to kind curation'):
        tools.prepare(ctx, 'run_scan', {'kind': 'cleanlab', 'model': 'm1'}, tools.REGISTRY)
    assert tools.run(ctx, 'list_models', {}, tools.REGISTRY)[0]['models'][0]['id'] == 'm1'
    ctx.app = SimpleNamespace(curation=lambda s: c, models=None, model_service=None)
    with pytest.raises(tools.ToolError, match='Settings › Models'):
        tools.prepare(ctx, 'run_eval_all', {}, tools.REGISTRY)
    assert {n for n, t in tools.REGISTRY.items() if t.risk == 'run'} == set(visual.RUN_ACTIONS)


def test_plan_mode_offers_no_run_or_page_actions(monkeypatch, session):  # noqa: F811 - the fixture
    seen = script(monkeypatch, ('Plan.', []))
    drive(_run(session, 'plan it', 'plan'))
    names = {s['name'] for s in seen[0]['specs']}
    assert not names & (set(visual.RUN_ACTIONS) | set(visual.UI_ACTIONS) | {'navigate'}) and 'list_models' in names


# ---------------------------------------------------------------- page actions
def test_page_action_waits_for_the_answer(monkeypatch, session):  # noqa: F811 - the fixture
    seen = script(monkeypatch, ('', [('set_color_by', {'by': 'suspicion'}), ('show_issue', {'issue': 'dups'})]),
                  ('Done.', []))
    drive(_run(session, 'colour it', 'auto'), [{'ok': True, 'detail': 'coloured'},
                                               {'ok': False, 'detail': 'the Find issues button is disabled'}])
    r = results(seen)
    assert 'coloured' in r[0]['content'] and not r[0].get('is_error')
    assert r[1]['is_error'] and 'disabled' in r[1]['content']


def test_open_tab_applies_in_ask_mode(monkeypatch, session):  # noqa: F811 - the fixture
    script(monkeypatch, ('', [('open_tab', {'tab': 'model'}), ('set_color_by', {'by': 'class'})]), ('Open.', []))
    evs = drive(_run(session, 'open the model tab', 'ask'), [{'ok': True, 'detail': 'Model is open'}])
    ui = {e['action']: e for e in evs if e['type'] == 'ui_action'}
    assert ui['open_tab']['applied'] and ui['open_tab'].get('wait') and not ui['set_color_by']['applied']


# ---------------------------------------------------------------- navigate
def test_navigate_is_checked_and_ends_the_turn(monkeypatch, svc):  # noqa: F811
    ctx = react.Ctx(user=ALICE, role='data', scope={}, skills={})
    with pytest.raises(tools.ToolError, match='goal is required'):
        tools.prepare(ctx, 'navigate', {'screen': 'home', 'goal': ' '}, tools.REGISTRY)
    with pytest.raises(tools.ToolError, match='session_id is required'):
        tools.prepare(ctx, 'navigate', {'screen': 'viz', 'goal': 'show the map'}, tools.REGISTRY)
    monkeypatch.setattr(nav, '_sessions', lambda user, kind, ids=None: [{'id': 's1', 'name': 'cars', 'tags': []}] if not ids or ids == ['s1'] else [])
    clean, label = tools.prepare(ctx, 'navigate', {'screen': 'viz', 'goal': 'run yolov8s', 'session_id': 's1', 'tab': 'model'}, tools.REGISTRY)
    assert clean == {'screen': 'viz', 'goal': 'run yolov8s', 'session_id': 's1', 'tab': 'model'} and label == 'Open Visualizer › model'
    with pytest.raises(tools.ToolError, match="no visualize session 'zz'"):
        tools.prepare(ctx, 'navigate', {'screen': 'viz', 'goal': 'x', 'session_id': 'zz'}, tools.REGISTRY)
    monkeypatch.setattr(nav, '_need_zone', lambda screen: None)
    clean, label = tools.prepare(ctx, 'navigate', {'screen': 'analysis', 'goal': 'count orders'}, tools.REGISTRY)
    assert clean == {'screen': 'analysis', 'goal': 'count orders'} and label == 'Open Data Analysis'
    # the engine: applied in ask mode, no second provider step (the page reloads)
    script(monkeypatch, ('Opening the visualizer.', [('navigate', {'screen': 'viz', 'goal': 'run yolov8s', 'session_id': 's1'})]))
    opts = react.options({'mode': 'ask'}, OPENAI)
    r = react.prepare(ALICE, 'data', OPENAI, react.history([{'role': 'user', 'text': 'show it'}]), {}, opts)
    r.ctx.ui_ack = True
    evs = list(react.events(r))
    ui = next(e for e in evs if e['type'] == 'ui_action')
    assert ui['action'] == 'navigate' and ui['applied'] and 'wait' not in ui and evs[-1]['type'] == 'done'
    assert tools.run(ctx, 'list_sessions', {'q': 'CAR'}, tools.REGISTRY)[0]['total'] == 2      # viz + label kinds


# ---------------------------------------------------------------- uiwait + routes + hand-off
def test_uiwait_owner_and_unknown():
    key = uiwait.open_(ALICE, 'r1', 'a1')
    with pytest.raises(confirm.AgentError) as e:
        uiwait.deliver({'id': 'mallory'}, 'r1', 'a1', {'ok': True})
    assert e.value.status == 403
    with pytest.raises(confirm.AgentError) as e:
        uiwait.deliver(ALICE, 'r1', 'nope', {'ok': True})
    assert e.value.status == 404
    uiwait.deliver(ALICE, 'r1', 'a1', {'ok': True, 'detail': 'x' * 5000, 'junk': 1})
    r = uiwait.wait(key, threading.Event(), 1)
    assert r['ok'] and len(r['detail']) == 2000 and 'junk' not in r
    cancel = threading.Event()
    cancel.set()
    assert uiwait.wait(uiwait.open_(ALICE, 'r2', 'a1'), cancel, 1) == 'cancelled'


def test_ui_result_route_and_stream(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])
    assert client.post('/api/llm/agent/ui-result', json={}).status_code == 400
    assert client.post('/api/llm/agent/ui-result', json={'run_id': 'x', 'action_id': 'y', 'ok': True}).status_code == 404
    script(monkeypatch, ('Hello.', []))
    r = client.post('/api/llm/agent/data/chat', json={'text': 'hi', 'ui_ack': True})
    assert r.status_code == 200 and json.loads(r.text.splitlines()[-1])['type'] == 'done'
    assert r.headers.get('content-encoding') != 'gzip', 'a gzipped stream holds the events back'


# ---------------------------------------------------------------- the Analysis editor
def test_analysis_reads_the_editor_and_proposes_sql_for_apply(monkeypatch):
    from dataviz.analysis import service
    monkeypatch.setattr(service, 'sources', lambda user: [{'id': 'c1', 'name': 'DB', 'kind': 'mssql'}])
    monkeypatch.setattr(service, 'user_prefs', lambda user: {})
    fix = {'sql': 'SELECT TOP 3 a FROM t', 'mode': 'replace', 'note': 'LIMIT → TOP for SQL Server'}
    seen = script(monkeypatch, ('', [('read_editor', {})]), ('', [('set_editor_query', fix)]), ('Waiting for Apply.', []))
    run = react.prepare(ALICE, 'analysis', OPENAI, react.history([{'role': 'user', 'text': 'fix my sql'}]),
                        {'page': 'analysis', 'session': 's1', 'connection': 'c1', 'sql': 'SELECT a FROM t LIMIT 3'},
                        react.options({'mode': 'ask'}, OPENAI))
    run.ctx.ui_ack = True
    evs = drive(run)
    assert 'SELECT a FROM t LIMIT 3' in results(seen)[0]['content']
    ui = [e for e in evs if e['type'] == 'ui_action']
    assert len(ui) == 1 and not ui[0]['applied'] and ui[0]['args'] == fix and ui[0]['label'] == fix['note']
    assert 'Apply' in results(seen, 2)[0]['content']          # the model is told it waits for the user
