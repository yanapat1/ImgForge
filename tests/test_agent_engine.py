"""The ◆ agent engine (llm/engine): the ReAct loop's limits (50 steps, 50 retries, loop guard), modes ask / auto /
plan with confirm-and-continue, role separation (the visual agent has no Data Center tool), effort, small models,
and the prompt built from llm/prompts/. The model is scripted through providers.stream_tools."""

import copy
import threading

import pytest

from dataviz.llm import prompting, providers, tools
from dataviz.llm.engine import confirm, react, roles
from dataviz.llm.providers import LLMError
from test_llm_tools import ALICE, OPENAI, FakeService


@pytest.fixture
def svc(monkeypatch):
    s = FakeService()
    monkeypatch.setattr(tools, '_svc', lambda: s)
    monkeypatch.setattr(react, '_sleep', lambda cancel, s: cancel.is_set())     # no real backoff in tests
    yield s


def script(monkeypatch, *steps):
    """Each provider step answers with the next (text, [(name, args)]); an Exception instance is raised instead."""
    seen, it = [], iter(steps)

    def stream_tools(p, system, msgs, specs, think=None, native_web=False, max_tokens=None):
        seen.append({'system': system, 'msgs': copy.deepcopy(msgs), 'specs': specs, 'think': think, 'p': p})
        step = next(it)
        if isinstance(step, Exception):
            raise step
        text, calls = step
        if text:
            yield 'text', text
        for n, a in calls:
            yield 'tool_call', {'id': '', 'name': n, 'args': a}
        yield 'usage', {'in': 10, 'out': 2}

    monkeypatch.setattr(providers, 'stream_tools', stream_tools)
    return seen


def start(text, role='data', scope=None, provider=OPENAI, **body):
    opts = react.options(body, provider)
    return react.prepare(ALICE, role, provider, react.history([{'role': 'user', 'text': text}]), scope or {}, opts)


def visible(evs):
    return [e for e in evs if e['type'] not in ('step', 'ping')]


def types(evs):
    return [e['type'] for e in visible(evs)]


def drain(run, decide=None):
    """All events; `decide(confirm event) -> (approve ids, reject ids)` answers each confirmation (in a thread,
    as the page would, while the run waits)."""
    out = []
    for ev in react.events(run):
        out.append(ev)
        if ev['type'] == 'confirm' and decide:
            ap, rj = decide(ev)
            threading.Timer(0.05, lambda e=ev: confirm.decide(ALICE, e['confirm_id'], ap, rj)).start()
    return out


# ---------------------------------------------------------------- loop basics
def test_read_tool_then_answer(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('list_buckets', {})]), ('You have 4 buckets.', []))
    evs = drain(start('how many buckets?'))
    assert types(evs) == ['meta', 'tool_start', 'tool_end', 'text', 'done']
    assert evs[0]['max_steps'] == 30 and evs[0]['mode'] == 'ask'
    end = next(e for e in evs if e['type'] == 'tool_end')
    assert end['ok'] and end['summary'] == '→ 4 buckets' and '"cars"' in end['preview']
    assert evs[-1]['steps'] == 2 and evs[-1]['usage'] == {'in': 20, 'out': 4}
    sysp = seen[0]['system']
    assert 'You are ◆' in sysp and 'Data assistant' in sysp and '## Mode\n**ask**' in sysp and '`list_buckets`' in sysp


def test_step_limit_is_50_at_most(monkeypatch, svc):
    script(monkeypatch, *[('', [('list_folder', {'bucket': 'cars', 'path': f'p{i}'})]) for i in range(60)])
    evs = drain(start('loop', effort='high'))
    assert evs[0]['max_steps'] == react.MAX_STEPS == 50
    assert sum(e['type'] == 'step' for e in evs) == 50
    assert evs[-1]['type'] == 'error' and 'stopped after 50 steps' in evs[-1]['detail']
    script(monkeypatch, *[('', [('list_folder', {'bucket': 'cars', 'path': f'p{i}'})]) for i in range(60)])
    evs = drain(start('loop', effort='low'))
    assert sum(e['type'] == 'step' for e in evs) == 15


