# 6. Modes and confirmations

## Modes (chosen by the user per chat; a chip in the UI, `/mode`)

| mode | reads | changes | dangerous | prompt template |
|---|---|---|---|---|
| **ask** (default) | at once | confirm card, then the agent continues with the result | confirm card | `mode_ask.md` |
| **auto** | at once | at once | confirm card | `mode_auto.md` |
| **plan** | at once | not offered | not offered | `mode_plan.md` → `submit_plan` |

Template texts that work:

- **ask**: "read-only tools run at once. Every change you call is shown to the user as a confirmation card and runs
  only when they approve it; you then get its result and continue. Put all the changes a step needs into that step."
- **auto**: "read-only tools and ordinary changes run at once. Dangerous changes (delete, overwrite) still wait for the
  user's confirmation. Work through the whole request; report what you changed at the end with the real names. A big
  request, an unclear one, or one that would change or delete a lot: call `enter_plan_mode` first."
- **plan**: "you can only look things up; nothing can be changed in this mode. Research with the read-only tools,
  then call `submit_plan` with a short summary and the concrete steps (real names). For a pure question, just answer."

Approving a plan sends a message such as "Approved: carry out the plan" in mode auto, with the plan in the dynamic
prompt.

## Confirmation: park the stream, do not end the turn

The stream stays open while the user decides. `ping` events every 15 s keep proxies from closing it. After the decision
the agent continues **in the same turn**, with the real results.

```python
import threading, time, uuid
from collections import OrderedDict

_pending, _lock = OrderedDict(), threading.Lock()
MAX_PENDING, MAX_USER_PENDING = 200, 10


def open_(user_id, run_id, actions):
    cid = uuid.uuid4().hex[:12]
    with _lock:
        mine = [k for k, v in _pending.items() if v['user_id'] == user_id]
        for k in mine[:max(0, len(mine) - MAX_USER_PENDING + 1)]:
            _drop(k, 'cancelled')
        while len(_pending) >= MAX_PENDING:
            _drop(next(iter(_pending)), 'cancelled')
        _pending[cid] = {'user_id': user_id, 'run_id': run_id, 'ids': {a['id'] for a in actions},
                         'event': threading.Event(), 'decision': None, 'created': time.time()}
    return cid


def _drop(cid, why):
    p = _pending.pop(cid, None)
    if p and p['decision'] is None:
        p['decision'] = why
        p['event'].set()


def decide(user_id, cid, approve=(), reject=(), edits=None):
    """POST /confirm. edits = {action id: {arg: value}}: the user edited the arguments on the card."""
    with _lock:
        p = _pending.get(cid)
        if p is None:
            raise KeyError('no such confirmation (expired or answered)')          # -> 404
        if p['user_id'] != user_id:
            raise PermissionError('not yours')                                    # -> 403
        p['decision'] = {'approve': set(approve) & p['ids'], 'reject': set(reject), 'edits': edits or {}}
        p['event'].set()
        _pending.pop(cid)


def wait(cid, cancel, timeout):
    """-> decision dict | 'pending' (call again) | 'cancelled' | 'timeout'."""
    with _lock:
        p = _pending.get(cid)
    if p is None:
        return 'cancelled'
    if p['event'].wait(timeout):
        return p['decision']
    return 'cancelled' if cancel.is_set() else 'pending'
```

In the loop:

```python
cid = confirm.open_(run.user_id, run.run_id, actions)       # actions: [{id:'a1', call_id, tool, args, summary, risk}]
yield {'type': 'confirm', 'confirm_id': cid, 'actions': actions, 'timeout_s': run.confirm_timeout}
waited, decision = 0, 'pending'
while decision == 'pending' and waited < run.confirm_timeout:
    decision = confirm.wait(cid, run.cancel, 15)
    waited += 15
    if decision == 'pending':
        yield {'type': 'ping'}
if not isinstance(decision, dict):                          # stopped / timed out (10 min): nothing changed
    yield {'type': 'confirmed', 'confirm_id': cid, 'results': [], 'status': 'stopped'}
    yield {'type': 'done', 'usage': usage, 'steps': step}
    return
```

## Rules around changes

- **One bad change → none runs.** If any change in the step failed `prepare`, every change of that step gets "not run:
  another change in this step failed; send all changes again, fixed". No half-applied sets.
- **Model order.** In auto, ordinary changes run in order. From the first one that needs the user (a danger), it
  and all later changes of the step go on one card.
- **Edited args** are validated again (`prepare`). If refused, the result says why.
- **Declined** → `{'content': 'the user declined this action: do not do it again unless they ask'}`, no `is_error`.
- **After a change** the loop guard counters reset (looking again is not a repeat now).
- **Undo**: a change returns `undo_id`. The UI shows Undo. The id goes into compaction summaries verbatim.
- In memory and bounded: a waiting confirmation only means something while its stream is open. A multi-process
  deployment needs a shared store (Redis pub/sub) instead of `threading.Event`.
