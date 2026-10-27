"""The Data Center agent (llm/tools.py, llm/agent.py) with no network and no real datacenter.service: provider
request bodies with tools and a tool history (OpenAI / Anthropic / Gemini JSON shapes), tool calls parsed from
canned answers, the agent loop on a scripted model over a fake service (read-only tools run, changes become a plan
and are not run until confirm, ask / cannot end the turn, the step limit), confirm (only the checked actions, its
user only, expiry), and the /api/llm/dc/* routes gated like every AI feature (403)."""

import json

import pytest

from dataviz.llm import agent, providers, tools

OPENAI = {'kind': 'openai', 'model': 'gpt-x', 'api_key': 'sk-test', 'base_url': 'http://fake/v1'}
COMPAT = {**OPENAI, 'kind': 'openai_compat', 'api_key': ''}
ANTHROPIC = {'kind': 'anthropic', 'model': 'claude-x', 'api_key': 'sk-ant', 'base_url': 'http://fake'}
GEMINI = {'kind': 'gemini', 'model': 'models/gemini-x', 'api_key': 'AIza', 'base_url': 'http://fake/v1beta'}
ALICE, BOB = {'id': 'u-alice', 'username': 'alice'}, {'id': 'u-bob', 'username': 'bob'}

SPECS = [{'name': 'list_buckets', 'description': 'List buckets.',
          'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': [],
                         'additionalProperties': False}},
         {'name': 'noargs', 'description': 'No arguments.', 'parameters': {'type': 'object', 'properties': {}}}]

# a conversation with one tool round: the user, the model's call, the result, the model's answer, the next question
HISTORY = [
    {'role': 'user', 'parts': [{'text': 'which buckets?'}]},
    {'role': 'assistant', 'parts': [{'text': 'Looking.'},
                                    {'tool_call': {'id': 'c1', 'name': 'list_buckets', 'args': {'q': 'cars'},
                                                   'sig': 'SIG'}}]},
    {'role': 'tool', 'parts': [{'tool_result': {'id': 'c1', 'name': 'list_buckets', 'content': '{"total":1}'}},
                               {'tool_result': {'id': 'c2', 'name': 'noargs', 'content': 'boom', 'is_error': True}}]},
    {'role': 'assistant', 'parts': [{'text': 'One bucket.'}]},
    {'role': 'user', 'parts': [{'text': 'thanks'}]},
]


# ---------------------------------------------------------------- request bodies
@pytest.mark.parametrize('p', [OPENAI, COMPAT], ids=['openai', 'openai_compat'])
def test_openai_body_with_tools(p):
    _, body, _ = providers._request(p, 'SYS', HISTORY, False, tools=SPECS)
    m = body['messages']
    assert [x['role'] for x in m] == ['system', 'user', 'assistant', 'tool', 'tool', 'assistant', 'user']
    call = m[2]
    assert call['content'] == 'Looking.'
    assert call['tool_calls'] == [{'id': 'c1', 'type': 'function',
                                   'function': {'name': 'list_buckets', 'arguments': '{"q": "cars"}'}}]
    assert m[3] == {'role': 'tool', 'tool_call_id': 'c1', 'content': '{"total":1}'}
    assert m[4]['tool_call_id'] == 'c2' and m[4]['content'] == 'boom'
    assert body['tools'][0] == {'type': 'function', 'function': {
        'name': 'list_buckets', 'description': 'List buckets.', 'parameters': SPECS[0]['parameters']}}
    assert body['stream'] is False


def test_anthropic_body_with_tools():
    _, body, _ = providers._request(ANTHROPIC, 'SYS', HISTORY, False, tools=SPECS)
    m = body['messages']
    assert [x['role'] for x in m] == ['user', 'assistant', 'user', 'assistant', 'user']   # results: a user turn
    assert m[1]['content'] == [{'type': 'text', 'text': 'Looking.'},
                               {'type': 'tool_use', 'id': 'c1', 'name': 'list_buckets', 'input': {'q': 'cars'}}]
    user = m[2]['content']
    assert user[0] == {'type': 'tool_result', 'tool_use_id': 'c1', 'content': '{"total":1}'}
    assert user[1] == {'type': 'tool_result', 'tool_use_id': 'c2', 'content': 'boom', 'is_error': True}
    assert len(user) == 2
    assert body['tools'][0] == {'name': 'list_buckets', 'description': 'List buckets.',
                                'input_schema': SPECS[0]['parameters']}


