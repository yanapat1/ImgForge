"""
Background agents: long work of a chat runs in its own agent on the server while the chat's own assistant (the lead)
stays free to talk. The lead starts one with `spawn_task` (tools/tasks.py), reads them with `task_status`, answers
their questions with `task_message` and stops them with `stop_task`; the page shows them in the dock's Agents strip
(GET /api/llm/agent/tasks) and sends the lead each result when it is ready (chat body.task_event).

    spawn(lead_ctx, title, goal, mode)   -> the task's status (queued, or running at once)
    status(user, chat_id, tid=None)      -> [status dict]: state, topic, step, done / next steps, tokens, time, eta
    message(user, chat_id, tid, text)    answer a task that asked or planned: it goes on with the answer
    stop(user, chat_id, tid)
    take_result(user, chat_id, tid)      -> the text the lead reads for a finished task (marks it delivered)

A worker is a normal run of a page's specialist (engine/react.py) with these differences: it has no page (no page
action or run action, no navigate), it cannot consult, delegate or spawn (a sub-agent never starts a sub-agent),
its mode is never looser than the chat's, and it ends with `finish_task {summary, show?}`. A change it proposes
waits on a confirmation like any other (engine/confirm.py): the page shows it on the agent's row and the user
decides with the same /api/llm/agent/confirm; the model has no tool that approves for the user.

Limits: AGENTS_MAX running per user (the user's own setting 1..AGENTS_MAX, default AGENTS_DEFAULT), SERVER_MAX on the
whole server, QUEUE_MAX waiting per user. Each running agent holds one thread and one model stream.

In memory (one process, like confirm.py): a server restart drops running agents; a result that was ready is kept
for RESULT_TTL so a reload or another page still delivers it.
"""

import contextvars
import threading
import time
from collections import OrderedDict

from ...core import logging as vlog
from ..providers import LLMError
from ..tools import ToolError
from .planner import approved_message

_log = vlog.get_logger('llm')

AGENTS_MAX = 4                  # running at once per user, at most (the user's setting is 1..AGENTS_MAX)
AGENTS_DEFAULT = 2
SERVER_MAX = 8                  # running at once on the whole server
QUEUE_MAX = 8                   # waiting per user
TASKS_KEPT = 20                 # finished tasks kept per chat
RESULT_TTL = 6 * 3600
CONFIRM_TIMEOUT = 1800          # a worker waits this long for the user (a foreground turn: react.CONFIRM_TIMEOUT)
WORKER_STEPS = 25
RESULT_CHARS = 4000
OUT = ('navigate', 'consult', 'delegate', 'spawn_task', 'task_status', 'task_message', 'stop_task', 'enter_plan_mode')
LEAD_TOOLS = ('spawn_task', 'task_status', 'task_message', 'stop_task')
ACTIVE = ('queued', 'running', 'waiting')
NOTE = ('(You are agent {id} of the assistant, working for {user}. Do only this task. You cannot open pages or start '
        'other agents. For 3 or more steps keep a todo. When done call finish_task: summary = the result with the '
        'exact names and numbers; show = the page and tab where the user should see it, if any. If a choice is the '
        "user's, ask_user.)")

_lock = threading.RLock()
_chats = {}                     # chat id -> OrderedDict(task id -> Task)
_state_locks = {}               # id(visualizer state) -> Lock: its session object is used by one worker at a time


