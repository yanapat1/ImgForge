"""
The Visual agent's tools (domain 'visual'): they read the open visualizer session (`ctx.state`, a State) and the
server context (`ctx.app`: curation, models). None of them reaches the Data Center (no datacenter.service import),
and none changes data: page changes are UI actions the page applies through its own controls.

Read tools (risk read, run at once):
    view_state            what the page reported (ctx.scope['view']) + session facts (dataset, computed features)
    dataset_summary       counts, per-class table, quality quantiles, curation counts, tags (llm/context.py)
    class_stats           per class: boxes, images, area % / aspect / brightness / blur quantiles, k-NN suspicion
    eval_summary          the Evaluation tab's leaderboard and findings
    list_features         the feature registry (Features dropdown): id, label, group, kind, dims, computed, text
    list_methods          analysis.METHODS (Method dropdown) with parameters, defaults and the page's hints
    projection_quality    trustworthiness / continuity / kNN kept of a computed map
    issue_summary         curation scan counts + k-NN / cleanlab counts of a feature
    selection_view        a legend of selected boxes (id, class, image, size) + class counts
    search_boxes_by_text  boxes closest to a text, CLIP / SigLIP features only (the page's "Describe boxes…")

UI ACTION CONTRACT (risk 'ui'; the engine emits {type: 'ui_action', id, action, args, label, applied}; the page,
views/assistant.ts, applies `args` through its existing controls). Names, args and values are fixed:

    set_projection {feature?: str, method?: str, params?: {n_neighbors?: int, min_dist?: float,
                                                           perplexity?: float, gamma?: float}}
        feature: a key of list_features (e.g. 'openclip-h14', 'dinov2-base', 'hog', 'yolo:<run>')  -> #feature
        method:  pca | svd | ica | fa | umap | pacmap | localmap | tsne | kpca | isomap | lle | spectral | mds |
                 random | trees                                                                      -> #method
        params:  only the method's own (umap: n_neighbors, min_dist · pacmap / localmap / isomap / lle / spectral:
                 n_neighbors · tsne: perplexity · kpca: gamma)           -> #p-<method>-<param>, then Re-project
        At least one of feature / method / params. Omitted keys keep the page's current value.
    set_color_by {by: str}                                                                       -> #color-by
        class | group | suggested | session | suspicion | outlier | area | aspect | brightness | blur |
        cl_issue | cl_quality | cl_suggested | cl_given_prob | cl_ood_feat | cl_ood_prob   (after the cleanlab check)
        origin | diff                                                                    (a combined dataset)
        m_status | m_pred | m_score | cl_swap | cl_badloc | cl_image_quality             (a model in the Model tab)
    apply_filter {spec: object}           the filter bar (core/filter.ts emptyFilter() shape, see tasks.FILTER_SYSTEM;
                                          cleaned by tasks.clean_filter: known class ids / tags / issue types only)
    open_tab {tab: str}                   explore | stats | curation | model | eval | compare | ai  (activity bar)
    show_issue {issue: str}               a Find issues view (its data-view):
        suspicious | far | far2d | dups | cl-issues | cl-low | cl-ood | cl-swap | cl-badloc | sel-outliers
    fit_view {}                           the plot's Fit button (#btn-fit)

Each one's `preview(None, args)` is the Apply button's label. Mode ask: an Apply button (applied false); mode
auto: applied at once (applied true); mode plan: not offered. `check(ctx, args)` validates the session-dependent
values (feature id, method params, the filter's classes / tags) for tools.prepare / tools.run.
"""

import re

import numpy as np

from . import A, E, I, S, Tool, ToolError, _obj, register

MAX_IDS = 200

# the Method dropdown's parameter inputs (static/ts/views/controls.ts PARAM_INFO): label, hint
PARAM_INFO = {
    'n_neighbors': ('nbrs', 'neighbours each point looks at: larger = more global structure'),
    'min_dist': ('min_dist', 'smaller = tighter clusters'),
    'perplexity': ('perplexity', '~ number of close neighbours each point considers'),
    'gamma': ('gamma', 'RBF kernel width: larger = more local; 0 = auto (1 / dims)'),
}
PARAM_RANGE = {'n_neighbors': (2, 200), 'min_dist': (0.0, 1.0), 'perplexity': (2.0, 100.0), 'gamma': (0.0, 100.0)}

