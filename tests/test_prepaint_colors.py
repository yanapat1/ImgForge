"""The critical <style> in static/index.html paints the page background and text before any stylesheet arrives (no
white flash). It repeats --bg / --text of every theme, look and palette by hand, so it is checked here against the
token files: a colour changed in tokens.css / palettes.css without index.html fails this test."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'static'


def _blocks(css):
    """[(selector, {--bg, --text})] for every rule of the file that sets one of them, in source order."""
    out = []
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', re.sub(r'/\*.*?\*/', '', css, flags=re.S)):
        vals = dict(re.findall(r'(--bg|--text)\s*:\s*([^;]+);', body))
        if vals:
            out.append((sel.strip(), {k: v.strip().lower() for k, v in vals.items()}))
    return out


def _attrs(sel):
    """The [data-…="…"] conditions of a ':root[…][…]' selector; None for any other selector."""
    if not sel.startswith(':root'):
        return None
    rest = sel[len(':root'):]
    got = dict(re.findall(r'\[data-(\w+)="?([\w-]+)"?\]', rest))
    return got if re.fullmatch(r'(\[data-\w+="?[\w-]+"?\])*', rest) else None


def _resolve(blocks, want):
    v = {}
    for sel, vals in blocks:
        cond = _attrs(sel)
        if cond is not None and all(want.get(k) == x for k, x in cond.items()):
            v.update(vals)
    return v


def _prepaint():
    html = (STATIC / 'index.html').read_text()
    style = re.search(r'<style>(.*?)</style>', html, re.S).group(1)
    out = {}
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', style):
        bg, fg = re.search(r'background:(#[0-9a-f]+)', body), re.search(r'color:(#[0-9a-f]+)', body)
        if not (bg and fg):
            continue
        first = sel.split(',')[0]
        cond = dict(re.findall(r'\[data-(\w+)=([\w-]+)\]', first))
        out[(cond.get('look', 'default'), cond.get('palette', 'default'), cond.get('theme', 'dark'))] = (bg[1].lower(), fg[1].lower())
    return out


def test_prepaint_colours_match_the_tokens():
    blocks = _blocks((STATIC / 'css/tokens.css').read_text()) + _blocks((STATIC / 'css/palettes.css').read_text())
    pre = _prepaint()
    assert len(pre) >= 20, pre                          # 6 looks + 5 palettes, light and dark
    for (look, pal, theme), (bg, fg) in pre.items():
        want = {'theme': theme}
        if look != 'default':
            want['look'] = look
        if pal != 'default':
            want['palette'] = pal
        v = _resolve(blocks, want)
        assert (v.get('--bg'), v.get('--text')) == (bg, fg), f'{look}/{pal}/{theme}: index.html {bg} {fg}, tokens {v}'