def test_gemini_body_with_tools():
    path, body, _ = providers._request(GEMINI, 'SYS', HISTORY, False, tools=SPECS)
    assert path == '/models/gemini-x:generateContent'
    c = body['contents']
    assert [x['role'] for x in c] == ['user', 'model', 'user', 'model', 'user']
    assert c[1]['parts'][1] == {'functionCall': {'name': 'list_buckets', 'args': {'q': 'cars'}},
                                'thoughtSignature': 'SIG'}
    assert c[2]['parts'][0] == {'functionResponse': {'name': 'list_buckets', 'response': {'content': '{"total":1}'}}}
    assert c[2]['parts'][1] == {'functionResponse': {'name': 'noargs', 'response': {'error': 'boom'}}}
    decl = body['tools'][0]['functionDeclarations']
    assert decl[0] == {'name': 'list_buckets', 'description': 'List buckets.',
                       'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}   # no additionalProperties
    assert 'parameters' not in decl[1]                    # an empty object schema is left out (Gemini rejects it)
    assert body['systemInstruction'] == {'parts': [{'text': 'SYS'}]}


def test_anthropic_results_open_the_user_turn():
    msgs = HISTORY[:3] + [{'role': 'user', 'parts': [{'text': 'and more?'}]}]    # results + a new question
    m = providers._anthropic_body(ANTHROPIC, '', msgs, False, None)['messages']
    assert [x['role'] for x in m] == ['user', 'assistant', 'user']
    assert [x['type'] for x in m[2]['content']] == ['tool_result', 'tool_result', 'text']
    o = providers._openai_body(OPENAI, '', msgs, False, False, None)['messages']
    assert [x['role'] for x in o] == ['user', 'assistant', 'tool', 'tool', 'user']


def test_gemini_schema_strips_unsupported_keys():
    s = tools.TOOLS['list_folder'].schema
    g = providers.gemini_schema(s)
    assert 'additionalProperties' not in g and g['required'] == ['bucket']
    assert g['properties']['sort']['enum'] == ['name', '-mtime', 'size']
    assert providers.gemini_schema({'type': ['string', 'null']}) == {'type': 'string', 'nullable': True}
    for t in tools.specs():                               # every real tool survives the conversion
        json.dumps(providers.gemini_schema(t['parameters']))


# ---------------------------------------------------------------- parsing answers
class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


CANNED = {
    'openai': {'choices': [{'message': {'role': 'assistant', 'content': 'Checking.', 'tool_calls': [
        {'id': 'call_a', 'type': 'function', 'function': {'name': 'list_buckets', 'arguments': '{"q": "cars"}'}},
        {'id': 'call_b', 'type': 'function', 'function': {'name': 'search', 'arguments': 'not json'}},
        {'type': 'function', 'function': {'name': 'noargs', 'arguments': ''}}]}}],
        'usage': {'prompt_tokens': 30, 'completion_tokens': 7}},
    'anthropic': {'content': [{'type': 'text', 'text': 'Checking.'},
                              {'type': 'tool_use', 'id': 'toolu_a', 'name': 'list_buckets', 'input': {'q': 'cars'}},
                              {'type': 'tool_use', 'id': 'toolu_b', 'name': 'search', 'input': 'oops'},
                              {'type': 'tool_use', 'id': 'toolu_c', 'name': 'noargs', 'input': {}}],
                  'usage': {'input_tokens': 20, 'cache_read_input_tokens': 10, 'output_tokens': 7}},
    'gemini': {'candidates': [{'content': {'role': 'model', 'parts': [
        {'text': 'Checking.'},
        {'functionCall': {'name': 'list_buckets', 'args': {'q': 'cars'}}, 'thoughtSignature': 'S1'},
        {'functionCall': {'name': 'search', 'args': ['bad']}},
        {'functionCall': {'id': 'g3', 'name': 'noargs'}}]}}],
        'usageMetadata': {'promptTokenCount': 30, 'candidatesTokenCount': 7}},
}


@pytest.mark.parametrize('p', [OPENAI, COMPAT, ANTHROPIC, GEMINI], ids=['openai', 'openai_compat', 'anthropic', 'gemini'])
def test_complete_tools_parses_calls(monkeypatch, p):
    sent = {}

    def post(prov, path, body, h, what):
        sent.update(path=path, body=body)
        return _Resp(CANNED['openai' if p['kind'] == 'openai_compat' else p['kind']])

    monkeypatch.setattr(providers, '_post', post)
    ans = providers.complete_tools(p, 'SYS', [{'role': 'user', 'parts': [{'text': 'hi'}]}], SPECS)
    assert 'tools' in sent['body']
    assert ans['text'] == 'Checking.' and ans['usage'] == {'in': 30, 'out': 7}
    calls = ans['tool_calls']
    assert [c['name'] for c in calls] == ['list_buckets', 'search', 'noargs']
    assert calls[0]['args'] == {'q': 'cars'}
    assert calls[1]['args'] is None                        # not a JSON object: the agent reports it back
    assert calls[2]['args'] == {}
    if p['kind'] == 'gemini':
        assert calls[0]['sig'] == 'S1' and calls[0]['id'] == '' and calls[2]['id'] == 'g3'
    elif p['kind'] == 'anthropic':
        assert [c['id'] for c in calls] == ['toolu_a', 'toolu_b', 'toolu_c']
    else:
        assert [c['id'] for c in calls] == ['call_a', 'call_b', '']


def test_complete_tools_error_status(monkeypatch):
    monkeypatch.setattr(providers, '_post', lambda *a: _Resp({'error': {'message': 'bad tool schema'}}, 400))
    with pytest.raises(providers.LLMError) as e:
        providers.complete_tools(OPENAI, '', [{'role': 'user', 'parts': [{'text': 'hi'}]}], SPECS)
    assert e.value.status == 400 and 'bad tool schema' in str(e.value) and 'sk-test' not in str(e.value)


# ---------------------------------------------------------------- fake service + scripted model
class FakeService:
    """The datacenter.service functions the tools call, recording every call."""

    def __init__(self):
        self.calls = []
        self.buckets = [
            {'id': 'b1', 'name': 'cars', 'kind': 'managed', 'writable': True, 'format': {'format': 'yolo-detect'},
             'tags': [{'tag': 'format:yolo-detect', 'auto': True}], 'stats': {'images': 120, 'size': 9000}},
            {'id': 'b2', 'name': 'dupe', 'kind': 'managed', 'writable': True, 'tags': [], 'stats': {}},
            {'id': 'b3', 'name': 'dupe', 'kind': 'local', 'writable': True, 'tags': [], 'stats': {}},
            {'id': 'b4', 'name': 'cloud', 'kind': 's3', 'writable': False, 'tags': [], 'stats': {}},
        ]

    def list_buckets(self, user, q='', tags=(), formats=()):
        self.calls.append(('list_buckets', q))
        return [b for b in self.buckets if q.lower() in b['name'].lower()]

    def get_bucket(self, user, bid):
        return next(b for b in self.buckets if b['id'] == bid)

    def ls(self, user, bid, path='', offset=0, limit=500, sort='name', q=''):
        self.calls.append(('ls', bid, path))
        ents = [{'name': f'img{i}.jpg', 'path': f'{path}/img{i}.jpg'.lstrip('/'), 'type': 'file', 'kind': 'image',
                 'size': 10} for i in range(3)]
        return {'path': path, 'total': 3, 'entries': ents[offset:offset + limit], 'format': None, 'tags': [],
                'writable': True}

    def search(self, user, q='', tags=(), formats=(), bid=None, path='', glob=None, limit=200):
        self.calls.append(('search', q, bid, glob))
        return {'buckets': [], 'files': [{'bucket': 'b1', 'path': f'night/n{i}.jpg', 'name': f'n{i}.jpg',
                                          'type': 'file', 'size': 1} for i in range(38)]}

    def ops(self, user, bid, ops):
        self.calls.append(('ops', bid, ops))
        return {'applied': ops, 'errors': [], 'undo_id': 'u1',
                'retagged': {'path': '', 'from': 'yolo-detect', 'to': 'other', 'reason': 'labels/ missing'}}

    def set_tags(self, user, bid, path='', add=(), remove=()):
        self.calls.append(('set_tags', bid, path, tuple(add), tuple(remove)))
        return [{'tag': t, 'auto': False} for t in add]


@pytest.fixture
def svc(monkeypatch):
    s = FakeService()
    monkeypatch.setattr(tools, '_svc', lambda: s)
    agent._plans.clear()
    yield s
    agent._plans.clear()


def script(monkeypatch, *steps):
    """The model answers each step with the next (text, [(name, args), ...]); every request is recorded."""
    seen = []
    it = iter(steps)

    def complete_tools(p, system, msgs, specs, max_tokens=None):
        seen.append({'system': system, 'msgs': json.loads(json.dumps(msgs, default=str)), 'specs': specs})
        text, calls = next(it)
        return {'text': text, 'tool_calls': [{'id': '', 'name': n, 'args': a} for n, a in calls],
                'usage': {'in': 100, 'out': 10}}

    monkeypatch.setattr(providers, 'complete_tools', complete_tools)
    return seen


def chat(user, text, scope=None, **kw):
    return list(agent.run(user, OPENAI, [{'role': 'user', 'text': text}], scope or {}, **kw))


def types(evs):
    return [e['type'] for e in evs]


# ---------------------------------------------------------------- the loop
def test_read_only_tool_runs_then_answers(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('search', {'glob': '*night*.jpg', 'bucket': 'cars'})]),
                  ('38 night images in cars.', []))
    evs = chat(ALICE, 'find the night images', {'bucket': 'b1', 'path': 'train', 'selection': ['train/a.jpg']})
    assert types(evs) == ['meta', 'tool', 'delta', 'done']
    tool = evs[1]
    assert tool['name'] == 'search' and tool['ok'] is True and tool['summary'] == '→ 38 matches'
    assert tool['id'] and tool['args'] == {'glob': '*night*.jpg', 'bucket': 'cars'}
    assert ('search', '', 'b1', '*night*.jpg') in svc.calls          # the bucket name resolved to its id
    assert evs[0]['model'] == 'gpt-x' and evs[0]['in_est'] > 0
    assert evs[-1]['usage'] == {'in': 200, 'out': 20}
    assert all(set(e) >= {'type'} and 't' not in e for e in evs)    # the contract's key, not the ◆ chat's 't'
    # the second step saw the call and its result, the system prompt the tools, rules and the scope
    msgs = seen[1]['msgs']
    assert msgs[-2]['role'] == 'assistant' and msgs[-2]['parts'][0]['tool_call']['name'] == 'search'
    res = msgs[-1]['parts'][0]['tool_result']
    assert msgs[-1]['role'] == 'tool' and res['id'] == tool['id'] and '"files_total":38' in res['content']
    sysp = seen[0]['system']
    assert '`ask_user`' in sysp and '`cannot_do`' in sysp and "'cars'" in sysp and 'train/a.jpg' in sysp
    assert {s['name'] for s in seen[0]['specs']} == set(tools.TOOLS)