UI_ACTIONS = ('set_projection', 'set_color_by', 'apply_filter', 'open_tab', 'show_issue', 'fit_view')
COLOR_BY = ('class', 'group', 'suggested', 'session', 'suspicion', 'outlier', 'area', 'aspect', 'brightness', 'blur',
            'cl_issue', 'cl_quality', 'cl_suggested', 'cl_given_prob', 'cl_ood_feat', 'cl_ood_prob',
            'origin', 'diff',
            'm_status', 'm_pred', 'm_score', 'cl_swap', 'cl_badloc', 'cl_image_quality')
COLOR_LABEL = {'class': 'class', 'group': 'group', 'suggested': 'kNN-suggested class', 'session': 'session',
               'suspicion': 'label suspicion', 'outlier': 'outlier score', 'area': 'box area (log)',
               'aspect': 'aspect ratio (log)', 'brightness': 'brightness', 'blur': 'sharpness (log)',
               'cl_issue': 'cleanlab: label issue', 'cl_quality': 'cleanlab: label quality',
               'cl_suggested': 'cleanlab: suggested class', 'cl_given_prob': 'cleanlab: P(given class)',
               'cl_ood_feat': 'cleanlab: out-of-distribution', 'cl_ood_prob': 'cleanlab: out-of-distribution (probs)',
               'origin': 'dataset (A / B / …)', 'diff': 'compare: box diff',
               'm_status': 'model: found / missed / wrong', 'm_pred': 'model: predicted class',
               'm_score': 'model: confidence', 'cl_swap': 'cleanlab: wrong-class score',
               'cl_badloc': 'cleanlab: placement score', 'cl_image_quality': 'cleanlab: image label quality'}
TABS = ('explore', 'stats', 'curation', 'model', 'eval', 'compare', 'ai')
TAB_LABEL = {'explore': 'Explore', 'stats': 'Class stats', 'curation': 'Curation', 'model': 'Model',
             'eval': 'Evaluation', 'compare': 'Compare', 'ai': 'AI assistant'}
ISSUES = ('suspicious', 'far', 'far2d', 'dups', 'cl-issues', 'cl-low', 'cl-ood', 'cl-swap', 'cl-badloc',
          'sel-outliers')
ISSUE_LABEL = {'suspicious': 'Suspicious labels', 'far': 'Far from class', 'far2d': 'Far from class (on the map)',
               'dups': 'Near-duplicates', 'cl-issues': 'Label issues (cleanlab)',
               'cl-low': 'Lowest label quality (cleanlab)', 'cl-ood': 'Out-of-distribution (cleanlab)',
               'cl-swap': 'Wrong class (cleanlab, model)', 'cl-badloc': 'Badly placed (cleanlab, model)',
               'sel-outliers': 'Select all outliers (density groups)'}


# ---------------------------------------------------------------- helpers
def _methods():
    from ...ml import analysis
    return analysis.METHODS


def _state(ctx):
    st = getattr(ctx, 'state', None)
    if st is None:
        raise ToolError('no visualizer session is open: the user must open a dataset in the visualizer first')
    if not getattr(st, 'ready', False) or getattr(st, 'index', None) is None:
        raise ToolError('the dataset is still being indexed: try again in a moment')
    return st


def _view(ctx):
    v = (getattr(ctx, 'scope', None) or {}).get('view')
    return v if isinstance(v, dict) else {}


def _curation(ctx, st):
    app = getattr(ctx, 'app', None)
    fn = getattr(app, 'curation', None)
    if fn is None:
        return None
    try:
        return fn(st)
    except Exception:  # noqa: BLE001 - optional context
        return None


def _live(st):
    dm = st.deleted_mask()
    return np.arange(len(st.samples)) if dm is None else np.flatnonzero(~dm)


