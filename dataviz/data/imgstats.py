"""
Image conditions: per-image light / sharpness / noise / colour / capture metrics, what the Compare tab's "Image
conditions" section (data/shift.py) compares between two members of a combined dataset to tell whether a model
trained on A will meet the same pictures in B (a new camera, new lighting).

One decode per image (PIL, then cv2 / numpy, which release the GIL: a thread pool):
    header     size, format, EXIF camera / ISO / exposure / f-number, JPEG quantisation tables (quality estimate)
    decode     JPEG draft: decoded at 1/2..1/8 while the long side stays >= DRAFT px (most of the cost)
    noise      Immerkaer's fast sigma estimate on a <= 512 px centre crop of the decoded pixels
    the rest   on the image shrunk by an integer factor to a long side in (WORK/2, WORK] (INTER_AREA): about one
               scale for every camera, so sharpness and edge density compare across resolutions
Every metric is a float32 column (METRICS: key, group, label, log-scale flag); NaN = not available (EXIF missing,
not a JPEG). `hist` per image: 32-bin luma + 32-bin R, G, B shares (the light / colour curves) + the 4x4 grid of
mean luma (the light map: vignetting, uneven light, light direction).

Cache <member workspace>/imgstats.npz (atomic replace), keyed by the member's own image name + image_sig: every
combined dataset that contains the member reuses it, and only new / changed images are read again.
"""

import hashlib
import io
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

VERSION = 4            # 4: light layout (grid, vignetting, direction), haze, CCT, blur direction, chroma noise,
                      #    JPEG blocking, EXIF hour; 3: sharpness net of noise (floored), edges on a sigma-1 blur
FILE = 'imgstats.npz'
WORK = 512           # the light / sharpness / colour metrics: long side cut by an integer factor to <= this
DRAFT = 1024         # JPEG draft decode keeps the long side at least this
NOISE_CROP = 512
NHIST = 32
GRID = 4             # the light map: GRID x GRID cells of mean luma
HIST_W = 4 * NHIST + GRID * GRID

# key, group, label, log scale (for the histograms), unit / hint
METRICS = [
    ('brightness', 'light', 'brightness (mean luma)', False, '0-255'),
    ('contrast', 'light', 'contrast (luma std)', False, '0-255'),
    ('dark_clip', 'light', 'crushed shadows (% luma <= 8)', False, '%'),
    ('bright_clip', 'light', 'blown highlights (% luma >= 247)', False, '%'),
    ('dyn_range', 'light', 'dynamic range (p99 - p1)', False, '0-255'),
    ('entropy', 'light', 'tonal entropy', False, 'bits'),
    ('midtone', 'light', 'midtones (median luma)', False, '0-255'),
    ('local_contrast', 'light', 'local contrast (16 px block std)', False, '0-255'),
    ('vignetting', 'light', 'vignetting (corners / centre luma)', False, 'ratio'),
    ('uniformity', 'light', 'uneven light (4x4 grid CV)', False, '%'),
    ('light_dx', 'light', 'light direction left(-) / right(+)', False, '%'),
    ('light_dy', 'light', 'light direction top(-) / bottom(+)', False, '%'),
    ('haze', 'light', 'haze / glare (dark channel)', False, '0-255'),
    ('sharpness', 'sharpness', 'sharpness (Laplacian var, noise removed)', True, ''),
    ('tenengrad', 'sharpness', 'edge strength (Tenengrad, noise removed)', True, ''),
    ('edge_density', 'sharpness', 'edge density (% Canny)', False, '%'),
    ('blur_aniso', 'sharpness', 'blur direction (0 round / 1 one way: motion)', False, '0-1'),
    ('noise', 'noise', 'noise sigma (Immerkaer)', True, '0-255'),
    ('chroma_noise', 'noise', 'colour noise sigma (Lab a*, b*)', False, '0-255'),
    ('r_mean', 'colour', 'red mean', False, '0-255'),
    ('g_mean', 'colour', 'green mean', False, '0-255'),
    ('b_mean', 'colour', 'blue mean', False, '0-255'),
    ('saturation', 'colour', 'saturation (HSV)', False, '0-255'),
    ('colorfulness', 'colour', 'colourfulness (Hasler)', False, ''),
    ('cast_a', 'colour', 'tint green(-) / magenta(+) (Lab a*)', False, ''),
    ('cast_b', 'colour', 'tint blue(-) / yellow(+) (Lab b*)', False, ''),
    ('wb_rg', 'colour', 'white balance R / G', False, 'ratio'),
    ('wb_bg', 'colour', 'white balance B / G', False, 'ratio'),
    ('cct', 'colour', 'colour temperature (McCamy)', False, 'K'),
    ('width', 'capture', 'width', False, 'px'),
    ('height', 'capture', 'height', False, 'px'),
    ('megapixels', 'capture', 'megapixels', True, 'MP'),
    ('aspect', 'capture', 'aspect w / h', False, ''),
    ('bytes_per_px', 'capture', 'file bytes per pixel', True, 'B/px'),
    ('jpeg_q', 'capture', 'JPEG quality (estimated)', False, '1-100'),
    ('blockiness', 'capture', 'JPEG blocking (8 px edges / inside)', False, 'ratio'),
    ('iso', 'capture', 'ISO (EXIF)', True, ''),
    ('exposure', 'capture', 'exposure time (EXIF)', True, 's'),
    ('fnumber', 'capture', 'f-number (EXIF)', False, 'f/'),
    ('hour', 'capture', 'time of day (EXIF)', False, 'h'),
]
KEYS = [m[0] for m in METRICS]
COL = {k: i for i, k in enumerate(KEYS)}
GROUPS = ['light', 'sharpness', 'noise', 'colour', 'capture']

