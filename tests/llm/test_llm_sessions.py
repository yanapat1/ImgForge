"""Saved ◆ chats (llm/sessions.py, /api/llm/sessions*): 20 per user per role with least-recently-opened eviction,
pinned chats never removed, soft delete + restore, search, purge after the retention period, owner-only access, and
the chat route saving the user message and the answer with its compacted trace."""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from conftest import make_user
from dataviz import db
from dataviz.auth import settings
from dataviz.llm import providers, sessions
from dataviz.llm.engine import react
from test_agent_engine import _connect, script, svc  # noqa: F401 - svc is a fixture


def _mk(u, n, role='data', scope=None):
    return [sessions.create(u, role, scope or {}, f'chat {i}')['session']['id'] for i in range(n)]


def _raw(sid):
    with db.connect() as c:
        return c.execute(text('SELECT * FROM app.llm_sessions WHERE id = :s'), {'s': sid}).first()


def _age(sid, days, col='last_opened_at'):
    with db.begin() as c:
        c.execute(text(f"UPDATE app.llm_sessions SET {col} = now() - make_interval(days => :d) WHERE id = :s"),
                  {'d': days, 's': sid})


# ---------------------------------------------------------------- limit per role
def test_limit_removes_least_recently_opened_per_role(pg):
    u = make_user('ann')
    ids = _mk(u, 20)
    for i, sid in enumerate(ids):                      # chat 0 is the oldest
        _age(sid, 40 - i)
    sessions.get(u, ids[0])                            # opening the oldest protects it
    vis = _mk(u, 3, 'visual')
    lst = sessions.list_(u, 'data')
    assert lst['count'] == 20 and lst['max'] == 20 and lst['will_remove']['id'] == ids[1]
    made = sessions.create(u, 'data', {}, 'the 21st')
    assert made['removed']['id'] == ids[1] and made['removed']['title'] == 'chat 1'
    r = _raw(ids[1])
    assert r.deleted_at is not None and r.delete_reason == 'limit'
    assert _raw(ids[0]).deleted_at is None
    assert sessions.list_(u, 'data')['count'] == 20
    assert sessions.list_(u, 'visual')['count'] == 3 and all(_raw(v).deleted_at is None for v in vis)
    # visual has its own 20: filling it never touches data
    _mk(u, 18, 'visual')
    assert sessions.list_(u, 'data')['count'] == 20 and sessions.list_(u, 'visual')['count'] == 20


def test_pinned_never_removed_and_all_pinned_refused(pg):
    u = make_user('ann')
    ids = _mk(u, 20)
    for sid in ids[:19]:
        sessions.update(u, sid, pinned=True)
    made = sessions.create(u, 'data', {}, 'x')
    assert made['removed']['id'] == ids[19]
    sessions.update(u, made['session']['id'], pinned=True)
    lst = sessions.list_(u, 'data')
    assert lst['will_remove'] is None and lst['all_pinned'] is True
    with pytest.raises(sessions.SessionError) as e:
        sessions.create(u, 'data', {}, 'y')
    assert e.value.status == 409 and '20 pinned chats' in str(e.value)
    assert sessions.list_(u, 'data')['count'] == 20


