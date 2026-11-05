"""
Context compaction of a ◆ chat: the older part of a chat becomes one structured summary (prompts/templates/
compact.md: goal, preferences, app state, work done with exact numbers and ids, findings, declined actions, open
tasks, facts) that the model reads instead of those messages; the page still shows every message.

    needs(est, limit, n_msgs)          auto-compact when the request estimate passes THRESHOLD of the budget, or the
                                       unsummarised messages pass sessions.HISTORY_TURNS (they would be cut silently)
    plan_cut(messages, keep)           the last seq to fold in: keeps the newest `keep` messages, starting on a user
    transcript(rows)                   messages + their stored traces (tool calls and results, confirmations, page
                                       actions, refusals): the details the plain history never replays
    summarize(provider, prev, rows, limit, focus)   the new summary (rolling over chunks when the input is big)
    compact(user, sid, provider, limit, keep, focus)  fold a saved chat -> {summary, covered, before, after, turns}

The summary is incremental: the previous one + the newly covered messages make the next one, so a long chat costs
one small call per compaction. A failed summary call is retried (RETRIES, backoff), then raises: the caller falls
back to budget.fit's dropping, a failure never blocks a turn.
"""

import json
import time

from .. import budget, prompting, providers
from .. import sessions as chat_sessions
from ..providers import LLMError

THRESHOLD = 0.80            # auto-compact above this share of the input budget
KEEP_AUTO = 6               # messages kept verbatim after an automatic compaction (3 exchanges)
KEEP_MANUAL = 2             # /compact keeps the last exchange
RETRIES = 2
CAP_SHARE = 0.08            # the summary's token cap: this share of the budget, clamped to CAP_MIN..CAP_MAX
CAP_MIN, CAP_MAX = 600, 3000
TOOL_ARGS_MAX, TOOL_RES_MAX, TEXT_MAX = 300, 400, 6000


class CompactError(ValueError):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def summary_cap(limit):
    return max(CAP_MIN, min(CAP_MAX, int(budget.cap(limit) * CAP_SHARE)))


def needs(est, limit, n_msgs):
    return est > THRESHOLD * budget.cap(limit) or n_msgs > chat_sessions.HISTORY_TURNS


def plan_cut(messages, keep):
    """The seq of the last message to fold in (None = nothing to fold): everything but the newest `keep`
    messages, moved back so the kept part starts with a user message."""
    if len(messages) <= keep:
        return None
    i = len(messages) - keep                    # first kept index
    while 0 < i < len(messages) and messages[i]['role'] != 'user':
        i -= 1
    return messages[i - 1]['seq'] if i > 0 else None


def _cut(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str, separators=(',', ':'))
    return s if len(s) <= n else s[:n] + '…'


def _trace_lines(trace):
    """The stored trace of one answer -> short lines: what ran, with what, and how it ended."""
    out = []
    for ev in trace or []:
        t = ev.get('type') if isinstance(ev, dict) else None
        if t == 'tool':
            ok = {True: 'ok', False: 'FAILED'}.get(ev.get('ok'), 'not run')
            line = f"- tool {ev.get('name')}({_cut(ev.get('args') or {}, TOOL_ARGS_MAX)}) → {ok}"
            if ev.get('summary'):
                line += f": {_cut(ev['summary'], TOOL_RES_MAX)}"
            if ev.get('preview') and ev.get('ok'):
                line += f" | result: {_cut(ev['preview'], TOOL_RES_MAX)}"
            for k in ('undo_id', 'url'):
                if ev.get(k):
                    line += f' [{k} {ev[k]}]'
            out.append(line)
        elif t == 'confirm':
            acts = ', '.join(str(a.get('summary') or a.get('tool')) for a in ev.get('actions') or [])
            res = ', '.join(('declined ' if r.get('declined') else ('done ' if r.get('ok') else 'failed '))
                            + str(r.get('summary') or '') for r in ev.get('results') or [])
            out.append(f"- asked to confirm: {_cut(acts, TOOL_RES_MAX)} → {ev.get('status')}: {_cut(res, TOOL_RES_MAX)}")
        elif t == 'ui_action':
            out.append(f"- page action {ev.get('action')} {_cut(ev.get('args') or {}, TOOL_ARGS_MAX)} "
                       f"({'applied' if ev.get('applied') else 'offered'})")
        elif t == 'cannot':
            out.append(f"- could not: {ev.get('reason')} (instead: {ev.get('alternative') or '-'})")
        elif t == 'ask':
            out.append(f"- asked the user: {ev.get('question')} {ev.get('options') or ''}")
        elif t == 'plan_proposal':
            out.append(f"- proposed plan: {ev.get('summary')}")
        elif t == 'todo':
            out.append('- checklist: ' + '; '.join(f"{i.get('text')} [{i.get('state')}]" for i in ev.get('items') or []))
        elif t == 'error':
            out.append(f"- error: {_cut(ev.get('detail') or '', TOOL_RES_MAX)}")
        elif t == 'meta' and ev.get('model'):
            out.append(f"- (model {ev.get('model')}, mode {ev.get('mode')})")
    return out


