"""Projection preprocessing (ml/prep.py, image/prepcrops.py, State.request_projection prep) and the recommended
projections (ml/recipes.yaml): the spec is canonical (Default = the old cache keys), the key parser round-trips,
the feature prep math (fuse, debias, scale, pca) and the filter (hidden or grey), image-prep variants built from the
index crops (a polygon mask, flip-averaged), and every recipe validates, resolves and merges."""

import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from conftest import make_jpeg
from dataviz.ml import analysis, features as feats, prep, recipes


# ---------------------------------------------------------------- the spec
def test_default_is_empty_and_keeps_the_old_keys():
    from dataviz.state import parse_projection_key, projection_key
    assert prep.validate(None) == prep.validate({}) == {}
    assert prep.validate({'image': {'context': 'square10', 'fit': 'square', 'tone': 'none'},
                          'feature': {'pca': 50, 'scale': 'none', 'show': 'grey'}}) == {}     # all defaults
    old = projection_key('dinov2-base', 'umap', {'n_neighbors': 15, 'min_dist': 0.1}, None)
    assert projection_key('dinov2-base', 'umap', {'n_neighbors': 15, 'min_dist': 0.1}, None, 2, {}) == old
    spec = prep.validate({'image': {'tone': 'gray'}, 'feature': {'scale': 'whiten'}})
    k = projection_key('dinov2-base', 'umap', {'n_neighbors': 15}, [1, 0], 3, spec)
    assert k.startswith('dinov2-base~') and '|f:' in k and k.endswith('|3d')
    assert parse_projection_key(k)[1:] == ('umap', {'n_neighbors': 15}, [0, 1], 3)
    assert parse_projection_key(old) == ('dinov2-base', 'umap', {'n_neighbors': 15, 'min_dist': 0.1}, None, 2)
    # the same spec in another order / with defaults names the same variant
    assert prep.tag(spec['image']) == prep.tag(prep.validate({'image': {'tone': 'gray', 'fit': 'square'}})['image'])


@pytest.mark.parametrize('bad', [
    {'image': {'context': 'huge'}}, {'image': {'blur': 1}}, {'feature': {'pca': 7}}, {'feature': {'scale': 'log'}},
    {'feature': {'fuse': {'feature': 'hog', 'weight': 2}}}, {'feature': {'filter': {'brightness': [200, 10]}}},
    {'image': {'context': 'image', 'mask': 'polygon'}}, {'other': {}},
])
def test_bad_specs_are_refused(bad):
    with pytest.raises(prep.PrepError):
        prep.validate(bad)


def test_feature_prep_math():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(40, 8)).astype(np.float32)
    a[:20] += 5                                             # group 0 sits far from group 1
    groups = np.r_[np.zeros(20), np.ones(20)].astype(np.int32)
    x, notes = prep.apply_features(a, {'debias': 'session'}, 'euclidean', groups)
    assert np.allclose(x[:20].mean(0), 0, atol=1e-5) and np.allclose(x[20:].mean(0), 0, atol=1e-5)
    assert notes == ['2 session groups centred']
    _, notes = prep.apply_features(a, {'debias': 'session'}, 'euclidean', np.zeros(40, np.int32))
    assert 'nothing to remove' in notes[0]
    s, _ = prep.apply_features(a, {'scale': 'standardize'}, 'euclidean')
    assert np.allclose(s.std(0), 1, atol=1e-4)
    w, _ = prep.apply_features(a, {'scale': 'whiten', 'pca': 20}, 'euclidean')
    assert w.shape == (40, 8)                               # whiten keeps at most the dims there are
    f, _ = prep.apply_features(a, {'fuse': {'feature': 'b', 'weight': 0.25}}, 'cosine', extra=a[:, :3])
    assert f.shape == (40, 11) and np.allclose(np.linalg.norm(f, axis=1), 1, atol=1e-5)
    # pre-PCA dims reach the methods: t-SNE / sklearn reduce to that many, 0 = none
    xy, _ = analysis.project(rng.normal(size=(60, 80)).astype(np.float32), 'isomap', None, 'euclidean', pre_pca=0)
    assert xy.shape == (60, 2)


