"""Helpers for the auth tests: a small app with only the auth layer, accounts with real passwords, clients."""

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

PW = 'correct horse battery'
XRW = {'X-Requested-With': 'dataviz'}


def make_app(cfg_patch=None):
    from dataviz.auth import deps
    from dataviz.auth.middleware import install_auth
    from dataviz.core.config import load_config
    cfg = load_config()
    for k, v in (cfg_patch or {}).items():
        cfg['auth'][k] = v
    app = FastAPI()
    install_auth(app, cfg)

    @app.get('/api/ping')
    def ping(user=Depends(deps.current_user)):
        return {'user': user}

    @app.post('/api/ping')
    def ping_post(user=Depends(deps.current_user)):
        return {'ok': True}

    @app.get('/s/{sid}/')
    def viz_page(sid: str):
        return HTMLResponse('page')

    @app.get('/')
    def home():
        return HTMLResponse('home')
    return app


def client(app, **kw):
    return TestClient(app, headers=XRW, **kw)


def account(username, admin=False, password=PW, display_name=None):
    from dataviz.auth import users
    return users.create(username, f'{username}@example.test', display_name or username.title(), password,
                        is_admin=admin)


def login(c, login_, password=PW):
    return c.post('/api/auth/login', json={'login': login_, 'password': password})


def signed_in(app, username, admin=False, password=PW):
    """(client, user) for a fresh account, already logged in."""
    u = account(username, admin, password)
    c = client(app)
    r = login(c, username, password)
    assert r.status_code == 200, r.text
    return c, u
