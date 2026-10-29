"""
Pause / resume for confirmations: a change the user must approve parks the agent's stream on a threading.Event
until POST /api/llm/agent/confirm decides it (or it times out, or the user stops the run). The stream stays open
(ping events keep proxies from closing it), so the agent continues with the results in the same turn instead of
ending it with a plan.

    cid = open_(user, run_id, actions)       -> id shown on the confirm card
    decide(user, cid, approve, reject, edits) -> wakes the waiting run (AgentError 404 / 403 / 400); edits =
                                               {action id: {arg: value}}: run that approved action with these
                                               arguments instead (the user edited a web query on the card)
    wait(cid, cancel, timeout)               -> {'approve': set, 'reject': set, 'edits': {id: args}} | 'pending'
                                               | 'timeout' | 'cancelled'

In memory, per user, bounded: the app is one process (python app.py), and a waiting confirm is only meaningful
while its stream is open.
"""

import threading
import time
import uuid
from collections import OrderedDict

MAX_PENDING = 200
MAX_USER_PENDING = 10

_pending = OrderedDict()            # cid -> {user_id, run_id, ids, event, decision, created}
_lock = threading.Lock()


class AgentError(ValueError):
    """A confirm / stop problem with an HTTP status for the route."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def open_(user, run_id, actions):
    uid = (user or {}).get('id')
    cid = uuid.uuid4().hex[:12]
    with _lock:
        mine = [k for k, v in _pending.items() if v['user_id'] == uid]
        for k in mine[:max(0, len(mine) - MAX_USER_PENDING + 1)]:
            _drop(k, 'cancelled')
        while len(_pending) >= MAX_PENDING:
            _drop(next(iter(_pending)), 'cancelled')
        _pending[cid] = {'user_id': uid, 'run_id': run_id, 'ids': {a['id'] for a in actions},
                         'event': threading.Event(), 'decision': None, 'created': time.time()}
    return cid


def _drop(cid, why):
    """Remove a pending confirm, waking its run with `why` (lock held)."""
    p = _pending.pop(cid, None)
    if p and p['decision'] is None:
        p['decision'] = why
        p['event'].set()


def decide(user, cid, approve=(), reject=(), edits=None):
    approve = {str(i) for i in approve or []}
    reject = {str(i) for i in reject or []} - approve
    if edits is not None and not (isinstance(edits, dict) and all(isinstance(v, dict) for v in edits.values())):
        raise AgentError('edits must be an object {action id: {argument: value}}', 400)
    edits = {str(k): dict(v) for k, v in (edits or {}).items()}
    with _lock:
        p = _pending.get(str(cid or ''))
        if p is None:
            raise AgentError('nothing waits for this confirmation any more (the answer ended or was stopped)', 404)
        if p['user_id'] != (user or {}).get('id'):
            raise AgentError('this confirmation belongs to another user', 403)
        unknown = (approve | reject | set(edits)) - p['ids']
        if unknown:
            raise AgentError(f'no action {", ".join(sorted(unknown))} here', 400)
        p['decision'] = {'approve': approve, 'reject': p['ids'] - approve,
                         'edits': {k: v for k, v in edits.items() if k in approve}}
        del _pending[cid]
        p['event'].set()
    return {'ok': True}


def wait(cid, cancel, timeout):
    """One wait of at most `timeout` seconds: the decision, 'pending' (call again; the run pings meanwhile),
    or 'cancelled' when the run was stopped."""
    with _lock:
        p = _pending.get(cid)
    if p is None:
        return 'cancelled'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if p['event'].wait(min(0.25, max(0.0, deadline - time.monotonic()))):
            return p['decision']
        if cancel is not None and cancel.is_set():
            cancel_run(cid)
            return 'cancelled'
    return 'pending'


def cancel_run(cid, why='cancelled'):
    with _lock:
        _drop(cid, why)


def pending_for(user):
    uid = (user or {}).get('id')
    with _lock:
        return [k for k, v in _pending.items() if v['user_id'] == uid]
