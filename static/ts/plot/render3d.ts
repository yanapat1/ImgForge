// The 3D cloud (a projection computed with dims = 3): one WebGL scatter3d trace per colour, drawn by one
// Plotly.react, plus what only a 3D view can do: camera presets (iso / XY / XZ / YZ, orthographic), a turntable
// spin, the cloud's shadows on the walls, depth fog, drop lines from the picked dots to the floor, feature-space
// distance lines between picks, class centres with spokes, and screen-space selection (Shift+drag, see
// interactions.ts enableLasso3d), which projects every dot through the scene camera.
//
// render.ts renderPlot() hands over here when S.view has z; colouring (categorical / continuous / metadata),
// visibility and hover text are the 2D code's (categorical, visibleIndices, hoverOf), so both views agree.

import type * as P from 'plotly.js';
import type { PlotDiv, Scene3dLayout } from '../types-plot.js';
import { $req, mixHex, rgba } from '../core/dom.js';
import { pts, S, status } from '../core/state.js';
import { clsColor, clsName, CONTINUOUS, isContinuous, plotBase, SCALE, THEME } from '../core/theme.js';
import { originOrder } from '../core/compare-core.js';
import { is3d, plotDiv, restyle, viewPos, xyzOf } from './access.js';
import { categorical, hoverOf, metaColumn, visibleIndices } from './render.js';
import { hoverDotId, MAX_DIST_PICKS, pickDistances } from './marks.js';
import { centreMode, classCentres, explainDot, explainLines3d } from './explain.js';

declare const Plotly: typeof import('plotly.js');

type Trace = Partial<P.PlotData> & Record<string, unknown>;
type Xyz = [number, number, number];
type Col = Array<number | null>;

// 2D marker symbols (combined datasets: one per member) -> the few scatter3d supports
const SYMBOL_3D: Record<string, string> = { circle: 'circle', diamond: 'diamond', square: 'square', 'triangle-up': 'diamond-open',
  cross: 'cross', star: 'square-open', hexagon: 'circle-open', x: 'x' };

// overlay traces (never selected or clicked, hover skipped): names shared with marks.ts OVERLAY
export const OVERLAY_3D = ['highlight', 'centres', 'expl-lines', 'pick-lines', 'drop-lines', 'labels-3d', 'picked', 'hover-dot'];

// the per-colour dot traces of the last render: their ids and base colour, so depth fog can re-colour them
interface DotTrace { idx: number; ids: number[]; base: string | null }
let dotTraces: DotTrace[] = [];
let floorZ = 0;                 // the lowest shown z (drop lines end there)
let renderedKey: string | null = null;   // the cloud last drawn (its camera is kept on a re-render)
let fogged = false;

const checked = (id: string): boolean => $req<HTMLInputElement>(id).checked;

/** One colour with depth fog applied: the fog level is quantised, so mixHex runs a few times per colour, not per dot. */
const fogCache = new Map<string, string>();
const FOG_LEVELS = 12;
function fogColor(base: string, f: number): string {
  const lv = Math.max(0, Math.min(FOG_LEVELS, Math.round(f * FOG_LEVELS)));
  const k = `${base}|${lv}`;
  let c = fogCache.get(k);
  if (c === undefined) { c = lv ? mixHex(base, THEME.plot, 0.78 * lv / FOG_LEVELS) : base; fogCache.set(k, c); }
  return c;
}

function sceneOf(gd: PlotDiv): Scene3dLayout | null {
  return gd._fullLayout?.scene ?? null;
}

// 4 x 4 column-major product a · b (gl-plot3d's matrices)
function mul(a: ArrayLike<number>, b: ArrayLike<number>): Float64Array {
  const o = new Float64Array(16);
  for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
    let v = 0;
    for (let k = 0; k < 4; k++) v += (a[k * 4 + r] ?? 0) * (b[c * 4 + k] ?? 0);
    o[c * 4 + r] = v;
  }
  return o;
}

/** Screen positions (#plot pixels, [x0, y0, x1, y1, …]) of the view indices `idx` through the scene camera, NaN for a
 *  dot behind the camera; null before the scene is drawn. The same maths as Plotly's own 3D annotations:
 *  clip = projection · view · model · (x, y, z) · dataScale, then the scene domain inside the plot area. */
