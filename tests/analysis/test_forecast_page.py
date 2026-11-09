"""Analyze › Forecast and the dashboard Forecast widget in a real browser, against a real PostgreSQL."""

import pytest
from test_analysis_page import target  # noqa: F401 - the fixture
from test_page_smoke import _browser, server  # noqa: F401 - the fixture

sync_api = pytest.importorskip('playwright.sync_api')

SQL = ("SELECT d::date AS day, (100 + extract(dow FROM d) * 10 + extract(doy FROM d))::float AS sales, "
       "CASE WHEN extract(day FROM d)::int % 2 = 0 THEN 'north' ELSE 'south' END AS region "
       "FROM generate_series(DATE '2025-01-06', DATE '2025-08-31', INTERVAL '1 day') d")


def test_forecast_sheet_and_widget(server, target):  # noqa: F811
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 400 and '/api/' in r.url else None)
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)
        page.select_option('#an-conn', target['conn'])
        page.wait_for_function("document.querySelector('#an-db option:nth-child(2)')", timeout=10000)
        page.select_option('#an-db', target['db'])
        page.locator('#an-sql .cm-content').click()
        page.keyboard.insert_text(SQL)
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#an-rc .an-grid td', timeout=20000)

        page.click('.an-rail [data-k="analyze"]')
        page.click('[data-act="sheet"][data-k="forecast"]')
        page.wait_for_selector('#an-f-date', timeout=20000)
        assert page.locator('#an-f-date').input_value() == 'day'
        assert page.locator('#an-f-val').input_value() == 'sales'
        page.select_option('#an-f-group', 'region')
        page.click('[data-act="fc-run"]')
        page.wait_for_selector('#an-f-out .an-fc svg polygon', timeout=20000)
        assert 'Next day' in page.locator('#an-f-out .an-hint').first.inner_text()
        assert page.locator('#an-f-out .stat-strip b').count() == 4
        assert page.locator('#an-f-out td:has-text("north")').count() == 1

        # leaving and coming back keeps the answer
        page.click('[data-act="sheet"][data-k="profile"]')
        page.click('[data-act="sheet"][data-k="forecast"]')
        page.wait_for_selector('#an-f-out .an-fc svg', timeout=20000)

        # a wrong pick is said plainly, not a crash
        page.select_option('#an-f-date', 'region')
        page.click('[data-act="fc-run"]')
        page.wait_for_selector('#an-f-out .notice.bad:has-text("holds no dates")', timeout=20000)
        errors[:] = [e for e in errors if '/forecast' not in e]       # that 400 was the point

        # the dashboard draft has a forecast widget with its band
        page.click('.an-rail [data-k="dash"]')
        page.wait_for_selector('.an-fld:has-text("sales")', timeout=20000)
        page.click('[data-act="d-auto"]')
        page.wait_for_selector('.an-w:has-text("Forecast by day") svg polygon', timeout=20000)
        browser.close()
    assert not errors, errors