def test_move_becomes_plan_and_is_not_run(monkeypatch, svc):
    script(monkeypatch, ('', [('list_folder', {'bucket': 'cars', 'path': 'train'})]),
           ('I will move them.', [('move', {'bucket': 'cars', 'paths': ['train/img0.jpg', 'train/img1.jpg'],
                                            'to': 'val'}),
                                  ('add_tags', {'bucket': 'cars', 'tags': 'checked'})]))
    evs = chat(ALICE, 'move img0 and img1 to val and tag the bucket checked')
    assert types(evs) == ['meta', 'tool', 'delta', 'plan', 'done']
    plan = evs[3]
    assert [a['tool'] for a in plan['actions']] == ['move', 'add_tags']
    assert [a['id'] for a in plan['actions']] == ['a1', 'a2']
    assert plan['actions'][0]['args'] == {'bucket': 'b1', 'paths': ['train/img0.jpg', 'train/img1.jpg'], 'to': 'val'}
    assert plan['actions'][1]['args']['tags'] == ['checked']        # a bare string became a list
    assert 'Move 2 items' in plan['actions'][0]['summary'] and 'val/' in plan['actions'][0]['summary']
    assert not [c for c in svc.calls if c[0] in ('ops', 'set_tags')]  # nothing changed yet

    # confirm only the tag: the move never runs
    out = agent.confirm(ALICE, plan['plan_id'], ['a2'])
    assert [r['id'] for r in out['results']] == ['a2'] and out['results'][0]['ok']
    assert [c[0] for c in svc.calls if c[0] in ('ops', 'set_tags')] == ['set_tags']
    with pytest.raises(agent.AgentError) as e:                      # a plan runs once
        agent.confirm(ALICE, plan['plan_id'], ['a1'])
    assert e.value.status == 404


