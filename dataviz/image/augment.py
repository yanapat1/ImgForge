"""
Augmentation recipes for the visualizer's Augment tab: a curated set of Albumentations transforms, what each one
simulates, and a recipe format the page edits, previews and exports.

    recipe   {"version": 1, "mode": "all" | "one_of" | "some_of", "n": 2, "strength": 1.0,
              "steps": [{"t": "MotionBlur", "p": 0.5, "on": true, "args": {"blur_limit": [3, 9]}}, ...]}

    catalog()                    -> {groups, transforms, presets}: what the page shows (no albumentations needed)
    validate(recipe)             -> the recipe cleaned: known transforms and args only, values inside their ranges
    build(recipe, seed)          -> an albumentations Compose with YOLO-safe boxes (pascal_voc, kept when 30 % shows)
    apply(rgb, boxes, recipe, seed) -> (uint8 image, boxes [(cls, x1, y1, x2, y2)], dropped, applied step names)
    to_albu_json / from_albu_json / to_python / to_yaml   interchange with code and other tools

`strength` scales every magnitude toward the transform's identity (0 = no change, 2 = twice as strong), so one slider
tries a gentler or harsher recipe. albumentations is imported only when a recipe runs (the visualizer zone has it).
"""

import copy
import hashlib
import json

GROUPS = [('light', 'Light & colour'), ('weather', 'Weather & scene'), ('camera', 'Camera & sensor'), ('blur', 'Blur & motion'),
          ('geometry', 'Geometry (boxes follow)'), ('occlusion', 'Occlusion')]
MAX_STEPS = 20


def R(k, lo, hi, default, identity, step=None, integer=False, odd=False, label=None):
    """A [low, high] range argument."""
    return {'k': k, 'kind': 'range', 'min': lo, 'max': hi, 'default': list(default), 'identity': identity,
            'step': step or (1 if integer else round((hi - lo) / 100, 4)), 'int': integer, 'odd': odd, 'label': label or k.replace('_', ' ')}


def F(k, lo, hi, default, identity, step=None, integer=False, label=None):
    """One number."""
    return {'k': k, 'kind': 'int' if integer else 'float', 'min': lo, 'max': hi, 'default': default, 'identity': identity,
            'step': step or (1 if integer else round((hi - lo) / 100, 4)), 'label': label or k.replace('_', ' ')}


def C(k, choices, default, label=None):
    return {'k': k, 'kind': 'choice', 'choices': list(choices), 'default': default, 'label': label or k.replace('_', ' ')}


def T(t, group, label, help_, covers, args=(), p=0.5, bbox_safe=True):
    return {'t': t, 'group': group, 'label': label, 'help': help_, 'covers': covers, 'args': list(args), 'p_default': p, 'bbox_safe': bbox_safe}


