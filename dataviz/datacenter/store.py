"""
The Data Center's filesystem layer: where buckets live, safe path joins, paged folder listings, file operations
with an undo manifest, streamed uploads / safe unzip, thumbnails, and the read-only S3 view. No database and no
users here: datacenter/service.py decides who may touch which bucket, this module only keeps every path inside
its bucket.

Why the care with paths: a bucket path comes from the page or from an LLM tool call, so `..`, absolute paths,
drive letters and symlinks pointing out of the bucket are all refused (DCError 400) before anything is opened,
and every resolved path is also checked against the allowed roots (connections.check_local).

Listing is os.scandir based: names and the dir/file bit come from the dirent, so only the PAGE that is returned
is stat'ed (a 20k-file folder sorted by name costs one scandir + 500 stats).
"""

import hashlib
import io
import os
import shutil
import stat as statmod
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from ..data import connections

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tif', '.tiff', '.gif')
KIND_EXTS = {
    'yaml': ('.yaml', '.yml'), 'json': ('.json',), 'xml': ('.xml',), 'npy': ('.npy', '.npz'),
    'archive': ('.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar'),
    'text': ('.txt', '.csv', '.tsv', '.md', '.log', '.cfg', '.ini', '.names', '.py', '.sh', '.parquet'),
}
SPLITS = ('train', 'val', 'valid', 'validation', 'test')
TRASH = '.trash'
ITEMS_CAP = 10000                   # a folder's "items" badge stops counting here
ZIP_MAX_FILES = 500_000
CHUNK = 1 << 20


