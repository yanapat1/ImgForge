"""
The export job: one shared images/ folder plus one folder per task, written through a Sink (sinks.py), or
per-task runs into a PostgreSQL / MSSQL database (db_export.py).

    images/<file_name>                 once, shared by every task (optional)
    <task-slug>/labels/<stem>.txt      YOLO detect / seg / pose / cls (formats.py)
    <task-slug>/classes.txt
    <task-slug>/data.yaml
    manifest.json                      project, tasks, counts, warnings, exported_at

Rows stream from ProjectStore.iter_task_export, so memory stays flat whatever the project size. Each task
gets its own export_runs row in the local store and its own progress stage.
"""

import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

import orjson

from . import formats
from ..core import logging as vlog

_log = vlog.get_logger('labeling')
IMAGE_THREADS = 8                  # copies are I/O bound (remote fetch / upload); 8 keeps a link busy


def _stem(file_name):
    return PurePosixPath(file_name).stem


def run_export(job, project, store, target, options, image_path=None, image_exists=None):
    """Export `project` (Project JSON) from `store`.

    target: a Sink (zip / local / s3 / ftp / sftp), or {'kind': 'db', 'conn': <connection dict>}.
    options: {task_ids?, include_images=True, only_done=False, overwrite=False, run_id?, create_views?,
              classes?: {task_id: [class_id]} (a task not listed keeps every class; kept ones renumber 0..N-1)}
    image_path: callable(media_row) -> Path | None of the image bytes on disk (source path or cache).
    image_exists: callable(media_row) -> bool, a cheap check used when images are not copied (None = skip it).
    Returns the summary dict (also stored per task in export_runs)."""
    t0 = time.time()
    progress = job.progress if job is not None else (lambda **k: None)
    tasks = [t for t in project['tasks'] if not options.get('task_ids') or t['id'] in options['task_ids']]
    if not tasks:
        raise ValueError('pick at least one task to export')
    run_id = options.get('run_id') or uuid.uuid4().hex

    if isinstance(target, dict) and target.get('kind') == 'db':
        from . import db_export
        runs = {t['id']: f'{run_id}-{i}' for i, t in enumerate(tasks)}
        for t in tasks:
            store.add_export_run(runs[t['id']], t['id'], 'db', target['conn'].get('host'), options)
        try:
            res = db_export.export_to_db(target['conn'], store, project, [t['id'] for t in tasks], options, progress)
        except Exception as e:
            for r in runs.values():
                store.finish_export_run(r, 'error', error=f'{type(e).__name__}: {e}')
            raise
        for tid, r in runs.items():
            got = (res.get('tasks') or {}).get(tid, {})
            store.finish_export_run(r, 'done', got.get('n_media'), got.get('n_annotations'))
        return {'kind': 'db', 'run_id': run_id, **res, 'seconds': round(time.time() - t0, 2)}

    sink = target
    keep_classes = options.get('classes') or {}
    only_done = bool(options.get('only_done'))
    progress(stage='checking the target')
    sink.check_target(bool(options.get('overwrite')))
    warnings = {}
    missing = set()
    slugs, task_summary = {}, {}
    for t in tasks:
        slugs[t['id']] = formats.slugify(t['name'], set(slugs.values()))
    try:
        # ---- images once
        n_images = 0
        if options.get('include_images', True):
            done_any = None
            if only_done:
                done_any = set()
                for t in tasks:
                    for rows in store.iter_rows('media_task_status', {'task_id': t['id'], 'status': 'done'}):
                        done_any.update(r['media_id'] for r in rows)
            total = store.media_count() if done_any is None else len(done_any)
            progress(stage='copying images', done=0, total=total)

            def copy(m):
                p = image_path(m) if image_path else None
                if p is None or not p.is_file():
                    return m['id']
                sink.copy_file(f"images/{m['file_name']}", p)
                return None

            with ThreadPoolExecutor(IMAGE_THREADS) as pool:
                pending = deque()
                for rows in store.iter_rows('media', batch=500):
                    batch = [m for m in rows if done_any is None or m['id'] in done_any]
                    pending.extend(pool.submit(copy, m) for m in batch)
                    # drain as we go so futures (and their rows) never pile up for a 1M-image project
                    while len(pending) > 2000:
                        mid = pending.popleft().result()
                        n_images += 1
                        if mid:
                            missing.add(mid)
                        progress(done=n_images)
                for f in pending:
                    mid = f.result()
                    n_images += 1
                    if mid:
                        missing.add(mid)
                    if n_images % 50 == 0:
                        progress(done=n_images)
            n_images -= len(missing)
            if missing:
                warnings['images_missing'] = len(missing)
                _log.warning(f'export: {len(missing)} image(s) missing from the source were left out')

        # ---- labels per task
        checked = {}                                   # media id -> image exists (images not copied)
        for ti, t in enumerate(tasks):
            run = f'{run_id}-{ti}'
            store.add_export_run(run, t['id'], sink.kind, sink.uri, options)
            slug = slugs[t['id']]
            keep = keep_classes.get(t['id'])
            index, names = formats.class_map(t['classes'], keep)
            excluded = [c['name'] for c in t['classes'] if c['id'] not in index]
            tw = {}
            n_media = n_ann = 0
            total = store.media_count()
            progress(stage=f'labels: {t["name"]}', done=0, total=total)
            try:
                for k, (m, rows) in enumerate(store.iter_task_export(t['id'], only_done)):
                    if k % 500 == 0:
                        progress(done=k)
                    if m['id'] in missing:
                        continue
                    if not options.get('include_images', True) and image_exists is not None:
                        ok = checked.get(m['id'])
                        if ok is None:
                            ok = checked[m['id']] = bool(image_exists(m))
                        if not ok:
                            missing.add(m['id'])
                            continue
                    if keep is not None:
                        # excluded classes: their rows go (pose parents of another task stay, as boxes only)
                        parents = {r.get('parent_id') for r in rows if r.get('class_id') in index}
                        rows = [r for r in rows if r.get('_parent_only') or r.get('class_id') in index
                                or r['id'] in parents]
                    if not rows and m['status'] != 'done':
                        continue                       # never looked at: no file (done + empty = a labeled negative)
                    sink.write(f'{slug}/labels/{_stem(m["file_name"])}.txt',
                               formats.label_text(t, rows, index, tw).encode())
                    n_media += 1
                    n_ann += sum(1 for r in rows if not r.get('_parent_only'))
                sink.write(f'{slug}/classes.txt', formats.classes_txt(names).encode())
                sink.write(f'{slug}/data.yaml', formats.data_yaml(t, names).encode())
            except Exception as e:
                store.finish_export_run(run, 'error', n_media, n_ann, f'{type(e).__name__}: {e}')
                raise
            store.finish_export_run(run, 'done', n_media, n_ann)
            task_summary[t['id']] = {'name': t['name'], 'task_type': t['task_type'], 'folder': slug,
                                     'n_media': n_media, 'n_annotations': n_ann, 'classes': names,
                                     **({'excluded_classes': excluded} if excluded else {}),
                                     **({'warnings': tw} if tw else {})}
            for k, v in tw.items():
                warnings[k] = warnings.get(k, 0) + v
            progress(done=total)

        if missing and not options.get('include_images', True):
            warnings['images_missing'] = len(missing)
            _log.warning(f'export: {len(missing)} image(s) missing from the source were left out')
        manifest = {'project': {'id': project['id'], 'name': project['name']}, 'exported_at': time.time(),
                    'run_id': run_id, 'only_done': only_done, 'n_images': n_images, 'tasks': task_summary,
                    'warnings': warnings,
                    'excluded_classes': {s['folder']: s['excluded_classes'] for s in task_summary.values()
                                         if s.get('excluded_classes')},
                    'layout': 'images/ shared by every task; <task>/labels/<image stem>.txt, classes.txt, data.yaml'}
        sink.write('manifest.json', orjson.dumps(manifest, option=orjson.OPT_INDENT_2))
        progress(stage='finishing')
    except BaseException:
        # a failed zip must not leave a half archive that looks downloadable
        (getattr(sink, 'abort', None) or sink.close)()
        raise
    closed = sink.close()
    return {'kind': sink.kind, 'run_id': run_id, 'uri': (closed or {}).get('uri', sink.uri),
            'path': (closed or {}).get('path'), 'files': (closed or {}).get('files'),
            'bytes': (closed or {}).get('bytes'), 'n_images': n_images, 'tasks': task_summary,
            'warnings': warnings, 'seconds': round(time.time() - t0, 2)}
