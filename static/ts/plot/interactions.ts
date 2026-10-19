// Mouse, trackpad and touch on the plot: lasso (own SVG layer), middle-drag to move it, right-drag / two-finger
// pan, wheel / pinch zoom.

import type { PlotDiv, PlotGeom } from '../types-plot.js';
import { $req } from '../core/dom.js';
import { bindTouch, classifyWheel, isGesturing } from '../core/gesture.js';
import { S } from '../core/state.js';
import { plotDiv, svgReq } from './access.js';
import { pointInPoly, scheduleRender, visibleIndices } from './render.js';
import { setSelection } from '../views/gallery.js';

// Plotly's hover reset is not in its typings
declare const Plotly: typeof import('plotly.js') & { Fx: { unhover(gd: HTMLElement): void } };

/** A point in plot pixels (or data units), [x, y]. */
type Pt = [number, number];
/** The pan / zoom transform p -> s * p + t (see below). */
interface Transform { s: number; tx: number; ty: number }

const lassoLayer = (): SVGSVGElement => svgReq<SVGSVGElement>('#lasso-layer');
const lassoPath = (): SVGPathElement => svgReq<SVGPathElement>('#lasso-path');
const hoverTip = (): HTMLElement => $req('hover-tip');
/** Is the event's target inside an element matching `sel`? */
const hit = (e: Event, sel: string): boolean => e.target instanceof Element && !!e.target.closest(sel);

export function plotAxes(): PlotGeom | null {
  const gd = plotDiv();
  const fl = gd._fullLayout;
  if (!fl || !fl.xaxis || !fl.xaxis._length || !fl.yaxis) return null;
  return { gd, xa: fl.xaxis, ya: fl.yaxis, r: gd.getBoundingClientRect() };
}

const inPlotArea = (g: PlotGeom, px: number, py: number): boolean => px >= g.xa._offset && px <= g.xa._offset + g.xa._length &&
  py >= g.ya._offset && py <= g.ya._offset + g.ya._length;

// ---- pan / zoom fast path
// While a gesture runs the drawn plot is only CSS-transformed (translate + scale, at most once per animation
// frame: no Plotly work per event); one real relayout happens when the gesture rests for 160 ms or ends.
// Plotly.relayout on every frame was the lag. T maps a pixel of the drawn plot (#plot coordinates, which the
// lasso layer shares) to where it is shown now: p -> T.s * p + T.t.
const IDLE_MS = 160;
const ID: Transform = { s: 1, tx: 0, ty: 0 };
let T: Transform = ID, frame: number | null = null, idle: number | undefined, busy: Promise<void> | null = null, held = 0;

const isId = (t: Transform): boolean => t.s === 1 && !t.tx && !t.ty;
const tCss = (): string => (isId(T) ? '' : `translate(${T.tx}px, ${T.ty}px) scale(${T.s})`);

// the lasso layer is drawn in plot pixels too, so it follows T; a middle-drag of the lasso adds its screen offset
let lassoDrawing = false, lassoShift: { dx: number; dy: number } | null = null;
function layerCss(): string {
  const base = S.lasso || lassoDrawing ? tCss() : '';
  return lassoShift ? `translate(${lassoShift.dx}px, ${lassoShift.dy}px) ${base}`.trim() : base;
}
const syncLayer = (): void => { lassoLayer().style.transform = layerCss(); };

function paint(): void {
  frame = null;
  const gd = plotDiv();
  const css = tCss();
  for (const el of gd.querySelectorAll<HTMLElement>('.gl-container, .main-svg:first-of-type')) el.style.transform = css;
  syncLayer();
}

function changed(): void {
  const gd = plotDiv();
  if (!gd.classList.contains('panning')) {
    hoverTip().classList.add('hidden');
    gd.classList.add('panning');               // no hover while it moves (main.js)
  }
  if (frame === null) frame = requestAnimationFrame(paint);
  clearTimeout(idle);
  idle = setTimeout(commit, IDLE_MS);          // resting mid-gesture: draw the newly exposed edges
}

export function panBy(dx: number, dy: number): void {
  if (!dx && !dy) return;
  T = { s: T.s, tx: T.tx + dx, ty: T.ty + dy };
  changed();
}

