"""
Data curation: scan a YOLO dataset for label and image problems, then fix them in place, with a backup
of every touched label file and an undo.

Rules work on the raw label text (nothing silently clamped, unlike the indexer):

    labels     bad_line, bad_class, degenerate, oob, dup_box, conflict_box, extreme_aspect
    images     missing_image, empty_label, unreadable_image, dup_image (same file bytes)
    splits     split_leak (a val / test image is a copy of a train one: same bytes, or boxes near-identical in a feature)
    dataset    class_imbalance, rare_class
    embedding  likely_mislabel, outlier, near_dup          (when k-NN scores of a feature exist)
               cl_label_issue, cl_ood                       (when cleanlab label quality of a feature exists)
    model      model_wrong_cls, model_missing_label, model_missed   (when a model was evaluated)
               cl_badloc, cl_swap, cl_overlooked            (cleanlab object detection on its predictions)

Speed: label files are read in parallel (raw bytes), parsed once, and every box rule is a numpy
expression over all boxes of the dataset at once; within-image IoU pairs are generated per box count
(vectorised), so 20k images scan in about a second.

Fixes are line edits of one label file: delete / dedupe / drop_line (remove the line), set_class
(replace the class token), clamp (rewrite the box clipped to the image). A file is written only when its
sha1 is still the one the client saw (sources.Conflict otherwise). Every fix batch keeps the original
bytes in <workspace root>/curation/<dataset id>/curation_backup/<undo_id>/ (last 50 kept, kept across re-indexes); undo writes them back byte for byte.
The open State is patched in memory (class changes, a `deleted` mask), so the page updates without a
re-index; the patch is returned to the client.
"""

import io
import os
import re
import json
import time
import uuid
import shutil
import hashlib
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .sources import Conflict, sha1_bytes
from .indexer import find_image, yolo_to_pixel

# type -> (group, severity, label, bulk-fixable)
TYPES = {
    'bad_line': ('labels', 'error', 'Malformed line', True),
    'bad_class': ('labels', 'error', 'Unknown class id', True),
    'degenerate': ('labels', 'error', 'Empty or tiny box', True),
    'conflict_box': ('labels', 'error', 'Same box, two classes', False),
    'oob': ('labels', 'warn', 'Box outside the image', True),
    'dup_box': ('labels', 'warn', 'Duplicate box', True),
    'extreme_aspect': ('labels', 'info', 'Extreme aspect ratio', False),
    'unreadable_image': ('images', 'error', 'Unreadable image', False),
    'missing_image': ('images', 'warn', 'Label without image', False),
    'dup_image': ('images', 'warn', 'Duplicate image file', False),
    'empty_label': ('images', 'info', 'Background image (no boxes)', False),
    'split_leak': ('splits', 'error', 'Train / val leakage', False),
    'rare_class': ('dataset', 'warn', 'Rare class', False),
    'class_imbalance': ('dataset', 'info', 'Under-represented class', False),
    'likely_mislabel': ('embedding', 'warn', 'Likely mislabel', True),
    'outlier': ('embedding', 'info', 'Outlier', False),
    'near_dup': ('embedding', 'info', 'Near-duplicate boxes', False),
    'cl_label_issue': ('embedding', 'warn', 'Label issue (cleanlab)', True),
    'cl_ood': ('embedding', 'info', 'Out-of-distribution (cleanlab)', False),
    'model_wrong_cls': ('model', 'warn', 'Model says another class', True),
    'model_missing_label': ('model', 'warn', 'Model finds an unlabelled object', False),
    'model_missed': ('model', 'warn', 'Model sees nothing at a label', False),
    'cl_swap': ('model', 'warn', 'Wrong class (cleanlab)', True),
    'cl_badloc': ('model', 'warn', 'Badly placed box (cleanlab)', False),
    'cl_overlooked': ('model', 'warn', 'Overlooked object (cleanlab)', False),
}
TYPE_ORDER = {t: i for i, t in enumerate(TYPES)}
SEV_RANK = {'error': 0, 'warn': 1, 'info': 2}
DELETE_OPS = ('delete', 'dedupe', 'drop_line')
OPS = DELETE_OPS + ('set_class', 'clamp', 'set_box', 'add')      # set_box / add: the image editor

DEFAULTS = {
    'tiny_px': 4,               # boxes narrower / lower than this (pixels) are degenerate
    'oob_eps': 0.002,           # tolerance outside [0, 1] before a box counts as out of bounds
    'iou_dup': 0.9,             # dup_box / conflict_box overlap
    'aspect_max': 20.0,         # extreme_aspect: w/h above this (or below 1/this)
    'imbalance_frac': 1 / 20,   # class_imbalance: fewer boxes than this share of the median class
    'rare_min': 10,             # rare_class: fewer boxes than this
    'suspicion_min': 0.8,       # likely_mislabel: share of neighbours with another class
    'outlier_z': 3.0,           # outlier: z-score of the distance to the class centre, within its class
    'near_dup': 0.97,           # near_dup: cosine similarity
    'leak_sim': 0.95,           # split_leak: cosine between boxes of a train and a val image (below near_dup: the
                                # same scene re-shot is already a leak, not only the same pixels)
    'cl_ood_z': 3.0,            # cl_ood: z-score of cleanlab's out-of-distribution score (features)
    'cl_od_max': 0.1,           # cl_badloc / cl_swap / cl_overlooked: cleanlab score below this (0-1, low = bad)
    'limit_per_type': 20000,    # items kept per embedding / model type
    'model_min_score': 0.5,
    'pair_vector_max': 64,      # images with more boxes than this get a per-image IoU matrix
    'keep_backups': 50,
}
_CLASS_TOKEN = re.compile(r'^(\s*)(\S+)')


# ---------------------------------------------------------------- parsing

def split_lines(text):
    """Lines with their own line ending kept (joined back they give `text` exactly)."""
    return text.splitlines(keepends=True)


def _body(line):
    """(content, line ending) of one line from split_lines."""
    parts = line.splitlines()
    content = parts[0] if parts else ''
    return content, line[len(content):]


def parse_label(text):
    """Every line of a label file, classified.

    Returns (boxes, bad): boxes = [(line, cls, cx, cy, w, h, n_parts)] for exactly the lines the indexer
    turns into boxes (same order, so the k-th entry is the k-th box id of that image; `line` is its box_idx), and
    bad = [(line, reason, type)] for the lines it drops (type 'bad_line' or 'degenerate')."""
    boxes, bad = [], []
    for ln, line in enumerate(text.splitlines()):
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 5:
            bad.append((ln, f'{len(parts)} values, need 5', 'bad_line'))
            continue
        try:
            cls = int(float(parts[0]))
            cx, cy, w, h = (float(v) for v in parts[1:5])
        except (ValueError, OverflowError):
            bad.append((ln, 'not a number', 'bad_line'))
            continue
        if cls < 0:
            bad.append((ln, f'negative class id {cls}', 'bad_line'))
            continue
        if w <= 0 or h <= 0:
            bad.append((ln, f'zero or negative size (w={w:g}, h={h:g})', 'degenerate'))
            continue
        boxes.append((ln, cls, cx, cy, w, h, len(parts)))
    return boxes, bad


def _fmt(v):
    return f'{v:.6f}'.rstrip('0').rstrip('.') if v else '0'


def _px_box(b, img_w, img_h):
    """A parsed box (line, cls, cx, cy, w, h, ...) as the indexer's pixel [x1, y1, x2, y2]."""
    return list(yolo_to_pixel(b[2], b[3], b[4], b[5], img_w, img_h))


