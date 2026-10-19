"""
Existing annotations into a labeling task: YOLO label folders, COCO JSON, CSV / TSV / text tables and databases.

Every parser yields Records, one per shape, before matching to the project's images:

    Record(key, cls, gtype, geom, px, score)
        key    the image the shape belongs to, as the source names it (a relative path, a file name or a stem)
        cls    class name (str) or YOLO class index (int, resolved through the source's class names)
        gtype  bbox | polygon | keypoints | tag  (store geometry types)
        geom   store geometry: bbox {x, y, w, h} top-left, polygon {points}, keypoints {points [x, y, v]}, tag {}
        px     True when geom is in pixels of the image (divided by the media size when matched)

match() maps keys to media ids (exact relative path, then file name, then stem, case-insensitive) and
ProjectStore.import_annotations() stores them in one transaction per batch (source='import').

Sources of the files: the media sources (local folder, S3, FTP, SFTP) listing .txt / .json / .csv instead of images,
an uploaded file (a .json, .csv / .tsv / .txt, or a .zip of YOLO labels), or a database connection (the tables
db_export.py writes, or a SELECT the user writes). Nothing here writes anything.
"""

import csv
import io
import posixpath
import re
import zipfile
from collections import Counter
from dataclasses import dataclass

import orjson
import yaml

FORMATS = ('yolo', 'coco', 'text', 'db')
MAX_ERRORS = 50                      # bad lines / records reported back (the rest are only counted)
TEXT_EXTS = ('.txt',)
TABLE_EXTS = ('.csv', '.tsv', '.txt')
SQL_START = re.compile(r'^\s*(select|with)\b', re.I)
SQL_FORBIDDEN = re.compile(r'\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|exec|execute|call)\b', re.I)


@dataclass(slots=True)
class Record:
    key: str
    cls: object
    gtype: str
    geom: dict
    px: bool = False
    score: float | None = None


class Report:
    """Counts of what was skipped and why, plus the first few problems word for word."""

    def __init__(self):
        self.skipped = Counter()
        self.errors = []

    def skip(self, why, detail=None):
        self.skipped[why] += 1
        if detail and len(self.errors) < MAX_ERRORS:
            self.errors.append(detail)

    def to_dict(self):
        return {'skipped': dict(self.skipped), 'errors': self.errors}


# ---------------------------------------------------------------- YOLO txt
def _floats(parts):
    return [float(v) for v in parts]


def yolo_lines(key, text, task_type, n_kpt, rep, name=''):
    """Records of one YOLO label file: detect (cls cx cy w h), seg (cls x y x y ...), pose (cls box + x y v ...)."""
    for ln, line in enumerate(text.splitlines()):
        p = line.split()
        if not p:
            continue
        try:
            c = int(float(p[0]))
            v = _floats(p[1:])
        except ValueError:
            rep.skip('bad_line', f'{name}:{ln + 1}: not numbers')
            continue
        if c < 0:
            rep.skip('bad_line', f'{name}:{ln + 1}: negative class')
            continue
        if task_type == 'segmentation':
            if len(v) == 4:                          # a detect line in a seg task: its box as a polygon
                cx, cy, w, h = v
                v = [cx - w / 2, cy - h / 2, cx + w / 2, cy - h / 2, cx + w / 2, cy + h / 2, cx - w / 2, cy + h / 2]
            if len(v) < 6 or len(v) % 2:
                rep.skip('bad_line', f'{name}:{ln + 1}: a polygon needs 3+ x y pairs')
                continue
            yield Record(key, c, 'polygon', {'points': [[v[i], v[i + 1]] for i in range(0, len(v), 2)]})
        elif task_type == 'keypoints':
            if len(v) < 4:
                rep.skip('bad_line', f'{name}:{ln + 1}: a pose line starts with a box')
                continue
            kv = v[4:]
            step = 3 if n_kpt and len(kv) == 3 * n_kpt else 2 if n_kpt and len(kv) == 2 * n_kpt else 3
            pts = [[kv[i], kv[i + 1], int(kv[i + 2]) if step == 3 else 2] for i in range(0, len(kv) - step + 1, step)]
            if not pts:
                rep.skip('bad_line', f'{name}:{ln + 1}: no keypoints')
                continue
            yield Record(key, c, 'keypoints', {'points': pts})
        elif task_type == 'classification':
            yield Record(key, c, 'tag', {})
        else:
            if len(v) < 4:
                rep.skip('bad_line', f'{name}:{ln + 1}: {len(p)} values, need 5')
                continue
            if len(v) > 4 and len(v) % 2 == 0 and len(v) >= 6:   # a polygon line in a detect task: its extent
                xs, ys = v[0::2], v[1::2]
                cx, cy, w, h = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs), max(ys) - min(ys)
            else:
                cx, cy, w, h = v[:4]
            if w <= 0 or h <= 0:
                rep.skip('degenerate', f'{name}:{ln + 1}: zero size')
                continue
            yield Record(key, c, 'bbox', {'x': cx - w / 2, 'y': cy - h / 2, 'w': w, 'h': h})


def class_names_from(files):
    """YOLO class names from classes.txt / data.yaml / *.yaml among {rel: bytes} (None when there is none)."""
    by = {posixpath.basename(k).lower(): v for k, v in files.items()}
    if 'classes.txt' in by:
        return [ln.strip() for ln in by['classes.txt'].decode('utf-8', 'replace').splitlines() if ln.strip()]
    for k in ('data.yaml', 'dataset.yaml', 'data.yml'):
        if k in by:
            try:
                names = (yaml.safe_load(by[k]) or {}).get('names')
            except yaml.YAMLError:
                return None
            if isinstance(names, dict):
                return [str(names[i]) for i in sorted(names, key=lambda x: int(x))]
            if isinstance(names, list):
                return [str(n) for n in names]
    return None


def _label_key(rel):
    """`labels/train/a.txt` -> `train/a` (a stem with its sub-folder, matched against image paths without the extension)."""
    parts = rel.split('/')
    if 'labels' in parts:
        parts = parts[len(parts) - parts[::-1].index('labels'):]
    return posixpath.splitext('/'.join(parts))[0]


# ---------------------------------------------------------------- COCO json
def coco(data, task_type, rep):
    """(records, class names) of a COCO instances / keypoints file (pixel boxes, normalised by images[] sizes)."""
    try:
        d = orjson.loads(data)
    except orjson.JSONDecodeError as e:
        raise ValueError(f'not a JSON file: {e}') from None
    if not isinstance(d, dict) or 'annotations' not in d or 'images' not in d:
        raise ValueError('not a COCO file: it needs "images" and "annotations"')
    cats = {c['id']: str(c.get('name', c['id'])) for c in d.get('categories') or [] if 'id' in c}
    imgs = {}
    for im in d['images']:
        if 'id' in im and im.get('file_name'):
            imgs[im['id']] = (str(im['file_name']).replace('\\', '/'), im.get('width'), im.get('height'))
    out = []
    for a in d['annotations']:
        im = imgs.get(a.get('image_id'))
        if im is None:
            rep.skip('no_image', f'annotation {a.get("id")}: unknown image_id {a.get("image_id")}')
            continue
        key, W, H = im
        if a.get('category_id') not in cats:
            rep.skip('bad_class', f'annotation {a.get("id")}: unknown category_id {a.get("category_id")}')
            continue
        cls = cats[a['category_id']]
        norm = bool(W and H)
        sx, sy = (1.0 / W, 1.0 / H) if norm else (1.0, 1.0)
        score = a.get('score')
        if task_type == 'classification':
            out.append(Record(key, cls, 'tag', {}, score=score))
            continue
        if task_type == 'segmentation':
            seg = a.get('segmentation')
            if isinstance(seg, dict):                         # RLE: no polygon form
                rep.skip('rle')
                continue
            polys = [p for p in (seg or []) if isinstance(p, list) and len(p) >= 6]
            if not polys and a.get('bbox'):                   # no polygon: the box as one
                x, y, w, h = a['bbox']
                polys = [[x, y, x + w, y, x + w, y + h, x, y + h]]
            for p in polys:
                out.append(Record(key, cls, 'polygon', {'points': [[p[i] * sx, p[i + 1] * sy] for i in range(0, len(p) - 1, 2)]},
                                  px=not norm, score=score))
            continue
        if task_type == 'keypoints':
            kp = a.get('keypoints')
            if not kp:
                rep.skip('other_type')
                continue
            out.append(Record(key, cls, 'keypoints', {'points': [[kp[i] * sx, kp[i + 1] * sy, int(kp[i + 2])]
                                                                   for i in range(0, len(kp) - 2, 3)]}, px=not norm, score=score))
            continue
        b = a.get('bbox')
        if not b or len(b) != 4:
            rep.skip('other_type', f'annotation {a.get("id")}: no bbox')
            continue
        x, y, w, h = b
        if w <= 0 or h <= 0:
            rep.skip('degenerate', f'annotation {a.get("id")}: zero size')
            continue
        out.append(Record(key, cls, 'bbox', {'x': x * sx, 'y': y * sy, 'w': w * sx, 'h': h * sy}, px=not norm, score=score))
    return out, sorted(set(cats.values()), key=list(cats.values()).index)


