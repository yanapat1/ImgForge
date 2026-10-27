// The A/B compare engine (no DOM, no imports). Everything the compare tab computes on the client lives here:
// decoding the server's binary column payload ('DVC1'), origin / diff visibility masks, the density-difference
// grid drawn under the map, shared histogram edges + per-side histograms with a KS distance, per-class counts,
// and id selection by box status. All loops run over typed arrays with no per-element allocation, so each call
// stays in the low milliseconds at 500k points.

export type TA = Uint8Array | Int8Array | Uint16Array | Int16Array | Int32Array | Uint32Array | Float32Array;
/** Decoded payload: the header's `meta` and one typed array per column (views on the buffer, f2 widened). */
export interface Columns { meta: unknown; cols: Record<string, TA> }

interface ColSpec { name: string; dtype: string; offset: number; length: number }

// half -> float, bit-exact (subnormals, inf, nan)
const halfToFloat = (h: number): number => {
  const s = h & 0x8000 ? -1 : 1, e = (h >>> 10) & 0x1f, f = h & 0x3ff;
  if (e === 0) return s * f * 2 ** -24;
  if (e === 31) return f ? NaN : s * Infinity;
  return s * (1 + f / 1024) * 2 ** (e - 15);
};

/**
 * Decode a 'DVC1' buffer: magic (4 bytes), u32 LE header length H, H bytes of UTF-8 JSON
 * {meta, cols: [{name, dtype, offset, length}]}, then 8-aligned little-endian column buffers at absolute offsets.
 * Integer / f4 columns are zero-copy views on `buf`; f2 is converted into a new Float32Array. Throws on bad input.
 */
export function decodeColumns(buf: ArrayBuffer): Columns {
  const u8 = new Uint8Array(buf);
  if (u8.length < 8 || u8[0] !== 0x44 || u8[1] !== 0x56 || u8[2] !== 0x43 || u8[3] !== 0x31) throw new Error('compare columns: bad magic');
  const H = new DataView(buf).getUint32(4, true);
  if (8 + H > u8.length) throw new Error('compare columns: truncated header');
  const head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + H))) as { meta?: unknown; cols?: ColSpec[] };
  const cols: Record<string, TA> = {};
  for (const c of head.cols ?? []) {
    const { offset: o, length: n } = c;
    switch (c.dtype) {
      case 'u1': cols[c.name] = new Uint8Array(buf, o, n); break;
      case 'i1': cols[c.name] = new Int8Array(buf, o, n); break;
      case 'u2': cols[c.name] = new Uint16Array(buf, o, n); break;
      case 'i2': cols[c.name] = new Int16Array(buf, o, n); break;
      case 'i4': cols[c.name] = new Int32Array(buf, o, n); break;
      case 'u4': cols[c.name] = new Uint32Array(buf, o, n); break;
      case 'f4': cols[c.name] = new Float32Array(buf, o, n); break;
      case 'f2': {
        const src = new Uint16Array(buf, o, n), out = new Float32Array(n);
        for (let i = 0; i < n; i++) out[i] = halfToFloat(src[i]!);
        cols[c.name] = out;
        break;
      }
      default: throw new Error(`compare columns: unknown dtype ${c.dtype}`);
    }
  }
  return { meta: head.meta ?? null, cols };
}

/** Box diff status codes (match the server's union diff). */
export const BOX = { NA: 0, SAME: 1, MOVED: 2, RECLASS: 3, RECLASS_MOVED: 4, UNMATCHED: 5, IMAGE_ONLY: 6 } as const;
export type OriginMode = 'a' | 'b' | 'both' | 'diff';

/**
 * Visibility mask over sample ids (1 = shown). a / b: that member only; both: either; diff: either and the
 * status is a change (moved, reclass, unmatched, image-only; null status = both). Other members are always hidden.
 * `out` is reused when it has the right length.
 */
export function originMask(origin: Uint8Array, status: Uint8Array | null, mode: OriginMode, a: number, b: number, out?: Uint8Array): Uint8Array {
  const n = origin.length;
  const m = out && out.length === n ? out : new Uint8Array(n);
  const diff = mode === 'diff' && status !== null;
  for (let i = 0; i < n; i++) {
    const o = origin[i]!;
    let v = mode === 'a' ? o === a : mode === 'b' ? o === b : o === a || o === b;
    if (v && diff) { const s = status[i]!; v = s >= BOX.MOVED && s <= BOX.IMAGE_ONLY; }
    m[i] = v ? 1 : 0;
  }
  return m;
}

/** Density-difference grid: z[row*G+col] = blurred pB - pA (NaN under minCount), raw counts, novelty share of B. */
export interface Grid { G: number; x0: number; y0: number; dx: number; dy: number; z: Float32Array; cA: Uint32Array; cB: Uint32Array; nA: number; nB: number; novelty: number }

