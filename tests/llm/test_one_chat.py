"""One ◆ chat across every page (llm/sessions.py role 'app', engine/roles.py router, server/routes/agent.py): a
chat started on one page continues on any other, each message keeps the page it was written on, the history marks
where the user moved, a tool or skill of another page points to navigate, navigate refuses a page whose assistant
is off, and only one turn at a time answers a chat (another tab gets 409)."""

import json
import threading
from types import SimpleNamespace

import pytest
from test_agent_engine import _connect, script, svc  # noqa: F401 - svc is a fixture

from dataviz.llm import sessions, tools
from dataviz.llm.engine import react, roles
from dataviz.llm.tools import nav
from dataviz.llm.tools.skills import _use_skill


def _lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x]


def test_app_chats_list_and_count_every_role(client, monkeypatch):
    u = client.user
    old = sessions.create(u, 'bucket', {}, 'an older Bucket chat')['session']
    one = sessions.create(u, 'app', {}, 'one chat')['session']
    listed = sessions.list_(u, 'app')
    assert {s['id'] for s in listed['sessions']} == {old['id'], one['id']} and listed['count'] == 2
    assert [s['id'] for s in sessions.list_(u, 'bucket')['sessions']] == [old['id']]
    # the limit counts every chat: the next one removes the oldest of any role
    monkeypatch.setattr(sessions, 'max_sessions', lambda: 2)
    made = sessions.create(u, 'app', {}, 'third')
    assert made['removed']['id'] == old['id']
    with pytest.raises(sessions.SessionError) as e:
        sessions.restore(u, [old['id']])
    assert e.value.status == 409
    # every chat of the user, one role or all
    assert sessions.delete(u, role='app')['deleted']
    assert sessions.list_(u, 'app')['count'] == 0


def test_history_marks_page_changes():
    dc = {'role': 'bucket', 'screen': 'bucket', 'label': 'Bucket'}
    viz = {'role': 'visual', 'screen': 'viz', 'label': 'Visualizer · a1'}
    h = react.history([{'role': 'user', 'text': 'find the truck folder', 'page': dc},
                       {'role': 'assistant', 'text': 'traffic/val', 'page': dc},
                       {'role': 'user', 'text': 'colour by class', 'page': viz},
                       {'role': 'assistant', 'text': 'done', 'page': viz},
                       {'role': 'user', 'text': 'tag them', 'page': dc}])
    texts = [m['parts'][0]['text'] for m in h]
    assert texts[0] == 'find the truck folder' and texts[1] == 'traffic/val'
    assert texts[2] == '[on Visualizer · a1] colour by class'
    assert texts[4].startswith('[the user moved to Bucket') and texts[4].endswith('tag them')
    # no pages (older chats, the page's own transcript): unchanged
    assert [m['parts'][0]['text'] for m in react.history([{'role': 'user', 'text': 'hi'}])] == ['hi']


def test_one_chat_continues_on_another_page(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['bucket_data', 'assistant'])
    seen = script(monkeypatch, ('Here are your sessions.', []), ('Bucket cars has 3 folders.', []),
                  ('Continuing.', []))
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'show my sessions', 'scope': {'page': 'home'}}))
    sid = next(e for e in evs if e['type'] == 'session')['session_id']
    g = client.get(f'/api/llm/sessions/{sid}').json()
    assert g['session']['role'] == 'app' and g['session']['last_page']['label'] == 'Home'
    # the same chat on the Bucket page: the model is told the user moved, with the Bucket page's prompt
    r = client.post('/api/llm/agent/bucket/chat', json={'text': 'what is in cars?', 'session_id': sid,
                                                      'scope': {'bucket': 'b1'}})
    assert r.status_code == 200
    last = seen[1]['msgs'][-1]['parts'][0]['text']
    assert last.startswith('[the user moved to Bucket') and last.endswith('what is in cars?')
    assert '## Other pages' in seen[1]['system'] and 'Visualizer (screen viz' in seen[1]['system']
    # a navigate goal arrives as a continued message
    client.post('/api/llm/agent/bucket/chat', json={'text': 'tag the trucks', 'session_id': sid, 'continued': True,
                                                  'scope': {'bucket': 'b1'}})
    msgs = client.get(f'/api/llm/sessions/{sid}').json()['messages']
    assert [m['page'].get('label') for m in msgs] == ['Home', 'Home', 'Bucket', 'Bucket',
                                                      'Bucket', 'Bucket']
    assert msgs[4]['page'].get('continued') is True and not msgs[5]['page'].get('continued')
    assert client.get('/api/llm/sessions?role=app').json()['count'] == 1


