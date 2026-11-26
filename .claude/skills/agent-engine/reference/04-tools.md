# 4. Tools: one registry, risks, validation

## The Tool object

```python
from dataclasses import dataclass, field

RISKS = ('read', 'change', 'danger', 'ui', 'run', 'control')


class ToolError(ValueError):
    """A problem the model can fix (bad args, unknown id): its message goes back as the tool result."""


@dataclass
class Tool:
    name: str
    description: str                # what it does + when to use it: the model reads this
    schema: dict                    # JSON schema of the arguments (type object)
    mutating: bool
    run: object = None              # (ctx, clean_args) -> (result dict, one-line summary for the UI)
    preview: object = None          # (clean_args) -> one line for the confirm card
    check: object = None            # (ctx, clean_args) -> clean_args: deeper checks before it is proposed
    danger_if: object = None        # (clean_args) -> True: this call is dangerous (overwrite=true ...)
    confirm_if: object = None       # (ctx, clean_args) -> True: a read that still waits for the user (web search)
    control: bool = False           # handled by the engine (05)
    domain: str = 'common'          # which agents get it (roles pick domains)
    group: str = ''                 # for mid-size models: loaded on demand (08)
    risk: str = ''                  # '' = derived from control / mutating

    def __post_init__(self):
        self.risk = self.risk or ('control' if self.control else 'change' if self.mutating else 'read')
        assert self.risk in RISKS, self.risk

    def spec(self):
        return {'name': self.name, 'description': self.description, 'parameters': self.schema}

    def risk_of(self, clean):
        return 'danger' if self.risk == 'change' and self.danger_if and self.danger_if(clean or {}) else self.risk


REGISTRY = {}


def register(tool):
    assert tool.name not in REGISTRY, tool.name
    REGISTRY[tool.name] = tool
    return tool
```

`tools/__init__.py` imports every module of the folder (`pkgutil.iter_modules`). A new tool is a new
`register(Tool(...))` in the right file and nothing else.

## Risks

| risk | ask mode | auto mode | plan mode |
|---|---|---|---|
| `read` | runs at once | runs at once | runs at once |
| `change` | confirm card | runs at once | not offered |
| `danger` (delete, overwrite) | confirm card | **confirm card** | not offered |
| `ui` (a page action: switch view, filter) | an Apply button | applied | not offered |
| `run` (starts long work; the page reports the end) | confirm card | runs, waits for the page's result | not offered |
| `control` | engine | engine | engine |

`for_role(domains, mode)` = the tools of those domains + `control` + `common`, minus `change/danger/ui/run` in plan
mode, minus `submit_plan` outside plan mode, minus `enter_plan_mode` outside auto.

## Schema helpers (short, readable tool definitions)

```python
def obj(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}

S = lambda d: {'type': 'string', 'description': d}                                          # noqa: E731
I = lambda d, lo=0, hi=None: {'type': 'integer', 'description': d, 'minimum': lo, **({'maximum': hi} if hi else {})}  # noqa: E731,E741
A = lambda d: {'type': 'array', 'items': {'type': 'string'}, 'description': d}              # noqa: E731
B = lambda d: {'type': 'boolean', 'description': d}                                         # noqa: E731
E = lambda d, values: {'type': 'string', 'enum': list(values), 'description': d}            # noqa: E731


def _search_items(ctx, a):
    rows = db_search(ctx.user, a.get('query', ''), a.get('state'), limit=a.get('limit', 50))
    return {'count': len(rows), 'items': rows[:50]}, f'→ {len(rows)} items'


register(Tool('search_items', 'Find items by words and state. Use before changing an item to get its exact id.',
              obj({'query': S('words to match'), 'state': E('filter by state', ('open', 'closed')),
                   'limit': I('max rows', 1, 200)}), False, _search_items, domain='items'))
register(Tool('delete_item', 'Delete one item by id (cannot be undone).', obj({'id': S('the item id')}, ['id']),
              True, lambda ctx, a: (db_delete(ctx.user, a['id']), f'deleted {a["id"]}'),
              preview=lambda a: f'Delete item {a["id"]}', domain='items', risk='danger'))
```

## validate(): minimal and forgiving

Models make small type mistakes. Fix the harmless ones and refuse the rest with a message the model can act on.

```python
_TYPES = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool, 'array': list, 'object': dict}


def validate(tool, args):
    """(clean args, None) or (None, error message)."""
    if not isinstance(args, dict):
        return None, 'arguments must be a JSON object'
    props, out = tool.schema.get('properties') or {}, {}
    for k, v in args.items():
        sch = props.get(k)
        if sch is None or v is None:
            continue                                    # unknown keys are dropped, not an error
        t = sch.get('type')
        if t == 'array' and isinstance(v, str):
            v = [v]                                     # "a" where ["a"] was wanted
        if t == 'integer' and isinstance(v, float) and v.is_integer():
            v = int(v)                                  # 3.0 -> 3
        if t == 'integer' and isinstance(v, str) and v.strip().lstrip('-').isdigit():
            v = int(v)                                  # "3" -> 3
        if t == 'boolean' and isinstance(v, str) and v.lower() in ('true', 'false'):
            v = v.lower() == 'true'
        if t in _TYPES and (not isinstance(v, _TYPES[t]) or (t in ('integer', 'number') and isinstance(v, bool))):
            return None, f'{k}: expected {t}'
        if 'enum' in sch and v not in sch['enum']:
            return None, f'{k}: one of {", ".join(map(str, sch["enum"]))}'
        if 'minimum' in sch and v < sch['minimum'] or 'maximum' in sch and v > sch['maximum']:
            return None, f'{k}: between {sch.get("minimum")} and {sch.get("maximum")}'
        out[k] = v
    missing = [k for k in tool.schema.get('required') or () if k not in out]
    return (None, f'missing: {", ".join(missing)}') if missing else (out, None)
```

`prepare(ctx, name, args, tools)` = validate + `check` + `preview` → `(clean, summary)` or a raised `ToolError`.
`run(ctx, name, args, tools)` = prepare + `tool.run`. A raised exception becomes an `is_error` result with its
message (`ToolError` as is; anything else as "internal error: <type>"). The turn never crashes.

## Results the model can use

- Return **compact JSON**: ids, names, counts. Lists ≤ 50 entries plus a `"more": N` count, text ≤ 8000 chars, the
  whole result ≤ 6000 chars after dumping (cut with "…").
- The `summary` (the second return value) is for the UI row: `→ 12 items`, `created report.md`.
- A change returns what it did and an **undo id** when it can be undone.
- A long job returns `job_id`. A `job_status(job_id)` read tool lets the model check it later. Do not block the turn.

## Tool groups (for mid-size models)

Give each tool a `group`. A model with the *medium* profile gets the look-up tools plus the groups whose words
appear in the last messages. The prompt lists the other groups by name, and `more_tools {group}` (or calling a tool
that is not loaded) loads one for the rest of the turn. That is about 6k tokens of prompt and tools instead of 10-12k.

For a *small* model, `pick_tools(all, question, n=8)` scores tools by word overlap: 3 × matches in the name + matches in the
description. It keeps the top 8 plus the control and look-up tools that are always on.
