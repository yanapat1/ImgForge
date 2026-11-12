"""Coverage maths (data/augment_cover.py) on synthetic embeddings with a known answer (tight clusters: real features are
L2-normalised, so what matters is the direction)."""

import numpy as np

from dataviz.data import augment_cover as cov


def _sets(seed=0):
    r = np.random.default_rng(seed)
    base = np.zeros(16)
    base[0] = 4.0
    train = base + r.normal(0, 0.3, (300, 16))
    shift = np.zeros(16)
    shift[1] = 6.0
    target = base + shift + r.normal(0, 0.3, (120, 16))
    return r, train, target, shift


def test_an_augmentation_toward_the_target_covers_it():
    r, train, target, shift = _sets()
    aug = train + shift + r.normal(0, 0.1, train.shape)
    rep = cov.run(train, aug, target, [['Shift']] * len(aug))
    assert rep['coverage']['before'] < 0.3 and rep['coverage']['after'] > 0.8
    assert rep['credit'][0]['t'] == 'Shift' and rep['rescued'] > 0
    assert rep['domain_auc']['after'] <= rep['domain_auc']['before']
    assert 'covers' in rep['verdict'] and len(rep['map']['target']['xy']) == len(target)


def test_far_away_noise_adds_nothing_and_is_flagged():
    r, train, target, _ = _sets(1)
    aug = np.eye(16)[5] * 6 + r.normal(0, 0.3, (300, 16))          # a direction neither set has
    rep = cov.run(train, aug, target, [['Noise']] * len(aug))
    assert abs(rep['coverage']['gain']) < 0.05 and rep['off_domain'] > 0.8


def test_gap_names_the_darker_images():
    dark = [{'brightness': 60.0 + i % 5, 'contrast': 40, 'sharpness': 5, 'saturation': 80, 'warmth': 0, 'noise': 2} for i in range(20)]
    lit = [{'brightness': 140.0 + i % 7, 'contrast': 40 + i % 3, 'sharpness': 5, 'saturation': 80, 'warmth': 0, 'noise': 2} for i in range(40)]
    g = cov.gap(lit, dark)
    assert g[0]['key'] == 'brightness' and g[0]['step']['t'] == 'RandomBrightnessContrast'
    assert g[0]['step']['args']['brightness_limit'][0] < -0.3 and 'darker' in g[0]['text']
