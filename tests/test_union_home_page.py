"""End-to-end: Compare / Combine on the Sessions home and the Combined form of Settings (views/home.ts, settings/forms.ts).
Two local sessions are picked (the pick box and Ctrl + click), Compare creates a combined dataset and opens s/<id>/#compare;
picking the same two again reuses it; combined and embeddings cards cannot be picked; a member in use cannot be removed
(the server's message is shown); the Settings form builds a 3-member combined dataset in a chosen order and edits it.

DATAVIZ_SCREENSHOTS=<dir> saves a screenshot of every step there (see docs/screenshots/2026-10-27-compare/NOTES.md)."""

import os
import re
import shutil

import pytest

from test_page_smoke import _browser, _get, _make_embeddings, _send, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')


def _unions():
    return [d for d in _get_datasets() if d['kind'] == 'union']


def _get_datasets():
    return _get(_get_datasets.base + '/api/datasets')['datasets']


def _card(page, name):
    return page.locator(f'#home-panel-viz .session-card:has(.sc-open:text-is("{name}"))')


def test_compare_combine_from_the_home_and_settings(server, tmp_path):  # noqa: F811
    _get_datasets.base = server
    for name in ('survey-a', 'survey-b', 'survey-c'):
        shutil.copytree(tmp_path / 'ds', tmp_path / name)
        _send(f'{server}/api/datasets', {'kind': 'local', 'path': str(tmp_path / name), 'name': name.replace('-', ' ').title()})
    _make_embeddings(tmp_path / 'speech')
    _send(f'{server}/api/datasets', {'kind': 'embeddings', 'path': str(tmp_path / 'speech'), 'name': 'Speech vectors'})
    by = {d['name']: d['id'] for d in _get_datasets()}
    a, b, c = by['Survey A'], by['Survey B'], by['Survey C']

    shots = os.environ.get('DATAVIZ_SCREENSHOTS')
    shot = (lambda page, name, el=None: (page.locator(el) if el else page).screenshot(path=f'{shots}/{name}.png')) \
        if shots else (lambda *a, **k: None)
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1280, 'height': 860})
        on_home = lambda: '/s/' not in page.url      # noqa: E731 - the visualizer is another agent's screen
        page.on('pageerror', lambda e: on_home() and errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and on_home() and errors.append(f'console: {m.text}'))
        page.on('dialog', lambda d: d.accept())       # the Remove confirm
        page.goto(server)
        _card(page, 'Survey A').wait_for(timeout=20000)
        page.evaluate("document.documentElement.dataset.theme = 'light'")
        page.wait_for_timeout(400)              # let the colour transitions finish
        shot(page, '50-home-cards-light')

        # an embeddings card cannot be picked; its pick box says why
        emb = _card(page, 'Speech vectors')
        assert emb.locator('[data-act="pick"]').is_disabled()
        assert 'embeddings' in emb.locator('.sc-pick').get_attribute('title')
        _card(page, 'Survey A').hover()
        shot(page, '51-home-pick-on-hover', '#home-panel-viz')

        # pick A with its box, B with Ctrl + click: the bar offers Compare (2) and Combine (2)
        _card(page, 'Survey A').locator('.sc-pick').click()
        _card(page, 'Survey B').locator('.sc-body').click(modifiers=['Control'])
        bar = page.locator('#home-selbar')
        bar.wait_for()
        assert '2 selected' in bar.inner_text()
        assert bar.locator('[data-act="sel-compare"]').is_enabled()
        assert bar.locator('[data-act="sel-combine"]').inner_text().strip() == 'Combine (2)'
        assert page.url.rstrip('/') == server                 # Ctrl + click did not open the card
        shot(page, '52-home-two-picked-light')
        page.evaluate("document.documentElement.dataset.theme = 'dark'")
        page.wait_for_timeout(400)              # let the colour transitions finish
        shot(page, '53-home-two-picked-dark')

        # a third pick: Compare needs exactly 2
        _card(page, 'Survey C').locator('[data-act="pick"]').check()
        assert bar.locator('[data-act="sel-compare"]').is_disabled()
        assert bar.locator('[data-act="sel-combine"]').inner_text().strip() == 'Combine (3)'
        shot(page, '54-home-three-picked-dark', '#home-selbar')
        _card(page, 'Survey C').locator('[data-act="pick"]').uncheck()

        # Compare: the combined dataset is made and its Compare tab opens
        bar.locator('[data-act="sel-compare"]').click()
        page.wait_for_url(re.compile(r'/s/[0-9a-f]+/#compare$'), timeout=20000)
        uid = re.search(r'/s/([0-9a-f]+)/#compare$', page.url).group(1)
        unions = _unions()
        assert [u['id'] for u in unions] == [uid]
        assert [(x['id'], x['alias']) for x in unions[0]['parts']] == [(a, 'A'), (b, 'B')]
        assert unions[0]['name'] == 'Survey A vs Survey B' and unions[0]['uri'] == f'union://{uid}'
        page.wait_for_timeout(1500)
        shot(page, '55-home-compare-opened')

        # back home: the combined card (layers badge, member chips) cannot be picked; the same two again reuse it
        page.goto(server)
        uc = _card(page, 'Survey A vs Survey B')
        uc.wait_for(timeout=20000)
        assert uc.locator('[data-act="pick"]').is_disabled()
        assert 'combined' in uc.locator('.sc-pick').get_attribute('title').lower()
        assert uc.locator('.sc-src').inner_text().strip().lower() == 'combined'
        assert [' '.join(t.split()) for t in uc.locator('.sc-part').all_inner_texts()] == ['A Survey A', 'B Survey B']
        uc.hover()
        shot(page, '56-home-combined-card-light', '#home-panel-viz')
        _card(page, 'Survey A').locator('[data-act="pick"]').check()
        _card(page, 'Survey B').locator('[data-act="pick"]').check()
        bar.locator('[data-act="sel-compare"]').click()
        page.wait_for_url(re.compile(r'/s/[0-9a-f]+/#compare$'), timeout=20000)
        assert f'/s/{uid}/' in page.url and len(_unions()) == 1

        # removing a member that a combined dataset uses: the server's 400 shows on the home
        page.goto(server)
        _card(page, 'Survey A').wait_for(timeout=20000)
        _card(page, 'Survey A').locator('[data-act="remove"]').click()
        status = page.locator('#home-status .status-line.err')
        status.wait_for(timeout=10000)
        assert 'used by the combined dataset' in status.inner_text() and 'Survey A vs Survey B' in status.inner_text()
        assert _card(page, 'Survey A').count() == 1
        shot(page, '57-home-remove-member-refused')

        # Settings › New session › Combined datasets: 3 members, a bad alias is refused in place, C moved first
        page.click('#home-new')
        page.click('[data-act="new-union"]')
        for sid in (a, b, c):
            page.select_option('#settings-body select[data-f="pick"]', sid)
            page.click('#settings-body [data-act="un-add"]')
        rows = page.locator('#settings-body .un-row')
        assert rows.count() == 3
        assert page.locator('#settings-body select[data-f="pick"] option', has_text='Speech vectors').count() == 0
        assert page.locator('#settings-body select[data-f="pick"] option', has_text='vs').count() == 0
        rows.nth(2).locator('.un-alias').fill('bad alias')
        assert 'letters, digits' in rows.nth(2).locator('.un-err').inner_text()
        assert page.locator('#settings-body [data-act="save-union"]').first.is_disabled()
        shot(page, '58-settings-union-bad-alias-dark', '.settings-box')
        rows.nth(2).locator('.un-alias').fill('Cnew')
        assert page.locator('#settings-body [data-act="save-union"]').first.is_enabled()
        rows.nth(2).locator('[data-act="un-up"]').click()
        rows.nth(1).locator('[data-act="un-up"]').click()       # the focus followed the row: C is now first
        assert rows.nth(0).locator('.un-alias').input_value() == 'Cnew'
        page.fill('#settings-body input[data-f="name"]', 'Three surveys')
        page.evaluate("document.documentElement.dataset.theme = 'light'")
        page.wait_for_timeout(400)              # let the colour transitions finish
        shot(page, '59-settings-union-three-light', '.settings-box')
        page.locator('#settings-body button:not(.primary)[data-act="save-union"]').click()
        page.wait_for_selector('#settings', state='hidden', timeout=10000)
        three = next(u for u in _unions() if u['name'] == 'Three surveys')
        assert [(x['id'], x['alias']) for x in three['parts']] == [(c, 'Cnew'), (a, 'A'), (b, 'B')]
        _card(page, 'Three surveys').wait_for(timeout=10000)
        shot(page, '60-home-three-member-card-light', '#home-panel-viz')

        # editing it: the form starts from its members, in order
        _card(page, 'Three surveys').locator('[data-act="tags"]').click()
        page.click('#home-tagedit [data-te="full"]')
        page.wait_for_selector('#settings-body .un-row')
        assert page.locator('#settings-body .un-alias').evaluate_all('els => els.map(e => e.value)') == ['Cnew', 'A', 'B']
        page.click('#settings-body [data-act="back"]')

        # a phone-width home: the bar wraps under the cards, in both themes
        page.set_viewport_size({'width': 400, 'height': 820})
        _card(page, 'Survey B').locator('[data-act="pick"]').check()
        _card(page, 'Survey C').locator('[data-act="pick"]').check()
        bar.scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        shot(page, '61-home-narrow-picked-light')
        page.evaluate("document.documentElement.dataset.theme = 'dark'")
        page.wait_for_timeout(400)              # let the colour transitions finish
        shot(page, '62-home-narrow-picked-dark')
        page.keyboard.press('Escape')
        assert bar.is_hidden()
        page.click('#home-new')
        page.click('[data-act="new-union"]')
        for sid in (a, b):
            page.select_option('#settings-body select[data-f="pick"]', sid)
            page.click('#settings-body [data-act="un-add"]')
        shot(page, '63-settings-union-narrow-dark', '.settings-box')
        browser.close()
    # the refused Remove is a 400 on purpose: the browser logs it as a failed resource
    assert not [e for e in errors if 'status of 400' not in e], errors
