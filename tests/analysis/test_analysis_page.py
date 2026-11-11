"""The Analysis page in a real browser, against a real PostgreSQL."""

from urllib.parse import urlsplit

import json
import os
import re

from pathlib import Path

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

        # after a run the server version pill shows: Layout stays with its buttons, the pane headers stay one line
        page.wait_for_selector('.an-ver')
        lay = page.locator('.an-lay').bounding_box()
        assert lay['height'] < 40, lay
        for head in page.locator('.an-sqlhead').all():
            assert head.bounding_box()['height'] <= 36

        # scrolling far loads later rows, no page holds them all
        page.locator('.an-gridbox').evaluate('e => { e.scrollTop = 28 * 480 }')     # 28 px rows (kit .data-grid, grid.ts ROW)
        page.wait_for_selector('#an-rc .an-grid td:text-is("481")', timeout=10000)
        assert page.locator('#an-rc .an-grid tbody tr').count() < 120

        # sort and filter stay on the saved result
        page.click('th[data-sort="id"]')
        page.click('th[data-sort="id"]')
        page.wait_for_selector('#an-rc .an-grid td:text-is("500")', timeout=10000)
        page.fill('#an-gfilter', 'even')
        page.wait_for_function("document.querySelector('.an-gcount').textContent.startsWith('250 of 500')", timeout=10000)
        page.fill('#an-gfilter', '')

        # Export: pick the format; the file is that format, and the last one used comes first next time
        page.click('#an-rc [data-act="export"]')
        page.wait_for_selector('#an-menu [data-m="xlsx"]')
        assert page.locator('#an-menu [data-m]').count() == 9
        with page.expect_download() as dl:
            page.click('#an-menu [data-m="csv"]')
        assert dl.value.suggested_filename.endswith('.csv')
        assert Path(dl.value.path()).read_text().splitlines()[0] == 'id,total,note'
        page.click('#an-rc [data-act="export"]')
        page.wait_for_selector('#an-menu [data-m]')
        assert page.locator('#an-menu [data-m]').first.get_attribute('data-m') == 'csv'
        page.keyboard.press('Escape')

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