def test_one_turn_at_a_time(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['bucket_data'])
    script(monkeypatch, ('One.', []), ('Two.', []))
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'hello'}))
    sid = next(e for e in evs if e['type'] == 'session')['session_id']
    busy = threading.Event()
    assert react.claim_chat(sid, busy)                         # another tab's run is answering it
    r = client.post('/api/llm/agent/bucket/chat', json={'text': 'again', 'session_id': sid})
    assert r.status_code == 409 and 'still answering' in r.json()['detail']
    busy.set()                                                 # that run ended (stopped / closed)
    assert client.post('/api/llm/agent/bucket/chat', json={'text': 'again', 'session_id': sid}).status_code == 200
    assert react.claim_chat(sid, threading.Event()), 'the stream released the chat when it ended'


def test_another_pages_tool_or_skill_points_to_navigate(monkeypatch):
    seen = script(monkeypatch, ('', [('set_projection', {'feature': 'x', 'method': 'umap'})]), ('OK.', []))
    run = react.prepare({'id': 'u'}, 'bucket', {'kind': 'openai', 'model': 'gpt-x'},
                        react.history([{'role': 'user', 'text': 't-SNE please'}]), {}, react.options({}, {}))
    assert 'set_projection' not in run.tools and 'navigate' in run.tools
    list(react.events(run))
    res = seen[1]['msgs'][-1]['parts'][0]['tool_result']
    assert res['is_error'] and 'Visualizer tool' in res['content'] and "navigate(screen='viz'" in res['content']
    ctx = SimpleNamespace(role='bucket', skills={})
    with pytest.raises(tools.ToolError, match='Visualizer skill'):
        _use_skill(ctx, {'name': 'read-the-map'})


def test_navigate_refuses_a_page_whose_assistant_is_off():
    ctx = SimpleNamespace(user={'id': 'u'}, roles_on={'bucket': True, 'visual': False})
    with pytest.raises(tools.ToolError, match='switched off'):
        nav._check_nav(ctx, {'screen': 'viz', 'goal': 'run t-SNE', 'session_id': 'a1'})
    assert nav._check_nav(ctx, {'screen': 'home', 'goal': 'list them'})[0]['screen'] == 'home'


def test_page_of():
    assert roles.page_of('bucket', {'page': 'home'}) == {'role': 'bucket', 'screen': 'home', 'label': 'Home'}
    p = roles.page_of('visual', {'view': {'tab': 'model'}}, {'dataset_id': 'a1', 'name': 'traffic'})
    assert p == {'role': 'visual', 'screen': 'viz', 'label': 'Visualizer · traffic', 'session_id': 'a1', 'tab': 'model'}
    assert roles.page_of('bucket', {'bucket': 'b1', 'path': 'x/val'})['label'] == 'Bucket · val'


def test_regenerate_and_edit_replace_the_rest_of_the_chat(client, monkeypatch, svc):  # noqa: F811
    """from_seq (a regenerated answer / an edited message): the chat goes on from that message; it and every later
    one are deleted, the model never sees them, and the session event names the seq the new user message got."""
    _connect(client, ['bucket_data'])
    seen = script(monkeypatch, ('A1', []), ('A2', []), ('A2 again', []), ('B1', []))
    seq = lambda evs: next(e for e in evs if e['type'] == 'session')['user_seq']  # noqa: E731
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'q1'}))
    assert seq(evs) == 1
    sid = next(e for e in evs if e['type'] == 'session')['session_id']
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'q2', 'session_id': sid}))
    assert seq(evs) == 3
    # regenerate the 2nd answer: resend q2 from its seq
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'q2', 'session_id': sid, 'from_seq': 3}))
    assert seq(evs) == 3
    assert [m['parts'][0]['text'] for m in seen[2]['msgs']] == ['q1', 'A1', 'q2']
    msgs = client.get(f'/api/llm/sessions/{sid}').json()['messages']
    assert [(m['seq'], m['text']) for m in msgs] == [(1, 'q1'), (2, 'A1'), (3, 'q2'), (4, 'A2 again')]
    # edit the first message: everything is replaced
    _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'q1 edited', 'session_id': sid, 'from_seq': 1}))
    assert [m['parts'][0]['text'] for m in seen[3]['msgs']] == ['q1 edited']
    msgs = client.get(f'/api/llm/sessions/{sid}').json()['messages']
    assert [m['text'] for m in msgs] == ['q1 edited', 'B1']
    assert client.get(f'/api/llm/sessions/{sid}').json()['session']['n_messages'] == 2
    # bad from_seq / no saved chat
    for bad in ({'from_seq': 0, 'session_id': sid}, {'from_seq': 'x', 'session_id': sid}, {'from_seq': 1}):
        assert client.post('/api/llm/agent/bucket/chat', json={'text': 'x', **bad}).status_code == 400


