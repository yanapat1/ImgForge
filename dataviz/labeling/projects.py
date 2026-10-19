"""
ProjectRegistry: the labeling sessions index and the per-project folders (images/, cache/, thumbs/, exports/
under <workspace_root>/labeling/<pid>/).

The index lives in PostgreSQL: one app.sessions row per label session (kind 'label': owner, name, tags,
visibility, sharing — the access model both kinds of card share, data/sessions.py), its app.label_projects row
(description, source, export_default, n_media, task_ids, model_connections, review) and its lbl.projects row
(the annotation store's root, labeling/store.py). Deleting the session row cascades to every lbl row.

An entry (get / entries) has the shape the routes and the page always used: {id, name, description, created,
updated, source, export_default, n_media, task_ids, model_connections, tags, review} (epoch times) plus
owner_id and visibility.

duplicate() copies a session with a NEW id for every row (project, tasks, classes, media, annotations,
statuses, export runs) in one transaction, so the original and the copy can be exported into one PostgreSQL /
MSSQL database without primary-key clashes.
"""

import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import orjson
from sqlalchemy import text

from .. import db
from ..core.names import clean_tags
from ..data import sessions
from .store import TABLES, ProjectStore

SOURCE_KINDS = ('upload', 'local', 's3', 'ftp', 'sftp')
N_COVER = 4
_SOURCE_FIELDS = {'upload': (), 'local': ('path', 'recursive'), 's3': ('connection_id', 'bucket', 'prefix'),
                  'ftp': ('connection_id', 'path'), 'sftp': ('connection_id', 'path')}

_ENTRY_SQL = """
    SELECT s.id, s.name, s.owner_id, s.visibility, s.tags, s.created_at, s.updated_at, lp.description, lp.source,
           lp.export_default, lp.n_media, lp.task_ids, lp.model_connections, lp.review
    FROM app.sessions s JOIN app.label_projects lp ON lp.session_id = s.id
    WHERE s.kind = 'label'"""


def _json(v):
    return None if v is None else orjson.dumps(v).decode()


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


def _entry(r):
    return {'id': r.id, 'name': r.name, 'description': r.description or '', 'created': sessions.epoch(r.created_at),
            'updated': sessions.epoch(r.updated_at), 'source': r.source or {'kind': 'upload'},
            'export_default': r.export_default, 'n_media': r.n_media, 'task_ids': list(r.task_ids or []),
            'model_connections': list(r.model_connections or []), 'tags': list(r.tags or []),
            'review': bool(r.review), 'owner_id': r.owner_id, 'visibility': r.visibility}