def test_visual_builder(server, target):  # noqa: F811
    errors = []
    with psycopg.connect(target['url'], autocommit=True) as c:
        c.execute('CREATE TABLE public.customers (id int PRIMARY KEY, name text, segment text)')
        c.execute("INSERT INTO public.customers SELECT i, 'c' || i, 'SME' FROM generate_series(1, 20) i")
        c.execute('ALTER TABLE public.orders ADD COLUMN customer_id int')
        c.execute('UPDATE public.orders SET customer_id = id % 30')
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 500 else None)
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)
        page.select_option('#an-conn', target['conn'])
        page.wait_for_function("document.querySelector('#an-db option:nth-child(2)')", timeout=10000)
        page.select_option('#an-db', target['db'])
        page.click('.an-tn:has-text("sales")')
        page.click(f'.an-tn:has-text("{target["db"]}")')
        page.wait_for_selector('.an-tn:has-text("public.orders")', timeout=10000)

        def sql():
            return page.locator('#an-sql .cm-content').inner_text().replace('\n', ' ')

        for name in ('public.orders', 'public.customers'):
            page.drag_and_drop(f'.an-tn:has-text("{name}")', '#an-canvas', target_position={'x': 300, 'y': 200})
            page.wait_for_selector(f'.an-tb:has-text("{name.split(".")[1]}") .an-col', timeout=10000)
        page.click('.an-tb[data-t="o"] .an-col[data-c="id"] .lbl')
        page.click('.an-tb[data-t="c"] .an-col[data-c="name"] .lbl')
        page.wait_for_function("document.querySelector('#an-sql .cm-content').innerText.includes('name')", timeout=10000)
        assert sql().count('SELECT') == 2                         # not joined: one result each

        # nothing ran: editing the builder never reaches the database
        assert page.locator('#an-rc .an-grid').count() == 0
        a = page.locator('.an-col[data-t="o"][data-c="customer_id"]').bounding_box()
        b = page.locator('.an-col[data-t="c"][data-c="id"]').bounding_box()
        page.mouse.move(a['x'] + 30, a['y'] + 6)
        page.mouse.down()
        page.mouse.move(a['x'] + 60, a['y'] + 26, steps=4)
        page.mouse.move(b['x'] + 30, b['y'] + 6, steps=6)
        page.mouse.up()
        page.wait_for_selector('.an-jl', timeout=10000)
        page.wait_for_function("document.querySelector('#an-sql .cm-content').innerText.includes('INNER JOIN')", timeout=10000)
        assert sql().count('SELECT') == 1

        # join popup: LEFT, then Run
        page.click('.an-jl')
        page.select_option('#an-j-type', 'LEFT')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#an-sql .cm-content').innerText.includes('LEFT OUTER JOIN')", timeout=10000)
        page.click('.an-toolbar [data-act="run"]')
        page.wait_for_selector('#an-rc .an-grid td', timeout=20000)
        assert '500' in page.locator('#an-sum').inner_text()

        # after an edit the results stay, with a notice
        page.click('.an-col[data-t="c"][data-c="segment"] .lbl')
        page.wait_for_selector('#an-stale:not([hidden])', timeout=10000)
        assert page.locator('#an-rc .an-grid td').count() > 0

        # CAST and a computed column reach the SQL
        page.click('.an-ty[data-t="o"][data-c="total"]')
        page.fill('#an-c-type', 'integer')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#an-sql .cm-content').innerText.includes('CAST')", timeout=10000)
        page.click('[data-act="b-calc"]')
        page.click('.an-seg button[data-tab="window"]')
        page.fill('#an-k-alias', 'rn')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#an-sql .cm-content').innerText.includes('ROW_NUMBER')", timeout=10000)
        page.click('.an-toolbar [data-act="run"]')
        page.wait_for_selector('th[data-sort="rn"]', timeout=20000)

        # undo walks back, right-click removes the join
        page.click('#an-canvas .an-cvt.tl [data-act="b-undo"]')
        page.wait_for_function("!document.querySelector('#an-sql .cm-content').innerText.includes('ROW_NUMBER')", timeout=10000)
        page.click('.an-jl', button='right')
        page.wait_for_function("!document.querySelector('.an-jl')", timeout=10000)

        # typed SQL: the canvas waits, then follows after a successful run
        box = page.locator('#an-sql .cm-content')
        box.click()
        page.keyboard.press('Control+a')
        page.keyboard.press('Delete')
        page.keyboard.insert_text("SELECT c.name, CASE WHEN c.id > 5 THEN 'big' ELSE 'small' END AS size FROM public.customers AS c")
        page.wait_for_selector('#an-canvas .an-cvt.tl:has-text("Press Run")', timeout=10000)
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#an-canvas .an-tb[data-t="c"]', timeout=20000)
        page.wait_for_function("document.querySelectorAll('#an-canvas .an-tb').length === 1", timeout=10000)
        page.click('[data-act="b-panel"]')
        page.wait_for_selector('.an-cvt.bl:has-text("size")', timeout=10000)

        # subqueries are cards with their columns, joined by a line (a SELECT * card used to have no rows to end a line on)
        box.click()
        page.keyboard.press('Control+a')
        page.keyboard.press('Delete')
        page.keyboard.insert_text('SELECT * FROM (SELECT id, name FROM public.customers) AS cu '
                                  'LEFT JOIN (SELECT * FROM public.orders WHERE total > 10) AS od ON od.customer_id = cu.id')
        page.keyboard.press('Control+Enter')
        page.wait_for_function("document.querySelectorAll('#an-canvas .an-tb.sub').length === 2", timeout=20000)
        page.wait_for_selector('.an-tb[data-t="cu"] .an-col[data-c="name"]')
        page.wait_for_selector('.an-tb[data-t="od"] .an-col[data-c="customer_id"]')
        assert page.locator('#an-lines .an-jhit').count() == 1 and page.inner_text('.an-jl') == 'LEFT'
        assert not page.locator('#an-canvas .an-cvt.tl:has-text("Locked")').count()

        # a query the builder cannot hold still runs, and says why
        box.click()
        page.keyboard.press('Control+a')
        page.keyboard.press('Delete')
        page.keyboard.insert_text('SELECT id FROM public.customers UNION ALL SELECT id FROM public.orders')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#an-canvas .an-cvt.tl:has-text("Locked")', timeout=20000)
        page.wait_for_selector('#an-rc .an-grid td', timeout=20000)

        # the builder is on the left, next to the object explorer; it closes for SQL only and comes back from the toolbar
        viz, sqlp = (page.locator(f'[data-pane="{k}"]').bounding_box() for k in ('viz', 'sql'))
        assert viz['x'] < sqlp['x']
        page.click('[data-pane="viz"] [data-act="viz-hide"]')
        page.wait_for_selector('[data-pane="viz"]', state='hidden')
        assert page.locator('[data-pane="sql"]').bounding_box()['width'] > viz['width'] + sqlp['width'] - 10
        page.reload()
        page.wait_for_selector('#an-sql .cm-content', timeout=20000)
        assert page.locator('[data-pane="viz"]').is_hidden()                         # remembered
        page.click('.an-vizbtn')
        page.wait_for_selector('[data-pane="viz"] .an-canvas', state='visible')

        # tabs: the open one is filled; double-click renames, its own duplicate copies it
        page.click('[data-act="tab-new"]')
        page.wait_for_selector('.an-tabw.on:has-text("query 2")')
        bg = page.eval_on_selector('.an-tabw.on', 'e => getComputedStyle(e).backgroundColor')
        assert bg not in ('rgba(0, 0, 0, 0)', 'transparent'), bg
        page.dblclick('.an-tabw.on [data-act="qtab"]')
        page.fill('#ui-dlg-in', 'monthly sales')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_selector('.an-tabw.on:has-text("monthly sales")')
        page.click('.an-tabw.on [data-act="tab-dup"]')
        page.wait_for_selector('.an-tabw.on:has-text("monthly sales copy")')
        browser.close()
    assert not errors, errors


