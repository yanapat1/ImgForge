"""End-to-end: Compare › Image conditions in the page (the real app, headless Chromium). Dataset A: 24 well-lit, sharp
scenes; B: the same kind of scenes from a "new camera" - darker, noisier, softer. The section opens, Analyse runs the
measuring job, the verdict says shifted, brightness and noise are flagged, the charts draw, the preview shows three
images, "B outside A's range" fills the image grid, and the CSV export carries every metric. Screenshots (light, then
dark) go to docs/screenshots/2026-11-02-image-conditions/ (DATAVIZ_SCREENSHOTS=<dir> elsewhere)."""

import os
from pathlib import Path

import numpy as np
import pytest
from test_page_smoke import _browser, _send, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')

SHOTS = Path(os.environ.get('DATAVIZ_SCREENSHOTS') or Path(__file__).resolve().parents[1] / 'docs/screenshots/2026-11-02-image-conditions')


def _scene(rng, w=320, h=240):
    import cv2
    a = np.zeros((h, w, 3), np.uint8)
    a[:] = rng.integers(90, 150, 3)
    for _ in range(14):
        x, y = int(rng.integers(0, w - 40)), int(rng.integers(0, h - 40))
        cv2.rectangle(a, (x, y), (x + int(rng.integers(15, 90)), y + int(rng.integers(15, 70))), rng.integers(20, 235, 3).tolist(), -1)
    return a


def _datasets(tmp):
    import cv2
    from PIL import Image
    rng = np.random.default_rng(7)
    roots = {}
    for side in ('A', 'B'):
        root = tmp / f'cam{side}'
        (root / 'images').mkdir(parents=True)
        (root / 'labels').mkdir()
        (root / 'classes.txt').write_text('part\n')
        for k in range(24):
            a = _scene(rng).astype(np.float32)
            if side == 'B':                               # the new camera: darker, softer, noisier
                a = cv2.GaussianBlur(a * 0.55, (0, 0), 1.6) + rng.normal(0, 7, a.shape[:2] + (1,))
            Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(root / 'images' / f'f{k:02d}.jpg', 'JPEG', quality=92)
            (root / 'labels' / f'f{k:02d}.txt').write_text('0 0.5 0.5 0.3 0.3\n')
        roots[side] = root
    return roots['A'], roots['B']


def test_image_conditions_end_to_end(server, tmp_path):  # noqa: F811
    a, b = _datasets(tmp_path)
    A = _send(f'{server}/api/datasets', {'kind': 'local', 'name': 'camera A', 'path': str(a)})
    B = _send(f'{server}/api/datasets', {'kind': 'local', 'name': 'camera B', 'path': str(b)})
    U = _send(f'{server}/api/datasets', {'kind': 'union', 'name': 'cam A vs B', 'reuse': True,
                                         'parts': [{'id': A['id'], 'alias': 'A'}, {'id': B['id'], 'alias': 'B'}]})
    SHOTS.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1440, 'height': 3200}, accept_downloads=True)
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/s/{U["id"]}/#compare')
        page.wait_for_selector('#cmp-side:not(.hidden)', timeout=90000)
        page.evaluate("document.documentElement.style.setProperty('--left-w', '560px')")
        page.click('#cmp-shift-d > summary')
        page.wait_for_selector('#shf-go', timeout=20000)
        page.click('#shf-go')
        page.wait_for_selector('.shf-verdict', timeout=120000)
        assert page.get_attribute('.shf-verdict', 'data-level') == 'shifted'
        assert 'darker' in page.inner_text('.shf-verdict')
        sev = lambda k: page.get_attribute(f'tr[data-shf-metric="{k}"] .shf-sev', 'data-sev')
        assert sev('brightness') in ('moderate', 'major') and sev('noise') in ('moderate', 'major')
        assert sev('width') == 'ok'
        page.wait_for_selector('#shf-metric-chart .main-svg')
        page.wait_for_selector('#shf-light .main-svg')
        page.wait_for_selector('#shf-scatter .main-svg')
        page.wait_for_function("document.querySelectorAll('#shf-preview img').length === 3", timeout=20000)
        page.wait_for_function("[...document.querySelectorAll('#shf-preview img')].every(i => i.complete && i.naturalWidth > 0)", timeout=20000)
        assert 'brightness ×' in page.inner_text('#shf-preview-p')
        page.locator('#cmp-shift-d').screenshot(path=str(SHOTS / '01-image-conditions.png'))
        page.click('tr[data-shf-metric="noise"]')
        page.wait_for_function("document.querySelector('#shf-metric-chart .gtitle')?.textContent.includes('noise')", timeout=10000)
        page.click('[data-shf-curve="rgb"]')
        page.click('[data-shf-scatter="blur"]')
        page.locator('#shf-body').screenshot(path=str(SHOTS / '02-noise-rgb-blur-scatter.png'))
        page.click('[data-shf-group="light"]')
        assert page.locator('tr[data-shf-metric]').count() == 6
        with page.expect_download() as dl:
            page.click('[data-shf-export="csv"]')
        csv = Path(dl.value.path()).read_text()
        assert csv.startswith('metric,group') and '\nbrightness,light,' in csv
        page.evaluate("document.documentElement.dataset.theme = 'dark'; window.dispatchEvent(new Event('theme-changed'))")
        page.wait_for_timeout(400)
        page.locator('#cmp-shift-d').screenshot(path=str(SHOTS / '03-image-conditions-dark.png'))
        page.click('tr[data-shf-metric="brightness"]')
        page.click('[data-shf-list="low"]')
        page.wait_for_function("document.getElementById('cmp-side').classList.contains('hidden')", timeout=10000)
        browser.close()
    assert not errors, errors
