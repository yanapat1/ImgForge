"""Augment › Model (image/augment_model.py) with a stand-in detector: it finds bright rectangles, so mirroring the image is
handled (the labels move with it) and darkening it is not."""

import numpy as np
import pytest

from dataviz.image import augment_model as am

pytest.importorskip('albumentations')


def _image(k):
    a = np.full((96, 128, 3), 40, np.uint8)
    x = 10 + 6 * k
    a[20:60, x:x + 30] = 250
    return a, [(0, float(x), 20.0, float(x + 30), 60.0)]


def _detector(imgs):
    out = []
    for im in imgs:
        g = np.asarray(im).mean(axis=2)
        ys, xs = np.nonzero(g > 200)
        out.append(np.array([[xs.min(), ys.min(), xs.max() + 1, ys.max() + 1, 0.9, 0]], np.float32) if len(xs) else np.zeros((0, 6), np.float32))
    return out


def test_the_model_handles_a_flip_but_not_the_dark():
    names = [f'i{k}' for k in range(8)]
    recipe = {'steps': [{'t': 'HorizontalFlip', 'p': 0.5},
                        {'t': 'RandomBrightnessContrast', 'p': 0.5, 'args': {'brightness_limit': [-0.6, -0.5], 'contrast_limit': [0, 0]}}]}
    r = am.run(lambda nm: _image(int(nm[1:])), names, recipe, _detector, [], ['part'], conf=0.25)
    assert r['n'] == 8 and r['clean']['map50'] > 0.9
    by = {s['key']: s for s in r['steps']}
    assert by['HorizontalFlip']['handled'] and by['HorizontalFlip']['drop'] < 0.05
    assert not by['RandomBrightnessContrast']['handled'] and by['RandomBrightnessContrast']['drop'] > 0.5
    assert r['steps'][0]['key'] == 'RandomBrightnessContrast'                    # the worst first
    assert r['recipe'] is not None and 'Brightness' in r['verdict']
    assert r['all']['label'] == 'Every step together' and r['all']['map50'] is not None    # the steps mixed, each at full chance
    assert r['recipe']['map50'] is not None and {'all', 'recipe'}.isdisjoint(by)
    one = am.run(lambda nm: _image(int(nm[1:])), names, {'steps': [{'t': 'HorizontalFlip', 'p': 0.5}]}, _detector, [], ['part'], conf=0.25)
    assert one['all'] is None and one['recipe'] is not None                       # one step: nothing to mix, the recipe row stays
    with pytest.raises(ValueError):
        am.run(lambda nm: _image(0), names, {'steps': []}, _detector, [], ['part'])