def test_workspaces(server, target):  # noqa: F811
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
        page.wait_for_selector('#an-sql .cm-content')
        page.locator('#an-sql .cm-content').click()
        page.keyboard.insert_text("SELECT id, total, CASE WHEN id % 2 = 0 THEN 'even' ELSE 'odd' END AS kind, DATE '2026-01-01' + (id % 5) AS day FROM public.orders")
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#an-rc .an-grid td', timeout=20000)

        # Analyze: profile, a suggested test in plain words, quality, pivot
        page.click('.an-rail [data-k="analyze"]')
        page.wait_for_selector('.an-card h4:has-text("total")', timeout=20000)
        page.click('[data-act="sheet"][data-k="tests"]')
        page.click('.an-suggest button >> nth=0', timeout=20000)
        page.wait_for_selector('#an-t-out .an-hint', timeout=20000)
        page.click('[data-act="sheet"][data-k="quality"]')
        page.wait_for_selector('.an-doc td:has-text("No duplicate rows")', timeout=20000)
        page.click('[data-act="sheet"][data-k="pivot"]')
        page.select_option('#an-p-row', 'kind')
        page.select_option('#an-p-col', 'day')
        page.click('[data-act="pivot-run"]')
        page.wait_for_selector('#an-p-out td:has-text("even")', timeout=20000)

        # Dashboards: a draft from the result, then a saved board comes back
        page.click('.an-rail [data-k="dash"]')
        page.wait_for_selector('.an-fld:has-text("total")', timeout=20000)
        page.click('[data-act="d-auto"]')
        page.wait_for_selector('.an-w svg', timeout=20000)
        n = page.locator('.an-w').count()
        page.click('[data-act="d-add"][data-k="text"]')
        page.wait_for_function(f"document.querySelectorAll('.an-w').length === {n + 1}", timeout=10000)
        page.wait_for_timeout(900)
        page.click('.an-rail [data-k="history"]')
        page.click('.an-rail [data-k="dash"]')
        page.wait_for_function(f"document.querySelectorAll('.an-w').length === {n + 1}", timeout=10000)

        # Catalog: a description is saved and stays
        page.click('.an-tn:has-text("sales")')
        page.click(f'.an-tn:has-text("{target["db"]}")')
        page.click('.an-rail [data-k="catalog"]')
        page.click('.an-tn:has-text("public.orders")', timeout=20000)
        page.wait_for_selector('.an-desc[data-col="note"]', timeout=20000)
        page.fill('.an-desc[data-col="note"]', 'Sales note')
        page.keyboard.press('Tab')
        page.wait_for_selector('td:has-text("You")', timeout=10000)

        # Scripts: named boxes, confirm for a temp table, the temp table appears with its mark
        page.click('.an-rail [data-k="scripts"]')
        page.wait_for_selector('#an-sbody', timeout=20000)
        assert page.locator('#an-work .an-toolbar').bounding_box()['height'] < 60   # one row: name, Run all, connection, database
        page.select_option('#an-sconn', target['conn'])
        page.fill('#an-sdb', target['db'])
        page.fill('#an-sname', '3 temp with doc')
        page.fill('#an-sbody', 'CREATE TEMP TABLE recent AS SELECT * FROM public.orders WHERE id < 5;\nSELECT count(*) FROM recent;')
        page.click('[data-act="script-run"]')
        page.wait_for_selector('.an-confirm code:has-text("CREATE TEMP")', timeout=10000)
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_selector('.an-tn.temp:has-text("recent")', timeout=20000)
        page.wait_for_timeout(800)
        page.reload()
        page.wait_for_selector('.an-main', timeout=20000)
        page.click('.an-rail [data-k="scripts"]')
        page.wait_for_selector('.an-cmd:has-text("3 temp with doc")', timeout=20000)
        page.wait_for_selector('.an-tn.temp:has-text("recent")', timeout=20000)      # a refresh loses nothing
        browser.close()
    assert not errors, errors


def test_plain_http_without_random_uuid(server, target):  # noqa: F811
    """A server opened over plain http at its address is not a secure context: the browser has no crypto.randomUUID.
    The page must still start and make new query tabs, scripts and dashboards (core/uuid.ts)."""
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        page.add_init_script('Object.defineProperty(Crypto.prototype, "randomUUID", {value: undefined, configurable: true})')
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)
        assert page.evaluate('typeof crypto.randomUUID') == 'undefined'
        assert page.locator('.status-line.err').count() == 0
        page.click('[data-act="tab-new"]')
        ids = page.evaluate("[...new Set([...document.querySelectorAll('.an-tab[data-act=qtab]')].map(b => b.textContent))].length")
        assert ids >= 2
        page.click('.an-rail [data-k="scripts"]')
        page.wait_for_timeout(500)
        page.click('.an-rail [data-k="dash"]')
        page.wait_for_timeout(500)
        uid = page.evaluate("import('./static/js/core/uuid.js').then(m => m.uuid())")
        assert re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', uid), uid
        browser.close()
    assert not errors, errors


