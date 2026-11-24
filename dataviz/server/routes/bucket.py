"""
The Bucket page API (/api/bucket/*, contract: bucket/API.md). Thin on purpose: every handler maps its request
onto one bucket/service.py function (the same ones the LLM tools and the CLI call) and turns BucketError into
an HTTP error. Visibility (owner / admin) and the bucket.manage permission are decided in the service, so the
assistant cannot do more than the page; the routes add the sign-in guard and the permission dependency on the
changing routes so a refused call never reaches the service.

Handlers are plain `def`: FastAPI runs them in its thread pool (disk and S3 work never blocks the event loop);
upload/part reads its raw body async, then does the same.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from ...auth import deps
from ...bucket import BucketError, service

MANAGE = deps.require_perm('bucket.manage')
CACHE = {'Cache-Control': 'private, max-age=300'}
PART_MAX = 64 << 20                 # one upload chunk (POST upload/part); the page sends 8 MB or less


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except BucketError as e:
        raise HTTPException(e.status, e.message) from None


def attachment(name):
    """Content-Disposition of a download named `name`: starlette sends headers as latin-1, so a Thai (any
    non-latin-1) name goes in filename* (RFC 5987) with an ASCII stand-in in filename for old clients."""
    ascii_name = ''.join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else '_' for ch in name)
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name, safe="")}'


def router(state, ctx):
    service.configure(ctx=ctx)
    r = APIRouter(prefix='/api/bucket')

    @r.get('/summary')
    def summary(user=Depends(deps.current_user)):
        return _call(service.summary, user)

    @r.get('/buckets')
    def list_buckets(q: str = '', tag: list[str] = Query(default=[]), format: list[str] = Query(default=[]),
                     user=Depends(deps.current_user)):
        return _call(service.list_buckets, user, q=q, tags=tuple(tag), formats=tuple(format))

    @r.post('/buckets')
    def create_bucket(body: dict, user=Depends(MANAGE)):
        return _call(service.create_bucket, user, body.get('name'), kind=body.get('kind') or 'managed',
                     path=body.get('path'), connection_id=body.get('connection_id'), s3_bucket=body.get('s3_bucket'),
                     prefix=body.get('prefix') or '', tags=tuple(body.get('tags') or ()), mode=body.get('mode') or 'stream')

    @r.get('/buckets/{bid}')
    def get_bucket(bid: str, user=Depends(deps.current_user)):
        return _call(service.get_bucket, user, bid)

    @r.patch('/buckets/{bid}')
    def update_bucket(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.update_bucket, user, bid, name=body.get('name'), mode=body.get('mode'))

    @r.post('/buckets/{bid}/sync')
    def sync(bid: str, user=Depends(MANAGE)):
        return _call(service.sync, user, bid)

    @r.get('/buckets/{bid}/export')
    def export(bid: str, path: str = '', user=Depends(deps.current_user)):
        name, chunks = _call(service.export, user, bid, path)
        return StreamingResponse(chunks, media_type='application/zip', headers={'Content-Disposition': attachment(name)})

    @r.get('/buckets/{bid}/export/check')
    def export_check(bid: str, path: str = '', user=Depends(deps.current_user)):
        # the checks GET export runs before streaming: a 4xx here is a toast, not a silently failed download
        return _call(service.export_check, user, bid, path)

    @r.post('/buckets/{bid}/export')
    def export_to(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.export_to, user, bid, body.get('path') or '', target=body.get('target') or {},
                     create_bucket=bool(body.get('create_bucket')), overwrite=bool(body.get('overwrite')))

    @r.get('/buckets/{bid}/source')
    def source(bid: str, path: str = '', user=Depends(deps.current_user)):
        return _call(service.source, user, bid, path)

    @r.delete('/buckets/{bid}')
    def delete_bucket(bid: str, delete_files: int = 0, user=Depends(MANAGE)):
        return _call(service.delete_bucket, user, bid, delete_files=bool(delete_files))

    @r.get('/buckets/{bid}/ls')
    def ls(bid: str, path: str = '', offset: int = 0, limit: int = 500, sort: str = 'name', q: str = '',
           retry: int = 0, refresh: int = 0, user=Depends(deps.current_user)):
        return _call(service.ls, user, bid, path, offset=offset, limit=limit, sort=sort, q=q, retry=bool(retry),
                     refresh=bool(refresh))

    @r.post('/buckets/{bid}/local')
    def fetch_local(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.fetch_local, user, bid, body.get('paths') or [], mirror=bool(body.get('mirror')))

    @r.post('/buckets/{bid}/local/check')
    def check_local(bid: str, user=Depends(MANAGE)):
        return _call(service.check_local, user, bid)

    @r.post('/buckets/{bid}/compare')
    def compare_cloud(bid: str, body: dict, user=Depends(deps.current_user)):
        return _call(service.compare_cloud, user, bid, str(body.get('path') or ''), verify=bool(body.get('verify')))

    @r.get('/buckets/{bid}/compare')
    def compare_result(bid: str, path: str = '', status: str = '', q: str = '', offset: int = 0, limit: int = 200,
                       user=Depends(deps.current_user)):
        return _call(service.compare_result, user, bid, path, status or None, q, offset, limit)

    @r.post('/buckets/{bid}/local/remove')
    def drop_local(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.drop_local, user, bid, body.get('paths') or [])

    @r.get('/buckets/{bid}/file')
    def read_file(bid: str, path: str, w: int | None = None, t: float | None = None, user=Depends(deps.current_user)):
        if not w:
            local = _call(service.file_path, user, bid, path)
            if local is not None:                       # a local file is streamed, never read into memory
                return FileResponse(local[0], media_type=local[1], headers=CACHE)
        data, mt = _call(service.read_file, user, bid, path, w=w, t=t)
        return Response(data, media_type=mt, headers=CACHE)

    @r.get('/buckets/{bid}/text')
    def read_text(bid: str, path: str, limit: int = 65536, user=Depends(deps.current_user)):
        return _call(service.read_text, user, bid, path, limit=limit)

    @r.put('/buckets/{bid}/text')
    def write_text(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.write_text, user, bid, body.get('path') or '', body.get('text'),
                     expect_mtime_ns=body.get('expect_mtime_ns'), create=bool(body.get('create')))

    @r.post('/buckets/{bid}/ops')
    def ops(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.ops, user, bid, body.get('ops'))

    @r.get('/buckets/{bid}/changes')
    def list_changes(bid: str, prefix: str = '', offset: int = 0, limit: int = 200, user=Depends(deps.current_user)):
        return _call(service.list_changes, user, bid, prefix, offset, limit)

    @r.post('/buckets/{bid}/changes/clear')
    def clear_changes(bid: str, body: dict, user=Depends(deps.current_user)):
        return _call(service.clear_changes, user, bid, body.get('paths') or (), body.get('prefix'))

    @r.post('/buckets/{bid}/push')
    def push(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.push, user, bid, body.get('paths') or [], body.get('overwrite') or ())

    @r.get('/session-folder/{did}')
    def session_info(did: str, user=Depends(deps.current_user)):
        return _call(service.session_info, user, did)

    @r.post('/session-folder/{did}/export')
    def export_session(did: str, body: dict, user=Depends(MANAGE)):
        return _call(service.export_session, user, did, body.get('format') or 'coco', body.get('to_bucket') or None,
                     body.get('to_path') or None)

    @r.post('/session-folder/{did}/label')
    def session_to_label(did: str, body: dict, user=Depends(MANAGE)):
        return _call(service.session_to_label, user, did, body.get('name') or None, body.get('task_type') or None)

    @r.post('/copy')
    def copy_to(body: dict, user=Depends(MANAGE)):
        return _call(service.copy_to, user, body.get('from_bucket') or '', body.get('paths') or [],
                     body.get('to_bucket') or '', body.get('to_path') or '')

    @r.post('/buckets/{bid}/undo')
    def undo(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.undo, user, bid, body.get('undo_id'))

    @r.post('/buckets/{bid}/convert')
    def convert(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.convert_vectors, user, bid, body.get('path') or '', to=body.get('to') or 'parquet')

    @r.get('/buckets/{bid}/video')
    def video_info(bid: str, path: str, user=Depends(deps.current_user)):
        return _call(service.video_info, user, bid, path)

    @r.post('/buckets/{bid}/video-auto')
    def video_auto(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.set_video_auto, user, bid, body.get('opts'))

    @r.post('/frames/estimate')
    def frames_estimate(body: dict, user=Depends(deps.current_user)):
        return _call(service.estimate_frames, user, body.get('bucket'), body.get('paths') or [], body.get('opts'))

    @r.post('/frames')
    def frames(body: dict, user=Depends(MANAGE)):
        return _call(service.extract_frames, user, body.get('bucket'), body.get('paths') or [], body.get('opts'),
                     target=body.get('target') or 'inplace', new_name=body.get('new_name'), out=body.get('out'),
                     remember=bool(body.get('remember')))

    @r.post('/buckets/{bid}/upload')
    def upload(bid: str, path: str = '', unzip: int = 1, files: list[UploadFile] = File(...), user=Depends(MANAGE)):
        try:
            return _call(service.upload, user, bid, path, [(f.filename or 'file', f.file) for f in files],
                         unzip=bool(unzip))
        finally:
            for f in files:
                f.file.close()

    @r.post('/buckets/{bid}/upload/part')
    async def upload_part(bid: str, request: Request, name: str, id: str, offset: int, total: int, path: str = '',
                          unzip: int = 1, user=Depends(MANAGE)):
        # a raw chunk (application/octet-stream), not multipart: one big file in requests a proxy lets through
        if int(request.headers.get('content-length') or 0) > PART_MAX:
            raise HTTPException(413, f'a chunk is {PART_MAX >> 20} MB at most')
        data = await request.body()
        if len(data) > PART_MAX:
            raise HTTPException(413, f'a chunk is {PART_MAX >> 20} MB at most')
        return await run_in_threadpool(_call, service.upload_part, user, bid, path, name, id, offset, total, data,
                                       unzip=bool(unzip))

    @r.get('/buckets/{bid}/upload/part')
    def part_status(bid: str, name: str, id: str, total: int, path: str = '', unzip: int = 1, user=Depends(MANAGE)):
        return _call(service.part_status, user, bid, path, name, id, total, unzip=bool(unzip))

    @r.put('/buckets/{bid}/tags')
    def set_tags(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.set_tags, user, bid, body.get('path') or '', add=tuple(body.get('add') or ()),
                     remove=tuple(body.get('remove') or ()))

    @r.get('/buckets/{bid}/detect')
    def detect(bid: str, path: str = '', force: int = 0, user=Depends(deps.current_user)):
        return _call(service.detect, user, bid, path, force=bool(force))

    @r.get('/buckets/{bid}/stats')
    def stats(bid: str, path: str = '', user=Depends(deps.current_user)):
        return _call(service.stats, user, bid, path)

    @r.get('/tags')
    def all_tags(user=Depends(deps.current_user)):
        return _call(service.all_tags, user)

    @r.get('/search')
    def search(q: str = '', tag: list[str] = Query(default=[]), format: list[str] = Query(default=[]),
               bucket: str = '', path: str = '', glob: str = '', limit: int = 200, user=Depends(deps.current_user)):
        return _call(service.search, user, q=q, tags=tuple(tag), formats=tuple(format), bid=bucket or None,
                     path=path, glob=glob or None, limit=limit)

    @r.post('/send')
    def send(body: dict, user=Depends(MANAGE)):
        return _call(service.send, user, body.get('bucket'), body.get('paths') or [''],
                     target=body.get('target') or 'visualize', name=body.get('name'),
                     task_type=body.get('task_type') or 'detect')

    @r.get('/buckets/{bid}/session-check')
    def session_check(bid: str, path: str = '', user=Depends(deps.current_user)):
        """What a session made from this folder can do (Create session's Format step)."""
        return _call(service.session_check, user, bid, path)

    @r.post('/session')
    def create_session(body: dict, user=Depends(MANAGE)):
        """One session from a bucket folder: Visualize, Label or both linked (the default)."""
        return _call(service.create_session, user, body.get('bucket'), body.get('path') or '',
                     visualize=body.get('visualize', True) is not False, label=body.get('label', True) is not False,
                     name=body.get('name'), task_type=body.get('task_type') or 'detect')

    @r.post('/reformat')
    def reformat(body: dict, user=Depends(MANAGE)):
        """Write a folder again in another label format, as a new folder of the bucket "<name> · formats" (a job)."""
        return _call(service.reformat, user, body.get('bucket'), body.get('path') or '', to=body.get('to') or 'coco')

    @r.get('/sources')
    def list_sources(user=Depends(deps.current_user)):
        return _call(service.list_sources, user)

    @r.post('/sources/inspect')
    def inspect_source(body: dict, user=Depends(MANAGE)):
        return _call(service.inspect_source, user, body.get('source'), body.get('ref'),
                     connection_id=body.get('connection_id'), opts=body.get('opts') or {})

    @r.post('/import')
    def import_source(body: dict, user=Depends(MANAGE)):
        return _call(service.import_source, user, body.get('source'), body.get('ref'), name=body.get('name'),
                     connection_id=body.get('connection_id'), opts=body.get('opts') or {},
                     bucket=body.get('bucket'), path=body.get('path') or '')

    @r.post('/buckets/{bid}/reimport')
    def reimport(bid: str, user=Depends(MANAGE)):
        return _call(service.reimport, user, bid)

    @r.post('/autolabel')
    def autolabel(body: dict, user=Depends(MANAGE)):
        return _call(service.autolabel, user, body.get('bucket'), body.get('path') or '', model=body.get('model'),
                     target=body.get('target') or 'inplace', new_name=body.get('new_name'), conf=body.get('conf'),
                     write_yolo=body.get('write_yolo', True) is not False, name=body.get('name'),
                     formats=body.get('formats'))

    return r
