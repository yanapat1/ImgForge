"""
Sessions (saved datasets: local folders, S3 / RustFS prefixes, a task of a labeling session) and connections: the Sessions home
cards (with stats and a crop mosaic from the session's workspace), opening one (hot-swaps the served
dataset), cache-mode sync, connection test / browsing (S3 buckets, FTP / SFTP folders, database SELECT 1)
and the local folder browser. Network and disk work runs off the event loop (run_io).
"""

import json
import time
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...core import logging as vlog
from ...data import sources
from ...paths import PROJECT_ROOT
from ...workspace import find_workspace


def router(state, ctx):
    r = APIRouter()

    # ------------------------------------------------------------ datasets + S3 connections (Settings)
    reg = ctx.datasets
    s3cfg = ctx.cfg.get('s3', {})
    ds_log = vlog.get_logger('datasets')

    def need_registry():
        if reg is None:
            raise HTTPException(501, 'dataset switching is not enabled in this server')

    def run_io(fn, *args):
        """Blocking network / disk work off the event loop."""
        return asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def active_id():
        st = ctx.state
        return st.dataset_id if st is not None else None

    N_COVER = 4                                  # crops in a card's mosaic
    manifest_memo = {}                           # manifest path -> (mtime_ns, summary): big for large buckets

    def mirror_summary(man):
        try:
            mt = man.stat().st_mtime_ns
        except OSError:
            return None
        hit = manifest_memo.get(man)
        if hit is None or hit[0] != mt:
            try:
                m = json.loads(man.read_text())
                hit = (mt, {'files': len(m), 'bytes': sum(v[0] for v in m.values()), 'synced': mt / 1e9})
            except (OSError, ValueError):
                hit = (mt, None)
            manifest_memo[man] = hit
        return hit[1]

    def dataset_uri(d):
        if d['kind'] == 's3':
            return f"s3://{d['bucket']}/{d.get('prefix', '')}".rstrip('/')
        if d['kind'] == 'labeling':                 # = LabelingSource.uri (workspaces are matched by it)
            return f"label://{d.get('project_id')}/{d.get('task_id')}" + ('?only_done=1' if d.get('only_done') else '')
        return d.get('path')

    def labeling_info(d):
        """{project, task, task_type} of a labeling-backed session, or {error} when its session / task is gone."""
        lab = ctx.labeling
        try:
            if lab is None:
                raise KeyError('labeling sessions are not enabled in this server')
            p = lab.get(d['project_id'])
            t = lab.store(d['project_id']).task(d['task_id'])
            return {'project': p['name'], 'task': t['name'], 'task_type': t['task_type']}
        except KeyError:
            return {'error': 'its labeling session or task was deleted'}

    def dataset_public(d, active=None):
        out = dict(d)
        out['active'] = d['id'] == active
        if d['kind'] == 's3':
            try:
                c = reg.connection(d['connection_id'])
                out['connection'] = c.get('name') or c.get('endpoint')
                out['uri'] = dataset_uri(d)
                mirror = (reg.mirror_root() / sources._safe(c.get('id')) / d['bucket'] / d.get('prefix', '')).resolve()
                summary = mirror_summary(mirror / '.manifest.json')
                if summary:
                    out['mirror'] = {'path': str(mirror), **summary}
            except KeyError:
                out['connection'] = None
                out['error'] = 'its connection was deleted'
        elif d['kind'] == 'labeling':
            out['uri'] = dataset_uri(d)
            out['labeling'] = labeling_info(d)
            if 'error' in out['labeling']:
                out['error'] = out['labeling'].pop('error')
        else:
            out['uri'] = dataset_uri(d)
            out['exists'] = Path(d.get('path', '')).is_dir()
        # card stats from the newest workspace of this dataset (small workspace.json only, memoised by mtime)
        ws = find_workspace(reg.root, dataset_uri(d))
        if ws:
            folder, meta = ws
            classes = meta.get('classes') or []
            out['stats'] = {'n_boxes': meta.get('n_boxes'), 'n_images': meta.get('n_images'),
                            'n_classes': len(classes) if classes else None, 'classes': classes[:8],
                            'last_opened': meta.get('last_opened'), 'workspace': folder.name}
            out['cover'] = min(N_COVER, meta.get('n_boxes') or 0) if (folder / 'crops').is_dir() else 0
        return out

    def list_public():
        active = active_id()
        return {'datasets': [dataset_public(d, active) for d in reg.datasets()], 'active': active}

    @r.get('/api/datasets')
    async def list_datasets():
        need_registry()
        return await run_io(list_public)            # folder checks on a slow mount never block the server

    @r.get('/api/cover/{did}/{k}')
    def dataset_cover(did: str, k: int):
        """One crop of the card's mosaic: crops spread evenly over the session's boxes (already 224 px jpgs)."""
        need_registry()
        try:
            ws = find_workspace(reg.root, dataset_uri(reg.dataset(did)))
        except KeyError as e:
            raise HTTPException(404, str(e))
        n = (ws[1].get('n_boxes') or 0) if ws else 0
        count = min(N_COVER, n)
        if not 0 <= k < count:
            raise HTTPException(404, 'no cover image')
        path = ws[0] / 'crops' / f'{(2 * k + 1) * n // (2 * count):06d}.jpg'
        if not path.is_file():
            raise HTTPException(404, 'no cover image')
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'max-age=3600'})

    def check_labeling(ds):
        """Before a labeling-backed session is saved: its session + task exist and have boxes; no name given =
        '<session> · <task>'."""
        src = reg.source({**ds, 'name': None}, s3cfg, ctx.labeling)      # its own name: '<session> · <task>'
        src.check()
        if ds['name'] == 'labeling session':        # upsert_dataset's placeholder for a blank name
            ds['name'] = src.name

    @r.post('/api/datasets')
    async def save_dataset(body: dict):
        need_registry()
        try:
            if body.get('kind') == 'labeling':
                ds = await run_io(reg.upsert_dataset, body, check_labeling)
            else:
                ds = reg.upsert_dataset(body)
            if ds['kind'] == 'local':
                await run_io(sources.LocalSource(ds['path']).check)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        ds_log.info(f'saved session [bold]{vlog.esc(ds["name"])}[/] ({ds["kind"]}'
                    + (f', {ds["mode"]}' if ds['kind'] == 's3' else '') + ')')
        return await run_io(dataset_public, ds, active_id())

    @r.put('/api/datasets/{did}/tags')
    async def set_dataset_tags(did: str, body: dict):
        """The card's own tags (the session forms never send them)."""
        need_registry()
        try:
            ds = reg.set_dataset_tags(did, body.get('tags'))
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return await run_io(dataset_public, ds, active_id())

    @r.delete('/api/datasets/{did}')
    def delete_dataset(did: str):
        need_registry()
        try:
            reg.delete_dataset(did)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if did == active_id():
            ctx.state = None                     # close it: running jobs keep their own State; routes now answer 409
        ds_log.info(f'removed session {did}')
        return {'ok': True}

    @r.post('/api/datasets/{did}/open')
    def open_dataset(did: str):
        need_registry()
        try:
            ds = reg.dataset(did)
        except KeyError:
            raise HTTPException(404, 'this session no longer exists (removed, or saved on another machine)')
        try:
            new = ctx.open_dataset(ds)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
        return {'ok': True, 'job_id': new.index_job.id if new.index_job else None}

    @r.post('/api/datasets/{did}/refresh')
    def refresh_dataset(did: str):
        """Re-index a session whose files changed since its cache was built (the page shows the old cache meanwhile)."""
        need_registry()
        try:
            ds = reg.dataset(did)
        except KeyError:
            raise HTTPException(404, 'this session no longer exists (removed, or saved on another machine)')
        try:
            new = ctx.open_dataset(ds, refresh=True)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
        return {'ok': True, 'job_id': new.index_job.id if new.index_job else None}

    @r.post('/api/datasets/{did}/sync')
    def sync_dataset(did: str):
        """Re-sync the local mirror of an S3 dataset (cache mode) in the background."""
        need_registry()
        try:
            ds = reg.dataset(did)
            if ds['kind'] != 's3':
                raise ValueError('only S3 datasets are synced')
            src = reg.source(ds, s3cfg)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
        job = ctx.jobs.submit(f'sync:{did}', f'Sync · {ds["name"]}', lambda job: src.sync(job.progress), lane='io')
        return {'job_id': job.id}

    @r.get('/api/connections')
    def list_connections():
        need_registry()
        return {'connections': [reg.public_connection(c) for c in reg.load()['connections']]}

    @r.post('/api/connections')
    def save_connection(body: dict):
        need_registry()
        kind = body.get('kind') or 's3'
        if kind == 's3' and not body.get('endpoint'):
            raise HTTPException(400, 'endpoint URL is required')
        if kind == 'model' and not body.get('url') and not body.get('id'):
            raise HTTPException(400, 'the model server URL is required')
        if kind not in ('s3', 'model') and not body.get('host'):
            raise HTTPException(400, 'host is required')
        try:
            c = reg.upsert_connection(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if kind == 's3':
            ds_log.info(f'saved S3 connection [bold]{vlog.esc(c.get("name") or c["endpoint"])}[/] ({vlog.esc(c["endpoint"])})')
        else:
            where = c.get('url') if kind == 'model' else c['host']
            ds_log.info(f'saved {kind} connection [bold]{vlog.esc(c.get("name") or where)}[/] ({vlog.esc(where)})')
        return reg.public_connection(c)

    @r.delete('/api/connections/{cid}')
    def delete_connection(cid: str):
        need_registry()
        try:
            reg.delete_connection(cid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {'ok': True}

    def _s3_error(e):
        return HTTPException(400, f'{type(e).__name__}: {e}')

    def merged(body):
        try:
            return reg.merged_connection(body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    async def test_other(conn):
        """FTP / SFTP: list the base path; databases: SELECT 1 + whether the labeling tables exist; model: the
        detect result ({protocol, models})."""
        kind = conn['kind']
        log = vlog.get_logger(kind)
        where = conn.get('url') if kind == 'model' else conn.get('host')
        try:
            if kind == 'model':
                from ...labeling import models
                t0 = time.time()
                res = await run_io(models.detect, conn, True)
                res = {'ok': True, **res, 'ms': round((time.time() - t0) * 1000)}
            elif kind in ('ftp', 'sftp'):
                from ...labeling import media
                res = await run_io(media.remote_test, conn)
            else:
                from ...labeling import db_export
                res = await run_io(db_export.test, conn)
        except Exception as e:  # noqa: BLE001 - shown in the form
            log.warning(f'test {vlog.esc(where)} failed: {vlog.esc(e)}')
            raise HTTPException(400, str(e)) if kind == 'model' else _s3_error(e)
        log.info(f'test {vlog.esc(where)}: [green]ok[/] [dim]({res.get("ms")} ms)[/]')
        return res

    @r.post('/api/connections/test')
    async def test_connection(body: dict):
        """Check a connection (saved or typed in the form): S3 lists its buckets, other kinds per test_other."""
        need_registry()
        conn = merged(body)
        if (conn.get('kind') or 's3') != 's3':
            return await test_other(conn)
        t0 = time.time()
        try:
            res = await run_io(sources.browse, conn, None, '', s3cfg)
        except Exception as e:  # noqa: BLE001 - shown in the form
            vlog.get_logger('s3').warning(f'test {vlog.esc(conn.get("endpoint"))} failed: {vlog.esc(e)}')
            raise _s3_error(e)
        vlog.get_logger('s3').info(f'test {vlog.esc(conn.get("endpoint"))}: [green]ok[/], {len(res["buckets"])} buckets '
                                   f'[dim]({(time.time() - t0) * 1000:.0f} ms)[/]')
        return {'ok': True, 'buckets': res['buckets'], 'ms': round((time.time() - t0) * 1000)}

    @r.post('/api/connections/browse')
    async def browse_connection(body: dict):
        """S3: folders under bucket/prefix (each flagged when it holds images/ + labels/), plus a summary when it
        does. FTP / SFTP: the folders under `path` with their image counts."""
        need_registry()
        conn = merged(body.get('connection') or {})
        if (conn.get('kind') or 's3') in ('ftp', 'sftp'):
            # {connection, path} -> {path, parent, folders:[{name, images}], images}
            from ...labeling import media
            try:
                return await run_io(media.browse_remote, conn, body.get('path') or None)
            except Exception as e:  # noqa: BLE001
                raise _s3_error(e)
        if (conn.get('kind') or 's3') != 's3':
            raise HTTPException(400, 'only S3 / FTP / SFTP connections can be browsed')
        bucket, prefix = body.get('bucket') or None, body.get('prefix') or ''
        try:
            res = await run_io(sources.browse, conn, bucket, prefix, s3cfg)
            if bucket and res.get('yolo'):
                src = sources.S3Source(conn, bucket, prefix, s3cfg=s3cfg)
                res['info'] = await run_io(src.summary)
        except Exception as e:  # noqa: BLE001
            raise _s3_error(e)
        return res

    @r.get('/api/fs/browse')
    async def browse_folder(path: str = '', images: int = 0):
        need_registry()
        try:
            return await run_io(sources.browse_local, path or str(PROJECT_ROOT), bool(images))
        except ValueError as e:
            raise HTTPException(400, str(e))

    return r