def _cls_name(st, c):
    c = int(c)
    return st.classes[c] if 0 <= c < len(st.classes) else str(c)


def _feature_key(ctx, st, feature):
    """The feature id to use: the argument, else the page's, else the session's default. ToolError when unknown."""
    key = feature or _view(ctx).get('feature') or st.default_feature
    if key not in st.registry:
        raise ToolError(f'no feature {key!r}; the features are: {", ".join(list(st.registry)[:30])}')
    return key


def _scores(st, key):
    """k-NN scores of a feature when already computed (never starts a computation), else None."""
    if key in st.scores:
        return st.scores[key]
    try:
        if st._scores_file(key).exists():
            return st.get_scores(key)
    except Exception:  # noqa: BLE001
        return None
    return None


def _dims(st, key, spec):
    emb = st.features.get(key) if isinstance(getattr(st, 'features', None), dict) else None
    if emb is not None and getattr(emb, 'ndim', 0) == 2:
        return int(emb.shape[1])
    m = re.search(r'(\d+)-d\b', str(getattr(spec, 'label', '') or ''))
    return int(m.group(1)) if m else None


def _q(a, qs=(10, 50, 90), nd=2):
    return [round(float(v), nd) for v in np.percentile(a, qs)] if len(a) else None


def _class_ids(st, names):
    """Class names / ids -> ids (case-insensitive); ToolError naming the unknown ones."""
    by = {str(n).casefold(): i for i, n in enumerate(st.classes)}
    out, bad = [], []
    for x in names or []:
        s = str(x).strip()
        if s.isdigit() and int(s) < len(st.classes):
            out.append(int(s))
        elif s.casefold() in by:
            out.append(by[s.casefold()])
        else:
            bad.append(s)
    if bad:
        raise ToolError(f'unknown class(es) {", ".join(bad)}; the classes are: {", ".join(st.classes[:60])}')
    return sorted(set(out))


def _tags(st):
    try:
        return st.load_tags() or {}
    except Exception:  # noqa: BLE001 - no workspace yet
        return {}


# ---------------------------------------------------------------- read tools
def _view_state(ctx, a):
    v = _view(ctx)
    st = getattr(ctx, 'state', None)
    out = {'page': v or None}
    if st is None:
        out['session'] = None
        return out, '→ no session open'
    facts = {'dataset': getattr(st.source, 'name', '') or 'dataset', 'kind': getattr(st.source, 'kind', None),
             'ready': bool(getattr(st, 'ready', False))}
    if facts['ready']:
        live = _live(st)
        facts.update({'boxes': int(len(live)), 'images': int(len(st.by_image)), 'classes': len(st.classes),
                      'default_feature': st.default_feature,
                      'startup_method': getattr(st, 'startup_method', None),
                      'features_computed': [k for k in st.registry if _safe_cached(st, k)][:30],
                      'combined': bool(getattr(st, 'members', None))})
        cur = _curation(ctx, st)
        facts['curation_scanned'] = cur is not None
    out['session'] = facts
    if not v:
        out['note'] = 'the page did not report its controls; ask the user or use the defaults above'
    return out, f'→ {facts["dataset"]}' + (f', {v.get("method")} on {v.get("feature")}' if v.get('method') else '')


def _safe_cached(st, k):
    try:
        return bool(st.feature_cached(k))
    except Exception:  # noqa: BLE001
        return False


def _dataset_summary(ctx, a):
    from .. import context
    st = _state(ctx)
    text = context.dataset_summary(st, _curation(ctx, st))
    return {'summary': text}, f'→ {len(st.classes)} classes'


