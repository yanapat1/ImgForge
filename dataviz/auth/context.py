"""
Who is asking, and which visualizer session the request is bound to, for the length of one request.

Set by the outermost ASGI middleware (auth/middleware.py sets USER, server/routing.py sets BOUND); read
anywhere below it — route handlers, the state proxy, repositories. contextvars follow the request into
FastAPI's thread pool (anyio copies the context), but NOT into loop.run_in_executor or the JobManager's
worker threads: resolve what a job needs (user id, State) in the handler and pass it in.

A user is a plain dict: {id, username, email, display_name, is_admin, avatar_ver}.
"""

from contextvars import ContextVar
from dataclasses import dataclass

from starlette.exceptions import HTTPException

USER: ContextVar = ContextVar('dataviz_user', default=None)
BOUND: ContextVar = ContextVar('dataviz_bound', default=None)


@dataclass(frozen=True)
class Bound:
    """The /s/<session_id>/ a request came through: the session, the caller's role on it, its loaded State."""
    session_id: str
    role: str                     # 'owner' | 'labeler' | 'viewer'  (admins are 'owner')
    state: object                 # dataviz.state.State


def user():
    """The signed-in user (dict) or None."""
    return USER.get()


def require_user():
    u = USER.get()
    if u is None:
        raise HTTPException(401, 'sign in first')
    return u


def user_id():
    u = USER.get()
    return u['id'] if u else None


def bound():
    """The Bound of this request, or None outside /s/<id>/."""
    return BOUND.get()
