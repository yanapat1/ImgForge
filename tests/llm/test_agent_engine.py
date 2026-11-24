"""The ◆ agent engine (llm/engine): the ReAct loop's limits (50 steps, 50 retries, loop guard), modes ask / auto /
plan with confirm-and-continue, role separation (the visual agent has no Bucket tool), effort, small models,
and the prompt built from llm/prompts/. The model is scripted through providers.stream_tools."""

import copy
import json
import threading

import pytest

from dataviz.llm import budget, prompting, providers, tools
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


def start(text, role='bucket', scope=None, provider=OPENAI, **body):
    opts = react.options(body, provider)
    return react.prepare(ALICE, role, provider, react.history([{'role': 'user', 'text': text}]), scope or {}, opts)


def visible(evs):
    return [e for e in evs if e['type'] not in ('step', 'ping', 'context')]


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
    assert 'You are ◆' in sysp and 'You are the Bucket assistant' in sysp and '## Mode\n**ask**' in sysp and '`list_buckets`' in sysp


def test_context_events_follow_each_step_and_done_has_the_last_not_the_sum(monkeypatch, svc):
    """The ring shows the context of the next call: our estimate after meta and after the tool results, the
    provider's own count once a step answered. done.usage stays the billed sum over the steps."""
    script(monkeypatch, ('', [('list_buckets', {})]), ('', [('list_buckets', {'q': 'x'})]), ('Done.', []))
    run = start('how many buckets?')
    evs = drain(run)
    ctx = [e for e in evs if e['type'] == 'context']
    assert evs[0]['type'] == 'meta' and evs[0]['limit'] == run.limit and evs[1]['type'] == 'context'
    assert ctx[0] == {'type': 'context', 'used': run.est, 'limit': run.limit, 'exact': False}
    assert [e['exact'] for e in ctx] == [False, True, False, True, False, True]      # estimate, then each step's count
    assert ctx[2]['used'] > run.est                         # the tool result made the next request bigger
    done = evs[-1]
    assert done['ctx'] == 10 and done['limit'] == run.limit and done['usage'] == {'in': 30, 'out': 6}


def test_compact_fit_counts_down_instead_of_again(monkeypatch):
    from dataviz.llm import budget
    from dataviz.llm.engine import compact
    big = 'x' * 4000
    msgs = [{'role': 'user', 'parts': [{'text': 'q'}]}]
    for i in range(6):
        msgs.append({'role': 'assistant', 'parts': [{'tool_call': {'id': str(i), 'name': 't', 'args': {}}}]})
        msgs.append({'role': 'tool', 'parts': [{'tool_result': {'id': str(i), 'name': 't', 'content': big}}]})
    full = compact.estimate('openai', 'sys', msgs)
    ok, est = compact.fit('openai', 'sys', msgs, full - 2000)
    assert ok and est == compact.estimate('openai', 'sys', msgs) and est <= full - 2000      # the running total is exact
    assert compact.fit('openai', 'sys', msgs, 10) == (False, compact.estimate('openai', 'sys', msgs))
    assert budget.text_tokens(big) > 1000


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


def test_an_answer_cut_off_while_thinking_is_tried_once_with_twice_the_room(monkeypatch, svc):
    """A thinking answer of an OpenAI-style model gets THINK_OUT room (low: 4096 over the card's 2048); it ran out in
    the thinking (no text, no call, stop 'length'): once more with twice that and a nudge to act; cut off again: an
    error that says what to change, not "empty answer"."""
    limits, outs = [], iter([[], [('text', 'Done.')]])

    def stream_tools(p, system, msgs, specs, think=None, native_web=False, max_tokens=None):
        limits.append(max_tokens)
        evs = next(outs)
        yield 'thinking', {'text': 'long plan…'}
        yield from evs
        yield 'stop', 'stop' if evs else 'length'
        yield 'usage', {'in': 10, 'out': 2048}

    monkeypatch.setattr(providers, 'stream_tools', stream_tools)
    evs = drain(start('organise the board', provider={**OPENAI, 'max_output_tokens': 2048}))
    assert limits == [4096, 8192]
    assert any(e['type'] == 'notice' and '4096 tokens' in e['text'] for e in evs)
    assert not any(e['type'] == 'error' for e in evs)
    outs = iter([[], []])
    limits.clear()
    evs = drain(start('organise the board', provider={**OPENAI, 'max_output_tokens': 2048}))
    err = next(e for e in evs if e['type'] == 'error')
    assert limits == [4096, 8192] and 'output limit (8192 tokens)' in err['detail'] and 'Max output tokens' in err['detail']


