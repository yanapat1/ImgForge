"""Web search for the ◆ agents: the privacy guard (llm/web_guard.py) per level, the engine chain (the user's engine,
the bundled SearXNG, DuckDuckGo) over an httpx.MockTransport, fetch_url's SSRF guard, Ask before each search with an
edited query, 3 blocked queries end web search for the turn, and the admin's maximum level."""

import threading

import httpx
import pytest

from dataviz.auth import settings
from dataviz.datacenter.sources import fetch
from dataviz.llm import tools, web_guard
from dataviz.llm.engine import confirm, react
from dataviz.llm.tools import web
from test_agent_engine import drain, script, start, types
from test_llm_tools import ALICE, OPENAI, FakeService

SECRET_WORD = 'โครงการลับ'


class _State:
    classes = ['forklift_v2', 'pallet']


@pytest.fixture
def svc(monkeypatch):
    s = FakeService()
    s.buckets.append({'id': 'b9', 'name': 'night_cam-01', 'kind': 'managed', 'writable': True,
                      'tags': [{'tag': 'warehouse-zone', 'auto': False}], 'stats': {}})
    monkeypatch.setattr(tools, '_svc', lambda: s)
    monkeypatch.setattr(react, '_sleep', lambda cancel, s: cancel.is_set())
    org = [{'text': 'Project Falcon', 'strength': 'never'}]
    monkeypatch.setattr(settings, 'get', lambda k: {'llm_org_words': org, 'llm_web_max_level': 3}[k])
    yield s


def ctx(level=1, words=(), **cfg):
    return react.Ctx(user={'id': 'u-alice', 'username': 'alice.w', 'email': 'alice@corp.example'}, role='visual',
                     scope={}, skills={}, state=_State(), web_level=level,
                     web_cfg={'level': level, 'ask_before': False, 'engine': 'auto', 'url': '',
                              'words': list(words), **cfg})


def ok(q, c):
    return web_guard.check(q, c)[0]


# ---------------------------------------------------------------- the guard
def test_guard_levels(svc):
    words = [{'text': SECRET_WORD, 'strength': 'private'}]
    p, t, f = ctx(1, words), ctx(2, words), ctx(3, words)
    bucket = 'yolo tips for night cam 01 images'            # separators normalised: night_cam-01
    klass = 'forklift v2 detection occlusion'
    thai = f'ข้อมูล{SECRET_WORD}2024'                         # Thai: substring, no spaces
    key = 'why does sk-abcdefghijklmnop1234567890 fail'
    assert [ok(bucket, c) for c in (p, t, f)] == [False, False, True]
    assert [ok(klass, c) for c in (p, t, f)] == [False, True, True]
    assert [ok(thai, c) for c in (p, t, f)] == [False, False, True]
    assert [ok(key, c) for c in (p, t, f)] == [False, False, False]
    assert [ok('Project_Falcon release notes', c) for c in (p, t, f)] == [False, False, False]   # 'never', org list
    generic = 'yolo train val images labels coco person car detection tips'
    assert all(ok(generic, c) for c in (p, t, f))
    assert ok('warehouse-zone dataset', p) is False and ok('warehouse zone dataset', t) is True   # a tag: level 2


def test_guard_reasons_and_redaction(svc):
    good, reasons, red = web_guard.check('compare night_cam-01 with alice.w notes on 10.0.0.5', ctx(1))
    assert not good and reasons == ['bucket name', 'person', 'internal address']
    assert red == 'compare [bucket name] with [person] notes on [internal address]'
    assert 'night' not in web_guard.message(reasons) and '3 internal terms (bucket name, person' in \
        web_guard.message(reasons)
    c = ctx(1)
    c.seen.add('cars/train/dock7_shift.jpg')
    assert not ok('dock7 shift corrupted', c) and not ok('dock7_shift.jpg', c) and ok('jpg corrupted', c)
    assert not ok('password=hunter2 in yaml', ctx(3)) and not ok('AKIAABCDEFGHIJKLMNOP leaked', ctx(3))
    assert not ok('regex secret ACME-1234', ctx(1, [{'text': '/acme-\\d+/', 'strength': 'private'}]))


def test_guard_urls(svc):
    assert ok('https://docs.ultralytics.com/datasets/detect/coco/', ctx(1))
    assert not ok('https://example.com/search?q=night_cam-01', ctx(1))
    assert not ok('http://intranet.corp/wiki', ctx(1))
    assert not ok('https://user:pw@example.com/', ctx(3))


