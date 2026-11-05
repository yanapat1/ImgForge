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

HERE = Path(__file__).resolve().parents[2]


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
        top = rows.nth(2).bounding_box()['y']
        rows.nth(0).click()
        rows.nth(2).click(modifiers=['Control'])
        page.wait_for_selector('#dc-selbar:not(.hidden)')
        assert '2 selected' in page.locator('#dc-selbar').inner_text()
        assert rows.nth(2).bounding_box()['y'] == top     # the bar lies over the folder bar: the list does not move
        page.keyboard.press('Escape')
        page.wait_for_selector('#dc-selbar.hidden', state='attached')
        # back to Thumbnails
        page.click('#dc-view-thumbs')
        page.wait_for_selector('#dc-grid .dc-th')
        assert page.locator('#dc-grid .dc-lrow').count() == 0
        # right-click menu: a left click anywhere outside it closes it, even on the tile that opened it or on the
        # empty folder area (the menu's anchor there is the whole body)
        tile = page.locator('#dc-grid .dc-th').nth(1)
        tile.click(button='right')
        page.wait_for_selector('#dc-menu:not(.hidden)')
        tile.click()
        page.wait_for_selector('#dc-menu.hidden', state='attached')
        box = page.locator('#dc-body').bounding_box()
        page.mouse.click(box['x'] + box['width'] - 20, box['y'] + box['height'] - 20, button='right')
        page.wait_for_selector('#dc-menu:not(.hidden)')
        page.mouse.click(box['x'] + box['width'] - 40, box['y'] + box['height'] - 40)
        page.wait_for_selector('#dc-menu.hidden', state='attached')
        # the browser's back button returns to All buckets
        page.go_back()
        page.wait_for_selector(f'#dc-body .dc-bcard[data-bid="{bid}"]')
        browser.close()
    assert not errors, '\n'.join(errors)


def test_bucket_card_delete_and_edit_actions(server, tmp_path):
    """A card has ✎ Edit and 🗑 Delete; the Edit dialog's Actions are tiles plus a separate Delete strip; the card's 🗑
    asks first, then removes the bucket."""
    bid = _send(f'{server}/api/dc/buckets', {'name': 'card-bucket', 'kind': 'managed'})['id']
    shots = os.environ.get('DC_SHOTS')              # a folder: also save screenshots of the dialog and card
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/?data')
        card = page.locator(f'#dc-body .dc-bcard[data-bid="{bid}"]')
        card.wait_for(timeout=30000)
        card.hover()
        dele = card.locator('button.bc-del[data-act="delete-bucket"]')
        assert dele.get_attribute('aria-label') == 'Delete card-bucket'
        if shots:
            card.screenshot(path=f'{shots}/card.png')
        # the Edit dialog: four action tiles, then the Delete strip
        card.locator('button[data-act="edit-bucket"]').click()
        dlg = page.locator('.ui-dlg:has(.dc-edit)')
        dlg.wait_for()
        assert [t.get_attribute('data-ed') for t in dlg.locator('.dc-edit-acts .dc-edit-act').all()] == \
            ['export', 'export-to', 'redetect', 'autolabel']
        assert 'Delete this bucket' in dlg.locator('.dc-edit-danger').inner_text()
        assert dlg.locator('.dc-edit-act .ico').first.bounding_box()['width'] > 0
        if shots:
            dlg.screenshot(path=f'{shots}/edit.png')
            page.emulate_media(color_scheme='dark')
            page.evaluate("document.documentElement.dataset.theme = 'dark'")
            dlg.screenshot(path=f'{shots}/edit-dark.png')
            page.evaluate("document.documentElement.dataset.theme = 'light'")
        dlg.locator('[data-dlg="cancel"]').first.click()
        dlg.wait_for(state='detached')
        # the card's 🗑: a confirmation first (the card is still there), then it is gone
        card.hover()
        dele.click()
        conf = page.locator('.ui-dlg:has-text("Delete card-bucket?")')
        conf.wait_for()
        assert card.count() == 1
        conf.locator('[data-dlg="ok"]').click()
        card.wait_for(state='detached', timeout=15000)
        browser.close()
    assert all(x['id'] != bid for x in _get(f'{server}/api/dc/buckets'))
    assert not errors, '\n'.join(errors)