def test_transient_errors_retry_50_times_then_stop(monkeypatch, svc):
    script(monkeypatch, *[LLMError('timeout', 502, transient=True) for _ in range(60)])
    evs = []
    with pytest.raises(LLMError, match='gave up after 50 retries'):     # the route makes it the error event
        for ev in react.events(start('hi')):
            evs.append(ev)
    retries = [e for e in evs if e['type'] == 'retry']
    assert len(retries) == react.MAX_RETRIES == 50 and retries[-1]['attempt'] == 50 and retries[0]['wait_s'] == 1
    assert retries[5]['wait_s'] == 20


def test_retry_then_success_and_no_retry_for_setup_errors(monkeypatch, svc):
    script(monkeypatch, LLMError('busy', 502, transient=True, retry_after=3), ('ok', []))
    evs = visible(drain(start('hi')))
    assert types(evs) == ['meta', 'retry', 'text', 'done'] and evs[1]['wait_s'] == 3
    script(monkeypatch, LLMError('bad key', 400))
    with pytest.raises(LLMError, match='bad key'):
        drain(start('hi'))


def test_loop_guard_nudges_then_stops(monkeypatch, svc):
    seen = script(monkeypatch, *[('', [('list_folder', {'bucket': 'cars'})]) for _ in range(6)])
    evs = drain(start('again'))
    assert evs[-1]['type'] == 'error' and 'repeating itself' in evs[-1]['detail']
    third = seen[3]['msgs'][-1]['parts'][0]['tool_result']['content']
    assert 'same arguments' in third


# ---------------------------------------------------------------- modes and confirmations
def test_ask_mode_confirms_then_continues(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('create_folder', {'bucket': 'cars', 'path': 'night'})]), ('Created night/.', []))
    evs = visible(drain(start('make a night folder'), decide=lambda e: (['a1'], [])))
    assert types(evs) == ['meta', 'confirm', 'tool_start', 'tool_end', 'confirmed', 'text', 'done']
    c = evs[1]
    assert c['actions'][0]['risk'] == 'change' and c['actions'][0]['args']['bucket'] == 'b1'
    assert ('ops', 'b1', [{'op': 'mkdir', 'path': 'night'}]) in svc.calls
    assert next(e for e in evs if e['type'] == 'tool_end')['undo_id'] == 'u1'
    assert '"applied":1' in seen[1]['msgs'][-1]['parts'][0]['tool_result']['content']


def test_declined_change_is_not_run(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('delete', {'bucket': 'cars', 'paths': ['a.jpg']})]), ('Kept it.', []))
    evs = drain(start('delete a.jpg'), decide=lambda e: ([], ['a1']))
    assert not [x for x in svc.calls if x[0] == 'ops']
    assert next(e for e in evs if e['type'] == 'confirmed')['results'][0]['declined']
    assert 'declined' in seen[1]['msgs'][-1]['parts'][0]['tool_result']['content']


def test_auto_mode_runs_changes_but_delete_still_waits(monkeypatch, svc):
    script(monkeypatch, ('', [('add_tags', {'bucket': 'cars', 'tags': ['night']}),
                              ('delete', {'bucket': 'cars', 'paths': ['old']})]), ('Done.', []))
    evs = drain(start('tag and delete', mode='auto'), decide=lambda e: (['a1'], []))
    ts = types(evs)
    assert ts.index('tool_end') < ts.index('confirm')           # the tag ran before the card
    c = next(e for e in evs if e['type'] == 'confirm')
    assert [a['tool'] for a in c['actions']] == ['delete'] and c['actions'][0]['risk'] == 'danger'
    assert [x[0] for x in svc.calls if x[0] in ('set_tags', 'ops')] == ['set_tags', 'ops']


def test_confirm_belongs_to_its_user_and_stop_cancels(monkeypatch, svc):
    script(monkeypatch, ('', [('delete', {'bucket': 'cars', 'paths': ['a']})]))
    run = start('delete a')
    it = react.events(run)
    ev = next(e for e in it if e['type'] == 'confirm')
    with pytest.raises(confirm.AgentError) as e:
        confirm.decide({'id': 'bob'}, ev['confirm_id'], ['a1'])
    assert e.value.status == 403
    with pytest.raises(confirm.AgentError):
        react.stop({'id': 'bob'}, run.run_id)
    react.stop(ALICE, run.run_id)
    rest = list(it)
    assert any(e['type'] == 'notice' and 'nothing was changed' in e['text'] for e in rest)
    assert not [x for x in svc.calls if x[0] == 'ops']


