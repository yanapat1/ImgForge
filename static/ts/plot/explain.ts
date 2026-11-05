// Why a dot is flagged. "Far from class" and "Suspicious labels" are scored in the full feature space, and a 2D map
// (UMAP, t-SNE) distorts distances, so a flagged dot can look close to its class. While one of those checks is on:
// - each shown class gets a centre star (the box nearest its feature-space centre: lowest `outlier`; the 2D median
//   for the on-the-map check),
// - the hovered / picked dots get lines: to their class centre (far), or to their nearest neighbours in the
//   features, coloured by the neighbour's class (suspicious),
// - "Far from class (on the map)" scores what the eye sees: distance to the class's 2D median over its median.

import type * as P from 'plotly.js';
import type { SimilarPayload } from '../types-plot.js';
import { getJSON } from '../core/api.js';
import { $, esc, rgba } from '../core/dom.js';
import { pts, S } from '../core/state.js';
import { clsColor, clsName, THEME } from '../core/theme.js';
import { plotDiv, xyOf, xyzOf } from './access.js';
import { updateMarks } from './marks.js';
import { visibleIndices } from './render.js';

type Mode = 'feat' | 'map';
/** Class -> its centre on the map (and the box it sits on, for 'feat'). */
type Centres = Map<number, { x: number; y: number; z: number; id: number | null }>;   // z: NaN on a 2D map

// the checks that explain themselves; Display › Class centres shows the stars on any view
const EXPLAINED: Record<string, Mode> = { far: 'feat', suspicious: 'feat', far2d: 'map' };

/** How the centres are placed now, or null when none are drawn. */
export function centreMode(): Mode | null {
  const m = EXPLAINED[S.activeView ?? ''];
  if (m) return m;
  return ($<HTMLInputElement>('show-centres')?.checked) ? 'feat' : null;
}

let cache: { key: string; view: unknown; mask: unknown; outlier: unknown; c: Centres } | null = null;

export type { Centres };

export function classCentres(mode: Mode): Centres {
  const view = S.view, P = pts();
  const out: Centres = new Map();
  if (!view) return out;
  const useFeat = mode === 'feat' && !!P.outlier;
  const vis = visibleIndices();
  const key = `${view.key}|${useFeat}|${vis.length}|${[...S.hidden].join(',')}|${$<HTMLSelectElement>('mode')?.value}`;
  if (cache && cache.key === key && cache.view === view && cache.mask === S.filterMask && cache.outlier === P.outlier) return cache.c;
  if (useFeat) {
    const o = P.outlier ?? [], best = new Map<number, number>();
    for (const i of vis) {
      const id = view.id[i] ?? -1, c = P.cls[id] ?? -1, b = best.get(c);
      if (b === undefined || (o[id] ?? Infinity) < (o[b] ?? Infinity)) best.set(c, id);
    }
    for (const [c, id] of best) { const [x, y, z] = xyzOf(id); out.set(c, { x, y, z, id }); }
  } else {
    // per class: the median on each axis (a 3D cloud: z too)
    const Z = view.z, xs = new Map<number, number[][]>();
    for (const i of vis) {
      const c = P.cls[view.id[i] ?? -1] ?? -1;
      let a = xs.get(c);
      if (!a) { a = [[], [], []]; xs.set(c, a); }
      a[0]?.push(view.x[i] ?? NaN); a[1]?.push(view.y[i] ?? NaN);
      if (Z) a[2]?.push(Z[i] ?? NaN);
    }
    for (const [c, a] of xs) out.set(c, { x: median(a[0] ?? []), y: median(a[1] ?? []), z: Z ? median(a[2] ?? []) : NaN, id: null });
  }
  cache = { key, view, mask: S.filterMask, outlier: P.outlier, c: out };
  return out;
}

function median(a: number[]): number {
  if (!a.length) return NaN;
  const s = Float64Array.from(a).sort(), m = s.length >> 1;
  return s.length % 2 ? s[m] ?? NaN : ((s[m - 1] ?? NaN) + (s[m] ?? NaN)) / 2;
}

