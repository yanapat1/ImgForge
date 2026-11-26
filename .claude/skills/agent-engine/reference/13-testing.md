# 13. Testing without an API key

Replace `stream_tools` with a **scripted fake** that replays one list of events per step and records what it was
sent. Every engine behaviour can then be tested quickly, without randomness.

```python
import pytest


class FakeLLM:
    """steps: one list per provider call, e.g. [('text', 'hi')] or [('tool_call', {...})]."""

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def __call__(self, p, system, msgs, tools, **kw):
        self.calls.append({'system': system, 'msgs': [dict(m) for m in msgs], 'tools': [t['name'] for t in tools]})
        if not self.steps:
            raise AssertionError('the engine asked for more steps than scripted')
        for ev in self.steps.pop(0):
            if isinstance(ev, Exception):
                raise ev
            yield ev
        yield ('usage', {'in': 100, 'out': 10})


def call(name, **args):
    return ('tool_call', {'id': '', 'name': name, 'args': args})


@pytest.fixture
def fake(monkeypatch):
    def install(*steps):
        f = FakeLLM(*steps)
        monkeypatch.setattr('app.llm.engine.react.stream_tools', f)
        return f
    return install


def types(evs):
    return [e['type'] for e in evs]


def test_read_then_answer(fake, make_run):
    f = fake([call('search_items', query='x')], [('text', 'Found 2.')])
    evs = list(events(make_run(mode='ask')))
    assert types(evs)[-1] == 'done' and 'tool_end' in types(evs)
    assert f.calls[1]['msgs'][-1]['role'] == 'tool'            # the result went back to the model
```

## Tests to write (one each)

- **Loop**: answer with no tool; read → answer; an unknown tool gives an error result and the model recovers; bad args;
  args `None`; the step limit gives an error (compaction off); the cut-off retry happens once and then errors; empty answer.
- **Retries**: a transient error then success emits `retry` and finishes. A non-transient error ends at once. Stop
  during the wait.
- **Control**: `ask_user` / `cannot_do` / `submit_plan` end the turn with their card. `todo` does not. The first valid
  ender wins.
- **Modes**: plan mode removes the change tools. A change in ask emits `confirm`. Approving runs it and the turn
  continues. Declining gives the "declined" result. A timeout changes nothing. A danger is confirmed in auto. One bad
  change means none runs. `enter_plan_mode` drops the step's changes.
- **Verify**: effort high + a change → one `(check)` pass. Effort low → none.
- **Budget**: `fit` stubs the oldest results first, never the last 2 messages, and drops old thinking whole. Run
  compaction fires at 90% / the step limit and resumes with the summary. At most 3. `compact=off` stops. `ask` waits
  for a card.
- **Loop guard**: the 3rd repeat is nudged, the 5th compacts and blocks, the 3rd recovery stops. A change resets it.
- **Prompts**: front matter parses. Core topics are inline only from effort medium. User instructions come last. The
  DYNAMIC split puts the mode and Now after the marker. Editing a file changes the next prompt (mtime cache).
- **validate**: coercions ("3" → 3, "a" → ["a"]), enum and range errors, unknown keys dropped, required keys.
- **Providers** (recorded HTTP fixtures): the history mapping per kind, tool calls with split argument chunks, usage,
  stop reasons, a refused parameter dropped and remembered, the key scrubbed from errors.
