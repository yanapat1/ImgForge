# 11. Events and the chat UI

`POST /api/agent/<role>/chat` answers with `application/x-ndjson`: one JSON object per line, keyed `type`. Flush
after every line and disable proxy buffering (`X-Accel-Buffering: no`). Every turn ends with exactly one `done` or
`error`.

| type | fields | UI |
|---|---|---|
| `meta` | run_id, role, model, provider, mode, effort, think, profile, max_steps, in_est, tools, limit, session_id? | header chips. Keep run_id for Stop |
| `context` | used, limit, exact | context ring (estimate dashed, exact solid) |
| `session` | session_id, title, user_seq | a new chat got an id |
| `step` | n, max | "step 3/30" |
| `thinking` / `thinking_end` | text / ms | collapsible "Thought for 4 s" |
| `text` | text | streamed Markdown (render in batches with requestAnimationFrame) |
| `tool_start` / `tool_end` | id, name, args, risk / ok, summary, ms, preview?, undo_id? | one row per call, expandable |
| `skill` | name | "using skill x" |
| `todo` | items[{text, state}] | the live checklist card (update it in place) |
| `confirm` | confirm_id, actions[{id, tool, args, summary, risk}], timeout_s | card: per-action approve/reject, edit args, danger in red |
| `confirmed` | confirm_id, results[{id, ok, summary, declined?, undo_id?}], status | close the card, show the outcome |
| `ask` | question, options | option buttons. A click sends the option as the next message |
| `cannot` | reason, alternative | a notice card |
| `plan_proposal` | summary, steps | the plan card + Approve (runs in auto) / Edit |
| `mode` | mode, reason | the mode chip switches to plan |
| `retry` | attempt, max, reason, wait_s, discard, step | "retrying in 4 s (2/50)". discard → drop the step's text |
| `run_compact` | n, phase, reason, used, limit, before, after, summary | "compacted the work (context): 18k → 1.2k" |
| `compact` | state start/done, summary, covered, before, after | chat compaction notice |
| `notice` | level, text | a small line |
| `ping` | — | ignore (keep-alive while waiting) |
| `error` | detail | red, ends the turn |
| `done` | usage{in, out}, ms, steps, ctx, limit | ends the turn. Show tokens and time |

## Route sketch

```python
def chat_route(body, user):
    run = prepare(user, body)                    # 400 on a bad request before any streaming starts
    if not claim_chat(run.session_id, run.cancel):
        return error(409, 'this chat is answering in another tab')

    def gen():
        trace = []
        try:
            for ev in events(run):
                if ev['type'] not in ('text', 'thinking', 'ping', 'context'):
                    trace.append(ev)             # the compacted trace saved with the answer (09c reads it)
                yield json.dumps(ev, ensure_ascii=False) + '\n'
        except LLMError as e:
            yield json.dumps({'type': 'error', 'detail': str(e)}) + '\n'
        finally:
            release_chat(run.session_id, run.cancel)
            save_answer(run, trace)              # text + trace + usage + ctx
    return stream(gen(), 'application/x-ndjson')
```

Other routes: `POST /confirm {confirm_id, approve[], reject[], edits{}}`, `POST /stop {run_id}`,
`POST /compact {session_id, focus?}`, `GET/DELETE /sessions…`.

## UI details that matter

- One chat dock. Slash commands: `/mode`, `/effort`, `/think`, `/compact`, `/skill <name>`, `/new`.
- Stop button = `POST /stop`. It works during streaming, retry waits and confirmations.
- On reload, a saved chat shows its messages with their traces (tool rows, cards) from the stored trace.
- Long chats: virtualize the message list. Batch DOM updates per animation frame while streaming.
