"""Data Center: the parallel S3 listing (datacenter/s3list.py) yields every object below the prefix exactly once
(nested folders with objects at every level, a flat folder too big to split, many folders listed in runs, folder
marker objects), stops on cancel / close without leaving threads, and raises a listing error; against moto too."""

import threading
import time

import pytest

from dataviz.datacenter import s3list


class FakeS3:
    """list_objects_v2 with Prefix / Delimiter / StartAfter / ContinuationToken, `page` entries per page."""

    def __init__(self, keys, page=7, fail=None):
        self.keys = sorted(set(keys))
        self.page = page
        self.fail = fail
        self.calls = 0
        self._lock = threading.Lock()

    def list_objects_v2(self, Bucket, Prefix='', Delimiter=None, StartAfter=None, ContinuationToken=None,
                        MaxKeys=1000):
        with self._lock:
            self.calls += 1
        if self.fail and Prefix.startswith(self.fail):
            raise RuntimeError(f'boom {Prefix}')
        after = ContinuationToken or StartAfter or ''
        entries, seen = [], set()
        for k in self.keys:
            if not k.startswith(Prefix) or k <= after:
                continue
            rest = k[len(Prefix):]
            if Delimiter and Delimiter in rest:
                cp = Prefix + rest[:rest.index(Delimiter) + 1]
                if cp in seen or cp <= after:
                    continue
                seen.add(cp)
                entries.append(('p', cp))
            else:
                entries.append(('k', k))
            if len(entries) > self.page:
                break
        more = len(entries) > self.page
        entries = entries[:self.page]
        out = {'Contents': [{'Key': k, 'Size': len(k)} for t, k in entries if t == 'k'],
               'CommonPrefixes': [{'Prefix': k} for t, k in entries if t == 'p'], 'IsTruncated': more}
        if more:
            out['NextContinuationToken'] = entries[-1][1]
        return out


def _tree():
    keys = ['top.txt', 'a/', 'a/x.jpg']
    keys += [f'flat/{i:04d}.jpg' for i in range(150)]                     # > MAX_PAGES pages: not split
    keys += [f'img/{c}/{i}.jpg' for c in 'abcdefghij' for i in range(5)]
    keys += [f'img/{c}/deep/{i}.txt' for c in 'ace' for i in range(3)]
    keys += ['img/loose.jpg', 'img/a!b.jpg', 'imgx.jpg', 'img0/q.jpg']
    keys += [f'many/{i:03d}/f.jpg' for i in range(120)] + ['many/root.jpg']   # many folders: runs
    return keys


def _all(c, prefix=''):
    return sorted(k for k in c.keys if k.startswith(prefix))


@pytest.mark.parametrize('workers', [1, 2, 8])
@pytest.mark.parametrize('prefix', ['', 'img/', 'many/', 'flat/', 'nope/'])
def test_every_object_once(workers, prefix):
    c = FakeS3(_tree())
    got = [o['Key'] for o in s3list.objects(c, 'b', prefix, workers=workers)]
    assert len(got) == len(set(got))
    assert sorted(got) == _all(c, prefix)


def test_many_folders_listed_in_runs_cost_about_a_plain_walk():
    keys = [f'd{i:04d}/{j}.jpg' for i in range(2000) for j in range(2)]
    c = FakeS3(keys, page=100)
    got = sorted(o['Key'] for o in s3list.objects(c, 'b', '', workers=4))
    assert got == sorted(keys)
    plain = len(keys) // 100
    assert c.calls < plain * 1.5 + 40, c.calls        # runs, not one listing per folder


def _threads():
    return [t for t in threading.enumerate() if t.name.startswith('s3list')]


def _settle():
    for _ in range(50):
        if not _threads():
            return True
        time.sleep(0.05)
    return False


def test_close_and_cancel_stop_the_threads():
    c = FakeS3([f'd{i:03d}/{j}.jpg' for i in range(100) for j in range(50)], page=5)
    it = s3list.objects(c, 'b', '', workers=4)
    first = [next(it)['Key'] for _ in range(20)]
    assert len(set(first)) == 20
    it.close()
    assert _settle()
    flag = {'n': 0}

    def cancelled():
        flag['n'] += 1
        return flag['n'] > 3
    got = list(s3list.objects(c, 'b', '', workers=4, cancelled=cancelled))
    assert len(got) < len(c.keys)
    assert _settle()


