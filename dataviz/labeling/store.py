"""
ProjectStore: one labeling project's rows in the shared PostgreSQL schema `lbl` (dataviz/db/schema.py).

Every lbl table holds the rows of every project of every user, each row carrying project_id = the label
session id. Every query here filters by it (or joins through a row that does): a query without it would read or
change another project's rows. tests/test_store.py::test_projects_never_see_each_other guards it.

Connections come from the process-wide pool (dataviz.db). write() is one transaction holding
pg_advisory_xact_lock(hashtext(pid)): the writers of one project run one at a time across threads and
processes, which the optimistic base_version check of put_annotations relies on. A write() opened inside
another one on the same thread joins it, and reads made while a write() is open on the thread go through its
connection (they see its uncommitted rows). Other reads take a pooled connection each.

Stored values: times are epoch seconds (double precision: the API, the card stats and the visualizer's
live-source fingerprint all work in epoch floats), JSON columns are JSONB (dicts in, dicts out), booleans are
booleans. iter_rows() converts rows into what the export targets' SQLAlchemy inserts expect (datetime for *_at).
"""

import functools
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import orjson
from psycopg import errors as pg_errors
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from .. import db
from ..db import schema as dbs
from . import schema

try:                                    # optional: a regex search with a timeout (the strip's /regex/ filter)
    import regex as _regex
except ImportError:                     # pragma: no cover - plain re: the nested-repetition check + a total deadline
    _regex = None

_SORT = orjson.OPT_SORT_KEYS
# a repeated group with a repetition inside ((a+)+, (\w*)*, (x\d{2,})+): exponential backtracking on a near miss;
# refused up front (same rule as static/js/core/filter.js)
_NESTED_QUANT = re.compile(r'\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)(?:[+*]|\{\d*,)')
REGEX_BUDGET_S = 0.5                    # one strip regex pass: this + 5 µs per name (re holds the GIL: a server freeze)
_REGEX_NAME_MAX = 1024
_ID = re.compile(r'^[0-9a-fA-F-]{8,36}$')
_HOTKEY = re.compile(r'^((ctrl|alt|shift|meta)\+)*[a-z0-9\[\];\',./`=\-]+$|^(f[1-9]|f1[0-2])$')

# the 4 task types the page draws, and the geometry types each accepts (keypoints may link a parent box)
TASK_GEOMS = {'object_detection': ('bbox',), 'segmentation': ('polygon', 'mask_rle'),
              'keypoints': ('keypoints', 'bbox'), 'classification': ('tag',)}

TABLES = {t.name: t for t in (dbs.lbl_projects, dbs.lbl_tasks, dbs.lbl_label_classes, dbs.lbl_media,
                              dbs.lbl_annotations, dbs.lbl_annotation_relations, dbs.lbl_media_task_status,
                              dbs.lbl_export_runs)}
_COLS = {name: [c.name for c in t.columns] for name, t in TABLES.items()}
_JSON_COLS = {name: {c.name for c in t.columns if isinstance(c.type, JSONB)} for name, t in TABLES.items()}
_TIME_COLS = {name: {c.name for c in t.columns if c.name.endswith('_at')} for name, t in TABLES.items()}
_STREAM_BATCH = 1000

# the write() connection open on this thread, per project id (nested writes and reads inside a write join it)
_tx = threading.local()


def _open():
    m = getattr(_tx, 'm', None)
    if m is None:
        m = _tx.m = {}
    return m


@functools.lru_cache(maxsize=1024)
def _q(sql):
    """text() once per SQL string (the statements here are fixed strings or a few filter combinations)."""
    return text(sql)


def _rows(c, sql, params=None):
    return c.execute(_q(sql), params or {}).mappings().all()


def _many(c, sql, rows):
    """executemany (psycopg 3 pipelines it); an empty list is a no-op, not a parameterless execute."""
    if rows:
        c.execute(_q(sql), rows)


def _seq(t, k):
    """created_at of the k-th row of one batch: insertion order survives an ORDER BY created_at (SQLite kept it
    through rowid); 1 µs steps stay distinct at epoch magnitudes (the double's resolution there is ~0.24 µs)."""
    return t + k * 1e-6


ANN_FIELDS = ('id', 'task_id', 'class_id', 'geometry_type', 'geometry', 'attributes', 'parent_id', 'z_order',
              'version', 'score', 'source')


class Conflict(Exception):
    """A PUT based on a stale version: carries what the server has now so the page can reload it."""

    def __init__(self, version, annotations):
        super().__init__('this image was changed in another tab or window; reload it to see the latest shapes')
        self.version = version
        self.annotations = annotations


class InUse(Exception):
    """A class that still has annotations was deleted without reassign_to / delete_annotations."""


def _now():
    return time.time()


def _dumps(v):
    return None if v is None else orjson.dumps(v, option=_SORT).decode()


def _loads(v):
    """JSONB arrives decoded; text (an old row, a raw value) is parsed."""
    return orjson.loads(v) if isinstance(v, (str, bytes)) else v


def new_id():
    return uuid.uuid4().hex


def _clamp(v):
    v = float(v)
    if v != v:                              # NaN from a broken client must not reach the export
        raise ValueError('a coordinate is not a number')
    return round(min(1.0, max(0.0, v)), 7)


def _pts(points, minimum, what, with_v=False):
    if not isinstance(points, list) or len(points) < minimum:
        raise ValueError(f'a {what} needs at least {minimum} points')
    out = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise ValueError(f'a {what} point must be [x, y{", v" if with_v else ""}]')
        if with_v:
            v = int(p[2]) if len(p) > 2 else 2
            if v not in (0, 1, 2):
                raise ValueError('keypoint visibility must be 0, 1 or 2')
            out.append([_clamp(p[0]), _clamp(p[1]), v])
        else:
            out.append([_clamp(p[0]), _clamp(p[1])])
    return out


def _extent(xy):
    xs, ys = [p[0] for p in xy], [p[1] for p in xy]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def normalize_geometry(gtype, g, task):
    """Validated, clamped geometry + its bbox_* extent (None for non-2D shapes). Raises ValueError."""
    if not isinstance(g, dict):
        raise ValueError('geometry must be an object')
    if gtype == 'bbox':
        try:
            x0, y0 = _clamp(g['x']), _clamp(g['y'])
            x1, y1 = _clamp(float(g['x']) + float(g['w'])), _clamp(float(g['y']) + float(g['h']))
        except (KeyError, TypeError):
            raise ValueError('a box needs x, y, w, h') from None
        x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            raise ValueError('a box has no area inside the image')
        out = {'x': x0, 'y': y0, 'w': round(x1 - x0, 7), 'h': round(y1 - y0, 7)}
        return out, (x0, y0, out['w'], out['h'])
    if gtype == 'polygon':
        out = {'points': _pts(g.get('points'), 3, 'polygon')}
        if g.get('holes'):
            out['holes'] = [_pts(h, 3, 'polygon hole') for h in g['holes']]
        return out, _extent(out['points'])
    if gtype == 'keypoints':
        pts = _pts(g.get('points'), 1, 'keypoint set', with_v=True)
        names = (task.get('config') or {}).get('keypoints') or []
        if names and len(pts) != len(names):
            raise ValueError(f'this task has {len(names)} keypoints, got {len(pts)}')
        lab = [p for p in pts if p[2] > 0]
        return {'points': pts}, (_extent(lab) if lab else None)
    if gtype == 'polyline':
        out = {'points': _pts(g.get('points'), 2, 'polyline')}
        return out, _extent(out['points'])
    if gtype == 'point':
        try:
            x, y = _clamp(g['x']), _clamp(g['y'])
        except (KeyError, TypeError):
            raise ValueError('a point needs x, y') from None
        return {'x': x, 'y': y}, (x, y, 0.0, 0.0)
    if gtype == 'tag':
        return {}, None
    if gtype == 'mask_rle':
        if not isinstance(g.get('size'), list) or len(g['size']) != 2 or not isinstance(g.get('counts'), (str, list)):
            raise ValueError('an RLE mask needs size [h, w] and counts')
        return {'size': [int(v) for v in g['size']], 'counts': g['counts']}, None
    if gtype in schema.GEOMETRY:                         # future modalities: stored as given
        return g, None
    raise ValueError(f'unknown geometry type {gtype!r}')


