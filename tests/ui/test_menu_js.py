"""static/ts/core/menu.ts run under node (skipped without node / esbuild): where a sub-menu opens (beside its row,
flipped to the left at the screen edge, clamped on screen), the safe-triangle test that keeps a sub-menu open while
the pointer heads for it, and a sub-menu with no row shown is left out of the menu."""

import shutil
import subprocess
from pathlib import Path

import pytest

MENU_TS = Path(__file__).resolve().parents[2] / 'static' / 'ts' / 'core' / 'menu.ts'

SCRIPT = r"""
import assert from 'node:assert/strict';
import { placeSub, inTriangle, sub, item, menuHtml, SEP } from './menu.mjs';

const row = { left: 100, right: 320, top: 200, bottom: 232 };
const menu = { left: 96, right: 324, top: 150, bottom: 500 };
// room on the right: beside the row, its top at the row
assert.deepEqual(placeSub(row, menu, 220, 160, 1280, 800), { left: 318, top: 196, flipped: false });
// no room on the right: flipped to the left of the menu
const far = { left: 1000, right: 1220, top: 200, bottom: 232 }, farMenu = { left: 996, right: 1224, top: 150, bottom: 500 };
const f = placeSub(far, farMenu, 220, 160, 1280, 800);
assert.equal(f.flipped, true);
assert.equal(f.left, 996 - 220 + 2);
// near the bottom: moved up so it stays on screen
assert.equal(placeSub({ ...row, top: 760, bottom: 792 }, menu, 220, 300, 1280, 800).top, 800 - 8 - 300);
// no room on either side: kept on screen, not flipped off it
const tiny = placeSub({ left: 4, right: 200, top: 10, bottom: 40 }, { left: 0, right: 204, top: 0, bottom: 300 }, 300, 100, 320, 400);
assert.equal(tiny.flipped, false);
assert.ok(tiny.left >= 8 && tiny.left + 300 <= 320 - 8 + 300);

// the safe triangle: from the last pointer point to the sub-menu's near corners
const a = { x: 300, y: 216 }, b = { x: 318, y: 196 }, c = { x: 318, y: 356 };
assert.equal(inTriangle({ x: 310, y: 250 }, a, b, c), true);       // heading down-right into the sub-menu
assert.equal(inTriangle({ x: 290, y: 260 }, a, b, c), false);      // moving back into the menu
assert.equal(inTriangle(b, a, b, c), true);                         // an edge counts

// a sub-menu with nothing to show is left out; separators only between non-empty groups
assert.equal(sub({ ico: 'cloud', label: 'Cloud (S3)', groups: [[false, null], []] }), '');
const s = sub({ ico: 'send', label: 'Send to', groups: [[item({ act: 'send', ico: 'scatter', label: 'Visualize' })], [false]] });
assert.match(s, /aria-haspopup="menu"/);
assert.match(s, /data-sub/);
assert.match(s, /role="menu"/);
assert.equal((s.match(/class="sep"/g) || []).length, 0);
assert.equal(menuHtml([[item({ act: 'a', ico: 'x', label: 'A' })], [], [item({ act: 'b', ico: 'x', label: 'B' })]]).split(SEP).length, 2);
// a disabled row keeps its reason
assert.match(item({ act: 'rename', ico: 'text', label: 'Rename…', dis: 'Streamed from S3' }), /disabled title="Streamed from S3"/);
console.log('ok');
"""


def test_menu_js(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    esbuild = Path(__file__).resolve().parents[2] / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.exists():
        pytest.skip('run `npm install` to get esbuild')
    b = subprocess.run([str(esbuild), str(MENU_TS), '--bundle', '--format=esm', '--platform=neutral',
                        f'--outfile={tmp_path / "menu.mjs"}'], capture_output=True, text=True, timeout=60)
    assert b.returncode == 0, b.stderr
    (tmp_path / 't.mjs').write_text(SCRIPT)
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert r.returncode == 0 and 'ok' in r.stdout, r.stderr + r.stdout