def test_output_room_and_admin_limits(monkeypatch):
    """output_room: thinking room for OpenAI-style models only, under Admin › Settings llm_think_output and what the
    context leaves; the retry doubles under the same ceilings. step_limits: the admin's numbers, profile ones capped."""
    admin = {}
    monkeypatch.setattr(react, '_admin', lambda k, d: admin.get(k, d))
    monkeypatch.setattr(providers, 'model_limits', lambda p: {'context': p.get('context_window'), 'output': None})
    local = {'kind': 'openai_compat', 'model': 'qwen3-32b', 'max_output_tokens': 4096}
    assert react.output_room(local, 'high', 1000) == 16384
    assert react.output_room(local, 'low', 1000) is None, 'the card already gives 4096'
    assert react.output_room(local, None, 1000) is None and react.output_room(local, 'off', 1000) is None
    assert react.output_room({**local, 'context_window': 32768}, 'high', 20000) == 32768 - 20000 - react.OUT_MARGIN
    assert react.output_room({**local, 'context_window': 32768}, 'high', 30000) is None, 'no room: the card value'
    assert react.output_room({**local, 'kind': 'anthropic'}, 'high', 1000) is None, 'Claude sizes its own'
    admin['llm_think_output'] = 65536
    assert react.output_room(local, 'high', 0, retry=True) == 8192
    assert react.output_room({**local, 'max_output_tokens': 40000}, None, 0, retry=True) == 65536
    admin['llm_think_output'] = 8192
    assert react.output_room(local, 'high', 0) == 8192
    assert react.step_limits() == {'max': 50, 'medium': 25, 'small': 20}
    admin.update(llm_steps_max=80, llm_steps_medium=60, llm_steps_small=100)
    assert react.step_limits() == {'max': 80, 'medium': 60, 'small': 80}
    med = {'kind': 'openai_compat', 'model': 'qwen3-32b', 'context_window': 32768}
    assert react.options({'effort': 'high'}, med)['max_steps'] == 60
    assert react.options({'effort': 'medium'}, med)['max_steps'] == 30
    assert react.options({'effort': 'high'}, {'kind': 'openai', 'model': 'gpt-5'})['max_steps'] == 80


def _same(n):
    return [('', [('list_folder', {'bucket': 'cars'})]) for _ in range(n)]


def _tool_texts(seen):
    return [m['parts'][0]['tool_result']['content'] for s in seen for m in s['msgs'][-1:]
            if m['role'] == 'tool']


def test_loop_guard_nudges_recovers_then_stops(monkeypatch, svc):
    seen = script(monkeypatch, *_same(20))
    asked = _summary(monkeypatch)
    run = start('again')
    run.max_steps = 40
    evs = drain(run)
    third = seen[3]['msgs'][-1]['parts'][0]['tool_result']['content']
    assert 'same arguments' in third, 'the 3rd identical call is nudged'
    rc = [e for e in evs if e['type'] == 'run_compact']
    assert [e['phase'] for e in rc] == ['start', 'done'] * react.LOOP_RECOVER_MAX
    assert all(e['reason'] == 'loop' for e in rc)
    assert 'repeating list_folder' in asked[0]['text'], 'the summary is told about the loop'
    resumed = json.dumps(seen[5]['msgs'][-1])
    assert 'compacted' in resumed and 'do not call it with those arguments again' in resumed
    # after a recovery the call is blocked: its last result comes back, the tool does not run again
    blocked = seen[6]['msgs'][-1]['parts'][0]['tool_result']['content']
    assert blocked.startswith('(blocked:')
    assert len([x for x in svc.calls if x[0] == 'ls']) == 4, 'only the first 4 calls ran'
    assert evs[-1]['type'] == 'error' and 'repeating itself' in evs[-1]['detail'] \
        and f'after {react.LOOP_RECOVER_MAX} loop recoveries' in evs[-1]['detail']


def test_loop_guard_off_stops_at_the_fifth(monkeypatch, svc):
    script(monkeypatch, *_same(6))
    asked = _summary(monkeypatch)
    evs = drain(start('again', compact='off'))
    assert not asked and not [e for e in evs if e['type'] == 'run_compact']
    assert evs[-1]['type'] == 'error' and 'repeating itself' in evs[-1]['detail'] and '/autocompact' in evs[-1]['detail']


def test_loop_guard_ask_declined_ends_the_turn(monkeypatch, svc):
    script(monkeypatch, *_same(6))
    asked = _summary(monkeypatch)
    out = []
    for ev in react.events(start('again', compact='ask')):
        out.append(ev)
        if ev['type'] == 'run_compact' and ev['phase'] == 'ask':
            threading.Timer(0.05, lambda e=ev: confirm.decide(ALICE, e['confirm_id'], [], ['a1'])).start()
    assert [e['phase'] for e in out if e['type'] == 'run_compact'] == ['ask', 'declined']
    assert not asked and out[-1]['type'] == 'done'


def test_loop_guard_recovers_into_an_answer(monkeypatch, svc):
    script(monkeypatch, *_same(5), ('The bucket cars has the files listed.', []))
    _summary(monkeypatch)
    evs = drain(start('again'))
    assert evs[-1]['type'] == 'done' and any(e['type'] == 'text' for e in evs)
    assert not any(e['type'] == 'error' for e in evs)


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
def test_visual_agent_has_no_bucket_tools(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('delete', {'bucket': 'cars', 'paths': ['a']})]), ('I cannot.', []))
    evs = drain(start('delete a', role='visual'))
    names = {s['name'] for s in seen[0]['specs']}
    assert not names & set(tools.TOOLS) - {'ask_user', 'cannot_do'}
    res = seen[1]['msgs'][-1]['parts'][0]['tool_result']
    assert res['is_error'] and 'Bucket tool' in res['content'] and 'navigate' in res['content']
    assert not svc.calls and 'confirm' not in types(evs)
    assert 'Visual assistant' in seen[0]['system']


def test_small_model_profile(monkeypatch, svc):
    small = {**OPENAI, 'model': 'qwen2.5-7b-instruct'}
    assert react.profile_of(small) == 'small' and react.profile_of({'model': 'qwen2.5-72b'}) == 'large'
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
    sk = prompting.skills('bucket', [{'name': 'mine', 'description': 'my steps', 'body': 'Step 1: be nice.'}])
    assert sk['mine']['source'] == 'yours'
    seen = script(monkeypatch, ('', [('todo', {'items': [{'text': 'look', 'state': 'active'}]}),
                                     ('read_docs', {'topic': 'formats'})]), ('Done.', []))
    evs = drain(start('explain formats'))
    assert next(e for e in evs if e['type'] == 'todo')['items'][0]['text'] == 'look'
    assert 'yolo-detect' in seen[1]['msgs'][-1]['parts'][1]['tool_result']['content']


def test_user_prompts_are_appended_last(monkeypatch, svc):
    opts = react.options({}, OPENAI)
    run = react.prepare(ALICE, 'bucket', OPENAI, react.history([{'role': 'user', 'text': 'hi'}]), {}, opts,
                        user_prompts=[{'name': 'Tone', 'body': 'Answer in Thai.', 'role': 'bucket'},
                                      {'name': 'Other', 'body': 'Visual only.', 'role': 'visual'}])
    assert run.system.rstrip().endswith('Answer in Thai.') and 'Visual only.' not in run.system
    assert run.system.index('## Your own instructions') > run.system.index('## Tools')