def test_plan_mode_is_read_only_and_submits_a_plan(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('list_folder', {'bucket': 'cars'})]),
                  ('', [('submit_plan', {'summary': 'Move 3 images', 'steps': ['create val/', 'move img0..2']})]))
    evs = drain(start('reorganise', mode='plan'))
    names = {s['name'] for s in seen[0]['specs']}
    assert 'submit_plan' in names and not names & {'move', 'delete', 'create_folder', 'add_tags'}
    assert evs[-2]['type'] == 'plan_proposal' and evs[-2]['steps'] == ['create val/', 'move img0..2']
    assert '**plan**' in seen[0]['system']


def test_bad_change_in_a_step_proposes_none(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('create_folder', {'bucket': 'cars', 'path': 'x'}),
                                     ('move', {'bucket': 'nope', 'paths': ['a'], 'to': 'x'})]), ('Sorry.', []))
    evs = drain(start('do it'))
    assert 'confirm' not in types(evs)
    contents = [p['tool_result']['content'] for p in seen[1]['msgs'][-1]['parts']]
    assert 'send all changes again' in contents[0] and "no bucket 'nope'" in contents[1]


# ---------------------------------------------------------------- roles, effort, small models, prompts
def test_visual_agent_has_no_data_center_tools(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('delete', {'bucket': 'cars', 'paths': ['a']})]), ('I cannot.', []))
    evs = drain(start('delete a', role='visual'))
    names = {s['name'] for s in seen[0]['specs']}
    assert not names & set(tools.TOOLS) - {'ask_user', 'cannot_do'}
    res = seen[1]['msgs'][-1]['parts'][0]['tool_result']
    assert res['is_error'] and 'other assistant' in res['content']
    assert not svc.calls and 'confirm' not in types(evs)
    assert 'Visual assistant' in seen[0]['system']


def test_small_model_profile(monkeypatch, svc):
    small = {**OPENAI, 'model': 'qwen2.5-7b-instruct'}
    assert react.profile_of(small) == 'small' and react.profile_of({'model': 'qwen2.5-32b'}) == 'large'
    assert react.profile_of({'model': 'x', 'agent_profile': 'small'}) == 'small'
    seen = script(monkeypatch, ('ok', []))
    evs = drain(start('delete the old folder in cars', provider=small, effort='high'))
    assert evs[0]['max_steps'] == react.SMALL_STEPS == 20 and evs[0]['profile'] == 'small'
    names = {s['name'] for s in seen[0]['specs']}
    assert 'delete' in names and 'ask_user' in names and len(names) <= react.SMALL_TOOLS + 8
    assert seen[0]['p']['temperature'] == 0.1


def test_effort_sets_thinking_and_verify(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('add_tags', {'bucket': 'cars', 'tags': ['x']})]), ('Tagged.', []),
                  ('Checked: tagged.', []))
    evs = drain(start('tag it', mode='auto', effort='high'))
    assert seen[0]['think'] == 'high' and len(seen) == 3
    assert seen[2]['msgs'][-1]['parts'][0]['text'].startswith('(check)')
    assert any(e['type'] == 'notice' and 'Checking' in e['text'] for e in evs)
    script(monkeypatch, ('hi', []))
    assert react.options({'effort': 'low'}, OPENAI)['think'] == 'off'
    assert react.options({'effort': 'low', 'think': 'medium'}, OPENAI)['think'] == 'medium'


def test_todo_and_skills(monkeypatch, svc):
    sk = prompting.skills('data', [{'name': 'mine', 'description': 'my steps', 'body': 'Step 1: be nice.'}])
    assert sk['mine']['source'] == 'yours'
    seen = script(monkeypatch, ('', [('todo', {'items': [{'text': 'look', 'state': 'active'}]}),
                                     ('read_docs', {'topic': 'formats'})]), ('Done.', []))
    evs = drain(start('explain formats'))
    assert next(e for e in evs if e['type'] == 'todo')['items'][0]['text'] == 'look'
    assert 'yolo-detect' in seen[1]['msgs'][-1]['parts'][1]['tool_result']['content']


def test_user_prompts_are_appended_last(monkeypatch, svc):
    opts = react.options({}, OPENAI)
    run = react.prepare(ALICE, 'data', OPENAI, react.history([{'role': 'user', 'text': 'hi'}]), {}, opts,
                        user_prompts=[{'name': 'Tone', 'body': 'Answer in Thai.', 'role': 'data'},
                                      {'name': 'Other', 'body': 'Visual only.', 'role': 'visual'}])
    assert run.system.rstrip().endswith('Answer in Thai.') and 'Visual only.' not in run.system
    assert run.system.index('## Your own instructions') > run.system.index('## Tools')


def test_roles():
    assert roles.get('data').feature == 'data_center' and roles.get('visual').needs_session
    with pytest.raises(KeyError):
        roles.get('labeling')
    assert 'view_state' in roles.visual_scope(None, {}) or 'did not report' in roles.visual_scope(None, {})
    assert 'UMAP' in roles.visual_scope(None, {'view': {'method': 'UMAP', 'feature': 'dinov2-base'}})


# ---------------------------------------------------------------- routes
def _connect(client, features):
    s, u = client.ctx.llm, client.user
    p = s.upsert({'kind': 'openai', 'name': 'fake', 'base_url': 'http://fake/v1', 'model': 'gpt-x', 'api_key': 'k'}, u)
    s.record_test(p['id'], {'ok': True}, u)
    s.set_config({'features': {f: True for f in features}}, u)
    return p


def test_agent_routes(client, monkeypatch, svc):
    import json as _json
    body = {'messages': [{'role': 'user', 'text': 'tag cars'}], 'scope': {}}
    assert client.post('/api/llm/agent/data/chat', json=body).status_code == 403       # switch off
    assert client.post('/api/llm/agent/nope/chat', json=body).status_code == 404
    p = _connect(client, ['data_center', 'assistant'])
    seen = script(monkeypatch, ('', [('add_tags', {'bucket': 'cars', 'tags': ['x']})]), ('Tagged.', []))
    r = client.post('/api/llm/agent/data/chat', json={**body, 'mode': 'auto', 'effort': 'low',
                                                      'model': {'provider_id': p['id'], 'model': 'gpt-y'}})
    evs = [_json.loads(x) for x in r.text.splitlines() if x]
    assert r.status_code == 200 and evs[0]['type'] == 'meta' and evs[0]['model'] == 'gpt-y' and evs[-1]['type'] == 'done'
    assert ('set_tags', 'b1', '', ('x',), ()) in svc.calls and seen[0]['p']['model'] == 'gpt-y'
    # the visual agent needs a visualizer session
    assert client.post('/api/llm/agent/visual/chat', json=body).status_code == 409
    cat = client.get('/api/llm/agent/data/catalog').json()
    assert {'delete', 'list_buckets', 'use_skill'} <= {t['name'] for t in cat['tools']}
    assert any(c['name'] == 'model' for c in cat['commands']) and cat['limits'] == {'max_steps': 50, 'max_retries': 50}
    vis = client.get('/api/llm/agent/visual/catalog').json()
    assert not {t['name'] for t in vis['tools']} & {'delete', 'move', 'list_buckets'}
    assert client.post('/api/llm/agent/confirm', json={'confirm_id': 'zz', 'approve': []}).status_code == 404
    assert client.post('/api/llm/agent/stop', json={'run_id': 'zz'}).status_code == 404
    # remembered chat settings (what /model and the chips save)
    client.put('/api/llm/config', json={'chat': {'data': {'mode': 'plan', 'effort': 'bad', 'provider_id': p['id']}}})
    assert client.get('/api/llm/agent/data/catalog').json()['chat'] == {'mode': 'plan', 'provider_id': p['id']}


def test_web_key_is_stored_encrypted(client):
    s, u = client.ctx.llm, client.user
    assert s.web_key(u) == '' and s.set_web_key('brv-123', u) == {'has_key': True} and s.web_key(u) == 'brv-123'
    from dataviz import db
    from sqlalchemy import text
    with db.connect() as c:
        raw = c.execute(text('SELECT web_secrets FROM app.llm_user_config WHERE user_id = :u'), {'u': u['id']}).scalar()
    assert b'brv-123' not in bytes(raw)
    s.set_web_key('', u)
    assert s.web_key(u) == ''
