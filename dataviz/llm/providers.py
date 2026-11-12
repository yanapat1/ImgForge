"""
LLM provider clients over raw httpx (no SDKs): OpenAI, OpenAI-compatible servers (vLLM, Ollama, LM Studio,
OpenRouter ...), Anthropic and Gemini behind one message format.

    msgs = [{'role': 'user' | 'assistant' | 'tool', 'parts': [{'text': str} | {'jpeg': bytes, 'cache'?: True}
             | {'wav': bytes} | {'tool_call': {id, name, args, sig?}} | {'tool_result': {id, name, content, is_error?}}
             | {'thinking': {text, sig, kind, redacted?}}]}]

Tool calls are assistant parts, tool results go in a 'tool' message (sent as the user's turn to Anthropic / Gemini,
as role 'tool' messages to OpenAI). `chat_stream()` yields ('delta', text) ... then ('usage', {'in', 'out'});
`complete()` / `complete_json()` answer at once; `complete_tools()` is one native tool-calling step and
`stream_tools()` the same step streamed for the agent engine (thinking, live text, native web search);
`list_models()` fills the Settings model dropdown; `test()` is Test connection. Errors are LLMError with a
human message (provider status + its own message, trimmed) and an HTTP status for the route: 400 for a setup problem
(bad key, unknown model), 502 when the provider failed. The API key never appears in a message or a log line.
"""

import base64
import io
import json
import math
import re
import struct
import threading
import time

import httpx

from ..core import logging as vlog
from .prompting import plain, split
from .store import resolve_key

_log = vlog.get_logger('llm')
ANTHROPIC_VERSION = '2023-06-01'


class LLMError(ValueError):
    """A provider problem with a human message. status: the HTTP code a route answers with."""

    def __init__(self, msg, status=400, transient=False, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.transient = transient          # worth retrying: unreachable, timeout, 408 / 425 / 429 / 5xx upstream
        self.retry_after = retry_after      # seconds the provider asked us to wait (Retry-After), when it said


# ---------------------------------------------------------------- clients
_clients = {}
_clients_lock = threading.Lock()


def _client(p):
    """The shared keep-alive client of a provider (a changed url / key / timeout gets a new one)."""
    base = str(p.get('base_url') or '').rstrip('/')
    if not base.startswith(('http://', 'https://')):
        raise LLMError('the provider has no http(s) base URL')
    t = float(p.get('timeout_s') or 120)
    k = (base, resolve_key(p), t)
    c = _clients.get(k)
    if c is None:
        with _clients_lock:
            c = _clients.get(k)
            if c is None:
                c = _clients[k] = httpx.Client(base_url=base, follow_redirects=False,   # a redirect could point it at an internal host
                                               
                                               timeout=httpx.Timeout(t, connect=min(10.0, t)),
                                               limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
    return c


def close_clients():
    with _clients_lock:
        for c in _clients.values():
            c.close()
        _clients.clear()


def _headers(p):
    key = resolve_key(p)
    kind = p.get('kind')
    if kind == 'anthropic':
        if not key:
            raise LLMError('Claude needs an API key')
        return {'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION}
    if kind == 'gemini':
        if not key:
            raise LLMError('Gemini needs an API key')
        return {'x-goog-api-key': key}
    if kind == 'openai' and not key:
        raise LLMError('OpenAI needs an API key')
    return {'Authorization': f'Bearer {key}'} if key else {}       # openai_compat: the key is optional


def _scrub(p, text):
    key = resolve_key(p)
    text = str(text)
    return text.replace(key, '•••') if key and len(key) > 3 else text


def _err_message(r):
    """The provider's own error text from a failed response (all three use {error: {message}})."""
    try:
        body = r.json()
    except ValueError:
        body = None
    msg = None
    if isinstance(body, dict):
        e = body.get('error', body.get('detail', body.get('message')))
        if isinstance(e, dict):
            msg = e.get('message') or e.get('type')
        elif isinstance(e, str):
            msg = e
        elif isinstance(body, dict) and isinstance(body.get('message'), str):
            msg = body['message']
    elif isinstance(body, list) and body and isinstance(body[0], dict) and isinstance(body[0].get('error'), dict):
        msg = body[0]['error'].get('message')              # gemini sometimes answers a list
    if not msg:
        return ''                    # only a provider's structured error text: never echo a raw body (internal pages)
    msg = re.sub(r'\s+', ' ', str(msg))
    return msg[:200] + ('…' if len(msg) > 200 else '')


def _fail(p, r, what):
    msg = _scrub(p, f'{what}: the provider answered {r.status_code}' + (f': {m}' if (m := _err_message(r)) else ''))
    code = r.status_code
    try:
        after = float((getattr(r, 'headers', None) or {}).get('retry-after'))
    except (TypeError, ValueError):
        after = None
    return LLMError(msg, 400 if code in (400, 401, 403, 404, 422) else 502,
                    transient=code in (408, 409, 425, 429) or code >= 500, retry_after=after)


def _unreachable(p, e):
    return LLMError(_scrub(p, f'cannot reach {str(p.get("base_url"))}: {type(e).__name__}: {e}'), 502, transient=True)


# ---------------------------------------------------------------- request bodies
def _b64(b):
    return base64.b64encode(b).decode('ascii')


def _keep(x):
    return (bool(x.get('text') or x.get('jpeg') or x.get('wav')) or 'tool_call' in x or 'tool_result' in x
            or 'thinking' in x)


def _own_thinking(x, kind):
    """A history thinking part this provider can take back: signed, and written by this kind of provider (a Claude
    signature means nothing to Gemini and the other way round). Untagged parts count as Claude's."""
    t = x.get('thinking') if isinstance(x, dict) else None
    return isinstance(t, dict) and bool(t.get('sig')) and (t.get('kind') or 'anthropic') == kind


def _merge(msgs):
    """Consecutive messages of one role merged (Anthropic / Gemini want alternating turns); empty ones dropped.
    A 'tool' message (tool results) counts as the user's turn."""
    out = []
    for m in msgs:
        parts = [x for x in m.get('parts') or [] if _keep(x)]
        if not parts:
            continue
        role = 'assistant' if m.get('role') == 'assistant' else 'user'
        if out and out[-1]['role'] == role:
            out[-1]['parts'] = out[-1]['parts'] + parts
        else:
            out.append({'role': role, 'parts': list(parts)})
    return out


def _max_tokens(p, max_tokens):
    return int(max_tokens or p.get('max_output_tokens') or 2048)


def _tool_args_json(args):
    return json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)


def _openai_body(p, system, msgs, stream, json_mode, max_tokens, temperature=True, tools=None):
    out = [{'role': 'system', 'content': system}] if system else []
    for m in _merge(msgs):
        calls = [x['tool_call'] for x in m['parts'] if 'tool_call' in x]
        results = [x['tool_result'] for x in m['parts'] if 'tool_result' in x]
        # thinking is never sent back to OpenAI-style servers (they return no signature to verify it with)
        rest = [x for x in m['parts'] if 'tool_call' not in x and 'tool_result' not in x and 'thinking' not in x]
        # tool results are role 'tool' messages, right after the assistant message that called them
        for t in results:
            out.append({'role': 'tool', 'tool_call_id': str(t['id']), 'content': str(t.get('content') or '')})
        if calls:
            out.append({'role': 'assistant', 'content': '\n\n'.join(x['text'] for x in rest if 'text' in x) or None,
                        'tool_calls': [{'id': str(c['id']), 'type': 'function',
                                        'function': {'name': c['name'], 'arguments': _tool_args_json(c.get('args'))}}
                                       for c in calls]})
            continue
        if not rest:
            continue
        m = {'role': m['role'], 'parts': rest}
        if all('text' in x for x in m['parts']):
            out.append({'role': m['role'], 'content': '\n\n'.join(x['text'] for x in m['parts'])})
            continue
        content = []
        for x in m['parts']:
            if 'text' in x:
                content.append({'type': 'text', 'text': x['text']})
            elif 'jpeg' in x:
                content.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + _b64(x['jpeg'])}})
            elif 'wav' in x:
                content.append({'type': 'input_audio', 'input_audio': {'data': _b64(x['wav']), 'format': 'wav'}})
        out.append({'role': m['role'], 'content': content})
    body = {'model': p['model'], 'messages': out, 'stream': bool(stream)}
    # current OpenAI models only take max_completion_tokens; most compatible servers only know max_tokens
    body['max_completion_tokens' if p['kind'] == 'openai' else 'max_tokens'] = _max_tokens(p, max_tokens)
    if temperature and p.get('temperature') is not None:
        body['temperature'] = float(p['temperature'])
    if stream and p['kind'] == 'openai':
        body['stream_options'] = {'include_usage': True}   # not sent to compatible servers: some reject it
    if json_mode and p['kind'] == 'openai':
        body['response_format'] = {'type': 'json_object'}
    if tools:
        body['tools'] = [{'type': 'function', 'function': {'name': t['name'], 'description': t.get('description', ''),
                                                           'parameters': t.get('parameters') or {'type': 'object'}}}
                         for t in tools]
    return body


