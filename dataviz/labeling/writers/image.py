"""
Image label writers: YOLO (detect / seg / pose / cls, wrapping formats.py), YOLO-OBB, COCO, Pascal VOC, LabelMe,
CreateML, CSV and imagefolder (classification). See base.py for the contract and the ctx keys.
"""

import csv
import math
import posixpath
import shutil
from xml.sax.saxutils import escape

import orjson

from .. import formats
from .base import Writer, bbox_of, poly_area, rnd

IMAGE_TASKS = ('object_detection', 'segmentation', 'keypoints', 'classification')


def _clamp(v):
    return min(1.0, max(0.0, float(v)))


class Yolo(Writer):
    key = 'yolo'
    label = 'YOLO'
    tasks = IMAGE_TASKS
    layout = ('├── images/            (loose images are moved here)\n│   └── a.jpg\n'
              '├── labels/            mirrors images/\n│   └── a.txt\n├── data.yaml\n└── classes.txt')
    export_layout = '├── labels/\n│   └── a.txt\n├── data.yaml\n└── classes.txt'
    note = 'one .txt per image: detect, segment, pose or classify'

    def add(self, m, rows):
        self.write(self.ctx['label_rel'](m), formats.label_text(self.task, rows, self.index, self.warnings))
        self.n_media += 1

    def finish(self):
        self.write('classes.txt', formats.classes_txt(self.names))
        dy = self.ctx.get('data_yaml')
        self.write('data.yaml', (dy(self.task, self.names) if dy else None) or formats.data_yaml(self.task, self.names))
        return super().finish()


