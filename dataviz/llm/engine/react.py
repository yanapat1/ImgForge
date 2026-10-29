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
from . import compact, confirm, roles

_log = vlog.get_logger('llm')

MAX_STEPS = 50
MAX_RETRIES = 50
MAX_TURNS = 40
MAX_RESULT = 6000               # characters of one tool result fed back to the model
CONFIRM_TIMEOUT = 600
PING_S = 15
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


def history(messages):
    """[{role, text}] from the page -> provider messages (text only, starting with the user's)."""
    out = []
    for m in (messages or [])[-MAX_TURNS:]:
        t = str((m or {}).get('text') or '').strip()
        if t:
            out.append({'role': 'assistant' if m.get('role') == 'assistant' else 'user', 'parts': [{'text': t}]})
    while out and out[0]['role'] != 'user':
        out.pop(0)
    return out


_WORD = re.compile(r'[a-z0-9]+')


def pick_tools(all_tools, question, n=SMALL_TOOLS):
    """A small model gets only the tools that match the request (by name / description words), plus the control
    and lookup tools: fewer choices, fewer wrong calls."""
    always = {'ask_user', 'cannot_do', 'list_buckets', 'list_folder', 'search', 'view_state', 'read_docs', 'use_skill'}
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
            plugin_tools=(), forced_skills=(), limit=None):
    """Everything one run needs, checked before the stream starts (LLMError 400 when it cannot fit)."""
    role = roles.get(role) if isinstance(role, str) else role
    if not msgs or msgs[-1]['role'] != 'user':
        raise LLMError("the last message must be the user's", 400)
    domains = tuple(d for d in role.domains if d != 'web' or opts['web'] > 0)
    plugin = [t for t in plugin_tools if getattr(t, 'roles', None) in (None, (), role.name)
              or role.name in (getattr(t, 'roles', None) or ())]
    tset = tools.for_role(domains, opts['mode'], extra=plugin)
    if opts['profile'] == 'small':
        q = ' '.join(x.get('text', '') for x in msgs[-1]['parts'] if 'text' in x)
        tset = pick_tools(tset, q)
    sk = prompting.skills(role.name, user_skills)
    scope_text = roles.SCOPES[role.name](user, scope)
    system = prompting.build(role.name, tools_catalog=tools.catalog(tset), scope_text=scope_text, mode=opts['mode'],
                             effort=opts['effort'], profile=opts['profile'], skill_list=sk,
                             user_prompts=[p for p in user_prompts if p.get('role') in (None, 'all', role.name)],
                             user_name=(user or {}).get('display_name') or (user or {}).get('username') or '',
                             forced_skills=forced_skills)
    lim = limit or budget.MAX_BUDGET
    fitted, est = budget.fit(provider['kind'], system + json.dumps([t.spec() for t in tset.values()]), [], msgs, lim)
    run_id = uuid.uuid4().hex[:12]
    ctx = Ctx(user=user, role=role.name, scope=scope if isinstance(scope, dict) else {}, skills=sk, state=state,
              app=app, web_level=opts['web'], run_id=run_id)
    p = dict(provider)
    if opts['profile'] == 'small':
        p['temperature'] = min(float(p.get('temperature') or 0.1), 0.1)
    return Run(run_id=run_id, user=user, role=role, provider=p, system=system, msgs=list(fitted), tools=tset, ctx=ctx,
               mode=opts['mode'], effort=opts['effort'], think=opts['think'], web=opts['web'],
               native_web=opts['web'] >= 3, profile=opts['profile'], max_steps=opts['max_steps'], limit=lim, est=est)