def _class_stats(ctx, a):
    st = _state(ctx)
    want = _class_ids(st, a.get('classes')) if a.get('classes') else list(range(len(st.classes)))
    ids = _live(st)
    s = st.samples
    labels = np.asarray(st.labels)[ids] if len(ids) else np.zeros(0, np.int64)
    area = np.array([s[i]['area'] for i in ids], float) * 100
    aspect = np.array([s[i]['aspect'] for i in ids], float)
    bright = np.array([s[i].get('brightness', 0) for i in ids], float)
    blur = np.array([s[i].get('blur', 0) for i in ids], float)
    imgs = np.array([s[i]['image'] for i in ids], dtype=object)
    key = None
    sc = None
    try:
        key = _feature_key(ctx, st, a.get('feature'))
        sc = _scores(st, key)
    except ToolError:
        if a.get('feature'):
            raise
    total = max(1, len(ids))
    rows = []
    for c in want:
        m = labels == c
        n = int(m.sum())
        r = {'id': c, 'class': _cls_name(st, c), 'boxes': n, 'share_pct': round(100 * n / total, 1),
             'images': int(len(set(imgs[m]))) if n else 0}
        if n:
            r.update({'area_pct_p10_50_90': _q(area[m]), 'aspect_p50': round(float(np.median(aspect[m])), 2),
                      'brightness_p50': round(float(np.median(bright[m])), 1),
                      'blur_p10_50': _q(blur[m], (10, 50), 1)})
            if sc is not None:
                sus = np.asarray(sc['suspicion'])[ids][m]
                r['suspicious_ge_0_5'] = int((sus >= 0.5).sum())
                sug = np.asarray(sc['suggested'])[ids][m]
                other = sug[sug != c]
                if len(other):
                    top = np.bincount(other, minlength=len(st.classes)).argmax()
                    r['mostly_confused_with'] = _cls_name(st, top)
        rows.append(r)
    out = {'classes': rows, 'boxes': int(len(ids)),
           'units': 'area % of the image; blur = laplacian variance (low = blurry); brightness 0-255'}
    if sc is not None:
        out['suspicion_from'] = key
    else:
        out['note'] = 'k-NN suspicion not computed for this feature yet (Compute embedding on the page)'
    return out, f'→ {len(rows)} classes'


def _eval_summary(ctx, a):
    from .. import context
    _state(ctx)
    ev = context.eval_summary(getattr(ctx, 'app', None)) if getattr(ctx, 'app', None) is not None else None
    if not ev:
        return {'evaluation': None, 'note': 'no model has been evaluated on this dataset yet (Model tab: pick a '
                                            'model and Run on this dataset; Settings › Models adds one)'}, '→ no models'
    return {'evaluation': ev}, '→ evaluation'


def _list_features(ctx, a):
    st = _state(ctx)
    cur = _view(ctx).get('feature')
    rows = []
    for k in st.registry:
        try:
            spec = st.registry[k]
        except Exception:  # noqa: BLE001
            continue
        kind = getattr(spec, 'kind', '') or ('yolo' if str(k).startswith('yolo:') else '')
        rows.append({'id': k, 'label': getattr(spec, 'label', k) if spec is not None else k,
                     'group': getattr(spec, 'group', None), 'kind': kind or None, 'dims': _dims(st, k, spec),
                     'metric': getattr(spec, 'metric', 'cosine'), 'computed': _safe_cached(st, k),
                     'text_search': bool(getattr(spec, 'text_model', None)),
                     **({'note': spec.note[:120]} if getattr(spec, 'note', '') else {}),
                     **({'current': True} if k == cur else {}),
                     **({'default': True} if k == st.default_feature else {})})
    return {'features': rows}, f'→ {len(rows)} features'


def _list_methods(ctx, a):
    out = []
    for k, m in _methods().items():
        out.append({'id': k, 'label': m['label'], 'category': m['category'],
                    'params': {p: {'default': v, 'label': PARAM_INFO.get(p, (p, ''))[0],
                                   'hint': PARAM_INFO.get(p, ('', ''))[1]} for p, v in m['params'].items()},
                    **({'note': m['note']} if m.get('note') else {})})
    return {'methods': out}, f'→ {len(out)} methods'