def clamp_box(cx, cy, w, h):
    """The box clipped to [0, 1] as (cx, cy, w, h), or None when nothing of it is inside the image."""
    x1, y1 = max(0.0, cx - w / 2), max(0.0, cy - h / 2)
    x2, y2 = min(1.0, cx + w / 2), min(1.0, cy + h / 2)
    if x2 - x1 <= 1e-6 or y2 - y1 <= 1e-6:
        return None
    return (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1


def apply_ops(text, ops):
    """Apply line ops to the text of one label file. Line numbers refer to `text` (all ops at once).

    Returns (new_text, line_map): line_map[old line] = new line number, or None when it was removed.
    A delete wins over an edit of the same line."""
    lines = split_lines(text)
    n = len(lines)
    removed, edits, added = set(), {}, []
    for op in ops:
        kind, ln = op.get('op'), op.get('line')
        if kind not in OPS:
            raise ValueError(f'unknown op {kind!r}')
        if kind == 'add':                # a new box line, appended (no line number: it has none yet)
            cls = op.get('cls')
            if not isinstance(cls, int) or isinstance(cls, bool) or cls < 0:
                raise ValueError(f'add needs a class id >= 0, got {cls!r}')
            c = clamp_box(*_op_box(op))
            if c is None:
                raise ValueError('add: the box is outside the image')
            added.append(' '.join([str(cls), *(_fmt(v) for v in c)]))
            continue
        if not isinstance(ln, int) or not 0 <= ln < n:
            raise ValueError(f'line {ln!r} out of range (file has {n} lines)')
        if kind in DELETE_OPS:
            removed.add(ln)
        elif kind == 'set_class':
            cls = op.get('cls')
            if not isinstance(cls, int) or cls < 0:
                raise ValueError(f'set_class needs a class id >= 0, got {cls!r}')
            content, end = _body(edits.get(ln, lines[ln]))
            if not _CLASS_TOKEN.match(content):
                raise ValueError(f'line {ln} is empty')
            edits[ln] = _CLASS_TOKEN.sub(lambda m: m.group(1) + str(cls), content, count=1) + end
        elif kind == 'clamp':
            content, end = _body(edits.get(ln, lines[ln]))
            parts = content.split()
            try:
                cls_tok = parts[0]
                cx, cy, w, h = (float(v) for v in parts[1:5])
            except (ValueError, IndexError) as e:
                raise ValueError(f'line {ln} is not a box') from e
            c = clamp_box(cx, cy, w, h)
            if c is None:
                removed.add(ln)          # nothing left inside the image
                continue
            edits[ln] = ' '.join([cls_tok, *(_fmt(v) for v in c), *parts[5:]]) + end
        elif kind == 'set_box':          # new coordinates; the class token and any extra tokens are kept
            content, end = _body(edits.get(ln, lines[ln]))
            parts = content.split()
            if len(parts) < 5:
                raise ValueError(f'line {ln} is not a box')
            c = clamp_box(*_op_box(op))
            if c is None:
                raise ValueError(f'set_box: line {ln} would be outside the image')
            edits[ln] = ' '.join([parts[0], *(_fmt(v) for v in c), *parts[5:]]) + end
    out, line_map = [], {}
    for i, line in enumerate(lines):
        if i in removed:
            line_map[i] = None
            continue
        line_map[i] = len(out)
        out.append(edits.get(i, line))
    # deleting the last line can leave the new last line without its newline only if it had none: keep as is
    if added:
        nl = '\r\n' if lines and lines[0].endswith('\r\n') else '\n'
        if out and not out[-1].endswith(('\n', '\r')):
            out[-1] += nl                # the old last line had no newline: the new lines go below it
        out.extend(a + nl for a in added)
    return ''.join(out), line_map


def _op_box(op):
    """(cx, cy, w, h) of an add / set_box op, as finite floats with a positive size."""
    try:
        v = tuple(float(op[k]) for k in ('cx', 'cy', 'w', 'h'))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f'{op.get("op")} needs numbers cx, cy, w, h') from e
    if not all(np.isfinite(v)) or v[2] <= 0 or v[3] <= 0:
        raise ValueError(f'{op.get("op")}: bad box {v}')
    return v


def _decode(data):
    return data.decode('utf-8', 'surrogateescape')


def _encode(text):
    return text.encode('utf-8', 'surrogateescape')


# ---------------------------------------------------------------- the per-file rules (vectorised)

def _pairs(starts, counts, vmax):
    """(a, b) global box indices of every within-image pair, for images with 2..vmax boxes."""
    aa, bb = [], []
    for n in np.unique(counts):
        if n < 2 or n > vmax:
            continue
        s = starts[counts == n]
        iu, ju = np.triu_indices(int(n), 1)
        aa.append((s[:, None] + iu[None, :]).ravel())
        bb.append((s[:, None] + ju[None, :]).ravel())
    if not aa:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    return np.concatenate(aa), np.concatenate(bb)


def _iou(x1, y1, x2, y2, a, b):
    iw = np.clip(np.minimum(x2[a], x2[b]) - np.maximum(x1[a], x1[b]), 0, None)
    ih = np.clip(np.minimum(y2[a], y2[b]) - np.maximum(y1[a], y1[b]), 0, None)
    inter = iw * ih
    area = (x2 - x1) * (y2 - y1)
    return inter / np.maximum(area[a] + area[b] - inter, 1e-12)


def _norm_box(cx, cy, w, h):
    return [round(cx - w / 2, 5), round(cy - h / 2, 5), round(cx + w / 2, 5), round(cy + h / 2, 5)]


