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
    box = page.locator('#dt-sql .cm-content')
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)

        # the object explorer: server, database, table, columns
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        page.wait_for_selector('.dt-tn:has-text("public.orders")', timeout=10000)
        page.click('.dt-tn:has-text("public.orders")')
        page.wait_for_selector('.dt-tcol:has-text("total")', timeout=10000)

        # double-click: a new tab with a ready query, nothing has run
        page.dblclick('.dt-tn:has-text("public.orders")')
        page.wait_for_selector('.dt-tab.on:has-text("orders")')
        assert page.locator('.dt-sql .cm-content').inner_text().startswith('SELECT')
        assert page.locator('#dt-rc .dt-grid').count() == 0

        page.wait_for_selector('#dt-sql .cm-content')
        type_sql(page, 'SELECT id, total, note FROM public.orders ORDER BY id')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#dt-rc .dt-grid td', timeout=20000)
        assert '500' in page.locator('#dt-sum').inner_text()
        assert page.locator('#dt-rc .dt-grid td.dt-null').first.inner_text() == 'NULL'

        # after a run the server version pill shows: Layout stays with its buttons, the pane headers stay one line
        page.wait_for_selector('.dt-ver')
        lay = page.locator('.dt-lay').bounding_box()
        assert lay['height'] < 40, lay
        for head in page.locator('.dt-sqlhead').all():
            assert head.bounding_box()['height'] <= 36

        # scrolling far loads later rows, no page holds them all
        page.locator('.dt-gridbox').evaluate('e => { e.scrollTop = 28 * 480 }')     # 28 px rows (kit .data-grid, grid.ts ROW)
        page.wait_for_selector('#dt-rc .dt-grid td:text-is("481")', timeout=10000)
        assert page.locator('#dt-rc .dt-grid tbody tr').count() < 120

        # sort and filter stay on the saved result
        page.click('th[data-sort="id"]')
        page.click('th[data-sort="id"]')
        page.wait_for_selector('#dt-rc .dt-grid td:text-is("500")', timeout=10000)
        page.fill('#dt-gfilter', 'even')
        page.wait_for_function("document.querySelector('.dt-gcount').textContent.startsWith('250 of 500')", timeout=10000)
        page.fill('#dt-gfilter', '')

        # Export: pick the format; the file is that format, and the last one used comes first next time
        page.click('#dt-rc [data-act="export"]')
        page.wait_for_selector('#dt-menu [data-m="xlsx"]')
        assert page.locator('#dt-menu [data-m]').count() == 9
        with page.expect_download() as dl:
            page.click('#dt-menu [data-m="csv"]')
        assert dl.value.suggested_filename.endswith('.csv')
        assert Path(dl.value.path()).read_text().splitlines()[0] == 'id,total,note'
        page.click('#dt-rc [data-act="export"]')
        page.wait_for_selector('#dt-menu [data-m]')
        assert page.locator('#dt-menu [data-m]').first.get_attribute('data-m') == 'csv'
        page.keyboard.press('Escape')

        # a change asks first, and Cancel leaves the data alone
        type_sql(page, 'DELETE FROM public.orders WHERE id = 1')
        page.wait_for_selector('#dt-stale:not([hidden])')
        page.click('.dt-toolbar [data-act="run"]')
        page.wait_for_selector('.dt-confirm code:has-text("DELETE")', timeout=10000)
        page.click('.ui-dlg [data-dlg="cancel"]')
        with psycopg.connect(target['url']) as c:
            assert c.execute('SELECT count(*) FROM public.orders').fetchone()[0] == 500

        # confirmed, it runs and reaches the history with its text
        page.click('.dt-toolbar [data-act="run"]')
        page.wait_for_selector('.dt-confirm code')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("!document.querySelector('.dt-toolbar [data-act=\"run\"]').disabled", timeout=10000)
        page.wait_for_timeout(600)
        with psycopg.connect(target['url']) as c:
            assert c.execute('SELECT count(*) FROM public.orders').fetchone()[0] == 499

        # a failed query keeps its message and does not crash the page
        type_sql(page, 'SELECT * FROM public.nothing_here')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('.dt-err', timeout=10000)
        assert 'nothing_here' in page.locator('.dt-err').inner_text()

        # stop
        type_sql(page, 'SELECT pg_sleep(30)')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('[data-act="stop"]:not([disabled])', timeout=10000)
        page.click('[data-act="stop"]')
        page.wait_for_function("!document.querySelector('.dt-toolbar [data-act=\"run\"]').disabled", timeout=10000)

        # cost
        type_sql(page, 'SELECT * FROM public.orders o JOIN public.orders b ON o.id = b.id')
        page.click('[data-act="rt"][data-k="cost"]')
        page.click('[data-act="explain"]')
        page.wait_for_selector('.dt-cost', timeout=10000)

        page.click('.dt-rail [data-k="history"]')
        page.wait_for_selector('.dt-hq:has-text("DELETE FROM public.orders")', timeout=10000)
        page.fill('#dt-hfind', 'nothing_here')
        assert page.locator('#dt-hist tbody tr:not([hidden])').count() == 1

        # tabs come back with the session
        page.reload()
        page.wait_for_selector('.dt-tab:has-text("orders")', timeout=20000)
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.select_option('#dt-conn', target['conn'])
        page.wait_for_function("document.querySelector('#dt-db option:nth-child(2)')", timeout=10000)
        page.select_option('#dt-db', target['db'])
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        page.wait_for_selector('.dt-tn:has-text("public.orders")', timeout=10000)

        def sql():
            return page.locator('#dt-sql .cm-content').inner_text().replace('\n', ' ')

        for name in ('public.orders', 'public.customers'):
            page.drag_and_drop(f'.dt-tn:has-text("{name}")', '#dt-canvas', target_position={'x': 300, 'y': 200})
            page.wait_for_selector(f'.dt-tb:has-text("{name.split(".")[1]}") .dt-col', timeout=10000)
        page.click('.dt-tb[data-t="o"] .dt-col[data-c="id"] .lbl')
        page.click('.dt-tb[data-t="c"] .dt-col[data-c="name"] .lbl')
        page.wait_for_function("document.querySelector('#dt-sql .cm-content').innerText.includes('name')", timeout=10000)
        assert sql().count('SELECT') == 2                         # not joined: one result each

        # nothing ran: editing the builder never reaches the database
        assert page.locator('#dt-rc .dt-grid').count() == 0
        a = page.locator('.dt-col[data-t="o"][data-c="customer_id"]').bounding_box()
        b = page.locator('.dt-col[data-t="c"][data-c="id"]').bounding_box()
        page.mouse.move(a['x'] + 30, a['y'] + 6)
        page.mouse.down()
        page.mouse.move(a['x'] + 60, a['y'] + 26, steps=4)
        page.mouse.move(b['x'] + 30, b['y'] + 6, steps=6)
        page.mouse.up()
        page.wait_for_selector('.dt-jl', timeout=10000)
        page.wait_for_function("document.querySelector('#dt-sql .cm-content').innerText.includes('INNER JOIN')", timeout=10000)
        assert sql().count('SELECT') == 1

        # join popup: LEFT, then Run
        page.click('.dt-jl')
        page.select_option('#dt-j-type', 'LEFT')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#dt-sql .cm-content').innerText.includes('LEFT OUTER JOIN')", timeout=10000)
        page.click('.dt-toolbar [data-act="run"]')
        page.wait_for_selector('#dt-rc .dt-grid td', timeout=20000)
        assert '500' in page.locator('#dt-sum').inner_text()

        # the line's ends: many orders to one customer, from the keys (customers.id is the primary key; the order's
        # customer_id may be NULL, so the customer end is "zero or one")
        assert page.locator('.dt-jl .dt-jc').inner_text() == 'N:1' and 'LEFT' in page.locator('.dt-jl').inner_text()
        assert page.locator('.dt-jl svg.dt-venn').count() == 1     # the join type's Venn icon
        line = page.locator('#dt-lines path.dt-jline')
        assert line.get_attribute('marker-start') == 'url(#dt-mN)' and line.get_attribute('marker-end') == 'url(#dt-m01)'
        page.click('.dt-jl')
        page.click('#dt-j-check')
        page.wait_for_selector('#dt-j-cstat:has-text("The data says N:1")', timeout=20000)
        page.select_option('#dt-j-card', 'N:M')                    # the user's own choice wins
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_selector('.dt-jl.many:has-text("N:M")', timeout=10000)
        page.click('[data-act="b-ends"]')                          # text only: no crow's feet
        assert page.locator('#dt-lines path.dt-jline').get_attribute('marker-start') is None
        page.click('[data-act="b-ends"]')                          # off: the label is the join type again
        assert page.locator('.dt-jl').inner_text().strip() == 'LEFT' and page.locator('.dt-jl .dt-jc').count() == 0
        page.click('[data-act="b-ends"]')

        # cards stacked one above the other: the line is a bracket outside both, the label off the cards
        oh = page.locator('.dt-tb[data-t="o"] .dt-th').bounding_box()
        ch = page.locator('.dt-tb[data-t="c"] .dt-th').bounding_box()
        ob = page.locator('.dt-tb[data-t="o"]').bounding_box()
        page.mouse.move(ch['x'] + 40, ch['y'] + ch['height'] / 2)
        page.mouse.down()
        page.mouse.move(oh['x'] + 40, ob['y'] + ob['height'] + 60 + ch['height'] / 2, steps=8)
        page.mouse.up()
        page.wait_for_timeout(300)
        ob, cb = (page.locator(f'.dt-tb[data-t="{t}"]').bounding_box() for t in 'oc')
        lb = page.locator('#dt-lines path.dt-jline').bounding_box()
        jb = page.locator('.dt-jl').bounding_box()
        assert abs(ob['x'] - cb['x']) < 30, (ob, cb)
        outside = lambda b: b['x'] + b['width'] / 2 > max(ob['x'] + ob['width'], cb['x'] + cb['width']) or b['x'] + b['width'] / 2 < min(ob['x'], cb['x'])  # noqa: E731
        assert outside(lb) and outside(jb), (ob, cb, lb, jb)

        # after an edit the results stay, with a notice
        page.click('.dt-col[data-t="c"][data-c="segment"] .lbl')
        page.wait_for_selector('#dt-stale:not([hidden])', timeout=10000)
        assert page.locator('#dt-rc .dt-grid td').count() > 0

        # CAST and a computed column reach the SQL
        page.click('.dt-ty[data-t="o"][data-c="total"]')
        page.fill('#dt-c-type', 'integer')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#dt-sql .cm-content').innerText.includes('CAST')", timeout=10000)
        page.click('[data-act="b-calc"]')
        page.click('.dt-seg button[data-tab="window"]')
        page.fill('#dt-k-alias', 'rn')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#dt-sql .cm-content').innerText.includes('ROW_NUMBER')", timeout=10000)
        page.click('.dt-toolbar [data-act="run"]')
        page.wait_for_selector('th[data-sort="rn"]', timeout=20000)

        # undo walks back, right-click removes the join
        page.click('#dt-canvas .dt-cvt.tl [data-act="b-undo"]')
        page.wait_for_function("!document.querySelector('#dt-sql .cm-content').innerText.includes('ROW_NUMBER')", timeout=10000)
        page.click('.dt-jl', button='right')
        page.wait_for_function("!document.querySelector('.dt-jl')", timeout=10000)

        # typed SQL: the canvas waits, then follows after a successful run
        box = page.locator('#dt-sql .cm-content')
        box.click()
        page.keyboard.press('Control+a')
        page.keyboard.press('Delete')
        page.keyboard.insert_text("SELECT c.name, CASE WHEN c.id > 5 THEN 'big' ELSE 'small' END AS size FROM public.customers AS c")
        page.wait_for_selector('#dt-canvas .dt-cvt.tl:has-text("Press Run")', timeout=10000)
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#dt-canvas .dt-tb[data-t="c"]', timeout=20000)
        page.wait_for_function("document.querySelectorAll('#dt-canvas .dt-tb').length === 1", timeout=10000)
        page.click('[data-act="b-panel"]')
        page.wait_for_selector('.dt-cvt.bl:has-text("size")', timeout=10000)

        # subqueries are cards with their columns, joined by a line (a SELECT * card used to have no rows to end a line on)
        box.click()
        page.keyboard.press('Control+a')
        page.keyboard.press('Delete')
        page.keyboard.insert_text('SELECT * FROM (SELECT id, name FROM public.customers) AS cu '
                                  'LEFT JOIN (SELECT * FROM public.orders WHERE total > 10) AS od ON od.customer_id = cu.id')
        page.keyboard.press('Control+Enter')
        page.wait_for_function("document.querySelectorAll('#dt-canvas .dt-tb.sub').length === 2", timeout=20000)
        page.wait_for_selector('.dt-tb[data-t="cu"] .dt-col[data-c="name"]')
        page.wait_for_selector('.dt-tb[data-t="od"] .dt-col[data-c="customer_id"]')
        assert page.locator('#dt-lines .dt-jhit').count() == 1 and page.inner_text('.dt-jl') == 'LEFT'
        assert not page.locator('#dt-canvas .dt-cvt.tl:has-text("Locked")').count()

        # a query the builder cannot hold still runs, and says why
        box.click()
        page.keyboard.press('Control+a')
        page.keyboard.press('Delete')
        page.keyboard.insert_text('SELECT id FROM public.customers UNION ALL SELECT id FROM public.orders')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#dt-canvas .dt-cvt.tl:has-text("Locked")', timeout=20000)
        page.wait_for_selector('#dt-rc .dt-grid td', timeout=20000)

        # the builder is on the left, next to the object explorer; it closes for SQL only and comes back from the toolbar
        viz, sqlp = (page.locator(f'[data-pane="{k}"]').bounding_box() for k in ('viz', 'sql'))
        assert viz['x'] < sqlp['x']
        page.click('[data-pane="viz"] [data-act="viz-hide"]')
        page.wait_for_selector('[data-pane="viz"]', state='hidden')
        assert page.locator('[data-pane="sql"]').bounding_box()['width'] > viz['width'] + sqlp['width'] - 10
        page.reload()
        page.wait_for_selector('#dt-sql .cm-content', timeout=20000)
        assert page.locator('[data-pane="viz"]').is_hidden()                         # remembered
        page.click('.dt-vizbtn')
        page.wait_for_selector('[data-pane="viz"] .dt-canvas', state='visible')

        # tabs: the open one is filled; double-click renames, its own duplicate copies it
        page.click('[data-act="tab-new"]')
        page.wait_for_selector('.dt-tabw.on:has-text("query 2")')
        bg = page.eval_on_selector('.dt-tabw.on', 'e => getComputedStyle(e).backgroundColor')
        assert bg not in ('rgba(0, 0, 0, 0)', 'transparent'), bg
        page.dblclick('.dt-tabw.on [data-act="qtab"]')
        page.fill('#ui-dlg-in', 'monthly sales')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_selector('.dt-tabw.on:has-text("monthly sales")')
        page.click('.dt-tabw.on [data-act="tab-dup"]')
        page.wait_for_selector('.dt-tabw.on:has-text("monthly sales copy")')
        browser.close()
    assert not errors, errors


