"""Home (/, views/home.ts): the DataViz overview. One card per page in the header's fixed order (Bucket, Image, Data),
each with its counts and recent items; the header lists the same three pages; the cards open their pages and the
Visualize / Label buttons open the Image page on that tab."""

import pytest

from test_bucket_page import server  # noqa: F401 - the fixture (an admin, a storage root for buckets)
from test_page_smoke import _browser, _send

sync_api = pytest.importorskip('playwright.sync_api')


def test_home_shows_the_three_doors_in_order(server):  # noqa: F811
    bid = _send(f'{server}/api/bucket/buckets', {'name': 'home-bucket', 'kind': 'managed'})['id']
    problems = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: problems.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and problems.append(f'console: {m.text}'))
        page.goto(f'{server}/')
        assert page.evaluate('document.documentElement.dataset.screen') == 'home'
        page.wait_for_selector(f'#door-bucket .door-recent a[href*="bucket={bid}"]', timeout=20000)
        assert [x.strip() for x in page.locator('.door h2').all_inner_texts()] == ['Bucket', 'Image', 'Data']
        assert [x.strip() for x in page.locator('#home .hdr-nav a').all_inner_texts()] == ['Bucket', 'Image', 'Data']
        assert page.locator('#home .hdr-nav a.on').count() == 0          # Home is none of the three
        assert page.locator('#home .brand h1').inner_text() == 'DataViz'
        assert page.title() == 'DataViz'
        assert '1 bucket' in page.inner_text('#door-bucket .door-stats')
        page.wait_for_selector('#door-image:not([aria-busy]) .door-stats', timeout=20000)
        assert '1 visualize session' in page.inner_text('#door-image .door-stats')     # the smoke dataset
        page.wait_for_selector('#door-data:not([aria-busy]) .door-body', timeout=20000)
        assert page.locator('#image, #bucket, #data').evaluate_all('els => els.every((e) => !e.offsetParent)')
        # Visualize opens the Image page on its tab; the brand comes back here
        page.click('#door-image .door-btns a:has-text("Visualize")')
        page.wait_for_url('**/?image=viz*', timeout=10000)
        assert page.evaluate('document.documentElement.dataset.screen') == 'image'
        page.wait_for_selector('#image-tab-viz[aria-selected="true"]', timeout=20000)
        page.keyboard.press('Escape')
        page.click('#image-brand')
        page.wait_for_selector('#door-bucket', timeout=20000)
        page.click('#door-bucket .door-open')
        page.wait_for_url('**/?bucket', timeout=10000)
        page.wait_for_selector('.bk-header', timeout=20000)
        # an old ?analysis link lands on Data
        page.goto(f'{server}/?analysis')
        page.wait_for_url('**/?data', timeout=10000)
        assert page.evaluate('document.documentElement.dataset.screen') == 'data'
        # a phone: one column, no horizontal scroll
        page.set_viewport_size({'width': 390, 'height': 800})
        page.goto(f'{server}/')
        page.wait_for_selector('#door-data', timeout=20000)
        xs = page.locator('.door').evaluate_all('els => els.map((e) => Math.round(e.getBoundingClientRect().left))')
        assert len(set(xs)) == 1
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        browser.close()
    assert not problems, problems