class Task:
    def __init__(self, tid, user, chat_id, role, title, goal, mode, env):
        self.id, self.user, self.chat_id, self.role = tid, user, chat_id, role
        self.title, self.goal, self.mode, self.env = title, goal, mode, env
        self.state = 'queued'
        self.topic = 'waiting for a free agent'
        self.step, self.max_steps = 0, WORKER_STEPS
        self.todo, self.used = [], []
        self.usage = {'in': 0, 'out': 0}
        self.created, self.started, self.ended, self.ran_s = time.time(), None, None, 0.0
        self.result, self.show, self.error, self.ok = None, None, None, True
        self.waiting = None                 # {kind: confirm | ask | plan, ...}: what it needs from the user
        self.history = []                   # [{role, text}] of this task (the goal, its questions, the answers)
        self.cancel = threading.Event()
        self.delivered = False
        self.run_id = None

    # ---- what the page and the lead read
    def status(self):
        now = time.time()
        elapsed = self.ran_s + (now - self.started if self.state == 'running' and self.started else 0)
        closed = [x['text'] for x in self.todo if x.get('state') in ('done', 'skipped')]
        open_ = [x['text'] for x in self.todo if x.get('state') not in ('done', 'skipped')]
        left = len(open_) if self.todo else max(0, self.max_steps - self.step)
        done_n = len(closed) if self.todo else self.step
        # only with a checklist: without one the step limit is an upper bound, not a promise
        eta = round(elapsed / done_n * left) if self.todo and self.state == 'running' and done_n and left else None
        out = {'id': self.id, 'title': self.title, 'role': self.role, 'mode': self.mode, 'state': self.state,
               'topic': self.topic, 'step': self.step, 'max_steps': self.max_steps, 'done': closed[-8:], 'next': open_[:8],
               'tools': self.used[-8:], 'tokens': dict(self.usage), 'elapsed_s': round(elapsed), 'eta_s': eta,
               'delivered': self.delivered}
        if self.waiting:
            out['waiting'] = self.waiting
        if self.result is not None:
            out['result'] = self.result[:RESULT_CHARS]
        if self.show:
            out['show'] = self.show
        if self.error:
            out['error'] = self.error
        return out


def max_running(cfg_agents):
    """The user's own limit (Settings › AI assistant › Sub-agents › Running at once), 1..AGENTS_MAX."""
    try:
        n = int((cfg_agents or {}).get('max_running') or AGENTS_DEFAULT)
    except (TypeError, ValueError):
        n = AGENTS_DEFAULT
    return max(1, min(AGENTS_MAX, n))


def _server_max():
    try:
        from ...auth import settings
        return max(1, int(settings.get('llm_agents_max')))
    except Exception:  # noqa: BLE001 - no such setting / no database: the built-in limit
        return SERVER_MAX


def _mine(uid):
    return [t for ts in _chats.values() for t in ts.values() if t.user.get('id') == uid]


def _get(user, chat_id, tid):
    with _lock:
        t = (_chats.get(str(chat_id or '')) or {}).get(str(tid or '').upper())
    if t is None or t.user.get('id') != (user or {}).get('id'):
        raise ToolError(f'no agent {tid!r} in this chat (task_status lists them)')
    return t


def _prune(chat_id):
    ts = _chats.get(chat_id)
    if not ts:
        return
    now = time.time()
    old = [k for k, t in ts.items() if t.state not in ACTIVE and (now - (t.ended or now) > RESULT_TTL)]
    done = [k for k, t in ts.items() if t.state not in ACTIVE and t.delivered]
    for k in old + done[:max(0, len(done) - TASKS_KEPT)]:
        ts.pop(k, None)