def _anthropic_body(p, system, msgs, stream, max_tokens, tools=None):
    merged = _merge(msgs)
    # one cache breakpoint on the last context image flagged cache (the exemplar sheet): repeat questions reuse it
    last_cache = None
    for i, m in enumerate(merged):
        for j, x in enumerate(m['parts']):
            if x.get('cache') and 'jpeg' in x:
                last_cache = (i, j)
    out = []
    for i, m in enumerate(merged):
        # signed thinking opens the assistant turn it belongs to (Claude checks it before the tool_use it led to)
        content = [{'type': 'redacted_thinking', 'data': t['redacted']} if t.get('redacted') else
                   {'type': 'thinking', 'thinking': t.get('text') or '', 'signature': t['sig']}
                   for t in (x['thinking'] for x in m['parts'] if m['role'] == 'assistant' and _own_thinking(x, 'anthropic'))]
        # tool_result blocks must open the user turn that answers a tool_use
        content += [{'type': 'tool_result', 'tool_use_id': str(t['id']), 'content': str(t.get('content') or ''),
                     **({'is_error': True} if t.get('is_error') else {})}
                    for t in (x['tool_result'] for x in m['parts'] if 'tool_result' in x)]
        for j, x in enumerate(m['parts']):
            if 'tool_result' in x or 'thinking' in x:
                continue
            if 'tool_call' in x:
                c = x['tool_call']
                content.append({'type': 'tool_use', 'id': str(c['id']), 'name': c['name'],
                                'input': c.get('args') if isinstance(c.get('args'), dict) else {}})
            elif 'text' in x:
                content.append({'type': 'text', 'text': x['text']})
            elif 'jpeg' in x:
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                                            'data': _b64(x['jpeg'])}})
                if (i, j) == last_cache:
                    content[-1]['cache_control'] = {'type': 'ephemeral'}
            elif 'wav' in x:
                raise LLMError('Claude has no audio input: turn voice off for this provider')
        out.append({'role': m['role'], 'content': content})
    # the conversation so far is read from the cache on the next step of the loop: a breakpoint on its last block
    # (thinking blocks take none). With the stable system part and the exemplar sheet: 3 of the 4 Claude allows.
    for blk in reversed(out[-1]['content'] if out else []):
        if blk.get('type') in ('text', 'image', 'tool_result', 'tool_use'):
            blk['cache_control'] = {'type': 'ephemeral'}
            break
    body = {'model': p['model'], 'max_tokens': _max_tokens(p, max_tokens), 'messages': out}
    if system:
        stable, dynamic = split(system)
        # the stable part (rules, knowledge, tools) is cached across steps and messages; what changes per message
        # (mode, the clock, where the user is) comes after the breakpoint so it never invalidates it
        body['system'] = [{'type': 'text', 'text': stable, 'cache_control': {'type': 'ephemeral'}}] + \
            ([{'type': 'text', 'text': dynamic}] if dynamic.strip() else [])
    if p.get('temperature') is not None:
        body['temperature'] = min(1.0, float(p['temperature']))       # anthropic: 0..1
    if stream:
        body['stream'] = True
    if tools:
        body['tools'] = [{'name': t['name'], 'description': t.get('description', ''),
                          'input_schema': t.get('parameters') or {'type': 'object'}} for t in tools]
    return body


# the OpenAPI subset Gemini's functionDeclarations accept: anything else (additionalProperties, default, $schema ...)
# is a 400, so it is stripped
_GEMINI_SCHEMA = {'type', 'format', 'description', 'nullable', 'enum', 'properties', 'required', 'items',
                  'minItems', 'maxItems', 'minimum', 'maximum', 'anyOf', 'title', 'propertyOrdering'}


def gemini_schema(s):
    """A JSON schema cut down to what Gemini accepts (recursively; `required` keeps only declared properties)."""
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k not in _GEMINI_SCHEMA:
            continue
        if k == 'properties' and isinstance(v, dict):
            out[k] = {name: gemini_schema(sub) for name, sub in v.items()}
        elif k == 'items':
            out[k] = gemini_schema(v)
        elif k == 'anyOf' and isinstance(v, list):
            out[k] = [gemini_schema(x) for x in v]
        elif k == 'type' and isinstance(v, list):         # ["string", "null"] -> string + nullable
            types = [t for t in v if t != 'null']
            out['type'] = types[0] if types else 'string'
            if 'null' in v:
                out['nullable'] = True
        else:
            out[k] = v
    if 'required' in out:
        out['required'] = [r for r in out['required'] if r in (out.get('properties') or {})]
        if not out['required']:
            del out['required']
    return out


def _gemini_body(p, system, msgs, json_mode, max_tokens, tools=None):
    contents = []
    for m in _merge(msgs):
        parts = []
        for x in m['parts']:
            if 'tool_call' in x:
                c = x['tool_call']
                part = {'functionCall': {'name': c['name'], 'args': c.get('args') if isinstance(c.get('args'), dict) else {}}}
                if c.get('sig'):
                    part['thoughtSignature'] = c['sig']      # thinking models refuse a call history without it
                parts.append(part)
            elif 'thinking' in x:
                if _own_thinking(x, 'gemini'):          # a signed thought goes back as it came
                    t = x['thinking']
                    parts.append({'text': t.get('text') or '', 'thought': True, 'thoughtSignature': t['sig']})
            elif 'tool_result' in x:
                t = x['tool_result']
                parts.append({'functionResponse': {'name': t['name'], 'response': {
                    ('error' if t.get('is_error') else 'content'): str(t.get('content') or '')}}})
            elif 'text' in x:
                parts.append({'text': x['text']})
            elif 'jpeg' in x:
                parts.append({'inline_data': {'mime_type': 'image/jpeg', 'data': _b64(x['jpeg'])}})
            elif 'wav' in x:
                parts.append({'inline_data': {'mime_type': 'audio/wav', 'data': _b64(x['wav'])}})
        contents.append({'role': 'model' if m['role'] == 'assistant' else 'user', 'parts': parts})
    cfg = {'maxOutputTokens': _max_tokens(p, max_tokens)}
    if p.get('temperature') is not None:
        cfg['temperature'] = float(p['temperature'])
    if json_mode:
        cfg['responseMimeType'] = 'application/json'
    body = {'contents': contents, 'generationConfig': cfg}
    if system:
        body['systemInstruction'] = {'parts': [{'text': system}]}
    if tools:
        body['tools'] = [{'functionDeclarations': [
            {'name': t['name'], 'description': t.get('description', ''),
             **({'parameters': gemini_schema(t['parameters'])} if (t.get('parameters') or {}).get('properties') else {})}
            for t in tools]}]
    return body


