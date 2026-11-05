"""The per-user repositories (data/connections.py, llm/store.py, models/registry.py) and the routes over them:
secrets encrypted at rest and masked in every response, one user never lists / uses / deletes another's
connections, providers or models, a member of a shared session reads it through the owner's connection without
seeing it, env: secrets are admin-only, local folders stay inside storage.allowed_roots, and the rows the legacy
importer writes are read back correctly."""

import json
import os
import types

import pytest
from sqlalchemy import text

from conftest import make_user

S3_SECRET, S3_ACCESS = 'S3-SECRET-VALUE-123456', 'AKIA-ACCESS-KEY-987'


def s3_body(**kw):
    return {'kind': 's3', 'name': 'store', 'endpoint': 'https://s3.example', 'region': 'eu',
            'access_key': S3_ACCESS, 'secret_key': S3_SECRET, **kw}


def yolo_dir(path):
    for sub in ('images', 'labels'):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def stub_loader(ctx, seen):
    """ctx.open_dataset without a real State: records the source each load is built with."""
    def build(ds, **kw):
        seen.append(ctx.datasets.source(ds, None, ctx.labeling))
        return types.SimpleNamespace(index_job=None, dataset_id=ds['id'], tag=ds['id'], busy=lambda: False,
                                     close=lambda: None)
    ctx.open_dataset = build


@pytest.fixture
def two(client_as, tmp_path):
    """(client of user a, client of user b), both plain users; tmp_path is an allowed root."""
    client_as.ctx.cfg['storage']['allowed_roots'] = [str(tmp_path)]
    return client_as(make_user('alice')), client_as(make_user('bob'))


# ---------------------------------------------------------------- connections
def test_connection_secrets_encrypted_at_rest_and_masked(two):
    from dataviz import db
    a, _ = two
    r = a.post('/api/connections', json=s3_body())
    assert r.status_code == 200, r.text
    assert S3_SECRET not in r.text and S3_ACCESS not in r.text and r.json()['has_secret']
    cid = r.json()['id']
    with db.connect() as c:
        row = c.execute(text('SELECT config, secrets FROM app.connections WHERE id = :i'), {'i': cid}).first()
    assert row.secrets and S3_SECRET.encode() not in bytes(row.secrets) and S3_ACCESS.encode() not in bytes(row.secrets)
    assert S3_SECRET not in json.dumps(row.config) and row.config['endpoint'] == 'https://s3.example'
    listed = a.get('/api/connections')
    assert S3_SECRET not in listed.text and S3_ACCESS not in listed.text
    assert listed.json()['connections'][0]['access_key'].endswith(S3_ACCESS[-4:])
    # a blank secret in an edit keeps the stored one
    assert a.post('/api/connections', json={**s3_body(secret_key='', access_key=''), 'id': cid,
                                            'region': 'us'}).status_code == 200
    saved = a.ctx.datasets.connection(cid)
    assert saved['secret_key'] == S3_SECRET and saved['access_key'] == S3_ACCESS and saved['region'] == 'us'
    # names are unique per owner
    r = a.post('/api/connections', json=s3_body())
    assert r.status_code == 400 and 'already have a connection' in r.json()['detail']


def test_users_never_see_or_use_each_others_connections(two):
    a, b = two
    cid = a.post('/api/connections', json=s3_body()).json()['id']
    assert b.get('/api/connections').json() == {'connections': []}
    assert b.delete(f'/api/connections/{cid}').status_code == 404
    assert b.post('/api/connections', json={**s3_body(), 'id': cid}).status_code == 404
    # testing with A's id and a blank secret never borrows A's secret
    merged = b.ctx.datasets.merged_connection({'id': cid, 'kind': 's3', 'endpoint': 'https://evil'}, b.user['id'])
    assert not merged.get('secret_key') and not merged.get('access_key')
    # a dataset / label session of B cannot read through A's connection
    r = b.post('/api/datasets', json={'kind': 's3', 'connection_id': cid, 'bucket': 'bkt'})
    assert r.status_code == 400 and 'your own' in r.json()['detail']
    r = b.post('/api/label/projects', json={'name': 'L', 'source': {'kind': 's3', 'connection_id': cid, 'bucket': 'b'},
                                            'tasks': [{'name': 'D', 'task_type': 'object_detection', 'classes': ['x']}]})
    assert r.status_code == 400 and 'your own' in r.json()['detail']
    with pytest.raises(KeyError):
        b.ctx.datasets.scoped(b.user['id']).connection(cid)
    assert b.ctx.datasets.scoped(a.user['id']).connection(cid)['secret_key'] == S3_SECRET
    assert a.delete(f'/api/connections/{cid}').json() == {'ok': True}