export function screenPoints(idx: number[]): Float64Array | null {
  const gd = plotDiv(), sc = sceneOf(gd), size = gd._fullLayout?._size, view = S.view;
  const cp = sc?._scene?.glplot?.cameraParams, ds = sc?._scene?.dataScale;
  if (!sc || !size || !cp || !ds || !view?.z) return null;
  const m = mul(cp.projection, mul(cp.view, cp.model));
  const X = view.x, Y = view.y, Z = view.z;
  const [dx0, dx1] = sc.domain.x, [dy0, dy1] = sc.domain.y;
  const ox = size.l + dx0 * size.w, oy = size.t + (1 - dy1) * size.h, w = size.w * (dx1 - dx0), h = size.h * (dy1 - dy0);
  const out = new Float64Array(idx.length * 2);
  for (let n = 0; n < idx.length; n++) {
    const i = idx[n] ?? -1;
    const x = (X[i] ?? NaN) * ds[0], y = (Y[i] ?? NaN) * ds[1], z = (Z[i] ?? NaN) * ds[2];
    const cw = (m[3] ?? 0) * x + (m[7] ?? 0) * y + (m[11] ?? 0) * z + (m[15] ?? 0);
    if (!(cw > 0)) { out[2 * n] = NaN; out[2 * n + 1] = NaN; continue; }
    const cx = (m[0] ?? 0) * x + (m[4] ?? 0) * y + (m[8] ?? 0) * z + (m[12] ?? 0);
    const cy = (m[1] ?? 0) * x + (m[5] ?? 0) * y + (m[9] ?? 0) * z + (m[13] ?? 0);
    out[2 * n] = ox + 0.5 * (1 + cx / cw) * w;
    out[2 * n + 1] = oy + 0.5 * (1 - cy / cw) * h;
  }
  return out;
}

/** Distance of each dot of `ids` from the camera (view space), or null before the scene is drawn. */
function depths(ids: number[]): Float64Array | null {
  const gd = plotDiv(), sc = sceneOf(gd), cp = sc?._scene?.glplot?.cameraParams, ds = sc?._scene?.dataScale;
  if (!cp || !ds) return null;
  const m = mul(cp.view, cp.model), out = new Float64Array(ids.length);
  for (let n = 0; n < ids.length; n++) {
    const [x, y, z] = xyzOf(ids[n] ?? -1);
    out[n] = -((m[2] ?? 0) * x * ds[0] + (m[6] ?? 0) * y * ds[1] + (m[10] ?? 0) * z * ds[2] + (m[14] ?? 0));
  }
  return out;
}

function axisTitles(): [string, string, string] {
  const ev = S.view?.info?.explained_variance;
  if (ev && ev.length >= 3) return [1, 2, 3].map((n) => `PC${n} (${((ev[n - 1] ?? NaN) * 100).toFixed(1)}%)`) as [string, string, string];
  const m = S.viewReq ? status().methods[S.viewReq.method]?.label ?? S.viewReq.method : 'dim';
  return [`${m} 1`, `${m} 2`, `${m} 3`];
}

function sceneAxis(title: string): Record<string, unknown> {
  const walls = checked('d3-walls');
  return { title: { text: title, font: { size: 11, color: THEME.muted } }, showticklabels: false, ticks: '', zeroline: false,
    showgrid: walls, gridcolor: THEME.grid, showbackground: walls, backgroundcolor: rgba(THEME.surface2, 0.45),
    showline: walls, linecolor: THEME.border, showspikes: true, spikecolor: THEME.highlight, spikethickness: 1, spikesides: true };
}

function cameraProjection(): Record<string, unknown> { return { type: checked('d3-ortho') ? 'orthographic' : 'perspective' }; }