CATALOG = [
    # light & colour
    T('RandomBrightnessContrast', 'light', 'Brightness & contrast', 'Lighter or darker, flatter or punchier.', 'time of day, exposure, a darker or brighter camera',
      [R('brightness_limit', -0.6, 0.6, (-0.2, 0.2), 0), R('contrast_limit', -0.6, 0.6, (-0.2, 0.2), 0)]),
    T('RandomGamma', 'light', 'Gamma', 'Brightens or darkens the mid-tones, keeps black and white.', 'a camera with another tone curve, dusk',
      [R('gamma_limit', 40, 200, (80, 120), 100, integer=True)]),
    T('HueSaturationValue', 'light', 'Hue, saturation, value', 'Shifts the colours, their strength and brightness.', 'another white balance, faded or vivid colours',
      [R('hue_shift_limit', -60, 60, (-10, 10), 0, integer=True), R('sat_shift_limit', -100, 100, (-30, 30), 0, integer=True),
       R('val_shift_limit', -100, 100, (-20, 20), 0, integer=True)]),
    T('ColorJitter', 'light', 'Colour jitter', 'Brightness, contrast, saturation and hue together, as torchvision does.', 'mixed cameras and lighting',
      [R('brightness', 0, 2, (0.8, 1.2), 1), R('contrast', 0, 2, (0.8, 1.2), 1), R('saturation', 0, 2, (0.8, 1.2), 1),
       R('hue', -0.5, 0.5, (-0.05, 0.05), 0)]),
    T('RGBShift', 'light', 'RGB shift', 'Moves each colour channel up or down.', 'a colour cast, cheap sensors',
      [R('r_shift_limit', -60, 60, (-15, 15), 0, integer=True), R('g_shift_limit', -60, 60, (-15, 15), 0, integer=True),
       R('b_shift_limit', -60, 60, (-15, 15), 0, integer=True)]),
    T('PlanckianJitter', 'light', 'Colour temperature', 'Warmer or cooler light, along real light sources.', 'indoor tungsten vs daylight, sunset',
      [C('mode', ['blackbody', 'cied'], 'blackbody')]),
    T('RandomToneCurve', 'light', 'Tone curve', 'Bends the brightness curve, like a camera\'s picture style.', 'another camera\'s processing',
      [F('scale', 0, 0.5, 0.1, 0)]),
    T('CLAHE', 'light', 'Local contrast (CLAHE)', 'Boosts contrast tile by tile.', 'HDR-like or enhanced images',
      [R('clip_limit', 1, 8, (1, 4), 1)]),
    T('ToGray', 'light', 'Grey scale', 'Removes the colour.', 'night / IR cameras, black-and-white footage', [], p=0.1),
    T('Illumination', 'light', 'Uneven light', 'A light gradient, spot or corner falloff across the image.', 'a lamp, vignetting, sun from one side',
      [C('mode', ['linear', 'corner', 'gaussian'], 'linear'), R('intensity_range', 0, 0.5, (0.01, 0.2), 0)]),
    # weather & scene
    T('RandomRain', 'weather', 'Rain', 'Rain streaks and a darker, blurrier image.', 'rain, a wet lens',
      [C('rain_type', ['default', 'drizzle', 'heavy', 'torrential'], 'drizzle'), F('brightness_coefficient', 0.3, 1, 0.8, 1),
       F('blur_value', 1, 15, 3, 1, integer=True)], p=0.3),
    T('RandomFog', 'weather', 'Fog', 'A white haze that hides the far away.', 'fog, mist, smoke, a dirty lens',
      [R('fog_coef_range', 0, 1, (0.2, 0.5), 0), F('alpha_coef', 0, 0.3, 0.08, 0)], p=0.3),
    T('RandomSnow', 'weather', 'Snow', 'Bright snow patches.', 'snow on the ground or the lens',
      [R('snow_point_range', 0, 1, (0.1, 0.3), 0), F('brightness_coeff', 1, 4, 2.5, 1)], p=0.2),
    T('RandomSunFlare', 'weather', 'Sun flare', 'A bright flare with rings.', 'the sun in the frame, headlights',
      [F('src_radius', 50, 600, 200, 50, integer=True)], p=0.2),
    T('RandomShadow', 'weather', 'Shadows', 'Dark polygons across the scene.', 'buildings, trees, clouds casting shadows',
      [R('num_shadows_limit', 1, 5, (1, 2), 1, integer=True), R('shadow_intensity_range', 0, 1, (0.3, 0.6), 0)], p=0.3),
    T('Spatter', 'weather', 'Spatter', 'Drops or mud on the lens.', 'a dirty or wet camera window', [C('mode', ['rain', 'mud'], 'rain')], p=0.2),
    # camera & sensor
    T('GaussNoise', 'camera', 'Sensor noise', 'Grain on every pixel.', 'low light, a small sensor, high ISO',
      [R('std_range', 0, 0.5, (0.02, 0.1), 0)], p=0.3),
    T('ISONoise', 'camera', 'ISO noise', 'Colour speckle like a camera at high ISO.', 'night shots, cheap cameras',
      [R('color_shift', 0, 0.2, (0.01, 0.05), 0), R('intensity', 0, 1, (0.1, 0.5), 0)], p=0.3),
    T('MultiplicativeNoise', 'camera', 'Gain noise', 'Pixels multiplied by small random factors.', 'sensor gain differences',
      [R('multiplier', 0.5, 1.5, (0.9, 1.1), 1)], p=0.3),
    T('Downscale', 'camera', 'Lower resolution', 'Shrinks and enlarges again: detail is lost.', 'a lower-resolution or farther camera, digital zoom',
      [R('scale_range', 0.1, 1, (0.4, 0.8), 1)], p=0.3),
    T('ImageCompression', 'camera', 'JPEG compression', 'Blocky artefacts of strong compression.', 'video streams, CCTV, messaging apps',
      [R('quality_range', 5, 100, (40, 90), 100, integer=True)], p=0.3),
    T('Defocus', 'camera', 'Out of focus', 'A lens blur with round highlights.', 'a camera focused elsewhere',
      [R('radius', 1, 15, (2, 5), 1, integer=True), R('alias_blur', 0, 1, (0.1, 0.3), 0)], p=0.2),
    T('ChromaticAberration', 'camera', 'Colour fringes', 'Red / green fringes at the edges, like a cheap lens.', 'wide or low-cost lenses',
      [R('primary_distortion_limit', -0.1, 0.1, (-0.02, 0.02), 0), R('secondary_distortion_limit', -0.2, 0.2, (-0.05, 0.05), 0)], p=0.2),
    T('OpticalDistortion', 'camera', 'Lens distortion', 'Barrel or pincushion bending; boxes follow.', 'wide-angle and fisheye cameras',
      [R('distort_limit', -0.5, 0.5, (-0.1, 0.1), 0)], p=0.3),
    T('Sharpen', 'camera', 'Sharpen', 'Crisper edges, as a camera\'s own sharpening.', 'phones and cameras that sharpen',
      [R('alpha', 0, 1, (0.2, 0.5), 0)], p=0.2),
    T('Posterize', 'camera', 'Fewer colours', 'Fewer bits per channel.', 'old or heavily compressed video', [F('num_bits', 1, 8, 4, 8, integer=True)], p=0.1),
    # blur & motion
    T('MotionBlur', 'blur', 'Motion blur', 'A streak in one direction.', 'moving objects, a shaking or moving camera',
      [R('blur_limit', 3, 31, (3, 9), 3, integer=True, odd=True)], p=0.3),
    T('GaussianBlur', 'blur', 'Blur', 'A soft blur.', 'out of focus, rain on the window, low quality',
      [R('sigma_limit', 0.1, 8, (0.5, 2), 0.1)], p=0.3),
    T('ZoomBlur', 'blur', 'Zoom blur', 'Streaks out from the centre.', 'a camera moving forward, zooming',
      [R('max_factor', 1, 1.5, (1, 1.1), 1)], p=0.1),
    T('GlassBlur', 'blur', 'Glass blur', 'Pixels shuffled locally, like frosted glass.', 'looking through a window or a dirty cover',
      [F('sigma', 0, 2, 0.7, 0), F('max_delta', 1, 8, 2, 1, integer=True)], p=0.1),
    # geometry
    T('HorizontalFlip', 'geometry', 'Mirror', 'Flips left and right.', 'the same scene seen from the other side', [], p=0.5),
    T('Affine', 'geometry', 'Scale, rotate, shift', 'Zoom, turn, move and shear; boxes follow.', 'another camera height, distance or angle',
      [R('scale', 0.5, 1.5, (0.9, 1.1), 1), R('rotate', -45, 45, (-10, 10), 0), R('translate_percent', -0.3, 0.3, (-0.05, 0.05), 0),
       R('shear', -30, 30, (0, 0), 0)], p=0.5),
    T('Perspective', 'geometry', 'Perspective', 'Tilts the view as from another angle; boxes follow.', 'a camera mounted higher, lower or sideways',
      [R('scale', 0, 0.2, (0.02, 0.06), 0)], p=0.3),
    T('SafeRotate', 'geometry', 'Rotate (whole image kept)', 'Turns the image without cutting it; boxes follow.', 'a tilted camera',
      [R('limit', -90, 90, (-15, 15), 0)], p=0.3),
    T('BBoxSafeRandomCrop', 'geometry', 'Crop around the boxes', 'Crops a random part that keeps every box.', 'a narrower view, digital zoom',
      [F('erosion_rate', 0, 0.5, 0, 0)], p=0.3),
    # occlusion
    T('CoarseDropout', 'occlusion', 'Holes (occlusion)', 'Black rectangles over parts of the image.', 'objects partly hidden by others',
      [R('num_holes_range', 1, 20, (1, 4), 1, integer=True), R('hole_height_range', 0.02, 0.5, (0.05, 0.15), 0.02),
       R('hole_width_range', 0.02, 0.5, (0.05, 0.15), 0.02)], p=0.3),
]
BY_NAME = {t['t']: t for t in CATALOG}


