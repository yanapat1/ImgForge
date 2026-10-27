"""static/ts/core/compare-core.ts run under node (skipped without node / esbuild): the 'DVC1' column decoder
(views, f2 widening, bad magic), origin / diff masks, the density-difference grid (counts, mass-preserving
blur, minCount, novelty), shared histogram edges, KS, class deltas, id selection and a 500k-point timing check."""

import shutil
import subprocess
from pathlib import Path

import pytest

CORE_TS = Path(__file__).resolve().parents[1] / 'static' / 'ts' / 'core' / 'compare-core.ts'

SCRIPT = r"""
import assert from 'node:assert/strict';
import { decodeColumns, originMask, densityDiff, sharedEdges, histPair, classDelta, selectIds, BOX } from './core.mjs';

// ---- decodeColumns: build a DVC1 buffer by hand
const encode = (meta, cols) => {
  const size = { u1: 1, i1: 1, u2: 2, i2: 2, i4: 4, u4: 4, f2: 2, f4: 4 };
  let specs = cols.map(c => ({ name: c.name, dtype: c.dtype, offset: 0, length: c.bytes.length / size[c.dtype] }));
  let head, H, off;
  for (let pass = 0; pass < 2; pass++) {          // offsets depend on the header length
    head = new TextEncoder().encode(JSON.stringify({ meta, cols: specs }));
    H = head.length; off = Math.ceil((8 + H) / 8) * 8;
    specs = specs.map((s, k) => { const o = off; off = Math.ceil((off + cols[k].bytes.length) / 8) * 8; return { ...s, offset: o }; });
  }
  head = new TextEncoder().encode(JSON.stringify({ meta, cols: specs }));
  assert.equal(head.length, H);
  const buf = new ArrayBuffer(off), u8 = new Uint8Array(buf);
  u8.set([0x44, 0x56, 0x43, 0x31]); new DataView(buf).setUint32(4, H, true); u8.set(head, 8);
  specs.forEach((s, k) => u8.set(cols[k].bytes, s.offset));
  return { buf, specs };
};
const bytes = ta => new Uint8Array(ta.buffer, ta.byteOffset, ta.byteLength);
// float16 bit patterns: 1.5 = 0x3e00, -2 = 0xc000, 65504 = 0x7bff, 0 = 0, 2^-24 = 0x0001, inf = 0x7c00
const f2 = Uint16Array.from([0x3e00, 0xc000, 0x7bff, 0x0000, 0x0001, 0x7c00]);
const { buf, specs } = encode({ hello: 'world', n: 3 }, [
  { name: 'u', dtype: 'u1', bytes: Uint8Array.from([1, 2, 255]) },
  { name: 'i', dtype: 'i4', bytes: bytes(Int32Array.from([-2, 7, 2147483647])) },
  { name: 'f', dtype: 'f4', bytes: bytes(Float32Array.from([1.5, -2, 0.25])) },
  { name: 'h', dtype: 'f2', bytes: bytes(f2) },
]);
for (const s of specs) assert.equal(s.offset % 8, 0);
const D = decodeColumns(buf);
assert.deepEqual(D.meta, { hello: 'world', n: 3 });
assert.deepEqual([...D.cols.u], [1, 2, 255]);
assert.ok(D.cols.u instanceof Uint8Array && D.cols.u.buffer === buf);      // a view, not a copy
assert.deepEqual([...D.cols.i], [-2, 7, 2147483647]);
assert.ok(D.cols.i instanceof Int32Array && D.cols.i.buffer === buf);
assert.deepEqual([...D.cols.f], [1.5, -2, 0.25]);
assert.ok(D.cols.h instanceof Float32Array);
assert.deepEqual([...D.cols.h], [1.5, -2, 65504, 0, Math.fround(2 ** -24), Infinity]);
const bad = buf.slice(0); new Uint8Array(bad)[3] = 0x32;
assert.throws(() => decodeColumns(bad), /magic/);

// ---- originMask: members 0 (other), 1 (a), 2 (b)
const origin = Uint8Array.from([1, 1, 2, 2, 0, 1, 2]);
const status = Uint8Array.from([BOX.SAME, BOX.MOVED, BOX.SAME, BOX.UNMATCHED, BOX.MOVED, BOX.IMAGE_ONLY, BOX.RECLASS]);
assert.deepEqual([...originMask(origin, status, 'a', 1, 2)], [1, 1, 0, 0, 0, 1, 0]);
assert.deepEqual([...originMask(origin, status, 'b', 1, 2)], [0, 0, 1, 1, 0, 0, 1]);
assert.deepEqual([...originMask(origin, status, 'both', 1, 2)], [1, 1, 1, 1, 0, 1, 1]);
assert.deepEqual([...originMask(origin, status, 'diff', 1, 2)], [0, 1, 0, 1, 0, 1, 1]);
assert.deepEqual([...originMask(origin, null, 'diff', 1, 2)], [1, 1, 1, 1, 0, 1, 1]);
const out = new Uint8Array(7);
assert.equal(originMask(origin, status, 'a', 1, 2, out), out);
assert.notEqual(originMask(origin, status, 'a', 1, 2, new Uint8Array(3)).length, 3);

// ---- densityDiff on a hand-built 4x4 grid. Points sit at cell centres of [0,4]^2 plus corner anchors at 0 and 4;
// bounds = [0,4] padded 2% -> x0 = -0.08, dx = 4.16/4 = 1.04: x = 0.5 -> col 0, 1.5 -> 1, 2.5 -> 2, 4 -> 3
{
  const pts = [  // [x, y, origin]
    [0, 0, 1], [4, 4, 1],                   // A anchors: cells (r0,c0), (r3,c3)
    [0.5, 0.5, 1], [0.5, 0.5, 1],           // A: 2 more in (0,0)
    [1.5, 2.5, 2], [1.5, 2.5, 2], [1.5, 2.5, 2], // B: 3 in row 2, col 1 (A has none -> novelty)
    [0.5, 0.5, 2],                          // B: 1 in (0,0)
    [3.5, 0.5, 0],                          // other member: ignored
  ];
  const n = pts.length;
  const x = Float32Array.from(pts.map(p => p[0])), y = Float32Array.from(pts.map(p => p[1]));
  const ids = Int32Array.from(pts.map((_, i) => n - 1 - i));         // sample ids reversed: origin is by id
  const org = new Uint8Array(n); pts.forEach((p, i) => { org[n - 1 - i] = p[2]; });
  const g = densityDiff(x, y, ids, org, 1, 2, null, 4, 0, 0);
  assert.equal(g.G, 4); assert.equal(g.nA, 4); assert.equal(g.nB, 4);
  assert.ok(Math.abs(g.x0 + 0.08) < 1e-9 && Math.abs(g.dx - 1.04) < 1e-9);
  assert.equal(g.cA[0], 3); assert.equal(g.cA[15], 1); assert.equal(g.cB[0], 1); assert.equal(g.cB[2 * 4 + 1], 3);
  assert.equal([...g.cA].reduce((s, v) => s + v, 0), 4);
  assert.ok(Math.abs(g.z[0] - (0.25 - 0.75)) < 1e-6);
  assert.ok(Math.abs(g.z[9] - 0.75) < 1e-6);
  assert.ok(Math.abs(g.novelty - 0.75) < 1e-9);
  // minCount: cells with fewer than 2 raw points -> NaN
  const gm = densityDiff(x, y, ids, org, 1, 2, null, 4, 0, 2);
  assert.ok(!Number.isNaN(gm.z[0]) && !Number.isNaN(gm.z[9]));
  assert.ok(Number.isNaN(gm.z[15]) && Number.isNaN(gm.z[5]));
  // mask (by sample id) drops points from the counts but not the bounds
  const mask = new Uint8Array(n).fill(1); mask[n - 1 - 4] = 0;       // one of the three B points
  const gk = densityDiff(x, y, ids, org, 1, 2, mask, 4, 0, 0);
  assert.equal(gk.nB, 3); assert.equal(gk.cB[9], 2); assert.equal(gk.x0, g.x0);
}

// blur: separable box, renormalised at the edges, so blurred pA and pB each still sum to 1
{
  const n = 5000; let seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const x = new Float32Array(n), y = new Float32Array(n), ids = new Int32Array(n), org = new Uint8Array(n);
  for (let i = 0; i < n; i++) { x[i] = rnd() ** 3; y[i] = rnd(); ids[i] = i; org[i] = i % 3 === 0 ? 1 : 2; }
  for (const blur of [1, 2, 5]) {
    const g = densityDiff(x, y, ids, org, 1, 2, null, 32, blur, 0);
    const sum = [...g.z].reduce((s, v) => s + v, 0);
    assert.ok(Math.abs(sum) < 1e-5, `blur ${blur}: z sums to ${sum}`);            // sum pB - sum pA = 0
    const one = densityDiff(x, y, ids, Uint8Array.from(org, o => (o === 2 ? 2 : 0)), 1, 2, null, 32, blur, 0);
    const sB = [...one.z].reduce((s, v) => s + v, 0);
    assert.ok(Math.abs(sB - 1) < 1e-5, `blur ${blur}: pB sums to ${sB}`);
  }
  // a single point in the corner, blur 1: mass spreads over the 2x2 in-bounds window, 1/4 each
  const g1 = densityDiff(Float32Array.from([0, 1]), Float32Array.from([0, 1]), Int32Array.from([0, 1]),
    Uint8Array.from([2, 0]), 1, 2, null, 4, 1, 0);
  assert.ok(Math.abs(g1.z[0] - 0.25) < 1e-6 && Math.abs(g1.z[5] - 0.25) < 1e-6 && g1.z[2] === 0);
}

// ---- sharedEdges
{
  const v = Float64Array.from({ length: 1001 }, (_, i) => i);
  const e = sharedEdges(v, 10, false);
  assert.equal(e.length, 11);
  for (let k = 1; k < e.length; k++) assert.ok(e[k] > e[k - 1]);
  assert.equal(e[0], 10); assert.equal(e[10], 990);
  const lv = Float64Array.from({ length: 1000 }, (_, i) => (i % 2 ? 10 ** (i / 250) : NaN)); lv[0] = -5; lv[2] = 0;
  const le = sharedEdges(lv, 8, true);
  assert.ok(le[0] > 0);
  const r0 = le[1] / le[0];
  for (let k = 1; k < le.length; k++) { assert.ok(le[k] > le[k - 1]); assert.ok(Math.abs(le[k] / le[k - 1] - r0) < 1e-9); }
  const flat = sharedEdges(Float64Array.from([3, 3, 3]), 4, false);
  assert.ok(flat[4] > flat[0]);
}

// ---- histPair + KS
{
  const n = 2000, v = new Float64Array(n), org = new Uint8Array(n);
  for (let i = 0; i < n; i++) { v[i] = (i >> 1) % 100; org[i] = i % 2 ? 2 : 1; }        // identical distributions
  const e = sharedEdges(v, 20, false);
  const h = histPair(v, org, 1, 2, null, e);
  assert.equal(h.ks, 0);
  assert.ok(Math.abs(h.ha.reduce((s, x) => s + x, 0) - 1) < 1e-9 && Math.abs(h.hb.reduce((s, x) => s + x, 0) - 1) < 1e-9);
  for (let i = 0; i < n; i++) v[i] = org[i] === 1 ? i % 50 : 1000 + (i % 50);             // disjoint
  const d = histPair(v, org, 1, 2, null, sharedEdges(v, 20, false));
  assert.ok(d.ks > 0.95, `disjoint ks ${d.ks}`);
  assert.ok(Math.abs(d.ha.reduce((s, x) => s + x, 0) - 1) < 1e-9);                     // clamped, nothing lost
  const empty = histPair(v, new Uint8Array(n), 1, 2, null, e);
  assert.equal(empty.ks, 0); assert.equal(empty.ha.reduce((s, x) => s + x, 0), 0);
  const mask = Uint8Array.from(org, o => (o === 1 ? 1 : 0));
  assert.equal(histPair(v, org, 1, 2, mask, e).hb.reduce((s, x) => s + x, 0), 0);
  // arbitrary edges (binary search) and the linear / geometric fast paths agree on edge values
  const vv = Float64Array.from([-5, 0, 0.5, 1, 9.99, 10, 50, 100, 1e6]), oo = new Uint8Array(9).fill(1);
  assert.deepEqual([...histPair(vv, oo, 1, 2, null, Float64Array.from([0, 1, 10, 100])).ha].map(x => x * 9), [3, 2, 4]);
  assert.deepEqual([...histPair(vv, oo, 1, 2, null, Float64Array.from([1, 10, 100, 1000])).ha].map(x => x * 9), [5, 2, 2]);
  assert.deepEqual([...histPair(vv, oo, 1, 2, null, Float64Array.from([0, 10, 20, 30])).ha].map(x => Math.round(x * 9)), [5, 1, 3]);
}

// ---- classDelta
{
  const cls = Int32Array.from([0, 1, 1, 2, 0, 5, -1, 1]);
  const org = Uint8Array.from([1, 1, 2, 2, 2, 1, 1, 0]);
  const del = Uint8Array.from([0, 0, 1, 0, 0, 0, 0, 0]);
  const r = classDelta(cls, org, del, 3, 1, 2);
  assert.deepEqual([...r.a], [1, 1, 0]); assert.deepEqual([...r.b], [1, 0, 1]);
  const r2 = classDelta(cls, org, null, 3, 1, 2);
  assert.deepEqual([...r2.b], [1, 1, 1]);
}

// ---- selectIds
{
  const st = Uint8Array.from([BOX.SAME, BOX.MOVED, BOX.UNMATCHED, BOX.UNMATCHED, BOX.MOVED]);
  const org = Uint8Array.from([1, 1, 1, 2, 2]);
  assert.deepEqual(selectIds(st, org, [BOX.UNMATCHED], 2), [3]);
  assert.deepEqual(selectIds(st, org, [BOX.UNMATCHED], 1), [2]);
  assert.deepEqual(selectIds(st, org, [BOX.MOVED, BOX.UNMATCHED], null), [1, 2, 3, 4]);
  assert.deepEqual(selectIds(st, org, [], null), []);
}

// ---- timing at 500k points
{
  const n = 500000; let seed = 11;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const x = new Float32Array(n), y = new Float32Array(n), ids = new Int32Array(n), org = new Uint8Array(n);
  const v = new Float32Array(n), st = new Uint8Array(n);
  for (let i = 0; i < n; i++) { x[i] = rnd(); y[i] = rnd(); ids[i] = i; org[i] = 1 + (i & 1); v[i] = rnd() * 100 + 0.01; st[i] = i % 7; }
  const time = (f) => { f(); const t = performance.now(); for (let k = 0; k < 5; k++) f(); return (performance.now() - t) / 5; };
  const T = {
    densityDiff: time(() => densityDiff(x, y, ids, org, 1, 2, null, 128, 2, 3)),
    originMask: time(() => originMask(org, st, 'diff', 1, 2)),
    sharedEdges: time(() => sharedEdges(v, 40, true)),
    histPair: ((e) => time(() => histPair(v, org, 1, 2, null, e)))(sharedEdges(v, 40, true)),
    classDelta: time(() => classDelta(st, org, null, 7, 1, 2)),
    selectIds: time(() => selectIds(st, org, [BOX.UNMATCHED], 2)),
  };
  console.error(JSON.stringify(Object.fromEntries(Object.entries(T).map(([k, t]) => [k, +t.toFixed(2)]))));
  assert.ok(T.densityDiff < 50, `densityDiff ${T.densityDiff} ms`);
}
console.log('ok');
"""


def test_compare_js(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    esbuild = Path(__file__).resolve().parents[1] / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.exists():
        pytest.skip('run `npm install` to get esbuild')
    b = subprocess.run([str(esbuild), str(CORE_TS), '--format=esm', f'--outfile={tmp_path / "core.mjs"}'],
                       capture_output=True, text=True, timeout=60)
    assert b.returncode == 0, b.stderr
    (tmp_path / 't.mjs').write_text(SCRIPT)
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=120)
    print(r.stderr)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr
