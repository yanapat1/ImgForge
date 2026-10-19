"""
Labeling sessions (/api/label/*): projects, tasks, classes, uploads, the import job, media pages, images and
thumbnails, annotations (autosave with version check), commit / skip / reopen, the export job and its zip
download, the class colour sequence and the labeling page settings.

v2: model-assisted labeling (detect a model server, link a model to a task, predict one image, the label-all
job on the 'model' lane, remove model labels), class stats + bulk class delete, duplicate a session (job).

Everything is keyed by the project id; nothing here uses ctx.state (a labeling session is not a YOLO
dataset session). Handlers are plain `def` (FastAPI's thread pool) unless they await something; the
multipart upload awaits the form and writes files through run_io. Slow work (import, export) is a job.
"""

import asyncio
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ...core import logging as vlog
from ...labeling import colors, models
from ...labeling.export import run_export
from ...labeling.models import ModelError
from ...labeling.store import Conflict, InUse
from ..models import (ModelActivateIn, AnnotationsPut, DuplicateIn, ExportIn, LabelClassesIn, LabelClassIdsIn, LabelClassPatch,
                      LabelIdsIn, LabelImportIn, LabelProjectIn, LabelProjectPatch, LabelTaskIdsIn, LabelTaskIn, ReviewIn,
                      LabelTaskPatch, ModelDetectIn, PredictAllIn, PredictIn, RemoveModelLabelsIn, TaskModelIn)

_log = vlog.get_logger('labeling')
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.webp')    # = data/sources.IMAGE_EXTS (what the import reads)
CHUNK = 1 << 20                     # upload copy size: 1 MiB
PROBE_THREADS = 8                   # header reads are I/O bound (remote: one round trip each)
INSERT_BATCH = 500
PREFETCH_NEXT = 3
WRITE_EVERY = 50                    # label-all: images per store transaction
IMG_CACHE = {'Cache-Control': 'private, max-age=86400'}


def _json(obj):
    return Response(orjson.dumps(obj), media_type='application/json')


def _media():
    from ...labeling import media                    # Agent-2 module, imported lazily
    return media


def _sinks():
    from ...labeling import sinks
    return sinks


@contextmanager
def _errors():
    """Store / registry exceptions -> HTTP errors with a human message."""
    try:
        yield
    except HTTPException:
        raise
    except Conflict as e:
        raise HTTPException(409, {'detail': str(e), 'version': e.version, 'annotations': e.annotations})
    except InUse as e:
        raise HTTPException(409, str(e))
    except ModelError as e:
        raise HTTPException(e.status, str(e))
    except KeyError as e:
        raise HTTPException(404, e.args[0] if e.args else 'not found')
    except FileNotFoundError:
        raise HTTPException(404, 'this image is missing from its source')
    except ValueError as e:
        raise HTTPException(400, str(e))


def safe_rel(rel):
    """A dropped file's relative path, cleaned: '/' separators, no '..', no absolute / drive part.
    None when it cannot be made safe."""
    parts = [p for p in re.split(r'[\\/]+', rel or '') if p not in ('', '.')]
    if parts and re.fullmatch(r'[A-Za-z]:', parts[0]):
        parts = parts[1:]
    if not parts or any(p == '..' or p.startswith('.') for p in parts):
        return None
    return '/'.join(parts)


def assign_names(rels, taken):
    """file_name for each new rel: its base name, or the path flattened (`sub__name.jpg`) when two files
    share a base name (dropped folders often have many `0001.jpg`). The STEMS are unique case-insensitively:
    the export writes `<stem>.txt` per image, so `a.jpg` + `a.png` must not share `a.txt`, and the label files
    must not collide on Windows / macOS file systems either."""
    stem_of = lambda n: os.path.splitext(n)[0].lower()  # noqa: E731
    taken = {stem_of(t) for t in taken}
    base_count = {}
    for r in rels:
        b = stem_of(PurePosixPath(r).name)
        base_count[b] = base_count.get(b, 0) + 1
    out = []
    for r in rels:
        b = PurePosixPath(r).name
        name = b if base_count[stem_of(b)] == 1 and stem_of(b) not in taken else r.replace('/', '__')
        stem, ext = os.path.splitext(name)
        k = 2
        while stem_of(name) in taken:
            name, k = f'{stem}_{k}{ext}', k + 1
        taken.add(stem_of(name))
        out.append(name)
    return out


