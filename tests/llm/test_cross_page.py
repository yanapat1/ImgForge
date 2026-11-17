"""Work across pages in the one ◆ chat: the checklist (todo, with a page per step) is saved on the chat and shown
to every later turn while a step is open; the Visualizer finds the Bucket page folder of its dataset
(data_location, service.locate: local folders, downloaded and streamed S3 links) and the Bucket page finds the
sessions made from a folder (list_sessions bucket + path); sending a folder to labeling is always confirmed."""

import json
import os
from types import SimpleNamespace

import pytest
from conftest import make_user
from sqlalchemy import text
from test_agent_engine import _connect, script, svc  # noqa: F401 - svc is a fixture

from dataviz import db
from dataviz.bucket import service
from dataviz.llm import sessions, tools
from dataviz.llm.engine import react
from dataviz.llm.tools import nav

pytestmark = pytest.mark.usefixtures('tmp_roots')


def _lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x]


def test_plan_section():
    assert react.plan_section([]) == '' and react.plan_section([{'text': 'a', 'state': 'done'}]) == ''
    s = react.plan_section([{'text': 'find the folder', 'state': 'done', 'page': 'bucket'},
                            {'text': 't-SNE map', 'state': 'active', 'page': 'viz'}, {'text': 'explain', 'state': 'pending'}])
    assert s.startswith('## Plan of this chat') and '✓ find the folder (Bucket)' in s
    assert '▶ t-SNE map (Visualizer)' in s and '· explain' in s


def test_the_plan_is_kept_with_the_chat(client, monkeypatch, svc):  # noqa: F811
    _connect(client, ['bucket_data'])
    items = [{'text': 'find the truck folder', 'state': 'done', 'page': 'bucket'},
             {'text': 'map it with t-SNE', 'state': 'active', 'page': 'viz'}]
    seen = script(monkeypatch, ('', [('todo', {'items': items})]), ('Going there.', []), ('On it.', []),
                  ('', [('todo', {'items': [{**i, 'state': 'done'} for i in items]})]), ('All done.', []),
                  ('Hello.', []))
    evs = _lines(client.post('/api/llm/agent/bucket/chat', json={'text': 'visualize trucks with t-SNE'}))
    sid = next(e for e in evs if e['type'] == 'session')['session_id']
    assert sessions.context(client.user, sid)['plan'] == items
    client.post('/api/llm/agent/bucket/chat', json={'text': 'go on', 'session_id': sid})
    assert '## Plan of this chat' in seen[2]['system'] and '▶ map it with t-SNE (Visualizer)' in seen[2]['system']
    client.post('/api/llm/agent/bucket/chat', json={'text': 'finish', 'session_id': sid})
    client.post('/api/llm/agent/bucket/chat', json={'text': 'hi', 'session_id': sid})
    assert '## Plan of this chat' not in seen[5]['system'], 'every step done: no plan in the prompt'
    assert client.get(f'/api/llm/sessions/{sid}').json()['session']['plan'][1]['state'] == 'done'


