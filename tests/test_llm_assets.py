"""Settings › Prompts & Skills (llm/assets.py, llm/tools/http.py, server/routes/assets.py): per-user prompts and
skills (isolation, limits), plugin manifests (validation, namespaced tool names, zip packages, encrypted write-only
secrets), the HTTP tools (a local server, the SSRF guard, confirm for change tools in the engine), for_run's role
filtering, and the web search settings capped by the admins' maximum."""

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import text

from conftest import make_user
from dataviz.llm import assets, providers
from dataviz.llm.engine import confirm, react
from dataviz.llm.tools import ToolError
from dataviz.llm.tools import http as ptools


def manifest(**kw):
    m = {'name': 'acme', 'version': '1.0', 'description': 'Acme tracker',
         'secrets': ['TOKEN'],
         'skills': [{'name': 'file-a-bug', 'description': 'How to file a bug', 'when': 'the user reports a bug',
                     'body': '1. search\n2. create', 'role': 'data'}],
         'prompts': [{'name': 'house style', 'role': 'visual', 'body': 'Answer in British English.'}],
         'tools': [{'name': 'search', 'description': 'Search issues', 'risk': 'read', 'roles': ['data', 'visual'],
                    'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}, 'n': {'type': 'integer'}},
                                   'required': ['q']},
                    'http': {'method': 'GET', 'url': 'https://api.acme.test/issues/{{q}}',
                             'query': {'limit': '{{n}}', 'fixed': 'yes'},
                             'headers': {'Authorization': 'Bearer ${secret:TOKEN}'}}},
                   {'name': 'acme__create', 'description': 'Create an issue', 'risk': 'change', 'roles': ['data'],
                    'parameters': {'type': 'object', 'properties': {'title': {'type': 'string'},
                                                                    'labels': {'type': 'array', 'items': {'type': 'string'}}},
                                   'required': ['title']},
                    'http': {'method': 'POST', 'url': 'https://api.acme.test/issues',
                             'headers': {'X-Key': '${secret:TOKEN}'},
                             'body': {'title': '{{title}}', 'labels': '{{labels}}', 'note': 'from {{title}}'}}}]}
    m.update(kw)
    return m


@pytest.fixture
def users(pg):
    return make_user('alice'), make_user('bob'), make_user('root', admin=True)


# ---------------------------------------------------------------- prompts and skills
def test_items_crud_and_isolation(users):
    alice, bob, _ = users
    p = assets.create_item(alice, {'kind': 'prompt', 'role': 'data', 'name': 'Short', 'body': 'Be brief.'})
    s = assets.create_item(alice, {'kind': 'skill', 'role': 'all', 'name': 'triage', 'description': 'Sort the inbox',
                                   'when': 'asked to triage', 'body': 'step 1'})
    assert s['when'] == 'asked to triage' and s['body'] == 'step 1' and s['enabled']
    assert {i['id'] for i in assets.list_items(alice)} == {p['id'], s['id']}
    assert assets.list_items(bob) == []
    with pytest.raises(assets.NotFound):
        assets.update_item(bob, p['id'], {'body': 'hacked'})
    with pytest.raises(assets.NotFound):
        assets.delete_item(bob, p['id'])
    u = assets.update_item(alice, s['id'], {'enabled': False, 'when': 'never mind'})
    assert not u['enabled'] and u['when'] == 'never mind' and u['body'] == 'step 1'
    with pytest.raises(assets.AssetError, match='kind'):
        assets.update_item(alice, s['id'], {'kind': 'prompt'})
    assets.delete_item(alice, p['id'])
    assert [i['id'] for i in assets.list_items(alice)] == [s['id']]