def _gemini_model(p):
    m = str(p['model'])
    return m[len('models/'):] if m.startswith('models/') else m


def _request(p, system, msgs, stream, json_mode=False, max_tokens=None, tools=None):
    """(path, body, headers) of a generate request for this provider. tools: [{name, description, parameters}]."""
    kind = p.get('kind')
    h = _headers(p)
    if kind != 'anthropic' and system:
        system = plain(system)          # these cache an identical prefix on their own: one text, stable part first
    if kind in ('openai', 'openai_compat'):
        return '/chat/completions', _openai_body(p, system, msgs, stream, json_mode, max_tokens, tools=tools), h
    if kind == 'anthropic':
        return '/v1/messages', _anthropic_body(p, system, msgs, stream, max_tokens, tools=tools), h
    if kind == 'gemini':
        path = f'/models/{_gemini_model(p)}:' + ('streamGenerateContent?alt=sse' if stream else 'generateContent')
        return path, _gemini_body(p, system, msgs, json_mode, max_tokens, tools=tools), h
    raise LLMError(f'unknown provider kind {kind!r}')


# ---------------------------------------------------------------- answers
def _usage_openai(u):
    return {'in': int(u.get('prompt_tokens') or 0), 'out': int(u.get('completion_tokens') or 0)} if u else None


def _usage_anthropic(u):
    if not u:
        return None
    return {'in': int(u.get('input_tokens') or 0) + int(u.get('cache_read_input_tokens') or 0)
            + int(u.get('cache_creation_input_tokens') or 0), 'out': int(u.get('output_tokens') or 0)}


def _usage_gemini(u):
    return {'in': int(u.get('promptTokenCount') or 0), 'out': int(u.get('candidatesTokenCount') or 0)} if u else None


def _gemini_text(body):
    cands = body.get('candidates') or []
    if not cands:
        why = (body.get('promptFeedback') or {}).get('blockReason')
        raise LLMError(f'Gemini returned no answer{f" (blocked: {why})" if why else ""}', 502)
    parts = (cands[0].get('content') or {}).get('parts') or []
    return ''.join(x.get('text', '') for x in parts if not x.get('thought'))


def _parse_answer(kind, body):
    if not isinstance(body, dict):
        raise LLMError('the provider answered something that is not JSON', 502)
    if kind in ('openai', 'openai_compat'):
        ch = body.get('choices') or []
        if not ch:
            raise LLMError('the provider returned no choices', 502)
        c = (ch[0].get('message') or {}).get('content')
        if isinstance(c, list):
            c = ''.join(x.get('text', '') for x in c if isinstance(x, dict))
        return c or '', _usage_openai(body.get('usage'))
    if kind == 'anthropic':
        text = ''.join(x.get('text', '') for x in body.get('content') or [] if x.get('type') == 'text')
        return text, _usage_anthropic(body.get('usage'))
    return _gemini_text(body), _usage_gemini(body.get('usageMetadata'))


def _post(p, path, body, h, what):
    try:
        r = _client(p).post(path, json=body, headers=h)
    except httpx.HTTPError as e:
        raise _unreachable(p, e) from None
    return r


# ---------------------------------------------------------------- parameters a server refuses
# Optional parameters we send (sampling, usage in streams, thinking, native web search) that some models or servers
# answer with a 400. The refused one is dropped and the request sent again; what was refused is remembered per
# (kind, base URL, model) for the life of the process, so the next request leaves it out from the start.
#   name: (paths in the body, words of the provider's error text that name it)
_PARAMS = {
    'temperature': ((('temperature',), ('generationConfig', 'temperature')), ('temperature',)),
    'stream_options': ((('stream_options',),), ('stream_options', 'include_usage')),
    'reasoning_effort': ((('reasoning_effort',),), ('reasoning_effort', 'reasoning effort', 'reasoning.effort')),
    'chat_template_kwargs': ((('chat_template_kwargs',),), ('chat_template_kwargs', 'enable_thinking')),
    'thinking': ((('thinking',),), ('thinking', 'budget_tokens', 'adaptive')),
    'effort': ((('output_config',),), ('effort', 'output_config')),
    'thinkingConfig': ((('generationConfig', 'thinkingConfig'),),
                       ('thinkingconfig', 'thinking_config', 'thinking_budget', 'thinkingbudget', 'includethoughts',
                        'include_thoughts', 'thinking budget', 'budget', 'thinking')),
}
# a refused native search names itself; Gemini says 'Tool use with function calling is unsupported' (not every
# model mixes google_search with function declarations), Claude's tool errors are about our own tools
_NATIVE_WORDS = {'gemini': ('search', 'grounding', 'tool', 'function')}
_refused = {}                   # (kind, base_url, model) -> {param name}; 'thinkingConfig:off' = Gemini budget 0
_native_off = set()             # (kind, base_url, model) whose native web search failed: the engine's own is used
_refused_lock = threading.Lock()


def _mkey(p):
    return str(p.get('kind') or ''), str(p.get('base_url') or '').rstrip('/'), str(p.get('model') or '')


def _remember(p, names):
    with _refused_lock:
        _refused.setdefault(_mkey(p), set()).update(names)


def _get(body, path):
    for k in path[:-1]:
        body = body.get(k) if isinstance(body, dict) else None
    return body.get(path[-1]) if isinstance(body, dict) else None


def _param_name(name, body):
    """The remembered name: Gemini's budget 0 (thinking off) is refused by models that always think, which still
    take a budget when thinking is on, so the two are remembered apart."""
    if name == 'thinkingConfig' and (_get(body, ('generationConfig', 'thinkingConfig')) or {}).get('thinkingBudget') == 0:
        return 'thinkingConfig:off'
    return name


def _present(body):
    return [n for n, (paths, _) in _PARAMS.items() if any(_get(body, x) is not None for x in paths)]


def _drop(body, name):
    for path in _PARAMS[name][0]:
        d = body
        for k in path[:-1]:
            d = d.get(k) if isinstance(d, dict) else None
        if isinstance(d, dict):
            d.pop(path[-1], None)


def _drop_refused(p, body):
    """Leaves out what this model refused before."""
    gone = _refused.get(_mkey(p)) or ()
    for n in _present(body):
        if _param_name(n, body) in gone:
            _drop(body, n)


def _is_native(t):
    return isinstance(t, dict) and (str(t.get('type') or '').startswith('web_search_') or 'google_search' in t)


def _native_in(body):
    return 'web_search_options' in body or any(_is_native(t) for t in body.get('tools') or [])


def _drop_native(body):
    body.pop('web_search_options', None)
    if 'tools' in body:
        body['tools'] = [t for t in body['tools'] if not _is_native(t)]
        if not body['tools']:
            del body['tools']


def _retry_without(p, r, body, guessed=None):
    """After a 400 / 422: drops what the provider's error names (the parameter named first) and remembers it.
    Returns 'native' (native web search dropped), 'param', 'guess' (a compatible server said nothing useful:
    every optional parameter dropped, collected in `guessed` and remembered only once the retry works) or None
    (nothing left to drop: the error is real)."""
    if r.status_code not in (400, 422):
        return None
    msg = _err_message(r).lower()
    if _native_in(body) and (not msg or any(w in msg for w in _NATIVE_WORDS.get(p.get('kind'), ('search',)))):
        _drop_native(body)
        with _refused_lock:
            _native_off.add(_mkey(p))
        return 'native'
    present = _present(body)
    hits = sorted((min(i for w in _PARAMS[n][1] if (i := msg.find(w)) >= 0), k, n)
                  for k, n in enumerate(present) if any(w in msg for w in _PARAMS[n][1]))
    if hits:
        n = hits[0][2]
        _remember(p, {_param_name(n, body)})
        _drop(body, n)
        return 'param'
    if p.get('kind') == 'openai_compat' and present and guessed is not None and not guessed:
        guessed.update(_param_name(n, body) for n in present)
        for n in present:
            _drop(body, n)
        return 'guess'
    return None


