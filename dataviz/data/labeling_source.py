"""
LabelingSource: one task of a labeling session (its rows in PostgreSQL lbl.*, labeling/store.py) read live as a YOLO dataset, so
a visualize session can explore what is being labeled without an export. Nothing is copied: labels are built in
memory from the annotation rows, images come through the project's ImageCache (local folders read in place,
remote bytes cached once in <pid>/cache/).

Same interface as sources.LocalSource / S3Source (name, uri, kind, root, fingerprint(), list_labels(),
list_images(), read_label(name), read_labels(names), read_classes(), open_image(name), image_ref(name),
label_ref(name), check()) plus the curation write API (label_writable, write_label(name, text, expect_sha1)).

    object_detection   one `cls cx cy w h` line per box (= formats.yolo_detect, what the YOLO export writes)
    segmentation       one line per polygon: its bbox_* extent (RLE masks have no extent: skipped)
    keypoints          one line per instance: the linked parent box, else the labeled points' extent + 2 %
                       (= the box of the pose export); boxes no instance links are lines of their own
    classification     refused (no boxes)

Label names are `<stem>.txt` of the media file_name (the import keeps stems unique). The text of every label is
built in ONE pass over the task (ProjectStore.iter_task_export, a private streamed connection) and cached per
fingerprint, so the indexer's read_labels() and the curation scan never run a query per image.

Coordinates in the store are relative to the EXIF-oriented image, so open_image() returns the transposed image
(the indexer itself never applies EXIF orientation).
"""

import difflib
import hashlib
import threading

from PIL import Image, ImageOps

from ..auth.context import user_id
from ..labeling import formats
from ..labeling.store import Conflict as StoreConflict, new_id
from .sources import INDEX_VERSION, Conflict

TASK_TYPES = ('object_detection', 'segmentation', 'keypoints')
_ORIENTATION = 0x0112


class LabelConflict(Conflict):
    """write_label() with an expect_sha1 that no longer matches (the label changed since it was read)."""

    def __init__(self, name, current_sha1=None):
        super().__init__(name, current_sha1)
        self.name = name
        self.current_sha1 = current_sha1


