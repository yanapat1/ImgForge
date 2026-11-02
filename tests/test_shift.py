"""data/shift.py: KS equals scipy, PSI on a known split, alike sets fit, a darker / noisier B is flagged on exactly those
metrics with the matching recommendation, the separability AUC, the verdict levels, model_fit per side with the
masked ap_stats, and simulate() moving an image toward B's medians."""

import numpy as np
from scipy import stats

from dataviz.data import imgstats, shift
from dataviz.models import evaluate


def table(n, seed, **shifts):
    """An imgstats.Table of n images whose metrics are drawn around plausible values; shifts: key -> multiplier."""
    rng = np.random.default_rng(seed)
    base = {'brightness': 120, 'contrast': 50, 'dark_clip': 1, 'bright_clip': 1, 'dyn_range': 200, 'entropy': 5,
            'sharpness': 400, 'tenengrad': 30, 'edge_density': 8, 'noise': 2, 'r_mean': 120, 'g_mean': 120,
            'b_mean': 120, 'saturation': 80, 'colorfulness': 40, 'cast_a': 0, 'cast_b': 0, 'wb_rg': 1, 'wb_bg': 1,
            'width': 1920, 'height': 1080, 'megapixels': 2.07, 'aspect': 1.78, 'bytes_per_px': 0.3, 'jpeg_q': 90,
            'iso': np.nan, 'exposure': np.nan, 'fnumber': np.nan}
    vec = np.zeros((n, len(imgstats.KEYS)), np.float32)
    for k, v in base.items():
        j = imgstats.COL[k]
        f = shifts.get(k, 1.0)
        if k in ('width', 'height', 'megapixels', 'aspect', 'jpeg_q') or np.isnan(v):
            vec[:, j] = v * f
        elif k in ('cast_a', 'cast_b'):
            vec[:, j] = rng.normal(0, 1, n) + (f - 1)
        else:
            vec[:, j] = v * f * np.exp(rng.normal(0, 0.12, n))
    hist = np.full((n, 4 * imgstats.NHIST), 1 / imgstats.NHIST, np.float32)
    return imgstats.Table(names=[f'X/i{seed}_{i}.jpg' for i in range(n)], vec=vec, hist=hist,
                          camera=['Acme 1'] * n, fmt=['JPEG'] * n)


def test_ks_matches_scipy_and_psi():
    rng = np.random.default_rng(0)
    x, y = rng.normal(0, 1, 500), rng.normal(0.3, 1.2, 700)
    assert abs(shift.ks_2samp(x, y) - stats.ks_2samp(x, y).statistic) < 1e-12
    assert shift.psi(x, x) < 1e-9
    a = np.r_[np.zeros(50), np.ones(50)]                      # 2 bins of 50% vs 10 / 90
    b = np.r_[np.zeros(10), np.ones(90)]
    expect = (0.1 - 0.5) * np.log(0.1 / 0.5) + (0.9 - 0.5) * np.log(0.9 / 0.5)
    assert abs(shift.psi(a, b) - expect) < 1e-6


def test_alike_sets_fit():
    rep, _ = shift.build(table(600, 1), table(600, 2))
    px = [m for m in rep['metrics'] if m['available'] and m['group'] in shift.PIXEL_GROUPS]
    assert all(m['severity'] in ('ok', 'minor') for m in px), [(m['key'], m['severity']) for m in px if m['severity'] != 'ok']
    assert rep['separability']['auc'] < 0.62
    assert rep['verdict']['level'] == 'fits'
    assert not any(m['available'] for m in rep['metrics'] if m['key'] == 'iso')