def _step(t, p=None, **args):
    meta = BY_NAME[t]
    out = {'t': t, 'p': meta['p_default'] if p is None else p, 'on': True, 'args': {a['k']: copy.copy(a['default']) for a in meta['args']}}
    out['args'].update(args)
    return out


PRESETS = {
    'day': ('Day outdoor', 'Sun, shade and colour shifts of a day outside.',
            [_step('RandomBrightnessContrast'), _step('RandomGamma', 0.3), _step('HueSaturationValue', 0.3), _step('RandomShadow', 0.2),
             _step('HorizontalFlip')]),
    'night': ('Night / low light', 'Dark, noisy, low-contrast frames.',
              [_step('RandomBrightnessContrast', 0.7, brightness_limit=[-0.5, -0.15], contrast_limit=[-0.3, 0]),
               _step('RandomGamma', 0.5, gamma_limit=[120, 180]), _step('ISONoise', 0.5, intensity=[0.3, 0.8]),
               _step('GaussianBlur', 0.2), _step('ToGray', 0.15)]),
    'weather': ('Bad weather', 'Rain, fog, snow and a wet lens.',
                [_step('RandomRain', 0.25), _step('RandomFog', 0.25), _step('RandomSnow', 0.1), _step('Spatter', 0.1),
                 _step('RandomBrightnessContrast', 0.5, brightness_limit=[-0.3, 0.1])]),
    'camera': ('New camera (generic)', 'Colour, sharpness, lens and compression of an unknown camera.',
               [_step('PlanckianJitter', 0.4), _step('RandomToneCurve', 0.4), _step('HueSaturationValue', 0.4), _step('Sharpen', 0.2),
                _step('Defocus', 0.15), _step('OpticalDistortion', 0.2), _step('ImageCompression', 0.4, quality_range=[50, 95]),
                _step('GaussNoise', 0.2)]),
    'cheap': ('Cheap / low-res camera', 'Low resolution, blocky compression, noise and colour fringes.',
              [_step('Downscale', 0.6, scale_range=[0.3, 0.7]), _step('ImageCompression', 0.6, quality_range=[20, 60]),
               _step('ISONoise', 0.4), _step('ChromaticAberration', 0.3), _step('GaussianBlur', 0.3)]),
    'motion': ('Motion & vibration', 'Moving objects and a shaking camera.',
               [_step('MotionBlur', 0.5, blur_limit=[5, 15]), _step('ZoomBlur', 0.1), _step('Affine', 0.4, rotate=[-5, 5])]),
    'indoor': ('Indoor lighting', 'Warm lamps, uneven light and colour casts.',
               [_step('PlanckianJitter', 0.5), _step('Illumination', 0.4), _step('RGBShift', 0.3), _step('RandomGamma', 0.3)]),
    'geometry': ('Geometry (YOLO-like)', 'Flip, scale, shift and a little perspective, like YOLO training.',
                 [_step('HorizontalFlip'), _step('Affine', 0.7, scale=[0.6, 1.4], translate_percent=[-0.1, 0.1], rotate=[0, 0]),
                  _step('Perspective', 0.2)]),
}