// mass-preserving 1-D box blur along `len` cells at stride `st`: each cell scatters its value evenly over the
// in-bounds part of its window (renormalised at the edges, so the sum is unchanged); `d` is a len+1 scratch
function blur1(v: Float64Array, base: number, st: number, len: number, r: number, d: Float64Array): void {
  d.fill(0);
  for (let j = 0; j < len; j++) {
    const x = v[base + j * st]!;
    if (x === 0) continue;
    const lo = j - r < 0 ? 0 : j - r, hi = j + r >= len ? len - 1 : j + r;
    const w = x / (hi - lo + 1);
    d[lo]! += w; d[hi + 1]! -= w;
  }
  let acc = 0;
  for (let j = 0; j < len; j++) { acc += d[j]!; v[base + j * st] = acc; }
}

/**
 * Bin the a / b points of a projection into a G x G grid and diff their normalised densities.
 * x[i], y[i] belong to sample ids[i]; origin and mask are indexed by sample id. Bounds come from every finite
 * a/b point (mask ignored, so the grid does not jump while filtering), padded 2%. The blur is a separable box
 * of radius `blur` cells, renormalised at the edges so sum(pA) = sum(pB) = 1 is preserved.
 */
export function densityDiff(x: ArrayLike<number>, y: ArrayLike<number>, ids: ArrayLike<number>, origin: Uint8Array, a: number, b: number,
  mask: Uint8Array | null, G: number, blur: number, minCount: number): Grid {
  const n = ids.length;
  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  for (let i = 0; i < n; i++) {
    const o = origin[ids[i]!];
    if (o !== a && o !== b) continue;
    const px = x[i]!, py = y[i]!;
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
    if (px < xmin) xmin = px; if (px > xmax) xmax = px;
    if (py < ymin) ymin = py; if (py > ymax) ymax = py;
  }
  if (xmin > xmax) { xmin = 0; xmax = 1; ymin = 0; ymax = 1; }
  const wx = xmax - xmin || 1, wy = ymax - ymin || 1;
  const x0 = xmin - 0.02 * wx, y0 = ymin - 0.02 * wy, dx = (wx * 1.04) / G, dy = (wy * 1.04) / G;
  const N = G * G, cA = new Uint32Array(N), cB = new Uint32Array(N);
  const ix = 1 / dx, iy = 1 / dy;
  let nA = 0, nB = 0;
  // one binning pass
  for (let i = 0; i < n; i++) {
    const s = ids[i]!, o = origin[s];
    if (o !== a && o !== b) continue;
    if (mask && !mask[s]) continue;
    const px = x[i]!, py = y[i]!;
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
    let c = ((px - x0) * ix) | 0, r = ((py - y0) * iy) | 0;
    if (c < 0) c = 0; else if (c >= G) c = G - 1;
    if (r < 0) r = 0; else if (r >= G) r = G - 1;
    if (o === a) { cA[r * G + c]!++; nA++; } else { cB[r * G + c]!++; nB++; }
  }
  // normalised densities (+ novelty before the blur)
  const pA = new Float64Array(N), pB = new Float64Array(N);
  const fa = nA ? 1 / nA : 0, fb = nB ? 1 / nB : 0;
  let novelty = 0;
  for (let k = 0; k < N; k++) {
    pA[k] = cA[k]! * fa; pB[k] = cB[k]! * fb;
    if (cA[k] === 0) novelty += pB[k]!;
  }
  const r = Math.max(0, Math.floor(blur));
  if (r > 0) {
    const d = new Float64Array(G + 1);
    for (const p of [pA, pB]) {
      for (let row = 0; row < G; row++) blur1(p, row * G, 1, G, r, d);
      for (let col = 0; col < G; col++) blur1(p, col, G, G, r, d);
    }
  }
  const z = new Float32Array(N);
  for (let k = 0; k < N; k++) z[k] = cA[k]! + cB[k]! < minCount ? NaN : pB[k]! - pA[k]!;
  return { G, x0, y0, dx, dy, z, cA, cB, nA, nB, novelty };
}

/**
 * bins+1 shared histogram edges spanning the 1st..99th percentile of the finite values (> 0 when log; log spacing
 * is geometric). Percentiles come from a strided sample of at most 16384 values so big columns stay cheap.
 */
export function sharedEdges(v: ArrayLike<number>, bins: number, log: boolean): Float64Array {
  const n = v.length, step = Math.max(1, Math.ceil(n / 16384));
  const tmp = new Float64Array(Math.ceil(n / step));
  let m = 0;
  for (let i = 0; i < n; i += step) {
    const t = v[i]!;
    if (Number.isFinite(t) && (!log || t > 0)) tmp[m++] = t;
  }
  const s = tmp.subarray(0, m).sort();
  let lo = m ? s[Math.floor(0.01 * (m - 1))]! : log ? 1 : 0;
  let hi = m ? s[Math.ceil(0.99 * (m - 1))]! : log ? 10 : 1;
  if (!(hi > lo)) { if (log) { lo /= 2; hi = hi * 2 || 2; } else { lo -= 0.5; hi += 0.5; } }
  const e = new Float64Array(bins + 1);
  if (log) { const l0 = Math.log(lo), w = (Math.log(hi) - l0) / bins; for (let k = 0; k <= bins; k++) e[k] = Math.exp(l0 + k * w); }
  else { const w = (hi - lo) / bins; for (let k = 0; k <= bins; k++) e[k] = lo + k * w; }
  e[0] = lo; e[bins] = hi;
  return e;
}

