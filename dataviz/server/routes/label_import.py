"""
Annotation import into labeling sessions (/api/label/annotations/*, /api/label/projects/{pid}/import-annotations):
YOLO label folders, COCO JSON, CSV / TSV / text tables and databases, from a local path, S3, FTP, SFTP, an upload or
a database connection. Parsing: labeling/importers.py; storing: ProjectStore.import_annotations.

    POST /api/label/annotations/upload        multipart `files` (.json, .csv / .tsv / .txt, .zip of YOLO labels,
                                              classes.txt / data.yaml) -> {upload_id, files, bytes}
    POST /api/label/annotations/preview       {format: auto|yolo|coco|text|db, source, task_type, n_kpt?, coords?,
                                              project_id?, task_id?} -> {format, records, images, classes:[{name, n,
                                              match}], matched?, unmatched_sample?, skipped, errors}
    POST /api/label/projects/{pid}/import-annotations
                                              {task_id, format, source, mode: merge|replace|skip_labeled, class_map?,
                                              add_classes?, coords?} -> {job_id}; the job result (public) is
                                              {annotations, images, unmatched, unmatched_sample, classes_added, skipped}

source: {kind: upload, upload_id} | {kind: local, path} | {kind: s3, connection_id, bucket, prefix}
      | {kind: ftp|sftp, connection_id, path} | {kind: db, connection_id, mode: dataviz|sql, sql?, task_name?}
A local path / S3 prefix / remote path may name one file (a COCO .json, a .csv) instead of a folder.
The import job runs on the 'io' lane after the session's image import (one worker per lane, FIFO), so an import
started right after a session is created sees its images.
"""

import posixpath
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ...core import logging as vlog
from ...labeling import importers as imp

_log = vlog.get_logger('label')

EXTS = {'yolo': ('.txt', '.yaml', '.yml'), 'coco': ('.json',), 'text': ('.csv', '.tsv', '.txt'),
        'auto': ('.txt', '.yaml', '.yml', '.json', '.csv', '.tsv')}
UPLOAD_EXTS = ('.json', '.csv', '.tsv', '.txt', '.zip', '.yaml', '.yml')
UPLOAD_TTL = 86400                  # s: upload folders older than this are removed on the next upload
PREVIEW_DB_ROWS = 5000
BATCH_IMAGES = 2000                 # images per store transaction