export function renderPlot3d(): void {
  const view = S.view;
  if (!view?.z) return;
  const Z = view.z;
  let colorBy = $req<HTMLSelectElement>('color-by').value;
  if (isContinuous(colorBy) && !pts()[colorBy]) colorBy = 'class';
  if (colorBy.startsWith('meta:') && !metaColumn(colorBy)) colorBy = 'class';
  const mc = metaColumn(colorBy), metaNum = mc && mc.type === 'num' ? mc : null;
  const dot = +$req<HTMLInputElement>('dot-size').value;
  const vis = visibleIndices();
  const traces: Trace[] = [];
  dotTraces = [];
  fogged = false;
  const idAt = (i: number): number => view.id[i] ?? -1;
  const xs = (idx: number[]): number[] => idx.map((i) => view.x[i] ?? NaN);
  const ys = (idx: number[]): number[] => idx.map((i) => view.y[i] ?? NaN);
  const zs = (idx: number[]): number[] => idx.map((i) => Z[i] ?? NaN);
  floorZ = Infinity;
  for (const i of vis) { const z = Z[i] ?? NaN; if (z < floorZ) floorZ = z; }
  if (!Number.isFinite(floorZ)) floorZ = 0;
  // shadows: each wall gets the dots' projection (three flat views around the cloud)
  const wall = { show: true, opacity: 0.45, scale: 0.9 };
  const proj = checked('d3-shadows') ? { x: wall, y: wall, z: wall } : undefined;
  const outLasso = (id: number): boolean => !!S.dimSet && !S.dimSet.has(id);
  const dotTrace = (name: string, idx: number[], marker: Record<string, unknown>, legend: boolean): Trace => {
    const ids = idx.map(idAt);
    return { type: 'scatter3d', mode: 'markers', name, showlegend: legend, x: idx.length ? xs(idx) : [null], y: idx.length ? ys(idx) : [null],
      z: idx.length ? zs(idx) : [null], customdata: ids, text: ids.map(hoverOf), hoverinfo: 'text',
      marker: { size: dot, line: { width: 0 }, opacity: 1, ...marker }, ...(proj ? { projection: proj } : {}) };
  };
  // scatter3d has one opacity per trace: dots outside a selection go into one trace coloured toward the background
  const inside = S.dimSet ? vis.filter((i) => !outLasso(idAt(i))) : vis;
  if (S.dimSet) {
    const out = vis.filter((i) => outLasso(idAt(i)));
    if (out.length) traces.push(dotTrace('outside lasso', out, { color: mixHex(THEME.muted, THEME.plot, 0.7) }, false));
  }

  if (isContinuous(colorBy) || colorBy === 'group' || metaNum) {
    const ids = inside.map(idAt);
    let marker: Record<string, unknown>;
    const bar = (text: string): Record<string, unknown> => ({ title: { text, side: 'right', font: { color: THEME.muted } }, thickness: 10,
      outlinewidth: 0, tickfont: { color: THEME.muted, size: 11 }, len: 0.6 });
    if (metaNum) {
      marker = { color: ids.map((id) => { const v = metaNum.values[id]; return typeof v === 'number' ? v : NaN; }), colorscale: SCALE.seq,
        showscale: true, colorbar: bar(metaNum.name) };
    } else if (colorBy === 'group') {
      marker = { color: THEME.ungrouped };          // density groups are 2D only
    } else if (isContinuous(colorBy)) {
      const cfg = CONTINUOUS[colorBy], col = pts()[colorBy];
      marker = { color: ids.map((id) => cfg.f(col?.[id] ?? NaN)), colorscale: SCALE[cfg.scale], showscale: true, colorbar: bar(cfg.label) };
    } else {
      marker = {};
    }
    traces.push(dotTrace(colorBy, inside, marker, false));
  } else {
    const { key, label, color, symbol } = categorical(colorBy);
    const groups = new Map<number, number[]>();
    for (const i of inside) {
      const k = key(idAt(i));
      let g = groups.get(k);
      if (!g) { g = []; groups.set(k, g); }
      g.push(i);
    }
    let keys = [...groups.keys()].sort((a, b) => a - b);
    if (colorBy === 'origin' && S.cmp) keys = originOrder(keys, S.cmp.a, S.cmp.b);
    for (const k of keys) {
      const idx = groups.get(k) ?? [], base = color(k), sym = symbol ? { symbol: SYMBOL_3D[symbol(k)] ?? 'circle' } : {};
      const name = `${label(k)} (${idx.length})`, group = `k${k}`;
      // the legend entry is its own empty trace in the dots' legendgroup (a click still hides the dots): its swatch
      // keeps the real colour when depth fog re-colours the dots, which Plotly would otherwise take the swatch from
      traces.push({ type: 'scatter3d', mode: 'markers', name, legendgroup: group, showlegend: true, hoverinfo: 'skip',
        x: [null], y: [null], z: [null], marker: { size: dot, color: base, ...sym } });
      dotTraces.push({ idx: traces.length, ids: idx.map(idAt), base });
      traces.push({ ...dotTrace(name, idx, { color: base, ...sym }, false), legendgroup: group });
    }
  }

  // rings on the highlighted boxes (similar / suspicious / the gallery's view)
  const pos = viewPos();
  const hl = S.highlight.filter((id) => pos.has(id));
  const hxyz = hl.map(xyzOf);
  traces.push({ type: 'scatter3d', mode: 'markers', name: 'highlight', showlegend: false, hoverinfo: 'skip',
    x: hxyz.map((p) => p[0]), y: hxyz.map((p) => p[1]), z: hxyz.map((p) => p[2]), customdata: hl,
    marker: { size: dot + 6, symbol: 'circle-open', color: THEME.ring, line: { color: THEME.ring, width: 2 } } });
  const ct = centreTrace3d(dot);
  if (ct) traces.push(ct);
  // overlays filled by updateMarks3d: lines first (under the markers), then labels and the picked / hovered dots
  for (const name of ['expl-lines', 'pick-lines', 'drop-lines']) {
    traces.push({ type: 'scatter3d', mode: 'lines', name, showlegend: false, hoverinfo: 'skip', x: [], y: [], z: [],
      line: { width: name === 'expl-lines' ? 3 : 2, color: THEME.highlight, ...(name === 'drop-lines' ? { dash: 'dot' } : {}) } });
  }
  traces.push({ type: 'scatter3d', mode: 'text', name: 'labels-3d', showlegend: false, hoverinfo: 'skip', x: [], y: [], z: [], text: [],
    textfont: { family: THEME.mono, size: 11, color: THEME.text } });
  traces.push({ type: 'scatter3d', mode: 'text+markers', name: 'picked', showlegend: false, hoverinfo: 'skip', x: [], y: [], z: [], text: [],
    textposition: 'top center', textfont: { family: THEME.mono, size: 14, color: THEME.text },
    marker: { size: dot + 9, color: THEME.highlight, line: { color: THEME.ring, width: 2 }, opacity: 1 } });
  traces.push({ type: 'scatter3d', mode: 'markers', name: 'hover-dot', showlegend: false, hoverinfo: 'skip', x: [], y: [], z: [],
    marker: { size: dot + 13, color: rgba(THEME.highlight, 0.85), line: { color: THEME.ring, width: 2 }, opacity: 0.9 } });

  const [tx, ty, tz] = axisTitles();
  // the same cloud again (a filter, a colour, a selection): keep the camera as it is now. uirevision keeps only what
  // the mouse changed, not the presets / the spin (Plotly.relayout), which a react would otherwise reset
  const gd = plotDiv();
  const cur = renderedKey === view.key ? (gd._fullLayout?.scene as { camera?: Record<string, unknown> } | undefined)?.camera : undefined;
  const camera = cur ? { ...structuredClone(cur), projection: cameraProjection() } : { projection: cameraProjection() };
  renderedKey = view.key;
  const layout: Record<string, unknown> = {
    ...plotBase(THEME.plot),
    margin: { l: 0, r: 0, t: 0, b: 0 }, hovermode: 'closest',
    scene: { xaxis: sceneAxis(tx), yaxis: sceneAxis(ty), zaxis: sceneAxis(tz), bgcolor: THEME.plot, aspectmode: 'data',
      dragmode: 'turntable', camera },
    legend: { bgcolor: rgba(THEME.surface2, 0.92), bordercolor: THEME.border, borderwidth: 1, itemsizing: 'constant',
      font: { size: 12, color: THEME.text }, x: 0.99, xanchor: 'right', y: 0.01, yanchor: 'bottom' },
    modebar: { bgcolor: 'rgba(0,0,0,0)', color: THEME.muted, activecolor: THEME.accent },
    uirevision: view.key,            // legend clicks etc. survive re-renders of the same cloud
  };
  void Plotly.react(gd, traces as P.Data[], layout as Partial<P.Layout>, {
    responsive: true, displaylogo: false, scrollZoom: true,
    modeBarButtonsToRemove: ['orbitRotation', 'tableRotation', 'zoom3d', 'pan3d', 'resetCameraLastSave3d'] as P.ModeBarDefaultButtons[],
  }).then(() => {
    bindScene(gd);
    updateMarks3d();
    scheduleFog();
  });
}

