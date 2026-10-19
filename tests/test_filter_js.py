"""static/ts/core/filter.ts (built to static/js/core/filter.js) run under node (skipped without node): class bitsets for every class id (the
'all' mode compared a signed int32 with an unsigned word: classes 31, 63, ... never matched), deleted boxes,
and the regex guard."""

import shutil
import subprocess
from pathlib import Path

import pytest

FILTER_TS = Path(__file__).resolve().parents[1] / 'static' / 'ts' / 'core' / 'filter.ts'

SCRIPT = r"""
import assert from 'node:assert/strict';
import { buildAgg, runFilter, emptyFilter } from './filter.mjs';

// one image per class id 0..99 plus an image with classes 31 and 63, one with 5 only
const cls = [], image = [];
for (let c = 0; c < 100; c++) { cls.push(c); image.push(`img_${c}.jpg`); }
cls.push(31, 63, 5); image.push('both.jpg', 'both.jpg', 'five.jpg');
const P = { cls: Int32Array.from(cls), image, session: image.map(() => 's') };
const agg = buildAgg(P, 100);
const run = (mode, ids, scope = 'image') => {
  const F = emptyFilter(); F.cls = { mode, ids }; F.scope = scope;
  return runFilter(F, P, agg);
};
for (const c of [0, 5, 30, 31, 32, 63, 64, 95, 99]) {
  assert.equal(run('all', [c]).nImg, c === 5 ? 2 : (c === 31 || c === 63) ? 2 : 1, `all [${c}]`);
  assert.equal(run('any', [c]).nImg, run('all', [c]).nImg, `any [${c}]`);
}
assert.equal(run('all', [31, 63]).nImg, 1);                 // only both.jpg
assert.equal(run('all', [31, 63, 5]).nImg, 0);
assert.equal(run('none', [31]).nImg, agg.nImg - 2);
assert.equal(run('all', [31], 'box').nBox, 2);
// a deleted box no longer gives its image the class
P.deleted = new Uint8Array(P.cls.length); P.deleted[100] = 1;  // the 31 of both.jpg
const fresh = buildAgg(P, 100);
const F = emptyFilter(); F.cls = { mode: 'all', ids: [31, 63] };
assert.equal(runFilter(F, P, fresh).nImg, 0);
// regex: plain ones work, nested repetition is refused (it would freeze the tab), never thrown
const R = emptyFilter(); R.name = { mode: 'regex', q: '/^img_3\\d\\.jpg$/', neg: false };
assert.equal(runFilter(R, P, fresh).nImg, 10);
R.name = { mode: 'regex', q: '/(a+)+$/', neg: false };
const bad = runFilter(R, P, buildAgg(P, 100));
assert.match(bad.error, /nested repetition/);
console.log('ok');
"""


def test_filter_js(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    esbuild = Path(__file__).resolve().parents[1] / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.exists():
        pytest.skip('run `npm install` to get esbuild')
    b = subprocess.run([str(esbuild), str(FILTER_TS), '--format=esm', f'--outfile={tmp_path / "filter.mjs"}'],
                       capture_output=True, text=True, timeout=60)
    assert b.returncode == 0, b.stderr
    (tmp_path / 't.mjs').write_text(SCRIPT)
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr
