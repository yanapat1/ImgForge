"""Streamed tool calling (providers.stream_tools) over fake SSE bodies, no network: per provider kind the text,
thinking and tool-call events (OpenAI arguments split over chunks, parallel calls, reasoning_content, <think> tags),
Claude's thinking signature sent back in the next request, Gemini thought + functionCall signatures, native web
search events and the fallback memory when a provider refuses it, a refused parameter dropped and remembered,
usage, and errors (a stream that breaks off is transient, the key never shows)."""

import json

import httpx
import pytest

from dataviz.llm import providers
from dataviz.llm.providers import LLMError

OPENAI = {'kind': 'openai', 'model': 'gpt-x', 'api_key': 'sk-test', 'base_url': 'http://fake/v1'}
COMPAT = {**OPENAI, 'kind': 'openai_compat', 'model': 'qwen3-8b', 'api_key': ''}
CLAUDE = {'kind': 'anthropic', 'model': 'claude-sonnet-4-5', 'api_key': 'sk-ant-secret', 'base_url': 'http://fake'}
GEMINI = {'kind': 'gemini', 'model': 'models/gemini-2.5-flash', 'api_key': 'AIza', 'base_url': 'http://fake/v1beta'}
SPECS = [{'name': 'list_buckets', 'description': 'List buckets.',
          'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}}},
         {'name': 'list_folder', 'description': 'List a folder.',
          'parameters': {'type': 'object', 'properties': {'bucket': {'type': 'string'}}}}]
ASK = [{'role': 'user', 'parts': [{'text': 'which buckets?'}]}]


def sse(*events, done=False):
    """An SSE body: each event a `data:` line (Claude's also carry an `event:` line, ignored by the parser)."""
    out = ''
    for e in events:
        if isinstance(e, dict) and 'type' in e:
            out += f'event: {e["type"]}\n'
        out += 'data: ' + json.dumps(e) + '\n\n'
    return out + ('data: [DONE]\n\n' if done else '')


class Fake:
    """Answers each request with the next scripted (status, body); keeps the request bodies and headers."""

    def __init__(self, *answers):
        self.answers, self.bodies, self.headers, self.paths = list(answers), [], [], []

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        self.paths.append(str(request.url))
        status, body = self.answers.pop(0)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, content=body.encode(), headers={'content-type': 'text/event-stream'})


@pytest.fixture
def fake(monkeypatch):
    providers._refused.clear()
    providers._native_off.clear()

    def install(*answers):
        f = Fake(*answers)
        client = httpx.Client(transport=httpx.MockTransport(f), base_url='http://fake')
        monkeypatch.setattr(providers, '_client', lambda p: client)
        return f
    yield install
    providers._refused.clear()
    providers._native_off.clear()


def run(p, msgs=ASK, **kw):
    return list(providers.stream_tools(p, 'SYS', msgs, SPECS, **kw))


def of(evs, kind):
    return [v for k, v in evs if k == kind]


# ---------------------------------------------------------------- OpenAI / compatible
def oa(delta=None, usage=None, **extra):
    ev = {'choices': [{'index': 0, 'delta': delta}] if delta is not None else []}
    if usage:
        ev['usage'] = usage
    ev.update(extra)
    return ev


def test_openai_text_and_two_parallel_calls_with_split_arguments(fake):
    f = fake((200, sse(
        oa({'role': 'assistant', 'content': 'Look'}), oa({'content': 'ing.'}),
        oa({'tool_calls': [{'index': 0, 'id': 'call_a', 'type': 'function',
                            'function': {'name': 'list_buckets', 'arguments': ''}}]}),
        oa({'tool_calls': [{'index': 0, 'function': {'arguments': '{"q": "ca'}}]}),
        oa({'tool_calls': [{'index': 1, 'id': 'call_b', 'function': {'name': 'list_folder', 'arguments': '{"buc'}}]}),
        oa({'tool_calls': [{'index': 0, 'function': {'arguments': 'rs"}'}}]}),
        oa({'tool_calls': [{'index': 1, 'function': {'arguments': 'ket": "b1"}'}}]}),
        oa(usage={'prompt_tokens': 50, 'completion_tokens': 9}), done=True)))
    evs = run(OPENAI)
    assert ''.join(of(evs, 'text')) == 'Looking.'
    assert of(evs, 'tool_call') == [{'id': 'call_a', 'name': 'list_buckets', 'args': {'q': 'cars'}},
                                    {'id': 'call_b', 'name': 'list_folder', 'args': {'bucket': 'b1'}}]
    assert evs[-1] == ('usage', {'in': 50, 'out': 9})
    body = f.bodies[0]
    assert body['stream'] is True and body['stream_options'] == {'include_usage': True}
    assert [t['function']['name'] for t in body['tools']] == ['list_buckets', 'list_folder']
    assert 'reasoning_effort' not in body                       # gpt-x is not a reasoning model


