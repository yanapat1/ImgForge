"""Render the assistant dock mockups to PNG: .venv/bin/python design/assistant/_src/render.py"""
from pathlib import Path
from playwright.sync_api import sync_playwright

SRC = Path(__file__).resolve().parent
OUT = SRC.parent
SIZES = {'desktop': (1600, 1000), 'narrow': (420, 900)}
SHOTS = [(v, t, s, '') for v in (1, 2, 3) for t in ('light', 'dark') for s in SIZES]
SHOTS += [(1, 'dark', 'desktop', 'data'), (1, 'light', 'desktop', 'collapsed')]

with sync_playwright() as p:
    b = p.chromium.launch()
    for v, theme, size, extra in SHOTS:
        w, h = SIZES[size]
        pg = b.new_page(viewport={'width': w, 'height': h}, device_scale_factor=1)
        q = f'theme={theme}' + ('&scene=data' if extra == 'data' else '') + ('&collapsed=1' if extra == 'collapsed' else '')
        pg.goto(f'{(SRC / f"variant{v}.html").as_uri()}?{q}')
        pg.wait_for_timeout(250)
        name = f'v{v}-{theme}-{size}' + (f'-{extra}' if extra else '') + '.png'
        pg.screenshot(path=str(OUT / name))
        pg.close()
        print(name)
    b.close()