def native_web_ok(p):
    """Whether native web search may be asked of this provider: Claude's web_search tool, Gemini's google_search,
    OpenAI's *search* models. False for compatible servers, and once it failed for this model."""
    kind = p.get('kind')
    if _mkey(p) in _native_off:
        return False
    if kind in ('anthropic', 'gemini'):
        return True
    return kind == 'openai' and 'search' in str(p.get('model') or '').lower()


def _send(p, path, body, h):
    """A non-streamed request, sent again without each parameter the provider refuses."""
    _drop_refused(p, body)
    guessed = set()
    for _ in range(len(_PARAMS) + 2):
        r = _post(p, path, body, h, 'generate')
        if r.status_code < 400 or not _retry_without(p, r, body, guessed):
            break
    if guessed and r.status_code < 400:
        _remember(p, guessed)
    return r


def complete(p, system, msgs, json=False, max_tokens=None, quiet=False):
    """(text, usage {'in', 'out'} or None) of one non-streamed answer. quiet: a short helper answer (a summary, a
    title): thinking off where it can be, else the least of it with room above max_tokens (_quiet_thinking)."""
    path, body, h = _request(p, system, msgs, False, json, max_tokens)
    if quiet:
        _quiet_thinking(p, body)
    r = _send(p, path, body, h)
    if r.status_code >= 400:
        raise _fail(p, r, f'{p.get("model")}')
    try:
        data = r.json()
    except ValueError:
        raise LLMError('the provider answered something that is not JSON', 502) from None
    text, usage = _parse_answer(p['kind'], data)
    if p['kind'] == 'openai_compat':
        text = _strip_think(text)
    return text, usage or {'in': 0, 'out': 0}


def _strip_think(text):
    """A compatible server without a reasoning parser puts the thinking in the text as <think>…</think>: only
    what follows it is the answer ('' when the answer was cut off inside the thinking)."""
    t = str(text or '')
    if not t.lstrip().startswith('<think>'):
        return t
    i = t.find('</think>')
    return t[i + len('</think>'):].lstrip() if i >= 0 else ''


def _quiet_thinking(p, body):
    """Thinking settings of a short non-streamed helper answer. A model that thinks by default (every Claude 5.x,
    Gemini 2.5, OpenAI reasoning models, Qwen3 / DeepSeek-R1 templates) otherwise spends a small max_tokens on
    thinking and returns no text. Refused settings are dropped and remembered by _send, as for streams."""
    kind = p.get('kind')
    if kind == 'anthropic':
        _, always = _claude_thinking(p.get('model'))
        if always:                                      # cannot be off: the least of it, room above the answer
            body['output_config'] = {'effort': 'low'}
            body['max_tokens'] = max(body['max_tokens'] + _ROOM, _ADAPTIVE_MAX['low'])
            body.pop('temperature', None)
    elif kind == 'gemini':
        cfg = body['generationConfig']
        cfg['thinkingConfig'] = {'thinkingBudget': 0}   # models that always think refuse it: dropped, remembered
        cfg['maxOutputTokens'] += _ROOM
    elif kind == 'openai':
        if _openai_reasoning(p.get('model')):
            body['reasoning_effort'] = 'low'
            body.pop('temperature', None)
            body['max_completion_tokens'] += _ROOM
    else:
        body['chat_template_kwargs'] = {'enable_thinking': False}


def _parse_tool_calls(kind, body):
    """[{id, name, args, sig?}] of an answer. args is None when the model sent arguments that are not a JSON
    object (the agent reports that back to it instead of guessing). id is '' when the provider sent none (Gemini
    usually, some compatible servers): the caller gives it one that is unique over the whole conversation."""
    calls = []
    if kind in ('openai', 'openai_compat'):
        msg = ((body.get('choices') or [{}])[0].get('message') or {})
        for c in msg.get('tool_calls') or []:
            fn = c.get('function') or {}
            raw = fn.get('arguments')
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw or '{}')
            except ValueError:
                args = None
            calls.append({'id': str(c.get('id') or ''), 'name': str(fn.get('name') or ''),
                          'args': args if isinstance(args, dict) else None})
    elif kind == 'anthropic':
        for x in body.get('content') or []:
            if x.get('type') == 'tool_use':
                inp = x.get('input')
                calls.append({'id': str(x.get('id') or ''), 'name': str(x.get('name') or ''),
                              'args': inp if isinstance(inp, dict) else None})
    else:
        parts = (((body.get('candidates') or [{}])[0].get('content') or {}).get('parts')) or []
        for x in parts:
            fc = x.get('functionCall')
            if not fc:
                continue
            args = fc.get('args')
            c = {'id': str(fc.get('id') or ''), 'name': str(fc.get('name') or ''),
                 'args': args if isinstance(args, dict) else ({} if args is None else None)}
            if x.get('thoughtSignature'):
                c['sig'] = x['thoughtSignature']
            calls.append(c)
    return calls


def complete_tools(p, system, msgs, tools, max_tokens=None):
    """One non-streamed step with native tool calling -> {text, tool_calls: [{id, name, args, sig?}], usage}.
    tools: [{name, description, parameters (JSON schema)}]."""
    path, body, h = _request(p, system, msgs, False, False, max_tokens, tools=tools)
    r = _send(p, path, body, h)
    if r.status_code >= 400:
        raise _fail(p, r, f'{p.get("model")}')
    try:
        data = r.json()
    except ValueError:
        raise LLMError('the provider answered something that is not JSON', 502) from None
    kind = p['kind']
    text, usage = _parse_answer(kind, data)
    calls = _parse_tool_calls(kind, data)
    return {'text': text or '', 'tool_calls': calls, 'usage': usage or {'in': 0, 'out': 0}}


# ---------------------------------------------------------------- streamed tool calling (the agent engine)
THINK_LEVELS = ('off', 'low', 'medium', 'high')
_BUDGET = {'low': 2048, 'medium': 8192, 'high': 24576}           # thinking tokens (Claude <= 4.5, Gemini)
_ROOM = 4096                                                      # max_tokens left for the answer above a budget
_ADAPTIVE_MAX = {'low': 8192, 'medium': 16384, 'high': 32000}     # max_tokens with adaptive thinking (it counts)
_MAX_PAUSES = 3                                                   # Claude's pause_turn (long server-tool turns)


def _claude_version(model):
    """(family, (major, minor)) of a Claude model id: claude-opus-4-6, claude-sonnet-4-5-20250929,
    claude-3-7-sonnet-20250219, anthropic.claude-haiku-4-5 ... (None, None) when it is not one."""
    s = str(model or '').lower()
    m = re.search(r'(opus|sonnet|haiku|fable|mythos)-(\d{1,2})(?!\d)(?:[-.](\d{1,2})(?!\d))?', s)
    if m:
        return m.group(1), (int(m.group(2)), int(m.group(3) or 0))
    m = re.search(r'claude-(\d+)(?:[-.](\d)(?!\d))?-(opus|sonnet|haiku)', s)
    if m:
        return m.group(3), (int(m.group(1)), int(m.group(2) or 0))
    return None, None


def _claude_thinking(model):
    """How a Claude model thinks: ('adaptive', always_on) for Opus / Sonnet 4.6+, Haiku 5+, Fable and Mythos
    (budget_tokens is a 400 on most of them; effort sets the depth), ('budget', False) for older ones. always_on:
    thinking cannot be turned off (Opus 5.5, Fable ...) or runs when `thinking` is left out (every 5.x), so 'off'
    asks for the least of it (effort low) instead of sending a refused {type: disabled}."""
    fam, v = _claude_version(model)
    if fam in ('fable', 'mythos'):
        return 'adaptive', True
    if (fam in ('opus', 'sonnet') and v >= (4, 6)) or (fam == 'haiku' and v >= (5, 0)):
        return 'adaptive', v >= (5, 0)
    return 'budget', False