/** Class centres as labelled diamonds (Display › Class centres, or a Far / Suspicious check). */
function centreTrace3d(dot: number): Trace | null {
  const mode = centreMode();
  if (!mode) return null;
  const x: number[] = [], y: number[] = [], z: number[] = [], color: string[] = [], text: string[] = [];
  for (const [k, v] of [...classCentres(mode)].sort((a, b) => a[0] - b[0])) {
    if (!Number.isFinite(v.z)) continue;
    x.push(v.x); y.push(v.y); z.push(v.z); color.push(clsColor(k)); text.push(clsName(k) ?? String(k));
  }
  return { type: 'scatter3d', mode: 'text+markers', name: 'centres', showlegend: false, hoverinfo: 'text', x, y, z, text,
    hovertext: text.map((t) => `centre of ${t}`), textposition: 'top center', textfont: { family: THEME.font, size: 12, color: THEME.text },
    marker: { symbol: 'diamond', size: dot + 8, color, line: { color: THEME.text, width: 1.5 }, opacity: 1 } };
}

const traceIndex = (gd: PlotDiv, name: string): number => (gd.data ?? []).findIndex((t) => (t as { name?: string }).name === name);

/** Segments [p, q] as one lines trace: points separated by nulls, one colour per vertex. */
function segments(segs: Array<{ p: Xyz; q: Xyz; color: string }>): { x: Col; y: Col; z: Col; c: string[] } {
  const x: Col = [], y: Col = [], z: Col = [], c: string[] = [];
  for (const s of segs) {
    x.push(s.p[0], s.q[0], null); y.push(s.p[1], s.q[1], null); z.push(s.p[2], s.q[2], null);
    c.push(s.color, s.color, s.color);
  }
  return { x, y, z, c };
}

