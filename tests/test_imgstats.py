"""data/imgstats.py: each metric moves the right way on a synthetic scene (darker, blurred, noisy, tinted,
recompressed), the JPEG quality estimate, EXIF camera facts, the per-member cache (a second run reads nothing, a
changed sig re-measures, a sample is deterministic and nested) and a bad file is skipped."""

import io

import cv2
import numpy as np
import pytest
from PIL import Image

from dataviz.data import imgstats as S


def scene(w=800, h=600, seed=0):
    rng = np.random.default_rng(seed)
    a = np.zeros((h, w, 3), np.uint8)
    a[:] = (110, 120, 100)
    for _ in range(40):                                      # rectangles: edges, tones and colours
        x, y = int(rng.integers(0, w - 60)), int(rng.integers(0, h - 60))
        a[y:y + int(rng.integers(20, 120)), x:x + int(rng.integers(20, 120))] = rng.integers(30, 220, 3)
    return a


def jpeg(a, q=92, exif=None):
    b = io.BytesIO()
    im = Image.fromarray(a)
    im.save(b, 'JPEG', quality=q, **({'exif': exif} if exif is not None else {}))
    return b.getvalue()


def png(a):
    b = io.BytesIO()
    Image.fromarray(a).save(b, 'PNG')
    return b.getvalue()


def m(data, key):
    return float(S.measure(data)[0][S.COL[key]])


def test_metrics_move_the_right_way():
    a = scene()
    base = png(a)
    dark = png((a * 0.5).astype(np.uint8))
    assert m(dark, 'brightness') < 0.6 * m(base, 'brightness')
    assert m(dark, 'contrast') < m(base, 'contrast')
    blurred = png(cv2.GaussianBlur(a, (0, 0), 3))
    assert m(blurred, 'sharpness') < 0.3 * m(base, 'sharpness')
    assert m(blurred, 'tenengrad') < m(base, 'tenengrad')
    rng = np.random.default_rng(1)
    n = rng.normal(0, 10, a.shape[:2] + (1,))                 # the same noise on every channel = luma sigma 10
    noisy = png(np.clip(a + n, 0, 255).astype(np.uint8))
    assert m(base, 'noise') < 1.5
    assert 7.5 < m(noisy, 'noise') < 12.5
    soft_noisy = png(np.clip(cv2.GaussianBlur(a.astype(np.float32), (0, 0), 1.6) + rng.normal(0, 7, a.shape[:2] + (1,)), 0, 255)
                     .astype(np.uint8))                     # a soft, noisy camera must not read as sharp
    assert m(soft_noisy, 'sharpness') < 0.3 * m(base, 'sharpness')
    assert m(soft_noisy, 'tenengrad') < m(base, 'tenengrad')
    assert abs(m(noisy, 'sharpness') / m(base, 'sharpness') - 1) < 0.15
    blue = a.astype(np.int16)
    blue[..., 2] += 40
    blue = png(np.clip(blue, 0, 255).astype(np.uint8))
    assert m(blue, 'cast_b') < m(base, 'cast_b') - 5
    assert m(blue, 'wb_bg') > m(base, 'wb_bg')
    grey = png(np.repeat(cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)[..., None], 3, 2))
    assert m(grey, 'saturation') < 1 and m(grey, 'colorfulness') < 1
    v = S.measure(base)[0]
    assert v[S.COL['width']] == 800 and v[S.COL['height']] == 600
    assert np.isnan(v[S.COL['jpeg_q']]) and np.isnan(v[S.COL['iso']])


def test_hist_shares_sum_to_one():
    _, h, _, _ = S.measure(png(scene()))
    for k in range(4):
        assert abs(h[k * S.NHIST:(k + 1) * S.NHIST].sum() - 1) < 1e-4


@pytest.mark.parametrize('q', [30, 50, 75, 90])
def test_jpeg_quality(q):
    assert abs(m(jpeg(scene(), q), 'jpeg_q') - q) <= 3


def test_exif_camera():
    ex = Image.Exif()
    ex[0x010F] = 'Acme'
    ex[0x0110] = 'Cam X1'
    sub = ex.get_ifd(0x8769)
    sub[0x8827] = 800
    _, _, cam, fmt = S.measure(jpeg(scene(), exif=ex.tobytes()))
    assert cam == 'Acme Cam X1' and fmt == 'JPEG'
    v = S.measure(jpeg(scene(), exif=ex.tobytes()))[0]
    assert v[S.COL['iso']] == 800


def test_large_jpeg_uses_draft_but_reports_real_size():
    a = cv2.resize(scene(), (4000, 3000), interpolation=cv2.INTER_NEAREST)
    v = S.measure(jpeg(a))[0]
    assert v[S.COL['width']] == 4000 and v[S.COL['height']] == 3000


def test_cache_sample_and_bad_file(tmp_path):
    files = {f'img{i}.png': png((scene(seed=i) * (0.5 + i / 20)).clip(0, 255).astype(np.uint8)) for i in range(8)}
    files['bad.jpg'] = b'not an image'
    sigs = {n: 's1' for n in files}
    reads = []

    def rd(n):
        reads.append(n)
        return files[n]

    t = S.ensure(tmp_path, sorted(files), rd, sigs.get, workers=2)
    assert len(t.names) == 8 and t.failed == 1 and t.measured == 8
    assert 'bad.jpg' not in t.names and t.vec.shape == (8, len(S.KEYS))
    reads.clear()
    t2 = S.ensure(tmp_path, sorted(files), rd, sigs.get)
    assert reads == ['bad.jpg']                              # every good image came from the cache
    np.testing.assert_allclose(t2.vec, t.vec)
    sigs['img3.png'] = 's2'
    reads.clear()
    S.ensure(tmp_path, sorted(files), rd, sigs.get)
    assert sorted(reads) == ['bad.jpg', 'img3.png']
    names = [f'n{i}' for i in range(100)]
    s10, s20 = S.sample(names, 10), S.sample(names, 20)
    assert len(s10) == 10 and set(s10) <= set(s20) and S.sample(names, 10) == s10
    assert S.sample(names, 0) == names