def test_roles():
    assert roles.get('bucket').feature == 'bucket_data' and roles.get('visual').needs_session
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
    assert client.post('/api/llm/agent/bucket/chat', json=body).status_code == 403       # switch off
    assert client.post('/api/llm/agent/nope/chat', json=body).status_code == 404
    p = _connect(client, ['bucket_data', 'assistant'])
    seen = script(monkeypatch, ('', [('add_tags', {'bucket': 'cars', 'tags': ['x']})]), ('Tagged.', []))
    r = client.post('/api/llm/agent/bucket/chat', json={**body, 'mode': 'auto', 'effort': 'low',
                                                      'model': {'provider_id': p['id'], 'model': 'gpt-y'}})
    evs = [_json.loads(x) for x in r.text.splitlines() if x]
    assert r.status_code == 200 and evs[0]['type'] == 'meta' and evs[0]['model'] == 'gpt-y' and evs[-1]['type'] == 'done'
    assert ('set_tags', 'b1', '', ('x',), ()) in svc.calls and seen[0]['p']['model'] == 'gpt-y'
    # the visual agent needs a visualizer session
    assert client.post('/api/llm/agent/visual/chat', json=body).status_code == 409
    cat = client.get('/api/llm/agent/bucket/catalog').json()
    assert {'delete', 'list_buckets', 'use_skill'} <= {t['name'] for t in cat['tools']}
    assert any(c['name'] == 'model' for c in cat['commands']) and cat['limits'] == {'max_steps': 50, 'max_retries': 50}
    vis = client.get('/api/llm/agent/visual/catalog').json()
    assert not {t['name'] for t in vis['tools']} & {'delete', 'move', 'list_buckets'}
    assert client.post('/api/llm/agent/confirm', json={'confirm_id': 'zz', 'approve': []}).status_code == 404
    assert client.post('/api/llm/agent/stop', json={'run_id': 'zz'}).status_code == 404
    # remembered chat settings (what /model and the chips save)
    client.put('/api/llm/config', json={'chat': {'bucket': {'mode': 'plan', 'effort': 'bad', 'provider_id': p['id']}}})
    assert client.get('/api/llm/agent/bucket/catalog').json()['chat'] == {'mode': 'plan', 'provider_id': p['id']}


def test_the_run_limit_follows_the_model_and_the_setting(client, monkeypatch, svc):
    """Auto (the default): the model's own maximum; a number: kept under it; the page is told both."""
    import json as _json
    from dataviz.llm import providers
    providers._limits.clear()
    p = _connect(client, ['bucket_data'])
    s, u = client.ctx.llm, client.user
    s.upsert({**p, 'context_window': 64000}, u)                     # the card's Model context (gpt-x is not in the table)
    s.record_test(p['id'], {'ok': True}, u)
    assert s.load(u)['budget']['max_input_tokens'] == 'auto'
    lim = client.get('/api/llm/state').json()['limits']
    assert lim == {'context': 64000, 'ceiling': int(64000 * 0.98) - 4096, 'source': 'manual'}

    def limit_of_a_turn():
        script(monkeypatch, ('ok', []))
        r = client.post('/api/llm/agent/bucket/chat', json={'messages': [{'role': 'user', 'text': 'hi'}], 'scope': {},
                                                          'ephemeral': True, 'session_id': None})
        return next(e for e in map(_json.loads, r.text.splitlines()) if e['type'] == 'meta')['limit']
    assert limit_of_a_turn() == lim['ceiling']
    client.put('/api/llm/config', json={'budget': {'max_input_tokens': 20000}})
    assert limit_of_a_turn() == 20000
    client.put('/api/llm/config', json={'budget': {'max_input_tokens': 900000}})       # more than the model takes
    assert limit_of_a_turn() == lim['ceiling']
    s.upsert({**s.provider(p['id'], u), 'context_window': ''}, u)                      # cleared: detect again
    assert 'context_window' not in s.provider(p['id'], u)


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


