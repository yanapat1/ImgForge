"""
Page shell and dataset-level endpoints: the page itself, plotly.js, status (polled at startup),
remembered UI settings, background jobs, dataset meta, the columnar points payload, workspace info.
"""

import json
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from ...ml import analysis
from ...paths import STATIC_DIR
from ...workspace import list_workspaces
from ..deps import guards, json_bytes
from ..static import NO_CACHE, render_index


def router(state, ctx):
    r = APIRouter()
    need_index = guards(state).need_index

    @r.get('/')
    def index_page():
        return HTMLResponse(render_index(STATIC_DIR), headers=NO_CACHE)

    @r.get('/plotly.min.js')
    def plotly_js():
        import plotly
        return FileResponse(Path(plotly.__file__).parent / 'package_data' / 'plotly.min.js',
                            media_type='application/javascript', headers={'Cache-Control': 'max-age=86400'})

    @r.get('/api/status')
    def status():
        st, cfg = ctx.state, ctx.cfg            # st is None on the Sessions home (no session open)
        out = {
            'ready': bool(st and st.ready),
            'methods': analysis.METHODS,
            'ui': {**cfg['ui'], 'groups': cfg['analysis']['groups'],
                   'duplicate_threshold': cfg['analysis']['duplicate_threshold'], 'config': cfg.get('_path')},
            'jobs': ctx.jobs.list()[-8:],
            'active': ctx.jobs.active(),
            'dataset': None,
            'llm': ctx.llm.state() if ctx.llm else None,
        }
        if st is None:
            return out
        out.update({
            'default_feature': st.default_feature,
            'startup': {'feature': st.default_feature, 'method': st.startup_method, 'cached': st.embedding_cached()},
            'features': [{'key': s.key, 'label': s.label, 'group': s.group, 'note': s.note, 'text': bool(s.text_model),
                          'cached': st.feature_cached(s.key)} for s in st.registry.values()],
            'dataset': {'id': st.dataset_id, 'name': st.source.name, 'uri': st.source.uri,
                        'kind': st.source.kind, 'mode': getattr(st.source, 'mode', None),
                        'index_job': st.index_job.id if st.index_job else None,
                        'stale': st.stale},
        })
        return out

    @r.get('/api/settings')
    def get_settings():
        return state.load_settings()

    @r.post('/api/settings')
    def set_settings(settings: dict):
        state.save_settings(settings)
        return {'ok': True}

    size_cache = {'t': 0.0, 'size': 0, 'tag': None}

    @r.get('/api/workspace')
    def workspace():
        need_index()
        if time.time() - size_cache['t'] > 60 or size_cache['tag'] != state.tag:
            size_cache.update(t=time.time(), tag=state.tag,
                              size=sum(f.stat().st_size for f in state.cache_dir.rglob('*') if f.is_file()))
        size = size_cache['size']
        meta_path = state.cache_dir / 'workspace.json'
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        return {
            'name': state.cache_dir.name, 'path': str(state.cache_dir), 'created': meta.get('created'),
            'size_mb': round(size / 1e6, 1),
            'n_features': sum(1 for k in state.registry if state.feature_cached(k)) if state.ready else 0,
            'n_projections': len(state.saved_projections()),
            'others': [w for w in list_workspaces(state.cache_dir.parent, state.source.name)
                       if w['name'] != state.cache_dir.name],
        }

    @r.get('/api/jobs')
    def jobs():
        return {'jobs': ctx.jobs.list(), 'active': ctx.jobs.active()}

    @r.get('/api/jobs/{jid}')
    def job(jid: int):
        j = ctx.jobs.get(jid)
        if j is None:
            raise HTTPException(404, f'no job {jid}')
        return j.to_dict()

    @r.post('/api/jobs/{jid}/cancel')
    def cancel_job(jid: int):
        """Stop a job: a queued one never starts, a running one stops at its next check (between batches)."""
        if ctx.jobs.get(jid) is None:
            raise HTTPException(404, f'no job {jid}')
        ctx.jobs.cancel(jid)
        return {'ok': True}

    @r.get('/api/meta')
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

    @r.get('/api/points')
    def points():
        need_index()
        s = state.samples
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