// k > 1 zooms out (the ranges grow k times) around the client point (cx, cy), clamped to the plot area
export function zoomBy(k: number, cx: number, cy: number): void {
  const g = plotAxes();
  if (!g || !(k > 0) || k === 1) return;
  const px = g.xa._offset + Math.max(0, Math.min(g.xa._length, cx - g.r.left - g.xa._offset));
  const py = g.ya._offset + Math.max(0, Math.min(g.ya._length, cy - g.r.top - g.ya._offset));
  T = { s: T.s / k, tx: px + (T.tx - px) / k, ty: py + (T.ty - py) / k };
  changed();
}

function settle(): void {                      // nothing left to draw and no button held: hover comes back
  if (!held && isId(T) && !busy) plotDiv().classList.remove('panning');
}

function commit(): Promise<void> {
  clearTimeout(idle);
  if (busy) return busy;                       // the relayout in flight re-checks T when it is done
  const gd = plotDiv(), g = plotAxes();
  if (isId(T) || !g) { T = ID; paint(); settle(); return Promise.resolve(); }
  const done = T;
  // the plot area's edges now show the drawn pixels (edge - t) / s
  const xs = (p: number): number => g.xa.p2l((p - done.tx) / done.s - g.xa._offset);
  const ys = (p: number): number => g.ya.p2l((p - done.ty) / done.s - g.ya._offset);
  const rx: Pt = [xs(g.xa._offset), xs(g.xa._offset + g.xa._length)];
  const ry: Pt = [ys(g.ya._offset + g.ya._length), ys(g.ya._offset)];
  const run: Promise<void> = Plotly.relayout(gd, { 'xaxis.range': rx, 'yaxis.range': ry }).catch(() => {}).then(() => {
    busy = null;
    // what moved while Plotly drew stays: the new drawing already is `done`, so show T · done⁻¹ over it
    const s = T.s / done.s;
    T = { s, tx: T.tx - s * done.tx, ty: T.ty - s * done.ty };
    if (Math.abs(T.s - 1) < 1e-9 && Math.abs(T.tx) < 0.01 && Math.abs(T.ty) < 0.01) T = ID;
    if (frame !== null) cancelAnimationFrame(frame);
    paint();
    redrawLasso();
    if (!isId(T)) { clearTimeout(idle); idle = setTimeout(commit, IDLE_MS); }
    settle();
  });
  busy = run;
  return run;
}

// right / middle button held on the plot: the drag pans
function startPan(gd: PlotDiv, e: MouseEvent, button: number): void {
  let last = { x: e.clientX, y: e.clientY };
  held++;
  hoverTip().classList.add('hidden');
  gd.classList.add('panning');
  const move = (ev: MouseEvent): void => {
    ev.stopPropagation();
    panBy(ev.clientX - last.x, ev.clientY - last.y);
    last = { x: ev.clientX, y: ev.clientY };
  };
  const up = (ev: MouseEvent): void => {
    if (ev.button !== button) return;
    ev.stopPropagation();
    window.removeEventListener('mousemove', move, true);
    window.removeEventListener('mouseup', up, true);
    held--;
    void commit().then(settle);
  };
  window.addEventListener('mousemove', move, true);
  window.addEventListener('mouseup', up, true);
}

export function enableRightPan(gd: PlotDiv): void {
  gd.addEventListener('contextmenu', (e) => e.preventDefault());
  gd.addEventListener('mousedown', (e) => {
    if (e.button !== 2 || !plotAxes()) return;
    e.preventDefault();
    e.stopPropagation();                       // Plotly never sees the right button
    startPan(gd, e, 2);
  }, true);
}

// Plotly's lasso re-ran the selection over every WebGL trace on each mouse move (the drawing lagged), and
// dragging its outline with the middle button re-selected continuously (the freeze). Here drawing only
// updates one <path>; the selection is computed once, on release. The polygon is kept in data coordinates
// (S.lasso) so it stays on its dots through pan and zoom.
export let lassoEndedAt = 0;
// A two-finger touch pan / pinch (or a Safari gesture) is running, or ended at gestureEndedAt: the tap Plotly
// reads from its last finger is not a click on a dot.
let touchGesture = false, gestureEndedAt = 0;
export const touchGestureRecent = (ms = 350): boolean => touchGesture || isGesturing() || Date.now() - gestureEndedAt < ms;
let abortLasso: (() => void) | null = null;   // a second finger turns a one-finger lasso into a pinch

