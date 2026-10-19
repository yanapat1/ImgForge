"""
Image-only sources for labeling sessions (plain image folders: no images/ + labels/ layout needed), the
remote image cache and thumbnails.

    UploadSource(dir)                     files the upload endpoint streamed to <pid>/images/
    LocalImageSource(path, recursive)     a local folder, read in place
    S3ImageSource(conn, bucket, prefix)   S3 / RustFS / MinIO prefix
    FTPImageSource(conn, path)            FTP / FTPS, 4 pooled connections (ftplib is not thread-safe)
    SFTPImageSource(conn, path)           SFTP (paramiko), pooled clients

Every source: kind, uri, list_images() -> [(rel, size)] sorted by rel, read_bytes(rel), read_head(rel, n),
local_path(rel) (local kinds only: served directly, never copied), fetch_to(rel, fileobj), close().

probe() reads only the header bytes (64 KB Range GET on S3) to get the EXIF-oriented size; ImageCache keeps
remote bytes in <pid>/cache/ and builds thumbnails / previews lazily. Remote reads run on threads: the
network waits release the GIL.
"""

import io
import os
import stat
import time
import ftplib
import hashlib
import posixpath
import threading
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps

from ..core import logging as vlog
from ..data import sources
from ..data.sources import IMAGE_EXTS, _is_image

_log = vlog.get_logger('label')

# labeling sessions hold the user's own (sometimes > 16k px) images: the 178 MP bomb guard would refuse them
Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, 1_000_000_000)

HEAD_BYTES = 65536
NET_TIMEOUT = 30                 # seconds, connect + per-operation for FTP / SFTP
RETRIES = 2                      # extra attempts on a broken pooled connection
SCAN_CAP = 10000                 # scan_folder_images stops counting here (browse stays instant)

__all__ = ['IMAGE_EXTS', 'UploadSource', 'LocalImageSource', 'S3ImageSource', 'FTPImageSource', 'SFTPImageSource',
           'make_source', 'probe', 'ImageCache', 'scan_folder_images', 'browse_remote', 'remote_test']


def _hidden(rel):
    return any(part.startswith('.') for part in rel.split('/'))


def _check_rel(rel):
    """rel comes from the DB / the page: never let it escape the source root."""
    rel = str(rel).replace('\\', '/')
    if rel.startswith('/') or '..' in rel.split('/') or not rel:
        raise ValueError(f'bad image path: {rel!r}')
    return rel


class ImageSource:
    kind = 'base'
    uri = ''

    def __init__(self):
        self._sizes = {}

    def list_images(self):
        raise NotImplementedError

    def read_bytes(self, rel):
        raise NotImplementedError

    def read_head(self, rel, n=HEAD_BYTES):
        return self.read_bytes(rel)[:n]

    def fetch_to(self, rel, f):
        f.write(self.read_bytes(rel))

    def local_path(self, rel):
        return None

    def size(self, rel):
        """Size from the last listing (None when unknown)."""
        return self._sizes.get(rel)

    def close(self):
        pass

    def _remember(self, items):
        items.sort()
        self._sizes = dict(items)
        return items


# ---------------------------------------------------------------- local kinds

class LocalImageSource(ImageSource):
    kind = 'local'

    def __init__(self, path, recursive=True):
        super().__init__()
        self.root = Path(path).expanduser().resolve()
        self.recursive = recursive
        self.uri = str(self.root)

    def list_images(self, match=_is_image):
        """(rel, size) of every image; `match` picks other files instead (the annotation import lists .txt / .json)."""
        if not self.root.is_dir():
            raise ValueError(f'not a folder: {self.root}')
        out, stack = [], [('', self.root)]
        while stack:                                   # os.scandir: one syscall per folder, sizes from the dirent
            prefix, d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError as e:
                _log.warning(f'cannot list {vlog.esc(d)}: {vlog.esc(e)}')
                continue
            with it:
                for e in it:
                    if e.name.startswith('.'):
                        continue
                    if e.is_dir(follow_symlinks=True):
                        if self.recursive:
                            stack.append((f'{prefix}{e.name}/', e.path))
                    elif match(e.name):
                        try:
                            out.append((prefix + e.name, e.stat().st_size))
                        except OSError:
                            pass
        return self._remember(out)

    def local_path(self, rel):
        return self.root / _check_rel(rel)

    def read_bytes(self, rel):
        return self.local_path(rel).read_bytes()

    def read_head(self, rel, n=HEAD_BYTES):
        with open(self.local_path(rel), 'rb') as f:
            return f.read(n)

    def fetch_to(self, rel, f):
        with open(self.local_path(rel), 'rb') as src:
            while chunk := src.read(1 << 20):
                f.write(chunk)


