"""
YOLO label writers (detect / seg / pose / cls), classes.txt and data.yaml: pure functions over annotation
rows (dicts with class_id, geometry_type, geometry, id, parent_id), so they are unit-tested exactly.

Coordinates are already normalised 0..1 of the EXIF-oriented image, so nothing here needs the pixel size.
The class id written is the class's index among the task's non-deleted classes in position order, which is
also its line in classes.txt and its key in data.yaml `names`.
"""

import re
import unicodedata

import yaml

FMT = '{:.6f}'


def _f(v):
    return FMT.format(min(1.0, max(0.0, float(v))))


def _lines(lines):
    return ''.join(line + '\n' for line in lines)


def yolo_detect(rows, class_index):
    """`cls cx cy w h` per bbox row."""
    out = []
    for r in rows:
        if r['geometry_type'] != 'bbox' or r.get('class_id') not in class_index:
            continue
        g = r['geometry']
        out.append(f"{class_index[r['class_id']]} {_f(g['x'] + g['w'] / 2)} {_f(g['y'] + g['h'] / 2)} "
                   f"{_f(g['w'])} {_f(g['h'])}")
    return _lines(out)


def yolo_seg(rows, class_index, warnings=None):
    """`cls x1 y1 x2 y2 ...` per polygon row. Holes and RLE masks have no YOLO form: dropped, counted."""
    out = []
    for r in rows:
        if r.get('class_id') not in class_index:
            continue
        if r['geometry_type'] != 'polygon':
            if warnings is not None and r['geometry_type'] == 'mask_rle':
                warnings['mask_rle_dropped'] = warnings.get('mask_rle_dropped', 0) + 1
            continue
        g = r['geometry']
        if g.get('holes') and warnings is not None:
            warnings['holes_dropped'] = warnings.get('holes_dropped', 0) + len(g['holes'])
        out.append(f"{class_index[r['class_id']]} " + ' '.join(f'{_f(x)} {_f(y)}' for x, y, *_ in g['points']))
    return _lines(out)


def _kpt_box(points, pad=0.02):
    vis = [(x, y) for x, y, v in points if v > 0] or [(x, y) for x, y, _ in points]
    if not vis:
        return 0.5, 0.5, 0.0, 0.0
    xs, ys = [p[0] for p in vis], [p[1] for p in vis]
    x0, x1 = max(0.0, min(xs) - pad), min(1.0, max(xs) + pad)
    y0, y1 = max(0.0, min(ys) - pad), min(1.0, max(ys) + pad)
    return (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0


def yolo_pose(rows, class_index, n_kpt):
    """`cls cx cy w h x1 y1 v1 ...` per keypoints row. The box is the linked parent bbox when there is one,
    else the labeled points' extent + 2 % padding. Unlabeled points (v=0) are written as `0 0 0`."""
    boxes = {r['id']: r['geometry'] for r in rows if r['geometry_type'] == 'bbox'}
    out = []
    for r in rows:
        if r['geometry_type'] != 'keypoints' or r.get('class_id') not in class_index:
            continue
        pts = [(float(p[0]), float(p[1]), int(p[2]) if len(p) > 2 else 2) for p in r['geometry']['points']]
        pts = (pts + [(0.0, 0.0, 0)] * n_kpt)[:n_kpt]
        b = boxes.get(r.get('parent_id'))
        cx, cy, w, h = ((b['x'] + b['w'] / 2, b['y'] + b['h'] / 2, b['w'], b['h']) if b else _kpt_box(pts))
        kp = ' '.join(f'{_f(x)} {_f(y)} {v}' if v > 0 else '0.000000 0.000000 0' for x, y, v in pts)
        out.append(f"{class_index[r['class_id']]} {_f(cx)} {_f(cy)} {_f(w)} {_f(h)} {kp}")
    return _lines(out)


def yolo_cls(rows, class_index):
    """One class id per line (multi-label tasks write several lines), sorted and unique."""
    ids = sorted({class_index[r['class_id']] for r in rows
                  if r['geometry_type'] == 'tag' and r.get('class_id') in class_index})
    return _lines(str(i) for i in ids)


def label_text(task, rows, class_index, warnings=None):
    """The .txt content of one image for one task (task = {task_type, config})."""
    t = task['task_type']
    if t == 'object_detection':
        return yolo_detect(rows, class_index)
    if t == 'segmentation':
        return yolo_seg(rows, class_index, warnings)
    if t == 'keypoints':
        return yolo_pose(rows, class_index, len((task.get('config') or {}).get('keypoints') or []))
    if t == 'classification':
        return yolo_cls(rows, class_index)
    raise ValueError(f'no YOLO format for task type {t!r}')


def class_map(classes, keep=None):
    """{class_id: yolo id} from the task's classes (deleted ones are skipped; order = position).
    keep: the class ids to export (None = all); the kept ones are renumbered 0..N-1 in position order, so an
    export without some classes still has dense ids that match its classes.txt / data.yaml."""
    live = sorted((c for c in classes if not c.get('is_deleted')), key=lambda c: (c.get('position', 0), c['name']))
    if keep is not None:
        keep = set(keep)
        live = [c for c in live if c['id'] in keep]
    return {c['id']: i for i, c in enumerate(live)}, [c['name'] for c in live]


def classes_txt(names):
    return _lines(names)


def data_yaml(task, names):
    """ultralytics-style data.yaml for one task folder; the images are shared one level up in ../images."""
    d = {'path': '.', 'train': '../images', 'val': '../images', 'nc': len(names),
         'names': {i: n for i, n in enumerate(names)}}
    if task['task_type'] == 'keypoints':
        cfg = task.get('config') or {}
        k = len(cfg.get('keypoints') or [])
        d['kpt_shape'] = [k, 3]
        flip = cfg.get('flip_idx')
        d['flip_idx'] = list(flip) if flip and len(flip) == k else list(range(k))
        if cfg.get('keypoints'):
            d['kpt_names'] = {0: list(cfg['keypoints'])}
    head = (f"# {task.get('name', '')} ({task['task_type']})\n"
            '# labels are in ./labels; ultralytics finds labels by swapping /images/ for /labels/ in each image\n'
            '# path, so to train, link ../images next to ./labels (or copy) and set train/val to that folder.\n')
    return head + yaml.safe_dump(d, sort_keys=False, allow_unicode=True, default_flow_style=None)


def slugify(name, taken=()):
    """Folder name for a task: ascii, lower-case, dashes; unique among `taken`."""
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-').lower() or 'task'
    out, i = s, 2
    while out in taken:
        out, i = f'{s}-{i}', i + 1
    return out


# ------------------------------------------------------------ parsers (round-trip tests, future import)
def parse_lines(text):
    return [line.split() for line in text.splitlines() if line.strip()]


def parse_detect(text):
    return [(int(p[0]), *map(float, p[1:5])) for p in parse_lines(text)]


def parse_seg(text):
    return [(int(p[0]), [(float(p[i]), float(p[i + 1])) for i in range(1, len(p), 2)]) for p in parse_lines(text)]


def parse_pose(text, n_kpt):
    out = []
    for p in parse_lines(text):
        box = tuple(map(float, p[1:5]))
        kp = [(float(p[5 + 3 * i]), float(p[6 + 3 * i]), int(p[7 + 3 * i])) for i in range(n_kpt)]
        out.append((int(p[0]), box, kp))
    return out
