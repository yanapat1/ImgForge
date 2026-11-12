"""
"Promote to labeling": materialize a combined dataset (data/union.py) as a labeling session, with an optional live
visualize session over it that opens with nothing to decode or embed.

    run(state, ctx, user, opts, job)       state: the union's State (indexed); a job on the io lane

opts:
    target            'new' (default) | 'existing'
    name              the new labeling session's name (default '<union name> · labeling')
    project_id, task_id   target 'existing': an upload-sourced session the caller can label, an object_detection task
    dedup             'hash' (default): one media per identical image content (full blake2b of every byte);
                      'none': every union image is its own media
    labels            'prefer' (default): the boxes of the first member in `priority` that has boxes for that
                      content; 'merge': every member's boxes, same-class duplicates (IoU > 0.9) dropped; 'all'
    priority          member aliases in preference order (default: member order); also picks the file kept
    include_background  images with no box are promoted too (default True)
    create_viz        a kind=labeling visualize session '<name> · live' (default True), its workspace seeded from
                      the union's through donor reuse (data/reuse.py): same crops, same feature rows

Steps: full hashes per member (in the member's own workspace: its sessions reuse them), group by content, pick
representatives, create / check the project, skip media whose checksum the project already has, hard-link (else
copy / download) the files into <project>/images/, add the media rows in batches of 1000 (checksum, size, meta
{origin, origins, origin_names, hash, union_id}), import the union's CURRENT boxes (curation-deleted ones skipped),
refresh the counts, the optional live session, an audit entry.

File names: '<alias>__<inner path with / -> __>', '~<hash[:8]>' before the extension on a collision.
"""

import mimetypes
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core import logging as vlog
from . import imagehash

BATCH = 1000
IOU_DUP = 0.9
_lg = vlog.get_logger('promote')


def _progress(job):
    return job.progress if job is not None else (lambda *a, **k: None)


def _iou(a, b):
    ax1, ay1, ax2, ay2 = a['x'], a['y'], a['x'] + a['w'], a['y'] + a['h']
    bx1, by1, bx2, by2 = b['x'], b['y'], b['x'] + b['w'], b['y'] + b['h']
    iw, ih = max(0.0, min(ax2, bx2) - max(ax1, bx1)), max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = a['w'] * a['h'] + b['w'] * b['h'] - inter
    return inter / union if union > 0 else 0.0


def _geometry(s):
    """Normalized {x, y, w, h} of a sample: its YOLO line when that still is its box (exact: the live session's
    labels then match the union's boxes line for line), else from the pixel bbox (a box the image editor moved)."""
    from .indexer import yolo_to_pixel
    W, H = s['img_w'], s['img_h']
    y = s.get('yolo')
    if y is not None and list(yolo_to_pixel(*y, W, H)) == list(s['bbox']):
        cx, cy, w, h = y
        return {'x': cx - w / 2, 'y': cy - h / 2, 'w': w, 'h': h}
    x1, y1, x2, y2 = s['bbox']
    return {'x': x1 / W, 'y': y1 / H, 'w': (x2 - x1) / W, 'h': (y2 - y1) / H}


def orient_box(g, o):
    """Normalized {x, y, w, h} in the raw pixel frame -> the same box in the EXIF-oriented frame (orientation o,
    1..8: what PIL ImageOps.exif_transpose shows; the labeling store's coordinates are in that frame)."""
    x, y, w, h = g['x'], g['y'], g['w'], g['h']
    if o == 2:
        x = 1 - x - w
    elif o == 3:
        x, y = 1 - x - w, 1 - y - h
    elif o == 4:
        y = 1 - y - h
    elif o == 5:
        x, y, w, h = y, x, h, w
    elif o == 6:
        x, y, w, h = 1 - y - h, x, h, w
    elif o == 7:
        x, y, w, h = 1 - y - h, 1 - x - w, h, w
    elif o == 8:
        x, y, w, h = y, 1 - x - w, h, w
    return {'x': x, 'y': y, 'w': w, 'h': h}


