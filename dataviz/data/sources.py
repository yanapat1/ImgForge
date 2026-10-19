"""
Where a YOLO dataset lives: a local folder (read in place, nothing copied) or an S3 / RustFS bucket.

Every source has the same small interface, so the indexer and the API never care where files are:

    name, uri, root (local folder or None), fingerprint(), list_labels(), list_images(),
    read_label(name), read_classes(), open_image(name) -> PIL.Image, image_ref(name)

Label files can be written back (data curation): `label_writable` and
`write_label(name, text, expect_sha1) -> new sha1`, which raises Conflict when the file changed
since it was read (sha1 of its raw bytes != expect_sha1; None = do not check). `read_label_bytes(name)`
returns the raw bytes (b'' for a missing file), so a backup restores a file byte for byte.

S3 has two modes:
    stream  images are downloaded in parallel while indexing, turned into crops and dropped;
            the full-image view fetches one image on demand. No local copy of the images.
    cache   the bucket prefix is mirrored to a local folder (incremental, parallel downloads),
            then the dataset is read from the mirror like any local folder: fastest browsing.

Downloads run on a thread pool: boto3 releases the GIL while waiting on the network, so
`concurrency` requests really are in flight at once.
"""

import io
import os
import json
import time
import hashlib
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml
from PIL import Image

from ..core import logging as vlog

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.webp')
INDEX_VERSION = 1
_log = vlog.get_logger('s3')


# ---------------------------------------------------------------- decoded-image LRU

class ImageLRU:
    """Thread-safe LRU of decoded RGB images, bounded by bytes. Cached images are SHARED and must be treated as
    read-only: crop()/resize()/convert-to-another-mode all make new images, but never draw on or thumbnail() one
    (use .copy() first)."""

    def __init__(self, max_bytes=256 * 2**20):
        from collections import OrderedDict
        self.max_bytes = int(max_bytes)
        self._d = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _size(im):
        return im.width * im.height * 3

    def get(self, key):
        with self._lock:
            im = self._d.get(key)
            if im is not None:
                self._d.move_to_end(key)
            return im

    def put(self, key, im):
        n = self._size(im)
        if n > self.max_bytes:                    # a single huge image is not worth evicting everything for
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._bytes -= self._size(old)
            self._d[key] = im
            self._bytes += n
            while self._bytes > self.max_bytes and self._d:
                _, ev = self._d.popitem(last=False)
                self._bytes -= self._size(ev)

    def clear(self):
        with self._lock:
            self._d.clear()
            self._bytes = 0

    def __len__(self):
        return len(self._d)

    @property
    def nbytes(self):
        return self._bytes


_IMAGES = ImageLRU()


def cached_rgb(source, name):
    """Decoded RGB image `name` of `source`, decoded once for many crops. READ-ONLY (shared): copy before drawing."""
    key = (id(source), getattr(source, 'uri', None), name)
    im = _IMAGES.get(key)
    if im is None:
        with source.open_image(name) as raw:
            im = raw.convert('RGB')
            im.load()
        _IMAGES.put(key, im)
    return im


def clear_image_cache():
    _IMAGES.clear()


class Conflict(Exception):
    """write_label: the label file changed since it was read (its sha1 is not the expected one)."""

    def __init__(self, name, current=None):
        super().__init__(f'{name} changed since it was read')
        self.name = name
        self.current = current            # sha1 of the file as it is now (None when unknown)


def sha1_bytes(data):
    return hashlib.sha1(data).hexdigest()


def _as_bytes(text):
    return text if isinstance(text, (bytes, bytearray)) else str(text).encode('utf-8')


def _is_image(name):
    return name.lower().endswith(IMAGE_EXTS)