def _claude_web_tool(model):
    """The newest web search tool the model takes (dynamic filtering needs Opus / Sonnet 4.6+, Fable)."""
    fam, v = _claude_version(model)
    new = fam in ('fable', 'mythos') or (fam in ('opus', 'sonnet') and v >= (4, 6))
    return {'type': 'web_search_20260209' if new else 'web_search_20250305', 'name': 'web_search', 'max_uses': 5}


def _openai_reasoning(model):
    """OpenAI's reasoning models (o1 / o3 / o4-mini ..., gpt-5*): the only ones that take reasoning_effort."""
    return bool(re.match(r'(?:openai/)?(?:o\d|gpt-5)', str(model or '').lower()))


def _stream_extras(p, body, h, level, web, tools):
    """Thinking and native web search on a streamed request. level: None | off | low | medium | high."""
    kind = p['kind']
    on = level in _BUDGET
    if kind == 'anthropic':
        mode, always = _claude_thinking(p.get('model'))
        if on and mode == 'adaptive':
            body['thinking'] = {'type': 'adaptive', 'display': 'summarized'}   # summarized: else the text is empty
            body['output_config'] = {'effort': level}
            body['max_tokens'] = max(body['max_tokens'], _ADAPTIVE_MAX[level])
        elif on:
            body['thinking'] = {'type': 'enabled', 'budget_tokens': _BUDGET[level]}
            body['max_tokens'] = max(body['max_tokens'], _BUDGET[level] + _ROOM)    # must exceed the budget
            if tools:                                   # thinking between tool calls, not only before the first
                h['anthropic-beta'] = 'interleaved-thinking-2025-05-14'
        elif always:
            body['output_config'] = {'effort': 'low'}
            body['max_tokens'] = max(body['max_tokens'], _ADAPTIVE_MAX['low'])
        if on or always:
            body.pop('temperature', None)               # thinking takes no sampling settings (a 400)
        if web:
            body.setdefault('tools', []).append(_claude_web_tool(p.get('model')))
    elif kind == 'gemini':
        cfg = body['generationConfig']
        if on:
            cfg['thinkingConfig'] = {'thinkingBudget': _BUDGET[level], 'includeThoughts': True}
            cfg['maxOutputTokens'] = max(cfg['maxOutputTokens'], _BUDGET[level] + _ROOM)
        else:
            cfg['thinkingConfig'] = {'thinkingBudget': 0}      # models that always think refuse it: remembered
        if web:
            body.setdefault('tools', []).append({'google_search': {}})
    elif kind == 'openai':
        if on and _openai_reasoning(p.get('model')):
            body['reasoning_effort'] = level
            body.pop('temperature', None)               # reasoning models take only the default
        if web:
            body['web_search_options'] = {}
    else:
        body['stream_options'] = {'include_usage': True}       # dropped (and remembered) where refused
        if level is not None:                           # Qwen3 / QwQ / DeepSeek-R1 chat templates
            body['chat_template_kwargs'] = {'enable_thinking': on}


def _stream_error(p, e):
    """An error event inside a 200 stream (overloaded mid-answer ...)."""
    if isinstance(e, dict):
        kind_, msg = str(e.get('type') or e.get('status') or e.get('code') or ''), e.get('message') or e.get('type')
    else:
        kind_, msg = '', e
    msg = re.sub(r'\s+', ' ', str(msg or 'error'))[:200]
    low = f'{kind_} {msg}'.lower()
    transient = any(w in low for w in ('overload', 'rate', 'api_error', 'server', 'timeout', 'unavailable',
                                       'internal', 'exhausted', '429', '500', '503', '529'))
    return LLMError(_scrub(p, f'{p.get("model")}: provider error: {msg}'), 502, transient=transient)


def _broken(p, e, started):
    if not started:
        return _unreachable(p, e)
    return LLMError(_scrub(p, f'{p.get("model")}: the answer stream broke off ({type(e).__name__}: {e})'), 502,
                    transient=True)


def _json_args(raw):
    """Tool arguments as a dict; None when they are not a JSON object (the engine tells the model)."""
    if isinstance(raw, dict):
        return raw
    if raw is None or not str(raw).strip():
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


class _ThinkTags:
    """`<think>…</think>` at the start of an answer is thinking (Qwen3 / DeepSeek-R1 on a server without a
    reasoning parser). Tags split over chunks are held back until they are whole."""
    OPEN, CLOSE = '<think>', '</think>'

    def __init__(self):
        self.state, self.buf = 'start', ''

    def feed(self, s):
        if self.state == 'text':
            yield 'text', s
            return
        self.buf += s
        if self.state == 'start':
            b = self.buf.lstrip()
            if not b or (len(b) < len(self.OPEN) and self.OPEN.startswith(b)):
                return
            if not b.startswith(self.OPEN):
                self.state, out, self.buf = 'text', self.buf, ''
                yield 'text', out
                return
            self.state, self.buf = 'think', b[len(self.OPEN):]
        i = self.buf.find(self.CLOSE)
        if i >= 0:
            if i:
                yield 'thinking', {'text': self.buf[:i]}
            rest = self.buf[i + len(self.CLOSE):].lstrip()
            self.state, self.buf = 'text', ''
            if rest:
                yield 'text', rest
            return
        keep = next((k for k in range(min(len(self.CLOSE) - 1, len(self.buf)), 0, -1)
                     if self.CLOSE.startswith(self.buf[-k:])), 0)
        if len(self.buf) > keep:
            yield 'thinking', {'text': self.buf[:len(self.buf) - keep]}
            self.buf = self.buf[len(self.buf) - keep:]

    def flush(self):
        if self.buf:
            yield ('thinking', {'text': self.buf}) if self.state == 'think' else ('text', self.buf)
        self.buf = ''


def _openai_events(p, r, st):
    """OpenAI / compatible chunks: content, reasoning_content / reasoning (vLLM, Ollama, DeepSeek, Qwen ...),
    tool_calls accumulated by index (arguments arrive in pieces), url_citation annotations of search models."""
    calls, cites = {}, {}
    tags = _ThinkTags() if p['kind'] == 'openai_compat' else None
    for ev in _sse_data(r):
        if ev.get('error'):
            raise _stream_error(p, ev['error'])
        if ev.get('usage'):
            st['usage'] = _usage_openai(ev['usage'])
        for ch in ev.get('choices') or []:
            if ch.get('finish_reason'):
                st['stop'] = ch['finish_reason']        # 'length': the output limit cut the answer
            d = ch.get('delta') or ch.get('message') or {}
            th = d.get('reasoning_content') or d.get('reasoning')
            if isinstance(th, str) and th:
                yield 'thinking', {'text': th}
            c = d.get('content')
            if isinstance(c, list):
                c = ''.join(x.get('text', '') for x in c if isinstance(x, dict))
            if c:
                if tags:
                    yield from tags.feed(c)
                else:
                    yield 'text', c
            for a in d.get('annotations') or []:
                u = a.get('url_citation') if isinstance(a, dict) else None
                if isinstance(u, dict) and u.get('url'):
                    cites.setdefault(u['url'], str(u.get('title') or u['url']))
            for tc in d.get('tool_calls') or []:
                slot, tid = tc.get('index'), tc.get('id')
                if not isinstance(slot, int):           # servers that send whole calls without an index
                    slot = next((k for k, v in calls.items() if tid and v['id'] == tid), None)
                    if slot is None:
                        slot = len(calls) if (tid or not calls) else max(calls)
                c = calls.setdefault(slot, {'id': '', 'name': '', 'args': ''})
                if tid:
                    c['id'] = str(tid)
                fn = tc.get('function') or {}
                if fn.get('name'):
                    c['name'] = str(fn['name'])
                a = fn.get('arguments')
                if isinstance(a, dict):
                    c['args'] = a
                elif isinstance(a, str) and isinstance(c['args'], str):
                    c['args'] += a
    if tags:
        yield from tags.flush()
    for k in sorted(calls):
        c = calls[k]
        yield 'tool_call', {'id': c['id'], 'name': c['name'], 'args': _json_args(c['args'])}
    if cites:
        yield 'search', {'query': '', 'engine': 'openai', 'results': [{'title': t, 'url': u} for u, t in cites.items()]}


