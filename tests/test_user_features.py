"""Your own models as feature spaces (dataviz/ml/user_features.py): pooling model outputs, the per-session feature
registry overlay, a custom torch model embedding crops through a forward hook or its own output, the embed settings
of Settings › Models (hf hub ids for everyone, server files for admins only) and the owner's model list."""

import numpy as np
import pytest

from conftest import make_jpeg
from dataviz.ml import features as feats, user_features as uf


def test_pool_shapes():
    assert uf.pool(np.ones((2, 8, 3, 3))).shape == (2, 8)                     # conv map: average over H, W
    assert uf.pool(np.ones((2, 8, 3, 3)), 'flatten').shape == (2, 72)
    tok = np.arange(2 * 5 * 4, dtype=np.float32).reshape(2, 5, 4)
    np.testing.assert_array_equal(uf.pool(tok, 'cls'), tok[:, 0])            # token sequence: CLS or mean
    np.testing.assert_allclose(uf.pool(tok), tok.mean(1))
    assert uf.pool({'last_hidden_state': tok, 'pooler_output': None}).shape == (2, 4)
    assert uf.pool([np.ones((2, 3, 4, 4)), np.ones((2, 6, 2, 2))]).shape == (2, 6)     # multi-scale: the deepest
    with pytest.raises(ValueError):
        uf.pool(object())


def test_feature_registry_overlay():
    base = {'geom': 'G'}
    extra = {'model:x:1': 'M'}
    reg = feats.FeatureRegistry(base, lambda: extra)
    assert reg['geom'] == 'G' and reg['model:x:1'] == 'M' and 'model:x:1' in reg and 'nope' not in reg
    assert list(reg) == ['geom', 'model:x:1'] and len(reg) == 2 and reg.get('nope') is None
    extra.clear()
    assert 'model:x:1' not in reg and list(reg.values()) == ['G']
    assert list(feats.FeatureRegistry(base)) == ['geom']


def test_feature_key_follows_weights_and_settings():
    m = {'id': 'abc', 'family': 'custom', 'embed': {'enabled': True, 'layer': '0'}}
    k = uf.feature_key(m, 'fp1')
    assert k.startswith('model:abc:') and k == uf.feature_key(m, 'fp1')
    assert k != uf.feature_key(m, 'fp2')                                       # retrained weights
    assert k != uf.feature_key({**m, 'embed': {'enabled': True, 'layer': '2'}}, 'fp1')
    assert uf.feature_key({**m, 'embed': {'enabled': False, 'layer': '0'}}, 'fp1') == k


@pytest.fixture
def crops(tmp_path):
    paths = []
    for i in range(5):
        p = tmp_path / 'crops' / f'{i:06d}.jpg'
        make_jpeg(p, 40 + 7 * i, 30, color=(30 * i, 90, 200 - 30 * i))
        paths.append(p)
    return paths


@pytest.mark.parametrize('layer,dim', [('0', 6), ('', 4)])
def test_custom_torch_model_embeds(tmp_path, crops, layer, dim):
    torch = pytest.importorskip('torch')
    from dataviz.models import runners
    net = torch.nn.Sequential(torch.nn.Conv2d(3, 6, 3), torch.nn.ReLU(), torch.nn.AdaptiveAvgPool2d(1),
                              torch.nn.Flatten(), torch.nn.Linear(6, 4))
    path = tmp_path / 'net.pt'
    torch.save(net, path)
    m = {'id': f'n{dim}', 'name': 'tiny', 'family': 'custom', 'format': 'pt', 'path': str(path), 'imgsz': 64,
         'embed': {'enabled': True, 'layer': layer, 'pool': 'avg', 'imgsz': 64}}
    spec = uf.spec_for(m, 'fp', tmp_path / 'work')
    try:
        emb = spec.fn(crops, [{}] * len(crops), lambda *a, **k: None)
    finally:
        runners.release()
    assert emb.shape == (5, dim) and emb.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(emb, axis=1), 1, atol=1e-5)
    assert spec.group == 'model' and str(path) not in spec.note             # members never see the server path


