"""
Keeps a long agent run (up to 50 steps) inside the token budget: instead of failing when tool results pile up,
the OLDEST tool results become one-line stubs (the model keeps the call, its name and the first characters of
what it returned, and can call it again if it needs the details). The last two messages are never touched.
"""

import json

from .. import budget

STUB_AT = 240               # a result shorter than this is left as it is


def estimate(kind, system, msgs):
    """budget.estimate plus the tool parts (it only counts text / media)."""
    extra = 0
    for m in msgs:
        for x in m.get('parts') or []:
            if 'tool_call' in x:
                extra += budget.text_tokens(json.dumps(x['tool_call'].get('args'))) + 8
            elif 'tool_result' in x:
                extra += budget.text_tokens(x['tool_result'].get('content')) + 8
            elif 'thinking' in x:
                extra += budget.text_tokens((x['thinking'] or {}).get('text')) + 4
    return budget.estimate(kind, system, msgs) + extra


def _stub(r):
    c = str(r.get('content') or '')
    if len(c) < STUB_AT or c.startswith('[elided'):
        return False
    r['content'] = f'[elided to save space: {c[:160]}…] (call {r.get("name")} again if you need it)'
    return True


def fit(kind, system, msgs, cap):
    """Stub old tool results (oldest first) until the request fits `cap` tokens. True when it fits."""
    if estimate(kind, system, msgs) <= cap:
        return True
    for m in msgs[:-2]:
        if m.get('role') != 'tool':
            continue
        for x in m.get('parts') or []:
            if 'tool_result' in x and _stub(x['tool_result']) and estimate(kind, system, msgs) <= cap:
                return True
    for m in msgs[:-2]:     # old thinking goes next, whole (a cut block would break its signature; only the last
        if any('thinking' in x for x in m.get('parts') or []):     # step's thinking must be sent back)
            m['parts'] = [x for x in m['parts'] if 'thinking' not in x]
    return estimate(kind, system, msgs) <= cap