def test_confirm_runs_in_plan_order_and_aggregates_retagged(monkeypatch, svc):
    script(monkeypatch, ('', [('delete', {'bucket': 'cars', 'paths': ['labels']}),
                              ('create_folder', {'bucket': 'cars', 'path': 'archive'})]))
    plan = chat(ALICE, 'delete labels, make archive')[-2]
    assert plan['type'] == 'plan'
    out = agent.confirm(ALICE, plan['plan_id'], ['a2', 'a1'])        # order of the plan, not of the request
    ops = [c[2][0]['op'] for c in svc.calls if c[0] == 'ops']
    assert ops == ['delete', 'mkdir']
    assert [r['id'] for r in out['results']] == ['a1', 'a2']
    assert out['results'][0]['undo_id'] == 'u1' and out['results'][0]['bucket'] == 'b1'
    assert len(out['retagged']) == 2 and out['retagged'][0] == {'bucket': 'b1', 'path': '', 'from': 'yolo-detect',
                                                                 'to': 'other', 'reason': 'labels/ missing'}


def test_ask_user_ends_turn(monkeypatch, svc):
    script(monkeypatch, ('', [('list_buckets', {}), ('ask_user', {'question': 'Which one?',
                                                                  'options': ['dupe (managed)', 'dupe (local)']})]))
    evs = chat(ALICE, 'clean up dupe')
    assert types(evs) == ['meta', 'ask', 'done']
    assert evs[1] == {'type': 'ask', 'question': 'Which one?', 'options': ['dupe (managed)', 'dupe (local)']}
    assert not svc.calls                                             # the turn ended before the lookup ran


