"""
The assistant's tools: one registry, filled by every module of this folder (each calls `register(Tool(...))`), so
a new tool is a new function in the right file, nothing else.

    REGISTRY = {name: Tool}           every tool, all domains
    TOOLS    = {name: Tool}           the Data Center agent's set (data + control), the CLI and SKILL.md
    for_role(role, mode)              the tools one agent may call (llm/engine/roles.py picks the domains)

A tool has a `domain` ('data' Data Center, 'visual' the visualizer, 'web', 'common' skills / docs, 'control' the
turn enders and the planner checklist, 'plugin' a user's HTTP tool) and a `risk`:

    read     runs at once, its compact result goes back to the model
    change   changes something: proposed for confirmation (mode ask) or run at once (mode auto)
    danger   delete / overwrite: always confirmed by the user, in every mode
    ui       a visualizer page action (switch the projection ...): an Apply button, or applied in mode auto
    control  answered by the user or the engine (ask_user, cannot_do, todo, submit_plan, enter_plan_mode)

A read tool may also have `confirm_if(ctx, clean)`: when it says True the call is shown on a confirm card (risk
`confirm_risk`: 'web' for web search, 'query' for a database query in mode ask / plan) like a change, and the user
may edit its arguments before it runs.

The two agents never share data tools: the visual agent's domains do not include 'data', so a Data Center call
from it is an unknown tool, checked on the server. `run` takes (user, args) for the data tools (the CLI shares
them) and (ctx, args) for the tools marked `ctx=True` (they need the session, the role, the user's skills).
"""

import importlib
import json
import pkgutil
from dataclasses import dataclass, field

MAX_LIST = 50                   # entries / files / buckets per result
MAX_TEXT = 8000                 # characters of a text file per result
# read runs at once · change: confirmed in ask, runs in auto · danger: always confirmed · ui: a page action (Apply
# button in ask, applied in auto) · run: a page action that starts work (a model run, a map, a scan): confirmed in
# ask, runs in auto, the page reports its result (engine/uiwait.py) · control: handled by the engine
RISKS = ('read', 'change', 'danger', 'ui', 'run', 'control')