# the image-conditions advice (image/shift.py, image/stress.py) names albumentations transforms: map them to catalog steps
_ADVICE = {'RandomBrightnessContrast': 'RandomBrightnessContrast', 'RandomGamma': 'RandomGamma', 'RandomShadow': 'RandomShadow',
           'RandomFog': 'RandomFog', 'RGBShift': 'RGBShift', 'MotionBlur': 'MotionBlur', 'ISONoise': 'ISONoise',
           'GaussNoise': 'GaussNoise', 'ImageCompression': 'ImageCompression', 'GaussianBlur': 'GaussianBlur', 'Downscale': 'Downscale',
           'HueSaturationValue': 'HueSaturationValue', 'ColorJitter': 'ColorJitter', 'Defocus': 'Defocus', 'CLAHE': 'CLAHE',
           'ToGray': 'ToGray', 'PlanckianJitter': 'PlanckianJitter', 'Sharpen': 'Sharpen', 'RandomRain': 'RandomRain'}


def steps_from_advice(names):
    """Albumentations names in advice text -> catalog steps (the known ones, once each)."""
    out, seen = [], set()
    for n in names:
        t = _ADVICE.get(str(n))
        if t and t not in seen:
            seen.add(t)
            out.append(_step(t))
    return out


def catalog():
    return {'groups': [{'id': g, 'label': lbl} for g, lbl in GROUPS], 'transforms': CATALOG,
            'presets': [{'id': k, 'label': lbl, 'help': h, 'recipe': {'version': 1, 'mode': 'all', 'n': 2, 'strength': 1.0, 'steps': s}}
                        for k, (lbl, h, s) in PRESETS.items()]}