def _orientation(path):
    """EXIF orientation of an image file (1 = none / unreadable)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            o = int(im.getexif().get(0x0112, 1) or 1)
        return o if 1 <= o <= 8 else 1
    except (OSError, ValueError, SyntaxError):
        return 1


def _flat_name(alias, inner):
    return f'{alias}__{inner.replace("/", "__").replace(chr(92), "__")}'


def _existing_checksums(pid):
    from sqlalchemy import text
    from .. import db
    with db.connect() as c:
        return set(c.execute(text('SELECT checksum FROM lbl.media WHERE project_id = :p AND checksum IS NOT NULL'),
                             {'p': pid}).scalars())


def _member_hashes(state, full_hash, progress):
    """{union image name: images.json entry} from every member's own workspace (full hashes when asked)."""
    src = state.source
    out = {}
    for b in state.members:
        m = src.by_alias[b['alias']]
        ws = state.workspace_root / b['ws']
        progress(stage=f'hashing member {b["alias"]}: {b.get("name", "")}')
        hashes = imagehash.ensure(m.src, ws, b['fp'], full_hash=full_hash, progress=progress)
        for n, e in hashes.items():
            out[f'{b["alias"]}/{n}'] = e
    return out


def plan(state, opts, hashes):
    """[{rep, names, key}] groups of union image names (one media each), representative first by priority."""
    aliases = [b['alias'] for b in state.members]
    prio = [a for a in (opts.get('priority') or []) if a in aliases]
    prio += [a for a in aliases if a not in prio]
    rank = {a: i for i, a in enumerate(prio)}
    boxed = set()
    deleted = state.deleted_mask()
    for s in state.samples:
        if deleted is None or not deleted[s['id']]:
            boxed.add(s['image'])
    names = state.image_names()
    if not opts.get('include_background', True):
        names = [n for n in names if n in boxed]
    groups = {}
    for n in names:
        e = hashes.get(n)
        k = None
        if opts.get('dedup', 'hash') == 'hash' and e is not None:
            k = imagehash.match_key(e)
        groups.setdefault(k or f'name:{n}', []).append(n)

    def order(n):
        return rank.get(n.split('/', 1)[0], len(rank)), n
    out = []
    for k, ns in groups.items():
        ns = sorted(ns, key=order)
        out.append({'key': k, 'names': ns, 'rep': ns[0], 'rank': order})
    out.sort(key=lambda g: g['rep'])
    return out, prio


def _boxes_for(state, group, mode, rank):
    """[(class name, geometry)] of one group's media per labels mode, in the media's EXIF-oriented frame (a local /
    S3 member's boxes are in the raw pixel frame; a labeling member's already oriented)."""
    deleted = state.deleted_mask()
    o = group.get('orientation', 1)
    members = state.source.members
    per = {}
    for n in group['names']:
        rows = []
        for sid in state.by_image.get(n, ()):
            if deleted is not None and deleted[sid]:
                continue
            s = state.samples[sid]
            c = int(s['cls'])
            g = _geometry(s)
            if o != 1 and members[int(s.get('origin', 0))].src.kind != 'labeling':
                g = orient_box(g, o)
            rows.append((state.classes[c] if c < len(state.classes) else f'class_{c}', g))
        per[n] = rows
    ordered = sorted(group['names'], key=rank)
    if mode == 'prefer':
        for n in ordered:
            if per[n]:
                group['box_src'] = n            # the live session's donor image: same boxes, same pixels
                return per[n]
        return []
    out = []
    for n in ordered:
        for cname, g in per[n]:
            if mode == 'merge' and any(c == cname and _iou(g, k) > IOU_DUP for c, k in out):
                continue
            out.append((cname, g))
    return out


