"""The Analysis page in a real browser, against a real PostgreSQL."""

from urllib.parse import urlsplit

import psycopg
import pytest

from test_page_smoke import _browser, _send, server  # noqa: F401 - the fixture

sync_api = pytest.importorskip('playwright.sync_api')


def type_sql(page, sql):
    box = page.locator('#an-sql .cm-content')
    box.click()
    page.keyboard.press('Control+a')
    page.keyboard.press('Delete')
    page.keyboard.insert_text(sql)


@pytest.fixture
def target(server, pg):  # noqa: F811
    url = urlsplit(pg.replace('+psycopg', ''))
    with psycopg.connect(pg.replace('+psycopg', ''), autocommit=True) as c:
        c.execute('CREATE TABLE public.orders (id int PRIMARY KEY, total numeric(10,2), note text)')
        c.execute("INSERT INTO public.orders SELECT i, i * 1.5, CASE WHEN i % 2 = 0 THEN 'even' END FROM generate_series(1, 500) i")
    conn = _send(f'{server}/api/connections', {'kind': 'postgres', 'name': 'sales', 'host': url.hostname, 'port': url.port,
                                               'database': url.path.lstrip('/'), 'username': url.username,
                                               'secret_key': url.password, 'schema': 'public'})
    return {'url': pg.replace('+psycopg', ''), 'db': url.path.lstrip('/'), 'conn': conn['id']}


def test_query_loop(server, target, pg):  # noqa: F811
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 500 else None)
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)

        # the object explorer: server, database, table, columns
        page.click('.an-tn:has-text("sales")')
        page.click(f'.an-tn:has-text("{target["db"]}")')
        page.wait_for_selector('.an-tn:has-text("public.orders")', timeout=10000)
        page.click('.an-tn:has-text("public.orders")')
        page.wait_for_selector('.an-tcol:has-text("total")', timeout=10000)

        # double-click: a new tab with a ready query, nothing has run
        page.dblclick('.an-tn:has-text("public.orders")')
        page.wait_for_selector('.an-tab.on:has-text("orders")')
        assert page.locator('.an-sql .cm-content').inner_text().startswith('SELECT')
        assert page.locator('#an-rc .an-grid').count() == 0

        page.wait_for_selector('#an-sql .cm-content')
        type_sql(page, 'SELECT id, total, note FROM public.orders ORDER BY id')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#an-rc .an-grid td', timeout=20000)
        assert '500' in page.locator('#an-sum').inner_text()
        assert page.locator('#an-rc .an-grid td.an-null').first.inner_text() == 'NULL'

        # scrolling far loads later rows, no page holds them all
        page.locator('.an-gridbox').evaluate('e => { e.scrollTop = 26 * 480 }')
        page.wait_for_selector('#an-rc .an-grid td:text-is("481")', timeout=10000)
        assert page.locator('#an-rc .an-grid tbody tr').count() < 120

        # sort and filter stay on the saved result
        page.click('th[data-sort="id"]')
        page.click('th[data-sort="id"]')
        page.wait_for_selector('#an-rc .an-grid td:text-is("500")', timeout=10000)
        page.fill('#an-gfilter', 'even')
        page.wait_for_function("document.querySelector('.an-gcount').textContent.startsWith('250 of 500')", timeout=10000)
        page.fill('#an-gfilter', '')

        # a change asks first, and Cancel leaves the data alone
        type_sql(page, 'DELETE FROM public.orders WHERE id = 1')
        page.wait_for_selector('#an-stale:not([hidden])')
        page.click('.an-toolbar [data-act="run"]')
        page.wait_for_selector('.an-confirm code:has-text("DELETE")', timeout=10000)
        page.click('.ui-dlg [data-dlg="cancel"]')
        with psycopg.connect(target['url']) as c:
            assert c.execute('SELECT count(*) FROM public.orders').fetchone()[0] == 500

        # confirmed, it runs and reaches the history with its text
        page.click('.an-toolbar [data-act="run"]')
        page.wait_for_selector('.an-confirm code')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("!document.querySelector('.an-toolbar [data-act=\"run\"]').disabled", timeout=10000)
        page.wait_for_timeout(600)
        with psycopg.connect(target['url']) as c:
            assert c.execute('SELECT count(*) FROM public.orders').fetchone()[0] == 499

        # a failed query keeps its message and does not crash the page
        type_sql(page, 'SELECT * FROM public.nothing_here')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('.an-err', timeout=10000)
        assert 'nothing_here' in page.locator('.an-err').inner_text()

        # stop
        type_sql(page, 'SELECT pg_sleep(30)')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('[data-act="stop"]:not([disabled])', timeout=10000)
        page.click('[data-act="stop"]')
        page.wait_for_function("!document.querySelector('.an-toolbar [data-act=\"run\"]').disabled", timeout=10000)

        # cost
        type_sql(page, 'SELECT * FROM public.orders o JOIN public.orders b ON o.id = b.id')
        page.click('[data-act="rt"][data-k="cost"]')
        page.click('[data-act="explain"]')
        page.wait_for_selector('.an-cost', timeout=10000)

        page.click('.an-rail [data-k="history"]')
        page.wait_for_selector('.an-hq:has-text("DELETE FROM public.orders")', timeout=10000)
        page.fill('#an-hfind', 'nothing_here')
        assert page.locator('#an-hist tbody tr:not([hidden])').count() == 1

        # tabs come back with the session
        page.reload()
        page.wait_for_selector('.an-tab:has-text("orders")', timeout=20000)
        browser.close()
    assert not errors, errors