# ---------------------------------------------------------------- checking and scaling a recipe
def _num(v, a, what):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f'{what}: a number') from None
    f = min(a['max'], max(a['min'], f))
    if a.get('int') or a['kind'] == 'int':
        f = int(round(f))
        if a.get('odd') and f % 2 == 0:
            f = f + 1 if f + 1 <= a['max'] else f - 1
    return f


def _arg(a, v, what):
    if a['kind'] == 'choice':
        if v not in a['choices']:
            raise ValueError(f'{what}: one of {", ".join(map(str, a["choices"]))}')
        return v
    if a['kind'] == 'range':
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ValueError(f'{what}: [low, high]')
        lo, hi = sorted((_num(v[0], a, what), _num(v[1], a, what)))
        return [lo, hi]
    return _num(v, a, what)


def validate(recipe):
    """-> the recipe with known transforms and arguments only, every value inside its range. ValueError says what is wrong."""
    if not isinstance(recipe, dict):
        raise ValueError('a recipe is an object')
    steps = recipe.get('steps') or []
    if not isinstance(steps, list):
        raise ValueError('steps: a list')
    if len(steps) > MAX_STEPS:
        raise ValueError(f'a recipe holds at most {MAX_STEPS} steps')
    mode = recipe.get('mode') or 'all'
    if mode not in ('all', 'one_of', 'some_of'):
        raise ValueError('mode: all, one_of or some_of')
    out = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or s.get('t') not in BY_NAME:
            raise ValueError(f'step {i + 1}: unknown augmentation {s.get("t") if isinstance(s, dict) else s!r}')
        meta = BY_NAME[s['t']]
        known = {a['k']: a for a in meta['args']}
        args = {a['k']: copy.copy(a['default']) for a in meta['args']}
        for k, v in (s.get('args') or {}).items():
            if k not in known:
                raise ValueError(f'step {i + 1} ({s["t"]}): no argument {k!r}')
            args[k] = _arg(known[k], v, f'step {i + 1} ({s["t"]}) {k}')
        p = min(1.0, max(0.0, float(s.get('p', meta['p_default']))))
        out.append({'t': s['t'], 'p': round(p, 3), 'on': s.get('on', True) is not False, 'args': args})
    n = int(recipe.get('n') or 2)
    strength = min(2.0, max(0.0, float(recipe.get('strength', 1.0))))
    return {'version': 1, 'mode': mode, 'n': max(1, min(n, max(1, len(out)))), 'strength': round(strength, 3), 'steps': out}


