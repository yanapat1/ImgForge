"""
Saved connections (Settings › Connections, Data Center, labeling): list, save, delete, test, browse (S3 buckets,
FTP / SFTP folders, database SELECT 1); and the browser of the server's allowed folders (storage.allowed_roots only).
Core: every zone reads its data through these.

Connections are private to their owner: every route works on the caller's own only, and creating / testing /
browsing one is subject to the admins' who_can_add_connections switch.

    GET/POST /api/connections, DELETE /api/connections/{id}
    POST /api/connections/test, /create-bucket, /browse
    GET /api/fs/browse
"""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException

from ... import zones
from ...auth import deps, tiers
from ...core import logging as vlog
from ...data import connections, sources


def router(state, ctx):
    r = APIRouter()
    reg = ctx.datasets
    s3cfg = ctx.cfg.get('s3', {})
    ds_log = vlog.get_logger('datasets')
    run_io = asyncio.to_thread              # blocking network / disk work off the event loop, context kept

    def need_registry():
        if reg is None:
            raise HTTPException(501, 'dataset switching is not enabled in this server')

    def roots():
        return connections.allowed_roots(ctx.cfg)

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
        if kind in connections.TOKEN_KINDS and not body.get('api_key') and not body.get('id'):
            raise HTTPException(400, 'the API key / token is required')
        if kind not in ('s3', 'model', *connections.TOKEN_KINDS) and not body.get('host'):
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
            where = c.get('url') if kind == 'model' else c.get('host') or kind
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
        where = conn.get('url') if kind == 'model' else conn.get('host') or kind
        try:
            if kind in connections.TOKEN_KINDS:
                from ...datacenter.sources import tokens
                res = await run_io(tokens.test, conn)
            elif kind == 'model':
                from ...labeling import models
                t0 = time.time()
                res = await run_io(models.detect, conn, True)
                res = {'ok': True, **res, 'ms': round((time.time() - t0) * 1000)}
            elif kind in ('ftp', 'sftp'):
                from ...labeling import media
                res = await run_io(media.remote_test, conn)
            elif kind == 'mongo' or not zones.on('labeling', ctx.cfg):
                from ...analysis import adapters
                res = await run_io(adapters.ping, conn)
            else:
                from ...labeling import db_export
                res = await run_io(db_export.test, conn)
        except Exception as e:  # noqa: BLE001 - shown in the form
            log.warning(f'test {vlog.esc(where)} failed: {vlog.esc(e)}')
            raise HTTPException(400, str(e)) if kind == 'model' or kind in connections.TOKEN_KINDS else _s3_error(e)
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

    @r.post('/api/connections/create-bucket')
    async def create_bucket(body: dict, user=Depends(deps.can_add_connections)):
        """{id, bucket}: make S3 bucket `bucket` through the caller's own saved connection (an export target's
        New bucket) -> {ok, bucket, created: false when it already existed}."""
        need_registry()
        try:
            conn = reg.connection(str(body.get('id') or ''), owner_id=user['id'])
        except KeyError:
            raise HTTPException(400, 'pick one of your own connections') from None
        if (conn.get('kind') or 's3') != 's3':
            raise HTTPException(400, 'only an S3 connection has buckets')
        from ...labeling import sinks

        def go():
            client = sources.make_client(conn, s3cfg)
            try:
                return sinks.ensure_bucket(client, body.get('bucket'))
            finally:
                client.close()
        try:
            created = await run_io(go)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except Exception as e:  # noqa: BLE001 - shown in the form
            vlog.get_logger('s3').warning(f'create bucket {vlog.esc(body.get("bucket"))} failed: {vlog.esc(e)}')
            raise _s3_error(e)
        return {'ok': True, 'bucket': str(body.get('bucket')).strip(), 'created': created}

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
        if not (tiers.has(user, 'dataset.create') or tiers.has(user, 'label.create')):     # both pick a folder
            tiers.check(user, 'dataset.create')
        try:
            return await run_io(browse_allowed, path, bool(images))
        except ValueError as e:
            raise HTTPException(400, str(e))

    return r
