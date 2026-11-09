// Overlays on the plot: the hovered / picked cards' dots, crosshairs and distance lines between picks.

import { MAX_DIST_PICKS } from '../settings/forms.js';
import type * as P from 'plotly.js';
import type { DistancesPayload } from '../types.js';
import type { PlotDiv } from '../types-plot.js';
import { postJSON } from '../core/api.js';
import { $req, rgba } from '../core/dom.js';
import { pts, S } from '../core/state.js';
import { clsColor, THEME } from '../core/theme.js';
import { is3d, plotDiv, restyle, updateTraces, viewPos, xyOf } from './access.js';
import { updateMarks3d } from './render3d.js';
import { explainDot, explainMarks } from './explain.js';

declare const Plotly: typeof import('plotly.js');

// hover-dot: the card under the mouse (grows in, gone when the mouse leaves the gallery)
// picked:    cards clicked in the gallery (ctrl/cmd+click stacks several); stay until cleared
export let hoverDotId: number | null = null, hoverAnim: number | null = null;

const MAX_CROSSHAIRS = 8;

export const OVERLAY = new Set(['highlight', 'picked', 'hover-dot', 'bubbles', 'centres',      // never part of a selection
  'expl-lines', 'pick-lines', 'drop-lines', 'labels-3d']);                                        // + the 3D overlays (render3d.ts)

interface DotMarker {
  x: number[]; y: number[];
  marker: { size: number[]; color: string[]; line: { color: string[]; width: number[] }; opacity: number };
}

function dotMarker(ids: number[], k = 1): DotMarker {           // 3 stacked markers per dot: yellow halo, yellow ring, the dot itself
  const dot = +$req<HTMLInputElement>('dot-size').value;
  const x: number[] = [], y: number[] = [], size: number[] = [], color: string[] = [], lc: string[] = [], lw: number[] = [];
  for (const id of ids) {
    const [px, py] = xyOf(id);
    x.push(px, px, px); y.push(py, py, py);
    size.push((dot + 40) * k, (dot + 18) * k, dot + 7 * k);
    color.push(rgba(THEME.highlight, 0.30), 'rgba(0,0,0,0)', clsColor(pts().cls[id] ?? 0));
    lc.push('rgba(0,0,0,0)', THEME.highlight, THEME.ring);
    lw.push(0, 3, 2);
  }
  return { x, y, marker: { size, color, line: { color: lc, width: lw }, opacity: 1 } };
}

function crosshairs(ids: number[]): Array<Partial<P.Shape>> {                  // dashed lines through each dot across the whole plot
  const line: Partial<P.ShapeLine> = { color: rgba(THEME.highlight, 0.55), width: 1, dash: 'dash' };
  return ids.slice(0, MAX_CROSSHAIRS).flatMap((id): Array<Partial<P.Shape>> => {
    const [x, y] = xyOf(id);
    return [{ type: 'line', xref: 'x', yref: 'paper', x0: x, x1: x, y0: 0, y1: 1, line, layer: 'below' },
      { type: 'line', xref: 'paper', yref: 'y', x0: 0, x1: 1, y0: y, y1: y, line, layer: 'below' }];
  });
}

// redraw both overlays; `animate` grows the hover dot in (small -> overshoot -> settle, ~220 ms)
export function updateMarks(animate = false): void {
  const gd: PlotDiv = plotDiv();
  if (!S.view || !gd.data) return;
  if (is3d()) { updateMarks3d(); return; }
  const hi = gd.data.findIndex((t) => t.name === 'hover-dot');
  const pi = gd.data.findIndex((t) => t.name === 'picked');
  if (hi < 0 || pi < 0) return;
  if (hoverAnim !== null) cancelAnimationFrame(hoverAnim);
  const pos = viewPos();
  const picked = S.picked.filter((id) => pos.has(id));
  const hover = hoverDotId !== null && pos.has(hoverDotId) && !picked.includes(hoverDotId) ? [hoverDotId] : [];
  const p = dotMarker(picked), h = dotMarker(hover, animate ? 0.3 : 1);
  const dist = distanceMarks(picked);
  const ed = explainDot();
  const ex = explainMarks([...(ed !== null && pos.has(ed) ? [ed] : []), ...hover, ...picked]);   // why a flagged dot is flagged
  void updateTraces(gd, { x: [p.x, h.x], y: [p.y, h.y], marker: [p.marker, h.marker] },
    { shapes: [...ex.shapes, ...dist.shapes, ...crosshairs([...hover, ...picked.slice().reverse()])],
      annotations: [...pcaLabels(), ...dist.annotations, ...ex.annotations] }, [pi, hi]);
  const id = hover[0];
  if (!animate || id === undefined) return;
  const t0 = performance.now();
  const step = (now: number): void => {
    const t = Math.min(1, (now - t0) / 220);
    const m = dotMarker(hover, 0.3 + 0.7 * t + 0.25 * Math.sin(Math.PI * t));
    void restyle(gd, { x: [m.x], y: [m.y], marker: [m.marker] }, [hi]);
    if (t < 1 && hoverDotId === id) hoverAnim = requestAnimationFrame(step);
  };
  hoverAnim = requestAnimationFrame(step);
}

