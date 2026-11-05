"""Unknown URLs and server crashes: an HTML error page for browsers, JSON for the API."""

from fastapi.testclient import TestClient

HTML = {'accept': 'text/html'}


def test_unknown_url_html(client):
    r = client.get('/nope', headers=HTML)
    assert r.status_code == 404
    assert 'text/html' in r.headers['content-type'] and 'Page not found' in r.text


def test_unknown_api_stays_json(client):
    r = client.get('/api/nope', headers=HTML)
    assert r.status_code == 404 and r.json()['detail']


def test_unknown_url_json_without_html_accept(client):
    assert client.get('/nope').json()['detail']


def test_crash_is_500_page(app_ctx):
    app = app_ctx[1]

    @app.get('/boom')
    def boom():
        raise RuntimeError('<script>x</script>')
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get('/boom', headers=HTML)
        assert r.status_code == 500 and 'The server hit a problem' in r.text and '<script>x' not in r.text
        assert c.get('/boom').json() == {'detail': 'internal server error'}


def test_error_page_links_are_relative(client):
    # behind a path prefix (a proxy) absolute /static links break the page: they are relative to the URL asked for
    r = client.get('/a/b/nope', headers=HTML)
    assert r.status_code == 404 and 'href="../../static/css/tokens.css"' in r.text and '/static/' not in r.text.replace('../static/', '')


def test_missing_session_folder_gets_its_own_page(monkeypatch):
    # a session whose dataset folder is gone: a page that says so (title + what to do), the path as the detail
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse

    from dataviz.auth import access, context
    from dataviz.server import routing

    class Ctx:
        def state_for(self, sid, user):
            raise ValueError('not a folder: /data/gone')
    seen = []

    def page(code, title, message, path='/', detail=''):
        seen.append((code, title, path, detail))
        return HTMLResponse(title, status_code=code)
    monkeypatch.setattr(context, 'user', lambda: {'id': 'u'})
    monkeypatch.setattr(access, 'viz_role', lambda user, sid: 'owner')
    app = routing.SessionRouting(Starlette(), Ctx(), error_page=page)
    with TestClient(app) as c:
        r = c.get('/s/abc/', headers=HTML)
        assert r.status_code == 409 and seen == [(409, routing._MISSING_TITLE, '/s/abc/', 'cannot open this session: not a folder: /data/gone')]
        assert c.get('/s/abc/api/points', headers=HTML).json()['detail'].endswith('/data/gone')
