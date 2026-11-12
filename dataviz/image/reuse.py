"""
Donor reuse: an index build takes the boxes of images that did not change from an earlier index (the donor) instead
of decoding them again, and the feature matrix takes the donor's rows for those boxes.

    Donor        an indexed workspace: its samples, images.json and index params (thumb size, pad, draft, version)
    line_key     a box's identity inside one image: its YOLO geometry formatted '%.6f' (class excluded, so a
                 relabel is still reused)

indexer.build_index(..., donor=) reuses an image when the params match, the image is the same (image_sig, or both
images.json hashes) and every one of its boxes has a line_key in the donor's boxes of that image: the samples are
copied (new ids, label, box_idx, class), the crops hard-linked. <ws>/reuse.npz maps every new sample to its donor
sample (-1: computed), and State.get_features fills those rows from the donor's cached matrix (transplant_features),
computing only the rest.

Used by: a labeling session's re-index (the fingerprint changes on every edit: the previous workspace is the donor),
"Promote to labeling" (data/promote.py: the union's workspace seeds the live session's) and the live catch-up.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import imagehash
from .sources import INDEX_VERSION

REUSE_FILE = 'reuse.npz'


def line_key(cx, cy, w, h):
    return f'{float(cx):.6f} {float(cy):.6f} {float(w):.6f} {float(h):.6f}'


def index_params(thumb_size, pad, draft):
    return {'thumb_size': int(thumb_size), 'pad': round(float(pad), 6), 'draft': int(draft), 'version': INDEX_VERSION}


def _loads(b):
    try:
        import orjson
        return orjson.loads(b)
    except ImportError:              # pragma: no cover
        return json.loads(b)


@dataclass
class Donor:
    ws: Path                          # its workspace folder (crops/, features/)
    fp: str                           # the fingerprint of its samples.json (its feature caches are keyed by it)
    params: dict
    sig: dict                         # donor image -> image_sig
    boxes: dict                       # donor image -> {line_key: donor sample id}
    samples: list
    name_map: object = None           # new image name -> donor image name (None: the same name)
    hashes: dict = field(default_factory=dict)    # donor image -> images.json entry

    def donor_name(self, name):
        m = self.name_map
        if m is None:
            return name
        return m.get(name) if isinstance(m, dict) else m(name)

    @classmethod
    def from_index(cls, ws, idx, name_map=None, hashes=None, params=None):
        """A Donor of an index dict (samples.json content) in workspace `ws`; None when it is unusable (no index
        params, a sample without its yolo geometry)."""
        ws = Path(ws)
        params = params or idx.get('params') or _member_params(ws, idx)
        samples = idx.get('samples') or []
        if not params:
            return None
        boxes = {}
        for i, s in enumerate(samples):
            y = s.get('yolo')
            if y is None or s.get('id') != i:
                return None
            boxes.setdefault(s['image'], {})[line_key(*y)] = i
        if hashes is None:
            hashes = (imagehash.load(ws) or {}).get('images') or {}
        return cls(ws=ws, fp=idx.get('fingerprint'), params=params,
                   sig={**(idx.get('sigs') or {}), **{n: e['sig'] for n, e in hashes.items() if e.get('sig')}},
                   boxes=boxes, samples=samples,
                   name_map=name_map, hashes=hashes)

    @classmethod
    def from_workspace(cls, ws, name_map=None):
        """The Donor of an indexed workspace folder, or None (no samples.json / no params / no yolo geometry)."""
        if ws is None:
            return None
        ws = Path(ws)
        try:
            idx = _loads((ws / 'samples.json').read_bytes())
        except (OSError, ValueError):
            return None
        return cls.from_index(ws, idx, name_map)

    # ------------------------------------------------------------ what the indexer asks
    def match(self, image, boxes, sig, own_hashes=None):
        """[donor sample id per box] when `image` (its parsed label boxes, its image_sig now) can be taken from the
        donor, else None. own_hashes: the new workspace's images.json entries (still valid ones compared by hash)."""
        if not boxes:
            return None
        d = self.donor_name(image)
        have = self.boxes.get(d) if d is not None else None
        if not have:
            return None
        sids = []
        for b in boxes:
            sid = have.get(line_key(*b[1:5]))
            if sid is None:
                return None
            sids.append(sid)
        if sig is not None and self.sig.get(d) == sig:
            return sids
        mine = (own_hashes or {}).get(image)
        theirs = self.hashes.get(d)
        if mine and theirs and (sig is None or mine.get('sig') in (None, sig)):
            k = imagehash.match_key(mine, theirs)
            if k is not None and k == imagehash.match_key(theirs, mine):
                return sids
        return None

    def hash_entry(self, image, sig, n):
        e = self.hashes.get(self.donor_name(image))
        if not e:
            return None
        out = {k: e.get(k) for k in ('q', 'h', 'w', 'hh')}
        out.update(sig=sig, n=n)
        return out