def test_connection_in_use_cannot_be_deleted(two):
    a, _ = two
    cid = a.post('/api/connections', json=s3_body()).json()['id']
    ds = a.post('/api/datasets', json={'kind': 's3', 'connection_id': cid, 'bucket': 'bkt', 'name': 'Street'}).json()
    r = a.delete(f'/api/connections/{cid}')
    assert r.status_code == 400 and r.json()['detail'] == 'connection is used by: Street'
    assert a.delete(f"/api/datasets/{ds['id']}").json() == {'ok': True}
    assert a.delete(f'/api/connections/{cid}').json() == {'ok': True}


def test_member_reads_the_owners_s3_session_without_seeing_its_connection(two, tmp_path):
    from dataviz.data import sessions
    a, b = two
    cid = a.post('/api/connections', json=s3_body()).json()['id']
    ds = a.post('/api/datasets', json={'kind': 's3', 'connection_id': cid, 'bucket': 'bkt', 'prefix': '/x/y/',
                                       'name': 'Street'}).json()
    loc = a.post('/api/datasets', json={'kind': 'local', 'path': str(yolo_dir(tmp_path / 'local'))}).json()
    assert ds['prefix'] == 'x/y' and ds['my_role'] == 'owner' and ds['owner']['id'] == a.user['id']
    # not shared: B sees nothing, cannot open / tag / delete (404, the id does not leak)
    assert b.get('/api/datasets').json() == {'datasets': []}
    for call in (lambda: b.post(f"/api/datasets/{ds['id']}/open"), lambda: b.delete(f"/api/datasets/{ds['id']}"),
                 lambda: b.put(f"/api/datasets/{ds['id']}/tags", json={'tags': []})):
        assert call().status_code == 404
    for sid in (ds['id'], loc['id']):
        sessions.set_sharing(sid, 'private', [{'user_id': b.user['id'], 'role': 'viewer'}], a.user['id'])
    r = b.get('/api/datasets')
    cards = {d['id']: d for d in r.json()['datasets']}
    assert S3_SECRET not in r.text and S3_ACCESS not in r.text and 'secret_key' not in r.text
    assert cards[ds['id']]['my_role'] == 'viewer' and cards[ds['id']]['connection'] == 'store'
    assert cards[ds['id']]['loaded'] is False and 'active' not in cards[ds['id']]
    # a viewer opens it (built with A's connection), but cannot change it
    seen = []
    stub_loader(b.ctx, seen)
    assert b.post(f"/api/datasets/{ds['id']}/open").status_code == 200
    assert seen[-1].conn['secret_key'] == S3_SECRET and seen[-1].bucket == 'bkt'
    assert b.post(f"/api/datasets/{loc['id']}/open").status_code == 200
    assert seen[-1].kind == 'local'
    card = next(d for d in b.get('/api/datasets').json()['datasets'] if d['id'] == ds['id'])
    assert card['loaded'] is True and card['opened_at']
    assert b.put(f"/api/datasets/{ds['id']}/tags", json={'tags': ['x']}).status_code == 403
    assert b.delete(f"/api/datasets/{ds['id']}").status_code == 403
    assert b.post(f"/api/datasets/{ds['id']}/sync").status_code == 403
    # a viewer cannot edit its source; a stranger's "edit" is a new session of their own
    r = b.post('/api/datasets', json={'id': ds['id'], 'kind': 's3', 'connection_id': cid, 'bucket': 'bkt'})
    assert r.status_code == 403
    sessions.set_sharing(ds['id'], 'private', [], a.user['id'])
    r = b.post('/api/datasets', json={'id': ds['id'], 'kind': 'local', 'path': str(tmp_path / 'local'), 'name': 'Mine'})
    assert r.status_code == 200 and r.json()['id'] != ds['id'] and r.json()['my_role'] == 'owner'
    assert a.ctx.datasets.dataset(ds['id'])['owner_id'] == a.user['id']