def test_item_validation_and_limits(users, monkeypatch):
    alice = users[0]
    bad = [({'kind': 'x', 'name': 'a', 'body': 'b'}, 'kind'), ({'kind': 'prompt', 'role': 'all', 'name': '', 'body': 'b'}, 'name'),
           ({'kind': 'prompt', 'name': 'n' * 121, 'body': 'b'}, '120'),
           ({'kind': 'prompt', 'name': 'a', 'body': 'b' * 20001}, '20000'),
           ({'kind': 'prompt', 'name': 'a', 'role': 'both', 'body': 'b'}, 'role'),
           ({'kind': 'skill', 'name': 'a', 'body': 'b'}, 'description')]
    for body, msg in bad:
        with pytest.raises(assets.AssetError, match=msg):
            assets.create_item(alice, body)
    assets.create_item(alice, {'kind': 'prompt', 'name': 'ok', 'body': 'b' * 20000})
    monkeypatch.setattr(assets, 'MAX_ITEMS', 2)
    assets.create_item(alice, {'kind': 'prompt', 'name': 'two', 'body': 'b'})
    with pytest.raises(assets.AssetError, match='already have 2 prompts'):
        assets.create_item(alice, {'kind': 'prompt', 'name': 'three', 'body': 'b'})
    assets.create_item(alice, {'kind': 'skill', 'name': 's', 'description': 'd', 'body': 'b'})    # its own limit


# ---------------------------------------------------------------- plugins
def test_manifest_validation_errors(users):
    alice, _, root = users
    cases = [
        (manifest(name='Acme'), 'lowercase'),
        (manifest(name='a__b'), 'lowercase'),
        (manifest(tools=[{**manifest()['tools'][0], 'name': 'Search'}]), 'tool names'),
        (manifest(tools=[manifest()['tools'][0]] * 2), 'used twice'),
        (manifest(tools=[{**manifest()['tools'][0], 'name': f't{i}'} for i in range(21)]), 'at most 20 tools'),
        (manifest(tools=[{**manifest()['tools'][0], 'risk': 'nuke'}]), 'risk'),
        (manifest(tools=[{**manifest()['tools'][0], 'roles': ['admin']}]), 'roles'),
        (manifest(tools=[{**manifest()['tools'][0], 'parameters': {'type': 'array'}}]), 'JSON schema object'),
        (manifest(tools=[{**manifest()['tools'][0], 'http': {'method': 'TRACE', 'url': 'https://x.test'}}]), 'method'),
        (manifest(tools=[{**manifest()['tools'][0], 'http': {'url': 'http://x.test/a'}}]), 'https'),
        (manifest(tools=[{**manifest()['tools'][0], 'http': {'url': 'https://{{q}}.test/a'}}]), 'host'),
        (manifest(tools=[{**manifest()['tools'][0], 'http': {'url': 'https://x.test/{{nope}}'}}]), 'nope'),
        (manifest(tools=[{**manifest()['tools'][0], 'http': {'url': 'https://x.test/${secret:TOKEN}'}}]), 'not in the URL'),
        (manifest(secrets=[]), 'not declared'),
        (manifest(skills=[{'name': 's', 'body': 'b'}]), 'description'),
        ('{not json', 'valid JSON'),
    ]
    for m, msg in cases:
        with pytest.raises(assets.AssetError, match=msg):
            assets.install(alice, m)
    assert assets.list_plugins(alice) == []
    # plain http is fine for an admin
    t = {**manifest()['tools'][0], 'http': {'url': 'http://intranet.test/a'}}
    assert assets.install(root, manifest(tools=[t]))['tools'][0]['url'] == 'http://intranet.test/a'


