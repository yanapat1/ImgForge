"""End-to-end: the ◆ assistant dock (static/ts/core/dock.ts) on the right of every screen. On the home page with the
Data assistant switched on: the dock is open on the right and the page reflows beside it (body padding), the head
names the page, Collapse leaves a rail (remembered over a reload), Ctrl+. opens it again, the left edge drags the
width, a streamed answer shows in it and the chat reopens after a reload, the tasks strip follows the agent's checklist. Below 900 px it is a sheet
opened from a floating ◆ button. Off (no feature): no dock at all. Skips like test_page_smoke without a browser.
DATAVIZ_TEST_SHOTS=<folder> saves screenshots."""

import json
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
            page.goto(f'{server}/?image')
            page.wait_for_selector('#image .app-header')
            page.wait_for_function(f"{_DOCK} === 'off'", timeout=10000)
            assert page.is_hidden('#ai-dock') and page.is_hidden('#ai-fab')

            p = _post(f'{server}/api/llm/providers', {'name': 'fake', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                     'model': 'gpt-x', 'vision': True})['provider']
            assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
            _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
            page.evaluate("window.dispatchEvent(new Event('llm-changed'))")
            page.wait_for_selector('#ai-dock #ai-page #ai-page-q', timeout=10000)
            assert page.evaluate(_DOCK) == 'open'
            dock = page.locator('#ai-dock').bounding_box()
            assert abs(dock['x'] + dock['width'] - 1400) <= 1 and 320 <= dock['width'] <= 640
            hdr = page.locator('#image .app-header').bounding_box()
            assert hdr['x'] + hdr['width'] <= dock['x'] + 1, 'the page reflows beside the dock'
            assert 'Image' in page.inner_text('#ai-dock .dock-head')

            # a streamed answer in the dock
            page.fill('#ai-page-q', 'What can you do?')
            page.press('#ai-page-q', 'Enter')
            page.wait_for_selector('#ai-page .ai-msg.bot:has-text("fake model")', timeout=20000)
            _shot(page, 'dock-home')
            # drag-selecting the answer keeps the selection: a click in the dock no longer re-opens it and focuses the
            # chat box (<html data-dock="open"> matched the dock's [data-dock] click handler)
            b = page.locator('#ai-page .ai-msg.bot .ct-md').last.bounding_box()
            page.mouse.move(b['x'] + 2, b['y'] + 8)
            page.mouse.down()
            page.mouse.move(b['x'] + b['width'] * 0.7, b['y'] + 8, steps=8)
            page.mouse.up()
            page.wait_for_timeout(200)
            assert 'Hello there' in page.evaluate('getSelection().toString()')
            assert page.evaluate('document.activeElement.id') != 'ai-page-q'
            page.evaluate('getSelection().removeAllRanges()')

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


# ---------------------------------------------------------------- the agent drives the visualizer
def _tool_model():
    """An OpenAI-compatible model that calls one tool, picked by the user's words, then answers with the tool's
    result: 'open session <id>' -> navigate there (goal: open the model tab), 'open the model tab' -> open_tab,
    else compute_stats."""
    import json as _json
    import re as _re

    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    app = FastAPI()

    @app.get('/v1/models')
    def models():
        return {'data': [{'id': 'gpt-x'}]}

    @app.post('/v1/chat/completions')
    async def chat(request: Request):
        body = await request.json()
        msgs = body['messages']
        last = msgs[-1]
        text = last['content'] if isinstance(last['content'], str) else ' '.join(c.get('text', '') for c in last['content'] or [])

        if not body.get('stream'):                 # the connection test, chat titles
            return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
                    'usage': {'prompt_tokens': 5, 'completion_tokens': 1}}

        def sse(d):
            return 'data: ' + _json.dumps(d) + '\n\n'

        def gen():
            if last['role'] == 'tool':
                yield sse({'choices': [{'delta': {'content': f'TOOL SAID: {text}'}}]})
            else:
                m = _re.search(r'open session (\S+)', text)
                c = _re.search(r'consult (\S+)', text)
                if 'Asked by the assistant' in text:          # a consult: the other specialist answers at once
                    yield sse({'choices': [{'delta': {'content': 'VIZ SAYS 13 boxes'}}]})
                    yield 'data: [DONE]\n\n'
                    return
                if c:
                    name, args = 'consult', {'page': 'viz', 'session_id': c.group(1), 'question': 'How many boxes?'}
                elif m:
                    name, args = 'navigate', {'screen': 'viz', 'session_id': m.group(1), 'goal': 'open the model tab'}
                else:
                    name, args = ('open_tab', {'tab': 'model'}) if 'model tab' in text else ('compute_stats', {})
                yield sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                                                                   'function': {'name': name, 'arguments': _json.dumps(args)}}]}}]})
                yield sse({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]})
            yield 'data: [DONE]\n\n'
        return StreamingResponse(gen(), media_type='text/event-stream')
    return app