def test_data_center_assistant_dock_chat(server):
    """The ◆ assistant in the right-side dock (datacenter/ai.ts on core/dock.ts + core/chat.ts): the answer of a fake
    OpenAI-compatible model streams in, the Tools fold lists the catalogue's tools, Shift+Tab in #dc-ai-q switches
    the mode chip (and the mode sent), and the chat is saved (History)."""
    from test_llm import REPLY, fake_app
    from test_models import _serve
    REPLY['text'] = 'Hello there, this is the fake model.'
    fake, url = _serve(fake_app())
    try:
        p = _send(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                  'model': 'gpt-x', 'vision': True})['provider']
        assert _send(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _send(f'{server}/api/llm/config', {'features': {'data_center': True}}, 'PUT')
        errors = []
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1600, 'height': 1000})
            page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
            page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
            page.goto(f'{server}/?data')
            page.wait_for_selector('#ai-dock #dc-ai #dc-ai-q', timeout=30000)
            page.fill('#dc-ai-q', 'Which buckets are there?')
            page.press('#dc-ai-q', 'Enter')
            page.wait_for_selector('#dc-ai .chat .ai-msg.bot:has-text("fake model")', timeout=20000)
            page.click('#dc-ai [data-dcai="tools"]')
            page.wait_for_selector('#dc-ai .dc-ai-tools .tchip', timeout=10000)
            assert page.inner_text('#dc-ai .chat-chip.mode').strip() == 'ask'
            page.focus('#dc-ai-q')
            page.keyboard.press('Shift+Tab')
            assert page.inner_text('#dc-ai .chat-chip.mode').strip() == 'auto'
            page.fill('#dc-ai-q', 'And now?')
            with page.expect_request(lambda r: '/api/llm/agent/data/chat' in r.url) as req:
                page.press('#dc-ai-q', 'Enter')
            sent = json.loads(req.value.post_data)
            assert sent['mode'] == 'auto' and sent['session_id'] and 'selection' in sent['scope'], sent
            page.wait_for_function("document.querySelectorAll('#dc-ai .chat-turn.ai').length >= 2 && "
                                   "document.querySelector('#dc-ai .chat').getAttribute('aria-busy') === 'false'", timeout=20000)
            page.click('#dc-ai [data-ca="history"]')
            page.wait_for_selector('#dc-ai .chat-hrow.cur', timeout=5000)      # the title may be improved by a title call
            browser.close()
        assert not errors, '\n'.join(errors)
    finally:
        fake.should_exit = True


def test_data_center_extract_frames(server, tmp_path):
    """A video tile shows its poster; double-click opens Extract frames (player, meta, live estimate + film strip);
    the "burst" preset runs as a job and the page opens the new frames folder."""
    from test_dc_video import make_clip
    clip = Path(make_clip(tmp_path / 'walk.mp4', secs=4))
    b = _send(f'{server}/api/dc/buckets', {'name': 'video-bucket', 'kind': 'managed'})
    bid = b['id']
    _upload(f'{server}/api/dc/buckets/{bid}/upload?path=', [(clip.name, clip.read_bytes())])
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 1000})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/?data={bid}')
        tile = page.locator('#dc-grid .dc-th[data-path="walk.mp4"]')
        tile.wait_for(timeout=30000)
        page.wait_for_selector('#dc-grid .dc-th[data-path="walk.mp4"] img[src*="w="]', timeout=15000)
        assert page.locator('#dc-grid .dc-th[data-path="walk.mp4"] .vb').count() == 1
        # one click: the details pane plays it and offers Extract frames
        tile.click()
        page.wait_for_selector('#dc-details video.dc-d-video')
        tile.dblclick()
        page.wait_for_selector('.ui-dlg .dc-fr video.dc-fr-player')
        page.wait_for_selector('.dc-fr-meta:has-text("160×96")', timeout=15000)
        page.wait_for_selector('#dc-fr-est .status-line.ok:has-text("4 frames")', timeout=15000)   # 1 per second
        page.click('.dc-fr [data-preset="burst"]')
        page.wait_for_selector('#dc-fr-est .status-line.ok:has-text("30 frames")', timeout=15000)  # 15 from 1 s every 2 s
        page.wait_for_function('[...document.querySelectorAll(".dc-fr-strip img")].filter((i) => i.complete && i.naturalWidth > 0).length === 8',
                               timeout=20000)
        page.screenshot(path=str(tmp_path / 'frames-dialog.png'))
        page.click('.ui-dlg button.primary:has-text("Extract frames")')
        page.wait_for_function(f'location.search.includes("data={bid}") && location.search.includes("walk-frames")', timeout=30000)
        page.wait_for_selector('#dc-grid .dc-th[data-path="walk-frames/images"]', timeout=15000)
        browser.close()
    ls = _get(f'{server}/api/dc/buckets/{bid}/ls?path=walk-frames/images')
    assert ls['total'] == 30
    assert not errors, '\n'.join(errors)
