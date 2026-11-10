"""The Visual agent's tools (llm/tools/visual.py) on a real visualizer session State: the read tools, the UI actions'
validation and labels, the engine's ui_action events per mode, the frontend contract (colour-by values, Find issues
views, tabs, parameter hints match static/), and the drift guards of the knowledge / skills prompts (every feature,
method and tool they name exists) plus the visual prompt's token budget."""

import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_agent_engine import drain, script, types
from test_curation import _Job, make_dataset
from test_llm_tools import ALICE, OPENAI

from dataviz.data import curation as cur
from dataviz.data.sources import LocalSource
from dataviz.llm import budget, prompting, tasks, tools
from dataviz.llm.engine import react, roles
from dataviz.llm.engine.react import Ctx
from dataviz.llm.tools import visual
from dataviz.ml import analysis
from dataviz.ml import features as feats

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / 'dataviz' / 'llm' / 'prompts'
PLANNED_TOOLS = {'web_search', 'fetch_url'}         # tools/web.py (another workstream): named by web-research


# ---------------------------------------------------------------- a session
@pytest.fixture(scope='module')
def session(tmp_path_factory):
    """(State, Curation) of the curation test dataset with two simple feature spaces; `geometry` has its k-NN scores
    and a PCA map (with its quality numbers), `hog` nothing computed."""
    from dataviz.state import State
    tmp = tmp_path_factory.mktemp('viz')
    reg = {}
    for k in ('geometry', 'hog'):
        label, fn, metric, note = feats.SIMPLE[k]
        reg[k] = feats.FeatureSpec(k, label, 'simple', fn, metric=metric, note=note)
    st = State(LocalSource(make_dataset(tmp / 'ds')), tmp / 'ws', reg, default_feature='geometry')
    st._index_job(_Job())
    st.get_scores('geometry')
    _, key, _ = st.request_projection('geometry', 'pca', {}, None)
    t0 = time.time()
    while st.get_projection(key) is None and time.time() - t0 < 60:
        time.sleep(0.05)
    c = cur.Curation(st)
    c.scan()
    return st, c


def ctx_of(session, view=None, app=True):
    st, c = session
    a = SimpleNamespace(curation=lambda s: c, models=None, model_service=None) if app else None
    return Ctx(user=ALICE, role='visual', scope={'view': view} if view is not None else {}, skills={}, state=st, app=a)


VIEW = {'tab': 'explore', 'feature': 'geometry', 'method': 'pca', 'params': {}, 'color_by': 'class',
        'selection': [0, 1, 2]}


def run(ctx, name, args=None):
    return tools.run(ctx, name, args or {}, tools.REGISTRY)


# ---------------------------------------------------------------- read tools
def test_tools_are_visual_ctx_tools_without_the_data_center():
    names = {t.name for t in tools.REGISTRY.values() if t.domain == 'visual'}
    assert names == {'view_state', 'dataset_summary', 'class_stats', 'eval_summary', 'compare_shift', 'compare_stress', 'compare_tests', 'list_features', 'list_methods',
                     'projection_quality', 'issue_summary', 'selection_view', 'search_boxes_by_text',
                     'set_projection', 'set_color_by', 'apply_filter', 'open_tab', 'show_issue', 'fit_view',
                     'list_models', 'run_model', 'run_eval_all', 'run_scan', 'compute_stats', 'data_location'}
    assert all(tools.REGISTRY[n].ctx for n in names)
    assert {n for n in names if tools.REGISTRY[n].risk == 'ui'} == set(visual.UI_ACTIONS) - set(visual.RUN_ACTIONS)
    assert {n for n in names if tools.REGISTRY[n].risk == 'run'} == set(visual.RUN_ACTIONS)
    src = Path(visual.__file__).read_text()
    assert 'datacenter' not in re.sub(r'""".*?"""', '', src, flags=re.S) and '_svc' not in src
    assert not set(tools.for_role(roles.get('visual').domains)) & {t for t in tools.TOOLS if tools.TOOLS[t].domain == 'data'}


def test_view_state(session):
    out, summary = run(ctx_of(session, VIEW), 'view_state')
    assert out['page']['method'] == 'pca' and out['session']['boxes'] == 13 and out['session']['classes'] == 8
    assert out['session']['features_computed'] == ['geometry'] and out['session']['curation_scanned']
    assert 'pca on geometry' in summary
    out, _ = run(Ctx(user=ALICE, role='visual', scope={}, skills={}), 'view_state')
    assert out['session'] is None                       # no session: says so instead of failing