// PCA: each axis' explained variance, inside the plot (it has no margins to hold axis titles)
export function pcaLabels(): Array<Partial<P.Annotations>> {
  const ev = S.view?.info?.explained_variance;
  if (!ev) return [];
  const text = (n: number): string => `PC${n} (${((ev[n - 1] ?? NaN) * 100).toFixed(1)}%)`;
  const base: Partial<P.Annotations> = { font: { size: 12, color: THEME.muted }, showarrow: false, xref: 'paper', yref: 'paper' };
  return [{ ...base, text: text(1), x: 0.5, y: 0, yanchor: 'bottom', yshift: 4 },
    { ...base, text: text(2), x: 0, y: 0.5, xanchor: 'left', xshift: 4, textangle: '-90' }];
}

// 2+ picked cards: a line between every pair of their dots, labelled with the distance in the current
// feature space (the 2D map distorts distances, the features do not). Dots are numbered like the cards.
export { MAX_DIST_PICKS };

interface DistanceMarks { shapes: Array<Partial<P.Shape>>; annotations: Array<Partial<P.Annotations>> }

/** Feature-space distances between the picked boxes (2..MAX_DIST_PICKS, Display › Distances on), or null while
 *  they are fetched (once per pick set; updateMarks runs again when they land) or not wanted. 2D and 3D share it. */
export function pickDistances(picked: number[]): { d: number[][]; metric: string } | null {
  // a drag-select of many cards: lines among the first few would only be noise
  if (picked.length < 2 || picked.length > MAX_DIST_PICKS || !$req<HTMLInputElement>('show-dist').checked || !S.scoresFeature) return null;
  const key = `${S.scoresFeature}|${picked.join(',')}`;
  const dist = S.dist;
  if (dist && dist.key === key && 'd' in dist) return { d: dist.d, metric: dist.metric };
  if (!dist || dist.pending !== key) {
    S.dist = { key: null, pending: key };
    postJSON<DistancesPayload>('api/distances', { ids: picked, feature: S.scoresFeature }).then((r) => {
      if (S.dist && S.dist.pending === key) { S.dist = { key, d: r.d, metric: r.metric }; updateMarks(); }
    }).catch(() => { if (S.dist && S.dist.pending === key) S.dist = null; });
  }
  return null;
}

function distanceMarks(picked: number[]): DistanceMarks {
  const out: DistanceMarks = { shapes: [], annotations: [] };
  const dist = pickDistances(picked);
  if (!dist) return out;
  const ids = picked;
  const pos = ids.map(xyOf);
  const line: Partial<P.ShapeLine> = { color: rgba(THEME.highlight, 0.85), width: 1.5, dash: 'dot' };
  const font: Partial<P.Font> = { family: THEME.mono, size: 11, color: THEME.text };
  for (let a = 0; a < ids.length; a++) {
    for (let b = a + 1; b < ids.length; b++) {
      const [x0, y0] = pos[a] ?? [NaN, NaN], [x1, y1] = pos[b] ?? [NaN, NaN];
      const d = dist.d[a]?.[b] ?? NaN;
      out.shapes.push({ type: 'line', xref: 'x', yref: 'y', x0, y0, x1, y1, line, layer: 'above' });
      out.annotations.push({ x: (x0 + x1) / 2, y: (y0 + y1) / 2, xref: 'x', yref: 'y', showarrow: false,
        text: d.toFixed(3), font, bgcolor: rgba(THEME.surface2, 0.92), bordercolor: rgba(THEME.highlight, 0.9),
        borderwidth: 1, borderpad: 3, hovertext: `#${ids[a]} ↔ #${ids[b]}: ${dist.metric} ${d.toFixed(4)}` });
    }
  }
  ids.forEach((_id, n) => out.annotations.push({ x: pos[n]?.[0] ?? NaN, y: pos[n]?.[1] ?? NaN, xref: 'x', yref: 'y', showarrow: false, yshift: 20,
    text: `<b>${n + 1}</b>`, font: { family: THEME.mono, size: 12, color: '#2b1f04' }, bgcolor: THEME.highlight, borderpad: 3 }));
  return out;
}

export function showHoverDot(id: number | null, animate = true): void {
  hoverDotId = id;
  updateMarks(animate && id !== null);
}

// gallery picks: plain click = only this card, ctrl/cmd+click = add / remove it
export function pickCard(id: number, add: boolean): void {
  if (add) S.picked = S.picked.includes(id) ? S.picked.filter((x) => x !== id) : [...S.picked, id];
  else S.picked = [id];
  syncPicked();
}

/** Replace the picks (the gallery's drag-select). */
export function setPicked(ids: number[]): void {
  S.picked = [...new Set(ids)];
  syncPicked();
}

export function clearPicked(): void {
  if (!S.picked.length) return;
  S.picked = [];
  syncPicked();
}

// picks are numbered (1, 2, …) like their dots on the plot once there are two or more
export function pickNumber(id: number): string {
  const n = S.picked.indexOf(id);
  return S.picked.length > 1 && S.picked.length <= MAX_DIST_PICKS && n >= 0 ? String(n + 1) : '';
}

function syncPicked(): void {
  const set = new Set(S.picked);
  document.querySelectorAll<HTMLElement>('#gallery .card').forEach((c) => {
    const id = +(c.dataset.id ?? NaN), on = set.has(id);
    c.classList.toggle('picked', on);
    c.setAttribute('aria-pressed', String(on));
    const n = c.querySelector('.pick-n');
    if (n) n.textContent = pickNumber(id);
  });
  updateMarks();
  window.dispatchEvent(new CustomEvent('picks-changed'));      // the gallery's pick bar
}
