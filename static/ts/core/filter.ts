// The image / box filter engine (no DOM). A filter spec F becomes a Uint8Array mask over the boxes of the
// points payload in one pass over typed arrays. Per-image aggregates (box -> image, class bitset, box count)
// are built once per payload; name matching (substring, wildcard, regex, fuzzy) runs once per image, never
// per box, and is memoised while the name part of the spec is unchanged. Never throws: a bad regex comes
// back as `error` and that predicate is skipped.

import type { ClsMode, FilterSpec, NameMode, Points, Range } from '../types.js';

/** Per-payload aggregates built by buildAgg (box -> image maps, class bitsets, memoised name matching). */
export interface Agg {
  n: number;                       // boxes
  nImg: number;                    // distinct images
  imgIdx: Int32Array;              // box -> image index
  names: string[];                 // image index -> name
  lower: string[];
  imgOf: Map<string, number>;      // name -> image index
  sessIdx: Int32Array;             // box -> session index
  sessions: string[];
  sessOf: Map<string, number>;
  W: number;                       // 32-bit words per class bitset
  imgCls: Uint32Array;             // image index * W -> class bitset
  nbox: Int32Array;                // live boxes per image
  tokens: string[][] | null;
  memo: { key: string; val: NameMask } | null;
  buf?: { ip: Uint8Array; hit: Uint8Array; mask: [Uint8Array, Uint8Array]; k: number };
}
/** Uint8Array per image (1 = passes the name predicate), or {error} for a bad pattern. */
export type NameMask = Uint8Array | { error: string };
/** Predicates whose data comes from the server (Uint8Array over boxes / images; null = not loaded). */
export interface FilterExt {
  tagMask?: Uint8Array | null; issueBox?: Uint8Array | null; issueImg?: Uint8Array | null; clipMask?: Uint8Array | null;
}
export interface FilterResult { mask: Uint8Array | null; nImg: number; nBox: number; error: string | null; active: boolean }

const isErr = (v: NameMask): v is { error: string } => !(v instanceof Uint8Array);

// the spec (persisted in ui_settings as `filter`); null bounds = open
export function emptyFilter(): FilterSpec {
  return {
    scope: 'image', name: { mode: 'substr', q: '', neg: false }, cls: { mode: 'any', ids: [] },
    nbox: [null, null], area: [null, null], aspect: [null, null], brightness: [null, null], blur: [null, null],
    session: [], tags: [], m_status: [], suspicion_min: null, outlier_min: null, issues: [], clip: { q: '', k: 500 },
  };
}

// merge a stored spec over the defaults (old / partial / foreign JSON never breaks the page)
export function normalizeFilter(raw: unknown): FilterSpec {
  const d = emptyFilter();
  if (!raw || typeof raw !== 'object') return d;
  const rec = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' ? (v as Record<string, unknown>) : {});
  const f = rec(raw), name = rec(f.name), cls = rec(f.cls), clip = rec(f.clip);
  const num = (v: unknown): number | null => (v === null || v === undefined || v === '' || !Number.isFinite(+v) ? null : +v);
  const range = (r: unknown): Range => (Array.isArray(r) && r.length === 2 ? [num(r[0]), num(r[1])] : [null, null]);
  const arr = (a: unknown): unknown[] => (Array.isArray(a) ? a.slice() : []);
  const NAME_MODES: readonly unknown[] = ['substr', 'glob', 'regex', 'fuzzy'];
  const CLS_MODES: readonly unknown[] = ['any', 'all', 'none'];
  return {
    scope: f.scope === 'box' ? 'box' : 'image',
    name: { mode: NAME_MODES.includes(name.mode) ? (name.mode as NameMode) : 'substr', q: String(name.q || ''), neg: !!name.neg },
    cls: { mode: CLS_MODES.includes(cls.mode) ? (cls.mode as ClsMode) : 'any', ids: arr(cls.ids).map(Number) },
    nbox: range(f.nbox), area: range(f.area), aspect: range(f.aspect), brightness: range(f.brightness), blur: range(f.blur),
    session: arr(f.session).map(String), tags: arr(f.tags).map(String), m_status: arr(f.m_status).map(Number),
    suspicion_min: num(f.suspicion_min),
    outlier_min: num(f.outlier_min),
    issues: arr(f.issues).map(String),
    clip: { q: String(clip.q || ''), k: Math.max(1, Math.min(20000, +(clip.k as number) || 500)) },
  };
}

const hasRange = (r: Range): boolean => r[0] !== null || r[1] !== null;

// is any predicate set? (the scope alone filters nothing)
export function filterActive(F: FilterSpec): boolean {
  return !!(F.name.q.trim() || F.cls.ids.length || hasRange(F.nbox) || hasRange(F.area) || hasRange(F.aspect) ||
    hasRange(F.brightness) || hasRange(F.blur) || F.session.length || F.tags.length || F.m_status.length ||
    F.suspicion_min !== null || F.outlier_min !== null || F.issues.length || F.clip.q.trim());
}

// ---------------------------------------------------------------- per-payload aggregates

/** Aggregates of a points payload; `nCls` = number of classes (the bitset width also covers any larger id). */
export function buildAgg(P: Points, nCls: number): Agg {
  const n = P.cls.length;
  const imgIdx = new Int32Array(n);
  const map = new Map<string, number>(), names: string[] = [];
  const sessIdx = new Int32Array(n), sessMap = new Map<string, number>(), sessions: string[] = [];
  for (let i = 0; i < n; i++) {
    const nm = P.image[i] as string;
    let j = map.get(nm);
    if (j === undefined) { j = names.length; map.set(nm, j); names.push(nm); }
    imgIdx[i] = j;
    const s = P.session[i] as string;
    let k = sessMap.get(s);
    if (k === undefined) { k = sessions.length; sessMap.set(s, k); sessions.push(s); }
    sessIdx[i] = k;
  }
  let maxCls = nCls - 1;
  for (let i = 0; i < n; i++) { const c = P.cls[i] as number; if (c > maxCls) maxCls = c; }
  const W = Math.max(1, Math.ceil((maxCls + 1) / 32));
  const agg: Agg = { n, nImg: names.length, imgIdx, names, lower: names.map((s) => s.toLowerCase()), imgOf: map,
    sessIdx, sessions, sessOf: sessMap, W, imgCls: new Uint32Array(names.length * W), nbox: new Int32Array(names.length),
    tokens: null, memo: null };
  refreshAgg(agg, P);
  return agg;
}

// class bitsets + box counts per image (after a class change / delete from curation): no name work
export function refreshAgg(agg: Agg, P: Points): void {
  const { n, imgIdx, W, imgCls, nbox } = agg;
  const del = P.deleted;
  imgCls.fill(0);
  nbox.fill(0);
  const maxCls = W * 32 - 1;
  for (let i = 0; i < n; i++) {
    if (del && del[i]) continue;
    const c = P.cls[i] as number;
    if (c < 0 || c > maxCls) continue;
    const j = imgIdx[i] as number;
    imgCls[j * W + (c >>> 5)] = (imgCls[j * W + (c >>> 5)] as number) | (1 << (c & 31));
    nbox[j] = (nbox[j] as number) + 1;
  }
}

// ---------------------------------------------------------------- name matching (once per image)

const tokensOf = (s: string): string[] => s.replace(/\.[a-z0-9]{2,5}$/, '').match(/[a-z]+|\d+/g) || [];

function globRe(q: string): RegExp {
  const re = q.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(/[*?]/.test(q) ? `^${re}$` : re, 'i');
}

