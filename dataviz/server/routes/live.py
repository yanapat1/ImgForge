"""
The live change feed of a labeling session (or a combined dataset with labeling members), under /s/<id>/:

    GET /api/live   ?since=&wait=25&proj=  long poll (data/live.py LiveFeed.poll):
                    {gen, live, patches: [{gen, cls, deleted, restored, moved, new, diff?, pending_new}], pending_new,
                     reindex?, resync?}; without `since`: {gen, live} (where the page starts)
                    sessions with no labeling source: {live: false, gen: 0} at once
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ...auth import deps
from ...data import live
from ..deps import guards

NO_STORE = {'Cache-Control': 'no-store'}


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.require_bound('viewer'))])
    g = guards(state)

    @r.get('/api/live')
    async def live_poll(since: int | None = None, wait: float = 25, proj: str | None = None):
        g.need_index()
        st = ctx.current()
        kind = getattr(st.source, 'kind', None)
        if kind != 'labeling' and not (kind == 'union' and getattr(st.source, 'live', False)):
            return JSONResponse({'live': False, 'gen': 0}, headers=NO_STORE)
        feed = live.feed_for(st)
        return JSONResponse(await feed.poll(since, wait, proj), headers=NO_STORE)

    return r
