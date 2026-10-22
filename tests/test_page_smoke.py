"""End-to-end smoke test of the page: the real app.py on a tiny generated dataset (and a fresh PostgreSQL database),
driven by headless Chromium signed in as the first admin. Opens the session, waits for the plot, scans in Curation and expects the seeded train/val leak. Fails on any page
error or console error. Skips without playwright (+ `playwright install chromium`)."""

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import TEST_SECRET_KEY, make_jpeg

sync_api = pytest.importorskip('playwright.sync_api')

HERE = Path(__file__).resolve().parents[1]
ADMIN = ('admin', 'smoke-test-password-1')
_JAR = http.cookiejar.CookieJar()                 # the admin's session cookie, shared by urllib and the browser
_HTTP = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
_HEADERS = {'Content-Type': 'application/json', 'X-Requested-With': 'dataviz'}


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _get(url):
    with _HTTP.open(url, timeout=5) as r:
        return json.loads(r.read())


def _send(url, body, method='POST'):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method, headers=_HEADERS)
    with _HTTP.open(req, timeout=10) as r:
        return json.loads(r.read())


def _make(tmp):
    ds, split = tmp / 'ds', tmp / 'split'
    (ds / 'labels').mkdir(parents=True)
    (ds / 'classes.txt').write_text('cat\ndog\n')
    for k in range(24):
        make_jpeg(ds / 'images' / f'im{k:02d}.jpg', 64, 48, color=(10 * k, 255 - 10 * k, (37 * k) % 255))
        (ds / 'labels' / f'im{k:02d}.txt').write_text(f'{k % 2} 0.5 0.5 {0.2 + 0.02 * k:.2f} 0.3\n1 0.3 0.3 0.1 0.1\n')
        make_jpeg(split / ('val' if k >= 18 else 'train') / 'images' / f'im{k:02d}.jpg')
    (ds / 'images' / 'leak.jpg').write_bytes((ds / 'images' / 'im00.jpg').read_bytes())   # a val copy of a train image
    (ds / 'labels' / 'leak.txt').write_text('0 0.5 0.5 0.2 0.3\n')
    make_jpeg(split / 'val' / 'images' / 'leak.jpg')
    make_jpeg(ds / 'images' / 'zz_bg.jpg', 64, 48, color=(5, 5, 5))      # a background image: no label file
    cfg = tmp / 'config.yaml'
    cfg.write_text(json.dumps({                       # JSON is YAML
        'dataset': {'workspace_root': str(tmp / 'ws')},
        'startup': {'feature': 'geometry', 'method': 'pca'},
        'features': {'models': [], 'yolo': {'auto_discover': False}},
        'workers': {'enabled': False},
        'models': {'split_source': str(split)},
        'logging': {'http': False},
        'storage': {'allowed_roots': [str(tmp)]},
    }))
    return ds, cfg


@pytest.fixture
def server(pg, tmp_path):
    ds, cfg = _make(tmp_path)
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
                _get(f'{base}/api/auth/config')       # public: answers before anyone signs in
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


class _SignedIn:
    """A browser whose every context carries the admin's session cookie."""

    def __init__(self, browser):
        self.browser = browser

    def new_context(self, **kw):
        ctx = self.browser.new_context(**kw)
        ctx.add_cookies([{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path} for c in _JAR])
        return ctx

    def new_page(self, **kw):
        return self.new_context(**kw).new_page()

    def close(self):
        self.browser.close()


def _browser(p):
    try:
        return _SignedIn(p.chromium.launch())
    except Exception as e:  # noqa: BLE001 - no browser downloaded, or its system libraries missing
        why = next((ln.strip() for ln in str(e).splitlines() if 'shared libraries' in ln), str(e).splitlines()[0])
        pytest.skip(f'chromium cannot start ({why}): `playwright install chromium` (+ `playwright install-deps`)')


def test_page_opens_plots_and_curation_finds_the_leak(server):
    sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page()
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/?session={sid}')
        page.click('#btn-compute')                # Explore starts idle: the embedding runs on demand
        page.wait_for_selector('#plot .main-svg', timeout=60000)
        page.click('#act-curation')
        page.click('#cur-scan')
        page.wait_for_selector('#cur-types >> text=Train / val leakage', timeout=60000)
        browser.close()
    assert not errors, '\n'.join(errors)


