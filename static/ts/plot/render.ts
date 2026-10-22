// Draws the embedding plot: one WebGL trace per colour, hover text cache, the highlight rings.

import type * as P from 'plotly.js';
import type { Points, ProjectionReady } from '../types.js';
import { $req, esc, mixHex, rgba } from '../core/dom.js';
import { pts, S } from '../core/state.js';
import { clsColor, clsName, CONTINUOUS, ERR, errColor, isContinuous, plotBase, SCALE, THEME } from '../core/theme.js';
import { curGroups, groupList, plotDiv, restyle, viewPos, xyOf } from './access.js';
import { isOutlier } from './groups.js';
import { redrawLasso } from './interactions.js';
import { updateMarks } from './marks.js';

declare const Plotly: typeof import('plotly.js');

// the projection always holds every class; the mode / class chips only choose which classes are drawn
export function classShown(c: number): boolean {
  const mode = $req<HTMLSelectElement>('mode').value;
  if (mode === 'single') return c === +$req<HTMLSelectElement>('class-a').value;
  if (mode === 'compare') return c === +$req<HTMLSelectElement>('class-a').value || c === +$req<HTMLSelectElement>('class-b').value;
  return !S.hidden.has(c);
}

export function visibleIndices(): number[] {
  const out: number[] = [];
  if (!S.view) return out;
  const mask = S.filterMask;               // filter bar (core/filter.js): 1 = shown; also hides curation-deleted boxes
  const cls = pts().cls;
  for (let i = 0; i < S.view.id.length; i++) {
    const id = S.view.id[i];
    if (id === undefined) continue;
    if (mask && !mask[id]) continue;
    if (!classShown(cls[id] ?? -1)) continue;
    out.push(i);
  }
  return out;
}

/** Colouring by a categorical key: key per box, legend label and colour per key (class is the default). */
interface Categorical {
  key: (id: number) => number;
  label: (k: number) => string | undefined;
  color: (k: number) => string;
}

// the class-coloured traces of the last render, so a filter change can restyle only their point arrays
// (null when the last render had per-dot opacity, a lasso fade or outlier marks: those re-render fully)
interface CatTraces { colorBy: string; view: ProjectionReady; keys: number[]; label: Categorical['label']; idx: number[] }
let catTraces: CatTraces | null = null;

// the filter mask changed: restyle x / y / customdata / text / name of the existing per-colour traces in one
// Plotly.restyle (no layout work, hover strings come from the cache); anything else falls back to a full render
export function applyFilterMask(): void {
  const gd = plotDiv();
  if (!S.view || !gd.data) return;
  const view = S.view;
  const ct = catTraces;
  if (!ct || ct.colorBy !== $req<HTMLSelectElement>('color-by').value || ct.view !== S.view || S.dimSet ||
      (S.groups && ($req<HTMLInputElement>('g-dim').checked || $req<HTMLInputElement>('g-outliers').checked))) {
    scheduleRender();
    return;
  }
  const { key } = categorical(ct.colorBy);
  const groups = new Map<number, number[]>(ct.keys.map((k) => [k, []]));
  for (const i of visibleIndices()) {
    const g = groups.get(key(view.id[i] ?? -1));
    if (!g) { scheduleRender(); return; }          // a colour the plot has no trace for yet
    g.push(i);
  }
  const upd: { x: Array<Array<number | null>>; y: Array<Array<number | null>>; customdata: number[][]; text: string[][]; name: string[] } =
    { x: [], y: [], customdata: [], text: [], name: [] };
  for (const k of ct.keys) {
    const idx = groups.get(k) ?? [];
    const ids = idx.map((i) => view.id[i] ?? -1);
    upd.x.push(idx.length ? idx.map((i) => view.x[i] ?? NaN) : [null]);
    upd.y.push(idx.length ? idx.map((i) => view.y[i] ?? NaN) : [null]);
    upd.customdata.push(ids);
    upd.text.push(ids.map(hoverOf));
    upd.name.push(`${ct.label(k)} (${idx.length})`);
  }
  void restyle(gd, upd, ct.idx);
}

