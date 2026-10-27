"""
The Data Center agent: a short native tool-calling loop over llm/tools.py.

    run(user, provider, messages, scope)  -> iterator of the NDJSON events of /api/llm/dc/chat (datacenter/API.md)
    confirm(user, plan_id, action_ids)    -> {results, retagged}: runs only the actions the user checked

Each step asks the provider once (providers.complete_tools). Read-only calls run at once and their compact
results go back to the model; ask_user / cannot_do end the turn with an 'ask' / 'cannot' event; any change
(move, delete, tag, send ...) is never run here: the step's changes become a plan (kept in memory for 15 min,
for its user only) that the page shows with checkboxes, and nothing happens until confirm(). A mutating call
the model got wrong (unknown bucket, an S3 bucket, bad args) goes back to it as an error so it can fix it,
instead of reaching the user as a broken plan.
"""

import json
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from ..core import logging as vlog
from . import budget, providers, tools
from .providers import LLMError

_log = vlog.get_logger('llm')
MAX_STEPS = 8
PLAN_TTL = 15 * 60
MAX_PLANS = 100                 # kept at once (oldest dropped first) ...
MAX_USER_PLANS = 10             # ... and per user, so one busy user cannot push out everyone else's
MAX_TURNS = 40
MAX_RESULT = 6000               # characters of one tool result fed back to the model
PROMPT = Path(__file__).with_name('prompts') / 'datacenter.md'

_plans = OrderedDict()          # plan_id -> {user_id, actions, created}
_plans_lock = threading.Lock()