def test_the_prompt_starts_with_a_part_that_never_changes_in_a_chat(svc):
    """Rules, knowledge, skills and the tool card come first and stay byte-identical between messages (the provider
    caches them); the mode, the clock and where the user is come after the marker."""
    a = start('first', scope={'bucket': 'b1', 'path': 'x'})
    b = start('second', scope={'bucket': 'b1', 'path': 'y/z'}, mode='auto')
    sa, da = prompting.split(a.system)
    sb, db = prompting.split(b.system)
    assert sa == sb and da != db and '## Mode' in da and '## Now' in da and '## Mode' not in sa
    assert '## Tools\n- Run at once (read-only): `list_buckets`' in sa
    assert '| tool | kind |' not in a.system, 'the tools reach the model as native specs, not as a second table'
    assert 'image datasets' in sa and prompting.app_intro() in sa


def test_provider_bodies_cache_the_stable_part_and_the_history():
    system = 'RULES' + prompting.DYNAMIC + 'NOW 12:00'
    msgs = [{'role': 'user', 'parts': [{'text': 'q'}]},
            {'role': 'assistant', 'parts': [{'tool_call': {'id': 'c1', 'name': 't', 'args': {}}}]},
            {'role': 'tool', 'parts': [{'tool_result': {'id': 'c1', 'name': 't', 'content': 'r'}}]}]
    body = providers._anthropic_body({'model': 'claude-x'}, system, msgs, True, 1024)
    assert [b['text'] for b in body['system']] == ['RULES', 'NOW 12:00']
    assert 'cache_control' in body['system'][0] and 'cache_control' not in body['system'][1]
    assert body['messages'][-1]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    marks = sum(1 for m in body['messages'] for c in m['content'] if 'cache_control' in c) + 1
    assert marks <= 4
    oa = providers._openai_body({'model': 'gpt-x', 'kind': 'openai'}, prompting.plain(system), msgs, True, False, 1024)
    assert oa['messages'][0]['content'] == 'RULES\n\nNOW 12:00'
    _, oa2, _ = providers._request({'kind': 'openai', 'model': 'gpt-x', 'api_key': 'k', 'base_url': 'http://x/v1'}, system, msgs, True)
    assert '<!-- dynamic -->' not in oa2['messages'][0]['content']


def test_auto_switches_itself_to_plan_mode(monkeypatch, svc):
    """enter_plan_mode (auto only): the change tools go for the rest of the turn, submit_plan comes, a change sent
    after it in the same step is refused, and the page gets a 'mode' event before the plan."""
    seen = script(monkeypatch,
                  ('', [('enter_plan_mode', {'reason': 'This moves 300 images over 3 folders'}),
                        ('create_folder', {'bucket': 'b1', 'path': 'val'})]),
                  ('', [('list_folder', {'bucket': 'b1', 'path': ''})]),
                  ('', [('submit_plan', {'summary': 'Split into val', 'steps': ['create val/', 'move 300 images']})]))
    run = start('reorganise everything', mode='auto')
    assert 'enter_plan_mode' in run.tools and 'submit_plan' not in run.tools
    evs = drain(run)
    assert [e for e in evs if e['type'] == 'mode'] == [{'type': 'mode', 'mode': 'plan', 'reason': 'This moves 300 images over 3 folders'}]
    assert types(evs)[-2:] == ['plan_proposal', 'done'] and 'confirm' not in types(evs)
    names = {s['name'] for s in seen[1]['specs']}
    assert 'submit_plan' in names and not names & {'create_folder', 'move', 'delete', 'enter_plan_mode'}
    res = [p['tool_result'] for p in seen[1]['msgs'][-1]['parts']]
    assert 'Plan mode is on' in res[0]['content'] and 'not available in mode plan' in res[1]['content']
    # not offered in ask / plan
    assert 'enter_plan_mode' not in start('x', mode='ask').tools and 'enter_plan_mode' not in start('x', mode='plan').tools