class Curation:
    """Scan results + fix / undo of one open dataset (one State)."""

    def __init__(self, state, cfg=None):
        self.state = state                  # the real State (not the routes' proxy): it is patched in place
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.lock = threading.RLock()        # the in-memory results (short holds: never across file I/O)
        self.fix_lock = threading.Lock()     # one fix / undo batch at a time; a scan waits for the running one
        self.ready = False
        self.scanning = False
        self.scanned_at = None
        self.scan_s = None
        self.files = {}           # label name -> {'image', 'sha1', 'ids': [box id per valid line] | None, 'wh'}
        self.file_items = {}      # label name -> [items]
        self.image_items = []     # images without a label file, unreadable / duplicate images
        self.derived = []         # dataset, embedding and model items (rebuilt after every fix)
        self.n_classes = 0
        self.class_names = []
        self.scores = None        # k-NN scores used for the embedding rules (feature key, dict)
        self.model_raw = None     # (model name, label_issues, fp boxes, cleanlab od) of the last scan with a model
        self.patch_cls = {}       # box id -> class since the index was built
        self.splits = {}          # image base name -> 'train' | 'val' | 'test' (models.split_source), for split_leak
        self._flat = None
        self._fp = getattr(state, 'fingerprint', None)
        self._migrate_backups()

    # ------------------------------------------------------------ helpers
    @property
    def backup_dir(self):
        """Per saved dataset, not per index version (State.persist_dir): a fix changes the fingerprint, so after a
        re-index the workspace folder is a new one, and the undo history must still be there."""
        return Path(getattr(self.state, 'persist_dir', None) or self.state.cache_dir) / 'curation_backup'

    def _migrate_backups(self):
        """Backups made when they lived in the workspace folder: moved to the per-dataset folder once."""
        cd = getattr(self.state, 'cache_dir', None)
        if cd is None:
            return
        old, new = Path(cd) / 'curation_backup', self.backup_dir
        if old == new or not old.is_dir():
            return
        try:
            new.mkdir(parents=True, exist_ok=True)
            for p in old.iterdir():
                if p.is_dir() and not (new / p.name).exists():
                    p.rename(new / p.name)
            shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass

    @property
    def cache_path(self):
        return Path(self.state.cache_dir) / f'curation_{(self._fp or "none")[:12]}.json'

    def _deleted(self):
        st = self.state
        d = getattr(st, 'deleted', None)
        if d is None or len(d) != len(st.samples):
            d = np.zeros(len(st.samples), bool)
            st.deleted = d
        return d

    def _img_wh(self, image):
        ids = self.state.by_image.get(image) if self.state.by_image else None
        if ids:
            s = self.state.samples[ids[0]]
            return s['img_w'], s['img_h']
        return None

    def _read_all(self, src, names, progress):
        if hasattr(src, 'read_labels_bytes'):
            return src.read_labels_bytes(names)
        if getattr(src, 'root', None) is not None and getattr(src, 'kind', '') == 'local':
            # a plain loop: small local files come from the page cache, and threads only fight over the GIL
            # (measured on 20k files: 0.45 s in a loop, 3 s on 8 threads)
            d = os.path.join(str(src.root), 'labels')
            out = []
            for k, n in enumerate(names):
                try:
                    with open(os.path.join(d, n), 'rb') as f:
                        out.append(f.read())
                except FileNotFoundError:
                    out.append(b'')
                if k % 5000 == 0:
                    progress(k, len(names))
            return out
        if hasattr(src, 'read_label_bytes'):
            return [src.read_label_bytes(n) for n in names]
        if hasattr(src, 'read_labels'):
            return [_encode(t) for t in src.read_labels(names)]
        return [_encode(src.read_label(n)) for n in names]

    def _read_one(self, label):
        src = self.state.source
        if hasattr(src, 'read_label_bytes'):
            return src.read_label_bytes(label)
        try:
            return _encode(src.read_label(label))
        except FileNotFoundError:
            return b''

    def _box_ids(self, image, boxes):
        """Index box ids aligned with the valid lines of a label file, or None when the index does not match it."""
        ids = self.state.by_image.get(image) if image else None
        if ids is None:
            return [] if not boxes else None
        if len(ids) != len(boxes):
            return None
        lab = self.state.labels
        if any(int(lab[i]) != b[1] for i, b in zip(ids, boxes)):
            return None
        return list(ids)

    # ------------------------------------------------------------ scan
    def scan(self, progress=None, feature=None, model=None, splits=None):
        """Full scan (runs as a background job). model = (name, label_issues list) or None; splits = {image base
        name: split} or None."""
        progress = progress or (lambda *a, **k: None)
        t0 = time.time()
        st, src = self.state, self.state.source
        with self.fix_lock, self.lock:       # a running fix batch finishes first
            self.scanning = True
        try:
            progress(stage='listing files')
            labels = src.list_labels()
            images = src.list_images()
            image_set = set(images)
            classes = src.read_classes()
            self.n_classes = len(classes)
            self.class_names = list(st.classes) if st.classes else list(classes)
            progress(0, len(labels), 'reading label files')
            datas = self._read_all(src, labels, progress)
            progress(0, len(labels), 'checking labels')
            files, file_items = {}, {}
            for name, data in zip(labels, datas):
                img = find_image(image_set, Path(name).stem)
                files[name], file_items[name] = self._scan_file(name, img, data)
            self._check_boxes(files, file_items, labels)
            progress(stage='checking images')
            image_items = self._check_images(src, images, files)
            with self.lock:
                self.files, self.file_items, self.image_items = files, file_items, image_items
                self.model_raw = model
                self.splits = dict(splits or {})
                self.scores = self._pick_scores(feature)
                self._derive()
                self.ready = True
                self.scanned_at = time.time()
                self.scan_s = round(time.time() - t0, 2)
            self._save()
            return self.summary()
        finally:
            with self.lock:
                self.scanning = False

    def _scan_file(self, name, image, data):
        """(file info, line-level items without the cross-box rules) for one label file."""
        text = _decode(data)
        boxes, bad = parse_label(text)
        info = {'image': image, 'sha1': sha1_bytes(data), 'boxes': boxes, 'ids': self._box_ids(image, boxes),
                'wh': self._img_wh(image) if image else None, 'n_lines': len(text.splitlines())}
        items = []
        if image is None:
            items.append(self._item('missing_image', name, None, None, f'{name} has no image file next to it'))
        elif not boxes and not bad:
            items.append(self._item('empty_label', name, image, None, 'label file is empty: a background image'))
        for ln, reason, typ in bad:
            items.append(self._item(typ, name, image, ln, f'line {ln + 1}: {reason}',
                                    fixes=[{'op': 'drop_line' if typ == 'bad_line' else 'delete', 'line': ln,
                                            'label': 'Remove line' if typ == 'bad_line' else 'Delete box'}]))
        return info, items

    def _check_boxes(self, files, file_items, names):
        """Every box rule at once over all boxes of the given files (numpy)."""
        cfg = self.cfg
        f_idx, rows = [], []
        for k, name in enumerate(names):
            for b in files[name]['boxes']:
                f_idx.append(k)
                rows.append(b)
        if not rows:
            return
        arr = np.asarray([r[:6] for r in rows], np.float64)
        f_idx = np.asarray(f_idx, np.int64)
        line, cls, cx, cy, w, h = (arr[:, i] for i in range(6))
        line, cls = line.astype(np.int64), cls.astype(np.int64)
        nparts = np.asarray([r[6] for r in rows], np.int64)
        x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        W = np.full(len(names), np.nan)
        H = np.full(len(names), np.nan)
        for k, name in enumerate(names):
            wh = files[name]['wh']
            if wh:
                W[k], H[k] = wh
        bw, bh = W[f_idx], H[f_idx]
        known = ~np.isnan(bw)
        finite = np.isfinite(arr[:, 2:6]).all(1)

        def add(i, typ, detail, fixes, extra=None):
            k = int(f_idx[i])
            name = names[k]
            fi = files[name]
            item = self._item(typ, name, fi['image'], int(line[i]), detail, fixes=fixes,
                              boxes=[_norm_box(cx[i], cy[i], w[i], h[i])] if finite[i] else [])
            ids = fi['ids']
            if ids is not None:
                bi = self._valid_pos(fi, int(line[i]))
                if bi is not None:
                    item['box_ids'] = [ids[bi]]
            if extra:
                item.update(extra)
            file_items[name].append(item)

        cname = self._cname
        for i in np.nonzero(~finite)[0]:
            add(i, 'bad_line', f'line {int(line[i]) + 1}: not a finite number',
                [{'op': 'drop_line', 'line': int(line[i]), 'label': 'Remove line'}])
        if self.n_classes:
            for i in np.nonzero(finite & (cls >= self.n_classes))[0]:
                add(i, 'bad_class', f'line {int(line[i]) + 1}: class id {int(cls[i])} but only {self.n_classes} classes',
                    [{'op': 'delete', 'line': int(line[i]), 'label': 'Delete box'},
                     {'op': 'set_class', 'line': int(line[i]), 'label': 'Change class'}])
        eps = cfg['oob_eps']
        oob = finite & ((x1 < -eps) | (y1 < -eps) | (x2 > 1 + eps) | (y2 > 1 + eps))
        tiny = finite & known & ((w * bw < cfg['tiny_px']) | (h * bh < cfg['tiny_px']))
        for i in np.nonzero(tiny)[0]:
            add(i, 'degenerate', f'line {int(line[i]) + 1}: {cname(cls[i])} box is {w[i] * bw[i]:.1f} × {h[i] * bh[i]:.1f} px',
                [{'op': 'delete', 'line': int(line[i]), 'label': 'Delete box'}])
        for i in np.nonzero(oob & ~tiny)[0]:
            c = clamp_box(cx[i], cy[i], w[i], h[i])
            fixes = ([{'op': 'clamp', 'line': int(line[i]), 'label': 'Clamp to image'}] if c is not None and nparts[i] == 5 else [])
            fixes.append({'op': 'delete', 'line': int(line[i]), 'label': 'Delete box'})
            add(i, 'oob', f'line {int(line[i]) + 1}: {cname(cls[i])} box reaches '
                          f'x {x1[i]:.3f}…{x2[i]:.3f}, y {y1[i]:.3f}…{y2[i]:.3f}' + ('' if c else ' (entirely outside)'), fixes)
        asp = np.where(known, (w * bw) / np.maximum(h * bh, 1e-9), w / np.maximum(h, 1e-9))
        amax = cfg['aspect_max']
        for i in np.nonzero(finite & ~tiny & ((asp > amax) | (asp < 1 / amax)))[0]:
            add(i, 'extreme_aspect', f'line {int(line[i]) + 1}: {cname(cls[i])} box aspect {asp[i]:.1f}:1'
                if asp[i] >= 1 else f'line {int(line[i]) + 1}: {cname(cls[i])} box aspect 1:{1 / asp[i]:.1f}',
                [{'op': 'delete', 'line': int(line[i]), 'label': 'Delete box'}])
        # overlapping pairs inside one image: contiguous runs of f_idx
        starts = np.flatnonzero(np.r_[True, f_idx[1:] != f_idx[:-1]])
        counts = np.diff(np.r_[starts, len(f_idx)])
        a, b = _pairs(starts, counts, cfg['pair_vector_max'])
        big = starts[counts > cfg['pair_vector_max']]
        for s, n in zip(big, counts[counts > cfg['pair_vector_max']]):
            iu, ju = np.triu_indices(int(n), 1)
            a, b = np.r_[a, s + iu], np.r_[b, s + ju]
        if len(a):
            iou = _iou(x1, y1, x2, y2, a, b)
            hit = (iou >= cfg['iou_dup']) & finite[a] & finite[b]
            for p in np.nonzero(hit)[0]:
                i, j = int(a[p]), int(b[p])
                k = int(f_idx[i])
                name = names[k]
                fi = files[name]
                same = cls[i] == cls[j]
                typ = 'dup_box' if same else 'conflict_box'
                detail = (f'lines {int(line[i]) + 1} and {int(line[j]) + 1}: two {cname(cls[i])} boxes, IoU {iou[p]:.2f}' if same else
                          f'lines {int(line[i]) + 1} and {int(line[j]) + 1}: {cname(cls[i])} and {cname(cls[j])} on the same object, IoU {iou[p]:.2f}')
                fixes = ([{'op': 'dedupe', 'line': int(line[j]), 'label': 'Remove duplicate'}] if same else
                         [{'op': 'delete', 'line': int(line[i]), 'label': f'Keep {cname(cls[j])}'},
                          {'op': 'delete', 'line': int(line[j]), 'label': f'Keep {cname(cls[i])}'}])
                item = self._item(typ, name, fi['image'], int(line[j]), detail, fixes=fixes,
                                  boxes=[_norm_box(cx[i], cy[i], w[i], h[i]), _norm_box(cx[j], cy[j], w[j], h[j])])
                item['lines'] = [int(line[i]), int(line[j])]
                item['id'] = f'{typ}|{name}|{int(line[i])}|{int(line[j])}'
                if fi['ids'] is not None:
                    pi, pj = self._valid_pos(fi, int(line[i])), self._valid_pos(fi, int(line[j]))
                    item['box_ids'] = [fi['ids'][q] for q in (pi, pj) if q is not None]
                file_items[name].append(item)

    @staticmethod
    def _valid_pos(fi, ln):
        for k, b in enumerate(fi['boxes']):
            if b[0] == ln:
                return k
        return None

    def _cname(self, c):
        c = int(c)
        return self.class_names[c] if 0 <= c < len(self.class_names) else f'class {c}'

    def _check_images(self, src, images, files):
        items = []
        labelled = {fi['image'] for fi in files.values() if fi['image']}
        for img in images:
            if img not in labelled:
                items.append(self._item('empty_label', None, img, None, 'no label file: a background image'))
        indexed = set(self.state.by_image) if self.state.by_image else set()
        root = getattr(src, 'root', None)
        if root is not None and (Path(root) / 'images').is_dir():
            items += self._local_image_checks(Path(root) / 'images', images, indexed)
        elif getattr(src, 'kind', None) == 's3':
            try:
                lst = src.listing()
            except Exception:  # noqa: BLE001 - the scan still reports the label rules
                lst = {}
            groups = {}
            for img in images:
                size, etag = lst.get(f'images/{img}', (None, None))
                if size == 0:
                    items.append(self._item('unreadable_image', None, img, None, 'empty file (0 bytes)'))
                elif size is not None and etag:
                    groups.setdefault((size, etag), []).append(img)
            items += self._dup_items([g for g in groups.values() if len(g) > 1])
        return items

    def _local_image_checks(self, folder, images, indexed):
        sizes = {}
        with os.scandir(folder) as it:
            for e in it:
                try:
                    sizes[e.name] = e.stat().st_size
                except OSError:
                    pass
        items = []
        # unreadable: only images the indexer never decoded (an indexed image was opened fine)
        todo = [img for img in images if img not in indexed]

        def readable(img):
            if sizes.get(img, 0) == 0:
                return img, 'empty file (0 bytes)'
            try:
                with Image.open(folder / img) as im:
                    im.size
                return img, None
            except Exception as e:  # noqa: BLE001
                return img, f'cannot be decoded ({type(e).__name__})'

        # same reason as _read_all: plain loops over local files beat a thread pool
        for img in todo:
            err = readable(img)[1]
            if err:
                items.append(self._item('unreadable_image', None, img, None, err))
        # duplicate files: only same-size files can be identical; then a cheap partial hash, then the full one
        by_size = {}
        for img in images:
            s = sizes.get(img)
            if s:
                by_size.setdefault(s, []).append(img)
        cands = [g for g in by_size.values() if len(g) > 1]

        def part(img):
            size = sizes[img]
            with open(os.path.join(fdir, img), 'rb') as f:
                head = f.read(65536)
                if size > 131072:
                    f.seek(-65536, 2)
                    head += f.read(65536)
            return hashlib.blake2b(head + str(size).encode(), digest_size=16).hexdigest()

        def full(img):
            h = hashlib.blake2b(digest_size=16)
            with open(os.path.join(fdir, img), 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    h.update(chunk)
            return h.hexdigest()

        fdir = str(folder)
        groups = {}
        for g in cands:
            for img in g:
                groups.setdefault((sizes[img], part(img)), []).append(img)
        dups = []
        for g in groups.values():
            if len(g) < 2:
                continue
            fh = {}
            for img in g:
                fh.setdefault(full(img), []).append(img)
            dups += [x for x in fh.values() if len(x) > 1]
        return items + self._dup_items(dups)

    def _dup_items(self, groups):
        items = []
        for g in groups:
            g = sorted(g)
            for img in g[1:]:
                it = self._item('dup_image', None, img, None, f'same file bytes as {g[0]}' +
                                (f' (+{len(g) - 2} more copies)' if len(g) > 2 else ''))
                it['others'] = [g[0]]
                items.append(it)
        return items

    def _item(self, typ, label, image, line, detail, fixes=None, boxes=None):
        group, sev, _, _ = TYPES[typ]
        return {'id': f'{typ}|{label or image}|{"" if line is None else line}', 'type': typ, 'severity': sev,
                'group': group, 'image': image, 'label': label, 'line': line, 'box_ids': [], 'boxes': boxes or [],
                'detail': detail, 'fixes': fixes or []}

    # ------------------------------------------------------------ dataset, embedding and model rules
    def _pick_scores(self, feature):
        st = self.state
        keys = [k for k in (feature, getattr(st, 'scoresFeature', None), st.default_feature) if k]
        keys += [k for k in list(st.scores) if k not in keys]
        for k in keys:
            sc = st.scores.get(k)
            if sc is None and k in st.registry:
                try:
                    if st.ready and st._scores_file(k).exists():
                        sc = st.get_scores(k)
                except Exception:  # noqa: BLE001
                    sc = None
            if sc is not None and len(sc['suspicion']) == len(st.samples):
                return k, sc
        return None

    def _pick_label_quality(self):
        """(feature, per-box arrays) of the cleanlab label quality computed for the k-NN scores' feature (else the
        default one), None when there is none for the current labels."""
        st = self.state
        if not hasattr(st, 'cached_label_quality'):
            return None
        keys = [k for k in ((self.scores or (None,))[0], st.default_feature) if k]
        for k in dict.fromkeys(keys):
            try:
                hit = st.cached_label_quality(k)
            except Exception:  # noqa: BLE001 - the other rules still run
                hit = None
            if hit is not None and len(hit[0]['cl_quality']) == len(st.samples):
                return k, hit[0]
        return None

    def _derive(self):
        """Dataset, embedding and model items from the current files + patched labels."""
        st, cfg = self.state, self.cfg
        out = []
        loc = {}                   # box id -> (label name, line)
        counts = np.zeros(max(self.n_classes, len(self.class_names), 1), np.int64)
        for name, fi in self.files.items():
            for k, b in enumerate(fi['boxes']):
                if b[1] < len(counts):
                    counts[b[1]] += 1
                if fi['ids'] is not None:
                    loc[fi['ids'][k]] = (name, b[0])
        nc = self.n_classes or len(self.class_names)
        if nc:
            c = counts[:nc]
            med = float(np.median(c)) if len(c) else 0.0
            for k in range(nc):
                if c[k] < cfg['rare_min']:
                    out.append(self._ds_item('rare_class', k, f'{self._cname(k)}: only {int(c[k])} boxes '
                                             f'(fewer than {cfg["rare_min"]})'))
                elif med > 0 and c[k] < med * cfg['imbalance_frac']:
                    out.append(self._ds_item('class_imbalance', k, f'{self._cname(k)}: {int(c[k])} boxes, the median class '
                                             f'has {int(med)}'))
        deleted = self._deleted()
        if self.scores is not None:
            out += self._embedding_items(self.scores, loc, deleted)
        lq = self._pick_label_quality()
        if lq is not None:
            out += self._cleanlab_items(lq, loc, deleted)
        if self.model_raw is not None:
            out += self._model_items(self.model_raw, loc, deleted)
        if self.splits:
            out += self._leak_items(loc, deleted)
        self.derived = out
        self._flat = None

    def _ds_item(self, typ, k, detail):
        it = self._item(typ, None, None, None, detail)
        it['id'] = f'{typ}|{k}'
        it['cls'] = k
        return it

    def _box_item(self, typ, sid, loc, detail, fixes_fn=None):
        s = self.state.samples[sid]
        name, ln = loc.get(sid, (s.get('label'), None))
        w, h = s['img_w'], s['img_h']
        x1, y1, x2, y2 = s['bbox']
        it = self._item(typ, name, s['image'], ln, detail,
                        fixes=fixes_fn(ln) if (fixes_fn and ln is not None) else [],
                        boxes=[[round(x1 / w, 5), round(y1 / h, 5), round(x2 / w, 5), round(y2 / h, 5)]])
        it['id'] = f'{typ}|{sid}'
        it['box_ids'] = [int(sid)]
        return it

    def _embedding_items(self, scores, loc, deleted):
        key, sc = scores
        st, cfg, lim = self.state, self.cfg, self.cfg['limit_per_type']
        lab = st.labels
        out = []
        sus, sug = np.asarray(sc['suspicion']), np.asarray(sc['suggested'])
        cand = np.nonzero((sus >= cfg['suspicion_min']) & (sug != lab) & ~deleted)[0]
        cand = cand[np.argsort(-sus[cand], kind='stable')][:lim]
        for sid in cand:
            g = int(sug[sid])
            out.append(self._box_item('likely_mislabel', int(sid), loc,
                                      f'{self._cname(lab[sid])} box, but {sus[sid] * 100:.0f}% of its nearest neighbours '
                                      f'({key}) are {self._cname(g)}',
                                      lambda ln, g=g: [{'op': 'set_class', 'line': ln, 'cls': g, 'label': f'Change to {self._cname(g)}'},
                                                       {'op': 'delete', 'line': ln, 'label': 'Delete box'}]))
        o = np.asarray(sc['outlier'], np.float64)
        z = np.zeros_like(o)
        for c in np.unique(lab):
            m = (lab == c) & ~deleted
            if m.sum() >= 5:
                sd = o[m].std()
                z[m] = (o[m] - o[m].mean()) / sd if sd > 0 else 0
        cand = np.nonzero((z >= cfg['outlier_z']) & ~deleted)[0]
        cand = cand[np.argsort(-z[cand], kind='stable')][:lim]
        for sid in cand:
            out.append(self._box_item('outlier', int(sid), loc, f'{self._cname(lab[sid])} box far from its class '
                                      f'centre (z = {z[sid]:.1f}, {key})',
                                      lambda ln: [{'op': 'delete', 'line': ln, 'label': 'Delete box'}]))
        from ..ml import analysis
        pairs = analysis.near_duplicates(sc['nbr_idx'], sc['nbr_sim'], cfg['near_dup'], lim * 2,
                                         deleted if deleted.any() else None)
        samples = st.samples
        n = 0
        for a, b, sim in pairs:
            if deleted[a] or deleted[b] or samples[a]['image'] == samples[b]['image']:
                continue
            it = self._box_item('near_dup', int(b), loc, f'near-identical to box #{a} in {samples[a]["image"]} '
                                f'(cosine {sim:.3f})', lambda ln: [{'op': 'delete', 'line': ln, 'label': 'Delete this box'}])
            it['id'] = f'near_dup|{a}|{b}'
            it['box_ids'] = [int(b), int(a)]
            it['others'] = [samples[a]['image']]
            out.append(it)
            n += 1
            if n >= lim:
                break
        return out

    def _cleanlab_items(self, lq, loc, deleted):
        key, per = lq
        lab, cfg, lim = self.state.labels, self.cfg, self.cfg['limit_per_type']
        out = []
        issue, rank, sug = per['cl_issue'], per['cl_rank'], per['cl_suggested']
        cand = np.nonzero(issue & ~deleted)[0]
        cand = cand[np.argsort(rank[cand], kind='stable')][:lim]
        for sid in cand:
            g, q, gp = int(sug[sid]), float(per['cl_quality'][sid]), float(per['cl_given_prob'][sid])
            fixes = (lambda ln, g=g: [{'op': 'set_class', 'line': ln, 'cls': g, 'label': f'Change to {self._cname(g)}'},
                                      {'op': 'delete', 'line': ln, 'label': 'Delete box'}]) if g != lab[sid] else \
                (lambda ln: [{'op': 'delete', 'line': ln, 'label': 'Delete box'}])
            out.append(self._box_item('cl_label_issue', int(sid), loc,
                                      f'{self._cname(lab[sid])} box: a {key} classifier that never saw it gives '
                                      f'{self._cname(lab[sid])} {gp * 100:.0f}% and {self._cname(g)} '
                                      f'{float(per["pred_probs"][sid][g]) * 100:.0f}% (label quality {q:.2f})', fixes))
        o = np.asarray(per['cl_ood_feat'], np.float64)
        ok = np.isfinite(o) & ~deleted
        if ok.sum() >= 5 and o[ok].std() > 0:
            z = np.zeros_like(o)
            z[ok] = (o[ok] - o[ok].mean()) / o[ok].std()
            cand = np.nonzero(ok & (z >= cfg['cl_ood_z']))[0]
            cand = cand[np.argsort(-z[cand], kind='stable')][:lim]
            for sid in cand:
                out.append(self._box_item('cl_ood', int(sid), loc, f'{self._cname(lab[sid])} box unlike the rest of the '
                                          f'dataset (cleanlab out-of-distribution, z = {z[sid]:.1f}, {key})',
                                          lambda ln: [{'op': 'delete', 'line': ln, 'label': 'Delete box'}]))
        return out

    def _cleanlab_model_items(self, name, od, loc, deleted):
        per, overlooked = od
        lab, cfg, lim = self.state.labels, self.cfg, self.cfg['limit_per_type']
        out = []
        for typ, arr, text in (('cl_swap', per['cl_swap'], 'probably has the wrong class'),
                               ('cl_badloc', per['cl_badloc'], 'is probably drawn in the wrong place')):
            a = np.asarray(arr, np.float64)
            cand = np.nonzero(np.isfinite(a) & (a < cfg['cl_od_max']) & ~deleted)[0]
            cand = cand[np.argsort(a[cand], kind='stable')][:lim]
            for sid in cand:
                out.append(self._box_item(typ, int(sid), loc, f'{self._cname(lab[sid])} box {text} '
                                          f'(cleanlab on {name}, score {a[sid]:.2f})',
                                          lambda ln: [{'op': 'delete', 'line': ln, 'label': 'Delete box'}]))
        for it in overlooked[:lim]:
            if it['quality'] >= cfg['cl_od_max']:
                continue
            x = self._item('cl_overlooked', None, it['image'], None,
                           f'{name} finds a {self._cname(it["cls"])} ({it["score"]:.2f}) the labels miss '
                           f'(cleanlab overlooked score {it["quality"]:.2f})', boxes=[it['box']] if it.get('box') else [])
            x['id'] = f'cl_overlooked|{it["image"]}|{it["pid"]}'
            x['pred'] = {'pid': it['pid'], 'cls': it['cls'], 'score': it['score']}
            out.append(x)
        return out

    def _split_of(self, image):
        return self.splits.get(Path(image).name) if image else None

    def _leak_items(self, loc, deleted):
        """split_leak: one item per (val / test image, train image) pair. Exact copies come from the dup_image items;
        near-copies from every k-NN pair across two splits at >= leak_sim (filtered before any limit, unlike near_dup)."""
        st, lim = self.state, self.cfg['limit_per_type']
        out, seen = [], set()
        for it in self.image_items:
            if it['type'] != 'dup_image':
                continue
            a, b = it['image'], it['others'][0]
            sa, sb = self._split_of(a), self._split_of(b)
            if not sa or not sb or sa == sb:
                continue
            if sa == 'train':
                a, b, sa, sb = b, a, sb, sa
            x = self._item('split_leak', None, a, None, f'{sa} image is a byte-identical copy of {sb} image {b}')
            x['id'] = f'split_leak|{a}|{b}'
            x['others'] = [b]
            out.append(x)
            seen.add((a, b))
        if self.scores is None or not len(st.samples):
            return out
        key, sc = self.scores
        spec = st.registry.get(key) if hasattr(st.registry, 'get') else None
        if getattr(spec, 'group', None) == 'simple':
            # box geometry / HOG / colour: any two boxes of a similar size look "identical" there (a 25-image smoke
            # dataset flagged most of its val images), so only an appearance model's neighbours count as a copy
            return out
        nbr_idx, nbr_sim = np.asarray(sc['nbr_idx']), np.asarray(sc['nbr_sim'])
        if nbr_idx.size == 0:
            return out
        names = sorted(st.by_image) if st.by_image else sorted({s['image'] for s in st.samples})
        code = {'train': 1, 'val': 2, 'test': 3}
        img_split = {n: code.get(self._split_of(n), 0) for n in names}
        split = np.fromiter((img_split.get(s['image'], 0) for s in st.samples), np.int8, len(st.samples))
        rows, cols = np.nonzero(nbr_sim >= self.cfg['leak_sim'])
        j = nbr_idx[rows, cols]
        keep = (split[rows] > 0) & (split[j] > 0) & (split[rows] != split[j]) & ~deleted[rows] & ~deleted[j]
        rows, j, sim = rows[keep], j[keep], nbr_sim[rows[keep], cols[keep]]
        # held-out side first: a pair is listed once, under the val / test image (train-vs-train never gets here)
        a = np.where(split[rows] != 1, rows, j)
        b = np.where(split[rows] != 1, j, rows)
        pairs = {}
        for x, y, s in zip(a.tolist(), b.tolist(), sim.tolist()):
            k = (st.samples[x]['image'], st.samples[y]['image'])
            p = pairs.setdefault(k, {'sim': 0.0, 'a': set(), 'b': set()})
            p['sim'] = max(p['sim'], s)
            p['a'].add(x)
            p['b'].add(y)
        for (ia, ib), p in sorted(pairs.items(), key=lambda kv: -kv[1]['sim'])[:lim]:
            if (ia, ib) in seen:
                continue
            ids = sorted(p['a'])
            boxes = []
            for sid in ids:
                s = st.samples[sid]
                w, h = s['img_w'], s['img_h']
                x1, y1, x2, y2 = s['bbox']
                boxes.append([round(x1 / w, 5), round(y1 / h, 5), round(x2 / w, 5), round(y2 / h, 5)])
            n = len(ids)
            x = self._item('split_leak', None, ia, None,
                           f'{self._split_of(ia)} image shares {n} box{"es" if n > 1 else ""} with {self._split_of(ib)} '
                           f'image {ib} (cosine {p["sim"]:.3f}, {key})', boxes=boxes)
            x['id'] = f'split_leak|{ia}|{ib}'
            x['box_ids'] = ids + sorted(p['b'])
            x['others'] = [ib]
            out.append(x)
        return out

    def _model_items(self, model_raw, loc, deleted):
        name, issues, fp_boxes, *rest = model_raw
        lab = self.state.labels
        out = self._cleanlab_model_items(name, rest[0], loc, deleted) if rest and rest[0] else []
        for it in issues:
            kind = it.get('kind')
            if kind == 'wrong_cls':
                sid, pc = it['sid'], it['pred_cls']
                if deleted[sid] or lab[sid] == pc:
                    continue
                out.append(self._box_item('model_wrong_cls', sid, loc,
                                          f'{self._cname(lab[sid])} box, {name} says {self._cname(pc)} ({it["score"]:.2f})',
                                          lambda ln, pc=pc: [{'op': 'set_class', 'line': ln, 'cls': int(pc),
                                                              'label': f'Change to {self._cname(pc)}'}]))
            elif kind == 'missed':
                sid = it['sid']
                if deleted[sid]:
                    continue
                out.append(self._box_item('model_missed', sid, loc,
                                          f'{self._cname(lab[sid])} box: {name} finds nothing here (best overlap score {it["score"]:.2f})',
                                          lambda ln: [{'op': 'delete', 'line': ln, 'label': 'Delete box'}]))
            elif kind == 'fp_bg':
                box = fp_boxes.get(it['pid'])
                x = self._item('model_missing_label', None, it['image'], None,
                               f'{name} finds a {self._cname(it["pred_cls"])} ({it["score"]:.2f}) where there is no label',
                               boxes=[box] if box else [])
                x['id'] = f'model_missing_label|{it["image"]}|{it["pid"]}'
                x['pred'] = {'pid': it['pid'], 'cls': it['pred_cls'], 'score': it['score']}
                out.append(x)
        return out

    # ------------------------------------------------------------ queries
    def all_items(self):
        with self.lock:
            if self._flat is None:
                flat = [it for items in self.file_items.values() for it in items] + self.image_items + self.derived
                flat.sort(key=lambda it: (SEV_RANK[it['severity']], TYPE_ORDER[it['type']], it['image'] or it['label'] or '',
                                          it['line'] if it['line'] is not None else -1))
                self._flat = flat
            return self._flat

    def summary(self, ignored=()):
        ign = set(ignored)
        out = {}
        for it in self.all_items():
            if it['id'] in ign:
                continue
            t = it['type']
            if t not in out:
                g, sev, label, bulk = TYPES[t]
                out[t] = {'n': 0, 'severity': sev, 'group': g, 'label': label, 'bulk': bulk}
            out[t]['n'] += 1
        return out

    def items(self, typ=None, offset=0, limit=200, ignored=(), only_ignored=False):
        ign = set(ignored)
        sel = [it for it in self.all_items() if (not typ or it['type'] == typ) and ((it['id'] in ign) == only_ignored)]
        page = sel[offset:offset + limit]
        return len(sel), [self._public(it) for it in page]

    def _public(self, it):
        fi = self.files.get(it['label']) if it['label'] else None
        out = dict(it)
        out['sha1'] = fi['sha1'] if fi else None
        wh = (fi['wh'] if fi else None) or (self._img_wh(it['image']) if it['image'] else None)
        out['wh'] = list(wh) if wh else None
        return out

    def ids_for(self, types, ignored=()):
        """{'images': names with an image-level issue, 'boxes': box ids} of the given issue types."""
        ts, ign = set(types), set(ignored)
        images, boxes = set(), set()
        for it in self.all_items():
            if it['type'] not in ts or it['id'] in ign:
                continue
            if it['box_ids']:
                boxes.update(it['box_ids'])
            elif it['image']:
                images.add(it['image'])
        return {'images': sorted(images), 'boxes': sorted(boxes)}

    def default_fix_items(self, typ, ignored=()):
        """Fix-all of one type: the first fix of every item, grouped per label file."""
        ign = set(ignored)
        per = {}
        for it in self.all_items():
            if it['type'] != typ or it['id'] in ign or not it['fixes'] or not it['label']:
                continue
            fx = it['fixes'][0]
            if fx['op'] == 'set_class' and 'cls' not in fx:
                continue
            per.setdefault(it['label'], []).append({k: v for k, v in fx.items() if k != 'label'})
        return [{'label': name, 'expect_sha1': self.files[name]['sha1'], 'ops': ops} for name, ops in per.items()
                if name in self.files]

    # ------------------------------------------------------------ fixes
    def fix(self, items, summary_text=''):
        """Apply op lists to label files. items: [{label | image, expect_sha1, ops}].

        Returns {applied, files, conflicts, errors, undo_id, patch}. A file that fails (bad op, write error) is
        reported in `errors` and the others still go through: every file written is in the backup (its bytes are
        saved before the write) and in meta.json, so the batch can always be undone. The reads and writes run
        outside self.lock (summary / issues stay answerable during a long S3 batch); fix_lock keeps fixes, undos
        and the start of a scan one at a time."""
        src = self.state.source
        if not getattr(src, 'label_writable', False):
            raise PermissionError(f'{getattr(src, "kind", "this")} source cannot write label files')
        with self.fix_lock:
            with self.lock:
                if self.scanning:
                    raise RuntimeError('a scan is running: try again when it finished')
                deleted = self._deleted()
            undo_id = f'{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:6]}'
            bdir = self.backup_dir / undo_id
            records, conflicts, errors, applied = [], [], [], 0
            patch = {'cls': {}, 'deleted': []}
            by_label = {}
            for it in items:
                name = it.get('label') or self._label_of(it.get('image'))
                if not name:
                    errors.append({'file': it.get('image') or '?', 'error': f'no label file for {it.get("image")!r}'})
                    continue
                ent = by_label.setdefault(name, {'expect': it.get('expect_sha1'), 'ops': []})
                ent['ops'] += list(it.get('ops') or [])
            rows_api = hasattr(src, 'write_label_rows')          # labeling: the undo restores the store rows
            try:
                for k, (name, ent) in enumerate(by_label.items()):
                    fi = self.files.get(name)
                    if fi is None:
                        errors.append({'file': name, 'error': f'unknown label file {name!r} (scan first)'})
                        continue
                    shown = fi['image'] or name
                    binp = bdir / f'{k}.bin'
                    try:
                        before = self._read_one(name)
                        if ent['expect'] is not None and sha1_bytes(before) != ent['expect']:
                            conflicts.append(shown)
                            continue
                        new_text, line_map = apply_ops(_decode(before), ent['ops'])
                        data = _encode(new_text)
                        bdir.mkdir(parents=True, exist_ok=True)
                        binp.write_bytes(before)                  # saved before the write: always undoable
                        payload = data if src.kind in ('local', 's3') else new_text
                        if rows_api:
                            after, rows_before = src.write_label_rows(name, payload, sha1_bytes(before))
                        else:
                            after, rows_before = src.write_label(name, payload, sha1_bytes(before)), None
                    except Conflict:
                        conflicts.append(shown)
                        binp.unlink(missing_ok=True)
                        continue
                    except Exception as e:  # noqa: BLE001 - bad op, OSError, S3 / store error: this file only
                        errors.append({'file': shown, 'error': str(e) or type(e).__name__})
                        binp.unlink(missing_ok=True)
                        continue
                    rec = {'label': name, 'image': fi['image'], 'before_sha1': sha1_bytes(before), 'after_sha1': after,
                           'bin': binp.name, 'ids_before': fi['ids'], 'n_ops': len(ent['ops']),
                           'cls_before': {}, 'deleted': []}
                    if rows_before is not None:
                        rec['rows'] = rows_before
                    records.append(rec)
                    applied += len(ent['ops'])
                    with self.lock:
                        smp = self.state.samples
                        live = self.state.by_image.get(fi['image'], []) if fi['image'] and self.state.by_image else []
                        # line + pixel box of every live box: the undo puts moved / renumbered boxes back
                        rec['idx_before'] = {str(i): [smp[i].get('box_idx'), list(smp[i]['bbox'])] for i in live}
                        rec['cls_before'], rec['deleted'] = self._patch_state(name, data, line_map, patch, deleted)
            finally:
                if records:
                    meta = {'undo_id': undo_id, 'time': time.time(), 'fingerprint': self._fp, 'summary': summary_text,
                            'applied': applied, 'files': records}
                    try:
                        (bdir / 'meta.json').write_text(json.dumps(meta))
                    except (OSError, TypeError, ValueError) as e:
                        errors.append({'file': 'meta.json', 'error': f'backup not saved: {e}'})
                    self._prune()
                    with self.lock:
                        self._derive()
                        self._invalidate()
                elif bdir.is_dir():
                    shutil.rmtree(bdir, ignore_errors=True)
            return {'applied': applied, 'files': len(records), 'conflicts': conflicts, 'errors': errors,
                    'undo_id': undo_id if records else None, 'patch': patch}

    def label_name(self, image):
        """The label file of an image: the scanned one, the index's, else <stem>.txt (created by the first edit)."""
        st = self.state
        ids = st.by_image.get(image) if st.by_image else None
        if ids:
            return st.samples[ids[0]]['label']
        for name, fi in self.files.items():
            if fi['image'] == image:
                return name
        stem = image.rsplit('.', 1)[0] if '.' in image else image
        return f'{stem}.txt'

    def image_boxes(self, image):
        """{label, sha1, boxes:[{line, cls, cx, cy, w, h, sid}]} of one image, straight from its label file."""
        name = self.label_name(image)
        data = self._read_one(name)
        boxes, _ = parse_label(_decode(data))
        ids = self._ids_by_line(image, boxes)
        if ids is None:
            ids = self._box_ids(image, boxes) or [None] * len(boxes)
        return {'label': name, 'sha1': sha1_bytes(data),
                'boxes': [{'line': b[0], 'cls': b[1], 'cx': b[2], 'cy': b[3], 'w': b[4], 'h': b[5], 'sid': sid}
                          for b, sid in zip(boxes, ids)]}

    def edit(self, image, expect_sha1, ops):
        """The image editor's save: one op batch on one image's label file (made when the image had none), through
        fix() (backup, undo, sha1 check). Works without a scan: the file is (re)read and scanned first, so the ops'
        line numbers and expect_sha1 refer to the bytes on disk now. Returns fix()'s result + the boxes after."""
        if not image or not isinstance(image, str):
            raise ValueError('which image?')
        if not ops:
            raise ValueError('nothing to save')
        st = self.state
        if not (st.has_image(image) if hasattr(st, 'has_image') else image in set(st.source.list_images())):
            raise ValueError(f'unknown image {image!r}')
        name = self.label_name(image)
        data = self._read_one(name)

        def ids_of(info):
            ids = self._ids_by_line(image, info['boxes'])
            if ids is not None and None not in ids:
                return ids
            return info['ids']                          # count-based (_box_ids): right while nothing was added
        with self.lock:
            self._rescan(name, image, data, ids_of)
        n_add = sum(1 for o in ops if o.get('op') == 'add')
        res = self.fix([{'label': name, 'expect_sha1': expect_sha1, 'ops': list(ops)}],
                       f'Edit · {image}' + (f' (+{n_add} box{"es" if n_add != 1 else ""})' if n_add else ''))
        with self.lock:
            fi = self.files.get(name)
            n_new = max(0, len(fi['boxes']) - len(st.by_image.get(image, []))) if fi else 0
            pend = getattr(st, 'pending_new', None)
            if pend is None:
                pend = st.pending_new = {}
            if n_new:
                pend[image] = n_new
            else:
                pend.pop(image, None)
        return {**res, **self.image_boxes(image)}

    def edit_status(self):
        st = self.state
        return {'pending_new': sum((getattr(st, 'pending_new', None) or {}).values()),
                'moved': len(getattr(st, 'stale_ids', None) or ()),
                'images': sorted((getattr(st, 'pending_new', None) or {}).keys())[:1000]}

    def _invalidate(self):
        """The State changed under the index: drop the points payload, bump the edit counter (eval / groups caches)."""
        st = self.state
        st.payloads.pop('points', None)
        st.edit_seq = getattr(st, 'edit_seq', 0) + 1

    def _label_of(self, image):
        for name, fi in self.files.items():
            if fi['image'] == image:
                return name
        return None

    def _patch_state(self, name, data, line_map, patch, deleted):
        """Re-scan one fixed file; carry its box ids over to the surviving lines; patch the State.

        The ids of the file before come from the scan (fi['ids']) or, when those are unknown because an earlier edit
        added boxes the index has no id for, from each live box's box_idx (trusted only when every one of them still
        names a box line of the same class). Boxes the edit added get no id (they are placed in the plot by the next
        re-index); moved boxes keep theirs, with the new pixel box, and are listed in State.stale_ids."""
        st = self.state
        fi = self.files[name]
        old_ids, old_boxes = fi['ids'], fi['boxes']
        if old_ids is None:
            old_ids = self._ids_by_line(fi['image'], old_boxes)
        cls_before, gone = {}, []

        def carry(info):
            if old_ids is None:
                return None
            by_new_line = {}
            for k, b in enumerate(old_boxes):
                if old_ids[k] is None:                 # a line an earlier edit added: no id yet
                    continue
                nl = line_map.get(b[0])
                if nl is None:
                    gone.append(old_ids[k])
                else:
                    by_new_line[nl] = old_ids[k]
            ids = [by_new_line.pop(b[0], None) for b in info['boxes']]
            gone.extend(by_new_line.values())          # lines that no longer parse as boxes
            for sid, b in zip(ids, info['boxes']):
                if sid is None:
                    continue
                smp = st.samples[sid]
                smp['box_idx'] = b[0]
                if int(st.labels[sid]) != b[1]:
                    cls_before[sid] = int(st.labels[sid])
                    self._set_cls(sid, b[1])
                    patch['cls'][sid] = b[1]
                bb = _px_box(b, smp['img_w'], smp['img_h'])
                if bb != list(smp['bbox']):
                    smp['bbox'] = bb
                    st.stale_ids = getattr(st, 'stale_ids', None) or set()
                    st.stale_ids.add(sid)
                    patch.setdefault('moved', []).append(sid)
            return None if any(i is None for i in ids) else ids

        self._rescan(name, fi['image'], data, carry)
        for sid in gone:
            deleted[sid] = True
            st.samples[sid]['box_idx'] = None              # no line in the file any more
            patch['deleted'].append(sid)
        if gone and fi['image'] in st.by_image:
            g = set(gone)
            st.by_image[fi['image']] = [i for i in st.by_image[fi['image']] if i not in g]
        return {str(k): v for k, v in cls_before.items()}, gone

    def _ids_by_line(self, image, boxes):
        """Box ids aligned with `boxes` from the live samples' box_idx, or None when they do not all fit."""
        st = self.state
        live = st.by_image.get(image) if image and st.by_image else None
        if not live:
            return None if boxes else []
        at = {b[0]: (k, b[1]) for k, b in enumerate(boxes)}
        ids = [None] * len(boxes)
        for sid in live:
            ln = st.samples[sid].get('box_idx')
            hit = at.get(ln)
            if hit is None or hit[1] != int(st.labels[sid]) or ids[hit[0]] is not None:
                return None
            ids[hit[0]] = sid
        return ids                      # None entries: lines an earlier edit added

    def _set_cls(self, sid, c):
        st = self.state
        st.labels[sid] = c
        s = st.samples[sid]
        s['cls'] = c
        s['cls_name'] = st.classes[c] if c < len(st.classes) else f'class_{c}'
        self.patch_cls[sid] = c

    def _rescan(self, name, image, data, ids_fn):
        """_scan_file + the box rules for one file (after a fix / undo); ids_fn(info) gives its box ids."""
        info, items = self._scan_file(name, image, data)
        info['ids'] = ids_fn(info)
        if info['ids']:                                 # line numbers move when lines are removed / put back
            samples = self.state.samples
            for sid, b in zip(info['ids'], info['boxes']):
                samples[sid]['box_idx'] = b[0]
        fitems = {name: items}
        self._check_boxes({name: info}, fitems, [name])
        self.files[name] = info
        self.file_items[name] = fitems[name]
        self._flat = None
        return info

    def _prune(self):
        d = self.backup_dir
        if not d.is_dir():
            return
        dirs = sorted(p for p in d.iterdir() if p.is_dir())
        for p in dirs[:max(0, len(dirs) - int(self.cfg['keep_backups']))]:
            shutil.rmtree(p, ignore_errors=True)

    def history(self):
        d = self.backup_dir
        out = []
        if not d.is_dir():
            return out
        for p in sorted((p for p in d.iterdir() if p.is_dir()), reverse=True):
            try:
                m = json.loads((p / 'meta.json').read_text())
            except (OSError, ValueError):
                continue
            out.append({'undo_id': m['undo_id'], 'time': m['time'], 'summary': m.get('summary', ''),
                        'applied': m.get('applied', 0), 'files': len(m.get('files', [])),
                        'current': m.get('fingerprint') == self._fp})
        return out

    def n_fixes(self):
        return sum(h['applied'] for h in self.history() if h['current'])

    def undo(self, undo_id):
        """Write the backed-up labels of one fix batch back. A labeling source gets its store rows back (soft-deleted
        shapes revived with their ids, attributes, source and score; edited ones as they were) instead of the text.
        A file that conflicts or fails stays in the backup (a retry undoes only those); `stale` = the fix was made
        before the last re-index (the files are restored, the open index does not show those boxes)."""
        if not re.fullmatch(r'[0-9]{13}-[0-9a-f]{6}', str(undo_id)):
            raise KeyError(undo_id)
        src = self.state.source
        with self.fix_lock:
            with self.lock:
                if self.scanning:
                    raise RuntimeError('a scan is running: try again when it finished')
                deleted = self._deleted()
            bdir = self.backup_dir / undo_id
            if not (bdir / 'meta.json').exists():
                raise KeyError(undo_id)
            meta = json.loads((bdir / 'meta.json').read_text())
            same = meta.get('fingerprint') == self._fp
            patch = {'cls': {}, 'restored': []}
            conflicts, errors, left, restored = [], [], [], 0
            rows_api = hasattr(src, 'restore_rows')
            for rec in reversed(meta['files']):
                shown = rec['image'] or rec['label']
                try:
                    before = (bdir / rec['bin']).read_bytes()
                    if rows_api and rec.get('rows') is not None:
                        src.restore_rows(rec['label'], rec['rows'], rec['after_sha1'])
                    else:
                        src.write_label(rec['label'], before if src.kind in ('local', 's3') else _decode(before),
                                        rec['after_sha1'])
                except Conflict:
                    conflicts.append(shown)
                    left.append(rec)
                    continue
                except Exception as e:  # noqa: BLE001 - OSError, S3 / store error, a class deleted since: this file only
                    errors.append({'file': shown, 'error': str(e) or type(e).__name__})
                    left.append(rec)
                    continue
                restored += 1
                with self.lock:
                    if not same:          # fixed before the last re-index: the index boxes are other ones now
                        if self.ready:
                            self._rescan(rec['label'], rec['image'], before,
                                         lambda info, img=rec['image']: self._box_ids(img, info['boxes']))
                        continue
                    st = self.state
                    for sid, c in rec.get('cls_before', {}).items():
                        self._set_cls(int(sid), c)
                        patch['cls'][int(sid)] = c
                    for sid in rec.get('deleted', []):
                        deleted[sid] = False
                        patch['restored'].append(sid)
                    if rec.get('deleted') and rec['image']:
                        ids = set(st.by_image.get(rec['image'], [])) | set(rec['deleted'])
                        st.by_image[rec['image']] = sorted(ids)
                    for sid, (ln, bb) in rec.get('idx_before', {}).items():
                        smp = st.samples[int(sid)]
                        smp['box_idx'] = ln
                        if list(smp['bbox']) != bb:
                            smp['bbox'] = bb
                            patch.setdefault('moved', []).append(int(sid))
                        if getattr(st, 'stale_ids', None):
                            st.stale_ids.discard(int(sid))
                    if self.ready:
                        self._rescan(rec['label'], rec['image'], before, lambda info, ids=rec['ids_before']: ids)
                    elif rec.get('ids_before'):
                        for sid, b in zip(rec['ids_before'], parse_label(_decode(before))[0]):
                            st.samples[sid]['box_idx'] = b[0]
            if not left:
                shutil.rmtree(bdir, ignore_errors=True)
            elif restored:                # keep only what is still to undo
                left.reverse()
                meta.update(files=left, applied=sum(r.get('n_ops', 0) for r in left))
                (bdir / 'meta.json').write_text(json.dumps(meta))
            if restored:
                with self.lock:
                    self._flat = None
                    if self.ready:
                        self._derive()
                    self._invalidate()
            return {'restored': restored, 'conflicts': conflicts, 'errors': errors, 'patch': patch, 'stale': not same}

    def state_patch(self):
        """Every in-memory change since the index was built: {'cls': {id: c}, 'deleted': [ids]}."""
        d = getattr(self.state, 'deleted', None)
        return {'cls': {int(k): int(v) for k, v in self.patch_cls.items()},
                'deleted': np.nonzero(d)[0].tolist() if d is not None else []}

    # ------------------------------------------------------------ cache
    def _save(self):
        try:
            data = {'fingerprint': self._fp, 'scanned_at': self.scanned_at, 'scan_s': self.scan_s,
                    'n_classes': self.n_classes, 'class_names': self.class_names,
                    'files': {k: {**v, 'boxes': [list(b) for b in v['boxes']]} for k, v in self.files.items()},
                    'file_items': self.file_items, 'image_items': self.image_items, 'splits': self.splits}
            tmp = self.cache_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data))
            tmp.replace(self.cache_path)
        except (OSError, TypeError, ValueError):
            pass

    def load_cached(self):
        """Reuse the last scan of this exact index (files unchanged since): True when loaded."""
        p = self.cache_path
        if self.ready or not p.exists():
            return self.ready
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            return False
        if data.get('fingerprint') != self._fp:
            return False
        with self.lock:
            self.files = {k: {**v, 'boxes': [tuple(b) for b in v['boxes']],
                              'wh': tuple(v['wh']) if v.get('wh') else None} for k, v in data['files'].items()}
            self.file_items, self.image_items = data['file_items'], data['image_items']
            self.n_classes, self.class_names = data['n_classes'], data['class_names']
            self.scanned_at, self.scan_s = data['scanned_at'], data['scan_s']
            self.splits = data.get('splits') or {}
            self.scores = self._pick_scores(None)
            self._derive()
            self.ready = True
        return True


