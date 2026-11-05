"""End-to-end: Compare › Image conditions in the page (the real app, headless Chromium). Dataset A: 24 well-lit, sharp
scenes; B: the same kind of scenes from a "new camera" - darker, noisier, softer, vignetted and warmer. The section
opens, Analyse runs the measuring job, the verdict says shifted, brightness and noise are flagged, the charts (light
map, exposure mix too) draw, the preview shows three images (A as B, then B corrected to A), "B outside A's range"
fills the image grid, and the CSV export carries every metric. The Stress test runs a generic API model (a tiny HTTP
server here that only finds the part in bright enough images) on A: the brightness curve falls on the dark side and B
lands outside the safe window. Screenshots (light, then dark) go to docs/screenshots/2026-11-02-image-conditions/
(DATAVIZ_SCREENSHOTS=<dir> elsewhere)."""

import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from test_page_smoke import _browser, _send, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')

SHOTS = Path(os.environ.get('DATAVIZ_SCREENSHOTS') or Path(__file__).resolve().parents[2] / 'docs/screenshots/2026-11-02-image-conditions')


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
            if side == 'B':                               # the new camera: darker, softer, noisier, vignetted, warmer
                h, w = a.shape[:2]
                yy, xx = np.mgrid[0:h, 0:w]
                vig = 1 - 0.4 * (((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / 2
                a = cv2.GaussianBlur(a * 0.55, (0, 0), 1.6) * vig[..., None] * np.array([1.12, 1.0, 0.82], np.float32)
                a = a + rng.normal(0, 7, a.shape[:2] + (1,))
            Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(root / 'images' / f'f{k:02d}.jpg', 'JPEG', quality=92)
            (root / 'labels' / f'f{k:02d}.txt').write_text('0 0.5 0.5 0.3 0.3\n')
        roots[side] = root
    return roots['A'], roots['B']


class _Model(BaseHTTPRequestHandler):
    """A detector behind HTTP: the labelled part (centre box) when the image is bright enough, nothing otherwise."""

    def do_POST(self):
        from PIL import Image
        data = self.rfile.read(int(self.headers['Content-Length']))
        lum = float(np.asarray(Image.open(io.BytesIO(data)).convert('L')).mean())
        dets = [{'box': [0.35, 0.35, 0.65, 0.65], 'score': 0.9, 'class_name': 'part'}] if lum > 80 else []
        body = json.dumps({'detections': dets}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_image_conditions_end_to_end(server, tmp_path):  # noqa: F811
    a, b = _datasets(tmp_path)
    A = _send(f'{server}/api/datasets', {'kind': 'local', 'name': 'camera A', 'path': str(a)})
    B = _send(f'{server}/api/datasets', {'kind': 'local', 'name': 'camera B', 'path': str(b)})
    U = _send(f'{server}/api/datasets', {'kind': 'union', 'name': 'cam A vs B', 'reuse': True,
                                         'parts': [{'id': A['id'], 'alias': 'A'}, {'id': B['id'], 'alias': 'B'}]})
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    model = _send(f'{server}/api/models', {
        'name': 'bright-only', 'family': 'custom', 'format': 'api',
        'api': {'preset': 'generic', 'url': f'http://127.0.0.1:{srv.server_address[1]}/predict', 'send': 'raw',
                'mapping': {'detections': 'detections', 'box': 'box', 'score': 'score', 'class_name': 'class_name',
                            'box_format': 'xyxy', 'normalized': 'true'}}})
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
        assert 'brightness ×' in page.inner_text('#shf-preview-p') and 'vignette' in page.inner_text('#shf-preview-p')
        page.wait_for_selector('#shf-lightmap .main-svg')
        page.wait_for_selector('#shf-exposure .main-svg')
        assert sev('vignetting') in ('minor', 'moderate', 'major') and sev('cct') in ('minor', 'moderate', 'major')
        page.locator('#cmp-shift-d').screenshot(path=str(SHOTS / '01-image-conditions.png'))
        page.click('[data-shf-pmode="correct"]')
        page.wait_for_function("document.getElementById('shf-preview-p').textContent.includes('Per-channel')", timeout=20000)
        page.wait_for_function("[...document.querySelectorAll('#shf-preview img')].length === 3 && "
                               "[...document.querySelectorAll('#shf-preview img')].every(i => i.complete && i.naturalWidth > 0)", timeout=20000)
        with page.expect_download() as dl:
            page.click('a[href*="api/compare/shift/lut"]')
        assert len(json.loads(Path(dl.value.path()).read_text())['lut']) == 3
        page.locator('#shf-preview').screenshot(path=str(SHOTS / '04-b-corrected-to-a.png'))
        page.click('[data-shf-pmode="as_b"]')
        page.click('tr[data-shf-metric="noise"]')
        page.wait_for_function("document.querySelector('#shf-metric-chart .gtitle')?.textContent.includes('noise')", timeout=10000)
        page.click('[data-shf-curve="rgb"]')
        page.click('[data-shf-scatter="blur"]')
        page.locator('#shf-body').screenshot(path=str(SHOTS / '02-noise-rgb-blur-scatter.png'))
        page.click('[data-shf-group="light"]')
        assert page.locator('tr[data-shf-metric]').count() == 13
        with page.expect_download() as dl:
            page.click('[data-shf-export="csv"]')
        csv = Path(dl.value.path()).read_text()
        assert csv.startswith('metric,group') and '\nbrightness,light,' in csv
        page.evaluate("document.documentElement.dataset.theme = 'dark'; window.dispatchEvent(new Event('theme-changed'))")
        page.wait_for_timeout(400)
        page.locator('#cmp-shift-d').screenshot(path=str(SHOTS / '03-image-conditions-dark.png'))
        page.evaluate("document.documentElement.dataset.theme = 'light'; window.dispatchEvent(new Event('theme-changed'))")
        # the stress test: the model on A at every brightness / noise step, B placed on each axis
        page.click('#shf-stress-d > summary')
        page.wait_for_function(f"[...document.querySelectorAll('#str-model option')].some(o => o.value === '{model['id']}')", timeout=20000)
        page.select_option('#str-model', model['id'])
        page.select_option('#str-n', '50')
        for cb in page.locator('[data-str-axis]').all():
            if cb.get_attribute('data-str-axis') not in ('brightness', 'noise'):
                cb.uncheck()
        page.click('#str-go')
        page.wait_for_selector('.str-table tr[data-str-strip]', timeout=240000)
        page.wait_for_selector('#shf-stress-chart .main-svg')
        assert page.locator('.str-table tr[data-str-strip]').count() == 2
        br = page.locator('tr[data-str-strip="brightness"]')
        assert br.locator('.shf-sev').get_attribute('data-sev') == 'major'
        assert 'Expected mAP50' in page.inner_text('#str-body') and 'brightness' in page.inner_text('#str-body .shf-verdict')
        br.click()
        page.wait_for_function("document.querySelectorAll('#str-strip img').length === 8", timeout=20000)
        page.wait_for_function("[...document.querySelectorAll('#str-strip img')].every(i => i.complete && i.naturalWidth > 0)", timeout=20000)
        page.locator('#shf-stress-d').screenshot(path=str(SHOTS / '05-stress-test.png'))
        page.click('tr[data-shf-metric="brightness"]')
        page.click('[data-shf-list="low"]')
        page.wait_for_function("document.getElementById('cmp-side').classList.contains('hidden')", timeout=10000)
        browser.close()
    srv.shutdown()
    assert not errors, errors