def router(state, ctx):
    r = APIRouter()
    reg = getattr(ctx, 'labeling', None)
    s3cfg = (ctx.cfg or {}).get('s3', {})
    sources = {}                    # pid -> (source json, ImageSource, ImageCache)
    sources_lock = threading.Lock()
    runs = {}                       # run_id -> {pid, kind, job_id, path?}
    threading.Thread(target=colors.palette, daemon=True, name='label-colors').start()   # ~300 ms, once

    def run_io(fn, *args):
        """Blocking network / disk work off the event loop."""
        return asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def need():
        if reg is None:
            raise HTTPException(501, 'labeling sessions are not enabled in this server')
        return reg

    def store(pid):
        with _errors():
            return need().store(pid)

    def project(pid):
        with _errors():
            return need().project(pid)

    def source_for(pid):
        """The project's ImageSource + ImageCache, built once per source setting (pooled connections)."""
        entry = need().get(pid)
        key = json.dumps(entry['source'], sort_keys=True)
        with sources_lock:
            hit = sources.get(pid)
            if hit and hit[0] == key:
                return hit[1], hit[2]
            m = _media()
            src = m.make_source(entry['source'], reg.project_dir(pid), ctx.datasets, s3cfg)
            cache = m.ImageCache(reg.project_dir(pid), src)
            sources[pid] = (key, src, cache)
        if hit:
            try:
                hit[1].close()
            except Exception:  # noqa: BLE001 - closing a stale pool is best effort
                pass
        return src, cache

    def drop_source(pid):
        with sources_lock:
            hit = sources.pop(pid, None)
        if hit:
            try:
                hit[1].close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ import job
    def submit_import(pid, source):
        entry = reg.get(pid)

        def fn(job):
            m = _media()
            src = m.make_source(source, reg.project_dir(pid), ctx.datasets, s3cfg)
            st = reg.store(pid)
            try:
                job.progress(stage='listing images')
                items = src.list_images()
                known = st.media_names()
                known_uris = set(known.values())
                new = [rel for rel, _ in items if rel not in known_uris]
                names = assign_names(new, known)
                job.progress(stage='reading image sizes', done=0, total=len(new))

                def probe(rel):
                    try:
                        return m.probe(src, rel)
                    except Exception as e:  # noqa: BLE001 - one unreadable file must not stop the import
                        _log.warning(f'import: cannot read {vlog.esc(rel)}: {vlog.esc(e)}')
                        return None

                added = failed = 0
                with ThreadPoolExecutor(PROBE_THREADS) as pool:
                    for i in range(0, len(new), INSERT_BATCH):
                        chunk = new[i:i + INSERT_BATCH]
                        rows = []
                        for rel, name, info in zip(chunk, names[i:i + INSERT_BATCH], pool.map(probe, chunk)):
                            if info is None:
                                failed += 1
                                continue
                            rows.append({'file_name': name, 'uri': rel, 'mime': info.get('mime'),
                                         'size_bytes': info.get('size_bytes'), 'width': info.get('width'),
                                         'height': info.get('height'),
                                         'meta': {'rel_path': rel, 'orientation': info.get('orientation')}})
                        added += st.add_media(rows)
                        job.progress(done=i + len(chunk), extra=f'{added} added')
                reg.set_counts(pid)
                _log.info(f'import [bold]{vlog.esc(entry["name"])}[/]: {added} new image(s), '
                          f'{len(items) - len(new)} already there, {failed} unreadable')
                return {'added': added, 'existing': len(items) - len(new), 'failed': failed}
            finally:
                src.close()

        job = ctx.jobs.submit(f'label-import:{pid}', f'import images · {entry["name"]}', fn, lane='io')
        return job.id

    # ------------------------------------------------------------ projects
    @r.get('/api/label/projects')
    def list_projects():
        return _json({'projects': need().list()})

    @r.post('/api/label/projects')
    def create_project(body: LabelProjectIn):
        data = body.model_dump()
        data['tasks'] = [{**t, 'classes': [c if isinstance(c, str) else {k: v for k, v in c.items() if v is not None}
                                           for c in t['classes']]} for t in data['tasks']]
        with _errors():
            entry = need().create(data)
        job_id = None
        if entry['source']['kind'] != 'upload':
            job_id = submit_import(entry['id'], entry['source'])
        return {'project': project(entry['id']), 'job_id': job_id}

    @r.get('/api/label/projects/{pid}')
    def get_project(pid: str):
        out = project(pid)
        with active_lock:
            out['model_active'] = sorted(active.get(pid, ()))
        return out

    @r.post('/api/label/projects/{pid}/tasks/{tid}/model/activate')
    def activate_model(pid: str, tid: str, body: ModelActivateIn | None = None):
        """Let the task's linked model label images in this server session (on), or stop it (off). Nothing runs
        a model until this is on: no auto-label on open, no label-all, no single-image run."""
        st = store(pid)
        with _errors():
            task = st.task(tid)
        on = True if body is None else body.on
        if on:
            model_link(task)
        with active_lock:
            s = active.setdefault(pid, set())
            (s.add if on else s.discard)(tid)
        return {'task_id': tid, 'active': on}

    @r.patch('/api/label/projects/{pid}')
    def patch_project(pid: str, body: LabelProjectPatch):
        with _errors():
            need().update(pid, name=body.name, description=body.description, export_default=body.export_default,
                          tags=body.tags, review=body.review)
        return project(pid)

    @r.delete('/api/label/projects/{pid}')
    def delete_project(pid: str, delete_files: int = 0):
        drop_source(pid)
        with _errors():
            need().delete(pid, bool(delete_files))
        return {'ok': True}

    # ------------------------------------------------------------ tasks + classes
    @r.post('/api/label/projects/{pid}/tasks')
    def add_task(pid: str, body: LabelTaskIn):
        st = store(pid)
        with _errors():
            t = st.add_task(body.name, body.task_type, body.config,
                            [c if isinstance(c, str) else c.model_dump(exclude_none=True) for c in body.classes])
            reg.set_counts(pid)
        return t

    @r.post('/api/label/projects/{pid}/tasks/reorder')
    def reorder_tasks(pid: str, body: LabelIdsIn):
        st = store(pid)
        with _errors():
            st.reorder_tasks(body.ids)
            reg.set_counts(pid)
        return {'ok': True}

    @r.patch('/api/label/projects/{pid}/tasks/{tid}')
    def patch_task(pid: str, tid: str, body: LabelTaskPatch):
        st = store(pid)
        with _errors():
            return st.update_task(tid, name=body.name, config=body.config)

    @r.delete('/api/label/projects/{pid}/tasks/{tid}')
    def delete_task(pid: str, tid: str):
        st = store(pid)
        with _errors():
            if len(st.tasks()) <= 1:
                raise ValueError('a session needs at least one task')
            st.delete_task(tid)
            reg.set_counts(pid)
        return {'ok': True}

    @r.post('/api/label/projects/{pid}/tasks/{tid}/classes')
    def add_classes(pid: str, tid: str, body: LabelClassesIn):
        st = store(pid)
        items = list(body.names or []) + [c.model_dump(exclude_none=True) for c in body.classes or []]
        if not items:
            raise HTTPException(400, 'give at least one class name')
        with _errors():
            return {'classes': st.add_classes(tid, items)}

    @r.patch('/api/label/projects/{pid}/classes/{cid}')
    def patch_class(pid: str, cid: str, body: LabelClassPatch):
        st = store(pid)
        kw = {k: getattr(body, k) for k in ('name', 'color', 'position')}
        if 'hotkey' in body.model_fields_set:                  # null = clear the hotkey; absent = keep it
            kw['hotkey'] = body.hotkey or None
        with _errors():
            return st.update_class(cid, **kw)

    @r.delete('/api/label/projects/{pid}/classes/{cid}')
    def delete_class(pid: str, cid: str, reassign_to: str | None = None, delete_annotations: int = 0):
        st = store(pid)
        with _errors():
            st.delete_class(cid, reassign_to=reassign_to or None, delete_annotations=bool(delete_annotations))
        return {'ok': True}

    # ------------------------------------------------------------ upload + import
    @r.post('/api/label/projects/{pid}/upload')
    async def upload(pid: str, request: Request):
        """Multipart: repeated `files` + parallel repeated `rel_paths` (folder drops keep their structure)."""
        need()
        with _errors():
            entry = reg.get(pid)
        if entry['source']['kind'] != 'upload':
            raise HTTPException(400, 'this session reads its images from a folder or bucket; uploads are off')
        root = reg.project_dir(pid) / 'images'
        form = await request.form(max_files=100_000, max_fields=200_000)
        try:
            files = form.getlist('files')
            rels = form.getlist('rel_paths')
            saved = skipped = 0
            for i, f in enumerate(files):
                if not hasattr(f, 'read'):
                    skipped += 1
                    continue
                rel = safe_rel(rels[i] if i < len(rels) and rels[i] else f.filename)
                if rel is None or not rel.lower().endswith(IMAGE_EXTS):
                    skipped += 1
                    continue
                dst = root / rel
                size = f.size
                if dst.is_file():
                    if size is not None and dst.stat().st_size == size:
                        skipped += 1                           # the same file dropped again
                        continue
                    # a different file at a known path: never replace an image that may carry annotations,
                    # keep both (the new one imports as a new image)
                    k = 2
                    while (alt := dst.with_name(f'{dst.stem}_{k}{dst.suffix}')).exists():
                        k += 1
                    dst = alt
                await run_io(_save_upload, f.file, dst)
                saved += 1
        finally:
            await form.close()
        return {'saved': saved, 'skipped': skipped}

    @r.post('/api/label/projects/{pid}/import')
    def start_import(pid: str, body: LabelImportIn | None = None):
        need()
        with _errors():
            entry = reg.get(pid)
            source = entry['source']
            if body is not None and body.source:
                from ...labeling.projects import clean_source
                new = clean_source(body.source)
                if new != source:
                    if reg.store(pid).media_count():
                        raise HTTPException(409, 'this session already has images from another source; '
                                                 'create a new session for this one')
                    reg.update(pid, source=new)
                    drop_source(pid)
                    source = new
            return {'job_id': submit_import(pid, source)}

    # ------------------------------------------------------------ media
    @r.get('/api/label/projects/{pid}/media')
    def media_page(pid: str, offset: int = 0, limit: int = 200, task_id: str | None = None,
                   status: str | None = None, q: str | None = None, cls: str | None = None, cls_mode: str = 'any',
                   min_ann: int | None = None, max_ann: int | None = None, review: str | None = None):
        # the image strip's filter: q = name (text / wildcard / /regex/), cls = comma-separated class ids
        st = store(pid)
        with _errors():
            return _json(st.media_page(max(0, offset), max(1, min(limit, 5000)), task_id or None, status or None,
                                       q=q, cls=[x for x in (cls or '').split(',') if x], cls_mode=cls_mode,
                                       min_ann=min_ann, max_ann=max_ann, review=review or None))

    @r.get('/api/label/projects/{pid}/media-ids')
    def media_ids(pid: str):
        return _json({'ids': store(pid).media_ids()})

    def _media_path(pid, mid, fn):
        st = store(pid)
        with _errors():
            m = st.media_row(mid)
            src, cache = source_for(pid)
        try:
            return st, m, cache, fn(cache, m)
        except (FileNotFoundError, OSError, ValueError) as e:
            _log.warning(f'image {vlog.esc(m["uri"])}: {vlog.esc(e)}')
            raise HTTPException(404, 'this image is missing from its source')

    @r.get('/api/label/projects/{pid}/image/{mid}')
    def image(pid: str, mid: str, max: int | None = None):
        st, m, cache, path = _media_path(pid, mid, lambda c, m: c.preview(m['uri'], max) if max else c.path(m['uri']))
        ids = st.media_ids()
        try:
            i = ids.index(mid)
            nxt = st.media_rows(ids[i + 1:i + 1 + PREFETCH_NEXT])
            cache.prefetch([nxt[x]['uri'] for x in ids[i + 1:i + 1 + PREFETCH_NEXT] if x in nxt])
        except ValueError:
            pass
        return FileResponse(path, media_type=m.get('mime') if not max else None, headers=IMG_CACHE)

    @r.get('/api/label/projects/{pid}/thumb/{mid}')
    def thumb(pid: str, mid: str):
        _, _, _, path = _media_path(pid, mid, lambda c, m: c.thumb(m['id'], m['uri']))
        return FileResponse(path, media_type='image/jpeg', headers=IMG_CACHE)

    # ------------------------------------------------------------ annotations + status
    @r.get('/api/label/projects/{pid}/annotations/{mid}')
    def get_annotations(pid: str, mid: str):
        st = store(pid)
        with _errors():
            return _json(st.get_annotations(mid))

    @r.put('/api/label/projects/{pid}/annotations/{mid}')
    def put_annotations(pid: str, mid: str, body: AnnotationsPut):
        st = store(pid)
        try:
            with _errors():
                return st.put_annotations(mid, body.task_id, body.annotations, body.base_version)
        except HTTPException as e:
            if e.status_code == 409 and isinstance(e.detail, dict):    # contract: {detail, version, annotations}
                return Response(orjson.dumps(e.detail), status_code=409, media_type='application/json')
            raise

    def _status(pid, mid, body, action):
        st = store(pid)
        with _errors():
            return {'status': st.set_status(mid, body.task_ids, action)}

    @r.post('/api/label/projects/{pid}/commit/{mid}')
    def commit(pid: str, mid: str, body: LabelTaskIdsIn):
        return _status(pid, mid, body, 'commit')

    @r.post('/api/label/projects/{pid}/skip/{mid}')
    def skip(pid: str, mid: str, body: LabelTaskIdsIn):
        return _status(pid, mid, body, 'skip')

    @r.post('/api/label/projects/{pid}/reopen/{mid}')
    def reopen(pid: str, mid: str, body: LabelTaskIdsIn):
        return _status(pid, mid, body, 'reopen')

    @r.post('/api/label/projects/{pid}/review')
    def review(pid: str, body: ReviewIn):
        # the optional review: 'manual' on some images, 'bulk' (Review all) on every done one not reviewed yet,
        # 'clear' to undo. Only done images are ever marked. {changed: {task_id: n}}
        st = store(pid)
        with _errors():
            return {'changed': st.set_review(body.task_ids, body.mode, body.media_ids)}

    # ------------------------------------------------------------ export
    @r.post('/api/label/projects/{pid}/export')
    def export(pid: str, body: ExportIn):
        proj = project(pid)
        st = store(pid)
        target = dict(body.target or {})
        kind = target.get('kind') or 'zip'
        if kind not in ('zip', 'local', 's3', 'ftp', 'sftp', 'db'):
            raise HTTPException(400, f'unknown export target {kind!r}')
        live = {t['id'] for t in proj['tasks']}
        task_ids = [t for t in (body.task_ids or []) if t in live] or None
        if body.task_ids and not task_ids:
            raise HTTPException(400, 'none of the picked tasks exist any more')
        conn = None
        if kind == 'db':
            with _errors():
                conn = ctx.datasets.connection(target.get('connection_id') or '')
            if (conn.get('kind') or 's3') not in ('postgres', 'mssql'):
                raise HTTPException(400, 'pick a PostgreSQL or SQL Server connection')
        elif kind in ('s3', 'ftp', 'sftp') and not target.get('connection_id'):
            raise HTTPException(400, 'pick a saved connection for the export')
        elif kind == 'local' and not str(target.get('path') or '').strip():
            raise HTTPException(400, 'pick the folder to export into')
        run_id = uuid.uuid4().hex
        classes = {t: list(v) for t, v in (body.classes or {}).items() if t in live} or None
        options = {'task_ids': task_ids, 'include_images': body.include_images, 'only_done': body.only_done,
                   'overwrite': body.overwrite, 'run_id': run_id, 'create_views': bool(target.get('create_views')),
                   'target': {k: v for k, v in target.items() if k != 'conn'}, 'classes': classes}
        # remembered for "Re-export": the target's fields stay at the top level too (older pages read them there)
        tgt = {k: v for k, v in target.items() if k != 'conn'}
        with _errors():
            reg.update(pid, export_default={**tgt, 'target': tgt, 'task_ids': task_ids, 'classes': classes,
                                            'include_images': body.include_images, 'only_done': body.only_done})
        run_dir = reg.project_dir(pid) / 'exports' / run_id

        def image_path(m):
            _, cache = source_for(pid)
            try:
                return cache.path(m['uri'])
            except Exception:  # noqa: BLE001 - a missing image is reported in the summary, not fatal
                return None

        def fn(job):
            if kind == 'db':
                tgt = {'kind': 'db', 'conn': conn}
            else:
                tgt = _sinks().make_sink(target, ctx.datasets, s3cfg, run_dir)
            src, _ = source_for(pid)
            # stat-only existence check for on-disk sources (remote ones would need a download per image)
            exists = (lambda m: (p := src.local_path(m['uri'])) is not None and p.is_file()) \
                if src.kind in ('local', 'upload') else None
            res = run_export(job, proj, st, tgt, options, image_path=image_path, image_exists=exists)
            if res.get('path'):
                runs[run_id]['path'] = res['path']
            return res

        runs[run_id] = {'pid': pid, 'kind': kind, 'job_id': None}       # before submit: the job may finish first
        job = ctx.jobs.submit(f'label-export:{pid}:{run_id}', f'export {proj["name"]} → {kind}', fn, lane='io')
        runs[run_id]['job_id'] = job.id
        return {'job_id': job.id, 'run_id': run_id}

    def _zip_path(run_id):
        info = runs.get(run_id) or {}
        if info.get('path'):
            return Path(info['path'])
        if reg is not None and re.fullmatch(r'[0-9a-f]{32}', run_id):
            for p in reg.root.glob(f'*/exports/{run_id}/*.zip'):        # after a restart: find it on disk
                return p
        return None

    @r.get('/api/label/exports/{run_id}')
    def export_status(run_id: str):
        info = runs.get(run_id)
        path = _zip_path(run_id)
        if info is None:
            if path is None:
                raise HTTPException(404, 'no such export (it may have been from before a restart)')
            return {'run_id': run_id, 'status': 'done', 'kind': 'zip', 'summary': None,
                    'download': f'/api/label/exports/{run_id}/download'}
        job = ctx.jobs.get(info['job_id'])
        status = job.status if job else ('done' if path else 'unknown')
        out = {'run_id': run_id, 'status': status, 'kind': info['kind'],
               'summary': job.result if job and job.status == 'done' else None,
               'error': job.error if job else None}
        if info['kind'] == 'zip' and status == 'done' and path and path.is_file():
            out['download'] = f'/api/label/exports/{run_id}/download'
        return out

    @r.get('/api/label/exports/{run_id}/download')
    def export_download(run_id: str):
        path = _zip_path(run_id)
        if path is None or not path.is_file():
            raise HTTPException(404, 'this export has no zip (not finished, failed, or not a zip export)')
        info = runs.get(run_id) or {}
        name = 'labels'
        if info.get('pid'):
            try:
                name = re.sub(r'[^\w.-]+', '_', reg.get(info['pid'])['name']).strip('_') or name
            except KeyError:
                pass
        return FileResponse(path, media_type='application/zip', filename=f'{name}-{run_id[:8]}.zip')

    # ------------------------------------------------------------ models (v2)
    def model_conn(cid):
        """A saved connection of kind 'model' (400 when it is missing or of another kind)."""
        try:
            conn = ctx.datasets.connection(cid or '')
        except KeyError:
            raise HTTPException(400, 'the model connection no longer exists; pick another one in Model settings')
        if conn.get('kind') != 'model':
            raise HTTPException(400, 'pick a Model connection')
        return conn

    def model_link(task):
        link = (task.get('config') or {}).get('model')
        if not link or not link.get('model_id') or not (link.get('connection_id') or link.get('registry_id')):
            raise HTTPException(400, f'task {task["name"]!r} has no model linked: open Model settings first')
        return link

    # ---- a linked model runs only after it is activated in this server session (never by itself after a restart)
    active = {}                     # pid -> {task ids}
    active_lock = threading.Lock()

    def is_active(pid, tid):
        with active_lock:
            return tid in active.get(pid, ())

    def need_active(pid, task):
        if not is_active(pid, task['id']):
            raise HTTPException(409, f'the model of {task["name"]!r} is not active: activate it for this session '
                                     '(model menu › Activate) before it labels anything')

    def registry_model(mid):
        if getattr(ctx, 'models', None) is None:
            raise HTTPException(400, 'models are not enabled in this server')
        try:
            return ctx.models.model(mid)
        except KeyError:
            raise HTTPException(400, 'that model is no longer in Settings › Models; pick another one') from None

    def workdir():
        svc = getattr(ctx, 'model_service', None)
        return svc.workdir if svc is not None else ctx.models.store / '_converted'

    def adapter_for(link):
        """The adapter of a link: a Settings › Models model (models/hub.py) or a model server connection."""
        from ...models import hub
        if link.get('registry_id'):
            return hub.adapter(ctx.models, registry_model(link['registry_id']), workdir())
        return models.adapter(model_conn(link['connection_id']), link['model_id'], link.get('version'))

    def map_predictions(task, link, preds):
        """Model class names -> this task's class ids through the link's class_map (same name, case-insensitive,
        for names the map does not list); 'ignore' and unknown classes are dropped."""
        cmap = link.get('class_map') or {}
        live = {c['id'] for c in task['classes']}
        by_name = {c['name'].casefold(): c['id'] for c in task['classes']}
        out = []
        for p in preds:
            v = cmap.get(p['class_name'])
            cid = v if v in live else by_name.get(p['class_name'].casefold()) if v in (None, 'new') else None
            if cid is not None:
                out.append({**p, 'class_id': cid})
        return out

    @r.post('/api/label/models/detect')
    async def detect_model(body: ModelDetectIn):
        need()
        if body.registry_id:
            from ...models import hub
            m = registry_model(body.registry_id)
            with _errors():
                info = await run_io(hub.info, ctx.models, m, workdir())
            return {'protocol': 'registry', 'models': [info]}
        if body.connection_id:
            conn = model_conn(body.connection_id)
        elif body.connection:
            with _errors():
                conn = ctx.datasets.merged_connection({**body.connection, 'kind': 'model'})
        else:
            raise HTTPException(400, 'give a connection_id or a connection {url}')
        with _errors():
            return await run_io(models.detect, conn, True)

    @r.put('/api/label/projects/{pid}/tasks/{tid}/model')
    def put_task_model(pid: str, tid: str, body: TaskModelIn | None = None):
        """Link a model to a task (or unlink with a null body). 'new' class_map entries become classes."""
        st = store(pid)
        with _errors():
            task = st.task(tid)
            cfg = dict(task['config'])
            if body is None:
                cfg.pop('model', None)
                out = st.update_task(tid, config=cfg)
            else:
                if body.registry_id:
                    from ...models import hub
                    m = hub.info(ctx.models, registry_model(body.registry_id), workdir())
                elif body.connection_id:
                    _, m = models.find_model(model_conn(body.connection_id), body.model_id, fresh=True)
                else:
                    raise ValueError('pick a model: one of Settings › Models, or a model server connection')
                if m['task_type'] != task['task_type']:
                    raise ValueError(f'{m["name"]} is a {m["task_type"].replace("_", " ")} model; task '
                                     f'{task["name"]!r} needs a {task["task_type"].replace("_", " ")} model')
                n_kpt = len(cfg.get('keypoints') or [])
                if task['task_type'] == 'keypoints' and m.get('keypoints') and len(m['keypoints']) != n_kpt:
                    raise ValueError(f'{m["name"]} returns {len(m["keypoints"])} keypoints; this task has {n_kpt}')
                if not 0 <= body.conf <= 1:
                    raise ValueError('confidence must be between 0 and 1')
                with st.write():                         # new classes + the link: all or nothing
                    new = [n for n, v in body.class_map.items() if v == 'new']
                    if new:
                        st.add_classes(tid, new)         # names that exist already are skipped (and mapped below)
                    classes = st.task(tid)['classes']
                    ids = {c['id'] for c in classes}
                    by_name = {c['name'].casefold(): c['id'] for c in classes}
                    cmap = {}
                    for name, v in body.class_map.items():
                        if v == 'new':
                            cmap[name] = by_name[name.strip().casefold()]
                        elif v in ids:
                            cmap[name] = v
                        else:                            # 'ignore', or a class deleted since the map was sent
                            cmap[name] = 'ignore'
                    src = ({'registry_id': body.registry_id, 'model_id': body.registry_id, 'name': m.get('name')}
                           if body.registry_id else {'connection_id': body.connection_id, 'model_id': body.model_id})
                    cfg['model'] = {**src, 'version': body.version, 'conf': body.conf, 'auto_on_open': body.auto_on_open,
                                    'class_map': cmap}
                    out = st.update_task(tid, config=cfg)
            reg.set_counts(pid)
        with active_lock:                                # another (or no) model: it must be activated again
            active.get(pid, set()).discard(tid)
        return out

    @r.post('/api/label/projects/{pid}/predict/{mid}')
    def predict_one(pid: str, mid: str, body: PredictIn):
        """Run the task's model on one image now and store the result (model rows replaced, manual kept)."""
        t0 = time.perf_counter()
        st = store(pid)
        with _errors():
            task = st.task(body.task_id)
            link = model_link(task)
            need_active(pid, task)
            m = st.media_row(mid)
            _, cache = source_for(pid)
        try:
            data = cache.path(m['uri']).read_bytes()
        except (OSError, ValueError) as e:
            _log.warning(f'predict: image {vlog.esc(m["uri"])}: {vlog.esc(e)}')
            raise HTTPException(404, 'this image is missing from its source')
        conf = body.conf if body.conf is not None else link.get('conf')
        with _errors():
            ad = adapter_for(link)
            preds = ad.predict(data, conf, (m['width'], m['height']), m['file_name'])
            res = st.apply_predictions(mid, task['id'], map_predictions(task, link, preds))
        return _json({**res, 'ms': round((time.perf_counter() - t0) * 1000, 1)})

    @r.post('/api/label/projects/{pid}/predict-all')
    def predict_all(pid: str, body: PredictAllIn):
        """The label-all job ('model' lane): batches of models.BATCH images, models.IN_FLIGHT calls open,
        a store write every WRITE_EVERY images, cancel checked between batches."""
        st = store(pid)
        with _errors():
            entry = reg.get(pid)
            task = st.task(body.task_id)
            link = model_link(task)
            need_active(pid, task)
            if not link.get('registry_id'):
                model_conn(link['connection_id'])        # 400 now, not in the job, when the connection is gone
            if body.scope not in ('unlabeled', 'not_done', 'all'):
                raise ValueError(f'unknown scope {body.scope!r} (unlabeled, not_done or all)')
        tid, scope = task['id'], body.scope
        conf = body.conf if body.conf is not None else link.get('conf')

        def fn(job):
            ad = adapter_for(link)
            _, cache = source_for(pid)
            n, batches = st.predict_targets(tid, scope)
            job.progress(stage=f'{link["model_id"]} · {task["name"]}', done=0, total=n)
            t_start = time.time()
            stats = {'n_images': 0, 'n_shapes': 0, 'failed': 0, 'seen': 0}
            buf = []
            fails = {'batches': 0, 'last': None}

            def run(chunk):
                items, mids, missing = [], [], 0
                for mid, uri, w, h, name in chunk:
                    try:
                        items.append((cache.path(uri).read_bytes(), (w, h), name))
                        mids.append(mid)
                    except Exception as e:  # noqa: BLE001 - a missing image is counted, not fatal
                        _log.debug(f'label-all: {vlog.esc(uri)}: {vlog.esc(e)}')
                        missing += 1
                return list(zip(mids, ad.predict_batch(items, conf) if items else [])), missing

            def flush():
                if buf:
                    res = st.apply_predictions_many(tid, buf, scope=scope)
                    stats['n_images'] += len(res)
                    stats['n_shapes'] += sum(x['n_model'] for x in res.values())
                    buf.clear()

            def collect(fut, size):
                try:
                    pairs, missing = fut.result()
                except Exception as e:  # noqa: BLE001 - the whole call failed (server down, 5xx)
                    pairs, missing = [], size
                    fails['last'] = e
                stats['failed'] += missing
                ok = 0
                for mid, res in pairs:
                    if isinstance(res, Exception):
                        stats['failed'] += 1
                        fails['last'] = res
                    else:
                        buf.append((mid, map_predictions(task, link, res)))
                        ok += 1
                fails['batches'] = 0 if ok else fails['batches'] + 1
                stats['seen'] += size
                if len(buf) >= WRITE_EVERY:
                    flush()
                rate = stats['seen'] / max(1e-6, time.time() - t_start)
                job.progress(done=stats['seen'], extra=f'{rate:.1f} img/s · {stats["n_shapes"]:,} shapes')

            cancelled = False
            pending = deque()
            try:
                with ThreadPoolExecutor(models.IN_FLIGHT, thread_name_prefix='label-model') as pool:
                    for rows in batches:
                        for i in range(0, len(rows), models.BATCH):
                            if job.cancelled.is_set():
                                cancelled = True
                                break
                            # nothing came back at all from the last 3 batches: the server is down / broken
                            if fails['batches'] >= 3 and not stats['n_images'] and not buf:
                                break
                            while len(pending) >= models.IN_FLIGHT:
                                collect(*pending.popleft())
                            chunk = rows[i:i + models.BATCH]
                            pending.append((pool.submit(run, chunk), len(chunk)))
                        else:
                            continue
                        break
                    while pending:                   # what is in flight is kept, even after a cancel
                        collect(*pending.popleft())
            finally:
                batches.close()
                flush()
            if fails['batches'] >= 3 and not stats['n_images']:
                raise ModelError(f'the model failed on every image: {fails["last"]}', 502)
            took = time.time() - t_start
            _log.info(f'label-all [bold]{vlog.esc(task["name"])}[/]: {stats["n_images"]} image(s), '
                      f'{stats["n_shapes"]} shape(s), {stats["failed"]} failed'
                      f'{" (cancelled)" if cancelled else ""} [dim]({stats["seen"] / max(took, 1e-6):.1f} img/s)[/]')
            return {'n_images': stats['n_images'], 'n_shapes': stats['n_shapes'], 'failed': stats['failed'],
                    'cancelled': cancelled, 'seconds': round(took, 2)}

        job = ctx.jobs.submit(f'label-predict:{pid}:{tid}', f'label all · {entry["name"]} · {task["name"]}', fn,
                              lane='model', public_result=True)
        return {'job_id': job.id}

    @r.post('/api/label/projects/{pid}/remove-model-labels')
    def remove_model_labels(pid: str, body: RemoveModelLabelsIn):
        st = store(pid)
        with _errors():
            return st.remove_model_annotations(body.task_id, body.media_ids)

    # ------------------------------------------------------------ class stats + bulk delete (v2)
    @r.get('/api/label/projects/{pid}/class-stats')
    def class_stats(pid: str):
        return _json({'stats': store(pid).class_stats()})

    @r.post('/api/label/projects/{pid}/classes/delete')
    def delete_classes(pid: str, body: LabelClassIdsIn):
        st = store(pid)
        with _errors():
            return st.delete_classes(body.class_ids)

    # ------------------------------------------------------------ duplicate (v2)
    @r.post('/api/label/projects/{pid}/duplicate')
    def duplicate(pid: str, body: DuplicateIn | None = None):
        need()
        with _errors():
            entry = reg.get(pid)
        name = ((body.name if body else None) or '').strip() or None
        if name:
            with _errors():
                reg.check_name(name)            # a typed name that is taken: 400 now, not a failed job

        def fn(job):
            t0 = time.time()
            new = reg.duplicate(pid, name, job.progress)
            _log.info(f'duplicated [bold]{vlog.esc(entry["name"])}[/] -> [bold]{vlog.esc(new["name"])}[/] '
                      f'[dim]({new["n_media"]} images, {time.time() - t0:.1f}s)[/]')
            return reg.card(new)

        job = ctx.jobs.submit(f'label-dup:{pid}', f'duplicate · {entry["name"]}', fn, lane='io', public_result=True)
        return {'job_id': job.id}

    # ------------------------------------------------------------ colours + settings
    @r.get('/api/label/colors')
    def get_colors(n: int = 64):
        n = max(1, min(n, 256))
        de = colors.min_de(n)
        return {'colors': colors.palette(n), 'min_de': [None if d == float('inf') else round(d, 2) for d in de]}

    def settings_path():
        return need().root.parent / 'label_settings.json'

    @r.get('/api/label/settings')
    def get_settings():
        p = settings_path()
        try:
            return json.loads(p.read_text()) if p.is_file() else {}
        except ValueError:
            return {}

    @r.put('/api/label/settings')
    async def put_settings(request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'settings must be a JSON object')
        if not isinstance(body, dict):
            raise HTTPException(400, 'settings must be a JSON object')
        await run_io(_atomic_json, settings_path(), body)
        return body

    return r


def _save_upload(fileobj, dst):
    """Stream an uploaded file to dst in 1 MiB chunks (tmp then replace: never a half image on disk)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f'.{dst.name}.{uuid.uuid4().hex[:8]}.part')
    try:
        fileobj.seek(0)
        with open(tmp, 'wb') as out:
            while chunk := fileobj.read(CHUNK):
                out.write(chunk)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f'.{uuid.uuid4().hex[:8]}.tmp')
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)