class AgentError(ValueError):
    """A confirm problem with an HTTP status for the route (404 / 403 / 410 / 400)."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- prompt
def _scope_text(user, scope):
    """Where the user is on the page (the bucket's name looked up, best effort): it makes "this folder" resolvable."""
    scope = scope if isinstance(scope, dict) else {}
    bid, path = scope.get('bucket'), str(scope.get('path') or '')
    sel = [str(x) for x in scope.get('selection') or [] if isinstance(x, str)]
    if not bid:
        return 'The user is on the Data Center overview (no bucket open).'
    name = None
    try:
        b = tools._svc().get_bucket(user, bid)
        name = b.get('name')
        kind = b.get('kind')
    except Exception:  # noqa: BLE001 - a stale scope: the model still has the id and the tools
        kind = None
    out = [f'The user has bucket {bid!r}' + (f' ({name!r}, {kind})' if name else '') + ' open, folder '
           + (repr(path) if path else 'the root') + '.']
    if sel:
        shown = sel[:50]
        out.append(f'Selected ({len(sel)}): ' + ', '.join(shown) + (' ...' if len(sel) > len(shown) else '')
                   + '. "These" / "the selection" means these paths.')
    return ' '.join(out)


def system_prompt(user, scope):
    return (PROMPT.read_text(encoding='utf-8').strip() + '\n\n## Tools\n' + tools.catalog()
            + '\n\n## Now\n' + _scope_text(user, scope))


def _history(messages):
    out = []
    for m in (messages or [])[-MAX_TURNS:]:
        t = str((m or {}).get('text') or '').strip()
        if t:
            out.append({'role': 'assistant' if m.get('role') == 'assistant' else 'user', 'parts': [{'text': t}]})
    while out and out[0]['role'] != 'user':
        out.pop(0)
    return out


def prepare(user, provider, messages, scope, limit=None):
    """{system, msgs, est} of a request, trimmed to the budget (LLMError 400 when it cannot fit): the route calls
    it before it starts streaming so a too-large request is a plain 400."""
    system = system_prompt(user, scope)
    hist = _history(messages)
    if not hist or hist[-1]['role'] != 'user':
        raise LLMError("the last message must be the user's", 400)
    msgs, est = budget.fit(provider['kind'], system + json.dumps(tools.specs()), [], hist, limit or budget.MAX_BUDGET)
    return {'system': system, 'msgs': msgs, 'est': est}


def _est(kind, system, msgs):
    """budget.estimate plus the tool parts (it only counts text / media)."""
    extra = 0
    for m in msgs:
        for x in m.get('parts') or []:
            if 'tool_call' in x:
                extra += budget.text_tokens(json.dumps(x['tool_call'].get('args'))) + 8
            elif 'tool_result' in x:
                extra += budget.text_tokens(x['tool_result'].get('content')) + 8
    return budget.estimate(kind, system, msgs) + extra


# ---------------------------------------------------------------- plans
def _store_plan(user, actions):
    now = time.time()
    pid = uuid.uuid4().hex[:12]
    with _plans_lock:
        for k in [k for k, v in _plans.items() if now - v['created'] > PLAN_TTL]:
            del _plans[k]
        uid = (user or {}).get('id')
        mine = [k for k, v in _plans.items() if v['user_id'] == uid]
        for k in mine[:max(0, len(mine) - MAX_USER_PLANS + 1)]:
            del _plans[k]
        while len(_plans) >= MAX_PLANS:
            _plans.popitem(last=False)
        _plans[pid] = {'user_id': uid, 'actions': actions, 'created': now}
    return pid


def _dump(result):
    s = json.dumps(result, ensure_ascii=False, default=str, separators=(',', ':'))
    return s if len(s) <= MAX_RESULT else s[:MAX_RESULT] + '… (cut: narrow the request)'


def _error_text(e):
    if isinstance(e, tools.ToolError):
        return str(e)
    status = getattr(e, 'status', None)
    if status is not None or type(e).__name__ == 'DCError':           # the service's own human messages
        return str(e)
    _log.warning(f'dc tool: {type(e).__name__}: {vlog.esc(str(e))}')
    return f'{type(e).__name__}: {e}'


# ---------------------------------------------------------------- the loop
def run(user, provider, messages, scope, limit=None, prepared=None, max_steps=MAX_STEPS):
    """The events of one chat turn (dicts; the route writes one JSON line each). Provider errors propagate:
    the route turns them into an 'error' event."""
    prep = prepared or prepare(user, provider, messages, scope, limit)
    system, msgs, est = prep['system'], list(prep['msgs']), prep['est']
    kind = provider['kind']
    cap = budget.cap(limit or budget.MAX_BUDGET)
    specs = tools.specs()
    yield {'type': 'meta', 'model': provider.get('model'), 'in_est': est}
    usage = {'in': 0, 'out': 0}
    t0 = time.perf_counter()

    def done():
        if not usage['in']:
            usage['in'] = est
        return {'type': 'done', 'usage': usage, 'ms': round((time.perf_counter() - t0) * 1000)}

    for step in range(max_steps):
        if step and _est(kind, system, msgs) > cap:
            yield {'type': 'error', 'detail': 'the tool results grew over the token budget: ask for less at once'}
            return
        ans = providers.complete_tools(provider, system, msgs, specs)
        u = ans.get('usage') or {}
        usage['in'] += int(u.get('in') or 0)
        usage['out'] += int(u.get('out') or 0)
        text, calls = (ans.get('text') or '').strip(), ans.get('tool_calls') or []
        if text:
            yield {'type': 'delta', 'text': text}
        if not calls:
            if not text:
                yield {'type': 'error', 'detail': 'the model returned an empty answer: try again or rephrase'}
                return
            yield done()
            return
        for k, c in enumerate(calls):           # ids must be unique for the history: some servers send none
            c['id'] = str(c.get('id') or f'call_{step}_{k}')
        msgs.append({'role': 'assistant', 'parts': ([{'text': text}] if text else [])
                     + [{'tool_call': c} for c in calls]})

        # control tools end the turn (the first valid one wins)
        for c in calls:
            t = tools.TOOLS.get(c.get('name'))
            if t is None or not t.control:
                continue
            args, err = tools.validate(t, c.get('args'))
            if err:
                continue
            if t.name == 'ask_user':
                yield {'type': 'ask', 'question': args['question'], 'options': [str(o) for o in args['options']][:8]}
            else:
                yield {'type': 'cannot', 'reason': args['reason'], 'alternative': args.get('alternative') or None}
            yield done()
            return

        results, plan, bad_change = [], [], False
        for c in calls:
            name, args = c.get('name'), c.get('args')
            t = tools.TOOLS.get(name)
            res = {'id': c['id'], 'name': name or '?'}
            if t is None:
                results.append({**res, 'content': f'unknown tool {name!r}; the tools are: {", ".join(tools.TOOLS)}',
                                'is_error': True})
                continue
            if args is None:
                results.append({**res, 'content': 'the arguments were not a JSON object: call it again', 'is_error': True})
                bad_change |= t.mutating
                continue
            if t.control:                                       # invalid ask_user / cannot_do args
                _, err = tools.validate(t, args)
                results.append({**res, 'content': err or 'invalid', 'is_error': True})
                continue
            if t.mutating:
                try:
                    clean, summary = tools.prepare(user, name, args)
                except Exception as e:  # noqa: BLE001 - the model fixes it on the next step
                    results.append({**res, 'content': _error_text(e), 'is_error': True})
                    bad_change = True
                    continue
                plan.append({'id': f'a{len(plan) + 1}', 'tool': name, 'args': clean, 'summary': summary})
                results.append({**res, 'content': 'not run: proposed to the user for confirmation'})
                continue
            try:
                out, summary = tools.run(user, name, args)
                ok, content = True, _dump(out)
            except Exception as e:  # noqa: BLE001 - reported to the model, not raised
                ok, summary = False, '→ ' + _error_text(e)
                content = _error_text(e)
            yield {'type': 'tool', 'id': c['id'], 'name': name, 'args': args, 'summary': summary, 'ok': ok}
            results.append({**res, 'content': content, **({} if ok else {'is_error': True})})

        if plan and not bad_change:
            yield {'type': 'plan', 'plan_id': _store_plan(user, plan), 'actions': plan}
            yield done()
            return
        if plan:            # some changes were wrong: none is proposed, the model resends the whole set fixed
            for r in results:
                if r.get('content') == 'not run: proposed to the user for confirmation':
                    r['content'] = 'not proposed: another change in this step failed; send all changes again, fixed'
        msgs.append({'role': 'tool', 'parts': [{'tool_result': r} for r in results]})
    yield {'type': 'error', 'detail': f'stopped after {max_steps} tool steps without an answer: try a narrower request'}


def confirm(user, plan_id, action_ids):
    """Run the checked actions of a plan, in plan order -> {results: [{id, ok, summary, error?}], retagged: [..]}.
    A plan runs once (it is removed), belongs to its user and expires after PLAN_TTL."""
    key = str(plan_id or '')
    with _plans_lock:
        plan = _plans.get(key)
        if plan is None:
            raise AgentError('no such plan (plans expire after 15 minutes): ask again', 404)
        if plan['user_id'] != (user or {}).get('id'):
            raise AgentError('this plan belongs to another user', 403)
        if time.time() - plan['created'] > PLAN_TTL:
            del _plans[key]
            raise AgentError('this plan expired (15 minutes): ask again', 410)
        ids = {str(i) for i in action_ids or []}
        unknown = ids - {a['id'] for a in plan['actions']}
        if unknown:
            raise AgentError(f'no action {", ".join(sorted(unknown))} in this plan', 400)
        if not ids:
            raise AgentError('check at least one action', 400)
        del _plans[key]
    results, retagged = [], []
    for a in plan['actions']:
        if a['id'] not in ids:
            continue
        try:
            out, summary = tools.run(user, a['tool'], a['args'])
        except Exception as e:  # noqa: BLE001 - one failed action does not stop the others
            results.append({'id': a['id'], 'ok': False, 'summary': a['summary'], 'error': _error_text(e)})
            continue
        errs = out.get('errors') if isinstance(out, dict) else None
        r = {'id': a['id'], 'ok': not errs, 'summary': summary}
        if errs:
            r['error'] = '; '.join(f'{e.get("path")}: {e.get("error")}' for e in errs[:5])
        if isinstance(out, dict) and out.get('undo_id'):
            r['undo_id'] = out['undo_id']
            r['bucket'] = a['args'].get('bucket')
        if isinstance(out, dict) and out.get('url'):
            r['url'] = out['url']
        results.append(r)
        if isinstance(out, dict) and out.get('retagged'):
            retagged.append({'bucket': a['args'].get('bucket'), **out['retagged']})
    return {'results': results, 'retagged': retagged}