def test_install_names_secrets_and_isolation(users, pg):
    alice, bob, _ = users
    p = assets.install(alice, manifest())
    assert [t['name'] for t in p['tools']] == ['acme__search', 'acme__create']
    assert p['secrets'] == [{'name': 'TOKEN', 'set': False}]
    assert [s['name'] for s in p['skills']] == ['file-a-bug'] and [x['name'] for x in p['prompts']] == ['house style']
    assert assets.list_items(alice) == []                     # plugin rows are not "my" items
    assert assets.list_plugins(bob) == []
    with pytest.raises(assets.NotFound):
        assets.update_plugin(bob, p['id'], {'enabled': False})
    with pytest.raises(assets.AssetError, match='not a secret'):
        assets.update_plugin(alice, p['id'], {'secrets': {'OTHER': 'x'}})
    p = assets.update_plugin(alice, p['id'], {'secrets': {'TOKEN': 'tok-SECRET-123'}})
    assert p['secrets'] == [{'name': 'TOKEN', 'set': True}] and 'tok-SECRET-123' not in json.dumps(p)
    assert 'tok-SECRET-123' not in json.dumps(assets.list_plugins(alice))
    from dataviz import db
    with db.connect() as c:
        raw = c.execute(text('SELECT secrets FROM app.llm_plugins WHERE id = :i'), {'i': p['id']}).scalar()
    assert raw and b'tok-SECRET-123' not in bytes(raw)
    assert assets.plugin_secrets(alice, p['id']) == {'TOKEN': 'tok-SECRET-123'}
    assert assets.plugin_secrets(bob, p['id']) == {}
    # upgrade by name: same id, secrets kept, rows replaced
    p2 = assets.install(alice, manifest(version='2.0', skills=[]))
    assert p2['id'] == p['id'] and p2['version'] == '2.0' and p2['skills'] == [] and p2['secrets'][0]['set']
    # the same plugin name for another user is fine
    assert assets.install(bob, manifest())['id'] != p['id']
    assets.uninstall(alice, p['id'])
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM app.llm_user_prompts WHERE plugin_id = :i'), {'i': p['id']}).scalar() == 0
    with pytest.raises(assets.NotFound):
        assets.uninstall(alice, p['id'])


def test_zip_package(users):
    alice = users[0]
    buf = io.BytesIO()
    m = manifest(skills=[])
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('acme/plugin.json', json.dumps(m))
        z.writestr('acme/skills/close-bug/SKILL.md', '---\nname: close-bug\ndescription: Close a bug\nwhen: done\n'
                                                     'role: data\n---\nSteps to close.\n')
    p = assets.install(alice, buf.getvalue())
    assert [s['name'] for s in p['skills']] == ['close-bug']
    sk = assets.for_run(alice, 'data')['skills']
    assert sk == [{'name': 'close-bug', 'description': 'Close a bug', 'when': 'done', 'body': 'Steps to close.',
                   'source': 'plugin acme'}]
    with pytest.raises(assets.AssetError, match='plugin.json'):
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, 'w') as z:
            z.writestr('readme.txt', 'x')
        assets.install(alice, buf2.getvalue())


def test_for_run_filters_roles_and_switches(users):
    alice, bob, _ = users
    assets.create_item(alice, {'kind': 'prompt', 'role': 'data', 'name': 'D', 'body': 'data only'})
    assets.create_item(alice, {'kind': 'prompt', 'role': 'all', 'name': 'A', 'body': 'both'})
    off = assets.create_item(alice, {'kind': 'prompt', 'role': 'all', 'name': 'Off', 'body': 'x', 'enabled': False})
    assets.create_item(alice, {'kind': 'skill', 'role': 'visual', 'name': 'V', 'description': 'vis', 'body': 'v'})
    p = assets.install(alice, manifest())
    d, v = assets.for_run(alice, 'data'), assets.for_run(alice, 'visual')
    assert {x['name'] for x in d['prompts']} == {'D', 'A'} and off['name'] not in {x['name'] for x in v['prompts']}
    assert {x['name'] for x in v['prompts']} == {'A', 'house style'}
    assert {x['name'] for x in d['skills']} == {'file-a-bug'} and {x['name'] for x in v['skills']} == {'V'}
    assert {t.name for t in d['tools']} == {'acme__search', 'acme__create'}
    assert {t.name for t in v['tools']} == {'acme__search'}
    t = next(t for t in d['tools'] if t.name == 'acme__create')
    assert t.domain == 'plugin' and t.ctx and t.risk == 'change' and t.roles == ('data',)
    assets.update_plugin(alice, p['id'], {'enabled': False})
    d = assets.for_run(alice, 'data')
    assert d['tools'] == [] and {x['name'] for x in d['skills']} == set()
    assert assets.for_run(bob, 'data') == {'prompts': [], 'skills': [], 'tools': []}