# ---------------------------------------------------------------- text tables
_ALIASES = {
    'image': ('image', 'file', 'filename', 'file_name', 'image_name', 'img', 'path', 'name', 'image_path'),
    'cls': ('class', 'label', 'class_name', 'category', 'cls', 'class_id', 'category_id'),
    'x1': ('x1', 'xmin', 'left', 'x_min'), 'y1': ('y1', 'ymin', 'top', 'y_min'),
    'x2': ('x2', 'xmax', 'right', 'x_max'), 'y2': ('y2', 'ymax', 'bottom', 'y_max'),
    'cx': ('cx', 'x_center', 'xc', 'center_x'), 'cy': ('cy', 'y_center', 'yc', 'center_y'),
    'x': ('x',), 'y': ('y',), 'w': ('w', 'width', 'box_w'), 'h': ('h', 'height', 'box_h'),
    'score': ('score', 'conf', 'confidence'),
}


def _columns(header):
    low = [h.strip().lower() for h in header]
    out = {}
    for k, names in _ALIASES.items():
        for n in names:
            if n in low:
                out[k] = low.index(n)
                break
    return out


def table(data, task_type, rep, coords='auto'):
    """Records of a CSV / TSV table with a header (image, class, and x1 y1 x2 y2 | cx cy w h | x y w h), or of a
    headerless list (`image class x1 y1 x2 y2` or `image class` per line). coords: norm | px | auto (any value
    above 1.5 means pixels)."""
    text = data.decode('utf-8-sig', 'replace')
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith('#')]
    if not lines:
        return []
    dialect = '\t' if '\t' in lines[0] else ',' if ',' in lines[0] else ';' if ';' in lines[0] else None
    rows = list(csv.reader(lines, delimiter=dialect)) if dialect else [ln.split() for ln in lines]
    cols = _columns(rows[0])
    if 'image' in cols and 'cls' in cols:
        body = rows[1:]
    else:                                          # no header: image, class, then 4 numbers (or none)
        cols = {'image': 0, 'cls': 1, 'x1': 2, 'y1': 3, 'x2': 4, 'y2': 5}
        body = rows
    if 'x1' in cols and 'x2' in cols:
        kind = 'xyxy'
    elif 'cx' in cols and 'w' in cols:
        kind = 'cxcywh'
    elif 'x' in cols and 'w' in cols:
        kind = 'xywh'
    else:
        kind = None
    recs, big = [], False
    for k, r in enumerate(body):
        try:
            key, cls = r[cols['image']].strip(), r[cols['cls']].strip()
        except IndexError:
            rep.skip('bad_line', f'line {k + 2}: too few columns')
            continue
        if not key or not cls:
            rep.skip('bad_line', f'line {k + 2}: no image or class')
            continue
        cls = int(cls) if re.fullmatch(r'\d+', cls) else cls
        score = None
        if 'score' in cols and len(r) > cols['score']:
            try:
                score = float(r[cols['score']])
            except ValueError:
                pass
        if task_type == 'classification' or kind is None or len(r) <= max(cols.get(c, 0) for c in cols if c not in ('score',)):
            if task_type == 'classification' or kind is None:
                recs.append(Record(key, cls, 'tag', {}, score=score))
            else:
                rep.skip('bad_line', f'line {k + 2}: too few columns for a box')
            continue
        try:
            if kind == 'xyxy':
                x1, y1, x2, y2 = (float(r[cols[c]]) for c in ('x1', 'y1', 'x2', 'y2'))
                g = {'x': min(x1, x2), 'y': min(y1, y2), 'w': abs(x2 - x1), 'h': abs(y2 - y1)}
            elif kind == 'cxcywh':
                cx, cy, w, h = (float(r[cols[c]]) for c in ('cx', 'cy', 'w', 'h'))
                g = {'x': cx - w / 2, 'y': cy - h / 2, 'w': w, 'h': h}
            else:
                x, y, w, h = (float(r[cols[c]]) for c in ('x', 'y', 'w', 'h'))
                g = {'x': x, 'y': y, 'w': w, 'h': h}
        except ValueError:
            rep.skip('bad_line', f'line {k + 2}: not numbers')
            continue
        big = big or max(abs(g['x']) + g['w'], abs(g['y']) + g['h']) > 1.5
        recs.append(Record(key, cls, 'bbox', g, score=score))
    px = coords == 'px' or (coords == 'auto' and big)
    if px:
        for rc in recs:
            if rc.gtype == 'bbox':
                rc.px = True
    if task_type in ('segmentation', 'keypoints') and recs:
        rep.skip('other_type', f'a table holds boxes / tags; this task is {task_type.replace("_", " ")}')
        return []
    return recs