def router(state, ctx):
    r = APIRouter()
    reg = getattr(ctx, 'labeling', None)
    s3cfg = (ctx.cfg or {}).get('s3', {})

    def need():
        if reg is None:
            raise HTTPException(501, 'labeling sessions are not enabled in this server')
        return reg

    def uploads_root():
        return Path(need().root) / '_imports'

    def upload_dir(uid):
        if not uid or not all(ch in '0123456789abcdef' for ch in uid) or len(uid) != 32:
            raise ValueError('unknown upload')
        d = uploads_root() / uid
        if not d.is_dir():
            raise ValueError('the uploaded files are gone (older than a day?): upload them again')
        return d

    # ------------------------------------------------------------ reading the source
    def read_files(source, fmt):
        """{rel: bytes} of the label files a source holds (one file when it names a file)."""
        kind = (source or {}).get('kind')
        exts = EXTS.get(fmt, EXTS['auto'])
        if kind == 'upload':
            d = upload_dir(source.get('upload_id', ''))
            out = {}
            for p in sorted(d.rglob('*')):
                if not p.is_file():
                    continue
                rel = p.relative_to(d).as_posix()
                if p.suffix.lower() == '.zip':
                    out.update({f'{p.stem}/{k}': v for k, v in imp.unzip(p.read_bytes(), exts).items()})
                elif rel.lower().endswith(exts):
                    out[rel] = p.read_bytes()
            return out
        from ...labeling import media
        single = None
        spec = dict(source)
        if kind == 'local':
            p = Path(str(spec.get('path') or '')).expanduser()
            if p.is_file():
                return {p.name: p.read_bytes()}
            spec['recursive'] = True
        elif kind == 's3':
            pre = str(spec.get('prefix') or '')
            if pre.lower().endswith(exts) and not pre.endswith('/'):
                single = posixpath.basename(pre)
                spec['prefix'] = posixpath.dirname(pre)
        elif kind in ('ftp', 'sftp'):
            pth = str(spec.get('path') or '')
            if pth.lower().endswith(exts):
                single = posixpath.basename(pth)
                spec['path'] = posixpath.dirname(pth) or '/'
        else:
            raise ValueError(f'unknown source kind {kind!r}')
        src = media.make_source(spec, reg.root, ctx.datasets, s3cfg)
        try:
            if single:
                items = src.list_images(match=lambda n: posixpath.basename(n) == single)
                items = [it for it in items if '/' not in it[0]]
                if not items:
                    raise ValueError(f'{single} not found')
                return {single: src.read_bytes(items[0][0])}
            return imp.read_files(src, exts)
        finally:
            src.close()

    def records(body, rep, limit=None):
        """(format, records, class names | None) of a request body."""
        fmt = body.get('format') or 'auto'
        source = body.get('source') or {}
        task_type = body.get('task_type') or 'object_detection'
        n_kpt = int(body.get('n_kpt') or 0)
        coords = body.get('coords') or 'auto'
        if coords not in ('auto', 'norm', 'px'):
            raise ValueError('coords must be auto, norm or px')
        if fmt == 'db' or source.get('kind') == 'db':
            conn = ctx.datasets.connection(source.get('connection_id') or '')
            if conn.get('kind') not in ('postgres', 'mssql'):
                raise ValueError('pick a PostgreSQL or SQL Server connection')
            recs = imp.database(conn, task_type, rep, source.get('mode') or 'dataviz', source.get('sql'),
                                source.get('task_name') or None, limit, coords)
            return 'db', recs, None
        if fmt not in ('auto',) + imp.FORMATS:
            raise ValueError(f'unknown format {fmt!r}')
        files = read_files(source, fmt)
        if not files:
            raise ValueError('no label files found there (.txt / .json / .csv)')
        if fmt == 'auto':
            fmt = imp.guess_format(files)
        recs, names = imp.records_from_files(fmt, files, task_type, n_kpt, rep, coords)
        return fmt, recs, names

    def task_of(pid, tid):
        st = need().store(pid)
        t = st.task(tid)
        if not t or t.get('is_deleted'):
            raise ValueError('unknown task')
        return st, t

    # ------------------------------------------------------------ routes
    @r.post('/api/label/annotations/upload')
    async def upload(request: Request):
        need()
        root = uploads_root()
        root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for d in root.iterdir():                          # leftovers of imports never run
            try:
                if d.is_dir() and now - d.stat().st_mtime > UPLOAD_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
        uid = uuid.uuid4().hex
        dst = root / uid
        form = await request.form(max_files=100_000, max_fields=200_000)
        n = size = 0
        try:
            files = form.getlist('files')
            rels = form.getlist('rel_paths')
            for i, f in enumerate(files):
                if not hasattr(f, 'read'):
                    continue
                rel = (rels[i] if i < len(rels) and rels[i] else f.filename or '').replace('\\', '/').lstrip('/')
                if not rel or '..' in rel.split('/') or not rel.lower().endswith(UPLOAD_EXTS):
                    continue
                p = dst / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                data = await f.read()
                p.write_bytes(data)
                n += 1
                size += len(data)
        finally:
            await form.close()
        if not n:
            shutil.rmtree(dst, ignore_errors=True)
            raise HTTPException(400, 'no label file in the upload (.json, .csv, .tsv, .txt, .zip, classes.txt, data.yaml)')
        return {'upload_id': uid, 'files': n, 'bytes': size}

    @r.post('/api/label/annotations/preview')
    def preview(body: dict):
        """Parse (a database: the first rows) and say what an import would do; nothing is written."""
        need()
        rep = imp.Report()
        pid, tid = body.get('project_id'), body.get('task_id')
        try:
            if pid and tid:
                st, task = task_of(pid, tid)
                body = {**body, 'task_type': task['task_type'], 'n_kpt': len((task.get('config') or {}).get('keypoints') or [])}
            fmt, recs, names = records(body, rep, limit=PREVIEW_DB_ROWS)
            if pid and tid and fmt == 'yolo' and not names:     # no classes.txt: index i is the task's i-th class
                names = [c['name'] for c in task['classes']] or None
        except (ValueError, KeyError, OSError) as e:
            raise HTTPException(400, str(e) or type(e).__name__)
        except Exception as e:  # noqa: BLE001 - S3 / FTP / DB driver errors carry a readable message
            raise HTTPException(400, f'{type(e).__name__}: {e}')
        cls = Counter()
        for rc in recs:
            c = rc.cls
            if isinstance(c, int):
                c = names[c] if names and 0 <= c < len(names) else f'#{c}'
            cls[str(c)] += 1
        keys = {rc.key for rc in recs}
        out = {'format': fmt, 'records': len(recs), 'images': len(keys), 'class_names': names, **rep.to_dict()}
        existing = {}
        if pid and tid:
            existing = {c['name'].casefold(): c['name'] for c in task['classes']}
            idx = imp.media_index(st.media_for_import())
            hit = {k for k in keys if imp.find(idx, k)}
            out['matched'] = len(hit)
            out['unmatched_sample'] = sorted(keys - hit)[:20]
            out['project_images'] = st.media_count()
        out['classes'] = [{'name': n, 'n': c, 'match': existing.get(n.casefold())}
                          for n, c in cls.most_common()]
        return out

    @r.post('/api/label/projects/{pid}/import-annotations')
    def start(pid: str, body: dict):
        need()
        try:
            entry = reg.get(pid)
            st, task = task_of(pid, body.get('task_id') or '')
        except (KeyError, ValueError) as e:
            raise HTTPException(404, str(e))
        mode = body.get('mode') or 'merge'
        if mode not in ('merge', 'replace', 'skip_labeled'):
            raise HTTPException(400, 'mode must be merge, replace or skip_labeled')
        spec = {**body, 'task_type': task['task_type'], 'n_kpt': len((task.get('config') or {}).get('keypoints') or [])}
        upload_id = (body.get('source') or {}).get('upload_id') if (body.get('source') or {}).get('kind') == 'upload' else None
        if upload_id:
            try:
                upload_dir(upload_id)
            except ValueError as e:
                raise HTTPException(400, str(e))

        def fn(job):
            rep = imp.Report()
            job.progress(stage='reading the annotations')
            fmt, recs, names = records(spec, rep)
            if fmt == 'yolo' and not names:                       # no classes.txt: index i is the task's i-th class
                names = [c['name'] for c in reg.store(pid).task(task['id'])['classes']] or None
            job.progress(stage='matching images', extra=f'{len(recs):,} shapes')
            store = reg.store(pid)
            idx = imp.media_index(store.media_for_import())
            items, unmatched = imp.resolve(recs, idx, names, rep)
            by_mid = {}
            for it in items:
                by_mid.setdefault(it[0], []).append(it)
            mids = list(by_mid)
            total = {'annotations': 0, 'images': 0, 'classes_added': [], 'skipped': Counter(rep.skipped)}
            cmap = body.get('class_map') or {}
            add = body.get('add_classes', True) is not False
            job.progress(stage='storing', done=0, total=len(mids))
            for i in range(0, len(mids), BATCH_IMAGES):
                if job.cancelled.is_set():
                    break
                chunk = [it for m in mids[i:i + BATCH_IMAGES] for it in by_mid[m]]
                res = store.import_annotations(task['id'], chunk, mode, cmap, add)
                total['annotations'] += res['annotations']
                total['images'] += res['images']
                total['classes_added'] += res['classes_added']
                total['skipped'].update(res['skipped'])
                job.progress(done=min(i + BATCH_IMAGES, len(mids)), extra=f'{total["annotations"]:,} shapes')
            reg.set_counts(pid)
            if upload_id and not job.cancelled.is_set():
                shutil.rmtree(uploads_root() / upload_id, ignore_errors=True)
            _log.info(f'annotations imported into [bold]{vlog.esc(entry["name"])}[/] › {vlog.esc(task["name"])}: '
                      f'{total["annotations"]:,} shapes on {total["images"]:,} images ({fmt}), '
                      f'{sum(unmatched.values()):,} shapes of {len(unmatched):,} unknown images skipped')
            return {'format': fmt, 'annotations': total['annotations'], 'images': total['images'],
                    'unmatched': len(unmatched), 'unmatched_sample': sorted(unmatched)[:20],
                    'classes_added': total['classes_added'], 'skipped': dict(total['skipped']), 'errors': rep.errors}

        job = ctx.jobs.submit(f'label-ann-import:{pid}:{task["id"]}', f'import annotations · {entry["name"]} · {task["name"]}',
                              fn, lane='io', public_result=True)
        return {'job_id': job.id}

    return r

