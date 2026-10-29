"""
The ◆ agents API (/api/llm/agent/*): one streamed chat per assistant (role data = Data Center, visual = the
visualizer), the confirmation of a paused change, stop, and the catalogue the chat box shows (tools, skills,
commands, models). Events: llm/engine/events.py.

Both roles run on the caller's providers and switches (llm/store.py). The visual role needs the visualizer session
the request is bound to (/s/<id>/); the data role needs none: its tools act through datacenter/service.py as the
caller, which also checks data.manage. Handlers are plain `def` (thread pool); the chat streams NDJSON from a sync
generator, so a run waiting for a confirmation holds one worker thread (pings keep the connection open).
"""

import json
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ...auth import access, deps, tiers
from ...auth import context as auth_context
from ...core import logging as vlog
from ...llm import prompting, providers, tools
from ...llm.engine import confirm, react, roles
from ...llm.providers import LLMError

_log = vlog.get_logger('llm')
MODELS_TTL = 600
COMMANDS = [
    # name, args, what it does, roles (None = both)
    ('model', '', 'pick the provider and model of this chat', None),
    ('mode', 'ask|auto|plan', 'ask: confirm every change · auto: changes run, dangerous ones ask · plan: read-only, '
                              'then a plan to approve (Shift+Tab)', None),
    ('effort', 'low|medium|high', 'how hard it works: steps, thinking, a final check', None),
    ('think', 'off|low|medium|high', 'thinking only (where the model supports it)', None),
    ('web', 'off|public|technical|full', 'web search, and how much of your data a query may contain', None),
    ('skills', '', 'list the skills', None),
    ('skill', '<name>', 'use a skill for the next message', None),
    ('sessions', '', 'open the chat history', None),
    ('new', '', 'start a new chat (the old one stays in history)', None),
    ('clear', '', 'start a new chat', None),
    ('resume', '<search>', 'open an earlier chat', None),
    ('rename', '<title>', 'rename this chat', None),
    ('delete', '', 'delete this chat', None),
    ('cost', '', 'tokens used by this chat', None),
    ('export', '', 'download this chat as Markdown', None),
    ('prompts', '', 'your own instructions and skills (Settings)', None),
    ('plugins', '', 'installed plugins (Settings)', None),
    ('help', '', 'list the commands', None),
]
WEB_LEVELS = {'off': 0, 'public': 1, 'technical': 2, 'full': 3}


def _max_web():
    """The admins' cap on the web search level (Admin › Settings), 3 when settings cannot be read."""
    try:
        from ...auth import settings
        return int(settings.get('llm_web_max_level'))
    except Exception:  # noqa: BLE001 - no database (tests without accounts)
        return 3


