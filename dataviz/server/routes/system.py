"""
Page shell and dataset-level endpoints: the page itself (home at /, the visualizer at /s/<id>/), plotly.js,
status (polled at startup), remembered UI settings (per user and session), background jobs, dataset meta, the
columnar points payload, workspace info.

    GET /                  the home page; /?session=<id> (old links) -> 302 s/<id>/
    GET /s/<id>/           the visualizer page (assets under ../../static/), marks the session opened by me
    GET /api/status        works on the home page too (dataset: null); my_role + user
    /api/jobs              only jobs of sessions I can open, or that I started
"""

import json
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ...auth import context, deps
from ...ml import analysis
from ...paths import STATIC_DIR
from ...workspace import list_workspaces
from ..deps import capabilities, guards, json_bytes
from ..static import NO_CACHE, render_index


def public_user(u):
    return {k: u.get(k) for k in ('id', 'username', 'display_name', 'is_admin', 'avatar_ver')} if u else None


def router(state, ctx):
    r = APIRouter()
    need_index = guards(state).need_index
    signed_in = [Depends(deps.current_user)]
    viewer = [Depends(deps.require_bound('viewer'))]

    @r.get('/')
    def index_page(session: str | None = None):
        b = context.bound()
        if b is None or not b.session_id:
            if session:                         # old /?session=<id> links: the session's own URL now
                return RedirectResponse(f's/{quote(session, safe="")}/', status_code=302)
            return HTMLResponse(render_index(STATIC_DIR), headers=NO_CACHE)
        uid = context.user_id()
        if uid:
            try:
                from ...data import sessions
                sessions.mark_opened(uid, b.session_id)
            except Exception:  # noqa: BLE001 - "last opened" is a nicety
                pass
        return HTMLResponse(render_index(STATIC_DIR, prefix='../../'), headers=NO_CACHE)

    @r.get('/plotly.min.js')
    def plotly_js():
        import plotly
        return FileResponse(Path(plotly.__file__).parent / 'package_data' / 'plotly.min.js',
                            media_type='application/javascript', headers={'Cache-Control': 'max-age=86400'})

    def visible():
        return ctx.job_filter(context.user())

    @r.get('/api/status', dependencies=signed_in)
    def status():
        st, cfg = ctx.state, ctx.cfg            # st is None on the Sessions home (no session open)
        b = context.bound()
        vis = visible()
        out = {
            'ready': bool(st and st.ready),
            'methods': analysis.METHODS,
            'ui': {**cfg['ui'], 'groups': cfg['analysis']['groups'],
                   'duplicate_threshold': cfg['analysis']['duplicate_threshold'], 'config': cfg.get('_path')},
            'jobs': ctx.jobs.list(vis)[-8:],
            'active': ctx.jobs.active(vis),
            'dataset': None,
            'my_role': b.role if b is not None else None,
            'user': public_user(context.user()),
            'llm': ctx.llm.state() if ctx.llm else None,
        }
        if st is None:
            return out
        out.update({
            'default_feature': st.default_feature,
            'startup': {'feature': st.default_feature, 'method': st.startup_method, 'cached': st.embedding_cached()},
            'features': [{'key': s.key, 'label': s.label, 'group': s.group, 'note': s.note, 'text': bool(s.text_model),
                          'metric': s.metric, 'cached': st.feature_cached(s.key)} for s in st.registry.values()],
            'caps': capabilities(st),
            'dataset': {'id': st.dataset_id, 'name': st.source.name, 'uri': st.source.uri,
                        'kind': st.source.kind, 'mode': getattr(st.source, 'mode', None),
                        'modality': (st.index or {}).get('modality'),
                        'index_job': st.index_job.id if st.index_job else None,
                        'stale': st.stale},
        })
        return out

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

    @r.get('/api/jobs', dependencies=signed_in)
    def jobs():
        vis = visible()
        return {'jobs': ctx.jobs.list(vis), 'active': ctx.jobs.active(vis)}

    def job_or_404(jid):
        j = ctx.jobs.get(jid)
        vis = visible()
        if j is None or (vis is not None and not vis(j)):
            raise HTTPException(404, f'no job {jid}')
        return j

    @r.get('/api/jobs/{jid}', dependencies=signed_in)
    def job(jid: int):
        return job_or_404(jid).to_dict()

    @r.post('/api/jobs/{jid}/cancel', dependencies=signed_in)
    def cancel_job(jid: int):
        """Stop a job: a queued one never starts, a running one stops at its next check (between batches).
        Who started it, an admin or the owner of its session."""
        j = job_or_404(jid)
        ctx.can_cancel(context.user(), j)
        ctx.jobs.cancel(jid)
        return {'ok': True}

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
        }

    @r.get('/api/points', dependencies=viewer)
    def points():
        need_index()
        s = state.samples
        if state.source.kind == 'embeddings':          # rows, not boxes: metadata columns instead of box geometry
            from ...data.embedding_source import shown_previews
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
        return json_bytes(state.payload('points', lambda: {
            'id': list(range(len(s))),
            'cls': state.labels,
            'image': [x['image'] for x in s],
            'session': [x['session'] for x in s],
            'area': [x['area'] for x in s],
            'aspect': [x['aspect'] for x in s],
            'brightness': [x['brightness'] for x in s],
            'blur': [x['blur'] for x in s],
        }))

    return r
