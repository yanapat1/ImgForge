"""Donor reuse (data/reuse.py): a re-index takes unchanged images from an earlier index (no decode, crops
hard-linked, same samples as a full rebuild) and the feature matrix takes the donor's rows."""

import os

import numpy as np

from conftest import make_jpeg
from vizutil import _Job, geom_registry, make_yolo

from dataviz.data import indexer, reuse
from dataviz.data.sources import LocalSource


def _count(monkeypatch):
    calls = []
    real_one, real_path = indexer._index_one, indexer._index_one_path

    def one(source, label_name, *a, **k):
        calls.append(label_name)
        return real_one(source, label_name, *a, **k)

    def path(p, label_name, *a, **k):
        calls.append(label_name)
        return real_path(p, label_name, *a, **k)
    monkeypatch.setattr(indexer, '_index_one', one)
    monkeypatch.setattr(indexer, '_index_one_path', path)
    return calls


def _strip(samples):
    return [{k: v for k, v in s.items()} for s in samples]


def test_reindex_reuses_unchanged_images(tmp_path, monkeypatch):
    root = make_yolo(tmp_path / 'ds', boxes=(('a', '0 0.5 0.5 0.4 0.4\n1 0.3 0.3 0.2 0.2\n'),
                                             ('b', '1 0.5 0.5 0.2 0.2\n'), ('c', '0 0.4 0.6 0.3 0.3\n')))
    ws1, ws2, ws3 = tmp_path / 'w1', tmp_path / 'w2', tmp_path / 'w3'
    first = indexer.build_index(LocalSource(root), ws1, workers=1)
    assert first['params']['thumb_size'] == 224 and all('yolo' in s for s in first['samples'])
    # relabel one box of a (class only) + a new image d with a box
    (root / 'labels' / 'a.txt').write_text('1 0.5 0.5 0.4 0.4\n1 0.3 0.3 0.2 0.2\n')
    make_jpeg(root / 'images' / 'd.jpg', 64, 48, color=(1, 2, 3))
    (root / 'labels' / 'd.txt').write_text('0 0.5 0.5 0.5 0.5\n')

    calls = _count(monkeypatch)
    donor = reuse.Donor.from_workspace(ws1)
    assert donor is not None
    got = indexer.build_index(LocalSource(root), ws2, workers=1, donor=donor)
    assert calls == ['d.txt']                                        # only the new image decoded
    full = indexer.build_index(LocalSource(root), ws3, workers=1, rebuild=True)
    assert _strip(got['samples']) == _strip(full['samples'])
    assert got['samples'][0]['cls'] == 1 and got['samples'][0]['cls_name'] == 'dog'
    # crops are hard links of the donor's
    a0 = got['samples'][0]['id']
    assert os.stat(ws2 / 'crops' / f'{a0:06d}.jpg').st_ino == os.stat(ws1 / 'crops' / '000000.jpg').st_ino
    src, dws, dfp, dn = reuse.load_map(ws2)
    assert dws == ws1 and dfp == first['fingerprint'] and dn == len(first['samples'])
    d_new = [s['id'] for s in got['samples'] if s['image'] == 'd.jpg']
    assert [int(v) for v in src] == [0, 1, 2, 3, -1] and d_new == [4]
    # images.json: the reused images keep their hash
    from dataviz.data import imagehash
    imagehash.ensure(LocalSource(root), ws1, first['fingerprint'])       # the backfill hashes ws1's images
    calls.clear()
    got = indexer.build_index(LocalSource(root), ws2, workers=1, rebuild=True, donor=reuse.Donor.from_workspace(ws1))
    assert calls == ['d.txt']
    h1, h2 = imagehash.load(ws1)['images'], imagehash.load(ws2)['images']
    assert h2['a.jpg']['q'] == h1['a.jpg']['q'] and h2['a.jpg']['n'] == 2


def test_moved_box_or_changed_image_is_decoded(tmp_path, monkeypatch):
    root = make_yolo(tmp_path / 'ds', boxes=(('a', '0 0.5 0.5 0.4 0.4\n'), ('b', '1 0.5 0.5 0.2 0.2\n')))
    indexer.build_index(LocalSource(root), tmp_path / 'w1', workers=1)
    (root / 'labels' / 'a.txt').write_text('0 0.5 0.5 0.41 0.4\n')            # moved: a new geometry
    make_jpeg(root / 'images' / 'b.jpg', 64, 48, color=(250, 0, 0))            # new pixels
    os.utime(root / 'images' / 'b.jpg', ns=(1, 1))
    calls = _count(monkeypatch)
    indexer.build_index(LocalSource(root), tmp_path / 'w2', workers=1,
                        donor=reuse.Donor.from_workspace(tmp_path / 'w1'))
    assert sorted(calls) == ['a.txt', 'b.txt']
    # other params: never reused
    calls.clear()
    indexer.build_index(LocalSource(root), tmp_path / 'w3', workers=1, thumb_size=96,
                        donor=reuse.Donor.from_workspace(tmp_path / 'w2'))
    assert sorted(calls) == ['a.txt', 'b.txt']


def test_feature_transplant_computes_missing_rows(tmp_path):
    from dataviz.ml import features as feats
    from dataviz.state import State
    root = make_yolo(tmp_path / 'ds', boxes=(('a', '0 0.5 0.5 0.4 0.4\n1 0.3 0.3 0.2 0.2\n'),
                                             ('b', '1 0.5 0.5 0.2 0.2\n')))
    calls = []
    reg = feats.FeatureRegistry(geom_registry(calls))
    st1 = State(LocalSource(root), tmp_path / 'w1', reg, default_feature='geom')
    st1._index_job(_Job())
    st1.get_features('geom', _Job())
    assert calls == [3]
    make_jpeg(root / 'images' / 'c.jpg', 64, 48, color=(5, 5, 5))
    (root / 'labels' / 'c.txt').write_text('0 0.5 0.5 0.6 0.3\n')
    indexer.build_index(LocalSource(root), tmp_path / 'w2', donor=reuse.Donor.from_workspace(tmp_path / 'w1'))
    st2 = State(LocalSource(root), tmp_path / 'w2', reg, default_feature='geom')
    st2._index_job(_Job())
    calls.clear()
    emb = np.asarray(st2.get_features('geom', _Job()))
    assert calls == [1]                                              # only the new box
    want = np.asarray([[s['area'], s['aspect']] for s in st2.samples], np.float32)
    assert np.allclose(emb, want)
    assert feats.load_cached(tmp_path / 'w2' / 'features', 'geom', st2.fingerprint, 4) is not None


def test_previous_workspace_excludes_current(tmp_path):
    root = make_yolo(tmp_path / 'ds')
    import json
    for i, name in enumerate(('old', 'cur')):
        d = tmp_path / 'ws' / name
        d.mkdir(parents=True)
        (d / 'samples.json').write_text('{}')
        (d / 'workspace.json').write_text(json.dumps({'dataset': 'label://p/t', 'last_opened': f'2026-01-0{i + 1}'}))
    assert reuse.previous_workspace(tmp_path / 'ws', 'label://p/t', exclude=tmp_path / 'ws' / 'cur') == \
        tmp_path / 'ws' / 'old'
    assert reuse.Donor.from_workspace(tmp_path / 'ws' / 'old') is None          # no params / yolo: unusable
    assert root.exists()