class UploadSource(LocalImageSource):
    kind = 'upload'

    def __init__(self, dir):
        Path(dir).mkdir(parents=True, exist_ok=True)
        super().__init__(dir, recursive=True)


# ---------------------------------------------------------------- S3

class S3ImageSource(ImageSource):
    kind = 's3'

    def __init__(self, conn, bucket, prefix='', s3cfg=None):
        super().__init__()
        self.bucket = bucket
        self.prefix = sources._norm_prefix(prefix)
        self.client = sources.make_client(conn, s3cfg)
        self.uri = f's3://{bucket}/{self.prefix}'.rstrip('/')

    def list_images(self, match=_is_image):
        out = []
        n = len(self.prefix)
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=self.prefix):
            for o in page.get('Contents', []):
                rel = o['Key'][n:]
                if rel and match(rel) and not _hidden(rel):
                    out.append((rel, int(o['Size'])))
        return self._remember(out)

    def _body(self, rel, **kw):
        return self.client.get_object(Bucket=self.bucket, Key=self.prefix + _check_rel(rel), **kw)['Body']

    def read_bytes(self, rel):
        # get_object + iter_chunks: 3.3x faster than download_file (data/README.md)
        return b''.join(self._body(rel).iter_chunks(1 << 20))

    def read_head(self, rel, n=HEAD_BYTES):
        return self._body(rel, Range=f'bytes=0-{n - 1}').read()

    def fetch_to(self, rel, f):
        for chunk in self._body(rel).iter_chunks(1 << 20):
            f.write(chunk)

    def close(self):
        try:
            self.client.close()
        except Exception:  # noqa: BLE001 - older botocore
            pass


# ---------------------------------------------------------------- connection pool (FTP / SFTP)

class _Pool:
    """At most `size` live connections, checked out one per worker; a connection that raised is dropped.
    close() drops every connection (in-use ones when returned); a later call reconnects lazily."""

    def __init__(self, factory, closer, size=4, retry_on=(OSError, EOFError, ftplib.error_temp, ftplib.error_reply)):
        self.factory, self.closer, self.size, self.retry_on = factory, closer, size, retry_on
        self._idle, self._n, self._gen = [], 0, 0
        self._cv = threading.Condition()

    @contextmanager
    def conn(self):
        with self._cv:
            while not self._idle and self._n >= self.size:
                self._cv.wait()
            c = self._idle.pop() if self._idle else None
            if c is None:
                self._n += 1
            gen = self._gen
        if c is None:
            try:
                c = self.factory()
            except BaseException:
                with self._cv:
                    self._n -= 1
                    self._cv.notify()
                raise
        ok = False
        try:
            yield c
            ok = True
        finally:
            with self._cv:
                keep = ok and gen == self._gen
                if keep:
                    self._idle.append(c)
                else:
                    self._n -= 1
                self._cv.notify()
            if not keep:
                self._safe_close(c)

    def run(self, fn, retries=RETRIES):
        """fn(conn) with a fresh connection on a network error (a pooled one may have timed out)."""
        for attempt in range(retries + 1):
            try:
                with self.conn() as c:
                    return fn(c)
            except self.retry_on as e:
                if attempt == retries or isinstance(e, FileNotFoundError):
                    raise
                time.sleep(0.2 * (attempt + 1))

    def _safe_close(self, c):
        try:
            self.closer(c)
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        with self._cv:
            self._gen += 1
            idle, self._idle = self._idle, []
            self._n -= len(idle)
            self._cv.notify_all()
        for c in idle:
            self._safe_close(c)


