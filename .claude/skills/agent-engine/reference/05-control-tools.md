# 5. Control tools

Control tools are not actions. The engine answers them, or they end the turn with a card the user answers. They are
what makes the agent ask instead of guess, refuse clearly, and show a plan.

| Tool | Ends the turn | Event / card | Notes |
|---|---|---|---|
| `ask_user {question, options[2-5]}` | yes | `ask` (clickable options) | The click is the next user message |
| `cannot_do {reason, alternative?}` | yes | `cannot` | The closest thing it can do |
| `todo {items:[{text, state}]}` | no | `todo` (checklist) | The WHOLE list each time. States pending / active / done / skipped |
| `submit_plan {summary, steps[]}` | yes | `plan_proposal` + Approve | Plan mode only. Approve runs it next in auto |
| `enter_plan_mode {reason}` | no | `mode {mode:'plan'}` | Auto mode only. The rest of the turn is plan mode |
| `more_tools {group}` | no | — | Mid-size profile only: loads a tool group |
| `finish_task {summary, show?}` | yes | `task_result` | Background workers only (12) |

```python
ITEM = {'type': 'object', 'additionalProperties': False, 'required': ['text', 'state'],
        'properties': {'text': S('one step, short'),
                       'state': {'type': 'string', 'enum': ['pending', 'active', 'done', 'skipped']}}}

register(Tool('ask_user', 'Ask the user a clarifying question with 2-5 concrete options instead of guessing. '
                          'Ends your turn.',
              obj({'question': S('one short question'), 'options': A('2-5 concrete answers the user can click')},
                  ['question', 'options']), False, control=True, domain='control'))
register(Tool('cannot_do', 'Say the request cannot be done with these tools, why, and the closest thing you can '
                           'do. Ends your turn.',
              obj({'reason': S('why not, one sentence'), 'alternative': S('the closest thing you can do')},
                  ['reason']), False, control=True, domain='control'))
register(Tool('todo', 'Show / update your checklist for a request of 3 or more steps: send the WHOLE list each '
                      'time. Does not end your turn.',
              obj({'items': {'type': 'array', 'items': ITEM, 'description': 'the whole checklist'}}, ['items']),
              False, control=True, domain='control'))
register(Tool('enter_plan_mode', 'Auto mode: switch to plan mode BEFORE acting when the request is big, unclear, '
                                 'or would change or delete a lot. Not for small, clear requests.',
              obj({'reason': S('why it needs a plan, one short sentence the user sees')}, ['reason']),
              False, control=True, domain='control'))
register(Tool('submit_plan', 'Plan mode: after looking things up, submit the plan for the user to approve. Ends '
                             'your turn; nothing is changed until they approve it.',
              obj({'summary': S('one or two sentences'), 'steps': A('the concrete steps, in order, with real names')},
                  ['summary', 'steps']), False, control=True, domain='control'))
```

## Engine handling

- **Turn enders**: scan the step's calls. The first *valid* turn-ender wins: yield its card and `done`. The other calls of
  that step do not run.
- **todo**: yield `{'type': 'todo', 'items': items[:30]}`, keep it on `run.todo`, and **save it on the chat**. While a
  step is still open, the next turns get it in the dynamic prompt ("## Your plan", ✓ done / ▶ active / – skipped, ≤ 12
  lines), so the work goes on across messages and compaction. The result to the model is just `checklist shown`.
- **enter_plan_mode**: set `run.mode = 'plan'` and remove the change/ui/run tools from `run.tools`. Every change
  in the same step gets "not run: you switched to plan mode; put it in the plan". The tool result explains the plan
  mode rules. Keep the system prompt as it is: changing it would break the provider cache.
- **A control tool with bad args** gets the validation error as its result (the model fixes it) and does not end the turn.

## Why they matter

Without `ask_user` a model guesses. Without `cannot_do` it invents a tool or loops. Without `todo` it forgets step 4
of 6. Without `submit_plan` a big change has no review point. The base rules (07) tell it when to use each one.