# ---------------------------------------------------------------- start
def spawn(ctx, title, goal, mode=None):
    """Start (or queue) one background agent for the chat of `ctx` (the lead's context) -> its status."""
    env = getattr(ctx, 'task_env', None)
    chat_id = getattr(ctx, 'chat_id', None)
    if getattr(ctx, 'depth', 0) or not env or not chat_id:
        raise ToolError('agents can only be started by the chat\'s own assistant in a saved chat: do this task yourself')
    title = ' '.join(str(title or '').split())[:60]
    goal = str(goal or '').strip()[:4000]
    if not title or len(goal) < 10:
        raise ToolError('give a title (3-5 words) and the whole task as goal (exact names, what to return)')
    lead_mode = getattr(ctx, 'mode', 'ask')
    mode = 'plan' if mode == 'plan' or lead_mode == 'plan' else lead_mode      # never looser than the chat's mode
    uid = ctx.user.get('id')
    with _lock:
        mine = _mine(uid)
        if any(t.state in ACTIVE and t.chat_id == chat_id and t.title.lower() == title.lower() for t in mine):
            raise ToolError(f'an agent "{title}" is already working in this chat: task_status shows it')
        if sum(t.state == 'queued' for t in mine) >= QUEUE_MAX:
            raise ToolError(f'{QUEUE_MAX} agents are already waiting: wait for one to finish (task_status)')
        ts = _chats.setdefault(chat_id, OrderedDict())
        n = max([int(k[1:]) for k in ts] or [0]) + 1
        t = Task(f'T{n}', ctx.user, chat_id, ctx.role, title, goal, mode, env)
        t.history = [{'role': 'user', 'text': goal + '\n\n' + NOTE.format(
            id=t.id, user=ctx.user.get('display_name') or ctx.user.get('username') or 'the user')}]
        ts[t.id] = t
        _prune(chat_id)
        _dispatch()
    return t.status()


def _dispatch():
    """Start queued tasks while the user's and the server's limits allow (lock held)."""
    running = [t for ts in _chats.values() for t in ts.values() if t.state in ('running', 'waiting') and t.started and not t.ended]
    free = _server_max() - len(running)
    for t in sorted((t for ts in _chats.values() for t in ts.values() if t.state == 'queued'), key=lambda x: x.created):
        if free <= 0:
            return
        uid = t.user.get('id')
        if sum(1 for r in running if r.user.get('id') == uid) >= max_running(t.env.get('agents')):
            continue
        _start(t)
        running.append(t)
        free -= 1


def _start(t):
    t.state, t.started, t.ended, t.topic = 'running', time.time(), None, 'starting'
    cv = contextvars.copy_context()             # the signed-in user of the request that spawned or resumed it
    threading.Thread(target=lambda: cv.run(_work, t), daemon=True, name=f'agent-{t.chat_id[:6]}-{t.id}').start()


# ---------------------------------------------------------------- the worker
def _worker_run(t):
    from . import react
    env = t.env
    p = {**env['provider'], 'model': env['sub_model']} if env.get('sub_model') else env['provider']
    opts = react.options({'mode': t.mode, 'effort': 'medium', 'web': env.get('web', 0)}, p)
    opts['max_steps'] = min(opts['max_steps'], WORKER_STEPS)
    run = react.prepare(t.user, t.role, p, react.history(t.history), env.get('scope') or {}, opts,
                        state=env.get('state'), app=env.get('app'), user_prompts=env.get('prompts') or (),
                        user_skills=env.get('skills') or (), plugin_tools=env.get('plugin_tools') or (),
                        limit=env.get('limit'), worker=True)
    run.cancel = t.cancel
    run.ctx.cancel = t.cancel
    run.ctx.depth = 1
    run.ctx.web_cfg = env.get('web_cfg')
    run.ctx.roles_on = env.get('roles_on')
    run.confirm_timeout = CONFIRM_TIMEOUT
    return run


def _work(t):
    from . import react
    texts, asked = [], None
    st = t.env.get('state')
    slock = _state_locks.setdefault(id(st), threading.Lock()) if st is not None else None
    try:
        if slock:
            slock.acquire()
        run = _worker_run(t)
        t.run_id, t.max_steps = run.run_id, run.max_steps
        for ev in react.events(run):
            k = ev.get('type')
            if k == 'step':
                t.step, texts = t.step + 1, []
                t.topic = _topic(t) or 'thinking'
            elif k == 'text':
                texts.append(str(ev.get('text') or ''))
            elif k == 'todo':
                t.todo = [x for x in ev.get('items') or [] if isinstance(x, dict) and x.get('text')][:30]
                t.topic = _topic(t) or t.topic
            elif k == 'tool_start':
                t.used.append(ev.get('name'))
                t.topic = _topic(t) or str(ev.get('summary') or ev.get('name') or '')[:60]
            elif k == 'confirm':
                t.waiting = {'kind': 'confirm', 'confirm_id': ev.get('confirm_id'), 'actions': [
                    {'id': a['id'], 'tool': a['tool'], 'summary': a.get('summary') or a['tool'], 'risk': a.get('risk')}
                    for a in ev.get('actions') or []]}
                t.state, t.topic = 'waiting', 'waiting for your OK'
            elif k == 'confirmed':
                t.waiting = None
                if ev.get('status') == 'decided':
                    t.state = 'running'
                else:
                    t.error = 'nobody answered its confirmation: nothing was changed' if ev.get('status') != 'stopped' else None
            elif k == 'ask':
                asked = {'kind': 'ask', 'question': ev.get('question'), 'options': ev.get('options') or []}
            elif k == 'plan_proposal':
                asked = {'kind': 'plan', 'summary': ev.get('summary'), 'steps': ev.get('steps') or []}
            elif k == 'task_result':
                t.result, t.show = str(ev.get('summary') or ''), ev.get('show') or None
            elif k == 'cannot':
                t.ok = False
                t.result = f'It could not do it: {ev.get("reason")}' + (f' Instead: {ev["alternative"]}' if ev.get('alternative') else '')
            elif k == 'error':
                t.error = str(ev.get('detail') or 'failed')[:500]
            elif k == 'done':
                u = ev.get('usage') or {}
                for x in ('in', 'out'):
                    t.usage[x] += int(u.get(x) or 0)
    except (LLMError, ToolError) as e:
        t.error = str(e)[:500]
    except Exception as e:  # noqa: BLE001 - a worker never takes the server down
        _log.warning(f'agent {t.id}: {type(e).__name__}: {vlog.esc(str(e))}')
        t.error = f'{type(e).__name__}: {e}'[:500]
    finally:
        if slock:
            slock.release()
        with _lock:
            t.ran_s += time.time() - (t.started or time.time())
            t.ended = time.time()
            said = ''.join(texts).strip()
            if t.cancel.is_set():
                t.state, t.topic, t.waiting = 'stopped', 'stopped', None
            elif t.error:
                t.state, t.topic, t.ok, t.waiting = 'failed', 'failed', False, None
            elif asked:                         # its run ended on a question or a plan: message() continues it
                if said:
                    t.history.append({'role': 'assistant', 'text': said})
                t.history.append({'role': 'assistant', 'text': _asked_text(asked)})
                t.state, t.waiting = 'waiting', asked
                t.topic = 'asks you a question' if asked['kind'] == 'ask' else 'has a plan for you to approve'
            else:
                t.state, t.topic, t.waiting = 'done', 'done', None
                if t.result is None:
                    t.result = said or '(it ended without a result)'
            _dispatch()


def _topic(t):
    """What it is doing now: the active checklist step (else the first open one)."""
    for want in ('active', None):
        for x in t.todo:
            if (x.get('state') == want) if want else x.get('state') not in ('done', 'skipped'):
                return str(x['text'])[:60]
    return ''


def _asked_text(a):
    if a['kind'] == 'ask':
        return f'{a["question"]}' + (f' ({" / ".join(map(str, a["options"]))})' if a.get('options') else '')
    return 'Proposed plan: ' + str(a.get('summary') or '') + '\n' + '\n'.join(f'{i}. {s}' for i, s in enumerate(a.get('steps') or [], 1))


# ---------------------------------------------------------------- what the lead and the page do with them
def status(user, chat_id, tid=None):
    if tid:
        return [_get(user, chat_id, tid).status()]
    with _lock:
        _prune(str(chat_id or ''))
        ts = list((_chats.get(str(chat_id or '')) or {}).values())
    return [t.status() for t in ts if t.user.get('id') == (user or {}).get('id')]


