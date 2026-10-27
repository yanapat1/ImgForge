"""
Dataset format detection: what a folder holds (YOLO detect / seg / pose / OBB, COCO, Pascal VOC, classification
folders, plain images, embeddings, empty, other), with the checks that decided it, a confidence and the class
count. Pure: it reads through a small tree interface (LocalTree = os.scandir, KeyTree = an S3 key listing), so
the same rules run on a local folder and on an S3 prefix, and the tests need nothing but temp folders.

    detect(tree, rel='')      -> {format, label, confidence, checks: [{name, ok, detail, count}], classes, reason}
    tree_stats(tree, rel='')  -> {images, labels, size}            (Bucket.stats; walks the tree once)
    fingerprint(abs_path)     -> sha1 of the top two levels' names, counts and mtimes (the cache key)

Why sampling: label files are only opened for at most SAMPLE of them (their column counts decide detect / seg /
pose / OBB), and COCO json is only parsed whole below COCO_LOAD_MAX, so a 100k-image folder is detected from a
listing plus a few dozen small reads.
"""

import hashlib
import json
import os
import re
from collections import Counter

import yaml

from ..data import sources
from .store import IMAGE_EXTS, SPLITS, join

LABELS = {'yolo-detect': 'YOLO detect', 'yolo-seg': 'YOLO segment', 'yolo-pose': 'YOLO pose',
          'yolo-obb': 'YOLO OBB', 'coco': 'COCO', 'voc': 'Pascal VOC', 'classification': 'Classification folders',
          'images': 'Images', 'embeddings': 'Embeddings', 'other': 'Other', 'empty': 'Empty'}
FORMATS = tuple(LABELS)
SAMPLE = 50                        # label files whose column counts are read
WALK_CAP = 1_000_000               # entries walked when counting
IMAGES_CAP = 20_000                # entries looked at for "only images"
COCO_LOAD_MAX = 30 << 20           # COCO json parsed in full up to this size, else its head is sniffed
HEAD = 64 << 10
JUNK = {'thumbs.db', 'desktop.ini', 'readme', 'readme.md', 'readme.txt', 'license', 'license.txt', 'license.md'}


def _img(name):
    return name.lower().endswith(IMAGE_EXTS)


# ---------------------------------------------------------------------------------------------- trees
class LocalTree:
    """A local folder: scan() = one os.scandir (names + dir bit from the dirent, no stat)."""

    def __init__(self, root):
        self.root = os.path.realpath(str(root))

    def abs(self, rel):
        return os.path.join(self.root, rel) if rel else self.root

    def scan(self, rel=''):
        try:
            with os.scandir(self.abs(rel)) as it:
                return [(e.name, e.is_dir()) for e in it if not e.name.startswith('.')]
        except OSError:
            return []

    def read(self, rel, n=HEAD):
        try:
            with open(self.abs(rel), 'rb') as f:
                return f.read(n) if n else f.read()
        except OSError:
            return b''

    def size(self, rel):
        try:
            return os.path.getsize(self.abs(rel))
        except OSError:
            return 0

    def walk(self, rel='', cap=WALK_CAP, sizes=False):
        """(rel path, size or None) of every file below rel."""
        stack, n = [(rel, self.abs(rel))], 0
        while stack:
            r, d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    if e.name.startswith('.'):
                        continue
                    n += 1
                    if n > cap:
                        return
                    try:
                        isdir = e.is_dir()
                    except OSError:
                        continue
                    if isdir:
                        stack.append((join(r, e.name), e.path))
                    else:
                        sz = None
                        if sizes:
                            try:
                                sz = e.stat().st_size
                            except OSError:
                                sz = 0
                        yield join(r, e.name), sz