class ToolError(ValueError):
    """A problem the model can fix (bad args, unknown bucket): its message goes back as the tool result."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    mutating: bool
    run: object = None                      # (user | ctx, args) -> (result dict, summary str)
    preview: object = None                  # (bucket dict | None, args) -> one line for a confirm card
    control: bool = False                   # ask_user / cannot_do / todo / submit_plan: handled by the engine
    s3_ok: bool = False                     # allowed on a read-only (S3) bucket
    domain: str = 'data'
    risk: str = ''                          # '' = derived: control / change / read
    danger_if: object = None                # (clean args) -> True when this call is dangerous (an overwrite ...)
    check: object = None                    # (user | ctx, clean args) -> (clean, bucket | None): before a proposal
    ctx: bool = False                       # run / check take the agent context instead of the user
    confirm_if: object = None               # (ctx, clean args) -> True: this read call waits for the user first
                                            # (risk 'web' on the confirm card: web search's "Ask before each search")
    confirm_risk: str = 'web'               # the risk such a confirm card shows: web | query (a database query)
    kind: str = field(init=False, default='read')

    def __post_init__(self):
        self.kind = 'control' if self.control else 'change' if self.mutating else 'read'
        if not self.risk:
            self.risk = 'control' if self.control else 'change' if self.mutating else 'read'
        assert self.risk in RISKS, self.risk

    def spec(self):
        """What a provider is given: {name, description, parameters}."""
        return {'name': self.name, 'description': self.description, 'parameters': self.schema}

    def risk_of(self, clean):
        """The risk of one call: a change can be dangerous for some arguments (overwrite=true ...)."""
        if self.risk == 'change' and self.danger_if and self.danger_if(clean or {}):
            return 'danger'
        return self.risk


def _svc():
    """The Data Center service (lazy: optional at import time, and tests swap it through this name)."""
    from ...datacenter import service
    return service


# ---------------------------------------------------------------- schema helpers
def _obj(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


S = lambda d: {'type': 'string', 'description': d}                                    # noqa: E731
I = lambda d, lo=0, hi=None: {'type': 'integer', 'description': d, 'minimum': lo, **({'maximum': hi} if hi else {})}  # noqa: E731,E741
A = lambda d: {'type': 'array', 'items': {'type': 'string'}, 'description': d}        # noqa: E731
B = lambda d: {'type': 'boolean', 'description': d}                                   # noqa: E731
N = lambda d: {'type': 'number', 'description': d}                                    # noqa: E731
E = lambda d, values: {'type': 'string', 'enum': list(values), 'description': d}      # noqa: E731

_TYPES = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool, 'array': list, 'object': dict}


def validate(tool, args):
    """(clean args, None) or (None, error message). Minimal on purpose: required keys, types, enums, ranges;
    unknown keys are dropped. A bare string where a list of strings is wanted becomes [string]."""
    if not isinstance(args, dict):
        return None, 'arguments must be a JSON object'
    props = tool.schema.get('properties') or {}
    out = {}
    for k, v in args.items():
        sch = props.get(k)
        if sch is None or v is None:
            continue
        t = sch.get('type')
        if t == 'array' and isinstance(v, str):
            v = [v]
        if t == 'integer' and isinstance(v, float) and v.is_integer():
            v = int(v)
        if t == 'integer' and isinstance(v, str) and v.strip().lstrip('-').isdigit():
            v = int(v)
        if t == 'number' and isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        if t == 'boolean' and isinstance(v, str) and v.lower() in ('true', 'false'):
            v = v.lower() == 'true'
        py = _TYPES.get(t)
        if py and (not isinstance(v, py) or (t in ('integer', 'number') and isinstance(v, bool))):
            return None, f'{k} must be a{"n" if t[0] in "aeiou" else ""} {t}'
        if t == 'array' and (sch.get('items') or {}).get('type') == 'string' and not all(isinstance(x, str) for x in v):
            return None, f'{k} must be a list of strings'
        if 'enum' in sch and v not in sch['enum']:
            return None, f'{k} must be one of {", ".join(map(str, sch["enum"]))}'
        if t in ('integer', 'number') and ('minimum' in sch and v < sch['minimum'] or 'maximum' in sch and v > sch['maximum']):
            return None, f'{k} must be between {sch.get("minimum")} and {sch.get("maximum")}'
        out[k] = v
    missing = [k for k in tool.schema.get('required') or [] if k not in out]
    if missing:
        return None, f'missing required argument{"s" if len(missing) > 1 else ""}: {", ".join(missing)}'
    for k in tool.schema.get('required') or []:
        if props.get(k, {}).get('type') == 'array' and not out[k]:
            return None, f'{k} must not be empty'
    return out, None


# ---------------------------------------------------------------- registry
REGISTRY = {}
TOOLS = {}                      # the Data Center set, in registration order (data + control)


def register(t):
    assert t.name not in REGISTRY, f'tool {t.name} registered twice'
    REGISTRY[t.name] = t
    if t.domain in ('data', 'control') and t.name not in CONTROL_ONLY_ENGINE:
        TOOLS[t.name] = t
    return t


CONTROL_ONLY_ENGINE = {'todo', 'submit_plan'}     # the engine's planner tools: not part of the old DC agent / CLI


def for_role(domains, mode='ask', extra=()):
    """The tools of these domains (+ extra Tool objects, e.g. a user's plugin tools), as {name: Tool}.
    Mode plan offers no change / danger / ui / run tool: the agent researches and submits a plan instead."""
    out = {}
    for t in [*REGISTRY.values(), *extra]:
        if t.domain not in domains:
            continue
        if mode == 'plan' and t.risk in ('change', 'danger', 'ui', 'run'):
            continue
        if mode != 'plan' and t.name == 'submit_plan':
            continue
        if mode != 'auto' and t.name == 'enter_plan_mode':
            continue
        out[t.name] = t
    return out


def specs(tools=None):
    return [t.spec() for t in (TOOLS if tools is None else tools).values()]


def catalog(tools=None):
    """A tool catalogue as a Markdown table (the agents' system prompts and SKILL.md show the same one)."""
    rows = ['| tool | kind | arguments | what it does |', '|---|---|---|---|']
    for t in (TOOLS if tools is None else tools).values():
        req = set(t.schema.get('required') or [])
        args = ', '.join(k if k in req else k + '?' for k in (t.schema.get('properties') or {}))
        kind = t.kind if tools is None else t.risk
        rows.append(f'| `{t.name}` | {kind} | {args} | {t.description} |')
    return '\n'.join(rows)


RISK_LINE = (('read', 'Run at once (read-only)'), ('query', 'Database reads (the user approves each in ask / plan; at once in auto)'),
             ('change', 'Changes (confirmed as the mode says)'),
             ('danger', 'Always confirmed by the user'), ('ui', 'Page actions (Apply button, or applied in auto)'),
             ('run', 'Page runs (they start work; confirmed in ask)'), ('control', 'Turn control'))


def risk_card(tools):
    """What the native tool specs do not say: which tools run at once and which wait for the user, one line per
    kind. The names, arguments and descriptions already reach the model as the provider's tool definitions, so the
    system prompt does not repeat them (tools.catalog is still the table the CLI and SKILL.md show)."""
    by = {}
    for t in tools.values():
        if t.name == 'enter_plan_mode':     # auto only: kept out, so the card (cached) is the same in every mode
            continue
        k = 'query' if t.confirm_if and t.confirm_risk == 'query' else t.risk
        by.setdefault(k, []).append(f'`{t.name}`')
    return '\n'.join(f'- {label}: {", ".join(by[k])}' for k, label in RISK_LINE if by.get(k))


def _who(t, ctx_or_user):
    """A ctx tool gets the agent context; a user tool gets the user (from a ctx, or as given)."""
    if t.ctx:
        return ctx_or_user
    return getattr(ctx_or_user, 'user', ctx_or_user)


def prepare(user, name, args, tools=None):
    """A change checked before it is proposed or run -> (clean args, summary line). ToolError for anything the
    model must fix first (unknown / read-only bucket, bad args)."""
    t = (REGISTRY if tools is None else tools).get(name)
    if t is None:
        raise ToolError(f'unknown tool {name!r}')
    clean, err = validate(t, args)
    if err:
        raise ToolError(err)
    b = None
    if t.check:
        clean, b = t.check(_who(t, user), clean)
    return clean, t.preview(b, clean) if t.preview else f'{name} {json.dumps(clean, ensure_ascii=False)}'


def run(user, name, args, tools=None):
    """Validate and run one tool -> (result, summary). ToolError for bad args; service errors pass through."""
    reg = REGISTRY if tools is None else tools
    t = reg.get(name)
    if t is None:
        raise ToolError(f'unknown tool {name!r}; the tools are: {", ".join(reg if tools is not None else TOOLS)}')
    if t.control:
        raise ToolError(f'{name} is answered by the user, not run')
    clean, err = validate(t, args)
    if err:
        raise ToolError(err)
    if t.mutating and t.check:
        clean, _ = t.check(_who(t, user), clean)
    return t.run(_who(t, user), clean)


# every module of this folder registers its tools (datacenter first: TOOLS keeps the order the CLI shows)
from . import datacenter  # noqa: E402
from .datacenter import resolve_bucket  # noqa: E402,F401  (the CLI and tests use it from here)

for _m in pkgutil.iter_modules(__path__):
    if _m.name != 'datacenter':
        importlib.import_module(f'{__name__}.{_m.name}')
