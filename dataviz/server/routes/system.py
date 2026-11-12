"""
Page shell: the page itself (home at /, the visualizer at /s/<id>/), plotly.js, status (polled at startup) and
background jobs. Core: every install has these. What one visualizer session serves is in session_data.py.

    GET /                  the home page; /?session=<id> (old links) -> 302 s/<id>/
    GET /s/<id>/           the visualizer page (assets under ../../static/), marks the session opened by me
    GET /api/status        works on the home page too (dataset: null); my_role + user
    /api/jobs              only jobs of sessions I can open, or that I started
"""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ... import zones
from ...auth import context, deps, tiers
from ...paths import STATIC_DIR
from ..deps import capabilities
from ..static import NO_CACHE, render_index


def public_user(u):
    if not u:
        return None
    return {**{k: u.get(k) for k in ('id', 'username', 'display_name', 'is_admin', 'avatar_ver')}, **tiers.describe(u)}


def projection_methods(cfg):
    """The visualizer's projection methods; none in an install without it."""
    if not zones.on('visualizer', cfg):
        return []
    from ...ml import analysis
    return analysis.METHODS


def router(state, ctx):
    r = APIRouter()
    signed_in = [Depends(deps.current_user)]

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
                from ...image import sessions
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
            'methods': projection_methods(cfg),
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

    return r
