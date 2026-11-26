"""
Recommended projections (recipes.yaml): named, categorised settings for one purpose ("Find mislabels", "Camera /
lighting shift"), each setting any of feature, method, params, dims, preprocessing and colour. Loaded and checked once
at import (a bad entry fails loudly, tests/ml/test_prep.py). for_state(state) says which work on a session and why the
others do not; resolve(recipe, state) gives the concrete feature key the session has.
"""

from pathlib import Path

import yaml

from . import analysis
from . import prep as mprep

_DATA = yaml.safe_load((Path(__file__).with_name('recipes.yaml')).read_text(encoding='utf-8'))
CATEGORIES = tuple(_DATA['categories'])
NEEDS = ('polygons', 'session', 'crops')
COLORS = ('class', 'group', 'suggested', 'session', 'suspicion', 'outlier', 'area', 'aspect', 'brightness', 'blur')


def _check(r):
    rid = r.get('id')
    for k in ('id', 'name', 'category', 'use_for', 'sets'):
        if not r.get(k):
            raise ValueError(f'recipe {rid!r}: {k} is required')
    if r['category'] not in CATEGORIES:
        raise ValueError(f'recipe {rid!r}: unknown category {r["category"]!r}')
    s = r['sets']
    if set(s) - {'feature', 'method', 'params', 'dims', 'prep', 'color'}:
        raise ValueError(f'recipe {rid!r}: unknown sets keys {sorted(set(s) - {"feature", "method", "params", "dims", "prep", "color"})}')
    if 'method' in s and s['method'] not in analysis.METHODS:
        raise ValueError(f'recipe {rid!r}: unknown method {s["method"]!r}')
    for k in s.get('params') or {}:
        if k not in analysis.METHODS[s.get('method', 'umap')]['params']:
            raise ValueError(f'recipe {rid!r}: {k} is no parameter of {s.get("method", "umap")}')
    if 'prep' in s:
        mprep.validate(s['prep'])
    if s.get('color') and s['color'] not in COLORS:
        raise ValueError(f'recipe {rid!r}: unknown colour {s["color"]!r}')
    if set(r.get('needs') or ()) - set(NEEDS):
        raise ValueError(f'recipe {rid!r}: unknown needs {r["needs"]}')
    return r


RECIPES = {r['id']: _check(r) for r in _DATA['recipes']}
if len(RECIPES) != len(_DATA['recipes']):
    raise ValueError('recipes.yaml: two recipes share an id')


def _pick_feature(prefs, registry):
    """The first feature of a preference list this registry has (None: none of them)."""
    specs = [(k, registry[k]) for k in registry if getattr(registry[k], 'group', '') not in ('variant', 'imported')]
    for p in prefs:
        if p in registry and getattr(registry[p], 'group', '') != 'variant':
            return p
        if p.startswith('kind:'):
            hit = next((k for k, s in specs if s.kind == p[5:]), None)
        elif p.startswith('group:'):
            hit = next((k for k, s in specs if s.group == p[6:]), None)
        elif p == 'yolo':
            hit = next((k for k, _ in specs if k.startswith('yolo:')), None)
        else:
            hit = None
        if hit:
            return hit
    return None


def resolve(r, state):
    """(the concrete settings of recipe `r` on this session, why it does not work here or '')."""
    s = dict(r['sets'])
    if 'feature' in s:
        f = _pick_feature(s['feature'], state.registry)
        if f is None:
            return s, f'needs one of: {", ".join(s["feature"])} (Settings › Models, or features.models in the config)'
        s['feature'] = f
    src = state.source
    for need in r.get('needs') or ():
        if need == 'crops' and (hasattr(src, 'assemble_features') or getattr(src, 'unit', None) == 'image'
                                or getattr(src, 'kind', '') == 'embeddings'):
            return s, 'needs box crops (not a whole-image, vector or combined session)'
        if need == 'polygons' and not state.has_polygons():
            return s, 'needs segmentation labels (YOLO polygons)'
        if need == 'session' and state.ready and len({x.get('session') for x in state.samples[:5000]}) < 2:
            return s, 'needs several sessions / cameras (image names like cam1_0001.jpg)'
    fu = ((s.get('prep') or {}).get('feature') or {}).get('fuse')
    if fu and fu['feature'] not in state.registry:
        return s, f'needs the {fu["feature"]} feature'
    return s, ''


def for_state(state):
    """Every recipe with {available, why, sets (resolved), instant (its map is computed already)}."""
    from ..state import normalize_params, projection_key
    out = []
    for r in RECIPES.values():
        s, why = resolve(r, state)
        instant = False
        if not why and state.ready and 'feature' in s and 'method' in s:
            try:
                key = projection_key(s['feature'], s['method'], normalize_params(s['method'], s.get('params')), None,
                                     s.get('dims', 2), mprep.validate(s.get('prep')))
                instant = state.get_projection(key) is not None
            except ValueError:
                pass
        out.append({'id': r['id'], 'name': r['name'], 'category': r['category'], 'use_for': r['use_for'],
                    'needs': list(r.get('needs') or ()), 'sets': s, 'available': not why, 'why': why,
                    'instant': instant})
    return out


def merge(current, sets):
    """The Map settings after a recipe: its fields replace the current ones; its prep keys replace the current
    prep's keys one by one (a filter replaces the filter as a whole). current / result: {feature, method, params,
    dims, prep, color}."""
    out = {**current}
    for k in ('feature', 'method', 'dims', 'color'):
        if k in sets:
            out[k] = sets[k]
    if 'method' in sets:
        out['params'] = {**analysis.METHODS[sets['method']]['params'], **(sets.get('params') or {})}
    elif 'params' in sets:
        out['params'] = {**(current.get('params') or {}), **sets['params']}
    if 'prep' in sets:
        cur = current.get('prep') or {}
        new = {}
        for part in ('image', 'feature'):
            merged = {**(cur.get(part) or {}), **((sets['prep'].get(part)) or {})}
            if merged:
                new[part] = merged
        out['prep'] = mprep.validate(new)
    return out