class KeyTree:
    """An object listing {rel key: size} seen as folders (S3); `reader(rel, n)` reads the head of one object."""

    def __init__(self, keys, reader=None):
        self.keys = keys
        self.reader = reader
        self.kids = {}
        for k in keys:
            parts = k.split('/')
            for i in range(len(parts)):
                d = self.kids.setdefault('/'.join(parts[:i]), {})
                d[parts[i]] = d.get(parts[i], False) or i < len(parts) - 1

    def scan(self, rel=''):
        return [(n, d) for n, d in self.kids.get(rel, {}).items() if not n.startswith('.')]

    def read(self, rel, n=HEAD):
        return self.reader(rel, n) if self.reader else b''

    def size(self, rel):
        return self.keys.get(rel, 0)

    def walk(self, rel='', cap=WALK_CAP, sizes=False):
        pre = rel + '/' if rel else ''
        for i, (k, s) in enumerate(self.keys.items()):
            if i >= cap:
                return
            if k.startswith(pre):
                yield k, s


# ---------------------------------------------------------------------------------------------- helpers
def _check(name, ok, detail=None, count=None, warn=False):
    out = {'name': name, 'ok': bool(ok), 'detail': detail, 'count': count}
    if warn:
        out['warn'] = True
    return out


def _result(fmt, confidence, checks, reason, classes=None, **extra):
    return {'format': fmt, 'label': LABELS[fmt], 'confidence': round(float(confidence), 2), 'checks': checks,
            'classes': classes, 'reason': reason, **extra}


def _split_names(names):
    return [n for n in names if n.lower() in SPLITS]