def test_openai_bad_arguments_are_none_and_reasoning_effort(fake):
    f = fake((200, sse(oa({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'list_buckets',
                                                                              'arguments': '[1, 2'}}]}), done=True)))
    evs = run({**OPENAI, 'model': 'o4-mini', 'temperature': 0.2}, think='high')
    assert of(evs, 'tool_call') == [{'id': 'c', 'name': 'list_buckets', 'args': None}]
    assert f.bodies[0]['reasoning_effort'] == 'high' and 'temperature' not in f.bodies[0]
    assert evs[-1] == ('usage', {'in': 0, 'out': 0})


def test_compat_reasoning_content_and_think_tags(fake):
    f = fake((200, sse(oa({'reasoning_content': 'Need the '}), oa({'reasoning_content': 'list.'}),
                       oa({'content': 'Here.'}), done=True)),
             (200, sse(oa({'content': '<thi'}), oa({'content': 'nk>plan it</th'}), oa({'content': 'ink>\n\nDone'}),
                       oa({'content': '!'}), done=True)))
    evs = run(COMPAT, think='low')
    assert ''.join(x['text'] for x in of(evs, 'thinking')) == 'Need the list.'
    assert of(evs, 'text') == ['Here.']
    assert f.bodies[0]['chat_template_kwargs'] == {'enable_thinking': True}
    evs = run(COMPAT, think='off')
    assert ''.join(x['text'] for x in of(evs, 'thinking')) == 'plan it'
    assert ''.join(of(evs, 'text')) == 'Done!'
    assert f.bodies[1]['chat_template_kwargs'] == {'enable_thinking': False}


def test_compat_calls_without_index(fake):
    fake((200, sse(oa({'tool_calls': [{'id': 'x1', 'function': {'name': 'list_buckets', 'arguments': '{}'}}]}),
                   oa({'tool_calls': [{'id': 'x2', 'function': {'name': 'list_folder', 'arguments': {'bucket': 'b'}}}]}),
                   done=True)))
    assert [(c['id'], c['args']) for c in of(run(COMPAT), 'tool_call')] == [('x1', {}), ('x2', {'bucket': 'b'})]


def test_refused_param_is_dropped_and_remembered(fake):
    """A compatible server that rejects stream_options: dropped, sent again, and left out from then on."""
    f = fake((400, {'error': {'message': 'Unrecognized request argument supplied: stream_options'}}),
             (200, sse(oa({'content': 'ok'}), done=True)),
             (200, sse(oa({'content': 'again'}), done=True)))
    evs = run(COMPAT)
    assert of(evs, 'text') == ['ok']
    assert 'stream_options' in f.bodies[0] and 'stream_options' not in f.bodies[1]
    assert run(COMPAT)[0] == ('text', 'again')
    assert len(f.bodies) == 3 and 'stream_options' not in f.bodies[2]
    other = {**COMPAT, 'model': 'llama3.1:8b'}                 # per model: another one still gets it
    f2 = fake((200, sse(oa({'content': 'x'}), done=True)))
    run(other)
    assert 'stream_options' in f2.bodies[0]


def test_temperature_refusal_is_remembered_for_complete_too(fake):
    p = {**OPENAI, 'model': 'gpt-4.1', 'temperature': 0.3}
    f = fake((400, {'error': {'message': "Unsupported value: 'temperature' does not support 0.3"}}),
             (200, {'choices': [{'message': {'content': 'hi'}}]}),
             (200, {'choices': [{'message': {'content': 'hi again'}}]}))
    assert providers.complete(p, '', ASK)[0] == 'hi'
    assert providers.complete(p, '', ASK)[0] == 'hi again'
    assert 'temperature' in f.bodies[0] and 'temperature' not in f.bodies[2]


def test_openai_search_model_native_web(fake):
    p = {**OPENAI, 'model': 'gpt-4o-search-preview'}
    assert providers.native_web_ok(p) and not providers.native_web_ok(OPENAI) and not providers.native_web_ok(COMPAT)
    f = fake((200, sse(oa({'content': 'See [1].', 'annotations': [
        {'type': 'url_citation', 'url_citation': {'url': 'https://a.example', 'title': 'A'}}]}), done=True)))
    evs = run(p, native_web=True)
    assert f.bodies[0]['web_search_options'] == {}
    assert of(evs, 'search') == [{'query': '', 'engine': 'openai', 'results': [{'title': 'A', 'url': 'https://a.example'}]}]


# ---------------------------------------------------------------- Anthropic
def an(t, **kw):
    return {'type': t, **kw}


def claude_turn():
    return sse(
        an('message_start', message={'usage': {'input_tokens': 100, 'cache_read_input_tokens': 20, 'output_tokens': 1}}),
        an('content_block_start', index=0, content_block={'type': 'thinking', 'thinking': '', 'signature': ''}),
        an('content_block_delta', index=0, delta={'type': 'thinking_delta', 'thinking': 'The user wants '}),
        an('content_block_delta', index=0, delta={'type': 'thinking_delta', 'thinking': 'buckets.'}),
        an('content_block_delta', index=0, delta={'type': 'signature_delta', 'signature': 'SIG-1'}),
        an('content_block_stop', index=0),
        an('content_block_start', index=1, content_block={'type': 'redacted_thinking', 'data': 'ENC'}),
        an('content_block_stop', index=1),
        an('content_block_start', index=2, content_block={'type': 'text', 'text': ''}),
        an('content_block_delta', index=2, delta={'type': 'text_delta', 'text': 'Checking.'}),
        an('content_block_stop', index=2),
        an('content_block_start', index=3, content_block={'type': 'tool_use', 'id': 'toolu_1', 'name': 'list_buckets',
                                                          'input': {}}),
        an('content_block_delta', index=3, delta={'type': 'input_json_delta', 'partial_json': '{"q": '}),
        an('content_block_delta', index=3, delta={'type': 'input_json_delta', 'partial_json': '"cars"}'}),
        an('content_block_stop', index=3),
        an('message_delta', delta={'stop_reason': 'tool_use'}, usage={'output_tokens': 42}),
        an('message_stop'))


def test_claude_thinking_signature_round_trip(fake):
    f = fake((200, claude_turn()), (200, sse(an('message_start', message={'usage': {'input_tokens': 5}}),
                                             an('message_delta', delta={'stop_reason': 'end_turn'},
                                                usage={'output_tokens': 1}))))
    evs = run({**CLAUDE, 'temperature': 0.5}, think='medium')
    th = of(evs, 'thinking')
    assert [x['text'] for x in th[1:]] == ['buckets.', '']      # chunks as they came ...
    signed = [x for x in th if x.get('sig')]                    # ... and the block, whole and signed
    assert signed == [{'text': 'The user wants buckets.', 'sig': 'SIG-1', 'kind': 'anthropic'},
                      {'text': '', 'sig': 'ENC', 'redacted': 'ENC', 'kind': 'anthropic'}]
    assert of(evs, 'text') == ['Checking.']
    assert of(evs, 'tool_call') == [{'id': 'toolu_1', 'name': 'list_buckets', 'args': {'q': 'cars'}}]
    assert evs[-1] == ('usage', {'in': 120, 'out': 42})
    b = f.bodies[0]
    assert b['thinking'] == {'type': 'enabled', 'budget_tokens': 8192} and b['max_tokens'] > 8192
    assert 'temperature' not in b
    assert f.headers[0]['anthropic-beta'] == 'interleaved-thinking-2025-05-14'

    # the engine's history (react.py keeps the signed thinking parts first) goes back as thinking blocks first
    msgs = ASK + [{'role': 'assistant', 'parts': [{'thinking': t} for t in signed] + [{'text': 'Checking.'}] + [
        {'tool_call': c} for c in of(evs, 'tool_call')]},
        {'role': 'tool', 'parts': [{'tool_result': {'id': 'toolu_1', 'name': 'list_buckets', 'content': '[]'}}]}]
    run(CLAUDE, msgs, think='medium')
    a = f.bodies[1]['messages'][1]
    assert a['role'] == 'assistant'
    assert a['content'] == [{'type': 'thinking', 'thinking': 'The user wants buckets.', 'signature': 'SIG-1'},
                            {'type': 'redacted_thinking', 'data': 'ENC'},
                            {'type': 'text', 'text': 'Checking.'},
                            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'list_buckets', 'input': {'q': 'cars'}}]
    assert f.bodies[1]['messages'][2]['content'][0]['type'] == 'tool_result'


