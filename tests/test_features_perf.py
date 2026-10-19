"""Feature extraction speed-ups: shared crop cache, prefetching, batched loop, model LRU, mmap cache. CPU only, offline."""
import time

import cv2
import numpy as np
import pytest

from dataviz.ml import features as F


@pytest.fixture()
def crops(tmp_path):
    d = tmp_path / 'ws' / 'crops'
    d.mkdir(parents=True)
    rng = np.random.default_rng(1)
    paths = []
    for i in range(37):
        h, w = rng.integers(20, 120, 2)
        p = d / f'{i:06d}.jpg'
        cv2.imwrite(str(p), rng.integers(0, 255, (h, w, 3), dtype=np.uint8))
        paths.append(p)
    return paths


def test_crop_cache_matches_load_and_is_reused(crops):
    mm = F.crop_cache(crops, 224)
    assert mm.shape == (len(crops), 224, 224, 3) and mm.dtype == np.uint8
    for i in (0, 5, 36):
        assert np.array_equal(mm[i], F._load_u8(crops[i], 224))
    files = list((crops[0].parent.parent / 'features').glob('_crops224_*.npy'))
    assert len(files) == 1
    mtime = files[0].stat().st_mtime_ns
    F.crop_cache(crops, 224)
    assert files[0].stat().st_mtime_ns == mtime            # reused, not rebuilt


def test_crop_cache_invalidated_by_change(crops):
    F.crop_cache(crops, 224)
    time.sleep(0.01)
    cv2.imwrite(str(crops[3]), np.full((50, 50, 3), 7, np.uint8))
    mm = F.crop_cache(crops, 224)
    assert np.array_equal(mm[3], F._load_u8(crops[3], 224))
    assert len(list((crops[0].parent.parent / 'features').glob('_crops224_*.npy'))) == 1   # stale one removed


def test_prefetch_order_and_errors():
    assert list(F._prefetch(lambda i: i * 2, 20)) == [i * 2 for i in range(20)]

    def boom(i):
        if i == 3:
            raise ValueError('x')
        return i
    with pytest.raises(ValueError):
        list(F._prefetch(boom, 10))


def test_batched_torch_matches_reference(crops):
    torch = pytest.importorskip('torch')
    mean, std = F.IMAGENET_MEAN, F.IMAGENET_STD
    w = torch.randn(3 * 8 * 8, 16, generator=torch.Generator().manual_seed(0))

    def forward(x):                                    # tiny "model": pool to 8x8 and project
        return torch.nn.functional.adaptive_avg_pool2d(x.float(), 8).flatten(1) @ w
    got = F._batched_torch(crops, lambda *a, **k: None, forward, mean, std, batch_size=8)
    ref = np.stack([F._load(p, 224, mean, std) for p in crops])
    ref = forward(torch.from_numpy(ref)).numpy()
    ref = F.l2_normalize(ref)
    assert got.shape == (len(crops), 16)
    cos = (got * ref).sum(1)
    assert cos.min() > 0.999
    assert np.allclose(np.linalg.norm(got, axis=1), 1, atol=1e-4)


def test_batched_torch_empty():
    assert F._batched_torch([], lambda *a, **k: None, lambda x: x, F.IMAGENET_MEAN, F.IMAGENET_STD).shape[0] == 0


def test_model_lru_keeps_two(monkeypatch):
    torch = pytest.importorskip('torch')
    F._MODELS.clear()
    built = []

    def build(k):
        built.append(k)
        return torch.nn.Linear(1, 1)
    for k in ('a', 'b', 'a', 'c', 'a'):
        F._resident(k, lambda k=k: build(k))
    assert built == ['a', 'b', 'c']                    # 'a' stayed resident, 'b' was evicted
    assert len(F._MODELS) == 2
    F._release_models()
    assert not F._MODELS


def test_cached_features_are_memmap_and_replace_is_safe(tmp_path):
    emb = np.random.default_rng(0).random((10, 4)).astype(np.float32)
    F.save_cached(tmp_path, 'k', 'fp', emb)
    mm = F.load_cached(tmp_path, 'k', 'fp', 10)
    assert isinstance(mm, np.memmap) and np.array_equal(mm, emb)
    F.save_cached(tmp_path, 'k', 'fp', emb * 2)        # atomic replace: the open memmap keeps the old data
    assert np.array_equal(mm, emb)
    assert np.array_equal(F.load_cached(tmp_path, 'k', 'fp', 10), emb * 2)
    assert F.load_cached(tmp_path, 'k', 'other', 10) is None


def test_map_cpu_keeps_order(crops):
    out = F._map_cpu(lambda p: np.array([int(p.stem)], np.float32), crops, lambda *a, **k: None, 's')
    assert out[:, 0].tolist() == list(range(len(crops)))