def stop(user, chat_id, tid):
    t = _get(user, chat_id, tid)
    with _lock:
        if t.state not in ACTIVE:
            raise ToolError(f'{t.id} already ended ({t.state})')
        t.cancel.set()
        if t.state == 'queued' or (t.state == 'waiting' and t.ended):      # no thread is running it
            t.state, t.topic, t.waiting, t.ended = 'stopped', 'stopped', None, time.time()
            _dispatch()
    return t.status()


def message(user, chat_id, tid, text, approve_plan=False):
    """The user's answer to a task that asked a question or proposed a plan: it goes on with it."""
    t = _get(user, chat_id, tid)
    text = str(text or '').strip()[:2000]
    with _lock:
        w = t.waiting
        if t.state != 'waiting' or not w or w['kind'] == 'confirm' or not t.ended:
            if w and w['kind'] == 'confirm':
                raise ToolError(f'{t.id} waits for the user\'s OK on a change: only they can give it, with the buttons '
                                'on its row in the Agents strip. Tell them what it asks.')
            raise ToolError(f'{t.id} is not waiting for an answer ({t.state}): stop_task and spawn_task again to change its goal')
        if w['kind'] == 'plan' and (approve_plan or not text):
            lead_mode = t.env.get('mode') or 'ask'
            t.mode = 'ask' if lead_mode == 'plan' else lead_mode
            text = approved_message(w.get('summary') or '', w.get('steps') or [], text if approve_plan else '')
        elif not text:
            raise ToolError('text is required: the answer to its question')
        t.history.append({'role': 'user', 'text': text})
        t.waiting, t.cancel = None, threading.Event()
        t.state, t.topic = 'queued', 'waiting for a free agent'
        _dispatch()
    return t.status()


def take_result(user, chat_id, tid):
    """The message the lead reads for a task that ended (done, failed, stopped) or needs the user; a finished one is
    marked delivered."""
    t = _get(user, chat_id, tid)
    s = t.status()
    took = f'{s["elapsed_s"] // 60} min {s["elapsed_s"] % 60} s, {s["step"]} steps, {s["tokens"]["in"] + s["tokens"]["out"]:,} tokens'
    head = f'[task {t.id} "{t.title}"'
    if t.state == 'waiting' and t.waiting:
        w = t.waiting
        what = ('waits for the user\'s OK (the buttons on its row) on: ' + '; '.join(a['summary'] for a in w['actions'])
                if w['kind'] == 'confirm' else 'asks: ' + _asked_text(w))
        return f'{head} needs the user] It {what}'
    if t.state in ACTIVE:
        raise ToolError(f'{t.id} is still {t.state}')
    with _lock:
        t.delivered = True
    if t.state == 'done':
        import json
        return (f'{head} done in {took}]\n{(t.result or "")[:RESULT_CHARS]}'
                + (f'\nshow: {json.dumps(t.show, ensure_ascii=False)}' if t.show else ''))
    return f'{head} {t.state} after {took}] {t.error or "the user stopped it"}'


def now_text(user, chat_id):
    """The lead's "Agents" lines under Now (only while a task is active or has an undelivered result)."""
    rows = []
    for s in status(user, chat_id):
        if s['state'] not in ACTIVE and s['delivered']:
            continue
        w = s.get('waiting')
        extra = (' · waits for the user: ' + ('their OK on its row' if w['kind'] == 'confirm' else _asked_text(w)[:200])) if w else ''
        rows.append(f'- {s["id"]} "{s["title"]}": {s["state"]} · {s["topic"]} · step {s["step"]}/{s["max_steps"]}{extra}')
    return ('## Agents of this chat\n' + '\n'.join(rows)) if rows else ''


def reset():
    """Tests: forget every task."""
    with _lock:
        for ts in _chats.values():
            for t in ts.values():
                t.cancel.set()
        _chats.clear()