def test_thinking_parts_stay_with_their_provider():
    msgs = ASK + [{'role': 'assistant', 'parts': [{'thinking': {'text': 'g', 'sig': 'GS', 'kind': 'gemini'}},
                                                  {'thinking': {'text': 'c', 'sig': 'CS', 'kind': 'anthropic'}},
                                                  {'text': 'hi'}]}, {'role': 'user', 'parts': [{'text': 'ok'}]}]
    a = providers._anthropic_body(CLAUDE, '', msgs, True, None)['messages'][1]['content']
    assert [x['type'] for x in a] == ['thinking', 'text'] and a[0]['signature'] == 'CS'
    g = providers._gemini_body(GEMINI, '', msgs, False, None)['contents'][1]['parts']
    assert g == [{'text': 'g', 'thought': True, 'thoughtSignature': 'GS'}, {'text': 'hi'}]
    o = providers._openai_body(OPENAI, '', msgs, True, False, None)['messages']
    assert o[1] == {'role': 'assistant', 'content': 'hi'}
    only = ASK + [{'role': 'assistant', 'parts': [{'thinking': {'text': 'c', 'sig': 'CS'}}]}]   # nothing else
    assert len(providers._openai_body(OPENAI, '', only, True, False, None)['messages']) == 1


@pytest.mark.parametrize('model,want', [
    ('claude-opus-4-6', {'thinking': {'type': 'adaptive', 'display': 'summarized'}, 'output_config': {'effort': 'high'}}),
    ('claude-opus-5-5', {'thinking': {'type': 'adaptive', 'display': 'summarized'}, 'output_config': {'effort': 'high'}}),
    ('claude-3-7-sonnet-20250219', {'thinking': {'type': 'enabled', 'budget_tokens': 24576}}),
    ('claude-haiku-4-5', {'thinking': {'type': 'enabled', 'budget_tokens': 24576}}),
])
def test_claude_thinking_per_model(fake, model, want):
    f = fake((200, sse(an('message_delta', delta={'stop_reason': 'end_turn'}, usage={'output_tokens': 1}))))
    run({**CLAUDE, 'model': model}, think='high')
    b = f.bodies[0]
    assert {k: b[k] for k in want} == want and b['max_tokens'] > 24576


def test_claude_off(fake):
    f = fake((200, sse(an('message_delta', delta={'stop_reason': 'end_turn'}))),
             (200, sse(an('message_delta', delta={'stop_reason': 'end_turn'}))))
    run({**CLAUDE, 'temperature': 0.2})
    assert 'thinking' not in f.bodies[0] and 'output_config' not in f.bodies[0] and f.bodies[0]['temperature'] == 0.2
    run({**CLAUDE, 'model': 'claude-opus-5-5'}, think='off')        # thinking cannot be turned off: the least of it
    assert 'thinking' not in f.bodies[1] and f.bodies[1]['output_config'] == {'effort': 'low'}


