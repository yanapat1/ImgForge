# 9. Token budget and compaction

Three layers, cheapest first. A long task should **never fail on size** while it can still shrink.

```
before every provider call:   (a) stub old tool results  →  fits?  yes → call
                              past 90% of the budget, or still too big →  (b) compact THIS ANSWER's steps, go on
end of a turn / next message: past 80% or 40 messages  →  (c) compact the CHAT's older messages into its summary
```

## Estimates and the limit

```python
import math

CHARS_PER_TOKEN = 3.5                       # an estimate, not a tokenizer: fast and good enough

def text_tokens(s):
    return math.ceil(len(s or '') / CHARS_PER_TOKEN)

def image_tokens(kind, w, h):
    if kind == 'anthropic':
        s = min(1.0, 1568 / max(w, h))
        return math.ceil(w * s * h * s / 750)
    if kind == 'gemini':
        return 258 if w <= 384 and h <= 384 else 258 * math.ceil(w / 768) * math.ceil(h / 768)
    s = min(1.0, 2048 / max(w, h)); w, h = w * s, h * s          # openai: 2048 box, short side 768, 512 tiles
    s = min(1.0, 768 / min(w, h)); w, h = w * s, h * s
    return 85 + 170 * math.ceil(w / 512) * math.ceil(h / 512)
```

Count tool calls (JSON args), tool results and thinking too (+8 / +4 overhead each), plus the tool specs.

**The effective limit** = the user's budget setting (a number, or `auto`), kept under the model's context window
minus the room for the answer and a 512 margin. If the window is unknown, use 100k. Send a `context {used, limit,
exact}` event after `meta`, after each step's tool results (an estimate) and once the provider reports usage (exact),
so the UI can show a context ring.

When the first request is too big, `budget.fit` of the history: replace older images with `[image omitted]`, drop the
oldest turns, downscale or drop context images, else return a 400 "message too large".

## (a) Stubbing old tool results

```python
STUB_AT = 240

def _stub(r):
    c = str(r.get('content') or '')
    if len(c) < STUB_AT or c.startswith('[elided'):
        return 0
    r['content'] = f'[elided to save space: {c[:160]}…] (call {r.get("name")} again if you need it)'
    return text_tokens(c) - text_tokens(r['content'])


def fit(kind, system, msgs, cap):
    """Stub old tool results (oldest first) until the request fits -> (fits, estimate). The last 2 messages are
    never touched. The estimate is updated by what each cut saved (no recount)."""
    est = estimate(kind, system, msgs)
    if est <= cap:
        return True, est
    for m in msgs[:-2]:
        for x in m.get('parts') or []:
            if 'tool_result' in x:
                est -= _stub(x['tool_result'])
                if est <= cap:
                    return True, est
    for m in msgs[:-2]:                 # then old thinking, whole blocks (a cut block breaks its signature)
        if any('thinking' in x for x in m.get('parts') or []):
            est -= sum(text_tokens(x['thinking'].get('text')) + 4 for x in m['parts'] if 'thinking' in x)
            m['parts'] = [x for x in m['parts'] if 'thinking' not in x]
    return est <= cap, est
```

The model keeps the call and a preview, and it knows how to get the details back.

## (b) Run compaction: fold this answer's own steps and go on

Triggers (`why`): `context` (past 90% of the budget after stubbing), `steps` (the step limit; another batch of
`max_steps` follows), `loop` (the loop guard, 10). At most **3** per answer, at least 1 step apart. Chat setting
`compact`: `auto` (just do it) | `ask` (a confirm card "Compact the work so far and continue?") | `off` (stop with the
limit's error, which says how to turn it on).

```python
RESUME = ('(system) Your work on this request was compacted to save room ({why}). Progress so far:\n\n{summary}\n\n'
          'Go on from "Next step". Do not redo what is under "Done".')


def compact_run(run, why):
    """run.msgs[:run.base_n] = the history + the user's message, never folded."""
    text = progress_text(run.msgs[run.base_n:], run.todo)        # '- call x(args)\n  → x ok: result…' lines
    system = template('run_compact', words=str(int(summary_cap(run.limit) * 0.7)))
    summary = complete(run.provider, system, [{'role': 'user', 'parts': [{'text':
                       f'WORK ON THE CURRENT REQUEST:\n{text}\n\nWrite the progress summary.'}]}])
    run.msgs = run.msgs[:run.base_n] + [{'role': 'user', 'parts': [{'text': RESUME.format(why=why, summary=summary)}]}]
```

Events: `run_compact {n, phase: ask|start|done|failed|declined, reason, used, limit, steps, before, after, ms,
summary}`. If the summary call fails, the answer stops with the limit's own error, as if compaction were off.

`templates/run_compact.md`:

```markdown
You compact the work an assistant has done so far on ONE request, because the answer reached a limit. The
assistant continues from your summary and no longer sees the steps below, so anything you leave out is forgotten.

Rules:
- Keep exact values verbatim: names, paths, ids (job, undo, session ids), numbers and counts.
- Separate what was done (a tool ran and succeeded) from what failed or was declined, so it is not repeated blindly.
- Short bullet points, no greetings, no tables. Skip a heading that has nothing.

## Goal
(the user's request, in their words)
## Done
(each step: action → result, with the exact values)
## Found so far
## Failed / declined
## Still to do
## Next step
(the single next action)

Stay under about $words words.
```

## (c) Chat compaction: a rolling structured summary

- **When**: the request estimate is past **80%** of the budget, or there are more than **40** unsummarised messages, or the
  user types `/compact [focus]`.
- **What**: every message except the newest **6** (auto) or **2** (manual). The kept part must start on a user message.
  Each assistant message's stored **trace** is included (tool calls + results, confirmations, refusals): the plain
  history never replays it.
- **How**: previous summary + new messages → one updated summary. If the input is big, roll it over chunks of half the
  budget. Cap the summary at 8% of the budget, clamped to 600-3000 tokens. Retry a transient failure twice. If the
  answer is empty, ask once more with twice the room.
- **Stored** on the chat: `summary`, `summary_seq` (the last folded message), `summary_tokens`. The model reads the
  summary in the dynamic prompt (`## Earlier in this chat (summary)`) plus the messages after `summary_seq`. The UI
  still shows every message. Save with a compare-and-set on `summary_seq` (409 if another tab compacted meanwhile).
- If compaction fails, fall back to dropping the oldest turns. A failure never blocks a turn.

`templates/compact.md` (headings to keep):

```markdown
You compact a conversation between a user and an assistant of <app>. Write the summary the assistant will read
INSTEAD of the earlier messages; anything you leave out is forgotten.

Rules:
- Merge the previous summary (if any) with the new messages into ONE up-to-date summary.
- Keep exact values verbatim: numbers, ids, names.
- Record what was actually done separately from what was only suggested. Record declined or failed actions and why.
- Record the user's preferences and instructions (and the language they write in).
- App state is what was chosen or created, never what a screen showed at one moment (the current screen is given
  with every message; an old copy would contradict it).
- Plain bullet points under these headings, skip a heading that has nothing:

## Goal
## User preferences and instructions
## App state
## Done (action → result, with numbers and ids)
## Findings
## Declined / failed (and why)
## Open tasks and next steps
## Facts to keep verbatim

$focus
Stay under about $words words.
```