def test_cannot_do_ends_turn(monkeypatch, svc):
    script(monkeypatch, ('', [('cannot_do', {'reason': 'I cannot turn images into a video.',
                                             'alternative': 'I can open the folder in the visualizer.'})]))
    evs = chat(ALICE, 'make a video of cars')
    assert types(evs) == ['meta', 'cannot', 'done']
    assert evs[1]['reason'].startswith('I cannot') and evs[1]['alternative'].startswith('I can open')
    script(monkeypatch, ('', [('cannot_do', {'reason': 'no'})]))
    assert chat(ALICE, 'x')[1]['alternative'] is None


def test_ambiguous_bucket_and_read_only_bucket_go_back_to_model(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('delete', {'bucket': 'dupe', 'paths': ['a.jpg']}),
                                     ('create_folder', {'bucket': 'cars', 'path': 'x'})]),
                  ('', [('delete', {'bucket': 'cloud', 'paths': ['a.jpg']})]),
                  ('Which dupe?', []))
    evs = chat(ALICE, 'delete a.jpg in dupe')
    assert 'plan' not in types(evs) and types(evs)[-1] == 'done'     # a broken change is never proposed
    first = [p['tool_result'] for p in seen[1]['msgs'][-1]['parts']]
    assert first[0]['is_error'] and 'ask_user' in first[0]['content'] and 'b2' in first[0]['content']
    assert first[1]['content'].startswith('not proposed')            # the valid one is withheld too
    second = seen[2]['msgs'][-1]['parts'][0]['tool_result']
    assert second['is_error'] and 'read-only' in second['content']
    assert not [c for c in svc.calls if c[0] == 'ops']


def test_bucket_resolution(svc):
    assert tools.resolve_bucket(ALICE, 'b3')['id'] == 'b3'
    assert tools.resolve_bucket(ALICE, 'CARS')['id'] == 'b1'         # a unique case-insensitive name
    with pytest.raises(tools.ToolError, match='2 buckets are named'):
        tools.resolve_bucket(ALICE, 'dupe')
    with pytest.raises(tools.ToolError, match="no bucket 'nope'"):
        tools.resolve_bucket(ALICE, 'nope')


def test_bad_args_and_unknown_tool_are_reported(monkeypatch, svc):
    seen = script(monkeypatch, ('', [('list_folder', {'path': 'x'}), ('frobnicate', {}), ('search', None)]),
                  ('Sorry.', []))
    evs = chat(ALICE, 'look')
    assert types(evs) == ['meta', 'tool', 'delta', 'done']
    assert evs[1]['ok'] is False and 'bucket' in evs[1]['summary']
    res = [p['tool_result'] for p in seen[1]['msgs'][-1]['parts']]
    assert all(r.get('is_error') for r in res) and 'unknown tool' in res[1]['content']
    assert len({r['id'] for r in res}) == 3                          # unique ids for the history