def _remote_root(conn, path):
    p = (path or conn.get('base_path') or '/').replace('\\', '/')
    return posixpath.normpath('/' + p.lstrip('/')) if p not in ('', '.') else '/'


# ---------------------------------------------------------------- FTP

def ftp_connect(conn):
    cls = ftplib.FTP_TLS if conn.get('tls') else ftplib.FTP
    ftp = cls(timeout=NET_TIMEOUT, encoding='utf-8')
    ftp.connect(conn['host'], int(conn.get('port') or 21))
    ftp.login(conn.get('username') or 'anonymous', conn.get('secret_key') or '')
    if conn.get('tls'):
        ftp.prot_p()                                  # encrypt the data channel too
    ftp.set_pasv(conn.get('passive', True) is not False)
    ftp.voidcmd('TYPE I')
    return ftp


def _ftp_close(ftp):
    try:
        ftp.quit()
    except Exception:  # noqa: BLE001
        ftp.close()


def _ftp_entries(ftp, path, state):
    """[(name, is_dir, size)] of one folder: MLSD, falling back to NLST + SIZE on servers without it."""
    if state.get('mlsd', True):
        try:
            out = []
            for name, facts in ftp.mlsd(path, facts=['type', 'size']):
                t = facts.get('type', '').lower()
                if name in ('.', '..') or t in ('cdir', 'pdir'):
                    continue
                out.append((name, t == 'dir', int(facts.get('size') or 0)))
            return out
        except ftplib.error_perm as e:
            if not str(e).startswith(('500', '501', '502', '504')):
                raise
            state['mlsd'] = False
    out = []
    try:
        names = ftp.nlst(path)
    except ftplib.error_perm as e:                    # 550 on an empty folder for some servers
        if str(e).startswith('550'):
            return []
        raise
    ftp.voidcmd('TYPE I')                             # NLST left ASCII mode on: SIZE is refused there
    for n in names:
        name = posixpath.basename(n.rstrip('/'))
        if name in ('.', '..', ''):
            continue
        full = posixpath.join(path, name)
        try:
            size = ftp.size(full)
            out.append((name, False, int(size or 0)))
        except ftplib.error_perm:                     # SIZE fails on folders
            out.append((name, True, 0))
    return out


class FTPImageSource(ImageSource):
    kind = 'ftp'

    def __init__(self, conn, path=None, pool_size=4):
        super().__init__()
        self.conn = conn
        self.root = _remote_root(conn, path)
        self.uri = f"ftp://{conn['host']}{self.root}"
        self.pool = _Pool(lambda: ftp_connect(conn), _ftp_close, pool_size)
        self._state = {}

    def _p(self, rel):
        return posixpath.join(self.root, _check_rel(rel))

    def entries(self, path):
        return self.pool.run(lambda ftp: _ftp_entries(ftp, path, self._state))

    def list_images(self, recursive=True, match=_is_image):
        out, stack = [], ['']
        while stack:
            prefix = stack.pop()
            for name, is_dir, size in self.entries(posixpath.join(self.root, prefix)):
                if name.startswith('.'):
                    continue
                if is_dir:
                    if recursive:
                        stack.append(f'{prefix}{name}/')
                elif match(name):
                    out.append((prefix + name, size))
        return self._remember(out)

    def fetch_to(self, rel, f):
        path = self._p(rel)

        def go(ftp):
            f.seek(0)
            f.truncate()
            ftp.retrbinary(f'RETR {path}', f.write, blocksize=1 << 20)
        self.pool.run(go)

    def read_bytes(self, rel):
        buf = io.BytesIO()
        self.fetch_to(rel, buf)
        return buf.getvalue()

    def read_head(self, rel, n=HEAD_BYTES):
        path = self._p(rel)

        def go(ftp):
            ftp.voidcmd('TYPE I')
            parts, got = [], 0
            with ftp.transfercmd(f'RETR {path}') as sock:
                while got < n:
                    b = sock.recv(min(1 << 16, n - got))
                    if not b:
                        break
                    parts.append(b)
                    got += len(b)
            try:                                      # closing early: the server answers 426 (or 226 if done)
                ftp.voidresp()
            except (ftplib.error_temp, ftplib.error_perm):
                pass
            return b''.join(parts)
        return self.pool.run(go)

    def close(self):
        self.pool.close()