def test_workspaces(server, target):  # noqa: F811
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 400 and '/api/' in r.url else None)
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.select_option('#dt-conn', target['conn'])
        page.wait_for_function("document.querySelector('#dt-db option:nth-child(2)')", timeout=10000)
        page.select_option('#dt-db', target['db'])
        page.wait_for_selector('#dt-sql .cm-content')
        page.locator('#dt-sql .cm-content').click()
        page.keyboard.insert_text("SELECT id, total, CASE WHEN id % 2 = 0 THEN 'even' ELSE 'odd' END AS kind, DATE '2026-01-01' + (id % 5) AS day FROM public.orders")
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#dt-rc .dt-grid td', timeout=20000)

        # Analyze: profile, a suggested test in plain words, quality, pivot
        page.click('.dt-rail [data-k="analyze"]')
        page.wait_for_selector('.dt-card h4:has-text("total")', timeout=20000)
        page.click('[data-act="sheet"][data-k="tests"]')
        page.click('.dt-suggest button >> nth=0', timeout=20000)
        page.wait_for_selector('#dt-t-out .dt-hint', timeout=20000)
        page.click('[data-act="sheet"][data-k="quality"]')
        page.wait_for_selector('.dt-doc td:has-text("No duplicate rows")', timeout=20000)
        page.click('[data-act="sheet"][data-k="pivot"]')
        page.select_option('#dt-p-row', 'kind')
        page.select_option('#dt-p-col', 'day')
        page.click('[data-act="pivot-run"]')
        page.wait_for_selector('#dt-p-out td:has-text("even")', timeout=20000)

        # Dashboards: a draft from the result, then a saved board comes back
        page.click('.dt-rail [data-k="dash"]')
        page.wait_for_selector('.dt-fld:has-text("total")', timeout=20000)
        page.click('[data-act="d-auto"]')
        page.wait_for_selector('.dt-w svg', timeout=20000)
        n = page.locator('.dt-w').count()
        page.wait_for_selector('.dt-w .dt-kpi', timeout=20000)                # Build from this result: KPIs, a donut, a ranking
        page.wait_for_selector('.dt-w .dt-donut', timeout=20000)
        page.wait_for_selector('.dt-w .dt-rank li', timeout=20000)
        page.click('[data-act="d-add"][data-k="text"]')
        page.wait_for_function(f"document.querySelectorAll('.dt-w').length === {n + 1}", timeout=10000)
        # a bar chart turns into a ranking from the side panel, takes a colour and a height; duplicate makes a copy
        page.click('[data-act="d-add"][data-k="bar"]')
        page.locator('.dt-fld:has-text("kind")').drag_to(page.locator('.dt-w.on .dt-wb'))
        page.wait_for_selector('.dt-w.on svg', timeout=20000)
        page.select_option('#dt-d-kind', 'hbar')
        page.wait_for_selector('.dt-w.on .dt-rank li', timeout=20000)
        page.click('#dt-dash [data-act="d-color"][data-k="violet"]')
        page.select_option('#dt-d-size', 'l')
        assert 'span 6' in page.eval_on_selector('.dt-w.on', 'e => e.style.gridRow')
        page.click('.dt-w.on [data-act="d-dup"]')
        page.wait_for_function(f"document.querySelectorAll('.dt-w').length === {n + 3}", timeout=10000)
        assert page.locator('.dt-w.on .lbl').text_content().endswith('(copy)')
        page.wait_for_timeout(900)
        page.click('.dt-rail [data-k="history"]')
        page.click('.dt-rail [data-k="dash"]')
        page.wait_for_function(f"document.querySelectorAll('.dt-w').length === {n + 3}", timeout=10000)
        assert page.locator('.dt-w.k-hbar').count() == 3              # the draft's ranking, the turned bar, its copy

        # Catalog: a description is saved and stays
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        page.click('.dt-rail [data-k="catalog"]')
        page.click('.dt-tn:has-text("public.orders")', timeout=20000)
        page.wait_for_selector('.dt-desc[data-col="note"]', timeout=20000)
        page.fill('.dt-desc[data-col="note"]', 'Sales note')
        page.keyboard.press('Tab')
        page.wait_for_selector('td:has-text("You")', timeout=10000)

        # Scripts: named boxes, confirm for a temp table, the temp table appears with its mark
        page.click('.dt-rail [data-k="scripts"]')
        page.wait_for_selector('#dt-sbody', timeout=20000)
        assert page.locator('#dt-work .dt-toolbar').bounding_box()['height'] < 60   # one row: name, Run all, connection, database
        page.select_option('#dt-sconn', target['conn'])
        page.fill('#dt-sdb', target['db'])
        page.fill('#dt-sname', '3 temp with doc')
        page.fill('#dt-sbody', 'CREATE TEMP TABLE recent AS SELECT * FROM public.orders WHERE id < 5;\nSELECT count(*) FROM recent;')
        page.click('[data-act="script-run"]')
        page.wait_for_selector('.dt-confirm code:has-text("CREATE TEMP")', timeout=10000)
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_selector('.dt-tn.temp:has-text("recent")', timeout=20000)
        page.wait_for_timeout(800)
        page.reload()
        page.wait_for_selector('.dt-main', timeout=20000)
        page.click('.dt-rail [data-k="scripts"]')
        page.wait_for_selector('.dt-cmd:has-text("3 temp with doc")', timeout=20000)
        page.wait_for_selector('.dt-tn.temp:has-text("recent")', timeout=20000)      # a refresh loses nothing
        browser.close()
    assert not errors, errors