function hoverText(id: number): string {
  const Pt = pts();
  let t = `<b>${esc(clsName(Pt.cls[id] ?? -1))}</b> #${id}<br>${esc(Pt.image[id])}`;
  if (Pt.suggested && Pt.suggested[id] !== Pt.cls[id]) {
    t += `<br>neighbours say: ${esc(clsName(Pt.suggested[id] ?? -1))} (${Math.round((Pt.suspicion?.[id] ?? NaN) * 100)}%)`;
  }
  if (Pt.m_status && (Pt.m_status[id] ?? -1) >= 0) {
    const st = Pt.m_status[id] ?? -1, pc = Pt.m_pred?.[id] ?? -1, sc = Pt.m_score?.[id] ?? NaN;
    t += `<br>model: <b style="color:${errColor(st)}">${ERR[st]?.label}</b>` +
      (pc >= 0 && st !== 0 ? ` → ${esc(clsName(pc))}` : '') + (sc > 0 ? ` (${sc.toFixed(2)})` : '');
  }
  if (S.groups) {
    const g = S.groups.of.get(id);
    t += g === -1 ? `<br><b style="color:${THEME.danger}">outlier</b> (no group)` : `<br>group ${g}`;
  }
  return t;
}

// hover strings are built once per (scores, groups) state, not on every render
let hoverCache: Array<string | undefined> | null = null;

export function invalidateHover(): void { hoverCache = null; }

export function hoverOf(id: number): string {
  if (!hoverCache) hoverCache = new Array<string | undefined>(pts().cls.length);
  let t = hoverCache[id];
  if (t === undefined) t = hoverCache[id] = hoverText(id);
  return t;
}

// coalesce bursts of renders (slider drags, chip clicks) into one per animation frame
let renderQueued = false;

export function scheduleRender(): void {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderPlot(); });
}

// selection changes only move the white rings: restyle that one trace instead of rebuilding the plot
export function updateHighlight(): void {
  const gd = plotDiv();
  if (!S.view || !gd.data) return;
  const idx = gd.data.findIndex((t) => t.name === 'highlight');
  if (idx < 0) { scheduleRender(); return; }
  const pos = viewPos();
  const hl = S.highlight.filter((id) => pos.has(id));
  void restyle(gd, { x: [hl.map((id) => xyOf(id)[0])], y: [hl.map((id) => xyOf(id)[1])], customdata: [hl] }, [idx]);
}