# ---------------------------------------------------------------- helpers
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
    yield {'type': 'meta', 'run_id': run.run_id, 'role': run.role.name, 'model': p.get('model'),
           'provider': p.get('name'), 'kind': p.get('kind'), 'mode': run.mode, 'effort': run.effort,
           'think': run.think, 'web': run.web, 'profile': run.profile, 'max_steps': run.max_steps, 'in_est': run.est,
           'tools': len(run.tools)}
    st = {'retries': 0, 'ans': None}
    usage = {'in': 0, 'out': 0}
    t0 = time.perf_counter()
    repeats, changed, verified = {}, False, False
    cap = budget.cap(run.limit)
    kind = p['kind']

    def done(steps):
        if not usage['in']:
            usage['in'] = run.est
        return {'type': 'done', 'usage': dict(usage), 'ms': round((time.perf_counter() - t0) * 1000), 'steps': steps}

    for step in range(1, run.max_steps + 1):
        if run.cancel.is_set():
            yield {'type': 'notice', 'level': 'info', 'text': 'Stopped.'}
            yield done(step - 1)
            return
        if step > 1 and not compact.fit(kind, run.system, run.msgs, cap):
            yield {'type': 'error', 'detail': 'the tool results grew over the token budget: ask for less at once'}
            return
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
        for c in calls:
            name, args = c.get('name'), c.get('args')
            t = run.tools.get(name)
            res = {'id': c['id'], 'name': name or '?'}
            if t is None:
                why = ('that tool belongs to the other assistant: you cannot use it here'
                       if name in tools.REGISTRY else f'unknown tool {name!r}')
                results.append({**res, 'content': f'{why}; your tools are: {", ".join(run.tools)}', 'is_error': True})
                continue
            if args is None:
                results.append({**res, 'content': 'the arguments were not a JSON object: call it again', 'is_error': True})
                bad_change |= t.risk in ('change', 'danger')
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
            if t.risk == 'ui':
                clean, err = tools.validate(t, args)
                if err:
                    results.append({**res, 'content': err, 'is_error': True})
                    continue
                applied = run.mode == 'auto'
                label = t.preview(None, clean) if t.preview else name
                yield {'type': 'ui_action', 'id': c['id'], 'action': name, 'args': clean, 'label': label,
                       'applied': applied}
                results.append({**res, 'content': 'applied on the page' if applied else
                                'shown to the user as an Apply button: do not assume it was applied'})
                continue
            if t.risk in ('change', 'danger'):
                try:
                    clean, summary = tools.prepare(run.ctx, name, args, run.tools)
                except Exception as e:  # noqa: BLE001 - the model fixes it on the next step
                    results.append({**res, 'content': _error_text(e), 'is_error': True})
                    bad_change = True
                    continue
                pending.append((c, t, clean, summary, t.risk_of(clean)))
                results.append(None)                        # filled when it runs / is decided
                continue
            # read: run at once
            yield {'type': 'tool_start', 'id': c['id'], 'name': name, 'args': args, 'risk': 'read'}
            t1 = time.perf_counter()
            try:
                out, summary = tools.run(run.ctx, name, args, run.tools)
                ok, content = True, _dump(out)
                _remember_names(run.ctx, out)
            except Exception as e:  # noqa: BLE001 - reported to the model, not raised
                ok, summary, out = False, '→ ' + _error_text(e), None
                content = _error_text(e)
            ms = round((time.perf_counter() - t1) * 1000)
            yield {'type': 'tool_end', 'id': c['id'], 'name': name, 'ok': ok, 'summary': summary, 'ms': ms,
                   'preview': _preview(out) if ok else content}
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
            if not need and run.mode == 'auto' and risk == 'change':
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
            for a, pc in zip(actions, need):
                if a['id'] in decision['approve']:
                    o = yield from _run_change(run, pc[0], pc[1], pc[2], pc[3], pc[4])
                    outcome[pc[0]['id']] = o
                    changed = True
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
    yield {'type': 'error', 'detail': f'stopped after {run.max_steps} steps without an answer: try a narrower '
                                      'request' + (' or a higher effort' if run.max_steps < MAX_STEPS else '')}


def _run_change(run, c, t, clean, summary, risk):
    """Runs one approved / auto change -> {ok, content, summary, extras}; yields its tool events."""
    yield {'type': 'tool_start', 'id': c['id'], 'name': t.name, 'args': clean, 'risk': risk, 'summary': summary}
    t1 = time.perf_counter()
    try:
        out, done_summary = tools.run(run.ctx, t.name, clean, run.tools)
        errs = out.get('errors') if isinstance(out, dict) else None
        ok, content, ex = not errs, _dump(out), _extras(out)
        _remember_names(run.ctx, out)
    except Exception as e:  # noqa: BLE001 - one failed action does not stop the others
        ok, content, done_summary, ex = False, _error_text(e), '→ ' + _error_text(e), {}
    yield {'type': 'tool_end', 'id': c['id'], 'name': t.name, 'ok': ok, 'summary': done_summary,
           'ms': round((time.perf_counter() - t1) * 1000), 'risk': risk, **ex}
    return {'ok': ok, 'content': content, 'summary': done_summary, 'extras': ex}