/** The centre stars, drawn above the dots (one SVG trace: a handful of points, with labels). */
export function centreTrace(dot: number): Partial<P.PlotData> | null {
  const mode = centreMode();
  if (!mode) return null;
  const c = classCentres(mode);
  const x: number[] = [], y: number[] = [], color: string[] = [], text: string[] = [], hover: string[] = [];
  for (const [k, v] of [...c].sort((a, b) => a[0] - b[0])) {
    x.push(v.x); y.push(v.y); color.push(clsColor(k)); text.push(esc(clsName(k)));
    hover.push(`<b>centre of ${esc(clsName(k))}</b><br>` + (v.id !== null
      ? `box #${v.id}: the one nearest the class centre in the features`
      : 'median of its dots on this map'));
  }
  return { type: 'scatter', mode: 'text+markers', name: 'centres', showlegend: false, x, y, text, hovertext: hover,
    hoverinfo: 'text', textposition: 'top center', textfont: { family: THEME.font, size: 12, color: THEME.text },
    marker: { symbol: 'star', size: dot + 16, color, line: { color: THEME.text, width: 1.5 }, opacity: 1 } } as Partial<P.PlotData>;
}

// ---- "Far from class (on the map)": distance to the class's 2D median, in multiples of the class's median distance
export const far2dScore = new Map<number, number>();

export function far2dIds(): number[] {
  const view = S.view, P = pts();
  far2dScore.clear();
  if (!view) return [];
  const c = classCentres('map'), d = new Map<number, number[]>(), rows: Array<[number, number, number]> = [];
  for (const i of visibleIndices()) {
    const id = view.id[i] ?? -1, k = P.cls[id] ?? -1, ce = c.get(k);
    if (!ce) continue;
    // a 3D cloud: the distance in 3D (what the eye sees there)
    const dz = view.z ? (view.z[i] ?? NaN) - ce.z : 0;
    const dist = Math.hypot((view.x[i] ?? NaN) - ce.x, (view.y[i] ?? NaN) - ce.y, dz);
    let a = d.get(k);
    if (!a) { a = []; d.set(k, a); }
    a.push(dist);
    rows.push([id, k, dist]);
  }
  const med = new Map([...d].map(([k, a]): [number, number] => [k, median(a) || 1e-9]));
  for (const [id, k, dist] of rows) far2dScore.set(id, dist / (med.get(k) ?? 1));
  return rows.map((r) => r[0]).sort((a, b) => (far2dScore.get(b) ?? 0) - (far2dScore.get(a) ?? 0));
}

// ---- lines for the hovered / picked dots
let explainId: number | null = null;

/** A dot hovered on the plot while a check is on: its lines stay until another dot, a blank click or a new selection. */
export function setExplainDot(id: number | null): void {
  if (id === explainId || (id !== null && !EXPLAINED[S.activeView ?? ''])) return;
  explainId = id;
  updateMarks();
}
export const explainDot = (): number | null => explainId;

const NBR_K = 10;
const nbrs = new Map<string, number[] | 'pending'>();      // feature|id -> the k nearest boxes

function neighbours(id: number): number[] | null {
  const key = `${S.scoresFeature}|${id}`, v = nbrs.get(key);
  if (v && v !== 'pending') return v;
  if (!v && S.scoresFeature) {
    nbrs.set(key, 'pending');
    getJSON<SimilarPayload>(`api/similar/${id}?k=${NBR_K}&feature=${encodeURIComponent(S.scoresFeature)}`)
      .then((r) => { nbrs.set(key, r.id); updateMarks(); })
      .catch(() => nbrs.delete(key));
  }
  return null;
}

export interface Marks { shapes: Array<Partial<P.Shape>>; annotations: Array<Partial<P.Annotations>> }