class DCError(Exception):
    """A Data Center failure with the HTTP status it maps to; the message is shown to the user / the model."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = str(message)
        self.status = int(status)

    def __str__(self):
        return self.message


# ---------------------------------------------------------------------------------------------- roots
def data_root(cfg):
    """storage.data_root resolved (relative = to the project root, like allowed_roots); created on first use."""
    from ..core.config import resolve
    raw = ((cfg or {}).get('storage') or {}).get('data_root') or 'data'
    p = Path(os.path.realpath(resolve(str(raw))))
    p.mkdir(parents=True, exist_ok=True)
    return p


def roots(cfg):
    """Where a resolved local path may lie: storage.allowed_roots plus the managed data root (always allowed)."""
    return [*connections.allowed_roots(cfg), data_root(cfg)]


def cache_dir(cfg, name='dc_thumbs'):
    from ..core.config import resolve
    ws = ((cfg or {}).get('dataset') or {}).get('workspace_root') or '.dataviz_cache'
    p = resolve(str(ws)) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------------------------- paths
def clean_rel(path):
    """A bucket path in its stored form: POSIX, no leading slash, '' for the root. DCError 400 for '..',
    absolute paths and drive letters (never silently stripped: the caller meant something else)."""
    raw = str(path or '').replace('\\', '/').strip()
    if raw.startswith('/') or (len(raw) > 1 and raw[1] == ':'):
        raise DCError('paths are relative to the bucket (no leading / or drive letter)')
    parts = [p for p in raw.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise DCError("'..' is not allowed in a bucket path")
    return '/'.join(parts)


def join(rel, name):
    return f'{rel}/{name}' if rel else name


def resolve(root, rel, allowed, must_exist=False):
    """Absolute path of `rel` inside the local bucket `root`: DCError 400 when it (symlinks resolved) leaves the
    bucket or the allowed roots, 404 when must_exist and there is nothing there. The returned path is the lexical
    one, so an op on a symlink acts on the link, never on what it points at."""
    rel = clean_rel(rel)
    base = os.path.realpath(root)
    lex = os.path.join(base, rel) if rel else base
    real = os.path.realpath(lex) if rel else base
    if real != base and not real.startswith(base.rstrip(os.sep) + os.sep):
        raise DCError('that path leaves the bucket')
    try:
        connections.check_local(real, allowed)
    except ValueError as e:
        raise DCError(str(e)) from None
    if must_exist and not os.path.lexists(lex):
        raise DCError(f'nothing at {rel or "/"}', 404)
    return lex


def kind_of(name, rel=''):
    """The Entry kind of a file name; a .txt under a labels/ folder is a 'label'."""
    low = name.lower()
    if low.endswith(IMAGE_EXTS):
        return 'image'
    if low.endswith('.txt') and low != 'classes.txt' and 'labels' in rel.lower().split('/'):
        return 'label'
    for k, exts in KIND_EXTS.items():
        if low.endswith(exts):
            return k
    return 'other'


def is_image(name):
    return name.lower().endswith(IMAGE_EXTS)


def label_dir_for(rel):
    """The YOLO label folder mirroring an image folder: the last 'images' part replaced by 'labels'."""
    parts = rel.split('/') if rel else []
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == 'images':
            return '/'.join(parts[:i] + ['labels'] + parts[i + 1:])
    return None


# ---------------------------------------------------------------------------------------------- listing
def _sort_key(sort):
    desc = sort.startswith('-')
    field = sort.lstrip('-') or 'name'
    if field not in ('name', 'mtime', 'size'):
        raise DCError('sort must be name, -mtime or size')
    return field, desc


def _count_items(path):
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if not e.name.startswith('.'):
                    n += 1
                    if n >= ITEMS_CAP:
                        break
    except OSError:
        return None
    return n


def _count_lines(path):
    try:
        with open(path, 'rb') as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return None


def list_local(root, rel, allowed, offset=0, limit=500, sort='name', q=''):
    """{path, total, entries} of one folder. Only the returned page is stat'ed when sorting by name; dirs first."""
    absdir = resolve(root, rel, allowed, must_exist=True)
    rel = clean_rel(rel)
    if not os.path.isdir(absdir):
        raise DCError(f'{rel or "/"} is not a folder')
    field, desc = _sort_key(sort or 'name')
    qq = (q or '').casefold()
    rows = []                                           # (name, is_dir, DirEntry)
    with os.scandir(absdir) as it:
        for e in it:
            if e.name.startswith('.') or (qq and qq not in e.name.casefold()):
                continue
            try:
                d = e.is_dir()
            except OSError:
                d = False
            rows.append((e.name, d, e))
    if field == 'name':
        rows.sort(key=lambda r: r[0].casefold(), reverse=desc)
    else:
        def k(r):
            try:
                st = r[2].stat()
            except OSError:
                return 0
            return st.st_mtime if field == 'mtime' else (0 if r[1] else st.st_size)
        rows.sort(key=k, reverse=desc)
    rows.sort(key=lambda r: not r[1])                   # stable: dirs first, each group keeps its order
    offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or 500), 5000))
    page = rows[offset:offset + limit]
    lab_rel = label_dir_for(rel)
    labels = None
    if lab_rel is not None and any(not d and is_image(n) for n, d, _ in page):
        lab_abs = os.path.join(os.path.realpath(root), lab_rel)
        try:
            with os.scandir(lab_abs) as it:
                labels = {e.name[:-4]: e.path for e in it if e.name.endswith('.txt')}
        except OSError:
            labels = None
    entries = []
    for name, d, e in page:
        try:
            st = e.stat()
            size, mtime = (0 if d else st.st_size), st.st_mtime
        except OSError:
            size, mtime = 0, None
        out = {'name': name, 'path': join(rel, name), 'type': 'dir' if d else 'file', 'size': size, 'mtime': mtime}
        if d:
            out['items'] = _count_items(e.path)
        else:
            out['kind'] = kind_of(name, rel)
            if labels is not None and out['kind'] == 'image':
                lp = labels.get(os.path.splitext(name)[0])
                if lp is not None:
                    out['labels'] = _count_lines(lp)
        entries.append(out)
    return {'path': rel, 'total': len(rows), 'entries': entries}


def walk_local(root, rel, allowed, cap=300_000):
    """(rel path, is_dir, abs path) of everything below `rel` (hidden skipped, symlinked dirs not followed)."""
    base = resolve(root, rel, allowed, must_exist=True)
    stack, n = [(clean_rel(rel), base)], 0
    while stack:
        r, d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name.startswith('.'):
                    continue
                n += 1
                if n > cap:
                    return
                sub = join(r, e.name)
                isdir = e.is_dir(follow_symlinks=False)
                yield sub, isdir, e.path
                if isdir:
                    stack.append((sub, e.path))