def test_truncate_clears_a_summary_that_covered_the_cut(client):
    u = client.user
    sid = sessions.create(u, 'app', {}, 'c')['session']['id']
    for i in range(1, 7):
        sessions.append(u, sid, 'user' if i % 2 else 'assistant', f'm{i}')
    from sqlalchemy import text as sql
    from dataviz import db
    with db.begin() as c:
        c.execute(sql("UPDATE app.llm_sessions SET summary = 'S', summary_seq = 4 WHERE id = :s"), {'s': sid})
    cx = sessions.context(u, sid, before=5)                  # the cut is after the summary: kept
    assert cx['summary'] == 'S' and [m['text'] for m in cx['messages']] == []
    cx = sessions.context(u, sid, before=3)                  # the summary covers the cut: every earlier message
    assert cx['summary'] is None and [m['text'] for m in cx['messages']] == ['m1', 'm2']
    assert sessions.truncate(u, sid, 5) == 2
    assert sessions.context(u, sid)['summary'] == 'S'
    assert sessions.truncate(u, sid, 3) == 2
    cx = sessions.context(u, sid)
    assert cx['summary'] is None and [m['text'] for m in cx['messages']] == ['m1', 'm2']
    with pytest.raises(sessions.SessionError):
        sessions.truncate(u, sid, 0)


def test_a_bucket_chat_is_saved_with_its_bucket(client, monkeypatch, svc):  # noqa: F811
    """The Bucket page sends the open bucket as scope.bucket: the chat is filed under it (History › This bucket)."""
    _connect(client, ['bucket_data'])
    script(monkeypatch, ('Three folders.', []), ('Hello.', []))
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'what is in here?', 'scope': {'bucket': 'b1', 'path': 'val'}}))
    sid = next(e for e in evs if e['type'] == 'session')['session_id']
    assert client.get(f'/api/llm/sessions/{sid}').json()['session']['scope'] == {'bucket_id': 'b1'}
    assert [s['id'] for s in client.get('/api/llm/sessions', params={'role': 'app', 'bucket_id': 'b1'}).json()['sessions']] == [sid]
    assert client.get('/api/llm/sessions', params={'role': 'app', 'bucket_id': 'b2'}).json()['sessions'] == []
    # a chat started on Home has no bucket
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'hi', 'scope': {'page': 'home'}}))
    home = next(e for e in evs if e['type'] == 'session')['session_id']
    assert client.get(f'/api/llm/sessions/{home}').json()['session']['scope'] == {}


def test_the_chat_keeps_its_model_and_mode_on_every_page(client, monkeypatch, svc):  # noqa: F811
    """The chips (model, mode, effort, web) are saved once for the one chat, not per page."""
    _connect(client, ['bucket_data'])
    # what a page remembered before chats were shared still counts for that page
    client.put('/api/llm/config', json={'chat': {'data': {'mode': 'plan'}}})
    assert client.get('/api/llm/agent/data/catalog').json()['chat'] == {'mode': 'plan'}
    assert client.get('/api/llm/agent/bucket/catalog').json()['chat'] == {}
    # the chat's own settings: every page reads them
    client.put('/api/llm/config', json={'chat': {'app': {'mode': 'auto', 'effort': 'high', 'nope': 1}}})
    for role in ('bucket', 'data'):
        assert client.get(f'/api/llm/agent/{role}/catalog').json()['chat'] == {'mode': 'auto', 'effort': 'high'}
    script(monkeypatch, ('Hello.', []))
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'hi', 'scope': {'page': 'home'}}))
    meta = next(e for e in evs if e['type'] == 'meta')
    assert meta['mode'] == 'auto' and meta['effort'] == 'high'


def test_a_page_drawn_turn_is_saved_as_a_note(client, monkeypatch, svc):  # noqa: F811
    """A label check / auto-tag the page draws itself is kept in the chat as two plain messages (note route)."""
    _connect(client, ['bucket_data'])
    seen = script(monkeypatch, ('It found 2 wrong boxes.', []))
    r = client.post('/api/llm/agent/bucket/note', json={'request': 'Check the labels of 5 boxes',
                                                        'result': 'Label check: 2 likely wrong.', 'scope': {'bucket': 'b1'}})
    sid = r.json()['session_id']
    msgs = client.get(f'/api/llm/sessions/{sid}').json()['messages']
    assert [(m['role'], m['text']) for m in msgs] == [('user', 'Check the labels of 5 boxes'),
                                                      ('assistant', 'Label check: 2 likely wrong.')]
    assert client.post('/api/llm/agent/bucket/note', json={'session_id': sid, 'request': 'x', 'result': ' '}).status_code == 400
    client.post('/api/llm/agent/bucket/chat', json={'text': 'what did the check find?', 'session_id': sid})
    assert '2 likely wrong' in json.dumps(seen[0]['msgs']), 'the model sees the note'
