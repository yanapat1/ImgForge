"""
The ReAct loop every chat runs on (Data agent and Visual agent): think → call tools → read results → repeat, until
the model answers, asks, gives up, or a limit is hit.

    run = prepare(user, role, provider, history, scope, opts, ...)   -> Run (system prompt, tools, trimmed history)
    for ev in events(run): ...                                        -> the NDJSON events (llm/engine/events.py)

Limits (the product's rules, not tuning knobs):
    MAX_STEPS   = 50   provider calls per agent run (effort / a small model lower it, never raise it)
    MAX_RETRIES = 50   retries per agent run of a transient failure (timeout, unreachable, 429, 5xx); backoff
                       min(2^n, 20) s or the provider's Retry-After. 400 / 401 / 403 fail at once.
    loop guard         the same call with the same arguments a 3rd time gets a nudge, a 5th time stops the run.

Modes: ask (every change is confirmed), auto (changes run, dangerous ones are confirmed), plan (read-only: the
agent researches and submits a plan the user approves). Confirmations pause the run (engine/confirm.py) and the
agent continues with the results in the same turn.
"""

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from ...core import logging as vlog
from .. import budget, prompting, providers, tools
from ..providers import LLMError
from . import compact, confirm, roles, uiwait

_log = vlog.get_logger('llm')

MAX_STEPS = 50
MAX_RETRIES = 50
MAX_TURNS = 40
MAX_RESULT = 6000               # characters of one tool result fed back to the model
CONFIRM_TIMEOUT = 600
PING_S = 15
UI_ACK_S = 20                   # a page action (open a tab, colour, filter) reports back within this, or is assumed done
RUN_WAIT_S = 600                # a run action (model run, map, scan) waits this long for the page, then "still running"
RUN_RETRIES = 2                 # a run the page reports as a transient failure (busy, 5xx, timeout) is retried
ALWAYS_APPLY = ('open_tab', 'fit_view', 'navigate')     # harmless and undone in a click: applied in ask mode too
REPEAT_NUDGE, REPEAT_STOP = 3, 5
SMALL_STEPS = 20
SMALL_TOOLS = 8
MODES = ('ask', 'auto', 'plan')
EFFORTS = {                     # one knob: steps, thinking, a check pass before the final answer
    'low': {'steps': 15, 'think': 'off', 'verify': False},
    'medium': {'steps': 30, 'think': 'low', 'verify': False},
    'high': {'steps': 50, 'think': 'high', 'verify': True},
}
THINK = ('off', 'low', 'medium', 'high')
VERIFY = ('(check) Before you finish: verify with read-only tools that the changes did what was asked, '
          'then give the short final answer.')

_runs = {}                      # run_id -> {user_id, cancel}: POST /api/llm/agent/stop
_runs_lock = threading.Lock()
_chats = {}                     # chat id -> (cancel event, time) of the run answering it: one turn at a time per chat
CHAT_CLAIM_S = 3600             # a claim whose stream never started (or never ended) frees itself after this


def claim_chat(sid, cancel):
    """Reserve a saved chat for one run (the same chat is open in several tabs / pages) -> False when another run
    is still answering it. release_chat frees it when the stream ends (also on stop or a closed connection)."""
    now = time.time()
    with _runs_lock:
        cur = _chats.get(sid)
        if cur is not None and cur[0] is not cancel and not cur[0].is_set() and now - cur[1] < CHAT_CLAIM_S:
            return False
        _chats[sid] = (cancel, now)
        return True


def release_chat(sid, cancel):
    with _runs_lock:
        cur = _chats.get(sid)
        if cur is not None and cur[0] is cancel:
            del _chats[sid]


def _sleep(cancel, s):
    return cancel.wait(s) if cancel is not None else time.sleep(s)


@dataclass
class Ctx:
    """What a ctx tool sees: who asks, which agent, where they are, their skills, the session's state."""
    user: dict
    role: str
    scope: dict
    skills: dict
    state: object = None            # the visualizer session (State) for the visual agent
    app: object = None              # the server context (curation, models ...)
    seen: set = field(default_factory=set)      # names the tools returned this turn (the web guard blocks them)
    web_level: int = 0
    run_id: str = ''
    web_cfg: dict = None            # store.clean_web of the user's settings (the route sets it)
    web_blocked: int = 0            # web queries the guard blocked this turn (3 = no more web this turn)
    events: list = field(default_factory=list)  # events a tool adds for the page ('search'), sent after its tool_end
    ui_ack: bool = False            # the page reports how its page actions ended (POST /api/llm/agent/ui-result)
    roles_on: dict = None           # {role: its assistant is switched on for this user} (navigate refuses an off page)
    provider: dict = None           # the run's provider (a consult runs the other specialist on it)
    limit: int = None               # the run's input budget
    cancel: object = None           # the run's stop event (a consult stops with it)
    depth: int = 0                  # 1 inside a consult: no consult of a consult
    sub_usage: dict = field(default_factory=lambda: {'in': 0, 'out': 0})   # tokens of consults, added to the turn


