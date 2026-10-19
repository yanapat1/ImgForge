"""The ◆ AI assistant: the provider store (0600, masked keys, env: keys, re-test on edit), the three provider APIs
against fake servers over real HTTP (OpenAI-compatible / Anthropic / Gemini: auth headers, SSE streaming, JSON
answers), the connection test, feature gating (403), the NDJSON chat, token budget trimming, box-format conversion,
and the structured features on a small generated dataset (label check, NL filter, auto-tag job) and a labeling
session (assist / prelabel / accept)."""

import json
import os
import stat

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from conftest import jpeg_bytes, wait_job
from test_models import _serve

OPENAI_KEY, ANTHROPIC_KEY, GEMINI_KEY = 'sk-openai-secret-123', 'sk-ant-secret-456', 'AIza-gemini-secret-789'
REPLY = {'text': 'Hello there, this is the fake model.'}     # what the fake models answer to a non-test prompt
SEEN = {}                                                    # last request body per kind


def _answer(texts, has_image, has_audio):
    joined = ' '.join(texts)
    if has_image and 'colour' in joined:
        return 'Red.'
    if has_audio and 'audio clip' in joined:
        return 'A beep.'
    if 'single word OK' in joined:
        return 'OK'
    return REPLY['text']


def _chunks(text, n=3):
    k = max(1, len(text) // n)
    return [text[i:i + k] for i in range(0, len(text), k)]


def fake_app():
    app = FastAPI()

    # ---- openai-compatible
    @app.get('/v1/models')
    def oa_models(request: Request):
        if request.headers.get('authorization') != f'Bearer {OPENAI_KEY}':
            return JSONResponse({'error': {'message': 'Incorrect API key provided'}}, 401)
        return {'data': [{'id': 'gpt-b'}, {'id': 'gpt-a'}]}

    @app.post('/v1/chat/completions')
    async def oa_chat(request: Request):
        auth = request.headers.get('authorization')
        if auth not in (f'Bearer {OPENAI_KEY}', None):
            return JSONResponse({'error': {'message': f'Incorrect API key provided: {auth[7:]}'}}, 401)
        body = await request.json()
        SEEN['openai'] = body
        texts, img, aud = [], False, False
        for m in body['messages']:
            if isinstance(m['content'], str):
                texts.append(m['content'])
                continue
            for c in m['content']:
                texts.append(c.get('text', ''))
                img |= c['type'] == 'image_url'
                aud |= c['type'] == 'input_audio'
        text = _answer(texts, img, aud)
        if not body.get('stream'):
            return {'choices': [{'message': {'role': 'assistant', 'content': text}}],
                    'usage': {'prompt_tokens': 11, 'completion_tokens': 3}}

        def gen():
            for c in _chunks(text):
                yield 'data: ' + json.dumps({'choices': [{'delta': {'content': c}}]}) + '\n\n'
            if (body.get('stream_options') or {}).get('include_usage'):
                yield 'data: ' + json.dumps({'choices': [], 'usage': {'prompt_tokens': 21, 'completion_tokens': 5}}) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(gen(), media_type='text/event-stream')

    # ---- anthropic
    @app.post('/v1/messages')
    async def an_messages(request: Request):
        if request.headers.get('x-api-key') != ANTHROPIC_KEY or request.headers.get('anthropic-version') != '2023-06-01':
            return JSONResponse({'type': 'error', 'error': {'type': 'authentication_error',
                                                            'message': 'invalid x-api-key'}}, 401)
        body = await request.json()
        SEEN['anthropic'] = body
        texts, img = [], False
        for m in body['messages']:
            for c in m['content']:
                texts.append(c.get('text', ''))
                img |= c['type'] == 'image'
        text = _answer(texts, img, False)
        if not body.get('stream'):
            return {'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 9, 'output_tokens': 2}}

        def gen():
            ev = lambda t, d: f'event: {t}\ndata: {json.dumps({"type": t, **d})}\n\n'  # noqa: E731
            yield ev('message_start', {'message': {'usage': {'input_tokens': 30, 'output_tokens': 1,
                                                             'cache_read_input_tokens': 4}}})
            yield ev('content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': ''}})
            for c in _chunks(text):
                yield ev('content_block_delta', {'index': 0, 'delta': {'type': 'text_delta', 'text': c}})
            yield ev('message_delta', {'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 7}})
            yield ev('message_stop', {})
        return StreamingResponse(gen(), media_type='text/event-stream')

    # ---- gemini
    @app.get('/v1beta/models')
    def ge_models(request: Request):
        if request.headers.get('x-goog-api-key') != GEMINI_KEY:
            return JSONResponse({'error': {'code': 400, 'message': 'API key not valid.'}}, 400)
        return {'models': [{'name': 'models/gemini-x', 'supportedGenerationMethods': ['generateContent']},
                           {'name': 'models/embed-y', 'supportedGenerationMethods': ['embedContent']}]}

    @app.post('/v1beta/models/{spec}')
    async def ge_generate(spec: str, request: Request):
        if request.headers.get('x-goog-api-key') != GEMINI_KEY:
            return JSONResponse({'error': {'code': 400, 'message': 'API key not valid.'}}, 400)
        model, method = spec.split(':')
        body = await request.json()
        SEEN['gemini'] = body
        texts, img, aud = [], False, False
        for c in body['contents']:
            for x in c['parts']:
                texts.append(x.get('text', ''))
                mt = (x.get('inline_data') or {}).get('mime_type', '')
                img |= mt.startswith('image/')
                aud |= mt.startswith('audio/')
        text = _answer(texts, img, aud)
        if method == 'generateContent':
            return {'candidates': [{'content': {'role': 'model', 'parts': [{'text': text}]}}],
                    'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 4}}
        assert method == 'streamGenerateContent' and request.query_params.get('alt') == 'sse'

        def gen():
            for k, c in enumerate(_chunks(text)):
                yield 'data: ' + json.dumps({'candidates': [{'content': {'parts': [{'text': c}]}}],
                                             'usageMetadata': {'promptTokenCount': 40, 'candidatesTokenCount': k + 1}}) + '\r\n\r\n'
        return StreamingResponse(gen(), media_type='text/event-stream')

    return app


@pytest.fixture(scope='module')
def fake():
    server, url = _serve(fake_app())
    yield url
    server.should_exit = True
    from dataviz.llm import providers
    providers.close_clients()


def form(kind, url, **kw):
    base = {'openai': f'{url}/v1', 'openai_compat': f'{url}/v1', 'anthropic': url, 'gemini': f'{url}/v1beta'}[kind]
    key = {'openai': OPENAI_KEY, 'openai_compat': '', 'anthropic': ANTHROPIC_KEY, 'gemini': GEMINI_KEY}[kind]
    model = {'gemini': 'gemini-x', 'anthropic': 'claude-x'}.get(kind, 'gpt-x')
    return {'name': kind, 'kind': kind, 'base_url': base, 'model': model, 'api_key': key, 'vision': True,
            'audio': kind != 'anthropic', **kw}


def connect(client, kind, url, features=(), **kw):
    p = client.post('/api/llm/providers', json=form(kind, url, **kw)).json()['provider']
    res = client.post('/api/llm/test', json={'id': p['id']}).json()
    assert res['ok'], res
    client.put('/api/llm/config', json={'active': p['id'], 'features': {f: True for f in features}})
    return p


# ---------------------------------------------------------------- store
def test_store_secrets_and_retest(client, monkeypatch):
    s = client.ctx.llm
    p = client.post('/api/llm/providers', json=form('openai', 'http://x')).json()
    pub = p['provider']
    assert pub['has_key'] and OPENAI_KEY not in json.dumps(p) and pub['api_key_masked'].endswith(OPENAI_KEY[-4:])
    assert stat.S_IMODE(os.stat(s.path).st_mode) == 0o600
    pid = pub['id']
    # blank key keeps the stored one
    client.post('/api/llm/providers', json={**form('openai', 'http://x'), 'id': pid, 'api_key': ''})
    assert s.provider(pid)['api_key'] == OPENAI_KEY
    s.record_test(pid, {'ok': True, 'ms': 5})
    assert client.get('/api/llm/state').json()['connected']
    # a non-retest field keeps verified, a model change clears it
    client.post('/api/llm/providers', json={'id': pid, **form('openai', 'http://x'), 'api_key': '', 'temperature': 0.5})
    assert s.provider(pid).get('verified_at')
    client.post('/api/llm/providers', json={'id': pid, **form('openai', 'http://x'), 'api_key': '', 'model': 'other'})
    assert not s.provider(pid).get('verified_at') and not client.get('/api/llm/state').json()['connected']
    # env: keys resolve at call time and are shown as their variable name
    monkeypatch.setenv('MY_LLM_KEY', 'from-env')
    from dataviz.llm.store import resolve_key
    p2 = client.post('/api/llm/providers', json=form('gemini', 'http://x', api_key='env:MY_LLM_KEY')).json()['provider']
    assert p2['key_env'] and p2['api_key_masked'] == 'env:MY_LLM_KEY'
    assert resolve_key(s.provider(p2['id'])) == 'from-env'
    assert client.post('/api/llm/providers', json=form('openai', 'ftp://x')).status_code == 400
    cfg = client.get('/api/llm/config').json()
    assert set(cfg['catalog']) >= {'assistant', 'voice'} and not any(cfg['features'].values())


# ---------------------------------------------------------------- provider calls
@pytest.mark.parametrize('kind', ['openai', 'openai_compat', 'anthropic', 'gemini'])
def test_connection_test_and_chat_stream(client, fake, kind):
    p = connect(client, kind, fake, features=('assistant',))
    saved = client.ctx.llm.provider(p['id'])
    assert saved['verify']['text_ok'] and saved['verify']['vision_ok']
    assert saved['verify']['audio_ok'] is (None if kind == 'anthropic' else True)
    st = client.get('/api/llm/state').json()
    assert st['connected'] and st['features']['assistant'] and not st['features']['dataset_qa']
    REPLY['text'] = 'Streaming answer number one.'
    r = client.post('/api/llm/chat', json={'messages': [{'role': 'user', 'text': 'hi'},
                                                         {'role': 'assistant', 'text': 'hello'},
                                                         {'role': 'user', 'text': 'how many classes?'}], 'ctx': {}})
    assert r.status_code == 200 and r.headers['content-type'].startswith('application/x-ndjson')
    lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert lines[0]['t'] == 'meta' and lines[0]['in_est'] > 0
    assert ''.join(x['text'] for x in lines if x['t'] == 'delta') == REPLY['text']
    done = lines[-1]
    assert done['t'] == 'done' and done['usage']['in'] > 0 and done['usage']['out'] > 0
    if kind == 'anthropic':
        assert SEEN['anthropic']['system'][0]['cache_control'] == {'type': 'ephemeral'}
        assert done['usage'] == {'in': 34, 'out': 7}
    if kind == 'openai':
        assert SEEN['openai']['stream_options'] == {'include_usage': True}
    if kind == 'openai_compat':
        assert 'stream_options' not in SEEN['openai']


@pytest.mark.parametrize('kind', ['openai', 'anthropic', 'gemini'])
def test_wrong_key_fails_without_leaking(client, fake, kind):
    p = client.post('/api/llm/providers', json=form(kind, fake, api_key='wrong-key-abcdef')).json()['provider']
    res = client.post('/api/llm/test', json={'id': p['id']}).json()
    assert res['ok'] is False and res['text_ok'] is False and '40' in res['error']
    assert 'wrong-key-abcdef' not in res['error']
    saved = client.ctx.llm.provider(p['id'])
    assert not saved.get('verified_at') and saved['last_error']
    # an unsaved form is tested but nothing is recorded
    res = client.post('/api/llm/test', json={'form': form(kind, fake)}).json()
    assert res['ok'], res


def test_models_list(client, fake):
    assert client.post('/api/llm/models', json={'form': form('openai', fake, model='')}).json() == {'models': ['gpt-a', 'gpt-b']}
    assert client.post('/api/llm/models', json={'form': form('gemini', fake)}).json() == {'models': ['gemini-x']}
    r = client.post('/api/llm/models', json={'form': form('openai', fake, api_key='nope-nope-nope')})
    assert r.status_code == 400 and 'nope-nope-nope' not in r.text


def test_features_gated(client, fake):
    msg = {'messages': [{'role': 'user', 'text': 'hi'}], 'ctx': {}}
    assert client.post('/api/llm/chat', json=msg).status_code == 403          # nothing configured
    connect(client, 'openai', fake)
    assert client.post('/api/llm/chat', json=msg).status_code == 403          # connected, switch off
    client.put('/api/llm/config', json={'features': {'assistant': True}})
    assert client.post('/api/llm/chat', json={**msg, 'ctx': {'dataset': True}}).status_code == 403
    client.put('/api/llm/config', json={'features': {'dataset_qa': True}})
    assert client.post('/api/llm/chat', json={**msg, 'ctx': {'dataset': True}}).status_code == 409   # no session
    for path, body in (('/api/llm/filter', {'text': 'x'}), ('/api/llm/label-check', {'ids': [1]}),
                       ('/api/llm/tag', {'scope': 'all'}), ('/api/llm/voice', {'wav_b64': 'AAAA', 'screen': 'viz'}),
                       ('/api/llm/label/p/assist', {}), ('/api/llm/label/p/accept', {})):
        assert client.post(path, json=body).status_code == 403, path
    # voice needs the audio capability too
    p = connect(client, 'anthropic', fake, features=('voice',))
    assert client.ctx.llm.provider(p['id'])['audio'] is False
    assert client.post('/api/llm/voice', json={'wav_b64': 'AAAA', 'screen': 'viz'}).status_code == 403


def test_complete_json_retry_and_fences(fake):
    from dataviz.llm import providers
    p = {'kind': 'openai_compat', 'base_url': f'{fake}/v1', 'model': 'm', 'api_key': ''}
    REPLY['text'] = 'Sure!\n```json\n{"a": [1, 2]}\n```'
    assert providers.complete_json(p, 'sys', [{'role': 'user', 'parts': [{'text': 'q'}]}])[0] == {'a': [1, 2]}
    assert providers.parse_json('noise [1, {"b": 2}] tail') == [1, {'b': 2}]
    REPLY['text'] = 'not json at all'
    with pytest.raises(providers.LLMError) as e:
        providers.complete_json(p, 'sys', [{'role': 'user', 'parts': [{'text': 'q'}]}])
    assert e.value.status == 502


# ---------------------------------------------------------------- budget + boxes
def test_budget_fit():
    from dataviz.llm import budget
    from dataviz.llm.providers import LLMError
    img = jpeg_bytes(1024, 1024)
    hist = []
    for k in range(30):
        hist += [{'role': 'user', 'parts': [{'text': 'q' * 3500}, {'jpeg': img}]},
                 {'role': 'assistant', 'parts': [{'text': 'a' * 3500}]}]
    hist.append({'role': 'user', 'parts': [{'text': 'latest'}, {'jpeg': img}]})
    msgs, est = budget.fit('anthropic', 'sys', [{'text': 'ctx'}], hist, 8000)
    assert est <= 8000 and msgs[0]['role'] == 'user' and len(msgs) < len(hist)
    assert all('jpeg' not in x for m in msgs[:-1] for x in m['parts'])           # older images omitted
    assert any(x.get('text') == budget.OMITTED for m in msgs[:-1] for x in m['parts'])
    assert 'jpeg' in msgs[-1]['parts'][-1] and msgs[-1]['parts'][0] == {'text': 'ctx'}
    # the hard cap: a 500k budget is still 100k
    assert budget.cap(500000) == 100000
    with pytest.raises(LLMError):
        budget.fit('openai', 'sys', [], [{'role': 'user', 'parts': [{'text': 'x' * 400000}]}], 500000)
    # context images are downscaled, then dropped, before giving up
    big = jpeg_bytes(2048, 2048)
    msgs, est = budget.fit('anthropic', 's', [{'jpeg': big}], [{'role': 'user', 'parts': [{'text': 'q'}]}], 1000)
    assert est <= 1000
    assert budget.image_tokens('gemini', 300, 300) == 258 and budget.image_tokens('openai', 512, 512) == 255


def test_box_formats():
    from dataviz.llm.tasks import to_norm, parse_boxes
    assert to_norm([100, 200, 300, 400], 'norm1000_xyxy', 0, 0) == (0.1, 0.2, 0.3, 0.4)
    assert to_norm([100, 200, 300, 400], 'norm1000_yxyx', 0, 0) == (0.2, 0.1, 0.4, 0.3)
    assert to_norm([32, 24, 64, 48], 'pixel_xyxy', 64, 48) == (0.5, 0.5, 1.0, 1.0)
    assert to_norm([300, 400, 100, 200], 'norm1000_xyxy', 0, 0) == (0.1, 0.2, 0.3, 0.4)     # swapped corners
    assert to_norm([-50, 0, 2000, 500], 'norm1000_xyxy', 0, 0) == (0.0, 0.0, 1.0, 0.5)      # clamped
    assert to_norm([100, 100, 100, 300], 'norm1000_xyxy', 0, 0) is None                     # no width
    assert to_norm(['a', 1, 2, 3], 'norm1000_xyxy', 0, 0) is None and to_norm([1, 2], 'pixel_xyxy', 9, 9) is None
    classes = [{'id': 'c1', 'name': 'Hand'}, {'id': 'c2', 'name': 'face'}]
    out = parse_boxes({'boxes': [{'class': 'hand', 'box': [0, 0, 500, 500], 'note': 'left'},
                                 {'class': 'tail', 'box': [0, 0, 100, 100]}, {'class': 'face', 'box': [1, 1, 1, 1]}]},
                      'norm1000_xyxy', (0, 0), classes)
    assert out == [{'class_name': 'Hand', 'class_id': 'c1', 'x1': 0.0, 'y1': 0.0, 'x2': 0.5, 'y2': 0.5, 'note': 'left'},
                   {'class_name': 'tail', 'class_id': None, 'x1': 0.0, 'y1': 0.0, 'x2': 0.1, 'y2': 0.1, 'note': ''}]


def test_clean_filter():
    from dataviz.llm.tasks import clean_filter, EMPTY_FILTER
    assert clean_filter(None, ['a']) == EMPTY_FILTER
    f = clean_filter({'scope': 'box', 'cls': {'mode': 'none', 'ids': [1, 'A', 9, True], 'names': ['b']},
                      'area': [None, '5'], 'brightness': [0, 'x'], 'tags': ['ai:night', 'nope'], 'issues': ['oob', 'zz'],
                      'clip': {'q': 'a dog', 'k': 99999}, 'suspicion_min': 'bad', 'extra': 1},
                     ['a', 'b'], ['ai:night'], ['oob'])
    assert f['scope'] == 'box' and f['cls'] == {'mode': 'none', 'ids': [0, 1]}
    assert f['area'] == [None, 5.0] and f['brightness'] == [0.0, None] and f['tags'] == ['ai:night']
    assert f['issues'] == ['oob'] and f['clip'] == {'q': 'a dog', 'k': 20000} and f['suspicion_min'] is None
    assert set(f) == set(EMPTY_FILTER)


# ---------------------------------------------------------------- a dataset session
@pytest.fixture
def viz(tmp_path, fake):
    from fastapi.testclient import TestClient
    from test_curation import make_dataset, open_state
    from dataviz.core.jobs import JobManager
    from dataviz.llm.store import LLMStore
    from dataviz.server.app import Ctx, create_app
    st = open_state(make_dataset(tmp_path / 'ds'), tmp_path / 'ws')
    ctx = Ctx(st, jobs=JobManager(), llm=LLMStore(tmp_path / 'root'))
    ctx.cfg.setdefault('logging', {})['http'] = False
    with TestClient(create_app(ctx)) as c:
        c.ctx, c.st = ctx, st
        connect(c, 'openai', fake, features=('assistant', 'dataset_qa', 'explain_eval', 'ask_selection', 'label_check',
                                             'nl_filter', 'auto_tag', 'report', 'voice'))
        yield c


def test_session_chat_with_context(viz):
    REPLY['text'] = 'It has three classes.'
    r = viz.post('/api/llm/chat', json={'messages': [{'role': 'user', 'text': 'summary?'}],
                                        'ctx': {'dataset': True, 'eval': True, 'selection': [0, 1, 2]}})
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert lines[-1]['t'] == 'done', lines
    sent = SEEN['openai']['messages'][-1]['content']
    texts = ' '.join(c.get('text', '') for c in sent)
    assert 'DATASET SUMMARY' in texts and 'EVALUATION' in texts and '#1 → box 0' in texts
    assert sum(c['type'] == 'image_url' for c in sent) == 1                    # one mosaic, not three images
    r = viz.post('/api/llm/chat', json={'messages': [{'role': 'user', 'text': ''}], 'ctx': {}, 'task': 'report'})
    assert json.loads(r.text.splitlines()[-1])['t'] == 'done'
    assert 'dataset report' in SEEN['openai']['messages'][0]['content']


def test_label_check_filter_tag(viz):
    st = viz.st
    ids = list(range(min(3, len(st.samples))))
    REPLY['text'] = json.dumps({'items': [{'n': 1, 'verdict': 'wrong', 'suggest': st.classes[1].upper(), 'why': 'x'},
                                          {'n': 2, 'verdict': 'ok', 'suggest': None, 'why': ''},
                                          {'n': 9, 'verdict': 'ok'}]})
    out = viz.post('/api/llm/label-check', json={'ids': ids}).json()
    assert [x['id'] for x in out['items']] == ids
    assert out['items'][0]['verdict'] == 'wrong' and out['items'][0]['suggest'] in (st.classes[1], None)
    assert out['items'][1]['verdict'] == 'ok' and out['items'][2]['verdict'] == 'unsure'
    assert viz.post('/api/llm/label-check', json={'ids': list(range(97))}).status_code == 400

    REPLY['text'] = json.dumps({'spec': {'cls': {'ids': [st.classes[0]]}, 'blur': [None, 50]}, 'explain': 'blurry'})
    f = viz.post('/api/llm/filter', json={'text': f'blurry {st.classes[0]}'}).json()
    assert f['spec']['cls']['ids'] == [0] and f['spec']['blur'] == [None, 50.0] and f['explain'] == 'blurry'
    assert st.classes[0] in SEEN['openai']['messages'][0]['content'] or \
        st.classes[0] in json.dumps(SEEN['openai']['messages'])

    REPLY['text'] = json.dumps({'images': [{'n': 1, 'tags': ['night', 'bogus']}]})
    r = viz.post('/api/llm/tag', json={'scope': 'selection', 'ids': [0], 'vocab': ['night', 'rain']}).json()
    j = wait_job(viz, r['job_id'])
    assert j['status'] == 'done', j
    assert j['result'] == {'images': 1, 'tags': {'ai:night': 1}}
    tags = st.load_tags()
    img = st.samples[0]['image']
    assert tags['ai:night'] == sorted(st.by_image[img]) and 'ai:bogus' not in tags


def test_voice(viz):
    from dataviz.llm.providers import tone_wav
    import base64
    b64 = base64.b64encode(tone_wav()).decode()
    REPLY['text'] = json.dumps({'transcript': 'go to evaluation', 'action': 'tab', 'args': {'tab': 'eval'}})
    assert viz.post('/api/llm/voice', json={'wav_b64': b64, 'screen': 'viz'}).json() == \
        {'transcript': 'go to evaluation', 'action': 'tab', 'args': {'tab': 'eval'}, 'usage': {'in': 11, 'out': 3}}
    REPLY['text'] = json.dumps({'transcript': 'delete everything', 'action': 'rm', 'args': {}})
    out = viz.post('/api/llm/voice', json={'wav_b64': b64, 'screen': 'label'}).json()
    assert out['action'] == 'ask' and out['args'] == {'text': 'delete everything'}
    REPLY['text'] = json.dumps({'transcript': 'class dog', 'action': 'class', 'args': {'name': 'DOG'}})
    out = viz.post('/api/llm/voice', json={'wav_b64': b64, 'screen': 'label', 'classes': ['cat', 'dog']}).json()
    assert out['action'] == 'class' and out['args'] == {'name': 'dog'}


# ---------------------------------------------------------------- the label page
def test_label_assist_prelabel_accept(client, fake):
    pytest.importorskip('dataviz.labeling.media')
    from test_routes import create
    out = create(client, tasks=[{'name': 'Detect', 'task_type': 'object_detection',
                                 'classes': [{'name': 'hand'}, {'name': 'face'}]},
                                {'name': 'Segment', 'task_type': 'segmentation', 'classes': ['x']}])
    pid = out['project']['id']
    det, seg = out['project']['tasks']
    files = [('files', ('a.jpg', jpeg_bytes(200, 100), 'image/jpeg')), ('files', ('b.jpg', jpeg_bytes(80, 80), 'image/jpeg'))]
    assert client.post(f'/api/label/projects/{pid}/upload', files=files).status_code == 200
    assert wait_job(client, client.post(f'/api/label/projects/{pid}/import', json={}).json()['job_id'])['status'] == 'done'
    ids = client.get(f'/api/label/projects/{pid}/media-ids').json()['ids']
    hand = det['classes'][0]['id']
    a = {'id': 'a' * 32, 'class_id': hand, 'geometry_type': 'bbox', 'geometry': {'x': .1, 'y': .1, 'w': .5, 'h': .5}}
    client.put(f'/api/label/projects/{pid}/annotations/{ids[1]}',
               json={'task_id': det['id'], 'base_version': 0, 'annotations': [a]})
    connect(client, 'anthropic', fake, features=('label_assist', 'prelabel'))
    REPLY['text'] = json.dumps({'answer': 'Box each hand from wrist to fingertips.',
                                'boxes': [{'class': 'Hand', 'box': [100, 200, 500, 600], 'note': 'left hand'},
                                          {'class': 'foot', 'box': [0, 0, 100, 100]}]})
    r = client.post(f'/api/llm/label/{pid}/assist', json={'mid': ids[0], 'task_id': det['id'],
                                                         'question': 'how do I label the hands here?'})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res['answer'].startswith('Box each hand') and res['usage'] == {'in': 9, 'out': 2}
    b = res['boxes']
    assert b[0] == {'class_name': 'hand', 'class_id': hand, 'x1': 0.1, 'y1': 0.2, 'x2': 0.5, 'y2': 0.6, 'note': 'left hand'}
    assert b[1]['class_id'] is None
    # the exemplar sheet is sent, flagged for prompt caching, and the class list is in the prompt
    content = SEEN['anthropic']['messages'][0]['content']
    imgs = [c for c in content if c['type'] == 'image']
    assert len(imgs) == 2 and imgs[0]['cache_control'] == {'type': 'ephemeral'} and 'cache_control' not in imgs[1]
    assert 'hand, face' in content[0]['text']
    assert client.post(f'/api/llm/label/{pid}/prelabel', json={'mid': ids[0], 'task_id': det['id']}).status_code == 200
    assert client.post(f'/api/llm/label/{pid}/assist', json={'mid': ids[0], 'task_id': seg['id'],
                                                            'question': 'q'}).status_code == 400
    acc = client.post(f'/api/llm/label/{pid}/accept', json={'mid': ids[0], 'task_id': det['id'], 'boxes': b})
    assert acc.status_code == 200, acc.text
    acc = acc.json()
    assert acc['n_model'] == 1 and len(acc['annotations']) == 1
    g = acc['annotations'][0]
    assert g['source'] == 'model' and g['class_id'] == hand and g['geometry'] == {'x': 0.1, 'y': 0.2, 'w': 0.4, 'h': 0.4}
    assert client.post(f'/api/llm/label/{pid}/accept', json={'mid': ids[0], 'task_id': det['id'],
                                                            'boxes': [b[1]]}).status_code == 400


def test_clean_filter_matches_filter_js(tmp_path):
    """The server's clean spec is a fixed point of the page's normalizeFilter(), and the empty one is emptyFilter()."""
    import shutil
    import subprocess
    from pathlib import Path
    from dataviz.llm.tasks import clean_filter, EMPTY_FILTER
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    shutil.copy(Path(__file__).resolve().parents[1] / 'static' / 'js' / 'core' / 'filter.js', tmp_path / 'filter.mjs')
    spec = clean_filter({'scope': 'box', 'cls': {'mode': 'all', 'ids': [1]}, 'nbox': [2, None], 'tags': ['t'],
                         'clip': {'q': 'dog'}, 'outlier_min': 0.5}, ['a', 'b'], ['t'])
    (tmp_path / 't.mjs').write_text(
        "import assert from 'node:assert/strict';\nimport { emptyFilter, normalizeFilter } from './filter.mjs';\n"
        f"assert.deepEqual(emptyFilter(), {json.dumps(EMPTY_FILTER)});\n"
        f"const s = {json.dumps(spec)};\nassert.deepEqual(normalizeFilter(s), s);\nconsole.log('ok');\n")
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr
