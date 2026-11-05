"""End-to-end: Projection › Generate (views/explore-bind.ts, controls.ts updateMapDirty). Changing the method only marks
the map as changed (the dot on the projection trigger, no projection request); choosing the shown one again clears
the dot; Generate (#btn-apply) computes the new map and clears it. Skips like test_page_smoke without a browser."""

import pytest

from test_page_smoke import _browser, _get, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')

# the request behind the map on screen (S.viewReq, set once a projection has finished loading)
_SHOWN = 'window.dataviz.S.viewReq && window.dataviz.S.viewReq.method'


def test_method_change_waits_for_generate(server):  # noqa: F811 - the fixture
    sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
    errors, posts = [], []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page()
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
        page.on('request', lambda r: r.method == 'POST' and r.url.endswith('/api/projection') and posts.append(r))
        page.goto(f'{server}/?session={sid}')
        page.click('#btn-compute')
        page.wait_for_function(f"({_SHOWN}) === 'pca'", timeout=60000)
        dirty = page.locator('#map-dirty')
        assert dirty.is_hidden()

        page.click('#map-btn')
        methods = page.eval_on_selector_all('#method option', 'os => os.map(o => o.value)')
        other = next((m for m in methods if m not in ('pca', 'umap')), None)   # umap pays a ~30 s numba compile
        if other is None:
            pytest.skip(f'no second fast projection method offered: {methods}')
        n = len(posts)
        page.select_option('#method', other)
        assert dirty.is_visible(), 'a changed method should mark the map as changed'
        page.wait_for_timeout(500)
        assert len(posts) == n, 'changing the method alone must not reproject'
        assert page.evaluate(_SHOWN) == 'pca'

        page.select_option('#method', 'pca')                  # back to what is shown: nothing to generate
        assert dirty.is_hidden()
        page.select_option('#method', other)
        assert dirty.is_visible()

        page.click('#btn-apply')                              # Generate
        page.wait_for_function(f"({_SHOWN}) === '{other}'", timeout=60000)
        assert dirty.is_hidden()
        assert len(posts) > n and all(r.post_data_json['method'] == other for r in posts[n:])
        browser.close()
    assert not errors, '\n'.join(errors)