def _anthropic_events(p, r, st):
    """Claude's content blocks: text, thinking (+ signature), redacted thinking, tool_use (input_json_delta),
    server_tool_use web_search + web_search_tool_result. st['content'] rebuilds the turn for a pause_turn.

    A thinking block streams as chunks; the first chunk's dict is the block: at content_block_stop it gets the
    whole text and the signature, so the engine keeps that one dict for the history (it has already shown each
    chunk) and Claude gets the block back exactly as written."""
    blocks, queries = {}, {}
    for ev in _sse_data(r):
        t = ev.get('type')
        if t == 'message_start':
            u = _usage_anthropic((ev.get('message') or {}).get('usage'))
            if u:
                st['usage'] = {'in': u['in'], 'out': u['out']}
        elif t == 'content_block_start':
            cb = dict(ev.get('content_block') or {})
            b = blocks[ev.get('index')] = {'type': cb.get('type'), 'raw': cb, 'text': '', 'sig': '', 'json': '',
                                           'head': None}
            if b['type'] == 'text' and cb.get('text'):
                b['text'] = cb['text']
                yield 'text', cb['text']
            elif b['type'] == 'thinking':
                b['text'], b['sig'] = cb.get('thinking') or '', cb.get('signature') or ''
            elif b['type'] == 'redacted_thinking':
                st['content'].append(cb)
                yield 'thinking', {'text': '', 'sig': str(cb.get('data') or ''), 'redacted': cb.get('data'),
                                   'kind': 'anthropic'}
            elif b['type'] == 'web_search_tool_result':
                st['content'].append(cb)
                res = cb.get('content')
                yield 'search', {'query': queries.get(cb.get('tool_use_id'), ''), 'engine': 'anthropic',
                                 'results': [{'title': str(x.get('title') or x.get('url')), 'url': str(x.get('url'))}
                                             for x in (res if isinstance(res, list) else [])
                                             if isinstance(x, dict) and x.get('url')]}
        elif t == 'content_block_delta':
            b, d = blocks.get(ev.get('index')), ev.get('delta') or {}
            if b is None:
                continue
            dt = d.get('type')
            if dt == 'text_delta' and d.get('text'):
                b['text'] += d['text']
                yield 'text', d['text']
            elif dt == 'thinking_delta' and d.get('thinking'):
                b['text'] += d['thinking']
                chunk = {'text': d['thinking']}
                if b['head'] is None:
                    b['head'] = chunk
                yield 'thinking', chunk
            elif dt == 'signature_delta':
                b['sig'] += d.get('signature') or ''
            elif dt == 'input_json_delta':
                b['json'] += d.get('partial_json') or ''
        elif t == 'content_block_stop':
            b = blocks.get(ev.get('index'))
            if b is None:
                continue
            bt, raw = b['type'], b['raw']
            if bt == 'text':
                st['content'].append({'type': 'text', 'text': b['text']})
            elif bt == 'thinking':
                st['content'].append({'type': 'thinking', 'thinking': b['text'], 'signature': b['sig']})
                head = b['head']
                if head is None:                        # no deltas (display omitted): only the signature
                    head = {'text': b['text']}
                head.update(text=b['text'], sig=b['sig'], kind='anthropic')
                if b['head'] is None:
                    yield 'thinking', head
            elif bt in ('tool_use', 'server_tool_use'):
                args = _json_args(b['json']) if b['json'] else (raw.get('input') if isinstance(raw.get('input'), dict) else {})
                st['content'].append({'type': bt, 'id': raw.get('id'), 'name': raw.get('name'),
                                      'input': args if isinstance(args, dict) else {}})
                if bt == 'tool_use':
                    yield 'tool_call', {'id': str(raw.get('id') or ''), 'name': str(raw.get('name') or ''),
                                        'args': args}
                else:
                    queries[raw.get('id')] = str((args or {}).get('query') or '')
        elif t == 'message_delta':
            st['stop'] = (ev.get('delta') or {}).get('stop_reason') or st.get('stop')
            u = ev.get('usage') or {}
            if u.get('output_tokens') is not None:
                st['usage']['out'] = int(u['output_tokens'] or 0)
            if u.get('input_tokens'):                   # newer streams repeat the (cumulative) input count
                st['usage']['in'] = max(st['usage']['in'], _usage_anthropic(u)['in'])
        elif t == 'error':
            raise _stream_error(p, ev.get('error') or {})


def _gemini_events(p, r, st):
    """Gemini chunks: thought parts (thinking, signature kept), text, functionCall (its thoughtSignature is the
    call's sig: thinking models refuse the next turn without it), groundingMetadata of google_search."""
    queries, results = [], {}
    for ev in _sse_data(r):
        if ev.get('error'):
            raise _stream_error(p, ev['error'])
        u = ev.get('usageMetadata')
        if u:                                           # cumulative: the last one counts; thoughts are billed out
            st['usage'] = {'in': int(u.get('promptTokenCount') or 0),
                           'out': int(u.get('candidatesTokenCount') or 0) + int(u.get('thoughtsTokenCount') or 0)}
        cands = ev.get('candidates') or []
        if not cands:
            why = (ev.get('promptFeedback') or {}).get('blockReason')
            if why:
                raise LLMError(f'Gemini returned no answer (blocked: {why})', 502)
            continue
        c = cands[0]
        if c.get('finishReason'):
            st['stop'] = c['finishReason']
        for x in (c.get('content') or {}).get('parts') or []:
            sig = x.get('thoughtSignature')
            fc = x.get('functionCall')
            if fc:
                a = fc.get('args')
                call = {'id': str(fc.get('id') or ''), 'name': str(fc.get('name') or ''),
                        'args': a if isinstance(a, dict) else ({} if a is None else None)}
                if sig:
                    call['sig'] = sig
                yield 'tool_call', call
            elif x.get('thought'):
                th = {'text': str(x.get('text') or ''), 'kind': 'gemini'}
                if sig:
                    th['sig'] = sig
                if th['text'] or sig:
                    yield 'thinking', th
            elif x.get('text'):
                yield 'text', x['text']
        gm = c.get('groundingMetadata') or {}
        for q in gm.get('webSearchQueries') or []:
            if q not in queries:
                queries.append(str(q))
        for g in gm.get('groundingChunks') or []:
            w = g.get('web') if isinstance(g, dict) else None
            if isinstance(w, dict) and w.get('uri'):
                results.setdefault(w['uri'], str(w.get('title') or w['uri']))
    if queries or results:
        yield 'search', {'query': '; '.join(queries), 'engine': 'google',
                         'results': [{'title': t, 'url': u} for u, t in results.items()]}


CUT_OFF = ('length', 'max_tokens', 'MAX_TOKENS')                  # stop reasons: the output limit ended the answer


def out_limit(p):
    """The provider's output limit for one answer (its setting, else the default)."""
    return _max_tokens(p, None)