def test_image_browser_viewer_copy_and_edit(server, tmp_path):
    """Activity bar groups; no selection = every image (the background one too); the viewer toggles its boxes,
    copies the image from the right-click menu, and Edit mode draws a box that Save writes to the label file."""
    sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context(viewport={'width': 1400, 'height': 900})
        ctx.grant_permissions(['clipboard-read', 'clipboard-write'], origin=server)
        page = ctx.new_page()
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/?session={sid}')
        page.click('#btn-compute')                # Explore starts idle: the embedding runs on demand
        page.wait_for_selector('#plot .main-svg', timeout=60000)
        # the (hidden) ◆ AI button first, then the data views, the separator, the model views
        order = page.eval_on_selector_all('#activity > *', 'els => els.map(e => e.dataset.tab || e.className)')
        assert order[:7] == ['ai', 'explore', 'stats', 'curation', 'act-sep', 'model', 'eval'], order
        # no LLM configured: no diamond, and no AI control anywhere on the visualizer
        assert page.locator('#act-ai').is_hidden()
        assert page.eval_on_selector_all('[data-llm]:not(.hidden)', 'els => els.length') == 0
        assert page.locator('#ai-side').is_hidden()
        # the image browser: 26 images, the background one included
        page.wait_for_selector('#gallery .img-card', timeout=30000)
        assert page.locator('#gallery-count').text_content().startswith('26 images')
        assert page.locator('#gallery .img-card.bg[data-img="zz_bg.jpg"]').count() == 1
        # the filter bar narrows it (name match covers the background image as well)
        page.fill('#fb-q', 'zz_')
        page.wait_for_function("document.querySelector('#gallery-count').textContent.startsWith('1 image')", timeout=10000)
        page.fill('#fb-q', '')
        page.wait_for_function("document.querySelector('#gallery-count').textContent.startsWith('26 images')", timeout=10000)
        # viewer: im00 has 2 boxes, drawn as SVG; B hides them
        page.click('#gallery .img-card[data-img="im00.jpg"]')
        page.wait_for_selector('#vw-svg rect.vw-box', timeout=10000)
        assert page.locator('#vw-svg rect.vw-box').count() == 2
        page.keyboard.press('b')
        assert page.locator('#vw-svg rect.vw-box').count() == 0
        page.keyboard.press('b')
        assert page.locator('#vw-svg rect.vw-box').count() == 2
        # right-click: copy with boxes -> a PNG on the clipboard
        page.click('#vw', button='right')
        page.wait_for_selector('#vw-menu:not(.hidden)')
        page.click('#vw-menu [data-vm="copy-boxes"]')
        page.wait_for_selector('#vw-flash', timeout=10000)
        assert 'Copied' in page.locator('#vw-flash').inner_text()
        types = page.evaluate('navigator.clipboard.read().then(items => items.flatMap(i => i.types))')
        assert 'image/png' in types
        page.keyboard.press('Escape')
        # edit the background image: draw a box, save -> the label file gets the line
        page.click('#gallery .img-card[data-img="zz_bg.jpg"]')
        page.wait_for_function("document.querySelector('#modal-title').textContent.includes('zz_bg.jpg')")
        page.keyboard.press('e')
        page.wait_for_selector('#vw-editbar:not(.hidden)')
        page.keyboard.press('n')
        bb = page.locator('#modal-img').bounding_box()
        page.mouse.move(bb['x'] + bb['width'] * 0.25, bb['y'] + bb['height'] * 0.25)
        page.mouse.down()
        page.mouse.move(bb['x'] + bb['width'] * 0.6, bb['y'] + bb['height'] * 0.5, steps=5)
        page.mouse.move(bb['x'] + bb['width'] * 0.75, bb['y'] + bb['height'] * 0.75, steps=5)
        page.mouse.up()
        assert page.locator('#vw-svg rect.vw-ebox.new').count() == 1
        page.keyboard.press('Control+s')
        page.wait_for_function("document.querySelector('#vw-edit-st').textContent.startsWith('Saved')", timeout=10000)
        page.wait_for_selector('#edit-banner:not(.hidden)', timeout=10000)
        browser.close()
    label = tmp_path / 'ds' / 'labels' / 'zz_bg.txt'
    parts = label.read_text().split()
    assert len(parts) == 5 and parts[0] == '0', label.read_text()
    cx, cy, w, h = map(float, parts[1:])
    assert abs(cx - 0.5) < 0.03 and abs(cy - 0.5) < 0.03 and abs(w - 0.5) < 0.03 and abs(h - 0.5) < 0.03
    assert not errors, '\n'.join(errors)


