"""
The lead's tools for background agents (engine/bgtasks.py): long work runs in its own agent while the chat's
assistant keeps talking. Offered only when the user switched Sub-agents on, in a saved chat, never to a small model
and never to a worker (a sub-agent cannot start a sub-agent).

    spawn_task {title, goal, mode?}   start one (queued when the user's limit of running agents is reached)
    task_status {id?}                 what each did, does now, has left; time, about how long is left, tokens
    task_message {id, text}           the user's answer to its question, or their go-ahead / change to its plan
    stop_task {id}

A change a worker proposes is approved by the user on its row (the Agents strip): no tool here approves for them.
"""

from . import E, S, Tool, _obj, register


def _fmt(s):
    m, sec = divmod(int(s['elapsed_s']), 60)
    out = {'id': s['id'], 'title': s['title'], 'state': s['state'], 'doing_now': s['topic'],
           'step': f'{s["step"]}/{s["max_steps"]}', 'done': s['done'], 'left_to_do': s['next'],
           'time_used': f'{m} min {sec} s', 'tokens': s['tokens']['in'] + s['tokens']['out']}
    if s.get('eta_s') is not None:
        out['about_left'] = f'{max(1, round(s["eta_s"] / 60))} min'
    for k in ('waiting', 'result', 'show', 'error'):
        if s.get(k):
            out[k] = s[k]
    return out


def _spawn(ctx, a):
    from ..engine import bgtasks
    s = bgtasks.spawn(ctx, a['title'], a['goal'], a.get('mode'))
    how = 'started' if s['state'] == 'running' else 'queued: it starts when an agent is free'
    return {'id': s['id'], 'title': s['title'], 'state': s['state'], 'mode': s['mode']}, f'→ {s["id"]} {how}'


def _status(ctx, a):
    from ..engine import bgtasks
    got = [_fmt(s) for s in bgtasks.status(ctx.user, getattr(ctx, 'chat_id', None), a.get('id'))]
    live = sum(1 for s in got if s['state'] in bgtasks.ACTIVE)
    return {'agents': got}, f'→ {len(got)} agent{"" if len(got) == 1 else "s"}, {live} working'


def _message(ctx, a):
    from ..engine import bgtasks
    s = bgtasks.message(ctx.user, getattr(ctx, 'chat_id', None), a['id'], a['text'])
    return {'id': s['id'], 'state': s['state']}, f'→ {s["id"]} goes on'


def _stop(ctx, a):
    from ..engine import bgtasks
    s = bgtasks.stop(ctx.user, getattr(ctx, 'chat_id', None), a['id'])
    return {'id': s['id'], 'state': s['state']}, f'→ {s["id"]} stopped'


_C = dict(domain='common', ctx=True)
register(Tool('spawn_task', 'Start a background agent for work of 3 or more steps, so you stay free to talk. It works '
                            'with this page\'s tools on the server (it cannot press buttons on a page) and reports back.',
              _obj({'title': S('3-5 words the user sees'),
                    'goal': S('the whole task: exact names and paths, what to return, what must not be touched'),
                    'mode': E('plan = it only researches and proposes a plan; else it follows this chat\'s mode', ['plan', 'chat'])},
                   ['title', 'goal']), False, _spawn, **_C))
register(Tool('task_status', 'The background agents of this chat: for each what it did, what it does now, what is left, '
                             'time used, about how long is left, tokens, and what it waits for.',
              _obj({'id': S('one agent (T1 ...); empty = all')}), False, _status, **_C))
register(Tool('task_message', 'Give a waiting agent the user\'s answer to its question, or their go-ahead on its plan '
                              '(with any change they asked for). Not for approving a change: the user does that on its row.',
              _obj({'id': S('the agent (T1 ...)'), 'text': S('the user\'s answer, in their words')}, ['id', 'text']),
              False, _message, **_C))
register(Tool('stop_task', 'Stop a background agent.', _obj({'id': S('the agent (T1 ...)')}, ['id']), False, _stop, **_C))