def test_no_session_is_a_fixable_error():
    ctx = Ctx(user=ALICE, role='visual', scope={}, skills={})
    for name in ('dataset_summary', 'class_stats', 'list_features', 'issue_summary', 'projection_quality'):
        with pytest.raises(tools.ToolError, match='no visualizer session'):
            run(ctx, name)


def test_dataset_summary_and_class_stats(session):
    ctx = ctx_of(session, VIEW)
    out, _ = run(ctx, 'dataset_summary')
    assert '13 boxes, 8 classes' in out['summary'] and 'Curation scan' in out['summary']
    out, summary = run(ctx, 'class_stats')
    rows = {r['class']: r for r in out['classes']}
    assert summary == '→ 8 classes' and rows['dog']['boxes'] == 6 and rows['class_3']['boxes'] == 0
    assert rows['cat']['area_pct_p10_50_90'][1] == 4.0 and 'suspicious_ge_0_5' in rows['cat']
    assert out['suspicion_from'] == 'geometry'
    out, _ = run(ctx, 'class_stats', {'classes': ['Dog', '2']})
    assert [r['class'] for r in out['classes']] == ['dog', 'bird']
    with pytest.raises(tools.ToolError, match='unknown class'):
        run(ctx, 'class_stats', {'classes': ['zebra']})
    out, _ = run(ctx_of(session, {'feature': 'hog'}), 'class_stats')
    assert 'not computed' in out['note']                # hog has no scores: never computed by a read tool
    assert 'hog' not in session[0].scores


def test_eval_features_methods(session):
    ctx = ctx_of(session, VIEW)
    out, _ = run(ctx, 'eval_summary')
    assert out['evaluation'] is None and 'run_model' in out['note']
    out, _ = run(ctx, 'compare_shift')
    assert out['report'] is None and 'combined' in out['note']
    out, _ = run(ctx, 'compare_stress')
    assert out['stress'] is None and 'combined' in out['note']
    out, _ = run(ctx, 'compare_tests')
    assert out['tests'] is None and 'combined' in out['note']
    out, _ = run(ctx, 'list_features')
    f = {r['id']: r for r in out['features']}
    assert f['geometry']['computed'] and f['geometry']['dims'] == 8 and f['geometry']['current']
    assert not f['hog']['computed'] and f['hog']['text_search'] is False and f['geometry']['metric'] == 'euclidean'
    out, _ = run(ctx, 'list_methods')
    m = {r['id']: r for r in out['methods']}
    assert set(m) == set(analysis.METHODS)
    assert m['umap']['params']['n_neighbors']['default'] == 15 and 'global' in m['umap']['params']['n_neighbors']['hint']
    assert m['pca']['params'] == {} and m['pacmap']['note']


def test_projection_quality(session):
    ctx = ctx_of(session, VIEW)
    out, summary = run(ctx, 'projection_quality')
    assert 0 < out['trustworthiness'] <= 1 and 0 < out['continuity'] <= 1 and out['points'] == 13
    assert out['explained_variance'] and summary.startswith('→ T ')
    with pytest.raises(tools.ToolError, match='not computed yet.*pca on geometry'):
        run(ctx, 'projection_quality', {'method': 'tsne'})
    with pytest.raises(tools.ToolError, match='no method'):
        run(ctx, 'projection_quality', {'method': 'trimap'})


def test_issue_summary(session):
    out, _ = run(ctx_of(session, VIEW), 'issue_summary')
    assert out['curation']['bad_line']['n'] == 2 and out['curation']['bad_line']['bulk_fix']
    assert out['knn']['feature'] == 'geometry' and out['knn']['suspicious_any'] >= out['knn']['suspicious_ge_0_5']
    assert out['cleanlab'] is None and 'Run label-quality check' in out['cleanlab_note']
    out, _ = run(ctx_of(session, VIEW, app=False), 'issue_summary')
    assert out['curation'] is None and 'Scan dataset' in out['curation_note']


def test_selection_view(session):
    ctx = ctx_of(session, VIEW)
    out, summary = run(ctx, 'selection_view')
    assert out['selected'] == 3 and '#1 → box 0' in out['legend'] and sum(out['by_class'].values()) == 3
    out, _ = run(ctx, 'selection_view', {'ids': [3, 4, 999]})
    assert out['selected'] == 2
    with pytest.raises(tools.ToolError, match='no selection'):
        run(ctx_of(session, {}), 'selection_view')


def test_search_boxes_by_text(session, monkeypatch):
    ctx = ctx_of(session, VIEW)
    with pytest.raises(tools.ToolError, match='no text encoder.*CLIP / SigLIP'):
        run(ctx, 'search_boxes_by_text', {'query': 'a cat'})
    st = session[0]
    spec = st.registry['hog']
    monkeypatch.setattr(spec, 'text_model', 'fake/clip')
    monkeypatch.setattr(st, 'text_ready', lambda k, q: True)
    monkeypatch.setattr(st, 'text_search', lambda k, q, n: {'id': [1, 2], 'score': [0.31, 0.29]})
    out, summary = run(ctx, 'search_boxes_by_text', {'query': 'a cat', 'feature': 'hog'})
    assert out['ready'] and out['ids'] == [1, 2] and out['scores'] == [0.31, 0.29] and summary == '→ 2 boxes'


# ---------------------------------------------------------------- UI actions
def test_ui_actions_validate(session):
    ctx = ctx_of(session, VIEW)
    out, _ = run(ctx, 'set_projection', {'feature': 'hog', 'method': 'umap', 'params': {'n_neighbors': 30.0}})
    assert out['args'] == {'feature': 'hog', 'method': 'umap', 'params': {'n_neighbors': 30}}
    bad = [('set_projection', {'method': 'bogus'}, 'method must be one of'),
           ('set_projection', {'feature': 'dinov9'}, "no feature 'dinov9'"),
           ('set_projection', {'method': 'pca', 'params': {'perplexity': 30}}, 'pca has no parameter'),
           ('set_projection', {'method': 'umap', 'params': {'n_neighbors': 1}}, 'between'),
           ('set_projection', {}, 'at least one'),
           ('set_color_by', {'by': 'rainbow'}, 'by must be one of'),
           ('open_tab', {'tab': 'label'}, 'tab must be one of'),
           ('show_issue', {'issue': 'cleanlab'}, 'issue must be one of'),
           ('apply_filter', {'spec': 'dogs'}, 'spec must be an object')]
    for name, args, msg in bad:
        with pytest.raises(tools.ToolError, match=msg):
            run(ctx, name, args)
        with pytest.raises(tools.ToolError, match=msg):
            tools.prepare(ctx, name, args, tools.REGISTRY)
    # params alone follow the page's method (umap has min_dist; pca on screen has none)
    with pytest.raises(tools.ToolError, match='pca has no parameter'):
        run(ctx, 'set_projection', {'params': {'min_dist': 0.2}})
    clean, label = tools.prepare(ctx, 'set_projection', {'method': 'tsne', 'params': {'perplexity': 20}}, tools.REGISTRY)
    assert clean['method'] == 'tsne' and label == 'Projection: t-SNE · perplexity=20.0'


def test_apply_filter_is_cleaned(session):
    ctx = ctx_of(session, VIEW)
    clean, label = tools.prepare(ctx, 'apply_filter', {'spec': {'scope': 'box', 'cls': {'names': ['Dog', 'zebra']},
                                                                'brightness': [None, 60], 'issues': ['bad_line', 'x'],
                                                                'tags': ['nope']}}, tools.REGISTRY)
    spec = clean['spec']
    assert spec['scope'] == 'box' and spec['cls']['ids'] == [1] and spec['brightness'] == [None, 60.0]
    assert spec['issues'] == ['bad_line'] and spec['tags'] == [] and set(spec) == set(tasks.EMPTY_FILTER)
    assert label.startswith('Filter: classes') and 'brightness' in label


def test_ui_labels():
    p = lambda n, a: tools.REGISTRY[n].preview(None, a)        # noqa: E731
    assert p('set_color_by', {'by': 'suspicion'}) == 'Colour by label suspicion'
    assert p('open_tab', {'tab': 'eval'}) == 'Open Evaluation'
    assert p('show_issue', {'issue': 'far2d'}) == 'Show Far from class (on the map)'
    assert p('fit_view', {}) == 'Fit the map'
    assert p('set_projection', {'feature': 'hog', 'method': 'umap'}) == 'Projection: features hog · UMAP'
    assert p('apply_filter', {'spec': {}}) == 'Filter: clear'


