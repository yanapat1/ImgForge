"""
File export targets. export.py writes `images/` once plus one folder per task through one of these:

    ZipSink(run_dir, name)            <run_dir>/<name>.zip  (ZIP_DEFLATED text, ZIP_STORED images)
    LocalDirSink(path)                a local folder, atomic writes
    S3Sink(conn, bucket, prefix)      put_object on a 16-thread pool
    FTPSink(conn, path) / SFTPSink    mkdir -p + put on 4 pooled connections

write(rel, data) and copy_file(rel, src) are thread-safe and may return before the upload finishes;
close() waits for every upload and raises the first error, then returns {files, bytes, uri, path?}.
"""

import io
import os
import time
import shutil
import ftplib
import posixpath
import threading
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from ..data import sources
from . import media

_STORED_EXTS = media.IMAGE_EXTS + ('.gif', '.tif', '.tiff', '.zip', '.gz')


def _check_rel(rel):
    rel = str(rel).replace('\\', '/').lstrip('/')
    if not rel or '..' in rel.split('/'):
        raise ValueError(f'bad export path: {rel!r}')
    return rel


class Sink:
    kind = 'base'
    uri = ''

    def __init__(self):
        self.files = 0
        self.bytes = 0
        self._stat_lock = threading.Lock()

    def _count(self, n):
        with self._stat_lock:
            self.files += 1
            self.bytes += n

    def check_target(self, overwrite):
        pass

    def summary(self):
        return {'files': self.files, 'bytes': self.bytes, 'uri': self.uri}


class _AsyncSink(Sink):
    """Uploads run on a pool; at most `window` are queued so bytes never pile up in memory."""

    def __init__(self, workers):
        super().__init__()
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix=f'sink-{self.kind}')
        self._sem = threading.BoundedSemaphore(workers * 4)
        self._futs = []
        self._error = None

    def _submit(self, fn, *args):
        if self._error is not None:
            raise self._error
        self._sem.acquire()
        fut = self._pool.submit(self._guard, fn, *args)
        self._futs.append(fut)
        if len(self._futs) > 4096:
            self._futs = [f for f in self._futs if not f.done()]

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except BaseException as e:
            if self._error is None:
                self._error = e
        finally:
            self._sem.release()

    def _finish(self):
        self._pool.shutdown(wait=True)
        if self._error is not None:
            raise self._error

    def close(self):
        self._finish()
        return self.summary()


# ---------------------------------------------------------------- zip

class ZipSink(Sink):
    kind = 'zip'

    def __init__(self, run_dir, name='export'):
        super().__init__()
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f'{name}.zip'
        self._tmp = self.path.with_name(self.path.name + '.part')
        self._zf = zipfile.ZipFile(self._tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
        self._lock = threading.Lock()
        self._names = set()
        self.uri = str(self.path)

    def _info(self, rel, compress):
        zi = zipfile.ZipInfo(rel, time.localtime()[:6])
        zi.compress_type = compress
        zi.external_attr = 0o644 << 16
        return zi

    def _compress_for(self, rel):
        # images are already compressed: deflating them only burns CPU
        return zipfile.ZIP_STORED if rel.lower().endswith(_STORED_EXTS) else zipfile.ZIP_DEFLATED

    def write(self, rel, data):
        rel = _check_rel(rel)
        if isinstance(data, str):
            data = data.encode()
        with self._lock:
            if rel in self._names:
                return
            self._names.add(rel)
            self._zf.writestr(self._info(rel, self._compress_for(rel)), data)
        self._count(len(data))

    def copy_file(self, rel, src):
        rel = _check_rel(rel)
        with self._lock:
            if rel in self._names:
                return
            self._names.add(rel)
            self._zf.write(src, rel, compress_type=self._compress_for(rel))
        self._count(os.path.getsize(src))

    def close(self):
        with self._lock:
            self._zf.close()
            os.replace(self._tmp, self.path)
        return {**self.summary(), 'path': str(self.path)}

    def abort(self):
        with self._lock:
            try:
                self._zf.close()
            finally:
                self._tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- local folder

class LocalDirSink(Sink):
    kind = 'local'

    def __init__(self, path):
        super().__init__()
        self.root = Path(path).expanduser().resolve()
        self.uri = str(self.root)
        self._dirs = set()
        self._dirs_lock = threading.Lock()

    def check_target(self, overwrite):
        if self.root.exists() and not self.root.is_dir():
            raise ValueError(f'not a folder: {self.root}')
        if not overwrite and self.root.is_dir() and any(self.root.iterdir()):
            raise ValueError(f'{self.root} is not empty (turn on overwrite to write into it)')

    def _dst(self, rel):
        dst = self.root / _check_rel(rel)
        parent = dst.parent
        if parent not in self._dirs:
            parent.mkdir(parents=True, exist_ok=True)
            with self._dirs_lock:
                self._dirs.add(parent)
        return dst

    def _tmp(self, dst):
        return dst.with_name(f'.{dst.name}.{threading.get_ident()}.part')

    def write(self, rel, data):
        if isinstance(data, str):
            data = data.encode()
        dst = self._dst(rel)
        tmp = self._tmp(dst)
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, dst)
        self._count(len(data))

    def copy_file(self, rel, src):
        dst = self._dst(rel)
        tmp = self._tmp(dst)
        shutil.copyfile(src, tmp)                     # copy_file_range / sendfile under the hood
        os.replace(tmp, dst)
        self._count(os.path.getsize(dst))

    def close(self):
        return {**self.summary(), 'path': str(self.root)}


# ---------------------------------------------------------------- S3