# ---------------------------------------------------------------- a mid-size model: tool groups
def test_medium_model_profile_and_tool_groups(monkeypatch, svc):
    """15-40B (or a context window of 40k or less): the look-ups plus the tool groups the message names, the other
    groups one more_tools call away; a call to a tool of an unloaded group loads it."""
    mid = {**OPENAI, 'model': 'qwen3-32b'}
    assert react.profile_of(mid) == 'medium' and react.profile_of({'model': 'gemma-27b-it'}) == 'medium'
    assert react.profile_of({'model': 'gpt-x', 'context_window': 32768}) == 'medium'
    assert react.profile_of({'model': 'gpt-x', 'context_window': 200000}) == 'large'
    assert react.profile_of({'model': 'qwen3-32b', 'agent_profile': 'large'}) == 'large'
    run = start('what is in the bucket cars?', provider=mid, effort='high')
    assert run.profile == 'medium' and run.max_steps == react.MEDIUM_STEPS and run.provider['temperature'] <= 0.2
    assert {'list_buckets', 'list_folder', 'search', 'more_tools', 'ask_user', 'navigate'} <= set(run.tools)
    assert not {'move', 'delete', 'extract_frames', 'import_dataset', 'set_s3_mode'} & set(run.tools)
    assert len(run.tools) < len(run.pool) and '`video`: cut videos into image frames' in run.system
    assert '## What you know' not in run.system, 'knowledge is an index for a mid-size model'
    specs = json.dumps([t.spec() for t in run.tools.values()])
    assert budget.text_tokens(run.system) + budget.text_tokens(specs) < 6500
    # the message names a group: it is loaded from the start, in the pool's order
    named = start('move the night images into val/night', provider=mid)
    assert {'move', 'copy', 'delete'} <= set(named.tools) and 'extract_frames' not in named.tools
    assert list(named.tools) == [k for k in named.pool if k in named.tools]
    # a large model has everything and no more_tools; a small one keeps its own picking
    assert 'more_tools' not in start('hi').tools and 'more_tools' not in start('hi', provider={**OPENAI, 'model': 'q-7b'}).tools
    # more_tools loads a group; an unloaded tool called directly loads its group and is called again
    seen = script(monkeypatch, ('', [('more_tools', {'group': 'video'})]), ('', [('more_tools', {'group': 'nope'})]),
                  ('', [('add_tags', {'bucket': 'cars', 'tags': ['night']})]), ('done', []))
    evs = drain(start('what is in the bucket cars?', provider=mid, mode='auto'))
    assert evs[-1]['type'] == 'done'
    r1 = seen[1]['msgs'][-1]['parts'][0]['tool_result']
    assert 'extract_frames' in r1['content'] and not r1.get('is_error')
    assert 'extract_frames' in {s['name'] for s in seen[1]['specs']} and 'extract_frames' not in {s['name'] for s in seen[0]['specs']}
    r2 = seen[2]['msgs'][-1]['parts'][0]['tool_result']
    assert r2['is_error'] and 'the groups are:' in r2['content'] and 'files' in r2['content']
    r3 = seen[3]['msgs'][-1]['parts'][0]['tool_result']
    assert r3['is_error'] and "tool group 'tags', loaded now: call add_tags again" in r3['content']
    assert 'add_tags' in {s['name'] for s in seen[3]['specs']}


def test_every_tool_group_names_real_tools():
    names = {n for _, ns, _ in tools.GROUPS.values() for n in ns.split()}
    assert names <= set(tools.REGISTRY), names - set(tools.REGISTRY)
    assert not [n for n in names if tools.REGISTRY[n].domain in ('control', 'common', 'web')]
    assert tools.group_of('move') == 'files' and tools.group_of('list_folder') is None


# ---------------------------------------------------------------- at a limit: compact the work so far and go on
def _summary(monkeypatch, fail=False):
    """providers.complete for the progress summary: records what it was given."""
    asked = []

    def complete(p, system, msgs, max_tokens=None, quiet=False, **kw):
        asked.append({'system': system, 'text': msgs[0]['parts'][0]['text']})
        if fail:
            raise LLMError('summary down', 400)
        return '## Goal\nlist the folders\n## Next step\nanswer', {}

    monkeypatch.setattr(providers, 'complete', complete)
    return asked


def _calls(n):
    return [('', [('list_folder', {'bucket': 'cars', 'path': f'p{i}'})]) for i in range(n)]