class ProjectRegistry:
    def __init__(self, root):
        self.root = Path(root)                      # files only: <root>/<pid>/images, cache, thumbs, exports
        self._lock = threading.Lock()
        self._stores = {}

    # ------------------------------------------------------------ index rows
    def _entries(self, c, ids=None):
        q, a = _ENTRY_SQL, {}
        if ids is not None:
            q += ' AND s.id = ANY(:ids)'
            a['ids'] = list(ids)
        return [_entry(r) for r in c.execute(text(q + ' ORDER BY s.updated_at DESC'), a)]

    def entries(self):
        """Every label session (all owners)."""
        with db.connect() as c:
            return self._entries(c)

    def get(self, pid, conn=None):
        if conn is not None:
            got = self._entries(conn, [pid])
        else:
            with db.connect() as c:
                got = self._entries(c, [pid])
        if not got:
            raise KeyError(f'no labeling session {pid!r}')
        return got[0]

    # ------------------------------------------------------------ folders + stores
    def project_dir(self, pid):
        if not pid or not all(c in '0123456789abcdef' for c in pid):
            raise KeyError(f'no labeling session {pid!r}')
        return self.root / pid

    def store(self, pid):
        """The project's ProjectStore (KeyError for an unknown project, checked every time: another process may
        have deleted it)."""
        self.project_dir(pid)
        with db.connect() as c:
            if c.execute(text('SELECT 1 FROM lbl.projects WHERE id = :p'), {'p': pid}).first() is None:
                raise KeyError(f'no labeling session {pid!r}')
        s = self._stores.get(pid)
        if s is None:
            with self._lock:
                s = self._stores.setdefault(pid, ProjectStore(pid))
        return s

    # ------------------------------------------------------------ names
    def check_name(self, name, owner_id, pid=None):
        """ValueError when this owner already has another label session (not `pid`) called `name`, ignoring case."""
        sessions.check_name(owner_id, 'label', name, sid=pid)

    # ------------------------------------------------------------ create / update / delete
    def create(self, body, owner_id):
        """Validate, then in one transaction: the session row (owned by owner_id), its label_projects row, the
        lbl.projects row, the tasks and classes. Creates the folders. Returns the entry."""
        name = (body.get('name') or '').strip()
        if not name:
            raise ValueError('give the session a name')
        if not owner_id:
            raise ValueError('a label session needs an owner (sign in first)')
        self.check_name(name, owner_id)
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
        description = body.get('description') or ''
        pid = uuid.uuid4().hex
        d = self.project_dir(pid)
        for sub in ('images', 'cache', 'thumbs', 'exports'):
            (d / sub).mkdir(parents=True, exist_ok=True)
        try:
            with db.begin() as c:
                sessions.create('label', owner_id, name, sid=pid, tags=tags, conn=c)
                c.execute(text('INSERT INTO app.label_projects(session_id, description, source, export_default, '
                               'review) VALUES (:id, :d, CAST(:src AS jsonb), CAST(:ed AS jsonb), :rv)'),
                          {'id': pid, 'd': description, 'src': _json(source), 'ed': _json(body.get('export_default')),
                           'rv': bool(body.get('review'))})
                st = ProjectStore(pid)
                with st.joined(c):
                    st.init_project(pid, name, description, {'source': source})
                    task_ids = [st.add_task(t['name'], t['task_type'], t.get('config'), t.get('classes') or [])['id']
                                for t in tasks]
                c.execute(text('UPDATE app.label_projects SET task_ids = :t WHERE session_id = :id'),
                          {'t': task_ids, 'id': pid})
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        with self._lock:
            self._stores[pid] = st
        return self.get(pid)

    def update(self, pid, name=None, description=None, export_default=None, source=None, tags=None, review=None):
        if name is not None and not name.strip():
            raise ValueError('give the session a name')
        sets = {}
        if review is not None:
            sets['review'] = bool(review)          # the optional review layer of the card / label page
        if description is not None:
            sets['description'] = description
        if export_default is not None:
            sets['export_default'] = _json(export_default)
        if source is not None:
            sets['source'] = _json(clean_source(source))
        with db.begin() as c:
            self.get(pid, c)                       # KeyError
            if name is not None or tags is not None:
                # only a new name is checked: old duplicates stay editable
                sessions.update(pid, name=name.strip() if name is not None else None, tags=tags, conn=c)
            else:
                sessions.touch(pid, c)
            if sets:
                assign = ', '.join(f'{k} = CAST(:{k} AS jsonb)' if k in ('export_default', 'source') else f'{k} = :{k}'
                                   for k in sets)
                c.execute(text(f'UPDATE app.label_projects SET {assign} WHERE session_id = :id'), {**sets, 'id': pid})
            if name is not None or description is not None:
                st = ProjectStore(pid)
                with st.joined(c):
                    st.update_project(name=name.strip() if name is not None else None, description=description)
            return self.get(pid, c)

    def set_counts(self, pid):
        """Refresh the index's n_media / task_ids / model_connections (the model connections the tasks link,
        so such a connection cannot be deleted) after an import or a task change."""
        s = self.store(pid)
        tasks = s.tasks()
        models = sorted({(t['config'].get('model') or {}).get('connection_id') for t in tasks} - {None})
        with db.begin() as c:
            c.execute(text('UPDATE app.label_projects SET n_media = :n, task_ids = :t, model_connections = :m '
                           'WHERE session_id = :id'),
                      {'n': s.media_count(), 't': [t['id'] for t in tasks], 'm': models, 'id': pid})
            sessions.touch(pid, c)
            return self.get(pid, c)

    def delete(self, pid, delete_files=False):
        """Delete the session; its label_projects row, members and every lbl row cascade."""
        self.project_dir(pid)
        with db.begin() as c:
            self.get(pid, c)                       # KeyError (and never deletes a visualize session here)
            sessions.delete(pid, c)
        with self._lock:
            self._stores.pop(pid, None)
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
                'tags': e.get('tags') or [], 'review': bool(e.get('review')), 'owner_id': e['owner_id'],
                'visibility': e['visibility']}

    def card(self, e):
        """Sessions home card: counts from one grouped status query + the first 4 media ids as the cover."""
        s = self.store(e['id'])
        st = s.stats()
        tasks = [{'id': t['id'], 'name': t['name'], 'task_type': t['task_type'], 'n_classes': len(t['classes']),
                  **st['tasks'].get(t['id'], {}),
                  **{f'review_{k}': v for k, v in st['review'].get(t['id'], {}).items()}} for t in s.tasks()]
        return {'id': e['id'], 'name': e['name'], 'created': e['created'], 'updated': e['updated'],
                'source': e['source'], 'n_media': st['n_media'], 'cover': s.cover(N_COVER), 'tasks': tasks,
                'tags': e.get('tags') or [], 'review': bool(e.get('review'))}

    def list(self, user=None, scope='all'):
        """The cards `user` sees in `scope` (data/sessions.py listing: newest opened first), each with the card
        header {owner, members, visibility, my_role, opened_at}. user None = an internal caller: every session."""
        if user is None:
            user, scope = {'id': '', 'is_admin': True}, 'everyone'
        heads = sessions.listing(user, 'label', scope)
        with db.connect() as c:
            entries = {e['id']: e for e in self._entries(c, [h['id'] for h in heads])}
        out = []
        for h in heads:
            e = entries.get(h['id'])
            if e is None:                          # a label session row without its project rows
                continue
            try:
                card = self.card(e)
            except Exception as ex:  # noqa: BLE001 - one broken project must not hide the others
                card = {'id': e['id'], 'name': e['name'], 'created': e.get('created'), 'updated': e.get('updated'),
                        'source': e.get('source'), 'n_media': e.get('n_media', 0), 'cover': [], 'tasks': [],
                        'tags': e.get('tags') or [], 'error': f'{type(ex).__name__}: {ex}'}
            card.update(owner=h['owner'], members=h['members'], visibility=h['visibility'], my_role=h['my_role'],
                        opened_at=h['opened_at'])
            out.append(card)
        return out

    def uses_connection(self, cid):
        """Names of the projects whose source uses connection `cid` (connections.delete_connection asks)."""
        with db.connect() as c:
            return list(c.execute(text("""
                SELECT s.name FROM app.label_projects lp JOIN app.sessions s ON s.id = lp.session_id
                WHERE lp.source->>'connection_id' = :cid ORDER BY s.name"""), {'cid': cid}).scalars())

    # ------------------------------------------------------------ duplicate
    def duplicate(self, pid, name=None, progress=None, owner_id=None):
        """Copy session `pid` into a new one owned by `owner_id` (default: the original's owner); returns the new
        entry. In ONE transaction (the source project's writers wait on its advisory lock meanwhile):
        1. the session + label_projects rows (same source, tags, review; export_default ids remapped),
        2. every lbl row copied with a NEW id through temp old -> new maps (parent_id, relations, task
           config.model.class_map too),
        3. images/ (upload sessions) and thumbs/ hard-linked (copied across file systems), thumbs renamed to
           the new media ids; cache/ and exports/ are left out (remote images refill the cache on demand).
        No name = "<name> (copy)", made unique per owner with " (copy 2)", …; a typed name that is taken is a
        ValueError. The copy is private."""
        progress = progress or (lambda **k: None)
        e = self.get(pid)
        owner = owner_id or e['owner_id']
        name = (name or '').strip()
        src_dir = self.project_dir(pid)
        new_pid = uuid.uuid4().hex
        d = self.project_dir(new_pid)
        for sub in ('images', 'cache', 'thumbs', 'exports'):
            (d / sub).mkdir(parents=True, exist_ok=True)
        try:
            with db.begin() as c:
                if name:
                    sessions.check_name(owner, 'label', name, conn=c)
                    new_name = name
                else:
                    new_name = sessions.free_name(owner, 'label', f"{e['name']} (copy)", c,
                                                  nth=lambda _, n: f"{e['name']} (copy {n})")
                with ProjectStore(pid).joined(c):            # the source's writers wait: one consistent copy
                    progress(stage='copying the database')
                    sessions.create('label', owner, new_name, sid=new_pid, tags=e['tags'], conn=c)
                    maps = _copy_rows(c, pid, new_pid, new_name)
                    c.execute(text('INSERT INTO app.label_projects(session_id, description, source, export_default, '
                                   'n_media, task_ids, model_connections, review) VALUES (:id, :d, CAST(:src AS jsonb), '
                                   'CAST(:ed AS jsonb), :n, :t, :m, :rv)'),
                              {'id': new_pid, 'd': e['description'], 'src': _json(e['source']),
                               'ed': _json(_remap_export_default(e.get('export_default'), maps)),
                               'n': len(maps['media']), 't': [maps['task'].get(t, t) for t in e['task_ids']],
                               'm': e['model_connections'], 'rv': e['review']})
                if e['source'].get('kind') == 'upload':
                    progress(stage='linking images')
                    _link_tree(src_dir / 'images', d / 'images', progress)
                progress(stage='linking thumbnails')
                _link_thumbs(src_dir / 'thumbs', d / 'thumbs', maps['media'])
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return self.get(new_pid)