# ---------------------------------------------------------------- databases
def _check_sql(sql):
    sql = (sql or '').strip().rstrip(';')
    if not SQL_START.match(sql) or ';' in sql or SQL_FORBIDDEN.search(re.sub(r"'[^']*'", "''", sql)):
        raise ValueError('only one SELECT (or WITH ... SELECT) query is allowed')
    return sql


def database(conn, task_type, rep, mode='dataviz', sql=None, task_name=None, limit=None, coords='auto'):
    """Records from a database: mode 'dataviz' reads the tables db_export.py writes (annotations joined to media
    and classes; task_name narrows to one exported task), mode 'sql' runs the user's SELECT, whose columns are
    named like a text table (image, class, x1 y1 x2 y2 | cx cy w h | x y w h, optional score, coords norm | px)."""
    from sqlalchemy import select, text
    from . import db_export, schema
    eng = db_export.engine_for(conn)
    try:
        with eng.connect() as c:
            c = c.execution_options(stream_results=True, yield_per=5000)
            if mode == 'dataviz':
                md, prefix = db_export._metadata(conn)
                tbl = schema.tables(md, prefix)
                a, m, k, t = (tbl[n] for n in ('annotations', 'media', 'label_classes', 'tasks'))
                q = (select(m.c.file_name, m.c.uri, k.c.name, a.c.geometry_type, a.c.geometry, a.c.score)
                     .select_from(a.join(m, a.c.media_id == m.c.id).join(k, a.c.class_id == k.c.id).join(t, a.c.task_id == t.c.id))
                     .where(a.c.is_deleted.is_(False)))
                if task_name:
                    q = q.where(t.c.name == task_name)
                if limit:
                    q = q.limit(limit)
                want = {'object_detection': 'bbox', 'segmentation': 'polygon', 'keypoints': 'keypoints',
                        'classification': 'tag'}.get(task_type)
                out = []
                for fn, uri, cname, gtype, geom, score in c.execute(q):
                    if gtype != want:
                        rep.skip('other_type')
                        continue
                    if isinstance(geom, (str, bytes)):
                        geom = orjson.loads(geom)
                    out.append(Record(uri or fn, cname, gtype, geom or {}, score=score))
                return out
            q = _check_sql(sql)
            if limit:
                q = f'SELECT * FROM ({q}) AS q' + ('' if conn.get('kind') == 'mssql' else f' LIMIT {int(limit)}')
            res = c.execute(text(q))
            header = list(res.keys())
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(header)
            for n, row in enumerate(res):
                if limit and n >= limit:
                    break
                w.writerow(['' if v is None else v for v in row])
            return table(buf.getvalue().encode(), task_type, rep, coords)
    finally:
        eng.dispose()


# ---------------------------------------------------------------- files from a source / upload
def read_files(src, exts, cap_bytes=2 << 30):
    """{rel: bytes} of every file of a media source whose name ends with one of exts (listing via list_images)."""
    items = src.list_images(match=lambda n: n.lower().endswith(exts))
    total = sum(size or 0 for _, size in items)
    if total > cap_bytes:
        raise ValueError(f'{total / 1e9:.1f} GB of label files: too much to import at once')
    return {rel: src.read_bytes(rel) for rel, _ in items}


def unzip(data, exts):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        out = {}
        for info in z.infolist():
            name = info.filename.replace('\\', '/')
            if info.is_dir() or name.startswith('/') or '..' in name.split('/') or '__MACOSX' in name:
                continue
            if name.lower().endswith(exts) or posixpath.basename(name).lower() in ('classes.txt', 'data.yaml'):
                out[name] = z.read(info)
        return out


