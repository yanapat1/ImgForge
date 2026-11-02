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
not a JPEG). `hist` per image: 32-bin luma + 32-bin R, G, B shares (the light / colour curves).

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

VERSION = 1
FILE = 'imgstats.npz'
WORK = 512           # the light / sharpness / colour metrics: long side cut by an integer factor to <= this
DRAFT = 1024         # JPEG draft decode keeps the long side at least this
NOISE_CROP = 512
NHIST = 32

# key, group, label, log scale (for the histograms), unit / hint
METRICS = [
    ('brightness', 'light', 'brightness (mean luma)', False, '0-255'),
    ('contrast', 'light', 'contrast (luma std)', False, '0-255'),
    ('dark_clip', 'light', 'crushed shadows (% luma <= 8)', False, '%'),
    ('bright_clip', 'light', 'blown highlights (% luma >= 247)', False, '%'),
    ('dyn_range', 'light', 'dynamic range (p99 - p1)', False, '0-255'),
    ('entropy', 'light', 'tonal entropy', False, 'bits'),
    ('sharpness', 'sharpness', 'sharpness (Laplacian variance)', True, ''),
    ('tenengrad', 'sharpness', 'edge strength (Sobel)', True, ''),
    ('edge_density', 'sharpness', 'edge density (% Canny)', False, '%'),
    ('noise', 'noise', 'noise sigma (Immerkaer)', True, '0-255'),
    ('r_mean', 'colour', 'red mean', False, '0-255'),
    ('g_mean', 'colour', 'green mean', False, '0-255'),
    ('b_mean', 'colour', 'blue mean', False, '0-255'),
    ('saturation', 'colour', 'saturation (HSV)', False, '0-255'),
    ('colorfulness', 'colour', 'colourfulness (Hasler)', False, ''),
    ('cast_a', 'colour', 'tint green(-) / magenta(+) (Lab a*)', False, ''),
    ('cast_b', 'colour', 'tint blue(-) / yellow(+) (Lab b*)', False, ''),
    ('wb_rg', 'colour', 'white balance R / G', False, 'ratio'),
    ('wb_bg', 'colour', 'white balance B / G', False, 'ratio'),
    ('width', 'capture', 'width', False, 'px'),
    ('height', 'capture', 'height', False, 'px'),
    ('megapixels', 'capture', 'megapixels', True, 'MP'),
    ('aspect', 'capture', 'aspect w / h', False, ''),
    ('bytes_per_px', 'capture', 'file bytes per pixel', True, 'B/px'),
    ('jpeg_q', 'capture', 'JPEG quality (estimated)', False, '1-100'),
    ('iso', 'capture', 'ISO (EXIF)', True, ''),
    ('exposure', 'capture', 'exposure time (EXIF)', True, 's'),
    ('fnumber', 'capture', 'f-number (EXIF)', False, 'f/'),
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


def _exif(im):
    """(camera 'Make Model' or '', iso, exposure s, f-number) from the header."""
    try:
        ex = im.getexif()
    except Exception:          # noqa: BLE001 - broken EXIF block: no camera facts
        return '', math.nan, math.nan, math.nan
    if not ex:
        return '', math.nan, math.nan, math.nan
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
    return cam, _ratio(iso) if iso is not None else math.nan, _ratio(sub.get(0x829A)), _ratio(sub.get(0x829D))


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


def measure_array(rgb, *, size=None, nbytes=None, fmt='', qtables=None, exif=('', math.nan, math.nan, math.nan)):
    """(vec float32[len(METRICS)], hist float32[4*NHIST]) of an RGB uint8 array. size: the original (w, h)."""
    import cv2
    h0, w0 = rgb.shape[:2]
    W, H = size or (w0, h0)
    vec = np.full(len(KEYS), np.nan, np.float32)
    # noise on the decoded pixels (centre crop) - before any resize smooths it away
    cy, cx, hc = h0 // 2, w0 // 2, NOISE_CROP // 2
    crop = cv2.cvtColor(np.ascontiguousarray(rgb[max(0, cy - hc):cy + hc, max(0, cx - hc):cx + hc]), cv2.COLOR_RGB2GRAY)
    vec[COL['noise']] = noise_sigma(crop.astype(np.float32))
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
    vec[COL['sharpness']] = float(cv2.Laplacian(y, cv2.CV_32F).var())
    gx = cv2.Sobel(y, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(y, cv2.CV_32F, 0, 1, ksize=3)
    vec[COL['tenengrad']] = float(cv2.magnitude(gx, gy).mean())
    med = _pct(cum, n, 0.5)
    lo, hi = max(5.0, 0.66 * med), min(255.0, max(20.0, 1.33 * med))
    vec[COL['edge_density']] = 100.0 * float(np.count_nonzero(cv2.Canny(y, lo, hi)) / n)
    f = small.reshape(-1, 3)
    mR, mG, mB = (float(v) for v in f.mean(0, dtype=np.float64))
    vec[COL['r_mean']], vec[COL['g_mean']], vec[COL['b_mean']] = mR, mG, mB
    vec[COL['wb_rg']] = mR / max(mG, 1e-3)
    vec[COL['wb_bg']] = mB / max(mG, 1e-3)
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
    _, vec[COL['iso']], vec[COL['exposure']], vec[COL['fnumber']] = exif
    hist = np.empty(4 * NHIST, np.float32)
    hist[:NHIST] = h256.reshape(NHIST, -1).sum(1) / n
    for c in range(3):
        hist[NHIST * (c + 1):NHIST * (c + 2)] = np.bincount(f[:, c] >> 3, minlength=NHIST)[:NHIST] / n
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
    hist = np.stack([rows[n][2] for n in names]).astype(np.float16) if k else np.zeros((0, 4 * NHIST), np.float16)
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
                 hist=np.stack([rows[n][2] for n in got]).astype(np.float32) if k else np.zeros((0, 4 * NHIST), np.float32),
                 camera=[rows[n][3] for n in got], fmt=[rows[n][4] for n in got],
                 measured=len(todo) - len(failed), failed=len(failed))


def metrics_info():
    return [{'key': k, 'group': g, 'label': lb, 'log': lg, 'unit': u} for k, g, lb, lg, u in METRICS]

