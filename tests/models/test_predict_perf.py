import threading
import numpy as np
from PIL import Image

from dataviz.core import hardware
from dataviz.data import sources
from dataviz.models import predict


def test_lru_bound_and_order():
    c = sources.ImageLRU(max_bytes=3 * 10 * 10 * 3)
    for k in 'abc':
        c.put(k, Image.new('RGB', (10, 10)))
    c.get('a')
    c.put('d', Image.new('RGB', (10, 10)))      # evicts b (least recently used)
    assert c.get('b') is None and c.get('a') is not None and c.nbytes <= c.max_bytes
    c.put('huge', Image.new('RGB', (100, 100)))
    assert c.get('huge') is None


def test_cached_rgb_decodes_once(tmp_path):
    sources.clear_image_cache()
    Image.new('RGB', (40, 30), (1, 2, 3)).save(tmp_path / 'a.png')
    calls = []

    class Src:
        uri = 'x'

        def open_image(self, name):
            calls.append(name)
            return Image.open(tmp_path / name)
    src = Src()
    errs = []

    def go():
        try:
            assert sources.cached_rgb(src, 'a.png').size == (40, 30)
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and 1 <= len(calls) <= 8
    n = len(calls)
    sources.cached_rgb(src, 'a.png')
    assert len(calls) == n


def test_oom_halves_then_cpu_fallback(monkeypatch):
    monkeypatch.setattr(hardware, 'device', lambda: 'cuda')
    monkeypatch.setattr(hardware, 'demote_to_cpu', lambda: None)
    sizes = []

    class R:
        def predict(self, b):
            sizes.append(len(b))
            if len(b) > 2:
                raise RuntimeError('CUDA out of memory')
            return [np.zeros((0, 6), np.float32)] * len(b)
    monkeypatch.setattr(predict.runners, 'get_runner', lambda *a: R())
    out = predict.predict_batch({}, 'fp', '.', [None] * 8)
    assert len(out) == 8 and sizes[0] == 8 and max(sizes[1:]) <= 4

    state = {'dev': 'cuda'}
    monkeypatch.setattr(hardware, 'device', lambda: state['dev'])
    monkeypatch.setattr(hardware, 'demote_to_cpu', lambda: state.update(dev='cpu'))

    class R2:
        def predict(self, b):
            if state['dev'] == 'cuda':
                raise RuntimeError('CUDA error: device-side assert')
            return [np.zeros((0, 6), np.float32)] * len(b)
    monkeypatch.setattr(predict.runners, 'get_runner', lambda *a: R2())
    assert len(predict.predict_batch({}, 'fp', '.', [None] * 3)) == 3 and state['dev'] == 'cpu'


def test_pick_batch_size(monkeypatch):
    class R:
        batch_size, max_batch = 16, 4
    monkeypatch.setattr(hardware, 'device', lambda: 'cpu')
    assert predict.pick_batch_size(R(), {}, 'auto') == 4
    assert predict.pick_batch_size(R(), {}, 2) == 2
    R.max_batch = None
    assert predict.pick_batch_size(R(), {}, None) == 16
    assert predict.pick_batch_size(R(), {}, 64) == 64


def test_decode_draft(tmp_path):
    Image.new('RGB', (4000, 3000), (9, 9, 9)).save(tmp_path / 'big.jpg')

    class Src:
        def open_image(self, n):
            return Image.open(tmp_path / n)
    im, size0 = predict._decode(Src(), 'big.jpg', 640, True)
    assert size0 == (4000, 3000) and im.width < 4000 and max(im.size) >= 1280
    im, size0 = predict._decode(Src(), 'big.jpg', 640, False)
    assert im.size == (4000, 3000)