def _place(src_union, name, dst):
    """The bytes of union image `name` at dst: a hard link of the local file, else a copy / a download."""
    m, inner = src_union.split(name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ref = None
    try:
        ref = m.src.image_ref(inner)
    except (OSError, KeyError, AttributeError):
        pass
    if ref and os.path.isfile(str(ref)):
        # linked / copied under a temp name, then moved over: a file already at dst (an earlier promote that failed
        # half way) is replaced, never kept with another image's media row
        tmp = dst.with_name(f'.{dst.name}.{os.getpid()}.tmp')
        tmp.unlink(missing_ok=True)
        try:
            os.link(ref, tmp)
        except OSError:
            shutil.copyfile(ref, tmp)
        os.replace(tmp, dst)
        return dst.stat().st_size
    data = m.src.read_image_bytes(inner)
    tmp = dst.with_name(f'.{dst.name}.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, dst)
    return len(data)


def run(state, ctx, user, opts, job=None):
    from ..auth import access
    progress = _progress(job)
    opts = dict(opts or {})
    if getattr(state.source, 'kind', None) != 'union' or not state.members:
        raise ValueError('promote needs a combined dataset')
    reg = ctx.labeling
    if reg is None:
        raise ValueError('labeling sessions are not enabled in this server')
    state.wait_index(job)
    mode = opts.get('labels') or 'prefer'
    if mode not in ('prefer', 'merge', 'all'):
        raise ValueError(f'unknown labels mode {mode!r} (prefer, merge or all)')
    dedup = opts.get('dedup') or 'hash'
    if dedup not in ('hash', 'none'):
        raise ValueError(f'unknown dedup {dedup!r} (hash or none)')
    target = opts.get('target') or 'new'
    union = state.source
    union_id = state.dataset_id

    # where to: check before any heavy work
    if target == 'existing':
        pid, tid = opts.get('project_id'), opts.get('task_id')
        if not pid or not tid:
            raise ValueError('pick the labeling session and its task')
        access.check(user, pid, 'labeler', kind='label', what='labeling session')
        entry = reg.get(pid)
        if (entry.get('source') or {}).get('kind') != 'upload':
            raise ValueError('that labeling session reads its images from a folder or bucket: promote needs one '
                             'with uploaded images')
        st = reg.store(pid)
        task = st.task(tid)
        if task['task_type'] != 'object_detection':
            raise ValueError('pick an object detection task')
        name = entry['name']
    elif target == 'new':
        # a second promote of the same data keeps its default name: take the next free one ('<name> (2)')
        from ..core.names import unique_name
        from . import sessions
        name = unique_name((opts.get('name') or '').strip() or f'{union.name} · labeling',
                           sessions.names(user['id'], 'label'))
        reg.check_name(name, user['id'])
        pid = tid = None
    else:
        raise ValueError(f'unknown target {target!r} (new or existing)')

    hashes = _member_hashes(state, dedup == 'hash', progress)
    groups, prio = plan(state, opts, hashes)

    if target == 'new':
        progress(stage='creating the labeling session')
        entry = reg.create({'name': name, 'source': {'kind': 'upload'},
                            'description': f'promoted from the combined dataset {union.name}',
                            'tasks': [{'name': 'Detect', 'task_type': 'object_detection',
                                       'classes': list(state.classes)}]}, user['id'])
        pid = entry['id']
        st = reg.store(pid)
        tid = st.tasks()[0]['id']
    known_sums = _existing_checksums(pid)
    taken = {fn.casefold() for fn in st.media_names()}

    todo, skipped_dup = [], sum(len(g['names']) - 1 for g in groups)
    n_existing = 0
    for g in groups:
        e = hashes.get(g['rep']) or {}
        h = e.get('h')
        if h and h in known_sums:
            n_existing += 1
            continue
        alias, inner = g['rep'].split('/', 1)
        fn = _flat_name(alias, inner)
        if fn.casefold() in taken:
            stem, ext = os.path.splitext(fn)
            tag = (h or e.get('q') or '')[:8] or 'dup'
            fn = f'{stem}~{tag}{ext}'
            k = 2
            while fn.casefold() in taken:
                fn = f'{stem}~{tag}_{k}{ext}'
                k += 1
        taken.add(fn.casefold())
        g['file_name'] = fn
        todo.append(g)

    images_dir = reg.project_dir(pid) / 'images'
    progress(0, len(todo), 'copying images')

    def place(g):
        try:
            return g, _place(union, g['rep'], images_dir / g['file_name'])
        except (OSError, KeyError, FileNotFoundError) as e:
            _lg.warning(f'promote: cannot copy {vlog.esc(g["rep"])}: {vlog.esc(e)}')
            return g, None

    placed = []
    with ThreadPoolExecutor(8, thread_name_prefix='promote') as ex:
        for i, (g, size) in enumerate(ex.map(place, todo)):
            if size is not None:
                g['size'] = size
                g['orientation'] = _orientation(images_dir / g['file_name'])
                placed.append(g)
            if (i + 1) % 200 == 0 or i + 1 == len(todo):
                progress(i + 1, len(todo), 'copying images')

    progress(0, len(placed), 'adding images')
    for a in range(0, len(placed), BATCH):
        rows = []
        for g in placed[a:a + BATCH]:
            e = hashes.get(g['rep']) or {}
            w, hh = e.get('w'), e.get('hh')
            if not w or not hh:
                try:
                    from PIL import Image
                    with Image.open(images_dir / g['file_name']) as im:
                        w, hh = im.size
                except OSError:
                    pass
            if g['orientation'] >= 5 and w and hh:       # the labeling store's size is the oriented one
                w, hh = hh, w
            rows.append({'file_name': g['file_name'], 'uri': g['file_name'], 'checksum': e.get('h'),
                         'size_bytes': g['size'], 'width': w, 'height': hh,
                         'mime': mimetypes.guess_type(g['file_name'])[0],
                         'meta': {'origin': g['rep'].split('/', 1)[0],
                                  'origins': sorted({n.split('/', 1)[0] for n in g['names']},
                                                    key=lambda x: prio.index(x) if x in prio else len(prio)),
                                  'origin_names': g['names'], 'hash': e.get('h') or e.get('q'),
                                  'union_id': union_id, 'rel_path': g['file_name']}})
        st.add_media(rows)
        progress(a + len(rows), len(placed), 'adding images')

    mids = {m['file_name']: m['id'] for m in st.media_for_import()}
    items = []
    for g in placed:
        mid = mids.get(g['file_name'])
        if mid is None:
            continue
        for cname, geom in _boxes_for(state, g, mode, g['rank']):
            items.append((mid, cname, 'bbox', geom, None))
    progress(stage=f'importing {len(items):,} boxes')
    res = st.import_annotations(tid, items, mode='merge') if items else {'annotations': 0}
    reg.set_counts(pid)

    viz_id = None
    # a labeling-backed visualize session is the labeling session owner's to make (datasets.check_labeling)
    if opts.get('create_viz', True) and (entry.get('owner_id') == user.get('id') or user.get('is_admin')):
        viz_id = _live_session(state, ctx, user, pid, tid, name, placed, hashes, progress)

    try:
        from ..auth import audit
        audit.log('promote', target=pid, user_id=user.get('id'),
                  meta={'union_id': union_id, 'task_id': tid, 'viz_session_id': viz_id, 'n_media': len(placed),
                        'dedup': dedup, 'labels': mode})
    except Exception:  # noqa: BLE001 - recording never fails the promote
        pass
    _lg.info(f'promoted {vlog.esc(union.name)} -> {vlog.esc(name)}: {len(placed):,} images, '
             f'{res.get("annotations", 0):,} boxes')
    return {'project_id': pid, 'task_id': tid, 'viz_session_id': viz_id, 'n_media': len(placed),
            'n_skipped_dup': skipped_dup + n_existing, 'n_existing': n_existing,
            'n_annotations': res.get('annotations', 0), 'classes': list(state.classes)}


def _live_session(state, ctx, user, pid, tid, name, placed, hashes, progress):
    """The kind=labeling visualize session over the new task, its workspace indexed now from the union's (donor
    reuse: crops linked, features transplanted on first use). Returns its id."""
    from . import reuse
    from .indexer import build_index
    from ..workspace import resolve_workspace
    from ..core.names import unique_name
    from . import sessions
    ds = ctx.datasets.upsert_dataset({'kind': 'labeling', 'project_id': pid, 'task_id': tid,
                                      'name': unique_name(f'{name} · live', sessions.names(user['id'], 'viz'))},
                                     user['id'])
    try:
        src = ctx.datasets.source(ds, (ctx.cfg or {}).get('s3'), ctx.labeling)
        src.check()
        ws = resolve_workspace(state.workspace_root, src)
        # the new names' hashes (same bytes as the union's), valid for the media sig: the donor match by content
        own = {}
        for g in placed:
            e = hashes.get(g['rep'])
            if e is None:
                continue
            own[g['file_name']] = {k: e.get(k) for k in ('q', 'h', 'w', 'hh', 'n')}
            own[g['file_name']]['sig'] = src.image_sig(g['file_name'])
        imagehash.save(ws, src.fingerprint(), own, complete=False)
        # an EXIF-turned image is shown turned by the labeling source: its crops are decoded again, not reused
        donor = reuse.Donor.from_workspace(state.cache_dir, name_map={g['file_name']: g.get('box_src') or g['rep']
                                                                         for g in placed if g['orientation'] == 1})
        progress(stage='indexing the live session (from the combined dataset)')
        cfg = state.cfg
        build_index(src, ws, thumb_size=int(cfg['index']['thumb_size']), pad=float(cfg['index']['crop_pad']),
                    progress=progress, workers=cfg['index'].get('workers', 'auto'), donor=donor)
        import json
        meta_path = ws / 'workspace.json'
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        meta['session_id'] = ds['id']
        meta_path.write_text(json.dumps(meta, indent=1))
    except Exception as e:  # noqa: BLE001 - the session opens (and indexes) normally without the seed
        _lg.warning(f'promote: live session not seeded: {vlog.esc(e)}')
    return ds['id']