# IJG standard luminance quantisation table (quality 50)
_STD_LUMA = np.array([
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99], np.float64)
_IMMERKAER = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)


def jpeg_quality(qtables):
    """IJG quality (1..100) a JPEG's luminance table was scaled from, NaN without tables."""
    if not qtables:
        return math.nan
    t = qtables.get(0) if isinstance(qtables, dict) else None
    if t is None or len(t) != 64:
        return math.nan
    scale = float(np.asarray(t, np.float64).sum() / _STD_LUMA.sum() * 100.0)   # order-free: sums, not positions
    if scale <= 0:
        return math.nan
    q = (200.0 - scale) / 2.0 if scale <= 100 else 5000.0 / scale
    return float(min(100.0, max(1.0, q)))


def _ratio(v):
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if float(v[1]) else math.nan
        f = float(v)
        return f if math.isfinite(f) else math.nan
    except (TypeError, ValueError, ZeroDivisionError):
        return math.nan


NO_EXIF = ('', math.nan, math.nan, math.nan, math.nan)


def _hour(v):
    """'YYYY:MM:DD HH:MM:SS' -> hour + minute / 60, NaN when it does not parse."""
    try:
        t = str(v).strip().strip('\x00').split(' ')[1].split(':')
        h, mi = int(t[0]), int(t[1])
        return h + mi / 60.0 if 0 <= h < 24 and 0 <= mi < 60 else math.nan
    except (IndexError, ValueError, TypeError):
        return math.nan


def _exif(im):
    """(camera 'Make Model' or '', iso, exposure s, f-number, hour of day) from the header."""
    try:
        ex = im.getexif()
    except Exception:          # noqa: BLE001 - broken EXIF block: no camera facts
        return NO_EXIF
    if not ex:
        return NO_EXIF
    make = str(ex.get(0x010F) or '').strip().strip('\x00')
    model = str(ex.get(0x0110) or '').strip().strip('\x00')
    if make and model.lower().startswith(make.lower()):
        make = ''
    cam = ' '.join(x for x in (make, model) if x)[:64]
    try:
        sub = ex.get_ifd(0x8769)
    except Exception:          # noqa: BLE001
        sub = {}
    iso = sub.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    dt = sub.get(0x9003) or sub.get(0x9004) or ex.get(0x0132)
    return (cam, _ratio(iso) if iso is not None else math.nan, _ratio(sub.get(0x829A)), _ratio(sub.get(0x829D)),
            _hour(dt) if dt else math.nan)