def records_from_files(fmt, files, task_type, n_kpt, rep, coords='auto'):
    """(records, class names or None) of {rel: bytes} in one format."""
    if fmt == 'yolo':
        names = class_names_from(files)
        recs = []
        for rel, data in sorted(files.items()):
            base = posixpath.basename(rel).lower()
            if not rel.lower().endswith('.txt') or base == 'classes.txt':
                continue
            recs.extend(yolo_lines(_label_key(rel), data.decode('utf-8', 'replace'), task_type, n_kpt, rep, rel))
        return recs, names
    if fmt == 'coco':
        recs, names = [], []
        for rel, data in sorted(files.items()):
            if rel.lower().endswith('.json'):
                r, n = coco(data, task_type, rep)
                recs.extend(r)
                names.extend(x for x in n if x not in names)
        return recs, names or None
    if fmt == 'text':
        recs = []
        for rel, data in sorted(files.items()):
            if rel.lower().endswith(TABLE_EXTS):
                recs.extend(table(data, task_type, rep, coords))
        return recs, None
    raise ValueError(f'unknown format {fmt!r} (one of {", ".join(FORMATS)})')


def guess_format(files):
    """yolo | coco | text from what the files look like."""
    names = [k.lower() for k in files]
    if any(n.endswith('.json') for n in names):
        return 'coco'
    if any(n.endswith(('.csv', '.tsv')) for n in names):
        return 'text'
    txt = [k for k in files if k.lower().endswith('.txt') and posixpath.basename(k).lower() != 'classes.txt']
    if len(txt) == 1:
        first = files[txt[0]].decode('utf-8', 'replace').split('\n', 1)[0].split()
        if first and not re.fullmatch(r'-?\d+(\.\d+)?', first[0]):
            return 'text'                          # `image.jpg cls ...`: a list, not a YOLO label file
    return 'yolo'


# ---------------------------------------------------------------- matching to the project's images
def media_index(rows):
    """Lookup tables of the project's media: rel path, file name, stem (all lower case) -> (id, w, h)."""
    by_rel, by_name, by_stem, by_stem_rel = {}, {}, {}, {}
    for r in rows:
        v = (r['id'], r['width'], r['height'])
        rel = str((r.get('meta') or {}).get('rel_path') or r.get('uri') or r['file_name']).replace('\\', '/').lower()
        by_rel.setdefault(rel, v)
        by_name.setdefault(r['file_name'].lower(), v)
        by_name.setdefault(posixpath.basename(rel), v)
        by_stem.setdefault(posixpath.splitext(posixpath.basename(rel))[0], v)
        by_stem.setdefault(posixpath.splitext(r['file_name'].lower())[0], v)
        by_stem_rel.setdefault(posixpath.splitext(rel)[0], v)
    return by_rel, by_name, by_stem, by_stem_rel


def find(idx, key):
    by_rel, by_name, by_stem, by_stem_rel = idx
    k = key.replace('\\', '/').lstrip('./').lower()
    for i in range(k.count('/') + 1):              # images/train/a.jpg -> train/a.jpg -> a.jpg
        sub = k.split('/', i)[-1]
        hit = by_rel.get(sub) or by_stem_rel.get(sub) or by_stem_rel.get(posixpath.splitext(sub)[0])
        if hit:
            return hit
    base = posixpath.basename(k)
    return by_name.get(base) or by_stem.get(posixpath.splitext(base)[0]) or by_stem.get(base)


def resolve(records, idx, names, rep):
    """[(media_id, class key, gtype, geom (normalised), score)] + the unmatched image keys. class key = a class name,
    or `#<index>` when a YOLO index has no name."""
    out, unmatched, cache = [], Counter(), {}
    for rc in records:
        hit = cache.get(rc.key)
        if hit is None:
            hit = cache[rc.key] = find(idx, rc.key) or False
        if not hit:
            unmatched[rc.key] += 1
            continue
        mid, w, h = hit
        g = rc.geom
        if rc.px:
            if not w or not h:
                rep.skip('no_size', f'{rc.key}: pixel coordinates but the image size is unknown')
                continue
            g = _scale(rc.gtype, g, 1.0 / w, 1.0 / h)
        c = rc.cls
        if isinstance(c, int):
            c = names[c] if names and 0 <= c < len(names) else f'#{c}'
        out.append((mid, str(c), rc.gtype, g, rc.score))
    return out, unmatched


def _scale(gtype, g, sx, sy):
    if gtype == 'bbox':
        return {'x': g['x'] * sx, 'y': g['y'] * sy, 'w': g['w'] * sx, 'h': g['h'] * sy}
    if gtype in ('polygon', 'keypoints'):
        return {**g, 'points': [[p[0] * sx, p[1] * sy, *p[2:]] for p in g['points']]}
    return g
