"""The page of a Bucket install (no visualizer, no labeling), in a real browser, with their libraries blocked."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import TEST_SECRET_KEY
from test_zones import APP_DIR, SERVE, VISUALIZER_LIBS

sync_api = pytest.importorskip('playwright.sync_api')
PASSWORD = 'zone-test-password-1'


@pytest.fixture
def dc_server(pg, tmp_path):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(json.dumps({'dataset': {'workspace_root': str(tmp_path / 'ws')}, 'workers': {'enabled': False},
                               'logging': {'level': 'WARNING'}}))
    env = {**os.environ, 'DATAVIZ_ZONES': 'core,bucket', 'DATABASE_URL': pg, 'DATAVIZ_SECRET_KEY': TEST_SECRET_KEY,
           'DATAVIZ_ADMIN_USERNAME': 'admin', 'DATAVIZ_ADMIN_PASSWORD': PASSWORD, 'PYTHONPATH': str(APP_DIR)}
    proc = subprocess.Popen([sys.executable, '-c', SERVE, ','.join(VISUALIZER_LIBS), '--config', str(cfg),
                             '--host', '127.0.0.1', '--port', str(port)], cwd=APP_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.fail(f'the server exited:\n{proc.stdout.read()[-3000:]}')
            try:
                urllib.request.urlopen(f'{base}/api/auth/config', timeout=5).read()
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail('the server did not answer in 60 s')
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_bucket_page_has_nothing_of_the_missing_zones(dc_server):
    problems = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: problems.append(f'pageerror: {e}'))
        page.on('response', lambda r: problems.append(f'{r.status} {r.url}')
                if r.status >= 400 and '/api/' in r.url and '/api/auth/me' not in r.url else None)
        page.request.post(f'{dc_server}/api/auth/login', data={'login': 'admin', 'password': PASSWORD},
                          headers={'X-Requested-With': 'dataviz'})
        page.goto(dc_server)                                       # Home: only the Bucket card in this install
        page.wait_for_selector('#door-bucket .door-recent, #door-bucket .door-empty', timeout=20000)
        assert page.locator('.door').count() == 1 and page.locator('#door-image, #door-data').count() == 0
        assert [a.strip() for a in page.locator('#home .hdr-nav a').all_inner_texts()] == ['Bucket']
        page.goto(f'{dc_server}/?bucket')
        page.wait_for_selector('.bk-header', timeout=20000)
        assert page.locator('.bk-header .hdr-nav a:has-text("Image")').count() == 0
        page.wait_for_timeout(500)

        bid = page.evaluate("""async () => (await (await fetch('api/bucket/buckets', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Requested-With': 'dataviz'},
            body: JSON.stringify({name: 'finance'})})).json()).id""")
        assert bid
        page.goto(f'{dc_server}/?bucket={bid}')
        page.wait_for_selector('[data-act="send-menu"]', timeout=20000)
        page.click('[data-act="send-menu"]')
        menu = page.locator('.bk-menu, [role="menu"]').first.inner_text()
        assert 'Visualize' not in menu and 'Label' not in menu
        page.keyboard.press('Escape')

        page.click('.bk-header [data-act="settings"]')
        page.wait_for_selector('.snav[data-sec="connections"].active', timeout=10000)
        for sec in ('datasets', 'models', 'labeling'):
            assert not page.locator(f'.snav[data-sec="{sec}"]').is_visible()
        page.wait_for_timeout(500)
        browser.close()
    assert not problems, problems
