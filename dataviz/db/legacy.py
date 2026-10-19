"""
One-time import of the single-user JSON / SQLite stores into PostgreSQL (plan section 10), run at start-up
once the first admin exists and while app.app_settings has no 'legacy_imported' key.

    import_legacy(ws_root, admin_id, log=print, progress=None) -> summary dict

Everything becomes the admin's, private. Each step runs in its own transaction and is idempotent (rows keep
their old ids; an id already present is left alone), so a crash half-way can simply be re-run. A broken source
file is logged and skipped; it never stops the other steps. The old files are never modified (they are the
backup). Feature vectors are not imported: they are backfilled into pgvector lazily (ml/vectors.py).

What lands where (the repositories read exactly these shapes):
    connections.json connections  app.connections: config = the entry minus id / kind / name / secret fields,
                                  secrets = encrypt_json({secret_key, access_key, private_key, api_key} that are set);
                                  kind defaults to 's3'; name falls back to endpoint / host / url, de-duplicated
    connections.json datasets     app.sessions (kind 'viz') + app.viz_sessions (source_kind = old kind)
    llm.json                      app.llm_providers (config = provider minus id / name / kind / api_key, secrets =
                                  {api_key}) + app.llm_user_config (active_provider_id, features, budget)
    models.json                   app.models (config = entry minus id / name, without api.api_key / api.headers;
                                  secrets = {api_key, headers})
    label_settings.json           app.user_prefs.data['label'] (keys already there win)
    labeling/projects.json + db   app.sessions (kind 'label') + app.label_projects + every lbl.* row
    curation/<id>/settings.json   app.dataset_state.data['curation_ignored'] + app.user_session_prefs.data
                                  {filter, filter_presets}
    <ws>/workspace.json folders   tags.json -> app.box_tags (fingerprint, tag, sample_ids); ui_settings.json ->
                                  app.user_session_prefs.data['ui'] of the viz session reading that dataset
"""

import json
import re
import sqlite3
import time
from pathlib import Path

import orjson
from sqlalchemy import Boolean, Float, text
from sqlalchemy.dialects.postgresql import JSONB

from . import begin, crypto
from . import schema as S
from ..core.names import clean_tags, unique_name

SECRET_FIELDS = ('secret_key', 'access_key', 'private_key', 'api_key')     # = data/connections.py
PERSIST_KEYS = ('curation_ignored', 'filter', 'filter_presets')           # = state.py: per dataset, not per version
DS_KINDS = ('local', 's3', 'labeling')
BATCH = 1000
# lbl tables in foreign-key order; the project row is written separately
LBL_TABLES = (S.lbl_tasks, S.lbl_label_classes, S.lbl_media, S.lbl_annotations, S.lbl_annotation_relations,
              S.lbl_media_task_status, S.lbl_export_runs)
_NO_USER = ('created_by', 'committed_by')         # old values were free text, never user ids


def _read_json(path):
    """Parsed JSON, None when the file does not exist; ValueError / OSError when it is broken."""
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _epoch(v):
    """An old time (epoch float, or an ISO / 'Y-m-d H:M:S' string) -> epoch float, or None."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _jsonb(v):
    """A value for CAST(:x AS jsonb): JSON text, or None for SQL NULL."""
    return None if v is None else orjson.dumps(v).decode()


def _exists(c, table, key, value):
    return c.execute(text(f'SELECT 1 FROM {table} WHERE {key} = :v'), {'v': value}).first() is not None


def _owner_names(c, table, owner_id):
    return list(c.execute(text(f'SELECT name FROM {table} WHERE owner_id = :o'), {'o': owner_id}).scalars())


def dataset_uri(ds):
    """The `uri` the visualizer's source object had for a saved dataset (= workspace.json 'dataset')."""
    kind = ds.get('kind')
    if kind == 'local':
        try:
            return str(Path(ds['path']).expanduser().resolve())
        except (KeyError, OSError, RuntimeError):
            return ds.get('path')
    if kind == 's3':
        return f"s3://{ds.get('bucket')}/{(ds.get('prefix') or '').strip('/')}".rstrip('/')
    if kind == 'labeling':
        return f"label://{ds.get('project_id')}/{ds.get('task_id')}" + ('?only_done=1' if ds.get('only_done') else '')
    return None