def _post(url, body):
    return _send(url, body)


def test_label_page_import_and_model_activation(server, tmp_path):
    """Label page: the classes ⋯ menu imports a COCO file into the task; a Settings › Models model (a dataviz-label/1
    server) is linked from the model menu, stays off until activated, then labels the image."""
    from test_models import KEY, _serve, generic_app
    gs, gurl = _serve(generic_app())
    try:
        imgs = tmp_path / 'lbl_imgs'
        for k in range(3):
            make_jpeg(imgs / f'p{k}.jpg', 80, 60, color=(40 * k, 90, 160))
        out = _post(f'{server}/api/label/projects', {'name': 'Smoke', 'source': {'kind': 'local', 'path': str(imgs)},
                                                     'tasks': [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat']}]})
        pid = out['project']['id']
        for _ in range(100):
            if _get(f"{server}/api/jobs/{out['job_id']}")['status'] == 'done':
                break
            time.sleep(0.1)
        coco = tmp_path / 'coco.json'
        coco.write_text(json.dumps({'images': [{'id': 1, 'file_name': 'p0.jpg', 'width': 80, 'height': 60}],
                                    'categories': [{'id': 1, 'name': 'cat'}],
                                    'annotations': [{'id': 1, 'image_id': 1, 'category_id': 1, 'bbox': [8, 6, 40, 30]}]}))
        m = _post(f'{server}/api/models', {'name': 'Pets API', 'family': 'custom', 'format': 'api',
                                          'api': {'preset': 'label', 'url': gurl, 'api_key': KEY, 'label_model': 'det'}})
        errors = []
        with sync_api.sync_playwright() as p:
            browser = _browser(p)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
            page.on('console', lambda m_: m_.type == 'error' and 'status of 409' not in m_.text and errors.append(f'console: {m_.text}'))
            page.goto(f'{server}/?label={pid}')
            page.wait_for_selector('#lb-cls-more', timeout=30000)
            # import: ⋯ › Import annotations… › Path on server › Preview › Import
            page.click('#lb-cls-more')
            page.click('#lb-menu [data-m="import"]')
            page.wait_for_selector('.lbm-import [data-ia="set"][data-v="local"]')
            page.click('.lbm-import [data-ia="set"][data-v="local"]')
            page.fill('.lbm-import [data-if="path"]', str(coco))
            page.click('.lbm-import [data-ia="preview"]')
            page.wait_for_selector('.lbm-import .imp-preview', timeout=10000)
            assert '1 of them match' in page.locator('.lbm-import .imp-preview').inner_text()
            page.click('.lbm-import [data-x="go"]')
            page.wait_for_selector('.lbm-import .status-line.ok', timeout=15000)
            assert 'Imported 1 shape' in page.locator('.lbm-import .status-line.ok').inner_text()
            page.click('.lbm-import [data-x="close"]')
            page.wait_for_load_state('load')                       # the page reloads to show the import
            page.wait_for_selector('#lb-model-btn', timeout=30000)
            # link the Settings › Models model; it is off until activated
            page.click('#lb-model-btn')
            page.click('.lb-mmenu [data-ma="link"]')
            page.wait_for_selector('.lb-dlg [data-d="conn"]')
            page.select_option('.lb-dlg [data-d="conn"]', f"reg:{m['id']}")
            page.wait_for_selector('.lb-dlg .lbm-map', timeout=15000)
            page.click('.lb-dlg [data-a="save"]')
            page.wait_for_function("document.querySelector('#lb-model-btn').classList.contains('inactive')", timeout=10000)
            page.click('#lb-model-btn')
            assert page.locator('.lb-mmenu [data-ma="this"]').is_disabled()
            page.click('.lb-mmenu [data-ma="activate"]')
            page.wait_for_function("!document.querySelector('#lb-model-btn').classList.contains('inactive')", timeout=10000)
            page.keyboard.press('Alt+m')                             # label this image
            page.wait_for_function("[...document.querySelectorAll('.lb-toast, .toast, [role=status]')].some(e => /model added/i.test(e.textContent))",
                                   timeout=15000)
            browser.close()
        got = _get(f"{server}/api/label/projects/{pid}")
        assert got['model_active'] == [got['tasks'][0]['id']]
        assert not errors, '\n'.join(errors)
    finally:
        gs.should_exit = True


def _put(url, body):
    return _send(url, body, 'PUT')


def test_ai_assistant_appears_only_when_connected_and_streams(server):
    """◆: hidden with a saved but untested provider, shown once tested + switched on, live (no reload); the panel
    streams an answer from a fake OpenAI-compatible server; Settings shows it Connected; switching it off hides it."""
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    from test_llm import REPLY, fake_app
    from test_models import _serve
    REPLY['text'] = 'Hello there, this is the fake model.'     # test_llm.py tests change it
    fake, url = _serve(fake_app())
    try:
        sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                 'model': 'gpt-x', 'vision': True})['provider']
        _put(f'{server}/api/llm/config', {'features': {'assistant': True, 'dataset_qa': True}})
        with sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('console', lambda m: m.type == 'error' and errors.append(m.text))
            page.goto(f'{server}/?session={sid}')
            page.click('#btn-compute')                # Explore starts idle: the embedding runs on demand
            page.wait_for_selector('#plot .main-svg', timeout=60000)
            assert page.is_hidden('#act-ai'), 'switched on but never tested: still hidden'
            assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
            page.evaluate("window.dispatchEvent(new Event('llm-changed'))")
            page.wait_for_selector('#act-ai:not(.hidden)', timeout=5000)
            page.click('#act-ai')
            page.wait_for_selector('#ai-q')
            assert page.is_visible('#plot'), 'the plot stays next to the chat'
            page.fill('#ai-q', 'Summarize this dataset')
            page.press('#ai-q', 'Enter')
            page.wait_for_selector('.ai-msg.bot:has-text("fake model")', timeout=15000)
            page.click('#btn-settings')
            page.click('.snav[data-sec="llm"]')
            page.wait_for_selector('.llm-card:has-text("Connected")')
            page.uncheck('[data-llm-feat="assistant"]')
            page.wait_for_selector('#act-ai.hidden', state='attached', timeout=5000)
            browser.close()
        assert not errors, errors
    finally:
        fake.should_exit = True


def test_review_card_label_page_review_all_and_every_theme(server, tmp_path):
    """Optional review: the card's second bar and checklist, the label page's Review button (Alt+R), Review all from
    the card's ⋯ menu, a hand review and a bulk one drawn differently, in every theme / palette / look, with no page error."""
    imgs = tmp_path / 'rv_imgs'
    for k in range(4):
        make_jpeg(imgs / f'p{k}.jpg', 80, 60, color=(40 * k, 90, 160))
    out = _post(f'{server}/api/label/projects', {'name': 'Reviewed', 'source': {'kind': 'local', 'path': str(imgs)}, 'review': True,
                                                 'tasks': [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat']}]})
    pid, tid = out['project']['id'], out['project']['tasks'][0]['id']
    for _ in range(100):
        if _get(f"{server}/api/jobs/{out['job_id']}")['status'] == 'done':
            break
        time.sleep(0.1)
    ids = _get(f'{server}/api/label/projects/{pid}/media-ids')['ids']
    base = f'{server}/api/label/projects/{pid}'
    # p1 done + reviewed by hand, p2 done + Review all (bulk), p0 done and not reviewed, p3 still to do
    for m in (ids[1], ids[2]):
        _post(f'{base}/commit/{m}', {'task_ids': [tid]})
    _post(f'{base}/review', {'task_ids': [tid], 'mode': 'manual', 'media_ids': [ids[1]]})
    _post(f'{base}/review', {'task_ids': [tid], 'mode': 'bulk'})
    _post(f'{base}/commit/{ids[0]}', {'task_ids': [tid]})
    errors = []

    def state():
        got = _get(f'{base}/media?task_id={tid}&limit=10')['items']
        return {x['file_name']: (x['review'].get(tid) or '') for x in got}

    assert state() == {'p0.jpg': '', 'p1.jpg': 'manual', 'p2.jpg': 'bulk', 'p3.jpg': ''}
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m_: m_.type == 'error' and 'status of 409' not in m_.text and errors.append(f'console: {m_.text}'))
        # the label page: the button is on, Alt+R reviews the open (done, unreviewed) image and again undoes it
        page.goto(f'{server}/?label={pid}')
        page.wait_for_selector('#lb-review:not(.hidden)', timeout=30000)
        page.wait_for_function("!document.querySelector('#lb-review').disabled", timeout=15000)
        assert page.get_attribute('#lb-review', 'aria-pressed') == 'false'
        page.keyboard.press('Alt+r')
        page.wait_for_function("document.querySelector('#lb-review').getAttribute('aria-pressed') === 'true'", timeout=10000)
        for _ in range(50):
            if state()['p0.jpg'] == 'manual':
                break
            time.sleep(0.1)
        assert state()['p0.jpg'] == 'manual'
        page.keyboard.press('Alt+r')
        page.wait_for_function("document.querySelector('#lb-review').getAttribute('aria-pressed') === 'false'", timeout=10000)
        for _ in range(50):
            if state()['p0.jpg'] == '':
                break
            time.sleep(0.1)
        assert state()['p0.jpg'] == ''
        # the strip's review filter
        page.select_option('#lb-strip-filter', 'review:bulk')
        page.wait_for_function("document.querySelector('#lb-strip-n').textContent.trim().startsWith('1 /')", timeout=10000)
        # the card: second bar (solid + hatched) and the checklist
        page.goto(f'{server}/')
        page.click('#home-tab-label')
        card = page.locator('.label-card', has_text='Reviewed')
        card.locator('.lp-review i.manual').wait_for(timeout=15000)
        assert card.locator('.lp-review i.bulk').count() == 1
        text = card.locator('.lp-checklist').inner_text()
        assert 'Labeled' in text and 'Reviewed 0/1' in text and 'bulk' in text, text   # p0 is done but not reviewed, p3 is to do
        assert not card.locator('.lp-chk.ok').count()
        # every theme: the two bars differ (colour vs hatch) and nothing throws
        probe = """() => { const m = document.querySelector('.lp-review i.manual'), b = document.querySelector('.lp-review i.bulk');
            const a = getComputedStyle(m), c = getComputedStyle(b);
            return [a.backgroundColor, a.backgroundImage, c.backgroundColor, c.backgroundImage, getComputedStyle(document.querySelector('.lp-chk.bulk')).color]; }"""
        looks = ['default', 'editorial', 'blueprint', 'brutalist', 'terminal', 'soft']
        palettes = ['default', 'cbsafe', 'contrast', 'fjord', 'ember', 'orchid']
        for theme in ('dark', 'light'):
            for pal in palettes:
                for look in looks:
                    page.evaluate("([t, p, l]) => { const d = document.documentElement; d.dataset.theme = t; d.dataset.palette = p; d.dataset.look = l; }",
                                  [theme, pal, look])
                    mc, mi, bc, bi, chk = page.evaluate(probe)
                    assert mi == 'none' and bi.startswith('repeating-linear-gradient'), (theme, pal, look, mi, bi)
                    assert mc not in ('', 'rgba(0, 0, 0, 0)') and chk not in ('', 'rgba(0, 0, 0, 0)'), (theme, pal, look, mc, chk)
        page.evaluate("() => { const d = document.documentElement; d.dataset.theme = 'dark'; d.dataset.palette = 'default'; d.dataset.look = 'default'; }")
        # Review all from the ⋯ menu: p0 becomes bulk, p3 (to do) is left alone
        card.locator('[data-act="lmore"]').click()
        page.click('#home-cardmenu [data-m="review"]')
        page.wait_for_selector('#home-dlg:not(.hidden)')
        assert 'Review 1' in page.inner_text('#home-dlg-ok')
        page.click('#home-dlg-ok')
        for _ in range(100):
            if state()['p0.jpg'] == 'bulk':
                break
            time.sleep(0.1)
        assert state() == {'p0.jpg': 'bulk', 'p1.jpg': 'manual', 'p2.jpg': 'bulk', 'p3.jpg': ''}
        page.wait_for_function("document.querySelector('.label-card .lp-checklist').innerText.includes('Reviewed 1/1')", timeout=10000)
        browser.close()
    assert not errors, '\n'.join(errors)


def test_accounts_signup_sharing_owner_card_and_viewer(server):
    """A new user signs up in the page and sees none of the admin's private sessions; once the admin shares one with
    them as Viewer, its card shows the owner's name + avatar and the member, the scope filter finds it under
    "Shared with me" (not "Mine"), and the visualizer opens read-only (server refuses writes too)."""
    sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (300, 200), (200, 40, 90)).save(buf, 'PNG')
    boundary = 'smokeboundary'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
            f'Content-Type: image/png\r\n\r\n').encode() + buf.getvalue() + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(f'{server}/api/me/avatar', data=body, method='PUT',
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                          'X-Requested-With': 'dataviz'})
    with _HTTP.open(req, timeout=10) as r:
        assert json.loads(r.read())['user']['avatar_ver'] == 1
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p).browser                       # NOT signed in: this is the new user's browser
        page = browser.new_page(viewport={'width': 1300, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and not ('401' in m.text or '403' in m.text)
                and errors.append(f'console: {m.text}'))
        refused = []                                        # requests the page itself sends that the server refuses
        page.on('response', lambda r: r.status == 403 and refused.append(f'{r.request.method} {r.url}'))
        page.goto(f'{server}/')
        page.wait_for_url('**/login?next=*')
        page.click('#lg-tab-up')
        page.fill('#lg-user', 'bob')
        page.fill('#lg-name', 'Bob Viewer')
        page.fill('#lg-email', 'bob@example.test')
        page.fill('#lg-up input[autocomplete="new-password"] >> nth=0', 'bob-password-123')
        pw2 = page.locator('#lg-up input[autocomplete="new-password"]')
        if pw2.count() > 1:
            pw2.nth(1).fill('bob-password-123')
        page.click('#lg-up-go')
        page.wait_for_url(lambda u: '/login' not in u, timeout=20000)
        page.wait_for_selector('#home', timeout=20000)
        page.wait_for_load_state('networkidle')
        assert page.locator(f'.session-card[data-id="{sid}"]').count() == 0        # private: invisible to bob
        bob = page.evaluate("fetch('api/auth/me').then(r => r.json())")['user']
        _put(f'{server}/api/sessions/{sid}/sharing', {'visibility': 'private',
                                                      'members': [{'user_id': bob['id'], 'role': 'viewer'}]})
        page.reload()
        card = page.locator(f'.session-card[data-id="{sid}"]')
        card.wait_for(timeout=20000)
        assert card.get_attribute('data-role') == 'viewer'
        assert 'Admin' in card.locator('.owner-chip').inner_text()
        assert card.locator('.owner-chip img').count() == 1                     # the uploaded avatar
        page.click('#home-scope [data-scope="mine"]')
        page.wait_for_function(f'!document.querySelector(\'.session-card[data-id="{sid}"]\')', timeout=10000)
        page.click('#home-scope [data-scope="shared"]')
        page.wait_for_selector(f'.session-card[data-id="{sid}"]', timeout=10000)
        page.goto(f'{server}/s/{sid}/')
        page.wait_for_function("document.documentElement.dataset.role === 'viewer'", timeout=30000)
        st = page.evaluate("fetch('api/status').then(r => r.json())")
        assert st['my_role'] == 'viewer'
        assert not refused, refused                         # the viewer's page never tries what it may not do
        code = page.evaluate("fetch('api/tags', {method: 'POST', headers: {'Content-Type': 'application/json', "
                             "'X-Requested-With': 'dataviz'}, body: JSON.stringify({tag: 'x', ids: [0]})})"
                             ".then(r => r.status)")
        assert code == 403
        browser.close()
    assert not errors, '\n'.join(errors)


def test_plot_bar_and_cleanlab_label_quality(server):
    """The one-row bar above the plot: projection / classes / display popovers hold the old controls (same ids);
    Find issues runs cleanlab's k-fold label-quality check, fills the gallery with its label issues, unlocks the
    cleanlab colourings and the Classes view's confident joint. DATAVIZ_SCREENSHOTS=<dir> saves the bar at 3 widths."""
    sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
    shots = os.environ.get('DATAVIZ_SCREENSHOTS')
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/?session={sid}')
        page.click('#btn-compute')
        page.wait_for_selector('#plot .main-svg', timeout=60000)
        bar = page.locator('#controls').bounding_box()
        assert bar['height'] < 60, f'the bar should be one row, is {bar["height"]}px'
        assert 'PCA' in page.locator('#map-summary').inner_text()
        assert page.locator('#feature').is_hidden()                 # inside the projection popover
        page.click('#map-btn')
        assert page.locator('#feature').is_visible() and page.locator('#btn-apply').is_visible()
        page.keyboard.press('Escape')
        assert page.locator('#map-pop').is_hidden()
        page.click('#cls-btn')
        page.select_option('#mode', 'single')
        page.wait_for_function("document.querySelector('#cls-summary').textContent !== 'All classes'")
        page.select_option('#mode', 'all')
        page.keyboard.press('Escape')
        # the filter's name options fold into one trigger: it names the mode only when it is not "contains"
        assert page.locator('#fb-mode').is_hidden() and page.locator('#fb-name-mode').is_hidden()
        page.click('#fb-name-btn')
        page.select_option('#fb-mode', 'regex')
        page.check('#fb-neg')
        assert page.locator('#fb-name-mode').inner_text() == 'not · regex'
        page.select_option('#fb-mode', 'substr')
        page.uncheck('#fb-neg')
        page.keyboard.press('Escape')
        if shots:
            for w in (1400, 900, 560):
                page.set_viewport_size({'width': w, 'height': 900})
                page.locator('#controls').screenshot(path=f'{shots}/bar-{w}.png')
            page.set_viewport_size({'width': 1400, 'height': 900})
        # Find issues › cleanlab: nothing computed yet
        page.click('#issues-btn')
        assert page.locator('#btn-cl-issues').is_disabled()
        assert page.locator('#color-by optgroup[data-needs="lq"]').evaluate('g => g.disabled')
        page.on('dialog', lambda d: (errors.append(f'dialog: {d.message}'), d.dismiss()))
        page.click('#btn-lq-run')
        page.wait_for_function("!document.querySelector('#btn-cl-issues').disabled", timeout=60000)
        assert 'health' in page.locator('#lq-info').inner_text()
        assert page.locator('#issues-pop').is_visible()           # the run button keeps the menu open for its result
        if shots:
            page.locator('#issues-pop').screenshot(path=f'{shots}/issues-menu.png')
        page.keyboard.press('Escape')
        flagged = int(page.locator('#cl-issues-n').inner_text().split()[0].replace(',', ''))
        if flagged:                                          # an earlier test may have fixed the noisy labels
            assert 'cleanlab' in page.locator('#gallery-title').inner_text().lower()
        page.select_option('#color-by', 'cl_quality')
        page.wait_for_selector('#plot .colorbar', timeout=10000)
        if shots:
            page.click('#map-btn')
            page.screenshot(path=f'{shots}/page-map-open.png')
            page.keyboard.press('Escape')
        page.click('#activity [data-tab="stats"]')
        page.wait_for_selector('#stat-cl-card:not(.hidden) #stat-cl .main-svg', timeout=20000)
        assert page.locator('#stat-cl-kpis .kpi').count() == 3
        browser.close()
    assert not errors, '\n'.join(errors)