def model_issues(svc, state, m, conf=None, match_iou=None, min_score=0.5, limit=5000):
    """(model name, label_issues, {pid: normalized box}) for the model rules; None when not predicted."""
    from ..models import evaluate
    got = svc.score(state, m, conf, match_iou, 'all')
    if got is None:
        return None
    preds, d, unmatched, split, avail, ap, out, summ, conf, match_iou = got
    issues = evaluate.label_issues(d, out, min_score=max(min_score, conf), limit=limit)
    sizes = {}
    for s in state.samples:
        sizes.setdefault(s['image'], (s['img_w'], s['img_h']))
    boxes = {}
    for it in issues:
        if it['kind'] == 'fp_bg':
            wh = sizes.get(it['image'])
            if wh:
                x1, y1, x2, y2 = (float(v) for v in d.p_box[it['pid']])
                boxes[it['pid']] = [round(x1 / wh[0], 5), round(y1 / wh[1], 5), round(x2 / wh[0], 5), round(y2 / wh[1], 5)]
    od = None
    try:
        got = svc.cleanlab(state, m)
    except Exception:  # noqa: BLE001 - cleanlab missing or failed: the other model rules still run
        got = None
    if got is not None:
        per, summary = got
        over = []
        for it in summary['overlooked'][:limit]:
            wh, pid = sizes.get(it['image']), it['pid']
            x1, y1, x2, y2 = (float(v) for v in d.p_box[pid])
            over.append({**it, 'cls': int(d.p_cls[pid]), 'score': float(d.p_score[pid]),
                         'box': [round(x1 / wh[0], 5), round(y1 / wh[1], 5), round(x2 / wh[0], 5), round(y2 / wh[1], 5)]
                         if wh else None})
        od = (per, over)
    return m.get('name') or m.get('id'), issues, boxes, od


def thumbnail(source, name, max_w=360):
    """JPEG bytes of one image scaled to max_w (issue cards)."""
    from PIL import ImageOps
    with source.open_image(name) as im:
        im = ImageOps.exif_transpose(im) if getattr(source, 'kind', '') == 'labeling' else im
        im = im.convert('RGB')
        im.thumbnail((max_w, max_w * 4), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=82)
        return buf.getvalue()
