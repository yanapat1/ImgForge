"""
The Data Center tools: one registry the in-app agent (llm/agent.py), the CLI (`python -m dataviz dc`) and the
skill (.claude/skills/datacenter/SKILL.md) all use, so the three can never drift apart.

    TOOLS = {name: Tool(name, description, schema, mutating, run(user, args) -> (result, summary))}

Every tool goes through `datacenter.service` only (imported lazily: the service module is optional at import
time and tests swap it). Read-only tools run at once; mutating ones are only ever run after the user confirmed
them (agent.confirm / CLI --yes). Results are compact (lists truncated, totals given) because they are fed back
to the model on every step. Buckets may be named by id or by exact name; an ambiguous name is an error that
tells the model to ask, never a guess.
"""

import json
from dataclasses import dataclass, field

MAX_LIST = 50                   # entries / files / buckets per result
MAX_TEXT = 8000                 # characters of a text file per result


class ToolError(ValueError):
    """A problem the model can fix (bad args, unknown bucket): its message goes back as the tool result."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    mutating: bool
    run: object = None                      # (user, args) -> (result dict, summary str)
    preview: object = None                  # (bucket dict | None, args) -> one line for a plan
    control: bool = False                   # ask_user / cannot_do: they end the agent's turn
    s3_ok: bool = False                     # allowed on a read-only (S3) bucket
    kind: str = field(init=False, default='read')

    def __post_init__(self):
        self.kind = 'control' if self.control else 'change' if self.mutating else 'read'

    def spec(self):
        """What a provider is given: {name, description, parameters}."""
        return {'name': self.name, 'description': self.description, 'parameters': self.schema}


def _svc():
    from ..datacenter import service
    return service


# ---------------------------------------------------------------- schema helpers
def _obj(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


S = lambda d: {'type': 'string', 'description': d}                                    # noqa: E731
I = lambda d, lo=0, hi=None: {'type': 'integer', 'description': d, 'minimum': lo, **({'maximum': hi} if hi else {})}  # noqa: E731,E741
A = lambda d: {'type': 'array', 'items': {'type': 'string'}, 'description': d}        # noqa: E731
BUCKET = S('bucket id, or its exact name (look it up with list_buckets first)')
PATH = S("path inside the bucket, POSIX, no leading slash; '' is the bucket root")
PATHS = A('paths inside the bucket (files or folders), exactly as list_folder / search returned them')

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
        py = _TYPES.get(t)
        if py and (not isinstance(v, py) or (t in ('integer', 'number') and isinstance(v, bool))):
            return None, f'{k} must be a{"n" if t[0] in "aeiou" else ""} {t}'
        if t == 'array' and (sch.get('items') or {}).get('type') == 'string' and not all(isinstance(x, str) for x in v):
            return None, f'{k} must be a list of strings'
        if 'enum' in sch and v not in sch['enum']:
            return None, f'{k} must be one of {", ".join(map(str, sch["enum"]))}'
        if t == 'integer' and ('minimum' in sch and v < sch['minimum'] or 'maximum' in sch and v > sch['maximum']):
            return None, f'{k} must be between {sch.get("minimum")} and {sch.get("maximum")}'
        out[k] = v
    missing = [k for k in tool.schema.get('required') or [] if k not in out]
    if missing:
        return None, f'missing required argument{"s" if len(missing) > 1 else ""}: {", ".join(missing)}'
    for k in tool.schema.get('required') or []:
        if props.get(k, {}).get('type') == 'array' and not out[k]:
            return None, f'{k} must not be empty'
    return out, None


# ---------------------------------------------------------------- buckets
def resolve_bucket(user, ref):
    """The bucket dict named by id or exact name (a unique case-insensitive name also counts). ToolError when
    there is none or the name is ambiguous: the model must ask, never pick one."""
    ref = str(ref or '').strip()
    if not ref:
        raise ToolError('bucket is required (an id or an exact name from list_buckets)')
    buckets = _svc().list_buckets(user)
    hit = [b for b in buckets if str(b.get('id')) == ref]
    if hit:
        return hit[0]
    for same in (lambda b: b.get('name') == ref, lambda b: str(b.get('name', '')).lower() == ref.lower()):
        hit = [b for b in buckets if same(b)]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            raise ToolError(f'{len(hit)} buckets are named {ref!r} (ids: {", ".join(str(b["id"]) for b in hit)}): '
                            'call ask_user to let the user pick one')
    names = ', '.join(repr(b.get('name')) for b in buckets[:15]) or 'none'
    raise ToolError(f'no bucket {ref!r}. Buckets you can use: {names}{" ..." if len(buckets) > 15 else ""}')


def _writable(b, tool):
    if not b.get('writable', True) and not tool.s3_ok:
        kind = 's3 buckets are read-only' if b.get('kind') == 's3' else 'it is read-only'
        raise ToolError(f'bucket {b.get("name")!r} cannot be changed ({kind}): use cannot_do, offer copying '
                        'into a managed bucket instead')


def _tags(tags):
    return [t['tag'] if isinstance(t, dict) else str(t) for t in tags or []]


def _bucket_row(b):
    st = b.get('stats') or {}
    fmt = b.get('format')
    return {'id': b.get('id'), 'name': b.get('name'), 'kind': b.get('kind'), 'writable': b.get('writable'),
            'format': fmt.get('format') if isinstance(fmt, dict) else fmt, 'tags': _tags(b.get('tags')),
            'images': st.get('images'), 'size': st.get('size')}


def _cut(rows, n=MAX_LIST):
    rows = list(rows or [])
    return rows[:n], len(rows)


def _fmt(f):
    if not isinstance(f, dict):
        return f
    return {k: f.get(k) for k in ('format', 'label', 'confidence', 'classes', 'reason', 'checks') if k in f}


def _plural(n, w, many=None):
    return f'{n} {w if n == 1 else many or w + "s"}'


def _bname(b, args):
    return repr(b.get('name')) if b else repr(args.get('bucket'))


# ---------------------------------------------------------------- read-only tools
def _list_buckets(user, a):
    rows, total = _cut(_svc().list_buckets(user, q=a.get('q', ''), tags=tuple(a.get('tags') or ()),
                                           formats=tuple(a.get('formats') or ())))
    return {'total': total, 'buckets': [_bucket_row(b) for b in rows], 'truncated': total > len(rows)}, \
        f'→ {_plural(total, "bucket")}'


def _entry(e):
    out = {k: e.get(k) for k in ('name', 'path', 'type', 'kind', 'size', 'items', 'labels') if e.get(k) is not None}
    return out


def _list_folder(user, a):
    b = resolve_bucket(user, a['bucket'])
    lim = min(int(a.get('limit') or MAX_LIST), 200)
    res = _svc().ls(user, b['id'], a.get('path', ''), offset=int(a.get('offset') or 0), limit=lim,
                    sort=a.get('sort') or 'name', q=a.get('q', ''))
    ents = res.get('entries') or []
    out = {'bucket': b['id'], 'path': res.get('path', a.get('path', '')), 'total': res.get('total', len(ents)),
           'offset': int(a.get('offset') or 0), 'entries': [_entry(e) for e in ents[:lim]],
           'format': _fmt(res.get('format')), 'tags': _tags(res.get('tags')), 'writable': res.get('writable')}
    if out['offset'] + len(out['entries']) < out['total']:
        out['more'] = f'{out["total"] - out["offset"] - len(out["entries"])} more: call again with a higher offset'
    return out, f'→ {_plural(out["total"], "entry", "entries")}'


def _search(user, a):
    bid = resolve_bucket(user, a['bucket'])['id'] if a.get('bucket') else None
    res = _svc().search(user, q=a.get('q', ''), tags=tuple(a.get('tags') or ()), formats=tuple(a.get('formats') or ()),
                        bid=bid, path=a.get('path', ''), glob=a.get('glob'), limit=200)
    files, nf = _cut(res.get('files'))
    bks, nb = _cut(res.get('buckets'), 20)
    out = {'buckets': [_bucket_row(b) for b in bks], 'buckets_total': nb,
           'files': [{k: f.get(k) for k in ('bucket', 'path', 'type', 'size')} for f in files], 'files_total': nf}
    if nf > len(files) or nf >= 200:
        out['note'] = 'more matches than shown: narrow the search (bucket, path, glob)'
    return out, f'→ {_plural(nf, "match", "matches")}' + (f', {_plural(nb, "bucket")}' if nb else '')


def _detect(user, a):
    b = resolve_bucket(user, a['bucket'])
    f = _svc().detect(user, b['id'], a.get('path', ''), force=bool(a.get('force')))
    f = _fmt(f) or {}
    return f, f'→ {f.get("label") or f.get("format")}' + (f' ({f["confidence"]:.0%})' if isinstance(f.get('confidence'), (int, float)) else '')


def _stats(user, a):
    b = resolve_bucket(user, a['bucket'])
    st = _svc().stats(user, b['id'], a.get('path', ''))
    return st, f'→ {_plural(int(st.get("images") or 0), "image")}, {_plural(int(st.get("boxes") or 0), "box", "boxes")}'


def _read_text(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().read_text(user, b['id'], a['path'], limit=MAX_TEXT)
    text = str(res.get('text') or '')
    cut = len(text) > MAX_TEXT
    return {'text': text[:MAX_TEXT], 'truncated': bool(res.get('truncated')) or cut}, f'→ {len(text[:MAX_TEXT])} chars'


# ---------------------------------------------------------------- mutating tools
def _ops_result(res, verb):
    applied = res.get('applied') or []
    errors = res.get('errors') or []
    out = {'applied': len(applied), 'errors': [{'path': (e.get('op') or {}).get('path'), 'error': e.get('error')}
                                               for e in errors[:20]],
           'undo_id': res.get('undo_id'), 'retagged': res.get('retagged')}
    s = f'{verb} {len(applied)}' + (f', {_plural(len(errors), "error")}' if errors else '')
    return out, s


def _ops(user, a, ops, verb):
    b = resolve_bucket(user, a['bucket'])
    return _ops_result(_svc().ops(user, b['id'], ops), verb)


def _folder(p):
    return (p or '').strip('/') + '/' if (p or '').strip('/') else '(root)'


def _mk(user, a):
    return _ops(user, a, [{'op': 'mkdir', 'path': a['path']}], 'created')


def _move(user, a):
    return _ops(user, a, [{'op': 'move', 'path': p, 'to': a['to']} for p in a['paths']], 'moved')


def _copy(user, a):
    return _ops(user, a, [{'op': 'copy', 'path': p, 'to': a['to']} for p in a['paths']], 'copied')


def _rename(user, a):
    return _ops(user, a, [{'op': 'rename', 'path': a['path'], 'name': a['name']}], 'renamed')


def _delete(user, a):
    return _ops(user, a, [{'op': 'delete', 'path': p} for p in a['paths']], 'deleted (to trash)')


def _set_tags(user, a, add):
    b = resolve_bucket(user, a['bucket'])
    tags = _svc().set_tags(user, b['id'], a.get('path', ''), add=tuple(a['tags']) if add else (),
                           remove=() if add else tuple(a['tags']))
    return {'tags': _tags(tags)}, f'{"tagged" if add else "untagged"} {", ".join(a["tags"])}'


def _send(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().send(user, b['id'], a['paths'], target=a.get('target', 'visualize'), name=a.get('name'),
                      task_type=a.get('task_type', 'detect'))
    return res, f'opened {res.get("target")} {res.get("url") or res.get("id")}'


def _items(paths):
    paths = list(paths or [])
    shown = ', '.join(paths[:3]) + (f' +{len(paths) - 3} more' if len(paths) > 3 else '')
    return f'{_plural(len(paths), "item")} ({shown})'


TOOLS = {}


def _add(t):
    TOOLS[t.name] = t


_add(Tool('list_buckets', 'List the buckets (data roots) the user can see, with format, tags and image counts.',
          _obj({'q': S('filter: name contains (optional)'), 'tags': A('only buckets with all these tags'),
                'formats': A('only these formats, e.g. yolo-detect, coco')}), False, _list_buckets))
_add(Tool('list_folder', 'List one folder of a bucket (paged), with its detected format and tags.',
          _obj({'bucket': BUCKET, 'path': PATH, 'offset': I('skip this many entries'),
                'limit': I(f'entries per page (default {MAX_LIST}, max 200)', 1, 200),
                'sort': {'type': 'string', 'enum': ['name', '-mtime', 'size'], 'description': 'order'},
                'q': S('only names containing this')}, ['bucket']), False, _list_folder))
_add(Tool('search', 'Find buckets and files by name (case-insensitive substring), glob (e.g. "*night*.jpg"), tags '
                    'or format. Optional bucket / path narrow it.',
          _obj({'q': S('name contains'), 'tags': A('tags'), 'formats': A('formats'), 'bucket': BUCKET,
                'path': S('folder to search under'), 'glob': S('file name glob like *night*.jpg')}), False, _search))
_add(Tool('detect_format', 'Detect the dataset format of a folder (yolo-detect, coco, voc, classification ...) '
                           'with the checks that decided it.',
          _obj({'bucket': BUCKET, 'path': PATH, 'force': {'type': 'boolean', 'description': 'ignore the cache'}},
               ['bucket']), False, _detect))
_add(Tool('get_stats', 'Counts of a folder: images, labels, boxes, classes, size, images per split.',
          _obj({'bucket': BUCKET, 'path': PATH}, ['bucket']), False, _stats))
_add(Tool('read_text', f'Read a text file (yaml, txt, json, xml ...), the first {MAX_TEXT} characters.',
          _obj({'bucket': BUCKET, 'path': S('file path')}, ['bucket', 'path']), False, _read_text))

_add(Tool('create_folder', 'Create a folder.', _obj({'bucket': BUCKET, 'path': S('new folder path')}, ['bucket', 'path']),
          True, _mk, lambda b, a: f'Create folder {_folder(a["path"])} in {_bname(b, a)}'))
_add(Tool('move', 'Move files / folders into another folder of the same bucket.',
          _obj({'bucket': BUCKET, 'paths': PATHS, 'to': S("destination folder ('' = root)")}, ['bucket', 'paths', 'to']),
          True, _move, lambda b, a: f'Move {_items(a["paths"])} → {_folder(a["to"])} in {_bname(b, a)}'))
_add(Tool('copy', 'Copy files / folders into another folder of the same bucket.',
          _obj({'bucket': BUCKET, 'paths': PATHS, 'to': S("destination folder ('' = root)")}, ['bucket', 'paths', 'to']),
          True, _copy, lambda b, a: f'Copy {_items(a["paths"])} → {_folder(a["to"])} in {_bname(b, a)}'))
_add(Tool('rename', 'Rename one file or folder (a new name, not a path).',
          _obj({'bucket': BUCKET, 'path': S('what to rename'), 'name': S('the new name')}, ['bucket', 'path', 'name']),
          True, _rename, lambda b, a: f'Rename {a["path"]} → {a["name"]} in {_bname(b, a)}'))
_add(Tool('delete', 'Delete files / folders (they go to the trash and can be undone).',
          _obj({'bucket': BUCKET, 'paths': PATHS}, ['bucket', 'paths']),
          True, _delete, lambda b, a: f'Delete {_items(a["paths"])} from {_bname(b, a)} (to trash, undoable)'))
_add(Tool('add_tags', "Add user tags to a bucket ('' path) or a folder / file. format:* tags are automatic.",
          _obj({'bucket': BUCKET, 'path': PATH, 'tags': A('tags to add')}, ['bucket', 'tags']),
          True, lambda u, a: _set_tags(u, a, True),
          lambda b, a: f'Tag {a.get("path") or "the bucket"} in {_bname(b, a)}: + {", ".join(a["tags"])}', s3_ok=True))
_add(Tool('remove_tags', 'Remove user tags (automatic format:* tags cannot be removed).',
          _obj({'bucket': BUCKET, 'path': PATH, 'tags': A('tags to remove')}, ['bucket', 'tags']),
          True, lambda u, a: _set_tags(u, a, False),
          lambda b, a: f'Untag {a.get("path") or "the bucket"} in {_bname(b, a)}: − {", ".join(a["tags"])}', s3_ok=True))
_add(Tool('send_to_session', 'Open folders / files in the visualizer (target visualize) or as a labeling project '
                             '(target label).',
          _obj({'bucket': BUCKET, 'paths': PATHS,
                'target': {'type': 'string', 'enum': ['visualize', 'label'], 'description': 'where to open it'},
                'name': S('session / project name (optional)'),
                'task_type': {'type': 'string', 'description': 'label task type (default detect)',
                              'enum': ['detect', 'segment', 'pose', 'obb', 'classify']}},
               ['bucket', 'paths', 'target']),
          True, _send, lambda b, a: f'Send {_items(a["paths"])} from {_bname(b, a)} to '
                                    f'{"the visualizer" if a["target"] == "visualize" else "a labeling project"}'
                                    + (f' as {a["name"]!r}' if a.get('name') else ''), s3_ok=True))

_add(Tool('ask_user', 'Ask the user a clarifying question with 2-5 concrete options instead of guessing. Ends your turn.',
          _obj({'question': S('one short question'), 'options': A('2-5 concrete answers the user can click')},
               ['question', 'options']), False, control=True))
_add(Tool('cannot_do', 'Say the request cannot be done with these tools, why, and the closest thing you can do. '
                       'Ends your turn.',
          _obj({'reason': S('why not, one sentence'), 'alternative': S('the closest thing you can do (optional)')},
               ['reason']), False, control=True))


def specs():
    return [t.spec() for t in TOOLS.values()]


def catalog():
    """The tool catalogue as a Markdown table (the agent's system prompt and SKILL.md show the same one)."""
    rows = ['| tool | kind | arguments | what it does |', '|---|---|---|---|']
    for t in TOOLS.values():
        req = set(t.schema.get('required') or [])
        args = ', '.join(k if k in req else k + '?' for k in (t.schema.get('properties') or {}))
        rows.append(f'| `{t.name}` | {t.kind} | {args} | {t.description} |')
    return '\n'.join(rows)


def prepare(user, name, args):
    """A mutating call checked before it goes into a plan -> (clean args with the bucket's id, summary line).
    ToolError for anything the model must fix first (unknown / read-only bucket, bad args)."""
    t = TOOLS.get(name)
    if t is None:
        raise ToolError(f'unknown tool {name!r}')
    clean, err = validate(t, args)
    if err:
        raise ToolError(err)
    b = resolve_bucket(user, clean['bucket']) if 'bucket' in clean else None
    if b is not None:
        _writable(b, t)
        clean['bucket'] = b['id']
    return clean, t.preview(b, clean) if t.preview else f'{name} {json.dumps(clean)}'


def run(user, name, args):
    """Validate and run one tool -> (result, summary). ToolError for bad args; service errors pass through."""
    t = TOOLS.get(name)
    if t is None:
        raise ToolError(f'unknown tool {name!r}; the tools are: {", ".join(TOOLS)}')
    if t.control:
        raise ToolError(f'{name} is answered by the user, not run')
    clean, err = validate(t, args)
    if err:
        raise ToolError(err)
    if t.mutating and 'bucket' in clean:
        _writable(resolve_bucket(user, clean['bucket']), t)
    return t.run(user, clean)
