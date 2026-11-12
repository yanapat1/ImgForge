"""The UI sheet (?ui): every shared part draws with the real stylesheets in each theme, look and palette, with no page
error or failed request. DATAVIZ_SCREENSHOTS=<dir> saves the sheet in dark and light."""

import os

import pytest

from test_page_smoke import _browser, server  # noqa: F401 - the fixture

sync_api = pytest.importorskip('playwright.sync_api')
PARTS = ('header', 'activity', 'side', 'buttons', 'segmented', 'inputs', 'tabs', 'toolbar', 'labels', 'feedback', 'tables', 'tiles',
         'foundations')


def test_ui_sheet_shows_every_part_in_every_style(server):  # noqa: F811
    problems = []
    shots = os.environ.get('DATAVIZ_SCREENSHOTS')
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: problems.append(f'pageerror: {e}'))
        page.on('response', lambda r: problems.append(f'{r.status} {r.url}') if r.status >= 400 else None)
        page.goto(f'{server}/?ui')
        page.wait_for_selector('#ui-sheet .ui-part', timeout=20000)
        assert page.evaluate('document.documentElement.dataset.screen') == 'ui'
        for part in PARTS:
            assert page.locator(f'#ui-{part}').count() == 1, part
        # the header is the shared one: brand, page links, settings and theme
        head = page.locator('#ui-header .app-header')
        assert head.locator('.brand h1').inner_text() == 'DataViz'
        assert head.locator('.hdr-nav a.on').inner_text().strip() == 'Data'
        assert [a.strip() for a in head.locator('.hdr-nav a').all_inner_texts()] == ['Bucket', 'Image', 'Data']
        assert head.locator('[data-act="settings"]').count() == 1 and head.locator('[data-act="theme"]').count() == 1
        # the parts are styled by the shared sheets, not by the browser defaults
        assert page.evaluate("getComputedStyle(document.querySelector('#ui-buttons button.primary')).backgroundColor") != 'rgba(0, 0, 0, 0)'
        assert page.evaluate("getComputedStyle(document.querySelector('#ui-tabs .tabbar [aria-selected=true]')).borderBottomStyle") == 'solid'
        page.click('[data-ui="dialog"]')
        page.wait_for_selector('.ui-dlg', timeout=5000)
        page.keyboard.press('Escape')
        for theme in ('dark', 'light'):
            page.select_option('#ui-theme', theme)
            page.wait_for_timeout(400)                     # controls fade between themes (--t-fast)
            if shots:
                page.screenshot(path=f'{shots}/ui-sheet-{theme}.png', full_page=False)
            for look in ('default', 'editorial', 'blueprint', 'brutalist', 'terminal', 'soft'):
                page.select_option('#ui-look', look)
                text = page.evaluate("getComputedStyle(document.querySelector('#ui-buttons .ui-demo')).color")
                assert text not in ('', 'rgba(0, 0, 0, 0)'), (theme, look)
            page.select_option('#ui-look', 'default')
            for pal in ('cbsafe', 'contrast', 'fjord', 'ember', 'orchid', 'default'):
                page.select_option('#ui-palette', pal)
        page.wait_for_timeout(300)
        browser.close()
    assert not problems, problems
