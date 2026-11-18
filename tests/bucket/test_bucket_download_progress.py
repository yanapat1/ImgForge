"""Byte progress of an S3 object download (video -> frames from an S3 link): S3View.download reports each chunk with
the object's size, and the frames job shows it in its `extra` text while its done / total keep counting frames."""
import io

import pytest

from dataviz.bucket import service, store


class _Client:
    def __init__(self, data, length=True):
        self.data, self.length = data, length

    def get_object(self, Bucket, Key):
        out = {'Body': io.BytesIO(self.data)}
        if self.length:
            out['ContentLength'] = len(self.data)
        return out


def _view(data, length=True):
    v = object.__new__(store.S3View)
    v.client, v.bucket, v.base = _Client(data, length), 'bkt', 'pre/'
    return v


def test_download_reports_every_chunk(tmp_path):
    data = b'x' * (store.CHUNK * 2 + 123)
    seen = []
    dest = tmp_path / 'clip.mp4'
    _view(data).download('clip.mp4', str(dest), lambda n, total: seen.append((n, total)))
    assert dest.read_bytes() == data
    assert sum(n for n, _ in seen) == len(data) and len(seen) == 3
    assert {t for _, t in seen} == {len(data)}


def test_download_without_size_or_callback(tmp_path):
    data = b'abc' * 1000
    seen = []
    _view(data, length=False).download('a.mp4', str(tmp_path / 'a.mp4'), lambda n, total: seen.append(total))
    assert seen == [None]
    _view(data).download('b.mp4', str(tmp_path / 'b.mp4'))                       # no callback: a plain copy
    assert (tmp_path / 'b.mp4').read_bytes() == data


def test_meter_text_and_cancel(tmp_path):
    calls = []
    prog = lambda **k: calls.append(k)
    on = service._download_meter(prog, 'downloading clip.mp4', lambda: False)
    on(500_000, 2_000_000)
    assert calls[-1]['stage'] == 'downloading clip.mp4'
    assert calls[-1]['extra'].startswith('25% · 500.0 KB of 2.0 MB · ') and '/s' in calls[-1]['extra']

    stop = service._download_meter(prog, 'downloading clip.mp4', lambda: True)
    dest = tmp_path / 'c.mp4'
    with pytest.raises(store.Cancelled):
        _view(b'y' * 10).download('c.mp4', str(dest), stop)
    assert not dest.exists() and not list(tmp_path.glob('c.mp4.part-*'))       # no half file left behind
