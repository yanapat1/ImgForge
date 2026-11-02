"""
Progressive listing of one S3 folder for the browser. A folder of 100k objects is 100 list requests in a row, so
`ls` does not wait for all of them: a background fill pages through the folder into a DirListing, the request
answers with what is there after a short wait (partial: true) and the page asks again until it is done. Later
pages ("N more", another sort or filter) are served from the same listing, never by listing the folder again.

Many folders at once: at most FILLERS folders fill side by side (the rest wait in the pool's queue), a folder
opened in several tabs shares one listing, and the cache keeps at most KEEP listings / TOTAL entries (the oldest
finished listing goes first; a running one is never dropped). invalidate stops the fills it forgets, so a
folder is never listed twice side by side. Each list request also goes through s3list's
process-wide request budget.
"""

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

FILLERS = 4             # folders listed at once
KEEP = 16               # listings kept
TOTAL = 1_000_000       # entries kept over all listings
MAX_ENTRIES = 500_000   # one folder's listing stops here (capped)
FRESH = 60              # a finished listing is reused this many seconds

_pool = ThreadPoolExecutor(FILLERS, thread_name_prefix='s3dir')
_cache = OrderedDict()  # key -> DirListing, oldest first
_lock = threading.Lock()


class DirListing:
    """The entries of one folder as they arrive: dirs {name: entry}, files [entry]; done once the fill ended
    (error: the exception that ended it early; capped: stopped at MAX_ENTRIES; stop: set by invalidate, the fill
    ends at its next page)."""

    def __init__(self):
        self.dirs = {}
        self.files = []
        self.done = False
        self.error = None
        self.capped = False
        self.finished = None
        self.stop = False
        self.cond = threading.Condition()

    def size(self):
        return len(self.dirs) + len(self.files)

    def wait(self, timeout):
        """Wait up to `timeout` s for the fill to end -> done."""
        with self.cond:
            self.cond.wait_for(lambda: self.done, timeout)
            return self.done

    def snapshot(self):
        """(dirs, files, done, error) as lists, consistent with each other."""
        with self.cond:
            return list(self.dirs.values()), list(self.files), self.done, self.error


def _fill(listing, pages):
    try:
        if listing.stop:                # forgotten while it waited in the pool's queue
            return
        for dirs, files in pages():
            if listing.stop:
                break
            with listing.cond:
                for d in dirs:
                    listing.dirs.setdefault(d['name'], d)
                listing.files.extend(files)
                full = listing.size() >= MAX_ENTRIES
                listing.capped = full
                listing.cond.notify_all()
            if full:
                break
    except Exception as e:  # noqa: BLE001 - kept on the listing: the request that reads it raises it
        listing.error = e
    finally:
        with listing.cond:
            listing.done = True
            listing.finished = time.monotonic()
            listing.cond.notify_all()


def _evict():
    """Drop the oldest finished listings beyond KEEP / TOTAL (under _lock)."""
    total = sum(x.size() for x in _cache.values())
    for k in list(_cache):
        if len(_cache) <= KEEP and total <= TOTAL:
            return
        x = _cache[k]
        if x.done:
            total -= x.size()
            del _cache[k]


def get(key, pages):
    """The listing of folder `key` (key[0] = the bucket id): the cached one while it runs or is FRESH, else a new
    one filled from `pages()` (an iterator of (dirs, files) per S3 page, which owns its client)."""
    with _lock:
        x = _cache.get(key)
        if x is not None and (not x.done or (x.error is None and time.monotonic() - x.finished < FRESH)):
            _cache.move_to_end(key)
            return x
        x = DirListing()
        _cache[key] = x
        _evict()
    _pool.submit(_fill, x, pages)
    return x


def invalidate(bid, rel=None):
    """Forget the listings of bucket `bid` (only folder `rel` and the folders above it when given): the next ls
    lists S3 again."""
    with _lock:
        for k in list(_cache):
            if k[0] == bid and (rel is None or rel == k[1] or rel.startswith(k[1] + '/') or not k[1]):
                _cache[k].stop = True
                del _cache[k]