def _projection_quality(ctx, a):
    from ...state import normalize_params, projection_key
    st = _state(ctx)
    v = _view(ctx)
    key = _feature_key(ctx, st, a.get('feature'))
    method = a.get('method') or v.get('method') or getattr(st, 'startup_method', None) or 'umap'
    method = _method_id(method)
    params = a.get('params') if isinstance(a.get('params'), dict) else \
        (v.get('params') if isinstance(v.get('params'), dict) and not a.get('method') else {})
    classes = _class_ids(st, a.get('classes')) if a.get('classes') else None
    pk = projection_key(key, method, normalize_params(method, params), classes)
    proj = st.get_projection(pk)
    if proj is None:
        saved = [e for e in st.saved_projections().values() if e.get('feature') == key and e.get('method') == method]
        if saved:
            proj = st.get_projection(saved[0]['key'])
            pk = saved[0]['key']
    if proj is None:
        done = [f'{e["method"]} on {e["feature"]}' for e in st.saved_projections().values()][:15]
        raise ToolError(f'{method} on {key} is not computed yet (Re-project on the page first)'
                        + (f'; computed maps: {", ".join(done)}' if done else ''))
    q = (proj.get('info') or {}).get('quality') or {}
    out = {'map': pk, 'points': int(len(proj['id'])), **q}
    if not q:
        out['note'] = 'this map was saved without quality numbers (computed by an older version): Re-project'
    else:
        out['read'] = ('trustworthiness: share of 2D neighbours that are real neighbours (1 = no false neighbours); '
                       'continuity: real neighbours kept close in 2D (1 = none torn apart); knn_preserved: mean '
                       'share of the k nearest kept; > 0.9 good, 0.8-0.9 fair, < 0.8 read distances with care')
    ev = (proj.get('info') or {}).get('explained_variance')
    if ev:
        out['explained_variance'] = ev
    return out, f'→ T {q.get("trustworthiness", "?")} · C {q.get("continuity", "?")}'


def _issue_summary(ctx, a):
    st = _state(ctx)
    out = {}
    cur = _curation(ctx, st)
    if cur is not None:
        try:
            summ = cur.summary()
        except Exception:  # noqa: BLE001
            summ = {}
        out['curation'] = {k: {'n': v['n'], 'label': v['label'], 'severity': v['severity'], 'group': v['group'],
                               'bulk_fix': v['bulk']} for k, v in summ.items()}
    else:
        out['curation'] = None
        out['curation_note'] = 'not scanned: Curation tab › Scan dataset'
    key = _feature_key(ctx, st, a.get('feature'))
    ids = _live(st)
    sc = _scores(st, key)
    if sc is not None:
        sus = np.asarray(sc['suspicion'])[ids]
        out['knn'] = {'feature': key, 'suspicious_any': int((sus > 0).sum()), 'suspicious_ge_0_5': int((sus >= 0.5).sum()),
                      'suspicious_ge_0_8': int((sus >= 0.8).sum()),
                      'outlier_p50_p99': _q(np.asarray(sc['outlier'])[ids], (50, 99), 3)}
    else:
        out['knn'] = None
    try:
        hit = st.cached_label_quality(key)
    except Exception:  # noqa: BLE001
        hit = None
    if hit is not None:
        per, summary = hit
        out['cleanlab'] = {'feature': key, 'n_issues': summary.get('n_issues'),
                           'health_score': summary.get('health_score'),
                           'worst_classes': [{'class': r['name'], 'issues': r['issues'],
                                              'noise_rate': round(r['noise_rate'], 3)}
                                             for r in (summary.get('class_ranking') or [])[:5]],
                           'overlapping': [{'a': r['a_name'], 'b': r['b_name'], 'n': r['n']}
                                           for r in (summary.get('overlapping_classes') or [])[:5]]}
    else:
        out['cleanlab'] = None
        out['cleanlab_note'] = 'not run for this feature: Find issues › Run label-quality check'
    n = sum(v['n'] for v in (out['curation'] or {}).values())
    return out, f'→ {n} curation issues' if out['curation'] is not None else '→ no curation scan'


def _selection_view(ctx, a):
    from .. import context
    st = _state(ctx)
    ids = a.get('ids')
    if not ids:
        sel = _view(ctx).get('selection')
        ids = sel if isinstance(sel, list) else []
    try:
        ids = [int(i) for i in ids if 0 <= int(i) < len(st.samples)]
    except (TypeError, ValueError):
        raise ToolError('ids must be box ids (integers)')
    if not ids:
        raise ToolError('no selection: pass box ids, or ask the user to select boxes (lasso on the map)')
    legend = ''
    try:
        _, legend, shown = context.selection_mosaic(st, ids, max_n=min(24, int(a.get('max') or 24)))
    except Exception:  # noqa: BLE001 - unreadable images: the legend without the picture
        shown = context.sample_even(ids, 24)
        lines = []
        for k, i in enumerate(shown):
            s = st.samples[i]
            lines.append(f'#{k + 1} → box {i}, class "{_cls_name(st, st.labels[i])}", image {s["image"]}')
        legend = '\n'.join(lines)
    counts = {}
    for i in ids:
        n = _cls_name(st, st.labels[i])
        counts[n] = counts.get(n, 0) + 1
    out = {'selected': len(ids), 'by_class': dict(sorted(counts.items(), key=lambda kv: -kv[1])),
           'legend': legend, 'note': 'tool results carry no images: to judge the crops, ask the user to look at '
                                     'them in the gallery (or Check labels there)'}
    return out, f'→ {len(ids)} boxes'


def _search_boxes_by_text(ctx, a):
    st = _state(ctx)
    key = _feature_key(ctx, st, a.get('feature'))
    spec = st.registry[key]
    if not getattr(spec, 'text_model', None):
        txt = [k for k in st.registry if getattr(st.registry[k], 'text_model', None)]
        raise ToolError(f'{getattr(spec, "label", key)} has no text encoder: text search needs a CLIP / SigLIP '
                        f'feature' + (f' ({", ".join(txt)})' if txt else ' (none configured)'))
    q = a['query'].strip()
    if not q:
        raise ToolError('empty query')
    k = int(a.get('k') or 50)
    if not st.text_ready(key, q):
        job = st.request_text_search(key, q, k)
        return {'ready': False, 'job_id': job.id,
                'note': 'loading the text tower / features: call again in a few seconds'}, '→ started'
    res = st.text_search(key, q, k)
    ids = [int(i) for i in res['id']]
    counts = {}
    for i in ids:
        n = _cls_name(st, st.labels[i])
        counts[n] = counts.get(n, 0) + 1
    return {'ready': True, 'feature': key, 'query': q, 'ids': ids[:MAX_IDS],
            'scores': [round(float(x), 3) for x in res['score'][:MAX_IDS]],
            'by_class': dict(sorted(counts.items(), key=lambda kv: -kv[1]))}, f'→ {len(ids)} boxes'


# ---------------------------------------------------------------- UI actions
def _method_id(m):
    """A method id or its label (UMAP, t-SNE ...) -> the id; ToolError otherwise."""
    ms = _methods()
    if m in ms:
        return m
    by = {v['label'].casefold(): k for k, v in ms.items()}
    k = by.get(str(m or '').casefold())
    if k is None:
        raise ToolError(f'no method {m!r}; the methods are: {", ".join(ms)}')
    return k


def _check_projection(ctx, a):
    if not any(k in a for k in ('feature', 'method', 'params')):
        raise ToolError('give at least one of feature, method, params')
    v = _view(ctx)
    st = getattr(ctx, 'state', None)
    out = {}
    if 'feature' in a:
        if st is not None and a['feature'] not in st.registry:
            raise ToolError(f'no feature {a["feature"]!r}; the features are: {", ".join(list(st.registry)[:30])}')
        out['feature'] = a['feature']
    method = a.get('method') or v.get('method')
    if 'method' in a:
        out['method'] = _method_id(a['method'])
        method = out['method']
    if a.get('params'):
        ms = _methods()
        m = _method_id(method) if method else None
        allowed = ms[m]['params'] if m else {k: None for k in PARAM_INFO}
        clean = {}
        for k, val in a['params'].items():
            if k not in allowed:
                raise ToolError(f'{m or "this method"} has no parameter {k!r}'
                                + (f'; its parameters: {", ".join(allowed) or "none"}' if m else ''))
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ToolError(f'{k} must be a number')
            lo, hi = PARAM_RANGE[k]
            if not lo <= val <= hi:
                raise ToolError(f'{k} must be between {lo} and {hi}')
            clean[k] = int(val) if k == 'n_neighbors' else float(val)
        out['params'] = clean
    return out, None


