"""Sub-agents (tools/agents.py delegate, engine/subrun.py): only offered when the user switched them on and never to
a small model; each task runs in its own read-only run at the same time; only the answers come back, in task order;
their tokens count for the turn; a failing task does not fail the others; a sub-agent cannot delegate again."""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_agent_engine import svc  # noqa: F401 - svc is a fixture
from test_llm_tools import ALICE, OPENAI

from dataviz.llm import providers, tools
from dataviz.llm.engine import react
from dataviz.llm.tools import agents

SUB = '(You are a sub-agent'


def fake(monkeypatch, tasks=None):
    """The model: the main run delegates `tasks` then answers; a sub-agent answers its task (by its text)."""
    seen, lock, live = [], threading.Lock(), {'now': 0, 'max': 0}

    def stream_tools(p, system, msgs, specs, think=None, native_web=False, max_tokens=None):
        first = msgs[0]['parts'][0]['text']
        with lock:
            seen.append({'first': first, 'specs': {s['name'] for s in specs}, 'model': p.get('model'), 'msgs': msgs})
        if SUB in first:
            with lock:
                live['now'] += 1
                live['max'] = max(live['max'], live['now'])
            time.sleep(0.15)                              # long enough to overlap when they run together
            with lock:
                live['now'] -= 1
            if 'boom' in first:
                raise providers.LLMError('the model refused', 400)
            yield 'text', 'answer for ' + first.split('\n')[0]
        elif msgs[-1]['role'] == 'user':
            yield 'tool_call', {'id': 'd1', 'name': 'delegate', 'args': {'tasks': tasks or []}}
        else:
            yield 'text', 'All done.'
        yield 'usage', {'in': 10, 'out': 2}
    monkeypatch.setattr(providers, 'stream_tools', stream_tools)
    return seen, live


def start(agents_on=True, provider=OPENAI, role='data'):
    opts = react.options({'agents': agents_on}, provider)
    return react.prepare(ALICE, role, provider, react.history([{'role': 'user', 'text': 'check two buckets'}]), {}, opts)


def test_off_by_default_and_never_for_a_small_model():
    assert 'delegate' not in start(agents_on=False).tools
    assert 'delegate' not in react.prepare(ALICE, 'data', OPENAI, react.history([{'role': 'user', 'text': 'x'}]), {},
                                           react.options({}, OPENAI)).tools
    small = {**OPENAI, 'model': 'qwen2.5-7b-instruct'}
    assert react.profile_of(small) == 'small' and 'delegate' not in start(provider=small).tools
    run = start()
    assert 'delegate' in run.tools and '## Sub-agents' in run.system


def test_tasks_run_at_the_same_time_and_only_their_answers_come_back(monkeypatch, svc):  # noqa: F811
    seen, live = fake(monkeypatch, [{'task': 'Count images in cars', 'hint': 'bucket cars'}, {'task': 'Count images in bikes'}])
    evs = list(react.events(start()))
    subs = [s for s in seen if SUB in s['first']]
    assert len(subs) == 2 and live['max'] == 2, 'the two sub-agents ran together'
    assert all('delegate' not in s['specs'] and 'consult' not in s['specs'] and 'delete' not in s['specs'] for s in subs)
    assert any('Where to look: bucket cars' in s['first'] for s in subs)
    final = seen[-1]['msgs'][-1]['parts'][0]['tool_result']
    out = json.loads(final['content'])
    assert [r['answer'] for r in out['results']] == ['answer for Count images in cars', 'answer for Count images in bikes']
    ag = [e for e in evs if e['type'] == 'agent']
    assert [(a['task'], a['ok'], a['steps'], a['in']) for a in ag] == [('Count images in cars', True, 1, 10),
                                                                       ('Count images in bikes', True, 1, 10)]
    assert evs[-1]['type'] == 'done' and evs[-1]['usage'] == {'in': 40, 'out': 8}, 'the sub-agents count for the turn'


def test_a_failing_task_does_not_fail_the_others(monkeypatch, svc):  # noqa: F811
    seen, _ = fake(monkeypatch, [{'task': 'boom please'}, {'task': 'Count images in cars'}])
    evs = list(react.events(start()))
    out = json.loads(seen[-1]['msgs'][-1]['parts'][0]['tool_result']['content'])
    assert not out['results'][0]['ok'] and 'sub-agent failed' in out['results'][0]['error']
    assert out['results'][1]['answer'] == 'answer for Count images in cars'
    assert [e['ok'] for e in evs if e['type'] == 'agent'] == [False, True]


def test_the_sub_agent_model_and_the_limits(monkeypatch, svc):  # noqa: F811
    seen, _ = fake(monkeypatch, [{'task': 'Count images in cars'}])
    run = start()
    run.ctx.sub_model = 'gpt-mini'
    list(react.events(run))
    assert [s['model'] for s in seen if SUB in s['first']] == ['gpt-mini']
    ctx = SimpleNamespace(depth=1, provider=OPENAI)
    with pytest.raises(tools.ToolError, match='cannot delegate'):
        agents._delegate(ctx, {'tasks': [{'task': 'x'}]})
    ctx = SimpleNamespace(depth=0, provider=OPENAI)
    with pytest.raises(tools.ToolError, match='at most 4'):
        agents._delegate(ctx, {'tasks': [{'task': str(i)} for i in range(5)]})
    with pytest.raises(tools.ToolError, match='give 1-4'):
        agents._delegate(ctx, {'tasks': [{'hint': 'no task'}]})


def test_the_switch_is_saved_per_user(client):
    s, u = client.ctx.llm, client.user
    assert s.load(u)['budget']['agents'] == {'on': False, 'model': ''}
    client.put('/api/llm/config', json={'budget': {'agents': {'on': True}}})
    client.put('/api/llm/config', json={'budget': {'agents': {'model': ' gpt-mini '}}})
    assert s.load(u)['budget']['agents'] == {'on': True, 'model': 'gpt-mini'}
    assert any(c['name'] == 'agents' for c in client.get('/api/llm/agent/data/catalog').json()['commands'])
