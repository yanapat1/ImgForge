"""Data Analysis: a database query (run_query) follows the chat's mode. ask / plan: a confirm card (risk 'query',
the SQL editable) first; auto: it runs at once. A side run (consult / delegate) cannot show a card, so it has no
run_query unless the chat is in auto."""

from test_agent_engine import ALICE, drain, script, start, types

from dataviz.llm.engine import confirm, react, roles, subrun
from dataviz.llm.tools import analysis as atools


def _fake(monkeypatch):
    seen = []

    def run_for_ai(user, sid, cid, db, sql, name, wait):
        seen.append(sql)
        return {'status': 'done', 'rows': 3, 'result_id': 'r1', 'columns': ['n']}
    monkeypatch.setattr(atools._svc(), 'run_for_ai', run_for_ai)
    monkeypatch.setattr(atools, '_target', lambda ctx, a: ('c1', 'db'))
    monkeypatch.setattr(atools, '_sid', lambda ctx: 's1')
    monkeypatch.setitem(roles.SCOPES, 'analysis', lambda *a, **k: '')      # no database behind the page
    return seen


def test_ask_mode_confirms_the_query_and_takes_the_edited_sql(monkeypatch):
    seen = _fake(monkeypatch)
    script(monkeypatch, ('', [('run_query', {'sql': 'SELECT count(*) FROM t'})]), ('3 rows.', []))
    run = start('how many?', role='analysis', mode='ask')
    evs = []
    for ev in react.events(run):
        evs.append(ev)
        if ev['type'] == 'confirm':
            a = ev['actions'][0]
            assert a['risk'] == 'query' and a['tool'] == 'run_query' and 'SELECT count(*) FROM t' in a['summary']
            assert not seen, 'nothing ran before the OK'
            import threading
            threading.Timer(0.05, lambda e=ev: confirm.decide(ALICE, e['confirm_id'], [a['id']], [],
                                                              edits={a['id']: {'sql': 'SELECT 1'}})).start()
    assert 'confirm' in types(evs) and evs[-1]['type'] == 'done'
    assert seen == ['SELECT 1']


def test_auto_mode_runs_the_query_at_once(monkeypatch):
    seen = _fake(monkeypatch)
    script(monkeypatch, ('', [('run_query', {'sql': 'SELECT 2'})]), ('ok', []))
    evs = drain(start('how many?', role='analysis', mode='auto'))
    assert 'confirm' not in types(evs) and seen == ['SELECT 2']


def test_declined_query_does_not_run(monkeypatch):
    seen = _fake(monkeypatch)
    script(monkeypatch, ('', [('run_query', {'sql': 'SELECT 3'})]), ('ok', []))
    evs = drain(start('q', role='analysis', mode='plan'), lambda ev: ([], [ev['actions'][0]['id']]))
    assert 'confirm' in types(evs) and not seen


def test_side_runs_have_no_query_unless_auto(monkeypatch):
    _fake(monkeypatch)
    got = {}

    def fake_events(sub):
        got['tools'] = set(sub.tools)
        yield {'type': 'done', 'usage': {}}
    monkeypatch.setattr(react, 'events', fake_events)
    for mode, has in (('ask', False), ('plan', False), ('auto', True)):
        parent = start('x', role='analysis', mode=mode)
        subrun.run_sub(parent.ctx, 'analysis', 'look it up')
        assert ('run_query' in got['tools']) is has, mode
        assert 'describe_table' in got['tools']