def _check_filter(ctx, a):
    from ..tasks import clean_filter
    st = getattr(ctx, 'state', None)
    if st is None:
        raise ToolError('no visualizer session is open')
    if not isinstance(a.get('spec'), dict):
        raise ToolError('spec must be a filter object')
    cur = _curation(ctx, st)
    try:
        issues = list(cur.summary()) if cur is not None else []
    except Exception:  # noqa: BLE001
        issues = []
    return {'spec': clean_filter(a['spec'], st.classes, sorted(_tags(st)), issues)}, None


def _ok(ctx, a):
    return a, None


def _ui_run(check):
    """tools.run on a UI action: validated args back (the engine emits the ui_action itself)."""
    def run(ctx, a):
        clean, _ = check(ctx, a)
        return {'ui_action': True, 'args': clean}, '→ shown as an Apply button'
    return run


def _proj_label(b, a):
    parts = []
    if a.get('feature'):
        parts.append(f'features {a["feature"]}')
    if a.get('method'):
        m = _methods().get(a['method'])
        parts.append(m['label'] if m else str(a['method']))
    if a.get('params'):
        parts.append(', '.join(f'{k}={v}' for k, v in a['params'].items()))
    return 'Projection: ' + ' · '.join(parts) if parts else 'Re-project'


def _filter_label(b, a):
    spec = a.get('spec') or {}
    bits = [k for k, v in spec.items() if v not in (None, '', [], {}, False, [None, None])
            and k not in ('scope', 'name', 'cls', 'clip')]
    if (spec.get('cls') or {}).get('ids') or (spec.get('cls') or {}).get('names'):
        bits.insert(0, 'classes')
    if (spec.get('name') or {}).get('q'):
        bits.insert(0, 'name')
    if (spec.get('clip') or {}).get('q'):
        bits.append('text')
    return 'Filter: ' + (', '.join(bits) if bits else 'clear')


PARAMS_SCHEMA = {'type': 'object', 'additionalProperties': False,
                 'description': "the method's own parameters only (see list_methods)",
                 'properties': {'n_neighbors': {'type': 'integer', 'minimum': 2, 'maximum': 200},
                                'min_dist': {'type': 'number', 'minimum': 0, 'maximum': 1},
                                'perplexity': {'type': 'number', 'minimum': 2, 'maximum': 100},
                                'gamma': {'type': 'number', 'minimum': 0, 'maximum': 100}}}

_R = dict(domain='visual', ctx=True)

register(Tool('view_state', "What the visualizer shows now (the page's tab, features, method, params, colour, "
                            'filter, selection count) and the session facts (dataset, computed features).',
              _obj({}), False, _view_state, **_R))
register(Tool('dataset_summary', 'The open dataset as text: images, boxes, per-class table (count, images, median '
                                 'area, aspect), quality quantiles, curation issue counts, tags.',
              _obj({}), False, _dataset_summary, **_R))
register(Tool('class_stats', 'Per class: boxes, share, images, box area % / aspect / brightness / blur quantiles, and '
                             'how many boxes look mislabelled by k-NN (with the class they are confused with).',
              _obj({'classes': A('class names or ids (default: all)'),
                    'feature': S('feature id for the k-NN numbers (default: the current one)')}),
              False, _class_stats, **_R))
register(Tool('eval_summary', 'The Evaluation tab: models evaluated on this dataset (mAP, P, R, F1, misses), per-class '
                              'AP of the best one and the findings.', _obj({}), False, _eval_summary, **_R))
register(Tool('list_features', 'The feature spaces of the Features dropdown: id, label, kind (clip / siglip / dinov2 / '
                               'yolo / simple), dims, computed or not, text search support.',
              _obj({}), False, _list_features, **_R))