# ---------------------------------------------------------------- SFTP

def _load_pkey(text, passphrase=None):
    import paramiko
    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except Exception as e:  # noqa: BLE001 - try the next key type
            errors.append(f'{cls.__name__}: {e}')
    raise ValueError('cannot read the private key (' + '; '.join(errors) + ')')


def sftp_connect(conn):
    import paramiko
    ssh = paramiko.SSHClient()
    # host keys are not pinned: the connection is configured by the server's owner in Settings
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = _load_pkey(conn['private_key'], conn.get('secret_key')) if conn.get('private_key') else None
    ssh.connect(conn['host'], port=int(conn.get('port') or 22), username=conn.get('username') or None,
                password=conn.get('secret_key') or None, pkey=pkey, look_for_keys=False, allow_agent=False,
                timeout=NET_TIMEOUT, banner_timeout=NET_TIMEOUT, auth_timeout=NET_TIMEOUT)
    sftp = ssh.open_sftp()
    sftp.get_channel().settimeout(NET_TIMEOUT)
    sftp._ssh = ssh                                   # keep the transport owner alive with the client
    return sftp


def _sftp_close(sftp):
    sftp.close()
    sftp._ssh.close()


def _sftp_entries(sftp, path):
    return [(a.filename, stat.S_ISDIR(a.st_mode or 0), int(a.st_size or 0)) for a in sftp.listdir_attr(path)]


class SFTPImageSource(ImageSource):
    kind = 'sftp'

    def __init__(self, conn, path=None, pool_size=8):
        super().__init__()
        self.conn = conn
        self.root = _remote_root(conn, path)
        self.uri = f"sftp://{conn['host']}{self.root}"
        # a checkout pool (not one client per thread): import / prefetch pools come and go, a pool caps the sessions
        import paramiko
        self.pool = _Pool(lambda: sftp_connect(conn), _sftp_close, pool_size,
                          retry_on=(OSError, EOFError, paramiko.SSHException))

    def _p(self, rel):
        return posixpath.join(self.root, _check_rel(rel))

    def entries(self, path):
        return self.pool.run(lambda s: _sftp_entries(s, path))

    def list_images(self, recursive=True, match=_is_image):
        out, stack = [], ['']
        while stack:
            prefix = stack.pop()
            for name, is_dir, size in self.entries(posixpath.join(self.root, prefix)):
                if name.startswith('.'):
                    continue
                if is_dir:
                    if recursive:
                        stack.append(f'{prefix}{name}/')
                elif match(name):
                    out.append((prefix + name, size))
        return self._remember(out)

    def fetch_to(self, rel, f):
        path = self._p(rel)

        def go(s):
            f.seek(0)
            f.truncate()
            s.getfo(path, f)
        self.pool.run(go)

    def read_bytes(self, rel):
        buf = io.BytesIO()
        self.fetch_to(rel, buf)
        return buf.getvalue()

    def read_head(self, rel, n=HEAD_BYTES):
        path = self._p(rel)

        def go(s):
            with s.open(path, 'rb') as f:
                return f.read(n)
        return self.pool.run(go)

    def close(self):
        self.pool.close()


# ---------------------------------------------------------------- factory / browse helpers

def _remote_source(conn, path=None):
    kind = conn.get('kind') or 's3'
    if kind == 'ftp':
        return FTPImageSource(conn, path)
    if kind == 'sftp':
        return SFTPImageSource(conn, path)
    raise ValueError(f'not an FTP / SFTP connection: {kind}')


