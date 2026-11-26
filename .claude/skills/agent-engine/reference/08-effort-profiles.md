# 8. Effort and model profiles

## Effort: one knob for the user (`/effort low|medium|high`)

```python
EFFORTS = {                      # tunable defaults
    'low':    {'steps': 15, 'think': 'off',  'verify': False},
    'medium': {'steps': 30, 'think': 'low',  'verify': False},
    'high':   {'steps': 50, 'think': 'high', 'verify': True},
}
THINK_OUT = {'low': 4096, 'medium': 8192, 'high': 16384}   # output room of a thinking step (OpenAI-style / local)
```

- **steps** = the step limit of one answer. Run compaction (09) can add another batch of steps up to 3 times.
- **think** = the provider's reasoning level. A user override (`/think off|low|medium|high`) wins over the effort.
- **verify** = at the end, if something changed, one more pass: "verify with read-only tools that the changes did
  what was asked, then give the short final answer".
- **Knowledge**: low = index only. medium = `core` topics inline. high = `core` + `high` topics inline.
- An admin sets the maximum (`steps_max` 50, `steps_medium` 25, `steps_small` 20). Effort high takes the whole maximum.

## Profiles: adapt to the model's size automatically

Detect from a provider setting (`agent_profile: auto|small|medium|large`), else from the model name
(`qwen2.5-7b`, `llama3.1:8b`, `…-32b`), else from the context window.

```python
import re

def profile_of(p):
    forced = str(p.get('agent_profile') or 'auto')
    if forced in ('small', 'medium', 'large'):
        return forced
    m = re.search(r'(?<![\d.])(\d+(?:\.\d+)?)\s*b(?![a-z])', str(p.get('model') or '').lower())
    size = float(m.group(1)) if m else None
    if size is not None and size <= 14:
        return 'small'
    ctx = int(p.get('context_window') or 0)
    return 'medium' if (size is not None and size <= 40) or 0 < ctx <= 40000 else 'large'
```

| profile | steps | tools | prompt | temperature |
|---|---|---|---|---|
| small (≤ 14B) | 20 | ≤ 8 matched by words + always-on look-ups and control | short, knowledge as index | 0.1 |
| medium (15-40B or ctx ≤ 40k) | 25 | look-ups + groups named in the last messages, `more_tools` | knowledge as index | ≤ 0.2 |
| large | by effort | all of the role | full | provider's |

Also for small and medium models: no sub-agents, no delegate, no background tasks. They lose the thread.

## Step limit by mode/profile

`max_steps = min(EFFORTS[effort]['steps'], limits[profile])`. For a large model, effort high uses `limits['max']`.