/** The picked / hovered dots, their drop lines, the distance lines between picks and the explanation lines. */
export function updateMarks3d(): void {
  const gd = plotDiv();
  if (!S.view?.z || !gd.data) return;
  const iPick = traceIndex(gd, 'picked'), iHover = traceIndex(gd, 'hover-dot'), iLab = traceIndex(gd, 'labels-3d');
  const iExpl = traceIndex(gd, 'expl-lines'), iDist = traceIndex(gd, 'pick-lines'), iDrop = traceIndex(gd, 'drop-lines');
  if ([iPick, iHover, iLab, iExpl, iDist, iDrop].some((i) => i < 0)) return;
  const pos = viewPos();
  const picked = S.picked.filter((id) => pos.has(id));
  const hover = hoverDotId !== null && pos.has(hoverDotId) && !picked.includes(hoverDotId) ? [hoverDotId] : [];
  const pp = picked.map(xyzOf), hp = hover.map(xyzOf);
  const numbered = picked.length > 1 && picked.length <= MAX_DIST_PICKS;
  // drop lines: from each picked / hovered dot straight down to the floor of the shown dots
  const drop = checked('d3-drop')
    ? segments([...pp, ...hp].map((p) => ({ p, q: [p[0], p[1], floorZ] as Xyz, color: rgba(THEME.highlight, 0.8) })))
    : segments([]);
  // distance lines between picks, labelled with the feature-space distance at their middle
  const dist = pickDistances(picked);
  const dsegs: Array<{ p: Xyz; q: Xyz; color: string }> = [];
  const lab: { x: number[]; y: number[]; z: number[]; t: string[] } = { x: [], y: [], z: [], t: [] };
  if (dist) {
    for (let a = 0; a < picked.length; a++) for (let b = a + 1; b < picked.length; b++) {
      const p = pp[a], q = pp[b];
      if (!p || !q) continue;
      dsegs.push({ p, q, color: THEME.highlight });
      lab.x.push((p[0] + q[0]) / 2); lab.y.push((p[1] + q[1]) / 2); lab.z.push((p[2] + q[2]) / 2);
      lab.t.push((dist.d[a]?.[b] ?? NaN).toFixed(3));
    }
  }
  const ed = explainDot();
  const ex = explainLines3d([...(ed !== null && pos.has(ed) ? [ed] : []), ...hover, ...picked]);
  for (const l of ex.labels) { lab.x.push(l.p[0]); lab.y.push(l.p[1]); lab.z.push(l.p[2]); lab.t.push(l.text); }
  const ds = segments(dsegs), es = segments(ex.segs);
  void restyle(gd, { x: [es.x, ds.x, drop.x], y: [es.y, ds.y, drop.y], z: [es.z, ds.z, drop.z], 'line.color': [es.c, ds.c, drop.c] },
    [iExpl, iDist, iDrop]);
  void restyle(gd, {
    x: [pp.map((p) => p[0]), hp.map((p) => p[0]), lab.x], y: [pp.map((p) => p[1]), hp.map((p) => p[1]), lab.y],
    z: [pp.map((p) => p[2]), hp.map((p) => p[2]), lab.z],
    text: [numbered ? picked.map((_, n) => `${n + 1}`) : picked.map(() => ''), [], lab.t],
  }, [iPick, iHover, iLab]);
}