class ProjectStore:
    def __init__(self, pid):
        self.pid = str(pid)
        self._ids_cache = None              # ((count, max position, max created_at), [ids]) of this process

    # ------------------------------------------------------------ connections
    @contextmanager
    def write(self):
        """One write transaction under the project's advisory lock; joins the one already open on this thread."""
        opened = _open()
        c = opened.get(self.pid)
        if c is not None:                                  # nested call from inside a write(): join it
            yield c
            return
        with db.begin() as c:
            c.execute(_q('SELECT pg_advisory_xact_lock(hashtext(:p))'), {'p': self.pid})
            opened[self.pid] = c
            try:
                yield c
            finally:
                opened.pop(self.pid, None)

    @contextmanager
    def joined(self, c):
        """Run this store's writes inside the caller's transaction `c` (registry create / duplicate: one
        transaction for the session row, the project row and its tasks)."""
        opened = _open()
        if opened.get(self.pid) is not None:
            yield c
            return
        c.execute(_q('SELECT pg_advisory_xact_lock(hashtext(:p))'), {'p': self.pid})
        opened[self.pid] = c
        try:
            yield c
        finally:
            opened.pop(self.pid, None)

    @contextmanager
    def _read(self):
        c = _open().get(self.pid)
        if c is not None:                                  # inside a write: see its own rows
            yield c
            return
        with db.connect() as c:
            yield c

    def _stream(self):
        """A private connection for a streamed generator (server-side cursor, one snapshot for every cursor of
        it). The generator closes it in `finally`; job threads consume these."""
        c = db.engine().connect()
        c.execution_options(isolation_level='REPEATABLE READ')
        return c

    def close(self):
        """Nothing to close: connections are pooled per process (kept for callers of the SQLite store)."""

    # ------------------------------------------------------------ project row
    def init_project(self, pid, name, description='', meta=None):
        """The lbl.projects row (its app.sessions row must exist: lbl.projects.id references it)."""
        if pid != self.pid:
            raise ValueError('init_project: the id differs from the store\'s project id')
        t = _now()
        with self.write() as c:
            c.execute(_q('INSERT INTO lbl.projects(id, name, description, created_at, updated_at, meta) '
                         'VALUES (:p, :name, :d, :t, :t, CAST(:meta AS jsonb))'),
                      {'p': pid, 'name': name, 'd': description or '', 't': t, 'meta': _dumps(meta or {})})

    def update_project(self, **fields):
        sets = {k: v for k, v in fields.items() if k in ('name', 'description') and v is not None}
        sets['updated_at'] = _now()
        with self.write() as c:
            c.execute(_q(f"UPDATE lbl.projects SET {', '.join(f'{k} = :{k}' for k in sets)} WHERE id = :p"),
                      {**sets, 'p': self.pid})

    def project_row(self):
        with self._read() as c:
            r = c.execute(_q('SELECT * FROM lbl.projects WHERE id = :p'), {'p': self.pid}).mappings().first()
        return dict(r) if r else None

    # ------------------------------------------------------------ tasks
    def _task_row(self, c, tid, live=True):
        r = c.execute(_q('SELECT * FROM lbl.tasks WHERE id = :id AND project_id = :p'
                         + (' AND NOT is_deleted' if live else '')), {'id': tid, 'p': self.pid}).mappings().first()
        if r is None:
            raise KeyError(f'no task {tid!r} in this session')
        return r

    @staticmethod
    def _class_json(r):
        return {'id': r['id'], 'task_id': r['task_id'], 'name': r['name'], 'color': r['color'],
                'position': r['position'], 'hotkey': r['hotkey']}

    def tasks(self, include_deleted=False):
        """[Task] in position order, each with its live classes in position order."""
        with self._read() as c:
            rows = _rows(c, 'SELECT * FROM lbl.tasks WHERE project_id = :p'
                         + ('' if include_deleted else ' AND NOT is_deleted') + ' ORDER BY position, created_at, id',
                         {'p': self.pid})
            classes = {}
            for r in _rows(c, 'SELECT * FROM lbl.label_classes WHERE project_id = :p AND NOT is_deleted '
                              'ORDER BY position, name COLLATE "C"', {'p': self.pid}):
                classes.setdefault(r['task_id'], []).append(self._class_json(r))
        return [{'id': r['id'], 'name': r['name'], 'task_type': r['task_type'], 'position': r['position'],
                 'config': _loads(r['config']) or {}, 'classes': classes.get(r['id'], []),
                 **({'is_deleted': bool(r['is_deleted'])} if include_deleted else {})} for r in rows]

    def task(self, tid):
        for t in self.tasks():
            if t['id'] == tid:
                return t
        raise KeyError(f'no task {tid!r} in this session')

    @staticmethod
    def check_task(name, task_type, config):
        if not (name or '').strip():
            raise ValueError('a task needs a name')
        if task_type not in schema.TASK_TYPES:
            raise ValueError(f'unknown task type {task_type!r}')
        config = dict(config or {})
        if task_type == 'keypoints':
            names = config.get('keypoints') or []
            if not names or not all(isinstance(n, str) and n.strip() for n in names):
                raise ValueError('a keypoints task needs its keypoint names (config.keypoints)')
            k = len(names)
            if config.get('flip_idx') is not None and sorted(config['flip_idx']) != list(range(k)):
                raise ValueError(f'flip_idx must be a permutation of 0..{k - 1}')
            for e in config.get('skeleton') or []:
                if len(e) != 2 or not all(isinstance(i, int) and 0 <= i < k for i in e):
                    raise ValueError('skeleton edges must be [i, j] keypoint indexes')
        return name.strip(), config

    def add_task(self, name, task_type, config=None, classes=()):
        name, config = self.check_task(name, task_type, config)
        tid = new_id()
        with self.write() as c:
            pos = c.execute(_q('SELECT COALESCE(MAX(position) + 1, 0) FROM lbl.tasks WHERE project_id = :p'),
                            {'p': self.pid}).scalar()
            c.execute(_q('INSERT INTO lbl.tasks(id, project_id, name, task_type, position, config, created_at, '
                         'is_deleted) VALUES (:id, :p, :name, :tt, :pos, CAST(:cfg AS jsonb), :t, false)'),
                      {'id': tid, 'p': self.pid, 'name': name, 'tt': task_type, 'pos': pos, 'cfg': _dumps(config),
                       't': _now()})
            if classes:
                self.add_classes(tid, classes, strict=True)
        return self.task(tid)

    def update_task(self, tid, name=None, config=None):
        with self.write() as c:
            r = self._task_row(c, tid)
            if name is not None:
                if not name.strip():
                    raise ValueError('a task needs a name')
                c.execute(_q('UPDATE lbl.tasks SET name = :n WHERE id = :id AND project_id = :p'),
                          {'n': name.strip(), 'id': tid, 'p': self.pid})
            if config is not None:
                _, cfg = self.check_task(r['name'], r['task_type'], config)
                c.execute(_q('UPDATE lbl.tasks SET config = CAST(:cfg AS jsonb) WHERE id = :id AND project_id = :p'),
                          {'cfg': _dumps(cfg), 'id': tid, 'p': self.pid})
            return self.task(tid)

    def delete_task(self, tid):
        with self.write() as c:
            self._task_row(c, tid)
            c.execute(_q('UPDATE lbl.tasks SET is_deleted = true WHERE id = :id AND project_id = :p'),
                      {'id': tid, 'p': self.pid})

    def reorder_tasks(self, ids):
        with self.write() as c:
            live = set(c.execute(_q('SELECT id FROM lbl.tasks WHERE project_id = :p AND NOT is_deleted'),
                                 {'p': self.pid}).scalars())
            if set(ids) != live or len(ids) != len(live):
                raise ValueError('reorder needs every task id exactly once')
            _many(c, 'UPDATE lbl.tasks SET position = :pos WHERE id = :id AND project_id = :p',
                  [{'pos': i, 'id': t, 'p': self.pid} for i, t in enumerate(ids)])

    # ------------------------------------------------------------ classes
    def add_classes(self, tid, classes, strict=False):
        """Append classes ({name, color?} or str) to a task with auto colours; returns the new ones.
        strict: a duplicate name is an error (create); else duplicates of existing names are skipped (paste)."""
        from . import colors
        with self.write() as c:
            self._task_row(c, tid)
            rows = _rows(c, 'SELECT name, color, position FROM lbl.label_classes WHERE project_id = :p '
                            'AND task_id = :t AND NOT is_deleted', {'p': self.pid, 't': tid})
            names = {r['name'].casefold() for r in rows}
            used = [r['color'] for r in rows]
            pos = max([r['position'] for r in rows], default=-1) + 1
            new = []
            for cl in classes:
                cl = {'name': cl} if isinstance(cl, str) else dict(cl)
                name = (cl.get('name') or '').strip()
                if not name:
                    if strict:
                        raise ValueError('a class needs a name')
                    continue
                if name.casefold() in names:
                    if strict:
                        raise ValueError(f'class {name!r} is listed twice in this task')
                    continue
                color = (cl.get('color') or '').lower() or colors.next_color(used)
                if not re.fullmatch(r'#[0-9a-f]{6}', color):
                    raise ValueError(f'{color!r} is not a #rrggbb colour')
                new.append({'id': new_id(), 'task_id': tid, 'name': name, 'color': color, 'position': pos,
                            'hotkey': cl.get('hotkey')})
                names.add(name.casefold())
                used.append(color)
                pos += 1
            _many(c, 'INSERT INTO lbl.label_classes(id, project_id, task_id, name, color, position, hotkey, '
                     'is_deleted) VALUES (:id, :p, :task_id, :name, :color, :position, :hotkey, false)',
                  [{**n, 'p': self.pid} for n in new])
        return new

    def _class_row(self, c, cid):
        r = c.execute(_q('SELECT * FROM lbl.label_classes WHERE id = :id AND project_id = :p AND NOT is_deleted'),
                      {'id': cid, 'p': self.pid}).mappings().first()
        if r is None:
            raise KeyError(f'no class {cid!r} in this session')
        return r

    def update_class(self, cid, name=None, color=None, hotkey=..., position=None):
        k = {'id': cid, 'p': self.pid}
        with self.write() as c:
            r = self._class_row(c, cid)
            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError('a class needs a name')
                dup = c.execute(_q('SELECT 1 FROM lbl.label_classes WHERE project_id = :p AND task_id = :t '
                                   'AND NOT is_deleted AND id <> :id AND lower(name) = lower(:n)'),
                                {**k, 't': r['task_id'], 'n': name}).first()
                if dup:
                    raise ValueError(f'this task already has a class {name!r}')
                c.execute(_q('UPDATE lbl.label_classes SET name = :n WHERE id = :id AND project_id = :p'),
                          {**k, 'n': name})
            if color is not None:
                color = color.lower()
                if not re.fullmatch(r'#[0-9a-f]{6}', color):
                    raise ValueError(f'{color!r} is not a #rrggbb colour')
                c.execute(_q('UPDATE lbl.label_classes SET color = :c WHERE id = :id AND project_id = :p'),
                          {**k, 'c': color})
            if hotkey is not ...:
                if hotkey is not None and not _HOTKEY.match(hotkey.lower()):
                    raise ValueError(f'{hotkey!r} is not a hotkey')
                c.execute(_q('UPDATE lbl.label_classes SET hotkey = :h WHERE id = :id AND project_id = :p'),
                          {**k, 'h': hotkey and hotkey.lower()})
            if position is not None:
                ids = list(c.execute(_q('SELECT id FROM lbl.label_classes WHERE project_id = :p AND task_id = :t '
                                        'AND NOT is_deleted ORDER BY position, name COLLATE "C"'),
                                     {'p': self.pid, 't': r['task_id']}).scalars())
                ids.remove(cid)
                ids.insert(max(0, min(int(position), len(ids))), cid)
                _many(c, 'UPDATE lbl.label_classes SET position = :pos WHERE id = :id AND project_id = :p',
                      [{'pos': i, 'id': x, 'p': self.pid} for i, x in enumerate(ids)])
            r = self._class_row(c, cid)
            return self._class_json(r)

    def _unlink_children(self, c, ids, t, tid):
        """Live rows whose parent (a keypoints set's box) is among `ids`, just soft-deleted, lose the link:
        parent_id NULL, version + 1. Their image / task status versions bump too (open tabs reload), except in
        task `tid`, whose statuses the caller bumps itself. Returns the number of rows unlinked."""
        ids = list(ids)
        if not ids:
            return 0
        rows = c.execute(_q('SELECT id, media_id, task_id FROM lbl.annotations WHERE project_id = :p '
                            'AND parent_id = ANY(:ids) AND NOT is_deleted'), {'p': self.pid, 'ids': ids}).all()
        if not rows:
            return 0
        c.execute(_q('UPDATE lbl.annotations SET parent_id = NULL, version = version + 1, updated_at = :t '
                     'WHERE project_id = :p AND id = ANY(:ids)'), {'t': t, 'p': self.pid, 'ids': [x[0] for x in rows]})
        pairs = {(x[1], x[2]) for x in rows if x[2] != tid}
        _many(c, 'UPDATE lbl.media_task_status SET version = version + 1, updated_at = :t '
                 'WHERE project_id = :p AND media_id = :m AND task_id = :k',
              [{'t': t, 'p': self.pid, 'm': m, 'k': k} for m, k in pairs])
        return len(rows)

    def delete_class(self, cid, reassign_to=None, delete_annotations=False):
        with self.write() as c:
            r = self._class_row(c, cid)
            tid, t = r['task_id'], _now()
            k = {'p': self.pid, 'cid': cid, 'tid': tid, 't': t}
            n = c.execute(_q('SELECT COUNT(*) FROM lbl.annotations WHERE project_id = :p AND class_id = :cid '
                             'AND NOT is_deleted'), k).scalar()
            if n and not reassign_to and not delete_annotations:
                raise InUse(f'class {r["name"]!r} is used by {n} annotation{"s" * (n != 1)}: move them to another '
                            f'class or delete them with it')
            if n:
                # other tabs holding these images must reload (their base_version goes stale)
                c.execute(_q('UPDATE lbl.media_task_status SET version = version + 1, updated_at = :t, review = NULL '
                             'WHERE project_id = :p AND task_id = :tid AND media_id IN (SELECT media_id FROM '
                             'lbl.annotations WHERE project_id = :p AND class_id = :cid AND NOT is_deleted)'), k)
                if reassign_to:
                    to = self._class_row(c, reassign_to)
                    if to['task_id'] != tid or reassign_to == cid:
                        raise ValueError('reassign_to must be another class of the same task')
                    c.execute(_q('UPDATE lbl.annotations SET class_id = :to, version = version + 1, updated_at = :t '
                                 'WHERE project_id = :p AND class_id = :cid AND NOT is_deleted'), {**k, 'to': reassign_to})
                else:
                    gone = c.execute(_q('SELECT id, media_id FROM lbl.annotations WHERE project_id = :p '
                                        'AND class_id = :cid AND NOT is_deleted'), k).all()
                    media = list(dict.fromkeys(x[1] for x in gone))
                    c.execute(_q('UPDATE lbl.annotations SET is_deleted = true, version = version + 1, updated_at = :t '
                                 'WHERE project_id = :p AND class_id = :cid AND NOT is_deleted'), k)
                    self._unlink_children(c, [x[0] for x in gone], t, tid)
                    c.execute(_q('UPDATE lbl.media_task_status s SET n_annotations = (SELECT COUNT(*) FROM '
                                 'lbl.annotations a WHERE a.media_id = s.media_id AND a.task_id = s.task_id '
                                 'AND NOT a.is_deleted) WHERE s.project_id = :p AND s.task_id = :tid '
                                 'AND s.media_id = ANY(:mids)'), {**k, 'mids': media})
                    c.execute(_q("UPDATE lbl.media_task_status SET status = 'todo' WHERE project_id = :p "
                                 "AND task_id = :tid AND media_id = ANY(:mids) AND status = 'in_progress' "
                                 'AND n_annotations = 0'), {**k, 'mids': media})
            c.execute(_q('UPDATE lbl.label_classes SET is_deleted = true WHERE id = :cid AND project_id = :p'), k)
            # a model link mapping to this class would make every later link save fail: map it to 'ignore'
            cfg = _loads(self._task_row(c, tid, live=False)['config']) or {}
            cmap = (cfg.get('model') or {}).get('class_map') or {}
            if cid in cmap.values():
                cfg['model']['class_map'] = {kk: 'ignore' if v == cid else v for kk, v in cmap.items()}
                c.execute(_q('UPDATE lbl.tasks SET config = CAST(:cfg AS jsonb) WHERE id = :tid AND project_id = :p'),
                          {**k, 'cfg': _dumps(cfg)})
        return n

    # ------------------------------------------------------------ media
    def add_media(self, rows):
        """Batch insert media rows (dicts of media columns; id / position / project_id filled in).
        Rows whose file_name already exists in this project are skipped (the unique index). Returns the number
        inserted."""
        if not rows:
            return 0
        cols = _COLS['media']
        jcols = _JSON_COLS['media']
        t = _now()
        count = 'SELECT COUNT(*) FROM lbl.media WHERE project_id = :p'
        with self.write() as c:
            pos = c.execute(_q('SELECT COALESCE(MAX(position) + 1, 0) FROM lbl.media WHERE project_id = :p'),
                            {'p': self.pid}).scalar()
            before = c.execute(_q(count), {'p': self.pid}).scalar()
            vals = []
            for r in rows:
                r = dict(r)
                r.setdefault('id', new_id())
                r['project_id'] = self.pid
                r.setdefault('media_type', 'image')
                r.setdefault('created_at', t)
                if r.get('position') is None:
                    r['position'] = pos
                    pos += 1
                vals.append({k: _dumps(r.get(k)) if k in jcols else r.get(k) for k in cols})
            ph = ', '.join(f'CAST(:{k} AS jsonb)' if k in jcols else f':{k}' for k in cols)
            # writers of this project are serialised by the advisory lock, so the count difference is ours
            _many(c, f"INSERT INTO lbl.media({', '.join(cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING", vals)
            n = c.execute(_q(count), {'p': self.pid}).scalar() - before
        self._ids_cache = None
        return n

    def media_count(self):
        with self._read() as c:
            return c.execute(_q('SELECT COUNT(*) FROM lbl.media WHERE project_id = :p'), {'p': self.pid}).scalar()

    def media_ids(self):
        """Every media id in position order (navigation). Cached while the project's media (insert-only) keep
        the same count / last position / last insert time, so an import by another process is seen."""
        with self._read() as c:
            key = tuple(c.execute(_q('SELECT COUNT(*), MAX(position), MAX(created_at) FROM lbl.media '
                                     'WHERE project_id = :p'), {'p': self.pid}).one())
            hit = self._ids_cache
            if hit is not None and hit[0] == key:
                return hit[1]
            ids = list(c.execute(_q('SELECT id FROM lbl.media WHERE project_id = :p ORDER BY position, id'),
                                 {'p': self.pid}).scalars())
        self._ids_cache = (key, ids)
        return ids

    def cover(self, n):
        """The first n media ids in position order (the card's cover)."""
        with self._read() as c:
            return list(c.execute(_q('SELECT id FROM lbl.media WHERE project_id = :p ORDER BY position, id '
                                     'LIMIT :n'), {'p': self.pid, 'n': int(n)}).scalars())

    def media_names(self):
        """{file_name: uri} of every media (import uses it to skip known files and keep names unique)."""
        with self._read() as c:
            return {r[0]: r[1] for r in c.execute(_q('SELECT file_name, uri FROM lbl.media WHERE project_id = :p'),
                                                  {'p': self.pid})}

    def media_row(self, mid):
        with self._read() as c:
            r = c.execute(_q('SELECT * FROM lbl.media WHERE id = :id AND project_id = :p'),
                          {'id': mid, 'p': self.pid}).mappings().first()
        if r is None:
            raise KeyError(f'no image {mid!r} in this session')
        return dict(r)

    def media_rows(self, ids):
        """{id: row} for a few ids (prefetch)."""
        if not ids:
            return {}
        with self._read() as c:
            return {r['id']: dict(r) for r in _rows(c, 'SELECT * FROM lbl.media WHERE project_id = :p '
                                                       'AND id = ANY(:ids)', {'p': self.pid, 'ids': list(ids)})}

    def _statuses(self, c, ids, tids):
        st = {m: ({t: 'todo' for t in tids}, {t: 0 for t in tids}, {t: None for t in tids}, {}) for m in ids}
        if not ids:
            return st
        for r in c.execute(_q('SELECT media_id, task_id, status, n_annotations, model_run_at, review '
                              'FROM lbl.media_task_status WHERE project_id = :p AND media_id = ANY(:ids)'),
                           {'p': self.pid, 'ids': list(ids)}):
            s = st[r[0]]
            if r[1] in s[0]:
                s[0][r[1]], s[1][r[1]], s[2][r[1]] = r[2], r[3], r[4]
                if r[5] and r[2] == 'done':
                    s[3][r[1]] = r[5]
        return st

    @staticmethod
    def _media_json(r, st):
        return {'id': r['id'], 'file_name': r['file_name'], 'width': r['width'], 'height': r['height'],
                'position': r['position'], 'status': st[0], 'n': st[1], 'model_run': st[2], 'review': st[3]}

    def _live_tids(self, c):
        return list(c.execute(_q('SELECT id FROM lbl.tasks WHERE project_id = :p AND NOT is_deleted '
                                 'ORDER BY position, created_at, id'), {'p': self.pid}).scalars())

    def media_page(self, offset=0, limit=200, task_id=None, status=None, q=None, cls=None, cls_mode='any',
                   min_ann=None, max_ann=None, review=None):
        """{total, items:[Media]} in position order, optionally only the images with `status` in `task_id`.
        The image strip's filter (any combination; None = not set):
          q        file name: plain text = substring, with * / ? = wildcard (on the whole name or its base name),
                   /pattern/ = regular expression (Python, case-insensitive; applied after the SQL filters)
          cls      class ids the image must have: cls_mode 'any' (one of them), 'all' (each) or 'none' (none of them)
          min_ann / max_ann  live annotation count of `task_id` (of every task without one)
          review   with `task_id`: 'none' (done, not reviewed), 'manual' or 'bulk'"""
        if review is not None and review not in ('none', *schema.REVIEWS):
            raise ValueError(f'unknown review filter {review!r}')
        q = (q or '').strip()
        cls = [x for x in (cls or ()) if x]
        if q or cls or min_ann is not None or max_ann is not None or (review and task_id):
            return self._media_filtered(offset, limit, task_id, status, q, cls, cls_mode, min_ann, max_ann, review)
        cols = 'm.id, m.file_name, m.width, m.height, m.position'
        a = {'p': self.pid, 'lim': int(limit), 'off': int(offset)}
        with self._read() as c:
            tids = self._live_tids(c)
            n_media = c.execute(_q('SELECT COUNT(*) FROM lbl.media WHERE project_id = :p'), a).scalar()
            if task_id and status:
                if status not in schema.STATUSES:
                    raise ValueError(f'unknown status {status!r}')
                a['tid'], a['st'] = task_id, status
                if status == 'todo':
                    # todo = no status row, or a 'todo' one: walk the media order, skip the touched ones
                    n_other = c.execute(_q("SELECT COUNT(*) FROM lbl.media_task_status WHERE project_id = :p "
                                           "AND task_id = :tid AND status <> 'todo'"), a).scalar()
                    total = n_media - n_other
                    rows = _rows(c, f'SELECT {cols} FROM lbl.media m WHERE m.project_id = :p AND NOT EXISTS ('
                                    'SELECT 1 FROM lbl.media_task_status s WHERE s.media_id = m.id AND s.task_id = :tid '
                                    "AND s.status <> 'todo') ORDER BY m.position, m.id LIMIT :lim OFFSET :off", a)
                else:
                    total = c.execute(_q('SELECT COUNT(*) FROM lbl.media_task_status WHERE project_id = :p '
                                         'AND task_id = :tid AND status = :st'), a).scalar()
                    rows = _rows(c, f'SELECT {cols} FROM lbl.media_task_status s JOIN lbl.media m ON m.id = s.media_id '
                                    'WHERE s.project_id = :p AND s.task_id = :tid AND s.status = :st '
                                    'ORDER BY m.position, m.id LIMIT :lim OFFSET :off', a)
            else:
                total = n_media
                rows = _rows(c, f'SELECT {cols} FROM lbl.media m WHERE m.project_id = :p '
                                'ORDER BY m.position, m.id LIMIT :lim OFFSET :off', a)
            st = self._statuses(c, [r['id'] for r in rows], tids)
        return {'total': total, 'items': [self._media_json(r, st[r['id']]) for r in rows]}

    @staticmethod
    def _like(pat):
        """An ILIKE pattern (ESCAPE '\\') from a user wildcard: * = any run, ? = one character, the rest literal."""
        out = []
        for ch in pat:
            out.append('%' if ch == '*' else '_' if ch == '?' else '\\' + ch if ch in '%_\\' else ch)
        return ''.join(out)

    def _media_filtered(self, offset, limit, task_id, status, q, cls, cls_mode, min_ann, max_ann, review=None):
        """media_page with the strip filter: one indexed SQL pass (status / name / class / count), a regex on the
        names it returns. Every predicate is a correlated EXISTS / scalar subquery on (media_id[, task_id]) indexes,
        so the cost follows the media count, not the annotation count."""
        args = {'p': self.pid}

        def arg(v):                                     # a bound parameter (never user text inside the SQL)
            k = f'a{len(args)}'
            args[k] = v
            return f':{k}'

        where = ['m.project_id = :p']
        if task_id and status:
            if status not in schema.STATUSES:
                raise ValueError(f'unknown status {status!r}')
            if status == 'todo':
                where.append('NOT EXISTS (SELECT 1 FROM lbl.media_task_status s WHERE s.media_id = m.id '
                             f"AND s.task_id = {arg(task_id)} AND s.status <> 'todo')")
            else:
                where.append('EXISTS (SELECT 1 FROM lbl.media_task_status s WHERE s.media_id = m.id '
                             f'AND s.task_id = {arg(task_id)} AND s.status = {arg(status)})')
        if review and task_id:
            cond = "s.status = 'done' AND s.review IS NULL" if review == 'none' else f's.review = {arg(review)}'
            where.append(f'EXISTS (SELECT 1 FROM lbl.media_task_status s WHERE s.media_id = m.id '
                         f'AND s.task_id = {arg(task_id)} AND {cond})')
        rx = None
        if len(q) >= 2 and q[0] == '/' and q[-1] == '/':
            if len(q) > 502:
                raise ValueError('the regular expression is too long')
            if _NESTED_QUANT.search(q[1:-1]):
                raise ValueError('bad regular expression: nested repetition (like (a+)+) can take forever, simplify it')
            try:
                rx = (_regex.compile(q[1:-1], _regex.I | _regex.V0) if _regex is not None else re.compile(q[1:-1], re.I))
            except (re.error, getattr(_regex, 'error', re.error)) as e:
                raise ValueError(f'bad regular expression: {e}') from None
        elif q:
            if '*' in q or '?' in q:
                pat = self._like(q)
                where.append(f"(m.file_name ILIKE {arg(pat)} ESCAPE '\\' OR m.file_name ILIKE {arg('%/' + pat)} "
                             "ESCAPE '\\')")
            else:
                where.append(f"m.file_name ILIKE {arg('%' + self._like(q.replace('*', '').replace('?', '')) + '%')} "
                             "ESCAPE '\\'")
        if cls:
            if cls_mode not in ('any', 'all', 'none'):
                raise ValueError(f'unknown class mode {cls_mode!r}')
            has = ('EXISTS (SELECT 1 FROM lbl.annotations a WHERE a.media_id = m.id AND NOT a.is_deleted '
                   'AND a.class_id {})')
            if cls_mode == 'all':
                for cid in cls:
                    where.append(has.format(f'= {arg(cid)}'))
            else:
                where.append(('NOT ' if cls_mode == 'none' else '') + has.format(f'= ANY({arg(list(cls))})'))
        if min_ann is not None or max_ann is not None:
            if task_id:
                n = ('COALESCE((SELECT s.n_annotations FROM lbl.media_task_status s WHERE s.media_id = m.id '
                     f'AND s.task_id = {arg(task_id)}), 0)')
            else:
                n = '(SELECT COUNT(*) FROM lbl.annotations a WHERE a.media_id = m.id AND NOT a.is_deleted)'
            if min_ann is not None:
                where.append(f'{n} >= {arg(int(min_ann))}')
            if max_ann is not None:
                where.append(f'{n} <= {arg(int(max_ann))}')
        sql_where = ' WHERE ' + ' AND '.join(where)
        cols = 'm.id, m.file_name, m.width, m.height, m.position'
        with self._read() as c:
            if rx is None:
                total = c.execute(text(f'SELECT COUNT(*) FROM lbl.media m{sql_where}'), args).scalar()
                rows = c.execute(text(f'SELECT {cols} FROM lbl.media m{sql_where} ORDER BY m.position, m.id '
                                      'LIMIT :lim OFFSET :off'),
                                 {**args, 'lim': int(limit), 'off': int(offset)}).mappings().all()
            else:
                every = c.execute(text(f'SELECT {cols} FROM lbl.media m{sql_where} ORDER BY m.position, m.id'),
                                  args).mappings().all()
                budget = REGEX_BUDGET_S + 5e-6 * len(every)
                deadline = time.monotonic() + budget
                slow = ValueError('bad regular expression: it takes too long over the image names, simplify it')
                # regex: one search can never run past the budget, and releases the GIL while it runs (concurrent)
                search = (lambda s: rx.search(s, timeout=budget, concurrent=True)) if _regex is not None else rx.search
                hit = []
                try:
                    for k, r in enumerate(every):
                        if search(r['file_name'][:_REGEX_NAME_MAX]):
                            hit.append(r)
                        if not k & 1023 and time.monotonic() > deadline:
                            raise slow
                except TimeoutError:
                    raise slow from None
                total = len(hit)
                rows = hit[int(offset):int(offset) + int(limit)]
            st = self._statuses(c, [r['id'] for r in rows], self._live_tids(c))
        return {'total': total, 'items': [self._media_json(r, st[r['id']]) for r in rows]}

    def media_item(self, mid):
        with self._read() as c:
            r = c.execute(_q('SELECT id, file_name, width, height, position FROM lbl.media WHERE id = :id '
                             'AND project_id = :p'), {'id': mid, 'p': self.pid}).mappings().first()
            if r is None:
                raise KeyError(f'no image {mid!r} in this session')
            return self._media_json(r, self._statuses(c, [mid], self._live_tids(c))[mid])

    # ------------------------------------------------------------ annotations
    @staticmethod
    def _ann_json(r):
        return {'id': r['id'], 'task_id': r['task_id'], 'class_id': r['class_id'],
                'geometry_type': r['geometry_type'], 'geometry': _loads(r['geometry']),
                'attributes': _loads(r['attributes']) or {}, 'parent_id': r['parent_id'], 'z_order': r['z_order'],
                'version': r['version'], 'score': r['score'], 'source': r['source']}

    def _live_anns(self, c, mid, tid=None):
        q = ('SELECT a.* FROM lbl.annotations a JOIN lbl.tasks t ON t.id = a.task_id AND NOT t.is_deleted '
             'WHERE a.project_id = :p AND a.media_id = :m AND NOT a.is_deleted')
        args = {'p': self.pid, 'm': mid}
        if tid:
            q += ' AND a.task_id = :t'
            args['t'] = tid
        return [self._ann_json(r) for r in _rows(c, q + ' ORDER BY a.z_order, a.created_at, a.id', args)]

    def live_annotations(self, mid, tid=None):
        """[Ann] live on image `mid` (of task `tid`, else of every live task)."""
        with self._read() as c:
            return self._live_anns(c, mid, tid)

    def get_annotations(self, mid):
        """{media, versions:{tid: int}, annotations:[Ann]} for every live task."""
        with self._read() as c:
            media = self.media_item(mid)
            versions = {t: 0 for t in media['status']}
            for r in c.execute(_q('SELECT task_id, version FROM lbl.media_task_status WHERE project_id = :p '
                                  'AND media_id = :m'), {'p': self.pid, 'm': mid}):
                if r[0] in versions:
                    versions[r[0]] = r[1]
            return {'media': media, 'versions': versions, 'annotations': self._live_anns(c, mid)}

    def _media_exists(self, c, mid):
        if c.execute(_q('SELECT 1 FROM lbl.media WHERE id = :m AND project_id = :p'),
                     {'m': mid, 'p': self.pid}).first() is None:
            raise KeyError(f'no image {mid!r} in this session')

    def _class_ids(self, c, tid):
        return set(c.execute(_q('SELECT id FROM lbl.label_classes WHERE project_id = :p AND task_id = :t '
                                'AND NOT is_deleted'), {'p': self.pid, 't': tid}).scalars())

    def put_annotations(self, mid, tid, rows, base_version, user=None):
        """Replace the live set of task `tid` on image `mid` with `rows` in one transaction (insert new ids,
        update changed ones with version + 1, soft-delete missing ones). Raises Conflict on a stale
        base_version, ValueError on invalid rows, KeyError on unknown image / task.
        user: the editing user's id (annotations.created_by of new rows)."""
        t = _now()
        with self.write() as c:
            task = self._task_row(c, tid)
            ttype = task['task_type']
            cfg = _loads(task['config']) or {}
            self._media_exists(c, mid)
            st = c.execute(_q('SELECT status, version FROM lbl.media_task_status WHERE project_id = :p '
                              'AND media_id = :m AND task_id = :t'), {'p': self.pid, 'm': mid, 't': tid}).first()
            cur_status, cur_version = (st[0], st[1]) if st else ('todo', 0)
            if int(base_version) != cur_version:
                raise Conflict(cur_version, self._live_anns(c, mid, tid))
            class_ids = self._class_ids(c, tid)
            allowed = TASK_GEOMS.get(ttype, (schema.TASK_GEOMETRY.get(ttype),))
            task_d = {'config': cfg}
            new, seen = {}, set()
            for a in rows:
                aid = a.get('id') or ''
                if not _ID.match(aid):
                    raise ValueError(f'annotation id {aid!r} is not a hex id')
                if aid in seen:
                    raise ValueError(f'annotation id {aid} is listed twice')
                seen.add(aid)
                gtype = a.get('geometry_type') or allowed[0]
                if gtype not in allowed:
                    raise ValueError(f'a {ttype.replace("_", " ")} task cannot hold {gtype} shapes')
                if a.get('class_id') not in class_ids:
                    raise ValueError('a shape uses a class that is not in this task (or was deleted)')
                geom, ext = normalize_geometry(gtype, a.get('geometry') if gtype != 'tag' else {}, task_d)
                src = a.get('source') or 'manual'
                if src not in schema.SOURCES:
                    raise ValueError(f'unknown source {src!r}')
                score = a.get('score')
                new[aid] = {'class_id': a['class_id'], 'geometry_type': gtype, 'geometry': geom,
                            'attributes': a.get('attributes') or {}, 'parent_id': a.get('parent_id'),
                            'z_order': int(a.get('z_order') or 0), 'score': None if score is None else float(score),
                            'source': src, 'ext': ext or (None, None, None, None)}
            if ttype == 'classification' and not cfg.get('multi_label'):
                if len(new) > 1:
                    raise ValueError('this classification task takes one class per image')
            old = {r['id']: r for r in _rows(c, 'SELECT id, class_id, geometry_type, geometry, attributes, parent_id, '
                                                'z_order, score, source, is_deleted FROM lbl.annotations '
                                                'WHERE project_id = :p AND media_id = :m AND task_id = :t',
                                             {'p': self.pid, 'm': mid, 't': tid})}
            ins, upd = [], []
            for aid, n in new.items():
                o = old.get(aid)
                # a stored model shape that comes back moved / resized / reclassified is the user's now (the page
                # flips it in its edit command; this is the safety net). A stored manual row arriving as 'model'
                # is the page's undo of that edit and is kept as sent.
                if n['source'] == 'model' and o is not None and o['source'] == 'model' and \
                        (n['class_id'] != o['class_id'] or n['geometry'] != o['geometry']):
                    n['source'], n['score'] = 'manual', None
                key = (n['class_id'], n['geometry_type'], n['geometry'], n['attributes'], n['parent_id'],
                       n['z_order'], n['score'], n['source'])
                x, y, w, h = n['ext']
                vals = {'class_id': n['class_id'], 'gt': n['geometry_type'], 'g': _dumps(n['geometry']),
                        'x': x, 'y': y, 'w': w, 'h': h, 'attrs': _dumps(n['attributes']), 'score': n['score'],
                        'src': n['source'], 'parent': n['parent_id'], 'z': n['z_order'], 't': t, 'id': aid,
                        'p': self.pid}
                if o is None:
                    ins.append({**vals, 'task': tid, 'm': mid, 'user': user, 'tc': _seq(t, len(ins))})
                elif o['is_deleted'] or key != (o['class_id'], o['geometry_type'], o['geometry'],
                                                o['attributes'] or {}, o['parent_id'], o['z_order'], o['score'],
                                                o['source']):
                    upd.append(vals)
            dels = [aid for aid, o in old.items() if aid not in new and not o['is_deleted']]
            if ins:
                try:
                    _many(c, 'INSERT INTO lbl.annotations(id, project_id, task_id, media_id, class_id, geometry_type, '
                             'geometry, bbox_x, bbox_y, bbox_w, bbox_h, attributes, score, source, parent_id, '
                             'z_order, created_by, created_at, updated_at, version, is_deleted) VALUES (:id, :p, '
                             ':task, :m, :class_id, :gt, CAST(:g AS jsonb), :x, :y, :w, :h, CAST(:attrs AS jsonb), '
                             ':score, :src, :parent, :z, :user, :tc, :t, 1, false)', ins)
                except IntegrityError as e:
                    if isinstance(e.orig, pg_errors.UniqueViolation):
                        raise ValueError('an annotation id is already used by another image or task') from None
                    raise
            _many(c, 'UPDATE lbl.annotations SET class_id = :class_id, geometry_type = :gt, '
                     'geometry = CAST(:g AS jsonb), bbox_x = :x, bbox_y = :y, bbox_w = :w, bbox_h = :h, '
                     'attributes = CAST(:attrs AS jsonb), score = :score, source = :src, parent_id = :parent, '
                     'z_order = :z, updated_at = :t, version = version + 1, is_deleted = false '
                     'WHERE id = :id AND project_id = :p', upd)
            if dels:
                c.execute(_q('UPDATE lbl.annotations SET is_deleted = true, version = version + 1, updated_at = :t '
                             'WHERE project_id = :p AND id = ANY(:ids)'), {'t': t, 'p': self.pid, 'ids': dels})
            n = len(new)
            if not (ins or upd or dels) and st is not None:
                return {'version': cur_version, 'status': cur_status, 'n': n}
            status = cur_status if cur_status == 'done' or (cur_status == 'skipped' and not n) else \
                ('in_progress' if n else 'todo')
            version = cur_version + 1
            c.execute(_q('INSERT INTO lbl.media_task_status(media_id, task_id, project_id, status, n_annotations, '
                         'version, updated_at) VALUES (:m, :t, :p, :st, :n, :v, :now) ON CONFLICT (media_id, task_id) '
                         'DO UPDATE SET status = EXCLUDED.status, n_annotations = EXCLUDED.n_annotations, '
                         'version = EXCLUDED.version, updated_at = EXCLUDED.updated_at, review = NULL'),
                      {'m': mid, 't': tid, 'p': self.pid, 'st': status, 'n': n, 'v': version, 'now': t})
        return {'version': version, 'status': status, 'n': n}

    def set_status(self, mid, task_ids, action, user=None):
        """commit -> done, skip -> skipped, reopen -> in_progress (or todo with no shapes). {tid: status}.
        user: the committing user's id (media_task_status.committed_by)."""
        t = _now()
        out = {}
        with self.write() as c:
            self._media_exists(c, mid)
            for tid in task_ids:
                self._task_row(c, tid)
                n = c.execute(_q('SELECT COUNT(*) FROM lbl.annotations WHERE project_id = :p AND media_id = :m '
                                 'AND task_id = :t AND NOT is_deleted'), {'p': self.pid, 'm': mid, 't': tid}).scalar()
                status = {'commit': 'done', 'skip': 'skipped'}.get(action) or ('in_progress' if n else 'todo')
                done = action == 'commit'
                c.execute(_q('INSERT INTO lbl.media_task_status(media_id, task_id, project_id, status, n_annotations, '
                             'version, committed_by, committed_at, updated_at) VALUES (:m, :t, :p, :st, :n, 0, :by, '
                             ':cat, :now) ON CONFLICT (media_id, task_id) DO UPDATE SET status = EXCLUDED.status, '
                             'n_annotations = EXCLUDED.n_annotations, updated_at = EXCLUDED.updated_at'
                             + (', committed_by = EXCLUDED.committed_by, committed_at = EXCLUDED.committed_at'
                                if done else ', review = NULL')),
                          {'m': mid, 't': tid, 'p': self.pid, 'st': status, 'n': n, 'by': user if done else None,
                           'cat': t if done else None, 'now': t})
                out[tid] = status
        return out

    def set_review(self, task_ids, mode, media_ids=None):
        """The optional review of done images: 'manual' (a person approved them: also replaces a 'bulk' mark),
        'bulk' (Review all: only images not reviewed yet, so a manual mark is never downgraded) or 'clear'.
        media_ids None = every image of the tasks. Only done images are reviewed. {tid: images changed}."""
        if mode not in (*schema.REVIEWS, 'clear'):
            raise ValueError(f'unknown review mode {mode!r}')
        t = _now()
        out = {}
        with self.write() as c:
            for tid in task_ids:
                self._task_row(c, tid)
                val = None if mode == 'clear' else mode
                cond = {'clear': 'review IS NOT NULL', 'manual': "status = 'done' AND COALESCE(review, '') <> 'manual'",
                        'bulk': "status = 'done' AND review IS NULL"}[mode]
                q = f'UPDATE lbl.media_task_status SET review = :v, updated_at = :t WHERE project_id = :p ' \
                    f'AND task_id = :tid AND {cond}'
                a = {'v': val, 't': t, 'p': self.pid, 'tid': tid}
                if media_ids is not None:
                    q += ' AND media_id = ANY(:ids)'
                    a['ids'] = list(media_ids)
                out[tid] = c.execute(_q(q), a).rowcount
        return out

    # ------------------------------------------------------------ model predictions
    def apply_predictions(self, mid, tid, preds, replace_model=True):
        """Store one image's model output for task `tid` (see apply_predictions_many). Returns
        {version, annotations: the task's full live set on that image, n_model}."""
        res = self.apply_predictions_many(tid, [(mid, preds)], replace_model)[mid]
        return {'version': res['version'], 'annotations': self.live_annotations(mid, tid),
                'n_model': res['n_model']}

    def _status_rows(self, c, tid, mids):
        """{media_id: (status, version, n_annotations)} of task `tid` on `mids`."""
        if not mids:
            return {}
        return {r[0]: (r[1], r[2], r[3] or 0) for r in c.execute(
            _q('SELECT media_id, status, version, n_annotations FROM lbl.media_task_status WHERE project_id = :p '
               'AND task_id = :t AND media_id = ANY(:ids)'), {'p': self.pid, 't': tid, 'ids': list(mids)})}

    def _live_counts(self, c, tid, mids):
        if not mids:
            return {}
        return dict(c.execute(_q('SELECT media_id, COUNT(*) FROM lbl.annotations WHERE project_id = :p '
                                 'AND task_id = :t AND NOT is_deleted AND media_id = ANY(:ids) GROUP BY media_id'),
                              {'p': self.pid, 't': tid, 'ids': list(mids)}).all())

    def _soft_delete(self, c, tid, mids, source_cond, t):
        """Soft-delete the live rows of task `tid` on `mids` matching `source_cond` and unlink their children."""
        if not mids:
            return
        gone = list(c.execute(_q('UPDATE lbl.annotations SET is_deleted = true, version = version + 1, '
                                 'updated_at = :t WHERE project_id = :p AND task_id = :tid AND media_id = ANY(:ids) '
                                 f'AND {source_cond} AND NOT is_deleted RETURNING id'),
                              {'t': t, 'p': self.pid, 'tid': tid, 'ids': list(mids)}).scalars())
        self._unlink_children(c, gone, t, tid)

    _INSERT_ANN = ('INSERT INTO lbl.annotations(id, project_id, task_id, media_id, class_id, geometry_type, geometry, '
                   'bbox_x, bbox_y, bbox_w, bbox_h, attributes, score, source, parent_id, z_order, created_at, '
                   'updated_at, version, is_deleted) VALUES (:id, :p, :tid, :m, :cid, :gt, CAST(:g AS jsonb), :x, :y, '
                   ':w, :h, CAST(:attrs AS jsonb), :score, :src, NULL, :z, :tc, :t, 1, false)')

    _UPSERT_STATUS = ('INSERT INTO lbl.media_task_status(media_id, task_id, project_id, status, n_annotations, '
                      'version, updated_at{extra_cols}) VALUES (:m, :tid, :p, :st, :n, :v, :t{extra_vals}) '
                      'ON CONFLICT (media_id, task_id) DO UPDATE SET status = EXCLUDED.status, '
                      'n_annotations = EXCLUDED.n_annotations, version = EXCLUDED.version, '
                      'updated_at = EXCLUDED.updated_at{extra_set}')

    def apply_predictions_many(self, tid, items, replace_model=True, skip_done=False, scope=None):
        """items: [(media_id, [{class_id, score, geometry_type, geometry, attributes?}])], one transaction:
        soft-delete each image's current source='model' rows of this task (replace_model), insert the new ones
        with source='model' + score, bump the status version (open tabs get a 409 and reload), set
        model_run_at, todo -> in_progress. Manual rows are never touched. Invalid predictions are dropped.
        scope: the label-all job's predict_targets scope, checked again here because the images may have
        changed since the job listed them: 'unlabeled' leaves images that now have shapes or are done /
        skipped, 'not_done' leaves done ones, 'all' / None leaves none. skip_done is scope='not_done'.
        Returns {media_id: {version, n_model, status}} (skipped / unknown images are absent)."""
        if scope not in (None, 'unlabeled', 'not_done', 'all'):
            raise ValueError(f"unknown scope {scope!r} (unlabeled, not_done or all)")
        if scope is None and skip_done:
            scope = 'not_done'
        t = _now()
        out = {}
        with self.write() as c:
            task = self._task_row(c, tid)
            ttype = task['task_type']
            cfg = _loads(task['config']) or {}
            task_d = {'config': cfg}
            allowed = TASK_GEOMS.get(ttype, (schema.TASK_GEOMETRY.get(ttype),))
            class_ids = self._class_ids(c, tid)
            single_tag = ttype == 'classification' and not cfg.get('multi_label')
            mids = [m for m, _ in items]
            known = set(c.execute(_q('SELECT id FROM lbl.media WHERE project_id = :p AND id = ANY(:ids)'),
                                  {'p': self.pid, 'ids': mids}).scalars()) if mids else set()
            cur = self._status_rows(c, tid, mids)
            picked = set()
            if single_tag and mids:              # images where the user already picked the class
                picked = set(c.execute(_q("SELECT DISTINCT media_id FROM lbl.annotations WHERE project_id = :p "
                                          "AND task_id = :t AND media_id = ANY(:ids) AND NOT is_deleted "
                                          "AND source <> 'model'"), {'p': self.pid, 't': tid, 'ids': mids}).scalars())
            ins, dels, todo = [], [], []
            for mid, preds in items:
                if mid not in known:
                    continue
                status, _, n_now = cur.get(mid, ('todo', 0, 0))
                if (scope == 'not_done' and status == 'done') or \
                        (scope == 'unlabeled' and (n_now or status in ('done', 'skipped'))):
                    continue
                rows = []
                for p in preds:
                    gtype = p.get('geometry_type') or allowed[0]
                    if gtype not in allowed or p.get('class_id') not in class_ids:
                        continue
                    try:
                        geom, ext = normalize_geometry(gtype, p.get('geometry') if gtype != 'tag' else {}, task_d)
                    except ValueError:
                        continue
                    score = p.get('score')
                    rows.append((score, gtype, geom, ext, p['class_id'], p.get('attributes') or {}))
                if single_tag and rows:
                    rows = [max(rows, key=lambda r: r[0] or 0)]
                if single_tag and mid in picked:
                    rows = []                    # one class per image, and the user already picked it
                if replace_model:
                    dels.append(mid)
                for z, (score, gtype, geom, ext, cid, attrs) in enumerate(rows):
                    x, y, w, h = ext or (None,) * 4
                    ins.append({'id': new_id(), 'p': self.pid, 'tid': tid, 'm': mid, 'cid': cid, 'gt': gtype,
                                'g': _dumps(geom), 'x': x, 'y': y, 'w': w, 'h': h, 'attrs': _dumps(attrs),
                                'score': None if score is None else float(score), 'src': 'model', 'z': 0,
                                'tc': _seq(t, len(ins)), 't': t})
                todo.append((mid, len(rows)))
            self._soft_delete(c, tid, dels, "source = 'model'", t)
            _many(c, self._INSERT_ANN, ins)
            counts = self._live_counts(c, tid, [m for m, _ in todo])
            ups = []
            for mid, n_model in todo:
                n = counts.get(mid, 0)
                status, version, _ = cur.get(mid, ('todo', 0, 0))
                if status == 'todo' and n:
                    status = 'in_progress'
                elif status == 'in_progress' and not n:
                    status = 'todo'
                ups.append({'m': mid, 'tid': tid, 'p': self.pid, 'st': status, 'n': n, 'v': version + 1, 't': t,
                            'run': t})
                out[mid] = {'version': version + 1, 'n_model': n_model, 'status': status}
            _many(c, self._UPSERT_STATUS.format(extra_cols=', model_run_at', extra_vals=', :run',
                                                extra_set=', model_run_at = EXCLUDED.model_run_at'), ups)
        return out

    def media_for_import(self):
        """[{id, file_name, uri, width, height, meta}] of every media: the annotation import matches keys against it."""
        with self._read() as c:
            return [{'id': r[0], 'file_name': r[1], 'uri': r[2], 'width': r[3], 'height': r[4],
                     'meta': _loads(r[5]) or {}}
                    for r in c.execute(_q('SELECT id, file_name, uri, width, height, meta FROM lbl.media '
                                          'WHERE project_id = :p'), {'p': self.pid})]

    def import_annotations(self, tid, items, mode='merge', class_map=None, add_classes=True):
        """Existing annotations into task `tid`, one transaction: items = [(media_id, class name, geometry_type,
        geometry (normalised), score | None)].

        mode: 'merge' adds them next to what is there; 'replace' first soft-deletes the task's non-model shapes on
        every image the import touches; 'skip_labeled' leaves images that already have shapes or are done / skipped.
        class_map: {source class name: class id | 'new' | 'ignore'}; names it does not list match a task class of the
        same name (case-insensitive), else become new classes (add_classes) or are skipped.
        Rows get source='import'; status todo -> in_progress and the version bumps (open tabs reload).
        Returns {annotations, images, classes_added: [names], skipped: {reason: n}}."""
        if mode not in ('merge', 'replace', 'skip_labeled'):
            raise ValueError(f"unknown mode {mode!r} (merge, replace or skip_labeled)")
        class_map = class_map or {}
        t = _now()
        skipped = {}

        def skip(why):
            skipped[why] = skipped.get(why, 0) + 1
        task = self.task(tid)
        ttype = task['task_type']
        allowed = TASK_GEOMS.get(ttype, (schema.TASK_GEOMETRY.get(ttype),))
        by_name = {c['name'].casefold(): c['id'] for c in task['classes']}
        live = {c['id'] for c in task['classes']}
        resolved, wanted = {}, []
        for _, cname, *_ in items:                       # class per source name, once
            if cname in resolved:
                continue
            v = class_map.get(cname)
            if v == 'ignore':
                resolved[cname] = None
            elif v in live:
                resolved[cname] = v
            elif cname.casefold() in by_name:
                resolved[cname] = by_name[cname.casefold()]
            elif add_classes or v == 'new':
                resolved[cname] = ...                    # created below
                wanted.append(cname)
            else:
                resolved[cname] = None
        added = []
        if wanted:
            added = self.add_classes(tid, [w for w in wanted if not w.startswith('#')] +
                                     [f'class {w[1:]}' for w in wanted if w.startswith('#')])
            fresh = {c['name'].casefold(): c['id'] for c in added}
            for w in wanted:
                resolved[w] = fresh.get((f'class {w[1:]}' if w.startswith('#') else w).casefold()) or \
                    by_name.get(w.casefold())
        task_d = {'config': task.get('config') or {}}
        single_tag = ttype == 'classification' and not (task.get('config') or {}).get('multi_label')
        per = {}
        for mid, cname, gtype, geom, score in items:
            cid = resolved.get(cname)
            if cid is None:
                skip('class_ignored')
                continue
            if gtype not in allowed:
                skip('other_type')
                continue
            try:
                g, ext = normalize_geometry(gtype, geom if gtype != 'tag' else {}, task_d)
            except ValueError:
                skip('invalid')
                continue
            per.setdefault(mid, []).append((cid, gtype, g, ext, None if score is None else float(score)))
        n_ann = 0
        with self.write() as c:
            mids = list(per)
            # media ids of another project are never touched (the caller matched them against this one)
            known = set(c.execute(_q('SELECT id FROM lbl.media WHERE project_id = :p AND id = ANY(:ids)'),
                                  {'p': self.pid, 'ids': mids}).scalars()) if mids else set()
            mids = [m for m in mids if m in known]
            cur = self._status_rows(c, tid, mids)
            ins, dels, touched = [], [], []
            for mid in mids:
                status, _, n_now = cur.get(mid, ('todo', 0, 0))
                if mode == 'skip_labeled' and (n_now or status in ('done', 'skipped')):
                    skip('image_already_labeled')
                    continue
                rows = per[mid]
                if single_tag:
                    rows = [max(rows, key=lambda r: r[4] or 0)]
                if mode == 'replace':
                    dels.append(mid)
                for z, (cid, gtype, g, ext, score) in enumerate(rows):
                    x, y, w, h = ext or (None,) * 4
                    ins.append({'id': new_id(), 'p': self.pid, 'tid': tid, 'm': mid, 'cid': cid, 'gt': gtype,
                                'g': _dumps(g), 'x': x, 'y': y, 'w': w, 'h': h, 'attrs': _dumps({}), 'score': score,
                                'src': 'import', 'z': z, 'tc': _seq(t, len(ins)), 't': t})
                touched.append(mid)
            self._soft_delete(c, tid, dels, "source <> 'model'", t)
            _many(c, self._INSERT_ANN, ins)
            n_ann = len(ins)
            counts = self._live_counts(c, tid, touched)
            ups = []
            for mid in touched:
                n = counts.get(mid, 0)
                status, version, _ = cur.get(mid, ('todo', 0, 0))
                if status == 'todo' and n:
                    status = 'in_progress'
                elif status == 'in_progress' and not n:
                    status = 'todo'
                ups.append({'m': mid, 'tid': tid, 'p': self.pid, 'st': status, 'n': n, 'v': version + 1, 't': t})
            _many(c, self._UPSERT_STATUS.format(extra_cols='', extra_vals='', extra_set=''), ups)
        return {'annotations': n_ann, 'images': len(touched), 'classes_added': [c['name'] for c in added],
                'skipped': skipped}

    def remove_model_annotations(self, tid, media_ids=None):
        """Soft-delete the model rows of task `tid` on `media_ids` (None = every image); counts, versions and
        statuses follow (in_progress with nothing left -> todo). Returns {removed, images}."""
        t = _now()
        with self.write() as c:
            self._task_row(c, tid)
            q = ("SELECT media_id, COUNT(*) FROM lbl.annotations WHERE project_id = :p AND task_id = :t "
                 "AND source = 'model' AND NOT is_deleted")
            a = {'p': self.pid, 't': tid}
            if media_ids is not None:
                q += ' AND media_id = ANY(:ids)'
                a['ids'] = list(dict.fromkeys(media_ids))
            hit = dict(c.execute(_q(q + ' GROUP BY media_id'), a).all())
            if not hit:
                return {'removed': 0, 'images': 0}
            mids = list(hit)
            self._soft_delete(c, tid, mids, "source = 'model'", t)
            c.execute(_q('UPDATE lbl.media_task_status s SET n_annotations = (SELECT COUNT(*) FROM lbl.annotations a '
                         'WHERE a.media_id = s.media_id AND a.task_id = s.task_id AND NOT a.is_deleted), '
                         'version = version + 1, updated_at = :now WHERE s.project_id = :p AND s.task_id = :t '
                         'AND s.media_id = ANY(:ids)'), {'now': t, 'p': self.pid, 't': tid, 'ids': mids})
            c.execute(_q("UPDATE lbl.media_task_status SET status = 'todo' WHERE project_id = :p AND task_id = :t "
                         "AND status = 'in_progress' AND n_annotations = 0"), {'p': self.pid, 't': tid})
        return {'removed': sum(hit.values()), 'images': len(hit)}

    def predict_targets(self, tid, scope='unlabeled', batch=500):
        """(count, iterator of [(media_id, uri, width, height, file_name)] lists) of the images a label-all job
        runs on, in media order, streamed from a private connection. scope: 'unlabeled' (no shapes in this
        task, not done / skipped), 'not_done' (anything but done), 'all'."""
        if scope not in ('unlabeled', 'not_done', 'all'):
            raise ValueError(f"unknown scope {scope!r} (unlabeled, not_done or all)")
        where = {'unlabeled': "COALESCE(s.n_annotations, 0) = 0 AND COALESCE(s.status, 'todo') NOT IN "
                              "('done', 'skipped')",
                 'not_done': "COALESCE(s.status, 'todo') <> 'done'", 'all': 'TRUE'}[scope]
        frm = ('FROM lbl.media m LEFT JOIN lbl.media_task_status s ON s.media_id = m.id AND s.task_id = :t '
               f'WHERE m.project_id = :p AND {where}')
        a = {'t': tid, 'p': self.pid}
        with self._read() as c:
            n = c.execute(_q(f'SELECT COUNT(*) {frm}'), a).scalar()

        def gen():
            c = self._stream()
            try:
                res = c.execution_options(stream_results=True, yield_per=batch).execute(
                    _q(f'SELECT m.id, m.uri, m.width, m.height, m.file_name {frm} ORDER BY m.position, m.id'), a)
                while rows := res.fetchmany(batch):
                    yield [tuple(r) for r in rows]
            finally:
                c.close()
        return n, gen()

    # ------------------------------------------------------------ class stats + bulk delete
    def class_stats(self):
        """{class_id: {n_annotations, n_images}} for every live class (0 for unused), one GROUP BY."""
        with self._read() as c:
            out = {r: {'n_annotations': 0, 'n_images': 0}
                   for r in c.execute(_q('SELECT id FROM lbl.label_classes WHERE project_id = :p AND NOT is_deleted'),
                                      {'p': self.pid}).scalars()}
            for cid, n, k in c.execute(_q('SELECT class_id, COUNT(*), COUNT(DISTINCT media_id) FROM lbl.annotations '
                                          'WHERE project_id = :p AND NOT is_deleted GROUP BY class_id'),
                                       {'p': self.pid}):
                if cid in out:
                    out[cid] = {'n_annotations': n, 'n_images': k}
        return out

    def delete_classes(self, class_ids):
        """Delete several classes with their annotations in ONE transaction (all or nothing: an unknown id
        rolls everything back). Returns {deleted, annotations, images}."""
        ids = list(dict.fromkeys(class_ids or []))
        if not ids:
            raise ValueError('pick at least one class')
        with self.write() as c:
            for cid in ids:
                self._class_row(c, cid)                 # KeyError before anything changes
            n_ann, n_img = c.execute(_q('SELECT COUNT(*), COUNT(DISTINCT media_id) FROM lbl.annotations '
                                        'WHERE project_id = :p AND class_id = ANY(:ids) AND NOT is_deleted'),
                                     {'p': self.pid, 'ids': ids}).one()
            for cid in ids:
                self.delete_class(cid, delete_annotations=True)     # joins this transaction
        return {'deleted': len(ids), 'annotations': n_ann, 'images': n_img}

    # ------------------------------------------------------------ stats (cards)
    def stats(self):
        """{n_media, tasks:{tid: {done, in_progress, skipped, todo}}, review:{tid: {manual, bulk}}}: two grouped
        queries over the statuses (review = done images approved by hand / by Review all)."""
        a = {'p': self.pid}
        with self._read() as c:
            n_media = c.execute(_q('SELECT COUNT(*) FROM lbl.media WHERE project_id = :p'), a).scalar()
            out = {t: {'done': 0, 'in_progress': 0, 'skipped': 0, 'todo': 0} for t in self._live_tids(c)}
            review = {t: {'manual': 0, 'bulk': 0} for t in out}
            for tid, status, n in c.execute(_q('SELECT task_id, status, COUNT(*) FROM lbl.media_task_status '
                                               'WHERE project_id = :p GROUP BY task_id, status'), a):
                if tid in out and status in out[tid]:
                    out[tid][status] = n
            for tid, rv, n in c.execute(_q("SELECT task_id, review, COUNT(*) FROM lbl.media_task_status "
                                           "WHERE project_id = :p AND status = 'done' AND review IS NOT NULL "
                                           'GROUP BY task_id, review'), a):
                if tid in review and rv in review[tid]:
                    review[tid][rv] = n
        for s in out.values():
            s['todo'] = max(0, n_media - s['done'] - s['in_progress'] - s['skipped'])
        return {'n_media': n_media, 'tasks': out, 'review': review}

    # ------------------------------------------------------------ the visualizer's live source + LLM context
    def fingerprint_parts(self, tid, only_done=False):
        """What LabelingSource.fingerprint() hashes: changes on any annotation edit / delete of task `tid`, a new
        image, a class rename / reorder / delete and (only_done) a status change. Indexed aggregates only."""
        a = {'p': self.pid, 't': tid}
        with self._read() as c:
            # every edit, soft delete included, bumps updated_at; the live count catches a restore at the same instant
            out = [tuple(c.execute(_q('SELECT MAX(updated_at), COUNT(*), COUNT(*) FILTER (WHERE NOT is_deleted) '
                                      'FROM lbl.annotations WHERE project_id = :p AND task_id = :t'), a).one()),
                   tuple(c.execute(_q('SELECT COUNT(*), MAX(position), MAX(created_at) FROM lbl.media '
                                      'WHERE project_id = :p'), a).one()),
                   tuple(tuple(r) for r in c.execute(_q(
                       'SELECT id, name, position FROM lbl.label_classes WHERE project_id = :p AND task_id = :t '
                       'AND NOT is_deleted ORDER BY position, name COLLATE "C"'), a))]
            if only_done:
                out.append(tuple(c.execute(_q("SELECT COUNT(*), MAX(updated_at), COUNT(*) FILTER (WHERE status = "
                                              "'done') FROM lbl.media_task_status WHERE project_id = :p "
                                              'AND task_id = :t'), a).one()))
        return tuple(out)

    def change_cursor(self, tid):
        """The newest lbl.change_seq of task `tid` (its shapes + statuses; 0 = none): where a live feed starts
        (data/live.py). Read BEFORE a snapshot so a write committed during it is replayed, never lost."""
        a = {'p': self.pid, 't': tid}
        with self._read() as c:
            return int(c.execute(_q(
                'SELECT GREATEST((SELECT MAX(change_seq) FROM lbl.annotations WHERE project_id = :p AND task_id = :t), '
                '(SELECT MAX(change_seq) FROM lbl.media_task_status WHERE project_id = :p AND task_id = :t))'),
                a).scalar() or 0)

    def changed_media(self, tid, since, limit=2000):
        """(media ids whose shapes or status of task `tid` changed after cursor `since`, the new cursor).
        Up to limit + 1 ids (more than `limit` = a bulk change: the caller re-indexes instead of patching).
        Writes to a project hold its advisory lock (write()), so per project change_seq follows commit order."""
        a = {'p': self.pid, 't': tid, 's': int(since), 'n': int(limit) + 1}
        with self._read() as c:
            rows = c.execute(_q(
                'SELECT media_id, MAX(seq) AS seq FROM ('
                'SELECT media_id, change_seq AS seq FROM lbl.annotations WHERE project_id = :p AND task_id = :t '
                'AND change_seq > :s UNION ALL '
                'SELECT media_id, change_seq FROM lbl.media_task_status WHERE project_id = :p AND task_id = :t '
                'AND change_seq > :s) x GROUP BY media_id ORDER BY MAX(seq) LIMIT :n'), a).all()
        return [r[0] for r in rows], max((int(r[1]) for r in rows), default=int(since))

    def parent_boxes(self, ids):
        """[{id, class_id, geometry}] of the live boxes among `ids` (pose boxes another task's keypoints link)."""
        if not ids:
            return []
        with self._read() as c:
            return [dict(r) for r in _rows(c, "SELECT id, class_id, geometry FROM lbl.annotations WHERE project_id = :p "
                                              "AND id = ANY(:ids) AND NOT is_deleted AND geometry_type = 'bbox'",
                                           {'p': self.pid, 'ids': list(ids)})]

    def bbox_extents(self, ids):
        """{id: (bbox_x, bbox_y, bbox_w, bbox_h)} of these annotations (live or not)."""
        if not ids:
            return {}
        with self._read() as c:
            return {r[0]: tuple(r[1:]) for r in c.execute(_q(
                'SELECT id, bbox_x, bbox_y, bbox_w, bbox_h FROM lbl.annotations WHERE project_id = :p '
                'AND id = ANY(:ids)'), {'p': self.pid, 'ids': list(ids)})}

    def box_signature(self, tid):
        """(live box count, last change) of task `tid`: the cache key of its LLM exemplar sheets."""
        with self._read() as c:
            return tuple(c.execute(_q("SELECT COUNT(*), MAX(updated_at) FROM lbl.annotations WHERE project_id = :p "
                                      "AND task_id = :t AND NOT is_deleted AND geometry_type = 'bbox'"),
                                   {'p': self.pid, 't': tid}).one())

    def class_boxes(self, tid, cid, limit=200):
        """[(media_id, geometry)] of class `cid`'s live boxes, manual ones first, biggest first."""
        with self._read() as c:
            return [(r[0], _loads(r[1])) for r in c.execute(_q(
                "SELECT media_id, geometry FROM lbl.annotations WHERE project_id = :p AND task_id = :t "
                "AND class_id = :c AND NOT is_deleted AND geometry_type = 'bbox' "
                "ORDER BY (source = 'model'), (bbox_w * bbox_h) DESC NULLS LAST LIMIT :n"),
                {'p': self.pid, 't': tid, 'c': cid, 'n': int(limit)})]

    # ------------------------------------------------------------ export
    def iter_task_export(self, task_id, only_done=False):
        """(media_row + 'status', [live ann rows]) in media position order, streamed with a merge of two
        server-side cursors in the same order on a private connection (memory stays flat for any size)."""
        c = self._stream()
        try:
            a = {'p': self.pid, 't': task_id}
            anns = c.execution_options(stream_results=True, yield_per=_STREAM_BATCH).execute(_q(
                'SELECT a.* FROM lbl.annotations a JOIN lbl.media m ON m.id = a.media_id WHERE a.project_id = :p '
                'AND a.task_id = :t AND NOT a.is_deleted ORDER BY m.position, m.id, a.z_order, a.created_at, a.id'),
                a).mappings()
            nxt = anns.fetchone()
            media = c.execute(_q("SELECT m.*, COALESCE(s.status, 'todo') AS status FROM lbl.media m "
                                 'LEFT JOIN lbl.media_task_status s ON s.media_id = m.id AND s.task_id = :t '
                                 'WHERE m.project_id = :p ORDER BY m.position, m.id'), a).mappings()
            for m in media:
                rows = []
                while nxt is not None and nxt['media_id'] == m['id']:
                    rows.append(dict(nxt))
                    nxt = anns.fetchone()
                if only_done and m['status'] != 'done':
                    continue
                m = dict(m)
                m['meta'] = _loads(m['meta'])
                for r in rows:
                    r['geometry'] = _loads(r['geometry'])
                    r['attributes'] = _loads(r['attributes'])
                # a keypoints instance may be linked to a box of ANOTHER task (the pose box on export): fetch
                # those parents too, marked _parent_only so they are never written or counted as this task's
                own = {r['id'] for r in rows}
                want = [r['parent_id'] for r in rows if r['geometry_type'] == 'keypoints' and r.get('parent_id')
                        and r['parent_id'] not in own]
                if want:
                    for p in c.execute(_q("SELECT * FROM lbl.annotations WHERE project_id = :p AND id = ANY(:ids) "
                                          "AND NOT is_deleted AND geometry_type = 'bbox'"),
                                       {'p': self.pid, 'ids': want}).mappings().all():
                        p = dict(p)
                        p['geometry'] = _loads(p['geometry'])
                        p['attributes'] = _loads(p['attributes'])
                        p['_parent_only'] = True
                        rows.append(p)
                yield m, rows
        finally:
            c.close()

    def iter_rows(self, table, where=None, batch=1000):
        """Lists of this project's row dicts of a table, converted for SQLAlchemy inserts (datetime UTC for *_at;
        JSON is already decoded, booleans are booleans). where: {column: value | list of values}."""
        if table not in _COLS:
            raise KeyError(f'no table {table!r}')
        cols = _COLS[table]
        conds, args = ['id = :p' if table == 'projects' else 'project_id = :p'], {'p': self.pid}
        for i, (k, v) in enumerate((where or {}).items()):
            if k not in cols:
                raise KeyError(f'no column {k!r} in {table}')
            if isinstance(v, (list, tuple, set)):
                conds.append(f'{k} = ANY(:w{i})')
                args[f'w{i}'] = list(v)
            else:
                conds.append(f'{k} = :w{i}')
                args[f'w{i}'] = v
        q = f'SELECT * FROM lbl.{table} WHERE ' + ' AND '.join(conds)
        jc, tc = _JSON_COLS[table], _TIME_COLS[table]
        c = self._stream()
        try:
            res = c.execution_options(stream_results=True, yield_per=batch).execute(text(q), args).mappings()
            while True:
                rows = res.fetchmany(batch)
                if not rows:
                    return
                out = []
                for r in rows:
                    d = dict(r)
                    for k in jc:
                        d[k] = _loads(d[k])
                    for k in tc:
                        if isinstance(d[k], (int, float)):
                            d[k] = datetime.fromtimestamp(d[k], timezone.utc)
                    out.append(d)
                yield out
        finally:
            c.close()

    def add_export_run(self, run_id, task_id, target, target_uri, options):
        with self.write() as c:
            c.execute(_q('INSERT INTO lbl.export_runs(id, project_id, task_id, target, target_uri, options, '
                         "started_at, status) VALUES (:id, :p, :t, :tg, :uri, CAST(:o AS jsonb), :now, 'running')"),
                      {'id': run_id, 'p': self.pid, 't': task_id, 'tg': target, 'uri': target_uri,
                       'o': _dumps(options), 'now': _now()})

    def finish_export_run(self, run_id, status, n_media=None, n_annotations=None, error=None):
        with self.write() as c:
            c.execute(_q('UPDATE lbl.export_runs SET status = :st, finished_at = :now, n_media = :nm, '
                         'n_annotations = :na, error = :e WHERE id = :id AND project_id = :p'),
                      {'st': status, 'now': _now(), 'nm': n_media, 'na': n_annotations, 'e': error, 'id': run_id,
                       'p': self.pid})

    def export_runs(self, limit=50):
        with self._read() as c:
            return [dict(r) for r in _rows(c, 'SELECT * FROM lbl.export_runs WHERE project_id = :p '
                                              'ORDER BY started_at DESC NULLS LAST LIMIT :n',
                                           {'p': self.pid, 'n': int(limit)})]