def test_a_listing_error_is_raised():
    c = FakeS3(_tree(), fail='img/')
    with pytest.raises(RuntimeError, match='boom'):
        list(s3list.objects(c, 'b', '', workers=4))
    assert _settle()


def test_many_listings_at_once_share_one_request_budget():
    """30 buckets / folders listed side by side: never more than MAX_REQUESTS list calls in flight, all complete."""
    class Counting(FakeS3):
        live = peak = 0
        guard = threading.Lock()

        def list_objects_v2(self, **kw):
            with Counting.guard:
                Counting.live += 1
                Counting.peak = max(Counting.peak, Counting.live)
            try:
                time.sleep(0.005)
                return super().list_objects_v2(**kw)
            finally:
                with Counting.guard:
                    Counting.live -= 1
    clients = [Counting([f'd{i:02d}/{j}.jpg' for i in range(20) for j in range(30)], page=10) for _ in range(30)]
    out = [None] * len(clients)

    def run(i):
        out[i] = sorted(o['Key'] for o in s3list.objects(clients[i], 'b', '', workers=8))
    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(clients))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert all(o == clients[0].keys for o in out)
    assert 1 < Counting.peak <= s3list.MAX_REQUESTS, Counting.peak
    assert _settle()


pytest_plugins = ['conftest_io']             # fixtures: s3_server, s3_bucket


def test_matches_a_plain_listing_on_moto(s3_bucket):
    from dataviz.data import sources
    client, bucket = s3_bucket
    keys = [f'p/{c}/{i}.jpg' for c in 'abcd' for i in range(3)] + ['p/top.txt', 'p/b/deep/z.txt', 'q.txt']
    for k in keys:
        client.put_object(Bucket=bucket, Key=k, Body=b'x')
    for pre in ('', 'p/'):
        got = [o['Key'] for o in s3list.objects(client, bucket, pre, workers=3)]
        assert len(got) == len(set(got))
        assert sorted(k[len(pre):] for k in got) == sorted(sources.list_objects(client, bucket, pre))


def test_pull_streams_skips_unchanged_and_fetches_big_objects_in_ranges(s3_server, s3_bucket, tmp_path, monkeypatch):
    from dataviz.datacenter import store
    client, bucket = s3_bucket
    for k in ('p/a/1.jpg', 'p/a/2.jpg', 'p/b/3.txt', 'p/.hidden/x', 'p/big.mp4'):
        client.put_object(Bucket=bucket, Key=k, Body=(b'v' * 300 if k.endswith('.mp4') else k.encode()))
    monkeypatch.setattr(store, 'BIG', 100)                    # big.mp4 goes through download_file (ranges)
    view = store.S3View(s3_server, bucket, 'p')
    seen = []
    try:
        res = view.pull(tmp_path, lambda *a, **k: seen.append(a or k))
        assert res == {'downloaded': 4, 'skipped': 0, 'bytes': 300 + 3 * len('p/a/1.jpg'), 'failed': [], 'total': 4}
        assert (tmp_path / 'big.mp4').read_bytes() == b'v' * 300 and (tmp_path / 'b' / '3.txt').read_bytes() == b'p/b/3.txt'
        assert not (tmp_path / '.hidden').exists() and not list(tmp_path.rglob('*.part'))
        assert seen[-1] == (4, 4, 'downloading', '0.0 MB')
        (tmp_path / 'a' / '1.jpg').write_bytes(b'mine!!!!!')            # same size, new mtime: fetched again
        res = view.pull(tmp_path)
        assert (res['downloaded'], res['skipped']) == (1, 3) and (tmp_path / 'a' / '1.jpg').read_bytes() == b'p/a/1.jpg'
        assert view.pull(tmp_path, cancelled=lambda: True)['downloaded'] == 0
        assert view.keys() == {'a/1.jpg': 9, 'a/2.jpg': 9, 'b/3.txt': 9, 'big.mp4': 300}
    finally:
        view.close()
