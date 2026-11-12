"""
Content hashes of a dataset's images: what links "the same image" across two datasets (data/diff.py) and what a
labeling project dedups on (data/promote.py, lbl.media.checksum).

Two hashes per image, both blake2b-128 (hashlib; 32 hex chars, fits lbl.media.checksum):
    h  full    of every byte. Computed for free while indexing: the indexer already holds the file's bytes.
    q  quick   of the size + first 64 KiB + last 64 KiB (the same idea as curation.part()). 128 KiB of I/O however
               big the file: what the backfill computes for images the indexer never reads (no boxes) and for
               workspaces indexed before hashes existed. Different bytes in the middle are missed - acceptable for
               photos (any re-encode changes the head), and `full` can be asked for where it matters (promote).
Matching uses h when both sides have it, else q (data/diff.py).

<workspace>/images.json, written atomically:
    {"version": 1, "fingerprint": <dataset fp>, "complete": <every listed image present>,
     "images": {"<name>": {"q": .., "h": ..|null, "w": px, "hh": px, "n": boxes, "sig": <cheap identity>}}}
`sig` (source.image_sig: size + mtime / ETag / media id) says whether a stored hash is still valid without reading
the file again.
"""

import hashlib
import io
import json
import os
from pathlib import Path

VERSION = 1
PART = 64 * 1024
FILE = 'images.json'


def full(data):
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def quick(head, tail, size):
    """Quick hash of a file of `size` bytes from head = its first PART bytes and tail = the bytes from
    max(PART, size - PART) to the end (empty for a file of at most PART bytes): every reader cuts them the same way."""
    h = hashlib.blake2b(digest_size=16)
    h.update(size.to_bytes(8, 'little'))
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def quick_bytes(data):
    return quick(data[:PART], data[max(PART, len(data) - PART):], len(data))


def quick_file(path):
    """Quick hash of a local file, reading at most 2 x PART bytes."""
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(PART)
        tail = b''
        if size > PART:
            f.seek(max(PART, size - PART))
            tail = f.read(PART)
    return quick(head, tail, size), head


def entry(data, n=0, sig=None, size=None):
    """images.json record of one image from its full bytes (+ its pixel size, read from the header only)."""
    w = hh = None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, hh = im.size
    except Exception:          # noqa: BLE001 - not an image PIL reads: still hashable
        pass
    return {'q': quick_bytes(data), 'h': full(data), 'w': w, 'hh': hh, 'n': n, 'sig': sig}


def match_key(e, other=None):
    """The key two images are compared on: the full hash when both have it, else the quick one."""
    if e is None:
        return None
    if e.get('h') and (other is None or other.get('h')):
        return 'h:' + e['h']
    return 'q:' + e['q'] if e.get('q') else None


def load(ws):
    path = Path(ws) / FILE
    try:
        d = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return d if d.get('version') == VERSION and isinstance(d.get('images'), dict) else None


def save(ws, fingerprint, images, complete):
    path = Path(ws) / FILE
    tmp = path.with_name(f'{FILE}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps({'version': VERSION, 'fingerprint': fingerprint, 'complete': bool(complete),
                               'images': images}, separators=(',', ':')))
    os.replace(tmp, path)


def _size_of(head):
    try:
        from PIL import Image
        with Image.open(io.BytesIO(head)) as im:
            return im.size
    except Exception:          # noqa: BLE001 - header beyond the first bytes / not an image
        return None, None


def ensure(source, ws, fingerprint, full_hash=False, progress=None, workers=8):
    """images.json of a workspace with an entry for EVERY image of `source` (the backfill): images the indexer did
    not read (no boxes, other source kinds) or whose sig changed get a quick hash (<= 128 KiB read; full_hash:
    the full one, every byte read - promote's dedup). Returns {name: entry}. A file already complete for this
    fingerprint returns at once (the fingerprint covers every image's size + mtime / ETag / media)."""
    from concurrent.futures import ThreadPoolExecutor
    d = load(ws)
    images = dict(d['images']) if d else {}
    if d and d.get('complete') and d.get('fingerprint') == fingerprint and \
            (not full_hash or all(e.get('h') for e in images.values())):
        return images
    names = source.list_images()
    sig_of = getattr(source, 'image_sig', None)
    keep = set(names)
    images = {n: e for n, e in images.items() if n in keep}
    todo = []
    for n in names:
        try:
            sig = sig_of(n) if sig_of else None
        except (OSError, KeyError):
            sig = None
        e = images.get(n)
        if e is None or (sig is not None and e.get('sig') not in (None, sig)) or (full_hash and not e.get('h')):
            todo.append((n, sig, (e or {}).get('n', 0)))
        elif e.get('sig') is None and sig is not None:
            e['sig'] = sig

    def one(job):
        n, sig, nbox = job
        try:
            if full_hash or not hasattr(source, 'read_image_part'):
                e = entry(source.read_image_bytes(n), n=nbox, sig=sig)
            else:
                q, head = source.read_image_part(n)
                w, hh = _size_of(head)
                e = {'q': q, 'h': None, 'w': w, 'hh': hh, 'n': nbox, 'sig': sig}
        except (OSError, KeyError, ValueError):
            return n, None                                 # unreadable: matched by name only (diff.py)
        return n, e

    if todo:
        done = 0
        with ThreadPoolExecutor(max(1, int(workers)), thread_name_prefix='hash') as ex:
            for n, e in ex.map(one, todo):
                if e is not None:
                    images[n] = e
                done += 1
                if progress and (done % 200 == 0 or done == len(todo)):
                    progress(done, len(todo), 'hashing images')
    save(ws, fingerprint, images, complete=True)
    return images
