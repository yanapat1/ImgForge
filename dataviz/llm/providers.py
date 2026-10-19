"""
LLM provider clients over raw httpx (no SDKs): OpenAI, OpenAI-compatible servers (vLLM, Ollama, LM Studio,
OpenRouter ...), Anthropic and Gemini behind one message format.

    msgs = [{'role': 'user' | 'assistant', 'parts': [{'text': str} | {'jpeg': bytes, 'cache'?: True} | {'wav': bytes}]}]

`chat_stream()` yields ('delta', text) ... then ('usage', {'in', 'out'}); `complete()` / `complete_json()` answer at
once; `list_models()` fills the Settings model dropdown; `test()` is Test connection. Errors are LLMError with a
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
from .store import resolve_key

_log = vlog.get_logger('llm')
ANTHROPIC_VERSION = '2023-06-01'


class LLMError(ValueError):
    """A provider problem with a human message. status: the HTTP code a route answers with."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


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
    return LLMError(msg, 400 if r.status_code in (400, 401, 403, 404, 422) else 502)


def _unreachable(p, e):
    return LLMError(_scrub(p, f'cannot reach {str(p.get("base_url"))}: {type(e).__name__}: {e}'), 502)


# ---------------------------------------------------------------- request bodies
def _b64(b):
    return base64.b64encode(b).decode('ascii')


def _merge(msgs):
    """Consecutive messages of one role merged (Anthropic / Gemini want alternating turns); empty ones dropped."""
    out = []
    for m in msgs:
        parts = [x for x in m.get('parts') or [] if x.get('text') or x.get('jpeg') or x.get('wav')]
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


def _openai_body(p, system, msgs, stream, json_mode, max_tokens, temperature=True):
    out = [{'role': 'system', 'content': system}] if system else []
    for m in _merge(msgs):
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
    return body


def _anthropic_body(p, system, msgs, stream, max_tokens):
    merged = _merge(msgs)
    # one cache breakpoint on the last context image flagged cache (the exemplar sheet): repeat questions reuse it
    last_cache = None
    for i, m in enumerate(merged):
        for j, x in enumerate(m['parts']):
            if x.get('cache') and 'jpeg' in x:
                last_cache = (i, j)
    out = []
    for i, m in enumerate(merged):
        content = []
        for j, x in enumerate(m['parts']):
            if 'text' in x:
                content.append({'type': 'text', 'text': x['text']})
            elif 'jpeg' in x:
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                                            'data': _b64(x['jpeg'])}})
                if (i, j) == last_cache:
                    content[-1]['cache_control'] = {'type': 'ephemeral'}
            elif 'wav' in x:
                raise LLMError('Claude has no audio input: turn voice off for this provider')
        out.append({'role': m['role'], 'content': content})
    body = {'model': p['model'], 'max_tokens': _max_tokens(p, max_tokens), 'messages': out}
    if system:
        body['system'] = [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}]
    if p.get('temperature') is not None:
        body['temperature'] = min(1.0, float(p['temperature']))       # anthropic: 0..1
    if stream:
        body['stream'] = True
    return body


def _gemini_body(p, system, msgs, json_mode, max_tokens):
    contents = []
    for m in _merge(msgs):
        parts = []
        for x in m['parts']:
            if 'text' in x:
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
    return body


def _gemini_model(p):
    m = str(p['model'])
    return m[len('models/'):] if m.startswith('models/') else m


def _request(p, system, msgs, stream, json_mode=False, max_tokens=None):
    """(path, body, headers) of a generate request for this provider."""
    kind = p.get('kind')
    h = _headers(p)
    if kind in ('openai', 'openai_compat'):
        return '/chat/completions', _openai_body(p, system, msgs, stream, json_mode, max_tokens), h
    if kind == 'anthropic':
        return '/v1/messages', _anthropic_body(p, system, msgs, stream, max_tokens), h
    if kind == 'gemini':
        path = f'/models/{_gemini_model(p)}:' + ('streamGenerateContent?alt=sse' if stream else 'generateContent')
        return path, _gemini_body(p, system, msgs, json_mode, max_tokens), h
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


def _retry_without(p, r, body):
    """Some models refuse a parameter we send by default (reasoning models: temperature). Drop it once."""
    if r.status_code != 400 or 'temperature' not in body:
        return False
    if 'temperature' in _err_message(r).lower():
        body.pop('temperature')
        return True
    return False


def complete(p, system, msgs, json=False, max_tokens=None):
    """(text, usage {'in', 'out'} or None) of one non-streamed answer."""
    path, body, h = _request(p, system, msgs, False, json, max_tokens)
    r = _post(p, path, body, h, 'generate')
    if _retry_without(p, r, body):
        r = _post(p, path, body, h, 'generate')
    if r.status_code >= 400:
        raise _fail(p, r, f'{p.get("model")}')
    try:
        data = r.json()
    except ValueError:
        raise LLMError('the provider answered something that is not JSON', 502) from None
    text, usage = _parse_answer(p['kind'], data)
    return text, usage or {'in': 0, 'out': 0}


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
    usage = {'in': 0, 'out': 0}
    for attempt in (0, 1):
        try:
            with _client(p).stream('POST', path, json=body, headers=h) as r:
                if r.status_code >= 400:
                    r.read()
                    if attempt == 0 and _retry_without(p, r, body):
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