def test_agent_drives_the_visualizer(server):  # noqa: F811 - the fixture
    from test_models import _serve
    from test_page_smoke import _get
    fake, url = _serve(_tool_model())
    errors = []
    try:
        sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
        p = _post(f'{server}/api/llm/providers', {'name': 'tools', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'assistant': True, 'dataset_qa': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1500, 'height': 950})
            page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
            page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
            page.goto(f'{server}/?session={sid}')
            page.wait_for_selector('#ai-dock #ai-q', timeout=60000)
            # auto: the tab opens at once, the page reports it, the model reads it
            page.focus('#ai-q')
            page.keyboard.press('Shift+Tab')
            assert page.inner_text('#ai-side .chat-chip.mode').strip() == 'auto'
            with page.expect_request(lambda r: '/api/llm/agent/ui-result' in r.url) as req:
                page.fill('#ai-q', 'open the model tab')
                page.press('#ai-q', 'Enter')
            sent = json.loads(req.value.post_data)
            assert sent['ok'] is True and req.value.response().status == 200
            # the page's answer reached the model in time (not "no answer yet")
            page.wait_for_selector('#ai-side .ai-msg.bot:has-text("Model is open")', timeout=15000)
            assert page.evaluate("document.querySelector('main').dataset.tab") == 'model'
            # ask: a run waits for Approve, then the page computes and opens Class stats
            page.keyboard.press('Shift+Tab')
            page.keyboard.press('Shift+Tab')            # auto -> plan -> ask
            assert page.inner_text('#ai-side .chat-chip.mode').strip() == 'ask'
            page.fill('#ai-q', 'show the class stats')
            page.press('#ai-q', 'Enter')
            page.wait_for_selector('#ai-side .ct-confirm [data-ca="approve"]', timeout=20000)
            assert page.evaluate("document.querySelector('main').dataset.tab") == 'model', 'nothing ran before Approve'
            _shot(page, 'drive-approve')
            page.click('#ai-side .ct-confirm [data-ca="approve"]')
            page.wait_for_selector('#ai-side .ai-msg.bot:has-text("Class stats is open")', timeout=90000)
            assert page.evaluate("document.querySelector('main').dataset.tab") == 'stats'
            _shot(page, 'drive-done')
            browser.close()
        assert not errors, '\n'.join(errors)
    finally:
        fake.should_exit = True


def test_one_chat_across_pages(server):  # noqa: F811 - the fixture
    """Bucket: "open session X" -> the agent navigates; the visualizer opens the SAME chat (the first message
    is there), sends the goal as a continued message and its specialist opens the Model tab; Home shows that chat."""
    from test_models import _serve
    from test_page_smoke import _get
    fake, url = _serve(_tool_model())
    errors = []
    try:
        sid = _get(f'{server}/api/datasets')['datasets'][0]['id']
        p = _post(f'{server}/api/llm/providers', {'name': 'one', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'assistant': True, 'bucket_data': True}})
        before = _get(f'{server}/api/llm/sessions?role=app')['count']
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1500, 'height': 950})
            page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
            page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
            page.goto(f'{server}/?bucket')
            page.wait_for_selector('#ai-dock #bk-ai-q', timeout=30000)
            page.fill('#bk-ai-q', f'open session {sid} in the visualizer')
            with page.expect_navigation(url=lambda u: f'/s/{sid}/' in u, timeout=30000):
                page.press('#bk-ai-q', 'Enter')
            # the same chat on the visualizer: the Bucket page message is there, the goal continues it
            page.wait_for_selector('#ai-side .chat-turn.user:has-text("open session")', timeout=60000)
            page.wait_for_selector('#ai-side .chat-turn.user:has-text("Continued")', timeout=20000)
            page.wait_for_selector('#ai-side .ai-msg.bot:has-text("Model is open")', timeout=30000)
            assert page.evaluate("document.querySelector('main').dataset.tab") == 'model'
            _shot(page, 'one-chat-viz')
            # the Image page: the same chat again (one current chat for the whole app)
            page.goto(f'{server}/?image')
            page.wait_for_selector('#ai-page .chat-turn.user:has-text("open session")', timeout=30000)
            assert page.locator('#ai-page .ai-msg.bot:has-text("Model is open")').count() == 1
            # a question about the visualizer from Home: consult answers it here, the page stays
            page.fill('#ai-page-q', f'consult {sid} about boxes')
            page.press('#ai-page-q', 'Enter')
            page.wait_for_selector('#ai-page .ai-msg.bot:has-text("VIZ SAYS 13 boxes")', timeout=60000)
            assert '/s/' not in page.url
            browser.close()
        assert _get(f'{server}/api/llm/sessions?role=app')['count'] == before + 1, 'one chat, not one per page'
        assert not errors, '\n'.join(errors)
    finally:
        fake.should_exit = True


