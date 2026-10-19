"""
ProjectRegistry: the labeling sessions index (<workspace_root>/labeling/projects.json) and the per-project
folders (annotations.db, images/, cache/, thumbs/, exports/). One ProjectStore per project is cached here,
so every request for a project reuses its thread-local SQLite connections.

The index holds what the Sessions home needs without opening every database ({id, name, description,
created, updated, source, export_default, n_media, task_ids, model_connections, tags}); tasks / classes /
annotations live in the project's own store.

duplicate() copies a session with a NEW id for every row (project, tasks, classes, media, annotations,
statuses, export runs), so the original and the copy can be exported into one PostgreSQL / MSSQL database
without primary-key clashes.
"""

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import orjson

from ..core.names import clean_tags, name_taken, unique_name
from .store import ProjectStore

SOURCE_KINDS = ('upload', 'local', 's3', 'ftp', 'sftp')
N_COVER = 4
_SOURCE_FIELDS = {'upload': (), 'local': ('path', 'recursive'), 's3': ('connection_id', 'bucket', 'prefix'),
                  'ftp': ('connection_id', 'path'), 'sftp': ('connection_id', 'path')}


def clean_source(src):
    """The stored form of a project source; ValueError with a human message when it is incomplete."""
    src = dict(src or {'kind': 'upload'})
    kind = src.get('kind') or 'upload'
    if kind not in SOURCE_KINDS:
        raise ValueError(f'unknown image source {kind!r}')
    out = {'kind': kind}
    for k in _SOURCE_FIELDS[kind]:
        if k in src and src[k] is not None:
            out[k] = src[k]
    if kind == 'local':
        if not str(out.get('path') or '').strip():
            raise ValueError('pick the local folder with the images')
        out['path'] = str(Path(out['path']).expanduser())
        out['recursive'] = bool(src.get('recursive', True))
    elif kind == 's3':
        if not out.get('connection_id') or not out.get('bucket'):
            raise ValueError('an S3 source needs a connection and a bucket')
        out['prefix'] = (out.get('prefix') or '').lstrip('/')
    elif kind in ('ftp', 'sftp'):
        if not out.get('connection_id'):
            raise ValueError(f'an {kind.upper()} source needs a saved connection')
        out['path'] = out.get('path') or '/'
    return out


