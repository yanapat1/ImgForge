"""The "embeddings" session source: folder validation, column types, feature spaces and the index (no database)."""
import csv
import json
import os

import numpy as np
import pytest
from PIL import Image

from dataviz.data.embedding_source import EmbeddingSource, column_types


def _folder(tmp_path, n=30, d=8, rows=None, extra=None, unit=False, images=True):
    rng = np.random.default_rng(0)
    x = rng.normal(size=(n, d)).astype(np.float32)
    if unit:
        x /= np.linalg.norm(x, axis=1, keepdims=True)
    np.save(tmp_path / 'vec.npy', x)
    if images:
        (tmp_path / 'img').mkdir(exist_ok=True)
    rows = rows or []
    if not rows:
        for i in range(n):
            r = {'id': f'r{i}', 'label': 'ab'[i % 2], 'score': str(i / 10), 'kind': 'xyz'[i % 3], 'note': f'free text {i}'}
            if images:
                Image.new('RGB', (40, 30), (i * 5, 0, 0)).save(tmp_path / 'img' / f'{i}.png')
                r['image'] = f'img/{i}.png'
            rows.append({**r, **(extra or {})})
    with open(tmp_path / 'meta.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return EmbeddingSource(tmp_path)


def test_column_types():
    rows = [{'a': '1', 'b': 'x', 'c': f't{i}', 'd': ''} for i in range(60)]
    rows[0]['a'] = ''
    assert column_types(rows, ['a', 'b', 'c', 'd']) == {'a': 'num', 'b': 'cat', 'c': 'text', 'd': 'cat'}


def test_check_summary(tmp_path):
    s = _folder(tmp_path).check()
    assert s['n'] == 30 and s['label'] and s['classes'] == 2 and s['previews'] == ['image']
    assert s['features'] == [{'key': 'byo-vec', 'name': 'vec', 'dim': 8}]
    assert {c['name']: c['type'] for c in s['columns']} == {'score': 'num', 'kind': 'cat', 'note': 'cat'}


def test_check_errors(tmp_path):
    src = _folder(tmp_path)
    np.save(tmp_path / 'bad.npy', np.zeros((5, 3)))
    with pytest.raises(ValueError, match='one row per metadata row'):
        src.check()
    (tmp_path / 'bad.npy').unlink()
    (tmp_path / 'meta.csv').unlink()
    with pytest.raises(ValueError, match='no meta.csv'):
        src.check()


def test_preview_path_cannot_leave_the_folder(tmp_path):
    src = _folder(tmp_path, extra={'audio': '../secret.wav'})
    with pytest.raises(ValueError, match='outside'):
        src.check()
    with pytest.raises(ValueError, match='outside'):
        src.preview_path('../../etc/passwd')
    assert src.preview_path('img/1.png') == tmp_path.resolve() / 'img' / '1.png'


def test_fingerprint_changes_with_files(tmp_path):
    src = _folder(tmp_path)
    fp = src.fingerprint()
    assert src.fingerprint() == fp
    np.save(tmp_path / 'vec.npy', np.ones((30, 8), np.float32))
    os.utime(tmp_path / 'vec.npy', ns=(1, 1))
    assert src.fingerprint() != fp


@pytest.mark.parametrize('unit,metric', [(True, 'cosine'), (False, 'euclidean')])
def test_feature_registry_metric(tmp_path, unit, metric):
    reg = _folder(tmp_path, unit=unit).feature_registry()
    spec = reg['byo-vec']
    assert spec.metric == metric and spec.group == 'provided'
    v = spec.fn([], [], lambda *a, **k: None)
    assert v.shape == (30, 8) and v.dtype == np.float32


def test_build_index(tmp_path):
    src = _folder(tmp_path)
    idx = src.build_index(tmp_path / 'ws', thumb_size=32)
    assert idx['classes'] == ['a', 'b'] and len(idx['samples']) == 30 and idx['modality'] == ['image']
    s = idx['samples'][3]
    assert s['cls'] == 1 and s['row_id'] == 'r3' and s['meta'] == {'score': 0.3, 'kind': 'x', 'note': 'free text 3'}
    assert s['preview'] == {'image': 'img/3.png'}
    assert idx['stats']['thumbs'] == 30
    with Image.open(tmp_path / 'ws' / 'crops' / '000003.jpg') as im:
        assert max(im.size) == 32
    assert json.loads((tmp_path / 'ws' / 'samples.json').read_text())['fingerprint'] == idx['fingerprint']
    assert src.build_index(tmp_path / 'ws') == idx                  # cache hit


def test_no_label_column(tmp_path):
    rows = [{'text': f'hello {i}', 'n': str(i)} for i in range(10)]
    src = _folder(tmp_path, n=10, rows=rows, images=False)
    idx = src.build_index(tmp_path / 'ws')
    assert idx['classes'] == ['all'] and {s['cls'] for s in idx['samples']} == {0}
    assert idx['modality'] == ['text'] and idx['samples'][2]['preview'] == {'text': 'hello 2'}
