"""
What one open visualizer session serves: remembered UI settings (per user and session), workspace info, dataset meta
and the columnar points payload.
"""

import json
import time
from collections import OrderedDict

import numpy as np
from fastapi import APIRouter, Depends

from ...auth import context, deps
from ...workspace import list_workspaces
from ..deps import guards, json_bytes


def router(state, ctx):
    r = APIRouter()
    need_index = guards(state).need_index
    viewer = [Depends(deps.require_bound('viewer'))]

    @r.get('/api/settings', dependencies=viewer)
    def get_settings():
        return state.load_settings(context.user_id())

    @r.post('/api/settings', dependencies=viewer)
    def set_settings(settings: dict):
        state.save_settings(settings, context.user_id())     # mine only: other users keep their own
        return {'ok': True}

    size_cache = OrderedDict()                   # State.tag -> (time, bytes), one entry per loaded session
    ctx.close_hooks.append(lambda tag: size_cache.pop(tag, None))

    @r.get('/api/workspace', dependencies=viewer)
    def workspace():
        need_index()
        tag = state.tag
        hit = size_cache.get(tag)
        if hit is None or time.time() - hit[0] > 60:
            hit = size_cache[tag] = (time.time(), sum(f.stat().st_size for f in state.cache_dir.rglob('*') if f.is_file()))
            while len(size_cache) > ctx.max_loaded + 2:
                size_cache.popitem(last=False)
        size_cache.move_to_end(tag)
        size = hit[1]
        meta_path = state.cache_dir / 'workspace.json'
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        b = context.bound()
        owner = b is not None and b.role == 'owner'
        return {
            'name': state.cache_dir.name, 'path': str(state.cache_dir) if owner else None, 'created': meta.get('created'),
            'size_mb': round(size / 1e6, 1),
            'n_features': sum(1 for k in state.registry if state.feature_cached(k)) if state.ready else 0,
            'n_projections': len(state.saved_projections()),
            'others': [w for w in list_workspaces(state.cache_dir.parent, state.source.name)
                       if w['name'] != state.cache_dir.name] if owner else [],
        }

    @r.get('/api/meta', dependencies=viewer)
    def meta():
        need_index()
        counts = np.bincount(state.labels, minlength=len(state.classes)) if len(state.labels) else \
            np.zeros(len(state.classes), int)
        return {
            'dataset': state.source.name,
            'dataset_uri': state.source.uri,
            'source': state.source.kind,
            'n_samples': len(state.samples),
            'n_images': len(state.by_image),
            'classes': state.classes,
            'counts': counts.tolist(),
            'palette': state.palette,
            'palette_light': state.palette_light,
            'knn_k': state.knn_k,
            'index_stats': state.index['stats'],
            # a combined dataset: its members, in order (alias = the name prefix; origin = the position)
            'members': [{k: m.get(k) for k in ('alias', 'name', 'sid', 'n', 'n_images', 'offset', 'unit')}
                        for m in state.members] or None,
        }

    @r.get('/api/points', dependencies=viewer)
    def points():
        need_index()
        s = state.samples
        if state.source.kind == 'embeddings':          # rows, not boxes: metadata columns instead of box geometry
            from ...image.embedding_source import shown_previews
            types = state.index.get('meta_types') or {}
            urls = bool((state.cfg.get('embeddings') or {}).get('allow_url_previews'))
            return json_bytes(state.payload('points', lambda: {
                'id': list(range(len(s))),
                'cls': state.labels,
                'image': [x['image'] for x in s],
                'session': [x['session'] for x in s],
                'row_id': [x['row_id'] for x in s],
                'preview': [shown_previews(x['preview'], urls) for x in s],
                'text': [x['preview'].get('text', '')[:300] for x in s] if any('text' in x['preview'] for x in s) else None,
                'meta': {c: [x['meta'].get(c) for x in s] for c in types},
                'meta_types': types,
            }))
        def build():
            out = {
                'id': list(range(len(s))),
                'cls': state.labels,
                'image': [x['image'] for x in s],
                'session': [x['session'] for x in s],
                'area': [x['area'] for x in s],
                'aspect': [x['aspect'] for x in s],
                'brightness': [x['brightness'] for x in s],
                'blur': [x['blur'] for x in s],
            }
            if state.origin is not None:               # combined dataset: member of each box + same-content group
                from ...image import imagehash
                imgs = (imagehash.load(state.cache_dir) or {}).get('images') or {}
                out['origin'] = state.origin
                out['hgroup'] = [(imgs.get(x['image']) or {}).get('g', -1) for x in s]
            return out
        return json_bytes(state.payload('points', build))

    return r