// Selection shape (toolbar over the plot, kept per browser): freehand lasso, box or circle, both dragged corner
// to corner (the circle fills the box as an ellipse; Shift keeps it round). Box and circle are turned into a polygon in data coordinates, so everything after the
// drawing (selection, middle-drag move, pan / zoom) is the same as the lasso's.
type SelectShape = 'lasso' | 'box' | 'circle';
const SHAPE_KEY = 'select-shape', SHAPES: readonly SelectShape[] = ['lasso', 'box', 'circle'];
const asShape = (v: unknown): SelectShape | null => SHAPES.find((s) => s === v) ?? null;
let shape: SelectShape = 'lasso';
try { shape = asShape(localStorage.getItem(SHAPE_KEY)) ?? shape; } catch (e) { /* private window */ }

export function setSelectShape(v: string): void {
  const next = asShape(v);
  if (!next) return;
  shape = next;
  try { localStorage.setItem(SHAPE_KEY, next); } catch (e) { /* private window */ }
  for (const b of document.querySelectorAll<HTMLElement>('#select-tools [data-shape]')) {
    const on = b.dataset.shape === next;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  }
}

export function enableSelectTools(): void {
  const bar = $req('select-tools');
  bar.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-shape]') : null;
    if (b && b.dataset.shape !== undefined) setSelectShape(b.dataset.shape);
  });
  bar.addEventListener('mousedown', (e) => e.stopPropagation());
  setSelectShape(shape);
}

// the outline (plot pixels) of a box / circle dragged corner to corner from p0 to p1: the circle is the ellipse
// filling that box; square (Shift) makes the circle round, a square box anchored at p0 in the drag direction
function shapePts(kind: SelectShape, p0: Pt, p1: Pt, square: boolean): Pt[] {
  if (kind === 'box') return [p0, [p1[0], p0[1]], p1, [p0[0], p1[1]]];
  let dx = p1[0] - p0[0], dy = p1[1] - p0[1];
  if (square) { const s = Math.max(Math.abs(dx), Math.abs(dy)); dx = (dx < 0 ? -1 : 1) * s; dy = (dy < 0 ? -1 : 1) * s; }
  const cx = p0[0] + dx / 2, cy = p0[1] + dy / 2, rx = Math.abs(dx) / 2, ry = Math.abs(dy) / 2, n = 64, out: Pt[] = [];
  for (let i = 0; i < n; i++) { const a = (i / n) * 2 * Math.PI; out.push([cx + rx * Math.cos(a), cy + ry * Math.sin(a)]); }
  return out;
}

function lassoPathD(pts: Pt[], close: boolean): string {
  if (!pts.length) return '';
  return 'M' + pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('L') + (close ? 'Z' : '');
}

export function redrawLasso(): void {
  const path = lassoPath();
  syncLayer();
  const lasso = S.lasso, g = lasso && plotAxes();
  if (!lasso || !g) { path.setAttribute('d', ''); return; }
  const pts = lasso.x.map((x, i): Pt => [g.xa.l2p(x) + g.xa._offset, g.ya.l2p(lasso.y[i] ?? NaN) + g.ya._offset]);
  path.setAttribute('d', lassoPathD(pts, true));
}

// a plain click in the plot area outside the drawn lasso / box / circle cancels it (main.js)
export function clickOutsideLasso(e: MouseEvent): boolean {
  const lasso = S.lasso, g = lasso && plotAxes();
  if (!lasso || !g || hit(e, '.modebar, .legend, .colorbar, #select-tools')) return false;
  const px = e.clientX - g.r.left, py = e.clientY - g.r.top;
  if (!inPlotArea(g, px, py)) return false;
  const x = g.xa.p2l((px - T.tx) / T.s - g.xa._offset), y = g.ya.p2l((py - T.ty) / T.s - g.ya._offset);
  return !pointInPoly(x, y, lasso.x, lasso.y);
}

export function clearLasso(): void {
  if (!S.lasso && !S.dimSet) return;
  S.lasso = null;
  lassoPath().setAttribute('d', '');
  syncLayer();
  if (S.dimSet) { S.dimSet = null; scheduleRender(); }
}