export function pointInPoly(x: number, y: number, xs: ArrayLike<number>, ys: ArrayLike<number>): boolean {
  let inside = false;
  for (let i = 0, j = xs.length - 1; i < xs.length; j = i++) {
    const xi = xs[i] ?? NaN, yi = ys[i] ?? NaN, xj = xs[j] ?? NaN, yj = ys[j] ?? NaN;
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

// which bubble is under the mouse (smallest one if they overlap)
export function bubbleAt(clientX: number, clientY: number): number | null {
  const gd = plotDiv();
  const xa = gd._fullLayout?.xaxis, ya = gd._fullLayout?.yaxis;
  if (!xa || !ya) return null;
  const r = gd.getBoundingClientRect();
  const x = xa.p2l(clientX - r.left - xa._offset), y = ya.p2l(clientY - r.top - ya._offset);
  let best: number | null = null, bestN = Infinity;
  for (const g of groupList(curGroups())) {
    if (g.pure && !classShown(g.cls)) continue;
    if (g.n < bestN && pointInPoly(x, y, g.x, g.y)) { best = g.id; bestN = g.n; }
  }
  return best;
}

// embeddings sessions: colour by a metadata column ('meta:<name>'); numbers go on a scale, categories get a legend
function metaColumn(colorBy: string): { name: string; type: string; values: Array<number | string | null> } | null {
  if (!colorBy.startsWith('meta:')) return null;
  const Pt = pts(), name = colorBy.slice(5);
  const values = Pt.meta?.[name], type = Pt.meta_types?.[name];
  return values && type ? { name, type, values } : null;
}

// categorical colourings: key per box, legend label and colour per key (class is the default)
function categorical(colorBy: string): Categorical {
  const Pt: Points = pts();
  const at = (a: ArrayLike<number>, id: number): number => a[id] ?? -1;
  const mc = metaColumn(colorBy);
  if (mc && mc.type !== 'num') {
    // values ordered by how often they occur, so the commonest get the first (most distinct) palette colours
    const counts = new Map<string, number>();
    for (const v of mc.values) if (v !== null && v !== '') counts.set(String(v), (counts.get(String(v)) ?? 0) + 1);
    const order = [...counts.keys()].sort((a, b) => (counts.get(b) ?? 0) - (counts.get(a) ?? 0) || a.localeCompare(b));
    const idx = new Map(order.map((v, i): [string, number] => [v, i]));
    return { key: (id) => { const v = mc.values[id]; return v === null || v === '' || v === undefined ? -1 : idx.get(String(v)) ?? -1; },
      label: (k) => (k < 0 ? `${mc.name}: blank` : order[k]), color: (k) => (k < 0 ? THEME.ungrouped : clsColor(k)) };
  }
  if (colorBy === 'session') {
    const idx = S.sessionIdx ?? new Map<string, number>();
    const sessions = [...idx.keys()];
    return { key: (id) => idx.get(Pt.session[id] ?? '') ?? -1, label: (k) => sessions[k], color: clsColor };
  }
  const sug = Pt.suggested, ms = Pt.m_status, mp = Pt.m_pred;
  if (colorBy === 'suggested' && sug) return { key: (id) => at(sug, id), label: clsName, color: clsColor };
  if (colorBy === 'm_status' && ms) {
    return { key: (id) => at(ms, id), label: (k) => (k < 0 ? 'not scored (other split)' : `model: ${ERR[k]?.label}`), color: errColor };
  }
  if (colorBy === 'm_pred' && mp && ms) {
    return { key: (id) => (at(ms, id) < 0 ? -2 : at(mp, id)),
      label: (k) => (k === -2 ? 'not scored (other split)' : k < 0 ? 'model: nothing found' : `model: ${clsName(k)}`),
      color: (k) => (k < 0 ? (k === -2 ? THEME.ungrouped : THEME.errMiss) : clsColor(k)) };
  }
  return { key: (id) => at(Pt.cls, id), label: clsName, color: clsColor };
}

type Xy = Array<number | null>;
type Trace = Partial<P.PlotData>;

export function renderPlot(): void {
  if (!S.view) return;
  const view = S.view;
  let colorBy = $req<HTMLSelectElement>('color-by').value;
  if (isContinuous(colorBy) && !pts()[colorBy]) colorBy = 'class';      // e.g. model colours before a model is scored
  if (colorBy.startsWith('meta:') && !metaColumn(colorBy)) colorBy = 'class';
  const metaNum = ((): { name: string; values: Array<number | string | null> } | null => {
    const mc = metaColumn(colorBy);
    return mc && mc.type === 'num' ? mc : null;
  })();
  const dot = +$req<HTMLInputElement>('dot-size').value;
  const vis = visibleIndices();
  const Pt = pts();
  const traces: Trace[] = [];
  catTraces = null;
  const showGroups = S.groups && $req<HTMLInputElement>('g-bubbles').checked;
  const dim = S.groups && $req<HTMLInputElement>('g-dim').checked;
  const idAt = (i: number): number => view.id[i] ?? -1;
  const xAt = (i: number): number => view.x[i] ?? NaN;
  const yAt = (i: number): number => view.y[i] ?? NaN;
  // dim grouped dots (outliers stand out) and/or dots outside the lasso. Outside the lasso the colour is
  // blended into the plot background at full opacity: UMAP stacks many dots on one pixel, and stacked
  // low-alpha dots add back up to a bright blob
  const outLasso = (id: number): boolean => !!S.dimSet && !S.dimSet.has(id);
  const opacityFor = (ids: number[], base: number): number | number[] => {
    if (!dim && !S.dimSet) return base;
    return ids.map((id) => (outLasso(id) ? 1 : dim && !isOutlier(id) ? 0.12 : base));
  };


  // group bubbles (drawn under the dots)
  if (showGroups) {
    // all bubbles of one colour go into ONE WebGL trace (polygons separated by null): ~16 traces instead of ~250
    const perClass = $req<HTMLSelectElement>('g-mode').value === 'class';
    const byCol = new Map<string, { x: Xy; y: Xy }>();
    for (const g of groupList(curGroups())) {
      if (g.pure && !classShown(g.cls)) continue;
      const col = perClass || g.pure ? clsColor(g.cls) : THEME.mixed;
      let b = byCol.get(col);
      if (!b) { b = { x: [], y: [] }; byCol.set(col, b); }
      if (b.x.length) { b.x.push(null); b.y.push(null); }
      for (let i = 0; i < g.x.length; i++) { b.x.push(g.x[i] ?? NaN); b.y.push(g.y[i] ?? NaN); }
    }
    for (const [col, b] of byCol) {
      traces.push({
        type: 'scattergl', mode: 'lines', name: 'bubbles', showlegend: false, hoverinfo: 'skip',
        x: b.x, y: b.y, fill: 'toself', fillcolor: rgba(col, 0.10), line: { color: rgba(col, 0.75), width: 1.5 },
      });
    }
  }

  if (isContinuous(colorBy) || colorBy === 'group' || metaNum) {
    const ids = vis.map(idAt);
    let marker: Partial<P.PlotMarker>;
    if (metaNum) {
      const num = (id: number): number => { const v = metaNum.values[id]; return typeof v === 'number' ? v : NaN; };
      marker = { size: dot, color: ids.map(num), colorscale: SCALE.seq, showscale: true,
        colorbar: { title: { text: metaNum.name, side: 'right', font: { color: THEME.muted } }, thickness: 10,
          outlinewidth: 0, tickfont: { color: THEME.muted, size: 11 } } };
    } else if (colorBy === 'group') {
      const of = S.groups ? S.groups.of : new Map<number, number>();
      marker = { size: dot, color: ids.map((id) => { const g = of.get(id); const c = g == null || g < 0 ? THEME.ungrouped : clsColor(g * 7 + 3);
        return outLasso(id) ? mixHex(c, THEME.plot, 0.82) : c; }) };
    } else if (isContinuous(colorBy)) {
      const cfg = CONTINUOUS[colorBy];
      const col = Pt[colorBy];
      marker = { size: dot, color: ids.map((id) => cfg.f(col?.[id] ?? NaN)), colorscale: SCALE[cfg.scale],
        showscale: true, colorbar: { title: { text: cfg.label, side: 'right', font: { color: THEME.muted } }, thickness: 10,
          outlinewidth: 0, tickfont: { color: THEME.muted, size: 11 } } };
    } else {
      marker = { size: dot };
    }
    // a colour scale cannot be faded per dot by colour: outside the lasso it falls back to low alpha
    marker.opacity = (isContinuous(colorBy) || metaNum) && S.dimSet ? ids.map((id) => (outLasso(id) ? 0.06 : 0.9)) : opacityFor(ids, 0.85);
    traces.push({
      type: 'scattergl', mode: 'markers', name: colorBy, showlegend: false,
      x: vis.map(xAt), y: vis.map(yAt),
      customdata: ids, text: ids.map(hoverOf), hoverinfo: 'text', marker,
    });
  } else {
    const { key, label, color } = categorical(colorBy);
    const groups = new Map<number, number[]>();
    for (const i of vis) {
      const k = key(idAt(i));
      let g = groups.get(k);
      if (!g) { g = []; groups.set(k, g); }
      g.push(i);
    }
    const keys = [...groups.keys()].sort((a, b) => a - b);
    // with a lasso, the dots outside it go into one faded, legend-less trace drawn first (underneath), so the
    // selected dots stay on top and every legend swatch keeps its real colour
    if (S.dimSet) {
      const out: number[] = [];
      for (const k of keys) for (const i of groups.get(k) ?? []) if (outLasso(idAt(i))) out.push(i);
      const ids = out.map(idAt);
      traces.push({
        type: 'scattergl', mode: 'markers', name: 'outside lasso', showlegend: false,
        x: out.map(xAt), y: out.map(yAt), customdata: ids, text: ids.map(hoverOf), hoverinfo: 'text',
        marker: { size: dot, color: ids.map((id) => mixHex(color(key(id)), THEME.plot, 0.82)), opacity: opacityFor(ids, 1) },
      });
    }
    if (!S.dimSet && !dim) catTraces = { colorBy, view, keys, label, idx: keys.map((_, n) => traces.length + n) };
    for (const k of keys) {
      const all = groups.get(k) ?? [];
      const idx = S.dimSet ? all.filter((i) => !outLasso(idAt(i))) : all;
      const ids = idx.map(idAt);
      traces.push({
        type: 'scattergl', mode: 'markers', name: `${label(k)} (${all.length})`,
        // a class with no dot inside the lasso keeps its legend entry through one empty (null) point
        x: idx.length ? idx.map(xAt) : [null], y: idx.length ? idx.map(yAt) : [null],
        customdata: ids, text: ids.map(hoverOf), hoverinfo: 'text',
        marker: { size: dot, color: color(k), opacity: opacityFor(ids, 0.8) },
      });
    }
  }

  // red ✕ on outliers
  if (S.groups && $req<HTMLInputElement>('g-outliers').checked) {
    const out = vis.filter((i) => isOutlier(idAt(i)));
    const ids = out.map(idAt);
    traces.push({
      type: 'scattergl', mode: 'markers', name: `outliers (${out.length})`,
      x: out.map(xAt), y: out.map(yAt),
      customdata: ids, text: ids.map(hoverOf), hoverinfo: 'text',
      marker: { size: dot + 5, symbol: 'x-thin-open', color: THEME.danger, line: { color: THEME.danger, width: 2 }, opacity: 0.9 },
    });
  }

  // highlight ring overlay (similar / suspicious / current gallery)
  const pos = viewPos();
  const hl = S.highlight.filter((id) => pos.has(id));
  traces.push({
    type: 'scattergl', mode: 'markers', name: 'highlight', showlegend: false, hoverinfo: 'skip',
    x: hl.map((id) => xyOf(id)[0]), y: hl.map((id) => xyOf(id)[1]), customdata: hl,
    marker: { size: dot + 8, color: 'rgba(0,0,0,0)', line: { color: THEME.ring, width: 2 } },
  });
  // gallery picks + hover: big yellow dots on top (filled by updateMarks)
  traces.push({ type: 'scattergl', mode: 'markers', name: 'picked', showlegend: false, hoverinfo: 'skip', x: [], y: [] });
  traces.push({ type: 'scattergl', mode: 'markers', name: 'hover-dot', showlegend: false, hoverinfo: 'skip', x: [], y: [] });

  const ev = view.info && view.info.explained_variance;
  const axis = (n: number): Partial<P.LayoutAxis> => ({
    showgrid: false, zeroline: false, showticklabels: false,
    ...(ev ? { title: { text: `PC${n} (${((ev[n - 1] ?? NaN) * 100).toFixed(1)}%)`, font: { size: 12, color: THEME.muted } } } : {}),
  });
  const layout: Partial<P.Layout> = {
    ...plotBase(THEME.plot),
    margin: { l: ev ? 30 : 10, r: 10, t: 30, b: ev ? 30 : 10 }, dragmode: false, hovermode: 'closest', hoverdistance: 10,
    xaxis: axis(1), yaxis: { ...axis(2), scaleanchor: 'x' },
    legend: { bgcolor: rgba(THEME.surface2, 0.92), bordercolor: THEME.border, borderwidth: 1, itemsizing: 'constant',
      font: { size: 12, color: THEME.text }, x: 1, xanchor: 'right', y: 0.98, yanchor: 'top' },
    modebar: { bgcolor: 'rgba(0,0,0,0)', color: THEME.muted, activecolor: THEME.accent },
    uirevision: view.key,
  };
  void Plotly.react('plot', traces, layout, {
    responsive: true, displaylogo: false, scrollZoom: false,   // wheel zoom: enableWheelZoom
    modeBarButtonsToRemove: ['autoScale2d', 'toggleSpikelines', 'lasso2d', 'select2d', 'zoom2d', 'pan2d'],
  }).then(() => {
    updateMarks();
    redrawLasso();
  });
}
