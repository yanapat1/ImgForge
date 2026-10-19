"""
Visualize sessions (saved datasets: local folders, S3 / RustFS prefixes, a task of a labeling session) and the
caller's own connections: the Sessions home cards (stats and a crop mosaic from the session's workspace, the
card header: owner, members, visibility, my role, last opened by me, loaded on the server), loading one
(it is shared by every user who opens it), cache-mode sync, connection test / browsing (S3 buckets, FTP / SFTP
folders, database SELECT 1) and the local folder browser (storage.allowed_roots only).

Who may do what: auth/access.py (viewer: see the card, open it; owner: tags, delete, re-index, sync; editing
its source). Connections are private to their owner: every connection route works on the caller's own only,
and creating / testing / browsing one is subject to the admins' who_can_add_connections switch. Network and
disk work runs off the event loop (asyncio.to_thread keeps the request's context: the signed-in user).
"""

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ...auth import access, deps
from ...core import logging as vlog
from ...data import connections, sessions, sources
from ...workspace import find_workspace

VIEWER = deps.require_session('viewer', param='did', kind='viz', what='visualize session')
OWNER = deps.require_session('owner', param='did', kind='viz', what='visualize session')


def router(state, ctx):
    r = APIRouter()

    # ------------------------------------------------------------ datasets + S3 connections (Settings)
    reg = ctx.datasets
    s3cfg = ctx.cfg.get('s3', {})
    ds_log = vlog.get_logger('datasets')

    def need_registry():
        if reg is None:
            raise HTTPException(501, 'dataset switching is not enabled in this server')

    run_io = asyncio.to_thread              # blocking network / disk work off the event loop, context kept

    def roots():
        return connections.allowed_roots(ctx.cfg)

    def loaded(did):
        fn = getattr(ctx, 'loaded', None)
        return bool(fn(did)) if fn is not None else did in (getattr(ctx, 'states', None) or {})

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

    def dataset_public(d, user=None):
        out = {k: v for k, v in d.items() if k != 'owner_id'}
        out['loaded'] = loaded(d['id'])
        if 'my_role' not in out and user is not None:          # a single card (save / tags): its header too
            head = sessions.header(user, d['id'])
            if head is not None:
                out.update(owner=head['owner'], members=head['members'], my_role=head['my_role'],
                           opened_at=head['opened_at'])
        if d['kind'] == 's3':
            try:
                c = reg.connection(d['connection_id'])          # its owner's (only the name is shown)
                out['connection'] = c.get('name') or c.get('endpoint')
                out['uri'] = dataset_uri(d)
                mirror = (reg.mirror_root() / sources._safe(c.get('id')) / d['bucket'] / d.get('prefix', '')).resolve()
                summary = mirror_summary(mirror / '.manifest.json')
                if summary:
                    out['mirror'] = {'path': str(mirror), **summary}
            except (KeyError, TypeError):
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

    def list_public(user, scope):
        try:
            rows = reg.datasets(user, scope)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {'datasets': [dataset_public(d) for d in rows]}

    @r.get('/api/datasets')
    async def list_datasets(scope: str = 'all', user=Depends(deps.current_user)):
        need_registry()
        return await run_io(list_public, user, scope)      # folder checks on a slow mount never block the server

    @r.get('/api/cover/{did}/{k}', dependencies=[Depends(VIEWER)])
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
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})

    def check_labeling(user):
        def check(ds):
            """Before a labeling-backed session is saved: the caller owns that labeling session (a visualize
            session of it can be shared on), its task exists and has boxes; no name given = '<session> · <task>'."""
            have = access.role(user, ds['project_id'], kind='label')
            if have != 'owner':
                raise ValueError('pick one of your own labeling sessions' if have is None else
                                 'only the owner of that labeling session can open it in the visualizer')
            src = reg.source({**ds, 'name': None}, s3cfg, ctx.labeling)      # its own name: '<session> · <task>'
            src.check()
            if ds['name'] == 'labeling session':        # upsert_dataset's placeholder for a blank name
                ds['name'] = src.name
        return check

    @r.post('/api/datasets')
    async def save_dataset(body: dict, user=Depends(deps.current_user)):
        """A new session owned by the caller, or (body.id) an edit of the caller's own session's source / name."""
        need_registry()
        if body.get('id'):
            have = access.role(user, body['id'], kind='viz')
            if have is None:                     # unknown / not visible: a new session (the id does not leak)
                body = {k: v for k, v in body.items() if k != 'id'}
            elif have != 'owner':
                raise HTTPException(403, 'only the owner of this session can do that')
        try:
            check = check_labeling(user) if body.get('kind') == 'labeling' else None
            ds = await run_io(reg.upsert_dataset, body, user['id'], check, roots())
            if ds['kind'] == 'local':
                await run_io(sources.LocalSource(ds['path']).check)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        ds_log.info(f'saved session [bold]{vlog.esc(ds["name"])}[/] ({ds["kind"]}'
                    + (f', {ds["mode"]}' if ds['kind'] == 's3' else '') + ')')
        return await run_io(dataset_public, ds, user)

    @r.put('/api/datasets/{did}/tags', dependencies=[Depends(OWNER)])
    async def set_dataset_tags(did: str, body: dict, user=Depends(deps.current_user)):
        """The card's own tags (the session forms never send them)."""
        need_registry()
        try:
            ds = await run_io(reg.set_dataset_tags, did, body.get('tags'))
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return await run_io(dataset_public, ds, user)

    def unload(did):
        evict = getattr(ctx, 'evict', None)
        if evict is not None:
            evict(did)                           # running jobs keep their own State; the next open builds anew

    @r.delete('/api/datasets/{did}', dependencies=[Depends(OWNER)])
    def delete_dataset(did: str):
        need_registry()
        try:
            reg.delete_dataset(did)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        unload(did)
        ds_log.info(f'removed session {did}')
        return {'ok': True}

    def saved(did):
        try:
            return reg.dataset(did)
        except KeyError:
            raise HTTPException(404, 'this session no longer exists (removed, or saved on another machine)')

    @r.post('/api/datasets/{did}/open', dependencies=[Depends(VIEWER)])
    def open_dataset(did: str, user=Depends(deps.current_user)):
        """Load the session on the server (already loaded: nothing to do) and remember that I opened it."""
        need_registry()
        ds = saved(did)
        try:
            state_for = getattr(ctx, 'state_for', None)
            new = state_for(did, user) if state_for is not None else ctx.open_dataset(ds)
        except (ValueError, KeyError, FileNotFoundError, NotADirectoryError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        sessions.mark_opened(user['id'], did)
        job = getattr(new, 'index_job', None)
        return {'ok': True, 'job_id': job.id if job is not None else None}

    @r.post('/api/datasets/{did}/refresh', dependencies=[Depends(OWNER)])
    def refresh_dataset(did: str):
        """Re-index a session whose files changed since its cache was built (the page shows the old cache meanwhile)."""
        need_registry()
        ds = saved(did)
        if ctx.open_dataset is None:
            raise HTTPException(501, 'opening sessions is not enabled in this server')
        try:
            new = ctx.open_dataset(ds, refresh=True)
        except (ValueError, KeyError, FileNotFoundError, NotADirectoryError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        return {'ok': True, 'job_id': new.index_job.id if new.index_job else None}

    @r.post('/api/datasets/{did}/sync', dependencies=[Depends(OWNER)])
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
    def list_connections(user=Depends(deps.current_user)):
        need_registry()
        return {'connections': [reg.public_connection(c) for c in reg.connections(user['id'])]}

    @r.post('/api/connections')
    def save_connection(body: dict, user=Depends(deps.can_add_connections)):
        need_registry()
        kind = body.get('kind') or 's3'
        if kind == 's3' and not body.get('endpoint'):
            raise HTTPException(400, 'endpoint URL is required')
        if kind == 'model' and not body.get('url') and not body.get('id'):
            raise HTTPException(400, 'the model server URL is required')
        if kind not in ('s3', 'model') and not body.get('host'):
            raise HTTPException(400, 'host is required')
        if body.get('id'):
            try:
                reg.connection(body['id'], owner_id=user['id'])
            except KeyError:
                raise HTTPException(404, f'no connection {body["id"]!r}') from None
        try:
            c = reg.upsert_connection(body, user['id'])
        except ValueError as e:
            raise HTTPException(400, str(e))
        if kind == 's3':
            ds_log.info(f'saved S3 connection [bold]{vlog.esc(c.get("name") or c["endpoint"])}[/] ({vlog.esc(c["endpoint"])})')
        else:
            where = c.get('url') if kind == 'model' else c['host']
            ds_log.info(f'saved {kind} connection [bold]{vlog.esc(c.get("name") or where)}[/] ({vlog.esc(where)})')
        return reg.public_connection(c)

    @r.delete('/api/connections/{cid}')
    def delete_connection(cid: str, user=Depends(deps.current_user)):
        need_registry()
        try:
            reg.delete_connection(cid, user['id'])
        except KeyError:
            raise HTTPException(404, f'no connection {cid!r}') from None
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {'ok': True}

    def _s3_error(e):
        return HTTPException(400, f'{type(e).__name__}: {e}')

    def merged(body, user):
        try:
            return reg.merged_connection(body, user['id'])        # a saved secret: only from the caller's own
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
    async def test_connection(body: dict, user=Depends(deps.can_add_connections)):
        """Check a connection (saved or typed in the form): S3 lists its buckets, other kinds per test_other."""
        need_registry()
        conn = merged(body, user)
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
    async def browse_connection(body: dict, user=Depends(deps.can_add_connections)):
        """S3: folders under bucket/prefix (each flagged when it holds images/ + labels/), plus a summary when it
        does. FTP / SFTP: the folders under `path` with their image counts."""
        need_registry()
        conn = merged(body.get('connection') or {}, user)
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

    def browse_allowed(path, images):
        """browse_local inside storage.allowed_roots only: no path = the first root; 'parent' stops at a root and
        `roots` lists them all (the page offers them as starting points)."""
        rs = roots()
        if not rs:
            raise ValueError(connections.OUTSIDE)
        where = connections.check_local(path, rs) if path else str(rs[0])
        out = sources.browse_local(where, images)
        if out.get('parent') and not connections.inside(out['parent'], rs):
            out['parent'] = None
        out['roots'] = [str(x) for x in rs]
        return out

    @r.get('/api/fs/browse')
    async def browse_folder(path: str = '', images: int = 0, user=Depends(deps.current_user)):
        need_registry()
        try:
            return await run_io(browse_allowed, path, bool(images))
        except ValueError as e:
            raise HTTPException(400, str(e))

    return r