def test_bad_layer_is_explained(tmp_path, crops):
    torch = pytest.importorskip('torch')
    from dataviz.models import runners
    path = tmp_path / 'net.pt'
    torch.save(torch.nn.Sequential(torch.nn.Conv2d(3, 2, 3)), path)
    m = {'id': 'bad', 'name': 'tiny', 'family': 'custom', 'format': 'pt', 'path': str(path),
         'embed': {'enabled': True, 'layer': 'head'}}
    with pytest.raises(ValueError, match='no layer'):
        try:
            uf.spec_for(m, 'fp', tmp_path).fn(crops, [{}] * 5, lambda *a, **k: None)
        finally:
            runners.release()
    with pytest.raises(ValueError):
        uf.spec_for({**m, 'format': 'api'}, 'fp', tmp_path)


# ---------------------------------------------------------------- Settings › Models (database)

@pytest.fixture
def reg(pg, tmp_path):
    from dataviz.models.registry import ModelRegistry
    return ModelRegistry(tmp_path, allowed_roots=[tmp_path])


def test_embed_settings_saved_and_checked(reg, tmp_path):
    from authutil import account
    from dataviz.models import runners
    bob, root = account('bob'), account('root', admin=True)
    m = reg.upsert({'name': 'dino', 'family': 'hf', 'format': 'hf', 'path': 'facebook/dinov2-small',
                    'embed': {'pool': 'cls'}}, owner_id=bob['id'], admin=False)    # a hub id: anyone
    assert m['task_type'] == 'embedding' and m['embed']['enabled'] and m['embed']['pool'] == 'cls'
    with pytest.raises(ValueError, match='only admins'):
        reg.upsert({'name': 'local', 'family': 'hf', 'format': 'hf', 'path': str(tmp_path)}, owner_id=bob['id'],
                   admin=False)
    with pytest.raises(ValueError, match='pooling'):
        reg.upsert({'name': 'x', 'family': 'hf', 'format': 'hf', 'path': 'a/b', 'embed': {'pool': 'max'}},
                   owner_id=bob['id'], admin=False)
    with pytest.raises(ValueError, match='does not detect'):
        runners.load_runner(m, tmp_path)
    w = tmp_path / 'best.pt'
    w.write_bytes(b'weights')
    y = reg.upsert({'name': 'yolo', 'family': 'yolo', 'format': 'pt', 'path': str(w),
                    'embed': {'enabled': True, 'imgsz': '320'}}, owner_id=root['id'], admin=True)
    assert y['embed'] == {'enabled': True, 'layer': '', 'pool': 'avg', 'output': '', 'imgsz': 320, 'batch_size': None}
    assert y['task_type'] == 'object_detection'


def test_owner_models_list_only_embedding_models(reg, tmp_path):
    from authutil import account
    root, bob = account('root', admin=True), account('bob')
    w = tmp_path / 'best.pt'
    w.write_bytes(b'weights')
    reg.upsert({'name': 'det only', 'family': 'yolo', 'format': 'pt', 'path': str(w)}, owner_id=root['id'], admin=True)
    e = reg.upsert({'name': 'embeds', 'family': 'yolo', 'format': 'pt', 'path': str(w), 'embed': {'enabled': True}},
                   owner_id=root['id'], admin=True)
    reg.upsert({'name': 'bobs', 'family': 'hf', 'format': 'hf', 'path': 'a/b'}, owner_id=bob['id'], admin=False)
    mine = uf.OwnerModels(reg, root['id'], tmp_path, ttl=60)
    specs = mine()
    assert [s.label for s in specs.values()] == ['embeds · my model']
    key = next(iter(specs))
    assert key == uf.feature_key(e, reg.fingerprint(e))
    reg.upsert({'id': e['id'], 'embed': {'enabled': False}}, owner_id=root['id'], admin=True)
    assert key in mine()                                   # cached for ttl seconds ...
    mine.refresh()
    assert mine() == {}                                    # ... or until a save refreshes it
    assert list(uf.OwnerModels(reg, bob['id'], tmp_path)())[0].startswith('model:')