@pytest.mark.skipif(not os.environ.get('DATAVIZ_SCREENSHOTS'), reason='DATAVIZ_SCREENSHOTS=<dir> saves the pictures')
def test_screenshots_of_every_workspace(server, target):  # noqa: F811
    """Pictures of each workspace in dark and light, wide and narrow, beside the Data Center, to compare with the UI
    sheet (?ui). Not a check by itself: the other tests check behaviour; this is for looking."""
    shots = os.environ['DATAVIZ_SCREENSHOTS']
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        for theme in ('dark', 'light'):
            page = browser.new_page(viewport={'width': 1500, 'height': 900})
            page.add_init_script(f'localStorage.setItem("theme", "{theme}")')
            page.goto(f'{server}/?data')
            page.wait_for_selector('.dc-header', timeout=20000)
            page.wait_for_timeout(600)
            page.screenshot(path=f'{shots}/dc-{theme}.png')
            page.goto(f'{server}/?analysis')
            page.wait_for_selector('.an-main', timeout=20000)
            page.select_option('#an-conn', target['conn'])
            page.wait_for_function("document.querySelector('#an-db option:nth-child(2)')", timeout=10000)
            page.select_option('#an-db', target['db'])
            page.wait_for_selector('#an-sql .cm-content')
            type_sql(page, 'SELECT id, total, note FROM public.orders ORDER BY id')
            page.keyboard.press('Control+Enter')
            page.wait_for_selector('#an-rc .an-grid td', timeout=20000)
            page.wait_for_timeout(600)
            page.screenshot(path=f'{shots}/an-query-{theme}.png')
            for k in ('catalog', 'scripts', 'analyze', 'dash', 'history'):
                page.click(f'.an-rail [data-k="{k}"]')
                page.wait_for_timeout(900)
                page.screenshot(path=f'{shots}/an-{k}-{theme}.png')
            page.set_viewport_size({'width': 400, 'height': 860})
            page.click('.an-rail [data-k="query"]')
            page.wait_for_timeout(600)
            page.screenshot(path=f'{shots}/an-query-{theme}-narrow.png')
            assert page.evaluate('document.documentElement.scrollWidth - innerWidth') <= 0
            page.close()
        browser.close()


def test_analysis_styles_follow_the_ui_sheet():
    """The page is built from the UI sheet's parts (css/kit.css): analysis.css only lays it out. No hand-written colours
    or font sizes, and none of the page's old look-alike parts come back."""
    root = Path(__file__).resolve().parents[2]
    css = (root / 'static/css/analysis.css').read_text(encoding='utf-8')
    body = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(', body), 'colours come from tokens only'
    assert not re.search(r'font-size:\s*\d|font:[^;]*\b\d+(\.\d+)?px', body), 'font sizes come from --fs-* tokens'
    ts = '\n'.join(p.read_text(encoding='utf-8') for p in (root / 'static/ts/analysis').glob('*.ts'))
    for old in ('an-btn', 'an-sel', 'an-zonebar', 'an-zones', 'an-brand', 'an-chip', 'an-tag', 'class="pb"', 'an-search', 'Data Platform'):
        assert old not in ts, f'{old}: use the UI sheet part instead'


def test_analysis_has_the_same_header_as_the_data_center(server):  # noqa: F811
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        heads = {}
        for screen, ready in (('data', '.dc-header'), ('analysis', '.an-main')):
            page.goto(f'{server}/?{screen}')
            page.wait_for_selector(ready, timeout=20000)
            h = page.locator('.app-header:visible').first
            heads[screen] = {'brand': h.locator('.brand h1').inner_text(),
                             'links': [a.strip() for a in h.locator('.hdr-nav a').all_inner_texts()],
                             'on': h.locator('.hdr-nav a.on').inner_text().strip(),
                             'actions': sorted(h.locator('.header-actions [data-act]').evaluate_all('a => a.map(x => x.dataset.act)'))}
        browser.close()
    assert heads['data']['brand'] == heads['analysis']['brand'] == 'Dataset Visualizer'
    assert [x.split('\n')[0] for x in heads['data']['links']] == [x.split('\n')[0] for x in heads['analysis']['links']]
    assert heads['data']['on'].startswith('Data') and heads['analysis']['on'] == 'Analysis'
    assert {'settings', 'theme'} <= set(heads['analysis']['actions']) and {'settings', 'theme'} <= set(heads['data']['actions'])


def test_the_assistant_dock_sits_beside_the_page(server):  # noqa: F811
    """#analysis is position:fixed, so the dock's body padding never reached it: the dock covered the page's right side."""
    from test_page_smoke import _post, _put
    from test_llm import fake_app
    from test_models import _serve
    fake, url = _serve(fake_app())
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1', 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'data_center': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.goto(f'{server}/?analysis')
            page.wait_for_selector('.an-main', timeout=20000)
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            # the welcome: the mark beside one text column (not each part squeezed into its own column)
            hello = page.locator('#ai-dock .dc-ai-hello > div')
            hello.wait_for(timeout=10000)
            assert hello.bounding_box()['width'] > 250
            dock = page.locator('#ai-dock').bounding_box()
            for sel in ('#analysis', '.an-header', '.an-work'):
                b = page.locator(sel).bounding_box()
                assert b['x'] + b['width'] <= dock['x'] + 1, (sel, b, dock)
            page.click('#ai-dock [data-dock="rail"]')
            page.wait_for_function("document.documentElement.dataset.dock === 'rail'")
            rail = page.locator('#ai-dock').bounding_box()
            b = page.locator('#analysis').bounding_box()
            assert abs(b['x'] + b['width'] - rail['x']) <= 1
            page.set_viewport_size({'width': 800, 'height': 800})        # below 900 px the dock is a sheet: the page is full width
            assert page.locator('#analysis').bounding_box()['width'] == 800
            # a suggestion is sent as the user's message and answered
            page.set_viewport_size({'width': 1400, 'height': 900})
            page.click('#ai-dock [data-dock="open"]')
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            page.click('#ai-dock .dc-ai-sugg button >> nth=0')
            page.wait_for_selector('#ai-dock :text("What tables do I have about customers?")', timeout=10000)
            page.wait_for_selector('#ai-dock :text("fake model")', timeout=20000)
            # the context ring shows the answer's context (the provider's count: 21 tokens of 100k), and the same
            # after a reload (it is saved with the answer), not "shown after the first answer"
            ring = '#ai-dock .dock-ring'
            page.wait_for_function(f"document.querySelector({json.dumps(ring)}).title.startsWith('Context used: 0% of 100k')", timeout=10000)
            page.reload()
            page.wait_for_selector('#ai-dock :text("fake model")', timeout=20000)
            page.wait_for_function(f"document.querySelector({json.dumps(ring)}).title.startsWith('Context used: 0% of 100k')", timeout=10000)
            browser.close()
    finally:
        fake.should_exit = True


def test_long_answer_options_wrap_inside_the_dock(server):  # noqa: F811
    """An ask_user option is a sentence the model wrote: it wraps in the dock instead of running past its right side."""
    from test_llm import fake_app
    from test_models import _serve
    from test_page_smoke import _post, _put
    fake, url = _serve(fake_app())
    long_opt = 'Use the monthly revenue table joined to the customer segments, grouped by region and product line, ' * 2
    events = [{'type': 'meta', 'run_id': 'r1', 'model': 'm', 'provider': 'p', 'mode': 'ask'},
              {'type': 'ask', 'question': 'Which data?', 'options': [long_opt.strip(), 'Short one']},
              {'type': 'done', 'usage': {'in': 1, 'out': 1}}]
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1', 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'data_center': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.route('**/api/llm/agent/analysis/chat', lambda r: r.fulfill(
                status=200, content_type='application/x-ndjson', body=''.join(json.dumps(e) + '\n' for e in events)))
            page.add_init_script("localStorage.setItem('dataviz.dock', JSON.stringify({state: 'open', width: 320}))")
            page.goto(f'{server}/?analysis')
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=20000)
            page.fill('#ai-analysis-q', 'show me revenue')
            page.keyboard.press('Enter')
            page.wait_for_selector('#ai-dock .ct-choice >> nth=0', timeout=10000)
            for width in (1400, 900):                       # the narrowest dock, and the sheet
                page.set_viewport_size({'width': width, 'height': 900})
                dock = page.locator('#ai-dock .dock-panel').bounding_box()
                for b in page.locator('#ai-dock .ct-choice').all():
                    box = b.bounding_box()
                    assert box['x'] + box['width'] <= dock['x'] + dock['width'] + 0.5, (width, box, dock)
                first = page.locator('#ai-dock .ct-choice').first.bounding_box()
                assert first['height'] > 40                  # wrapped onto several lines
                assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
            browser.close()
    finally:
        fake.should_exit = True


def test_token_budget_reaches_the_model_size(server):  # noqa: F811
    """Settings › AI assistant: Auto (the model's maximum) by default, the slider goes up to the model's own size,
    and turning Auto off saves a number."""
    from test_llm import fake_app
    from test_models import _serve
    from test_page_smoke import _post, _put
    fake, url = _serve(fake_app())
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                  'model': 'gpt-x', 'context_window': 400000})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'data_center': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.goto(f'{server}/?analysis')
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=20000)
            page.click('#ai-dock [data-dock="settings"]')
            rng = page.locator('#settings-body [data-llm-budget]')
            rng.wait_for(timeout=10000)
            ceiling = int(400000 * 0.98) - 4096
            assert rng.get_attribute('max') == str(ceiling) and rng.is_disabled()
            assert 'This model takes 400k tokens (set on the provider card)' in page.locator('#settings-body').inner_text()
            page.locator('#settings-body [data-act-change="llm-budget-auto"]').uncheck()
            page.wait_for_function("!document.querySelector('#settings-body [data-llm-budget]').disabled", timeout=5000)
            cfg = _put(f'{server}/api/llm/config', {})
            assert cfg['budget']['max_input_tokens'] == ceiling and cfg['limits']['ceiling'] == ceiling
            browser.close()
    finally:
        fake.should_exit = True


def test_joins_show_every_table_of_the_query(server, target):  # noqa: F811
    """A query with two joins: Joins opens on All tables; Count all draws one circle per table with the keys the query
    uses, and a join's numbered dot opens that join's own two circles."""
    with psycopg.connect(target['url'], autocommit=True) as c:
        c.execute('CREATE TABLE public.buyers (id int, region int)')
        c.execute('INSERT INTO public.buyers SELECT i, i % 3 FROM generate_series(1, 40) i')
        c.execute('CREATE TABLE public.regions (id int, name text)')
        c.execute("INSERT INTO public.regions VALUES (0, 'north'), (1, 'south'), (7, 'none')")
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('#an-sql .cm-content', timeout=20000)
        page.select_option('#an-conn', target['conn'])
        type_sql(page, 'SELECT o.id, b.region, r.name FROM public.orders AS o\n'
                       'JOIN public.buyers AS b ON o.id = b.id\nJOIN public.regions AS r ON b.region = r.id')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#an-rc .an-grid td', timeout=20000)
        page.click('#an-rtabs [data-k="joins"]')
        page.wait_for_selector('#an-rc [data-act="join-count-all"]', timeout=10000)
        assert page.locator('#an-rc .an-steps button.primary').inner_text() == 'All tables'
        page.click('#an-rc [data-act="join-count-all"]')
        svg = page.locator('#an-rc svg.an-venn-all')
        svg.wait_for(timeout=20000)
        text = page.locator('#an-rc').inner_text()
        # orders 1..500 x buyers 1..40 -> 40 rows; regions 0 and 1 match (2 of 3 keys), so 27 buyers rows remain
        assert '27\nrows the query keeps' in text or '27 rows the query keeps' in text.replace('\n', ' '), text
        assert svg.locator('.an-vj').count() == 2 and svg.locator('.an-vc').count() == 3 and svg.locator('.an-vc.cut').count() == 3
        assert page.locator('#an-rc table tr:has-text("regions")').inner_text().split() == ['regions', '3', '3', '2', '67%']
        box, rc = svg.bounding_box(), page.locator('#an-rc').bounding_box()
        assert box['x'] + box['width'] <= rc['x'] + rc['width'] + 1
        svg.locator('.an-vj').nth(1).click()
        page.wait_for_selector('#an-rc svg.an-venn:not(.an-venn-all)', timeout=10000)   # counted already: no second ask
        assert not errors, errors
        browser.close()