def recipe_hash(recipe):
    return hashlib.sha1(json.dumps(validate(recipe), sort_keys=True).encode()).hexdigest()[:16]


def _scaled(a, v, s):
    """A value moved toward (s < 1) or away from (s > 1) the identity, inside the argument's range."""
    if a['kind'] == 'choice' or s == 1 or a.get('identity') is None:
        return v
    idt = a['identity']
    if a['kind'] == 'range':
        return [_num(idt + (x - idt) * s, a, a['k']) for x in v]
    return _num(idt + (v - idt) * s, a, a['k'])


def _kwargs(step, strength):
    meta = BY_NAME[step['t']]
    kw = {a['k']: _scaled(a, step['args'][a['k']], strength) for a in meta['args']}
    kw = {k: tuple(v) if isinstance(v, list) else v for k, v in kw.items()}
    if step['t'] == 'MotionBlur':
        lo, hi = kw['blur_limit']
        kw['blur_limit'] = (max(3, lo | 1), max(3, hi | 1))
    if step['t'] == 'GaussianBlur':
        kw['blur_limit'] = 0                                   # from sigma
    return kw


def _albu():
    try:
        import albumentations as A
    except ImportError as e:  # pragma: no cover - the visualizer zone installs it
        raise RuntimeError('albumentations is not installed (pip install -r requirements/visualizer.txt)') from e
    return A


def build(recipe, seed=0):
    A = _albu()
    r = validate(recipe)
    on = [s for s in r['steps'] if s['on'] and s['p'] > 0] if r['strength'] > 0 else []
    ts = [getattr(A, s['t'])(p=s['p'], **_kwargs(s, r['strength'])) for s in on]
    if r['mode'] == 'one_of' and ts:
        ts = [A.OneOf(ts, p=1.0)]
    elif r['mode'] == 'some_of' and ts:
        ts = [A.SomeOf(ts, n=min(r['n'], len(ts)), p=1.0)]
    return A.Compose(ts, bbox_params=A.BboxParams(format='pascal_voc', label_fields=['cls'], min_visibility=0.3, clip=True),
                     seed=int(seed), save_applied_params=True)


def apply(rgb, boxes, recipe, seed=0, compose=None):
    """rgb: uint8 (H, W, 3); boxes [(cls, x1, y1, x2, y2)] px -> (image, boxes, dropped, applied names)."""
    import numpy as np
    t = compose or build(recipe, seed)
    if compose is not None:
        t.set_random_seed(int(seed))
    h, w = rgb.shape[:2]
    good = [(c, max(0.0, x1), max(0.0, y1), min(float(w), x2), min(float(h), y2)) for c, x1, y1, x2, y2 in boxes]
    good = [b for b in good if b[3] > b[1] + 0.5 and b[4] > b[2] + 0.5]
    res = t(image=np.ascontiguousarray(rgb), bboxes=[b[1:] for b in good] or np.zeros((0, 4), np.float32), cls=[b[0] for b in good])
    out = [(int(c), *map(float, b)) for c, b in zip(res['cls'], res['bboxes'], strict=False)]
    applied = [n for n, _ in res.get('applied_transforms') or [] if n not in ('Compose', 'OneOf', 'SomeOf')]
    return res['image'], out, len(good) - len(out), applied


# ---------------------------------------------------------------- interchange
def to_albu_json(recipe):
    A = _albu()
    return A.to_dict(build(recipe))