class ProjectRegistry:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'projects.json'
        self._lock = threading.RLock()
        self._stores = {}

    # ------------------------------------------------------------ index file
    def _load(self):
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
        except ValueError:
            data = {}
        data.setdefault('projects', [])
        return data

    def _save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        tmp.replace(self.path)                     # readers never see a half-written index

    def entries(self):
        return self._load()['projects']

    def get(self, pid):
        for p in self.entries():
            if p['id'] == pid:
                return p
        raise KeyError(f'no labeling session {pid!r}')

    def _update_entry(self, pid, **fields):
        with self._lock:
            data = self._load()
            for p in data['projects']:
                if p['id'] == pid:
                    p.update(fields)
                    p['updated'] = time.time()
                    self._save(data)
                    return p
        raise KeyError(f'no labeling session {pid!r}')

    # ------------------------------------------------------------ folders + stores
    def project_dir(self, pid):
        if not pid or not all(c in '0123456789abcdef' for c in pid):
            raise KeyError(f'no labeling session {pid!r}')
        return self.root / pid

    def store(self, pid):
        """The project's ProjectStore (cached; KeyError for an unknown project)."""
        s = self._stores.get(pid)
        if s is not None:
            return s
        with self._lock:
            s = self._stores.get(pid)
            if s is None:
                self.get(pid)
                s = self._stores[pid] = ProjectStore(self.project_dir(pid) / 'annotations.db')
        return s

    # ------------------------------------------------------------ names
    def check_name(self, name, pid=None, entries=None):
        """ValueError when another session (not `pid`) is already called `name`, ignoring case."""
        others = [e['name'] for e in (self.entries() if entries is None else entries) if e['id'] != pid]
        if name_taken(name, others):
            raise ValueError(f'a label session called “{name.strip()}” already exists: pick another name')

    # ------------------------------------------------------------ create / update / delete
    def create(self, body):
        """Validate, create the folder + store with tasks and classes, add the index entry. Returns the entry."""
        name = (body.get('name') or '').strip()
        if not name:
            raise ValueError('give the session a name')
        self.check_name(name)
        tags = clean_tags(body.get('tags'))
        tasks = body.get('tasks') or []
        if not tasks:
            raise ValueError('a session needs at least one task')
        for t in tasks:
            ProjectStore.check_task(t.get('name'), t.get('task_type'), t.get('config'))
            seen = set()
            for c in t.get('classes') or []:
                n = (c if isinstance(c, str) else c.get('name') or '').strip().casefold()
                if not n:
                    raise ValueError(f'a class of task {t["name"]!r} has no name')
                if n in seen:
                    raise ValueError(f'class {n!r} is listed twice in task {t["name"]!r}')
                seen.add(n)
        source = clean_source(body.get('source'))
        pid = uuid.uuid4().hex
        d = self.project_dir(pid)
        for sub in ('images', 'cache', 'thumbs', 'exports'):
            (d / sub).mkdir(parents=True, exist_ok=True)
        store = ProjectStore(d / 'annotations.db')
        store.init_project(pid, name, body.get('description') or '', {'source': source})
        task_ids = [store.add_task(t['name'], t['task_type'], t.get('config'), t.get('classes') or [])['id']
                    for t in tasks]
        now = time.time()
        entry = {'id': pid, 'name': name, 'description': body.get('description') or '', 'created': now,
                 'updated': now, 'source': source, 'export_default': body.get('export_default'), 'n_media': 0,
                 'task_ids': task_ids, 'tags': tags, 'review': bool(body.get('review'))}
        with self._lock:
            data = self._load()
            try:
                self.check_name(name, entries=data['projects'])      # again: another create may have won
            except ValueError:
                store.close()
                shutil.rmtree(d, ignore_errors=True)
                raise
            data['projects'].append(entry)
            self._save(data)
            self._stores[pid] = store
        return entry

    def update(self, pid, name=None, description=None, export_default=None, source=None, tags=None, review=None):
        fields = {}
        if name is not None:
            if not name.strip():
                raise ValueError('give the session a name')
            fields['name'] = name.strip()
        if tags is not None:
            fields['tags'] = clean_tags(tags)
        if review is not None:
            fields['review'] = bool(review)        # the optional review layer of the card / label page
        if description is not None:
            fields['description'] = description
        if export_default is not None:
            fields['export_default'] = export_default
        if source is not None:
            fields['source'] = clean_source(source)
        with self._lock:
            e = self.get(pid)
            if 'name' in fields and fields['name'].casefold() != e['name'].casefold():
                self.check_name(fields['name'], pid)    # only a new name: old duplicates stay editable
            if 'name' in fields or 'description' in fields:
                self.store(pid).update_project(name=fields.get('name'), description=fields.get('description'))
            return self._update_entry(pid, **fields)

    def set_counts(self, pid):
        """Refresh the index's n_media / task_ids / model_connections (the model connections the tasks link,
        so such a connection cannot be deleted) after an import or a task change."""
        s = self.store(pid)
        tasks = s.tasks()
        models = sorted({(t['config'].get('model') or {}).get('connection_id') for t in tasks} - {None})
        return self._update_entry(pid, n_media=s.media_count(), task_ids=[t['id'] for t in tasks],
                                  model_connections=models)

    def delete(self, pid, delete_files=False):
        with self._lock:
            data = self._load()
            before = len(data['projects'])
            data['projects'] = [p for p in data['projects'] if p['id'] != pid]
            if len(data['projects']) == before:
                raise KeyError(f'no labeling session {pid!r}')
            self._save(data)
            s = self._stores.pop(pid, None)
        if s is not None:
            s.close()
        if delete_files:
            shutil.rmtree(self.project_dir(pid), ignore_errors=True)

    # ------------------------------------------------------------ views for the page
    def project(self, pid):
        """Project JSON: the index entry + its tasks with classes."""
        e = self.get(pid)
        s = self.store(pid)
        return {'id': e['id'], 'name': e['name'], 'description': e.get('description') or '',
                'created': e['created'], 'updated': e['updated'], 'source': e['source'],
                'export_default': e.get('export_default'), 'n_media': s.media_count(), 'tasks': s.tasks(),
                'tags': e.get('tags') or [], 'review': bool(e.get('review'))}

    def card(self, e):
        """Sessions home card: counts from one grouped status query + the first 4 media ids as the cover."""
        s = self.store(e['id'])
        st = s.stats()
        tasks = [{'id': t['id'], 'name': t['name'], 'task_type': t['task_type'], 'n_classes': len(t['classes']),
                  **st['tasks'].get(t['id'], {}),
                  **{f'review_{k}': v for k, v in st['review'].get(t['id'], {}).items()}} for t in s.tasks()]
        return {'id': e['id'], 'name': e['name'], 'created': e['created'], 'updated': e['updated'],
                'source': e['source'], 'n_media': st['n_media'], 'cover': s.media_ids()[:N_COVER], 'tasks': tasks,
                'tags': e.get('tags') or [], 'review': bool(e.get('review'))}

    def list(self):
        out = []
        for e in self.entries():
            try:
                out.append(self.card(e))
            except Exception as ex:  # noqa: BLE001 - one broken project must not hide the others
                out.append({'id': e['id'], 'name': e['name'], 'created': e.get('created'), 'updated': e.get('updated'),
                            'source': e.get('source'), 'n_media': e.get('n_media', 0), 'cover': [], 'tasks': [],
                            'tags': e.get('tags') or [], 'error': f'{type(ex).__name__}: {ex}'})
        out.sort(key=lambda p: p.get('updated') or 0, reverse=True)
        return out

    def uses_connection(self, cid):
        """Names of the projects whose source uses connection `cid` (connections.delete_connection asks)."""
        return [e['name'] for e in self.entries() if (e.get('source') or {}).get('connection_id') == cid]

    # ------------------------------------------------------------ duplicate
    def duplicate(self, pid, name=None, progress=None):
        """Copy session `pid` into a new one (returns the new index entry):
        1. annotations.db with SQLite's backup API (consistent even while someone is saving),
        2. every id renewed inside the copy in ONE transaction through temp old -> new maps (parent_id,
           relations, task config.model.class_map and export_default ids too),
        3. images/ (upload sessions) and thumbs/ hard-linked (copied across file systems), thumbs renamed to
           the new media ids; cache/ and exports/ are left out (remote images refill the cache on demand),
        4. the index entry with the same source, export_default and tags. No name = "<name> (copy)", made
           unique with " (2)", " (3)" …; a typed name that is taken is a ValueError."""
        progress = progress or (lambda **k: None)
        e = self.get(pid)
        name = (name or '').strip()
        if name:
            self.check_name(name)
        new_name = name or unique_name(f"{e['name']} (copy)", [p['name'] for p in self.entries()],
                                          nth=lambda _, n: f"{e['name']} (copy {n})")
        src_dir = self.project_dir(pid)
        new_pid = uuid.uuid4().hex
        d = self.project_dir(new_pid)
        for sub in ('images', 'cache', 'thumbs', 'exports'):
            (d / sub).mkdir(parents=True, exist_ok=True)
        try:
            progress(stage='copying the database')
            src = self.store(pid)
            sc = sqlite3.connect(src.path, timeout=10)
            dc = sqlite3.connect(d / 'annotations.db')
            try:
                sc.backup(dc)
            finally:
                sc.close()
            try:
                progress(stage='renewing ids')
                maps = _reid(dc, new_pid, new_name)
            finally:
                dc.close()
            if e['source'].get('kind') == 'upload':
                progress(stage='linking images')
                _link_tree(src_dir / 'images', d / 'images', progress)
            progress(stage='linking thumbnails')
            _link_thumbs(src_dir / 'thumbs', d / 'thumbs', maps['media'])
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        st = ProjectStore(d / 'annotations.db')
        now = time.time()
        tasks = st.tasks()
        entry = {'id': new_pid, 'name': new_name, 'description': e.get('description') or '', 'created': now,
                 'updated': now, 'source': dict(e['source']),
                 'export_default': _remap_export_default(e.get('export_default'), maps),
                 'n_media': st.media_count(), 'task_ids': [t['id'] for t in tasks],
                 'model_connections': list(e.get('model_connections') or []), 'tags': list(e.get('tags') or []),
                 'review': bool(e.get('review'))}
        with self._lock:
            data = self._load()
            data['projects'].append(entry)
            self._save(data)
            self._stores[new_pid] = st
        return entry