def test_darker_noisier_b_is_shifted():
    rep, arrays = shift.build(table(600, 1), table(600, 2, brightness=0.6, r_mean=0.6, g_mean=0.6, b_mean=0.6, noise=3.0),
                              'A', 'B')
    by = {m['key']: m for m in rep['metrics']}
    assert by['brightness']['severity'] == 'major' and by['noise']['severity'] == 'major'
    assert by['sharpness']['severity'] in ('ok', 'minor') and by['saturation']['severity'] in ('ok', 'minor')
    assert by['brightness']['delta_pct'] < -30
    keys = {r['key'] for r in rep['recommendations']}
    assert {'brightness', 'noise', 'label_b'} <= keys
    assert rep['hyp']['hsv_v'] >= 0.5 and 'hsv_v:' in rep['hyp_yaml']
    assert rep['separability']['auc'] > 0.95
    assert rep['separability']['drivers'][0]['key'] in ('brightness', 'r_mean', 'g_mean', 'b_mean', 'noise')
    v = rep['verdict']
    assert v['level'] == 'shifted' and 'darker' in v['text'] and 'noisier' in v['text']
    assert len(arrays['p_b']) == 600 and arrays['p_b'].mean() > 0.8
    assert len(rep['curves']['luma']['a']) == imgstats.NHIST and len(rep['scatter']['b']['brightness']) == 600


def test_new_camera_and_resolution_is_check():
    tb = table(300, 2, width=0.5, height=0.5, megapixels=0.25)
    tb.camera = ['Other Z'] * 300
    rep, _ = shift.build(table(300, 1), tb)
    assert rep['cameras']['new_in_b'] == [('Other Z', 300)]
    assert rep['verdict']['level'] in ('check', 'shifted')
    assert any(r['key'] == 'camera' for r in rep['recommendations'])


class _D:
    """The part of evaluate.Data model_fit reads: 4 images (2 per side), A's predictions confident, B's not."""

    def __init__(self):
        self.images = ['A/1.jpg', 'A/2.jpg', 'B/1.jpg', 'B/2.jpg']
        self.p_score = np.array([0.9, 0.95, 0.3, 0.2], np.float32)
        self.p_img = np.array([0, 1, 2, 3])
        self.p_by_img = [np.array([0]), np.array([1]), np.array([2]), np.array([3])]
        self.gt_by_img = [[], [], [], []]


def test_model_fit_drop():
    side = np.array([0, 0, 1, 1], np.int16)
    fit = shift.model_fit(_D(), side, 0, 1, 0.25, lambda mask: None)
    assert fit['a']['mean_top'] > 0.9 and fit['b']['none_found'] == 0.5
    assert fit['drop'] > 0.6


def test_ap_stats_mask_default_unchanged():
    samples = [{'id': 0, 'image': 'A/1.jpg', 'bbox': [0, 0, 10, 10], 'cls': 0},
               {'id': 1, 'image': 'B/1.jpg', 'bbox': [0, 0, 10, 10], 'cls': 0}]
    preds = {'images': ['A/1.jpg', 'B/1.jpg'], 'img': np.array([0]),
             'det': np.array([[0, 0, 10, 10, 0.9, 0]], np.float32)}
    d = evaluate.Data(samples, 1, preds, np.array([0]), {})
    full = evaluate.ap_stats(d, 'all')
    a = evaluate.ap_stats(d, 'all', np.array([True, False]))
    b = evaluate.ap_stats(d, 'all', np.array([False, True]))
    assert a['map50'] > 0.99 and b['map50'] == 0.0 and 0 < full['map50'] < a['map50'] + 1e-9


def test_simulate_moves_toward_b():
    import cv2
    from test_imgstats import scene
    a = scene()
    va = imgstats.measure_array(a)[0]
    a_med = {k: float(va[imgstats.COL[k]]) for k in imgstats.KEYS if np.isfinite(va[imgstats.COL[k]])}
    b_med = dict(a_med, brightness=a_med['brightness'] * 0.6, sharpness=a_med['sharpness'] * 0.3,
                 noise=a_med['noise'] + 6)
    out, prm = shift.simulate(a, a_med, b_med)
    vo = imgstats.measure_array(out)[0]
    assert abs(vo[imgstats.COL['brightness']] / a_med['brightness'] - 0.6) < 0.1
    assert prm['blur_sigma_px'] > 0 and prm['noise_sigma'] > 5
    assert vo[imgstats.COL['noise']] > a_med['noise'] + 2
    same, p0 = shift.simulate(a, a_med, b_med, strength=0)
    assert np.abs(same.astype(int) - a).max() <= 1 and p0['blur_sigma_px'] == 0
    assert cv2 is not None