def from_albu_json(d):
    """An albumentations dict (A.to_dict / A.save) -> (recipe, names it skipped). Only catalog transforms come in."""
    tr = (d or {}).get('transform') or d or {}
    mode, items = 'all', tr.get('transforms') or []
    if len(items) == 1 and str(items[0].get('__class_fullname__', '')).rsplit('.', 1)[-1] in ('OneOf', 'SomeOf'):
        inner = items[0]
        mode = 'one_of' if inner['__class_fullname__'].endswith('OneOf') else 'some_of'
        items = inner.get('transforms') or []
    steps, skipped = [], []
    for it in items:
        name = str(it.get('__class_fullname__', '')).rsplit('.', 1)[-1]
        if name not in BY_NAME:
            skipped.append(name or '?')
            continue
        known = {a['k'] for a in BY_NAME[name]['args']}
        args = {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in it.items() if k in known}
        steps.append({'t': name, 'p': it.get('p', BY_NAME[name]['p_default']), 'args': args})
    return validate({'mode': mode, 'steps': steps}), skipped


def to_python(recipe):
    r = validate(recipe)
    on = [s for s in r['steps'] if s['on']]

    def lit(v):
        return repr(tuple(v)) if isinstance(v, list) else repr(v)
    lines = [f'    A.{s["t"]}({", ".join([*(f"{k}={lit(v)}" for k, v in _kwargs(s, r["strength"]).items()), f"p={s["p"]}"])}),' for s in on]
    body = '\n'.join(lines) or '    # (no steps)'
    if r['mode'] == 'one_of':
        body = '    A.OneOf([\n' + '\n'.join('    ' + x for x in lines) + '\n    ], p=1.0),'
    elif r['mode'] == 'some_of':
        body = '    A.SomeOf([\n' + '\n'.join('    ' + x for x in lines) + f'\n    ], n={r["n"]}, p=1.0),'
    return ('# made in the dataviz Augment tab\nimport albumentations as A\n\n'
            'transform = A.Compose([\n' + body + '\n], bbox_params=A.BboxParams(format="yolo", label_fields=["class_labels"], min_visibility=0.3))\n')


def to_ultralytics(recipe):
    """A training script for Ultralytics (8.4 takes Albumentations transforms as `augmentations=`)."""
    r = validate(recipe)
    code = to_python(r).split('transform = A.Compose([\n', 1)[1].rsplit('\n], bbox_params', 1)[0]
    return ('# made in the dataviz Augment tab: train YOLO with this recipe on top of its own augmentation\n'
            'import albumentations as A\nfrom ultralytics import YOLO\n\n'
            'augmentations = [\n' + code + '\n]\n\n'
            'model = YOLO("yolo11n.pt")\n'
            'model.train(data="data.yaml", epochs=100, imgsz=640, augmentations=augmentations)\n')


