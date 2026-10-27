"""
The Data Center API (/api/dc/*, contract: datacenter/API.md). Thin on purpose: every handler maps its request
onto one datacenter/service.py function (the same ones the LLM tools and the CLI call) and turns DCError into
an HTTP error. Visibility (owner / admin) and the data.manage permission are decided in the service, so the
assistant cannot do more than the page; the routes add the sign-in guard and the permission dependency on the
changing routes so a refused call never reaches the service.

Handlers are plain `def`: FastAPI runs them in its thread pool (disk and S3 work never blocks the event loop).
"""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.exceptions import HTTPException

from ...auth import deps
from ...datacenter import DCError, service

MANAGE = deps.require_perm('data.manage')
CACHE = {'Cache-Control': 'private, max-age=300'}


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except DCError as e:
        raise HTTPException(e.status, e.message) from None


def router(state, ctx):
    service.configure(ctx=ctx)
    r = APIRouter(prefix='/api/dc')

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
                     prefix=body.get('prefix') or '', tags=tuple(body.get('tags') or ()))

    @r.get('/buckets/{bid}')
    def get_bucket(bid: str, user=Depends(deps.current_user)):
        return _call(service.get_bucket, user, bid)

    @r.patch('/buckets/{bid}')
    def update_bucket(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.update_bucket, user, bid, name=body.get('name'))

    @r.delete('/buckets/{bid}')
    def delete_bucket(bid: str, delete_files: int = 0, user=Depends(MANAGE)):
        return _call(service.delete_bucket, user, bid, delete_files=bool(delete_files))

    @r.get('/buckets/{bid}/ls')
    def ls(bid: str, path: str = '', offset: int = 0, limit: int = 500, sort: str = 'name', q: str = '',
           user=Depends(deps.current_user)):
        return _call(service.ls, user, bid, path, offset=offset, limit=limit, sort=sort, q=q)

    @r.get('/buckets/{bid}/file')
    def read_file(bid: str, path: str, w: int | None = None, user=Depends(deps.current_user)):
        if not w:
            local = _call(service.file_path, user, bid, path)
            if local is not None:                       # a local file is streamed, never read into memory
                return FileResponse(local[0], media_type=local[1], headers=CACHE)
        data, mt = _call(service.read_file, user, bid, path, w=w)
        return Response(data, media_type=mt, headers=CACHE)

    @r.get('/buckets/{bid}/text')
    def read_text(bid: str, path: str, user=Depends(deps.current_user)):
        return _call(service.read_text, user, bid, path)

    @r.post('/buckets/{bid}/ops')
    def ops(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.ops, user, bid, body.get('ops'))

    @r.post('/buckets/{bid}/undo')
    def undo(bid: str, body: dict, user=Depends(MANAGE)):
        return _call(service.undo, user, bid, body.get('undo_id'))

    @r.post('/buckets/{bid}/upload')
    def upload(bid: str, path: str = '', unzip: int = 1, files: list[UploadFile] = File(...), user=Depends(MANAGE)):
        try:
            return _call(service.upload, user, bid, path, [(f.filename or 'file', f.file) for f in files],
                         unzip=bool(unzip))
        finally:
            for f in files:
                f.file.close()

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

    return r