# id-bearing columns per table: (table, column, map). Each map is old -> new for one id space.
_REID = [
    ('projects', 'id', 'project'),
    ('tasks', 'id', 'task'), ('tasks', 'project_id', 'project'),
    ('label_classes', 'id', 'class'), ('label_classes', 'task_id', 'task'), ('label_classes', 'parent_id', 'class'),
    ('media', 'id', 'media'), ('media', 'project_id', 'project'),
    ('annotations', 'id', 'ann'), ('annotations', 'project_id', 'project'), ('annotations', 'task_id', 'task'),
    ('annotations', 'media_id', 'media'), ('annotations', 'class_id', 'class'), ('annotations', 'parent_id', 'ann'),
    ('annotation_relations', 'id', 'rel'), ('annotation_relations', 'project_id', 'project'),
    ('annotation_relations', 'from_id', 'ann'), ('annotation_relations', 'to_id', 'ann'),
    ('media_task_status', 'media_id', 'media'), ('media_task_status', 'task_id', 'task'),
    ('export_runs', 'id', 'run'), ('export_runs', 'project_id', 'project'), ('export_runs', 'task_id', 'task'),
]
_MAP_SRC = {'task': 'tasks', 'class': 'label_classes', 'media': 'media', 'ann': 'annotations',
            'rel': 'annotation_relations', 'run': 'export_runs'}


def _reid(c, new_pid, new_name):
    """Renew every id of a copied store in one transaction; returns {'media': {old: new}, 'task': ..,
    'class': ..} (what the files and the index entry need). New ids are uuid4-like hex made by SQLite
    (randomblob), so 100k annotations never round-trip through Python."""
    c.execute('PRAGMA foreign_keys = OFF')             # the ids of parents and children change in steps
    c.isolation_level = None
    c.execute('BEGIN IMMEDIATE')
    try:
        old_pid = c.execute('SELECT id FROM projects LIMIT 1').fetchone()[0]
        c.execute('CREATE TEMP TABLE m_project(old TEXT PRIMARY KEY, new TEXT)')
        c.execute('INSERT INTO m_project VALUES (?, ?)', (old_pid, new_pid))
        for m, table in _MAP_SRC.items():
            c.execute(f'CREATE TEMP TABLE m_{m}(old TEXT PRIMARY KEY, new TEXT)')
            c.execute(f'INSERT INTO m_{m} SELECT id, lower(hex(randomblob(16))) FROM {table}')
        by_table = {}
        for table, col, m in _REID:
            by_table.setdefault(table, []).append((col, m))
        for table, cols in by_table.items():
            sets = ', '.join(f'{col} = COALESCE((SELECT new FROM m_{m} WHERE old = {table}.{col}), {col})'
                             for col, m in cols)
            c.execute(f'UPDATE {table} SET {sets}')
        c.execute('UPDATE projects SET name = ?, updated_at = ?', (new_name, time.time()))
        maps = {m: dict(c.execute(f'SELECT old, new FROM m_{m}')) for m in ('task', 'class', 'media')}
        # a task's model link maps model class names to class ids: those ids are renewed too
        for tid, cfg in c.execute('SELECT id, config FROM tasks').fetchall():
            cfg = json.loads(cfg) if cfg else None
            cm = ((cfg or {}).get('model') or {}).get('class_map')
            if isinstance(cm, dict):
                cfg['model']['class_map'] = {k: maps['class'].get(v, v) if isinstance(v, str) else v
                                             for k, v in cm.items()}
                c.execute('UPDATE tasks SET config = ? WHERE id = ?',
                          (orjson.dumps(cfg, option=orjson.OPT_SORT_KEYS).decode(), tid))
        for m in ('project', *_MAP_SRC):
            c.execute(f'DROP TABLE m_{m}')
        bad = c.execute('PRAGMA foreign_key_check').fetchall()
        if bad:
            raise RuntimeError(f'duplicate: {len(bad)} rows lost their parent while renewing ids')
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise
    return maps


def _remap_export_default(ed, maps):
    """The copy's export_default: task / class ids in it point at the copy's rows."""
    if not isinstance(ed, dict):
        return ed
    ed = json.loads(json.dumps(ed))
    t, k = maps['task'], maps['class']
    if isinstance(ed.get('task_ids'), list):
        ed['task_ids'] = [t.get(x, x) for x in ed['task_ids']]
    if isinstance(ed.get('classes'), dict):
        ed['classes'] = {t.get(tid, tid): [k.get(x, x) for x in ids] for tid, ids in ed['classes'].items()
                         if isinstance(ids, list)}
    return ed


def _link(src, dst):
    try:
        os.link(src, dst)
    except OSError:                                    # another file system / no hard links: copy
        shutil.copy2(src, dst)


def _link_tree(src, dst, progress):
    """Hard-link (else copy) every file of src into dst, keeping sub folders."""
    if not src.is_dir():
        return
    n = 0
    for root, dirs, files in os.walk(src):
        out = dst / os.path.relpath(root, src)
        out.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith('.part'):                     # an upload still being written
                continue
            _link(os.path.join(root, f), out / f)
            n += 1
            if n % 500 == 0:
                progress(done=n)


def _link_thumbs(src, dst, media_map):
    """thumbs/<old media id>[_size].jpg -> thumbs/<new media id>[_size].jpg."""
    if not src.is_dir():
        return
    for p in src.iterdir():
        mid, sep, size = p.stem.partition('_')
        new = media_map.get(mid)
        if new is not None and p.is_file():
            _link(p, dst / f'{new}{sep}{size}{p.suffix}')