# ---------------------------------------------------------------------------------------------- file ops
def _unique(dest):
    """`dest`, or `name (copy)`, `name (copy 2)` … next to it: the first free name (copying into the same folder)."""
    if not os.path.lexists(dest):
        return dest
    d, name = os.path.split(dest)
    stem, ext = os.path.splitext(name) if not os.path.isdir(dest) else (name, '')
    for i in range(1, 1000):
        cand = os.path.join(d, f'{stem} (copy{"" if i == 1 else f" {i}"}){ext}')
        if not os.path.lexists(cand):
            return cand
    raise DCError('too many copies with that name')


def _rel_of(base, absp):
    return os.path.relpath(absp, base).replace(os.sep, '/') if absp != base else ''


def apply_op(root, allowed, op, trash_dir, n):
    """Run one op on a local bucket -> (applied info, undo step). DCError for anything the user must fix."""
    base = os.path.realpath(root)
    kind = op.get('op')
    if kind == 'mkdir':
        p = resolve(root, op.get('path'), allowed)
        if p == base:
            raise DCError('give the new folder a name')
        if os.path.lexists(p):
            raise DCError(f'{clean_rel(op["path"])} already exists', 409)
        os.makedirs(p)
        rel = _rel_of(base, p)
        return {'op': 'mkdir', 'path': rel}, {'undo': 'rmdir', 'path': rel}
    src = resolve(root, op.get('path'), allowed, must_exist=True)
    if src == base:
        raise DCError('the bucket root cannot be moved, renamed or deleted')
    rel = _rel_of(base, src)
    if kind in ('move', 'copy'):
        to = resolve(root, op.get('to') or '', allowed)
        if not os.path.isdir(to):
            raise DCError(f'{clean_rel(op.get("to")) or "/"} is not a folder', 404 if not os.path.lexists(to) else 400)
        dest = os.path.join(to, os.path.basename(src))
        if os.path.isdir(src) and (to == src or to.startswith(src + os.sep)):
            raise DCError(f'cannot {kind} a folder into itself')
        if kind == 'move':
            if os.path.dirname(src) == to:
                raise DCError(f'{rel} is already in that folder')
            if os.path.lexists(dest):
                raise DCError(f'{_rel_of(base, dest)} already exists', 409)
            shutil.move(src, dest)
            new = _rel_of(base, dest)
            return {'op': 'move', 'path': rel, 'to': new}, {'undo': 'move', 'from': new, 'to': rel}
        dest = _unique(dest)
        if os.path.isdir(src):
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)
        new = _rel_of(base, dest)
        return {'op': 'copy', 'path': rel, 'to': new}, {'undo': 'remove', 'path': new}
    if kind == 'rename':
        name = str(op.get('name') or '').strip()
        if not name or '/' in name or '\\' in name or name in ('.', '..') or name.startswith('.'):
            raise DCError('a new name is one file or folder name (no /, not starting with .)')
        dest = os.path.join(os.path.dirname(src), name)
        if os.path.lexists(dest) and os.path.normcase(dest) != os.path.normcase(src):
            raise DCError(f'{_rel_of(base, dest)} already exists', 409)
        os.rename(src, dest)
        new = _rel_of(base, dest)
        return {'op': 'rename', 'path': rel, 'to': new}, {'undo': 'move', 'from': new, 'to': rel}
    if kind == 'delete':
        slot = os.path.join(trash_dir, str(n))
        os.makedirs(slot, exist_ok=True)
        dest = os.path.join(slot, os.path.basename(src))
        shutil.move(src, dest)
        return {'op': 'delete', 'path': rel}, {'undo': 'restore', 'trash': dest, 'path': rel}
    raise DCError(f'unknown op {kind!r} (mkdir, move, copy, rename, delete)')