function lassoIds(poly: { x: number[]; y: number[] }): number[] {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (let i = 0; i < poly.x.length; i++) {
    const px = poly.x[i] ?? NaN, py = poly.y[i] ?? NaN;
    x0 = Math.min(x0, px); x1 = Math.max(x1, px); y0 = Math.min(y0, py); y1 = Math.max(y1, py);
  }
  const out: number[] = [];
  const view = S.view;
  if (!view) return out;
  const X = view.x, Y = view.y;
  for (const i of visibleIndices()) {
    const x = X[i] ?? NaN, y = Y[i] ?? NaN;
    if (x < x0 || x > x1 || y < y0 || y > y1) continue;      // cheap bbox test first
    if (pointInPoly(x, y, poly.x, poly.y)) out.push(view.id[i] ?? -1);
  }
  return out;
}

function applyLasso(): void {
  if (!S.lasso) return;
  const ids = lassoIds(S.lasso);
  setSelection(ids, 'Lasso selection', { highlight: null, keepLasso: true });
  S.dimSet = new Set(ids);
  redrawLasso();
  scheduleRender();                            // one re-render dims the dots outside the lasso
}

export function enableLasso(gd: PlotDiv): void {
  const path = lassoPath();
  gd.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || !S.view || hit(e, '.modebar, .legend, .colorbar')) return;
    const g = plotAxes();
    if (!g) return;
    const p0: Pt = [e.clientX - g.r.left, e.clientY - g.r.top];
    if (!inPlotArea(g, p0[0], p0[1])) return;
    // each point goes to data coordinates when it is drawn, through the pan / zoom transform T showing then
    // (a pinch or wheel may run mid-lasso, a relayout may land: axes and T always describe the screen together)
    const toData = (x: number, y: number): Pt => {
      const fl = gd._fullLayout, xa = fl?.xaxis, ya = fl?.yaxis;
      if (!xa || !ya) return [NaN, NaN];
      return [xa.p2l((x - T.tx) / T.s - xa._offset), ya.p2l((y - T.ty) / T.s - ya._offset)];
    };
    const drawn = (): Pt[] => {                // data -> plot pixels; the layer's CSS transform adds T
      const fl = gd._fullLayout, xa = fl?.xaxis, ya = fl?.yaxis;
      if (!xa || !ya) return [];
      return dpts.map((p): Pt => [xa.l2p(p[0]) + xa._offset, ya.l2p(p[1]) + ya._offset]);
    };
    const kind = shape;
    let pts: Pt[] = [p0], dpts: Pt[] = [toData(p0[0], p0[1])];
    let drawing = false, frame: number | null = null;
    const move = (ev: MouseEvent): void => {
      const x = ev.clientX - g.r.left, y = ev.clientY - g.r.top;
      if (!drawing) {
        if (Math.hypot(x - p0[0], y - p0[1]) < 5) return;    // a click, not a lasso: Plotly handles it
        drawing = true;
        lassoDrawing = true;
        syncLayer();
        gd.classList.add('lassoing');
        path.classList.add('drawing');
        hoverTip().classList.add('hidden');
        Plotly.Fx.unhover(gd);
      }
      ev.stopPropagation();                    // no Plotly hover work while drawing
      if (kind !== 'lasso') {                  // box / circle: the whole outline follows the cursor
        pts = shapePts(kind, p0, [x, y], ev.shiftKey);
        dpts = pts.map((p) => toData(p[0], p[1]));
        if (frame === null) frame = requestAnimationFrame(() => { frame = null; path.setAttribute('d', lassoPathD(drawn(), true)); });
        return;
      }
      const last = pts[pts.length - 1] ?? p0;
      if (Math.hypot(x - last[0], y - last[1]) < 2) return;
      pts.push([x, y]);
      dpts.push(toData(x, y));
      if (frame === null) frame = requestAnimationFrame(() => { frame = null; path.setAttribute('d', lassoPathD(drawn(), false)); });
    };
    const stop = (): void => {
      abortLasso = null;
      window.removeEventListener('mousemove', move, true);
      window.removeEventListener('mouseup', up, true);
      if (frame !== null) cancelAnimationFrame(frame);
      lassoDrawing = false;
      syncLayer();
      gd.classList.remove('lassoing');
      path.classList.remove('drawing');
    };
    const up = (): void => {
      stop();
      if (!drawing) return;
      lassoEndedAt = Date.now();               // Plotly sees mousedown+mouseup without a move: ignore its "click"
      if (pts.length < 3) { clearLasso(); return; }
      S.lasso = { x: dpts.map((p) => p[0]), y: dpts.map((p) => p[1]) };
      applyLasso();
    };
    abortLasso = () => { stop(); lassoEndedAt = Date.now(); redrawLasso(); };
    window.addEventListener('mousemove', move, true);
    window.addEventListener('mouseup', up, true);
  }, true);
}

