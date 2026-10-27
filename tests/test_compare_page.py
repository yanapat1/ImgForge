"""End-to-end: the Compare tab of a combined dataset in the page (the real app.py, headless Chromium, signed in as the
first admin). Two overlapping YOLO datasets with a known answer: 5 identical images, one identical image renamed in B
(matched by content), one image with a moved, a reclassed and an added box, one image only in A, one only in B (other
pixels). The union is made through POST /api/datasets {kind: 'union', reuse: true}, then /s/<union>/#compare opens the
tab: summary counts, the Diff-only toggle (fewer dots), "Added" opens the gallery, the density layer, and the
side-by-side viewer keeps both panes on one transform.

Every assertion point saves a screenshot in docs/screenshots/2026-10-27-compare/ (logged in its NOTES.md); a dark
theme pass repeats the tab. DATAVIZ_SCREENSHOTS=<dir> saves them elsewhere."""

import os
import shutil
from pathlib import Path

import pytest

from conftest import make_jpeg
from test_page_smoke import _browser, _send, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')

SHOTS = Path(os.environ.get('DATAVIZ_SCREENSHOTS') or Path(__file__).resolve().parents[1] / 'docs/screenshots/2026-10-27-compare')

# every dot of the map (not the rings, picks, provisional live dots or the density layer)
DOTS = """() => (document.getElementById('plot').data || [])
  .filter(t => t.type === 'scattergl' && t.mode === 'markers' && !['highlight', 'picked', 'hover-dot'].includes(t.name)
    && !String(t.name).startsWith('new boxes') && t.name !== 'outside lasso')
  .reduce((n, t) => n + (t.x || []).filter(v => v !== null).length, 0)"""

SUMMARY = "() => (document.querySelector('#cmp-img-cards .cmp-card[data-img=\"same\"] b')?.textContent || '–') !== '–'"


def _yolo(root, boxes, classes=('cat', 'dog')):
    (root / 'labels').mkdir(parents=True, exist_ok=True)
    (root / 'classes.txt').write_text('\n'.join(classes) + '\n')
    for k, (name, lines) in enumerate(boxes):
        make_jpeg(root / 'images' / f'{name}.jpg', 96, 72, color=(30 + 20 * k, 200 - 15 * k, (53 * k) % 255))
        (root / 'labels' / f'{name}.txt').write_text(lines)
    return root


def _datasets(tmp):
    common = [(f'common{k}', f'{k % 2} 0.5 0.5 {0.2 + 0.05 * k:.2f} 0.3\n') for k in range(5)]
    a = _yolo(tmp / 'dsA', common + [('orig', '0 0.5 0.5 0.4 0.4\n'),
                                     ('edit', '0 0.3 0.3 0.2 0.2\n1 0.7 0.7 0.2 0.2\n'),
                                     ('onlya', '1 0.5 0.5 0.2 0.2\n')])
    b = _yolo(tmp / 'dsB', common + [('renamed', '0 0.5 0.5 0.4 0.4\n'),
                                     ('edit', '0 0.32 0.3 0.2 0.2\n0 0.7 0.7 0.2 0.2\n1 0.15 0.8 0.1 0.1\n'),
                                     ('onlyb', '0 0.5 0.5 0.3 0.3\n')])
    for k in range(5):                                   # same bytes on both sides
        shutil.copy(a / 'images' / f'common{k}.jpg', b / 'images' / f'common{k}.jpg')
    shutil.copy(a / 'images' / 'orig.jpg', b / 'images' / 'renamed.jpg')       # same content, other name
    shutil.copy(a / 'images' / 'edit.jpg', b / 'images' / 'edit.jpg')
    make_jpeg(b / 'images' / 'onlyb.jpg', 96, 72, color=(200, 10, 200))
    return a, b


class _Shots:
    """NN-<screen>-<what>.png in order."""

    def __init__(self, page):
        self.page, self.n = page, 0
        SHOTS.mkdir(parents=True, exist_ok=True)

    def __call__(self, name, el=None):
        self.n += 1
        target = self.page.locator(el) if el else self.page
        target.screenshot(path=str(SHOTS / f'{self.n:02d}-{name}.png'))


