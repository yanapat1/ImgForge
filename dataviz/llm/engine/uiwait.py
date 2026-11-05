"""
Page results of the agent's page actions: a `ui_action` the page applies (open a tab, run a model, generate a map,
scan) reports back through POST /api/llm/agent/ui-result, so the agent knows whether it really happened (and what
came out) instead of assuming it. The run waits on a threading.Event like a confirmation (engine/confirm.py).

    key = open_(user, run_id, action_id)          before the ui_action event is sent
    deliver(user, run_id, action_id, result)      the page's answer {ok, detail, transient?, result?} (AgentError
                                                  404 unknown / ended, 403 another user's run)
    wait(key, cancel, timeout)                    -> the result dict | 'pending' (not yet: wait again) |
                                                  'cancelled' (the user stopped the run)
    close(key)                                    the run stopped waiting (timeout / done)

In memory, bounded (the app is one process; a result only matters while its stream is open).
"""

import threading
import time
from collections import OrderedDict

from .confirm import AgentError

MAX_PENDING = 200
_pending = OrderedDict()            # (run_id, action_id) -> {user_id, event, result, created}
_lock = threading.Lock()


def open_(user, run_id, action_id):
    key = (str(run_id), str(action_id))
    with _lock:
        while len(_pending) >= MAX_PENDING:
            _pending.popitem(last=False)[1]['event'].set()
        _pending[key] = {'user_id': (user or {}).get('id'), 'event': threading.Event(), 'result': None,
                         'created': time.time()}
    return key


def _clean(result):
    r = result if isinstance(result, dict) else {}
    out = {'ok': r.get('ok') is True, 'detail': str(r.get('detail') or '')[:2000], 'transient': r.get('transient') is True}
    if isinstance(r.get('result'), (dict, list, str, int, float)):
        out['result'] = r['result']
    return out


def deliver(user, run_id, action_id, result):
    with _lock:
        p = _pending.get((str(run_id), str(action_id)))
        if p is None:
            raise AgentError('no page action is waiting for that result (the run ended)', 404)
        if p['user_id'] != (user or {}).get('id'):
            raise AgentError('this run belongs to another user', 403)
        p['result'] = _clean(result)
        p['event'].set()
    return {'ok': True}


def wait(key, cancel, timeout):
    """The page's result, 'pending' after `timeout` s, or 'cancelled' when the run was stopped."""
    with _lock:
        p = _pending.get(key)
    if p is None:
        return 'cancelled'
    end = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            close(key)
            return 'cancelled'
        left = end - time.monotonic()
        if left <= 0:
            return 'pending'
        if p['event'].wait(min(left, 0.5)):
            if p['result'] is None:          # pushed out of the bounded registry
                return 'cancelled'
            close(key)
            return p['result']


def close(key):
    with _lock:
        _pending.pop(key, None)