def test_concurrent_creates_never_exceed_the_limit(pg):
    u = make_user('ann')
    _mk(u, 15)
    errs = []

    def work():
        try:
            for _ in range(3):
                sessions.create(u, 'data', {}, 'c')
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ths = [threading.Thread(target=work) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert not errs
    with db.connect() as c:
        n = c.execute(text("SELECT count(*) FROM app.llm_sessions WHERE owner_id = :u AND deleted_at IS NULL"),
                      {'u': u['id']}).scalar()
        total = c.execute(text('SELECT count(*) FROM app.llm_sessions WHERE owner_id = :u'), {'u': u['id']}).scalar()
    assert n == 20 and total == 27


# ---------------------------------------------------------------- delete, search, restore
def test_delete_one_several_scope_and_restore(pg):
    u = make_user('ann')
    a = _mk(u, 3, scope={'bucket_id': 'b1'})
    b = _mk(u, 2, scope={'bucket_id': 'b2'})
    sessions.update(u, a[0], pinned=True)
    sessions.append(u, b[0], 'user', 'find the night images')
    assert [s['id'] for s in sessions.list_(u, 'data', q='night')['sessions']] == [b[0]]
    assert {s['id'] for s in sessions.list_(u, 'data', q='CHAT 1')['sessions']} == {a[1], b[1]}
    assert sessions.list_(u, 'data', q='50%_')['sessions'] == []
    assert {s['id'] for s in sessions.list_(u, 'data', scope={'bucket_id': 'b1'})['sessions']} == set(a)
    assert sessions.list_(u, 'data')['sessions'][0]['id'] == a[0]          # pinned first
    sessions.delete_one(u, b[0])
    assert sessions.list_(u, 'data', q='night')['sessions'] == []
    with pytest.raises(sessions.SessionError) as e:
        sessions.get(u, b[0])
    assert e.value.status == 404
    assert _raw(b[0]) is not None and _raw(b[0]).delete_reason == 'user'      # the row stays
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM app.llm_messages WHERE session_id = :s'), {'s': b[0]}).scalar() == 1
    # by scope: pinned kept unless include_pinned
    out = sessions.delete(u, role='data', scope={'bucket_id': 'b1'})
    assert set(out['deleted']) == set(a[1:])
    assert sessions.delete(u, role='data', scope={'bucket_id': 'b1'}, include_pinned=True)['deleted'] == [a[0]]
    # several by id
    assert sessions.delete(u, ids=[b[1]])['deleted'] == [b[1]]
    assert sessions.list_(u, 'data')['count'] == 0
    # undo
    res = sessions.restore(u, [b[0], a[1]])
    assert {s['id'] for s in res['restored']} == {b[0], a[1]}
    assert sessions.list_(u, 'data')['count'] == 2 and sessions.get(u, b[0])['messages'][0]['text'] == \
        'find the night images'


def test_restore_refused_past_the_limit(pg):
    u = make_user('ann')
    ids = _mk(u, 20)
    sessions.delete_one(u, ids[0])
    sessions.create(u, 'data', {}, 'new')
    with pytest.raises(sessions.SessionError) as e:
        sessions.restore(u, [ids[0]])
    assert e.value.status == 409
    sessions.delete_one(u, ids[1])
    assert sessions.restore(u, [ids[0]])['restored'][0]['id'] == ids[0]


def test_messages_history_usage_and_cap(pg, monkeypatch):
    u = make_user('ann')
    sid = sessions.create(u, 'data', {}, '')['session']['id']
    sessions.append(u, sid, 'user', 'hello there ' * 10)
    sessions.append(u, sid, 'assistant', 'hi', trace=[{'type': 'meta'}], usage={'in': 5, 'out': 2})
    sessions.append(u, sid, 'assistant', 'again', usage={'in': 1, 'out': 1})
    g = sessions.get(u, sid)
    assert len(g['session']['title']) <= 60 and g['session']['title'].startswith('hello there')
    assert g['session']['usage'] == {'in': 6, 'out': 3} and g['session']['n_messages'] == 3
    assert [m['seq'] for m in g['messages']] == [1, 2, 3] and g['messages'][1]['trace'] == [{'type': 'meta'}]
    assert sessions.history(u, sid)[-1] == {'role': 'assistant', 'text': 'again', 'page': {}}
    monkeypatch.setattr(sessions, 'MAX_MESSAGES', 4)
    for i in range(5):
        sessions.append(u, sid, 'user', f'm{i}')
    g = sessions.get(u, sid)
    assert [m['text'] for m in g['messages']] == ['m1', 'm2', 'm3', 'm4'] and g['session']['n_messages'] == 4


# ---------------------------------------------------------------- purge + admin settings
def test_purge_after_retention(pg):
    u = make_user('ann')
    ids = _mk(u, 4)
    sessions.append(u, ids[0], 'user', 'x')
    sessions.delete(u, ids=ids[:3])
    _age(ids[0], 40, 'deleted_at')
    _age(ids[1], 800, 'deleted_at')
    _age(ids[2], 5, 'deleted_at')
    assert sessions.purge() == 1 and _raw(ids[1]) is None and _raw(ids[0]) is not None     # default 730 days
    settings.update({'llm_chat_retention_days': 30})
    assert sessions.purge() == 1 and _raw(ids[0]) is None
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM app.llm_messages WHERE session_id = :s'), {'s': ids[0]}).scalar() == 0
    assert _raw(ids[2]) is not None and _raw(ids[3]) is not None                             # recent / live stay
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert sessions.purge(now=later) == 1 and _raw(ids[3]) is not None


def test_admin_settings_change_limit_and_retention(client):
    r = client.put('/api/admin/settings', json={'llm_max_sessions': 3, 'llm_chat_retention_days': 30})
    assert r.status_code == 200 and r.json()['llm_max_sessions'] == 3
    assert client.put('/api/admin/settings', json={'llm_max_sessions': 0}).status_code == 400
    assert client.put('/api/admin/settings', json={'llm_chat_retention_days': 10}).status_code == 400
    for i in range(4):
        made = client.post('/api/llm/sessions', json={'role': 'data', 'title': f't{i}'}).json()
    assert made['removed']['title'] == 't0'
    lst = client.get('/api/llm/sessions', params={'role': 'data'}).json()
    assert lst['count'] == 3 and lst['max'] == 3 and sessions.retention_days() == 30


# ---------------------------------------------------------------- routes + privacy
def test_routes_and_other_users_get_404(client_as):
    ann, bob = client_as(make_user('ann')), client_as(make_user('bob'))
    made = ann.post('/api/llm/sessions', json={'role': 'data', 'scope': {'bucket_id': 'b1'}, 'title': 'Night'})
    assert made.status_code == 200
    sid = made.json()['session']['id']
    assert ann.post('/api/llm/sessions', json={'role': 'nope'}).status_code == 400
    lst = ann.get('/api/llm/sessions', params={'role': 'data', 'scope': json.dumps({'bucket_id': 'b1'})}).json()
    assert [s['id'] for s in lst['sessions']] == [sid] and lst['next_cursor'] is None
    assert ann.get('/api/llm/sessions', params={'role': 'data', 'bucket_id': 'b2'}).json()['sessions'] == []
    assert ann.patch(f'/api/llm/sessions/{sid}', json={'title': 'Renamed', 'pinned': True}).json()['session'][
        'pinned'] is True
    assert ann.get(f'/api/llm/sessions/{sid}').json()['session']['title'] == 'Renamed'
    for call in (lambda c: c.get(f'/api/llm/sessions/{sid}'),
                 lambda c: c.patch(f'/api/llm/sessions/{sid}', json={'title': 'x'}),
                 lambda c: c.delete(f'/api/llm/sessions/{sid}')):
        assert call(bob).status_code == 404
    assert bob.post('/api/llm/sessions/delete', json={'ids': [sid]}).json() == {'deleted': []}
    assert bob.post('/api/llm/sessions/restore', json={'ids': [sid]}).status_code == 404
    assert bob.get('/api/llm/sessions', params={'role': 'data'}).json()['count'] == 0
    assert ann.delete(f'/api/llm/sessions/{sid}').status_code == 200
    assert ann.get(f'/api/llm/sessions/{sid}').status_code == 404
    assert ann.post('/api/llm/sessions/restore', json={'ids': [sid]}).json()['restored'][0]['id'] == sid
    assert ann.post('/api/llm/sessions/delete', json={'role': 'data', 'include_pinned': True}).json() == {
        'deleted': [sid]}
    # paging
    for i in range(3):
        ann.post('/api/llm/sessions', json={'role': 'visual', 'title': f'v{i}'})
    p1 = ann.get('/api/llm/sessions', params={'role': 'visual', 'limit': 2}).json()
    p2 = ann.get('/api/llm/sessions', params={'role': 'visual', 'limit': 2, 'cursor': p1['next_cursor']}).json()
    assert len(p1['sessions']) == 2 and len(p2['sessions']) == 1 and p2['next_cursor'] is None


def test_trace_compaction():
    evs = [{'type': 'meta', 'model': 'm', 'mode': 'ask', 'effort': 'low', 'run_id': 'r'}, {'type': 'step', 'n': 1},
           {'type': 'thinking', 'text': 'a' * 3000}, {'type': 'thinking', 'text': 'b' * 3000},
           {'type': 'thinking_end', 'ms': 5}, {'type': 'text', 'text': 'x'},
           {'type': 'tool_start', 'id': 't1', 'name': 'list', 'args': {'a': 1}, 'risk': 'read'},
           {'type': 'tool_end', 'id': 't1', 'name': 'list', 'ok': True, 'summary': '3 files', 'ms': 4,
            'preview': 'p' * 3000, 'undo_id': 'u1'},
           {'type': 'todo', 'items': [{'text': 'a'}]}, {'type': 'todo', 'items': [{'text': 'b'}]},
           {'type': 'retry', 'reason': 'busy'}, {'type': 'retry', 'reason': 'busy2'}, {'type': 'ping'},
           {'type': 'confirm', 'confirm_id': 'c1', 'actions': [{'id': 'a1'}]},
           {'type': 'confirmed', 'confirm_id': 'c1', 'results': [{'id': 'a1', 'ok': True}], 'status': 'decided'},
           {'type': 'search', 'query': 'q', 'results': []}, {'type': 'notice', 'level': 'info', 'text': 'n'},
           {'type': 'done', 'usage': {'in': 1, 'out': 2}, 'ms': 9, 'steps': 1}]
    tr = sessions.compact_trace(evs)
    kinds = [t['type'] for t in tr]
    assert kinds == ['meta', 'thinking', 'tool', 'todo', 'retry', 'confirm', 'search', 'notice', 'done']
    assert tr[0] == {'type': 'meta', 'model': 'm', 'provider': None, 'mode': 'ask', 'effort': 'low'}
    assert len(tr[1]['text']) <= 4001 and tr[1]['ms'] == 5
    assert tr[2]['ok'] is True and tr[2]['undo_id'] == 'u1' and len(tr[2]['preview']) <= 1501 and tr[2]['args'] == {'a': 1}
    assert tr[3]['items'] == [{'text': 'b'}] and tr[4]['count'] == 2
    assert tr[5]['status'] == 'decided' and tr[5]['actions'] == [{'id': 'a1'}]


# ---------------------------------------------------------------- the chat route saves every turn
def _events(r):
    return [json.loads(x) for x in r.text.splitlines() if x]


def test_chat_route_persists_turns(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])
    titles = []
    monkeypatch.setattr(providers, 'complete', lambda p, system, msgs, json=False, max_tokens=None, quiet=False:
                        (titles.append(max_tokens) or 'Tag the cars', {'in': 1, 'out': 1}))
    script(monkeypatch, ('', [('add_tags', {'bucket': 'cars', 'tags': ['x']})]), ('Tagged.', []))
    r = client.post('/api/llm/agent/data/chat', json={'messages': [{'role': 'user', 'text': 'tag cars'}],
                                                      'scope': {'bucket_id': 'b1'}, 'mode': 'auto'})
    evs = _events(r)
    assert evs[0]['type'] == 'meta' and evs[1]['type'] == 'session' and evs[-1]['type'] == 'done'
    sid = evs[1]['session_id']
    assert evs[0]['session_id'] == sid and evs[1]['title'] == 'tag cars' and evs[1]['removed'] is None
    for _ in range(50):                                     # the cheap title runs in a thread
        if client.get(f'/api/llm/sessions/{sid}').json()['session']['title'] == 'Tag the cars':
            break
        threading.Event().wait(0.05)
    g = client.get(f'/api/llm/sessions/{sid}').json()
    assert g['session']['title'] == 'Tag the cars' and titles == [16]
    assert g['session']['scope'] == {'bucket_id': 'b1'} and g['session']['mode'] == 'auto'
    assert [(m['role'], m['text']) for m in g['messages']] == [('user', 'tag cars'), ('assistant', 'Tagged.')]
    tr = g['messages'][1]['trace']
    assert any(t['type'] == 'tool' and t['name'] == 'add_tags' and t['ok'] for t in tr)
    assert not any(t['type'] in ('text', 'step', 'ping') for t in tr) and g['messages'][1]['usage']['out'] == 4
    # the next turn: only the text, the history comes from the database
    seen = script(monkeypatch, ('Done again.', []))
    r = client.post('/api/llm/agent/data/chat', json={'session_id': sid, 'text': 'and again'})
    assert _events(r)[1]['session_id'] == sid
    sent = [m['parts'][0]['text'] for m in seen[0]['msgs']]
    assert sent[0] == 'tag cars' and sent[-1] == 'and again' and 'Tagged.' in sent
    assert client.get(f'/api/llm/sessions/{sid}').json()['session']['n_messages'] == 4
    # another user's / a deleted chat: 404 before the stream
    client.delete(f'/api/llm/sessions/{sid}')
    assert client.post('/api/llm/agent/data/chat', json={'session_id': sid, 'text': 'x'}).status_code == 404
    # ephemeral: nothing stored
    n = client.get('/api/llm/sessions', params={'role': 'data'}).json()['count']
    script(monkeypatch, ('ok', []))
    r = client.post('/api/llm/agent/data/chat', json={'session_id': None, 'ephemeral': True,
                                                      'messages': [{'role': 'user', 'text': 'hi'}]})
    assert [e['type'] for e in _events(r)][-1] == 'done' and not any(e['type'] == 'session' for e in _events(r))
    assert client.get('/api/llm/sessions', params={'role': 'data'}).json()['count'] == n