def classes_from_yaml(text):
    """`names:` from an ultralytics data.yaml (list or {id: name})."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    names = data.get('names') if isinstance(data, dict) else None
    if isinstance(names, dict):
        return [str(names[k]) for k in sorted(names, key=int)]
    return [str(n) for n in names] if isinstance(names, list) else []


def parse_classes(classes_txt=None, data_yaml=None):
    if classes_txt is not None:
        names = [line.strip() for line in classes_txt.splitlines() if line.strip()]
        if names:
            return names
    return classes_from_yaml(data_yaml) if data_yaml is not None else []


# ---------------------------------------------------------------- local folder

class LocalSource:
    kind = 'local'

    def __init__(self, path, name=None):
        self.root = Path(path).expanduser().resolve()
        self.name = name or self.root.name
        self.uri = str(self.root)
        self._images = None

    def check(self):
        """Raise ValueError unless this is a YOLO folder."""
        if not (self.root / 'images').is_dir() or not (self.root / 'labels').is_dir():
            raise ValueError(f'{self.root} must contain images/ and labels/')

    def fingerprint(self):
        """Hash of label/image names, sizes and mtimes; changes when data changes."""
        h = hashlib.sha1(str(INDEX_VERSION).encode())
        for sub in ('labels', 'images'):
            d = self.root / sub
            if not d.exists():
                continue
            for p in sorted(d.iterdir()):
                st = p.stat()
                h.update(f'{sub}/{p.name}:{st.st_size}:{int(st.st_mtime)}'.encode())
        cls = self.root / 'classes.txt'
        if cls.exists():
            h.update(cls.read_bytes())
        return h.hexdigest()

    def list_labels(self):
        return sorted(p.name for p in (self.root / 'labels').glob('*.txt'))

    def list_images(self):
        if self._images is None:
            d = self.root / 'images'
            self._images = sorted(p.name for p in d.iterdir() if _is_image(p.name)) if d.is_dir() else []
        return self._images

    def read_label(self, name):
        return (self.root / 'labels' / name).read_text()

    # -- writing labels (data curation)
    label_writable = True

    def _label_path(self, name):
        if '/' in name or '\\' in name or name in ('', '.', '..'):
            raise ValueError(f'bad label name {name!r}')
        return self.root / 'labels' / name

    def read_label_bytes(self, name):
        try:
            return self._label_path(name).read_bytes()
        except FileNotFoundError:
            return b''

    def write_label(self, name, text, expect_sha1=None):
        """Atomically replace labels/<name> with `text` (str or bytes, written as-is); returns its sha1."""
        path = self._label_path(name)
        data = _as_bytes(text)
        if expect_sha1 is not None:
            cur = sha1_bytes(self.read_label_bytes(name))
            if cur != expect_sha1:
                raise Conflict(name, cur)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return sha1_bytes(data)

    def read_classes(self):
        txt, yml = self.root / 'classes.txt', self.root / 'data.yaml'
        return parse_classes(txt.read_text() if txt.exists() else None, yml.read_text() if yml.exists() else None)

    def open_image(self, name):
        return Image.open(self.root / 'images' / name)

    def image_ref(self, name):
        return str(self.root / 'images' / name)

    def label_ref(self, name):
        return str(self.root / 'labels' / name)


# ---------------------------------------------------------------- S3 / RustFS

S3_DEFAULTS = {'concurrency': 32, 'connect_timeout': 5, 'read_timeout': 60, 'retries': 5}


def make_client(conn, s3cfg=None):
    """boto3 client for an S3-compatible endpoint (RustFS / MinIO need path-style addressing)."""
    import boto3
    from botocore.config import Config
    c = {**S3_DEFAULTS, **(s3cfg or {})}
    return boto3.client(
        's3', endpoint_url=conn.get('endpoint') or None, region_name=conn.get('region') or 'us-east-1',
        aws_access_key_id=conn.get('access_key') or None, aws_secret_access_key=conn.get('secret_key') or None,
        verify=bool(conn.get('verify_ssl', True)),
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                      max_pool_connections=int(c['concurrency']) + 4,
                      connect_timeout=float(c['connect_timeout']), read_timeout=float(c['read_timeout']),
                      retries={'max_attempts': int(c['retries']), 'mode': 'adaptive'}))


def _norm_prefix(prefix):
    prefix = (prefix or '').strip().strip('/')
    return f'{prefix}/' if prefix else ''


def list_objects(client, bucket, prefix):
    """{relative key: (size, etag)} for every object below prefix."""
    out = {}
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get('Contents', []):
            out[o['Key'][len(prefix):]] = (int(o['Size']), o.get('ETag', '').strip('"'))
    return out


def browse(conn, bucket=None, prefix='', s3cfg=None):
    """Buckets (no bucket given) or the folders directly under prefix, flagged when they look like YOLO."""
    client = make_client(conn, s3cfg)
    if not bucket:
        return {'buckets': sorted(b['Name'] for b in client.list_buckets().get('Buckets', []))}
    prefix = _norm_prefix(prefix)
    folders = []
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix, Delimiter='/'):
        folders += [p['Prefix'][len(prefix):].rstrip('/') for p in page.get('CommonPrefixes', [])]
    here = set(folders)

    def is_yolo(sub):
        r = client.list_objects_v2(Bucket=bucket, Prefix=f'{prefix}{sub}/', Delimiter='/', MaxKeys=200)
        names = {p['Prefix'].rstrip('/').rsplit('/', 1)[-1] for p in r.get('CommonPrefixes', [])}
        return {'images', 'labels'} <= names

    with ThreadPoolExecutor(8) as ex:
        flags = dict(zip(folders, ex.map(is_yolo, folders)))
    return {'bucket': bucket, 'prefix': prefix.rstrip('/'), 'yolo': {'images', 'labels'} <= here,
            'folders': [{'name': f, 'yolo': flags[f]} for f in folders]}


class S3Source:
    kind = 's3'

    def __init__(self, conn, bucket, prefix='', name=None, mode='stream', mirror_root=None, s3cfg=None):
        self.conn = conn
        self.bucket = bucket
        self.prefix = _norm_prefix(prefix)
        self.mode = mode
        self.s3cfg = {**S3_DEFAULTS, **(s3cfg or {})}
        self.uri = f's3://{bucket}/{self.prefix}'.rstrip('/')
        self.name = name or (self.prefix.rstrip('/').rsplit('/', 1)[-1] or bucket)
        self.client = make_client(conn, s3cfg)
        self.pool = ThreadPoolExecutor(int(self.s3cfg['concurrency']), thread_name_prefix='s3')
        self._listing = None
        self._lock = threading.Lock()
        mirror_root = Path(mirror_root) if mirror_root else None
        self.mirror = (mirror_root / _safe(conn.get('id') or conn.get('name') or 'conn') / bucket /
                       self.prefix.rstrip('/')) if mirror_root else None
        self.local = None                  # LocalSource on the mirror, once synced (cache mode)
        if mode == 'cache' and self.mirror is not None and (self.mirror / '.manifest.json').exists():
            self.local = LocalSource(self.mirror, name=self.name)

    @property
    def root(self):
        return self.local.root if self.local else None

    # -- listing
    def listing(self, refresh=False):
        with self._lock:
            if self._listing is None or refresh:
                t0 = time.time()
                self._listing = list_objects(self.client, self.bucket, self.prefix)
                _log.info(f'listed {len(self._listing):,} objects in {vlog.esc(self.uri)} [dim]({time.time() - t0:.1f}s)[/]')
            return self._listing

    def check(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f'cannot open bucket {self.bucket!r}: {e}') from e
        st = self.summary(refresh=True)
        if not st['images'] or not st['labels']:
            raise ValueError(f'{self.uri} must contain images/ and labels/ '
                             f'(found {st["images"]} images, {st["labels"]} labels)')
        return st

    def summary(self, refresh=False):
        lst = self.listing(refresh)
        imgs = [k for k in lst if k.startswith('images/') and _is_image(k)]
        return {'images': len(imgs), 'labels': sum(1 for k in lst if k.startswith('labels/') and k.endswith('.txt')),
                'classes': 'classes.txt' in lst or 'data.yaml' in lst,
                'bytes': sum(s for k, (s, _) in lst.items() if k.startswith(('images/', 'labels/')) or '/' not in k)}

    def fingerprint(self):
        # always from the bucket listing (not the mirror's mtimes): stream and cache mode share one workspace
        h = hashlib.sha1(f'{INDEX_VERSION}|s3'.encode())
        for k, (size, etag) in sorted(self.listing().items()):
            if k.startswith(('images/', 'labels/')) or k == 'classes.txt':
                h.update(f'{k}:{size}:{etag}'.encode())
        return h.hexdigest()

    def list_labels(self):
        if self.local:
            return self.local.list_labels()
        return sorted(k[7:] for k in self.listing() if k.startswith('labels/') and k.endswith('.txt') and '/' not in k[7:])

    def list_images(self):
        if self.local:
            return self.local.list_images()
        return sorted(k[7:] for k in self.listing() if k.startswith('images/') and _is_image(k) and '/' not in k[7:])

    def get(self, rel):
        return self.client.get_object(Bucket=self.bucket, Key=self.prefix + rel)['Body'].read()

    def read_label(self, name):
        return self.local.read_label(name) if self.local else self.get(f'labels/{name}').decode('utf-8', 'replace')

    def read_labels(self, names):
        """All label files at once, downloaded in parallel (ordered like `names`)."""
        if self.local:
            return [self.local.read_label(n) for n in names]
        return list(self.pool.map(self.read_label, names))

    # -- writing labels (data curation): the bucket is the truth; cache mode also updates the mirror
    label_writable = True

    def read_label_bytes(self, name):
        if self.local:
            return self.local.read_label_bytes(name)
        try:
            return self.get(f'labels/{name}')
        except self.client.exceptions.NoSuchKey:
            return b''
        except Exception as e:  # noqa: BLE001 - some S3 servers answer a missing key with a plain 404 ClientError
            if getattr(e, 'response', {}).get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
                return b''
            raise

    def read_labels_bytes(self, names):
        """Raw bytes of many label files, downloaded in parallel (ordered like `names`)."""
        if self.local:
            return [self.local.read_label_bytes(n) for n in names]
        return list(self.pool.map(self.read_label_bytes, names))

    def write_label(self, name, text, expect_sha1=None):
        if '/' in name or not name:
            raise ValueError(f'bad label name {name!r}')
        data = _as_bytes(text)
        if expect_sha1 is not None:
            cur = sha1_bytes(self._remote_label_bytes(name))
            if cur != expect_sha1:
                raise Conflict(name, cur)
        key = f'labels/{name}'
        r = self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=bytes(data), ContentType='text/plain')
        etag = (r.get('ETag') or '').strip('"')
        with self._lock:
            if self._listing is not None:
                self._listing[key] = (len(data), etag)
        if self.local is not None:                 # cache mode: keep the mirror (and its manifest) in step
            self.local.write_label(name, data)
            man_path = self.mirror / '.manifest.json'
            try:
                man = json.loads(man_path.read_text()) if man_path.exists() else {}
                man[key] = [len(data), etag]
                _write_json(man_path, man)
            except (OSError, ValueError):
                pass
        return sha1_bytes(data)

    def _remote_label_bytes(self, name):
        """The bucket's copy (not the mirror): the conflict check must see edits made elsewhere."""
        try:
            return self.get(f'labels/{name}')
        except Exception as e:  # noqa: BLE001
            code = getattr(e, 'response', {}).get('Error', {}).get('Code')
            if code in ('404', 'NoSuchKey', 'NotFound'):
                return b''
            raise

    def read_classes(self):
        if self.local:
            return self.local.read_classes()
        lst = self.listing()
        txt = self.get('classes.txt').decode() if 'classes.txt' in lst else None
        yml = self.get('data.yaml').decode() if 'data.yaml' in lst else None
        return parse_classes(txt, yml)

    def open_image(self, name):
        if self.local:
            return self.local.open_image(name)
        return Image.open(io.BytesIO(self.get(f'images/{name}')))

    def image_ref(self, name):
        return self.local.image_ref(name) if self.local else f'{self.uri}/images/{name}'

    def label_ref(self, name):
        return self.local.label_ref(name) if self.local else f'{self.uri}/labels/{name}'

    # -- cache mode
    def sync(self, progress=None, refresh=True):
        """Mirror the prefix to self.mirror: only new/changed files are downloaded, removed ones deleted.

        refresh=False reuses a listing made moments ago (opening a dataset: check() just listed it)."""
        progress = progress or (lambda *a, **k: None)
        if self.mirror is None:
            raise RuntimeError('no mirror folder configured for this dataset')
        sync_log = vlog.get_logger('sync')
        progress(stage='listing bucket')
        lst = {k: v for k, v in self.listing(refresh=refresh).items()
               if (k.startswith(('images/', 'labels/')) and '/' not in k.split('/', 1)[1]) or k in ('classes.txt', 'data.yaml')}
        self.mirror.mkdir(parents=True, exist_ok=True)
        man_path = self.mirror / '.manifest.json'
        manifest = json.loads(man_path.read_text()) if man_path.exists() else {}
        todo = [k for k, (size, etag) in lst.items()
                if manifest.get(k) != [size, etag] or not (self.mirror / k).exists()]
        stale = [k for k in manifest if k not in lst]
        for k in stale:
            (self.mirror / k).unlink(missing_ok=True)
            manifest.pop(k, None)
        total_bytes = sum(lst[k][0] for k in todo)
        sync_log.info(f'{vlog.esc(self.uri)} → {vlog.esc(self.mirror)}: {len(todo):,} to download '
                      f'({total_bytes / 1e6:.1f} MB), {len(lst) - len(todo):,} up to date, {len(stale)} removed')
        (self.mirror / 'images').mkdir(exist_ok=True)
        (self.mirror / 'labels').mkdir(exist_ok=True)
        t0, got, done = time.time(), 0, 0
        progress(0, len(todo), 'downloading')

        def fetch(k):
            dst = self.mirror / k
            tmp = dst.with_name(dst.name + '.part')
            # plain streamed GET: download_file() starts its own transfer thread pool per file, and
            # 32 x 10 threads starve everything else of the GIL (progress froze until the very end)
            body = self.client.get_object(Bucket=self.bucket, Key=self.prefix + k)['Body']
            with open(tmp, 'wb') as f:
                for chunk in body.iter_chunks(1 << 20):
                    f.write(chunk)
            os.replace(tmp, dst)
            return k

        futs = {self.pool.submit(fetch, k): k for k in todo}
        last_save = time.time()
        try:
            for f in as_completed(futs):
                k = f.result()
                manifest[k] = list(lst[k])
                got += lst[k][0]
                done += 1
                el = max(time.time() - t0, 1e-3)
                progress(done, len(todo), extra=f'{got / 1e6:.1f}/{total_bytes / 1e6:.1f} MB · {got / 1e6 / el:.1f} MB/s')
                if time.time() - last_save > 5:            # an interrupted sync resumes where it stopped
                    _write_json(man_path, manifest)
                    last_save = time.time()
        finally:
            _write_json(man_path, manifest)
        el = time.time() - t0
        sync_log.info(f'synced {done:,} files ({got / 1e6:.1f} MB) in {el:.1f}s'
                      + (f' [dim]· {got / 1e6 / el:.1f} MB/s[/]' if done else ''))
        self.local = LocalSource(self.mirror, name=self.name)
        return {'downloaded': done, 'bytes': got, 'removed': len(stale)}

    def mirror_info(self):
        if self.mirror is None or not (self.mirror / '.manifest.json').exists():
            return None
        man = self.mirror / '.manifest.json'
        m = json.loads(man.read_text())
        return {'files': len(m), 'bytes': sum(v[0] for v in m.values()), 'synced': man.stat().st_mtime,
                'path': str(self.mirror)}


