"""Augmentation recipes (image/augment.py): every transform of the catalog runs, recipes are checked, boxes follow."""

import numpy as np
import pytest

from dataviz.image import augment as ag

pytest.importorskip('albumentations')

RGB = (np.random.RandomState(0).rand(120, 160, 3) * 255).astype(np.uint8)
BOX = [(0, 20.0, 30.0, 80.0, 90.0)]


@pytest.mark.parametrize('t', [x['t'] for x in ag.CATALOG])
def test_every_transform_runs_with_its_defaults(t):
    im, boxes, dropped, applied = ag.apply(RGB, BOX, {'steps': [{'t': t, 'p': 1}]}, seed=3)
    assert im.dtype == np.uint8 and im.ndim == 3
    assert dropped + len(boxes) == 1
    for _, x1, y1, x2, y2 in boxes:
        assert 0 <= x1 < x2 <= im.shape[1] and 0 <= y1 < y2 <= im.shape[0]


@pytest.mark.parametrize('k', list(ag.PRESETS))
def test_every_preset_runs(k):
    ag.apply(RGB, BOX, {'steps': ag.PRESETS[k][2]}, seed=1)


def test_validate_refuses_unknown_and_clamps():
    with pytest.raises(ValueError, match='unknown augmentation'):
        ag.validate({'steps': [{'t': 'Nope'}]})
    with pytest.raises(ValueError, match='no argument'):
        ag.validate({'steps': [{'t': 'MotionBlur', 'args': {'size': 3}}]})
    with pytest.raises(ValueError, match='one of'):
        ag.validate({'steps': [{'t': 'RandomRain', 'args': {'rain_type': 'hail'}}]})
    with pytest.raises(ValueError, match='at most'):
        ag.validate({'steps': [{'t': 'ToGray'}] * 21})
    r = ag.validate({'strength': 9, 'steps': [{'t': 'MotionBlur', 'p': 3, 'args': {'blur_limit': [40, 2]}}]})
    assert r['strength'] == 2 and r['steps'][0]['p'] == 1
    assert r['steps'][0]['args']['blur_limit'] == [3, 31]                       # sorted, clamped, odd


def test_seeded_and_boxes_follow_a_flip():
    flip = {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}
    im, boxes, _, applied = ag.apply(RGB, BOX, flip, seed=5)
    assert np.array_equal(im, RGB[:, ::-1]) and applied == ['HorizontalFlip']
    assert boxes == [(0, 160 - 80.0, 30.0, 160 - 20.0, 90.0)]
    noisy = {'steps': [{'t': 'GaussNoise', 'p': 1}]}
    assert np.array_equal(ag.apply(RGB, BOX, noisy, seed=7)[0], ag.apply(RGB, BOX, noisy, seed=7)[0])
    assert not np.array_equal(ag.apply(RGB, BOX, noisy, seed=7)[0], ag.apply(RGB, BOX, noisy, seed=8)[0])


def test_strength_zero_changes_nothing():
    r = {'strength': 0, 'steps': [{'t': 'RandomBrightnessContrast', 'p': 1}, {'t': 'MotionBlur', 'p': 1}]}
    im, _, _, applied = ag.apply(RGB, BOX, r, seed=1)
    assert np.array_equal(im, RGB) and applied == []


def test_interchange_round_trip_and_code():
    r = ag.validate({'mode': 'one_of', 'steps': ag.PRESETS['camera'][2]})
    back, skipped = ag.from_albu_json(ag.to_albu_json(r))
    assert skipped == [] and back['mode'] == 'one_of' and [s['t'] for s in back['steps']] == [s['t'] for s in r['steps']]
    code = ag.to_python(r)
    ns = {}
    exec(compile(code, 'augment.py', 'exec'), ns)                             # noqa: S102 - our own generated code
    assert ns['transform'] is not None
    assert 'RandomToneCurve' in ag.to_yaml(r)
    _, skipped = ag.from_albu_json({'transform': {'transforms': [{'__class_fullname__': 'Solarize', 'p': 1}]}})
    assert skipped == ['Solarize']


def _loader(boxes):
    return lambda name: (RGB, boxes)


def test_box_check_counts_lost_tiny_and_per_step():
    flip = ag.box_check(_loader(BOX), ['a', 'b'], {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}, k=2, classes=['cat'])
    assert flip['boxes_in'] == 4 and flip['lost'] == 0 and flip['kept'] == 4 and flip['warnings'] == []
    assert flip['transforms'][0]['t'] == 'HorizontalFlip' and flip['transforms'][0]['variants'] == 4
    # a box in the corner and a big shift: some are lost, the step and the class say so
    corner = [(0, 0.0, 0.0, 20.0, 20.0), (0, 140.0, 100.0, 160.0, 120.0)]
    shift = {'steps': [{'t': 'Affine', 'p': 1, 'args': {'translate_percent': [0.3, 0.3], 'scale': [1, 1], 'rotate': [0, 0]}}]}
    r = ag.box_check(_loader(corner), [f'i{n}' for n in range(5)], shift, k=2, classes=['cat'])
    assert r['lost'] > 0 and r['classes'][0]['name'] == 'cat' and r['examples']
    assert any('lost' in w for w in r['warnings'])
    # small boxes: tiny at the train size
    small = [(0, 10.0, 10.0, 14.0, 14.0)]
    t = ag.box_check(_loader(small), ['a'], {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}, k=1, min_px=8, train_size=160)
    assert t['tiny'] == 1 and t['tiny_pct'] == 100.0


def test_make_copy_writes_a_yolo_folder(tmp_path):
    from dataviz.image import augment_run as ar
    flip = {'steps': [{'t': 'HorizontalFlip', 'p': 1}]}
    res = ar.make_copy(['a/x.jpg', 'b.jpg'], _loader(BOX), flip, 2, True, tmp_path / 'out', ['cat'])
    assert res == {'images': 6, 'boxes': 6, 'lost': 0}
    names = sorted(p.name for p in (tmp_path / 'out' / 'images').iterdir())
    assert names == ['a_x.jpg', 'a_x_aug1.jpg', 'a_x_aug2.jpg', 'b.jpg', 'b_aug1.jpg', 'b_aug2.jpg']
    line = (tmp_path / 'out' / 'labels' / 'b_aug1.txt').read_text().split()
    assert line[0] == '0' and abs(float(line[1]) - (160 - 50) / 160) < 1e-4            # the box mirrored with the image
    assert (tmp_path / 'out' / 'classes.txt').read_text() == 'cat\n'
    assert '"variants": 2' in (tmp_path / 'out' / 'recipe.json').read_text()
    assert 'augmentations=augmentations' in ag.to_ultralytics(flip) and 'A.HorizontalFlip(p=1.0)' in ag.to_ultralytics(flip)