# ---------------------------------------------------------------- engines
def mock_http(monkeypatch, handler):
    calls = []

    def h(req):
        calls.append(req)
        return handler(req)

    monkeypatch.setattr(fetch, 'make_client', lambda **kw: httpx.Client(transport=httpx.MockTransport(h)))
    monkeypatch.setattr(fetch, 'resolve_host', lambda host: ['10.0.0.9'] if host == 'searxng' else ['93.184.216.34'])
    return calls


DDG_PAGE = '''<div class="result"><a rel="nofollow" class="result__a"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fyolo&amp;rut=x">YOLO <b>tips</b></a>
<a class="result__snippet" href="#">Train &amp; tune YOLO.</a></div>'''


def test_user_engine_first(monkeypatch, svc):
    calls = mock_http(monkeypatch, lambda r: httpx.Response(200, json={'web': {'results': [
        {'title': 'A', 'url': 'https://a.example/', 'description': 'about <b>a</b>'}]}}))
    monkeypatch.setattr(web, '_key', lambda c: 'brave-key')
    c = ctx(1, engine='brave')
    out, _ = web._web_search(c, {'query': 'yolo augmentation tips'})
    assert out['engine'] == 'brave' and out['results'] == [{'title': 'A', 'url': 'https://a.example/',
                                                             'snippet': 'about a'}]
    assert calls[0].url.host == 'api.search.brave.com' and calls[0].headers['x-subscription-token'] == 'brave-key'
    assert c.events == [{'type': 'search', 'query': 'yolo augmentation tips', 'engine': 'brave', 'level': 1,
                         'results': [{'title': 'A', 'url': 'https://a.example/'}]}]


def test_searxng_down_falls_back_to_duckduckgo(monkeypatch, svc):
    def handler(r):
        if r.url.host == 'searxng':
            raise httpx.ConnectError('down')
        assert r.url.host == 'html.duckduckgo.com' and r.method == 'POST'
        return httpx.Response(200, text=DDG_PAGE)
    calls = mock_http(monkeypatch, handler)
    monkeypatch.setenv('SEARXNG_URL', 'http://searxng:8080')
    out, _ = web._web_search(ctx(1), {'query': 'yolo tips'})
    assert [r.url.host for r in calls] == ['searxng', 'html.duckduckgo.com']
    assert out['engine'] == 'duckduckgo'
    assert out['results'] == [{'title': 'YOLO tips', 'url': 'https://example.org/yolo', 'snippet': 'Train & tune YOLO.'}]


def test_searxng_json(monkeypatch, svc):
    mock_http(monkeypatch, lambda r: httpx.Response(200, json={'results': [
        {'title': f't{i}', 'url': f'https://e.example/{i}', 'content': 'c'} for i in range(20)]}))
    out, summary = web._web_search(ctx(1), {'query': 'yolo tips', 'max_results': 3})
    assert out['engine'] == 'searxng' and len(out['results']) == 3 and summary == '→ 3 results (searxng)'
    assert [n for n, _ in web.chain({'engine': 'duckduckgo'})] == ['duckduckgo']
    assert [n for n, _ in web.chain({'engine': 'tavily'})] == ['tavily', 'searxng', 'duckduckgo']


# ---------------------------------------------------------------- fetch_url
def test_fetch_url_ssrf_and_text(monkeypatch, svc):
    page = ('<html><head><title>Docs</title><style>x{}</style></head><body><script>evil()</script>'
            '<h1>Hello</h1><p>World &amp; more</p></body></html>')
    calls = mock_http(monkeypatch, lambda r: httpx.Response(200, text=page, headers={'content-type': 'text/html'}))
    monkeypatch.setattr(fetch, 'private_allowed', lambda: False)
    for url in ('http://127.0.0.1/', 'http://169.254.169.254/latest/meta-data/', 'file:///etc/passwd'):
        with pytest.raises(tools.ToolError):
            web._fetch_url(ctx(3), {'url': url})
    assert calls == []
    out, _ = web._fetch_url(ctx(1), {'url': 'https://docs.example.org/page'})
    assert out['title'] == 'Docs' and 'Hello' in out['text'] and 'World & more' in out['text']
    assert 'evil' not in out['text'] and 'x{}' not in out['text']


# ---------------------------------------------------------------- in the engine
def run_with(monkeypatch, steps, level=1, **cfg):
    script(monkeypatch, *steps)
    run = start('look it up', role='visual', web=level)
    run.ctx.state = _State()
    run.ctx.web_cfg = {'level': level, 'ask_before': False, 'engine': 'auto', 'url': '', 'words': [], **cfg}
    return run