def test_catalog_tab_and_enter_move_to_the_next_description(server, target):  # noqa: F811
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        page.click('.dt-rail [data-k="catalog"]')
        page.click('.dt-tn:has-text("public.orders")', timeout=20000)
        page.wait_for_selector('.dt-desc[data-col="id"]', timeout=20000)
        page.click('.dt-desc[data-col="id"]')
        page.keyboard.type('Order number')
        page.keyboard.press('Tab')                        # saves id, the focus stays on the next box after the save
        page.keyboard.type('Amount')
        page.wait_for_function("document.getElementById('dt-catstat').textContent.includes('id')", timeout=10000)
        assert page.evaluate('document.activeElement.dataset.col') == 'total'
        page.keyboard.press('Enter')                      # Enter: next one too
        page.wait_for_function("document.getElementById('dt-catstat').textContent.includes('total')", timeout=10000)
        assert page.evaluate('document.activeElement.dataset.col') == 'note'
        page.keyboard.press('Shift+Enter')
        assert page.evaluate('document.activeElement.dataset.col') == 'total'
        page.keyboard.press('Control+Enter')              # the next empty description
        assert page.evaluate('document.activeElement.dataset.col') == 'note'
        assert page.locator('.dt-desc[data-col="id"]').input_value() == 'Order number'
        page.reload()
        page.wait_for_selector('.dt-main', timeout=20000)
        page.click('.dt-rail [data-k="catalog"]')
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        page.click('.dt-tn:has-text("public.orders")', timeout=20000)
        page.wait_for_selector('.dt-desc[data-col="total"]', timeout=20000)
        assert page.locator('.dt-desc[data-col="total"]').input_value() == 'Amount'
        browser.close()
    assert not errors, errors


def test_results_grid_drag_selects_a_block_and_copies_it(server, target):  # noqa: F811
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        # what the page puts on the clipboard, without a real clipboard
        page.add_init_script("window.__copied = []; navigator.clipboard.writeText = async (t) => { window.__copied.push(t); };")
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.select_option('#dt-conn', target['conn'])
        type_sql(page, "SELECT id, note, total FROM public.orders WHERE id <= 30 ORDER BY id")
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#dt-rc .dt-grid td[data-r="2"][data-c="1"]', timeout=20000)
        a = page.locator('#dt-rc td[data-r="0"][data-c="0"]').bounding_box()
        b = page.locator('#dt-rc td[data-r="2"][data-c="1"]').bounding_box()
        page.mouse.move(a['x'] + 10, a['y'] + 10)
        page.mouse.down()
        page.mouse.move(a['x'] + 30, a['y'] + 12, steps=3)              # inside the first cell: still a text selection
        page.mouse.move(b['x'] + 10, b['y'] + 10, steps=6)
        page.mouse.up()
        assert page.locator('#dt-rc td.sel').count() == 6
        page.keyboard.press('Control+c')
        page.wait_for_function('window.__copied.length === 1', timeout=5000)
        assert page.evaluate('window.__copied[0]') == '1\t\n2\teven\n3\t'
        # the block survives a scroll back and forth, Shift+arrow grows it, Ctrl+Shift+C adds the header row
        page.keyboard.press('Shift+ArrowRight')
        assert page.locator('#dt-rc td.sel').count() == 9
        page.keyboard.press('Control+Shift+c')
        page.wait_for_function('window.__copied.length === 2', timeout=5000)
        assert page.evaluate('window.__copied[1]').split('\n')[0] == 'id\tnote\ttotal'
        # a click is one cell again; a drag inside one cell selects its text, and Ctrl+C then copies that text (the browser's)
        page.click('#dt-rc td[data-r="1"][data-c="1"]')
        assert page.locator('#dt-rc td.sel').count() == 0
        c = page.locator('#dt-rc td[data-r="1"][data-c="1"]').bounding_box()
        page.mouse.move(c['x'] + 2, c['y'] + 14)
        page.mouse.down()
        page.mouse.move(c['x'] + 60, c['y'] + 14, steps=5)
        page.mouse.up()
        assert page.evaluate('getSelection().toString()') == 'even'
        page.keyboard.press('Control+c')
        page.wait_for_timeout(300)
        assert page.evaluate('window.__copied.length') == 2
        browser.close()
    assert not errors, errors