def _s3_bucket(owner, name, s3_bucket, prefix):
    bid = os.urandom(8).hex()
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.buckets(id, owner_id, name, kind, root, s3_bucket, prefix)
                          VALUES (:id, :o, :n, 's3', :r, :b, :p)"""),
                  {'id': bid, 'o': owner['id'], 'n': name, 'r': f's3://{s3_bucket}/{prefix}', 'b': s3_bucket, 'p': prefix})
    return bid


def test_locate_a_dataset_in_the_bucket(client, tmp_path):
    u = make_user('locator')
    deep = tmp_path / 'datasets' / 'traffic' / 'day' / 'val'
    deep.mkdir(parents=True)
    outer = service.create_bucket(u, 'all data', kind='local', path=str(tmp_path / 'datasets'))
    inner = service.create_bucket(u, 'traffic', kind='local', path=str(tmp_path / 'datasets' / 'traffic'))
    loc = service.locate(u, path=str(deep))
    assert loc == {'bucket': inner['id'], 'bucket_name': 'traffic', 'path': 'day/val', 'parents': ['day', '']}
    assert service.locate(u, path=str(tmp_path / 'datasets'))['bucket'] == outer['id']
    assert service.locate(u, path=str(tmp_path / 'elsewhere')) is None
    assert service.locate(make_user('stranger'), path=str(deep)) is None, "another user's buckets are not seen"
    s3 = _s3_bucket(u, 'cams', 'cam-bucket', 'raw')
    assert service.locate(u, s3_bucket='cam-bucket', prefix='raw/2024/a') == {
        'bucket': s3, 'bucket_name': 'cams', 'path': '2024/a', 'parents': ['2024', '']}
    assert service.locate(u, s3_bucket='cam-bucket', prefix='rawish') is None
    # the folder -> session source match list_sessions uses
    assert service.folder_source(u, inner['id'], 'day/val') == {'path': os.path.realpath(deep)}
    assert service.folder_source(u, s3, '2024') == {'s3_bucket': 'cam-bucket', 'prefix': 'raw/2024'}


class _Datasets:
    def __init__(self, rows):
        self.rows = {r['id']: r for r in rows}

    def dataset(self, did):
        if did not in self.rows:
            raise KeyError(did)
        return self.rows[did]

    def datasets(self, user):
        return list(self.rows.values())


def test_data_location_and_list_sessions_by_folder(client, tmp_path, monkeypatch):
    u = make_user('crosser')
    val = tmp_path / 'ds' / 'trucks' / 'val'
    val.mkdir(parents=True)
    b = service.create_bucket(u, 'ds', kind='local', path=str(tmp_path / 'ds'))
    reg = _Datasets([{'id': 's1', 'kind': 'local', 'path': str(val)},
                     {'id': 's2', 'kind': 'local', 'path': str(tmp_path / 'ds' / 'cars')},
                     {'id': 'u1', 'kind': 'union'}])
    ctx = SimpleNamespace(user=u, app=SimpleNamespace(datasets=reg), state=SimpleNamespace(dataset_id='s1'))
    res, _ = nav._data_location(ctx, {})
    assert res == {'bucket': b['id'], 'bucket_name': 'ds', 'path': 'trucks/val', 'parents': ['trucks', '']}
    with pytest.raises(tools.ToolError, match='union dataset'):
        nav._data_location(SimpleNamespace(user=u, app=ctx.app, state=SimpleNamespace(dataset_id='u1')), {})
    with pytest.raises(tools.ToolError, match='not opened from a saved dataset'):
        nav._data_location(SimpleNamespace(user=u, app=None, state=SimpleNamespace(dataset_id=None)), {})
    monkeypatch.setattr(nav, '_sessions', lambda user, kind, ids=None: [
        {'id': 's1', 'name': 'trucks val'}, {'id': 's2', 'name': 'cars'}] if kind == 'viz' else [])
    res, _ = nav._list_sessions(ctx, {'bucket': 'ds', 'path': 'trucks/val'})
    assert [s['id'] for s in res['sessions']] == ['s1'] and res['sessions'][0]['folder'] == os.path.realpath(val)
    assert nav._list_sessions(ctx, {'bucket': 'ds', 'path': 'trucks'})[0]['total'] == 0
    assert {s['id'] for s in nav._list_sessions(ctx, {})[0]['sessions']} == {'s1', 's2'}


def test_sending_to_labeling_is_always_confirmed():
    t = tools.REGISTRY['send_to_session']
    assert t.risk_of({'target': 'label'}) == 'danger' and t.risk_of({'target': 'visualize'}) == 'change'


def test_navigate_opens_the_tabs_of_every_page(monkeypatch):
    """navigate's tab is checked per screen: a visualizer view, a Data workspace, an Image list; other pages have none."""
    from types import SimpleNamespace
    from dataviz.llm import tools
    from dataviz.llm.tools import data, nav
    monkeypatch.setattr(nav, '_sessions', lambda user, kind, ids=None: [{'id': 'a1', 'name': 'traffic'}])
    ctx = SimpleNamespace(user={'id': 'u'}, roles_on=None)
    ok = lambda **a: nav._check_nav(ctx, {'goal': 'go on', **a})[0]  # noqa: E731
    assert ok(screen='data', tab='dash') == {'screen': 'data', 'goal': 'go on', 'tab': 'dash'}
    assert ok(screen='image', tab='label')['tab'] == 'label'
    assert ok(screen='viz', session_id='a1', tab='aug')['tab'] == 'aug'
    for bad, match in (({'screen': 'data', 'tab': 'aug'}, 'tab of data must be one of query'),
                       ({'screen': 'viz', 'session_id': 'a1', 'tab': 'dash'}, 'tab of viz must be one of explore'),
                       ({'screen': 'home', 'tab': 'x'}, 'Home has no tabs')):
        with pytest.raises(tools.ToolError, match=match):
            ok(**bad)
    assert nav.DATA_TABS == data.WORKSPACES
    # the Data specialist shows a workspace itself (applied in ask mode too), and the other pages are sent to Data for SQL
    t = tools.REGISTRY['open_workspace']
    assert t.risk == 'ui' and t.preview(None, {'workspace': 'query', 'result_tab': 'results'}) == 'Open Query › Results'
    from dataviz.llm.engine import react, roles
    assert 'open_workspace' in react.ALWAYS_APPLY
    assert "`navigate` to data with the question" in roles.other_pages('bucket')
