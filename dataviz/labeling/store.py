"""
ProjectStore: one labeling project's SQLite database (<pid>/annotations.db).

The tables come from schema.make_metadata('') (created once through SQLAlchemy so the local copy and the
PostgreSQL / MSSQL export targets can never drift); every read and write after that is raw sqlite3, which
is several times faster than SQLAlchemy Core on the per-request hot paths (autosave PUT, media pages).

Concurrency: one connection per thread (sqlite3 connections must not be shared), WAL so readers never wait
for the writer, and one process-wide write lock per store so writes never hit SQLITE_BUSY.

Stored values: times are epoch seconds (REAL), JSON columns are orjson text, booleans 0/1. iter_rows()
converts rows into what SQLAlchemy inserts expect (datetime, dict, bool) for the database export.
"""

import re
import time
import uuid
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import orjson

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

_MD = schema.make_metadata('')
TABLES = schema.tables(_MD)
_COLS = {name: [c.name for c in t.columns] for name, t in TABLES.items()}
_JSON_COLS = {name: {c.name for c in t.columns if isinstance(c.type, schema.JSONDoc)} for name, t in TABLES.items()}
_BOOL_COLS = {name: {c.name for c in t.columns if c.type.python_type is bool} for name, t in TABLES.items()}
_TIME_COLS = {name: {c.name for c in t.columns if c.name.endswith('_at')} for name, t in TABLES.items()}

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
    return None if v is None else orjson.loads(v)


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
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._wlock = threading.RLock()
        self._ids_cache = None
        self._conns = []
        self._conns_lock = threading.Lock()
        self._ensure_schema()
        row = self.conn().execute('SELECT id FROM projects LIMIT 1').fetchone()
        self.pid = row[0] if row else None

    # ------------------------------------------------------------ connections
    def conn(self):
        c = getattr(self._local, 'c', None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
            c.row_factory = sqlite3.Row
            for p in ('journal_mode=WAL', 'synchronous=NORMAL', 'foreign_keys=ON', 'busy_timeout=5000',
                      'cache_size=-20000'):
                c.execute(f'PRAGMA {p}')
            self._local.c = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    @contextmanager
    def write(self):
        """One write transaction (BEGIN IMMEDIATE under the store's lock)."""
        with self._wlock:
            c = self.conn()
            if c.in_transaction:                         # nested call from inside a write(): join it
                yield c
                return
            c.execute('BEGIN IMMEDIATE')
            try:
                yield c
            except BaseException:
                c.execute('ROLLBACK')
                raise
            c.execute('COMMIT')

    def close(self):
        with self._conns_lock:
            for c in self._conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
        self._local = threading.local()

    def _ensure_schema(self):
        c = sqlite3.connect(self.path)
        try:
            ok = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_info'").fetchone()
        finally:
            c.close()
        if not ok:
            from sqlalchemy import create_engine
            eng = create_engine(f'sqlite:///{self.path}')
            _MD.create_all(eng)
            eng.dispose()
        with self.write() as c:
            # local-only indexes the portable schema does not have: live rows only (partial), and the media
            # order without project_id first, so ORDER BY position, id walks an index instead of sorting
            # (a 200-row page at offset 9800 of 10k: 2.3 ms with a temp B-tree, measured)
            c.execute('CREATE INDEX IF NOT EXISTS ix_ann_media_live ON annotations(media_id) WHERE is_deleted = 0')
            c.execute('CREATE INDEX IF NOT EXISTS ix_ann_class_live ON annotations(class_id) WHERE is_deleted = 0')
            c.execute('CREATE INDEX IF NOT EXISTS ix_media_order ON media(position, id)')
            # local-only column (v2): when the model last ran on this image / task, so opening it does not run
            # the model again. Not in the portable schema: the DB export copies only schema columns.
            if 'model_run_at' not in {r[1] for r in c.execute('PRAGMA table_info(media_task_status)')}:
                c.execute('ALTER TABLE media_task_status ADD COLUMN model_run_at REAL')
            # local-only column (v3): the optional review of a done image, 'manual' (a person approved it) or
            # 'bulk' (Review all); NULL = not reviewed. Any label edit / reopen clears it.
            if 'review' not in {r[1] for r in c.execute('PRAGMA table_info(media_task_status)')}:
                c.execute('ALTER TABLE media_task_status ADD COLUMN review TEXT')
            if not ok:
                c.execute('INSERT OR REPLACE INTO schema_info(key, value) VALUES (?, ?)',
                          ('schema_version', str(schema.SCHEMA_VERSION)))

    # ------------------------------------------------------------ project row
    def init_project(self, pid, name, description='', meta=None):
        t = _now()
        with self.write() as c:
            c.execute('INSERT INTO projects(id, name, description, created_at, updated_at, meta) VALUES (?,?,?,?,?,?)',
                      (pid, name, description or '', t, t, _dumps(meta or {})))
        self.pid = pid

    def update_project(self, **fields):
        sets = {k: v for k, v in fields.items() if k in ('name', 'description') and v is not None}
        sets['updated_at'] = _now()
        with self.write() as c:
            c.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                      (*sets.values(), self.pid))

    # ------------------------------------------------------------ tasks
    def _task_row(self, c, tid, live=True):
        r = c.execute('SELECT * FROM tasks WHERE id = ?' + (' AND is_deleted = 0' if live else ''), (tid,)).fetchone()
        if r is None:
            raise KeyError(f'no task {tid!r} in this session')
        return r

    @staticmethod
    def _class_json(r):
        return {'id': r['id'], 'task_id': r['task_id'], 'name': r['name'], 'color': r['color'],
                'position': r['position'], 'hotkey': r['hotkey']}

    def tasks(self, include_deleted=False):
        """[Task] in position order, each with its live classes in position order."""
        c = self.conn()
        rows = c.execute('SELECT * FROM tasks' + ('' if include_deleted else ' WHERE is_deleted = 0')
                         + ' ORDER BY position, created_at').fetchall()
        classes = {}
        for r in c.execute('SELECT * FROM label_classes WHERE is_deleted = 0 ORDER BY position, name'):
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
            pos = c.execute('SELECT COALESCE(MAX(position) + 1, 0) FROM tasks').fetchone()[0]
            c.execute('INSERT INTO tasks(id, project_id, name, task_type, position, config, created_at, is_deleted) '
                      'VALUES (?,?,?,?,?,?,?,0)', (tid, self.pid, name, task_type, pos, _dumps(config), _now()))
            if classes:
                self.add_classes(tid, classes, strict=True)
        return self.task(tid)

    def update_task(self, tid, name=None, config=None):
        with self.write() as c:
            r = self._task_row(c, tid)
            if name is not None:
                if not name.strip():
                    raise ValueError('a task needs a name')
                c.execute('UPDATE tasks SET name = ? WHERE id = ?', (name.strip(), tid))
            if config is not None:
                _, cfg = self.check_task(r['name'], r['task_type'], config)
                c.execute('UPDATE tasks SET config = ? WHERE id = ?', (_dumps(cfg), tid))
        return self.task(tid)

    def delete_task(self, tid):
        with self.write() as c:
            self._task_row(c, tid)
            c.execute('UPDATE tasks SET is_deleted = 1 WHERE id = ?', (tid,))

    def reorder_tasks(self, ids):
        with self.write() as c:
            live = {r[0] for r in c.execute('SELECT id FROM tasks WHERE is_deleted = 0')}
            if set(ids) != live or len(ids) != len(live):
                raise ValueError('reorder needs every task id exactly once')
            c.executemany('UPDATE tasks SET position = ? WHERE id = ?', [(i, t) for i, t in enumerate(ids)])

    # ------------------------------------------------------------ classes
    def add_classes(self, tid, classes, strict=False):
        """Append classes ({name, color?} or str) to a task with auto colours; returns the new ones.
        strict: a duplicate name is an error (create); else duplicates of existing names are skipped (paste)."""
        from . import colors
        with self.write() as c:
            self._task_row(c, tid)
            rows = c.execute('SELECT name, color, position FROM label_classes WHERE task_id = ? AND is_deleted = 0',
                             (tid,)).fetchall()
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
                cid = new_id()
                c.execute('INSERT INTO label_classes(id, task_id, name, color, position, hotkey, is_deleted) '
                          'VALUES (?,?,?,?,?,?,0)', (cid, tid, name, color, pos, cl.get('hotkey')))
                new.append({'id': cid, 'task_id': tid, 'name': name, 'color': color, 'position': pos,
                            'hotkey': cl.get('hotkey')})
                names.add(name.casefold())
                used.append(color)
                pos += 1
        return new

    def _class_row(self, c, cid):
        r = c.execute('SELECT * FROM label_classes WHERE id = ? AND is_deleted = 0', (cid,)).fetchone()
        if r is None:
            raise KeyError(f'no class {cid!r} in this session')
        return r

    def update_class(self, cid, name=None, color=None, hotkey=..., position=None):
        with self.write() as c:
            r = self._class_row(c, cid)
            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError('a class needs a name')
                dup = c.execute('SELECT 1 FROM label_classes WHERE task_id = ? AND is_deleted = 0 AND id != ? '
                                'AND lower(name) = lower(?)', (r['task_id'], cid, name)).fetchone()
                if dup:
                    raise ValueError(f'this task already has a class {name!r}')
                c.execute('UPDATE label_classes SET name = ? WHERE id = ?', (name, cid))
            if color is not None:
                color = color.lower()
                if not re.fullmatch(r'#[0-9a-f]{6}', color):
                    raise ValueError(f'{color!r} is not a #rrggbb colour')
                c.execute('UPDATE label_classes SET color = ? WHERE id = ?', (color, cid))
            if hotkey is not ...:
                if hotkey is not None and not _HOTKEY.match(hotkey.lower()):
                    raise ValueError(f'{hotkey!r} is not a hotkey')
                c.execute('UPDATE label_classes SET hotkey = ? WHERE id = ?', (hotkey and hotkey.lower(), cid))
            if position is not None:
                ids = [x[0] for x in c.execute('SELECT id FROM label_classes WHERE task_id = ? AND is_deleted = 0 '
                                               'ORDER BY position, name', (r['task_id'],))]
                ids.remove(cid)
                ids.insert(max(0, min(int(position), len(ids))), cid)
                c.executemany('UPDATE label_classes SET position = ? WHERE id = ?', [(i, x) for i, x in enumerate(ids)])
            r = self._class_row(c, cid)
        return self._class_json(r)

    @staticmethod
    def _unlink_children(c, ids, t, tid):
        """Live rows whose parent (a keypoints set's box) is among `ids`, just soft-deleted, lose the link:
        parent_id NULL, version + 1. Their image / task status versions bump too (open tabs reload), except in
        task `tid`, whose statuses the caller bumps itself. Returns the number of rows unlinked."""
        ids, n, pairs = list(ids), 0, set()
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            q = ','.join('?' * len(chunk))
            rows = c.execute(f'SELECT id, media_id, task_id FROM annotations WHERE parent_id IN ({q}) AND is_deleted = 0',
                             chunk).fetchall()
            if rows:
                c.executemany('UPDATE annotations SET parent_id = NULL, version = version + 1, updated_at = ? '
                              'WHERE id = ?', [(t, x[0]) for x in rows])
                pairs.update((x[1], x[2]) for x in rows if x[2] != tid)
                n += len(rows)
        if pairs:
            c.executemany('UPDATE media_task_status SET version = version + 1, updated_at = ? '
                          'WHERE media_id = ? AND task_id = ?', [(t, m, k) for m, k in pairs])
        return n

    def delete_class(self, cid, reassign_to=None, delete_annotations=False):
        with self.write() as c:
            r = self._class_row(c, cid)
            tid, t = r['task_id'], _now()
            n = c.execute('SELECT COUNT(*) FROM annotations WHERE class_id = ? AND is_deleted = 0', (cid,)).fetchone()[0]
            if n and not reassign_to and not delete_annotations:
                raise InUse(f'class {r["name"]!r} is used by {n} annotation{"s" * (n != 1)}: move them to another '
                            f'class or delete them with it')
            if n:
                # other tabs holding these images must reload (their base_version goes stale)
                c.execute('UPDATE media_task_status SET version = version + 1, updated_at = ?, review = NULL '
                          'WHERE task_id = ? AND '
                          'media_id IN (SELECT media_id FROM annotations WHERE class_id = ? AND is_deleted = 0)',
                          (t, tid, cid))
                if reassign_to:
                    to = self._class_row(c, reassign_to)
                    if to['task_id'] != tid or reassign_to == cid:
                        raise ValueError('reassign_to must be another class of the same task')
                    c.execute('UPDATE annotations SET class_id = ?, version = version + 1, updated_at = ? '
                              'WHERE class_id = ? AND is_deleted = 0', (reassign_to, t, cid))
                else:
                    gone = c.execute('SELECT id, media_id FROM annotations WHERE class_id = ? AND is_deleted = 0',
                                     (cid,)).fetchall()
                    media = list(dict.fromkeys(x[1] for x in gone))
                    c.execute('UPDATE annotations SET is_deleted = 1, version = version + 1, updated_at = ? '
                              'WHERE class_id = ? AND is_deleted = 0', (t, cid))
                    self._unlink_children(c, [x[0] for x in gone], t, tid)
                    c.executemany('UPDATE media_task_status SET n_annotations = (SELECT COUNT(*) FROM annotations a '
                                  'WHERE a.media_id = ? AND a.task_id = ? AND a.is_deleted = 0) '
                                  'WHERE media_id = ? AND task_id = ?', [(m, tid, m, tid) for m in media])
                    c.executemany("UPDATE media_task_status SET status = 'todo' WHERE media_id = ? AND task_id = ? "
                                  "AND status = 'in_progress' AND n_annotations = 0", [(m, tid) for m in media])
            c.execute('UPDATE label_classes SET is_deleted = 1 WHERE id = ?', (cid,))
            # a model link mapping to this class would make every later link save fail: map it to 'ignore'
            cfg = _loads(self._task_row(c, tid, live=False)['config']) or {}
            cmap = (cfg.get('model') or {}).get('class_map') or {}
            if cid in cmap.values():
                cfg['model']['class_map'] = {k: 'ignore' if v == cid else v for k, v in cmap.items()}
                c.execute('UPDATE tasks SET config = ? WHERE id = ?', (_dumps(cfg), tid))
        return n

    # ------------------------------------------------------------ media
    def add_media(self, rows):
        """Batch insert media rows (dicts of media columns; id / position / project_id filled in).
        Rows whose file_name already exists are skipped (the UNIQUE index). Returns the number inserted."""
        if not rows:
            return 0
        cols = _COLS['media']
        jcols = _JSON_COLS['media']
        t = _now()
        with self.write() as c:
            pos = c.execute('SELECT COALESCE(MAX(position) + 1, 0) FROM media').fetchone()[0]
            before = c.total_changes
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
                vals.append(tuple(_dumps(r.get(k)) if k in jcols else r.get(k) for k in cols))
            c.executemany(f"INSERT OR IGNORE INTO media({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            n = c.total_changes - before
        self._ids_cache = None
        return n

    def media_count(self):
        return self.conn().execute('SELECT COUNT(*) FROM media').fetchone()[0]

    def media_ids(self):
        """Every media id in position order (navigation); cached until media changes."""
        ids = self._ids_cache
        if ids is None:
            ids = [r[0] for r in self.conn().execute('SELECT id FROM media ORDER BY position, id')]
            self._ids_cache = ids
        return ids

    def media_names(self):
        """{file_name: uri} of every media (import uses it to skip known files and keep names unique)."""
        return {r[0]: r[1] for r in self.conn().execute('SELECT file_name, uri FROM media')}

    def media_row(self, mid):
        r = self.conn().execute('SELECT * FROM media WHERE id = ?', (mid,)).fetchone()
        if r is None:
            raise KeyError(f'no image {mid!r} in this session')
        return dict(r)

    def media_rows(self, ids):
        """{id: row} for a few ids (prefetch)."""
        if not ids:
            return {}
        q = f"SELECT * FROM media WHERE id IN ({','.join('?' * len(ids))})"
        return {r['id']: dict(r) for r in self.conn().execute(q, list(ids))}

    def _statuses(self, c, ids, tids):
        st = {m: ({t: 'todo' for t in tids}, {t: 0 for t in tids}, {t: None for t in tids}, {}) for m in ids}
        for i in range(0, len(ids), 900):              # SQLite's bound-parameter cap
            chunk = ids[i:i + 900]
            for r in c.execute('SELECT media_id, task_id, status, n_annotations, model_run_at, review '
                               f"FROM media_task_status WHERE media_id IN ({','.join('?' * len(chunk))})", chunk):
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
        return [r[0] for r in c.execute('SELECT id FROM tasks WHERE is_deleted = 0 ORDER BY position')]

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
        c = self.conn()
        tids = self._live_tids(c)
        cols = 'm.id, m.file_name, m.width, m.height, m.position'
        lim = [int(limit), int(offset)]
        if task_id and status:
            if status not in schema.STATUSES:
                raise ValueError(f'unknown status {status!r}')
            if status == 'todo':
                # todo = no status row, or a 'todo' one: walk the media order, skip the touched ones
                n_other = c.execute("SELECT COUNT(*) FROM media_task_status WHERE task_id = ? AND status != 'todo'",
                                    (task_id,)).fetchone()[0]
                total = self.media_count() - n_other
                rows = c.execute(f'SELECT {cols} FROM media m WHERE NOT EXISTS (SELECT 1 FROM media_task_status s '
                                 "WHERE s.media_id = m.id AND s.task_id = ? AND s.status != 'todo') "
                                 'ORDER BY m.position, m.id LIMIT ? OFFSET ?', [task_id, *lim]).fetchall()
            else:
                # from the (task_id, status) index: 4.8 ms vs 20 ms for the LEFT JOIN + COALESCE (10k, measured)
                total = c.execute('SELECT COUNT(*) FROM media_task_status WHERE task_id = ? AND status = ?',
                                  (task_id, status)).fetchone()[0]
                rows = c.execute(f'SELECT {cols} FROM media_task_status s JOIN media m ON m.id = s.media_id '
                                 'WHERE s.task_id = ? AND s.status = ? ORDER BY m.position, m.id LIMIT ? OFFSET ?',
                                 [task_id, status, *lim]).fetchall()
        else:
            total = self.media_count()
            rows = c.execute(f'SELECT {cols} FROM media m ORDER BY m.position, m.id LIMIT ? OFFSET ?', lim).fetchall()
        st = self._statuses(c, [r['id'] for r in rows], tids)
        return {'total': total, 'items': [self._media_json(r, st[r['id']]) for r in rows]}

    @staticmethod
    def _like(pat):
        """A LIKE pattern (ESCAPE '\\') from a user wildcard: * = any run, ? = one character, the rest literal."""
        out = []
        for ch in pat:
            out.append('%' if ch == '*' else '_' if ch == '?' else '\\' + ch if ch in '%_\\' else ch)
        return ''.join(out)

    def _media_filtered(self, offset, limit, task_id, status, q, cls, cls_mode, min_ann, max_ann, review=None):
        """media_page with the strip filter: one indexed SQL pass (status / name / class / count), a regex on the
        names it returns. Every predicate is a correlated EXISTS / scalar subquery on (media_id[, task_id]) indexes,
        so the cost follows the media count, not the annotation count."""
        c = self.conn()
        where, args = [], []
        if task_id and status:
            if status not in schema.STATUSES:
                raise ValueError(f'unknown status {status!r}')
            if status == 'todo':
                where.append('NOT EXISTS (SELECT 1 FROM media_task_status s WHERE s.media_id = m.id AND s.task_id = ? '
                             "AND s.status != 'todo')")
                args.append(task_id)
            else:
                where.append('EXISTS (SELECT 1 FROM media_task_status s WHERE s.media_id = m.id AND s.task_id = ? '
                             'AND s.status = ?)')
                args += [task_id, status]
        if review and task_id:
            cond = "s.status = 'done' AND s.review IS NULL" if review == 'none' else 's.review = ?'
            where.append(f'EXISTS (SELECT 1 FROM media_task_status s WHERE s.media_id = m.id AND s.task_id = ? '
                         f'AND {cond})')
            args += [task_id] + ([] if review == 'none' else [review])
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
                where.append("(m.file_name LIKE ? ESCAPE '\\' OR m.file_name LIKE ? ESCAPE '\\')")
                args += [pat, '%/' + pat]
            else:
                where.append("m.file_name LIKE ? ESCAPE '\\'")
                args.append('%' + self._like(q.replace('*', '').replace('?', '')) + '%')
        if cls:
            if cls_mode not in ('any', 'all', 'none'):
                raise ValueError(f'unknown class mode {cls_mode!r}')
            has = ('EXISTS (SELECT 1 FROM annotations a WHERE a.media_id = m.id AND a.is_deleted = 0 '
                   'AND a.class_id {})')
            if cls_mode == 'all':
                for cid in cls:
                    where.append(has.format('= ?'))
                    args.append(cid)
            else:
                where.append(('NOT ' if cls_mode == 'none' else '') + has.format(f"IN ({','.join('?' * len(cls))})"))
                args += cls
        if min_ann is not None or max_ann is not None:
            if task_id:
                n = ('COALESCE((SELECT s.n_annotations FROM media_task_status s WHERE s.media_id = m.id '
                     'AND s.task_id = ?), 0)')
                nargs = [task_id]
            else:
                n = '(SELECT COUNT(*) FROM annotations a WHERE a.media_id = m.id AND a.is_deleted = 0)'
                nargs = []
            if min_ann is not None:
                where.append(f'{n} >= ?')
                args += nargs + [int(min_ann)]
            if max_ann is not None:
                where.append(f'{n} <= ?')
                args += nargs + [int(max_ann)]
        sql_where = (' WHERE ' + ' AND '.join(where)) if where else ''
        cols = 'm.id, m.file_name, m.width, m.height, m.position'
        if rx is None:
            total = c.execute(f'SELECT COUNT(*) FROM media m{sql_where}', args).fetchone()[0]
            rows = c.execute(f'SELECT {cols} FROM media m{sql_where} ORDER BY m.position, m.id LIMIT ? OFFSET ?',
                             [*args, int(limit), int(offset)]).fetchall()
        else:
            every = c.execute(f'SELECT {cols} FROM media m{sql_where} ORDER BY m.position, m.id', args).fetchall()
            budget = REGEX_BUDGET_S + 5e-6 * len(every)
            deadline = time.monotonic() + budget
            slow = ValueError('bad regular expression: it takes too long over the image names, simplify it')
            # regex: one search can never run past the budget, and releases the GIL while it runs (concurrent)
            search = (lambda s: rx.search(s, timeout=budget, concurrent=True)) if _regex is not None else rx.search
            hit = []
            try:
                for k, r in enumerate(every):
                    if search(r[1][:_REGEX_NAME_MAX]):
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
        c = self.conn()
        r = c.execute('SELECT id, file_name, width, height, position FROM media WHERE id = ?', (mid,)).fetchone()
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
        q = ('SELECT a.* FROM annotations a JOIN tasks t ON t.id = a.task_id AND t.is_deleted = 0 '
             'WHERE a.media_id = ? AND a.is_deleted = 0')
        args = [mid]
        if tid:
            q += ' AND a.task_id = ?'
            args.append(tid)
        return [self._ann_json(r) for r in c.execute(q + ' ORDER BY a.z_order, a.created_at', args)]

    def get_annotations(self, mid):
        """{media, versions:{tid: int}, annotations:[Ann]} for every live task."""
        c = self.conn()
        media = self.media_item(mid)
        versions = {t: 0 for t in media['status']}
        for r in c.execute('SELECT task_id, version FROM media_task_status WHERE media_id = ?', (mid,)):
            if r[0] in versions:
                versions[r[0]] = r[1]
        return {'media': media, 'versions': versions, 'annotations': self._live_anns(c, mid)}

    def put_annotations(self, mid, tid, rows, base_version, user=None):
        """Replace the live set of task `tid` on image `mid` with `rows` in one transaction (insert new ids,
        update changed ones with version + 1, soft-delete missing ones). Raises Conflict on a stale
        base_version, ValueError on invalid rows, KeyError on unknown image / task."""
        t = _now()
        with self.write() as c:
            task = self._task_row(c, tid)
            ttype = task['task_type']
            cfg = _loads(task['config']) or {}
            if c.execute('SELECT 1 FROM media WHERE id = ?', (mid,)).fetchone() is None:
                raise KeyError(f'no image {mid!r} in this session')
            st = c.execute('SELECT status, version FROM media_task_status WHERE media_id = ? AND task_id = ?',
                           (mid, tid)).fetchone()
            cur_status, cur_version = (st[0], st[1]) if st else ('todo', 0)
            if int(base_version) != cur_version:
                raise Conflict(cur_version, self._live_anns(c, mid, tid))
            class_ids = {r[0] for r in c.execute('SELECT id FROM label_classes WHERE task_id = ? AND is_deleted = 0',
                                                 (tid,))}
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
                new[aid] = {'class_id': a['class_id'], 'geometry_type': gtype, 'geometry': _dumps(geom),
                            'attributes': _dumps(a.get('attributes') or {}), 'parent_id': a.get('parent_id'),
                            'z_order': int(a.get('z_order') or 0), 'score': None if score is None else float(score),
                            'source': src, 'ext': ext or (None, None, None, None)}
            if ttype == 'classification' and not cfg.get('multi_label'):
                if len(new) > 1:
                    raise ValueError('this classification task takes one class per image')
            old = {r['id']: r for r in c.execute('SELECT id, class_id, geometry_type, geometry, attributes, parent_id, '
                                                 'z_order, score, source, is_deleted FROM annotations '
                                                 'WHERE media_id = ? AND task_id = ?', (mid, tid))}
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
                vals = (n['class_id'], n['geometry_type'], n['geometry'], *n['ext'], n['attributes'], n['score'],
                        n['source'], n['parent_id'], n['z_order'])
                if o is None:
                    ins.append((aid, self.pid, tid, mid, *vals, user, t, t))
                elif o['is_deleted'] or key != (o['class_id'], o['geometry_type'], o['geometry'], o['attributes'],
                                                o['parent_id'], o['z_order'], o['score'], o['source']):
                    upd.append((*vals, t, aid))
            dels = [(t, aid) for aid, o in old.items() if aid not in new and not o['is_deleted']]
            if ins:
                try:
                    c.executemany('INSERT INTO annotations(id, project_id, task_id, media_id, class_id, geometry_type, '
                                  'geometry, bbox_x, bbox_y, bbox_w, bbox_h, attributes, score, source, parent_id, '
                                  'z_order, created_by, created_at, updated_at, version, is_deleted) '
                                  'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)', ins)
                except sqlite3.IntegrityError:
                    raise ValueError('an annotation id is already used by another image or task') from None
            if upd:
                c.executemany('UPDATE annotations SET class_id = ?, geometry_type = ?, geometry = ?, bbox_x = ?, '
                              'bbox_y = ?, bbox_w = ?, bbox_h = ?, attributes = ?, score = ?, source = ?, '
                              'parent_id = ?, z_order = ?, updated_at = ?, version = version + 1, is_deleted = 0 '
                              'WHERE id = ?', upd)
            if dels:
                c.executemany('UPDATE annotations SET is_deleted = 1, version = version + 1, updated_at = ? '
                              'WHERE id = ?', dels)
            n = len(new)
            if not (ins or upd or dels) and st is not None:
                return {'version': cur_version, 'status': cur_status, 'n': n}
            status = cur_status if cur_status == 'done' or (cur_status == 'skipped' and not n) else \
                ('in_progress' if n else 'todo')
            version = cur_version + 1
            c.execute('INSERT INTO media_task_status(media_id, task_id, status, n_annotations, version, updated_at) '
                      'VALUES (?,?,?,?,?,?) ON CONFLICT(media_id, task_id) DO UPDATE SET status = excluded.status, '
                      'n_annotations = excluded.n_annotations, version = excluded.version, '
                      'updated_at = excluded.updated_at, review = NULL', (mid, tid, status, n, version, t))
        return {'version': version, 'status': status, 'n': n}

    def set_status(self, mid, task_ids, action, user=None):
        """commit -> done, skip -> skipped, reopen -> in_progress (or todo with no shapes). {tid: status}."""
        t = _now()
        out = {}
        with self.write() as c:
            if c.execute('SELECT 1 FROM media WHERE id = ?', (mid,)).fetchone() is None:
                raise KeyError(f'no image {mid!r} in this session')
            for tid in task_ids:
                self._task_row(c, tid)
                n = c.execute('SELECT COUNT(*) FROM annotations WHERE media_id = ? AND task_id = ? AND is_deleted = 0',
                              (mid, tid)).fetchone()[0]
                status = {'commit': 'done', 'skip': 'skipped'}.get(action) or ('in_progress' if n else 'todo')
                done = action == 'commit'
                c.execute('INSERT INTO media_task_status(media_id, task_id, status, n_annotations, version, '
                          'committed_by, committed_at, updated_at) VALUES (?,?,?,?,0,?,?,?) '
                          'ON CONFLICT(media_id, task_id) DO UPDATE SET status = excluded.status, '
                          'n_annotations = excluded.n_annotations, updated_at = excluded.updated_at'
                          + (', committed_by = excluded.committed_by, committed_at = excluded.committed_at'
                             if done else ', review = NULL'),
                          (mid, tid, status, n, user if done else None, t if done else None, t))
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
                cond = {'clear': 'review IS NOT NULL', 'manual': "status = 'done' AND COALESCE(review, '') != 'manual'",
                        'bulk': "status = 'done' AND review IS NULL"}[mode]
                n = 0
                if media_ids is None:
                    n = c.execute(f'UPDATE media_task_status SET review = ?, updated_at = ? WHERE task_id = ? AND {cond}',
                                  (val, t, tid)).rowcount
                else:
                    ids = list(media_ids)
                    for i in range(0, len(ids), 900):
                        chunk = ids[i:i + 900]
                        n += c.execute(f'UPDATE media_task_status SET review = ?, updated_at = ? WHERE task_id = ? '
                                       f"AND {cond} AND media_id IN ({','.join('?' * len(chunk))})",
                                       [val, t, tid, *chunk]).rowcount
                out[tid] = n
        return out

    # ------------------------------------------------------------ model predictions
    def apply_predictions(self, mid, tid, preds, replace_model=True):
        """Store one image's model output for task `tid` (see apply_predictions_many). Returns
        {version, annotations: the task's full live set on that image, n_model}."""
        res = self.apply_predictions_many(tid, [(mid, preds)], replace_model)[mid]
        return {'version': res['version'], 'annotations': self._live_anns(self.conn(), mid, tid),
                'n_model': res['n_model']}

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
            class_ids = {r[0] for r in c.execute('SELECT id FROM label_classes WHERE task_id = ? AND is_deleted = 0',
                                                 (tid,))}
            single_tag = ttype == 'classification' and not cfg.get('multi_label')
            mids = [m for m, _ in items]
            known, cur = set(), {}
            for i in range(0, len(mids), 900):
                chunk = mids[i:i + 900]
                q = ','.join('?' * len(chunk))
                known.update(r[0] for r in c.execute(f'SELECT id FROM media WHERE id IN ({q})', chunk))
                for r in c.execute(f'SELECT media_id, status, version, n_annotations FROM media_task_status '
                                   f'WHERE task_id = ? AND media_id IN ({q})', [tid, *chunk]):
                    cur[r[0]] = (r[1], r[2], r[3] or 0)
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
                if single_tag and c.execute("SELECT 1 FROM annotations WHERE media_id = ? AND task_id = ? AND "
                                            "is_deleted = 0 AND source != 'model' LIMIT 1", (mid, tid)).fetchone():
                    rows = []                    # one class per image, and the user already picked it
                if replace_model:
                    dels.append((t, mid, tid))
                for z, (score, gtype, geom, ext, cid, attrs) in enumerate(rows):
                    ins.append((new_id(), self.pid, tid, mid, cid, gtype, _dumps(geom), *(ext or (None,) * 4),
                                _dumps(attrs), None if score is None else float(score), t, t))
                todo.append((mid, len(rows)))
            if dels:
                gone = [x[0] for _, mid, _ in dels for x in c.execute(
                    "SELECT id FROM annotations WHERE media_id = ? AND task_id = ? AND source = 'model' "
                    'AND is_deleted = 0', (mid, tid))]
                c.executemany("UPDATE annotations SET is_deleted = 1, version = version + 1, updated_at = ? "
                              "WHERE media_id = ? AND task_id = ? AND source = 'model' AND is_deleted = 0", dels)
                self._unlink_children(c, gone, t, tid)
            if ins:
                c.executemany('INSERT INTO annotations(id, project_id, task_id, media_id, class_id, geometry_type, '
                              'geometry, bbox_x, bbox_y, bbox_w, bbox_h, attributes, score, source, parent_id, '
                              'z_order, created_at, updated_at, version, is_deleted) '
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'model',NULL,0,?,?,1,0)", ins)
            ups = []
            for mid, n_model in todo:
                n = c.execute('SELECT COUNT(*) FROM annotations WHERE media_id = ? AND task_id = ? AND is_deleted = 0',
                              (mid, tid)).fetchone()[0]
                status, version, _ = cur.get(mid, ('todo', 0, 0))
                if status == 'todo' and n:
                    status = 'in_progress'
                elif status == 'in_progress' and not n:
                    status = 'todo'
                ups.append((mid, tid, status, n, version + 1, t, t))
                out[mid] = {'version': version + 1, 'n_model': n_model, 'status': status}
            c.executemany('INSERT INTO media_task_status(media_id, task_id, status, n_annotations, version, updated_at, '
                          'model_run_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(media_id, task_id) DO UPDATE SET '
                          'status = excluded.status, n_annotations = excluded.n_annotations, '
                          'version = excluded.version, updated_at = excluded.updated_at, '
                          'model_run_at = excluded.model_run_at', ups)
        return out

    def media_for_import(self):
        """[{id, file_name, uri, width, height, meta}] of every media: the annotation import matches keys against it."""
        return [{'id': r[0], 'file_name': r[1], 'uri': r[2], 'width': r[3], 'height': r[4], 'meta': _loads(r[5]) or {}}
                for r in self.conn().execute('SELECT id, file_name, uri, width, height, meta FROM media')]

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
            cur = {}
            for i in range(0, len(mids), 900):
                chunk = mids[i:i + 900]
                q = ','.join('?' * len(chunk))
                for r in c.execute(f'SELECT media_id, status, version, n_annotations FROM media_task_status '
                                   f'WHERE task_id = ? AND media_id IN ({q})', [tid, *chunk]):
                    cur[r[0]] = (r[1], r[2], r[3] or 0)
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
                    dels.append((t, mid, tid))
                for z, (cid, gtype, g, ext, score) in enumerate(rows):
                    ins.append((new_id(), self.pid, tid, mid, cid, gtype, _dumps(g), *(ext or (None,) * 4), _dumps({}),
                                score, z, t, t))
                touched.append(mid)
            if dels:
                gone = [x[0] for _, mid, _ in dels for x in c.execute(
                    "SELECT id FROM annotations WHERE media_id = ? AND task_id = ? AND source != 'model' "
                    'AND is_deleted = 0', (mid, tid))]
                c.executemany("UPDATE annotations SET is_deleted = 1, version = version + 1, updated_at = ? "
                              "WHERE media_id = ? AND task_id = ? AND source != 'model' AND is_deleted = 0", dels)
                self._unlink_children(c, gone, t, tid)
            if ins:
                c.executemany('INSERT INTO annotations(id, project_id, task_id, media_id, class_id, geometry_type, '
                              'geometry, bbox_x, bbox_y, bbox_w, bbox_h, attributes, score, source, parent_id, '
                              'z_order, created_at, updated_at, version, is_deleted) '
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'import',NULL,?,?,?,1,0)", ins)
                n_ann = len(ins)
            ups = []
            counts = {}
            for i in range(0, len(touched), 900):
                chunk = touched[i:i + 900]
                q = ','.join('?' * len(chunk))
                for r in c.execute(f'SELECT media_id, COUNT(*) FROM annotations WHERE task_id = ? AND is_deleted = 0 '
                                   f'AND media_id IN ({q}) GROUP BY media_id', [tid, *chunk]):
                    counts[r[0]] = r[1]
            for mid in touched:
                n = counts.get(mid, 0)
                status, version, _ = cur.get(mid, ('todo', 0, 0))
                if status == 'todo' and n:
                    status = 'in_progress'
                elif status == 'in_progress' and not n:
                    status = 'todo'
                ups.append((mid, tid, status, n, version + 1, t))
            c.executemany('INSERT INTO media_task_status(media_id, task_id, status, n_annotations, version, updated_at) '
                          'VALUES (?,?,?,?,?,?) ON CONFLICT(media_id, task_id) DO UPDATE SET '
                          'status = excluded.status, n_annotations = excluded.n_annotations, '
                          'version = excluded.version, updated_at = excluded.updated_at', ups)
        return {'annotations': n_ann, 'images': len(touched), 'classes_added': [c['name'] for c in added],
                'skipped': skipped}

    def remove_model_annotations(self, tid, media_ids=None):
        """Soft-delete the model rows of task `tid` on `media_ids` (None = every image); counts, versions and
        statuses follow (in_progress with nothing left -> todo). Returns {removed, images}."""
        t = _now()
        with self.write() as c:
            self._task_row(c, tid)
            q = ("SELECT media_id, COUNT(*) FROM annotations WHERE task_id = ? AND source = 'model' AND is_deleted = 0")
            hit = {}
            if media_ids is None:
                hit = dict(c.execute(q + ' GROUP BY media_id', (tid,)).fetchall())
            else:
                ids = list(dict.fromkeys(media_ids))
                for i in range(0, len(ids), 900):
                    chunk = ids[i:i + 900]
                    hit.update(c.execute(q + f" AND media_id IN ({','.join('?' * len(chunk))}) GROUP BY media_id",
                                         [tid, *chunk]).fetchall())
            if not hit:
                return {'removed': 0, 'images': 0}
            mids = list(hit)
            gone = [x[0] for m in mids for x in c.execute(
                "SELECT id FROM annotations WHERE media_id = ? AND task_id = ? AND source = 'model' AND is_deleted = 0",
                (m, tid))]
            c.executemany("UPDATE annotations SET is_deleted = 1, version = version + 1, updated_at = ? "
                          "WHERE media_id = ? AND task_id = ? AND source = 'model' AND is_deleted = 0",
                          [(t, m, tid) for m in mids])
            self._unlink_children(c, gone, t, tid)
            c.executemany('UPDATE media_task_status SET n_annotations = (SELECT COUNT(*) FROM annotations a '
                          'WHERE a.media_id = media_task_status.media_id AND a.task_id = media_task_status.task_id '
                          'AND a.is_deleted = 0), version = version + 1, updated_at = ? '
                          'WHERE media_id = ? AND task_id = ?', [(t, m, tid) for m in mids])
            c.execute("UPDATE media_task_status SET status = 'todo' WHERE task_id = ? AND status = 'in_progress' "
                      'AND n_annotations = 0', (tid,))
        return {'removed': sum(hit.values()), 'images': len(hit)}

    def predict_targets(self, tid, scope='unlabeled', batch=500):
        """(count, iterator of [(media_id, uri, width, height, file_name)] lists) of the images a label-all job
        runs on, in media order, streamed from a private connection. scope: 'unlabeled' (no shapes in this
        task, not done / skipped), 'not_done' (anything but done), 'all'."""
        if scope not in ('unlabeled', 'not_done', 'all'):
            raise ValueError(f"unknown scope {scope!r} (unlabeled, not_done or all)")
        where = {'unlabeled': "COALESCE(s.n_annotations, 0) = 0 AND COALESCE(s.status, 'todo') NOT IN "
                              "('done', 'skipped')",
                 'not_done': "COALESCE(s.status, 'todo') != 'done'", 'all': '1'}[scope]
        frm = (f'FROM media m LEFT JOIN media_task_status s ON s.media_id = m.id AND s.task_id = ? WHERE {where}')
        n = self.conn().execute(f'SELECT COUNT(*) {frm}', (tid,)).fetchone()[0]

        def gen():
            c = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            try:
                cur = c.execute(f'SELECT m.id, m.uri, m.width, m.height, m.file_name {frm} ORDER BY m.position, m.id',
                                (tid,))
                while rows := cur.fetchmany(batch):
                    yield rows
            finally:
                c.close()
        return n, gen()

    # ------------------------------------------------------------ class stats + bulk delete
    def class_stats(self):
        """{class_id: {n_annotations, n_images}} for every live class (0 for unused), one GROUP BY."""
        c = self.conn()
        out = {r[0]: {'n_annotations': 0, 'n_images': 0}
               for r in c.execute('SELECT id FROM label_classes WHERE is_deleted = 0')}
        for cid, n, k in c.execute('SELECT class_id, COUNT(*), COUNT(DISTINCT media_id) FROM annotations '
                                   'WHERE is_deleted = 0 GROUP BY class_id'):
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
            q = ','.join('?' * len(ids))
            n_ann, n_img = c.execute(f'SELECT COUNT(*), COUNT(DISTINCT media_id) FROM annotations WHERE class_id IN '
                                     f'({q}) AND is_deleted = 0', ids).fetchone()
            for cid in ids:
                self.delete_class(cid, delete_annotations=True)     # joins this transaction
        return {'deleted': len(ids), 'annotations': n_ann, 'images': n_img}

    # ------------------------------------------------------------ stats (cards)
    def stats(self):
        """{n_media, tasks:{tid: {done, in_progress, skipped, todo}}, review:{tid: {manual, bulk}}}: two grouped
        queries over the statuses (review = done images approved by hand / by Review all)."""
        c = self.conn()
        n_media = self.media_count()
        out = {t: {'done': 0, 'in_progress': 0, 'skipped': 0, 'todo': 0} for t in self._live_tids(c)}
        review = {t: {'manual': 0, 'bulk': 0} for t in out}
        for tid, status, n in c.execute('SELECT task_id, status, COUNT(*) FROM media_task_status '
                                        'GROUP BY task_id, status'):
            if tid in out and status in out[tid]:
                out[tid][status] = n
        for tid, rv, n in c.execute("SELECT task_id, review, COUNT(*) FROM media_task_status "
                                    "WHERE status = 'done' AND review IS NOT NULL GROUP BY task_id, review"):
            if tid in review and rv in review[tid]:
                review[tid][rv] = n
        for s in out.values():
            s['todo'] = max(0, n_media - s['done'] - s['in_progress'] - s['skipped'])
        return {'n_media': n_media, 'tasks': out, 'review': review}

    # ------------------------------------------------------------ export
    def iter_task_export(self, task_id, only_done=False):
        """(media_row + 'status', [live ann rows with JSON decoded]) in media position order, streamed with a
        merge of two cursors in the same order on a private connection (memory stays flat for any size)."""
        c = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            anns = c.execute('SELECT a.* FROM annotations a JOIN media m ON m.id = a.media_id WHERE a.task_id = ? '
                             'AND a.is_deleted = 0 ORDER BY m.position, m.id, a.z_order, a.created_at', (task_id,))
            nxt = anns.fetchone()
            media = c.execute("SELECT m.*, COALESCE(s.status, 'todo') AS status FROM media m "
                              'LEFT JOIN media_task_status s ON s.media_id = m.id AND s.task_id = ? '
                              'ORDER BY m.position, m.id', (task_id,))
            for m in media:
                rows = []
                while nxt is not None and nxt['media_id'] == m['id']:
                    rows.append(nxt)
                    nxt = anns.fetchone()
                if only_done and m['status'] != 'done':
                    continue
                m = dict(m)
                m['meta'] = _loads(m['meta'])
                out = []
                for r in rows:
                    a = dict(r)
                    a['geometry'] = _loads(a['geometry'])
                    a['attributes'] = _loads(a['attributes'])
                    out.append(a)
                # a keypoints instance may be linked to a box of ANOTHER task (the pose box on export): fetch
                # those parents too, marked _parent_only so they are never written or counted as this task's
                own = {a['id'] for a in out}
                want = [a['parent_id'] for a in out if a['geometry_type'] == 'keypoints' and a.get('parent_id')
                        and a['parent_id'] not in own]
                if want:
                    q = ','.join('?' * len(want))
                    for r in c.execute(f"SELECT * FROM annotations WHERE id IN ({q}) AND is_deleted = 0 "
                                       "AND geometry_type = 'bbox'", want):
                        a = dict(r)
                        a['geometry'] = _loads(a['geometry'])
                        a['attributes'] = _loads(a['attributes'])
                        a['_parent_only'] = True
                        out.append(a)
                yield m, out
        finally:
            c.close()

    def iter_rows(self, table, where=None, batch=1000):
        """Lists of row dicts of a table, converted for SQLAlchemy inserts (datetime UTC, dict, bool).
        where: {column: value | list of values}."""
        if table not in _COLS:
            raise KeyError(f'no table {table!r}')
        cols = _COLS[table]
        q, args = f'SELECT * FROM {table}', []
        if where:
            conds = []
            for k, v in where.items():
                if k not in cols:
                    raise KeyError(f'no column {k!r} in {table}')
                if isinstance(v, (list, tuple, set)):
                    v = list(v)
                    conds.append(f"{k} IN ({','.join('?' * len(v))})" if v else '0')
                    args += v
                else:
                    conds.append(f'{k} = ?')
                    args.append(v)
            q += ' WHERE ' + ' AND '.join(conds)
        jc, bc, tc = _JSON_COLS[table], _BOOL_COLS[table], _TIME_COLS[table]
        c = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            cur = c.execute(q, args)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    return
                out = []
                for r in rows:
                    d = dict(r)
                    for k in jc:
                        d[k] = _loads(d[k])
                    for k in bc:
                        d[k] = bool(d[k]) if d[k] is not None else None
                    for k in tc:
                        if isinstance(d[k], (int, float)):
                            d[k] = datetime.fromtimestamp(d[k], timezone.utc)
                    out.append(d)
                yield out
        finally:
            c.close()

    def add_export_run(self, run_id, task_id, target, target_uri, options):
        with self.write() as c:
            c.execute('INSERT INTO export_runs(id, project_id, task_id, target, target_uri, options, started_at, '
                      "status) VALUES (?,?,?,?,?,?,?,'running')",
                      (run_id, self.pid, task_id, target, target_uri, _dumps(options), _now()))

    def finish_export_run(self, run_id, status, n_media=None, n_annotations=None, error=None):
        with self.write() as c:
            c.execute('UPDATE export_runs SET status = ?, finished_at = ?, n_media = ?, n_annotations = ?, '
                      'error = ? WHERE id = ?', (status, _now(), n_media, n_annotations, error, run_id))

    def export_runs(self, limit=50):
        return [dict(r) for r in self.conn().execute('SELECT * FROM export_runs ORDER BY started_at DESC LIMIT ?',
                                                     (limit,))]
