"""End-to-end: the ◆ assistant dock (static/ts/core/dock.ts) on the right of every screen. On the home page with the
Data assistant switched on: the dock is open on the right and the page reflows beside it (body padding), the head
names the page, Collapse leaves a rail (remembered over a reload), Ctrl+. opens it again, the left edge drags the
width, a streamed answer shows in it and the chat reopens after a reload, the tasks strip follows the agent's checklist. Below 900 px it is a sheet
opened from a floating ◆ button. Off (no feature): no dock at all. Skips like test_page_smoke without a browser.
DATAVIZ_TEST_SHOTS=<folder> saves screenshots."""

import os

import pytest

from test_page_smoke import _browser, _post, _put, server  # noqa: F401 - the app + its database

sync_api = pytest.importorskip('playwright.sync_api')

_DOCK = "document.documentElement.dataset.dock"


def _shot(page, name):
    d = os.environ.get('DATAVIZ_TEST_SHOTS')
    if d:
        page.screenshot(path=os.path.join(d, f'{name}.png'))


def test_dock_on_home(server):  # noqa: F811 - the fixture
    from test_llm import REPLY, fake_app
    from test_models import _serve
    REPLY['text'] = 'Hello there, this is the fake model.'
    fake, url = _serve(fake_app())
    errors = []
    try:
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
            page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
            # nothing connected: no dock
            page.goto(f'{server}/')
            page.wait_for_selector('#home .app-header')
            page.wait_for_function(f"{_DOCK} === 'off'", timeout=10000)
            assert page.is_hidden('#ai-dock') and page.is_hidden('#ai-fab')

            p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                     'model': 'gpt-x', 'vision': True})['provider']
            assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
            _put(f'{server}/api/llm/config', {'features': {'data_center': True}})
            page.evaluate("window.dispatchEvent(new Event('llm-changed'))")
            page.wait_for_selector('#ai-dock #ai-page #ai-page-q', timeout=10000)
            assert page.evaluate(_DOCK) == 'open'
            dock = page.locator('#ai-dock').bounding_box()
            assert abs(dock['x'] + dock['width'] - 1400) <= 1 and 320 <= dock['width'] <= 640
            hdr = page.locator('#home .app-header').bounding_box()
            assert hdr['x'] + hdr['width'] <= dock['x'] + 1, 'the page reflows beside the dock'
            assert 'Home' in page.inner_text('#ai-dock .dock-head')

            # a streamed answer in the dock
            page.fill('#ai-page-q', 'What can you do?')
            page.press('#ai-page-q', 'Enter')
            page.wait_for_selector('#ai-page .ai-msg.bot:has-text("fake model")', timeout=20000)
            _shot(page, 'dock-home')

            # collapse to the rail, remembered over a reload; Ctrl+. opens it again
            page.click('#ai-dock [data-dock="rail"]')
            page.wait_for_function(f"{_DOCK} === 'rail'")
            assert page.locator('#ai-dock').bounding_box()['width'] <= 48
            page.reload()
            page.wait_for_function(f"{_DOCK} === 'rail'", timeout=10000)
            page.keyboard.press('Control+.')
            page.wait_for_function(f"{_DOCK} === 'open'")
            page.wait_for_selector('#ai-page-q', state='visible')
            # the reload continues the same chat (the dock remembers it per host)
            page.wait_for_selector('#ai-page .ai-msg.bot:has-text("fake model")', timeout=10000)

            # drag the left edge 100 px to the left: 100 px wider
            w0 = page.locator('#ai-dock').bounding_box()['width']
            g = page.locator('#ai-dock .dock-grip').bounding_box()
            page.mouse.move(g['x'] + 4, g['y'] + g['height'] / 2)
            page.mouse.down()
            page.mouse.move(g['x'] - 96, g['y'] + g['height'] / 2, steps=6)
            page.mouse.up()
            page.wait_for_function(f"document.getElementById('ai-dock').getBoundingClientRect().width > {w0 + 80}")

            # the tasks strip follows the agent's checklist (the events the chat hands to the dock)
            page.evaluate("""() => import('./static/js/core/dock.js').then((d) => {
                d.dockEvent({ type: 'meta', in_est: 50000 });
                d.dockEvent({ type: 'todo', items: [{ text: 'Open Model tab', state: 'done' }, { text: 'Run the model', state: 'in_progress' }] });
                d.dockEvent({ type: 'ui_action', label: 'Model tab' });
            })""")
            page.wait_for_selector('#ai-dock .dock-tasks:not(.hidden) .dock-drive:has-text("Model tab")')
            assert page.locator('#ai-dock .dock-steps li').count() == 2
            assert page.inner_text('#ai-dock .dock-ring b').strip() == '50'
            _shot(page, 'dock-tasks')

            # narrow: a sheet opened from the floating button
            page.set_viewport_size({'width': 600, 'height': 900})
            page.wait_for_function(f"{_DOCK} === 'rail'")
            assert page.is_visible('#ai-fab') and page.is_hidden('#ai-dock')
            page.click('#ai-fab')
            page.wait_for_function(f"{_DOCK} === 'open'")
            sheet = page.locator('#ai-dock').bounding_box()
            assert sheet['x'] <= 1 and sheet['width'] >= 598
            _shot(page, 'dock-narrow')
            browser.close()
        assert not errors, '\n'.join(errors)
    finally:
        fake.should_exit = True