def test_keep_mask_and_groups():
    samples = [{'bbox': [0, 0, w, w], 'blur': b, 'brightness': br, 'image': f'cam{c}/x{i}.jpg', 'session': f's{c}'}
               for i, (w, b, br, c) in enumerate([(5, 50, 100, 1), (30, 5, 100, 1), (30, 50, 250, 2), (30, 50, 100, 2)])]
    ids = np.arange(4)
    assert prep.keep_mask(samples, ids, {'min_px': 12}).tolist() == [False, True, True, True]
    assert prep.keep_mask(samples, ids, {'min_blur': 20, 'brightness': [0, 200]}).tolist() == [True, False, False, True]
    assert prep.group_codes(samples, ids, 'folder').tolist() == [0, 0, 1, 1]
    assert prep.group_codes(samples, ids, 'session').tolist() == [0, 0, 1, 1]
    assert prep.group_codes(samples, ids, '') is None


# ---------------------------------------------------------------- recipes
def test_every_recipe_validates_and_merges():
    assert len(recipes.RECIPES) >= 10 and {r['category'] for r in recipes.RECIPES.values()} == set(recipes.CATEGORIES)
    cur = {'feature': 'hog', 'method': 'umap', 'params': {'n_neighbors': 30, 'min_dist': 0.3}, 'dims': 3,
           'prep': {'image': {'tone': 'gray'}, 'feature': {'filter': {'min_px': 40}, 'show': 'grey'}}}
    m = recipes.merge(cur, recipes.RECIPES['hide-tiny-blurry']['sets'])        # only prep: the rest stays
    assert m['method'] == 'umap' and m['dims'] == 3 and m['params']['n_neighbors'] == 30
    assert m['prep']['image'] == {'tone': 'gray'} and m['prep']['feature']['filter'] == {'min_px': 12, 'min_blur': 20}
    m = recipes.merge(cur, recipes.RECIPES['class-separation']['sets'])
    assert m['method'] == 'pacmap' and m['params'] == {'n_neighbors': 10} and m['prep']['feature']['scale'] == 'whiten'


def test_recipes_resolve_against_a_session():
    reg = {'hog': SimpleNamespace(group='simple', kind='', label='HOG'),
           'dinov2-base': SimpleNamespace(group='deep', kind='dinov2', label='DINOv2'),
           'geometry': SimpleNamespace(group='simple', kind='', label='Geometry')}
    st = SimpleNamespace(registry=reg, source=SimpleNamespace(kind='local'), ready=True,
                         samples=[{'session': 'a'}, {'session': 'b'}], has_polygons=lambda: False,
                         get_projection=lambda k: None)
    out = {r['id']: r for r in recipes.for_state(st)}
    assert out['find-mislabels']['available'] and out['find-mislabels']['sets']['feature'] == 'dinov2-base'
    assert not out['object-only']['available'] and 'segmentation' in out['object-only']['why']
    assert out['camera-shift']['available'] is False and 'clip' in out['camera-shift']['why'] or \
        out['camera-shift']['sets']['feature'] == 'dinov2-base'
    assert out['orientation']['sets']['feature'] == 'hog'


# ---------------------------------------------------------------- end to end on a small dataset
def _dataset(root, seg=False):
    (root / 'labels').mkdir(parents=True)
    (root / 'classes.txt').write_text('red\ngreen\n')
    poly = '0 0.3 0.3 0.7 0.3 0.7 0.7 0.3 0.7\n'
    for i in range(12):
        make_jpeg(root / 'images' / f'cam{i % 2}_{i:03d}.jpg', 96, 96, color=(200 if i % 2 else 30, 40 + 10 * i, 60))
        small = '1 0.1 0.1 0.05 0.05\n'                     # a 5 px box: the filter leaves it out
        (root / 'labels' / f'cam{i % 2}_{i:03d}.txt').write_text((poly if seg else '0 0.5 0.5 0.4 0.4\n') + small)
    return root


def _state(root, cache):
    from dataviz.state import State
    from dataviz.image.sources import LocalSource
    reg = {k: feats.FeatureSpec(k, v[0], 'simple', v[1], metric=v[2]) for k, v in feats.SIMPLE.items()}
    st = State(LocalSource(root), cache, reg, default_feature='color-hist')
    st._index_job(SimpleNamespace(progress=lambda *a, **k: None))
    return st