def undo_step(root, allowed, step, trash_root):
    """Reverse one recorded step; DCError when the file system no longer allows it (something else changed)."""
    base = os.path.realpath(root)
    u = step.get('undo')
    if u == 'rmdir':
        p = resolve(root, step['path'], allowed)
        if os.path.isdir(p):
            if any(True for _ in os.scandir(p)):
                raise DCError(f'{step["path"]} is not empty any more')
            os.rmdir(p)
        return step['path']
    if u == 'move':
        src = resolve(root, step['from'], allowed, must_exist=True)
        dest = resolve(root, step['to'], allowed)
        if os.path.lexists(dest):
            raise DCError(f'{step["to"]} exists again', 409)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        return step['to']
    if u == 'remove':
        p = resolve(root, step['path'], allowed)
        if p != base and os.path.lexists(p):
            shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
        return step['path']
    if u == 'restore':
        t = os.path.realpath(step['trash'])
        if not t.startswith(os.path.realpath(trash_root) + os.sep) or not os.path.lexists(t):
            raise DCError(f'{step["path"]} is no longer in the trash', 410)
        dest = resolve(root, step['path'], allowed)
        if os.path.lexists(dest):
            raise DCError(f'{step["path"]} exists again', 409)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(t, dest)
        return step['path']
    raise DCError(f'unknown undo step {u!r}')


