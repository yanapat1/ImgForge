"""Context compaction of a ◆ chat (llm/engine/summarize.py): the cut keeps the newest messages starting on a user
turn, the summariser reads the stored traces (tool calls, results, ids), summaries are incremental and only stored
over the one they were built from, transient failures are retried, the chat route compacts on its own past 80% of
the budget or HISTORY_TURNS (and goes on without a summary when that fails), the model then gets the summary in its
system prompt plus only the newer messages, and POST /api/llm/agent/<role>/compact (/compact) folds on demand."""

import json

import pytest
from conftest import make_user
from test_agent_engine import _connect, script, svc  # noqa: F401 - svc is a fixture

from dataviz.llm import providers, sessions
from dataviz.llm.engine import react, summarize
from dataviz.llm.providers import LLMError


def _events(r):
    return [json.loads(x) for x in r.text.splitlines() if x]


def _rows(n):
    return [{'seq': i + 1, 'role': 'user' if i % 2 == 0 else 'assistant', 'text': f'm{i + 1}'} for i in range(n)]


def _fake_complete(monkeypatch, answers=None):
    """providers.complete scripted: returns 'SUMMARY n' (or the next item of answers; an exception is raised)."""
    calls = []
    it = iter(answers or [])

    def complete(p, system, msgs, json=False, max_tokens=None, quiet=False):
        calls.append({'system': system, 'text': msgs[0]['parts'][0]['text'], 'max_tokens': max_tokens})
        a = next(it, None)
        if isinstance(a, Exception):
            raise a
        return (a if a is not None else f'SUMMARY {len(calls)}'), {'in': 1, 'out': 1}
    monkeypatch.setattr(providers, 'complete', complete)
    monkeypatch.setattr(summarize.time, 'sleep', lambda s: None)
    return calls


# ---------------------------------------------------------------- pure parts
def test_needs_and_cap():
    assert not summarize.needs(7999, 10000, 5) and summarize.needs(8001, 10000, 5)
    assert summarize.needs(10, 10000, sessions.HISTORY_TURNS + 1)
    assert summarize.summary_cap(4000) == summarize.CAP_MIN and summarize.summary_cap(100000) == summarize.CAP_MAX
    assert summarize.CAP_MIN <= summarize.summary_cap(20000) <= summarize.CAP_MAX


def test_plan_cut_keeps_newest_starting_on_user():
    assert summarize.plan_cut(_rows(6), 6) is None
    assert summarize.plan_cut(_rows(10), 6) == 4                # kept: 5..10, starts with user (seq 5)
    assert summarize.plan_cut(_rows(10), 5) == 4                # 6 would start on an assistant: one more kept
    assert summarize.plan_cut(_rows(3), 2) is None              # the kept part would start before the first user
    rows = [{'seq': 1, 'role': 'assistant', 'text': 'x'}, {'seq': 2, 'role': 'assistant', 'text': 'y'}]
    assert summarize.plan_cut(rows, 1) is None                  # no user turn to start the kept part


def test_transcript_reads_traces():
    rows = [{'seq': 1, 'role': 'user', 'text': 'tag the cars'},
            {'seq': 2, 'role': 'assistant', 'text': 'Tagged 214.', 'trace': [
                {'type': 'meta', 'model': 'gpt-x', 'mode': 'auto'},
                {'type': 'tool', 'name': 'add_tags', 'args': {'tags': ['dup-v3']}, 'ok': True, 'summary': '214 files',
                 'undo_id': 'u-81f2'},
                {'type': 'tool', 'name': 'delete', 'args': {'path': 'x'}, 'ok': False, 'summary': 'denied'},
                {'type': 'confirm', 'actions': [{'summary': 'delete x'}], 'results': [{'declined': True, 'summary': 'x'}],
                 'status': 'decided'},
                {'type': 'ui_action', 'action': 'open_tab', 'args': {'tab': 'model'}, 'applied': True},
                {'type': 'cannot', 'reason': 'no training', 'alternative': 'use Eval'},
            ]}]
    t = summarize.transcript(rows)
    assert '[USER #1]' in t and '[ASSISTANT #2]' in t and 'Tagged 214.' in t
    assert 'add_tags' in t and 'dup-v3' in t and 'undo_id u-81f2' in t and '→ ok' in t
    assert 'delete' in t and 'FAILED' in t and 'declined' in t and 'open_tab' in t and 'no training' in t


def test_summarize_rolls_over_chunks_and_retries(monkeypatch):
    calls = _fake_complete(monkeypatch, [LLMError('busy', 503, transient=True)] + [f'S{i}' for i in range(1, 20)])
    big = [{'seq': i + 1, 'role': 'user' if i % 2 == 0 else 'assistant', 'text': 'x' * 5000} for i in range(6)]
    out = summarize.summarize({'kind': 'openai'}, 'OLD', big, 4000)
    assert len(calls) >= 3 and calls[0]['text'] == calls[1]['text']          # the failed call was retried as is
    assert calls[1]['text'].startswith('PREVIOUS SUMMARY:\nOLD')
    assert calls[2]['text'].startswith('PREVIOUS SUMMARY:\nS1')              # each chunk folds into the last
    assert out == f'S{len(calls) - 1}' and calls[0]['max_tokens'] == summarize.summary_cap(4000)
    assert 'Goal' in calls[0]['system'] and 'verbatim' in calls[0]['system']


def test_summarize_gives_up_on_hard_error(monkeypatch):
    _fake_complete(monkeypatch, [LLMError('bad key', 401)])
    with pytest.raises(LLMError):
        summarize.summarize({'kind': 'openai'}, None, _rows(4), 8000)


# ---------------------------------------------------------------- the saved chat
def _chat(u, n):
    sid = sessions.create(u, 'data', {}, 'long')['session']['id']
    for r in _rows(n):
        sessions.append(u, sid, r['role'], r['text'], trace=[{'type': 'tool', 'name': f't{r["seq"]}', 'ok': True}]
                        if r['role'] == 'assistant' else None)
    return sid


def test_compact_is_incremental_and_history_starts_after(pg, monkeypatch):
    u = make_user('ann')
    sid = _chat(u, 12)
    calls = _fake_complete(monkeypatch)
    info = summarize.compact(u, sid, {'kind': 'openai'}, 8000)
    assert info['covered'] == 6 and info['turns'] == 6 and info['summary'] == 'SUMMARY 1'
    assert 't2' in calls[0]['text'] and 't6' in calls[0]['text'] and 'm7' not in calls[0]['text']
    cx = sessions.context(u, sid)
    assert cx['summary'] == 'SUMMARY 1' and cx['summary_seq'] == 6 and [m['seq'] for m in cx['messages']] == list(range(7, 13))
    assert [m['text'] for m in sessions.history(u, sid)] == [f'm{i}' for i in range(7, 13)]
    g = sessions.get(u, sid)
    assert len(g['messages']) == 12 and g['session']['summary'] == 'SUMMARY 1' and g['session']['summary_seq'] == 6
    # the next one folds the old summary + only the new messages
    for r in _rows(16)[12:]:
        sessions.append(u, sid, r['role'], r['text'])
    info = summarize.compact(u, sid, {'kind': 'openai'}, 8000)
    assert info['covered'] == 10 and calls[1]['text'].startswith('PREVIOUS SUMMARY:\nSUMMARY 1')
    assert 'm7' in calls[1]['text'] and 'm6' not in calls[1]['text']
    # nothing left to fold
    assert summarize.compact(u, sid, {'kind': 'openai'}, 8000, keep=20) is None


def test_save_summary_only_over_its_base(pg):
    u = make_user('ann')
    sid = _chat(u, 4)
    assert sessions.save_summary(u, sid, 'A', 2, 10, None)
    assert not sessions.save_summary(u, sid, 'B', 3, 10, None)                # built from no summary: stale
    assert sessions.save_summary(u, sid, 'C', 3, 10, 2)
    assert sessions.context(u, sid)['summary'] == 'C'
    other = make_user('bob')
    with pytest.raises(sessions.SessionError):
        sessions.context(other, sid)


# ---------------------------------------------------------------- the chat route
def _long_chat(client, n):
    sid = client.post('/api/llm/sessions', json={'role': 'data'}).json()['session']['id']
    for r in _rows(n):
        sessions.append(client.user, sid, r['role'], r['text'])
    return sid


def test_route_auto_compacts_past_history_turns(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])
    sid = _long_chat(client, sessions.HISTORY_TURNS + 2)
    calls = _fake_complete(monkeypatch, ['## Goal\n- keep the trucks'])
    seen = script(monkeypatch, ('Sure.', []))
    evs = _events(client.post('/api/llm/agent/data/chat', json={'session_id': sid, 'text': 'next?'}))
    kinds = [e['type'] for e in evs]
    assert kinds[:2] == ['compact', 'compact'] and kinds[-1] == 'done' and len(calls) == 1
    done = evs[1]
    assert done['state'] == 'done' and done['auto'] and done['summary'] == '## Goal\n- keep the trucks'
    assert done['turns'] > 30 and done['after'] < done['before']
    system, msgs = seen[0]['system'], seen[0]['msgs']
    assert react.SUMMARY_HEAD in system and 'keep the trucks' in system
    assert len(msgs) == summarize.KEEP_AUTO + 1                 # the kept messages + the new one
    assert msgs[-1]['parts'][0]['text'] == 'next?' and msgs[0]['role'] == 'user'
    g = client.get(f'/api/llm/sessions/{sid}').json()
    assert any(t['type'] == 'compact' and t.get('state') == 'done' for t in g['messages'][-1]['trace'])
    # the next turn reads the stored summary without compacting again
    seen = script(monkeypatch, ('Again.', []))
    evs = _events(client.post('/api/llm/agent/data/chat', json={'session_id': sid, 'text': 'and?'}))
    assert 'compact' not in [e['type'] for e in evs] and 'keep the trucks' in seen[0]['system'] and len(calls) == 1