export function explainMarks(ids: number[]): Marks {
  const out: Marks = { shapes: [], annotations: [] };
  const view = S.activeView ?? '', mode = EXPLAINED[view];
  if (!mode || !S.view) return out;
  const P = pts(), centres = classCentres(mode);
  const font: Partial<P.Font> = { family: THEME.mono, size: 11, color: THEME.text };
  // a label grows away from the nearer side edge, so a dot near the right edge does not push it out of the plot
  const xa = plotDiv()._fullLayout?.xaxis;
  const mid = xa ? (xa.p2l(0) + xa.p2l(xa._length)) / 2 : NaN;
  const label = (x: number, y: number, text: string, col: string): Partial<P.Annotations> => ({ x, y, xref: 'x', yref: 'y',
    xanchor: x > mid ? 'right' : 'left', xshift: x > mid ? 8 : -8,
    showarrow: false, yshift: -22, text, font, bgcolor: rgba(THEME.surface2, 0.94), bordercolor: col, borderwidth: 1, borderpad: 3 });
  for (const id of [...new Set(ids)].slice(0, view === 'suspicious' ? 3 : 8)) {
    const [x, y] = xyOf(id), c = P.cls[id] ?? -1;
    if (!Number.isFinite(x)) continue;
    if (view === 'suspicious') {
      const nb = neighbours(id);
      if (!nb) continue;
      let other = 0;
      for (const n of nb) {
        const [nx, ny] = xyOf(n), nc = P.cls[n] ?? -1;
        if (nc !== c) other++;
        if (!Number.isFinite(nx)) continue;
        out.shapes.push({ type: 'line', xref: 'x', yref: 'y', x0: x, y0: y, x1: nx, y1: ny, layer: 'above',
          line: { color: rgba(clsColor(nc), 0.9), width: 1.5 } });
      }
      const sug = P.suggested?.[id];
      out.annotations.push(label(x, y, `${other}/${nb.length} nearest (features) are not ${esc(clsName(c))}` +
        (sug !== undefined && sug !== c ? ` · mostly ${esc(clsName(sug))}` : ''), THEME.highlight));
    } else {
      const ce = centres.get(c);
      if (!ce) continue;
      out.shapes.push({ type: 'line', xref: 'x', yref: 'y', x0: x, y0: y, x1: ce.x, y1: ce.y, layer: 'above',
        line: { color: clsColor(c), width: 2, dash: 'dash' } });
      const text = mode === 'map'
        ? `${(far2dScore.get(id) ?? NaN).toFixed(1)}× the class's median distance (on the map)`
        : `distance to class centre ${(P.outlier?.[id] ?? NaN).toFixed(3)} (in the features)`;
      out.annotations.push(label(x, y, text, clsColor(c)));
    }
  }
  return out;
}

// ---- the same explanations in a 3D cloud: segments in data space (drawn as one scatter3d line trace by
// render3d.ts) + a label at each explained dot. With Display › Class centres on and no check, the hovered /
// picked dots get a spoke to their class centre, so their place in the cloud reads against it.
type Xyz = [number, number, number];
export interface Lines3d { segs: Array<{ p: Xyz; q: Xyz; color: string }>; labels: Array<{ p: Xyz; text: string }> }

export function explainLines3d(ids: number[]): Lines3d {
  const out: Lines3d = { segs: [], labels: [] };
  if (!S.view?.z) return out;
  const check = S.activeView ?? '', mode = EXPLAINED[check] ?? centreMode();
  if (!mode) return out;
  const P = pts(), centres = classCentres(mode);
  for (const id of [...new Set(ids)].slice(0, check === 'suspicious' ? 3 : 8)) {
    const p = xyzOf(id), c = P.cls[id] ?? -1;
    if (!Number.isFinite(p[0])) continue;
    if (check === 'suspicious') {
      const nb = neighbours(id);
      if (!nb) continue;
      let other = 0;
      for (const n of nb) {
        const q = xyzOf(n), nc = P.cls[n] ?? -1;
        if (nc !== c) other++;
        if (Number.isFinite(q[0])) out.segs.push({ p, q, color: clsColor(nc) });
      }
      out.labels.push({ p, text: `${other}/${nb.length} nearest are not ${clsName(c)}` });
      continue;
    }
    const ce = centres.get(c);
    if (!ce || !Number.isFinite(ce.z)) continue;
    out.segs.push({ p, q: [ce.x, ce.y, ce.z], color: clsColor(c) });
    if (check === 'far2d') out.labels.push({ p, text: `${(far2dScore.get(id) ?? NaN).toFixed(1)}× median distance (in 3D)` });
    else if (check === 'far') out.labels.push({ p, text: `distance to centre ${(P.outlier?.[id] ?? NaN).toFixed(3)} (features)` });
  }
  return out;
}