def stream_tools(p, system, msgs, tools, think=None, native_web=False, max_tokens=None):
    """One tool-calling step streamed for the agent engine. Yields
        ('thinking', {text, sig?, kind?, redacted?})   reasoning chunks; a signed one goes back in the history
        ('text', str)                                  answer chunks
        ('tool_call', {id, name, args, sig?})          once per complete call (args None: not a JSON object)
        ('search', {query, engine, results: [{title, url}], unavailable?})   provider-native web search
        ('stop', str)                                  why the answer ended, when the provider said (before usage)
        ('usage', {in, out})                           last
    think: None | 'off' | 'low' | 'medium' | 'high'. native_web: ask for the provider's own web search where
    native_web_ok(p); when the provider refuses it, the step is sent again without it, that is remembered and one
    ('search', {..., 'unavailable': True}) tells the engine to use its own web_search tool. Refused optional
    parameters (temperature, stream usage, thinking settings) are dropped and remembered the same way."""
    kind = p.get('kind')
    level = think if think in THINK_LEVELS else None
    if kind != 'openai_compat' and level is None:
        level = 'off'
    parse = {'openai': _openai_events, 'openai_compat': _openai_events, 'anthropic': _anthropic_events,
             'gemini': _gemini_events}.get(kind)
    if parse is None:
        raise LLMError(f'unknown provider kind {kind!r}')
    path, body, h = _request(p, system, msgs, True, False, max_tokens, tools=tools)
    h = dict(h)
    _stream_extras(p, body, h, level, bool(native_web) and native_web_ok(p), tools)
    _drop_refused(p, body)
    total, told, guessed = {'in': 0, 'out': 0}, False, set()
    stop = None
    for _pause in range(_MAX_PAUSES + 1):
        st = {'usage': {'in': 0, 'out': 0}, 'stop': None, 'content': []}
        for _ in range(len(_PARAMS) + 2):
            started = False
            try:
                with _client(p).stream('POST', path, json=body, headers=h) as r:
                    if r.status_code >= 400:
                        r.read()
                        why = _retry_without(p, r, body, guessed)
                        if why is None:
                            raise _fail(p, r, f'{p.get("model")}')
                        if why == 'native' and not told:
                            told = True
                            yield 'search', {'query': '', 'engine': 'native', 'results': [], 'unavailable': True}
                        continue
                    for ev in parse(p, r, st):
                        started = True
                        yield ev
                break
            except httpx.HTTPError as e:
                raise _broken(p, e, started) from None
        if guessed:
            _remember(p, guessed)
            guessed = set()
        stop = st['stop'] or stop
        total = {'in': total['in'] + int(st['usage'].get('in') or 0),
                 'out': total['out'] + int(st['usage'].get('out') or 0)}
        if kind != 'anthropic' or st['stop'] != 'pause_turn' or not st['content']:
            break
        # a long server-tool turn paused: send it back as it is and Claude carries on where it stopped
        ms = body['messages']
        if ms and ms[-1]['role'] == 'assistant':
            body['messages'] = ms[:-1] + [{'role': 'assistant', 'content': ms[-1]['content'] + st['content']}]
        else:
            body['messages'] = ms + [{'role': 'assistant', 'content': st['content']}]
    if stop:
        yield 'stop', str(stop)
    yield 'usage', total


def _sse_data(r):
    """The `data:` payloads of a server-sent-events response, decoded as JSON ([DONE] / junk skipped)."""
    for line in r.iter_lines():
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if not data or data == '[DONE]':
            continue
        try:
            yield json.loads(data)
        except ValueError:
            continue


def chat_stream(p, system, msgs, max_tokens=None):
    """Streamed answer: yields ('delta', text) ... then one ('usage', {'in', 'out'}) at the end."""
    kind = p.get('kind')
    path, body, h = _request(p, system, msgs, True, False, max_tokens)
    _drop_refused(p, body)
    usage = {'in': 0, 'out': 0}
    for _ in range(len(_PARAMS) + 1):
        try:
            with _client(p).stream('POST', path, json=body, headers=h) as r:
                if r.status_code >= 400:
                    r.read()
                    if _retry_without(p, r, body):
                        continue
                    raise _fail(p, r, f'{p.get("model")}')
                for ev in _sse_data(r):
                    if kind in ('openai', 'openai_compat'):
                        if ev.get('error'):
                            raise LLMError(_scrub(p, f'provider error: {ev["error"]}'), 502)
                        for ch in ev.get('choices') or []:
                            t = (ch.get('delta') or {}).get('content')
                            if t:
                                yield 'delta', t
                        if ev.get('usage'):
                            usage = _usage_openai(ev['usage'])
                    elif kind == 'anthropic':
                        t = ev.get('type')
                        if t == 'content_block_delta' and (ev.get('delta') or {}).get('type') == 'text_delta':
                            yield 'delta', ev['delta'].get('text', '')
                        elif t == 'message_start':
                            u = _usage_anthropic((ev.get('message') or {}).get('usage'))
                            if u:
                                usage = {'in': u['in'], 'out': u['out'] or usage['out']}
                        elif t == 'message_delta' and ev.get('usage'):
                            usage['out'] = int(ev['usage'].get('output_tokens') or usage['out'])
                        elif t == 'error':
                            raise LLMError(_scrub(p, f'provider error: {(ev.get("error") or {}).get("message")}'), 502)
                    else:
                        if ev.get('candidates'):
                            t = _gemini_text(ev)
                            if t:
                                yield 'delta', t
                        if ev.get('usageMetadata'):
                            usage = _usage_gemini(ev['usageMetadata'])      # cumulative: the last one counts
            break
        except httpx.HTTPError as e:
            raise _unreachable(p, e) from None
    yield 'usage', usage


# ---------------------------------------------------------------- JSON answers
_FENCE = re.compile(r'```(?:json|JSON)?\s*(.*?)```', re.S)


def parse_json(text):
    """The first JSON object / array in a model answer (``` fences and chatter around it are ignored).
    Raises ValueError."""
    t = (text or '').strip()
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    starts = [i for i in (t.find('{'), t.find('[')) if i >= 0]
    if not starts:
        raise ValueError('no JSON object in the answer')
    obj, _ = json.JSONDecoder().raw_decode(t[min(starts):])
    return obj


def complete_json(p, system, msgs, max_tokens=None):
    """(parsed JSON, usage): asks for JSON; one retry that shows the model its parse error."""
    text, usage = complete(p, system, msgs, json=True, max_tokens=max_tokens)
    try:
        return parse_json(text), usage
    except ValueError as e:
        err = e
    _log.debug(f'json retry: {vlog.esc(str(err))}')
    retry = list(msgs) + [{'role': 'assistant', 'parts': [{'text': text or '(empty)'}]},
                          {'role': 'user', 'parts': [{'text': f'That was not valid JSON ({err}). Answer again with '
                                                              'only the JSON, no other text.'}]}]
    text2, u2 = complete(p, system, retry, json=True, max_tokens=max_tokens)
    usage = {'in': usage['in'] + u2['in'], 'out': usage['out'] + u2['out']}
    try:
        return parse_json(text2), usage
    except ValueError as e:
        raise LLMError(f'the model did not answer valid JSON: {e}', 502) from None


# ---------------------------------------------------------------- models list + connection test
def list_models(p):
    """Model ids the provider offers (sorted); LLMError when it cannot list them."""
    kind = p.get('kind')
    h = _headers(p)
    c = _client(p)
    try:
        if kind in ('openai', 'openai_compat', 'anthropic'):
            r = c.get('/v1/models' if kind == 'anthropic' else '/models', headers=h,
                      params={'limit': 1000} if kind == 'anthropic' else None)
            if r.status_code >= 400:
                raise _fail(p, r, 'list models')
            data = r.json()
            ids = [str(m.get('id')) for m in (data.get('data') if isinstance(data, dict) else data) or []
                   if isinstance(m, dict) and m.get('id')]
        elif kind == 'gemini':
            ids, token = [], None
            for _ in range(10):
                r = c.get('/models', headers=h, params={'pageSize': 1000, **({'pageToken': token} if token else {})})
                if r.status_code >= 400:
                    raise _fail(p, r, 'list models')
                data = r.json()
                for m in data.get('models') or []:
                    if 'generateContent' in (m.get('supportedGenerationMethods') or ['generateContent']):
                        ids.append(str(m.get('name', '')).removeprefix('models/'))
                token = data.get('nextPageToken')
                if not token:
                    break
        else:
            raise LLMError(f'unknown provider kind {kind!r}')
    except httpx.HTTPError as e:
        raise _unreachable(p, e) from None
    except ValueError as e:
        if isinstance(e, LLMError):
            raise
        raise LLMError('the provider answered a model list that is not JSON', 502) from None
    return sorted({i for i in ids if i})