def test_step_limit_compacts_the_work_and_goes_on(monkeypatch, svc):
    seen = script(monkeypatch, *_calls(3), ('Found them all.', []))
    asked = _summary(monkeypatch)
    run = start('list every folder')
    run.max_steps = 3
    evs = drain(run)
    rc = [e for e in evs if e['type'] == 'run_compact']
    assert [e['phase'] for e in rc] == ['start', 'done'] and rc[1]['reason'] == 'steps' and rc[1]['steps'] == 3
    assert 'list_folder' in asked[0]['text'] and 'p2' in asked[0]['text'], 'the steps go into the summary'
    assert 'Goal' in asked[0]['system'] and 'Next step' in asked[0]['system']
    last = seen[3]['msgs']
    assert [m['role'] for m in last] == ['user', 'user'] and 'compacted' in json.dumps(last[1]) \
        and 'list the folders' in json.dumps(last[1]), 'only the user turn + the progress note reach the model'
    assert evs[-1]['type'] == 'done' and any(e['type'] == 'text' for e in evs)
    assert max(e['max'] for e in evs if e['type'] == 'step') == 6, 'a new batch of steps'


def test_context_limit_compacts_and_off_or_failure_stops(monkeypatch, svc):
    big = 'x' * 5000
    monkeypatch.setattr(svc, 'ls', lambda user, bid, path='', **k: {
        'path': path, 'total': 1, 'entries': [{'name': big, 'path': big, 'type': 'file', 'kind': 'image', 'size': 1}]})
    script(monkeypatch, *_calls(3), ('Done.', []))
    asked = _summary(monkeypatch)
    run = start('list every folder')
    run.limit = run.est + 2000          # (est counts the tool specs) one big result fills it: compacted before the next
    evs = drain(run)
    rc = [e for e in evs if e['type'] == 'run_compact']
    assert rc and rc[-1]['phase'] == 'done' and rc[0]['reason'] == 'context' and asked
    assert not any(e['type'] == 'error' for e in evs)
    # off: the old error, no summary
    script(monkeypatch, *_calls(3), ('Done.', []))
    asked.clear()
    run = start('list every folder', compact='off')
    run.max_steps = 2
    evs = drain(run)
    assert not asked and 'stopped after 2 steps' in evs[-1]['detail'] and '/autocompact' in evs[-1]['detail']
    # the summary call fails: phase failed, then the limit's own error
    script(monkeypatch, *_calls(3))
    _summary(monkeypatch, fail=True)
    run = start('list every folder')
    run.max_steps = 2
    evs = drain(run)
    assert [e['phase'] for e in evs if e['type'] == 'run_compact'] == ['start', 'failed'] and evs[-1]['type'] == 'error'


def test_compactions_per_answer_are_capped(monkeypatch, svc):
    script(monkeypatch, *_calls(20))
    asked = _summary(monkeypatch)
    run = start('list every folder')
    run.max_steps = 2
    evs = drain(run)
    assert len(asked) == react.RUN_COMPACT_MAX and evs[-1]['type'] == 'error'
    assert f'({react.RUN_COMPACT_MAX} compactions' in evs[-1]['detail']


def test_ask_mode_waits_for_the_user(monkeypatch, svc):
    asked = _summary(monkeypatch)
    for approve, want in ((True, 'done'), (False, 'declined')):
        script(monkeypatch, *_calls(2), ('Done.', []))
        asked.clear()
        run = start('list every folder', compact='ask')
        run.max_steps = 2
        out = []
        for ev in react.events(run):
            out.append(ev)
            if ev['type'] == 'run_compact' and ev['phase'] == 'ask':
                ap, rj = (['a1'], []) if approve else ([], ['a1'])
                threading.Timer(0.05, lambda e=ev, a=ap, r=rj: confirm.decide(ALICE, e['confirm_id'], a, r)).start()
        phases = [e['phase'] for e in out if e['type'] == 'run_compact']
        assert phases[0] == 'ask' and phases[-1] == want
        assert bool(asked) == approve and out[-1]['type'] == 'done'