@dataclass
class Run:
    run_id: str
    user: dict
    role: roles.Role
    provider: dict
    system: str
    msgs: list
    tools: dict
    ctx: Ctx
    mode: str = 'ask'
    effort: str = 'medium'
    think: str = 'low'
    web: int = 0
    native_web: bool = False
    profile: str = 'large'
    max_steps: int = 30
    limit: int = budget.MAX_BUDGET
    est: int = 0
    raw_est: int = 0                # the estimate before budget.fit cut anything (auto-compaction looks at it)
    cancel: threading.Event = field(default_factory=threading.Event)
    temperature: float = None


# ---------------------------------------------------------------- options
def profile_of(provider):
    """'small' for a model of <= 14B parameters (from the provider's Agent profile, or guessed from the model
    name like qwen2.5-7b-instruct / llama3.1:8b), else 'large'."""
    p = str((provider or {}).get('agent_profile') or 'auto')
    if p in ('small', 'large'):
        return p
    m = re.search(r'(?<![\d.])(\d+(?:\.\d+)?)\s*b(?![a-z])', str((provider or {}).get('model') or '').lower())
    return 'small' if m and float(m.group(1)) <= 14 else 'large'


def options(body, provider):
    """The run options from a chat body: mode, effort, think, web level, steps (all clamped)."""
    mode = body.get('mode') if body.get('mode') in MODES else 'ask'
    effort = body.get('effort') if body.get('effort') in EFFORTS else 'medium'
    e = EFFORTS[effort]
    think = body.get('think') if body.get('think') in THINK else e['think']
    try:
        web = max(0, min(3, int(body.get('web') or 0)))
    except (TypeError, ValueError):
        web = 0
    profile = profile_of(provider)
    steps = min(MAX_STEPS, e['steps'], SMALL_STEPS if profile == 'small' else MAX_STEPS)
    return {'mode': mode, 'effort': effort, 'think': think, 'web': web, 'profile': profile, 'max_steps': steps}


SUMMARY_HEAD = '## Earlier in this chat (summary)'
PLAN_MAX = 12
PLAN_MARK = {'done': '✓', 'skipped': '–', 'active': '▶'}


def plan_section(items):
    """The chat's checklist for the prompt (only while a step is still open): one line per step, its page."""
    items = [x for x in (items or []) if isinstance(x, dict) and x.get('text')]
    if not items or all(x.get('state') in ('done', 'skipped') for x in items):
        return ''
    rows = [f'{PLAN_MARK.get(x.get("state"), "·")} {x["text"]}' + (f' ({roles.SCREEN_LABEL.get(x["page"], x["page"])})'
                                                                 if x.get('page') else '')
            for x in items[:PLAN_MAX]]
    return ('## Plan of this chat\nContinue the ▶ (or the first ·) step; a step on another page: `navigate` there '
            'with it as the goal. Keep it up to date with todo.\n' + '\n'.join(rows))


def history(messages):
    """[{role, text, page?}] -> provider messages (text only, starting with the user's). One chat follows the user
    over the pages: a user message written on another page than the one before it starts with a short marker
    ("[on Data Center]"), and the newest one says the tools changed with the page, so the model never mixes up
    which page's tools it has."""
    out = []
    msgs = (messages or [])[-MAX_TURNS:]
    prev = None
    for i, m in enumerate(msgs):
        t = str((m or {}).get('text') or '').strip()
        page = (m or {}).get('page') if isinstance((m or {}).get('page'), dict) else {}
        lab = page.get('label')
        if t and lab and prev and lab != prev and m.get('role') != 'assistant':
            t = (f'[the user moved to {lab}: earlier turns ran on other pages; you now have this page\'s tools] {t}'
                 if i == len(msgs) - 1 else f'[on {lab}] {t}')
        if lab:
            prev = lab
        if t:
            out.append({'role': 'assistant' if m.get('role') == 'assistant' else 'user', 'parts': [{'text': t}]})
    while out and out[0]['role'] != 'user':
        out.pop(0)
    return out


_WORD = re.compile(r'[a-z0-9]+')


def pick_tools(all_tools, question, n=SMALL_TOOLS, keep=()):
    """A small model gets only the tools that match the request (by name / description words), plus the control
    and lookup tools and the role's own `keep` (roles.Role.always): fewer choices, fewer wrong calls."""
    always = {'ask_user', 'cannot_do', 'list_buckets', 'list_folder', 'search', 'view_state', 'read_docs', 'use_skill',
              *keep}
    words = set(_WORD.findall(question.lower()))
    scored = []
    for t in all_tools.values():
        if t.name in always:
            continue
        name = set(t.name.split('_'))
        desc = set(_WORD.findall(t.description.lower()))
        score = 3 * len(words & name) + len(words & desc)
        if score:
            scored.append((score, t.name))
    keep = {n_ for _, n_ in sorted(scored, reverse=True)[:n]}
    return {k: t for k, t in all_tools.items() if k in always or k in keep}


