"""
The assistant's tools: one registry, filled by every module of this folder (each calls `register(Tool(...))`), so
a new tool is a new function in the right file, nothing else.

    REGISTRY = {name: Tool}           every tool, all domains
    TOOLS    = {name: Tool}           the Bucket page agent's set (data + control), the CLI and SKILL.md
    for_role(role, mode)              the tools one agent may call (llm/engine/roles.py picks the domains)

A tool has a `domain` ('bucket' the Bucket page, 'data' the Data page, 'visual' the visualizer, 'web', 'common' skills / docs, 'control' the
turn enders and the planner checklist, 'plugin' a user's HTTP tool) and a `risk`:

    read     runs at once, its compact result goes back to the model
    change   changes something: proposed for confirmation (mode ask) or run at once (mode auto)
    danger   delete / overwrite: always confirmed by the user, in every mode
    ui       a visualizer page action (switch the projection ...): an Apply button, or applied in mode auto
    control  answered by the user or the engine (ask_user, cannot_do, todo, submit_plan, enter_plan_mode)

A read tool may also have `confirm_if(ctx, clean)`: when it says True the call is shown on a confirm card (risk
`confirm_risk`: 'web' for web search, 'query' for a database query in mode ask / plan) like a change, and the user
may edit its arguments before it runs.

The two agents never share data tools: the visual agent's domains do not include 'bucket', so a Bucket call
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
    domain: str = 'bucket'
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
    """The Bucket page service (lazy: optional at import time, and tests swap it through this name)."""
    from ...bucket import service
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
TOOLS = {}                      # the Bucket page set, in registration order (data + control)


def register(t):
    assert t.name not in REGISTRY, f'tool {t.name} registered twice'
    REGISTRY[t.name] = t
    if t.domain in ('bucket', 'control') and t.name not in CONTROL_ONLY_ENGINE:
        TOOLS[t.name] = t
    return t


CONTROL_ONLY_ENGINE = {'todo', 'submit_plan', 'more_tools', 'finish_task'}     # the engine's planner tools: not part of the old DC agent / CLI


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


# ---------------------------------------------------------------- tool groups (a mid-size model's tool set)
# A model of 15-40B (engine/react.py profile 'medium') chooses badly among 30-40 tools and pays ~7k tokens for their
# definitions. It gets the tools outside every group (the look-ups, the page's main actions, control, navigation)
# plus the groups whose words appear in the last messages; `more_tools {group}` loads another one, and a call to a
# tool of a group that is not loaded loads it. Whole groups in registry order: the same request keeps the same
# set, so a local server's prompt cache holds between turns (per-tool picking changed it on every message).
GROUPS = {
    # Bucket page
    'files': ('create, move, copy, rename, delete, write or undo files and folders',
              'create_folder move copy copy_to_bucket rename delete write_text_file undo list_changes mark_seen',
              'move copy paste rename delete remove trash folder mkdir create write edit undo organise organize split restore changed modified seen '
              'duplicate duplicates clean tidy yaml'),
    'tags': ('add or remove tags', 'add_tags remove_tags', 'tag tags tagged untag label labels'),
    's3': ('S3 links: stream / local mode, download folders, compare with S3',
           'set_s3_mode sync_from_s3 download_s3_folders check_s3_local compare_s3_local remove_local_copy',
           's3 stream streamed download downloaded local sync resync cloud mirror rustfs minio'),
    'transfer': ('import a dataset from the web, export to S3 / FTP / SFTP',
                 'inspect_dataset_source import_dataset export_to',
                 'import export kaggle hugging huggingface roboflow zenodo openml url ftp sftp upload fetch'),
    'video': ('cut videos into image frames', 'extract_frames set_video_auto', 'video videos frame frames mp4 mov fps clip'),
    'sessions': ('make a visualize / labeling session from a folder, and check what it can do', 'session_check send_to_session reformat_folder',
                 'visualize visualizer visualise labeling labelling session sessions send open map umap tsne project '
                 'reformat format convert coco voc yolo'),
    'buckets': ('create, rename or delete a bucket', 'create_bucket update_bucket delete_bucket',
                'unlink'),
    # Data page
    'stats': ('row samples, profiles, statistic tests, forecasts, a query\'s cost',
              'sample_rows profile_result stat_test forecast explain_query',
              'sample rows profile test significant significance correlation correlate forecast predict trend slow '
              'explain statistic statistics distribution outlier average mean median'),
    'write': ('check or convert SQL, describe columns, change data',
              'check_sql set_column_description run_change',
              'update insert delete change alter drop truncate temp describe description meaning fix syntax error '
              'convert translate merge'),
    'board': ('zones and notes on the visual builder board', 'read_board edit_board',
              'board zone zones note notes sticky builder diagram arrange group'),
    'dashboard': ('add charts to the dashboard', 'build_dashboard', 'dashboard chart charts kpi graph plot bar line donut'),
    # Visualizer
    'issues': ('find issues, the selection, text search, curation scans',
               'issue_summary show_issue run_scan selection_view search_boxes_by_text fit_view',
               'issue issues error errors wrong mislabeled mislabelled duplicate duplicates outlier outliers suspicious '
               'cleanlab scan curation select selected selection similar search find zoom fit'),
    'models': ('models: list, run, evaluate, class statistics',
               'list_models run_model run_eval_all eval_summary compute_stats',
               'model models eval evaluate evaluation map precision recall predict prediction yolo confusion stats '
               'leaderboard ap'),
    'compare': ('compare sessions: shift, tests, stress, map quality',
                'compare_shift compare_tests compare_stress projection_quality',
                'compare comparison shift camera cameras stress drift quality trust trustworthiness'),
    'augment': ('augmentation recipes and coverage', 'augment_catalog set_augment_recipe augment_coverage',
                'augment augmentation augmentations albumentations recipe coverage flip blur noise'),
}
_GROUP_OF = {n: g for g, (_, names, _) in GROUPS.items() for n in names.split()}
_GROUP_WORDS = {g: frozenset(words.split()) for g, (_, _, words) in GROUPS.items()}


def group_of(name):
    """The group a tool loads with (None: always offered)."""
    return _GROUP_OF.get(name)


def pick_groups(all_tools, text, extra=()):
    """{name: Tool} for a mid-size model: every tool outside the groups, plus the groups `text` (the last user
    messages) names or `extra` lists, in the order of `all_tools`."""
    import re
    words = set(re.findall(r'[a-z0-9]+', str(text or '').lower()))
    on = {g for g, kw in _GROUP_WORDS.items() if words & kw} | set(extra)
    return {k: t for k, t in all_tools.items() if _GROUP_OF.get(k) is None or _GROUP_OF[k] in on}


def groups_card(all_tools, loaded):
    """The prompt's line on the tool groups that are not loaded (only those this role has)."""
    have = {_GROUP_OF[k] for k in all_tools if k in _GROUP_OF}
    rest = [g for g in GROUPS if g in have and not any(_GROUP_OF.get(k) == g for k in loaded)]
    if not rest:
        return ''
    return ('More tools, not loaded yet (call `more_tools` with the group when the request needs one): '
            + '; '.join(f'`{g}`: {GROUPS[g][0]}' for g in rest) + '.')


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
        if t.name in ('enter_plan_mode', 'more_tools', 'finish_task'):     # auto only / named by the groups line: kept out, so the
            continue                                        # card (cached) is the same in every mode
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


# every module of this folder registers its tools (bucket first: TOOLS keeps the order the CLI shows)
from . import bucket  # noqa: E402
from .bucket import resolve_bucket  # noqa: E402,F401  (the CLI and tests use it from here)

for _m in pkgutil.iter_modules(__path__):
    if _m.name != 'bucket':
        importlib.import_module(f'{__name__}.{_m.name}')
