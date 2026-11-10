"""
Sub-agents: `delegate` splits independent read-only look-ups across copies of this page's specialist, each in its own
short run (engine/subrun.py). Their tool results stay in those runs and only the answers come back, so a long task
keeps the main chat small. Offered only when the user switched Sub-agents on (Settings › AI assistant, /agents) and
never to a small model; a sub-agent cannot delegate or consult again.

    delegate {tasks: [{task, hint?}]}   1-4 tasks, run at the same time (one after another for the visualizer,
                                        whose session object is not shared between threads)
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor

from . import S, Tool, ToolError, _obj, register

MAX_TASKS, SUB_STEPS, SUB_CHARS = 4, 8, 2000
NOTE = ('(You are a sub-agent of the assistant, for the user. Do only this task with your read-only tools and answer '
        'short, with the exact names, ids and numbers found. You cannot change anything or open pages.)')


def _text(t):
    hint = str(t.get('hint') or '').strip()[:300]
    return f'{str(t["task"]).strip()[:1500]}' + (f'\nWhere to look: {hint}' if hint else '') + f'\n\n{NOTE}'


def _delegate(ctx, a):
    from ..engine import subrun
    if getattr(ctx, 'depth', 0):
        raise ToolError('a sub-agent cannot delegate: do it with your own tools')
    if not getattr(ctx, 'provider', None):
        raise ToolError('no model to run sub-agents on')
    tasks = [t for t in a.get('tasks') or [] if isinstance(t, dict) and str(t.get('task') or '').strip()]
    if not tasks:
        raise ToolError('give 1-4 tasks, each {task, hint?}')
    if len(tasks) > MAX_TASKS:
        raise ToolError(f'at most {MAX_TASKS} tasks at once: merge some, or delegate again after these')
    p = {**ctx.provider, 'model': ctx.sub_model} if getattr(ctx, 'sub_model', '') else ctx.provider

    def one(t):
        try:
            return subrun.run_sub(ctx, ctx.role, _text(t), state=ctx.state, scope=ctx.scope, steps=SUB_STEPS,
                                  chars=SUB_CHARS, provider=p, label='sub-agent')
        except ToolError as e:
            return {'error': str(e), 'ok': False, 'steps': 0, 'tools': [], 'usage': {'in': 0, 'out': 0}}

    if ctx.role == 'visual' or len(tasks) == 1:
        got = [one(t) for t in tasks]
    else:                                   # each thread gets the request's contextvars (the signed-in user)
        with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix='subagent') as ex:
            got = list(ex.map(lambda t: contextvars.copy_context().run(one, t), tasks))
    out = []
    for t, g in zip(tasks, got):            # back on the calling thread: ctx is written here only
        for k in ('in', 'out'):
            ctx.sub_usage[k] = ctx.sub_usage.get(k, 0) + g['usage'][k]
        ok = 'error' not in g and g['ok']
        ctx.events.append({'type': 'agent', 'task': str(t['task'])[:200], 'ok': ok, 'steps': g['steps'], 'tools': g['tools'],
                           'in': g['usage']['in'], 'out': g['usage']['out']})
        out.append({'task': str(t['task'])[:200], 'ok': ok, **({'error': g['error']} if 'error' in g else {'answer': g['answer']})})
    n_ok = sum(1 for x in out if x['ok'])
    return {'results': out}, f'→ {n_ok}/{len(out)} sub-agent{"" if len(out) == 1 else "s"} answered'


register(Tool('delegate', 'Run 1-4 independent read-only look-ups at once, each by a sub-agent with its own context; only '
                          'their short answers come back. Each task must stand alone (names, ids, what to return).',
              _obj({'tasks': {'type': 'array', 'description': 'the look-ups, 1-4', 'items': {
                  'type': 'object', 'properties': {'task': S('the whole task, self-contained'),
                                                   'hint': S('where to look: table, folder, tab ...')},
                  'required': ['task'], 'additionalProperties': False}}}, ['tasks']),
              False, _delegate, domain='common', ctx=True))