def test_chat_route_all_pinned_is_409(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])
    settings.update({'llm_max_sessions': 1})
    sid = client.post('/api/llm/sessions', json={'role': 'data'}).json()['session']['id']
    client.patch(f'/api/llm/sessions/{sid}', json={'pinned': True})
    r = client.post('/api/llm/agent/data/chat', json={'text': 'hi'})
    assert r.status_code == 409 and '1 pinned chats' in r.json()['detail']


def test_stopped_turn_is_saved(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])

    def stream_tools(p, system, msgs, specs, think=None, native_web=False, max_tokens=None):
        yield 'text', 'Partial answer'
        for r in list(react._runs.values()):
            r['cancel'].set()                               # the user pressed Stop
        yield 'text', ' never shown'
        yield 'usage', {'in': 3, 'out': 1}
    monkeypatch.setattr(providers, 'stream_tools', stream_tools)
    r = client.post('/api/llm/agent/data/chat', json={'text': 'long job'})
    evs = _events(r)
    assert any(e['type'] == 'notice' and e['text'] == 'Stopped.' for e in evs)
    sid = evs[1]['session_id']
    msgs = client.get(f'/api/llm/sessions/{sid}').json()['messages']
    assert msgs[-1]['role'] == 'assistant' and msgs[-1]['text'] == 'Partial answer\n\n(stopped)'