# ---------------------------------------------------------------- HTTP tools
class _Fake(BaseHTTPRequestHandler):
    seen = []

    def _answer(self):
        n = int(self.headers.get('content-length') or 0)
        body = self.rfile.read(n).decode() if n else ''
        _Fake.seen.append({'method': self.command, 'path': self.path, 'headers': dict(self.headers), 'body': body})
        if self.path.startswith('/big'):
            out = b'x' * (3 << 20)
            ctype = 'text/plain'
        elif self.path.startswith('/fail'):
            out, ctype = b'{"message": "nope"}', 'application/json'
            self.send_response(404)
            self.send_header('content-type', ctype)
            self.end_headers()
            self.wfile.write(out)
            return
        else:
            out, ctype = json.dumps({'ok': True, 'path': self.path, 'items': list(range(5))}).encode(), 'application/json'
        self.send_response(200)
        self.send_header('content-type', ctype)
        self.send_header('content-length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_server():
    _Fake.seen = []
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Fake)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f'http://127.0.0.1:{srv.server_address[1]}'
    srv.shutdown()
    srv.server_close()


def _local_tools(base):
    return [{'name': 'get', 'description': 'get', 'risk': 'read',
             'parameters': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'n': {'type': 'integer'}}},
             'http': {'method': 'GET', 'url': base + '/items/{{id}}', 'query': {'n': '{{n}}'},
                      'headers': {'Authorization': 'Bearer ${secret:TOKEN}'}}},
            {'name': 'big', 'description': 'big', 'parameters': {'type': 'object', 'properties': {}},
             'http': {'method': 'GET', 'url': base + '/big'}},
            {'name': 'fail', 'description': 'fail', 'parameters': {'type': 'object', 'properties': {}},
             'http': {'method': 'GET', 'url': base + '/fail'}},
            {'name': 'post', 'description': 'post', 'risk': 'change',
             'parameters': {'type': 'object', 'properties': {'title': {'type': 'string'}, 'tags': {'type': 'array'}}},
             'http': {'method': 'POST', 'url': base + '/new', 'body': {'t': '{{title}}', 'tags': '{{tags}}', 'k': '${secret:TOKEN}'}}}]


def test_http_tool_against_a_local_server(users, fake_server):
    root = users[2]
    p = assets.install(root, manifest(tools=_local_tools(fake_server)))
    assets.update_plugin(root, p['id'], {'secrets': {'TOKEN': 'abc-123'}})
    ts = {t.name: t for t in assets.for_run(root, 'data')['tools']}
    from dataviz.llm import tools
    out, summary = tools.run(object(), 'acme__get', {'id': 'a b/c', 'n': 3}, ts)
    assert out['status'] == 200 and out['json']['ok'] and summary.startswith('→ HTTP 200')
    req = _Fake.seen[-1]
    assert req['path'] == '/items/a%20b%2Fc?n=3' and req['headers']['Authorization'] == 'Bearer abc-123'
    out, _ = tools.run(object(), 'acme__big', {}, ts)
    assert len(out['text']) <= ptools.MAX_OUT + 10 and out['text'].endswith('(cut)')
    with pytest.raises(ToolError, match='answered 404'):
        tools.run(object(), 'acme__fail', {}, ts)
    tools.run(object(), 'acme__post', {'title': 'Bug', 'tags': ['a', 'b']}, ts)
    assert json.loads(_Fake.seen[-1]['body']) == {'t': 'Bug', 'tags': ['a', 'b'], 'k': 'abc-123'}
    with pytest.raises(ToolError, match='must be'):
        tools.run(object(), 'acme__get', {'n': 'many'}, ts)