# id-bearing columns per lbl table, in insert order (FKs): {column: id space}; project_id is the new project
_REMAP = {
    'tasks': {'id': 'task'},
    'label_classes': {'id': 'class', 'task_id': 'task', 'parent_id': 'class'},
    'media': {'id': 'media'},
    'annotations': {'id': 'ann', 'task_id': 'task', 'media_id': 'media', 'class_id': 'class', 'parent_id': 'ann'},
    'annotation_relations': {'id': 'rel', 'from_id': 'ann', 'to_id': 'ann'},
    'media_task_status': {'media_id': 'media', 'task_id': 'task'},
    'export_runs': {'id': 'run', 'task_id': 'task'},
}
_MAP_SRC = {'task': 'tasks', 'class': 'label_classes', 'media': 'media', 'ann': 'annotations',
            'rel': 'annotation_relations', 'run': 'export_runs'}


def _copy_rows(c, old_pid, new_pid, new_name):
    """Copy every lbl row of project old_pid into new_pid with new ids, INSERT … SELECT through temp id maps (100k
    annotations never round-trip through Python). Returns {'media'|'task'|'class': {old: new}}."""
    a = {'old': old_pid, 'new': new_pid}
    for m, table in _MAP_SRC.items():
        c.execute(text(f'CREATE TEMP TABLE m_{m} (old text PRIMARY KEY, new text NOT NULL) ON COMMIT DROP'))
        # uuid hex like every other id (32 chars, no dashes)
        c.execute(text(f"INSERT INTO m_{m} SELECT id, replace(gen_random_uuid()::text, '-', '') FROM lbl.{table} "
                       'WHERE project_id = :old'), a)
    c.execute(text('INSERT INTO lbl.projects(id, name, description, created_at, updated_at, meta) '
                   'SELECT :new, :name, description, created_at, :now, meta FROM lbl.projects WHERE id = :old'),
              {**a, 'name': new_name, 'now': time.time()})
    for table, cols in _REMAP.items():
        names = [col.name for col in TABLES[table].columns]
        exprs = [':new' if n == 'project_id' else
                 f'COALESCE((SELECT m.new FROM m_{cols[n]} m WHERE m.old = t.{n}), t.{n})' if n in cols else f't.{n}'
                 for n in names]
        c.execute(text(f"INSERT INTO lbl.{table} ({', '.join(names)}) SELECT {', '.join(exprs)} "
                       f'FROM lbl.{table} t WHERE t.project_id = :old'), a)
    maps = {m: dict(c.execute(text(f'SELECT old, new FROM m_{m}')).all()) for m in ('task', 'class', 'media')}
    # a task's model link maps model class names to class ids: those ids are renewed too
    for tid, cfg in c.execute(text('SELECT id, config FROM lbl.tasks WHERE project_id = :new'), a).all():
        cm = ((cfg or {}).get('model') or {}).get('class_map')
        if isinstance(cm, dict):
            cfg['model']['class_map'] = {k: maps['class'].get(v, v) if isinstance(v, str) else v
                                         for k, v in cm.items()}
            c.execute(text('UPDATE lbl.tasks SET config = CAST(:cfg AS jsonb) WHERE id = :id'),
                      {'cfg': _json(cfg), 'id': tid})
    return maps


def _remap_export_default(ed, maps):
    """The copy's export_default: task / class ids in it point at the copy's rows."""
    if not isinstance(ed, dict):
        return ed
    ed = orjson.loads(orjson.dumps(ed))
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