_SOBEL_GAIN = []


def _sobel_noise_gain():
    """Sum of squares of the sigma-1 Gaussian + 3x3 Sobel kernel: the variance one gradient component gets from unit
    white noise (computed once from an impulse)."""
    if not _SOBEL_GAIN:
        import cv2
        d = np.zeros((31, 31), np.float32)
        d[15, 15] = 1.0
        k = cv2.Sobel(cv2.GaussianBlur(d, (0, 0), 1.0), cv2.CV_32F, 1, 0, ksize=3)
        _SOBEL_GAIN.append(float((k * k).sum()))
    return _SOBEL_GAIN[0]


_DIR_GAIN = []


def _dir_derivs(ys):
    """Second derivatives of `ys` along 0, 45, 90 and 135 degrees (3x3 Sobel)."""
    import cv2
    dxx = cv2.Sobel(ys, cv2.CV_32F, 2, 0, ksize=3)
    dyy = cv2.Sobel(ys, cv2.CV_32F, 0, 2, ksize=3)
    dxy = cv2.Sobel(ys, cv2.CV_32F, 1, 1, ksize=3)
    return dxx, 0.5 * (dxx + dyy) + dxy, dyy, 0.5 * (dxx + dyy) - dxy


def blur_direction(ys, sigma):
    """0 = blur the same every way (defocus / none), towards 1 = edges smeared one way (motion blur): 1 - min / max of
    the second-derivative energy along 4 directions of the sigma-1 blurred luma `ys`, white noise of `sigma` removed.
    A second derivative falls with the cube of the smear, so a few pixels of motion stand out of the content's own
    direction mix."""
    import cv2
    if not _DIR_GAIN:                                        # unit white noise -> energy per direction (once)
        d = np.zeros((31, 31), np.float32)
        d[15, 15] = 1.0
        _DIR_GAIN.extend(float((k * k).sum()) for k in _dir_derivs(cv2.GaussianBlur(d, (0, 0), 1.0)))
    yv = ys.astype(np.float32)
    e = [max(float(cv2.mean(k * k)[0]) - sigma * sigma * g, 0.0) for k, g in zip(_dir_derivs(yv), _DIR_GAIN)]
    hi = max(e)
    return 1.0 - min(e) / hi if hi > 1e-6 else 0.0


def noise_sigma(gray):
    """Immerkaer (1996) fast noise sigma of a grayscale float32 image, in 0..255 units."""
    import cv2
    h, w = gray.shape[:2]
    if h < 3 or w < 3:
        return math.nan
    r = cv2.filter2D(gray, cv2.CV_32F, _IMMERKAER, borderType=cv2.BORDER_REPLICATE)[1:-1, 1:-1]
    return float(np.abs(r).sum(dtype=np.float64) * math.sqrt(math.pi / 2.0) / (6.0 * (w - 2) * (h - 2)))


def _pct(cum, n, p):
    return float(np.searchsorted(cum, p * n))


# sRGB -> linear lookup (0..255 -> 0..1) and linear sRGB -> CIE XYZ (D65)
_LIN = np.where(np.arange(256) / 255.0 <= 0.04045, np.arange(256) / 255.0 / 12.92,
                ((np.arange(256) / 255.0 + 0.055) / 1.055) ** 2.4)
_XYZ = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])