class S3Sink(_AsyncSink):
    kind = 's3'

    def __init__(self, conn, bucket, prefix='', s3cfg=None, workers=16):
        self.client = sources.make_client(conn, {**(s3cfg or {}), 'concurrency': max(workers, int((s3cfg or {}).get('concurrency', 0)))})
        self.bucket = bucket
        self.prefix = sources._norm_prefix(prefix)
        super().__init__(workers)
        self.uri = f's3://{bucket}/{self.prefix}'.rstrip('/')

    def check_target(self, overwrite):
        r = self.client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
        if not overwrite and r.get('KeyCount', len(r.get('Contents', []))):
            raise ValueError(f'{self.uri} is not empty (turn on overwrite to write into it)')

    def _put(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        self._count(len(data))

    def _put_file(self, key, src):
        size = os.path.getsize(src)
        with open(src, 'rb') as f:                    # put_object, not upload_file: no extra transfer pool per file
            self.client.put_object(Bucket=self.bucket, Key=key, Body=f, ContentLength=size)
        self._count(size)

    def write(self, rel, data):
        self._submit(self._put, self.prefix + _check_rel(rel), data.encode() if isinstance(data, str) else data)

    def copy_file(self, rel, src):
        self._submit(self._put_file, self.prefix + _check_rel(rel), str(src))


# ---------------------------------------------------------------- FTP / SFTP

class _RemoteDirSink(_AsyncSink):
    def __init__(self, conn, path, workers=4):
        self.conn = conn
        self.root = media._remote_root(conn, path)
        self._made = {'/'}
        self._made_lock = threading.Lock()
        super().__init__(workers)

    def _ensure_dir(self, c, d):
        with self._made_lock:
            if d in self._made:
                return
        parts, cur = d.strip('/').split('/'), ''
        for p in parts:
            cur = f'{cur}/{p}'
            with self._made_lock:
                if cur in self._made:
                    continue
            self._mkdir(c, cur)
            with self._made_lock:
                self._made.add(cur)

    def _upload(self, rel, data=None, src=None):
        path = posixpath.join(self.root, rel)

        def go(c):
            self._ensure_dir(c, posixpath.dirname(path))
            if src is not None:
                with open(src, 'rb') as f:
                    self._put(c, path, f)
            else:
                self._put(c, path, io.BytesIO(data))
        self.pool.run(go)
        self._count(len(data) if src is None else os.path.getsize(src))

    def write(self, rel, data):
        self._submit(self._upload, _check_rel(rel), data.encode() if isinstance(data, str) else data)

    def copy_file(self, rel, src):
        self._submit(self._upload, _check_rel(rel), None, str(src))

    def check_target(self, overwrite):
        if overwrite:
            return
        try:
            entries = self.pool.run(lambda c: self._entries(c, self.root))
        except (FileNotFoundError, ftplib.error_perm):
            return                                    # missing folder: created on the first write
        if entries:
            raise ValueError(f'{self.uri} is not empty (turn on overwrite to write into it)')

    def close(self):
        try:
            self._finish()
        finally:
            self.pool.close()
        return self.summary()


class FTPSink(_RemoteDirSink):
    kind = 'ftp'

    def __init__(self, conn, path=None, workers=4):
        super().__init__(conn, path, workers)
        self.uri = f"ftp://{conn['host']}{self.root}"
        self.pool = media._Pool(lambda: media.ftp_connect(conn), media._ftp_close, workers)

    def _mkdir(self, ftp, d):
        try:
            ftp.mkd(d)
        except ftplib.error_perm:                     # 550 exists (or no permission: STOR reports it)
            pass

    def _put(self, ftp, path, f):
        ftp.storbinary(f'STOR {path}', f, blocksize=1 << 20)

    def _entries(self, ftp, path):
        state = {}
        return media._ftp_entries(ftp, path, state)


class SFTPSink(_RemoteDirSink):
    kind = 'sftp'

    def __init__(self, conn, path=None, workers=4):
        import paramiko
        super().__init__(conn, path, workers)
        self.uri = f"sftp://{conn['host']}{self.root}"
        self.pool = media._Pool(lambda: media.sftp_connect(conn), media._sftp_close, workers,
                                retry_on=(OSError, EOFError, paramiko.SSHException))

    def _mkdir(self, sftp, d):
        try:
            sftp.stat(d)
        except FileNotFoundError:
            try:
                sftp.mkdir(d)
            except OSError:                           # created by another worker meanwhile
                sftp.stat(d)

    def _put(self, sftp, path, f):
        sftp.putfo(f, path, confirm=False)

    def _entries(self, sftp, path):
        return sftp.listdir(path)


# ---------------------------------------------------------------- factory

def make_sink(target, connections, s3cfg, run_dir):
    kind = (target or {}).get('kind')
    if kind == 'zip':
        run_dir = Path(run_dir)
        return ZipSink(run_dir, target.get('name') or run_dir.name)
    if kind == 'local':
        if not target.get('path'):
            raise ValueError('path is required')
        return LocalDirSink(target['path'])
    if kind in ('s3', 'ftp', 'sftp'):
        if not target.get('connection_id'):
            raise ValueError('connection_id is required')
        conn = connections.connection(target['connection_id'])
        ckind = conn.get('kind') or 's3'
        if ckind != kind:
            raise ValueError(f'connection {conn.get("name")!r} is {ckind}, not {kind}')
        if kind == 's3':
            if not target.get('bucket'):
                raise ValueError('bucket is required')
            return S3Sink(conn, target['bucket'], target.get('prefix', ''), s3cfg)
        return (FTPSink if kind == 'ftp' else SFTPSink)(conn, target.get('path'))
    raise ValueError(f'unknown export target kind: {kind!r}')
