import numpy as np

from dataviz.labeling import colors

# measured 8.22 (worst of normal / protan / deutan / tritan) for the first 25: see colors.py docstring
MIN_DE_25 = 8.0


def test_first_25_distinct_under_every_vision():
    p = colors.palette(25)
    assert len(set(p)) == 25 and p[0] == colors.SEED
    labs = colors._lab_hex(p)                                    # (4, 25, 3)
    d = colors.ciede2000(labs[:, :, None, :], labs[:, None, :, :])
    d = d + np.eye(25)[None] * 1e9
    assert d.min() >= MIN_DE_25, d.min(axis=(1, 2))


def test_palette_is_hex_and_cached():
    p = colors.palette(64)
    assert len(p) == 64 and all(len(c) == 7 and c.startswith('#') for c in p)
    assert colors.palette(64) == p and colors.palette(70)[64] == p[0]
    de = colors.min_de(64)
    assert de[0] == float('inf') and all(de[i] >= de[i + 1] - 1.5 for i in range(1, 63))   # roughly decreasing


def test_next_color_and_distance():
    p = colors.palette(5)
    assert colors.next_color([]) == p[0]
    assert colors.next_color(p[:3]) == p[3]
    assert colors.next_color([p[0].upper()]) == p[1]
    assert colors.distance('#ff0000', '#ff0000') < 1e-6
    assert colors.distance(p[0], p[1]) >= MIN_DE_25
    # red vs green collapse for deutans: the min over visions is far below the normal-vision distance
    assert colors.distance('#d03030', '#30a030') < 30


def test_too_close():
    assert colors.too_close('#e69f00', ['#e69f01'])[0][0] == '#e69f01'
    assert colors.too_close('#e69f00', []) == []
    assert colors.too_close(colors.palette(2)[1], [colors.palette(1)[0]]) == []