def _project(st, feature, method='pca', prep_spec=None, dims=2):
    proj, key, job = st.request_projection(feature, method, {}, None, dims, prep_spec)
    if proj is None:
        job.wait(120)
        assert job.status == 'done', job.error
        proj = st.get_projection(key)
    return proj, key


def test_projection_with_feature_prep_and_filters(tmp_path):
    st = _state(_dataset(tmp_path / 'ds'), tmp_path / 'ws')
    n = len(st.samples)
    base, k0 = _project(st, 'color-hist')
    assert len(base['id']) == n and 'prep' not in base['info']
    hidden, k1 = _project(st, 'color-hist', prep_spec={'feature': {'filter': {'min_px': 12}, 'debias': 'session',
                                                                   'scale': 'standardize'}})
    assert k1 != k0 and len(hidden['id']) == n // 2 and hidden['info']['prep']['excluded'] == n // 2
    grey, _ = _project(st, 'color-hist', prep_spec={'feature': {'filter': {'min_px': 12}, 'show': 'grey'}})
    assert len(grey['id']) == n and int(grey['muted'].sum()) == n // 2 and grey['xy'].shape == (n, 2)
    fused, _ = _project(st, 'color-hist', 'umap', {'feature': {'fuse': {'feature': 'geometry', 'weight': 0.3}}})
    assert np.isfinite(fused['xy']).all()
    with pytest.raises(ValueError, match='leave no boxes'):
        _project(st, 'color-hist', prep_spec={'feature': {'filter': {'min_px': 9999}}})
    with pytest.raises(ValueError, match='fuse with another feature'):
        st.request_projection('color-hist', 'pca', {}, None, 2, {'feature': {'fuse': {'feature': 'color-hist', 'weight': .5}}})
    # saved views keep the base feature + the spec, so the page restores the controls
    entry = st.saved_projections()[k1]
    assert entry['feature'] == 'color-hist' and entry['prep']['feature']['debias'] == 'session'


def test_image_prep_variants_from_the_crops(tmp_path):
    st = _state(_dataset(tmp_path / 'ds', seg=True), tmp_path / 'ws')
    assert st.has_polygons()
    spec = {'image': {'context': 'box', 'mask': 'polygon', 'tone': 'gray', 'tta': 'hflip'}}
    proj, key = _project(st, 'color-hist', prep_spec=spec)
    t = prep.tag(prep.validate(spec)['image'])
    out = st.cache_dir / f'crops~{t}'
    assert len(list(out.glob('*.jpg'))) == len(st.samples) and len(list((out / 'f').glob('*.jpg'))) == len(st.samples)
    im = np.asarray(Image.open(out / f'{st.samples[0]["id"]:06d}.jpg'))
    assert abs(int(im[1, 1, 0]) - 114) < 12 and abs(int(im[1, 1, 0]) - int(im[1, 1, 2])) < 6, 'grey outside the polygon'
    assert json.loads((out / 'done.json').read_text())['fingerprint'] == st.fingerprint
    assert f'color-hist~{t}' in st.registry and st.registry[f'color-hist~{t}'].group == 'variant'
    assert key.startswith(f'color-hist~{t}|')
    # the index's own crop cache is left alone by a variant (features/_crops224_*.npy)
    assert (st.cache_dir / 'crops').is_dir()
    # a wider context re-reads the images; the Default needs no variant
    wide, _ = _project(st, 'color-hist', prep_spec={'image': {'context': 'pad50', 'fit': 'letterbox'}})
    assert len(wide['id']) == len(st.samples)
    assert st.prep_why_not('color-hist', {'mask': 'polygon'}) == ''


def test_a_mask_needs_polygons(tmp_path):
    st = _state(_dataset(tmp_path / 'ds'), tmp_path / 'ws')
    assert not st.has_polygons()
    with pytest.raises(ValueError, match='segmentation labels'):
        st.request_projection('color-hist', 'pca', {}, None, 2, {'image': {'mask': 'polygon'}})
