"""Smoke test of the Data Center page (?data, static/ts/datacenter/): the real app.py with a fresh PostgreSQL database,
headless Chromium signed in as the first admin. A managed bucket is created and filled through the API
(dataviz/datacenter/API.md); the page must show it in the tree and as a card, open it, and switch between
Thumbnails and Names only (the choice survives a reload: it is a user pref). Fails on any page error or console
error. Skips without playwright (+ `playwright install chromium`)."""

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from conftest import TEST_SECRET_KEY, make_jpeg
from test_page_smoke import ADMIN, _HEADERS, _HTTP, _JAR, _browser, _free_port, _get, _make, _send

sync_api = pytest.importorskip('playwright.sync_api')

HERE = Path(__file__).resolve().parents[1]


def _upload(url, files):
    """POST multipart/form-data with one `files` part per (name, bytes)."""
    boundary = uuid.uuid4().hex
    body = b''
    for name, data in files:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                 'Content-Type: image/jpeg\r\n\r\n').encode() + data + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': f'multipart/form-data; boundary={boundary}', 'X-Requested-With': _HEADERS['X-Requested-With']})
    with _HTTP.open(req, timeout=30) as r:
        return json.loads(r.read())


@pytest.fixture
def server(pg, tmp_path):
    ds, cfg = _make(tmp_path)
    conf = json.loads(cfg.read_text())
    conf['storage'] = {'allowed_roots': [str(tmp_path)], 'data_root': str(tmp_path / 'dc-data')}   # imports land here
    cfg.write_text(json.dumps(conf))
    port = _free_port()
    env = {**os.environ, 'DATABASE_URL': pg, 'DATAVIZ_SECRET_KEY': TEST_SECRET_KEY,
           'DATAVIZ_ADMIN_USERNAME': ADMIN[0], 'DATAVIZ_ADMIN_PASSWORD': ADMIN[1]}
    proc = subprocess.Popen([sys.executable, str(HERE / 'app.py'), '--config', str(cfg), '--dataset', str(ds),
                             '--host', '127.0.0.1', '--port', str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.fail(f'app.py exited:\n{proc.stdout.read()[-3000:]}')
            try:
                _get(f'{base}/api/auth/config')
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail('app.py did not answer in 60 s')
        _JAR.clear()
        _send(f'{base}/api/auth/login', {'login': ADMIN[0], 'password': ADMIN[1]})
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_data_center_tree_cards_and_view_toggle(server, tmp_path):
    b = _send(f'{server}/api/dc/buckets', {'name': 'smoke-bucket', 'kind': 'managed', 'tags': ['smoke']})
    bid = b['id']
    imgs = []
    for k in range(6):
        p = tmp_path / f'up{k}.jpg'
        make_jpeg(p, 64, 48, color=(40 * k, 200 - 20 * k, 90))
        imgs.append((p.name, p.read_bytes()))
    _upload(f'{server}/api/dc/buckets/{bid}/upload?path=', imgs)
    assert _get(f'{server}/api/dc/buckets/{bid}/ls?path=')['total'] == 6

    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/?data')
        assert page.evaluate('document.documentElement.dataset.screen') == 'data'
        # tree row + card for the bucket; the Data tab is the current one
        page.wait_for_selector(f'#dc-tree .t-row[data-bid="{bid}"]', timeout=30000)
        page.wait_for_selector(f'#dc-body .dc-bcard[data-bid="{bid}"]')
        assert 'smoke-bucket' in page.locator(f'#dc-body .dc-bcard[data-bid="{bid}"]').inner_text()
        assert page.locator('.dc-header .hdr-nav a.on').inner_text().startswith('Data')
        assert page.locator('#home').is_hidden()
        # a click focuses it (details on the right), its name opens it
        page.click(f'#dc-body .dc-bcard[data-bid="{bid}"] .dc-mosaic')
        page.wait_for_selector('#dc-details .d-title h3:has-text("smoke-bucket")')
        page.click(f'#dc-body .dc-bcard[data-bid="{bid}"] .bc-name')
        page.wait_for_function(f'location.search.includes("data={bid}")')
        # thumbnails: 6 tiles, the visible ones load ?w= thumbnails
        page.wait_for_selector('#dc-grid .dc-th')
        assert page.locator('#dc-grid .dc-th').count() == 6
        page.wait_for_selector('#dc-grid .dc-th img[src*="w=256"]', timeout=15000)
        # Names only: a list row per file, no tiles; remembered across a reload (user pref)
        page.click('#dc-view-names')
        page.wait_for_selector('#dc-grid .dc-lrow')
        assert page.locator('#dc-grid .dc-lrow').count() == 6
        assert page.locator('#dc-grid .dc-th').count() == 0
        assert page.get_attribute('#dc-view-names', 'aria-pressed') == 'true'
        page.wait_for_timeout(800)                  # the pref PUT is debounced
        page.reload()
        page.wait_for_selector('#dc-grid .dc-lrow', timeout=30000)
        assert page.get_attribute('#dc-view-names', 'aria-pressed') == 'true'
        # selection: click + Ctrl-click -> the bulk bar and the selection details
        rows = page.locator('#dc-grid .dc-lrow')
        rows.nth(0).click()
        rows.nth(2).click(modifiers=['Control'])
        page.wait_for_selector('#dc-selbar:not(.hidden)')
        assert '2 selected' in page.locator('#dc-selbar').inner_text()
        page.keyboard.press('Escape')
        page.wait_for_selector('#dc-selbar.hidden', state='attached')
        # back to Thumbnails
        page.click('#dc-view-thumbs')
        page.wait_for_selector('#dc-grid .dc-th')
        assert page.locator('#dc-grid .dc-lrow').count() == 0
        # the browser's back button returns to All buckets
        page.go_back()
        page.wait_for_selector(f'#dc-body .dc-bcard[data-bid="{bid}"]')
        browser.close()
    assert not errors, '\n'.join(errors)