def _assets(user, role):
    """The user's own prompts, skills and plugin tools for one assistant (llm/assets.py)."""
    try:
        from ...llm import assets
    except ImportError:
        return {'prompts': [], 'skills': [], 'tools': []}
    return assets.for_run(user, role)


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.current_user)])
    models_cache = {}               # (user id, provider id) -> (time, [models])
    models_lock = threading.Lock()

    def store():
        if ctx.llm is None:
            raise HTTPException(501, 'the AI assistant is not enabled in this server')
        return ctx.llm

    def role_of(name):
        try:
            return roles.get(name)
        except KeyError as e:
            raise HTTPException(404, str(e))

    def gate(role):
        """The caller's connected active provider for this assistant, else 403."""
        tiers.check(auth_context.user(), 'llm.use')
        s = store()
        if not s.on(role.feature):
            raise HTTPException(403, f'AI feature "{role.feature}" is off (Settings › AI assistant)')
        return s.active()

    def session_state():
        b = auth_context.bound()
        if b is None:
            raise HTTPException(409, 'no session open: the visual assistant works inside a visualizer session')
        if not access.allows(b.role, 'viewer'):
            raise HTTPException(403, 'you cannot view this session')
        if b.state.index is None:
            raise HTTPException(503, 'the dataset is still being indexed')
        return b.state

    def pick_provider(active, body, saved):
        """The provider + model of this chat: the body's /model choice, else the remembered one, else the active
        provider. Only the caller's own connected (tested) providers; the model may differ from the saved one."""
        want = body.get('model') if isinstance(body.get('model'), dict) else saved
        pid = (want or {}).get('provider_id')
        p = active
        if pid and pid != (active or {}).get('id'):
            q = store().provider(pid)
            if q is None or not q.get('verified_at'):
                raise HTTPException(400, 'that provider is not connected: test it in Settings › AI assistant')
            p = q
        p = dict(p)
        m = str((want or {}).get('model') or '').strip()
        if m and (not pid or pid == p['id']):
            p['model'] = m[:200]
        return p

    # ------------------------------------------------------------ chat (NDJSON stream)
    @r.post('/api/llm/agent/{role_name}/chat')
    def agent_chat(role_name: str, body: dict):
        role = role_of(role_name)
        active = gate(role)
        user = auth_context.user()              # captured now: the stream runs in another thread
        st = session_state() if role.needs_session else None
        cfg = store().load()
        saved = (cfg.get('chat') or {}).get(role.name) or {}
        p = pick_provider(active, body, saved)
        msgs_in = body.get('messages') or []
        if not isinstance(msgs_in, list) or not msgs_in or not isinstance(msgs_in[-1], dict) \
                or msgs_in[-1].get('role') != 'user':
            raise HTTPException(400, "the last message must be the user's")
        opts_in = {**saved, **{k: body[k] for k in ('mode', 'effort', 'think', 'web') if k in body}}
        web_cfg = store().clean_web(cfg.get('web') or {})
        w = opts_in.get('web')                  # off unless the chat turns it on; 'on' / true = the level in Settings
        if w is True or w == 'on':
            w = web_cfg['level']
        opts_in['web'] = WEB_LEVELS.get(w, 0) if isinstance(w, str) else w
        opts_in['web'] = min(int(opts_in['web'] or 0), _max_web())
        opts = react.options(opts_in, p)
        a = _assets(user, role.name)
        forced = [str(s) for s in body.get('skills') or [] if isinstance(s, str)][:3]
        scope = body.get('scope') if isinstance(body.get('scope'), dict) else {}
        try:
            run = react.prepare(user, role, p, react.history(msgs_in), scope, opts, state=st, app=ctx,
                                user_prompts=a['prompts'], user_skills=a['skills'], plugin_tools=a['tools'],
                                forced_skills=forced, limit=cfg['budget']['max_input_tokens'])
        except LLMError as e:
            raise HTTPException(e.status, str(e))
        run.ctx.web_cfg = web_cfg

        def gen():
            try:
                for ev in react.events(run):
                    yield json.dumps(ev, default=str, ensure_ascii=False) + '\n'
            except Exception as e:  # noqa: BLE001 - the page shows the message in the transcript
                msg = str(e) if isinstance(e, LLMError) else f'{type(e).__name__}: {e}'
                _log.warning(f'agent {role.name}: {vlog.esc(msg)}')
                yield json.dumps({'type': 'error', 'detail': msg}) + '\n'
            finally:
                run.cancel.set()                # a closed stream frees a confirmation wait at once
        return StreamingResponse(gen(), media_type='application/x-ndjson',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @r.post('/api/llm/agent/confirm')
    def agent_confirm(body: dict):
        user = auth_context.user()
        tiers.check(user, 'llm.use')
        ids = body.get('approve')
        if not isinstance(ids, list):
            raise HTTPException(400, 'approve must be a list of action ids')
        try:
            return confirm.decide(user, body.get('confirm_id'), ids, body.get('reject') or [])
        except confirm.AgentError as e:
            raise HTTPException(e.status, str(e))

    @r.post('/api/llm/agent/stop')
    def agent_stop(body: dict):
        try:
            return react.stop(auth_context.user(), body.get('run_id'))
        except confirm.AgentError as e:
            raise HTTPException(e.status, str(e))

    # ------------------------------------------------------------ what the chat box shows
    @r.get('/api/llm/agent/{role_name}/catalog')
    def agent_catalog(role_name: str):
        role = role_of(role_name)
        user = auth_context.user()
        tiers.check(user, 'llm.use')
        a = _assets(user, role.name)
        tset = tools.for_role(role.domains, 'ask', extra=a['tools'])
        sk = prompting.skills(role.name, a['skills'])
        data = store().load()
        provs = [{'id': p['id'], 'name': p['name'], 'kind': p['kind'], 'model': p.get('model'),
                  'connected': bool(p.get('verified_at')), 'profile': react.profile_of(p)}
                 for p in data['providers']]
        return {
            'role': role.name, 'title': role.title,
            'tools': [{'name': t.name, 'risk': t.risk, 'domain': t.domain, 'description': t.description}
                      for t in tset.values()],
            'skills': [{'name': s['name'], 'description': s['description'], 'source': s['source']}
                       for s in sk.values()],
            'commands': [{'name': n, 'args': a_, 'description': d} for n, a_, d, rs in COMMANDS
                         if rs is None or role.name in rs],
            'providers': provs, 'active': data['active'],
            'chat': (data.get('chat') or {}).get(role.name) or {},
            'web': store().clean_web(data.get('web') or {}),
            'limits': {'max_steps': react.MAX_STEPS, 'max_retries': react.MAX_RETRIES},
        }

    @r.get('/api/llm/agent/models')
    def agent_models(provider_id: str):
        """The models of one of the caller's connected providers, for /model (cached 10 min)."""
        user = auth_context.user()
        tiers.check(user, 'llm.use')
        p = store().provider(provider_id)
        if p is None:
            raise HTTPException(404, f'no provider {provider_id!r}')
        key = ((user or {}).get('id'), provider_id, p.get('base_url'))
        with models_lock:
            hit = models_cache.get(key)
        if hit and time.time() - hit[0] < MODELS_TTL:
            return {'models': hit[1], 'current': p.get('model')}
        try:
            ms = providers.list_models(p)
        except LLMError as e:
            return {'models': [p.get('model')], 'current': p.get('model'), 'error': str(e)}
        with models_lock:
            models_cache[key] = (time.time(), ms)
        return {'models': ms, 'current': p.get('model')}

    return r