def cct_of_rgb(lin_rgb):
    """McCamy (1992) correlated colour temperature in kelvin of a mean linear sRGB triple, clipped to 1500..20000."""
    X, Y, Z = _XYZ @ np.asarray(lin_rgb, np.float64)
    s = X + Y + Z
    if s <= 1e-9:
        return math.nan
    x, y = X / s, Y / s
    if abs(0.1858 - y) < 1e-9:
        return math.nan
    nn = (x - 0.3320) / (0.1858 - y)
    return float(min(20000.0, max(1500.0, 449.0 * nn ** 3 + 3525.0 * nn ** 2 + 6823.3 * nn + 5520.33)))


def blockiness(gray):
    """JPEG 8x8 blocking of a grayscale crop (aligned to the image's own 8 px grid): the mean |step| across block
    boundaries / the mean |step| inside blocks. ~1 = no blocking, > 1.2 = visible blocks."""
    g = gray.astype(np.float32)
    if g.shape[0] < 32 or g.shape[1] < 32:
        return math.nan
    dx = np.abs(np.diff(g, axis=1))                          # dx[:, j] = step between column j and j + 1
    dy = np.abs(np.diff(g, axis=0))
    cb = np.arange(dx.shape[1]) % 8 == 7
    rb = np.arange(dy.shape[0]) % 8 == 7
    inside = float(dx[:, ~cb].mean() + dy[~rb].mean())
    edge = float(dx[:, cb].mean() + dy[rb].mean())
    return edge / inside if inside > 0.05 else math.nan


def measure_array(rgb, *, size=None, nbytes=None, fmt='', qtables=None, exif=NO_EXIF):
    """(vec float32[len(METRICS)], hist float32[HIST_W]) of an RGB uint8 array. size: the original (w, h)."""
    import cv2
    h0, w0 = rgb.shape[:2]
    W, H = size or (w0, h0)
    vec = np.full(len(KEYS), np.nan, np.float32)
    # noise on the decoded pixels (centre crop) - before any resize smooths it away
    cy, cx, hc = h0 // 2, w0 // 2, NOISE_CROP // 2
    y0, x0 = max(0, cy - hc) // 8 * 8, max(0, cx - hc) // 8 * 8      # on the 8 px JPEG block grid
    crgb = np.ascontiguousarray(rgb[y0:y0 + NOISE_CROP, x0:x0 + NOISE_CROP])
    crop = cv2.cvtColor(crgb, cv2.COLOR_RGB2GRAY)
    vec[COL['noise']] = noise_sigma(crop.astype(np.float32))
    clab = cv2.cvtColor(crgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    vec[COL['chroma_noise']] = 0.5 * (noise_sigma(np.ascontiguousarray(clab[..., 1])) +
                                      noise_sigma(np.ascontiguousarray(clab[..., 2])))
    # blocking only means something on the pixels as decoded (a drafted JPEG keeps a scaled grid: 8 / scale)
    if fmt == 'JPEG' and (W, H) == (w0, h0):
        vec[COL['blockiness']] = blockiness(crop)
    k = -(-max(h0, w0) // WORK)                            # an integer factor: OpenCV's fast INTER_AREA path
    small = cv2.resize(rgb, (max(1, w0 // k), max(1, h0 // k)), interpolation=cv2.INTER_AREA) if k > 1 else rgb
    y = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    n = y.size
    h256 = np.bincount(y.ravel(), minlength=256).astype(np.float64)
    cum = np.cumsum(h256)
    lv = np.arange(256, dtype=np.float64)
    mean = float((h256 * lv).sum() / n)
    vec[COL['brightness']] = mean
    vec[COL['contrast']] = math.sqrt(max(0.0, float((h256 * (lv - mean) ** 2).sum() / n)))
    vec[COL['dark_clip']] = 100.0 * float(h256[:9].sum() / n)
    vec[COL['bright_clip']] = 100.0 * float(h256[247:].sum() / n)
    vec[COL['dyn_range']] = _pct(cum, n, 0.99) - _pct(cum, n, 0.01)
    p64 = h256.reshape(64, 4).sum(1) / n
    nz = p64[p64 > 0]
    vec[COL['entropy']] = float(-(nz * np.log2(nz)).sum())
    vec[COL['midtone']] = _pct(cum, n, 0.5)
    yf32 = y.astype(np.float32)
    mu = cv2.blur(yf32, (16, 16))
    vec[COL['local_contrast']] = float(np.sqrt(np.maximum(cv2.blur(yf32 * yf32, (16, 16)) - mu * mu, 0)).mean())
    grid = cv2.resize(yf32, (GRID, GRID), interpolation=cv2.INTER_AREA).astype(np.float64)
    gm = max(float(grid.mean()), 1e-3)
    corners = (grid[0, 0] + grid[0, -1] + grid[-1, 0] + grid[-1, -1]) / 4.0
    centre = float(grid[1:3, 1:3].mean())
    vec[COL['vignetting']] = corners / max(centre, 1e-3)
    vec[COL['uniformity']] = 100.0 * float(grid.std()) / gm
    vec[COL['light_dx']] = 100.0 * float(grid[:, GRID // 2:].mean() - grid[:, :GRID // 2].mean()) / gm
    vec[COL['light_dy']] = 100.0 * float(grid[GRID // 2:].mean() - grid[:GRID // 2].mean()) / gm
    dark = cv2.erode(small.min(axis=2), np.ones((7, 7), np.uint8))
    vec[COL['haze']] = float(dark.mean(dtype=np.float64))
    # sharpness without the noise: a noisy soft image must not read as sharp. Laplacian variance minus the part
    # white noise of the work image's own sigma adds (sum of the 3x3 kernel's squares = 20); edges on a sigma-1 blur
    yf = y.astype(np.float32)
    sw = noise_sigma(yf)
    lap = float(cv2.Laplacian(yf, cv2.CV_32F).var())
    vec[COL['sharpness']] = max(0.02 * lap, lap - 20.0 * sw * sw, 0.01)   # floored: a log scale, never 0
    ys = cv2.GaussianBlur(y, (0, 0), 1.0)
    gx = cv2.Sobel(ys, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(ys, cv2.CV_32F, 0, 1, ksize=3)
    g2 = float(cv2.mean(gx * gx + gy * gy)[0])
    vec[COL['blur_aniso']] = blur_direction(ys, sw)
    vec[COL['tenengrad']] = math.sqrt(max(0.0, g2 - 2.0 * sw * sw * _sobel_noise_gain()))
    med = _pct(cum, n, 0.5)
    lo, hi = max(5.0, 0.66 * med), min(255.0, max(20.0, 1.33 * med))
    vec[COL['edge_density']] = 100.0 * float(np.count_nonzero(cv2.Canny(ys, lo, hi)) / n)
    f = small.reshape(-1, 3)
    mR, mG, mB = (float(v) for v in f.mean(0, dtype=np.float64))
    vec[COL['r_mean']], vec[COL['g_mean']], vec[COL['b_mean']] = mR, mG, mB
    vec[COL['wb_rg']] = mR / max(mG, 1e-3)
    vec[COL['wb_bg']] = mB / max(mG, 1e-3)
    bc = [np.bincount(f[:, c], minlength=256) for c in range(3)]          # reused for the RGB curves below
    vec[COL['cct']] = cct_of_rgb([float(bc[c] @ _LIN) / max(1, len(f)) for c in range(3)])
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    vec[COL['saturation']] = float(hsv[..., 1].mean(dtype=np.float64))
    ff = small.astype(np.float32)
    m_rg, s_rg = (float(x[0, 0]) for x in cv2.meanStdDev(ff[..., 0] - ff[..., 1]))
    m_yb, s_yb = (float(x[0, 0]) for x in cv2.meanStdDev(0.5 * (ff[..., 0] + ff[..., 1]) - ff[..., 2]))
    vec[COL['colorfulness']] = float(math.hypot(s_rg, s_yb) + 0.3 * math.hypot(m_rg, m_yb))
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB)
    vec[COL['cast_a']] = float(lab[..., 1].mean(dtype=np.float64)) - 128.0
    vec[COL['cast_b']] = float(lab[..., 2].mean(dtype=np.float64)) - 128.0
    vec[COL['width']], vec[COL['height']] = W, H
    vec[COL['megapixels']] = W * H / 1e6
    vec[COL['aspect']] = W / max(1, H)
    if nbytes:
        vec[COL['bytes_per_px']] = nbytes / max(1, W * H)
    vec[COL['jpeg_q']] = jpeg_quality(qtables) if fmt == 'JPEG' else math.nan
    _, vec[COL['iso']], vec[COL['exposure']], vec[COL['fnumber']], vec[COL['hour']] = exif
    hist = np.empty(HIST_W, np.float32)
    hist[:NHIST] = h256.reshape(NHIST, -1).sum(1) / n
    for c in range(3):
        hist[NHIST * (c + 1):NHIST * (c + 2)] = bc[c].reshape(NHIST, -1).sum(1) / n
    hist[4 * NHIST:] = (grid / 255.0).ravel()
    return vec, hist


def measure(data):
    """(vec, hist, camera, format) of one image file's bytes. ValueError when it is not an image PIL reads."""
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        size = im.size
        fmt = im.format or ''
        exif = _exif(im)
        q = getattr(im, 'quantization', None)
        if fmt == 'JPEG' and max(size) >= 2 * DRAFT:
            r = DRAFT / max(size)
            im.draft('RGB', (max(1, int(size[0] * r)), max(1, int(size[1] * r))))
        im = ImageOps.exif_transpose(im)
        rgb = np.asarray(im.convert('RGB'))
    except Exception as e:     # noqa: BLE001 - truncated / unknown file: the caller skips it
        raise ValueError(f'not a readable image: {e}') from None
    if size[0] != size[1] and (rgb.shape[1] > rgb.shape[0]) != (size[0] > size[1]):
        size = (size[1], size[0])                          # EXIF orientation turned it: the size as shown
    vec, hist = measure_array(rgb, size=size, nbytes=len(data), fmt=fmt, qtables=q, exif=exif)
    return vec, hist, exif[0], fmt


# ---------------------------------------------------------------- cache + sample

@dataclass
class Table:
    names: list           # the images asked for that could be measured, in the order asked
    vec: np.ndarray       # float32[n, len(KEYS)]
    hist: np.ndarray      # float32[n, 4*NHIST]
    camera: list          # str per image ('' = no EXIF camera)
    fmt: list             # str per image
    measured: int = 0     # how many were read now (the rest came from the cache)
    failed: int = 0


_locks = {}
_locks_lock = threading.Lock()


def _lock(path):
    with _locks_lock:
        return _locks.setdefault(str(path), threading.Lock())


def _load(path):
    try:
        with np.load(path, allow_pickle=False) as z:
            if int(z['version']) != VERSION or list(z['keys']) != KEYS:
                return {}
            names, sigs = z['names'].tolist(), z['sigs'].tolist()
            vec, hist = z['vec'], z['hist'].astype(np.float32)
            cams, fmts = z['cams'].tolist(), z['fmts'].tolist()
            cam, fm = z['cam'], z['fmt']
    except (OSError, ValueError, KeyError):
        return {}
    return {n: (s, vec[i], hist[i], cams[cam[i]], fmts[fm[i]]) for i, (n, s) in enumerate(zip(names, sigs))}


def _save(path, rows):
    names = sorted(rows)
    cams = sorted({rows[n][3] for n in names} | {''})
    fmts = sorted({rows[n][4] for n in names} | {''})
    ci, fi = {c: i for i, c in enumerate(cams)}, {f: i for i, f in enumerate(fmts)}
    k = len(names)
    vec = np.stack([rows[n][1] for n in names]).astype(np.float32) if k else np.zeros((0, len(KEYS)), np.float32)
    hist = np.stack([rows[n][2] for n in names]).astype(np.float16) if k else np.zeros((0, HIST_W), np.float16)
    tmp = Path(path).with_name(f'{FILE}.{os.getpid()}.{threading.get_ident()}.tmp')
    with open(tmp, 'wb') as fh:
        np.savez(fh, version=np.int32(VERSION), keys=np.array(KEYS), names=np.array(names, dtype=str),
                 sigs=np.array([rows[n][0] for n in names], dtype=str), vec=vec, hist=hist,
                 cams=np.array(cams, dtype=str), fmts=np.array(fmts, dtype=str),
                 cam=np.array([ci[rows[n][3]] for n in names], np.int32),
                 fmt=np.array([fi[rows[n][4]] for n in names], np.int32))
    os.replace(tmp, path)


def sample(names, k):
    """A deterministic sample of `k` names (0 / None = all): ordered by a hash of the name, so a bigger sample is a
    superset of a smaller one and every run reuses the cache."""
    names = list(names)
    if not k or len(names) <= k:
        return names
    key = lambda n: hashlib.blake2b(n.encode(), digest_size=8).digest()
    return sorted(names, key=key)[:int(k)]


def ensure(ws, names, read_bytes, sig_of=None, progress=None, cancelled=None, workers=None, save_every=2000):
    """Table of `names` (one member's own image names), measuring what the cache at <ws>/imgstats.npz lacks or holds
    for another sig. read_bytes(name) -> bytes; sig_of(name) -> str | None. cancelled: threading.Event."""
    from ..core import hardware
    path = Path(ws) / FILE
    with _lock(path):
        rows = _load(path) if path.exists() else {}
    sigs = {}
    todo = []
    for n in names:
        try:
            s = sig_of(n) if sig_of else None
        except (OSError, KeyError, ValueError):
            s = None
        s = '' if s is None else str(s)
        sigs[n] = s
        r = rows.get(n)
        if r is None or (s and r[0] and r[0] != s):
            todo.append(n)

    def one(n):
        if cancelled is not None and cancelled.is_set():
            return n, None
        try:
            v, h, cam, fmt = measure(read_bytes(n))
        except (OSError, KeyError, ValueError):
            return n, False
        return n, (sigs[n], v, h, cam, fmt)

    failed = set()
    if todo:
        done = 0
        dirty = 0
        with ThreadPoolExecutor(max(1, int(workers or hardware.workers('io'))), thread_name_prefix='imgstats') as ex:
            for n, r in ex.map(one, todo):
                done += 1
                if r is None:
                    continue
                if r is False:
                    failed.add(n)
                else:
                    rows[n] = r
                    dirty += 1
                if progress and (done % 100 == 0 or done == len(todo)):
                    progress(done, len(todo), 'measuring image conditions')
                if dirty >= save_every:                      # a long run cancelled later keeps what it measured
                    with _lock(path):
                        _save(path, {**_load(path), **rows} if path.exists() else rows)
                    dirty = 0
        if cancelled is not None and cancelled.is_set():
            with _lock(path):
                _save(path, rows)
            raise InterruptedError('cancelled')
        with _lock(path):
            merged = {**_load(path), **rows} if path.exists() else rows
            _save(path, merged)
    got = [n for n in names if n in rows and n not in failed]
    k = len(got)
    return Table(names=got,
                 vec=np.stack([rows[n][1] for n in got]).astype(np.float32) if k else np.zeros((0, len(KEYS)), np.float32),
                 hist=np.stack([rows[n][2] for n in got]).astype(np.float32) if k else np.zeros((0, HIST_W), np.float32),
                 camera=[rows[n][3] for n in got], fmt=[rows[n][4] for n in got],
                 measured=len(todo) - len(failed), failed=len(failed))


def metrics_info():
    return [{'key': k, 'group': g, 'label': lb, 'log': lg, 'unit': u} for k, g, lb, lg, u in METRICS]

