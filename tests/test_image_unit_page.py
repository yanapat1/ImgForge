"""End-to-end: a bucket prefix of plain images (no labels, like s3://motion-sample-po/WD06-sec) through the page.
Settings › New visualize session › S3 counts the images, offers "One point per image", saves and opens the session,
and the Explore view shows one point per image with the box-only tools hidden.

DATAVIZ_SCREENSHOTS=<dir> saves a screenshot of every step there (see docs/screenshots/*/NOTES.md)."""

import os

import pytest

from conftest_io import jpeg_bytes
from test_page_smoke import _browser, _get, _send, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')
pytest_plugins = ['conftest_io']             # fixtures: s3_server

BUCKET, PREFIX = 'motion-sample-po', 'WD06-sec'
CAMERAS = {'': ((200, 60, 40), 12), 'cam1': ((40, 160, 70), 8), 'cam2': ((30, 70, 210), 6)}


def _seed(client):
    try:
        client.create_bucket(Bucket=BUCKET)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    for sub, ((r, g, b), n) in CAMERAS.items():
        for k in range(n):
            key = f'{PREFIX}/{sub + "/" if sub else ""}frame_{k:03d}.jpg'
            shade = (min(255, r + 9 * k), min(255, g + 5 * k), max(0, b - 7 * k))
            client.put_object(Bucket=BUCKET, Key=key, Body=jpeg_bytes(320, 240, shade))
    client.put_object(Bucket=BUCKET, Key=f'{PREFIX}/readme.txt', Body=b'not an image')


def test_images_only_s3_prefix_in_the_page(server, s3_server):  # noqa: F811
    from dataviz.data import sources
    _seed(sources.make_client(s3_server))
    conn = _send(f'{server}/api/connections', {k: v for k, v in s3_server.items() if k != 'id'})
    shots = os.environ.get('DATAVIZ_SCREENSHOTS')
    shot = (lambda page, name, el=None: (page.locator(el) if el else page).screenshot(path=f'{shots}/{name}.png')) \
        if shots else (lambda *a, **k: None)
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(server)
        page.wait_for_selector('#home', timeout=20000)
        page.click('#home-new')
        page.click('[data-act="new-s3"]')
        page.select_option('select[data-f="connId"]', conn['id'])
        page.wait_for_selector(f'select[data-f="bucket"] option:text("{BUCKET}")', state='attached', timeout=20000)
        page.select_option('select[data-f="bucket"]', BUCKET)
        page.fill('input[data-f="prefix"]', PREFIX)
        page.press('input[data-f="prefix"]', 'Enter')
        status = page.locator('.status-line.ok:has-text("Images found: 12 here")')   # the prefix, not the bucket root
        status.wait_for(timeout=20000)
        assert '12 here' in status.inner_text() and '14 in sub-folders' in status.inner_text()
        assert page.locator('.folder:has-text("cam1") .tagc:has-text("8 images")').count() == 1
        shot(page, '01-s3-prefix-counts', '#settings-body')

        card = page.locator('.radio-card:has(input[value="images"])')
        assert card.get_attribute('class').split().count('sel') == 1          # no labels: one point per image
        assert page.locator('input[name="unit"][value="boxes"]').is_disabled()
        page.check('input[data-f="cff"]')
        shot(page, '02-unit-step-folder-classes', '#settings-body')

        page.click('button.primary[data-act="save-s3"]')
        page.wait_for_selector('#btn-compute', timeout=90000)
        st = page.evaluate("fetch('api/status').then(r => r.json())")
        assert st['default_feature'] == 'color-hist'                       # not box geometry: every box is the image
        page.click('#btn-compute')
        page.wait_for_selector('#plot-idle', state='hidden', timeout=90000)
        page.wait_for_function("(document.getElementById('plot').data || []).some(t => (t.x || []).length > 0) "
                               "&& document.getElementById('plot-wait').classList.contains('hidden')", timeout=90000)
        page.wait_for_selector('#plot .main-svg', timeout=90000)
        st = page.evaluate("fetch('api/status').then(r => r.json())")
        assert st['caps']['unit'] == 'image' and st['caps']['layout'] == 'flat' and not st['caps']['curation']
        meta = page.evaluate("fetch('api/meta').then(r => r.json())")
        assert meta['n_images'] == 26 and meta['n_samples'] == 26                # one point per image
        assert sorted(meta['classes']) == ['(top folder)', 'cam1', 'cam2']
        assert page.locator('#ds-info').inner_text().count('per image') == 1
        assert page.locator('#activity [data-tab="curation"]').is_hidden()
        assert page.locator('#activity [data-tab="model"]').is_hidden()
        assert page.locator('#color-by option[value="area"]').count() == 0
        assert page.locator('#fb-count').inner_text().strip() == '26 images'
        assert page.locator('#gallery .img-card .badge').count() == 0
        page.wait_for_load_state('networkidle')
        shot(page, '03-explore-one-point-per-image')

        page.wait_for_selector('#gallery .img-card', timeout=30000)
        assert page.locator('#gallery-count').text_content().startswith('26 images')
        page.locator('#gallery .img-card').first.click()
        page.wait_for_function("document.querySelector('#modal-img').naturalWidth > 0", timeout=20000)
        assert page.locator('#vw-svg .vw-box').count() == 0                 # no outline around the whole image
        assert page.locator('#vw-edit').is_hidden()
        page.wait_for_selector('#modal-meta:has-text("one point per image")', timeout=20000)
        assert 'cam1' in page.locator('#modal-meta').inner_text()
        shot(page, '04-image-viewer')
        browser.close()
    assert not errors, errors
    sessions = _get(f'{server}/api/datasets')['datasets']
    saved = next(d for d in sessions if d['kind'] == 's3')
    assert saved['unit'] == 'auto' or saved['unit'] == 'images'
    assert saved['class_from_folder'] is True