def prepare(user, role, provider, msgs, scope, opts, *, state=None, app=None, user_prompts=(), user_skills=(),
            plugin_tools=(), forced_skills=(), limit=None, summary=None, plan=None):
    """Everything one run needs, checked before the stream starts (LLMError 400 when it cannot fit).
    summary: the chat's compacted earlier part (engine/summarize.py), read as part of the system prompt.
    plan: the chat's saved checklist (the todo items, over every page), shown while a step is open."""
    role = roles.get(role) if isinstance(role, str) else role
    if not msgs or msgs[-1]['role'] != 'user':
        raise LLMError("the last message must be the user's", 400)
    domains = tuple(d for d in role.domains if d != 'web' or opts['web'] > 0)
    plugin = [t for t in plugin_tools if getattr(t, 'roles', None) in (None, (), role.name)
              or role.name in (getattr(t, 'roles', None) or ())]
    tset = tools.for_role(domains, opts['mode'], extra=plugin)
    if opts['profile'] == 'small':
        q = ' '.join(x.get('text', '') for x in msgs[-1]['parts'] if 'text' in x)
        tset = pick_tools(tset, q, keep=role.always)
    sk = prompting.skills(role.name, user_skills)
    scope_text = roles.SCOPES[role.name](user, scope)
    system = prompting.build(role.name, tools_catalog=tools.catalog(tset), scope_text=scope_text, mode=opts['mode'],
                             effort=opts['effort'], profile=opts['profile'], skill_list=sk,
                             user_prompts=[p for p in user_prompts if p.get('role') in (None, 'all', role.name)],
                             user_name=(user or {}).get('display_name') or (user or {}).get('username') or '',
                             forced_skills=forced_skills, pages_text=roles.other_pages(role.name))
    plan_text = plan_section(plan)
    if plan_text:
        system += '\n\n' + plan_text
    if summary:
        system += (f'\n\n{SUMMARY_HEAD}\nThe earlier part of this chat was compacted. This summary replaces those '
                   f'messages: trust its numbers, ids and decisions, and continue from its open tasks.\n\n{summary}')
    lim = limit or budget.MAX_BUDGET
    sys_all = system + json.dumps([t.spec() for t in tset.values()])
    raw_est = budget.estimate(provider['kind'], sys_all, msgs)
    fitted, est = budget.fit(provider['kind'], sys_all, [], msgs, lim)
    run_id = uuid.uuid4().hex[:12]
    ctx = Ctx(user=user, role=role.name, scope=scope if isinstance(scope, dict) else {}, skills=sk, state=state,
              app=app, web_level=opts['web'], run_id=run_id, provider=provider, limit=lim)
    p = dict(provider)
    if opts['profile'] == 'small':
        p['temperature'] = min(float(p.get('temperature') or 0.1), 0.1)
    run = Run(run_id=run_id, user=user, role=role, provider=p, system=system, msgs=list(fitted), tools=tset, ctx=ctx,
               mode=opts['mode'], effort=opts['effort'], think=opts['think'], web=opts['web'],
               native_web=_native_ok(p, opts['web']), profile=opts['profile'], max_steps=opts['max_steps'], limit=lim, est=est,
               raw_est=raw_est)
    ctx.cancel = run.cancel
    return run


# ---------------------------------------------------------------- helpers
def _native_ok(provider, web):
    """The provider's own web search only at level Full (its queries cannot be checked before they go out), and
    only when the provider supports it alongside function tools (providers.native_web_ok, when present)."""
    if web < 3:
        return False
    ok = getattr(providers, 'native_web_ok', None)
    return bool(ok(provider)) if callable(ok) else True


def _tool_events(ctx):
    """The events tools queued on the context ('search' ...), emptied."""
    evs, ctx.events[:] = list(ctx.events), []
    return evs


def _dump(result):
    s = json.dumps(result, ensure_ascii=False, default=str, separators=(',', ':'))
    return s if len(s) <= MAX_RESULT else s[:MAX_RESULT] + '… (cut: narrow the request)'


def _preview(result):
    s = json.dumps(result, ensure_ascii=False, default=str, indent=1)
    return s if len(s) <= 1500 else s[:1500] + '\n…'


def _error_text(e):
    if isinstance(e, tools.ToolError):
        return str(e)
    if getattr(e, 'status', None) is not None or type(e).__name__ == 'DCError':     # the services' own messages
        return str(e)
    _log.warning(f'agent tool: {type(e).__name__}: {vlog.esc(str(e))}')
    return f'{type(e).__name__}: {e}'


PAGE_OF_DOMAIN = {r.name: (r.title, r.screen) for r in roles.ROLES.values()}