register(Tool('list_methods', 'The projection methods of the Method dropdown with their parameters (defaults, what '
                              'each does).', _obj({}), False, _list_methods, **_R))
register(Tool('projection_quality', 'How faithful a computed 2D map is: trustworthiness, continuity, k-NN kept '
                                    '(default: the map on screen).',
              _obj({'feature': S('feature id (default: the current one)'), 'method': S('method id (default: current)'),
                    'params': {**PARAMS_SCHEMA, 'description': 'the parameters of that map (default: the current)'},
                    'classes': A('the classes of a one-class / two-class map (default: all)')}),
              False, _projection_quality, **_R))
register(Tool('issue_summary', 'Problem counts: the curation scan (per issue type), k-NN suspicion / outliers and the '
                               'cleanlab label-quality result of a feature.',
              _obj({'feature': S('feature id (default: the current one)')}), False, _issue_summary, **_R))
register(Tool('selection_view', 'The selected boxes as a legend (box id, class, image, size) with counts per class '
                                '(default: the page selection). No pictures in tool results.',
              _obj({'ids': {'type': 'array', 'items': {'type': 'integer'}, 'description': 'box ids (default: the '
                                                                                            'selection)'},
                    'max': I('boxes listed, at most 24', 1, 24)}), False, _selection_view, **_R))
register(Tool('search_boxes_by_text', 'Boxes whose crops best match a text (e.g. "a red car at night"); needs a CLIP / '
                                      'SigLIP feature. Returns box ids, scores and counts per class.',
              _obj({'query': S('what is visible, a few words'), 'feature': S('a CLIP / SigLIP feature id (default: '
                                                                            'the current one)'),
                    'k': I('how many boxes (default 50)', 1, 500)}, ['query']), False, _search_boxes_by_text, **_R))

_U = dict(domain='visual', ctx=True, risk='ui')
register(Tool('set_projection', 'Page action: switch the Features / Method / parameters and re-project (an Apply '
                                'button for the user).',
              _obj({'feature': S('a feature id from list_features'),
                    'method': E('a method id from list_methods', list(_methods())), 'params': PARAMS_SCHEMA}),
              False, _ui_run(_check_projection), _proj_label, check=_check_projection, **_U))
register(Tool('set_color_by', 'Page action: colour the dots by something else (Color dropdown).',
              _obj({'by': E('what colours the dots', COLOR_BY)}, ['by']), False, _ui_run(_ok),
              lambda b, a: f'Colour by {COLOR_LABEL.get(a.get("by"), a.get("by"))}', check=_ok, **_U))
register(Tool('apply_filter', 'Page action: set the filter bar (a filter spec, the shape of the nl filter: scope, '
                              'name, cls {mode, ids or names}, nbox, area, aspect, brightness, blur, tags, issues, '
                              'suspicion_min, outlier_min, cl_quality_max, cl_issue, clip {q, k}).',
              _obj({'spec': {'type': 'object', 'description': 'the filter; omitted fields keep everything'}}, ['spec']),
              False, _ui_run(_check_filter), _filter_label, check=_check_filter, **_U))
register(Tool('open_tab', 'Page action: open a view of the activity bar.',
              _obj({'tab': E('the view', TABS)}, ['tab']), False, _ui_run(_ok),
              lambda b, a: f'Open {TAB_LABEL.get(a.get("tab"), a.get("tab"))}', check=_ok, **_U))
register(Tool('show_issue', 'Page action: turn on a Find issues view (loads those boxes into the gallery).',
              _obj({'issue': E('the Find issues view', ISSUES)}, ['issue']), False, _ui_run(_ok),
              lambda b, a: f'Show {ISSUE_LABEL.get(a.get("issue"), a.get("issue"))}', check=_ok, **_U))
register(Tool('fit_view', 'Page action: zoom the map so every shown dot is in view (the Fit button).',
              _obj({}), False, _ui_run(_ok), lambda b, a: 'Fit the map', check=_ok, **_U))
