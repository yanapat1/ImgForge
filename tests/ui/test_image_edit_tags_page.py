"""End-to-end: the Image page cards and their full edit (views/image.ts, core/tag-field.ts, settings/forms.ts, views/label-new.ts).
A broken session (its folder is gone) is no taller than a healthy one (its reason is a badge on the cover and one line in
the body, with Fix…), every card's footer stays on one line, and the pencil opens the full edit form, whose Tags field
saves the session's tags: a Visualize session (the Settings form, PUT …/tags) and a labeling session (the wizard, PATCH).

DATAVIZ_SCREENSHOTS=<dir> saves a screenshot of every step there."""

import os
import shutil
import time

import pytest

from test_page_smoke import _browser, _get, _send, server  # noqa: F401 - the app + its database
from test_page_smoke import _post, make_jpeg

sync_api = pytest.importorskip('playwright.sync_api')


def _viz(page, name):
    return page.locator(f'#image-panel-viz .session-card:has(.sc-open:text-is("{name}"))')


def _lab(page, name):
    return page.locator(f'#image-panel-label .session-card:has(.sc-open:text-is("{name}"))')


def _one_line(card):
    """The footer's buttons sit on the same row as its people."""
    foot = card.locator('.sc-foot')
    rows = foot.evaluate("f => [...f.children].map(c => Math.round(c.getBoundingClientRect().top + c.getBoundingClientRect().height / 2))")
    return max(rows) - min(rows) <= 4


def test_compact_cards_and_full_edit_with_tags(server, tmp_path):  # noqa: F811
    shutil.copytree(tmp_path / 'ds', tmp_path / 'good')
    shutil.copytree(tmp_path / 'ds', tmp_path / 'gone')
    _send(f'{server}/api/datasets', {'kind': 'local', 'path': str(tmp_path / 'good'), 'name': 'Good'})
    _send(f'{server}/api/datasets', {'kind': 'local', 'path': str(tmp_path / 'gone'), 'name': 'Gone'})
    shutil.rmtree(tmp_path / 'gone')
    imgs = tmp_path / 'lp_imgs'
    for k in range(2):
        make_jpeg(imgs / f'p{k}.jpg', 80, 60)
    out = _post(f'{server}/api/label/projects', {'name': 'Street', 'source': {'kind': 'local', 'path': str(imgs)},
                                                 'tasks': [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat']}]})
    pid = out['project']['id']
    for _ in range(600):
        if _get(f"{server}/api/jobs/{out['job_id']}")['status'] == 'done':
            break
        time.sleep(0.1)

    shots = os.environ.get('DATAVIZ_SCREENSHOTS')
    shot = (lambda page, name, el=None: (page.locator(el) if el else page).screenshot(path=f'{shots}/{name}.png')) \
        if shots else (lambda *a, **k: None)
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={'width': 1280, 'height': 860})
        # the one expected failure: Fix… opens the form on the missing folder, whose browse is a 400 the form shows
        expected = lambda r: '/api/fs/browse' in r.url and 'gone' in r.url      # noqa: E731
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('response', lambda r: r.status >= 400 and not expected(r) and errors.append(f'http {r.status} {r.url}'))
        page.goto(f'{server}/?image')
        good, gone = _viz(page, 'Good'), _viz(page, 'Gone')
        gone.wait_for(timeout=20000)
        page.wait_for_timeout(400)
        shot(page, '01-home-broken-and-healthy')

        # the broken card: a badge on the cover, one line in the body with Fix…; no taller than a healthy card
        assert 'Folder not found' in gone.locator('.sc-problem').inner_text()
        assert gone.locator('.sc-alert [data-act="edit"]').is_visible()
        assert gone.evaluate('c => c.offsetHeight') <= good.evaluate('c => c.offsetHeight') + 30
        assert gone.locator('.sc-alert').evaluate('a => a.offsetHeight') <= 30
        assert _one_line(good) and _one_line(gone)

        # Fix… opens the edit form of that session
        gone.locator('.sc-alert [data-act="edit"]').click()
        page.wait_for_selector('#settings-body [data-tagf="ds"]')
        page.keyboard.press('Escape')
        page.wait_for_selector('#settings', state='hidden')

        # the pencil opens the full edit: a Tags field under the display name; Enter / comma add, × removes
        good.locator('.sc-foot [data-act="edit"]').click()
        field = page.locator('#settings-body [data-tagf="ds"]')
        field.wait_for()
        inp = field.locator('.tagf-in')
        inp.fill('night')
        inp.press('Enter')
        inp.type('rain,wet,')
        assert field.locator('.tagf-chip .nm').all_inner_texts() == ['night', 'rain', 'wet']
        field.locator('[data-tagf-rm="2"]').click()
        inp.type('cars')                        # still in the box: Save adds it
        assert page.locator('#settings').is_visible()      # Enter in the box did not close the dialog
        shot(page, '02-settings-edit-with-tags', '.settings-box')
        page.locator('#settings-body button:not(.primary)[data-act="save-local"]').click()
        page.wait_for_selector('#settings', state='hidden', timeout=10000)
        good.locator('.sc-tag:text-is("cars")').wait_for(timeout=10000)
        assert good.locator('.sc-tag').all_inner_texts() == ['night', 'rain', 'cars']
        assert next(d for d in _get(f'{server}/api/datasets')['datasets'] if d['name'] == 'Good')['tags'] == ['night', 'rain', 'cars']

        # a labeling session: the pencil opens the wizard, its first step has the Tags field; Save changes keeps them
        page.click('#image-tab-label')
        lab = _lab(page, 'Street')
        lab.wait_for()
        assert _one_line(lab)
        lab.locator('.sc-foot [data-act="ledit"]').click()
        lfield = page.locator('#lw [data-tagf="lw"]')
        lfield.wait_for(timeout=10000)
        lfield.locator('.tagf-in').fill('batch-1')
        lfield.locator('.tagf-in').press('Enter')
        assert page.locator('#lw [data-f="name"]').input_value() == 'Street'   # Enter in the box did not go to the next step
        shot(page, '03-wizard-edit-with-tags', '#lw-box')
        for _ in range(3):
            page.click('#lw-foot [data-act="next"]')
            if page.locator('#lw').is_hidden():
                break
        page.wait_for_selector('#lw', state='hidden', timeout=10000)
        lab.locator('.sc-tag:text-is("batch-1")').wait_for(timeout=10000)
        assert _get(f'{server}/api/label/projects/{pid}')['tags'] == ['batch-1']

        page.set_viewport_size({'width': 400, 'height': 820})
        page.click('#image-tab-viz')
        page.wait_for_timeout(300)
        assert _one_line(_viz(page, 'Good')) and _one_line(_viz(page, 'Gone'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        shot(page, '04-home-narrow')
        browser.close()
    assert not errors, errors
