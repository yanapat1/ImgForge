# 1. Architecture

## Layers

```
 chat UI  ──POST /api/agent/chat──▶  route  ──▶ engine.events(run)  ──yield events──▶ NDJSON stream ──▶ chat UI
                                        │            │
                                        │            ├─ prompting.build()   Markdown → system prompt (stable | dynamic)
                                        │            ├─ providers.stream_tools()   one streamed step, any provider
                                        │            ├─ tools.REGISTRY      validate → run / confirm
                                        │            ├─ compact.fit() / _compact_run()   stay inside the budget
                                        │            └─ confirm.open_/wait  park the stream until the user decides
                                        └─ sessions (saved chats, summary, traces)
 chat UI  ──POST /api/agent/confirm──▶ confirm.decide()  (wakes the waiting run)
 chat UI  ──POST /api/agent/stop─────▶ run.cancel.set()
```

Each layer only knows the one below it. The engine never imports a provider SDK. The tools never know which
model is calling them. The UI never sees a key.

## Folder layout to copy

```
<app>/llm/
  providers.py        raw HTTP clients (httpx) for openai | openai_compat | anthropic | gemini, one message format
  budget.py           token estimates, effective limit of a request
  prompting.py        builds the system prompt from prompts/ (front matter, caching split, templates)
  store.py            provider configs per user (API key encrypted, write-only), feature switches
  sessions.py         saved chats: messages, traces, summary + summary_seq
  prompts/
    base.md                         rules every agent follows
    <role>/system.md                who this agent is, its scope
    <role>/knowledge/<topic>.md     facts about its area (front matter title, summary, core, high)
    skills/<role|common>/<name>/SKILL.md   playbooks (front matter name, description, when)
    templates/                      mode_ask.md, mode_auto.md, mode_plan.md, now.md, compact.md, run_compact.md
  tools/
    __init__.py       Tool, REGISTRY, register, validate, for_role, prepare, run (imports every module of the folder)
    control.py        ask_user, cannot_do, todo, submit_plan, enter_plan_mode, more_tools, finish_task
    skills.py         use_skill, read_docs
    <domain>.py       the project's own tools (one file per area)
  engine/
    react.py          Ctx, Run, prepare(), events(): the ReAct loop
    confirm.py        pause / resume for confirmations
    compact.py        estimate + stub old tool results
    summarize.py      chat compaction (rolling structured summary)
    roles.py          the agents (role → tool domains, prompt folder, scope text)
    events.py         the event protocol (doc + list of types)
```

A **role** is one specialist: a name, its tool domains, its prompt folder. A second agent is a new `Role` plus
its prompts folder. Nothing else changes.

## The neutral message format

All the engine's history uses one shape. Each provider adapter converts it.

```python
msgs = [
  {'role': 'user',      'parts': [{'text': 'How many open items?'}, {'jpeg': b'...', 'cache': True}]},
  {'role': 'assistant', 'parts': [{'thinking': {'text': '...', 'sig': 'abc'}},       # only signed thinking is kept
                                  {'text': 'Let me check.'},
                                  {'tool_call': {'id': 'call_1_0', 'name': 'search_items', 'args': {'state': 'open'}}}]},
  {'role': 'tool',      'parts': [{'tool_result': {'id': 'call_1_0', 'name': 'search_items',
                                                   'content': '{"count": 12, ...}', 'is_error': False}}]},
  {'role': 'assistant', 'parts': [{'text': 'There are **12** open items.'}]},
]
```

- Part kinds: `text`, `jpeg` (bytes, optional `cache` flag), `wav`, `thinking`, `tool_call`, `tool_result`.
- Tool results are always strings. Dump JSON compactly and cap the length (6000 chars by default).
- Call ids must be unique in a history. Some servers send none, so assign `call_<step>_<k>`.

## Run objects

```python
class Ctx:                      # what a tool sees: never the provider, never the key
    def __init__(self, user, role, session_id=None, skills=None, mode='ask'):
        self.user, self.role, self.session_id = user, role, session_id
        self.skills = skills or {}          # name -> {name, description, when, body}
        self.mode = mode
        self.sub_usage = {'in': 0, 'out': 0}   # tokens spent by sub-runs of this turn


class Run:                      # one turn
    def __init__(self, run_id, user, role, provider, system, msgs, tools, ctx, *, mode, effort, think,
                 max_steps, limit, compact='auto', cancel=None, confirm_timeout=600):
        self.run_id, self.user, self.role, self.provider = run_id, user, role, provider
        self.system, self.msgs, self.tools, self.ctx = system, msgs, tools, ctx
        self.mode, self.effort, self.think, self.max_steps, self.limit = mode, effort, think, max_steps, limit
        self.compact, self.cancel, self.confirm_timeout = compact, cancel, confirm_timeout
        self.todo, self.base_n = [], len(msgs)   # base_n: history + this user message, never folded
```

`prepare(...)` builds a `Run`: picks the role's tools for the mode and profile, builds the prompt, fits the history
into the budget, and estimates the input tokens (`run.est`) for the first `meta` event.