def test_providers_and_models_are_private(two):
    from dataviz.models.registry import ModelRegistry
    a, b = two
    form = {'name': 'p', 'kind': 'openai', 'base_url': 'http://x/v1', 'model': 'gpt', 'api_key': 'sk-alice-key-123'}
    p = a.post('/api/llm/providers', json=form).json()['provider']
    assert b.get('/api/llm/config').json()['providers'] == []
    assert b.get('/api/llm/state').json()['provider'] is None
    assert b.post('/api/llm/test', json={'id': p['id']}).status_code == 404
    assert b.post('/api/llm/models', json={'id': p['id']}).status_code == 404
    assert b.delete(f"/api/llm/providers/{p['id']}").status_code == 404
    assert b.post('/api/llm/providers', json={**form, 'id': p['id']}).status_code == 404
    r = b.post('/api/llm/models', json={'form': {**form, 'id': p['id'], 'api_key': ''}})
    assert 'sk-alice-key-123' not in r.text
    assert b.ctx.llm.provider(p['id'], b.user) is None
    assert b.ctx.llm.provider(p['id'], a.user)['api_key'] == 'sk-alice-key-123'
    reg = ModelRegistry(a.ctx.datasets.root)
    m = reg.upsert({'name': 'det', 'family': 'yolo', 'format': 'api',
                    'api': {'url': 'http://m', 'api_key': 'model-key-123456', 'headers': 'X-Token: tok-999'}},
                   owner_id=a.user['id'], admin=False)
    assert reg.models(owner_id=b.user['id']) == [] and [x['id'] for x in reg.models(owner_id=a.user['id'])] == [m['id']]
    for call in (lambda: reg.model(m['id'], owner_id=b.user['id']), lambda: reg.delete(m['id'], owner_id=b.user['id']),
                 lambda: reg.upsert({'id': m['id'], 'family': 'yolo', 'format': 'api', 'api': {'url': 'http://e'}},
                                    owner_id=b.user['id'])):
        with pytest.raises(KeyError):
            call()
    pub = reg.public(reg.model(m['id']))
    assert 'model-key-123456' not in json.dumps(pub) and 'tok-999' not in json.dumps(pub)
    assert pub['api']['has_key'] and pub['api']['headers'] == 'X-Token: ••••'
    # the masked headers sent back keep the stored ones; secrets are encrypted at rest
    reg.upsert({'id': m['id'], 'family': 'yolo', 'format': 'api', 'api': {**pub['api'], 'api_key': ''}},
               owner_id=a.user['id'], admin=False)
    full = reg.model(m['id'], owner_id=a.user['id'])
    assert full['api']['headers'] == 'X-Token: tok-999' and full['api']['api_key'] == 'model-key-123456'
    from dataviz import db
    with db.connect() as c:
        row = c.execute(text('SELECT config, secrets FROM app.models WHERE id = :i'), {'i': m['id']}).first()
    assert b'tok-999' not in bytes(row.secrets) and 'tok-999' not in json.dumps(row.config)
    reg.delete(m['id'], owner_id=a.user['id'])
    assert reg.models() == []


def test_env_secrets_are_admin_only(client_as, monkeypatch):
    from dataviz.models.registry import ModelRegistry
    monkeypatch.setenv('SERVER_SECRET', 'do-not-leak')
    user, admin = client_as(make_user('mallory')), client_as(make_user('root', admin=True))
    form = {'name': 'p', 'kind': 'openai', 'base_url': 'http://evil/v1', 'model': 'gpt', 'api_key': 'env:SERVER_SECRET'}
    for path, body in (('/api/llm/providers', form), ('/api/llm/test', {'form': form}),
                       ('/api/llm/models', {'form': form})):
        r = user.post(path, json=body)
        assert r.status_code == 400 and 'only admins' in r.json()['detail'], path
    p = admin.post('/api/llm/providers', json=form).json()['provider']
    assert p['key_env'] and admin.ctx.llm.provider(p['id'], admin.user)['api_key'] == 'env:SERVER_SECRET'
    # a provider saved while its owner was an admin is never resolved once they are not
    demoted = {**admin.user, 'is_admin': False}
    assert admin.ctx.llm.provider(p['id'], demoted)['api_key'] == ''
    reg = ModelRegistry(admin.ctx.datasets.root)
    api = {'name': 'm', 'family': 'yolo', 'format': 'api', 'api': {'url': 'http://evil', 'api_key': 'env:SERVER_SECRET'}}
    with pytest.raises(ValueError, match='only admins'):
        reg.upsert(api, owner_id=user.user['id'], admin=False)
    with pytest.raises(ValueError, match='only admins'):
        reg.upsert({**api, 'api': {'url': 'http://evil', 'headers': 'X: env:SERVER_SECRET'}}, owner_id=user.user['id'],
                   admin=False)
    assert reg.upsert(api, owner_id=admin.user['id'], admin=True)['api']['api_key'] == 'env:SERVER_SECRET'