// middle button: drag the lasso to another spot (selection is recomputed once, on release); no lasso = pan
export function enableMiddleDrag(gd: PlotDiv): void {
  const layer = lassoLayer();
  gd.addEventListener('auxclick', (e) => { if (e.button === 1) e.preventDefault(); });
  gd.addEventListener('mousedown', (e) => {
    if (e.button !== 1 || !plotAxes()) return;
    e.preventDefault();                        // no browser autoscroll
    e.stopPropagation();                       // Plotly never sees it
    if (!S.lasso) { startPan(gd, e, 1); return; }
    const start = { x: e.clientX, y: e.clientY };
    let dx = 0, dy = 0, frame: number | null = null;
    gd.classList.add('moving-lasso');
    layer.classList.add('moving');
    hoverTip().classList.add('hidden');
    const move = (ev: MouseEvent): void => {
      ev.stopPropagation();
      dx = ev.clientX - start.x;
      dy = ev.clientY - start.y;
      // composed with the pan / zoom transform T (paint() keeps the offset too)
      if (frame === null) frame = requestAnimationFrame(() => { frame = null; lassoShift = { dx, dy }; syncLayer(); });
    };
    const up = (ev: MouseEvent): void => {
      if (ev.button !== 1) return;
      ev.stopPropagation();
      window.removeEventListener('mousemove', move, true);
      window.removeEventListener('mouseup', up, true);
      if (frame !== null) cancelAnimationFrame(frame);
      lassoShift = null;
      syncLayer();
      layer.classList.remove('moving');
      gd.classList.remove('moving-lasso');
      const g = plotAxes(), lasso = S.lasso;
      if (!g || (!dx && !dy) || !lasso) return;
      // screen pixels are T.s plot pixels
      const ddx = g.xa.p2l(dx / T.s) - g.xa.p2l(0), ddy = g.ya.p2l(dy / T.s) - g.ya.p2l(0);
      S.lasso = { x: lasso.x.map((v) => v + ddx), y: lasso.y.map((v) => v + ddy) };
      applyLasso();
    };
    window.addEventListener('mousemove', move, true);
    window.addEventListener('mouseup', up, true);
  }, true);
}

// Plotly's own scrollZoom does not fire on this plot, so it is off and the wheel is handled here: a mouse
// wheel zooms around the cursor, a two-finger trackpad scroll pans, a pinch (ctrl+wheel) zooms (Settings ›
// Appearance can make every scroll zoom or pan). Two-finger touch and Safari's gesture events pan / pinch too.
export function enableWheelZoom(gd: PlotDiv): void {
  let acc = 0, at: { x: number; y: number } | null = null, zf: number | null = null;
  const ready = (): boolean => !!(gd._fullLayout && gd._fullLayout.xaxis && S.view);
  gd.addEventListener('wheel', (e) => {
    if (!ready()) return;
    e.preventDefault();
    e.stopPropagation();
    const kind = classifyWheel(e);
    if (kind === 'pinch' && isGesturing()) return;     // Safari already zooms through gesturechange
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? gd.clientHeight : 1;   // lines / pages -> px
    if (kind === 'pan') { panBy(-e.deltaX * unit, -e.deltaY * unit); return; }
    // zoom steps are summed per frame: a fast wheel is one transform, not many
    acc += kind === 'pinch' ? e.deltaY * unit * (0.01 / 0.0015) : e.deltaY * unit;
    at = { x: e.clientX, y: e.clientY };
    if (zf !== null) return;
    zf = requestAnimationFrame(() => {
      zf = null;
      const k = Math.exp(Math.max(-300, Math.min(300, acc)) * 0.0015);   // > 1 zooms out
      acc = 0;
      if (at) zoomBy(k, at.x, at.y);
    });
  }, { passive: false, capture: true });
  bindTouch(gd, {
    onStart: () => { touchGesture = true; if (abortLasso) abortLasso(); },
    onPan: (dx, dy) => { if (ready()) panBy(dx, dy); },
    onPinch: (scale, cx, cy) => { if (ready()) zoomBy(1 / scale, cx, cy); },
    onEnd: () => { gestureEndedAt = Date.now(); void commit(); },
    onRelease: () => { touchGesture = false; gestureEndedAt = Date.now(); },
  });
}