def _missing_tool(run, name):
    """Why a call names a tool this run does not have: another page's (navigate there), not in this mode, unknown."""
    t = tools.REGISTRY.get(name)
    here = (run.ctx.scope or {}).get('page') or ('viz' if run.role.name == 'visual' else 'data')
    if t is not None and t.domain in PAGE_OF_DOMAIN and t.domain not in run.role.domains:
        where, screen = PAGE_OF_DOMAIN[t.domain]
        if t.domain not in roles.installed():
            return f'{name} is a {where} tool and this install has no {where}: tell the user'
        return (f'{name} is a {where} tool and the user is on {roles.SCREEN_LABEL.get(here, here)}: call '
                f'navigate(screen={screen!r}, goal=<the request>) to continue there (it ends this turn), or answer '
                'with the tools you have')
    if t is not None and t.domain in run.role.domains:
        return f'{name} is not available in mode {run.mode}; your tools are: {", ".join(run.tools)}'
    return f'unknown tool {name!r}; your tools are: {", ".join(run.tools)}'


def _sig(c):
    return c.get('name', '') + json.dumps(c.get('args'), sort_keys=True, default=str)


def _remember_names(ctx, result):
    """Names a tool returned (buckets, folders, files, classes): the web guard treats them as internal."""
    def walk(x, depth=0):
        if depth > 4 or len(ctx.seen) > 5000:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ('name', 'path', 'bucket', 'class', 'title', 'label') and isinstance(v, str) and v:
                    ctx.seen.add(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x[:500]:
                walk(v, depth + 1)
    walk(result)


def _extras(out):
    """What a change's result gives the page: an undo button, a link, re-tagged datasets."""
    if not isinstance(out, dict):
        return {}
    ex = {}
    if out.get('undo_id'):
        ex['undo_id'] = out['undo_id']
    if out.get('url'):
        ex['url'] = out['url']
    if out.get('retagged'):
        ex['retagged'] = out['retagged']
    errs = out.get('errors')
    if errs:
        ex['errors'] = errs[:5]
    return ex


def register_run(run):
    with _runs_lock:
        _runs[run.run_id] = {'user_id': (run.user or {}).get('id'), 'cancel': run.cancel, 'at': time.time()}
        for k in [k for k, v in _runs.items() if time.time() - v['at'] > 6 * 3600]:
            del _runs[k]


def stop(user, run_id):
    with _runs_lock:
        r = _runs.get(str(run_id or ''))
    if r is None:
        raise confirm.AgentError('no such run (it already ended)', 404)
    if r['user_id'] != (user or {}).get('id'):
        raise confirm.AgentError('this run belongs to another user', 403)
    r['cancel'].set()
    return {'ok': True}


# ---------------------------------------------------------------- one provider step (streamed, retried)
def _step(run, state, step_no):
    """Streams one provider call -> yields events; the step's result lands in state['ans']."""
    specs = [t.spec() for t in run.tools.values()]
    while True:
        text, thinking, calls, usage, think_t0 = [], [], [], {}, None
        streamed = False
        try:
            for kind, val in providers.stream_tools(run.provider, run.system, run.msgs, specs, think=run.think,
                                                    native_web=run.native_web):
                if run.cancel.is_set():
                    break
                if kind == 'thinking':
                    if think_t0 is None:
                        think_t0 = time.perf_counter()
                    thinking.append(val)
                    streamed = True
                    yield {'type': 'thinking', 'text': val.get('text', '') if isinstance(val, dict) else val}
                    continue
                if think_t0 is not None and kind in ('text', 'tool_call'):
                    yield {'type': 'thinking_end', 'ms': round((time.perf_counter() - think_t0) * 1000)}
                    think_t0 = None
                if kind == 'text':
                    text.append(val)
                    streamed = True
                    yield {'type': 'text', 'text': val}
                elif kind == 'tool_call':
                    calls.append(val)
                elif kind == 'search':
                    yield {'type': 'search', **val}
                elif kind == 'usage':
                    usage = val or {}
            if think_t0 is not None:
                yield {'type': 'thinking_end', 'ms': round((time.perf_counter() - think_t0) * 1000)}
            state['ans'] = {'text': ''.join(text), 'thinking': thinking, 'calls': calls, 'usage': usage}
            return
        except LLMError as e:
            if not getattr(e, 'transient', False):
                raise
            err = e
        except (TimeoutError, ConnectionError) as e:
            err = LLMError(f'{type(e).__name__}: {e}', 502, transient=True)
        state['retries'] += 1
        if state['retries'] > MAX_RETRIES:
            raise LLMError(f'gave up after {MAX_RETRIES} retries: {err}', 502)
        n = state['retries']
        wait = err.retry_after if getattr(err, 'retry_after', None) else min(2 ** (n - 1), 20)
        wait = max(0.0, min(float(wait), 60.0))
        yield {'type': 'retry', 'attempt': n, 'max': MAX_RETRIES, 'reason': str(err)[:300], 'wait_s': round(wait, 1),
               'discard': streamed, 'step': step_no}
        if _sleep(run.cancel, wait):
            state['ans'] = None
            return


# ---------------------------------------------------------------- the loop
def events(run):
    """The events of one agent turn (dicts; the route writes one JSON line each)."""
    register_run(run)
    p = run.provider
    if (run.ctx.web_cfg or {}).get('ask_before'):
        run.native_web = False                  # every query is confirmed: the provider's own cannot be
    cap = budget.cap(run.limit)
    kind = p['kind']
    yield {'type': 'meta', 'run_id': run.run_id, 'role': run.role.name, 'model': p.get('model'),
           'provider': p.get('name'), 'kind': p.get('kind'), 'mode': run.mode, 'effort': run.effort,
           'think': run.think, 'web': run.web, 'profile': run.profile, 'max_steps': run.max_steps, 'in_est': run.est,
           'tools': len(run.tools), 'limit': cap}
    # the context of the NEXT provider call, for the page's ring: our estimate before a call, the provider's own
    # count once it answered (usage below is the turn's billed sum over every step, which is not a context size)
    ctx_now = run.est
    yield {'type': 'context', 'used': ctx_now, 'limit': cap, 'exact': False}
    spec_tokens = None                          # the tool specs' share: run.system has no specs (counted once)
    st = {'retries': 0, 'ans': None}
    usage = {'in': 0, 'out': 0}
    t0 = time.perf_counter()
    repeats, changed, verified = {}, False, False

    def done(steps):
        if not usage['in']:
            usage['in'] = run.est
        for k in ('in', 'out'):                 # what the consults of this turn used
            usage[k] += run.ctx.sub_usage.get(k, 0)
            run.ctx.sub_usage[k] = 0
        return {'type': 'done', 'usage': dict(usage), 'ms': round((time.perf_counter() - t0) * 1000), 'steps': steps,
                'ctx': ctx_now, 'limit': cap}

    for step in range(1, run.max_steps + 1):
        if run.cancel.is_set():
            yield {'type': 'notice', 'level': 'info', 'text': 'Stopped.'}
            yield done(step - 1)
            return
        if step > 1:
            if spec_tokens is None:
                spec_tokens = budget.text_tokens(json.dumps([t.spec() for t in run.tools.values()]))
            ok, est = compact.fit(kind, run.system, run.msgs, cap - spec_tokens)
            if not ok:
                yield {'type': 'error', 'detail': 'the tool results grew over the token budget: ask for less at once'}
                return
            ctx_now = est + spec_tokens
            yield {'type': 'context', 'used': ctx_now, 'limit': cap, 'exact': False}
        yield {'type': 'step', 'n': step, 'max': run.max_steps}
        yield from _step(run, st, step)
        ans = st['ans']
        if ans is None:                                     # stopped while waiting to retry
            yield {'type': 'notice', 'level': 'info', 'text': 'Stopped.'}
            yield done(step)
            return
        u = ans['usage'] or {}
        usage['in'] += int(u.get('in') or 0)
        usage['out'] += int(u.get('out') or 0)
        if u.get('in'):
            ctx_now = int(u['in'])
            yield {'type': 'context', 'used': ctx_now, 'limit': cap, 'exact': True}
        text, calls = ans['text'].strip(), ans['calls']
        if run.cancel.is_set():
            yield {'type': 'notice', 'level': 'info', 'text': 'Stopped.'}
            yield done(step)
            return
        if not calls:
            if not text:
                yield {'type': 'error', 'detail': 'the model returned an empty answer: try again or rephrase'}
                return
            if changed and not verified and EFFORTS[run.effort]['verify']:
                verified = True
                run.msgs.append({'role': 'assistant', 'parts': [{'text': text}]})
                run.msgs.append({'role': 'user', 'parts': [{'text': VERIFY}]})
                yield {'type': 'notice', 'level': 'info', 'text': 'Checking the result…'}
                continue
            yield done(step)
            return

        for k, c in enumerate(calls):           # ids must be unique for the history: some servers send none
            c['id'] = str(c.get('id') or f'call_{step}_{k}')
        parts = [{'thinking': t} for t in ans['thinking'] if isinstance(t, dict) and t.get('sig')]
        run.msgs.append({'role': 'assistant', 'parts': parts + ([{'text': text}] if text else [])
                         + [{'tool_call': c} for c in calls]})

        # control tools that end the turn (the first valid one wins)
        for c in calls:
            t = run.tools.get(c.get('name'))
            if t is None or not t.control or t.name == 'todo':
                continue
            args, err = tools.validate(t, c.get('args'))
            if err:
                continue
            if t.name == 'ask_user':
                yield {'type': 'ask', 'question': args['question'], 'options': [str(o) for o in args['options']][:8]}
            elif t.name == 'cannot_do':
                yield {'type': 'cannot', 'reason': args['reason'], 'alternative': args.get('alternative') or None}
            else:
                yield {'type': 'plan_proposal', 'summary': args['summary'], 'steps': [str(s) for s in args['steps']][:40]}
            yield done(step)
            return

        results = []
        pending = []            # changes in model order: (call, tool, clean, summary, risk)
        bad_change = False
        navigating = False      # a navigate was applied: the page reloads, the turn ends after this step
        for c in calls:
            name, args = c.get('name'), c.get('args')
            t = run.tools.get(name)
            res = {'id': c['id'], 'name': name or '?'}
            if t is None:
                results.append({**res, 'content': _missing_tool(run, name), 'is_error': True})
                continue
            if args is None:
                results.append({**res, 'content': 'the arguments were not a JSON object: call it again', 'is_error': True})
                bad_change |= t.risk in ('change', 'danger', 'run')
                continue
            sig = _sig(c)
            repeats[sig] = repeats.get(sig, 0) + 1
            if repeats[sig] >= REPEAT_STOP:
                yield {'type': 'error', 'detail': f'stopped: the model called {name} with the same arguments '
                                                  f'{REPEAT_STOP} times (it is repeating itself). Rephrase, or try a '
                                                  'larger model / higher effort.'}
                return
            nudge = (' (you already called this with the same arguments: use the result you have, or change approach)'
                     if repeats[sig] >= REPEAT_NUDGE else '')
            if t.name == 'todo':
                clean, err = tools.validate(t, args)
                if err:
                    results.append({**res, 'content': err, 'is_error': True})
                else:
                    yield {'type': 'todo', 'items': clean['items'][:30]}
                    results.append({**res, 'content': 'checklist shown'})
                continue
            if t.control:
                _, err = tools.validate(t, args)
                results.append({**res, 'content': err or 'invalid', 'is_error': True})
                continue
            if t.risk == 'ui':             # checked against the session (feature ids, the method's params, a filter)
                try:
                    clean, _ = tools.prepare(run.ctx, name, args, run.tools)
                except Exception as e:  # noqa: BLE001 - the model fixes it on the next step
                    results.append({**res, 'content': _error_text(e), 'is_error': True})
                    continue
                applied = run.mode == 'auto' or name in ALWAYS_APPLY
                label = t.preview(None, clean) if t.preview else name
                if not applied:
                    yield {'type': 'ui_action', 'id': c['id'], 'action': name, 'args': clean, 'label': label,
                           'applied': False}
                    results.append({**res, 'content': 'shown to the user as an Apply button: do not assume it was '
                                                      'applied'})
                    continue
                if name == 'navigate' or not run.ctx.ui_ack:
                    yield {'type': 'ui_action', 'id': c['id'], 'action': name, 'args': clean, 'label': label,
                           'applied': True}
                    results.append({**res, 'content': 'applied on the page'})
                    navigating = navigating or name == 'navigate'
                    continue
                key = uiwait.open_(run.user, run.run_id, c['id'])
                yield {'type': 'ui_action', 'id': c['id'], 'action': name, 'args': clean, 'label': label,
                       'applied': True, 'wait': True}
                r = yield from _await_page(run, key, UI_ACK_S)
                results.append({**res, **_page_content(r, label)})
                continue
            if t.risk in ('change', 'danger', 'run'):
                try:
                    clean, summary = tools.prepare(run.ctx, name, args, run.tools)
                except Exception as e:  # noqa: BLE001 - the model fixes it on the next step
                    results.append({**res, 'content': _error_text(e), 'is_error': True})
                    bad_change = True
                    continue
                pending.append((c, t, clean, summary, t.risk_of(clean)))
                results.append(None)                        # filled when it runs / is decided
                continue
            if t.confirm_if:                    # a read the user confirms first (web search: Ask before each)
                clean, err = tools.validate(t, args)
                if not err and t.confirm_if(run.ctx, clean):
                    summary = t.preview(None, clean) if t.preview else f'{name} {json.dumps(clean, ensure_ascii=False)}'
                    pending.append((c, t, clean, summary, 'web'))
                    results.append(None)
                    continue
            # read: run at once
            yield {'type': 'tool_start', 'id': c['id'], 'name': name, 'args': args, 'risk': 'read'}
            t1 = time.perf_counter()
            try:
                out, summary = tools.run(run.ctx, name, args, run.tools)
                ok, content = True, _dump(out)
                if t.domain != 'web':           # web results are public text, not internal names
                    _remember_names(run.ctx, out)
            except Exception as e:  # noqa: BLE001 - reported to the model, not raised
                ok, summary, out = False, '→ ' + _error_text(e), None
                content = _error_text(e)
            ms = round((time.perf_counter() - t1) * 1000)
            yield {'type': 'tool_end', 'id': c['id'], 'name': name, 'ok': ok, 'summary': summary, 'ms': ms,
                   'preview': _preview(out) if ok else content}
            yield from _tool_events(run.ctx)
            if name == 'use_skill' and ok:
                yield {'type': 'skill', 'name': args.get('name')}
            results.append({**res, 'content': content + nudge, **({} if ok else {'is_error': True})})

        # run the changes in model order; from the first one that needs the user, the rest go on one card.
        # If one change of the step was wrong, none runs: the model resends the whole set fixed.
        outcome = {}
        need = []
        if bad_change:
            for pc in pending:
                outcome[pc[0]['id']] = {'ok': True, 'content': 'not run: another change in this step failed; send '
                                                               'all changes again, fixed'}
        for pc in [] if bad_change else pending:
            c, t, clean, summary, risk = pc
            if not need and run.mode == 'auto' and risk in ('change', 'run'):
                outcome[c['id']] = yield from _run_change(run, c, t, clean, summary, risk)
                changed = True
            else:
                need.append(pc)
        if need:
            actions = [{'id': f'a{k + 1}', 'call_id': pc[0]['id'], 'tool': pc[1].name, 'args': pc[2],
                        'summary': pc[3], 'risk': pc[4]} for k, pc in enumerate(need)]
            cid = confirm.open_(run.user, run.run_id, actions)
            yield {'type': 'confirm', 'confirm_id': cid, 'actions': actions, 'timeout_s': CONFIRM_TIMEOUT}
            waited, decision = 0, 'pending'
            while decision == 'pending' and waited < CONFIRM_TIMEOUT:
                decision = confirm.wait(cid, run.cancel, PING_S)
                waited += PING_S
                if decision == 'pending':
                    yield {'type': 'ping'}
            if decision == 'pending':
                confirm.cancel_run(cid, 'timeout')
                decision = 'timeout'
            if not isinstance(decision, dict):
                why = 'stopped' if decision == 'cancelled' else 'timed out'
                yield {'type': 'confirmed', 'confirm_id': cid, 'results': [], 'status': why}
                yield {'type': 'notice', 'level': 'info',
                       'text': 'Stopped: nothing was changed.' if why == 'stopped'
                       else f'No answer in {CONFIRM_TIMEOUT // 60} minutes: nothing was changed.'}
                yield done(step)
                return
            res_list = []
            edits = decision.get('edits') or {}
            for a, pc in zip(actions, need):
                if a['id'] in decision['approve'] and a['id'] in edits:
                    try:                            # the user edited the arguments on the card
                        pc = _edited(run, pc, edits[a['id']])
                    except Exception as e:  # noqa: BLE001 - reported to the model, not raised
                        outcome[pc[0]['id']] = {'ok': False, 'content': 'the edited arguments were refused: '
                                                + _error_text(e)}
                        res_list.append({'id': a['id'], 'ok': False, 'summary': _error_text(e)})
                        continue
                if a['id'] in decision['approve']:
                    o = yield from _run_change(run, pc[0], pc[1], pc[2], pc[3], pc[4])
                    outcome[pc[0]['id']] = o
                    changed |= pc[4] != 'web'
                    res_list.append({'id': a['id'], 'ok': o['ok'], 'summary': o['summary'], **o['extras']})
                else:
                    outcome[pc[0]['id']] = {'ok': False, 'content': 'the user declined this action: do not do it '
                                                                    'again unless they ask', 'declined': True}
                    res_list.append({'id': a['id'], 'ok': False, 'declined': True, 'summary': a['summary']})
            yield {'type': 'confirmed', 'confirm_id': cid, 'results': res_list, 'status': 'decided'}
        if changed:
            repeats.clear()                 # the data changed: looking again is not a repeat
        k = 0
        for i, r in enumerate(results):
            if r is None:
                c = pending[k][0]
                k += 1
                o = outcome.get(c['id']) or {'ok': False, 'content': 'not run'}
                results[i] = {'id': c['id'], 'name': c['name'], 'content': o['content'],
                              **({} if o.get('ok') or o.get('declined') else {'is_error': True})}
        run.msgs.append({'role': 'tool', 'parts': [{'tool_result': r} for r in results]})
        if navigating:                  # the page is leaving: the chat continues there (core/dock.ts hand-off)
            yield done(step)
            return
    yield {'type': 'error', 'detail': f'stopped after {run.max_steps} steps without an answer: try a narrower '
                                      'request' + (' or a higher effort' if run.max_steps < MAX_STEPS else '')}


def _edited(run, pc, edit):
    """A pending call with the arguments the user edited on the card, checked again (ToolError when invalid)."""
    c, t, clean, summary, risk = pc
    args = {**clean, **edit}
    if t.risk in ('change', 'danger'):
        clean, summary = tools.prepare(run.ctx, t.name, args, run.tools)
        return c, t, clean, summary, max(risk, t.risk_of(clean), key=('change', 'danger').index)
    if t.risk == 'run':
        clean, summary = tools.prepare(run.ctx, t.name, args, run.tools)
        return c, t, clean, summary, 'run'
    clean, err = tools.validate(t, args)
    if err:
        raise tools.ToolError(err)
    return c, t, clean, t.preview(None, clean) if t.preview else summary, risk


def _await_page(run, key, timeout):
    """Wait for the page's result of a page action (pings keep the stream open) -> the result dict, 'pending'
    (no answer within `timeout`) or 'cancelled'."""
    waited = 0.0
    while True:
        step = min(PING_S, timeout - waited)
        r = uiwait.wait(key, run.cancel, step)
        if r != 'pending':
            return r
        waited += step
        if waited >= timeout:
            uiwait.close(key)
            return 'pending'
        yield {'type': 'ping'}


def _page_content(r, label):
    """A page result -> the tool result the model reads."""
    if r == 'cancelled':
        return {'content': 'stopped by the user before the page answered', 'is_error': True}
    if r == 'pending':
        return {'content': f'{label}: sent to the page, no answer yet (assume it is applied; check with view_state)'}
    if r.get('ok'):
        return {'content': _dump({'done': True, 'detail': r.get('detail'), **({'result': r['result']} if 'result' in r else {})})}
    return {'content': f'the page could not do it: {r.get("detail") or "no reason given"}', 'is_error': True}


def _run_page(run, c, t, clean, summary):
    """An approved / auto run action: the page does the work (its own buttons and job polling) and reports how it
    ended. Transient failures are retried RUN_RETRIES times; a job longer than RUN_WAIT_S reports "still running"."""
    yield {'type': 'tool_start', 'id': c['id'], 'name': t.name, 'args': clean, 'risk': 'run', 'summary': summary}
    t1 = time.perf_counter()
    attempt = 0
    while True:
        aid = c['id'] if not attempt else f'{c["id"]}.r{attempt}'
        ev = {'type': 'ui_action', 'id': aid, 'action': t.name, 'args': clean, 'label': summary, 'applied': True}
        if not run.ctx.ui_ack:                  # a page that cannot answer: started, outcome unknown
            yield ev
            ok, content, done_summary = True, 'started on the page (it does not report results: check the page state)', '→ started'
            break
        key = uiwait.open_(run.user, run.run_id, aid)
        yield {**ev, 'wait': True}
        r = yield from _await_page(run, key, RUN_WAIT_S)
        if r == 'cancelled':
            ok, content, done_summary = False, 'stopped by the user', '→ stopped'
            break
        if r == 'pending':
            ok, done_summary = True, f'→ still running after {RUN_WAIT_S // 60} min'
            content = (f'still running on the page after {RUN_WAIT_S // 60} minutes: tell the user it continues (the '
                       'progress shows on the page) and read the result later (eval_summary / issue_summary)')
            break
        if r.get('ok'):
            ok, done_summary = True, '→ ' + (r.get('detail') or 'done')[:200]
            content = _dump({'done': True, 'detail': r.get('detail'), **({'result': r['result']} if 'result' in r else {})})
            break
        why = r.get('detail') or 'no reason given'
        if r.get('transient') and attempt < RUN_RETRIES:
            attempt += 1
            wait = 2 ** attempt
            yield {'type': 'retry', 'attempt': attempt, 'max': RUN_RETRIES, 'reason': f'{t.name}: {why}'[:300],
                   'wait_s': wait, 'discard': False, 'step': None}
            if _sleep(run.cancel, wait):
                ok, content, done_summary = False, 'stopped by the user', '→ stopped'
                break
            continue
        tries = f' (after {attempt + 1} tries)' if attempt else ''
        ok, done_summary = False, '→ ' + why[:200]
        content = (f'the page could not do it{tries}: {why}. Do not repeat the same call: fix the arguments, or call '
                   'cannot_do with this reason and what the user can do instead.')
        break
    yield {'type': 'tool_end', 'id': c['id'], 'name': t.name, 'ok': ok, 'summary': done_summary,
           'ms': round((time.perf_counter() - t1) * 1000), 'risk': 'run'}
    return {'ok': ok, 'content': content, 'summary': done_summary, 'extras': {}}


def _run_change(run, c, t, clean, summary, risk):
    """Runs one approved / auto change -> {ok, content, summary, extras}; yields its tool events."""
    if risk == 'run':
        return (yield from _run_page(run, c, t, clean, summary))
    yield {'type': 'tool_start', 'id': c['id'], 'name': t.name, 'args': clean, 'risk': risk, 'summary': summary}
    t1 = time.perf_counter()
    try:
        out, done_summary = tools.run(run.ctx, t.name, clean, run.tools)
        errs = out.get('errors') if isinstance(out, dict) else None
        ok, content, ex = not errs, _dump(out), _extras(out)
        if t.domain != 'web':
            _remember_names(run.ctx, out)
    except Exception as e:  # noqa: BLE001 - one failed action does not stop the others
        ok, content, done_summary, ex = False, _error_text(e), '→ ' + _error_text(e), {}
    yield {'type': 'tool_end', 'id': c['id'], 'name': t.name, 'ok': ok, 'summary': done_summary,
           'ms': round((time.perf_counter() - t1) * 1000), 'risk': risk, **ex}
    yield from _tool_events(run.ctx)
    return {'ok': ok, 'content': content, 'summary': done_summary, 'extras': ex}
