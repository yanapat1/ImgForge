"""End-to-end: Projection › Dimensions = 3D cloud (plot/render3d.ts). Generate draws scatter3d traces with z, the
2D-only controls hide, a camera preset moves the camera, Shift+drag selects the dots under the outline (through the
scene camera), picking two cards draws their distance line, and 2D comes back as before. Skips like
test_page_smoke without a browser. DATAVIZ_TEST_SHOTS=<folder> saves screenshots of both views."""

import os

import pytest

from test_page_smoke import _browser, _get, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')

_SHOWN = 'window.dataviz.S.viewReq && window.dataviz.S.viewReq.dims'
_TYPES = "[...new Set(document.getElementById('plot').data.map(t => t.type))]"


def _shot(page, name):
    d = os.environ.get('DATAVIZ_TEST_SHOTS')
    if d:
        page.screenshot(path=os.path.join(d, f'{name}.png'))


def test_3d_cloud(server):  # noqa: F811 - the fixture
    sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.goto(f'{server}/?session={sid}')
        page.click('#btn-compute')
        page.wait_for_function(f'({_SHOWN}) === 2', timeout=60000)
        assert 'scattergl' in page.evaluate(_TYPES)
        _shot(page, '2d')

        page.click('#map-btn')
        page.select_option('#proj-dims', '3')
        assert page.locator('#map-dirty').is_visible(), 'switching to 3D marks the map as changed'
        page.click('#btn-apply')
        page.wait_for_function(f'({_SHOWN}) === 3', timeout=60000)
        page.wait_for_function("document.getElementById('plot').data.some(t => t.type === 'scatter3d' && t.z && t.z.length)")
        assert page.evaluate("document.body.classList.contains('plot-3d')")
        assert page.locator('#d3-tools').is_visible() and page.locator('#offscreen').is_hidden()
        page.keyboard.press('Escape')

        # camera preset: XY looks straight down the 3rd axis
        page.click('#d3-tools [data-cam="top"]')
        page.wait_for_function("(() => { const e = document.getElementById('plot')._fullLayout.scene.camera.eye; "
                               "return Math.abs(e.x) < 0.05 && Math.abs(e.y) < 0.05 && e.z > 2; })()")
        page.click('#d3-tools [data-cam="iso"]')
        page.wait_for_timeout(400)

        # the turntable turns the eye around the up axis; a second click stops it
        eye = "JSON.stringify(document.getElementById('plot')._fullLayout.scene.camera.eye)"
        before = page.evaluate(eye)
        page.click('#d3-rotate')
        page.wait_for_function(f'{eye} !== {before!r}', timeout=5000)
        page.click('#d3-rotate')
        assert page.get_attribute('#d3-rotate', 'aria-pressed') == 'false'
        page.click('#d3-tools [data-cam="iso"]')
        page.wait_for_timeout(400)

        # Shift+drag around the whole plot selects every shown dot
        box = page.locator('#plot').bounding_box()
        x0, y0, x1, y1 = box['x'] + 20, box['y'] + 60, box['x'] + box['width'] - 20, box['y'] + box['height'] - 20
        page.keyboard.down('Shift')
        page.mouse.move(x0, y0)
        page.mouse.down()
        for x, y in ((x1, y0), (x1, y1), (x0, y1), (x0, y0 + 5)):
            page.mouse.move(x, y, steps=8)
        page.mouse.up()
        page.keyboard.up('Shift')
        page.wait_for_function('window.dataviz.S.sel.length > 0', timeout=5000)
        n_sel = page.evaluate('window.dataviz.S.sel.length')
        n_vis = page.evaluate("document.getElementById('plot').data.filter(t => t.type === 'scatter3d' && t.customdata"
                              " && !['highlight','picked','hover-dot','centres'].includes(t.name))"
                              ".reduce((a, t) => a + t.customdata.length, 0)")
        assert n_sel == n_vis, (n_sel, n_vis)

        # two picked cards: the distance line between their dots
        cards = page.locator('#gallery .card')
        cards.nth(0).click(modifiers=['Control'])                 # ctrl+click picks (a plain click opens the image)
        cards.nth(1).click(modifiers=['Control'])
        page.wait_for_function("(document.getElementById('plot').data.find(t => t.name === 'pick-lines') || {x: []}).x.length >= 2",
                               timeout=10000)
        page.click('#btn-groups')
        page.check('#d3-shadows')
        page.check('#d3-fog')
        page.keyboard.press('Escape')
        page.wait_for_timeout(600)
        _shot(page, '3d')

        # back to the flat map
        page.click('#map-btn')
        page.select_option('#proj-dims', '2')
        page.click('#btn-apply')
        page.wait_for_function(f'({_SHOWN}) === 2', timeout=60000)
        page.wait_for_function("document.getElementById('plot').data.some(t => t.type === 'scattergl')")
        assert not page.evaluate("document.body.classList.contains('plot-3d')")
        assert 'scatter3d' not in page.evaluate(_TYPES)
        browser.close()
    assert not errors, '\n'.join(errors)