def test_step_limit(monkeypatch, svc):
    script(monkeypatch, *[('', [('list_buckets', {})])] * 3)
    evs = chat(ALICE, 'loop forever', max_steps=3)
    assert types(evs) == ['meta', 'tool', 'tool', 'tool', 'error']
    assert 'stopped after 3' in evs[-1]['detail']
    assert len({e['id'] for e in evs if e['type'] == 'tool'}) == 3
    assert agent.MAX_STEPS == 8


def test_empty_answer_is_an_error(monkeypatch, svc):
    script(monkeypatch, ('', []))
    assert types(chat(ALICE, 'hi')) == ['meta', 'error']


def test_last_message_must_be_users(svc):
    with pytest.raises(providers.LLMError):
        agent.prepare(ALICE, OPENAI, [{'role': 'assistant', 'text': 'hi'}], {})


# ---------------------------------------------------------------- confirm guards
def _plan(monkeypatch, user):
    script(monkeypatch, ('', [('create_folder', {'bucket': 'cars', 'path': 'new'})]))
    return chat(user, 'make new')[-2]['plan_id']


def test_other_users_plan_rejected(monkeypatch, svc):
    pid = _plan(monkeypatch, ALICE)
    with pytest.raises(agent.AgentError) as e:
        agent.confirm(BOB, pid, ['a1'])
    assert e.value.status == 403
    assert not [c for c in svc.calls if c[0] == 'ops']
    assert agent.confirm(ALICE, pid, ['a1'])['results'][0]['ok']    # still there for its owner


def test_expired_plan_rejected(monkeypatch, svc):
    pid = _plan(monkeypatch, ALICE)
    agent._plans[pid]['created'] -= agent.PLAN_TTL + 1
    with pytest.raises(agent.AgentError) as e:
        agent.confirm(ALICE, pid, ['a1'])
    assert e.value.status == 410 and pid not in agent._plans
    assert not [c for c in svc.calls if c[0] == 'ops']


def test_confirm_bad_ids(monkeypatch, svc):
    pid = _plan(monkeypatch, ALICE)
    for ids, status in ((['a9'], 400), ([], 400)):
        with pytest.raises(agent.AgentError) as e:
            agent.confirm(ALICE, pid, ids)
        assert e.value.status == status
    with pytest.raises(agent.AgentError) as e:
        agent.confirm(ALICE, 'nope', ['a1'])
    assert e.value.status == 404


def test_plans_bounded_per_user(monkeypatch, svc):
    pids = [_plan(monkeypatch, ALICE) for _ in range(agent.MAX_USER_PLANS + 2)]
    other = _plan(monkeypatch, BOB)
    assert sum(v['user_id'] == ALICE['id'] for v in agent._plans.values()) == agent.MAX_USER_PLANS
    assert pids[0] not in agent._plans and pids[-1] in agent._plans and other in agent._plans


# ---------------------------------------------------------------- catalogue / CLI
def test_catalog_lists_every_tool():
    cat = tools.catalog()
    for name, t in tools.TOOLS.items():
        assert f'| `{name}` | {t.kind} |' in cat
    assert '| `move` | change | bucket, paths, to |' in cat
    from pathlib import Path
    skill = Path(__file__).resolve().parents[1] / '.claude' / 'skills' / 'datacenter' / 'SKILL.md'
    if skill.exists():                                  # the skill shows the same table: regenerate it on a change
        assert cat in skill.read_text(encoding='utf-8')


def test_cli_list_and_usage(capsys):
    from dataviz import datacenter_cli
    assert datacenter_cli.main(['list']) == 0
    assert '`list_buckets`' in capsys.readouterr().out
    assert datacenter_cli.main(['ask_user', '--user', 'x']) == 2
    assert datacenter_cli.main(['search', '--args', '{bad', '--user', 'x']) == 2
    assert datacenter_cli.main(['search']) == 2                      # --user is required


# ---------------------------------------------------------------- routes
def test_dc_routes_gated(client):
    msg = {'messages': [{'role': 'user', 'text': 'hi'}], 'scope': {}}
    assert client.post('/api/llm/dc/chat', json=msg).status_code == 403            # feature off
    assert client.post('/api/llm/dc/confirm', json={'plan_id': 'x', 'action_ids': ['a1']}).status_code == 403
