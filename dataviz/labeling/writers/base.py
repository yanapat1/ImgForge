"""
The Writer contract shared by the session export (export.py) and the Data Center's "Label with model" (it writes
into the bucket): one instance per (task, format), rows streamed in, files written through a Sink (sinks.py).

    w = REGISTRY[key]()
    w.begin(sink, task, names, ctx)      task = {id, name, task_type, config, classes}
    w.add(media_row, rows)               once per image (rows: that task's live annotation rows, see store.py)
    w.finish() -> {files, ...}           writes what was held back (COCO's arrays, a CSV, data.yaml ...)
    w.abort()                            drops temp files after an error

ctx (built by export_ctx / bucket_ctx below) says where things go, so one writer serves both layouts:
    prefix            path inside the sink every file goes under ('<task-slug>/' on export, '' in a bucket)
    index             {class_id: dense 0..N-1 id} (formats.class_map)
    image_rel(m)      the image's path relative to the prefix folder (for imagePath / file_name / data.yaml)
    item(m)           the per-image file stem, sub-folders kept ('train/a' for images/train/a.jpg)
    split(m)          'train' / 'val' / 'test' / 'default': COCO writes one instances_<split>.json per split
    image_file(m)     a local Path of the bytes (or None): image size fallback, imagefolder copies, text content
    label_rel(m)      the YOLO .txt path (the bucket mirrors images/ -> labels/)
    data_yaml(task, names) -> str | None     the bucket's data.yaml (train / val from its splits)
    keys              every format of this run (yolo + yolo_obb then keep their label folders apart)
    warnings          {name: count}, shared by every writer of the task

Coordinates in rows are normalised 0..1; pixel formats (COCO, VOC, LabelMe, CreateML, CSV) need the media row's
width / height, read from the file when the row has none.
"""

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

SPLITS = {'train': 'train', 'val': 'val', 'valid': 'val', 'validation': 'val', 'test': 'test'}


class Writer:
    key = ''
    label = ''
    media = ('image',)
    tasks = ()
    layout = ''              # the tree written into a bucket folder (shown live in the autolabel dialog)
    export_layout = ''       # the tree written inside <task>/ of a session export
    note = ''

    @classmethod
    def meta(cls):
        return {'key': cls.key, 'label': cls.label, 'media': list(cls.media), 'tasks': list(cls.tasks),
                'layout': cls.layout, 'export_layout': cls.export_layout, 'note': cls.note}

    # ---- lifecycle
    def begin(self, sink, task, names, ctx):
        self.sink, self.task, self.names, self.ctx = sink, task, list(names), ctx
        self.index = ctx['index']
        self.warnings = ctx.setdefault('warnings', {})
        self.files = 0
        self.n_media = 0
        self._tmp = None
        self._sizes = {}

    def add(self, m, rows):
        raise NotImplementedError

    def finish(self):
        self.abort()
        return {'files': self.files, 'n_media': self.n_media}

    def abort(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    # ---- helpers
    def write(self, rel, data):
        self.sink.write(self.ctx.get('prefix', '') + rel, data if isinstance(data, bytes) else data.encode())
        self.files += 1

    def copy(self, rel, src):
        self.sink.copy_file(self.ctx.get('prefix', '') + rel, src)
        self.files += 1

    def tmp(self, name):
        """A temp file path private to this writer (COCO / CSV / JSONL stream into these)."""
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix=f'dv-{self.key}-', dir=self.ctx.get('tmp_dir'))
        return os.path.join(self._tmp, name)

    def warn(self, k, n=1):
        self.warnings[k] = self.warnings.get(k, 0) + n

    def own(self, rows, types=None):
        """This task's rows of a known class (pose parents of another task and excluded classes are left out)."""
        return [r for r in rows if not r.get('_parent_only') and r.get('class_id') in self.index
                and (types is None or r['geometry_type'] in types)]

    def cname(self, r):
        return self.names[self.index[r['class_id']]]

    def size(self, m):
        """(width, height) in pixels: the media row's, else read from the file header; (0, 0) when unknown."""
        w, h = m.get('width') or 0, m.get('height') or 0
        if w and h:
            return int(w), int(h)
        if m['id'] in self._sizes:
            return self._sizes[m['id']]
        got = (0, 0)
        fn = self.ctx.get('image_file')
        p = fn(m) if fn else None
        if p is not None:
            try:
                from PIL import Image
                with Image.open(p) as im:                 # header only; EXIF 5-8 rotate by 90 degrees
                    got = im.size[::-1] if im.getexif().get(0x0112, 1) in (5, 6, 7, 8) else im.size
            except Exception:  # noqa: BLE001 - an unreadable image is reported as size_unknown
                got = (0, 0)
        if not got[0]:
            self.warn('size_unknown')
        if len(self._sizes) < 100_000:
            self._sizes[m['id']] = got
        return got