def test_route_auto_compacts_past_the_budget(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])
    client.ctx.llm.set_config({'budget': {'max_input_tokens': 30000}}, client.user)
    sid = client.post('/api/llm/sessions', json={'role': 'data'}).json()['session']['id']
    for r in _rows(8):
        sessions.append(client.user, sid, r['role'], r['text'] + ' ' + 'w' * 10000)
    _fake_complete(monkeypatch)
    script(monkeypatch, ('ok', []))
    evs = _events(client.post('/api/llm/agent/data/chat', json={'session_id': sid, 'text': 'go'}))
    assert evs[0] == {'type': 'compact', 'state': 'start', 'auto': True} and evs[1]['state'] == 'done'


def test_route_goes_on_when_the_summary_fails(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])
    sid = _long_chat(client, sessions.HISTORY_TURNS + 2)
    _fake_complete(monkeypatch, [LLMError('bad key', 401)])
    script(monkeypatch, ('Answer anyway.', []))
    evs = _events(client.post('/api/llm/agent/data/chat', json={'session_id': sid, 'text': 'next?'}))
    assert evs[1]['type'] == 'notice' and 'Could not compact' in evs[1]['text'] and evs[-1]['type'] == 'done'
    assert sessions.context(client.user, sid)['summary'] is None


def test_compact_route(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['data_center', 'assistant'])
    assert client.post('/api/llm/agent/data/compact', json={}).status_code == 400
    sid = _long_chat(client, 8)
    calls = _fake_complete(monkeypatch)
    r = client.post('/api/llm/agent/data/compact', json={'session_id': sid, 'focus': 'the truck class'})
    assert r.status_code == 200 and r.json()['compacted'] and r.json()['covered'] == 6
    assert 'the truck class' in calls[0]['system']
    assert client.post('/api/llm/agent/data/compact', json={'session_id': sid}).json() == {'compacted': False}
    assert client.post('/api/llm/agent/data/compact', json={'session_id': 'nope'}).status_code == 404
    _fake_complete(monkeypatch, [LLMError('upstream', 503, transient=False)])
    for r_ in _rows(12)[8:]:
        sessions.append(client.user, sid, r_['role'], r_['text'])
    assert client.post('/api/llm/agent/data/compact', json={'session_id': sid}).status_code == 502
    names = [c['name'] for c in client.get('/api/llm/agent/data/catalog').json()['commands']]
    assert 'compact' in names


def test_empty_answer_retried_once_with_twice_the_room(monkeypatch):
    calls = _fake_complete(monkeypatch, ['', '', 'never asked'])
    with pytest.raises(LLMError, match='empty summary'):
        summarize.summarize({'kind': 'openai'}, None, _rows(4), 8000)
    cap = summarize.summary_cap(8000)
    assert [c['max_tokens'] for c in calls] == [cap, 2 * cap]                # two tries, not three of the same
    calls = _fake_complete(monkeypatch, ['', 'S'])
    assert summarize.summarize({'kind': 'openai'}, None, _rows(4), 8000) == 'S'


def test_quiet_thinking_leaves_room_for_the_answer():
    from dataviz.llm.providers import _quiet_thinking, _strip_think
    b = {'max_tokens': 600, 'temperature': 0.2}
    _quiet_thinking({'kind': 'anthropic', 'model': 'claude-opus-5-5'}, b)     # always thinks: least of it + room
    assert b['output_config'] == {'effort': 'low'} and b['max_tokens'] >= 600 + 4096 and 'temperature' not in b
    b = {'max_tokens': 600}
    _quiet_thinking({'kind': 'anthropic', 'model': 'claude-3-5-haiku'}, b)     # thinking is off by default
    assert b == {'max_tokens': 600}
    b = {'generationConfig': {'maxOutputTokens': 600}}
    _quiet_thinking({'kind': 'gemini', 'model': 'gemini-2.5-flash'}, b)
    assert b['generationConfig']['thinkingConfig'] == {'thinkingBudget': 0}
    b = {'max_completion_tokens': 600}
    _quiet_thinking({'kind': 'openai', 'model': 'gpt-5-mini'}, b)
    assert b['reasoning_effort'] == 'low' and b['max_completion_tokens'] > 600
    b = {'max_tokens': 600}
    _quiet_thinking({'kind': 'openai_compat', 'model': 'qwen3'}, b)
    assert b['chat_template_kwargs'] == {'enable_thinking': False}
    assert _strip_think('<think>hmm</think>\n## Goal') == '## Goal' and _strip_think('<think>cut off') == ''
    assert _strip_think('## Goal') == '## Goal'
