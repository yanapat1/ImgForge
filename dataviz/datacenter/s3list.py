"""
Parallel S3 listing. list_objects_v2 returns at most 1000 keys per request and each page needs the token of the
one before, so a plain walk of a million-object bucket is a thousand round trips in a row (minutes). objects()
maps the top folder levels first (one delimiter listing per folder, side by side), then lists the disjoint folders,
or runs of them, on worker threads at once: about `workers` times faster when the data sits in folders. A flat
folder of many objects is still one serial walk (S3 gives no way to split it without listing it).

Only client.list_objects_v2 is called (the token is handled here), so a small fake client can test it. Every call
goes through one process-wide semaphore (MAX_REQUESTS): many folders, buckets and users listing at once share that
budget instead of multiplying it, so S3 is not flooded into throttling (SlowDown) and the app stays responsive.
"""

import bisect
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

LEVELS = 3          # folder levels mapped at most before the listing starts
MAX_PAGES = 10      # a folder with more entries than this many pages is not split (listed whole)
MAX_REQUESTS = 24   # list requests in flight at once, across every listing of the process

_gate = threading.BoundedSemaphore(MAX_REQUESTS)

_DONE = object()


def request(client, **kw):
    """One list_objects_v2 call inside the process-wide request budget."""
    with _gate:
        return client.list_objects_v2(**kw)


def _children(client, bucket, prefix, max_pages):
    """(objects, sub-prefixes) right below `prefix` (a delimiter listing), or None when it has more than
    `max_pages` pages of entries: such a folder is listed whole instead."""
    kw = {'Bucket': bucket, 'Prefix': prefix, 'Delimiter': '/'}
    objs, subs = [], []
    for _ in range(max_pages):
        page = request(client, **kw)
        objs += page.get('Contents', [])
        subs += [p['Prefix'] for p in page.get('CommonPrefixes', [])]
        tok = page.get('NextContinuationToken')
        if not page.get('IsTruncated') or not tok:
            return objs, subs
        kw['ContinuationToken'] = tok
    return None


def _split(client, bucket, prefix, workers, max_pages, ex):
    """The folders to list (sorted, none inside another) and the objects already seen while mapping them (those
    right inside a mapped folder, which no folder below it holds)."""
    level, direct = [prefix], []
    for _ in range(LEVELS):
        if len(level) >= workers:
            break
        found, grew = [], False
        for p, res in zip(level, ex.map(lambda p: _children(client, bucket, p, max_pages), level)):
            if res is None:
                found.append(p)
            else:
                direct += res[0]
                found += res[1]
                grew = True
        level = sorted(found)
        if not grew:
            break
    return level, direct


def _chunks(level, n):
    """At most `n` runs of consecutive folders (one folder each when they are few)."""
    if len(level) <= n:
        return [[b] for b in level]
    size = -(-len(level) // n)
    return [level[i:i + size] for i in range(0, len(level), size)]


def _put(q, x, stop):
    while not stop.is_set():
        try:
            q.put(x, timeout=0.1)
            return
        except queue.Full:
            pass


def _lister(client, bucket, chunk, nxt, q, stop):
    """List one run of folders onto `q` (page by page). One folder: Prefix = it. A run: one listing from just
    before its first folder up to the next run's first folder, keeping only keys inside one of its folders (the
    objects of the folders above them were yielded by _split)."""
    try:
        if len(chunk) == 1:
            kw = {'Bucket': bucket, 'Prefix': chunk[0]}
        else:
            kw = {'Bucket': bucket, 'Prefix': os.path.commonprefix([chunk[0], chunk[-1]]), 'StartAfter': chunk[0][:-1]}
        while not stop.is_set():
            page = request(client, **kw)
            objs, end = page.get('Contents', []), False
            if len(chunk) > 1:
                keep = []
                for o in objs:
                    k = o['Key']
                    if nxt is not None and k >= nxt:
                        end = True
                        break
                    i = bisect.bisect_right(chunk, k) - 1     # the only folder that can hold k (none nest)
                    if i >= 0 and k.startswith(chunk[i]):
                        keep.append(o)
                objs = keep
            if objs:
                _put(q, objs, stop)
            tok = page.get('NextContinuationToken')
            if end or not page.get('IsTruncated') or not tok:
                break
            kw['ContinuationToken'] = tok
    except BaseException as e:  # noqa: BLE001 - re-raised by objects() in the caller's thread
        _put(q, e, stop)
    finally:
        _put(q, _DONE, stop)


def objects(client, bucket, prefix='', workers=8, cancelled=None, max_pages=MAX_PAGES):
    """Yield every object below `prefix` once (the S3 Contents dicts: Key, Size, LastModified, ...), in no set
    order, listed `workers` folders at a time. Memory stays bounded (pages wait in a short queue); stops when
    `cancelled()` is true or the generator is closed (a cap reached), and a listing error is raised here."""
    workers = max(1, int(workers))
    stop = threading.Event()
    q = queue.Queue(workers * 4)
    ex = ThreadPoolExecutor(workers, thread_name_prefix='s3list')
    try:
        level, direct = _split(client, bucket, prefix, workers, max_pages, ex)
        yield from direct
        runs = _chunks(level, workers * 4)
        for i, chunk in enumerate(runs):
            nxt = runs[i + 1][0] if len(chunk) > 1 and i + 1 < len(runs) else None
            ex.submit(_lister, client, bucket, chunk, nxt, q, stop)
        left = len(runs)
        while left:
            if cancelled is not None and cancelled():
                return
            try:
                x = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if x is _DONE:
                left -= 1
            elif isinstance(x, BaseException):
                raise x
            else:
                yield from x
    finally:
        stop.set()
        while True:                                    # free a lister blocked on a full queue
            try:
                q.get_nowait()
            except queue.Empty:
                break
        ex.shutdown(wait=False, cancel_futures=True)
