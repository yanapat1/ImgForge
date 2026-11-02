"""Data Center: the progressive S3 folder listing (datacenter/s3dir.py): a request sees the entries listed so far
while the fill runs, one listing is shared and reused (no second listing), an error is kept for the reader, and
the cache drops the oldest finished listings, never a running one; invalidate forgets a bucket's folders."""

import threading

import pytest

from dataviz.datacenter import s3dir


def _pages(n_pages, per=3, gate=None, fail_at=None, calls=None):
    def gen():
        if calls is not None:
            calls.append(1)
        for p in range(n_pages):
            if gate is not None:
                gate.wait(5)
            if fail_at == p:
                raise RuntimeError('S3 went away')
            yield ([{'name': f'd{p}', 'type': 'dir'}] if p == 0 else [],
                   [{'name': f'f{p}-{i}', 'type': 'file'} for i in range(per)])
    return gen


@pytest.fixture(autouse=True)
def _clean():
    s3dir._cache.clear()
    yield
    s3dir._cache.clear()


def test_partial_then_done_and_shared():
    gate, calls = threading.Event(), []
    x = s3dir.get(('b1', ''), _pages(3, gate=gate, calls=calls))
    assert not x.wait(0.05) and x.size() == 0
    assert s3dir.get(('b1', ''), _pages(3, calls=calls)) is x          # running: shared, not listed twice
    gate.set()
    assert x.wait(5)
    dirs, files, done, err = x.snapshot()
    assert done and err is None and [d['name'] for d in dirs] == ['d0'] and len(files) == 9
    assert s3dir.get(('b1', ''), _pages(3, calls=calls)) is x          # fresh: reused
    assert len(calls) == 1


def test_error_is_kept_and_the_next_get_lists_again():
    x = s3dir.get(('b2', ''), _pages(3, fail_at=1))
    assert x.wait(5) and isinstance(x.error, RuntimeError) and x.size() == 4
    y = s3dir.get(('b2', ''), _pages(1))
    assert y is not x and y.wait(5) and y.error is None


def test_cap_eviction_and_invalidate(monkeypatch):
    monkeypatch.setattr(s3dir, 'MAX_ENTRIES', 5)
    x = s3dir.get(('b3', 'big'), _pages(10))
    assert x.wait(5) and x.capped and x.size() == 7                    # stops at the first page past the cap
    monkeypatch.setattr(s3dir, 'KEEP', 2)
    gate = threading.Event()
    running = s3dir.get(('b4', ''), _pages(1, gate=gate))
    for i in range(3):
        s3dir.get(('b5', f'p{i}'), _pages(1)).wait(5)
    assert ('b4', '') in s3dir._cache                                   # a running listing is never dropped
    assert len(s3dir._cache) <= 3 and ('b3', 'big') not in s3dir._cache
    gate.set()
    running.wait(5)
    s3dir.get(('b6', 'a/b'), _pages(1)).wait(5)
    s3dir.get(('b6', 'c'), _pages(1)).wait(5)
    s3dir.invalidate('b6', 'a/b/x.jpg')
    assert ('b6', 'a/b') not in s3dir._cache and ('b6', 'c') in s3dir._cache
    s3dir.invalidate('b6')
    assert not any(k[0] == 'b6' for k in s3dir._cache)


def test_invalidate_stops_the_running_fill():
    gate, pulled = threading.Event(), []

    def pages():
        for p in range(50):
            gate.wait(5)
            pulled.append(p)
            yield [], [{'name': f'f{p}', 'type': 'file'}]
    x = s3dir.get(('b7', ''), pages)
    s3dir.invalidate('b7')
    gate.set()
    assert x.wait(5) and len(pulled) <= 1                  # no orphaned fill paging on beside the next listing