def test_favorites(server, target):  # noqa: F811
    """Star a table, keep it across a reload, put it in a category, query it from there, then remove it all."""
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 400 and '/api/' in r.url else None)
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)
        fav = '.an-fsec .an-tn[data-act="table"]:has-text("public.orders")'
        try:
            page.wait_for_selector('.an-fsec .an-fnote', timeout=10000)          # empty Favorites says how to fill it
            page.click('.an-tn:has-text("sales")')
            page.click(f'.an-tn:has-text("{target["db"]}")')
            page.wait_for_selector('.an-tn:has-text("public.orders")', timeout=10000)
            tree_star = '.an-trow:has(.an-tn[data-pre="t"][data-name="orders"]) .an-star'
            page.hover(f'.an-tn[data-pre="t"][data-name="orders"]')
            page.click(tree_star)
            page.wait_for_selector(fav)
            assert page.locator(tree_star).get_attribute('aria-pressed') == 'true'

            # kept in the user's prefs: back after a reload
            page.wait_for_timeout(900)
            page.reload()
            page.wait_for_selector(fav, timeout=20000)

            # a category from the right-click menu, the table goes into it
            page.click(fav, button='right')
            page.click('#an-menu [data-m="new"]')
            page.fill('#ui-dlg-in', 'Sales')
            page.click('.ui-dlg [data-dlg="ok"]')
            page.wait_for_selector('.an-fhead[data-g="Sales"]:has-text("1")')
            assert page.locator('.an-fhead[data-g="Sales"] + .an-trow .an-tn:has-text("public.orders")').count() == 1

            # its columns open on their own (not in the tree), a double click queries it
            page.click(fav)
            page.wait_for_selector('.an-fsec .an-tcol:has-text("total")', timeout=10000)
            page.dblclick(fav)
            page.wait_for_selector('.an-tab.on:has-text("orders")')

            # deleting the category keeps the table in Favorites, the star removes it
            page.hover('.an-fhead[data-g="Sales"]')
            page.click('.an-fhead[data-g="Sales"] [data-act="fav-gmenu"]')
            page.click('#an-menu [data-m="delete"]')
            page.wait_for_selector('.an-fhead[data-g="Sales"]', state='detached')
            page.wait_for_selector(fav)
            page.click('.an-fsec .an-trow:has(.an-tn[data-act="table"][data-name="orders"]) .an-star')
            page.wait_for_selector(fav, state='detached')
            page.wait_for_timeout(900)
            page.reload()
            page.wait_for_selector('.an-fsec .an-fnote', timeout=20000)
        finally:   # the other tests share this user: leave no favorites behind
            page.evaluate("fetch('api/me/prefs', {method: 'PUT', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'dataviz'}, body: JSON.stringify({prefs: {analysis_favorites: null}})})")
            page.wait_for_timeout(300)
            browser.close()
    assert not errors, errors


def test_ai_access_from_the_table_menu(server, target):  # noqa: F811
    """Right-click a table › ◆ AI access: Edit rows marks it ✎ and is kept on the server; Change structure asks first;
    Read only clears it."""
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 400 and '/api/' in r.url else None)
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)
        row = '.an-tn[data-pre="t"][data-name="orders"]'
        try:
            page.click('.an-tn:has-text("sales")')
            page.click(f'.an-tn:has-text("{target["db"]}")')
            page.wait_for_selector(row, timeout=10000)
            page.click(row, button='right')
            page.click('#an-menu [data-m="ai-access"][data-g="rows"]')
            page.wait_for_selector(f'{row} .an-aiacc.rows')
            got = page.evaluate("fetch('api/analysis/ai-access').then((r) => r.json())")
            assert [(x['t'], x['level']) for x in got['items']] == [('orders', 'rows')]
            page.click(row, button='right')                       # structure asks first; Cancel keeps Edit rows
            page.click('#an-menu [data-m="ai-access"][data-g="schema"]')
            page.click('.ui-dlg [data-dlg="cancel"]')
            assert page.locator(f'{row} .an-aiacc.rows').count() == 1
            page.click(row, button='right')
            page.click('#an-menu [data-m="ai-access"][data-g="read"]')
            page.wait_for_selector(f'{row} .an-aiacc', state='detached')
        finally:
            page.evaluate("fetch('api/me/prefs', {method: 'PUT', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'dataviz'}, body: JSON.stringify({prefs: {analysis_ai_access: null}})})")
            page.wait_for_timeout(300)
            browser.close()
    assert not errors, errors


def test_objects_panel_resizes(server, target):  # noqa: F811
    """Drag the Objects panel's edge to see long names; it is remembered; a double-click fits the longest name."""
    long = 'customer_lifetime_value_by_region_and_quarter_snapshot'
    with psycopg.connect(target['url'], autocommit=True) as c:
        c.execute(f'CREATE TABLE IF NOT EXISTS public.{long} (id int)')
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)
        page.evaluate("localStorage.removeItem('dataviz.analysis.layout')")
        page.reload()
        page.wait_for_selector('.an-main', timeout=20000)
        page.click('.an-tn:has-text("sales")')
        page.click(f'.an-tn:has-text("{target["db"]}")')
        row = page.locator(f'.an-tn[data-name="{long}"]')
        row.wait_for(timeout=10000)
        assert f'public.{long}' in row.get_attribute('title')                      # the full name on hover
        lbl = row.locator('.lbl')
        assert lbl.evaluate('l => l.scrollWidth > l.clientWidth')                    # cut at the default width
        w0 = page.locator('#an-explorer').bounding_box()['width']

        h = page.locator('.an-expsz').bounding_box()
        page.mouse.move(h['x'] + 3, h['y'] + 300)
        page.mouse.down()
        page.mouse.move(h['x'] + 103, h['y'] + 300, steps=5)
        page.mouse.up()
        w1 = page.locator('#an-explorer').bounding_box()['width']
        assert abs(w1 - (w0 + 100)) <= 2, (w0, w1)
        work = page.locator('.an-work').bounding_box()
        assert work['x'] >= page.locator('#an-explorer').bounding_box()['x'] + w1 - 1  # the work area moved over

        page.reload()                                                                 # remembered
        page.wait_for_selector('.an-main .an-expsz', timeout=20000)
        assert abs(page.locator('#an-explorer').bounding_box()['width'] - w1) <= 2

        page.click('.an-tn:has-text("sales")')
        page.click(f'.an-tn:has-text("{target["db"]}")')
        row.wait_for(timeout=10000)
        page.dblclick('.an-expsz')
        page.wait_for_timeout(100)
        assert lbl.evaluate('l => l.scrollWidth <= l.clientWidth')                   # fits: the whole name shows
        page.focus('.an-expsz')
        page.keyboard.press('Home')                                                   # back to the default
        assert abs(page.locator('#an-explorer').bounding_box()['width'] - w0) <= 2
        browser.close()
    assert not errors, errors


def test_builder_cards(server, target):  # noqa: F811
    """A card opens as wide as its name and can collapse to …; right-click renames it (header only, the SQL keeps the
    table); the Change type dialog offers themed chips and a combobox, not the browser's datalist."""
    long = 'customer_lifetime_value_by_region_and_quarter_snapshot'
    with psycopg.connect(target['url'], autocommit=True) as c:
        c.execute(f'CREATE TABLE IF NOT EXISTS public.{long} (id int, amount_total numeric(10,2))')
    errors = []
    shots = os.environ.get('DATAVIZ_SCREENSHOTS')
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 500 else None)
        page.goto(f'{server}/?analysis')
        page.wait_for_selector('.an-main', timeout=20000)
        page.select_option('#an-conn', target['conn'])
        page.wait_for_function("document.querySelector('#an-db option:nth-child(2)')", timeout=10000)
        page.select_option('#an-db', target['db'])
        page.click('.an-tn:has-text("sales")')
        page.click(f'.an-tn:has-text("{target["db"]}")')
        page.wait_for_selector(f'.an-tn[data-name="{long}"]', timeout=10000)
        for name in ('orders', long):
            page.drag_and_drop(f'.an-tn[data-pre="t"][data-name="{name}"]', '#an-canvas', target_position={'x': 200, 'y': 160})
            page.wait_for_selector(f'.an-tb:has(.an-th .lbl:text-is("{name}")) .an-col', timeout=10000)
        card = page.locator(f'.an-tb:has(.an-th .lbl:text-is("{long}"))')
        lbl = card.locator('.an-th .lbl')
        assert lbl.evaluate('l => l.scrollWidth <= l.clientWidth')                 # the full name shows
        boxes = page.eval_on_selector_all('#an-board .an-tb', 'els => els.map(e => [e.offsetLeft, e.offsetTop, e.offsetWidth, e.offsetHeight])')
        (ax, ay, aw, ah), (bx, by, bw, bh) = boxes
        assert ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay, boxes   # the two cards do not overlap
        if shots:
            page.screenshot(path=f'{shots}/builder-wide.png')

        # collapse: 200 px, the name ends in …; the button brings it back
        card.hover()
        card.locator('[data-act="b-slim"]').click()
        assert card.evaluate('e => e.offsetWidth') == 200
        assert lbl.evaluate('l => l.scrollWidth > l.clientWidth')
        card.locator('[data-act="b-slim"]').click()
        assert lbl.evaluate('l => l.scrollWidth <= l.clientWidth')

        # right-click: the card's menu; Rename changes the header only
        sql_before = page.locator('#an-sql .cm-content').inner_text()
        card.locator('.an-th').click(button='right')
        page.wait_for_selector('#an-menu:not(.hidden) [data-m="rename"]')
        if shots:
            page.screenshot(path=f'{shots}/builder-menu.png')
        page.click('#an-menu [data-m="rename"]')
        page.fill('#ui-dlg-in', 'CLV')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_selector('.an-tb .an-th .lbl:text-is("CLV")')
        assert page.locator('#an-sql .cm-content').inner_text() == sql_before          # the SQL still names the table
        assert long in sql_before
        page.locator('.an-tb:has(.an-th .lbl:text-is("CLV")) .an-th').click(button='right')
        page.click('#an-menu [data-m="slim"]')
        page.wait_for_selector('.an-tb.slim .an-th .lbl:text-is("CLV")')
        page.wait_for_timeout(1000)                                                    # the tab is saved
        page.reload()
        page.wait_for_selector('.an-tb.slim .an-th .lbl:text-is("CLV")', timeout=20000)   # kept

        # Change type: chips by group, the themed combobox, the SQL it makes
        clv = page.locator('.an-tb:has(.an-th .lbl:text-is("CLV"))')
        clv.locator('.an-ty[data-c="amount_total"]').click()
        page.wait_for_selector('.ui-dlg .an-cast')
        assert page.locator('.ui-dlg datalist').count() == 0
        assert page.locator('.ui-dlg .an-cast-now code').first.inner_text() == 'numeric(10,2)'
        page.click('.ui-dlg .chip[data-ty="integer"]')
        assert page.locator('.ui-dlg .chip.on').inner_text() == 'integer'
        assert 'AS integer' in page.locator('#an-c-prev').inner_text()
        page.click('.ui-dlg .cbx-btn')
        page.wait_for_selector('#cbx-list-an-cast', state='visible', timeout=5000)
        if shots:
            page.screenshot(path=f'{shots}/builder-cast.png')
        page.keyboard.press('Escape')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#an-sql .cm-content').innerText.includes('AS integer')", timeout=10000)
        browser.close()
    assert not errors, errors