def _sha1(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()


def _stem(file_name):
    i = file_name.rfind('.')
    return file_name[:i] if i > 0 else file_name


def _safe_name(name):
    """The name also names the workspace folder (<name>__<fingerprint>): no path separators."""
    return str(name).replace('/', '-').replace('\\', '-').strip() or 'labeling session'


def _box_row(cid, x, y, w, h):
    return {'geometry_type': 'bbox', 'class_id': cid, 'geometry': {'x': x, 'y': y, 'w': w, 'h': h}}


_ROW_KEYS = ('id', 'class_id', 'geometry_type', 'geometry', 'attributes', 'parent_id', 'z_order', 'score', 'source')


def _row_copy(a):
    """The fields of a stored shape that put_annotations takes back (JSON-safe: kept in the curation backup)."""
    return {k: a.get(k) for k in _ROW_KEYS}


def label_lines(task_type, rows, cmap):
    """(YOLO detect text, [ann ids behind each line, the first is the line's own row]) of one image.

    rows: live annotation rows of the task (geometry decoded; iter_task_export's _parent_only boxes included)."""
    out, refs = [], []
    if task_type == 'object_detection':
        for r in rows:
            if r['geometry_type'] == 'bbox' and r.get('class_id') in cmap and not r.get('_parent_only'):
                out.append(r)
                refs.append((r['id'],))
    elif task_type == 'segmentation':
        for r in rows:
            if r['geometry_type'] == 'polygon' and r.get('class_id') in cmap and r.get('bbox_w'):
                out.append(_box_row(r['class_id'], r['bbox_x'], r['bbox_y'], r['bbox_w'], r['bbox_h']))
                refs.append((r['id'],))
    elif task_type == 'keypoints':
        boxes = {r['id']: r for r in rows if r['geometry_type'] == 'bbox'}
        linked = set()
        for r in rows:
            if r['geometry_type'] != 'keypoints' or r.get('class_id') not in cmap:
                continue
            b = boxes.get(r.get('parent_id'))
            if b is not None:
                linked.add(b['id'])
                g = b['geometry']
                out.append(_box_row(r['class_id'], g['x'], g['y'], g['w'], g['h']))
            else:
                pts = [(float(p[0]), float(p[1]), int(p[2]) if len(p) > 2 else 2) for p in r['geometry']['points']]
                cx, cy, w, h = formats._kpt_box(pts)
                if w <= 0 or h <= 0:
                    continue
                out.append(_box_row(r['class_id'], cx - w / 2, cy - h / 2, w, h))
            # the same-task parent box goes (and is reclassified) with its instance
            refs.append((r['id'],) + ((b['id'],) if b is not None and not b.get('_parent_only') else ()))
        for r in rows:                                  # boxes no instance links: lines of their own
            if r['geometry_type'] == 'bbox' and r['id'] not in linked and not r.get('_parent_only') \
                    and r.get('class_id') in cmap:
                out.append(r)
                refs.append((r['id'],))
    else:
        raise ValueError(f'a {task_type.replace("_", " ")} task has no boxes to visualize')
    return formats.yolo_detect(out, cmap), refs


class LabelingSource:
    kind = 'labeling'
    root = None                       # no local YOLO folder: no model splits from folders ("all")
    label_writable = True

    def __init__(self, labeling, pid, task_id, only_done=False, name=None, connections=None, s3cfg=None):
        self.labeling = labeling      # labeling.projects.ProjectRegistry
        self.pid = pid
        self.task_id = task_id
        self.only_done = bool(only_done)
        self.connections = connections                  # data.connections.Registry: remote image sources
        self.s3cfg = s3cfg
        self.uri = f'label://{pid}/{task_id}' + ('?only_done=1' if self.only_done else '')
        self._name = _safe_name(name) if name else None
        self._lock = threading.Lock()
        self._snap = None             # (fingerprint, snapshot dict)
        self._cache = None            # (source json, labeling.media.ImageCache)

    # ------------------------------------------------------------ project / task
    def _store(self):
        return self.labeling.store(self.pid)            # KeyError: the labeling session was removed

    def _task(self):
        return self._store().task(self.task_id)         # KeyError: the task was deleted

    @property
    def name(self):
        if self._name is None:
            try:
                self._name = _safe_name(f"{self.labeling.get(self.pid)['name']} · {self._task()['name']}")
            except KeyError:
                return 'labeling session'
        return self._name

    def check(self):
        """Raise ValueError unless the session and the task exist and the task has boxes (not classification)."""
        try:
            self.labeling.get(self.pid)
            task = self._task()
        except KeyError as e:
            raise ValueError(e.args[0] if e.args else 'the labeling session or task no longer exists') from None
        if task['task_type'] not in TASK_TYPES:
            raise ValueError(f'task {task["name"]!r} is {task["task_type"].replace("_", " ")}: only detection, '
                             'segmentation and keypoints tasks have boxes to visualize')
        return task

    # ------------------------------------------------------------ fingerprint + snapshot
    def fingerprint(self):
        """Changes on any annotation edit / delete of this task, a new image, a class rename / reorder / delete,
        and (only_done) a status change. A handful of indexed aggregate queries: cheap at any size."""
        tid = self.task_id
        h = hashlib.sha1(f'{INDEX_VERSION}|labeling|{self.pid}|{tid}|{int(self.only_done)}'.encode())
        h.update(repr(self._store().fingerprint_parts(tid, self.only_done)).encode())
        return h.hexdigest()

    def _snapshot(self):
        """{images, uris, mids, labels:{name: text}, refs:{name: [ids per line]}, classes, task_type}, rebuilt in
        one streamed pass when the fingerprint changed."""
        fp = self.fingerprint()
        with self._lock:
            if self._snap is not None and self._snap[0] == fp:
                return self._snap[1]
            task = self.check()
            cmap, names = formats.class_map(task['classes'])
            ttype = task['task_type']
            images, uris, mids, labels, refs = [], {}, {}, {}, {}
            for m, rows in self._store().iter_task_export(self.task_id):
                fn = m['file_name']
                images.append(fn)
                uris[fn] = m['uri']
                name = f'{_stem(fn)}.txt'
                mids[name] = m['id']
                if self.only_done:
                    if m['status'] != 'done':
                        continue
                elif not any(not r.get('_parent_only') for r in rows):
                    continue
                labels[name], refs[name] = label_lines(ttype, rows, cmap)
            snap = {'images': images, 'uris': uris, 'mids': mids, 'labels': labels, 'refs': refs,
                    'classes': names, 'cmap': cmap, 'task_type': ttype}
            self._snap = (fp, snap)
            return snap

    # ------------------------------------------------------------ the source interface
    def list_images(self):
        return list(self._snapshot()['images'])

    def list_labels(self):
        return list(self._snapshot()['labels'])         # media position order (stable sample ids)

    def read_label(self, name):
        try:
            return self._snapshot()['labels'][name]
        except KeyError:
            raise FileNotFoundError(f'no label {name!r} in {self.uri}') from None

    def read_labels(self, names):
        """Every label at once from the cached snapshot (one pass over the task, however many images)."""
        labels = self._snapshot()['labels']
        return [labels.get(n, '') for n in names]

    def read_classes(self):
        return list(self._snapshot()['classes'])

    def _image_cache(self):
        from ..labeling import media                    # FTP / SFTP / S3 clients: imported only when used
        entry = self.labeling.get(self.pid)
        key = repr(sorted((entry.get('source') or {}).items()))
        hit = self._cache
        if hit is not None and hit[0] == key:
            return hit[1]
        with self._lock:
            if self._cache is None or self._cache[0] != key:
                pdir = self.labeling.project_dir(self.pid)
                src = media.make_source(entry['source'], pdir, self.connections, self.s3cfg)
                self._cache = (key, media.ImageCache(pdir, src))
            return self._cache[1]

    def open_image(self, name):
        snap = self._snap[1] if self._snap is not None else self._snapshot()
        uri = snap['uris'].get(name)
        if uri is None:
            uri = self._snapshot()['uris'].get(name)
        if uri is None:
            raise FileNotFoundError(f'no image {name!r} in {self.uri}')
        im = Image.open(self._image_cache().path(uri))
        # annotation coordinates are relative to the EXIF-oriented image; most files need no turn (stay lazy)
        if im.getexif().get(_ORIENTATION, 1) in (0, 1):
            return im
        out = ImageOps.exif_transpose(im)
        im.close()
        return out

    def image_ref(self, name):
        return f'{self.uri}/images/{name}'

    def label_ref(self, name):
        return f'{self.uri}/labels/{name}'

    # ------------------------------------------------------------ curation writes
    def _live(self, mid):
        """(base version, live rows of this task on `mid`, the rows label_lines() reads) from the store."""
        st = self._store()
        got = st.get_annotations(mid)
        rows = [a for a in got['annotations'] if a['task_id'] == self.task_id]
        ext = []
        if any(a['geometry_type'] == 'keypoints' and a.get('parent_id') for a in rows):
            own = {a['id'] for a in rows}
            want = [a['parent_id'] for a in rows if a['geometry_type'] == 'keypoints' and a.get('parent_id')
                    and a['parent_id'] not in own]
            if want:                                     # pose boxes of another task (read only here)
                for r in st.parent_boxes(want):
                    ext.append({'id': r['id'], 'class_id': r['class_id'], 'geometry_type': 'bbox',
                                'geometry': r['geometry'], '_parent_only': True})
        if any(a['geometry_type'] == 'polygon' for a in rows):
            bb = st.bbox_extents([a['id'] for a in rows if a['geometry_type'] == 'polygon'])
            view = [dict(a, bbox_x=bb[a['id']][0], bbox_y=bb[a['id']][1], bbox_w=bb[a['id']][2], bbox_h=bb[a['id']][3])
                    if a['id'] in bb else a for a in rows]
        else:
            view = list(rows)
        return got['versions'].get(self.task_id, 0), rows, view + ext

    def current_label(self, name):
        """(text, sha1) of one label straight from the store (not the snapshot)."""
        mid = self._mid(name)
        task = self.check()
        cmap, _ = formats.class_map(task['classes'])
        _, _, view = self._live(mid)
        text, _ = label_lines(task['task_type'], view, cmap)
        return text, _sha1(text)

    def _mid(self, name):
        snap = self._snap[1] if self._snap is not None else self._snapshot()
        mid = snap['mids'].get(name)
        if mid is None:
            mid = self._snapshot()['mids'].get(name)
        if mid is None:
            raise FileNotFoundError(f'no image for label {name!r} in {self.uri}')
        return mid

    def write_label(self, name, text, expect_sha1=None):
        """write_label_rows without the rows from before."""
        return self.write_label_rows(name, text, expect_sha1)[0]

    def write_label_rows(self, name, text, expect_sha1=None):
        """Make the task's shapes on that image say `text` (YOLO detect lines), through ProjectStore.put_annotations
        so versions, statuses and the labeling page's conflict checks stay right. Returns the sha1 of the label
        as stored afterwards. Raises LabelConflict when the label is not the one expect_sha1 hashes (or the image
        was saved in between), ValueError on a line it cannot apply.

        The new lines are diffed against the current ones (difflib): kept lines keep their row untouched, a
        changed line keeps its row with the new class (and, for a box, the new geometry when the numbers
        changed), removed lines are soft-deleted, added lines become new boxes (detection / keypoints tasks).

        Returns (sha1, rows): rows = the task's live shapes on that image before the write, every field kept
        (ids, geometry, attributes, parent, source, score), for restore_rows() (the curation undo)."""
        mid = self._mid(name)
        task = self.check()
        cmap, names = formats.class_map(task['classes'])
        by_index = {i: cid for cid, i in cmap.items()}
        ttype = task['task_type']
        version, rows, view = self._live(mid)
        old_text, refs = label_lines(ttype, view, cmap)
        if expect_sha1 is not None and _sha1(old_text) != expect_sha1:
            raise LabelConflict(name, _sha1(old_text))
        old, new = old_text.splitlines(), [ln for ln in text.splitlines() if ln.strip()]
        parsed = []
        for ln in new:
            p = ln.split()
            try:
                k = int(p[0])
                cx, cy, w, h = (float(v) for v in p[1:5])
            except (ValueError, IndexError):
                raise ValueError(f'{name}: cannot apply the line {ln!r}') from None
            if k not in by_index:
                raise ValueError(f'{name}: class {k} is not in this task ({len(names)} classes)')
            parsed.append((k, cx, cy, w, h))
        before = [_row_copy(a) for a in rows]
        keep = {a['id']: dict(a) for a in rows}
        removed, added = set(), []

        def change(i, j):
            """old line i -> new line j."""
            ids = refs[i]
            k, cx, cy, w, h = parsed[j]
            if old[i].split()[1:5] != new[j].split()[1:5]:
                a = keep.get(ids[0])
                target = a if a is not None and a['geometry_type'] == 'bbox' else \
                    keep.get(ids[1]) if len(ids) > 1 else None
                if target is not None:                    # polygons / free keypoint sets have no box to move
                    target['geometry'] = {'x': cx - w / 2, 'y': cy - h / 2, 'w': w, 'h': h}
            for aid in ids:
                if aid in keep:
                    keep[aid]['class_id'] = by_index[k]

        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if op == 'equal':
                continue
            n = min(i2 - i1, j2 - j1) if op == 'replace' else 0
            for d in range(n):
                change(i1 + d, j1 + d)
            for i in range(i1 + n, i2):
                removed.update(refs[i])
            for j in range(j1 + n, j2):
                if ttype == 'segmentation':
                    raise ValueError(f'{name}: a new box cannot be added to a segmentation task')
                k, cx, cy, w, h = parsed[j]
                added.append({'id': new_id(), 'class_id': by_index[k], 'geometry_type': 'bbox',
                              'geometry': {'x': cx - w / 2, 'y': cy - h / 2, 'w': w, 'h': h}, 'source': 'manual'})
        # put_annotations turns an edited model shape into a manual one itself
        out = [a for aid, a in keep.items() if aid not in removed] + added
        try:
            self._store().put_annotations(mid, self.task_id, out, version, user=user_id())
        except StoreConflict:
            raise LabelConflict(name) from None
        text_now, _ = self.current_label(name)
        return _sha1(text_now), before

    def restore_rows(self, name, rows, expect_sha1=None):
        """The undo of write_label_rows(): the task's shapes on that image become `rows` again through
        put_annotations (versioned): soft-deleted ones come back under their own id with their geometry (polygons,
        keypoints), attributes, source and score, edited ones get their old class / box, shapes the fix added are
        soft-deleted. LabelConflict when the label is not the one expect_sha1 hashes; ValueError when a row can no
        longer be stored (its class was deleted since). Returns the sha1 of the label afterwards."""
        mid = self._mid(name)
        task = self.check()
        cmap, _ = formats.class_map(task['classes'])
        version, _, view = self._live(mid)
        cur, _ = label_lines(task['task_type'], view, cmap)
        if expect_sha1 is not None and _sha1(cur) != expect_sha1:
            raise LabelConflict(name, _sha1(cur))
        try:
            self._store().put_annotations(mid, self.task_id, [_row_copy(a) for a in rows], version, user=user_id())
        except StoreConflict:
            raise LabelConflict(name) from None
        text_now, _ = self.current_label(name)
        return _sha1(text_now)