# ---------------------------------------------------------------- allowed roots
def test_check_local_resolves_and_refuses_escapes(tmp_path):
    from dataviz.data import connections
    root = tmp_path / 'allowed'
    (root / 'ds').mkdir(parents=True)
    (tmp_path / 'secret').mkdir()
    os.symlink(tmp_path / 'secret', root / 'link')
    roots = [root.resolve()]
    assert connections.check_local(str(root / 'ds'), roots) == str((root / 'ds').resolve())
    assert connections.check_local(str(root), roots) == str(root.resolve())
    for bad in (str(tmp_path / 'secret'), str(root / '..' / 'secret'), str(root / 'link'), str(root / 'link' / 'x'), '/'):
        with pytest.raises(ValueError, match='allowed_roots'):
            connections.check_local(bad, roots)
    with pytest.raises(ValueError):
        connections.check_local('', roots)


def test_routes_keep_local_paths_inside_the_allowed_roots(client_as, tmp_path):
    from dataviz.data.connections import OUTSIDE
    root = tmp_path / 'allowed'
    yolo_dir(root / 'ds')
    yolo_dir(tmp_path / 'outside')
    os.symlink(tmp_path / 'outside', root / 'link')
    client_as.ctx.cfg['storage']['allowed_roots'] = [str(root)]
    c = client_as(make_user('carol', admin=True))           # admins too
    for path in (tmp_path / 'outside', root / 'link', root / '..' / 'outside'):
        r = c.post('/api/datasets', json={'kind': 'local', 'path': str(path)})
        assert r.status_code == 400 and r.json()['detail'] == OUTSIDE, path
        assert c.get('/api/fs/browse', params={'path': str(path)}).status_code == 400
    assert c.post('/api/datasets', json={'kind': 'local', 'path': str(root / 'ds')}).status_code == 200
    br = c.get('/api/fs/browse').json()
    assert br['path'] == str(root.resolve()) and br['parent'] is None and br['roots'] == [str(root.resolve())]
    assert {f['name'] for f in br['folders']} >= {'ds', 'link'}
    assert c.get('/api/fs/browse', params={'path': str(root / 'ds')}).json()['parent'] == str(root.resolve())
    tasks = [{'name': 'D', 'task_type': 'object_detection', 'classes': ['x']}]
    r = c.post('/api/label/projects', json={'name': 'L', 'source': {'kind': 'local', 'path': str(tmp_path / 'outside')},
                                            'tasks': tasks})
    assert r.status_code == 400 and r.json()['detail'] == OUTSIDE
    pid = c.post('/api/label/projects', json={'name': 'U', 'tasks': tasks}).json()['project']['id']
    r = c.post(f'/api/label/projects/{pid}/export', json={'target': {'kind': 'local', 'path': str(tmp_path / 'out')}})
    assert r.status_code == 400 and r.json()['detail'] == OUTSIDE
    r = c.post('/api/label/annotations/preview', json={'source': {'kind': 'local', 'path': str(tmp_path / 'outside')}})
    assert r.status_code == 400 and r.json()['detail'] == OUTSIDE