def test_the_assistant_reads_the_editor_and_shows_its_sql_before_apply(server, target):  # noqa: F811
    """The chat is sent the editor's SQL; a set_editor_query proposal shows the SQL and the diff against the editor,
    Apply puts it in the editor and Ctrl+Z there brings the user's text back; Apply in a new tab keeps it."""
    from test_page_smoke import _post, _put
    from test_llm import fake_app
    from test_models import _serve
    fake, url = _serve(fake_app())
    sent, fix = [], 'SELECT *\nFROM public.orders\nLIMIT 5'
    events = [{'type': 'meta', 'run_id': 'r1', 'model': 'm', 'provider': 'p', 'mode': 'ask'},
              {'type': 'ui_action', 'id': 'u1', 'action': 'set_editor_query', 'label': 'LIMIT 5 instead of TOP',
               'args': {'sql': fix, 'mode': 'replace', 'note': 'LIMIT 5 instead of TOP'}, 'applied': False},
              {'type': 'text', 'text': 'It waits for Apply.'}, {'type': 'done', 'usage': {'in': 1, 'out': 1}}]

    def chat(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=200, content_type='application/x-ndjson', body=''.join(json.dumps(e) + '\n' for e in events))
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1', 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'data_center': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1500, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/api/llm/agent/analysis/chat', chat)
            page.goto(f'{server}/?analysis')
            page.wait_for_selector('#an-sql .cm-content', timeout=20000)
            mine = 'SELECT TOP 5 *\nFROM public.orders'
            type_sql(page, mine)
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            page.fill('#ai-analysis-q', 'fix my sql')
            page.keyboard.press('Enter')
            card = page.locator('#ai-dock .ct-uiw')
            card.wait_for(timeout=10000)
            assert sent[0]['scope']['sql'] == mine and sent[0]['scope']['sql_truncated'] is False
            diff = card.locator('.ct-diff').inner_text()
            assert '- SELECT TOP 5 *' in diff and '+ SELECT *' in diff and '+ LIMIT 5' in diff
            assert card.locator('pre.ct-sql').text_content() == fix          # folded under the diff
            card.locator('.ct-ui [data-ca="ui-apply"]').click()
            page.wait_for_function(f"document.querySelector('#an-sql .cm-content').innerText.trim() === {json.dumps(fix)}", timeout=5000)
            assert card.locator('.ct-okc').count() == 1                # applied: no second Apply
            page.locator('#an-sql .cm-content').click()
            page.keyboard.press('Control+z')
            page.wait_for_function(f"document.querySelector('#an-sql .cm-content').innerText.trim() === {json.dumps(mine)}", timeout=5000)
            # a second proposal, applied in a new tab: the user's tab keeps its text
            events[1] = {**events[1], 'id': 'u2'}
            page.fill('#ai-analysis-q', 'again')
            page.keyboard.press('Enter')
            new = page.locator('#ai-dock .ct-uiw').last
            new.locator('[data-ca="ui-apply"][data-mode="new_tab"]').wait_for(timeout=10000)
            tabs = page.locator('.an-tab').count()
            new.locator('[data-ca="ui-apply"][data-mode="new_tab"]').click()
            page.wait_for_function(f"document.querySelectorAll('.an-tab').length === {tabs + 1}", timeout=5000)
            assert page.locator('#an-sql .cm-content').inner_text().strip() == fix
            # type in that new tab, then go back: each message carries the text of the tab on screen, never an older copy
            type_sql(page, 'SELECT 2')
            events[1] = {**events[1], 'id': 'u3'}
            page.fill('#ai-analysis-q', 'and this one')
            page.keyboard.press('Enter')
            page.wait_for_function('() => document.querySelectorAll("#ai-dock .ct-uiw").length >= 3', timeout=10000)
            assert sent[-1]['scope']['sql'] == 'SELECT 2', sent[-1]['scope']
            assert not errors, errors
            browser.close()
    finally:
        fake.should_exit = True