def _visual_run(session, text, mode, **kw):
    st, c = session
    opts = react.options({'mode': mode, **kw}, OPENAI)
    app = SimpleNamespace(curation=lambda s: c, models=None, model_service=None)
    return react.prepare(ALICE, 'visual', OPENAI, react.history([{'role': 'user', 'text': text}]), {'view': VIEW},
                         opts, state=st, app=app)


@pytest.mark.parametrize('mode,applied', [('ask', False), ('auto', True)])
def test_engine_emits_ui_actions(monkeypatch, session, mode, applied):
    # (set_projection starts work: a run action, tests/llm/test_ui_control.py)
    seen = script(monkeypatch, ('', [('list_methods', {}), ('set_color_by', {'by': 'suspicion'}),
                                     ('show_issue', {'issue': 'nope'})]), ('Try UMAP.', []))
    evs = drain(_visual_run(session, 'better map?', mode))
    ui = [e for e in evs if e['type'] == 'ui_action']
    assert len(ui) == 1 and ui[0]['action'] == 'set_color_by' and ui[0]['applied'] is applied
    assert ui[0]['label'] == 'Colour by label suspicion' and ui[0]['args'] == {'by': 'suspicion'}
    assert 'confirm' not in types(evs) and evs[-1]['type'] == 'done'
    res = [p['tool_result'] for p in seen[1]['msgs'][-1]['parts']]
    assert '"methods"' in res[0]['content']
    assert ('applied on the page' if applied else 'Apply button') in res[1]['content']
    assert res[2]['is_error'] and 'issue must be one of' in res[2]['content']
    sysp = seen[0]['system']
    assert '`set_projection`' in sysp and 'Visual assistant' in sysp and 'The visualizer shows' in sysp


def test_plan_mode_offers_no_ui_actions(monkeypatch, session):
    seen = script(monkeypatch, ('Here is how.', []))
    drain(_visual_run(session, 'explain', 'plan'))
    names = {s['name'] for s in seen[0]['specs']}
    assert 'list_features' in names and not names & set(visual.UI_ACTIONS)


# ---------------------------------------------------------------- the frontend contract
def test_ui_contract_matches_the_page():
    html = (ROOT / 'static' / 'index.html').read_text()
    sel = html[html.index('<select id="color-by"'):]
    sel = sel[:sel.index('</select>')]
    assert set(re.findall(r'<option value="([^"]+)"', sel)) == set(visual.COLOR_BY)
    issues = set(re.findall(r'data-view="([^"]+)"', html))
    assert set(visual.ISSUES) == issues
    tabs = set(re.findall(r'class="act[^"]*" data-tab="([a-z]+)"', html)) | set(re.findall(r'data-tab="([a-z]+)" role="tab"', html))
    assert set(visual.TABS) - {'ai'} <= tabs and 'ai' not in tabs     # ai: the right-side dock, not a view
    assert 'id="act-ai"' in html                                         # (the ◆ icon still opens it)
    ctl = (ROOT / 'static' / 'ts' / 'views' / 'controls.ts').read_text()
    for k, (label, hint) in visual.PARAM_INFO.items():
        assert f"{k}: ['{label}', '{hint}'" in ctl
    inspect = ctl[ctl.index('const INSPECT_BTN'):]
    inspect = inspect[:inspect.index('};')]
    assert set(re.findall(r"'?([a-z0-9-]+)'?: 'btn-", inspect)) == set(visual.ISSUES)
    params = {p for m in analysis.METHODS.values() for p in m['params']}
    assert params == set(visual.PARAM_INFO) == set(visual.PARAMS_SCHEMA['properties'])
    doc = visual.__doc__
    for n in visual.UI_ACTIONS:
        assert n in doc
    for v in (*visual.COLOR_BY, *visual.ISSUES, *visual.TABS, *analysis.METHODS):
        assert v in doc, v


# ---------------------------------------------------------------- prompts drift
def _config_features():
    cfg = yaml.safe_load((ROOT / 'config.yaml').read_text())['features']
    return {m['key'] for m in cfg['models']} | set(cfg.get('simple') or [])


IDENT = re.compile(r'^[a-z0-9][a-z0-9_:.\-]*$')