class _Run:
    def __init__(self, root, admin_id, log, progress):
        self.root, self.admin, self.log, self.progress = Path(root), admin_id, log, progress or (lambda *a: None)
        self.summary = {'connections': 0, 'viz_sessions': 0, 'llm_providers': 0, 'models': 0, 'label_prefs': False,
                        'label_sessions': 0, 'lbl_rows': {}, 'curation': 0, 'box_tags': 0, 'ui_prefs': 0,
                        'skipped': []}
        self.viz = {}                 # imported (or already present) viz session id -> dataset uri

    def step(self, what, fn, *a):
        try:
            fn(*a)
        except Exception as e:  # noqa: BLE001 - one broken source must not stop the rest
            msg = f'{what}: skipped ({type(e).__name__}: {e})'
            self.summary['skipped'].append(msg)
            self.log(f'legacy import: {msg}')

    # ------------------------------------------------------------------ connections.json
    def connections(self, data):
        n = 0
        with begin() as c:
            for e in data.get('connections') or []:
                if not isinstance(e, dict) or not e.get('id') or _exists(c, 'app.connections', 'id', e['id']):
                    continue
                kind = e.get('kind') or 's3'
                base = str(e.get('name') or e.get('endpoint') or e.get('host') or e.get('url') or kind).strip()
                name = unique_name(base[:190] or kind, _owner_names(c, 'app.connections', self.admin))
                config = {k: v for k, v in e.items() if k not in ('id', 'kind', 'name', *SECRET_FIELDS)}
                secrets = {k: e[k] for k in SECRET_FIELDS if e.get(k)}
                c.execute(text("""INSERT INTO app.connections(id, owner_id, kind, name, config, secrets)
                                  VALUES (:id, :o, :k, :n, CAST(:cfg AS jsonb), :s) ON CONFLICT (id) DO NOTHING"""),
                          {'id': e['id'], 'o': self.admin, 'k': kind, 'n': name, 'cfg': _jsonb(config),
                           's': crypto.encrypt_json(secrets)})
                n += 1
        self.summary['connections'] = n

    def datasets(self, data):
        from ..data import sessions
        n = 0
        with begin() as c:
            for ds in data.get('datasets') or []:
                if not isinstance(ds, dict) or not ds.get('id'):
                    continue
                sid = ds['id']
                kind = ds.get('kind') or ('local' if ds.get('path') else 's3')
                if kind not in DS_KINDS:
                    self.log(f'legacy import: dataset {sid}: unknown kind {kind!r}, skipped')
                    continue
                ds = {**ds, 'kind': kind}
                cur = c.execute(text('SELECT kind FROM app.sessions WHERE id = :id'), {'id': sid}).first()
                if cur is not None:
                    if cur.kind == 'viz':
                        self.viz[sid] = dataset_uri(ds)
                    continue
                try:
                    tags = clean_tags(ds.get('tags') or [])
                except ValueError:
                    tags = []
                name = str(ds.get('name') or '').strip() or sid
                sessions.create('viz', self.admin, name[:200], sid=sid, tags=tags, conn=c, auto_name=True)
                t = _epoch(ds.get('created'))
                if t is not None:
                    c.execute(text('UPDATE app.sessions SET created_at = to_timestamp(:t), updated_at = to_timestamp(:t) '
                                   'WHERE id = :id'), {'t': t, 'id': sid})
                cid = ds.get('connection_id') if kind == 's3' else None
                if cid and not _exists(c, 'app.connections', 'id', cid):
                    cid = None
                pid = ds.get('project_id') if kind == 'labeling' else None
                if pid and c.execute(text("SELECT 1 FROM app.sessions WHERE id = :id AND kind = 'label'"),
                                     {'id': pid}).first() is None:
                    pid = None                      # its labeling session is gone: the card shows it as broken
                c.execute(text("""INSERT INTO app.viz_sessions(session_id, source_kind, path, connection_id, bucket,
                                                               prefix, mode, project_id, task_id, only_done)
                                  VALUES (:sid, :k, :path, :cid, :bucket, :prefix, :mode, :pid, :tid, :od)
                                  ON CONFLICT (session_id) DO NOTHING"""),
                          {'sid': sid, 'k': kind, 'path': ds.get('path') if kind == 'local' else None, 'cid': cid,
                           'bucket': ds.get('bucket') if kind == 's3' else None,
                           'prefix': ds.get('prefix') if kind == 's3' else None,
                           'mode': ds.get('mode') if kind == 's3' else None, 'pid': pid,
                           'tid': ds.get('task_id') if kind == 'labeling' else None,
                           'od': bool(ds.get('only_done')) if kind == 'labeling' else False})
                self.viz[sid] = dataset_uri(ds)
                n += 1
        self.summary['viz_sessions'] = n

    # ------------------------------------------------------------------ llm.json / models.json / label_settings.json
    def llm(self, data):
        n = 0
        with begin() as c:
            for p in data.get('providers') or []:
                if not isinstance(p, dict) or not p.get('id') or _exists(c, 'app.llm_providers', 'id', p['id']):
                    continue
                kind = p.get('kind') or 'openai_compat'
                name = str(p.get('name') or p.get('model') or kind).strip()[:200]
                config = {k: v for k, v in p.items() if k not in ('id', 'name', 'kind', 'api_key')}
                secrets = {'api_key': p['api_key']} if p.get('api_key') else None
                c.execute(text("""INSERT INTO app.llm_providers(id, owner_id, name, kind, config, secrets)
                                  VALUES (:id, :o, :n, :k, CAST(:cfg AS jsonb), :s) ON CONFLICT (id) DO NOTHING"""),
                          {'id': p['id'], 'o': self.admin, 'n': name, 'k': kind, 'cfg': _jsonb(config),
                           's': crypto.encrypt_json(secrets)})
                n += 1
            active = data.get('active')
            if active and not _exists(c, 'app.llm_providers', 'id', active):
                active = None
            feats = data.get('features') if isinstance(data.get('features'), dict) else {}
            budget = data.get('budget') if isinstance(data.get('budget'), dict) else {}
            c.execute(text("""INSERT INTO app.llm_user_config(user_id, active_provider_id, features, budget)
                              VALUES (:u, :a, CAST(:f AS jsonb), CAST(:b AS jsonb)) ON CONFLICT (user_id) DO NOTHING"""),
                      {'u': self.admin, 'a': active, 'f': _jsonb(feats), 'b': _jsonb(budget)})
        self.summary['llm_providers'] = n

    def models(self, data):
        n = 0
        with begin() as c:
            for m in data.get('models') or []:
                if not isinstance(m, dict) or not m.get('id') or _exists(c, 'app.models', 'id', m['id']):
                    continue
                base = str(m.get('name') or m.get('id')).strip()[:190]
                name = unique_name(base, _owner_names(c, 'app.models', self.admin))
                config = {k: v for k, v in m.items() if k not in ('id', 'name')}
                secrets = {}
                if isinstance(config.get('api'), dict):
                    api = dict(config['api'])
                    for k in ('api_key', 'headers'):
                        v = api.pop(k, None)
                        if v:
                            secrets[k] = v
                    config['api'] = api
                c.execute(text("""INSERT INTO app.models(id, owner_id, name, config, secrets)
                                  VALUES (:id, :o, :n, CAST(:cfg AS jsonb), :s) ON CONFLICT (id) DO NOTHING"""),
                          {'id': m['id'], 'o': self.admin, 'n': name, 'cfg': _jsonb(config),
                           's': crypto.encrypt_json(secrets)})
                n += 1
        self.summary['models'] = n

    def label_settings(self, data):
        if not isinstance(data, dict):
            raise ValueError('label_settings.json is not an object')
        with begin() as c:
            # prefs already in the database are newer than the file: they win key by key
            c.execute(text("""INSERT INTO app.user_prefs(user_id, data) VALUES (:u, jsonb_build_object('label', CAST(:d AS jsonb)))
                              ON CONFLICT (user_id) DO UPDATE SET data = jsonb_set(app.user_prefs.data, '{label}',
                                  CAST(:d AS jsonb) || COALESCE(app.user_prefs.data -> 'label', '{}'::jsonb)),
                                  updated_at = now()"""),
                      {'u': self.admin, 'd': _jsonb(data)})
        self.summary['label_prefs'] = True

    # ------------------------------------------------------------------ labeling
    def label_projects(self, data):
        entries = data.get('projects', data) if isinstance(data, dict) else data
        if isinstance(entries, dict):
            entries = list(entries.values())
        entries = [e for e in entries or [] if isinstance(e, dict) and e.get('id')]
        for i, e in enumerate(entries):
            self.progress(i, len(entries), f"labeling session {e.get('name') or e['id']}")
            self.step(f"labeling session {e.get('name') or e['id']}", self.label_project, e)
        self.progress(len(entries), len(entries), 'labeling sessions')

    def label_project(self, e):
        from ..data import sessions
        pid = e['id']
        if not re.fullmatch(r'[0-9a-f]+', pid):
            raise ValueError(f'unexpected session id {pid!r}')
        dbp = self.root / 'labeling' / pid / 'annotations.db'
        src = sqlite3.connect(f'file:{dbp}?mode=ro', uri=True) if dbp.exists() else None
        try:
            if src is not None:
                src.row_factory = sqlite3.Row
                src.execute('SELECT count(*) FROM sqlite_master').fetchone()     # a corrupt file fails here
            with begin() as c:
                cur = c.execute(text('SELECT kind FROM app.sessions WHERE id = :id'), {'id': pid}).first()
                if cur is not None and cur.kind != 'label':
                    raise ValueError(f'id {pid} is already used by a {cur.kind} session')
                if cur is None:
                    try:
                        tags = clean_tags(e.get('tags') or [])
                    except ValueError:
                        tags = []
                    name = str(e.get('name') or '').strip()[:200] or pid
                    row = sessions.create('label', self.admin, name, sid=pid, tags=tags, conn=c, auto_name=True)
                    created, updated = _epoch(e.get('created')), _epoch(e.get('updated'))
                    c.execute(text('UPDATE app.sessions SET created_at = COALESCE(to_timestamp(:c), created_at), '
                                   'updated_at = COALESCE(to_timestamp(:u), to_timestamp(:c), updated_at) WHERE id = :id'),
                              {'c': created, 'u': updated, 'id': pid})
                    self.summary['label_sessions'] += 1
                else:
                    row = sessions.get(pid, c)
                c.execute(text("""INSERT INTO app.label_projects(session_id, description, source, export_default, n_media,
                                                                 task_ids, model_connections, review)
                                  VALUES (:sid, :d, CAST(:src AS jsonb), CAST(:ed AS jsonb), :n, :tids, :mc, :rv)
                                  ON CONFLICT (session_id) DO NOTHING"""),
                          {'sid': pid, 'd': e.get('description') or '', 'src': _jsonb(e.get('source') or {'kind': 'upload'}),
                           'ed': _jsonb(e.get('export_default')), 'n': int(e.get('n_media') or 0),
                           'tids': [str(t) for t in e.get('task_ids') or []],
                           'mc': [str(t) for t in e.get('model_connections') or []], 'rv': bool(e.get('review'))})
                self._lbl_project(c, src, pid, row['name'], e)
                if src is not None:
                    self._lbl_rows(c, src, pid)
        finally:
            if src is not None:
                src.close()

    def _lbl_project(self, c, src, pid, name, e):
        old = None
        if src is not None and _has_table(src, 'projects'):
            old = src.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone() or \
                src.execute('SELECT * FROM projects LIMIT 1').fetchone()
        old = dict(old) if old is not None else {}
        meta = _loads(old.get('meta'))
        if meta is None:
            meta = {'source': e.get('source') or {'kind': 'upload'}}
        c.execute(text("""INSERT INTO lbl.projects(id, name, description, created_at, updated_at, meta)
                          VALUES (:id, :n, :d, :c, :u, CAST(:m AS jsonb)) ON CONFLICT (id) DO NOTHING"""),
                  {'id': pid, 'n': name, 'd': old.get('description', e.get('description') or ''),
                   'c': _epoch(old.get('created_at')) or _epoch(e.get('created')),
                   'u': _epoch(old.get('updated_at')) or _epoch(e.get('updated')), 'm': _jsonb(meta)})

    def _lbl_rows(self, c, src, pid):
        counts = self.summary['lbl_rows']
        seen = {}                       # table name -> ids imported (orphan rows would break the foreign keys)
        for tbl in LBL_TABLES:
            if not _has_table(src, tbl.name):
                continue
            src_cols = [r[1] for r in src.execute(f'PRAGMA table_info({tbl.name})')]
            cols = [col for col in tbl.columns if col.name in src_cols or col.name == 'project_id']
            names = [col.name for col in cols]
            vals = ', '.join(f'CAST(:{n} AS jsonb)' if isinstance(col.type, JSONB) else f':{n}'
                             for n, col in zip(names, cols))
            ins = text(f'INSERT INTO lbl.{tbl.name} ({", ".join(names)}) VALUES ({vals}) ON CONFLICT DO NOTHING')
            sel = ', '.join(n for n in names if n in src_cols)
            ids, batch, n, orphans = set(), [], 0, 0
            for r in src.execute(f'SELECT {sel} FROM {tbl.name}'):
                d = dict(r)
                if not self._parents_ok(tbl.name, d, seen):
                    orphans += 1
                    continue
                row = {}
                for col in cols:
                    v = pid if col.name == 'project_id' else d.get(col.name)
                    if col.name in _NO_USER:
                        v = None
                    elif isinstance(col.type, JSONB):
                        v = _jsonb(_loads(v))
                    elif isinstance(col.type, Boolean) and v is not None:
                        v = bool(v)
                    elif isinstance(col.type, Float) and col.name.endswith(('_at', '_on')):
                        v = _epoch(v)
                    row[col.name] = v
                if row.get('id') is not None:
                    ids.add(row['id'])
                batch.append(row)
                if len(batch) >= BATCH:
                    c.execute(ins, batch)
                    n += len(batch)
                    batch = []
            if batch:
                c.execute(ins, batch)
                n += len(batch)
            seen[str(tbl.name)] = ids
            counts[str(tbl.name)] = counts.get(str(tbl.name), 0) + n
            if orphans:
                self.log(f'legacy import: {pid}: {orphans} {tbl.name} row(s) pointing at missing parents, skipped')

    @staticmethod
    def _parents_ok(table, d, seen):
        tasks, media = seen.get('tasks', set()), seen.get('media', set())
        if table in ('label_classes', 'annotations', 'media_task_status') and d.get('task_id') not in tasks:
            return False
        if table in ('annotations', 'media_task_status') and d.get('media_id') not in media:
            return False
        return True

    # ------------------------------------------------------------------ per dataset / per workspace files
    def curation(self):
        base = self.root / 'curation'
        if not base.is_dir():
            return
        for d in sorted(base.iterdir()):
            sid = d.name
            if not d.is_dir() or sid not in self.viz:
                continue
            self.step(f'curation settings of {sid}', self._curation_one, sid, d / 'settings.json')

    def _curation_one(self, sid, path):
        data = _read_json(path)
        if not isinstance(data, dict):
            return
        with begin() as c:
            if 'curation_ignored' in data:
                c.execute(text("""INSERT INTO app.dataset_state(session_id, data) VALUES (:s, CAST(:d AS jsonb))
                                  ON CONFLICT (session_id) DO UPDATE SET data = CAST(:d AS jsonb) || app.dataset_state.data"""),
                          {'s': sid, 'd': _jsonb({'curation_ignored': data['curation_ignored']})})
            prefs = {k: data[k] for k in ('filter', 'filter_presets') if k in data}
            if prefs:
                self._session_prefs(c, sid, prefs)
        self.summary['curation'] += 1

    def _session_prefs(self, c, sid, data):
        # what is already stored is newer (or imported by an earlier step from a more specific file): it wins
        c.execute(text("""INSERT INTO app.user_session_prefs(user_id, session_id, data) VALUES (:u, :s, CAST(:d AS jsonb))
                          ON CONFLICT (user_id, session_id) DO UPDATE
                          SET data = CAST(:d AS jsonb) || app.user_session_prefs.data"""),
                  {'u': self.admin, 's': sid, 'd': _jsonb(data)})

    def workspaces(self):
        latest = {}                     # dataset uri -> (last_opened, folder): ui settings of the newest version
        for meta_path in sorted(self.root.glob('*/workspace.json')):
            ws = meta_path.parent
            try:
                meta = _read_json(meta_path)
                if not isinstance(meta, dict):
                    raise ValueError('not an object')
            except (OSError, ValueError) as e:
                self.summary['skipped'].append(f'{meta_path}: {e}')
                self.log(f'legacy import: {meta_path}: skipped ({e})')
                continue
            fp = meta.get('fingerprint')
            if fp and (ws / 'tags.json').exists():
                self.step(f'{ws.name}/tags.json', self._tags, fp, ws / 'tags.json')
            uri = meta.get('dataset')
            if uri and (ws / 'ui_settings.json').exists():
                lo = str(meta.get('last_opened') or '')
                if uri not in latest or lo > latest[uri][0]:
                    latest[uri] = (lo, ws)
        by_uri = {}
        for sid, uri in self.viz.items():
            by_uri.setdefault(uri, []).append(sid)
        for uri, (_lo, ws) in latest.items():
            sids = by_uri.get(uri)
            if sids:
                self.step(f'{ws.name}/ui_settings.json', self._ui, sids, ws / 'ui_settings.json')

    def _tags(self, fp, path):
        data = _read_json(path)
        if not isinstance(data, dict):
            raise ValueError('not an object')
        rows = []
        for tag, ids in data.items():
            tag = str(tag).strip()
            if not tag or len(tag) > 100 or not isinstance(ids, list):
                self.log(f'legacy import: {path}: tag {tag[:40]!r} skipped')
                continue
            rows.append({'fp': fp, 't': tag, 'ids': sorted({int(i) for i in ids})})
        if rows:
            with begin() as c:
                c.execute(text('INSERT INTO app.box_tags(fingerprint, tag, sample_ids) VALUES (:fp, :t, :ids) '
                               'ON CONFLICT (fingerprint, tag) DO NOTHING'), rows)
        self.summary['box_tags'] += len(rows)

    def _ui(self, sids, path):
        data = _read_json(path)
        if not isinstance(data, dict):
            raise ValueError('not an object')
        ui = {k: v for k, v in data.items() if k not in PERSIST_KEYS}
        # filter / presets / ignore list from before they moved to curation/<id>/: the curation step ran first and wins
        older = {k: data[k] for k in ('filter', 'filter_presets') if k in data}
        with begin() as c:
            for sid in sids:
                self._session_prefs(c, sid, {'ui': ui, **older})
                if 'curation_ignored' in data:
                    c.execute(text("""INSERT INTO app.dataset_state(session_id, data) VALUES (:s, CAST(:d AS jsonb))
                                      ON CONFLICT (session_id) DO UPDATE SET data = CAST(:d AS jsonb) || app.dataset_state.data"""),
                              {'s': sid, 'd': _jsonb({'curation_ignored': data['curation_ignored']})})
                self.summary['ui_prefs'] += 1

    # ------------------------------------------------------------------ all
    def run(self):
        def load(name):
            try:
                return _read_json(self.root / name)
            except (OSError, ValueError) as e:
                self.summary['skipped'].append(f'{name}: {e}')
                self.log(f'legacy import: {name}: skipped (unreadable: {e})')
                return None

        conns = load('connections.json')
        conns = conns if isinstance(conns, dict) else None
        if conns is not None:
            self.step('connections.json connections', self.connections, conns)
        for name, fn in (('llm.json', self.llm), ('models.json', self.models),
                         ('label_settings.json', self.label_settings),
                         ('labeling/projects.json', self.label_projects)):   # labels before datasets: project_id FK
            data = load(name)
            if data is not None:
                self.step(name, fn, data)
        if conns is not None:
            self.step('connections.json datasets', self.datasets, conns)
        self.step('curation settings', self.curation)     # before the workspaces: its filter / ignore list win
        self.step('workspace folders', self.workspaces)
        return self.summary