def test_claude_native_search_event_and_pause_turn(fake):
    def search_turn(stop):
        return sse(
            an('message_start', message={'usage': {'input_tokens': 10, 'output_tokens': 1}}),
            an('content_block_start', index=0, content_block={'type': 'server_tool_use', 'id': 'srv_1',
                                                              'name': 'web_search', 'input': {}}),
            an('content_block_delta', index=0, delta={'type': 'input_json_delta', 'partial_json': '{"query": "yolo v9"}'}),
            an('content_block_stop', index=0),
            an('content_block_start', index=1, content_block={
                'type': 'web_search_tool_result', 'tool_use_id': 'srv_1',
                'content': [{'type': 'web_search_result', 'url': 'https://x.example', 'title': 'X',
                             'encrypted_content': 'E'}]}),
            an('content_block_stop', index=1),
            an('message_delta', delta={'stop_reason': stop}, usage={'output_tokens': 5}))
    f = fake((200, search_turn('pause_turn')), (200, search_turn('end_turn')))
    evs = run({**CLAUDE, 'model': 'claude-opus-4-6'}, native_web=True)
    assert of(evs, 'search')[0] == {'query': 'yolo v9', 'engine': 'anthropic',
                                    'results': [{'title': 'X', 'url': 'https://x.example'}]}
    assert f.bodies[0]['tools'][-1] == {'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 5}
    assert not of(evs, 'tool_call')                     # a server tool is not the engine's to run
    cont = f.bodies[1]['messages'][-1]                  # the paused turn went back as it was
    assert cont['role'] == 'assistant' and [x['type'] for x in cont['content']] == ['server_tool_use',
                                                                                   'web_search_tool_result']
    assert evs[-1] == ('usage', {'in': 20, 'out': 10})


def test_native_search_refused_falls_back_and_is_remembered(fake):
    f = fake((400, {'type': 'error', 'error': {'type': 'invalid_request_error',
                                               'message': 'web search is not enabled for this organization'}}),
             (200, sse(an('content_block_start', index=0, content_block={'type': 'text', 'text': 'hi'}),
                       an('content_block_stop', index=0))),
             (200, sse(an('message_delta', delta={'stop_reason': 'end_turn'}))))
    assert providers.native_web_ok(CLAUDE)
    evs = run(CLAUDE, native_web=True)
    assert evs[0] == ('search', {'query': '', 'engine': 'native', 'results': [], 'unavailable': True})
    assert of(evs, 'text') == ['hi']
    assert any(t.get('name') == 'web_search' for t in f.bodies[0]['tools'])
    assert all(t.get('name') != 'web_search' for t in f.bodies[1]['tools'])
    assert not providers.native_web_ok(CLAUDE)
    evs = run(CLAUDE, native_web=True)                  # not asked again
    assert not of(evs, 'search') and all(t.get('name') != 'web_search' for t in f.bodies[2]['tools'])


def test_claude_errors_transient_and_key_scrubbed(fake):
    fake((200, sse(an('message_start', message={'usage': {'input_tokens': 1}}),
                   an('error', error={'type': 'overloaded_error', 'message': 'Overloaded sk-ant-secret'}))))
    with pytest.raises(LLMError) as e:
        run(CLAUDE)
    assert e.value.transient and 'sk-ant-secret' not in str(e.value)
    fake((429, {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'slow down'}}))
    with pytest.raises(LLMError) as e:
        run(CLAUDE)
    assert e.value.transient and e.value.status == 502
    fake((401, {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}))
    with pytest.raises(LLMError) as e:
        run(CLAUDE)
    assert not e.value.transient and e.value.status == 400


def test_timeout_mid_stream_is_transient(monkeypatch, fake):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: ' + json.dumps(oa({'content': 'par'})).encode() + b'\n\n'
            raise httpx.ReadTimeout('read timed out')

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Broken())),
                          base_url='http://fake')
    monkeypatch.setattr(providers, '_client', lambda p: client)
    got = []
    with pytest.raises(LLMError) as e:
        for ev in providers.stream_tools(OPENAI, '', ASK, SPECS):
            got.append(ev)
    assert got == [('text', 'par')] and e.value.transient and 'broke off' in str(e.value)
    assert 'sk-test' not in str(e.value)


