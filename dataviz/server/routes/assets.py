"""
Settings › Prompts & Skills (/api/llm/prompts, /api/llm/plugins, /api/llm/web*): the caller's own instructions and
skills for the ◆ assistants, their installed plugins (declarative: skills, prompts, HTTP tools; secrets write-only),
and their web search settings (level capped by the admins' maximum, engine, key, special words). Everything is per
user (llm/assets.py, llm/store.py); signing in and the tier permission llm.use are required.

    GET    /api/llm/prompts                 {items: [{id, kind, role, name, description, when, body, enabled, ...}]}
    POST   /api/llm/prompts                 {kind, role, name, description?, when?, body, enabled?} -> {item}
    PUT    /api/llm/prompts/{id}            the same fields (kind cannot change) -> {item}
    DELETE /api/llm/prompts/{id}
    GET    /api/llm/plugins                 {plugins: [{id, name, version, description, enabled, tools, skills,
                                             prompts, secrets: [{name, set}]}]}
    POST   /api/llm/plugins                 JSON {manifest} (an object or JSON text) or multipart `file`
                                            (plugin.json or a .zip) -> {plugin}
    PUT    /api/llm/plugins/{id}            {enabled?, secrets?: {NAME: value}} -> {plugin}
    DELETE /api/llm/plugins/{id}
    GET    /api/llm/web                     {web, has_key, max_level, org_words: n}
    PUT    /api/llm/web                     {level, ask_before, engine, url, cx, words} -> the same as GET
    PUT    /api/llm/web/key                 {api_key} ('' removes it) -> {has_key}
    POST   /api/llm/web/test                {query?} -> one search through llm/tools/web.py `test(user, query)`
                                            (501 while that module is not installed)
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import deps, tiers
from ...auth import context as auth_context
from ...llm import assets

MAX_UPLOAD = assets.MAX_ZIP


def _max_web():
    try:
        from ...auth import settings
        return int(settings.get('llm_web_max_level'))
    except Exception:  # noqa: BLE001 - no settings table: no cap
        return 3


def _org_words():
    try:
        from ...auth import settings
        return list(settings.get('llm_org_words') or [])
    except Exception:  # noqa: BLE001
        return []


def _call(fn, *a):
    try:
        return fn(*a)
    except assets.NotFound as e:
        raise HTTPException(404, str(e.args[0] if e.args else e)) from None
    except assets.AssetError as e:
        raise HTTPException(400, str(e)) from None


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.current_user)])

    def me():
        u = auth_context.user()
        if not u or not u.get('id'):
            raise HTTPException(401, 'sign in first')
        tiers.check(u, 'llm.use')
        return u

    def store():
        if ctx.llm is None:
            raise HTTPException(501, 'the AI assistant is not enabled in this server')
        return ctx.llm

    # ------------------------------------------------------------ prompts and skills
    @r.get('/api/llm/prompts')
    def list_prompts():
        return {'items': _call(assets.list_items, me()), 'max': assets.MAX_ITEMS, 'max_body': assets.MAX_BODY}

    @r.post('/api/llm/prompts')
    def create_prompt(body: dict):
        return {'item': _call(assets.create_item, me(), body)}

    @r.put('/api/llm/prompts/{iid}')
    def update_prompt(iid: str, body: dict):
        return {'item': _call(assets.update_item, me(), iid, body)}

    @r.delete('/api/llm/prompts/{iid}')
    def delete_prompt(iid: str):
        _call(assets.delete_item, me(), iid)
        return {'ok': True}

    # ------------------------------------------------------------ plugins
    @r.get('/api/llm/plugins')
    def list_plugins():
        return {'plugins': _call(assets.list_plugins, me()), 'max_tools': assets.MAX_TOOLS}

    @r.post('/api/llm/plugins')
    async def install_plugin(request: Request):
        user = me()
        ctype = request.headers.get('content-type', '')
        if ctype.startswith('multipart/form-data'):
            form = await request.form()
            f = form.get('file')
            if f is None or not hasattr(f, 'read'):
                raise HTTPException(400, 'send the plugin as the form field "file"')
            data = await f.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(400, 'the plugin file is larger than 2 MB')
        else:
            raw = await request.body()
            if len(raw) > MAX_UPLOAD:
                raise HTTPException(400, 'the manifest is larger than 2 MB')
            try:
                body = json.loads(raw or b'{}')
            except ValueError:
                raise HTTPException(400, 'send JSON {manifest} or a multipart file') from None
            data = body.get('manifest') if isinstance(body, dict) else None
            if data is None:
                raise HTTPException(400, 'send {manifest: {...}} (or the manifest as JSON text)')
        return {'plugin': await run_in_threadpool(_call, assets.install, user, data)}

    @r.put('/api/llm/plugins/{pid}')
    def update_plugin(pid: str, body: dict):
        return {'plugin': _call(assets.update_plugin, me(), pid, body)}

    @r.delete('/api/llm/plugins/{pid}')
    def delete_plugin(pid: str):
        _call(assets.uninstall, me(), pid)
        return {'ok': True}

    # ------------------------------------------------------------ web search
    def web_view(user):
        s = store()
        cap = _max_web()
        web = s.clean_web(s.load(user).get('web') or {})
        web['level'] = min(web['level'], cap)
        return {'web': web, 'has_key': bool(s.web_key(user)), 'max_level': cap, 'org_words': len(_org_words())}

    @r.get('/api/llm/web')
    def get_web():
        return web_view(me())

    @r.put('/api/llm/web')
    def put_web(body: dict):
        user = me()
        allowed = ('level', 'ask_before', 'engine', 'url', 'cx', 'words')
        patch = {k: body[k] for k in allowed if k in body}
        if 'level' in patch:
            try:
                patch['level'] = max(0, min(int(patch['level']), _max_web()))
            except (TypeError, ValueError):
                raise HTTPException(400, 'level must be 0, 1, 2 or 3') from None
        if 'words' in patch and not isinstance(patch['words'], list):
            raise HTTPException(400, 'words must be a list')
        try:
            store().set_config({'web': patch}, user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return web_view(user)

    @r.put('/api/llm/web/key')
    def put_web_key(body: dict):
        user = me()
        key = body.get('api_key')
        if key is not None and not isinstance(key, str):
            raise HTTPException(400, 'api_key must be text')
        if len(key or '') > 4000:
            raise HTTPException(400, 'api_key is too long')
        return store().set_web_key(key or '', user)

    @r.post('/api/llm/web/test')
    def test_web(body: dict | None = None):
        user = me()
        store()
        try:
            from ...llm.tools import web
        except ImportError:
            raise HTTPException(501, 'web search is not available on this server yet') from None
        fn = getattr(web, 'test', None)
        if fn is None:
            raise HTTPException(501, 'web search is not available on this server yet')
        if _max_web() <= 0:
            raise HTTPException(403, 'web search is turned off by the administrators')
        q = str((body or {}).get('query') or 'open source object detection datasets').strip()[:200]
        try:
            return fn(user, q)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 - the engine's message is the answer
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'[:300]}

    return r