def test_compare_tab_end_to_end(server, tmp_path):  # noqa: F811
    a, b = _datasets(tmp_path)
    A = _send(f'{server}/api/datasets', {'kind': 'local', 'name': 'shop A', 'path': str(a)})
    B = _send(f'{server}/api/datasets', {'kind': 'local', 'name': 'shop B', 'path': str(b)})
    U = _send(f'{server}/api/datasets', {'kind': 'union', 'name': 'A vs B', 'reuse': True,
                                         'parts': [{'id': A['id'], 'alias': 'A'}, {'id': B['id'], 'alias': 'B'}]})
    again = _send(f'{server}/api/datasets', {'kind': 'union', 'name': 'A vs B', 'reuse': True,
                                             'parts': [{'id': A['id']}, {'id': B['id']}]})
    assert again['id'] == U['id']                        # reuse: the same union, not a second one
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        shot = _Shots(page)
        page.goto(f'{server}/s/{U["id"]}/#compare')
        page.wait_for_selector('#cmp-side:not(.hidden)', timeout=90000)
        assert page.locator('#act-compare').is_visible()
        # ---- the summary: known answer
        page.wait_for_function(SUMMARY, timeout=60000)
        card = lambda attr, k: page.locator(f'#cmp-body .cmp-card[data-{attr}="{k}"] b').inner_text().strip()  # noqa: E731
        assert (card('img', 'only_a'), card('img', 'only_b'), card('img', 'same'), card('img', 'labels_changed')) == ('1', '1', '6', '1')
        assert (card('box', 'same'), card('box', 'moved'), card('box', 'reclass'), card('box', 'added'), card('box', 'removed')) == \
            ('6', '1', '1', '1', '0')
        assert card('box', 'only_a') == '1' and card('box', 'only_b') == '1'
        shot('compare-tab-summary', '#cmp-side')
        # ---- the map: compute it, then Both vs Diff only (a restyle: the number of traces stays)
        page.click('#btn-compute')
        page.wait_for_function(f'({DOTS})() > 0 && document.getElementById("plot-wait").classList.contains("hidden")', timeout=90000)
        page.click('#cmp-body [data-color="diff"]')
        page.wait_for_function("(document.getElementById('plot').data || []).some(t => String(t.name).startsWith('moved'))", timeout=20000)
        both = page.evaluate(DOTS)
        assert both == 19, both                          # A 9 boxes + B 10
        n_traces = page.evaluate("document.getElementById('plot').data.length")
        shot('compare-map-both-diff-colours')
        page.click('#cmp-body [data-show="diff"]')
        page.wait_for_function(f'({DOTS})() < {both}', timeout=10000)
        diff = page.evaluate(DOTS)
        assert diff == 7, diff                           # moved 2 + reclass 2 + added 1 + image only 2
        assert page.evaluate("document.getElementById('plot').data.length") == n_traces
        shot('compare-map-diff-only')
        page.click('#cmp-body [data-show="a"]')
        page.wait_for_function(f'({DOTS})() === 9', timeout=10000)
        page.click('#cmp-body [data-show="both"]')
        page.wait_for_function(f'({DOTS})() === {both}', timeout=10000)
        # ---- density layer: a heatmap trace first, under the dots
        page.check('#cmp-dens')
        page.wait_for_function("(document.getElementById('plot').data || [])[0]?.type === 'heatmap'", timeout=10000)
        assert page.locator('#cmp-dens-legend').inner_text().count('denser') == 2
        shot('compare-map-density')
        page.fill('#cmp-dens-g', '96')
        page.dispatch_event('#cmp-dens-g', 'input')
        page.wait_for_function("document.getElementById('plot').data[0].z.length === 96", timeout=10000)
        # ---- class table + confusion: dog -> cat once
        page.click('#cmp-conf-d > summary')
        page.wait_for_selector('#cmp-confusion .main-svg', timeout=10000)
        page.click('#cmp-hist-d > summary')
        page.wait_for_selector('#cmp-h-area .main-svg', timeout=10000)
        page.locator('#cmp-hist-d').scroll_into_view_if_needed()
        shot('compare-tab-classes-hists', '#cmp-side')
        # ---- Added -> the gallery with that one box
        page.click('#cmp-body .cmp-card[data-box="added"]')
        page.wait_for_selector('#gallery-panel:not(.hidden)', timeout=10000)
        page.wait_for_function("document.querySelector('#gallery-count').textContent.trim().startsWith('1 ')", timeout=10000)
        assert page.locator('#gallery .card[data-id]').count() == 1
        assert page.locator('#gallery-title').inner_text().startswith('Added')
        shot('explore-gallery-added')
        # ---- an image list -> the image browser
        page.click('#activity [data-tab="compare"]')
        page.click('#cmp-body .cmp-card[data-img="same"]')
        page.wait_for_function("document.querySelector('#gallery-count').textContent.startsWith('6 images')", timeout=10000)
        assert page.locator('#gallery .img-subset').count() == 1
        page.wait_for_function("[...document.querySelectorAll('#gallery .img-card img')].every(i => i.complete && i.naturalWidth > 0)", timeout=20000)
        shot('explore-images-identical')
        # ---- side by side: the image with the changes, zoom one pane, both transforms equal
        page.click('#activity [data-tab="compare"]')
        page.click('#cmp-body .cmp-card[data-img="labels_changed"] [data-sbs]')
        page.wait_for_selector('#cmpv:not(.hidden) .cmpv-pane[data-side="b"] rect.cmpv-box', timeout=20000)
        assert page.locator('.cmpv-pane[data-side="a"] rect.cmpv-box').count() == 2
        assert page.locator('.cmpv-pane[data-side="b"] rect.cmpv-box').count() == 3
        shot('viewer-side-by-side')
        box = page.locator('.cmpv-pane[data-side="a"] .cmpv-view').bounding_box()
        page.mouse.move(box['x'] + box['width'] * 0.3, box['y'] + box['height'] * 0.4)
        for _ in range(3):
            page.mouse.wheel(0, -240)
        page.wait_for_function("document.querySelector('.cmpv-pane[data-side=\"a\"] .cmpv-layer').style.transform !== ''", timeout=5000)
        ta, tb = page.evaluate("[...document.querySelectorAll('.cmpv-layer')].map(e => e.style.transform)")
        assert ta and ta == tb, (ta, tb)
        # hover a moved box of A: its partner in B is ringed too
        page.locator('.cmpv-pane[data-side="a"] rect.cmpv-box.k1').hover(force=True)
        assert page.locator('.cmpv-pane[data-side="b"] rect.cmpv-box.hl').count() == 1
        shot('viewer-zoomed-synced-hover')
        page.click('.cmpv [data-mode="swipe"]')
        page.mouse.move(box['x'] + box['width'] * 0.8, box['y'] + 100)
        shot('viewer-swipe')
        page.keyboard.press('Escape')
        assert page.locator('#cmpv').is_hidden()
        # ---- the full-image dialog offers Compare A | B
        page.click('#activity [data-tab="explore"]')
        page.locator('#gallery .img-card').first.click()
        page.wait_for_selector('#m-cmp:not(.hidden)', timeout=10000)
        page.click('#m-cmp')
        page.wait_for_selector('#cmpv:not(.hidden) .cmpv-pane[data-side="a"] img', timeout=10000)
        page.keyboard.press('Escape')
        # ---- dark theme: the tab and the viewer again
        page.evaluate("localStorage.setItem('theme', 'dark')")
        page.emulate_media(color_scheme='dark')
        page.goto(f'{server}/s/{U["id"]}/#compare')
        page.reload()                                    # same URL: goto alone would not load the page again
        page.wait_for_function(SUMMARY, timeout=60000)
        if page.locator('#btn-compute').is_visible():          # the map is cached by now: it loads on its own
            page.click('#btn-compute')
        page.wait_for_function(f'({DOTS})() > 0 && document.getElementById("plot-wait").classList.contains("hidden")', timeout=90000)
        page.click('#cmp-body [data-color="diff"]')
        page.check('#cmp-dens')
        page.wait_for_function("(document.getElementById('plot').data || [])[0]?.type === 'heatmap'", timeout=10000)
        shot('dark-compare-tab-map')
        page.click('#cmp-body .cmp-card[data-img="labels_changed"] [data-sbs]')
        page.wait_for_selector('#cmpv:not(.hidden) .cmpv-pane[data-side="b"] rect.cmpv-box', timeout=20000)
        shot('dark-viewer-side-by-side')
        browser.close()
    assert not errors, '\n'.join(errors)