# ---------------------------------------------------------------- a streaming answer stays clickable
def _slow_thinker():
    """An OpenAI-compatible model that thinks for ~4 s (a reasoning_content delta every 40 ms), then answers."""
    import asyncio
    import json as _json

    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    app = FastAPI()

    @app.get('/v1/models')
    def models():
        return {'data': [{'id': 'gpt-x'}]}

    @app.post('/v1/chat/completions')
    async def chat(request: Request):
        body = await request.json()
        if not body.get('stream'):
            return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}

        async def gen():
            for k in range(100):
                yield 'data: ' + _json.dumps({'choices': [{'delta': {'reasoning_content': f'step {k} of my thinking. '}}]}) + '\n\n'
                await asyncio.sleep(0.04)
            yield 'data: ' + _json.dumps({'choices': [{'delta': {'content': 'Done thinking.'}}]}) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(gen(), media_type='text/event-stream')
    return app


def test_streaming_answer_stays_clickable(server):  # noqa: F811 - the fixture
    """While the answer streams (an event every 40 ms) the turn is patched in place, not replaced: the activity header
    folds and opens on a click, the live Thinking row's text shows and follows its newest line, and the spinner stays
    the same element (it does not restart)."""
    from test_models import _serve
    fake, url = _serve(_slow_thinker())
    errors = []
    try:
        p = _post(f'{server}/api/llm/providers', {'name': 'slow', 'kind': 'openai_compat', 'base_url': f'{url}/v1',
                                                 'model': 'gpt-x'})['provider']
        assert _post(f'{server}/api/llm/test', {'id': p['id']})['ok']
        _put(f'{server}/api/llm/config', {'features': {'bucket_data': True}})
        with sync_api.sync_playwright() as pw:
            browser = _browser(pw)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
            page.on('console', lambda m: m.type == 'error' and errors.append(f'console: {m.text}'))
            page.goto(f'{server}/?image')
            page.wait_for_selector('#ai-dock #ai-page #ai-page-q', timeout=15000)
            page.fill('#ai-page-q', 'Think about it')
            page.press('#ai-page-q', 'Enter')
            head = page.locator('#ai-page .ct-act.live > .ct-act-h')
            page.wait_for_selector('#ai-page .ct-act.live .ct-act-now:has-text("Thinking")', timeout=15000)
            page.evaluate("document.querySelector('#ai-page .ct-act.live .ct-spin').__mark = 1")
            # it is open while the answer runs; a slow click (mousedown, 150 ms, mouseup) lands although several
            # events arrive in between: one folds it, the next opens it again
            page.wait_for_selector('#ai-page .ct-act.live.open', timeout=3000)
            for want in ('false', 'true'):
                b = head.bounding_box()
                page.mouse.move(b['x'] + 40, b['y'] + b['height'] / 2)
                page.mouse.down()
                page.wait_for_timeout(150)
                page.mouse.up()
                page.wait_for_function(f"document.querySelector('#ai-page .ct-act.live > .ct-act-h')?.getAttribute('aria-expanded') === '{want}'", timeout=3000)
            page.wait_for_selector('#ai-page .ct-act.live.open .ct-act-row.k-think .ct-act-text', timeout=3000)
            assert 'of my thinking' in page.inner_text('#ai-page .ct-act.live .ct-act-text')
            page.wait_for_timeout(400)
            assert page.evaluate("document.querySelector('#ai-page .ct-act.live .ct-spin').__mark") == 1, \
                'the spinner was replaced (its animation restarts)'
            # the live thinking box follows its newest line once it overflows; scrolled up, it stays where the user is
            box = '#ai-page .ct-act.live .ct-act-row.k-think .ct-act-text'
            page.wait_for_function(f"(b => b && b.scrollHeight > b.clientHeight + 40)(document.querySelector('{box}'))", timeout=10000)
            page.wait_for_timeout(200)
            gap = f"(b => b.scrollHeight - b.scrollTop - b.clientHeight)(document.querySelector('{box}'))"
            assert page.evaluate(gap) < 24, 'the thinking box does not show its newest text'
            page.evaluate(f"document.querySelector('{box}').scrollTop = 0")
            page.wait_for_timeout(300)
            assert page.evaluate(f"document.querySelector('{box}').scrollTop") == 0
            # it stays open through the end of the answer
            page.wait_for_selector('#ai-page .ai-msg.bot:has-text("Done thinking.")', timeout=20000)
            assert page.get_attribute('#ai-page .ct-act > .ct-act-h', 'aria-expanded') == 'true'
            browser.close()
        assert not errors, '\n'.join(errors)
    finally:
        fake.should_exit = True