def make_source(source, project_dir, connections, s3cfg=None):
    kind = (source or {}).get('kind')
    if kind == 'upload':
        return UploadSource(Path(project_dir) / 'images')
    if kind == 'local':
        if not source.get('path'):
            raise ValueError('path is required')
        return LocalImageSource(source['path'], recursive=source.get('recursive', True) is not False)
    if kind in ('s3', 'ftp', 'sftp'):
        if not source.get('connection_id'):
            raise ValueError('connection_id is required')
        conn = connections.connection(source['connection_id'])
        ckind = conn.get('kind') or 's3'
        if ckind != kind:
            raise ValueError(f'connection {conn.get("name")!r} is {ckind}, not {kind}')
        if kind == 's3':
            if not source.get('bucket'):
                raise ValueError('bucket is required')
            return S3ImageSource(conn, source['bucket'], source.get('prefix', ''), s3cfg)
        return _remote_source(conn, source.get('path'))
    raise ValueError(f'unknown image source kind: {kind!r}')


def browse_remote(conn, path=None):
    """FTP / SFTP folder browser: {path, parent, folders:[{name, images}], images} (image counts capped)."""
    src = _remote_source(conn, path)
    try:
        here = src.entries(src.root)
        folders = sorted((n for n, d, _ in here if d and not n.startswith('.')), key=str.lower)

        def count(name):
            try:
                return sum(1 for n, d, _ in src.entries(posixpath.join(src.root, name)) if not d and _is_image(n))
            except Exception:  # noqa: BLE001 - unreadable folder: shown without a count
                return None
        with ThreadPoolExecutor(src.pool.size) as ex:
            counts = list(ex.map(count, folders[:500]))
        counts += [None] * (len(folders) - len(counts))
        return {'path': src.root, 'parent': posixpath.dirname(src.root) if src.root != '/' else None,
                'folders': [{'name': n, 'images': c} for n, c in zip(folders, counts)],
                'images': sum(1 for n, d, _ in here if not d and _is_image(n))}
    finally:
        src.close()


def remote_test(conn):
    """Connect + list the base path (Settings › Connections › Test)."""
    t0 = time.time()
    src = _remote_source(conn)
    try:
        here = src.entries(src.root)
    finally:
        src.close()
    return {'ok': True, 'ms': round((time.time() - t0) * 1000), 'path': src.root,
            'folders': sum(1 for _, d, _ in here if d), 'images': sum(1 for n, d, _ in here if not d and _is_image(n))}


def scan_folder_images(path, cap=SCAN_CAP):
    """Direct image files in a local folder, counted up to `cap` (the folder browser's badge)."""
    return sources.count_images(path, cap)


# ---------------------------------------------------------------- probe

_ORIENT_SWAP = {5, 6, 7, 8}                         # EXIF orientations that rotate by 90 degrees


def _probe_image(im):
    w, h = im.size
    try:
        orientation = int(im.getexif().get(0x0112) or 1)
    except Exception:  # noqa: BLE001 - broken EXIF: treat as upright
        orientation = 1
    if orientation in _ORIENT_SWAP:
        w, h = h, w
    return {'width': w, 'height': h, 'mime': Image.MIME.get(im.format or '', None), 'orientation': orientation}


def probe(source, rel):
    """{width, height, mime, size_bytes, orientation}, reading only the header when possible."""
    lp = source.local_path(rel)
    if lp is not None:
        with Image.open(lp) as im:                    # lazy: reads the header, not the pixels
            out = _probe_image(im)
        out['size_bytes'] = lp.stat().st_size
        return out
    head = source.read_head(rel, HEAD_BYTES)
    data = None
    try:
        with Image.open(io.BytesIO(head)) as im:
            out = _probe_image(im)
        if len(head) < HEAD_BYTES:
            data = head                               # the whole file fitted in the head read
    except Exception:  # noqa: BLE001 - SOF / EXIF beyond 64 KB: read the whole file
        data = source.read_bytes(rel)
        with Image.open(io.BytesIO(data)) as im:
            out = _probe_image(im)
    size = source.size(rel)
    out['size_bytes'] = size if size is not None else (len(data) if data is not None else None)
    return out


# ---------------------------------------------------------------- cache, thumbnails, previews

_prefetch_pool = None
_prefetch_lock = threading.Lock()


def _shared_pool():
    global _prefetch_pool
    with _prefetch_lock:
        if _prefetch_pool is None:
            _prefetch_pool = ThreadPoolExecutor(4, thread_name_prefix='label-prefetch')
        return _prefetch_pool