def _yaml_info(tree, rel, files):
    """(class names, kpt_shape, yaml file name) from data.yaml (or another yaml with names:) / classes.txt."""
    names, kpt, src = [], None, None
    ys = sorted((f for f in files if f.lower().endswith(('.yaml', '.yml'))),
                key=lambda f: (f.lower() not in ('data.yaml', 'dataset.yaml'), f))
    for f in ys[:5]:
        try:
            data = yaml.safe_load(tree.read(join(rel, f), 1 << 20).decode('utf-8', 'replace')) or {}
        except (yaml.YAMLError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        n = data.get('names')
        if isinstance(n, dict):
            names = [str(n[k]) for k in sorted(n, key=lambda x: int(x) if str(x).isdigit() else 0)]
        elif isinstance(n, list):
            names = [str(x) for x in n]
        if data.get('kpt_shape'):
            kpt = data.get('kpt_shape')
        if names or kpt:
            src = f
            break
    if not names:
        for f in files:
            if f.lower() == 'classes.txt':
                names = sources.parse_classes(classes_txt=tree.read(join(rel, f), 1 << 20).decode('utf-8', 'replace'))
                src = f if names else src
                break
    return names, kpt, src


def _stem_map(tree, rel, want):
    """{rel path without extension below rel: count} of files matching `want` (images or .txt labels)."""
    out = set()
    n = 0
    pre = len(rel) + 1 if rel else 0
    for p, _ in tree.walk(rel):
        if want(p):
            out.add(os.path.splitext(p[pre:])[0])
            n += 1
    return out, n


# ---------------------------------------------------------------------------------------------- YOLO
def _yolo_layouts(tree, rel, top_dirs):
    """[(split or '', images rel, labels rel)] for images/ + labels/ (images/train … inside) or train/images …"""
    low = {d.lower(): d for d in top_dirs}
    out = []
    if 'images' in low and 'labels' in low:
        out.append(('', join(rel, low['images']), join(rel, low['labels'])))
    for s in _split_names(top_dirs):
        sub = {n.lower(): n for n, d in tree.scan(join(rel, s)) if d}
        if 'images' in sub and 'labels' in sub:
            out.append((s, join(rel, s, ) + '/' + sub['images'], join(rel, s) + '/' + sub['labels']))
    return out


def _yolo(tree, rel, top_dirs, top_files):
    layouts = _yolo_layouts(tree, rel, top_dirs)
    if not layouts:
        return None
    n_img = n_lab = matched = 0
    split_counts = {}
    samples = []
    for split, ir, lr in layouts:
        imgs, ni = _stem_map(tree, ir, _img)
        labs, nl = _stem_map(tree, lr, lambda p: p.endswith('.txt') and os.path.basename(p).lower() != 'classes.txt')
        n_img, n_lab, matched = n_img + ni, n_lab + nl, matched + len(imgs & labs)
        if split:
            split_counts[split] = ni
        else:
            for s in _split_names([n for n, d in tree.scan(ir) if d]):
                split_counts[s] = sum(1 for p, _ in tree.walk(join(ir, s)) if _img(p))
        for stem in sorted(labs)[:SAMPLE - len(samples)]:
            samples.append(join(lr, stem + '.txt'))
    names, kpt, ysrc = _yaml_info(tree, rel, top_files)
    cols, max_id = Counter(), -1
    for p in samples:
        for line in tree.read(p, 16 << 10).decode('utf-8', 'replace').splitlines()[:200]:
            parts = line.split()
            if not parts:
                continue
            cols[len(parts)] += 1
            try:
                max_id = max(max_id, int(float(parts[0])))
            except ValueError:
                pass
    counts = set(cols)
    conf = 0.98
    if kpt:
        fmt, why = 'yolo-pose', f'data.yaml kpt_shape {kpt}'
    elif not counts:
        fmt, why, conf = 'yolo-detect', 'no label lines yet: assumed detect', 0.6
    elif counts <= {5, 6}:
        fmt, why = 'yolo-detect', '5-column label lines'
    elif counts == {9}:
        fmt, why = 'yolo-obb', '9-column label lines (4 corner points)'
        conf = 0.85
    elif len(counts) == 1 and min(counts) >= 11 and (min(counts) - 5) % 3 in (0, 2) and len(samples) > 1:
        fmt, why, conf = 'yolo-pose', f'{min(counts)}-column label lines of one length (no kpt_shape)', 0.6
    elif all(c % 2 == 1 and c >= 7 for c in counts):
        fmt, why = 'yolo-seg', 'polygon label lines (odd column counts > 5)'
    else:
        top = cols.most_common(1)[0][0]
        fmt = 'yolo-detect' if top == 5 else 'yolo-seg'
        why, conf = f'mixed label line lengths ({", ".join(map(str, sorted(counts)))})', 0.55
    ratio = matched / n_img if n_img else 0
    if n_img and ratio < 0.9:
        conf -= 0.2
    if not names:
        conf -= 0.1
    if not n_img:
        conf -= 0.3
    splits = ' · '.join(split_counts) or None
    classes = len(names) if names else (max_id + 1 if max_id >= 0 else None)
    checks = [
        _check('images/', n_img > 0, splits, n_img),
        _check('labels/', n_lab > 0, None, n_lab, warn=n_lab == 0),
        _check('mirror', ratio >= 0.9, f'{matched} of {n_img} images have a label file', matched, warn=ratio < 0.9),
        _check(ysrc or 'data.yaml', bool(names), f'{len(names)} classes' if names else 'no class names',
               len(names) or None, warn=not names),
        _check('label lines', bool(counts), ', '.join(f'{c} columns' for c in sorted(counts)) or 'none sampled',
               sum(cols.values())),
    ]
    reason = 'images/ + labels/ mirror' + (', ' + (ysrc or 'data.yaml') + ' resolves' if names else '') + ', ' + why
    return _result(fmt, max(0.3, conf), checks, reason, classes, splits=split_counts)


# ---------------------------------------------------------------------------------------------- COCO / VOC
def _coco(tree, rel, top_dirs, top_files):
    cands = [join(rel, f) for f in top_files if f.lower().endswith('.json')]
    ann = next((d for d in top_dirs if d.lower() == 'annotations'), None)
    if ann:
        cands += [join(join(rel, ann), n) for n, d in tree.scan(join(rel, ann)) if not d and n.lower().endswith('.json')]
    n_img = n_ann = 0
    cats, found, sniffed = set(), [], False
    for p in cands[:10]:
        size = tree.size(p)
        if size and size <= COCO_LOAD_MAX:
            try:
                data = json.loads(tree.read(p, 0 if isinstance(tree, LocalTree) else size + 1))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(data, dict) and isinstance(data.get('images'), list) and \
                    isinstance(data.get('annotations'), list):
                found.append(p)
                n_img += len(data['images'])
                n_ann += len(data['annotations'])
                cats |= {str(c.get('name')) for c in data.get('categories') or [] if isinstance(c, dict)}
        else:
            head = tree.read(p, HEAD).decode('utf-8', 'replace')
            if '"images"' in head and ('"annotations"' in head or '"categories"' in head):
                found.append(p)
                sniffed = True
    if not found:
        return None
    files = ', '.join(os.path.basename(p) for p in found[:4])
    checks = [_check('annotation json', True, files, len(found)),
              _check('images', n_img > 0 or sniffed, None if sniffed else f'{n_img} listed', n_img or None),
              _check('annotations', n_ann > 0 or sniffed, None, n_ann or None),
              _check('categories', bool(cats) or sniffed, None, len(cats) or None)]
    return _result('coco', 0.8 if sniffed else 0.95, checks,
                   f'{files}: COCO json with images + annotations' + (' (large file: head sniffed)' if sniffed else ''),
                   len(cats) or None)


_NAME_RE = re.compile(r'<object>.*?<name>\s*([^<]+?)\s*</name>', re.S)


def _voc(tree, rel, top_dirs, top_files):
    ann = next((d for d in top_dirs if d.lower() == 'annotations'), None)
    base = join(rel, ann) if ann else rel
    xmls = [join(base, n) for n, d in (tree.scan(base) if ann else [(f, False) for f in top_files])
            if not d and n.lower().endswith('.xml')]
    if not xmls:
        return None
    ok, names = 0, set()
    sample = xmls[:20]
    for p in sample:
        head = tree.read(p, HEAD).decode('utf-8', 'replace')
        if '<annotation' in head and '<object' in head:
            ok += 1
            names |= set(_NAME_RE.findall(head))
    if ok * 2 < len(sample):
        return None
    has_imgs = any(d.lower() == 'jpegimages' for d in top_dirs)
    checks = [_check('Annotations/' if ann else '*.xml', True, f'{ok} of {len(sample)} sampled are VOC', len(xmls)),
              _check('JPEGImages/', has_imgs, None, None, warn=not has_imgs),
              _check('classes', bool(names), ', '.join(sorted(names)[:8]) or None, len(names) or None)]
    return _result('voc', 0.95 if has_imgs else 0.85, checks,
                   f'{"Annotations/" if ann else ""}*.xml with <annotation><object>', len(names) or None)


# ---------------------------------------------------------------------------------------------- folders
def _class_dirs(tree, rel, dirs):
    """The sub-folders of rel that look like one class each: >= 1 image, >= 80 % of their files images."""
    good = []
    for d in dirs:
        kids = tree.scan(join(rel, d))
        files = [n for n, isd in kids if not isd and n.lower() not in JUNK]
        n_img = sum(1 for n in files if _img(n))
        if n_img and n_img >= 0.8 * len(files):
            good.append((d, n_img))
    return good


def _classification(tree, rel, top_dirs, top_files):
    low = {d.lower() for d in top_dirs}
    if 'labels' in low or 'images' in low:
        return None
    splits = _split_names(top_dirs)
    if splits:
        classes, per_split, n = set(), {}, 0
        for s in splits:
            kids = [k for k, isd in tree.scan(join(rel, s)) if isd]
            good = _class_dirs(tree, join(rel, s), kids)
            if len(good) < 2 or len(good) < 0.8 * len(kids):
                return None
            classes |= {g for g, _ in good}
            per_split[s] = sum(c for _, c in good)
            n += per_split[s]
        checks = [_check('splits', True, ' · '.join(splits), len(splits)),
                  _check('class folders', True, ', '.join(sorted(classes)[:8]), len(classes)),
                  _check('images', n > 0, None, n)]
        return _result('classification', 0.9, checks, f'{" / ".join(splits)} each hold one folder of images per class',
                       len(classes), splits=per_split)
    if len(top_dirs) < 2:
        return None
    good = _class_dirs(tree, rel, top_dirs)
    if len(good) < 2 or len(good) < 0.8 * len(top_dirs):
        return None
    n = sum(c for _, c in good)
    checks = [_check('class folders', True, ', '.join(sorted(g for g, _ in good)[:8]), len(good)),
              _check('images', n > 0, None, n)]
    return _result('classification', 0.85, checks, f'{len(good)} sub-folders of images, one per class', len(good))


def _embeddings(tree, rel, top_files):
    low = {f.lower() for f in top_files}
    npy = [f for f in top_files if f.lower().endswith('.npy')]
    meta = 'meta.csv' if 'meta.csv' in low else 'meta.parquet' if 'meta.parquet' in low else None
    if not meta or not npy:
        return None
    checks = [_check(meta, True, None, None), _check('.npy vectors', True, ', '.join(npy[:4]), len(npy))]
    return _result('embeddings', 0.95, checks, f'{meta} next to {len(npy)} .npy file{"s" if len(npy) > 1 else ""}')


def _images(tree, rel):
    n_img = n_other = 0
    other = None
    for i, (p, _) in enumerate(tree.walk(rel, cap=IMAGES_CAP)):
        name = os.path.basename(p).lower()
        if _img(name):
            n_img += 1
        elif name not in JUNK and not name.endswith('.md'):
            n_other += 1
            other = other or p
            break
    if not n_img or n_other:
        return None, other
    return _result('images', 0.9, [_check('images', True, None, n_img)], 'only image files, no labels'), None


# ---------------------------------------------------------------------------------------------- entry points
def detect(tree, rel=''):
    """The format of folder `rel` of `tree` (a LocalTree / KeyTree)."""
    top = tree.scan(rel)
    if not top:
        return _result('empty', 1.0, [_check('entries', True, None, 0)], 'the folder is empty')
    dirs = [n for n, d in top if d]
    files = [n for n, d in top if not d]
    for fn in (lambda: _embeddings(tree, rel, files), lambda: _yolo(tree, rel, dirs, files),
               lambda: _coco(tree, rel, dirs, files), lambda: _voc(tree, rel, dirs, files),
               lambda: _classification(tree, rel, dirs, files)):
        r = fn()
        if r is not None:
            return r
    r, other = _images(tree, rel)
    if r is not None:
        return r
    low = {d.lower() for d in dirs}
    if 'images' in low and 'labels' not in low:
        reason = 'images/ without a labels/ folder: labels/ missing'
    elif 'labels' in low and 'images' not in low:
        reason = 'labels/ without an images/ folder: images/ missing'
    else:
        reason = f'{len(dirs)} folders, {len(files)} files: no known dataset layout' + (f' ({other})' if other else '')
    checks = [_check('images/ + labels/', False, None, None), _check('entries', True, None, len(top))]
    return _result('other', 0.5, checks, reason)


def tree_stats(tree, rel='', cap=WALK_CAP):
    """{images, labels, size} below rel (labels = .txt files under a labels/ folder)."""
    n_img = n_lab = size = 0
    for p, s in tree.walk(rel, cap=cap, sizes=True):
        size += s or 0
        low = p.lower()
        if low.endswith(IMAGE_EXTS):
            n_img += 1
        elif low.endswith('.txt') and '/labels/' in '/' + low and not low.endswith('classes.txt'):
            n_lab += 1
    return {'images': n_img, 'labels': n_lab, 'size': size}


def fingerprint(path):
    """sha1 of the folder's own mtime, its entries (file names; folders with their mtime) and, one level down,
    each folder's entry count and its sub-folders' mtimes: changes whenever an entry of the top two levels is added,
    removed or renamed. Files are not stat'ed (a flat 20k-image folder costs one scandir)."""
    h = hashlib.sha1()
    try:
        h.update(str(os.stat(path).st_mtime_ns).encode())
        with os.scandir(path) as it:
            top = sorted((e for e in it if not e.name.startswith('.')), key=lambda e: e.name)
    except OSError:
        return 'missing'
    for e in top:
        if not e.is_dir():                              # files: the name (the folder's mtime covers add / remove)
            h.update(f'f|{e.name}'.encode())
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        h.update(f'd|{e.name}|{st.st_mtime_ns}'.encode())
        n = 0
        try:
            with os.scandir(e.path) as it2:
                for e2 in it2:
                    n += 1
                    if e2.is_dir():
                        try:
                            h.update(f'{e2.name}|{e2.stat().st_mtime_ns}'.encode())
                        except OSError:
                            pass
        except OSError:
            pass
        h.update(f'#{n}'.encode())
    return h.hexdigest()