def test_missing_secret_and_private_hosts(users, monkeypatch):
    alice = users[0]
    from dataviz.datacenter.sources import fetch
    p = assets.install(alice, manifest())
    ts = {t.name: t for t in assets.for_run(alice, 'data')['tools']}
    from dataviz.llm import tools
    with pytest.raises(ToolError, match='secret TOKEN is not set'):
        tools.run(object(), 'acme__search', {'q': 'x'}, ts)
    assets.update_plugin(alice, p['id'], {'secrets': {'TOKEN': 'tok-SECRET-123'}})
    ts = {t.name: t for t in assets.for_run(alice, 'data')['tools']}
    monkeypatch.setattr(fetch, 'resolve_host', lambda h: ['127.0.0.1'])       # a public-looking name on loopback
    with pytest.raises(ToolError, match='private'):
        tools.run(object(), 'acme__search', {'q': 'x'}, ts)
    # a public host: the request goes out through the patched client, the secret only in its header
    monkeypatch.setattr(fetch, 'resolve_host', lambda h: ['93.184.216.34'])
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={'hits': 1})
    monkeypatch.setattr(ptools, '_client', lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    out, _ = tools.run(object(), 'acme__search', {'q': 'x y', 'n': 2}, ts)
    assert out == {'status': 200, 'json': {'hits': 1}}
    u = sent[0].url
    assert (u.scheme, u.host, u.raw_path.split(b'?')[0]) == ('https', 'api.acme.test', b'/issues/x%20y')
    assert dict(u.params) == {'limit': '2', 'fixed': 'yes'}
    assert sent[0].headers['authorization'] == 'Bearer tok-SECRET-123'


def test_change_plugin_tool_goes_through_confirm(users, monkeypatch):
    alice = users[0]
    from dataviz.datacenter.sources import fetch
    from test_llm_tools import OPENAI
    p = assets.install(alice, manifest())
    assets.update_plugin(alice, p['id'], {'secrets': {'TOKEN': 't0k3n'}})
    monkeypatch.setattr(fetch, 'resolve_host', lambda h: ['93.184.216.34'])
    sent = []
    monkeypatch.setattr(ptools, '_client', lambda: httpx.Client(transport=httpx.MockTransport(
        lambda req: sent.append(req) or httpx.Response(201, json={'id': 7}))))
    steps = iter([('', [('acme__create', {'title': 'Crash'})]), ('Filed #7.', [])])

    def stream_tools(p, system, msgs, specs, think=None, native_web=False, max_tokens=None):
        assert 'acme__create' in {s['name'] for s in specs}
        text_, calls = next(steps)
        if text_:
            yield 'text', text_
        for n, a in calls:
            yield 'tool_call', {'id': '', 'name': n, 'args': a}
        yield 'usage', {'in': 1, 'out': 1}
    monkeypatch.setattr(providers, 'stream_tools', stream_tools)
    a = assets.for_run(alice, 'data')
    opts = react.options({}, OPENAI)
    run = react.prepare(alice, 'data', OPENAI, react.history([{'role': 'user', 'text': 'file a bug'}]), {}, opts,
                        user_prompts=a['prompts'], user_skills=a['skills'], plugin_tools=a['tools'])
    evs = []
    for ev in react.events(run):
        evs.append(ev)
        if ev['type'] == 'confirm':
            assert not sent                                    # nothing ran before the user approved
            act = ev['actions'][0]
            assert act['tool'] == 'acme__create' and act['risk'] == 'change' and 'POST api.acme.test/issues' in act['summary']
            threading.Timer(0.05, lambda e=ev: confirm.decide(alice, e['confirm_id'], ['a1'], [])).start()
    assert len(sent) == 1 and json.loads(sent[0].content) == {'title': 'Crash', 'note': 'from Crash'}
    assert sent[0].headers['x-key'] == 't0k3n'
    assert [e['type'] for e in evs if e['type'] not in ('step', 'ping')][-1] == 'done'
    # the visual agent is never offered a data-only plugin tool
    v = assets.for_run(alice, 'visual')
    run = react.prepare(alice, 'visual', OPENAI, react.history([{'role': 'user', 'text': 'x'}]), {}, opts,
                        plugin_tools=a['tools'] + v['tools'])
    assert 'acme__create' not in run.tools and 'acme__search' in run.tools


# ---------------------------------------------------------------- routes
def test_routes(client_as, users):
    alice, bob, _ = users
    ca, cb = client_as(alice), client_as(bob)
    r = ca.post('/api/llm/prompts', json={'kind': 'prompt', 'role': 'all', 'name': 'x', 'body': 'y'})
    assert r.status_code == 200, r.text
    iid = r.json()['item']['id']
    assert [i['id'] for i in ca.get('/api/llm/prompts').json()['items']] == [iid]
    assert cb.get('/api/llm/prompts').json()['items'] == []
    assert cb.put(f'/api/llm/prompts/{iid}', json={'body': 'z'}).status_code == 404
    assert ca.put(f'/api/llm/prompts/{iid}', json={'body': 'z'}).json()['item']['body'] == 'z'
    assert ca.post('/api/llm/prompts', json={'kind': 'prompt', 'name': '', 'body': 'y'}).status_code == 400
    assert ca.delete(f'/api/llm/prompts/{iid}').status_code == 200
    # plugins: JSON object, JSON text, multipart file
    r = ca.post('/api/llm/plugins', json={'manifest': manifest()})
    assert r.status_code == 200, r.text
    pid = r.json()['plugin']['id']
    assert ca.post('/api/llm/plugins', json={'manifest': json.dumps(manifest(name='beta', tools=[]))}).status_code == 200
    files = {'file': ('plugin.json', json.dumps(manifest(name='gamma', tools=[])).encode(), 'application/json')}
    assert ca.post('/api/llm/plugins', files=files).status_code == 200
    assert ca.post('/api/llm/plugins', json={'manifest': manifest(name='Bad')}).status_code == 400
    assert {p['name'] for p in ca.get('/api/llm/plugins').json()['plugins']} == {'acme', 'beta', 'gamma'}
    r = ca.put(f'/api/llm/plugins/{pid}', json={'enabled': False, 'secrets': {'TOKEN': 'sss-very-secret'}})
    assert r.status_code == 200 and not r.json()['plugin']['enabled'] and 'sss-very-secret' not in r.text
    assert cb.put(f'/api/llm/plugins/{pid}', json={'enabled': True}).status_code == 404
    assert cb.delete(f'/api/llm/plugins/{pid}').status_code == 404
    assert ca.delete(f'/api/llm/plugins/{pid}').status_code == 200


def test_web_settings_capped_by_admin(client_as, users):
    from dataviz.auth import settings
    alice = users[0]
    c = client_as(alice)
    r = c.put('/api/llm/web', json={'level': 3, 'engine': 'brave', 'ask_before': True,
                                    'words': [{'text': 'Project Falcon', 'strength': 'never'}, 'night_cam']})
    assert r.status_code == 200, r.text
    w = r.json()
    assert w['web']['level'] == 3 and w['web']['engine'] == 'brave' and w['web']['ask_before']
    assert w['web']['words'] == [{'text': 'Project Falcon', 'strength': 'never'}, {'text': 'night_cam', 'strength': 'private'}]
    settings.update({'llm_web_max_level': 2, 'llm_org_words': ['acme-internal']})
    assert c.get('/api/llm/web').json()['web']['level'] == 2              # shown capped
    w = c.put('/api/llm/web', json={'level': 3}).json()
    assert w['web']['level'] == 2 and w['max_level'] == 2 and w['org_words'] == 1
    assert client_as.ctx.llm.load(alice)['web']['level'] == 2
    assert c.put('/api/llm/web', json={'level': 'high'}).status_code == 400
    assert c.put('/api/llm/web/key', json={'api_key': 'brave-key-1'}).json() == {'has_key': True}
    assert c.get('/api/llm/web').json()['has_key'] and 'brave-key-1' not in c.get('/api/llm/web').text
    assert client_as.ctx.llm.web_key(alice) == 'brave-key-1'
    assert c.put('/api/llm/web/key', json={'api_key': ''}).json() == {'has_key': False}
    try:
        import dataviz.llm.tools.web  # noqa: F401
    except ImportError:
        assert c.post('/api/llm/web/test', json={}).status_code == 501