# Context windows when the provider's API does not say (OpenAI has no such field). First match wins, so the longer
# prefixes come first. The provider card's "Model context" overrides all of it.
MODEL_CONTEXT = (
    ('claude-haiku-4-5', 200_000), ('claude-3', 200_000), ('claude-sonnet-4-5', 200_000), ('claude-opus-4-5', 200_000),
    ('claude-opus-4-1', 200_000), ('claude-opus-4-0', 200_000), ('claude-sonnet-4-0', 200_000), ('claude-', 1_000_000),
    ('gpt-4.1', 1_047_576), ('gpt-4o', 128_000), ('gpt-4-turbo', 128_000), ('gpt-5', 400_000), ('o1', 200_000),
    ('o3', 200_000), ('o4', 200_000), ('gemini-1.5-pro', 2_097_152), ('gemini-', 1_048_576),
)
_LIMIT_KEYS = ('context_length', 'max_model_len', 'context_window', 'max_context_length', 'max_input_tokens')
_limits = {}                    # (kind, base_url, model) -> the answer of model_limits (unknown included: asked once)
_limits_lock = threading.Lock()


def _int(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 1000 else None


def _compat_context(p, c, h):
    """An OpenAI-compatible server's own number: its /models entry (OpenRouter context_length, vLLM max_model_len,
    Groq context_window, LM Studio max_context_length, llama.cpp meta.n_ctx_train), else Ollama's /api/show."""
    r = c.get('/models', headers=h, timeout=5.0)
    if r.status_code < 400:
        data = r.json()
        for m in (data.get('data') if isinstance(data, dict) else data) or []:
            if isinstance(m, dict) and m.get('id') == p['model']:
                n = next((_int(m.get(k)) for k in _LIMIT_KEYS if _int(m.get(k))), None) \
                    or _int((m.get('meta') or {}).get('n_ctx_train'))
                if n:
                    return n, _int(m.get('max_completion_tokens'))
                break                           # listed without a size (Ollama): ask its own API below
    base = str(p.get('base_url') or '').rstrip('/')
    if base.endswith('/v1'):                    # Ollama answers OpenAI on /v1 and its own API at the root
        r = c.post(base[:-3] + '/api/show', headers=h, json={'model': p['model']}, timeout=5.0)
        if r.status_code < 400:
            d = r.json()
            m = re.search(r'(?m)^\s*num_ctx\s+(\d+)', str(d.get('parameters') or ''))
            if m:
                return _int(m.group(1)), None
            info = d.get('model_info') or {}
            return next((_int(v) for k, v in info.items() if k.endswith('.context_length') and _int(v)), None), None
    return None, None


def model_limits(p):
    """{'context', 'output', 'source'}: the model's context window and output cap, from the provider card
    ('manual'), the provider's own API ('api') or MODEL_CONTEXT ('table'); None where unknown. Asked once per model
    and remembered; never raises (a provider that cannot say is simply unknown)."""
    manual = _int(p.get('context_window'))
    if manual:
        return {'context': manual, 'output': None, 'source': 'manual'}
    key = (p.get('kind'), str(p.get('base_url') or ''), str(p.get('model') or ''))
    hit = _limits.get(key)
    if hit is not None:
        return hit
    kind, model = key[0], key[2]
    ctx = out = None
    try:
        if kind in ('gemini', 'anthropic', 'openai_compat') and model:
            c, h = _client(p), _headers(p)
            if kind == 'gemini':
                r = c.get(f'/models/{model}', headers=h, timeout=5.0)
                if r.status_code < 400:
                    d = r.json()
                    ctx, out = _int(d.get('inputTokenLimit')), _int(d.get('outputTokenLimit'))
            elif kind == 'anthropic':
                r = c.get(f'/v1/models/{model}', headers=h, timeout=5.0)
                if r.status_code < 400:
                    d = r.json()
                    ctx, out = _int(d.get('max_input_tokens')), _int(d.get('max_tokens'))
            else:
                ctx, out = _compat_context(p, c, h)
    except (httpx.HTTPError, LLMError, ValueError, TypeError, AttributeError):
        ctx = out = None
    src = 'api' if ctx else None
    if not ctx:
        ctx = next((n for pre, n in MODEL_CONTEXT if model.lower().startswith(pre)), None)
        src = 'table' if ctx else None
    got = {'context': ctx, 'output': out, 'source': src}
    with _limits_lock:
        _limits[key] = got
    return got


def red_jpeg(size=32):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (size, size), (220, 20, 20)).save(buf, 'JPEG', quality=95)
    return buf.getvalue()


def tone_wav(seconds=0.5, hz=440, rate=16000):
    """A 16-bit mono PCM WAV of a sine tone."""
    n = int(seconds * rate)
    pcm = b''.join(struct.pack('<h', int(12000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(n))
    hdr = b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16)
    return hdr + b'data' + struct.pack('<I', len(pcm)) + pcm


def test(p):
    """Test connection: {ok, ms, model, text_ok, vision_ok, audio_ok, reply, error?}. Text always; vision / audio
    only when the provider says it has them (else None). Never raises for a provider problem."""
    out = {'ok': False, 'ms': None, 'model': p.get('model'), 'text_ok': False, 'vision_ok': None, 'audio_ok': None,
           'reply': ''}
    # small max tokens, but enough for models that think before answering
    mt = min(512, int(p.get('max_output_tokens') or 512))
    try:
        t0 = time.perf_counter()
        text, _ = complete(p, '', [{'role': 'user', 'parts': [{'text': 'Reply with the single word OK.'}]}], max_tokens=mt)
        out['ms'] = round((time.perf_counter() - t0) * 1000)
        out['text_ok'] = True
        out['reply'] = (text or '').strip()[:200]
        errors = []
        if p.get('vision'):
            try:
                v, _ = complete(p, '', [{'role': 'user', 'parts': [
                    {'jpeg': red_jpeg()}, {'text': 'What colour is this square? Answer with one word.'}]}], max_tokens=mt)
                out['vision_ok'] = 'red' in (v or '').lower()
                if not out['vision_ok']:
                    errors.append(f'vision: expected "red", the model said {(v or "").strip()[:60]!r}')
            except LLMError as e:
                out['vision_ok'] = False
                errors.append(f'vision: {e}')
        if p.get('audio'):
            try:
                a, _ = complete(p, '', [{'role': 'user', 'parts': [
                    {'wav': tone_wav()}, {'text': 'Describe this audio clip in a few words.'}]}], max_tokens=mt)
                out['audio_ok'] = bool((a or '').strip())
                if not out['audio_ok']:
                    errors.append('voice: the model gave no answer to an audio clip')
            except LLMError as e:
                out['audio_ok'] = False
                errors.append(f'voice: {e}')
        out['ok'] = out['vision_ok'] is not False and out['audio_ok'] is not False
        if errors:
            out['error'] = '; '.join(errors)
    except LLMError as e:
        out['error'] = str(e)
    except Exception as e:  # noqa: BLE001 - a test never raises: the page shows the message
        out['error'] = _scrub(p, f'{type(e).__name__}: {e}')
    if out.get('error'):
        _log.info(f'test {vlog.esc(p.get("name") or p.get("model"))}: {vlog.esc(out["error"])}')
    return out
