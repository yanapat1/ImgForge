"""The Analysis page in a real browser, against a real PostgreSQL."""

from urllib.parse import urlsplit

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

        # a query the builder cannot hold still runs, and says why
        box.click()
        page.keyboard.press('Control+a')
        page.keyboard.press('Delete')
        page.keyboard.insert_text('SELECT id FROM public.customers UNION ALL SELECT id FROM public.orders')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#an-canvas .an-cvt.tl:has-text("Locked")', timeout=20000)
        page.wait_for_selector('#an-rc .an-grid td', timeout=20000)
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