export function updateHighlight3d(): void {
  const gd = plotDiv(), i = traceIndex(gd, 'highlight');
  if (i < 0) return;
  const pos = viewPos(), hl = S.highlight.filter((id) => pos.has(id)), p = hl.map(xyzOf);
  void restyle(gd, { x: [p.map((v) => v[0])], y: [p.map((v) => v[1])], z: [p.map((v) => v[2])], customdata: [hl] }, [i]);
}

/** Display › Walls and grid / Orthographic: one relayout of the scene, the dots stay. */
export function updateGrid3d(): void {
  if (!is3d() || !plotDiv().data) return;
  const [tx, ty, tz] = axisTitles();
  void Plotly.relayout(plotDiv(), { 'scene.xaxis': sceneAxis(tx), 'scene.yaxis': sceneAxis(ty), 'scene.zaxis': sceneAxis(tz),
    'scene.camera.projection': cameraProjection() } as Partial<P.Layout>);
}

// ---- depth fog: re-colour the per-colour traces by distance from the camera, after the camera rests
let fogTimer: ReturnType<typeof setTimeout> | undefined;
export function scheduleFog(delay = 120): void {
  clearTimeout(fogTimer);
  fogTimer = setTimeout(() => requestAnimationFrame(applyFog), delay);    // a frame later: gl-plot3d's matrices are current
}

function applyFog(): void {
  const gd = plotDiv();
  if (!is3d() || !gd.data || !dotTraces.length) return;
  const on = checked('d3-fog');
  if (!on && !fogged) return;
  const idx = dotTraces.map((t) => t.idx);
  if (!on) {
    fogged = false;
    void restyle(gd, { 'marker.color': dotTraces.map((t) => t.base) }, idx);
    return;
  }
  const all = dotTraces.flatMap((t) => t.ids), d = depths(all);
  if (!d) return;
  let lo = Infinity, hi = -Infinity;
  for (const v of d) { if (v < lo) lo = v; if (v > hi) hi = v; }
  const span = hi - lo || 1;
  let n = 0;
  const colors = dotTraces.map((t) => t.ids.map(() => fogColor(t.base ?? THEME.muted, ((d[n++] ?? lo) - lo) / span)));
  fogged = true;
  void restyle(gd, { 'marker.color': colors }, idx);
}

// ---- camera: presets, the Fit button, the turntable
const PRESETS: Record<string, { eye: Xyz; up: Xyz }> = {
  iso: { eye: [1.35, 1.35, 1.1], up: [0, 0, 1] },
  // straight down the up axis is degenerate for the turntable camera (it picks any roll): a hair of tilt keeps
  // the 1st axis across and the 2nd up the screen
  top: { eye: [0, -0.01, 2.3], up: [0, 0, 1] },
  front: { eye: [0, -2.3, 0], up: [0, 0, 1] },
  side: { eye: [2.3, 0, 0], up: [0, 0, 1] },
};
const v3 = (p: Xyz): { x: number; y: number; z: number } => ({ x: p[0], y: p[1], z: p[2] });

export function setCamera(name: string): void {
  const p = PRESETS[name];
  if (!p || !is3d()) return;
  stopSpin();
  void Plotly.relayout(plotDiv(), { 'scene.camera': { eye: v3(p.eye), up: v3(p.up), center: { x: 0, y: 0, z: 0 },
    projection: cameraProjection() } } as Partial<P.Layout>).then(() => scheduleFog());
}

