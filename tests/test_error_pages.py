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