def obb_points(r):
    """The 4 corners (normalised) of a bbox row (rotated by geometry.angle degrees, about its centre), or the
    points of a 4-point polygon; None for anything else."""
    g = r.get('geometry') or {}
    if r['geometry_type'] == 'polygon' and len(g.get('points') or []) == 4:
        return [(float(p[0]), float(p[1])) for p in g['points']]
    if r['geometry_type'] != 'bbox':
        return None
    cx, cy, hw, hh = g['x'] + g['w'] / 2, g['y'] + g['h'] / 2, g['w'] / 2, g['h'] / 2
    a = math.radians(float(g.get('angle') or (r.get('attributes') or {}).get('angle') or 0))
    c, s = math.cos(a), math.sin(a)
    return [(cx + dx * c - dy * s, cy + dx * s + dy * c) for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


class YoloObb(Writer):
    key = 'yolo_obb'
    label = 'YOLO-OBB'
    tasks = ('object_detection',)
    layout = ('├── images/            (loose images are moved here)\n│   └── a.jpg\n├── labels/            cls x1 y1 x2 y2 x3 y3 x4 y4\n│   └── a.txt\n'
              '├── data.yaml\n└── classes.txt')
    export_layout = '├── labels/            (labels-obb/ next to YOLO)\n│   └── a.txt\n├── data.yaml\n└── classes.txt'
    note = 'oriented boxes: 4 corners per object'

    def _rel(self, m):
        rel = self.ctx['label_rel'](m)
        if 'yolo' not in self.ctx.get('keys', ()):
            return rel
        parts = rel.split('/')
        i = next((k for k, p in enumerate(parts) if p == 'labels'), None)
        return '/'.join(parts[:i] + ['labels-obb'] + parts[i + 1:]) if i is not None else 'labels-obb/' + rel

    def add(self, m, rows):
        out = []
        for r in self.own(rows, ('bbox', 'polygon')):
            pts = obb_points(r)
            if pts:
                out.append(f'{self.index[r["class_id"]]} ' + ' '.join(f'{_clamp(x):.6f} {_clamp(y):.6f}' for x, y in pts))
        self.write(self._rel(m), ''.join(line + '\n' for line in out))
        self.n_media += 1

    def finish(self):
        if 'yolo' not in self.ctx.get('keys', ()):          # YOLO writes the same data.yaml / classes.txt
            self.write('classes.txt', formats.classes_txt(self.names))
            dy = self.ctx.get('data_yaml')
            self.write('data.yaml', (dy(self.task, self.names) if dy else None) or formats.data_yaml(self.task, self.names))
        return super().finish()


class Coco(Writer):
    """images / annotations stream into temp files (one pair per split); categories and the header are written at
    finish, so memory stays flat whatever the number of images."""
    key = 'coco'
    label = 'COCO'
    tasks = ('object_detection', 'segmentation', 'keypoints')
    layout = '├── …                  your images, left where they are\n└── annotations/\n    └── instances_<split>.json   (train / val / test / default)'
    export_layout = '└── annotations/\n    └── instances_default.json'
    note = 'one JSON per split: bbox, polygon, keypoints'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self.parts = {}
        self.img_id = self.ann_id = 0

    def _part(self, split):
        p = self.parts.get(split)
        if p is None:
            p = self.parts[split] = {'img': open(self.tmp(f'{split}.images'), 'wb'),
                                     'ann': open(self.tmp(f'{split}.anns'), 'wb'), 'ni': 0, 'na': 0}
        return p

    @staticmethod
    def _put(p, k, obj):
        p[k].write((b',' if p['n' + k[0]] else b'') + orjson.dumps(obj))
        p['n' + k[0]] += 1

    def add(self, m, rows):
        W, H = self.size(m)
        p = self._part(self.ctx['split'](m))
        self.img_id += 1
        name = m['file_name'] if self.ctx.get('mode') == 'export' else self.ctx['image_rel'](m)
        self._put(p, 'img', {'id': self.img_id, 'file_name': name, 'width': W, 'height': H})
        by_id = {r['id']: r for r in rows if r['geometry_type'] == 'bbox'}
        for r in self.own(rows, ('bbox', 'polygon', 'keypoints', 'mask_rle')):
            box = bbox_of(r, by_id)
            if box is None:
                continue
            x0, y0, x1, y1 = box
            a = {'id': self.ann_id + 1, 'image_id': self.img_id, 'category_id': self.index[r['class_id']] + 1,
                 'bbox': [rnd(x0 * W), rnd(y0 * H), rnd((x1 - x0) * W), rnd((y1 - y0) * H)], 'iscrowd': 0}
            a['area'] = rnd((x1 - x0) * W * (y1 - y0) * H)
            g = r['geometry']
            t = r['geometry_type']
            if t == 'polygon':
                pts = [(float(q[0]) * W, float(q[1]) * H) for q in g['points']]
                a['segmentation'] = [[rnd(v) for xy in pts for v in xy]]
                a['area'] = rnd(poly_area(pts))
                if g.get('holes'):
                    self.warn('holes_dropped', len(g['holes']))
            elif t == 'mask_rle':
                a['segmentation'] = {'size': g.get('size'), 'counts': g.get('counts')}
            elif t == 'keypoints':
                kp = []
                for q in g['points']:
                    v = int(q[2]) if len(q) > 2 else 2
                    kp += [rnd(float(q[0]) * W), rnd(float(q[1]) * H), v] if v > 0 else [0, 0, 0]
                n = len((self.task.get('config') or {}).get('keypoints') or []) or len(g['points'])
                kp = (kp + [0, 0, 0] * n)[:3 * n]
                a['keypoints'] = kp
                a['num_keypoints'] = sum(1 for i in range(2, len(kp), 3) if kp[i] > 0)
            if r.get('score') is not None and r.get('source') == 'model':
                a['score'] = rnd(r['score'], 4)
            self.ann_id += 1
            self._put(p, 'ann', a)
        self.n_media += 1

    def _categories(self):
        cfg = self.task.get('config') or {}
        cats = []
        for i, n in enumerate(self.names):
            c = {'id': i + 1, 'name': n, 'supercategory': ''}
            if self.task['task_type'] == 'keypoints':
                c['keypoints'] = list(cfg.get('keypoints') or [])
                c['skeleton'] = [[int(a) + 1, int(b) + 1] for a, b in (cfg.get('skeleton') or [])]
            cats.append(c)
        return cats

    def finish(self):
        if not self.parts:
            self._part('default')
        head = orjson.dumps({'description': self.task.get('name', ''), 'version': '1.0'})
        cats = orjson.dumps(self._categories())
        for split, p in sorted(self.parts.items()):
            p['img'].close()
            p['ann'].close()
            out = self.tmp(f'{split}.json')
            with open(out, 'wb') as f:
                f.write(b'{"info":' + head + b',"licenses":[],"categories":' + cats + b',"images":[')
                with open(p['img'].name, 'rb') as s:
                    shutil.copyfileobj(s, f, 1 << 20)
                f.write(b'],"annotations":[')
                with open(p['ann'].name, 'rb') as s:
                    shutil.copyfileobj(s, f, 1 << 20)
                f.write(b']}')
            self.copy(f'annotations/instances_{split}.json', out)
        return {**super().finish(), 'splits': sorted(self.parts), 'n_annotations': self.ann_id}

    def abort(self):
        for p in getattr(self, 'parts', {}).values():
            p['img'].close()
            p['ann'].close()
        super().abort()


class Voc(Writer):
    key = 'voc'
    label = 'Pascal VOC'
    tasks = ('object_detection',)
    layout = '├── …                  your images, left where they are\n└── Annotations/\n    └── a.xml'
    export_layout = '└── Annotations/\n    └── a.xml'
    note = 'one XML per image, boxes in pixels'

    def add(self, m, rows):
        W, H = self.size(m)
        img = self.ctx['image_rel'](m)
        objs = []
        for r in self.own(rows, ('bbox',)):
            g = r['geometry']
            xs = [max(0, min(W, round(v * W))) for v in (g['x'], g['x'] + g['w'])]
            ys = [max(0, min(H, round(v * H))) for v in (g['y'], g['y'] + g['h'])]
            objs.append(f'  <object>\n    <name>{escape(self.cname(r))}</name>\n    <pose>Unspecified</pose>\n'
                        f'    <truncated>0</truncated>\n    <difficult>0</difficult>\n'
                        f'    <bndbox><xmin>{xs[0]}</xmin><ymin>{ys[0]}</ymin><xmax>{xs[1]}</xmax><ymax>{ys[1]}</ymax></bndbox>\n'
                        '  </object>\n')
        folder = posixpath.basename(posixpath.dirname(img)) or 'images'
        xml = (f'<annotation>\n  <folder>{escape(folder)}</folder>\n  <filename>{escape(posixpath.basename(img))}</filename>\n'
               f'  <path>{escape(img)}</path>\n  <source><database>Unknown</database></source>\n'
               f'  <size><width>{W}</width><height>{H}</height><depth>3</depth></size>\n'
               f'  <segmented>0</segmented>\n{"".join(objs)}</annotation>\n')
        self.write(f'Annotations/{self.ctx["item"](m)}.xml', xml)
        self.n_media += 1


class LabelMe(Writer):
    key = 'labelme'
    label = 'LabelMe'
    tasks = ('object_detection', 'segmentation', 'keypoints')
    layout = '├── …                  your images, left where they are\n└── labelme/\n    └── a.json          imagePath relative to the json'
    export_layout = '└── labelme/\n    └── a.json'
    note = 'one JSON per image: rectangles, polygons, points'

    def add(self, m, rows):
        W, H = self.size(m)
        rel = f'labelme/{self.ctx["item"](m)}.json'
        img = posixpath.relpath(self.ctx['image_rel'](m), posixpath.dirname(rel))
        kp_names = (self.task.get('config') or {}).get('keypoints') or []
        shapes = []
        gid = 0

        def shape(label, pts, kind, group=None, desc=''):
            shapes.append({'label': label, 'points': [[rnd(x * W), rnd(y * H)] for x, y in pts], 'group_id': group,
                           'description': desc, 'shape_type': kind, 'flags': {}, 'mask': None})
        for r in self.own(rows, ('bbox', 'polygon', 'keypoints', 'point')):
            g, t, n = r['geometry'], r['geometry_type'], self.cname(r)
            if t == 'bbox':
                shape(n, [(g['x'], g['y']), (g['x'] + g['w'], g['y'] + g['h'])], 'rectangle')
            elif t == 'polygon':
                shape(n, [(float(p[0]), float(p[1])) for p in g['points']], 'polygon')
            elif t == 'point':
                shape(n, [(g['x'], g['y'])], 'point')
            else:
                gid += 1
                for i, p in enumerate(g['points']):
                    if len(p) < 3 or p[2] > 0:
                        shape(n, [(float(p[0]), float(p[1]))], 'point', gid, kp_names[i] if i < len(kp_names) else f'kp{i + 1}')
        doc = {'version': '5.4.1', 'flags': {}, 'shapes': shapes, 'imagePath': img, 'imageData': None,
               'imageHeight': H, 'imageWidth': W}
        self.write(rel, orjson.dumps(doc, option=orjson.OPT_INDENT_2))
        self.n_media += 1


class CreateML(Writer):
    key = 'createml'
    label = 'CreateML'
    tasks = ('object_detection',)
    layout = '├── …                  your images, left where they are\n└── createml.json      [{image, annotations: [{label, coordinates}]}]'
    export_layout = '└── createml.json'
    note = 'one JSON array, box centres in pixels'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self.fh = open(self.tmp('createml.json'), 'wb')
        self.fh.write(b'[')

    def add(self, m, rows):
        W, H = self.size(m)
        anns = []
        for r in self.own(rows, ('bbox',)):
            g = r['geometry']
            anns.append({'label': self.cname(r), 'coordinates': {
                'x': rnd((g['x'] + g['w'] / 2) * W), 'y': rnd((g['y'] + g['h'] / 2) * H),
                'width': rnd(g['w'] * W), 'height': rnd(g['h'] * H)}})
        self.fh.write((b',' if self.n_media else b'') + orjson.dumps({'image': self.ctx['image_rel'](m), 'annotations': anns}))
        self.n_media += 1

    def finish(self):
        self.fh.write(b']')
        self.fh.close()
        self.copy('createml.json', self.fh.name)
        return super().finish()

    def abort(self):
        if getattr(self, 'fh', None):
            self.fh.close()
        super().abort()


CSV_HEADER = ['image', 'width', 'height', 'label', 'class_index', 'geometry_type', 'xmin', 'ymin', 'xmax', 'ymax',
              'points', 'score', 'source']


class Csv(Writer):
    key = 'csv'
    label = 'CSV'
    tasks = IMAGE_TASKS
    layout = '├── …                  your images, left where they are\n└── labels.csv          one row per shape (a labeled negative: empty label)'
    export_layout = '└── labels.csv'
    note = 'one row per shape, any task'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self.fh = open(self.tmp('labels.csv'), 'w', newline='', encoding='utf-8')
        self.cw = csv.writer(self.fh)
        self.cw.writerow(CSV_HEADER)

    def add(self, m, rows):
        W, H = self.size(m)
        img = self.ctx['image_rel'](m)
        by_id = {r['id']: r for r in rows if r['geometry_type'] == 'bbox'}
        own = self.own(rows)
        for r in own:
            b = bbox_of(r, by_id) if r['geometry_type'] != 'tag' else None
            pts = (r.get('geometry') or {}).get('points')
            self.cw.writerow([img, W, H, self.cname(r), self.index[r['class_id']], r['geometry_type'],
                              *([rnd(b[0] * W), rnd(b[1] * H), rnd(b[2] * W), rnd(b[3] * H)] if b else ['', '', '', '']),
                              orjson.dumps(pts).decode() if pts else '',
                              '' if r.get('score') is None else rnd(r['score'], 4), r.get('source') or ''])
        if not own:
            self.cw.writerow([img, W, H, '', '', '', '', '', '', '', '', '', ''])
        self.n_media += 1

    def finish(self):
        self.fh.close()
        self.copy('labels.csv', self.fh.name)
        return super().finish()

    def abort(self):
        if getattr(self, 'fh', None):
            self.fh.close()
        super().abort()


def _safe_dir(name):
    s = str(name).replace('/', '_').replace('\\', '_').strip().strip('.')
    return s or '_'


class ImageFolder(Writer):
    key = 'imagefolder'
    label = 'imagefolder'
    tasks = ('classification',)
    layout = '└── imagefolder/       a copy of each image per class\n    ├── cat/\n    │   └── a.jpg\n    └── dog/'
    export_layout = '└── imagefolder/\n    └── cat/\n        └── a.jpg'
    note = 'class sub-folders (copies the images)'

    def add(self, m, rows):
        own = self.own(rows, ('tag',))
        if not own:
            return
        fn = self.ctx.get('image_file')
        src = fn(m) if fn else None
        if src is None:
            self.warn('images_missing')
            return
        img = self.ctx['image_rel'](m)
        ext = posixpath.splitext(img)[1]
        flat = self.ctx['item'](m).replace('/', '__') + ext
        for c in sorted({self.cname(r) for r in own}):
            self.copy(f'imagefolder/{_safe_dir(c)}/{flat}', src)
        self.n_media += 1


WRITERS = [Yolo, YoloObb, Coco, Voc, LabelMe, CreateML, Csv, ImageFolder]
