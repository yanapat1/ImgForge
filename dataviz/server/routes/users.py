"""
Other people, as any signed-in user may see them: search by username / display name (for the share dialog;
never the email) and avatars (served only as image/webp, behind a versioned URL so they cache for a day).
"""

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from ...auth import users
from ...auth.deps import current_user


def router(cfg, limiter):
    r = APIRouter()

    @r.get('/api/users/search')
    def search(q: str = '', user=Depends(current_user)):
        return {'users': users.search(q)}

    @r.get('/api/users/{uid}/avatar')
    def avatar(uid: str, user=Depends(current_user)):
        hit = users.avatar(uid)
        if hit is None:
            raise HTTPException(404, 'no avatar')
        return Response(hit[0], media_type='image/webp', headers={'Cache-Control': 'private, max-age=86400'})

    return r