# ---------------------------------------------------------------- legacy rows
def test_rows_written_by_the_legacy_importer_are_read_back(pg, tmp_path):
    from dataviz.data import connections
    from dataviz.db import legacy
    from dataviz.llm.store import LLMStore
    from dataviz.models.registry import ModelRegistry
    admin = make_user('boss', admin=True)
    root = tmp_path / 'ws'
    local = yolo_dir(tmp_path / 'local_ds')
    root.mkdir()
    (root / 'connections.json').write_text(json.dumps({
        'connections': [{'id': 'cs3', 'endpoint': 'https://s3.example', 'region': 'eu', 'access_key': 'AKIAOLD',
                         'secret_key': 'S3SECRETVALUE', 'verify_ssl': False},
                        {'id': 'cmod', 'kind': 'model', 'name': 'Srv', 'url': 'http://m:8000', 'api_key': 'MODELKEY123',
                         'timeout_s': 30}],
        'datasets': [{'id': 'd1', 'name': 'Street', 'kind': 's3', 'connection_id': 'cs3', 'bucket': 'bkt',
                      'prefix': 'a/b', 'mode': 'cache', 'created': 1_700_000_000.0, 'tags': ['prod']},
                     {'id': 'd4', 'name': 'Local', 'kind': 'local', 'path': str(local.resolve())}],
        'active': 'd1'}))
    (root / 'llm.json').write_text(json.dumps({
        'providers': [{'id': 'p1', 'name': 'GPT', 'kind': 'openai', 'base_url': 'http://x/v1', 'model': 'gpt-x',
                       'api_key': 'sk-old-key-123', 'vision': True, 'verified_at': 1.0}],
        'active': 'p1', 'features': {'assistant': True}, 'budget': {'max_input_tokens': 20000}}))
    (root / 'models.json').write_text(json.dumps({'models': [
        {'id': 'm1', 'name': 'API det', 'family': 'custom', 'format': 'api', 'task_type': 'object_detection',
         'api': {'preset': 'generic', 'url': 'http://api', 'api_key': 'MKEY-123456', 'headers': 'X-A: b'},
         'created': '2024-01-01 00:00:00'}]}))
    summary = legacy.import_legacy(root, admin['id'], log=lambda *a: None)
    assert (summary['connections'], summary['viz_sessions'], summary['models'], summary['llm_providers']) == (2, 2, 1, 1)

    reg = connections.Registry(root)
    conns = {c['id']: c for c in reg.connections(admin['id'])}
    assert conns['cs3']['kind'] == 's3' and conns['cs3']['secret_key'] == 'S3SECRETVALUE'
    assert conns['cs3']['access_key'] == 'AKIAOLD' and conns['cs3']['verify_ssl'] is False
    assert conns['cs3']['name'] == 'https://s3.example'
    pub = reg.public_connection(conns['cmod'])
    assert pub == {'id': 'cmod', 'kind': 'model', 'name': 'Srv', 'url': 'http://m:8000', 'has_secret': True,
                   'timeout_s': 30}
    ds = {d['id']: d for d in reg.datasets(admin)}
    assert ds['d1']['kind'] == 's3' and ds['d1']['connection_id'] == 'cs3' and ds['d1']['mode'] == 'cache'
    assert ds['d1']['tags'] == ['prod'] and ds['d1']['created'] == 1_700_000_000.0 and ds['d1']['my_role'] == 'owner'
    assert ds['d4']['path'] == str(local.resolve())
    src = reg.source(reg.dataset('d1'))
    assert src.conn['secret_key'] == 'S3SECRETVALUE' and src.bucket == 'bkt'
    assert reg.find_local(local, admin['id'])['id'] == 'd4'

    data = LLMStore().load(admin)
    assert data['active'] == 'p1' and data['features']['assistant'] and data['budget'] == {'max_input_tokens': 20000}
    p = data['providers'][0]
    assert p['api_key'] == 'sk-old-key-123' and p['model'] == 'gpt-x' and p['kind'] == 'openai' and p['name'] == 'GPT'
    assert LLMStore().state(admin)['connected']

    m = ModelRegistry(root).model('m1', owner_id=admin['id'])
    assert m['api']['api_key'] == 'MKEY-123456' and m['api']['headers'] == 'X-A: b' and m['api']['url'] == 'http://api'
    assert m['name'] == 'API det' and m['format'] == 'api' and m['created'] == '2024-01-01 00:00:00'