def _safe(s):
    return ''.join(c if c.isalnum() or c in '-_.' else '_' for c in str(s))


def _write_json(path, data):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


IMAGE_COUNT_CAP = 10000             # folder browser badges stop counting here, so huge folders stay instant


def count_images(path, cap=IMAGE_COUNT_CAP):
    """Direct (non-hidden) image files in a local folder, counted up to `cap`; 0 when unreadable."""
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if not e.name.startswith('.') and _is_image(e.name):
                    n += 1
                    if n >= cap:
                        break
    except OSError:
        return 0
    return n


def browse_local(path, images=False):
    """Sub-folders of a local path, each flagged when it holds images/ + labels/.

    images=True also counts the direct image files of every folder (labeling: plain image folders)."""
    p = Path(path or '~').expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f'not a folder: {p}')
    folders = []
    try:
        entries = sorted(p.iterdir(), key=lambda x: x.name.lower())
    except PermissionError as e:
        raise ValueError(str(e)) from e
    for d in entries:
        if d.name.startswith('.') or not d.is_dir():
            continue
        try:
            yolo = (d / 'images').is_dir() and (d / 'labels').is_dir()
        except PermissionError:
            yolo = False
        folders.append({'name': d.name, 'yolo': yolo})
    yolo_here = (p / 'images').is_dir() and (p / 'labels').is_dir()
    info = None
    if yolo_here:
        info = {'images': sum(1 for x in (p / 'images').iterdir() if _is_image(x.name)),
                'labels': sum(1 for _ in (p / 'labels').glob('*.txt')),
                'classes': (p / 'classes.txt').exists() or (p / 'data.yaml').exists()}
    out = {'path': str(p), 'parent': str(p.parent) if p.parent != p else None, 'yolo': yolo_here, 'info': info,
           'folders': folders}
    if images:
        with ThreadPoolExecutor(8) as ex:             # a slow network mount answers each scandir in parallel
            counts = list(ex.map(lambda f: count_images(p / f['name']), folders))
        for f, n in zip(folders, counts):
            f['images'] = n
        out['images'] = count_images(p)
    return out
