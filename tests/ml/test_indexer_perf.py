"""Index build: process pool, thread pool and the old full-image-gray maths must give identical output."""
import numpy as np
import cv2
from PIL import Image

from dataviz.data import indexer as ix


def _make(root, n=6):
    (root / 'images').mkdir(parents=True)
    (root / 'labels').mkdir()
    (root / 'classes.txt').write_text('a\nb\n')
    rng = np.random.default_rng(1)
    for i in range(n):
        Image.fromarray((rng.random((120, 160, 3)) * 255).astype(np.uint8)).save(root / 'images' / f'wd_1_image_{i}.jpg')
        (root / 'labels' / f'wd_1_image_{i}.txt').write_text(
            '0 0.5 0.5 0.3 0.3\n1 0.95 0.5 0.2 0.2\nbad line\n')


def _snapshot(res, cache):
    crops = [p.read_bytes() for p in sorted((cache / 'crops').glob('*.jpg'))]
    return res['samples'], res['stats'], crops


def test_process_thread_and_reference_agree(tmp_path):
    _make(tmp_path / 'ds')
    src = ix.LocalSource(tmp_path / 'ds')
    a = ix.build_index(src, tmp_path / 'c1', workers=2)          # process pool
    b = ix.build_index(src, tmp_path / 'c2', workers=1)          # threads
    assert _snapshot(a, tmp_path / 'c1') == _snapshot(b, tmp_path / 'c2')
    assert a['stats']['bad_lines'] == 6 and len(a['samples']) == 12
    # gray of the box region == region of the full-image gray (old behaviour)
    s = a['samples'][1]
    img = np.asarray(Image.open(tmp_path / 'ds' / 'images' / s['image']).convert('RGB'))
    x1, y1, x2, y2 = s['bbox']
    br, bl = ix.box_quality(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)[y1:y2, x1:x2])
    assert (round(br, 2), round(bl, 2)) == (s['brightness'], s['blur'])


def test_resolve_workers():
    assert ix.resolve_workers(3) == 3
    assert 1 <= ix.resolve_workers('auto') <= ix.MAX_AUTO_WORKERS
