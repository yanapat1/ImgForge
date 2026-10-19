"""
Class colours that stay distinguishable for colour-blind labelers.

Candidates are an OKLCH grid (L 0.45..0.90 step 0.05, C 0.08..0.25 step 0.01, h every 5 deg) kept only when
inside the sRGB gamut (5755 of them). Each candidate is converted to CIELAB under 4 visions: normal plus
protanopia, deuteranopia and tritanopia (Machado, Oliveira & Fernandes 2009, severity 1.0, applied in linear RGB). The sequence is a
greedy max-min: starting from Okabe-Ito orange, every next colour is the candidate whose smallest CIEDE2000
distance to the chosen ones, over all 4 visions, is largest. A running min array makes each pick O(N).

Measured (this machine, numpy), worst vision of the 4:
    first 25 colours: min pairwise dE2000 = 8.22 (normal-only is higher); all 64: 4.76
    generation of the 64: ~300 ms, once, lazily (the routes warm it in a thread at start)
The plan's narrower grid (L 0.55..0.85, C 0.08..0.20) measured only 6.39 for 25; a target of 12 for 25 colours
under all 3 dichromacies is not reachable with any grid tried (the widest, L 0.40..0.95 with greys, gave 9.37
but adds dull / near-black colours that vanish on photos), so 8.2 is the honest guarantee (tests assert >= 8).
"""

import threading
import time

import numpy as np

SEED = '#e69f00'                     # Okabe-Ito orange: salient and safe under every CVD
N_PALETTE = 64

# Machado 2009, severity 1.0, linear RGB
_CVD = {
    'protan': np.array([[0.152286, 1.052583, -0.204868],
                        [0.114503, 0.786281, 0.099216],
                        [-0.003882, -0.048116, 1.051998]]),
    'deutan': np.array([[0.367322, 0.860646, -0.227968],
                        [0.280085, 0.672501, 0.047413],
                        [-0.011820, 0.042940, 0.968881]]),
    'tritan': np.array([[1.255528, -0.076749, -0.178779],
                        [-0.078411, 0.930809, 0.147602],
                        [0.004733, 0.691367, 0.303900]]),
}
VISIONS = ('normal',) + tuple(_CVD)

_RGB2XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                     [0.2126729, 0.7151522, 0.0721750],
                     [0.0193339, 0.1191920, 0.9503041]])
_WHITE = np.array([0.95047, 1.0, 1.08883])


# ------------------------------------------------------------ conversions (all vectorised over rows)
def _oklch_to_linear(L, C, h):
    a, b = C * np.cos(np.radians(h)), C * np.sin(np.radians(h))
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return np.stack([4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
                     -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
                     -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_], axis=-1)


def _to_srgb(lin):
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)


def _to_linear(srgb):
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def _lab(lin):
    xyz = (np.clip(lin, 0, 1) @ _RGB2XYZ.T) / _WHITE
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def _labs(lin):
    """(4, N, 3) Lab of linear-RGB rows under each vision (simulated colours are clipped back into gamut)."""
    return np.stack([_lab(lin)] + [_lab(lin @ m.T) for m in _CVD.values()])


def ciede2000(lab1, lab2):
    """CIEDE2000 between broadcastable (..., 3) Lab arrays."""
    L1, a1, b1 = np.moveaxis(lab1, -1, 0)
    L2, a2, b2 = np.moveaxis(lab2, -1, 0)
    c_bar = (np.hypot(a1, b1) + np.hypot(a2, b2)) / 2
    g = 0.5 * (1 - np.sqrt(c_bar ** 7 / (c_bar ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, c2p - c1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(c1p * c2p == 0, 0, dh)
    dHp = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    l_bar, c_bar_p = (L1 + L2) / 2, (c1p + c2p) / 2
    hs = h1p + h2p
    h_bar = np.where(c1p * c2p == 0, hs,
                     np.where(np.abs(h1p - h2p) <= 180, hs / 2, np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)))
    t = (1 - 0.17 * np.cos(np.radians(h_bar - 30)) + 0.24 * np.cos(np.radians(2 * h_bar))
         + 0.32 * np.cos(np.radians(3 * h_bar + 6)) - 0.20 * np.cos(np.radians(4 * h_bar - 63)))
    d_theta = 30 * np.exp(-(((h_bar - 275) / 25) ** 2))
    rc = 2 * np.sqrt(c_bar_p ** 7 / (c_bar_p ** 7 + 25.0 ** 7))
    sl = 1 + 0.015 * (l_bar - 50) ** 2 / np.sqrt(20 + (l_bar - 50) ** 2)
    sc = 1 + 0.045 * c_bar_p
    sh = 1 + 0.015 * c_bar_p * t
    rt = -np.sin(np.radians(2 * d_theta)) * rc
    return np.sqrt((dLp / sl) ** 2 + (dCp / sc) ** 2 + (dHp / sh) ** 2 + rt * (dCp / sc) * (dHp / sh))


# ------------------------------------------------------------ hex helpers
def _hex(srgb):
    r, g, b = (int(round(float(v) * 255)) for v in np.clip(srgb, 0, 1))
    return f'#{r:02x}{g:02x}{b:02x}'


def _parse(c):
    c = c.strip().lstrip('#')
    if len(c) == 3:
        c = ''.join(ch * 2 for ch in c)
    if len(c) != 6:
        raise ValueError(f'not a #rrggbb colour: {c!r}')
    return np.array([int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def _lab_hex(colors):
    return _labs(_to_linear(np.array([_parse(c) for c in colors]).reshape(-1, 3)))


# ------------------------------------------------------------ the sequence
def _candidates():
    L, C, h = np.meshgrid(np.arange(0.45, 0.901, 0.05), np.arange(0.08, 0.251, 0.01), np.arange(0, 360, 5),
                          indexing='ij')
    L, C, h = L.ravel(), C.ravel(), h.ravel()
    lin = _oklch_to_linear(L, C, h)
    ok = np.all((lin >= -1e-4) & (lin <= 1 + 1e-4), axis=1)
    srgb = _to_srgb(lin[ok])
    srgb = np.round(srgb * 255) / 255                    # what a #rrggbb can actually show
    return srgb, h[ok]


def _generate(n):
    srgb, hue = _candidates()
    labs = _labs(_to_linear(srgb))                       # (4, N, 3)
    seed_lab = _lab_hex([SEED])                          # (4, 1, 3)
    out, min_de = [SEED], [float('inf')]
    min_d = ciede2000(labs, seed_lab).min(axis=0)        # (N,) worst-vision distance to the chosen set
    prev_h = float(np.degrees(np.arctan2(*_lab_hex([SEED])[0, 0, [2, 1]])) % 360)
    for _ in range(n - 1):
        best = min_d.max()
        # ties (within 0.05 dE) prefer a hue far from the previous pick, so neighbours in the list differ
        tie = np.flatnonzero(min_d >= best - 0.05)
        dh = np.abs((hue[tie] - prev_h + 180) % 360 - 180)
        i = int(tie[np.argmax(dh)])
        out.append(_hex(srgb[i]))
        min_de.append(float(min_d[i]))
        prev_h = float(hue[i])
        min_d = np.minimum(min_d, ciede2000(labs, labs[:, i:i + 1]).min(axis=0))
    return out, min_de


_cache = {}
_lock = threading.Lock()


def _palette_full():
    with _lock:
        if 'p' not in _cache:
            t0 = time.perf_counter()
            _cache['p'] = _generate(N_PALETTE)
            _cache['ms'] = (time.perf_counter() - t0) * 1000
        return _cache['p']


def palette(n=N_PALETTE):
    """The first n colours (cycles past 64, which no sane class list reaches)."""
    cols = _palette_full()[0]
    return [cols[i % len(cols)] for i in range(max(0, n))]


def min_de(n=N_PALETTE):
    """For each palette colour, its smallest dE2000 (worst vision) to the colours before it."""
    vals = _palette_full()[1]
    return [vals[i % len(vals)] if i < len(vals) else 0.0 for i in range(max(0, n))]


def distance(a, b):
    """Smallest CIEDE2000 between two #rrggbb colours over normal + 3 CVD visions."""
    la, lb = _lab_hex([a]), _lab_hex([b])
    return float(ciede2000(la, lb).min())


def next_color(used):
    """The first palette colour not in `used`; past 64 the one farthest from every used colour."""
    used_l = {u.lower() for u in used if u}
    for c in palette():
        if c not in used_l:
            return c
    cols = palette()
    lu = _lab_hex(list(used_l))
    scores = [float(ciede2000(_lab_hex([c]), lu).min()) for c in cols]
    return cols[int(np.argmax(scores))]


def too_close(c, used, threshold=10.0):
    """[(used colour, dE, vision)] for the used colours closer than `threshold` under some vision."""
    if not used:
        return []
    lc, lu = _lab_hex([c]), _lab_hex(list(used))
    d = ciede2000(lc, lu)                                # (4, len(used))
    out = []
    for j, u in enumerate(used):
        v = int(np.argmin(d[:, j]))
        if d[v, j] < threshold:
            out.append((u, float(d[v, j]), VISIONS[v]))
    return out