def transcript(rows):
    """[{seq, role, text, trace?}] -> one plain-text transcript, each message with its trace lines."""
    parts = []
    for m in rows:
        who = 'USER' if m['role'] == 'user' else 'ASSISTANT'
        lines = _trace_lines(m.get('trace')) if m['role'] == 'assistant' else []
        body = _cut(str(m.get('text') or '').strip(), TEXT_MAX)
        parts.append(f'[{who} #{m["seq"]}]\n' + ('\n'.join(lines) + '\n' if lines else '') + body)
    return '\n\n'.join(parts)


def _chunks(rows, max_tokens):
    """Rows split so each chunk's transcript is under max_tokens (a single huge message is its own chunk)."""
    out, cur, n = [], [], 0
    for m in rows:
        t = budget.text_tokens(transcript([m]))
        if cur and n + t > max_tokens:
            out.append(cur)
            cur, n = [], 0
        cur.append(m)
        n += t
    if cur:
        out.append(cur)
    return out


def _call(provider, system, user_text, cap):
    msgs = [{'role': 'user', 'parts': [{'text': user_text}]}]
    for n in range(RETRIES + 1):
        try:
            text, _ = providers.complete(provider, system, msgs, max_tokens=cap)
        except LLMError as e:
            if not getattr(e, 'transient', False) or n == RETRIES:
                raise
            time.sleep(min(float(getattr(e, 'retry_after', None) or 2 ** n), 10.0))
            continue
        text = str(text or '').strip()
        if text:
            return text
        if n == RETRIES:
            raise LLMError('the model returned an empty summary', 502)
    raise LLMError('no summary', 502)          # not reached


def summarize(provider, prev, rows, limit, focus=None):
    """The summary of `prev` (the previous summary or None) + `rows`, under summary_cap(limit) tokens. The input
    of one call stays under half the budget: more rows roll over several calls (each folds into the last)."""
    cap = summary_cap(limit)
    system = prompting.template('compact', words=str(int(cap * 0.7)),
                                focus=f'The user asked to focus on: {focus.strip()[:500]}' if focus else '')
    room = max(1000, budget.cap(limit) // 2 - cap - budget.text_tokens(system))
    summary = prev
    for chunk in _chunks(rows, room):
        head = f'PREVIOUS SUMMARY:\n{summary}\n\n' if summary else ''
        summary = _call(provider, system, f'{head}NEW MESSAGES:\n{transcript(chunk)}\n\nWrite the updated summary.', cap)
    cut = int(cap * budget.CHARS_PER_TOKEN * 1.2)
    return summary if len(summary) <= cut else summary[:cut] + '…'


def compact(user, sid, provider, limit, keep=KEEP_AUTO, focus=None):
    """Fold a saved chat's older messages into its summary -> {summary, covered, before, after, turns} or None
    when there is nothing to fold. Raises LLMError / CompactError; the chat is unchanged then."""
    ctx = chat_sessions.context(user, sid)
    upto = plan_cut(ctx['messages'], keep)
    if upto is None:
        return None
    rows = chat_sessions.for_summary(user, sid, ctx['summary_seq'], upto)
    if not rows:
        return None
    before = budget.text_tokens(transcript(rows)) + (ctx['summary_tokens'] or 0)
    summary = summarize(provider, ctx['summary'], rows, limit, focus)
    after = budget.text_tokens(summary)
    if not chat_sessions.save_summary(user, sid, summary, upto, after, ctx['summary_seq']):
        raise CompactError('the chat changed meanwhile (compacted in another tab): try again', 409)
    return {'summary': summary, 'covered': upto, 'before': before, 'after': after, 'turns': len(rows)}


def compact_messages(provider, msgs, limit, keep=KEEP_AUTO):
    """An unsaved chat ([{role, text}] from the page): (summary or None, the kept messages)."""
    rows = [{'seq': i + 1, 'role': m.get('role'), 'text': str(m.get('text') or '')} for i, m in enumerate(msgs)]
    upto = plan_cut(rows, keep)
    if upto is None:
        return None, msgs
    return summarize(provider, None, rows[:upto], limit), msgs[upto:]