/**
 * Normalised histograms of v (indexed by sample id) for members a and b over shared `edges`; out-of-range values
 * clamp into the end bins, non-finite ones are skipped. ks = max |CDF_A - CDF_B| at the bin edges (0 if a side is empty).
 */
export function histPair(v: ArrayLike<number>, origin: Uint8Array, a: number, b: number, mask: Uint8Array | null, edges: Float64Array): { ha: Float64Array; hb: Float64Array; ks: number } {
  const bins = edges.length - 1, ha = new Float64Array(Math.max(0, bins)), hb = new Float64Array(Math.max(0, bins));
  if (bins < 1) return { ha, hb, ks: 0 };
  const n = Math.min(v.length, origin.length), last = bins - 1;
  // spacing: 1 = linear, 2 = geometric (sharedEdges makes one of the two), 0 = arbitrary -> binary search
  const lin = (edges[bins]! - edges[0]!) / bins, geo = edges[0]! > 0 ? Math.log(edges[bins]! / edges[0]!) / bins : NaN;
  let isLin = lin > 0, isGeo = geo > 0;
  for (let k = 1; k < bins; k++) {
    const e = edges[k]!;
    if (isLin && Math.abs(e - (edges[0]! + k * lin)) > 1e-9 * Math.abs(e) + 1e-12) isLin = false;
    if (isGeo && Math.abs(Math.log(e / edges[0]!) - k * geo) > 1e-9) isGeo = false;
  }
  const mode = isLin ? 1 : isGeo ? 2 : 0;
  const e0 = mode === 2 ? Math.log(edges[0]!) : edges[0]!, inv = mode === 1 ? 1 / lin : mode === 2 ? 1 / geo : 0;
  let nA = 0, nB = 0;
  for (let i = 0; i < n; i++) {
    const o = origin[i]!;
    if (o !== a && o !== b) continue;
    if (mask && !mask[i]) continue;
    const t = v[i]!;
    if (!Number.isFinite(t)) continue;
    let lo: number;
    if (mode) {
      // direct guess from the (linear / geometric) spacing, then nudge so edges[lo] <= t < edges[lo+1]
      lo = Math.floor(mode === 1 ? (t - e0) * inv : t > 0 ? (Math.log(t) - e0) * inv : -1);
      if (!(lo >= 0)) lo = 0; else if (lo > last) lo = last;
      while (lo > 0 && edges[lo]! > t) lo--;
      while (lo < last && edges[lo + 1]! <= t) lo++;
    } else {
      // binary search for the last edge <= t, clamped to [0, bins-1]
      lo = 0; let hi = last;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (edges[mid]! <= t) lo = mid; else hi = mid - 1; }
    }
    if (o === a) { ha[lo]!++; nA++; } else { hb[lo]!++; nB++; }
  }
  const fa = nA ? 1 / nA : 0, fb = nB ? 1 / nB : 0;
  let ca = 0, cb = 0, ks = 0;
  for (let k = 0; k < bins; k++) {
    ha[k] = ha[k]! * fa; hb[k] = hb[k]! * fb;
    ca += ha[k]!; cb += hb[k]!;
    const d = Math.abs(ca - cb);
    if (d > ks) ks = d;
  }
  return { ha, hb, ks: nA && nB ? ks : 0 };
}

/** Per-class live box counts for members a and b (deleted boxes and class ids outside [0, nCls) skipped). */
export function classDelta(cls: ArrayLike<number>, origin: Uint8Array, deleted: Uint8Array | null, nCls: number, a: number, b: number): { a: Uint32Array; b: Uint32Array } {
  const ca = new Uint32Array(nCls), cb = new Uint32Array(nCls);
  const n = Math.min(cls.length, origin.length);
  for (let i = 0; i < n; i++) {
    if (deleted && deleted[i]) continue;
    const c = cls[i]!;
    if (!(c >= 0 && c < nCls)) continue;
    const o = origin[i]!;
    if (o === a) ca[c]!++; else if (o === b) cb[c]!++;
  }
  return { a: ca, b: cb };
}

/** Sample ids whose status is in wantStatus and (side null or origin === side). */
export function selectIds(status: Uint8Array, origin: Uint8Array, wantStatus: readonly number[], side: number | null): number[] {
  const want = new Uint8Array(256);
  for (const s of wantStatus) if (s >= 0 && s < 256) want[s] = 1;
  const out: number[] = [];
  const n = Math.min(status.length, origin.length);
  for (let i = 0; i < n; i++) if (want[status[i]!] && (side === null || origin[i] === side)) out.push(i);
  return out;
}