def test_ask_before_edited_query(monkeypatch, svc):
    sent = []

    def handler(r):
        sent.append(r.url.params.get('q'))
        return httpx.Response(200, json={'results': [{'title': 'T', 'url': 'https://t.example/', 'content': ''}]})
    mock_http(monkeypatch, handler)
    run = run_with(monkeypatch, [('', [('web_search', {'query': 'yolo tips'})]), ('Done.', [])], ask_before=True)
    assert 'web_search' in run.tools and not run.native_web
    out = []
    for ev in react.events(run):
        out.append(ev)
        if ev['type'] == 'confirm':
            a = ev['actions'][0]
            assert a['risk'] == 'web' and a['args'] == {'query': 'yolo tips'} and a['summary'] == \
                'Search the web: yolo tips'
            threading.Timer(0.05, lambda e=ev: confirm.decide(ALICE, e['confirm_id'], ['a1'], [],
                                                              {'a1': {'query': 'yolo small objects'}})).start()
    assert sent == ['yolo small objects']
    s = next(e for e in out if e['type'] == 'search')
    assert s['query'] == 'yolo small objects' and s['engine'] == 'searxng'
    assert types(out).index('tool_end') < types(out).index('search')


def test_ask_before_skip(monkeypatch, svc):
    sent = mock_http(monkeypatch, lambda r: httpx.Response(200, json={'results': []}))
    run = run_with(monkeypatch, [('', [('web_search', {'query': 'yolo tips'})]), ('OK.', [])], ask_before=True)
    evs = drain(run, decide=lambda ev: ([], ['a1']))
    assert sent == [] and 'confirmed' in types(evs)


def test_three_blocked_turns_web_off(monkeypatch, svc):
    sent = mock_http(monkeypatch, lambda r: httpx.Response(200, json={'results': []}))
    q = {'query': 'night_cam-01 tips'}
    seen = script(monkeypatch, ('', [('web_search', q)]), ('', [('web_search', {'query': 'night cam 01 help'})]),
                  ('', [('web_search', {'query': 'about night-cam-01'})]), ('', [('web_search', {'query': 'yolo'})]),
                  ('Sorry.', []))
    run = start('look it up', role='data', web=1)
    run.ctx.web_cfg = {'level': 1, 'ask_before': False, 'engine': 'auto', 'url': '', 'words': []}
    evs = list(react.events(run))
    blocked = [e for e in evs if e['type'] == 'search']
    assert len(blocked) == 3 and all(e['blocked'] for e in blocked)
    assert blocked[0]['query'] == '[bucket name] tips' and blocked[0]['reasons'] == ['bucket name']
    assert sent == []                                   # nothing left the server, not even the 4th (generic) query
    res = [p['tool_result']['content'] for m in seen[4]['msgs'] if m['role'] == 'tool' for p in m['parts']]
    assert res[0] == 'query contained 1 internal term (bucket name): rephrase it with general words only'
    assert 'night' not in ' '.join(res[:3])
    assert 'off for the rest of this turn' in res[3]


def test_admin_max_level_caps(monkeypatch, svc):
    monkeypatch.setattr(settings, 'get', lambda k: {'llm_org_words': [], 'llm_web_max_level': 1}[k])
    c = ctx(3)
    assert web.level_of(c) == 1
    assert not web_guard.check('forklift v2 detection', c, web.level_of(c))[0]     # technical words blocked at 1
    monkeypatch.setattr(settings, 'get', lambda k: {'llm_org_words': [], 'llm_web_max_level': 0}[k])
    with pytest.raises(tools.ToolError, match='off'):
        web._web_search(ctx(3), {'query': 'yolo'})


def test_native_search_only_at_full(monkeypatch, svc):
    assert start('q', web=3).native_web is True
    assert start('q', web=2).native_web is False and start('q', web=1).native_web is False
    assert 'web_search' not in start('q', web=0).tools
    monkeypatch.setattr(react.providers, 'native_web_ok', lambda p: False, raising=False)
    assert start('q', web=3, provider=OPENAI).native_web is False


def test_confirm_edits_checked():
    cid = confirm.open_(ALICE, 'r1', [{'id': 'a1'}, {'id': 'a2'}])
    with pytest.raises(confirm.AgentError) as e:
        confirm.decide(ALICE, cid, ['a1'], [], {'a9': {'query': 'x'}})
    assert e.value.status == 400
    with pytest.raises(confirm.AgentError):
        confirm.decide(ALICE, cid, ['a1'], [], {'a1': 'not an object'})
    p = confirm._pending[cid]
    confirm.decide(ALICE, cid, ['a1'], [], {'a1': {'query': 'x'}, 'a2': {'query': 'y'}})
    assert p['decision']['edits'] == {'a1': {'query': 'x'}}         # a rejected action's edit is dropped