def test_panes_open_and_close_and_the_objects_panel_comes_back(server, target):  # noqa: F811
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.goto(f'{server}/?data')
        page.wait_for_selector('#dt-sql .cm-content', timeout=20000)
        shown = lambda k: page.locator(f'[data-pane="{k}"]').is_visible()          # noqa: E731
        # SQL and Results closed: the builder alone fills the work area; the last open pane cannot close
        page.click('.dt-pbtn[data-k="sql"]')
        page.click('.dt-pbtn[data-k="res"]')
        assert shown('viz') and not shown('sql') and not shown('res')
        assert page.locator('.dt-pbtn[data-k="viz"]').is_disabled()
        work = page.locator('#dt-qbody').bounding_box()
        assert page.locator('[data-pane="viz"]').bounding_box()['height'] > work['height'] - 10
        page.click('.dt-pbtn[data-k="res"]')
        page.click('.dt-pbtn[data-k="sql"]')
        assert shown('viz') and shown('sql') and shown('res')
        page.click('[data-pane="sql"] [data-act="pane-hide"]')
        assert not shown('sql')
        page.click('.dt-pbtn[data-k="sql"]')
        # double-click a pane header fills the window with it (the Objects panel closes); Esc brings both back
        page.dblclick('[data-pane="sql"] .dt-sqlhead')
        assert shown('sql') and not shown('viz') and not shown('res')
        assert page.locator('#dt-explorer').is_hidden()
        page.keyboard.press('Escape')
        assert shown('viz') and shown('res') and page.locator('#dt-explorer').is_visible()
        # the Objects panel: closed from the rail, opened again from the folded strip, and with Ctrl+B
        page.click('.dt-rail-end')
        assert page.locator('#dt-explorer').is_hidden()
        page.click('#dt-expopen')
        assert page.locator('#dt-explorer').is_visible()
        # filled, the rail button still opens it (it used to stay hidden)
        page.click('[data-pane="viz"] [data-act="fill"]')
        assert page.locator('#dt-explorer').is_hidden()
        page.click('.dt-rail-end')
        assert page.locator('#dt-explorer').is_visible()
        page.keyboard.press('Escape')
        page.locator('#dt-work').click(position={'x': 5, 'y': 5})
        page.keyboard.press('Control+b')
        assert page.locator('#dt-explorer').is_hidden()
        page.reload()                                     # remembered
        page.wait_for_selector('#dt-sql .cm-content', timeout=20000)
        assert page.locator('#dt-explorer').is_hidden() and page.locator('#dt-expopen').is_visible()
        page.keyboard.press('Control+b')
        assert page.locator('#dt-explorer').is_visible()
        browser.close()
    assert not errors, errors