def purge_trash(trash_root, keep_ids, max_age=7 * 86400):
    """Drop trash folders older than max_age that no undo row points at any more (best effort)."""
    try:
        now = time.time()
        with os.scandir(trash_root) as it:
            for e in it:
                if e.name not in keep_ids and now - e.stat().st_mtime > max_age:
                    shutil.rmtree(e.path, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------- uploads
def _write_stream(fileobj, dest):
    tmp = f'{dest}.part-{uuid.uuid4().hex[:8]}'
    try:
        with open(tmp, 'wb') as f:
            shutil.copyfileobj(fileobj, f, CHUNK)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _zip_member_ok(info):
    name = info.filename.replace('\\', '/')
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if info.is_dir() or not parts or name.startswith('/') or (len(name) > 1 and name[1] == ':'):
        return None
    if any(p == '..' or p.startswith('.') or p == '__MACOSX' for p in parts):
        return None
    if statmod.S_ISLNK(info.external_attr >> 16):       # a stored symlink could point anywhere
        return None
    return '/'.join(parts)


def unzip_into(zpath, folder, root, allowed):
    """Unpack a zip into `folder` (zip-slip safe: every member re-resolved inside the bucket) -> (added, skipped)."""
    added = skipped = 0
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist()
        if len(infos) > ZIP_MAX_FILES:
            raise DCError(f'the zip has {len(infos)} entries: at most {ZIP_MAX_FILES}')
        need = sum(i.file_size for i in infos)
        free = shutil.disk_usage(folder).free
        if need > free:
            raise DCError(f'the zip unpacks to {need / 1e9:.1f} GB but only {free / 1e9:.1f} GB are free', 507)
        frel = _rel_of(os.path.realpath(root), folder)
        for info in infos:
            rel = _zip_member_ok(info)
            if rel is None:
                continue
            dest = resolve(root, join(frel, rel), allowed)
            if os.path.lexists(dest):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(info) as src:
                _write_stream(src, dest)
            added += 1
    return added, skipped


def upload_local(root, allowed, rel, files, unzip=True):
    """Stream uploaded files into folder `rel` (their names may hold sub-folders) -> {added, skipped, files}."""
    folder = resolve(root, rel, allowed)
    os.makedirs(folder, exist_ok=True)
    if not os.path.isdir(folder):
        raise DCError(f'{clean_rel(rel) or "/"} is not a folder')
    frel = _rel_of(os.path.realpath(root), folder)
    added = skipped = 0
    names = []
    for name, fobj in files:
        sub = clean_rel(name)
        if not sub or any(p.startswith('.') for p in sub.split('/')):
            skipped += 1
            continue
        dest = resolve(root, join(frel, sub), allowed)
        if unzip and sub.lower().endswith('.zip'):
            tmp = os.path.join(folder, f'.upload-{uuid.uuid4().hex}.zip')
            try:
                _write_stream(fobj, tmp)
                try:
                    a, s = unzip_into(tmp, os.path.dirname(dest), root, allowed)
                except zipfile.BadZipFile:
                    raise DCError(f'{sub} is not a valid zip file') from None
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            added, skipped = added + a, skipped + s
            names.append(sub)
            continue
        if os.path.lexists(dest):
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _write_stream(fobj, dest)
        added += 1
        names.append(sub)
    return {'added': added, 'skipped': skipped, 'files': names[:200]}


# ---------------------------------------------------------------------------------------------- thumbnails
def thumbnail(data_or_path, key, w, cfg):
    """A JPEG at most w px on its long side, cached on disk under the workspace root (key = path + mtime + size)."""
    from PIL import Image, ImageOps
    w = max(16, min(int(w), 1024))
    h = hashlib.sha1(f'{key}|{w}'.encode()).hexdigest()
    p = cache_dir(cfg) / h[:2] / f'{h}.jpg'
    try:
        return p.read_bytes()
    except OSError:
        pass
    src = data_or_path if isinstance(data_or_path, str) else io.BytesIO(data_or_path)
    try:
        with Image.open(src) as im:
            im.draft('RGB', (w, w))                     # JPEG: decode at a reduced scale
            im = ImageOps.exif_transpose(im)
            im.thumbnail((w, w))
            if im.mode not in ('RGB', 'L'):
                im = im.convert('RGB')
            out = io.BytesIO()
            im.save(out, 'JPEG', quality=82)
    except Exception as e:  # noqa: BLE001 - any undecodable file is the same answer
        raise DCError(f'cannot make a thumbnail: {e}', 415) from None
    data = out.getvalue()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f'.{uuid.uuid4().hex[:6]}.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except OSError:
        pass
    return data


# ---------------------------------------------------------------------------------------------- S3 (read-only)
class S3View:
    """A linked S3 bucket + prefix, read through the owner's connection (sources.make_client). Read-only."""

    def __init__(self, conn, bucket, prefix='', s3cfg=None):
        from ..data import sources
        self.client = sources.make_client(conn, s3cfg)
        self.bucket = bucket
        self.base = sources._norm_prefix(prefix)

    def key(self, rel):
        return self.base + clean_rel(rel)

    def list(self, rel, offset=0, limit=500, sort='name', q=''):
        rel = clean_rel(rel)
        pre = self.base + (rel + '/' if rel else '')
        field, desc = _sort_key(sort or 'name')
        qq = (q or '').casefold()
        dirs, files = [], []
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=pre,
                                                                           Delimiter='/'):
            for p in page.get('CommonPrefixes', []):
                n = p['Prefix'][len(pre):].rstrip('/')
                if n and not n.startswith('.') and (not qq or qq in n.casefold()):
                    dirs.append({'name': n, 'path': join(rel, n), 'type': 'dir', 'size': 0, 'mtime': None})
            for o in page.get('Contents', []):
                n = o['Key'][len(pre):]
                if not n or '/' in n or n.startswith('.') or (qq and qq not in n.casefold()):
                    continue
                lm = o.get('LastModified')
                files.append({'name': n, 'path': join(rel, n), 'type': 'file', 'size': int(o.get('Size') or 0),
                              'mtime': lm.timestamp() if lm is not None else None, 'kind': kind_of(n, rel)})
        keyf = {'name': lambda e: e['name'].casefold(), 'mtime': lambda e: e['mtime'] or 0,
                'size': lambda e: e['size']}[field]
        rows = sorted(dirs, key=keyf, reverse=desc) + sorted(files, key=keyf, reverse=desc)
        offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or 500), 5000))
        return {'path': rel, 'total': len(rows), 'entries': rows[offset:offset + limit]}

    def keys(self, rel='', cap=100_000):
        """{rel path below rel: size} of every object (capped)."""
        rel = clean_rel(rel)
        pre = self.base + (rel + '/' if rel else '')
        out = {}
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=pre):
            for o in page.get('Contents', []):
                k = o['Key'][len(pre):]
                if k and not any(p.startswith('.') for p in k.split('/')):
                    out[k] = int(o.get('Size') or 0)
                    if len(out) >= cap:
                        return out
        return out

    def read(self, rel, limit=None):
        kw = {'Range': f'bytes=0-{int(limit) - 1}'} if limit else {}
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.key(rel), **kw)
        except Exception as e:  # noqa: BLE001 - botocore's NoSuchKey / ClientError
            raise DCError(f'cannot read {rel}: {e}', 404) from None
        return obj['Body'].read(), obj.get('ETag', ''), int(obj.get('ContentLength') or 0)

    def size(self, rel):
        try:
            return int(self.client.head_object(Bucket=self.bucket, Key=self.key(rel)).get('ContentLength') or 0)
        except Exception:  # noqa: BLE001
            return None

    def close(self):
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def posix_parent(rel):
    p = str(PurePosixPath(rel).parent)
    return '' if p == '.' else p
