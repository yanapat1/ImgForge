"""
The Bucket page tools (domain 'bucket'): one registry the Bucket agent (llm/engine), the old agent (llm/agent.py), the
CLI (`python -m dataviz bucket`) and the skill (.claude/skills/bucket/SKILL.md) all use, so they never drift apart.

Every tool goes through `bucket.service` only (via tools._svc: imported lazily, tests swap it). Read-only tools
run at once; changes run only after the user confirmed them (or at once in mode auto, never a 'danger' one).
Results are compact (lists truncated, totals given) because they are fed back to the model on every step. Buckets
may be named by id or by exact name; an ambiguous name is an error that tells the model to ask, never a guess.
"""

import sys

from . import A, B, I, MAX_LIST, MAX_TEXT, N, S, Tool, ToolError, _obj, register

BUCKET = S('bucket id, or its exact name (look it up with list_buckets first)')
PATH = S("path inside the bucket, POSIX, no leading slash; '' is the bucket root")
PATHS = A('paths inside the bucket (files or folders), exactly as list_folder / search returned them')


def _svc():
    return sys.modules[__package__]._svc()


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
    if b.get('writable', True) or tool.s3_ok:
        return
    if b.get('kind') == 's3' and b.get('local_folders'):
        return          # a streamed link with downloaded folders: the service allows changes inside those, per path
    if b.get('kind') == 's3':
        raise ToolError(f'bucket {b.get("name")!r} cannot be changed here (streamed from S3, read-only): propose '
                        'download_s3_folders for the folder to change (then change it there, only that prefix is downloaded), or offer '
                        'copying into a managed bucket')
    raise ToolError(f'bucket {b.get("name")!r} cannot be changed (it is read-only): use cannot_do, offer copying '
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
    out = {k: e.get(k) for k in ('name', 'path', 'type', 'kind', 'size', 'items', 'labels', 'local') if e.get(k) is not None}
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
    if res.get('partial'):
        out['partial'] = 'S3 is still listing this folder: call again in a moment for the rest'
    if res.get('offline'):
        out['offline'] = 'S3 is unreachable: only the downloaded folders are listed'
    if res.get('here'):
        h = res['here']
        out['local_copy'] = {k: h.get(k) for k in ('path', 'state', 'diff') if h.get(k) is not None}
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


def _copy_to(user, a):
    b = resolve_bucket(user, a['bucket'])
    d = resolve_bucket(user, a['to_bucket'])
    res = _svc().copy_to(user, b['id'], a['paths'], d['id'], a.get('to', ''))
    return res, (f'copy into {d["name"]} started' if res.get('job_id') else f'copied into {d["name"]}')


def _push(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().push(user, b['id'], a['paths'], a.get('overwrite') or ())
    return res, (f'{b["name"]}: upload to S3 started' if res.get('job_id') else f'{b["name"]}: uploaded')


def _list_changes(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().list_changes(user, b['id'], a.get('path', ''), 0, 200)
    return res, f'→ {_plural(int(res.get("total") or 0), "changed path")}'


def _mark_seen(user, a):
    b = resolve_bucket(user, a['bucket'])
    paths = a.get('paths') or []
    res = _svc().clear_changes(user, b['id'], paths, None if paths else a.get('path', ''))
    return res, f'{res.get("cleared", 0)} marked as seen'


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
    if a.get('target') == 'both':               # one linked session: Label owns the labels, Visualize reads it live
        if len(a['paths']) != 1:
            raise ToolError('send one folder at a time (the folder that holds the images)')
        res = _svc().create_session(user, b['id'], a['paths'][0], visualize=True, label=True, name=a.get('name'),
                                    task_type=a.get('task_type', 'detect'))
        return res, (f'created {res["name"]!r}: Visualize {res["visualize"]["url"]} + Label {res["label"]["url"]} (linked)'
                     + (f'; its labels are being imported (job {res["label"]["import_job_id"]})' if res['label'].get('import_job_id') else ''))
    res = _svc().send(user, b['id'], a['paths'], target=a.get('target', 'visualize'), name=a.get('name'),
                      task_type=a.get('task_type', 'detect'))
    return res, f'opened {res.get("target")} {res.get("url") or res.get("id")}'


def _reformat(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().reformat(user, b['id'], a.get('path') or '', to=a['to'])
    return res, f'{b["name"]}: re-format to {a["to"]} started → {res["bucket_name"]}/{res["path"]} (job {res["job_id"]})'


def _session_check(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().session_check(user, b['id'], a.get('path') or '')
    if res.get('scanning'):
        return res, (f'→ still reading the folder from S3 (job {res["job_id"]}): job_status it, then call '
                     'session_check again')
    can = [k for k in ('visualize', 'label', 'linked') if res['can'][k]]
    return res, f'→ {res.get("label") or res["format"]}: ' + (', '.join(can) if can else 'no session can read it')


def _s3_mode(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().update_bucket(user, b['id'], mode=a['mode'])
    how = 'loading into a local folder (a download job)' if a['mode'] == 'local' else 'streamed from S3 again'
    return {'bucket': res['id'], 'mode': res.get('mode'), 'job_id': res.get('job_id')}, f'{res["name"]}: {how}'


def _s3_sync(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().sync(user, b['id'])
    return res, f'{b["name"]}: re-sync from S3 started'


def _s3_fetch(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().fetch_local(user, b['id'], a['paths'], mirror=bool(a.get('mirror')))
    return res, f'{b["name"]}: downloading {_items(res["paths"])} to local' + (' (mirror)' if a.get('mirror') else '')


def _s3_check(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().check_local(user, b['id'])
    return res, f'{b["name"]}: comparing the downloaded folders with S3 (a job)'


def _job_status(user, a):
    res = _svc().job_status(user, a['job_id'], a.get('wait_s') or 0)
    pct = f' {res["pct"]:.0f}%' if isinstance(res.get('pct'), (int, float)) else ''
    return res, f'→ {res["label"]}: {res["state"]}{pct}' + (f' ({res["error"]})' if res.get('error') else '')


COMPARE_WAIT_S = 90


def _s3_compare(user, a):
    """Compare (or re-compare) the local copy of a folder with S3 and read one page of the files."""
    import time
    b = resolve_bucket(user, a['bucket'])
    svc = _svc()
    path = a.get('path') or ''
    sts = [s for s in (a.get('status') or []) if s]
    cur = svc.compare_result(user, b['id'], path, sts, '', 0, 1)
    if a.get('refresh') or not (cur['ready'] or cur['running']):
        svc.compare_cloud(user, b['id'], path, verify=bool(a.get('verify')))
    t0 = time.time()
    while True:
        got = svc.compare_result(user, b['id'], path, sts, a.get('q') or '', a.get('offset') or 0,
                                 min(int(a.get('limit') or MAX_LIST), MAX_LIST))
        if not got['running'] or time.time() - t0 > COMPARE_WAIT_S:
            break
        time.sleep(0.25)
    if not got['ready']:
        return {'path': got['path'], 'running': got['running'],
                'note': 'still comparing: call again in a moment'}, f'{b["name"]}: comparing with S3…'
    keep = ('path', 'counts', 'total', 'matched', 'offset', 'finished_at', 'verify', 'complete', 'running')
    out = {k: got.get(k) for k in keep}
    out['files'] = [{'path': r['path'], 'status': r['status'], **({'verified': r['verified']} if r.get('verified') else {}),
                     'local_size': (r['local'] or {}).get('size'), 'cloud_size': (r['cloud'] or {}).get('size')}
                    for r in got.get('rows') or []]
    out['statuses'] = got['labels']
    c = got['counts']
    mine = sum(c[k] for k in ('added_local', 'deleted_local', 'edited_local', 'both'))
    cloud = sum(c[k] for k in ('new_cloud', 'changed_cloud', 'removed_cloud'))
    return out, f'{b["name"]}/{path}: {mine} changed here, {cloud} in S3, {c["same"]} same'


def _s3_drop(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().drop_local(user, b['id'], a['paths'])
    return res, f'{b["name"]}: local copy of {_items(res["removed"])} removed'


def _export_target(user, a):
    """The export_to target of the tool args; the connection by id or exact name, the user's own only."""
    ref = str(a.get('connection') or '').strip()
    conns = [c for c in _svc()._datasets().connections(user['id']) if (c.get('kind') or 's3') == a['to']]
    hit = [c for c in conns if str(c.get('id')) == ref] or [c for c in conns if c.get('name') == ref]
    if len(hit) != 1:
        names = ', '.join(repr(c.get('name')) for c in conns[:15]) or 'none'
        raise ToolError(f'no single {a["to"]} connection {ref!r}; yours: {names}')
    t = {'kind': a['to'], 'connection_id': hit[0]['id']}
    if a['to'] == 's3':
        t.update(bucket=a.get('s3_bucket') or '', prefix=a.get('prefix') or '')
    else:
        t['path'] = a.get('prefix') or None
    return t


def _export_to(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().export_to(user, b['id'], a.get('path', ''), target=_export_target(user, a),
                           create_bucket=bool(a.get('create_bucket')), overwrite=bool(a.get('overwrite')))
    return res, f'{b["name"]}: export to {a["to"]} started' if res.get('job_id') else f'{b["name"]}: exported'


def _export_preview(b, a):
    where = f'{a["to"]}://{a.get("s3_bucket") or a.get("connection")}/{a.get("prefix") or ""}'.rstrip('/')
    return (f'Export {_folder(a.get("path", ""))} of {_bname(b, a)} to {where} (paths kept from the bucket root)'
            + (', creating the bucket' if a.get('create_bucket') else '') + (', overwriting' if a.get('overwrite') else ''))


SOURCES = ['kaggle', 'huggingface', 'roboflow', 'url', 'zenodo', 'openml']
SOURCE_ARGS = ('version', 'revision', 'patterns', 'format')


def _source_opts(a):
    o = {k: a[k] for k in SOURCE_ARGS if a.get(k) not in (None, '', [])}
    for k in ('extract', 'nested', 'keep_archive'):
        if k in a:
            o[k] = bool(a[k])
    return o


def _inspect_source(user, a):
    res = _svc().inspect_source(user, a['source'], a['ref'], opts=_source_opts(a))
    keep = ('title', 'name', 'ref', 'size', 'files', 'license', 'version', 'versions', 'rows', 'columns', 'url',
            'warning', 'sample')
    out = {k: res[k] for k in keep if res.get(k) not in (None, [], '')}
    if isinstance(out.get('versions'), list):
        out['versions'] = out['versions'][:10]
    return out, f'→ {out.get("title")}' + (f', {out["files"]} files' if out.get('files') else '')


def _import(user, a):
    res = _svc().import_source(user, a['source'], a['ref'], name=a.get('name'), opts=_source_opts(a),
                               bucket=a.get('bucket'), path=a.get('path', ''))
    return {'bucket': res.get('bucket'), 'job_id': res.get('job_id')}, \
        f'import of {a["ref"]} started (job {res.get("job_id")}): progress shows at the top of the Bucket page'


def _write_text(user, a):
    b = resolve_bucket(user, a['bucket'])
    svc = _svc()
    try:
        cur = svc.read_text(user, b['id'], a['path'], limit=8 * 1024 * 1024)
        mtime, exists = cur.get('mtime_ns'), True
    except Exception as e:  # noqa: BLE001 - a missing file is created; any other error surfaces from the write
        if getattr(e, 'status', None) != 404:
            raise
        mtime, exists = None, False
    res = svc.write_text(user, b['id'], a['path'], a['text'], expect_mtime_ns=mtime, create=not exists)
    return {k: res.get(k) for k in ('path', 'size', 'undo_id', 'created', 'retagged')}, \
        f'{"created" if not exists else "saved"} {a["path"]} ({res.get("size")} bytes, undoable)'


def _write_preview(b, a):
    n = a['text'].count('\n') + (0 if a['text'].endswith('\n') or not a['text'] else 1)
    return f'Write {a["path"]} in {_bname(b, a)}: {_plural(n, "line")} (replaces the file; the old one goes to the trash, undoable)'


def _import_preview(b, a):
    named = f' {a["name"]!r}' if a.get('name') else ''
    where = f'folder {_folder(a.get("path", ""))} of {_bname(b, a)}' if b else f'a new bucket{named}'
    return f'Download {a["ref"]} from {a["source"]} into {where}' + (' (archives unpacked)' if a.get('extract', True) else '')


def _items(paths):
    paths = list(paths or [])
    shown = ', '.join(paths[:3]) + (f' +{len(paths) - 3} more' if len(paths) > 3 else '')
    return f'{_plural(len(paths), "item")} ({shown})'


# ---------------------------------------------------------------- video -> frames
_FRAME_MODES = ('all', 'every', 'pattern', 'count', 'keyframes', 'scene', 'smart')
_FRAME_OPTS = ('fps', 'mode', 'n', 'take', 'window_s', 'every_s', 'pick', 'start', 'end', 'dedupe', 'format',
               'max_side', 'layout')


def _frame_opts(a):
    return {k: a[k] for k in _FRAME_OPTS if a.get(k) is not None}


def _frames_how(a):
    from ...bucket import video
    try:
        return video.describe(video.options(_frame_opts(a)))
    except Exception as e:  # noqa: BLE001 - shown in the preview; the run reports the real error
        return f'invalid settings: {e}'


def _frames(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().extract_frames(user, b['id'], a['paths'], _frame_opts(a), target=a.get('target') or 'inplace',
                                new_name=a.get('new_name'), out=a.get('out'))
    est = res.get('estimate') or {}
    n = est.get('frames')
    return res, (f'{b["name"]}: cutting {"≈" if est.get("approx") else ""}{n if n is not None else "?"} frames into '
                 f'{res.get("path")}' + (' (a job)' if res.get('job_id') else ''))


def _frames_preview(b, a):
    where = 'a new bucket' + (f' {a["new_name"]!r}' if a.get('new_name') else '') if a.get('target') == 'copy' \
        else _bname(b, a)
    return f'Cut {_items(a["paths"])} into frames ({_frames_how(a)}) → {a.get("out") or "<video>-frames"} in {where}'


def _video_auto(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().set_video_auto(user, b['id'], None if a.get('off') else _frame_opts(a))
    return res, f'{b["name"]}: auto-extract ' + ('off' if a.get('off') else 'on')

def _check(user, clean):
    """Before a change is proposed or run: the bucket resolved (its id goes into the args) and writable."""
    b = resolve_bucket(user, clean['bucket']) if clean.get('bucket') else None
    if b is not None:
        clean = {**clean, 'bucket': b['id']}
    return clean, b


def _add(t):
    t.domain = 'bucket'
    if t.mutating:
        tool = t

        def check(user, clean, _t=tool):
            clean, b = _check(user, clean)
            if b is not None:
                _writable(b, _t)
            return clean, b
        t.check = check
    register(t)


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
_add(Tool('copy_to_bucket', 'Copy files / folders into a folder of ANOTHER bucket (or a big copy in the same one) as '
                           'a background job with progress (the status bar; task_status / job_status). Any source '
                           '(local, downloaded or streamed S3); the destination must be writable. A clashing name '
                           'gets "(copy)"; the job result has an undo_id.',
          _obj({'bucket': S('the source bucket (id or exact name)'), 'paths': PATHS,
                'to_bucket': S('the destination bucket (id or exact name)'),
                'to': S("destination folder in it ('' = root)")}, ['bucket', 'paths', 'to_bucket', 'to']),
          True, _copy_to, lambda b, a: f'Copy {_items(a["paths"])} of {_bname(b, a)} → {a["to_bucket"]}/{_folder(a["to"])}',
          s3_ok=True))
_add(Tool('push_to_s3', 'Upload to S3: files / folders of the local copy of an S3 link (a downloaded folder, or a bucket '
                       'loaded into local) go back to the SAME keys (<prefix>/<path>) as a background job. Unchanged '
                       'files are skipped; a file S3 changed since the download is a conflict (listed, not uploaded) '
                       'unless its path is in overwrite. Files deleted here stay in S3.',
          _obj({'bucket': BUCKET, 'paths': PATHS,
                'overwrite': {'type': 'array', 'items': {'type': 'string'},
                              'description': 'conflicting paths to upload anyway (replaces S3\'s newer copy)'}},
               ['bucket', 'paths']),
          True, _push, lambda b, a: f'Upload {_items(a["paths"])} of {_bname(b, a)} to S3 (same keys'
                                    + (f', overwriting {len(a["overwrite"])} changed in S3)' if a.get('overwrite') else ')'),
          s3_ok=True, danger_if=lambda a: bool(a.get('overwrite'))))
_add(Tool('list_changes', 'What changed in a bucket (under a folder) since it was downloaded or marked seen: edits on '
                         'the Bucket page, label fixes in Visualize (source visual), labeling exports, edits outside the '
                         'app (external), newest first. The page shows them as dots; ls entries carry `changed` too.',
          _obj({'bucket': BUCKET, 'path': S("folder ('' = the whole bucket)")}, ['bucket']), False, _list_changes, s3_ok=True))
_add(Tool('mark_seen', 'Clear change marks ("Mark as seen"): of `paths` (each with what is below), or of everything under '
                      '`path` when no paths. Nothing on disk changes.',
          _obj({'bucket': BUCKET, 'paths': PATHS, 'path': S("folder whose marks all go ('' = the whole bucket)")}, ['bucket']),
          True, _mark_seen, lambda b, a: f'Mark as seen: {_items(a["paths"]) if a.get("paths") else _folder(a.get("path", ""))} in {_bname(b, a)}',
          s3_ok=True))
_add(Tool('rename', 'Rename one file or folder (a new name, not a path).',
          _obj({'bucket': BUCKET, 'path': S('what to rename'), 'name': S('the new name')}, ['bucket', 'path', 'name']),
          True, _rename, lambda b, a: f'Rename {a["path"]} → {a["name"]} in {_bname(b, a)}'))
_add(Tool('delete', 'Delete files / folders (they go to the trash and can be undone).',
          _obj({'bucket': BUCKET, 'paths': PATHS}, ['bucket', 'paths']),
          True, _delete, lambda b, a: f'Delete {_items(a["paths"])} from {_bname(b, a)} (to trash, undoable)', risk='danger'))
_add(Tool('add_tags', "Add user tags to a bucket ('' path) or a folder / file. format:* tags are automatic.",
          _obj({'bucket': BUCKET, 'path': PATH, 'tags': A('tags to add')}, ['bucket', 'tags']),
          True, lambda u, a: _set_tags(u, a, True),
          lambda b, a: f'Tag {a.get("path") or "the bucket"} in {_bname(b, a)}: + {", ".join(a["tags"])}', s3_ok=True))
_add(Tool('remove_tags', 'Remove user tags (automatic format:* tags cannot be removed).',
          _obj({'bucket': BUCKET, 'path': PATH, 'tags': A('tags to remove')}, ['bucket', 'tags']),
          True, lambda u, a: _set_tags(u, a, False),
          lambda b, a: f'Untag {a.get("path") or "the bucket"} in {_bname(b, a)}: − {", ".join(a["tags"])}', s3_ok=True))
_add(Tool('session_check', 'Before a session is made from a folder: its detected format, image / label counts, and '
                           'whether Visualize, Label and the linked pair work on it (and why not), whether its labels '
                           'are imported into the Label session, and the formats it can be re-formatted to. A '
                           'streamed S3 folder read for the first time answers scanning + job_id: job_status it, '
                           'then call session_check again.',
          _obj({'bucket': BUCKET, 'path': PATH}, ['bucket']), False, _session_check, s3_ok=True))
_add(Tool('reformat_folder', 'Write a folder again in another label format (yolo, coco, voc, labelme, createml, csv) as '
                             'a NEW folder of the bucket "<bucket> · formats"; the original is not changed. Reads YOLO '
                             'detect / segment / pose and COCO labels. A local folder gets its images linked beside '
                             'the new labels; a streamed S3 folder gets the labels only, with images.txt and '
                             'source.json (the images stay in S3; to yolo it opens in Visualize and Label, images streamed). '
                             'A background job: job_status says when it is done.',
          _obj({'bucket': BUCKET, 'path': PATH,
                'to': {'type': 'string', 'enum': ['yolo', 'coco', 'voc', 'labelme', 'createml', 'csv'], 'description': 'the format to write'}},
               ['bucket', 'to']), True, _reformat,
          lambda b, a: f'Re-format {a.get("path") or "the bucket"} of {_bname(b, a)} to {a["to"]} (a new folder; the original stays)',
          s3_ok=True))
_add(Tool('send_to_session', 'Make a session from one folder: target both (the default choice: one linked session, the '
                             'Label side owns the labels, the folder\'s own are imported, Visualize reads them live), '
                             'visualize (the visualizer alone) or label (a labeling project alone).',
          _obj({'bucket': BUCKET, 'paths': PATHS,
                'target': {'type': 'string', 'enum': ['both', 'visualize', 'label'], 'description': 'what to create'},
                'name': S('session / project name (optional)'),
                'task_type': {'type': 'string', 'description': 'label task type (default detect)',
                              'enum': ['detect', 'segment', 'pose', 'obb', 'classify']}},
               ['bucket', 'paths', 'target']),
          True, _send, lambda b, a: f'Send {_items(a["paths"])} from {_bname(b, a)} to '
                                    + {'visualize': 'the visualizer', 'both': 'a linked Visualize + Label session'}.get(
                                        a['target'], 'a labeling project')
                                    + (f' as {a["name"]!r}' if a.get('name') else ''), s3_ok=True,
          danger_if=lambda a: a.get('target') in ('label', 'both')))      # a labeling project: always confirmed

_add(Tool('set_s3_mode', 'Choose how a WHOLE S3 bucket is read: stream (from S3 on every request) or local '
                        '(every object downloaded into an editable local folder that can be re-synced and exported). '
                        'Not needed to change one folder: download_s3_folders that folder instead.',
          _obj({'bucket': BUCKET, 'mode': {'type': 'string', 'enum': ['stream', 'local'], 'description': 'load mode'}},
               ['bucket', 'mode']),
          True, _s3_mode, lambda b, a: f'Set {_bname(b, a)} to '
                                       + ('load into a local folder (downloads it)' if a['mode'] == 'local'
                                          else 'stream from S3'), s3_ok=True))
_add(Tool('sync_from_s3', 'Re-sync an S3 bucket that is loaded locally: files with the same name are replaced by the S3 '
                          'version (unchanged files are skipped), local-only files are kept.',
          _obj({'bucket': BUCKET}, ['bucket']), True, _s3_sync,
          lambda b, a: f'Re-sync {_bname(b, a)} from S3 (same-name files are replaced)', s3_ok=True, risk='danger'))

_add(Tool('download_s3_folders', 'Download folders (or files) of a STREAMED S3 bucket into its local copy: fast to '
                                 'open, readable while S3 is unreachable, and EDITABLE (create_folder / move / rename '
                                 '/ delete / upload inside it change only the local copy, never S3); the rest keeps '
                                 'streaming and stays read-only. Run it '
                                 'again on a downloaded folder to update it (new + changed files; unchanged skipped; '
                                 'files edited here are kept, see the result\'s kept / conflicts). '
                                 'mirror also deletes local files S3 no longer has: only when the user asks.',
          _obj({'bucket': BUCKET, 'paths': A('folders / files to download'),
                'mirror': {'type': 'boolean', 'description': 'also remove local files S3 no longer has'}},
               ['bucket', 'paths']), True, _s3_fetch,
          lambda b, a: f'Download {_items(a["paths"])} of {_bname(b, a)} to local'
                       + (', removing local files S3 no longer has' if a.get('mirror') else ''),
          s3_ok=True, danger_if=lambda a: bool(a.get('mirror'))))
_add(Tool('check_s3_local', 'Compare the downloaded folders of a streamed S3 bucket with S3 now: new / changed / '
                            'removed files per folder (a job; list_folder then shows local_copy.state outdated).',
          _obj({'bucket': BUCKET}, ['bucket']), False, _s3_check, s3_ok=True))
_add(Tool('job_status', 'How a background job is doing (the job_id an import, export, frames, S3 download / sync / '
                        'compare tool returned): state queued | running | done | error | cancelled, stage, percent, '
                        'seconds left. Call it before a step that needs the job finished; wait_s waits for it first.',
          _obj({'job_id': I('the job_id a tool returned', 1), 'wait_s': I('wait up to this many seconds for it to finish', 0, 60)},
               ['job_id']), False, _job_status, s3_ok=True))
_add(Tool('compare_s3_local', 'File by file, the local copy of a folder of an S3 bucket against S3 (a streamed link\'s '
                              'downloaded folder, or a bucket loaded into local): what the user added, deleted or '
                              'edited here, and what is new, changed or removed in S3 since the download. Read-only. '
                              'Reuses the last comparison unless refresh; verify also compares the bytes (MD5 vs '
                              'ETag; slow). The user sees the same in the Bucket page (Compare with cloud).',
          _obj({'bucket': BUCKET, 'path': PATH,
                'status': {'type': 'array', 'items': {'type': 'string', 'enum': ['added_local', 'deleted_local',
                           'edited_local', 'both', 'new_cloud', 'changed_cloud', 'removed_cloud', 'same']},
                           'description': 'only these statuses (none: every file, the changes first)'},
                'q': S('only paths containing this'), 'offset': I('first file'), 'limit': I('files (max 50)', 1, 50),
                'refresh': B('compare again now'), 'verify': B('with refresh: also compare the contents')},
               ['bucket']), False, _s3_compare, s3_ok=True))
_add(Tool('remove_local_copy', 'Delete the downloaded copy of folders of a streamed S3 bucket (S3 is not touched; they '
                               'stream again). Only whole downloaded folders (or folders holding them).',
          _obj({'bucket': BUCKET, 'paths': A('downloaded folders')}, ['bucket', 'paths']), True, _s3_drop,
          lambda b, a: f'Remove the local copy of {_items(a["paths"])} of {_bname(b, a)} (S3 keeps them)', s3_ok=True))

_add(Tool('export_to', 'Copy a folder (or file) of a bucket to S3, FTP or SFTP through one of the user\'s connections '
                      'as a background job. Paths are kept from the bucket root: prefix + data/image/im1.png.',
          _obj({'bucket': BUCKET, 'path': PATH,
                'to': {'type': 'string', 'enum': ['s3', 'ftp', 'sftp'], 'description': 'target kind'},
                'connection': S('connection id or exact name (Settings › Connections)'),
                's3_bucket': S('S3: the target bucket'), 'prefix': S('S3 prefix / FTP or SFTP folder (optional)'),
                'create_bucket': {'type': 'boolean', 'description': 'S3: create the bucket when missing'},
                'overwrite': {'type': 'boolean', 'description': 'write into a non-empty target'}},
               ['bucket', 'to', 'connection']), True, _export_to, _export_preview, s3_ok=True,
          danger_if=lambda a: bool(a.get('overwrite'))))

_add(Tool('extract_frames', 'Cut videos (a video file, or a folder: its videos) into image frames to label or visualize: '
                           'a background job writing a new folder <video>-frames (YOLO-ready by default). Pick frames '
                           'with fps, or mode + its numbers; bursts = take frames from window_s every every_s.',
          _obj({'bucket': BUCKET, 'paths': PATHS,
                'fps': N('frames per second, e.g. 15 (0.5 = one every 2 s)'),
                'mode': {'type': 'string', 'enum': list(_FRAME_MODES), 'description': 'instead of fps'},
                'n': I('every: the step; count / smart: how many frames', 1),
                'take': I('pattern: frames per window', 1), 'window_s': N('pattern: window length (s)'),
                'every_s': N('pattern: a window every this many s'),
                'pick': {'type': 'string', 'enum': ['spread', 'consecutive'], 'description': 'pattern: spread or in a row'},
                'start': N('from this second'), 'end': N('to this second'),
                'dedupe': N('skip near-identical frames: 0 off, 0.01 light, 0.03 strong'),
                'format': {'type': 'string', 'enum': ['jpg', 'png', 'webp']}, 'max_side': I('shrink to this long side (px)', 16),
                'layout': {'type': 'string', 'enum': ['yolo', 'yolo-split', 'per-video', 'flat']},
                'target': {'type': 'string', 'enum': ['inplace', 'copy'], 'description': 'copy = into a new bucket '
                           '(the only way for a streamed S3 bucket)'},
                'new_name': S('copy: the new bucket name'), 'out': S('output folder name (optional)')},
               ['bucket', 'paths']), True, _frames, _frames_preview, s3_ok=True))
_add(Tool('set_video_auto', 'Make a bucket cut every new video (upload, import, S3 load) into frames by itself, with '
                           'extract_frames-style settings; off=true turns it off.',
          _obj({'bucket': BUCKET, 'fps': N('frames per second'), 'mode': {'type': 'string', 'enum': list(_FRAME_MODES)},
                'n': I('every / count / smart number', 1), 'layout': {'type': 'string', 'enum': ['yolo', 'yolo-split', 'per-video', 'flat']},
                'off': B('turn auto-extract off')}, ['bucket']), True, _video_auto,
          lambda b, a: f'{_bname(b, a)}: ' + ('stop cutting new videos into frames' if a.get('off')
                                             else f'cut every new video into frames ({_frames_how(a)})')))

_add(Tool('inspect_dataset_source', 'Preview a dataset on the web before importing it: Kaggle (owner/dataset, '
                                    'competitions/<name>), Hugging Face (user/dataset), Roboflow (workspace/project/version), '
                                    'a direct link (url), Zenodo (record id / DOI) or OpenML (dataset id): title, size, files, '
                                    'license, versions.',
          _obj({'source': {'type': 'string', 'enum': SOURCES, 'description': 'where the dataset is'},
                'ref': S('the dataset: its id / path or its link'), 'version': S('Kaggle / Roboflow version (optional)'),
                'revision': S('Hugging Face revision (optional)'), 'patterns': A('Hugging Face / Zenodo: only these globs'),
                'format': S('Roboflow export format (yolov8, coco, voc ...) or OpenML parquet / arff')},
               ['source', 'ref']), False, _inspect_source))
_add(Tool('import_dataset', 'Download a dataset from the web into a new bucket (or a folder of a writable bucket) '
                            'as a background job; archives are unpacked and the format is detected. The user needs the '
                            "source's token in Settings › Connections (Kaggle, Roboflow; Hugging Face / Zenodo only for "
                            'gated / restricted data).',
          _obj({'source': {'type': 'string', 'enum': SOURCES, 'description': 'where the dataset is'},
                'ref': S('the dataset: its id / path or its link'), 'name': S('new bucket name (default: from the ref)'),
                'bucket': S('import into this existing bucket instead (id or exact name)'),
                'path': S('folder of that bucket (default the root)'), 'version': S('Kaggle / Roboflow version'),
                'revision': S('Hugging Face revision'), 'patterns': A('Hugging Face / Zenodo: only these globs'),
                'format': S('Roboflow export format / OpenML parquet or arff'),
                'extract': {'type': 'boolean', 'description': 'unpack archives (default true)'},
                'nested': {'type': 'boolean', 'description': 'also unpack archives inside archives'}},
               ['source', 'ref']), True, _import, _import_preview))
_add(Tool('write_text_file', 'Write (replace or create) a text file such as data.yaml, classes.txt, a .json or .env. '
                             'Read it first with read_text and send the whole new content. Undoable.',
          _obj({'bucket': BUCKET, 'path': S('file path'), 'text': S('the complete new content of the file')},
               ['bucket', 'path', 'text']), True, _write_text, _write_preview, risk='danger'))



# ---------------------------------------------------------------- buckets themselves (settings, create, delete, undo)
def _create_bucket(user, a):
    b = _svc().create_bucket(user, a['name'], kind='managed', tags=tuple(a.get('tags') or ()))
    return _bucket_row(b), f'created bucket {b.get("name")!r}'


def _update_bucket(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().update_bucket(user, b['id'], name=a.get('name'), mode=a.get('mode'))
    return {'bucket': res.get('id'), 'name': res.get('name'), 'mode': res.get('mode'), 'job_id': res.get('job_id')}, \
        f'{b["name"]}: ' + ', '.join(x for x in (f'renamed to {a["name"]!r}' if a.get('name') else '',
                                                 f'mode {a["mode"]}' if a.get('mode') else '') if x)


def _update_preview(b, a):
    what = [f'rename to {a["name"]!r}' if a.get('name') else '',
            ('load into a local folder (downloads it)' if a.get('mode') == 'local' else 'stream from S3')
            if a.get('mode') else '']
    return f'Bucket {_bname(b, a)}: ' + ', '.join(x for x in what if x)


def _delete_bucket(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().delete_bucket(user, b['id'], delete_files=bool(a.get('delete_files')))
    return res, f'deleted bucket {b["name"]!r}' + (' and its files' if a.get('delete_files') else ' (files kept)')


def _delete_bucket_preview(b, a):
    if a.get('delete_files'):
        return f'Delete bucket {_bname(b, a)} AND ALL ITS FILES (permanent: not in the trash, cannot be undone)'
    return f'Remove bucket {_bname(b, a)} from the Bucket page (its files stay on disk / in S3)'


def _undo(user, a):
    b = resolve_bucket(user, a['bucket'])
    res = _svc().undo(user, b['id'], a['undo_id'])
    errs = res.get('errors') or []
    return {'restored': res.get('restored'), 'errors': errs[:20], 'retagged': res.get('retagged')}, \
        f'undid: {_plural(int(res.get("restored") or 0), "step")} restored' + (f', {_plural(len(errs), "error")}' if errs else '')


def _all_tags(user, a):
    tags = _svc().all_tags(user)
    rows, total = _cut(tags, 200)
    return {'total': total, 'tags': rows}, f'→ {_plural(total, "tag")}'


_add(Tool('all_tags', 'Every tag used in the buckets the user can see, with counts (to reuse existing tag names).',
          _obj({}), False, _all_tags))
_add(Tool('create_bucket', 'Create a new empty managed bucket (a folder kept by the Bucket page).',
          _obj({'name': S('the bucket name'), 'tags': A('user tags (optional)')}, ['name']),
          True, _create_bucket, lambda b, a: f'Create bucket {a["name"]!r}'
                                             + (f' tagged {", ".join(a["tags"])}' if a.get('tags') else '')))
_add(Tool('update_bucket', "Change a bucket's settings: its name, and for an S3 link its load mode (stream | local).",
          _obj({'bucket': BUCKET, 'name': S('new name (optional)'),
                'mode': {'type': 'string', 'enum': ['stream', 'local'], 'description': 'S3 links only (optional)'}},
               ['bucket']), True, _update_bucket, _update_preview, s3_ok=True))
_add(Tool('delete_bucket', 'Remove a bucket from the Bucket page. delete_files=true also deletes a managed bucket\'s '
                           'files PERMANENTLY (no trash); without it the files stay and only the link is forgotten.',
          _obj({'bucket': BUCKET, 'delete_files': B('also delete the files of a managed bucket (permanent)')},
               ['bucket']), True, _delete_bucket, _delete_bucket_preview, s3_ok=True, risk='danger'))
_add(Tool('undo', 'Undo one earlier change (move, copy, rename, delete, write) by the undo_id its result returned.',
          _obj({'bucket': BUCKET, 'undo_id': S('the undo_id from the change result')}, ['bucket', 'undo_id']),
          True, _undo, lambda b, a: f'Undo change {a["undo_id"]} in {_bname(b, a)}'))