def _atomic_write(dst, write):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f'{dst.name}.{threading.get_ident()}.part')
    try:
        with open(tmp, 'wb') as f:
            write(f)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst


def _render(src, dst, max_side, quality, resample, reducing_gap):
    """src -> EXIF-oriented RGB JPEG whose longest side is <= max_side (never upscaled), written atomically."""
    with Image.open(src) as im:
        if im.format == 'JPEG':
            im.draft('RGB', (max_side, max_side))     # decode at 1/2..1/8 scale: 4-10x faster on big JPEGs
        out = ImageOps.exif_transpose(im)             # a copy (or im itself): the file closes with the block
        if out.mode not in ('RGB', 'L'):
            out = out.convert('RGBA').convert('RGB') if out.mode in ('P', 'LA', 'RGBA', 'PA') else out.convert('RGB')
        out.thumbnail((max_side, max_side), resample, reducing_gap=reducing_gap)
    _atomic_write(dst, lambda f: out.save(f, 'JPEG', quality=quality))
    return dst


class ImageCache:
    """Remote bytes in <pid>/cache/sha1(rel).<ext>; local sources pass through. Thread-safe per key."""

    def __init__(self, project_dir, source):
        self.dir = Path(project_dir)
        self.source = source
        self.cache_dir = self.dir / 'cache'
        self.thumb_dir = self.dir / 'thumbs'
        self._locks = {}
        self._locks_lock = threading.Lock()
        self._inflight = set()

    def _lock(self, key):
        with self._locks_lock:
            lk = self._locks.get(key)
            if lk is None:
                lk = self._locks[key] = threading.Lock()
            return lk

    def _cached(self, rel):
        ext = os.path.splitext(rel)[1].lower() or '.bin'
        return self.cache_dir / f'{hashlib.sha1(rel.encode()).hexdigest()}{ext}'

    def path(self, rel):
        lp = self.source.local_path(rel)
        if lp is not None:
            if not lp.is_file():
                raise FileNotFoundError(f'image is missing: {lp}')
            return lp
        dst = self._cached(_check_rel(rel))
        if dst.is_file():
            return dst
        with self._lock(dst.name):
            if not dst.is_file():                     # another thread may have fetched it meanwhile
                _atomic_write(dst, lambda f: self.source.fetch_to(rel, f))
        return dst

    def prefetch(self, rels):
        if self.source.local_path('x') is not None:   # local kinds: nothing to fetch
            return
        pool = _shared_pool()
        for rel in rels:
            try:
                if self._cached(_check_rel(rel)).is_file():
                    continue
            except ValueError:
                continue
            with self._locks_lock:
                if rel in self._inflight:
                    continue
                self._inflight.add(rel)
            pool.submit(self._prefetch_one, rel)

    def _prefetch_one(self, rel):
        try:
            self.path(rel)
        except Exception as e:  # noqa: BLE001 - prefetch is best effort; the real request reports errors
            _log.debug(f'prefetch {vlog.esc(rel)} failed: {vlog.esc(e)}')
        finally:
            with self._locks_lock:
                self._inflight.discard(rel)

    def thumb(self, media_id, rel, size=160):
        dst = self.thumb_dir / (f'{media_id}.jpg' if size == 160 else f'{media_id}_{size}.jpg')
        if dst.is_file():
            return dst
        with self._lock(dst.name):
            if not dst.is_file():
                _render(self.path(rel), dst, size, 80, Image.Resampling.BILINEAR, 2.0)
        return dst

    def preview(self, rel, max_side):
        """A downscaled JPEG when the image is larger than max_side (the original path otherwise)."""
        src = self.path(rel)
        max_side = max(64, int(max_side))
        with Image.open(src) as im:
            if max(im.size) <= max_side:
                return src
        dst = self.cache_dir / 'preview' / f'{hashlib.sha1(rel.encode()).hexdigest()}_{max_side}.jpg'
        if dst.is_file():
            return dst
        with self._lock(dst.name):
            if not dst.is_file():
                _render(src, dst, max_side, 90, Image.Resampling.LANCZOS, 3.0)
        return dst
