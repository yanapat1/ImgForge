"""
The Bucket page's filesystem layer: where buckets live, safe path joins, paged folder listings, file operations
with an undo manifest, streamed uploads / safe unzip, thumbnails, and the read-only S3 view. No database and no
users here: bucket/service.py decides who may touch which bucket, this module only keeps every path inside
its bucket.

Why the care with paths: a bucket path comes from the page or from an LLM tool call, so `..`, absolute paths,
drive letters and symlinks pointing out of the bucket are all refused (BucketError 400) before anything is opened,
and every resolved path is also checked against the allowed roots (connections.check_local).

Listing is os.scandir based: names and the dir/file bit come from the dirent, so only the PAGE that is returned
is stat'ed (a 20k-file folder sorted by name costs one scandir + 500 stats).
"""

import hashlib
import io
import json
import os
import re
import gzip
import shutil
import stat as statmod
import tarfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from ..image import connections
from . import s3list

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tif', '.tiff', '.gif')
KIND_EXTS = {
    'yaml': ('.yaml', '.yml'), 'json': ('.json',), 'xml': ('.xml',), 'npy': ('.npy', '.npz'),
    'archive': ('.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar'),
    'table': ('.parquet', '.feather', '.arrow'),
    'audio': ('.wav', '.mp3', '.flac', '.ogg', '.oga', '.m4a', '.aac', '.opus', '.wma'),
    'video': ('.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv', '.mpg', '.mpeg'),
    'code': ('.js', '.ts', '.jsx', '.tsx', '.mjs', '.cjs', '.py', '.sh', '.bash', '.zsh', '.html', '.htm', '.css',
             '.scss', '.toml', '.ini', '.cfg', '.conf', '.properties', '.env', '.sql', '.dockerfile', '.r', '.go',
             '.rs', '.java', '.c', '.cpp', '.cc', '.hpp', '.h'),
    'text': ('.txt', '.csv', '.tsv', '.md', '.markdown', '.log', '.names', '.rst'),
}
CODE_NAMES = ('dockerfile', 'makefile', 'containerfile', 'gemfile', 'procfile')
# dot files that are shown (and editable) like any file; every other dot name (the trash, .s3, upload temps,
# dot folders such as .git or .cache) stays hidden everywhere
DOT_FILES = ('.env', '.gitignore', '.gitattributes', '.editorconfig', '.dockerignore', '.npmrc')
DOT_PREFIXES = ('.env.', '.prettierrc', '.eslintrc')
_PARTIAL = re.compile(r'\.part(-[0-9a-f]{8})?$')     # a write in flight (_write_stream, S3 pull)
SPLITS = ('train', 'val', 'valid', 'validation', 'test')
TRASH = '.trash'
ITEMS_CAP = 10000                   # a folder's "items" badge stops counting here
ZIP_MAX_FILES = 500_000
CHUNK = 1 << 20
BIG = 64 << 20                      # an S3 object this big is pulled in parallel ranges


class Cancelled(Exception):
    """The job was cancelled (a download or an unpack): the caller cleans up."""


class BucketError(Exception):
    """A Bucket failure with the HTTP status it maps to; the message is shown to the user / the model."""

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
    if str(p) not in _warned_roots:
        _warned_roots.add(str(p))
        if not on_mount(p):
            from ..core import logging as vlog
            vlog.get_logger('bucket').warning(
                f'Bucket root {p} is inside the container, not on a volume: buckets, S3 local copies and '
                'imports are lost when the container is recreated (up --build). Mount it (see docker-compose.yml).')
    return p


_warned_roots = set()


def on_mount(p):
    """False only inside Docker when no mount point lies between `p` and the container's / (the writable layer)."""
    if not os.path.exists('/.dockerenv'):
        return True
    q = Path(p)
    while q != q.parent:
        if os.path.ismount(q):
            return True
        q = q.parent
    return False


def roots(cfg):
    """Where a resolved local path may lie: storage.allowed_roots plus the managed data root (always allowed)."""
    return [*connections.allowed_roots(cfg), data_root(cfg)]


_moved_caches = set()