/** Fit in 3D: back to the isometric camera, the axes fitted to the shown dots. */
export function fit3d(): void {
  if (!is3d()) return;
  void Plotly.relayout(plotDiv(), { 'scene.xaxis.autorange': true, 'scene.yaxis.autorange': true, 'scene.zaxis.autorange': true } as Partial<P.Layout>)
    .then(() => setCamera('iso'));
}

interface Camera { eye?: { x: number; y: number; z: number }; up?: { x: number; y: number; z: number } }
let spinning = false, spinFrame: number | null = null, spinLast = 0, spinBusy = false, held = false, fogTick = 0;

function spinTick(now: number): void {
  spinFrame = null;
  if (!spinning) return;
  if (!is3d()) { stopSpin(); return; }
  spinFrame = requestAnimationFrame(spinTick);
  const dt = Math.min(100, now - (spinLast || now));
  spinLast = now;
  if (held || spinBusy) return;              // the user is rotating by hand / the last frame is still drawing
  const gd = plotDiv(), cam = (gd as unknown as { _fullLayout?: { scene?: { camera?: Camera } } })._fullLayout?.scene?.camera;
  const e = cam?.eye;
  if (!e) return;
  // turn the eye around the up axis (z for the presets; any up vector works by Rodrigues' rotation)
  const u = cam.up ?? { x: 0, y: 0, z: 1 }, un = Math.hypot(u.x, u.y, u.z) || 1;
  const k = { x: u.x / un, y: u.y / un, z: u.z / un };
  const a = (+$req<HTMLInputElement>('d3-speed').value * Math.PI / 180) * dt / 1000, c = Math.cos(a), s = Math.sin(a);
  const dot = k.x * e.x + k.y * e.y + k.z * e.z;
  const cr = { x: k.y * e.z - k.z * e.y, y: k.z * e.x - k.x * e.z, z: k.x * e.y - k.y * e.x };
  const eye = { x: e.x * c + cr.x * s + k.x * dot * (1 - c), y: e.y * c + cr.y * s + k.y * dot * (1 - c), z: e.z * c + cr.z * s + k.z * dot * (1 - c) };
  spinBusy = true;
  void Plotly.relayout(gd, { 'scene.camera.eye': eye } as Partial<P.Layout>).finally(() => {
    spinBusy = false;
    if (checked('d3-fog') && ++fogTick % 6 === 0) applyFog();     // fog follows the spin a few times a second
  });
}

export function startSpin(): void {
  if (spinning || !is3d()) return;
  spinning = true;
  spinLast = 0;
  $req('d3-rotate').setAttribute('aria-pressed', 'true');
  spinFrame = requestAnimationFrame(spinTick);
}

export function stopSpin(): void {
  spinning = false;
  if (spinFrame !== null) cancelAnimationFrame(spinFrame);
  spinFrame = null;
  $req('d3-rotate').setAttribute('aria-pressed', 'false');
}

// once per plot div: the camera moved by hand -> fog; a held mouse pauses the spin
let sceneBound = false;
function bindScene(gd: PlotDiv): void {
  if (sceneBound) return;
  sceneBound = true;
  gd.on('plotly_relayout', (ev: P.PlotRelayoutEvent) => {
    if (is3d() && Object.keys(ev).some((k) => k.startsWith('scene.camera')) && !spinning) scheduleFog();
  });
  gd.addEventListener('pointerdown', () => { held = true; }, true);
  window.addEventListener('pointerup', () => { if (held) { held = false; if (is3d()) scheduleFog(); } }, true);
}

/** The 3D toolbar over the plot and the Display › 3D cloud checks (bound once by explore-bind.ts). */
export function bind3dControls(): void {
  $req('d3-tools').addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('button') : null;
    if (!b) return;
    if (b.id === 'd3-rotate') { if (spinning) stopSpin(); else startSpin(); return; }
    if (b.dataset.cam) setCamera(b.dataset.cam);
  });
  $req('d3-tools').addEventListener('mousedown', (e) => e.stopPropagation());
  $req('d3-fog').addEventListener('change', () => scheduleFog(0));
  $req('d3-drop').addEventListener('change', updateMarks3d);
  $req('d3-walls').addEventListener('change', updateGrid3d);
  $req('d3-ortho').addEventListener('change', updateGrid3d);
  $req('d3-shadows').addEventListener('change', () => { if (is3d()) renderPlot3d(); });
  window.addEventListener('view-changed', () => { if (!is3d()) { stopSpin(); renderedKey = null; } });
}