def _member_params(ws, idx):
    """A combined dataset's samples.json has no params of its own: its members' (when they all agree)."""
    blocks = idx.get('members') or []
    if not blocks:
        return None
    seen = None
    for b in blocks:
        try:
            p = _loads((ws.parent / b['ws'] / 'samples.json').read_bytes()).get('params')
        except (OSError, ValueError, KeyError):
            return None
        if not p or (seen is not None and p != seen):
            return None
        seen = p
    return seen


def previous_workspace(root, uri, session_id=None, exclude=None):
    """The most recently opened OTHER indexed workspace of a dataset (by uri or session id), or None."""
    from ..workspace import _read_meta
    ex = Path(exclude).resolve() if exclude else None
    best = None
    for m in Path(root).glob('*/workspace.json'):
        if ex is not None and m.parent.resolve() == ex:
            continue
        meta = _read_meta(m)
        if not meta or not (m.parent / 'samples.json').exists():
            continue
        if meta.get('dataset') == uri or (session_id is not None and meta.get('session_id') == session_id):
            if best is None or (meta.get('last_opened') or '') > (best[1].get('last_opened') or ''):
                best = (m.parent, meta)
    return best[0] if best else None


def previous_donor(root, uri, session_id=None, exclude=None):
    """build_index(donor=) of a re-index: a callable (only evaluated on a cache miss) -> Donor or None."""
    def get():
        return Donor.from_workspace(previous_workspace(root, uri, session_id, exclude))
    return get


# ------------------------------------------------------------ the reuse map + features
def save_map(cache_dir, src, donor):
    np.savez(Path(cache_dir) / REUSE_FILE, src=np.asarray(src, np.int32), ws=np.asarray(Path(donor.ws).name),
             fp=np.asarray(donor.fp or ''), n=np.asarray(len(donor.samples), np.int64))


def load_map(cache_dir):
    """(src int32[N], donor workspace, donor fp, donor n) or None."""
    p = Path(cache_dir) / REUSE_FILE
    if not p.exists():
        return None
    try:
        with np.load(p) as z:
            return z['src'], Path(cache_dir).parent / str(z['ws']), str(z['fp']), int(z['n'])
    except (OSError, ValueError, KeyError):
        return None


def transplant_features(state, key, progress=None):
    """The feature matrix of `key` for a State whose index reused a donor: the donor's cached rows + the missing
    rows computed (registry fn on just those crops). None when there is no map or the donor has no cache."""
    from ..ml import features as feats
    if state.cache_dir is None:
        return None
    got = load_map(state.cache_dir)
    if got is None:
        return None
    src, dws, dfp, dn = got
    n = len(state.samples)
    if len(src) != n:
        return None
    d = feats.load_cached(dws / 'features', key, dfp, dn)
    if d is None or d.ndim != 2:
        return None
    have = src >= 0
    if have.any() and int(src[have].max()) >= len(d):
        return None
    out = np.empty((n, d.shape[1]), d.dtype)
    if have.any():
        out[have] = np.asarray(d[src[have]])
    miss = np.flatnonzero(~have)
    if len(miss):
        crops = state.crop_paths()
        part = state.registry[key].fn([crops[i] for i in miss], [state.samples[i] for i in miss],
                                      progress or (lambda *a, **k: None))
        part = np.asarray(part)
        if part.ndim != 2 or part.shape[1] != d.shape[1]:
            return None
        out[miss] = part
    return out


def link(a, b):
    """Hard link a -> b (a copy across file systems); FileNotFoundError when a is gone."""
    import shutil
    try:
        os.link(a, b)
    except FileExistsError:
        os.unlink(b)
        os.link(a, b)
    except FileNotFoundError:
        raise
    except OSError:
        shutil.copyfile(a, b)