def cache_dir(cfg, name='bucket_thumbs'):
    from ..core.config import resolve
    ws = ((cfg or {}).get('dataset') or {}).get('workspace_root') or '.dataviz_cache'
    p = resolve(str(ws)) / name
    if name == 'bucket_thumbs' and str(p) not in _moved_caches:     # once per process: the cache's old name
        _moved_caches.add(str(p))
        old = p.parent / 'dc_thumbs'
        if old.is_dir() and not p.exists():
            try:
                old.rename(p)
            except OSError:
                pass
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------------------------- paths
def clean_rel(path):
    """A bucket path in its stored form: POSIX, no leading slash, '' for the root. BucketError 400 for '..',
    absolute paths and drive letters (never silently stripped: the caller meant something else)."""
    raw = str(path or '').replace('\\', '/').strip()
    if raw.startswith('/') or (len(raw) > 1 and raw[1] == ':'):
        raise BucketError('paths are relative to the bucket (no leading / or drive letter)')
    parts = [p for p in raw.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise BucketError("'..' is not allowed in a bucket path")
    return '/'.join(parts)


def join(rel, name):
    return f'{rel}/{name}' if rel else name


def resolve(root, rel, allowed, must_exist=False):
    """Absolute path of `rel` inside the local bucket `root`: BucketError 400 when it (symlinks resolved) leaves the
    bucket or the allowed roots, 404 when must_exist and there is nothing there. The returned path is the lexical
    one, so an op on a symlink acts on the link, never on what it points at."""
    rel = clean_rel(rel)
    base = os.path.realpath(root)
    lex = os.path.join(base, rel) if rel else base
    real = os.path.realpath(lex) if rel else base
    if real != base and not real.startswith(base.rstrip(os.sep) + os.sep):
        raise BucketError('that path leaves the bucket')
    try:
        connections.check_local(real, allowed)
    except ValueError as e:
        raise BucketError(str(e)) from None
    if must_exist and not os.path.lexists(lex):
        raise BucketError(f'nothing at {rel or "/"}', 404)
    return lex


def visible(name, is_dir=False):
    """Whether a folder entry is listed: dot names are hidden except the allowlisted config files (DOT_FILES,
    .env.*, .prettierrc*, .eslintrc*); dot folders and half-written files never are."""
    if _PARTIAL.search(name):
        return False
    if not name.startswith('.'):
        return True
    if is_dir:
        return False
    return name in DOT_FILES or name.startswith(DOT_PREFIXES)


def is_secret(name):
    """A .env file: it holds secrets, so reading it needs bucket.manage."""
    low = os.path.basename(str(name or '')).lower()
    return low == '.env' or low.startswith('.env.') or low.endswith('.env')


def kind_of(name, rel=''):
    """The Entry kind of a file name; a .txt under a labels/ folder is a 'label'."""
    low = name.lower()
    if low.endswith(IMAGE_EXTS):
        return 'image'
    if low in CODE_NAMES or low.startswith('.env'):
        return 'code'
    if low.startswith('.'):
        return 'text' if low in DOT_FILES or low.startswith(DOT_PREFIXES) else 'other'
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
        raise BucketError('sort must be name, -mtime or size')
    return field, desc


def _count_items(path):
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if visible(e.name, e.is_dir()):
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
        raise BucketError(f'{rel or "/"} is not a folder')
    field, desc = _sort_key(sort or 'name')
    qq = (q or '').casefold()
    rows = []                                           # (name, is_dir, DirEntry)
    with os.scandir(absdir) as it:
        for e in it:
            if qq and qq not in e.name.casefold():
                continue
            try:
                d = e.is_dir()
            except OSError:
                d = False
            if not visible(e.name, d):
                continue
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
                isdir = e.is_dir(follow_symlinks=False)
                if not visible(e.name, isdir):
                    continue
                n += 1
                if n > cap:
                    return
                sub = join(r, e.name)
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
    raise BucketError('too many copies with that name')


def _rel_of(base, absp):
    return os.path.relpath(absp, base).replace(os.sep, '/') if absp != base else ''


def apply_op(root, allowed, op, trash_dir, n):
    """Run one op on a local bucket -> (applied info, undo step). BucketError for anything the user must fix."""
    base = os.path.realpath(root)
    kind = op.get('op')
    if kind == 'mkdir':
        p = resolve(root, op.get('path'), allowed)
        if p == base:
            raise BucketError('give the new folder a name')
        if os.path.lexists(p):
            raise BucketError(f'{clean_rel(op["path"])} already exists', 409)
        os.makedirs(p)
        rel = _rel_of(base, p)
        return {'op': 'mkdir', 'path': rel}, {'undo': 'rmdir', 'path': rel}
    if kind == 'create':
        p = resolve(root, op.get('path'), allowed)
        name = os.path.basename(p)
        if p == base or not name:
            raise BucketError('give the new file a name')
        if not visible(name):
            raise BucketError(f'{name} would be hidden: a name not starting with . (except .env, .gitignore and similar)')
        if os.path.lexists(p):
            raise BucketError(f'{clean_rel(op["path"])} already exists', 409)
        if not os.path.isdir(os.path.dirname(p)):
            raise BucketError(f'{posix_parent(clean_rel(op["path"])) or "/"} is not a folder', 404)
        with open(p, 'x', encoding='utf-8') as f:
            f.write(str(op.get('text') or ''))
        rel = _rel_of(base, p)
        return {'op': 'create', 'path': rel}, {'undo': 'remove', 'path': rel}
    src = resolve(root, op.get('path'), allowed, must_exist=True)
    if src == base:
        raise BucketError('the bucket root cannot be moved, renamed or deleted')
    rel = _rel_of(base, src)
    if kind in ('move', 'copy'):
        to = resolve(root, op.get('to') or '', allowed)
        if not os.path.isdir(to):
            raise BucketError(f'{clean_rel(op.get("to")) or "/"} is not a folder', 404 if not os.path.lexists(to) else 400)
        dest = os.path.join(to, os.path.basename(src))
        if os.path.isdir(src) and (to == src or to.startswith(src + os.sep)):
            raise BucketError(f'cannot {kind} a folder into itself')
        if kind == 'move':
            if os.path.dirname(src) == to:
                raise BucketError(f'{rel} is already in that folder')
            if os.path.lexists(dest):
                raise BucketError(f'{_rel_of(base, dest)} already exists', 409)
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
        if not name or '/' in name or '\\' in name or name in ('.', '..') \
                or not visible(name, os.path.isdir(src) and not os.path.islink(src)):
            raise BucketError('a new name is one file or folder name (no /, not starting with . except .env, '
                          '.gitignore and similar config files)')
        dest = os.path.join(os.path.dirname(src), name)
        if os.path.lexists(dest) and os.path.normcase(dest) != os.path.normcase(src):
            raise BucketError(f'{_rel_of(base, dest)} already exists', 409)
        os.rename(src, dest)
        new = _rel_of(base, dest)
        return {'op': 'rename', 'path': rel, 'to': new}, {'undo': 'move', 'from': new, 'to': rel}
    if kind == 'delete':
        slot = os.path.join(trash_dir, str(n))
        os.makedirs(slot, exist_ok=True)
        dest = os.path.join(slot, os.path.basename(src))
        shutil.move(src, dest)
        return {'op': 'delete', 'path': rel}, {'undo': 'restore', 'trash': dest, 'path': rel}
    raise BucketError(f'unknown op {kind!r} (mkdir, create, move, copy, rename, delete)')


def undo_step(root, allowed, step, trash_root):
    """Reverse one recorded step; BucketError when the file system no longer allows it (something else changed)."""
    base = os.path.realpath(root)
    u = step.get('undo')
    if u == 'rmdir':
        p = resolve(root, step['path'], allowed)
        if os.path.isdir(p):
            if any(True for _ in os.scandir(p)):
                raise BucketError(f'{step["path"]} is not empty any more')
            os.rmdir(p)
        return step['path']
    if u == 'move':
        src = resolve(root, step['from'], allowed, must_exist=True)
        dest = resolve(root, step['to'], allowed)
        if os.path.lexists(dest):
            raise BucketError(f'{step["to"]} exists again', 409)
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
            raise BucketError(f'{step["path"]} is no longer in the trash', 410)
        dest = resolve(root, step['path'], allowed)
        if os.path.lexists(dest):
            raise BucketError(f'{step["path"]} exists again', 409)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(t, dest)
        return step['path']
    if u == 'replace':                                  # a text save: the old content comes back over the new
        t = os.path.realpath(step['trash'])
        if not t.startswith(os.path.realpath(trash_root) + os.sep) or not os.path.lexists(t):
            raise BucketError(f'the previous version of {step["path"]} is no longer in the trash', 410)
        dest = resolve(root, step['path'], allowed)
        if os.path.isdir(dest):
            raise BucketError(f'{step["path"]} is a folder now', 409)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.replace(t, dest)
        except OSError:                                 # the trash is on another device than a linked folder
            with open(t, 'rb') as f:
                _write_stream(f, dest)
            shutil.copystat(t, dest)
            os.remove(t)
        return step['path']
    raise BucketError(f'unknown undo step {u!r}')


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
def _write_stream(fileobj, dest, on_bytes=None):
    """Copy `fileobj` into `dest` through a temp file (no half file on a failure); on_bytes(n) after each chunk
    (it may raise Cancelled to stop: the temp file is removed)."""
    tmp = f'{dest}.part-{uuid.uuid4().hex[:8]}'
    try:
        with open(tmp, 'wb') as f:
            if on_bytes is None:
                shutil.copyfileobj(fileobj, f, CHUNK)
            else:
                while b := fileobj.read(CHUNK):
                    f.write(b)
                    on_bytes(len(b))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _member_rel(name):
    """The safe relative path of an archive member, or None (absolute, .., dot names, __MACOSX)."""
    name = str(name).replace('\\', '/')
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if not parts or name.startswith('/') or (len(name) > 1 and name[1] == ':'):
        return None
    if any(p == '..' or p.startswith('.') or p == '__MACOSX' for p in parts):
        return None
    return '/'.join(parts)


def _zip_member_ok(info):
    if info.is_dir() or statmod.S_ISLNK(info.external_attr >> 16):   # a stored symlink could point anywhere
        return None
    return _member_rel(info.filename)


ARCHIVE_EXTS = ('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.gz', '.7z')
# unpacked on upload: a bare .gz (data.csv.gz) is kept as the file it is
UPLOAD_ARCHIVES = tuple(e for e in ARCHIVE_EXTS if e != '.gz')


def archive_kind(path):
    """'zip' | 'tar' | 'gz' (one compressed file) | '7z' | None, from the bytes first, then the name."""
    low = str(path).lower()
    try:
        if zipfile.is_zipfile(path):
            return 'zip'
        if tarfile.is_tarfile(path):
            return 'tar'
    except OSError:
        return None
    if low.endswith('.gz'):
        return 'gz'
    if low.endswith('.7z'):
        return '7z'
    return None


LAYOUT_DIRS = {'images', 'labels', 'annotations', 'jpegimages', 'train', 'val', 'valid', 'validation', 'test'}


def _strip_wrapper(members, folder, own=None):
    """Most dataset archives wrap everything in one folder (traffic/images/..., traffic/data.yaml): unpacked into an
    empty folder that wrapper would hide the layout from format detection, so its contents land there instead."""
    tops = {rel.split('/', 1)[0] for _, rel, _ in members}
    if len(tops) == 1 and next(iter(tops)).lower() in LAYOUT_DIRS and next(iter(tops)) != own:
        return members                                  # images/, labels/, train/ ...: part of the layout, kept
    if len(tops) == 1 and members and all('/' in rel for _, rel, _ in members) \
            and not any(not n.startswith('.') for n in os.listdir(folder)):
        cut = len(next(iter(tops))) + 1
        return [(k, rel[cut:], n) for k, rel, n in members]
    return members


def extract_into(path, folder, root, allowed, progress=None, cancelled=None, workers=4, overwrite=False,
                 nested=False, own=None):
    """Unpack the archive `path` (zip, tar / tar.gz / tar.bz2 / tar.xz, a single .gz, .7z with py7zr) into `folder`
    of bucket `root`. Every member is re-resolved inside the bucket (no slip), only regular files are written (tar
    links and devices are skipped), at most ZIP_MAX_FILES entries, the unpacked size must fit on the disk. Zip members
    unpack in `workers` threads (each with its own handle: zlib releases the GIL). progress(done, total) every 256
    files; cancelled() -> True stops it (Cancelled). nested: archives found inside are unpacked once more, each into
    a folder named after it (its own wrapper folder of that name dropped), and removed. own: that name.
    -> {added, skipped, replaced, nested}"""
    kind = archive_kind(path)
    if kind is None:
        raise BucketError(f'{os.path.basename(path)} is not an archive (zip, tar, tar.gz, tar.bz2, tar.xz, gz, 7z)')
    frel = _rel_of(os.path.realpath(root), folder)
    out = {'added': 0, 'skipped': 0, 'replaced': 0, 'nested': 0}
    lock = threading.Lock()
    written = []

    def check_cancel():
        if cancelled is not None and cancelled():
            raise Cancelled()

    def target(rel):
        dest = resolve(root, join(frel, rel), allowed)
        if os.path.lexists(dest):
            if not overwrite or os.path.isdir(dest):
                with lock:
                    out['skipped'] += 1
                return None
            with lock:
                out['replaced'] += 1
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return dest

    def done(dest, total):
        with lock:
            out['added'] += 1
            written.append(dest)
            n = out['added'] + out['skipped']
        if progress and n % 256 == 0:
            progress(n, total)

    def precheck(n, size):
        if n > ZIP_MAX_FILES:
            raise BucketError(f'the archive has {n} entries: at most {ZIP_MAX_FILES}')
        free = shutil.disk_usage(folder).free
        if size > free:
            raise BucketError(f'the archive unpacks to {size / 1e9:.1f} GB but only {free / 1e9:.1f} GB are free', 507)

    if kind == 'zip':
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            precheck(len(infos), sum(i.file_size for i in infos))
            members = _strip_wrapper([(i, rel, i.file_size) for i in infos if (rel := _zip_member_ok(i)) is not None],
                                     folder, own)
        total = len(members)

        def run(chunk):
            with zipfile.ZipFile(path) as zz:
                for info, rel, _ in chunk:
                    check_cancel()
                    dest = target(rel)
                    if dest is None:
                        continue
                    with zz.open(info.filename) as src:
                        _write_stream(src, dest)
                    done(dest, total)
        n = max(1, min(workers, len(members) // 64 or 1))
        chunks = [members[i::n] for i in range(n)]
        if n == 1:
            run(chunks[0])
        else:
            with ThreadPoolExecutor(n) as ex:
                for f in [ex.submit(run, c) for c in chunks]:
                    f.result()
    elif kind == 'tar':
        with tarfile.open(path) as t:
            infos = t.getmembers()
            precheck(len(infos), sum(i.size for i in infos if i.isreg()))
            members = _strip_wrapper([(i, rel, i.size) for i in infos
                                      if i.isreg() and (rel := _member_rel(i.name)) is not None], folder, own)
            total = len(members)
            for info, rel, _ in members:
                check_cancel()
                dest = target(rel)
                if dest is None:
                    continue
                src = t.extractfile(info)
                if src is None:
                    continue
                with src:
                    _write_stream(src, dest)
                done(dest, total)
    elif kind == 'gz':
        name = os.path.basename(path)[:-3] or 'file'
        rel = _member_rel(name)
        dest = target(rel) if rel else None
        if dest is not None:
            check_cancel()
            with gzip.open(path, 'rb') as src:
                _write_stream(src, dest)
            done(dest, 1)
    else:
        try:
            import py7zr
        except ImportError:
            raise BucketError('.7z archives need py7zr on the server (pip install py7zr): use zip or tar.gz') from None
        with py7zr.SevenZipFile(path) as z:
            infos = [i for i in z.list() if not i.is_directory]
            precheck(len(infos), sum(int(i.uncompressed or 0) for i in infos))
            members = _strip_wrapper([(i.filename, rel, 0) for i in infos if (rel := _member_rel(i.filename))], folder, own)
            names = {k: rel for k, rel, _ in members}
            for k, data in (z.read(list(names)) or {}).items():
                check_cancel()
                dest = target(names[k])
                if dest is not None:
                    _write_stream(data, dest)
                    done(dest, len(names))
    if progress:
        progress(out['added'] + out['skipped'], out['added'] + out['skipped'])
    if nested:
        for p in [w for w in written if w.lower().endswith(ARCHIVE_EXTS) and archive_kind(w)]:
            check_cancel()
            base = os.path.basename(p)
            stem = re.sub(r'(\.tar)?\.[A-Za-z0-9]{1,4}$', '', base) or base
            sub = os.path.join(os.path.dirname(p), stem)
            if os.path.lexists(sub) and not os.path.isdir(sub):
                continue
            os.makedirs(sub, exist_ok=True)
            r = extract_into(p, sub, root, allowed, None, cancelled, workers, overwrite, nested=False, own=stem)
            os.remove(p)
            out['nested'] += 1
            out['added'] += r['added'] - 1
            out['skipped'] += r['skipped']
    return out


def unzip_into(zpath, folder, root, allowed):
    """Unpack an archive into `folder` (the upload path) -> (added, skipped)."""
    r = extract_into(zpath, folder, root, allowed)
    return r['added'], r['skipped']


def _upload_folder(root, allowed, rel):
    """(absolute folder, its path below the bucket) of an upload into `rel`, created when missing."""
    folder = resolve(root, rel, allowed)
    os.makedirs(folder, exist_ok=True)
    if not os.path.isdir(folder):
        raise BucketError(f'{clean_rel(rel) or "/"} is not a folder')
    return folder, _rel_of(os.path.realpath(root), folder)


def _upload_name(name):
    """The uploaded name below the folder ('' when it is skipped: empty or a dot part anywhere)."""
    sub = clean_rel(name)
    return '' if not sub or any(p.startswith('.') for p in sub.split('/')) else sub


def _place(root, allowed, folder, frel, sub, write, unzip):
    """Put one uploaded file at `sub`: write(dest) writes it; an archive is unpacked there (unless unzip=False),
    an existing file is kept -> (added, skipped, placed)."""
    dest = resolve(root, join(frel, sub), allowed)
    if unzip and sub.lower().endswith(UPLOAD_ARCHIVES):
        ext = next(e for e in UPLOAD_ARCHIVES if sub.lower().endswith(e))
        tmp = os.path.join(folder, f'.upload-{uuid.uuid4().hex}{ext}')
        try:
            write(tmp)
            try:
                a, s = unzip_into(tmp, os.path.dirname(dest), root, allowed)
            except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError):
                raise BucketError(f'{sub} is not a valid archive') from None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return a, s, True
    if os.path.lexists(dest):
        return 0, 1, False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    write(dest)
    return 1, 0, True


def upload_local(root, allowed, rel, files, unzip=True):
    """Stream uploaded files into folder `rel` (their names may hold sub-folders) -> {added, skipped, files}."""
    folder, frel = _upload_folder(root, allowed, rel)
    added = skipped = 0
    names = []
    for name, fobj in files:
        sub = _upload_name(name)
        if not sub:
            skipped += 1
            continue
        a, s, placed = _place(root, allowed, folder, frel, sub, lambda dest, f=fobj: _write_stream(f, dest), unzip)
        added, skipped = added + a, skipped + s
        if placed:
            names.append(sub)
    return {'added': added, 'skipped': skipped, 'files': names[:200]}


_UPLOAD_ID = re.compile(r'^[0-9a-f]{32}$')
PART_MAX_AGE = 86400                # an upload part nobody finished is removed after a day
_part_locks = {}                    # part file -> Lock: the chunks of one file arrive in parallel
_part_locks_guard = threading.Lock()


def _part_lock(part):
    with _part_locks_guard:
        return _part_locks.setdefault(part, threading.Lock())


def _merge(ranges, a, b):
    out = []
    for x, y in sorted([*ranges, [a, b]]):
        if out and x <= out[-1][1]:
            out[-1][1] = max(out[-1][1], y)
        else:
            out.append([x, y])
    return out


class _Part(NamedTuple):
    folder: str
    frel: str
    sub: str                        # '' when the name is skipped
    dest: str
    part: str                       # .upload-<uid>.part: the bytes, written at their offsets (sparse)
    meta: str                       # .upload-<uid>.json: {name, total, ranges} received so far


def _part_paths(root, allowed, rel, name, uid):
    if not _UPLOAD_ID.match(str(uid or '')):
        raise BucketError('the upload id is 32 hex characters')
    folder, frel = _upload_folder(root, allowed, rel)
    sub = _upload_name(name)
    dest = resolve(root, join(frel, sub), allowed) if sub else ''
    base = os.path.join(folder, f'.upload-{uid}')
    return _Part(folder, frel, sub, dest, f'{base}.part', f'{base}.json')


def _read_meta(p, total):
    """The ranges held for this part ([] when none, or when they were for another size)."""
    try:
        with open(p.meta, encoding='utf-8') as f:
            m = json.load(f)
        if int(m.get('total', -1)) == total and os.path.exists(p.part):
            return [[int(a), int(b)] for a, b in m.get('ranges') or []]
    except (OSError, ValueError, TypeError):
        pass
    return []


def _held(ranges):
    return sum(b - a for a, b in ranges)


def _skipped(p, unzip):
    """A name upload() would skip (a dot part; a file already there that is not an archive to unpack)."""
    return not p.sub or (not (unzip and p.sub.lower().endswith(UPLOAD_ARCHIVES)) and os.path.lexists(p.dest))


SKIP = {'added': 0, 'skipped': 1, 'files': [], 'done': True}


def part_status(root, allowed, rel, name, uid, total, unzip=True):
    """What the server holds of an upload in parts -> {received, ranges}, or SKIP when the file would be skipped
    (nothing is being gathered for it). The page asks first, so a re-upload sends only the missing ranges."""
    p = _part_paths(root, allowed, rel, name, uid)
    total = int(total)
    with _part_lock(p.part):
        ranges = _read_meta(p, total)
        if not ranges and _skipped(p, unzip):
            return dict(SKIP)
        return {'received': _held(ranges), 'ranges': ranges}


def upload_part(root, allowed, rel, name, uid, offset, total, data, unzip=True):
    """One chunk of a big upload (a proxy in front of the app may refuse large requests): bytes `data` at `offset`
    of the file `name` (`total` bytes) into folder `rel`. Chunks may come in any order, in parallel and more than
    once: they are written in place into the hidden `.upload-<uid>.part` and the ranges held are kept beside it
    (`.upload-<uid>.json`), so an upload resumes after a cut or a reload. -> {received, ranges} while bytes are
    missing; {ready: True} to exactly one caller once all are in (it then calls finish_part); SKIP for a name
    upload() would skip, before any bytes are kept."""
    offset, total = int(offset), int(total)
    if total < 0 or offset < 0 or offset + len(data) > total:
        raise BucketError('the chunk does not fit the file size')
    p = _part_paths(root, allowed, rel, name, uid)
    with _part_lock(p.part):
        ranges = _read_meta(p, total)
        if not ranges:
            if _skipped(p, unzip):
                return dict(SKIP)
            _drop_stale_parts(p.folder)
            with open(p.part, 'wb') as f:
                f.truncate(total)
        fd = os.open(p.part, os.O_WRONLY)
        try:
            os.pwrite(fd, data, offset)
        finally:
            os.close(fd)
        if data or not total:
            ranges = _merge(ranges, offset, offset + len(data))
        if ranges == [[0, total]] or not total:
            try:
                os.remove(p.meta)
            except OSError:
                pass
            ready = f'{p.part}.ready'
            os.replace(p.part, ready)               # claimed: a late duplicate chunk starts nothing
            return {'ready': True, 'path': ready}
        tmp = f'{p.meta}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'name': p.sub, 'total': total, 'ranges': ranges}, f)
        os.replace(tmp, p.meta)
        return {'received': _held(ranges), 'ranges': ranges}


def finish_part(root, allowed, rel, name, uid, ready, unzip=True):
    """Place the file upload_part gathered (`ready`, the path it answered) -> {added, skipped, files, done}."""
    p = _part_paths(root, allowed, rel, name, uid)
    try:
        a, s, placed = _place(root, allowed, p.folder, p.frel, p.sub, lambda to: os.replace(ready, to), unzip)
    finally:
        try:
            os.remove(ready)
        except OSError:
            pass
    return {'added': a, 'skipped': s, 'files': [p.sub] if placed else [], 'done': True}


def _drop_stale_parts(folder):
    now = time.time()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        if n.startswith('.upload-') and n.endswith(('.part', '.json', '.ready', '.tmp')):
            q = os.path.join(folder, n)
            try:
                if now - os.path.getmtime(q) > PART_MAX_AGE:
                    os.remove(q)
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------- thumbnails
class _Pipe(io.RawIOBase):
    """A write-only sink zipfile writes into; the bytes are drained chunk by chunk (export streams, no temp file)."""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def take(self):
        out = bytes(self.buf)
        self.buf.clear()
        return out


class Item(NamedTuple):
    """One file an export holds: `rel` below the bucket root, `path` the local file (None for an S3 object) and
    `open()` a binary reader (a file, an S3 body: both read(n) + close())."""
    rel: str
    size: int
    mtime: float | None
    path: str | None
    open: object


def local_items(root, rel, allowed, secrets=True):
    """The Items of folder `rel` of `root` (or of that one file): hidden names and the trash are left out (and
    .env files unless `secrets`), symlinks too (a link to a file outside would leak it; a broken one has nothing)."""
    rel = clean_rel(rel)
    base = resolve(root, rel, allowed, must_exist=True)
    if os.path.isfile(base):                            # one file: resolve already kept it inside the bucket
        found, look = [(rel, base)], os.stat
    else:
        found = ((p, a) for p, isdir, a in walk_local(root, rel, allowed, cap=10_000_000) if not isdir)
        look = os.lstat
    for p, absp in found:
        if not secrets and is_secret(p):
            continue
        try:
            st = look(absp)
        except OSError:
            continue
        if statmod.S_ISREG(st.st_mode):                 # lstat: a symlink, a socket or a fifo is not
            yield Item(p, st.st_size, st.st_mtime, absp, lambda a=absp: open(a, 'rb'))


def _zinfo(name, it):
    if it.path is not None:
        zi = zipfile.ZipInfo.from_file(it.path, name, strict_timestamps=False)   # mtime 0 / before 1980: clamped
    else:
        t = time.localtime(it.mtime if it.mtime else time.time())[:6]
        zi = zipfile.ZipInfo(name, t if t[0] >= 1980 else (1980, 1, 1, 0, 0, 0))
        zi.external_attr = 0o644 << 16
    zi.file_size = it.size                              # z.open picks zip64 for big files from this
    zi.compress_type = zipfile.ZIP_STORED
    return zi


def zip_items(items, strip=''):
    """Yield a zip of `items` chunk by chunk (names relative to folder `strip`). Stored, not deflated: images do
    not shrink. A file that cannot be opened (or fails half-way: it ends short) is skipped, never the stream:
    the browser has already been sent the headers."""
    pipe = _Pipe()
    with zipfile.ZipFile(pipe, 'w', zipfile.ZIP_STORED, allowZip64=True) as z:
        for it in items:
            name = it.rel[len(strip) + 1:] if strip else it.rel
            try:
                zi = _zinfo(name, it)
                src = it.open()
            except Exception:  # noqa: BLE001 - OSError, a vanished S3 object: skip the file
                continue
            try:
                with z.open(zi, 'w') as dst:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        dst.write(chunk)
                        if len(pipe.buf) >= CHUNK:
                            yield pipe.take()
            except Exception:  # noqa: BLE001 - a read error half-way: the entry ends short, the zip stays valid
                pass
            finally:
                _try(src.close)
            if pipe.buf:
                yield pipe.take()
    if pipe.buf:
        yield pipe.take()


def zip_stream(root, rel, allowed, secrets=True):
    """Yield a zip of folder `rel` of `root` (or of one file, named by itself), chunk by chunk."""
    rel = clean_rel(rel)
    single = os.path.isfile(resolve(root, rel, allowed, must_exist=True))
    return zip_items(local_items(root, rel, allowed, secrets), posix_parent(rel) if single else rel)


def thumbnail(data_or_path, key, w, cfg):
    """A JPEG at most w px on its long side, cached on disk under the workspace root (key = path + mtime + size)."""
    from PIL import Image, ImageOps
    w = max(16, min(int(w), 1024))
    p = _thumb_path(cfg, key, w)
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
        raise BucketError(f'cannot make a thumbnail: {e}', 415) from None
    return _thumb_save(p, out.getvalue())


def video_thumbnail(path, key, w, t, cfg):
    """A JPEG of a video's frame at `t` seconds (None: its poster, 10 % in), at most w px, cached like thumbnail()."""
    from . import video
    w = max(16, min(int(w), 1024))
    t = None if t is None else round(max(0.0, float(t)), 2)
    p = _thumb_path(cfg, f'{key}|t={t}', w)
    try:
        return p.read_bytes()
    except OSError:
        pass
    try:
        data = video.frame_at(path, t, w)
    except BucketError:
        raise
    except Exception as e:  # noqa: BLE001 - any undecodable video is the same answer
        raise BucketError(f'cannot make a poster frame: {e}', 415) from None
    return _thumb_save(p, data)


def _thumb_path(cfg, key, w):
    h = hashlib.sha1(f'{key}|{w}'.encode()).hexdigest()
    return cache_dir(cfg) / h[:2] / f'{h}.jpg'


def _thumb_save(p, data):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f'.{uuid.uuid4().hex[:6]}.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except OSError:
        pass
    return data


# ---------------------------------------------------------------------------------------------- S3 (read-only)
class Offline(BucketError):
    """S3 cannot be reached (no route, refused, timed out): the browser falls back to the downloaded folders."""

    def __init__(self, message):
        super().__init__(message, 503)


_OFFLINE_FOR = 30                   # s an unreachable endpoint is not asked again (thumbnails fail fast)
_BROWSE = {'connect_timeout': 3, 'read_timeout': 20, 'retries': 2}   # an interactive read: fail in seconds
_CLIENTS_MAX = 32
_clients = {}                       # (connection, config) -> shared boto3 client, oldest first
_down = {}                          # connection key -> monotonic time it may be tried again
_READS = 16                         # object reads in flight per connection (a page of thumbnails, many tabs)
_gates = {}                         # connection key -> BoundedSemaphore(_READS)
_clients_lock = threading.Lock()


def _gate(ck):
    with _clients_lock:
        g = _gates.get(ck)
        if g is None:
            g = _gates[ck] = threading.BoundedSemaphore(_READS)
        return g


def _conn_key(conn):
    return (conn.get('id'), conn.get('endpoint'), conn.get('region'), conn.get('access_key'), conn.get('secret_key'),
            bool(conn.get('verify_ssl', True)))


def _is_offline(e):
    try:
        from botocore import exceptions as bx
    except ImportError:  # pragma: no cover - boto3 is a dependency
        return False
    return isinstance(e, (bx.EndpointConnectionError, bx.ConnectTimeoutError, bx.ReadTimeoutError,
                          bx.ConnectionClosedError))


def page_rows(dirs, files, offset=0, limit=500, sort='name', q=''):
    """One page of a folder listing: folders first, each part sorted by `sort`, names filtered by `q`."""
    field, desc = _sort_key(sort or 'name')
    qq = (q or '').casefold()
    if qq:
        dirs = [e for e in dirs if qq in e['name'].casefold()]
        files = [e for e in files if qq in e['name'].casefold()]
    keyf = {'name': lambda e: e['name'].casefold(), 'mtime': lambda e: e['mtime'] or 0,
            'size': lambda e: e['size']}[field]
    rows = sorted(dirs, key=keyf, reverse=desc) + sorted(files, key=keyf, reverse=desc)
    offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or 500), 5000))
    return len(rows), rows[offset:offset + limit]