# ---------------------------------------------------------------- geometry helpers
def bbox_of(r, rows_by_id=None):
    """Normalised (x0, y0, x1, y1) of any 2D row: its bbox, the points' extent, or the stored bbox_* columns."""
    g = r.get('geometry') or {}
    t = r['geometry_type']
    if t == 'bbox':
        return g['x'], g['y'], g['x'] + g['w'], g['y'] + g['h']
    if t == 'keypoints' and rows_by_id and r.get('parent_id') in rows_by_id:
        return bbox_of(rows_by_id[r['parent_id']])
    pts = g.get('points')
    if t == 'keypoints' and pts:
        pts = [p for p in pts if len(p) < 3 or p[2] > 0] or pts
    if pts:
        xs, ys = [float(p[0]) for p in pts], [float(p[1]) for p in pts]
        return min(xs), min(ys), max(xs), max(ys)
    if t == 'point':
        return g['x'], g['y'], g['x'], g['y']
    if r.get('bbox_w') is not None:
        return r['bbox_x'], r['bbox_y'], r['bbox_x'] + r['bbox_w'], r['bbox_y'] + r['bbox_h']
    return None


def poly_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i][0], pts[i][1]
        x1, y1 = pts[(i + 1) % len(pts)][0], pts[(i + 1) % len(pts)][1]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2


def rnd(v, n=2):
    return round(float(v), n)


# ---------------------------------------------------------------- contexts
def _stem_path(p):
    p = PurePosixPath(p)
    return str(p.with_suffix('')) if p.suffix else str(p)


def export_ctx(slug, index, image_path=None, keys=(), warnings=None, tmp_dir=None):
    """The session export: images/ shared at the root, this task's files in <slug>/."""
    def item(m):
        return PurePosixPath(m['file_name']).stem
    return {'mode': 'export', 'prefix': f'{slug}/', 'index': index, 'keys': set(keys),
            'warnings': warnings if warnings is not None else {}, 'tmp_dir': tmp_dir,
            'image_rel': lambda m: f'../images/{m["file_name"]}', 'item': item,
            'split': lambda m: 'default', 'image_file': image_path,
            'label_rel': lambda m: f'labels/{item(m)}.txt', 'data_yaml': None}


def split_of(rel):
    """The split named by an image's path: images/<split>/..., <split>/images/... or <split>/..."""
    for part in rel.split('/')[:-1][:3]:
        s = SPLITS.get(part.lower())
        if s:
            return s
    return 'default'


def bucket_ctx(base, index, label_rel, data_yaml=None, keys=(), warnings=None, image_prefix=''):
    """A folder of a bucket: image paths are relative to `base` (meta.rel_path of the session's media).
    image_prefix: prepended to each rel_path (the picked folder was images/ itself and the sink is its parent)."""
    def rel(m):
        meta = m.get('meta') if isinstance(m.get('meta'), dict) else {}
        return image_prefix + str(meta.get('rel_path') or m.get('uri') or m['file_name'])

    def item(m):
        parts = rel(m).split('/')
        if len(parts) > 1 and parts[0].lower() == 'images':
            parts = parts[1:]
        return _stem_path('/'.join(parts))

    def image_file(m):
        p = Path(base) / rel(m)
        return p if p.is_file() else None
    return {'mode': 'bucket', 'prefix': '', 'index': index, 'keys': set(keys),
            'warnings': warnings if warnings is not None else {}, 'tmp_dir': None,
            'image_rel': rel, 'item': item, 'split': lambda m: split_of(rel(m)), 'image_file': image_file,
            'label_rel': lambda m: label_rel(rel(m)), 'data_yaml': data_yaml}
