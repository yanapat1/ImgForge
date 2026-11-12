"""consult (tools/nav.py): from one page, the other page's specialist answers one question on the server with its
read-only tools: its tools never enter this prompt, its steps are not streamed (one `consult` event), only its
answer comes back (cut), its tokens count for the turn; no consult of a consult, not of the page the user is on,
not of a page whose assistant is off or a session the user cannot open."""

import json
from types import SimpleNamespace

import pytest
from test_agent_engine import script, svc  # noqa: F401 - svc is a fixture
from test_llm_tools import ALICE, OPENAI
from test_visual_agent import session  # noqa: F401 - session is a fixture

from dataviz.llm import tools
from dataviz.llm.engine import react
from dataviz.llm.tools import nav

VIZ = [{'id': 'a1', 'name': 'traffic'}]


def _data_run(text, app, mode='ask'):
    return react.prepare(ALICE, 'bucket', OPENAI, react.history([{'role': 'user', 'text': text}]), {}, react.options({'mode': mode}, OPENAI),
                         app=app)


def test_consult_the_visualizer_from_the_bucket(monkeypatch, session):  # noqa: F811
    st, _ = session
    monkeypatch.setattr(nav, '_sessions', lambda user, kind, ids=None: VIZ if kind == 'viz' and ids == ['a1'] else [])
    app = SimpleNamespace(state_for=lambda sid, user: st)
    seen = script(monkeypatch,
                  ('', [('consult', {'page': 'viz', 'session_id': 'a1', 'question': 'How many boxes per class?'})]),
                  ('', [('dataset_summary', {})]),                  # the Visual specialist, on the server
                  ('The dataset has 13 boxes in 8 classes.', []),
                  ('In traffic: 13 boxes, 8 classes.', []))
    run = _data_run('how many boxes in session traffic?', app, mode='plan')      # read-only: plan mode allows it
    assert 'consult' in run.tools and 'dataset_summary' not in run.tools
    evs = list(react.events(run))
    sub_specs = {s['name'] for s in seen[1]['specs']}
    assert 'dataset_summary' in sub_specs and not sub_specs & {'set_projection', 'run_model', 'navigate', 'consult',
                                                               'list_buckets', 'submit_plan', 'todo'}
    assert 'Asked by the assistant on the Bucket page' in seen[1]['msgs'][0]['parts'][0]['text']
    res = seen[3]['msgs'][-1]['parts'][0]['tool_result']
    out = json.loads(res['content'])
    assert out['answer'] == 'The dataset has 13 boxes in 8 classes.' and out['tools_used'] == ['dataset_summary']
    types = [e['type'] for e in evs]
    assert 'consult' in types and types.count('step') == 2, "the specialist's own steps are not streamed"
    c = next(e for e in evs if e['type'] == 'consult')
    assert c == {'type': 'consult', 'page': 'viz', 'steps': 2, 'tools': ['dataset_summary'], 'ok': True}
    assert evs[-1]['usage'] == {'in': 40, 'out': 8}, 'the consult tokens count for the turn'


def test_consult_the_bucket_from_the_visualizer(monkeypatch, session, svc):  # noqa: F811
    st, _ = session
    seen = script(monkeypatch, ('', [('consult', {'page': 'bucket', 'question': 'How many images in bucket cars?'})]),
                  ('', [('list_buckets', {})]), ('x' * 3000, []), ('Cars has images.', []))
    run = react.prepare(ALICE, 'visual', OPENAI, react.history([{'role': 'user', 'text': 'images in cars?'}]), {},
                        react.options({}, OPENAI), state=st, app=SimpleNamespace())
    list(react.events(run))
    assert 'list_buckets' in {s['name'] for s in seen[1]['specs']}
    out = json.loads(seen[3]['msgs'][-1]['parts'][0]['tool_result']['content'])
    assert len(out['answer']) == nav.CONSULT_CHARS + 1 and out['answer'].endswith('…')


def test_consult_refusals(monkeypatch, session):  # noqa: F811
    st, _ = session
    monkeypatch.setattr(nav, '_sessions', lambda user, kind, ids=None: [])
    base = dict(user=ALICE, role='bucket', scope={}, provider=OPENAI, limit=None, cancel=None, sub_usage={}, events=[],
                app=SimpleNamespace(state_for=lambda sid, user: st), roles_on=None, depth=0)
    q = {'page': 'viz', 'question': 'x', 'session_id': 'a1'}
    for over, args, match in [({'depth': 1}, q, 'cannot consult again'),
                              ({'role': 'visual'}, q, 'on that page already'),
                              ({'roles_on': {'visual': False}}, q, 'switched off'),
                              ({}, {'page': 'viz', 'question': 'x'}, 'session_id is required'),
                              ({}, q, "no visualize session 'a1'")]:
        with pytest.raises(tools.ToolError, match=match):
            nav._consult(SimpleNamespace(**{**base, **over}), args)
    monkeypatch.setattr(nav, '_sessions', lambda user, kind, ids=None: VIZ)
    st_idx = SimpleNamespace(index=None)
    with pytest.raises(tools.ToolError, match='still being indexed'):
        nav._consult(SimpleNamespace(**{**base, 'app': SimpleNamespace(state_for=lambda sid, user: st_idx)}), q)


def test_a_failing_specialist_is_a_tool_error(monkeypatch, session):  # noqa: F811
    from dataviz.llm.providers import LLMError
    st, _ = session
    monkeypatch.setattr(nav, '_sessions', lambda user, kind, ids=None: VIZ)
    script(monkeypatch, LLMError('bad request', 400))
    ctx = SimpleNamespace(user=ALICE, role='bucket', scope={}, provider=OPENAI, limit=None, cancel=None,
                          sub_usage={'in': 0, 'out': 0}, events=[], app=SimpleNamespace(state_for=lambda s, u: st),
                          roles_on=None, depth=0)
    with pytest.raises(tools.ToolError, match='Visualizer assistant failed: bad request'):
        nav._consult(ctx, {'page': 'viz', 'question': 'x', 'session_id': 'a1'})