class S3View:
    """A linked S3 bucket + prefix, read through the owner's connection (sources.make_client). Read-only.
    shared: reuse one client per connection (thread-safe; close() keeps it) with short timeouts for interactive
    reads, and the offline circuit breaker: an endpoint that did not answer is not asked again for _OFFLINE_FOR s."""

    def __init__(self, conn, bucket, prefix='', s3cfg=None, shared=False):
        from ..image import sources
        self.shared = shared
        self._ck = _conn_key(conn)
        if shared:
            cfg = {**(s3cfg or {}), **_BROWSE}
            ck = (self._ck, repr(sorted(cfg.items())))
            with _clients_lock:
                self.client = _clients.pop(ck, None) or sources.make_client(conn, cfg)
                _clients[ck] = self.client
                while len(_clients) > _CLIENTS_MAX:     # not closed: another thread may still read through it
                    _clients.pop(next(iter(_clients)))
        else:
            self.client = sources.make_client(conn, s3cfg)
        self.bucket = bucket
        self.base = sources._norm_prefix(prefix)

    def key(self, rel):
        return self.base + clean_rel(rel)

    def online(self, retry=False):
        """Raise Offline while the circuit breaker is open (retry: close it and try again)."""
        if retry:
            _down.pop(self._ck, None)
        if _down.get(self._ck, 0) > time.monotonic():
            raise Offline('S3 is unreachable: only downloaded folders are available')

    def _net(self, fn, *a, **kw):
        self.online()
        try:
            return fn(*a, **kw)
        except Exception as e:
            if _is_offline(e):
                _down[self._ck] = time.monotonic() + _OFFLINE_FOR
                raise Offline(f'S3 is unreachable ({type(e).__name__}): only downloaded folders are available') from None
            raise

    def dir_pages(self, rel):
        """The entries right inside folder `rel`, one S3 page at a time -> iterator of (dirs, files)."""
        rel = clean_rel(rel)
        pre = self.base + (rel + '/' if rel else '')
        kw = {'Bucket': self.bucket, 'Prefix': pre, 'Delimiter': '/'}
        while True:
            page = self._net(s3list.request, self.client, **kw)      # inside the process-wide request budget
            dirs, files = [], []
            for p in page.get('CommonPrefixes', []):
                n = p['Prefix'][len(pre):].rstrip('/')
                if n and visible(n, True):
                    dirs.append({'name': n, 'path': join(rel, n), 'type': 'dir', 'size': 0, 'mtime': None})
            for o in page.get('Contents', []):
                n = o['Key'][len(pre):]
                if not n or '/' in n or not visible(n):
                    continue
                lm = o.get('LastModified')
                files.append({'name': n, 'path': join(rel, n), 'type': 'file', 'size': int(o.get('Size') or 0),
                              'mtime': lm.timestamp() if lm is not None else None, 'kind': kind_of(n, rel)})
            yield dirs, files
            tok = page.get('NextContinuationToken')
            if not page.get('IsTruncated') or not tok:
                return
            kw['ContinuationToken'] = tok

    def list(self, rel, offset=0, limit=500, sort='name', q=''):
        rel = clean_rel(rel)
        dirs, files = [], []
        for d, f in self.dir_pages(rel):
            dirs += d
            files += f
        total, rows = page_rows(dirs, files, offset, limit, sort, q)
        return {'path': rel, 'total': total, 'entries': rows}

    def keys(self, rel='', cap=100_000, progress=None, cancelled=None):
        """{rel path below rel: size} of every object (capped), listed in parallel (s3list) and sorted (a detection
        must not depend on the listing order); progress(done, None, 'listing S3', 'n objects') as it goes."""
        rel = clean_rel(rel)
        pre = self.base + (rel + '/' if rel else '')
        out, n, last = {}, 0, 0.0
        with closing(s3list.objects(self.client, self.bucket, pre, cancelled=cancelled)) as objs:
            for o in objs:
                k = o['Key'][len(pre):]
                n += 1
                if progress is not None and time.monotonic() - last >= 0.25:
                    last = time.monotonic()
                    progress(n, None, 'listing S3', f'{n:,} objects')
                if _shown(k):
                    out[k] = int(o.get('Size') or 0)
                    if len(out) >= cap:
                        break
        return dict(sorted(out.items()))

    def what(self, rel):
        """'file' when `rel` is an object, 'dir' when objects lie below it (or it is the root), else None."""
        rel = clean_rel(rel)
        if not rel:
            return 'dir'
        if self.size(rel) is not None:
            return 'file'
        r = self.client.list_objects_v2(Bucket=self.bucket, Prefix=self.key(rel) + '/', MaxKeys=1)
        return 'dir' if r.get('KeyCount', len(r.get('Contents', []))) else None

    def items(self, rel='', secrets=True):
        """The export Items of folder `rel` (or of that one object), filtered like local_items; listed page by page
        (no full key list in memory) and each object read only when the zip / the copy gets to it."""
        rel = clean_rel(rel)

        def item(k, size, lm):
            return Item(k, size, lm.timestamp() if lm is not None else None, None,
                        lambda: self.client.get_object(Bucket=self.bucket, Key=self.base + k)['Body'])
        if rel and self.what(rel) == 'file':
            if secrets or not is_secret(rel):
                h = self.client.head_object(Bucket=self.bucket, Key=self.key(rel))
                yield item(rel, int(h.get('ContentLength') or 0), h.get('LastModified'))
            return
        pre = self.base + (rel + '/' if rel else '')
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=pre):
            for o in page.get('Contents', []):
                sub = o['Key'][len(pre):]
                if not _shown(sub):
                    continue
                k = join(rel, sub)
                if secrets or not is_secret(k):
                    yield item(k, int(o.get('Size') or 0), o.get('LastModified'))

    def read(self, rel, limit=None):
        kw = {'Range': f'bytes=0-{int(limit) - 1}'} if limit else {}
        try:
            with _gate(self._ck):
                obj = self._net(self.client.get_object, Bucket=self.bucket, Key=self.key(rel), **kw)
                data = obj['Body'].read()
        except Offline:
            raise
        except Exception as e:  # noqa: BLE001 - botocore's NoSuchKey / ClientError
            raise BucketError(f'cannot read {rel}: {e}', 404) from None
        return data, obj.get('ETag', ''), int(obj.get('ContentLength') or 0)

    def download(self, rel, dest, progress=None):
        """Stream object `rel` into local file `dest` (never whole in memory: a video can be gigabytes).
        progress(n, total) after each chunk of n bytes (total: the object's size, None when S3 does not say)."""
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.key(rel))
        except Exception as e:  # noqa: BLE001 - botocore's NoSuchKey / ClientError
            raise BucketError(f'cannot read {rel}: {e}', 404) from None
        total = int(obj.get('ContentLength') or 0) or None
        _write_stream(obj['Body'], dest, None if progress is None else (lambda n: progress(n, total)))

    def size(self, rel):
        try:
            return int(self.client.head_object(Bucket=self.bucket, Key=self.key(rel)).get('ContentLength') or 0)
        except Exception:  # noqa: BLE001
            return None

    def pull(self, dest, progress=None, cancelled=None, workers=16, rel='', mirror=False, listed=None, keep=None):
        """Download every object below the prefix (below its folder `rel` only, written to dest/rel) into folder
        `dest` ("load into local"): an object replaces the local file of the same name unless that file is unchanged
        (same size, and the mtime every download sets to the object's LastModified). Local files S3 does not have
        are kept, unless `mirror`: then, once the listing completed, they are removed (`removed`). Downloads start
        while the (parallel) listing runs; an object of BIG bytes or more is fetched in parallel ranges.
        listed(key, size, last_modified): called for every object listed (the download's manifest,
        bucket/cloudiff.Manifest).
        keep: {key: (size, last_modified)} of local files edited since the last download (their manifest rows):
        they are never replaced nor removed (`kept`), and their manifest row stays the version they were edited
        from, so Upload to S3 still sees a conflict when S3 changed meanwhile (`conflicts`: those keys).
        -> {downloaded, skipped, bytes (downloaded), size (of every object), failed, total, removed}."""
        progress = progress or (lambda *a, **k: None)
        stop = cancelled or (lambda: False)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        rel = clean_rel(rel)
        pre = self.base + (rel + '/' if rel else '')
        n = {'found': 0, 'done': 0, 'skipped': 0, 'bytes': 0, 'size': 0, 'listing': True, 'last': 0.0}
        failed, futs, seen = [], {}, set()
        keep = keep or {}
        kept, conflicts = [], []

        def fetch(k, size, lm):
            dst = dest / k
            if dst.is_dir():                                            # a local folder named like the object
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + '.part')
            try:
                if size >= BIG:
                    from boto3.s3.transfer import TransferConfig
                    self.client.download_file(self.bucket, self.base + k, str(tmp),
                                              Config=TransferConfig(multipart_threshold=BIG, max_concurrency=4))
                else:
                    body = self.client.get_object(Bucket=self.bucket, Key=self.base + k)['Body']
                    with open(tmp, 'wb') as f:
                        for chunk in body.iter_chunks(1 << 20):
                            f.write(chunk)
                if lm is not None:
                    os.utime(tmp, (lm, lm))
                os.replace(tmp, dst)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return size

        def report(force=False):
            if not force and time.monotonic() - n['last'] < 0.25:
                return
            n['last'] = time.monotonic()
            extra = f'{n["bytes"] / 1e6:.1f} MB' + (f' · {n["skipped"]:,} unchanged' if n['skipped'] else '')
            progress(n['done'], None if n['listing'] else n['found'], 'downloading',
                     f'listing… {n["found"]:,} found · {extra}' if n['listing'] else extra)

        def settle(block):
            ready, _ = wait(list(futs), timeout=None if block else 0, return_when=FIRST_COMPLETED)
            for f in ready:
                k = futs.pop(f)
                if f.cancelled():
                    continue
                n['done'] += 1
                e = f.exception()
                if e is not None:
                    failed.append({'path': k, 'error': str(e)})
                else:
                    n['bytes'] += f.result()

        report(True)
        with ThreadPoolExecutor(max(1, int(workers))) as ex:
            with closing(s3list.objects(self.client, self.bucket, pre, cancelled=stop)) as objs:
                for o in objs:
                    if stop():
                        break
                    sub = o['Key'][len(pre):]
                    if not _shown(sub):
                        continue
                    k = join(rel, sub)
                    if mirror:
                        seen.add(k)
                    n['found'] += 1
                    size, lm = int(o.get('Size') or 0), o.get('LastModified')
                    n['size'] += size
                    ts = lm.timestamp() if lm is not None else None
                    base = keep.get(k)
                    if base is not None:                    # edited here: the local file stays as it is
                        if listed is not None:
                            listed(k, base[0], base[1])
                        kept.append(k)
                        if base[0] != size or base[1] is None or ts is None or abs(base[1] - ts) > 1:
                            conflicts.append(k)
                        n['skipped'] += 1
                        n['done'] += 1
                        continue
                    if listed is not None:
                        listed(k, size, ts)
                    if _unchanged(dest / k, size, ts):
                        n['skipped'] += 1
                        n['done'] += 1
                    else:
                        futs[ex.submit(fetch, k, size, ts)] = k
                        if len(futs) >= 4 * max(1, int(workers)):
                            settle(True)
                    if time.monotonic() - n['last'] >= 0.25:
                        if futs:
                            settle(False)
                        report()
            n['listing'] = False
            if stop():
                for f in futs:
                    f.cancel()
            while futs:
                settle(True)
                report()
        report(True)
        removed = 0
        top = dest / rel if rel else dest
        if mirror and not stop() and top.is_dir():
            for path, _isdir, absp in list(walk_files(top, rel)):
                if path not in seen and path not in keep:
                    _try(os.unlink, absp)
                    removed += 1
            _prune_empty(top)
        return {'downloaded': n['done'] - n['skipped'] - len(failed), 'skipped': n['skipped'] - len(kept),
                'bytes': n['bytes'], 'size': n['size'], 'failed': failed[:50], 'total': n['found'], 'removed': removed,
                'kept': len(kept), 'conflicts': conflicts[:50], 'kept_paths': kept}

    def close(self):
        if self.shared:
            return
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001 - a close that fails must not hide the real outcome
        return e