def _vocab():
    return (_config_features() | set(analysis.METHODS) | set(tools.REGISTRY) | PLANNED_TOOLS | set(visual.COLOR_BY)
            | set(visual.ISSUES) | set(visual.TABS) | set(visual.PARAM_INFO) | set(cur.TYPES)
            | set(tasks.EMPTY_FILTER) | {'substr', 'glob', 'regex', 'fuzzy', 'clip', 'siglip', 'dinov2'}
            | set(prompting.knowledge('visual')) | set(prompting.skills('visual')))


def test_visual_knowledge_names_only_real_features_and_methods():
    vocab, feats_ = _vocab(), _config_features()
    feature_like = re.compile(r'dinov2|clip|siglip|^hog$|color-hist|^geometry$')
    for f in sorted((PROMPTS / 'visual' / 'knowledge').glob('*.md')):
        for tok in re.findall(r'`([^`\n]+)`', f.read_text()):
            tok = tok.strip()
            if feature_like.search(tok) and IDENT.match(tok) and tok not in ('clip', 'siglip', 'dinov2'):
                assert tok in feats_, f'{f.name}: feature `{tok}` is not in config.yaml'
            if IDENT.match(tok):
                assert tok in vocab, f'{f.name}: `{tok}` is not a feature, method, tool or page id'
    know = prompting.knowledge('visual')
    core = {k for k, v in know.items() if v['core']}
    assert {'page-map', 'features', 'projections'} <= core and len(core) <= 4
    assert all(v['summary'] for v in know.values())
    for m in analysis.METHODS:                          # every method is documented
        assert f'`{m}`' in know['projections']['body'], m
    for k in _config_features():                        # every configured feature is documented
        assert f'`{k}`' in know['features']['body'], k


def test_skills_name_only_real_tools():
    names = set(tools.REGISTRY) | PLANNED_TOOLS
    args = {k for t in tools.REGISTRY.values() for k in (t.schema.get('properties') or {})}
    vocab = _vocab() | set(prompting.skills('data')) | args         # a tool's own argument names are fine too
    call = re.compile(r'^([a-z_][a-z0-9_]*)(\(.*\))?$')
    files = sorted((PROMPTS / 'skills').glob('*/*/SKILL.md'))
    assert len(files) == 26
    for f in files:
        meta, body = prompting.front(f.read_text())
        assert meta['name'] == f.parent.name and meta['description'] and meta['when'], f
        assert re.search(r'^1\. ', body, re.M), f'{f}: numbered steps'
        used = set()
        for tok in re.findall(r'`([^`\n]+)`', body):
            m = call.match(tok.strip())
            if not m:
                continue                                # a path, a tag, a value: not a name
            assert m.group(1) in names | vocab, f'{f.parent.name}: `{tok}` is not a tool or a page id'
            if m.group(1) in names:
                used.add(m.group(1))
        assert used, f'{f.parent.name} names no tool'
        role = f.parent.parent.name
        if role != 'common':                            # a skill never names the other agent's tools
            other = {t.name for t in tools.REGISTRY.values() if t.domain == ('visual' if role == 'data' else 'data')}
            assert not used & other, f'{f.parent.name}: {used & other}'
    assert set(prompting.skills('visual')) >= {'choose-feature-and-method', 'find-label-errors', 'read-the-map',
                                               'near-duplicates', 'compare-models', 'curate-a-class', 'web-research'}
    assert set(prompting.skills('data')) >= {'organise-yolo-dataset', 'split-train-val-test', 'clean-up-duplicates',
                                             's3-local-and-sync', 'export-dataset', 'tag-taxonomy', 'import-from-web'}


@pytest.mark.parametrize('role', ['visual', 'data'])
def test_prompt_stays_small(role):
    r = roles.get(role)
    tset = tools.for_role(r.domains, 'ask')
    tset.pop('delegate', None)                      # as react.prepare does while Sub-agents are off (the default)
    s = prompting.build(role, tools_catalog=tools.catalog(tset), scope_text=roles.SCOPES[role](None, {'view': VIEW}),
                        mode='ask', effort='medium', pages_text=roles.other_pages(role))
    assert budget.text_tokens(s) < 6000, budget.text_tokens(s)
    assert '## What you know' in s and '## More you can read' in s and '## Skills' in s