# ---------------------------------------------------------------- Gemini
def test_gemini_thought_function_call_signature_and_grounding(fake):
    def g(parts=None, **kw):
        return {'candidates': [{'content': {'role': 'model', 'parts': parts or []}, **kw}]}
    f = fake((200, sse(
        g([{'text': 'Thinking about ', 'thought': True}]),
        g([{'text': 'buckets', 'thought': True, 'thoughtSignature': 'TS0'}]),
        g([{'text': 'Let me look.'}]),
        g([{'functionCall': {'name': 'list_buckets', 'args': {'q': 'cars'}}, 'thoughtSignature': 'TS1'},
           {'functionCall': {'name': 'list_folder', 'args': {'bucket': 'b'}}}],
          groundingMetadata={'webSearchQueries': ['bucket naming'],
                             'groundingChunks': [{'web': {'uri': 'https://g.example', 'title': 'G'}}]}),
        {'candidates': [{'content': {'parts': []}, 'finishReason': 'STOP'}],
         'usageMetadata': {'promptTokenCount': 30, 'candidatesTokenCount': 8, 'thoughtsTokenCount': 12}})))
    evs = run(GEMINI, think='low', native_web=True)
    assert of(evs, 'thinking') == [{'text': 'Thinking about ', 'kind': 'gemini'},
                                   {'text': 'buckets', 'kind': 'gemini', 'sig': 'TS0'}]
    assert of(evs, 'text') == ['Let me look.']
    assert of(evs, 'tool_call') == [{'id': '', 'name': 'list_buckets', 'args': {'q': 'cars'}, 'sig': 'TS1'},
                                    {'id': '', 'name': 'list_folder', 'args': {'bucket': 'b'}}]
    assert of(evs, 'search') == [{'query': 'bucket naming', 'engine': 'google',
                                  'results': [{'title': 'G', 'url': 'https://g.example'}]}]
    assert evs[-1] == ('usage', {'in': 30, 'out': 20})
    b = f.bodies[0]
    assert f.paths[0].endswith('/models/gemini-2.5-flash:streamGenerateContent?alt=sse')
    assert b['generationConfig']['thinkingConfig'] == {'thinkingBudget': 2048, 'includeThoughts': True}
    assert {'google_search': {}} in b['tools'] and 'functionDeclarations' in b['tools'][0]

    # the call's signature goes back with it
    msgs = ASK + [{'role': 'assistant', 'parts': [{'tool_call': c} for c in of(evs, 'tool_call')]}]
    parts = providers._gemini_body(GEMINI, '', msgs, False, None)['contents'][1]['parts']
    assert parts[0]['thoughtSignature'] == 'TS1' and 'thoughtSignature' not in parts[1]


def test_gemini_budget_zero_refused_is_remembered_apart(fake):
    p = {**GEMINI, 'model': 'gemini-2.5-pro'}
    ok = sse({'candidates': [{'content': {'parts': [{'text': 'ok'}]}}]})
    f = fake((400, {'error': {'code': 400, 'message': 'Budget 0 is invalid. This model only works in thinking mode.'}}),
             (200, ok), (200, ok), (200, ok))
    run(p)                                              # off: budget 0, refused, dropped
    assert f.bodies[0]['generationConfig']['thinkingConfig'] == {'thinkingBudget': 0}
    assert 'thinkingConfig' not in f.bodies[1]['generationConfig']
    run(p, think='off')                                 # remembered
    assert 'thinkingConfig' not in f.bodies[2]['generationConfig']
    run(p, think='high')                                # thinking on still gets a budget
    assert f.bodies[3]['generationConfig']['thinkingConfig']['thinkingBudget'] == 24576


def test_gemini_search_with_functions_refused(fake):
    f = fake((400, {'error': {'code': 400, 'message': 'Tool use with function calling is unsupported'}}),
             (200, sse({'candidates': [{'content': {'parts': [{'text': 'ok'}]}}]})))
    evs = run(GEMINI, native_web=True)
    assert of(evs, 'search')[0]['unavailable'] and of(evs, 'text') == ['ok']
    assert {'google_search': {}} not in f.bodies[1]['tools']
    assert not providers.native_web_ok(GEMINI) and providers.native_web_ok({**GEMINI, 'model': 'gemini-3-pro'})