def to_yaml(recipe):
    import yaml
    return yaml.safe_dump(validate(recipe), sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------- box check: do the labels survive the recipe?
def box_check(load, names, recipe, k=2, seed=0, min_px=8, train_size=640, classes=(), progress=None, cancelled=None):
    """Run the recipe k times on each image and count what happens to the boxes.

    load(name) -> (uint8 image, boxes [(cls, x1, y1, x2, y2)] px). A box is *lost* when less than 30 % of it stays
    in the image, *tiny* when its short side is under min_px once the image is resized to train_size on its long
    side (what YOLO trains on), and *at the edge* when it now touches the border (cut by a crop or a shift).
    -> {totals, classes: [...], transforms: [...], warnings: [...], examples: [...]}."""
    from collections import Counter
    compose = build(recipe, seed)
    cls_st, tr_st, examples = {}, {}, []
    tot = Counter()

    def edge(b, w, h):
        return b[1] <= 0.5 or b[2] <= 0.5 or b[3] >= w - 0.5 or b[4] >= h - 0.5
    for i, name in enumerate(names):
        if cancelled is not None and cancelled():
            break
        rgb, boxes = load(name)
        h, w = rgb.shape[:2]
        good = [b for b in boxes if b[3] - b[1] > 0.5 and b[4] - b[2] > 0.5]
        cin, ein = Counter(int(b[0]) for b in good), Counter(int(b[0]) for b in good if edge(b, w, h))
        for j in range(k):
            s = seed * 1000 + i * 10 + j
            im, out, dropped, applied = apply(rgb, good, recipe, s, compose=compose)
            hh, ww = im.shape[:2]
            f = train_size / max(hh, ww)
            cout = Counter(c for c, *_ in out)
            tiny = Counter(c for c, x1, y1, x2, y2 in out if min(x2 - x1, y2 - y1) * f < min_px)
            eout = Counter(c for c, *b in out if edge((c, *b), ww, hh))
            n_tiny = sum(tiny.values())
            for c in set(cin) | set(cout):
                st = cls_st.setdefault(c, Counter())
                st['in'] += cin[c]
                st['kept'] += cout[c]
                st['lost'] += max(0, cin[c] - cout[c])
                st['tiny'] += tiny[c]
                st['edge'] += max(0, eout[c] - ein[c])
            tot.update({'variants': 1, 'in': len(good), 'kept': len(out), 'lost': dropped, 'tiny': n_tiny,
                        'edge': sum(max(0, eout[c] - ein[c]) for c in eout)})
            for t in set(applied):
                ts = tr_st.setdefault(t, Counter())
                ts.update({'variants': 1, 'in': len(good), 'lost': dropped, 'tiny': n_tiny})
            if (dropped or n_tiny) and len(examples) < 12:
                examples.append({'name': name, 'seed': s, 'lost': dropped, 'tiny': n_tiny, 'applied': applied})
        if progress is not None:
            progress(i + 1, len(names), 'checking boxes')
    pct = lambda a, b: round(100.0 * a / b, 1) if b else 0.0
    cls_rows = [{'cls': c, 'name': classes[c] if 0 <= c < len(classes) else str(c), 'in': st['in'], 'kept': st['kept'],
                 'lost': st['lost'], 'tiny': st['tiny'], 'edge': st['edge'], 'lost_pct': pct(st['lost'], st['in']),
                 'tiny_pct': pct(st['tiny'], st['kept'])} for c, st in sorted(cls_st.items())]
    tr_rows = sorted(({'t': t, 'label': BY_NAME.get(t, {}).get('label', t), 'variants': ts['variants'], 'in': ts['in'], 'lost': ts['lost'],
                       'tiny': ts['tiny'], 'lost_pct': pct(ts['lost'], ts['in']), 'tiny_pct': pct(ts['tiny'], ts['in'])}
                      for t, ts in tr_st.items()), key=lambda r: -(r['lost_pct'] + r['tiny_pct']))
    warn = []
    for r in tr_rows:
        if r['lost_pct'] >= 5 and r['variants'] >= 3:
            warn.append(f'{r["label"]}: {r["lost_pct"]}% of the boxes are lost in the images it ran on.')
        if r['tiny_pct'] >= 10 and r['variants'] >= 3:
            warn.append(f'{r["label"]}: {r["tiny_pct"]}% of the boxes become smaller than {min_px} px at {train_size}.')
    for r in cls_rows:
        if r['lost_pct'] >= 10 and r['in'] >= 5:
            warn.append(f'“{r["name"]}”: {r["lost_pct"]}% of its boxes are lost.')
    return {'images': len(names), 'variants': tot['variants'], 'boxes_in': tot['in'], 'kept': tot['kept'], 'lost': tot['lost'],
            'tiny': tot['tiny'], 'edge': tot['edge'], 'lost_pct': pct(tot['lost'], tot['in']), 'tiny_pct': pct(tot['tiny'], tot['kept']),
            'classes': cls_rows, 'transforms': tr_rows, 'warnings': warn[:12], 'examples': examples,
            'min_px': min_px, 'train_size': train_size, 'k': k}
