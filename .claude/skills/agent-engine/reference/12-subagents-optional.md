# 12. Optional: sub-agents, background tasks, web guard

Add these only after the core works. Keep them **off by default** and never offer them to small or medium models.

## delegate: parallel read-only sub-runs inside one turn

```python
register(Tool('delegate', 'Split independent READ-ONLY research into 1-4 tasks run in parallel by copies of you; '
                          'only their answers come back. Not for changes, not for one quick look-up.',
              obj({'tasks': {'type': 'array', 'minItems': 1, 'maxItems': 4, 'items': S('one self-contained task')}},
                  ['tasks']), False, _delegate, domain='common'))
```

- Each task is a **sub-run**: the same role's prompt, its read tools only (no change, delegate, spawn or navigate),
  ≤ 8 steps, and an answer ≤ 2000 chars. It runs in a thread. Only the answer goes back to the lead as the tool result.
- Their tokens are added to the turn's usage (`ctx.sub_usage`). Emit `agent {task, ok, steps, tools, in, out}` per task.
- `consult(question)`: the same mechanism, aimed at *another* role's specialist (≤ 6 steps). No consult of a consult.

## Background tasks: workers that outlive the message

- `spawn_task {title, goal, mode?}` starts a worker in a server thread. It is a run of the same role with no page actions
  and no spawn/delegate/consult. **A sub-agent never starts a sub-agent.** Its mode is never looser than the chat's.
- The worker ends with `finish_task {summary, show?}`. `task_status` reports what each one did and has left (with its
  checklist), time and tokens. `task_message` answers its question or approves its plan. `stop_task` stops it.
- A change inside a worker waits on a normal confirmation (30 min). **Only the user** approves it, in a tasks strip,
  never the lead agent.
- Limits: 1-4 running per user (default 2), 8 on the server, 8 queued. They live in memory, so a restart drops them.
- When one finishes, the UI sends a message that only carries `task_event=<id>`, and the server puts the task's
  result to the lead as the user message (never text from the page).

## Web search + guard

- `web_search {query}` and `fetch_url {url}` as `web`-domain read tools. Use the provider's native search where it
  works. If it is refused, fall back to your own (02).
- **Guard every outgoing query and URL on the server.** Collect the turn's "internal names" from tool results and the
  user's private entities, and block a query that contains one. Use levels (public / technical / full), and a
  "never" word list that blocks in every mode. A blocked query goes back to the model as "blocked: rephrase
  generally". Option "Ask before each search" makes `web_search` a `confirm_if` read (the user can edit the query
  on the card).
- Web results are public text: do not add their words to the internal names.
