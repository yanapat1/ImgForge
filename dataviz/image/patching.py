"""
In-place patches of a loaded State (no re-index): what a curation fix / undo (data/curation.py) and the live change
feed of a labeling task (data/live.py) both do to the index in memory.

    patch = {'cls': {sid: new class}, 'deleted': [sid], 'restored': [sid], 'moved': [{'id': sid, 'bbox': [x1,y1,x2,y2]}],
             'box_idx': {sid: line}}          (box_idx: internal, the label line a box is on now; not sent)

apply_patch() updates State.labels, the samples' cls / cls_name / bbox / box_idx, the State.deleted mask, by_image,
State.stale_ids (moved boxes: their crop / features are from before), drops the points payload and bumps
State.edit_seq (invalidate), and tells the compare engine (State.on_boxes_patched(images), when it has one).
The caller holds the lock that guards the State (Curation.lock, else State.index_lock).
"""

import numpy as np


def deleted_mask(state):
    """State.deleted, (re)made as a bool per sample when it is missing or the wrong length."""
    d = getattr(state, 'deleted', None)
    if d is None or len(d) != len(state.samples):
        d = np.zeros(len(state.samples), bool)
        state.deleted = d
    return d


def invalidate(state):
    """The State changed under the index: drop the points payload, bump the edit counter (eval / groups caches)."""
    state.payloads.pop('points', None)
    state.edit_seq = getattr(state, 'edit_seq', 0) + 1


def set_cls(state, sid, c):
    """Box `sid` is class `c` now (labels + the sample's cls / cls_name)."""
    state.labels[sid] = c
    s = state.samples[sid]
    s['cls'] = c
    s['cls_name'] = state.classes[c] if c < len(state.classes) else f'class_{c}'


def set_bbox(state, sid, bbox):
    """Box `sid` has a new pixel box: True when it changed (it is then listed in State.stale_ids)."""
    smp = state.samples[sid]
    bb = [int(round(v)) for v in bbox]
    if bb == list(smp['bbox']):
        return False
    smp['bbox'] = bb
    if getattr(state, 'stale_ids', None) is None:
        state.stale_ids = set()
    state.stale_ids.add(sid)
    return True


def delete(state, sids, mask=None):
    """Boxes `sids` are gone: deleted mask, no label line, out of by_image."""
    mask = deleted_mask(state) if mask is None else mask
    by_img = {}
    for sid in sids:
        mask[sid] = True
        smp = state.samples[sid]
        smp['box_idx'] = None
        by_img.setdefault(smp['image'], set()).add(sid)
    if state.by_image:
        for image, g in by_img.items():
            if image in state.by_image:
                state.by_image[image] = [i for i in state.by_image[image] if i not in g]


def restore(state, sids, mask=None):
    """Boxes `sids` are back (cleared in the deleted mask, in by_image again)."""
    mask = deleted_mask(state) if mask is None else mask
    for sid in sids:
        mask[sid] = False
        image = state.samples[sid]['image']
        if state.by_image is not None:
            lst = state.by_image.setdefault(image, [])
            if sid not in lst:
                lst.append(sid)
                lst.sort()


def boxes_patched(state, images):
    """The compare engine's hook (State.on_boxes_patched): {cmp_key: {status, summary}}, {} without one."""
    fn = getattr(state, 'on_boxes_patched', None)
    if fn is None or not images:
        return {}
    try:
        return fn(sorted(set(images))) or {}
    except Exception:  # noqa: BLE001 - a compare refresh never fails the patch
        return {}


def apply_patch(state, patch):
    """Apply a live-shaped patch to the State in place. Returns the effective patch (only what changed, ids as
    ints) + 'images' (the images touched) + 'diff' (the compare engine's answer, when there is one)."""
    out = {'cls': {}, 'deleted': [], 'restored': [], 'moved': []}
    n = len(state.samples)
    mask = deleted_mask(state)
    cur = getattr(state, '_curation', None)
    images = set()
    gone = [int(s) for s in patch.get('deleted') or () if 0 <= int(s) < n and not mask[int(s)]]
    back = [int(s) for s in patch.get('restored') or () if 0 <= int(s) < n and mask[int(s)]]
    if back:
        restore(state, back, mask)
        out['restored'] = back
    for sid, line in (patch.get('box_idx') or {}).items():
        if 0 <= int(sid) < n:
            state.samples[int(sid)]['box_idx'] = line
    for sid, c in (patch.get('cls') or {}).items():
        sid, c = int(sid), int(c)
        if 0 <= sid < n and int(state.labels[sid]) != c:
            set_cls(state, sid, c)
            if cur is not None:
                cur.patch_cls[sid] = c
            out['cls'][sid] = c
    for m in patch.get('moved') or ():
        sid = int(m['id'])
        if 0 <= sid < n and set_bbox(state, sid, m['bbox']):
            out['moved'].append({'id': sid, 'bbox': list(state.samples[sid]['bbox'])})
    if gone:
        delete(state, gone, mask)
        out['deleted'] = gone
    for sid in list(out['cls']) + out['deleted'] + out['restored'] + [m['id'] for m in out['moved']]:
        images.add(state.samples[sid]['image'])
    if images:
        invalidate(state)
        diff = boxes_patched(state, images)
        if diff:
            out['diff'] = diff
    out['images'] = sorted(images)
    return out
