"""data/stress.py: every perturbation is neutral at its neutral level and moves its metric the right way; a run with a
fake model that only finds objects in bright enough images draws the curve that falls on the dark side, the safe
window ends between the right levels, B (half as bright as A) lands on the curve with the expected drop and advice,
the label-free consistency mode gives the same picture, and a cancelled job stops."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from test_imgstats import scene

from dataviz.image import imgstats, stress
from dataviz.models import predict, runners


def metric(rgb, key):
    return float(imgstats.measure_array(rgb)[0][imgstats.COL[key]])


@pytest.mark.parametrize('axis', list(stress.AXES))
def test_neutral_level_is_identity(axis):
    a = scene(320, 240)
    assert stress.apply(a, axis, stress.AXES[axis][2]) is a
    out = stress.apply(a, axis, next(lv for lv in stress.AXES[axis][1] if lv != stress.AXES[axis][2]))
    assert out.shape == a.shape and out.dtype == np.uint8 and not np.array_equal(out, a)


def test_perturbations_move_their_metric():
    a = scene(640, 480)
    assert metric(stress.apply(a, 'brightness', 0.5), 'brightness') < 0.6 * metric(a, 'brightness')
    assert metric(stress.apply(a, 'gamma', 2.0), 'midtone') < metric(a, 'midtone') - 30
    assert metric(stress.apply(a, 'contrast', 0.5), 'contrast') < 0.6 * metric(a, 'contrast')
    assert metric(stress.apply(a, 'blur', 2), 'sharpness') < 0.3 * metric(a, 'sharpness')
    assert metric(stress.apply(a, 'motion', 12), 'blur_aniso') > metric(a, 'blur_aniso') + 0.2
    assert metric(stress.apply(a, 'noise', 10), 'noise') > metric(a, 'noise') + 6
    assert metric(stress.apply(a, 'temperature', -2000), 'cct') < metric(a, 'cct') - 800 \
        < metric(stress.apply(a, 'temperature', 2000), 'cct') - 1600
    assert metric(stress.apply(a, 'saturation', 0), 'saturation') < 2
    g = lambda x: np.asarray(Image.fromarray(x).convert('L'))
    assert imgstats.blockiness(g(stress.apply(a, 'jpeg', 10))) > imgstats.blockiness(g(stress.apply(a, 'jpeg', 70))) + 0.3
    assert metric(stress.apply(a, 'resolution', 0.25), 'sharpness') < 0.5 * metric(a, 'sharpness')
    with pytest.raises(KeyError):
        stress.apply(a, 'nope', 1)


def test_measured_curve_maps_levels_to_metric_change():
    ims = [scene(320, 240, seed=s) for s in range(3)]
    c = stress.measure_curve(ims, 'brightness')
    y = dict(zip(c['levels'], c['y']))
    assert y[1.0] == 1.0 and abs(y[0.5 if 0.5 in y else 0.45] - 0.45) < 0.08
    assert stress.measure_curve(ims, 'jpeg')['y'] == stress.AXES['jpeg'][1]


BOX = [40.0, 40.0, 160.0, 140.0]


@pytest.fixture
def world(monkeypatch, tmp_path):
    """8 scenes of 320 x 240 with one 'cat' box each, and a model that finds it when the image is bright enough."""
    imgs = {f'A/{i}.jpg': scene(320, 240, seed=i) for i in range(8)}

    def open_image(name):
        return Image.fromarray(imgs[name])

    runner = SimpleNamespace(names=['cat'], draft_size=320, imgsz=320, batch_size=4, max_batch=None)
    monkeypatch.setattr(runners, 'get_runner', lambda m, fp, wd: runner)
    calls = []

    def fake_predict(m, fp, wd, batch):
        calls.append(len(batch))
        out = []
        for im in batch:
            lum = float(np.asarray(im.convert('L')).mean())
            out.append(np.array([BOX + [0.9, 0]], np.float32) if lum > 75 else np.zeros((0, 6), np.float32))
        return out
    monkeypatch.setattr(predict, 'predict_batch', fake_predict)
    names = sorted(imgs)
    samples = [{'id': i, 'image': n, 'bbox': BOX, 'cls': 0} for i, n in enumerate(names)]
    st = SimpleNamespace(source=SimpleNamespace(open_image=open_image), classes=['cat'],
                         by_image={n: [i] for i, n in enumerate(names)}, samples=samples)
    return SimpleNamespace(st=st, names=names, calls=calls, lum=float(np.mean([imgs[n].mean() for n in names])))


def job():
    return SimpleNamespace(progress=lambda *a, **k: None, cancelled=threading.Event())


M = {'id': 'm1', 'name': 'fake', 'format': 'pt'}


def test_run_curve_window_and_b_point(world, tmp_path):
    res = stress.run(world.st, M, 'fp', tmp_path, world.names, ['brightness', 'noise'], 0.25, job())
    assert res['score_key'] == 'map50' and res['clean']['map50'] > 0.95 and res['n'] == 8 and res['n_labels'] == 8
    br = next(a for a in res['axes'] if a['key'] == 'brightness')
    by = {r['level']: r['map50'] for r in br['rows']}
    assert by[0.3] == 0 and by[1.75] > 0.95 and by[1.0] == res['clean']['map50']
    nz = next(a for a in res['axes'] if a['key'] == 'noise')
    assert all(r['map50'] > 0.95 for r in nz['rows'])                     # the fake model ignores noise
    # the predictions ran once per image per non-neutral level (+ the clean pass)
    assert sum(world.calls) == 8 * (1 + 7 + 6)
    # B is half as bright as A: below the edge of the window (75 / mean luma) -> major
    edge = 75 / world.lum
    rep = {'metrics': [{'key': 'brightness', 'available': True,
                        'a': {'med': 120.0, 'p5': 100.0, 'p95': 140.0}, 'b': {'med': 60.0}},
                       {'key': 'noise', 'available': True, 'a': {'med': 1.0, 'p5': 0.5, 'p95': 1.5}, 'b': {'med': 4.0}}]}
    sm = stress.summarise(res, rep)
    ax = {a['key']: a for a in sm['axes']}
    lo, hi = ax['brightness']['window']
    assert edge - 0.1 < lo < 1.0 and hi == 1.75 and ax['brightness']['open'] == [False, True]   # darkest scene
    b = ax['brightness']['b']
    assert abs(b['level'] - 0.5) < 0.08 and b['band'][0] < 1 < b['band'][1]
    assert ax['brightness']['status'] == 'major' and ax['brightness']['expected'] < 0.5
    assert ax['noise']['status'] == 'ok' and ax['noise']['b']['level'] > 0
    assert sm['headline']['axis'] == 'brightness' and sm['advice'][0]['key'] == 'brightness'
    assert 'mAP50 falls' in sm['advice'][0]['text']
    nob = stress.summarise(res)                                              # no shift report: windows only
    assert all(a['b'] is None for a in nob['axes']) and nob['headline'] is None


def test_consistency_without_labels(world, tmp_path):
    res = stress.run(world.st, M, 'fp', tmp_path, world.names, ['brightness'], 0.25, job(), labelled=False)
    assert res['labelled'] is False and res['n_labels'] == 8                 # the clean predictions
    rows = {r['level']: r['map50'] for r in res['axes'][0]['rows']}
    assert rows[1.0] > 0.95 and rows[0.3] == 0


def test_cancel_and_cache(world, tmp_path):
    j = job()
    j.cancelled.set()
    with pytest.raises(InterruptedError):
        stress.run(world.st, M, 'fp', tmp_path, world.names, ['brightness'], 0.25, j)
    k1 = stress.key_of('m1', 'fp', world.names, ['noise', 'brightness'], 0.25)
    assert k1 == stress.key_of('m1', 'fp', world.names, ['brightness', 'noise'], 0.25) != \
        stress.key_of('m1', 'fp2', world.names, ['brightness', 'noise'], 0.25)
    stress.save(tmp_path, k1, {'x': 1})
    assert stress.load(tmp_path, k1) == {'x': 1} and stress.load(tmp_path, 'nope') is None


def test_window_interpolates_and_axes_info():
    win, open_ = stress._window([0.5, 1.0, 1.5], [0.0, 1.0, 0.5], 1.0, 0.95)
    assert win[0] == pytest.approx(0.975) and win[1] == pytest.approx(1.05) and open_ == [False, False]
    info = stress.axes_info()
    assert {a['key'] for a in info} == set(stress.AXES) and all(a['neutral'] in a['levels'] for a in info)