def test_board_notes_zones_text_lock_and_auto_zones(server, target):  # noqa: F811
    errors = []
    with psycopg.connect(target['url'], autocommit=True) as c:
        c.execute('CREATE TABLE public.customers (id int PRIMARY KEY, name text)')
        c.execute('CREATE TABLE public.products (id int PRIMARY KEY, title text)')
        c.execute('ALTER TABLE public.orders ADD COLUMN customer_id int')
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1500, 'height': 950})
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.select_option('#dt-conn', target['conn'])
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        page.wait_for_selector('.dt-tn:has-text("public.orders")', timeout=10000)
        for name, x in (('public.orders', 120), ('public.customers', 470)):
            page.drag_and_drop(f'.dt-tn:has-text("{name}")', '#dt-canvas', target_position={'x': x, 'y': 160})
            page.wait_for_selector(f'.dt-tb:has-text("{name.split(".")[1]}") .dt-col', timeout=10000)
        a = page.locator('.dt-col[data-t="o"][data-c="customer_id"]').bounding_box()
        b = page.locator('.dt-col[data-t="c"][data-c="id"]').bounding_box()
        page.mouse.move(a['x'] + 30, a['y'] + 6)
        page.mouse.down()
        page.mouse.move(b['x'] + 30, b['y'] + 6, steps=8)
        page.mouse.up()
        page.wait_for_selector('.dt-jl', timeout=10000)
        sql_before = page.locator('#dt-sql .cm-content').text_content()
        page.click('.dt-pbtn[data-k="sql"]')                                # the builder alone: room for notes
        page.click('.dt-pbtn[data-k="res"]')

        # a zone: right-click an empty spot of the board, written in, its text bottom right, then locked
        cv = page.locator('#dt-canvas').bounding_box()
        page.mouse.click(cv['x'] + 700, cv['y'] + 600, button='right')
        page.click('#dt-menu [data-m="zone"]')
        zone = page.locator('.dt-note.l-back').first
        zone.wait_for()
        assert page.locator('#dt-nbar').is_visible()
        zb = zone.bounding_box()
        page.mouse.dblclick(zb['x'] + zb['width'] - 40, zb['y'] + zb['height'] - 40)
        page.keyboard.press('Control+a')
        page.keyboard.type('Sales area')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('.dt-note.l-back:has-text("Sales area")')
        page.mouse.move(zb['x'] + zb['width'] / 2, zb['y'] + zb['height'] / 2)   # a zone drags from its middle too
        page.mouse.down()
        page.mouse.move(zb['x'] + zb['width'] / 2 + 80, zb['y'] + zb['height'] / 2 + 40, steps=6)
        page.mouse.up()
        assert zone.bounding_box()['x'] > zb['x'] + 40
        zb = zone.bounding_box()
        page.click('#dt-nbar .dt-al[data-ha="right"][data-va="bottom"]')
        st = page.eval_on_selector('.dt-note.l-back .dt-ntext', 'e => [getComputedStyle(e).justifyContent, getComputedStyle(e).textAlign]')
        assert st == ['flex-end', 'right'], st
        page.click('#dt-nbar [data-act="n-lock"]')
        box = zone.bounding_box()
        page.mouse.move(box['x'] + 40, box['y'] + 8)
        page.mouse.down()
        page.mouse.move(box['x'] + 240, box['y'] + 120, steps=6)
        page.mouse.up()
        assert abs(zone.bounding_box()['x'] - box['x']) < 1                 # locked: it stayed
        # a sticky note moves; none of this touched the SQL
        page.click('[data-act="n-add"]')
        page.click('#dt-menu [data-m="sticky"]')
        sticky = page.locator('.dt-note.l-front')
        sb = sticky.bounding_box()
        page.mouse.move(sb['x'] + 30, sb['y'] + 30)
        page.mouse.down()
        page.mouse.move(sb['x'] + 130, sb['y'] + 70, steps=6)
        page.mouse.up()
        assert sticky.bounding_box()['x'] > sb['x'] + 50
        # rotate: Shift+drag the round handle above it to its right (15° steps: 90°), the degrees box, ] (15°), and a
        # double-click on the handle straightens it; a rotated note still resizes along its own sides
        rot = lambda: page.eval_on_selector('.dt-note.l-front', 'e => e.style.transform')
        nb, hb = sticky.bounding_box(), page.locator('.dt-note.l-front .h-rot').bounding_box()
        cx, cy = nb['x'] + nb['width'] / 2, nb['y'] + nb['height'] / 2
        page.mouse.move(hb['x'] + hb['width'] / 2, hb['y'] + hb['height'] / 2)
        page.mouse.down()
        page.keyboard.down('Shift')
        page.mouse.move(cx + 120, cy + 40, steps=4)
        page.mouse.move(cx + 200, cy, steps=6)
        page.keyboard.up('Shift')
        page.mouse.up()
        assert rot() == 'rotate(90deg)', rot()
        page.fill('#dt-nbar [data-nf="rot"]', '30')
        page.locator('#dt-nbar [data-nf="rot"]').dispatch_event('change')
        assert rot() == 'rotate(30deg)', rot()
        w0 = page.eval_on_selector('.dt-note.l-front', 'e => e.offsetWidth')
        eb = page.locator('.dt-note.l-front .h-e').bounding_box()
        page.mouse.move(eb['x'] + 4, eb['y'] + 4)
        page.mouse.down()
        page.mouse.move(eb['x'] + 4 + 52, eb['y'] + 4 + 30, steps=5)        # along the turned side: about 60 px wider
        page.mouse.up()
        w1 = page.eval_on_selector('.dt-note.l-front', 'e => e.offsetWidth')
        assert 45 < w1 - w0 < 75, (w0, w1)
        assert rot() == 'rotate(30deg)'
        page.click('.dt-note.l-front')                                       # its middle: a turned note's corner is off it
        page.keyboard.press(']')
        assert rot() == 'rotate(45deg)', rot()
        hb = page.locator('.dt-note.l-front .h-rot').bounding_box()
        page.mouse.dblclick(hb['x'] + hb['width'] / 2, hb['y'] + hb['height'] / 2)
        assert rot() == '', rot()
        # left and up as far as wanted: past the board's first corner too (like the table cards). Shift+arrows bring
        # each note near the corner, then a mouse drag takes it across
        def to_corner(sel):
            left, top = page.eval_on_selector(sel, 'e => [parseFloat(e.style.left), parseFloat(e.style.top)]')
            for _ in range(int(left // 8)):
                page.keyboard.press('Shift+ArrowLeft')
            for _ in range(int(top // 8)):
                page.keyboard.press('Shift+ArrowUp')
            cv = page.locator('#dt-canvas').bounding_box()                  # pan: the corner out from under the toolbar
            px, py = cv['x'] + cv['width'] - 140, cv['y'] + cv['height'] - 160
            page.mouse.move(px, py)
            page.mouse.down(button='right')
            page.mouse.move(px + 250, py + 200, steps=6)
            page.mouse.up(button='right')
            return page.locator(sel).bounding_box()

        page.click('.dt-note.l-front', position={'x': 30, 'y': 30})
        sb = to_corner('.dt-note.l-front')
        page.mouse.move(sb['x'] + 30, sb['y'] + 30)
        page.mouse.down()
        page.mouse.move(sb['x'] - 60, sb['y'] - 60, steps=8)
        page.mouse.up()
        left, top = page.eval_on_selector('.dt-note.l-front', 'e => [parseFloat(e.style.left), parseFloat(e.style.top)]')
        assert left < 0 and top < 0, (left, top)
        zc = zone.bounding_box()
        page.mouse.click(zc['x'] + 40, zc['y'] + 8)                          # the zone too, once unlocked (Ctrl+L)
        page.keyboard.press('Control+l')
        zb2 = to_corner('.dt-note.l-back')
        gx = zb2['x'] + zb2['width'] - 50                                    # its strip right of the sticky note (also in the corner)
        page.mouse.move(gx, zb2['y'] + 8)
        page.mouse.down()
        page.mouse.move(gx - 100, zb2['y'] - 60, steps=8)
        page.mouse.up()
        zl, zt = page.eval_on_selector('.dt-note.l-back', 'e => [parseFloat(e.style.left), parseFloat(e.style.top)]')
        assert zl < 0 and zt < 0, (zl, zt)
        page.keyboard.press('Control+l')                                    # locked again, as the rest of the test expects
        assert page.locator('#dt-sql .cm-content').text_content() == sql_before
        # Auto zones: orders + customers are joined, one schema: one zone around both; the user's locked zone stays
        page.click('[data-act="n-add"]')
        page.click('#dt-menu [data-m="auto"]')
        page.wait_for_function("document.querySelectorAll('.dt-note.l-back').length === 2", timeout=5000)
        assert page.locator('.dt-note.l-back:has-text("Tables")').count() == 1
        page.wait_for_timeout(1200)                                        # the tab is saved after 800 ms
        page.reload()
        page.wait_for_selector('.dt-note.l-back:has-text("Sales area")', timeout=20000)
        assert page.locator('.dt-note').count() == 3
        page.click('[data-act="b-fit"]')                                    # Fit shows the notes too, wherever they are
        for sel in ('.dt-note.l-front', '.dt-note.l-back'):
            b = page.locator(sel).first.bounding_box()
            cv = page.locator('#dt-canvas').bounding_box()
            assert cv['x'] - 1 <= b['x'] and cv['y'] - 1 <= b['y'], (sel, b, cv)
        # Ctrl+Z on the board undoes the last board change
        page.click('.dt-note.l-front')
        page.keyboard.press('Delete')
        assert page.locator('.dt-note').count() == 2
        page.keyboard.press('Control+z')
        assert page.locator('.dt-note').count() == 3
        # typed SQL that runs redraws the tables from the text: the notes stay
        page.click('.dt-pbtn[data-k="sql"]')
        page.click('.dt-pbtn[data-k="res"]')
        type_sql(page, 'SELECT o.id, c.name FROM public.orders o JOIN public.customers c ON c.id = o.customer_id')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#dt-sum', timeout=20000)
        page.wait_for_function("!document.querySelector('#dt-canvas .dt-cvt.tl')?.textContent.includes('Press Run')", timeout=10000)
        assert page.locator('.dt-note').count() == 3
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        assert page.evaluate('typeof crypto.randomUUID') == 'undefined'
        assert page.locator('.status-line.err').count() == 0
        page.click('[data-act="tab-new"]')
        ids = page.evaluate("[...new Set([...document.querySelectorAll('.dt-tab[data-act=qtab]')].map(b => b.textContent))].length")
        assert ids >= 2
        page.click('.dt-rail [data-k="scripts"]')
        page.wait_for_timeout(500)
        page.click('.dt-rail [data-k="dash"]')
        page.wait_for_timeout(500)
        uid = page.evaluate("import('./static/js/core/uuid.js').then(m => m.uuid())")
        assert re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', uid), uid
        browser.close()
    assert not errors, errors


@pytest.mark.skipif(not os.environ.get('DATAVIZ_SCREENSHOTS'), reason='DATAVIZ_SCREENSHOTS=<dir> saves the pictures')
def test_screenshots_of_every_workspace(server, target):  # noqa: F811
    """Pictures of each workspace in dark and light, wide and narrow, beside the Bucket page, to compare with the UI
    sheet (?ui). Not a check by itself: the other tests check behaviour; this is for looking."""
    shots = os.environ['DATAVIZ_SCREENSHOTS']
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        for theme in ('dark', 'light'):
            page = browser.new_page(viewport={'width': 1500, 'height': 900})
            page.add_init_script(f'localStorage.setItem("theme", "{theme}")')
            page.goto(f'{server}/?bucket')
            page.wait_for_selector('.bk-header', timeout=20000)
            page.wait_for_timeout(600)
            page.screenshot(path=f'{shots}/dc-{theme}.png')
            page.goto(f'{server}/?data')
            page.wait_for_selector('.dt-main', timeout=20000)
            page.select_option('#dt-conn', target['conn'])
            page.wait_for_function("document.querySelector('#dt-db option:nth-child(2)')", timeout=10000)
            page.select_option('#dt-db', target['db'])
            page.wait_for_selector('#dt-sql .cm-content')
            type_sql(page, 'SELECT id, total, note FROM public.orders ORDER BY id')
            page.keyboard.press('Control+Enter')
            page.wait_for_selector('#dt-rc .dt-grid td', timeout=20000)
            page.wait_for_timeout(600)
            page.screenshot(path=f'{shots}/dt-query-{theme}.png')
            for k in ('catalog', 'scripts', 'analyze', 'dash', 'history'):
                page.click(f'.dt-rail [data-k="{k}"]')
                page.wait_for_timeout(900)
                page.screenshot(path=f'{shots}/an-{k}-{theme}.png')
            page.set_viewport_size({'width': 400, 'height': 860})
            page.click('.dt-rail [data-k="query"]')
            page.wait_for_timeout(600)
            page.screenshot(path=f'{shots}/dt-query-{theme}-narrow.png')
            assert page.evaluate('document.documentElement.scrollWidth - innerWidth') <= 0
            page.close()
        browser.close()


def test_data_styles_follow_the_ui_sheet():
    """The page is built from the UI sheet's parts (css/kit.css): data.css only lays it out. No hand-written colours
    or font sizes, and none of the page's old look-alike parts come back."""
    root = Path(__file__).resolve().parents[2]
    css = (root / 'static/css/data.css').read_text(encoding='utf-8')
    body = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(', body), 'colours come from tokens only'
    assert not re.search(r'font-size:\s*\d|font:[^;]*\b\d+(\.\d+)?px', body), 'font sizes come from --fs-* tokens'
    ts = '\n'.join(p.read_text(encoding='utf-8') for p in (root / 'static/ts/data').glob('*.ts'))
    for old in ('dt-btn', 'dt-sel', 'dt-zonebar', 'dt-zones', 'dt-brand', 'dt-chip', 'dt-tag', 'class="pb"', 'dt-search', 'Data Platform'):
        assert old not in ts, f'{old}: use the UI sheet part instead'


def test_data_has_the_same_header_as_the_bucket(server):  # noqa: F811
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        heads = {}
        for screen, ready in (('bucket', '.bk-header'), ('data', '.dt-main')):
            page.goto(f'{server}/?{screen}')
            page.wait_for_selector(ready, timeout=20000)
            h = page.locator('.app-header:visible').first
            heads[screen] = {'brand': h.locator('.brand h1').inner_text(),
                             'links': [a.strip() for a in h.locator('.hdr-nav a').all_inner_texts()],
                             'on': h.locator('.hdr-nav a.on').inner_text().strip(),
                             'actions': sorted(h.locator('.header-actions [data-act]').evaluate_all('a => a.map(x => x.dataset.act)'))}
        browser.close()
    assert heads['bucket']['brand'] == heads['data']['brand'] == 'DataViz'
    assert [x.split('\n')[0] for x in heads['bucket']['links']] == [x.split('\n')[0] for x in heads['data']['links']] \
        == ['Bucket', 'Image', 'Data']                       # one order on every page, whatever the zones' order
    assert heads['bucket']['on'] == 'Bucket' and heads['data']['on'] == 'Data'
    assert {'settings', 'theme'} <= set(heads['data']['actions']) and {'settings', 'theme'} <= set(heads['bucket']['actions'])


def test_the_assistant_dock_sits_beside_the_page(server):  # noqa: F811
    """#data is position:fixed, so the dock's body padding never reached it: the dock covered the page's right side."""
    from test_page_smoke import _post, _put
    from test_llm import fake_app
    from test_models import _serve
    fake, url = _serve(fake_app())
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1', 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.goto(f'{server}/?data')
            page.wait_for_selector('.dt-main', timeout=20000)
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            # the welcome: the mark beside one text column (not each part squeezed into its own column)
            hello = page.locator('#ai-dock .bk-ai-hello > div')
            hello.wait_for(timeout=10000)
            assert hello.bounding_box()['width'] > 250
            dock = page.locator('#ai-dock').bounding_box()
            for sel in ('#data', '.dt-header', '.dt-work'):
                b = page.locator(sel).bounding_box()
                assert b['x'] + b['width'] <= dock['x'] + 1, (sel, b, dock)
            page.click('#ai-dock [data-dock="rail"]')
            page.wait_for_function("document.documentElement.dataset.dock === 'rail'")
            rail = page.locator('#ai-dock').bounding_box()
            b = page.locator('#data').bounding_box()
            assert abs(b['x'] + b['width'] - rail['x']) <= 1
            page.set_viewport_size({'width': 800, 'height': 800})        # below 900 px the dock is a sheet: the page is full width
            assert page.locator('#data').bounding_box()['width'] == 800
            # a suggestion is sent as the user's message and answered
            page.set_viewport_size({'width': 1400, 'height': 900})
            page.click('#ai-dock [data-dock="open"]')
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            page.click('#ai-dock .bk-ai-sugg button >> nth=0')
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
        _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.route('**/api/llm/agent/data/chat', lambda r: r.fulfill(
                status=200, content_type='application/x-ndjson', body=''.join(json.dumps(e) + '\n' for e in events)))
            page.add_init_script("localStorage.setItem('dataviz.dock', JSON.stringify({state: 'open', width: 320}))")
            page.goto(f'{server}/?data')
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=20000)
            page.fill('#ai-data-q', 'show me revenue')
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
        _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.goto(f'{server}/?data')
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('#dt-sql .cm-content', timeout=20000)
        page.select_option('#dt-conn', target['conn'])
        type_sql(page, 'SELECT o.id, b.region, r.name FROM public.orders AS o\n'
                       'JOIN public.buyers AS b ON o.id = b.id\nJOIN public.regions AS r ON b.region = r.id')
        page.keyboard.press('Control+Enter')
        page.wait_for_selector('#dt-rc .dt-grid td', timeout=20000)
        page.click('#dt-rtabs [data-k="joins"]')
        page.wait_for_selector('#dt-rc [data-act="join-count-all"]', timeout=10000)
        assert page.locator('#dt-rc .dt-steps button.primary').inner_text() == 'All tables'
        page.click('#dt-rc [data-act="join-count-all"]')
        svg = page.locator('#dt-rc svg.dt-venn-all')
        svg.wait_for(timeout=20000)
        text = page.locator('#dt-rc').inner_text()
        # orders 1..500 x buyers 1..40 -> 40 rows; regions 0 and 1 match (2 of 3 keys), so 27 buyers rows remain
        assert '27\nrows the query keeps' in text or '27 rows the query keeps' in text.replace('\n', ' '), text
        assert svg.locator('.dt-vj').count() == 2 and svg.locator('.dt-vc').count() == 3 and svg.locator('.dt-vc.cut').count() == 3
        assert page.locator('#dt-rc table tr:has-text("regions")').inner_text().split() == ['regions', '3', '3', '2', '67%']
        box, rc = svg.bounding_box(), page.locator('#dt-rc').bounding_box()
        assert box['x'] + box['width'] <= rc['x'] + rc['width'] + 1
        svg.locator('.dt-vj').nth(1).click()
        page.wait_for_selector('#dt-rc svg.dt-venn:not(.dt-venn-all)', timeout=10000)   # counted already: no second ask
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        fav = '.dt-fsec .dt-tn[data-act="table"]:has-text("public.orders")'
        try:
            page.wait_for_selector('.dt-fsec .dt-fnote', timeout=10000)          # empty Favorites says how to fill it
            page.click('.dt-tn:has-text("sales")')
            page.click(f'.dt-tn:has-text("{target["db"]}")')
            page.wait_for_selector('.dt-tn:has-text("public.orders")', timeout=10000)
            tree_star = '.dt-trow:has(.dt-tn[data-pre="t"][data-name="orders"]) .dt-star'
            page.hover(f'.dt-tn[data-pre="t"][data-name="orders"]')
            page.click(tree_star)
            page.wait_for_selector(fav)
            assert page.locator(tree_star).get_attribute('aria-pressed') == 'true'

            # kept in the user's prefs: back after a reload
            page.wait_for_timeout(900)
            page.reload()
            page.wait_for_selector(fav, timeout=20000)

            # a category from the right-click menu, the table goes into it
            page.click(fav, button='right')
            page.click('#dt-menu [data-m="new"]')
            page.fill('#ui-dlg-in', 'Sales')
            page.click('.ui-dlg [data-dlg="ok"]')
            page.wait_for_selector('.dt-fhead[data-g="Sales"]:has-text("1")')
            assert page.locator('.dt-fhead[data-g="Sales"] + .dt-trow .dt-tn:has-text("public.orders")').count() == 1

            # its columns open on their own (not in the tree), a double click queries it
            page.click(fav)
            page.wait_for_selector('.dt-fsec .dt-tcol:has-text("total")', timeout=10000)
            page.dblclick(fav)
            page.wait_for_selector('.dt-tab.on:has-text("orders")')

            # deleting the category keeps the table in Favorites, the star removes it
            page.hover('.dt-fhead[data-g="Sales"]')
            page.click('.dt-fhead[data-g="Sales"] [data-act="fav-gmenu"]')
            page.click('#dt-menu [data-m="delete"]')
            page.wait_for_selector('.dt-fhead[data-g="Sales"]', state='detached')
            page.wait_for_selector(fav)
            page.click('.dt-fsec .dt-trow:has(.dt-tn[data-act="table"][data-name="orders"]) .dt-star')
            page.wait_for_selector(fav, state='detached')
            page.wait_for_timeout(900)
            page.reload()
            page.wait_for_selector('.dt-fsec .dt-fnote', timeout=20000)
        finally:   # the other tests share this user: leave no favorites behind
            page.evaluate("fetch('api/me/prefs', {method: 'PUT', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'dataviz'}, body: JSON.stringify({prefs: {data_favorites: null}})})")
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        row = '.dt-tn[data-pre="t"][data-name="orders"]'
        try:
            page.click('.dt-tn:has-text("sales")')
            page.click(f'.dt-tn:has-text("{target["db"]}")')
            page.wait_for_selector(row, timeout=10000)
            page.click(row, button='right')
            page.click('#dt-menu [data-m="ai-access"][data-g="rows"]')
            page.wait_for_selector(f'{row} .dt-aiacc.rows')
            got = page.evaluate("fetch('api/data/ai-access').then((r) => r.json())")
            assert [(x['t'], x['level']) for x in got['items']] == [('orders', 'rows')]
            page.click(row, button='right')                       # structure asks first; Cancel keeps Edit rows
            page.click('#dt-menu [data-m="ai-access"][data-g="schema"]')
            page.click('.ui-dlg [data-dlg="cancel"]')
            assert page.locator(f'{row} .dt-aiacc.rows').count() == 1
            page.click(row, button='right')
            page.click('#dt-menu [data-m="ai-access"][data-g="read"]')
            page.wait_for_selector(f'{row} .dt-aiacc', state='detached')
        finally:
            page.evaluate("fetch('api/me/prefs', {method: 'PUT', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'dataviz'}, body: JSON.stringify({prefs: {data_ai_access: null}})})")
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.evaluate("localStorage.removeItem('dataviz.data.layout')")
        page.reload()
        page.wait_for_selector('.dt-main', timeout=20000)
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        row = page.locator(f'.dt-tn[data-name="{long}"]')
        row.wait_for(timeout=10000)
        assert f'public.{long}' in row.get_attribute('title')                      # the full name on hover
        lbl = row.locator('.lbl')
        assert lbl.evaluate('l => l.scrollWidth > l.clientWidth')                    # cut at the default width
        w0 = page.locator('#dt-explorer').bounding_box()['width']

        h = page.locator('.dt-expsz').bounding_box()
        page.mouse.move(h['x'] + 3, h['y'] + 300)
        page.mouse.down()
        page.mouse.move(h['x'] + 103, h['y'] + 300, steps=5)
        page.mouse.up()
        w1 = page.locator('#dt-explorer').bounding_box()['width']
        assert abs(w1 - (w0 + 100)) <= 2, (w0, w1)
        work = page.locator('.dt-work').bounding_box()
        assert work['x'] >= page.locator('#dt-explorer').bounding_box()['x'] + w1 - 1  # the work area moved over

        page.reload()                                                                 # remembered
        page.wait_for_selector('.dt-main .dt-expsz', timeout=20000)
        assert abs(page.locator('#dt-explorer').bounding_box()['width'] - w1) <= 2

        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        row.wait_for(timeout=10000)
        page.dblclick('.dt-expsz')
        page.wait_for_timeout(100)
        assert lbl.evaluate('l => l.scrollWidth <= l.clientWidth')                   # fits: the whole name shows
        page.focus('.dt-expsz')
        page.keyboard.press('Home')                                                   # back to the default
        assert abs(page.locator('#dt-explorer').bounding_box()['width'] - w0) <= 2
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
        page.goto(f'{server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.select_option('#dt-conn', target['conn'])
        page.wait_for_function("document.querySelector('#dt-db option:nth-child(2)')", timeout=10000)
        page.select_option('#dt-db', target['db'])
        page.click('.dt-tn:has-text("sales")')
        page.click(f'.dt-tn:has-text("{target["db"]}")')
        page.wait_for_selector(f'.dt-tn[data-name="{long}"]', timeout=10000)
        for name in ('orders', long):
            page.drag_and_drop(f'.dt-tn[data-pre="t"][data-name="{name}"]', '#dt-canvas', target_position={'x': 200, 'y': 160})
            page.wait_for_selector(f'.dt-tb:has(.dt-th .lbl:text-is("{name}")) .dt-col', timeout=10000)
        card = page.locator(f'.dt-tb:has(.dt-th .lbl:text-is("{long}"))')
        lbl = card.locator('.dt-th .lbl')
        assert lbl.evaluate('l => l.scrollWidth <= l.clientWidth')                 # the full name shows
        boxes = page.eval_on_selector_all('#dt-board .dt-tb', 'els => els.map(e => [e.offsetLeft, e.offsetTop, e.offsetWidth, e.offsetHeight])')
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
        sql_before = page.locator('#dt-sql .cm-content').inner_text()
        card.locator('.dt-th').click(button='right')
        page.wait_for_selector('#dt-menu:not(.hidden) [data-m="rename"]')
        if shots:
            page.screenshot(path=f'{shots}/builder-menu.png')
        page.click('#dt-menu [data-m="rename"]')
        page.fill('#ui-dlg-in', 'CLV')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_selector('.dt-tb .dt-th .lbl:text-is("CLV")')
        assert page.locator('#dt-sql .cm-content').inner_text() == sql_before          # the SQL still names the table
        assert long in sql_before
        page.locator('.dt-tb:has(.dt-th .lbl:text-is("CLV")) .dt-th').click(button='right')
        page.click('#dt-menu [data-m="slim"]')
        page.wait_for_selector('.dt-tb.slim .dt-th .lbl:text-is("CLV")')
        page.wait_for_timeout(1000)                                                    # the tab is saved
        page.reload()
        page.wait_for_selector('.dt-tb.slim .dt-th .lbl:text-is("CLV")', timeout=20000)   # kept

        # Change type: chips by group, the themed combobox, the SQL it makes
        clv = page.locator('.dt-tb:has(.dt-th .lbl:text-is("CLV"))')
        clv.locator('.dt-ty[data-c="amount_total"]').click()
        page.wait_for_selector('.ui-dlg .dt-cast')
        assert page.locator('.ui-dlg datalist').count() == 0
        assert page.locator('.ui-dlg .dt-cast-now code').first.inner_text() == 'numeric(10,2)'
        page.click('.ui-dlg .chip[data-ty="integer"]')
        assert page.locator('.ui-dlg .chip.on').inner_text() == 'integer'
        assert 'AS integer' in page.locator('#dt-c-prev').inner_text()
        page.click('.ui-dlg .cbx-btn')
        page.wait_for_selector('#cbx-list-dt-cast', state='visible', timeout=5000)
        if shots:
            page.screenshot(path=f'{shots}/builder-cast.png')
        page.keyboard.press('Escape')
        page.click('.ui-dlg [data-dlg="ok"]')
        page.wait_for_function("document.querySelector('#dt-sql .cm-content').innerText.includes('AS integer')", timeout=10000)
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
        _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1500, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/api/llm/agent/data/chat', chat)
            page.goto(f'{server}/?data')
            page.wait_for_selector('#dt-sql .cm-content', timeout=20000)
            mine = 'SELECT TOP 5 *\nFROM public.orders'
            type_sql(page, mine)
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            page.fill('#ai-data-q', 'fix my sql')
            page.keyboard.press('Enter')
            card = page.locator('#ai-dock .ct-uiw')
            card.wait_for(timeout=10000)
            assert sent[0]['scope']['sql'] == mine and sent[0]['scope']['sql_truncated'] is False
            diff = card.locator('.ct-diff').inner_text()
            assert '- SELECT TOP 5 *' in diff and '+ SELECT *' in diff and '+ LIMIT 5' in diff
            assert card.locator('pre.ct-sql').text_content() == fix          # folded under the diff
            card.locator('.ct-ui [data-ca="ui-apply"]').click()
            page.wait_for_function(f"document.querySelector('#dt-sql .cm-content').innerText.trim() === {json.dumps(fix)}", timeout=5000)
            assert card.locator('.ct-okc').count() == 1                # applied: no second Apply
            page.locator('#dt-sql .cm-content').click()
            page.keyboard.press('Control+z')
            page.wait_for_function(f"document.querySelector('#dt-sql .cm-content').innerText.trim() === {json.dumps(mine)}", timeout=5000)
            # a second proposal, applied in a new tab: the user's tab keeps its text
            events[1] = {**events[1], 'id': 'u2'}
            page.fill('#ai-data-q', 'again')
            page.keyboard.press('Enter')
            new = page.locator('#ai-dock .ct-uiw').last
            new.locator('[data-ca="ui-apply"][data-mode="new_tab"]').wait_for(timeout=10000)
            tabs = page.locator('.dt-tab').count()
            new.locator('[data-ca="ui-apply"][data-mode="new_tab"]').click()
            page.wait_for_function(f"document.querySelectorAll('.dt-tab').length === {tabs + 1}", timeout=5000)
            assert page.locator('#dt-sql .cm-content').inner_text().strip() == fix
            # type in that new tab, then go back: each message carries the text of the tab on screen, never an older copy
            type_sql(page, 'SELECT 2')
            events[1] = {**events[1], 'id': 'u3'}
            page.fill('#ai-data-q', 'and this one')
            page.keyboard.press('Enter')
            page.wait_for_function('() => document.querySelectorAll("#ai-dock .ct-uiw").length >= 3', timeout=10000)
            assert sent[-1]['scope']['sql'] == 'SELECT 2', sent[-1]['scope']
            assert not errors, errors
            browser.close()
    finally:
        fake.should_exit = True


def test_the_assistant_writes_and_runs_sql_in_the_editor(server, target):  # noqa: F811
    """run_in_editor (a run action, applied): the page writes the SQL in the editor, runs it, the rows show in the
    Results tab, and the page reports rows, columns and result_id (never the rows); a failed run reports the error."""
    from test_page_smoke import _post, _put
    from test_llm import fake_app
    from test_models import _serve
    fake, url = _serve(fake_app())
    sql = 'SELECT id, total FROM public.orders ORDER BY total DESC LIMIT 7'
    events = [{'type': 'meta', 'run_id': 'r1', 'model': 'm', 'provider': 'p', 'mode': 'auto'},
              {'type': 'ui_action', 'id': 'u1', 'action': 'run_in_editor', 'label': 'Seven biggest orders',
               'args': {'sql': sql, 'mode': 'replace', 'connection': target['conn'], 'database': target['db']}, 'applied': True, 'wait': True},
              {'type': 'text', 'text': 'Running it.'}, {'type': 'done', 'usage': {'in': 1, 'out': 1}}]
    acks = []

    def chat(route):
        route.fulfill(status=200, content_type='application/x-ndjson', body=''.join(json.dumps(e) + '\n' for e in events))

    def ack(route):
        acks.append(route.request.post_data_json)
        route.fulfill(status=200, content_type='application/json', body='{"ok": true}')
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1', 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1500, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/api/llm/agent/data/chat', chat)
            page.route('**/api/llm/agent/ui-result', ack)
            page.goto(f'{server}/?data')
            page.wait_for_selector('#dt-sql .cm-content', timeout=20000)
            page.select_option('#dt-conn', target['conn'])
            page.wait_for_function("document.querySelector('#dt-db option:nth-child(2)')", timeout=10000)
            page.select_option('#dt-db', target['db'])
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            page.fill('#ai-data-q', 'show me the biggest orders')
            page.keyboard.press('Enter')
            page.wait_for_function('() => new Set([...document.querySelectorAll("#dt-rc .dt-grid td[data-r]")].map((td) => td.dataset.r)).size === 7', timeout=20000)
            assert page.locator('#dt-sql .cm-content').inner_text().strip() == sql
            for _ in range(50):
                if acks:
                    break
                page.wait_for_timeout(100)
            assert len(acks) == 1 and acks[0]['ok'] and acks[0]['action_id'] == 'u1', acks
            r = acks[0]['result']
            assert r['rows'] == 7 and r['columns'] == ['id', 'total'] and r['result_id']
            # a query the database rejects: the error goes back to the model, the Messages tab shows it
            events[1] = {**events[1], 'id': 'u2', 'args': {**events[1]['args'], 'sql': 'SELECT nope FROM public.orders'}}
            page.fill('#ai-data-q', 'again')
            page.keyboard.press('Enter')
            for _ in range(100):
                if len(acks) == 2:
                    break
                page.wait_for_timeout(200)
            assert len(acks) == 2 and not acks[1]['ok'] and 'the query failed' in acks[1]['detail'], acks
            assert not errors, errors
            browser.close()
    finally:
        fake.should_exit = True


def test_the_assistant_groups_and_describes_the_board_without_coordinates(server, target):  # noqa: F811
    """edit_board with describe + zone ops and arrange: the page measures the cards, pins each description above its
    table, lays the tables out zone by zone (no overlap) and draws each zone around its tables; dragging a table
    takes its description along. The SQL does not change."""
    from test_page_smoke import _post, _put
    from test_llm import fake_app
    from test_models import _serve
    with psycopg.connect(target['url'], autocommit=True) as c:
        c.execute('CREATE TABLE public.customers (id int PRIMARY KEY, name text)')
        c.execute('CREATE TABLE public.products (id int PRIMARY KEY, title text)')
    fake, url = _serve(fake_app())
    ops = [{'op': 'describe', 'table': 'o', 'text': 'One row per order: who bought, when and the total.'},
           {'op': 'describe', 'table': 'p', 'text': 'The catalogue.'},
           {'op': 'zone', 'label': 'Sales', 'tables': ['o', 'c']},
           {'op': 'zone', 'label': 'Products', 'tables': ['p']}]
    events = [{'type': 'meta', 'run_id': 'r1', 'model': 'm', 'provider': 'p', 'mode': 'ask'},
              {'type': 'ui_action', 'id': 'u1', 'action': 'edit_board', 'label': 'Group and describe',
               'args': {'ops': ops, 'arrange': True, 'note': 'Group and describe'}, 'applied': False},
              {'type': 'done', 'usage': {'in': 1, 'out': 1}}]
    errors = []
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1', 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1500, 'height': 950})
            page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
            page.route('**/api/llm/agent/data/chat', lambda r: r.fulfill(
                status=200, content_type='application/x-ndjson', body=''.join(json.dumps(e) + '\n' for e in events)))
            page.goto(f'{server}/?data')
            page.wait_for_selector('.dt-main', timeout=20000)
            page.select_option('#dt-conn', target['conn'])
            page.click('.dt-tn:has-text("sales")')
            page.click(f'.dt-tn:has-text("{target["db"]}")')
            page.wait_for_selector('.dt-tn:has-text("public.orders")', timeout=10000)
            sql_before = page.locator('#dt-sql .cm-content').text_content()
            page.click('.dt-pbtn[data-k="sql"]')                                # the builder alone: room for the tables
            page.click('.dt-pbtn[data-k="res"]')
            for name, x in (('public.orders', 60), ('public.products', 250), ('public.customers', 440)):   # mixed up
                page.drag_and_drop(f'.dt-tn:has-text("{name}")', '#dt-canvas', target_position={'x': x, 'y': 160})
                page.wait_for_selector(f'.dt-tb:has-text("{name.split(".")[1]}") .dt-col', timeout=10000)
            sql_before = page.locator('#dt-sql .cm-content').text_content()
            page.wait_for_function("document.documentElement.dataset.dock === 'open'", timeout=10000)
            page.fill('#ai-data-q', 'put the description of each table and group them in zones')
            page.keyboard.press('Enter')
            page.locator('#ai-dock [data-ca="ui-apply"]').first.click(timeout=10000)
            page.wait_for_selector('.dt-note.l-back:has-text("Products")', timeout=10000)
            rect = lambda sel: page.locator(sel).bounding_box()                           # noqa: E731
            inside = lambda a, z: z['x'] <= a['x'] and z['y'] <= a['y'] and a['x'] + a['width'] <= z['x'] + z['width'] and a['y'] + a['height'] <= z['y'] + z['height']   # noqa: E731
            sales, prods = rect('.dt-note.l-back:has-text("Sales")'), rect('.dt-note.l-back:has-text("Products")')
            o, c, pr = rect('.dt-tb[data-t="o"]'), rect('.dt-tb[data-t="c"]'), rect('.dt-tb[data-t="p"]')
            desc = rect('.dt-note.l-front:has-text("One row per order")')
            assert inside(o, sales) and inside(c, sales) and inside(desc, sales) and inside(pr, prods)
            assert sales['x'] + sales['width'] <= prods['x'] or prods['x'] + prods['width'] <= sales['x']    # side by side
            assert desc['y'] + desc['height'] <= o['y'] and abs(desc['x'] - o['x']) < 1                      # above its card
            # the description goes along when its table is dragged
            h = page.locator('.dt-tb[data-t="o"] .dt-th').bounding_box()
            page.mouse.move(h['x'] + 20, h['y'] + h['height'] / 2)
            page.mouse.down()
            page.mouse.move(h['x'] + 20, h['y'] + h['height'] / 2 + 90, steps=6)
            page.mouse.up()
            d2 = rect('.dt-note.l-front:has-text("One row per order")')
            assert abs(d2['y'] - desc['y'] - 90) < 12, (desc, d2)
            assert page.locator('#dt-sql .cm-content').text_content() == sql_before
            browser.close()
    finally:
        fake.should_exit = True
    assert not errors, errors