// 'related' names: every query token (letters / digit runs) prefixes a token of the name; digits also match
// by value (cam1 ~ cam_0001); with 3+ query tokens two thirds are enough
function fuzzyMatcher(q: string): ((toks: string[]) => boolean) | null {
  const qt = tokensOf(q.toLowerCase());
  if (!qt.length) return null;
  const need = qt.length <= 2 ? qt.length : Math.ceil(qt.length * 2 / 3);
  const qn = qt.map((t) => (/^\d+$/.test(t) ? +t : null));
  return (toks: string[]) => {
    let hit = 0;
    for (let a = 0; a < qt.length; a++) {
      const t = qt[a] as string;
      for (let b = 0; b < toks.length; b++) {
        const u = toks[b] as string;
        if (u.startsWith(t) || (t.length >= 4 && u.includes(t)) || (qn[a] !== null && /^\d/.test(u) && +u === qn[a])) { hit++; break; }
      }
      if (hit + (qt.length - a - 1) < need) return false;
    }
    return hit >= need;
  };
}

// patterns that backtrack exponentially: a repeated group with a repetition inside
// (same rule as the server's strip filter, labeling/store.py)
const NESTED_QUANT = /\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)(?:[+*]|\{\d*,)/;
export const riskyRegex = (src: string): boolean => src.length > 500 || NESTED_QUANT.test(src);

export function prepareTokens(agg: Agg): void { agg.tokens = agg.lower.map(tokensOf); }

// Uint8Array per image (1 = passes the name predicate) or {error}; memoised on (mode, q, neg)
function nameMask(agg: Pick<Agg, 'nImg' | 'names' | 'lower' | 'tokens' | 'memo'>, name: FilterSpec['name']): NameMask {
  const key = `${name.mode}\u0000${name.q}\u0000${name.neg}`;
  if (agg.memo && agg.memo.key === key) return agg.memo.val;
  const q = name.q.trim();
  const out = new Uint8Array(agg.nImg);
  let val: NameMask = out;
  try {
    let test: (j: number) => boolean;
    if (name.mode === 'regex') {
      const m = q.match(/^\/(.+)\/([a-z]*)$/);
      const src = m ? (m[1] as string) : q;
      // a nested quantifier ((a+)+, (\w*)*, ...) can backtrack exponentially and freeze the tab
      if (riskyRegex(src)) throw new Error('nested repetition (like (a+)+) can hang the page: simplify the pattern');
      const re = new RegExp(src, m ? (m[2] as string).replace(/[gy]/g, '') : 'i');
      test = (j) => re.test(agg.names[j] as string);
    } else if (name.mode === 'glob') {
      const re = globRe(q);
      test = (j) => re.test(agg.names[j] as string);
    } else if (name.mode === 'fuzzy') {
      const tokens = agg.tokens || (agg.tokens = agg.lower.map(tokensOf));
      const f = fuzzyMatcher(q);
      test = f ? (j) => f(tokens[j] as string[]) : () => true;
    } else {
      const ql = q.toLowerCase();
      test = (j) => (agg.lower[j] as string).includes(ql);
    }
    const neg = name.neg ? 1 : 0;
    for (let j = 0; j < agg.nImg; j++) out[j] = (test(j) ? 1 : 0) ^ neg;
  } catch (e) {
    val = { error: `${name.mode === 'regex' ? 'Invalid regular expression' : 'Invalid pattern'}: ${(e instanceof Error ? e.message : String(e)).replace(/^Invalid regular expression: /, '')}` };
  }
  agg.memo = { key, val };
  return val;
}

// the name predicate over names that are not in the points payload (background images of the image browser):
// Uint8Array per name, or {error}; same matching as the filter itself
export function namePasses(names: string[], name: FilterSpec['name']): NameMask {
  const lower = names.map((s: string) => s.toLowerCase());
  return nameMask({ nImg: names.length, names, lower, tokens: null, memo: null }, name);
}

// can an image with no box pass F? Only when every set predicate is one an empty image can meet
// (its name, "has none of these classes", a box count range that includes 0)
export function bgCanPass(F: FilterSpec): boolean {
  const none = (r: Range): boolean => r[0] === null && r[1] === null;
  return (!F.cls.ids.length || F.cls.mode === 'none') && (F.nbox[0] === null || F.nbox[0] <= 0) &&
    none(F.area) && none(F.aspect) && none(F.brightness) && none(F.blur) && !F.session.length && !F.tags.length &&
    !F.m_status.length && F.suspicion_min === null && F.outlier_min === null && !F.issues.length && !F.clip.q.trim();
}

// ---------------------------------------------------------------- run

// ext: {tagMask: Uint8Array(n) | null, issueBox: Uint8Array(n) | null, issueImg: Uint8Array(nImg) | null,
//       clipMask: Uint8Array(n) | null}: the predicates whose data comes from the server
export function runFilter(F: FilterSpec, P: Points, agg: Agg, ext: FilterExt = {}): FilterResult {
  const n = agg.n, imgIdx = agg.imgIdx, del = P.deleted || null;
  const res: FilterResult = { mask: null, nImg: 0, nBox: 0, error: null, active: filterActive(F) };
  if (!res.active && !del) { res.nImg = agg.nImg; res.nBox = n; return res; }

  // image-level predicates -> ip (one byte per image), or null when none is set
  // scratch buffers are reused between runs (no 300k-byte allocations per keystroke, no GC pauses)
  const buf = agg.buf || (agg.buf = { ip: new Uint8Array(agg.nImg), hit: new Uint8Array(agg.nImg), mask: [new Uint8Array(n), new Uint8Array(n)], k: 0 });
  let ip = null as Uint8Array | null;     // (assigned in a closure too: the cast keeps TS from narrowing it to null)
  const ensureIp = (): Uint8Array => { if (!ip) { ip = buf.ip; ip.fill(1); } return ip; };
  if (F.name.q.trim()) {
    const nm = nameMask(agg, F.name);
    if (isErr(nm)) res.error = nm.error;
    else { ip = buf.ip; ip.set(nm); }
  }
  // typed-array reads below are in range by construction: `!` only silences noUncheckedIndexedAccess
  if (F.cls.ids.length) {
    const W = agg.W, q = new Uint32Array(W);
    for (const c of F.cls.ids) if (c >= 0 && c < W * 32) q[c >>> 5]! |= 1 << (c & 31);
    const p = ensureIp(), bits = agg.imgCls, mode = F.cls.mode;
    for (let j = 0; j < agg.nImg; j++) {
      if (!p[j]) continue;
      let any = false, all = true;
      for (let w = 0, o = j * W; w < W; w++) {
        const x = (bits[o + w]! & q[w]!) >>> 0;     // & is signed int32, q[w] unsigned: class 31, 63, ... set bit 31
        if (x) any = true;
        if (x !== q[w]) all = false;
      }
      p[j] = mode === 'all' ? (all ? 1 : 0) : mode === 'none' ? (any ? 0 : 1) : (any ? 1 : 0);
    }
  }
  if (hasRange(F.nbox)) {
    const p = ensureIp(), lo = F.nbox[0] ?? -Infinity, hi = F.nbox[1] ?? Infinity, nb = agg.nbox;
    for (let j = 0; j < agg.nImg; j++) if (p[j] && (nb[j]! < lo || nb[j]! > hi)) p[j] = 0;
  }

  // box-level predicates, all in one pass
  type Bounds = [number, number];
  const r = (k: 'area' | 'aspect' | 'brightness' | 'blur', scale = 1): Bounds | null => {
    const [lo, hi] = F[k];
    return hasRange(F[k]) && P[k] ? [lo === null ? -Infinity : lo * scale, hi === null ? Infinity : hi * scale] : null;
  };
  const area = r('area', 0.01), aspect = r('aspect'), bright = r('brightness'), blur = r('blur');
  const A = P.area, AS = P.aspect, BR = P.brightness, BL = P.blur;
  let sessOk = null as Uint8Array | null;
  if (F.session.length) {
    sessOk = new Uint8Array(agg.sessions.length);
    for (const s of F.session) { const k = agg.sessOf.get(s); if (k !== undefined) sessOk[k] = 1; }
  }
  const sessIdx = agg.sessIdx;
  const tagMask = F.tags.length ? ext.tagMask || new Uint8Array(n) : null;
  let msOk = null as Uint8Array | null;
  const MS = P.m_status;
  if (F.m_status.length && MS) { msOk = new Uint8Array(8); for (const s of F.m_status) if (s >= -1 && s < 7) msOk[s + 1] = 1; }
  const sus = F.suspicion_min !== null && P.suspicion ? P.suspicion : null, susMin = F.suspicion_min ?? 0;
  const out = F.outlier_min !== null && P.outlier ? P.outlier : null, outMin = F.outlier_min ?? 0;
  const issues = F.issues.length ? { box: ext.issueBox || null, img: ext.issueImg || null } : null;
  const clip = F.clip.q.trim() ? ext.clipMask || null : null;
  const imageScope = F.scope !== 'box';
  // box scope: "has class" also keeps only the boxes of (any / all) or not of (none) the chosen classes
  let clsOk = null as Uint8Array | null;
  if (!imageScope && F.cls.ids.length) {
    clsOk = new Uint8Array(agg.W * 32);
    const want = F.cls.mode === 'none' ? 0 : 1;
    clsOk.fill(1 - want);
    for (const c of F.cls.ids) if (c >= 0 && c < clsOk.length) clsOk[c] = want;
  }
  const CLS = P.cls;

  // two alternating output buffers: the previous mask stays valid while the next one is built
  buf.k ^= 1;
  const mask = buf.mask[buf.k]!;
  mask.fill(0);
  const hit = buf.hit;
  if (imageScope) hit.fill(0);
  for (let i = 0; i < n; i++) {
    if (del && del[i]) continue;
    const j = imgIdx[i]!;
    if (ip && !ip[j]) continue;
    if (area) { const v = A[i]!; if (v < area[0] || v > area[1]) continue; }
    if (aspect) { const v = AS[i]!; if (v < aspect[0] || v > aspect[1]) continue; }
    if (bright) { const v = BR[i]!; if (v < bright[0] || v > bright[1]) continue; }
    if (blur) { const v = BL[i]!; if (v < blur[0] || v > blur[1]) continue; }
    if (sessOk && !sessOk[sessIdx[i]!]) continue;
    if (tagMask && !tagMask[i]) continue;
    if (msOk && !msOk[MS![i]! + 1]) continue;
    if (sus && !(sus[i]! >= susMin)) continue;
    if (out && !(out[i]! >= outMin)) continue;
    if (issues && !((issues.box && issues.box[i]) || (issues.img && issues.img[j]))) continue;
    if (clip && !clip[i]) continue;
    if (clsOk && !clsOk[CLS[i]!]) continue;
    if (imageScope) hit[j] = 1;
    else mask[i] = 1;
  }
  let nb = 0, ni = 0;
  if (imageScope) {
    for (let i = 0; i < n; i++) if (hit[imgIdx[i]!] && !(del && del[i])) { mask[i] = 1; nb++; }
    for (let j = 0; j < agg.nImg; j++) ni += hit[j]!;
  } else {
    const seen = new Uint8Array(agg.nImg);
    for (let i = 0; i < n; i++) if (mask[i]) { nb++; const j = imgIdx[i]!; if (!seen[j]) { seen[j] = 1; ni++; } }
  }
  res.mask = mask;
  res.nBox = nb;
  res.nImg = ni;
  return res;
}

// a Uint8Array(n) with 1 at every id (tags, CLIP hits, issue boxes)
export function idMask(n: number, ids: Iterable<number>): Uint8Array {
  const m = new Uint8Array(n);
  for (const id of ids) if (id >= 0 && id < n) m[id] = 1;
  return m;
}