def _has_table(src, name):
    return src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _loads(v):
    if v is None or not isinstance(v, (str, bytes)):
        return v
    try:
        return orjson.loads(v)
    except orjson.JSONDecodeError:
        return v                     # a plain string stays a JSON string


def import_legacy(ws_root, admin_id, log=print, progress=None):
    """Import the old files under ws_root once (see the module docstring). Returns the summary; when the import
    already ran: {'already': True, **the stored summary}."""
    with begin() as c:
        done = c.execute(text("SELECT value FROM app.app_settings WHERE key = 'legacy_imported'")).scalar()
    if done is not None:
        return {'already': True, **((done or {}).get('summary') or {})}
    t0 = time.time()
    summary = _Run(ws_root, admin_id, log, progress).run()
    with begin() as c:
        c.execute(text("""INSERT INTO app.app_settings(key, value) VALUES ('legacy_imported', CAST(:v AS jsonb))
                          ON CONFLICT (key) DO NOTHING"""), {'v': _jsonb({'at': time.time(), 'summary': summary})})
    rows = sum(summary['lbl_rows'].values())
    log(f"legacy import ({time.time() - t0:.1f}s): {summary['connections']} connections, {summary['viz_sessions']} "
        f"visualize + {summary['label_sessions']} label sessions ({rows:,} labeling rows), {summary['llm_providers']} "
        f"LLM providers, {summary['models']} models, {summary['box_tags']} box tags, {summary['ui_prefs']} UI settings, "
        f"{summary['curation']} curation settings" + (f"; {len(summary['skipped'])} skipped" if summary['skipped'] else ''))
    return summary