def walk_files(top, rel=''):
    """(rel path, False, abs path) of every visible file below folder `top` (no symlinks followed); `rel` is
    top's own path, prefixed to each one."""
    stack = [(clean_rel(rel), str(top))]
    while stack:
        r, d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                isdir = e.is_dir(follow_symlinks=False)
                if not visible(e.name, isdir):
                    continue
                if isdir:
                    stack.append((join(r, e.name), e.path))
                elif e.is_file(follow_symlinks=False):
                    yield join(r, e.name), False, e.path


def _prune_empty(top):
    """Remove the empty folders below `top` (bottom up; top itself stays; rmdir refuses a folder still in use)."""
    for d, _, _ in os.walk(top, topdown=False):
        if d != str(top):
            _try(os.rmdir, d)


def _shown(sub):
    """Whether object `sub` (a key below the listed prefix) is listed: a file name, no hidden part."""
    parts = sub.split('/')
    return bool(parts[-1]) and visible(parts[-1]) and all(visible(p, True) for p in parts[:-1])


def _unchanged(dst, size, mtime):
    """A local file a pull wrote from this very object version (same size, mtime = its LastModified to the
    millisecond: S3 times are whole seconds, so an edit made in that same second still counts as a change)."""
    try:
        st = os.stat(dst)
    except OSError:
        return False
    return statmod.S_ISREG(st.st_mode) and st.st_size == size and mtime is not None and abs(st.st_mtime - mtime) < 1e-3


def posix_parent(rel):
    p = str(PurePosixPath(rel).parent)
    return '' if p == '.' else p
