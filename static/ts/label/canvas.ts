// The canvas engine: 3 stacked canvases at devicePixelRatio inside #lb-canvas.
//   L0 image        drawImage of the ImageBitmap; redrawn on view change only (every frame of a pan: the canvases
//                   are viewport-sized, so moving them with a CSS transform would expose the stage background)
//   L1 shapes+tags  committed shapes (batched per class colour) and the name tags; shape / selection / hover / view
//   L2 interaction  crosshair, the tool's preview, handles, shapes being dragged; redrawn on pointermove
// Every redraw goes through invalidate(bits): per-layer dirty bits and one requestAnimationFrame.
// Coordinates are normalised (0–1) everywhere; screen = n * (img px * scale) + t (see T). Hit-testing uses a
// 64×64 uniform grid over the image, rebuilt only when shapes change; the smaller shape wins.

import { rgba } from '../core/dom.js';
import type { Ann, Bounds, ClassStyle, LabelTask, PointerPos, Pt, ShapeAnn, Suggestion, TagRect, Tool } from '../types-label.js';
import { keys } from './hotkeys.js';
import { L } from './state.js';
import { drawTags, layoutTags, modelTagText, setTagFont, syncTagSize, tagH } from './tags.js';
import type { Obstacle, Rect4, TagItem } from './tags.js';

export const D = { IMG: 1, SHAPES: 2, UI: 4, TAGS: 8, INDEX: 16, ALL: 31 };
const G = 64;                     // hit grid cells per side
const BIG = 256;                  // shapes covering more cells than this go in a list that is always checked
export const HIT_TOL = 6;         // px
const MIN_SCALE = 0.05, MAX_SCALE = 40;

/** Colour tokens read from the page CSS (--lb-*). */
export interface CanvasColors {
  cross: string; crossShadow: string; halo: string; handle: string; handleLine: string; sel: string; preview: string;
  tagDark: string; tagLight: string; gem: string; font: string;
}
/** What the page gives the canvas (initCanvas). */
export interface CanvasHandlers {
  tool: () => Tool | null;
  hover: () => void;
  pointer: (p: PointerPos | null) => void;
  view: () => void;
  overlays: () => Element[];
  editStart: () => void;
  /** the tool a press goes to while the area-select key is held */
  areaTool?: () => Tool | null;
}
/** The drawing look of a class (ClassStyle without ids). */
export type ClassLook = Omit<ClassStyle, 'id' | 'task_id'>;

interface CanvasState {
  wrap: HTMLElement | null; c: HTMLCanvasElement[]; x: CanvasRenderingContext2D[]; dpr: number; w: number; h: number; rect: DOMRect | null;
  bitmap: ImageBitmap | null; iw: number; ih: number; dirty: number; raf: number;
  pan: { x: number; y: number; tx: number; ty: number } | null;
  down: { id: number; tool: Tool | null } | null;
  ptr: { sx: number; sy: number; in: boolean };
  grid: Array<ShapeAnn[] | undefined> | null; big: ShapeAnn[]; tags: TagRect[]; vis: Rect4 | null; obstacles: Obstacle[];
  col: CanvasColors; cursor: string; tagTx: number; tagTy: number; sug: Suggestion[]; sugHover: number;
}
const C: CanvasState = {
  wrap: null, c: [], x: [], dpr: 1, w: 0, h: 0, rect: null,
  bitmap: null, iw: 0, ih: 0, dirty: 0, raf: 0,
  pan: null, down: null, ptr: { sx: 0, sy: 0, in: false },
  grid: null, big: [], tags: [], vis: null, obstacles: [],
  col: { cross: '', crossShadow: '', halo: '', handle: '', handleLine: '', sel: '', preview: '', tagDark: '', tagLight: '', gem: '', font: '' },
  cursor: '',
  tagTx: 0, tagTy: 0,             // T.tx / T.ty the tags were laid out at (a pan draws them shifted until relaid)
  sug: [], sugHover: -1,          // ◆ assistant suggestions (label/assist.js): drawn on L1, not hit-tested
};
const wrapEl = (): HTMLElement => { if (!C.wrap) throw new Error('canvas not initialised'); return C.wrap; };
const layer = (i: number): CanvasRenderingContext2D => { const x = C.x[i]; if (!x) throw new Error('canvas not initialised'); return x; };
export const T = { ax: 1, ay: 1, tx: 0, ty: 0 };     // n -> screen: n * a + t
export const CLS = new Map<string, ClassStyle>();    // class id -> {id, name, color, fill, fill2, task_id}
export const TASKS = new Map<string, LabelTask>();   // task id -> Task
export const perf: { on: boolean; l0: number[]; l1: number[]; l2: number[]; tags: number[]; tagDraw: number[]; index: number[] } =
  { on: false, l0: [], l1: [], l2: [], tags: [], tagDraw: [], index: [] };
let H: CanvasHandlers = { tool: () => null, hover: () => {}, pointer: () => {}, view: () => {}, overlays: () => [], editStart: () => {} };

const now = (): number => performance.now();
function rec(arr: number[], t0: number): void { if (perf.on) { arr.push(now() - t0); if (arr.length > 5000) arr.shift(); } }

// ---------------------------------------------------------------- classes / tokens
// each class gets its drawing styles from the settings (Appearance): `color` stays the class colour (lists, swatches);
// `ink` is what the canvas draws with (the class colour, or settings.single_color for every shape when color_mode is
// 'single'), `stroke` its line colour with settings.stroke_opacity, `fill` / `fill2` the normal / emphasised fills
export const STYLE = { sw: 1.5, so: 1, dash: false, hs: 7, halo: true, tagA: 0.85 };
const MODEL_DASH = [5, 4], USER_DASH = [9, 5];
const hex6 = (c: string | null | undefined): string => (c && /^#[0-9a-f]{6}$/i.test(c) ? c : '#888888');

export function indexClasses(): void {
  CLS.clear(); TASKS.clear();
  const s = L.settings;
  const op = +s.fill_opacity || 0;
  const so = s.stroke_opacity == null ? 1 : Math.max(0.05, Math.min(1, +s.stroke_opacity));
  const single = s.color_mode === 'single' ? hex6(s.single_color) : null;
  STYLE.sw = +s.stroke_width || 1.5;
  STYLE.so = so;
  STYLE.dash = s.line_style === 'dashed';
  STYLE.hs = Math.max(3, Math.min(16, +s.handle_size || 7));
  STYLE.halo = s.selected_style !== 'thick';
  STYLE.tagA = s.tag_opacity == null ? 0.85 : Math.max(0.1, Math.min(1, +s.tag_opacity));
  for (const t of (L.project && L.project.tasks) || []) {
    TASKS.set(t.id, t);
    for (const c of t.classes || []) {
      const ink = single || hex6(c.color);
      CLS.set(c.id, { id: c.id, name: c.name, color: c.color, ink, stroke: so >= 1 ? ink : rgba(ink, so), task_id: t.id,
        fill: rgba(ink, op), fill2: rgba(ink, Math.min(0.6, op * 2 + 0.12)) });
    }
  }
}
const FALLBACK: ClassLook = { name: '?', color: '#888888', ink: '#888888', stroke: '#888888', fill: 'rgba(136,136,136,.15)', fill2: 'rgba(136,136,136,.3)' };
export const clsOf = (a: { class_id: string }): ClassLook => CLS.get(a.class_id) || FALLBACK;

export function readTokens(): void {
  const cs = getComputedStyle(document.documentElement);
  const v = (n: string): string => cs.getPropertyValue(n).trim();
  C.col = { cross: v('--lb-cross'), crossShadow: v('--lb-cross-shadow'), halo: v('--lb-halo'), handle: v('--lb-handle'),
    handleLine: v('--lb-handle-line'), sel: v('--lb-sel'), preview: v('--lb-preview'), tagDark: v('--lb-tag-dark'),
    tagLight: v('--lb-tag-light'), gem: v('--gem-2') || '#8b7cf6', font: v('--font') || 'sans-serif' };
  setTagFont(cs.getPropertyValue('--font').trim());
  invalidate(D.ALL);
}
export const colors = (): CanvasColors => C.col;

// ---------------------------------------------------------------- geometry bounds (cached per immutable Ann)
const BOUNDS = new WeakMap<Ann, Bounds | null>();
export function bounds(a: Ann): Bounds | null {
  let b = BOUNDS.get(a);
  if (b !== undefined) return b;
  b = null;
  if (a.geometry_type === 'bbox') {
    const g = a.geometry;
    b = { x0: g.x, y0: g.y, x1: g.x + g.w, y1: g.y + g.h, area: g.w * g.h };
  } else if (a.geometry_type === 'polygon' && a.geometry.points && a.geometry.points.length) {
    const g = a.geometry;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, s = 0;
    const p = g.points;
    for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
      const pi = p[i], pj = p[j];
      if (!pi || !pj) continue;
      const x = pi[0], y = pi[1];
      if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
      s += (pj[0] + x) * (pj[1] - y);
    }
    b = { x0, y0, x1, y1, area: Math.abs(s / 2) };
  } else if (a.geometry_type === 'keypoints' && a.geometry.points) {
    const g = a.geometry;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, n = 0;
    for (const q of g.points) {
      if (!q[2]) continue;
      n++;
      if (q[0] < x0) x0 = q[0]; if (q[0] > x1) x1 = q[0]; if (q[1] < y0) y0 = q[1]; if (q[1] > y1) y1 = q[1];
    }
    if (n) b = { x0, y0, x1, y1, area: Math.max((x1 - x0) * (y1 - y0), 1e-6) };
  }
  BOUNDS.set(a, b);
  return b;
}

// ---------------------------------------------------------------- setup
export function initCanvas(wrap: HTMLElement, handlers: Partial<CanvasHandlers>): void {
  H = { ...H, ...handlers };
  C.wrap = wrap;
  wrap.innerHTML = '<canvas class="lb-c0" aria-hidden="true"></canvas><canvas class="lb-c1" aria-hidden="true"></canvas>' +
    '<canvas class="lb-c2" aria-hidden="true"></canvas>';
  C.c = [...wrap.querySelectorAll('canvas')];
  C.x = C.c.map((c, i) => {
    const x = c.getContext('2d', { alpha: true, desynchronized: i === 2 });
    if (!x) throw new Error('2d canvas is not available');
    return x;
  });
  readTokens();
  new ResizeObserver(resize).observe(wrap);
  addEventListener('resize', () => { C.rect = wrap.getBoundingClientRect(); });
  wrap.addEventListener('pointerdown', onDown);
  wrap.addEventListener('pointermove', onMove);
  wrap.addEventListener('pointerup', onUp);
  wrap.addEventListener('pointercancel', onUp);
  wrap.addEventListener('pointerleave', () => {
    C.ptr.in = false; L.cursor = null;
    if (!C.down && L.hoverId) { L.hoverId = null; H.hover(); }
    H.pointer(null);
    invalidate(D.UI);
  });
  wrap.addEventListener('wheel', onWheel, { passive: false });
  wrap.addEventListener('dblclick', (e) => { const t = H.tool(); if (!L.loading && t && t.onDbl) t.onDbl(pt(e), e); });
  wrap.addEventListener('contextmenu', (e) => e.preventDefault());
  new MutationObserver(readTokens).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-look'] });
}

function resize(): void {
  const r = wrapEl().getBoundingClientRect();
  C.rect = r;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
  if (w === C.w && h === C.h && dpr === C.dpr) return;
  const old = { w: C.w, h: C.h };
  C.w = w; C.h = h; C.dpr = dpr;
  for (const c of C.c) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  if (L.view.fitted || !old.w) fit();
  else {                                     // keep the image point at the centre where it was
    L.view.tx += (w - old.w) / 2; L.view.ty += (h - old.h) / 2;
    syncT();
  }
  C.vis = null;
  invalidate(D.ALL);
}

export function overlaysChanged(): void { C.vis = null; invalidate(D.TAGS | D.SHAPES); }

// ---------------------------------------------------------------- view
function syncT(): void {
  T.ax = C.iw * L.view.scale; T.ay = C.ih * L.view.scale; T.tx = L.view.tx; T.ty = L.view.ty;
}

export function setImage(bitmap: ImageBitmap | null, w?: number, h?: number): void {
  C.bitmap = bitmap;
  C.sug = []; C.sugHover = -1;              // suggestions belong to one image
  C.iw = w || (bitmap && bitmap.width) || 0;
  C.ih = h || (bitmap && bitmap.height) || 0;
  fit();
}
export const imageSize = (): { w: number; h: number } => ({ w: C.iw, h: C.ih });
export const hasImage = (): boolean => !!C.iw;

export function fit(): void {
  if (!C.iw || !C.w) { syncT(); invalidate(D.ALL); return; }
  const pad = 16;
  const s = Math.max(MIN_SCALE, Math.min((C.w - 2 * pad) / C.iw, (C.h - 2 * pad) / C.ih));
  L.view.scale = s;
  L.view.tx = (C.w - C.iw * s) / 2;
  L.view.ty = (C.h - C.ih * s) / 2;
  L.view.fitted = true;
  viewChanged();
}

function viewChanged(): void { syncT(); invalidate(D.IMG | D.SHAPES | D.TAGS | D.UI); H.view(); }

export function zoomAt(sx: number, sy: number, f: number): void {
  const v = L.view;
  const s = Math.min(MAX_SCALE, Math.max(MIN_SCALE, v.scale * f));
  f = s / v.scale;
  if (f === 1) return;
  v.tx = sx - (sx - v.tx) * f;
  v.ty = sy - (sy - v.ty) * f;
  v.scale = s;
  v.fitted = false;
  viewChanged();
}

export function zoomTo(s: number): void { zoomAt(C.w / 2, C.h / 2, s / L.view.scale); }

export function panBy(dx: number, dy: number): void { L.view.tx += dx; L.view.ty += dy; L.view.fitted = false; viewChanged(); }

// pan an annotation into view when any part of it is off the visible rect (zoom out if it does not fit)
export function panTo(a: Ann): void {
  const b = bounds(a);
  if (!b || !C.iw) return;
  const V = visible();
  const s = { x0: b.x0 * T.ax + T.tx, y0: b.y0 * T.ay + T.ty, x1: b.x1 * T.ax + T.tx, y1: b.y1 * T.ay + T.ty };
  if (s.x0 >= V.x0 && s.y0 >= V.y0 && s.x1 <= V.x1 && s.y1 <= V.y1) return;
  const vw = V.x1 - V.x0, vh = V.y1 - V.y0;
  const need = Math.max((s.x1 - s.x0) / (vw * 0.8), (s.y1 - s.y0) / (vh * 0.8));
  if (need > 1) zoomAt((s.x0 + s.x1) / 2, (s.y0 + s.y1) / 2, 1 / need);
  const cx = (b.x0 + b.x1) / 2 * T.ax + T.tx, cy = (b.y0 + b.y1) / 2 * T.ay + T.ty;
  panBy((V.x0 + V.x1) / 2 - cx, (V.y0 + V.y1) / 2 - cy);
}

// ---------------------------------------------------------------- scheduling
export function invalidate(bits: number): void {
  C.dirty |= bits;
  if (!C.raf) C.raf = requestAnimationFrame(frame);
}
export const shapesChanged = () => invalidate(D.INDEX | D.TAGS | D.SHAPES | D.UI);
export const selectionChanged = () => invalidate(D.TAGS | D.SHAPES | D.UI);
export const hoverChanged = () => invalidate(L.settings.tags === 'hover_selected' ? D.TAGS | D.SHAPES : D.SHAPES);

function frame(): void {
  C.raf = 0;
  const d = C.dirty;
  C.dirty = 0;
  if (!C.w) return;
  let t0: number;
  if (d & D.INDEX) { t0 = now(); buildIndex(); rec(perf.index, t0); }
  if (d & D.TAGS) { t0 = now(); computeTags(); rec(perf.tags, t0); }
  if (d & D.IMG) { t0 = now(); drawImage(); rec(perf.l0, t0); }
  if (d & D.SHAPES) { t0 = now(); drawShapes(); rec(perf.l1, t0); }
  if (d & D.UI) { t0 = now(); drawUI(); rec(perf.l2, t0); }
}

// ---------------------------------------------------------------- L0 image
function drawImage(): void {
  const x = layer(0);
  x.setTransform(C.dpr, 0, 0, C.dpr, 0, 0);
  x.clearRect(0, 0, C.w, C.h);
  if (!C.bitmap) return;
  x.imageSmoothingEnabled = L.view.scale < 3;
  x.imageSmoothingQuality = 'high';
  x.drawImage(C.bitmap, T.tx, T.ty, T.ax, T.ay);
}

// ---------------------------------------------------------------- L1 shapes
const sx = (n: number): number => n * T.ax + T.tx;
const sy = (n: number): number => n * T.ay + T.ty;

function onScreen(b: Rect4): boolean {
  return !(b.x1 * T.ax + T.tx < -2 || b.x0 * T.ax + T.tx > C.w + 2 || b.y1 * T.ay + T.ty < -2 || b.y0 * T.ay + T.ty > C.h + 2);
}

// adds the outline of one annotation to the current path (keypoints: only their skeleton)
function tracePath(x: CanvasRenderingContext2D, a: Ann): void {
  if (a.geometry_type === 'bbox') {
    const g = a.geometry;
    x.rect(sx(g.x), sy(g.y), g.w * T.ax, g.h * T.ay);
  } else if (a.geometry_type === 'polygon') {
    const g = a.geometry;
    const rings = [g.points, ...(g.holes || [])];
    for (const p of rings) {
      const [p0, ...rest] = p || [];
      if (!p0) continue;
      x.moveTo(sx(p0[0]), sy(p0[1]));
      for (const q of rest) x.lineTo(sx(q[0]), sy(q[1]));
      x.closePath();
    }
  } else if (a.geometry_type === 'keypoints') {
    const t = TASKS.get(a.task_id);
    const sk = (t && t.config && t.config.skeleton) || [];
    const p = a.geometry.points;
    for (const [i, j] of sk) {
      const a1 = p[i], b1 = p[j];
      if (!a1 || !b1 || !a1[2] || !b1[2]) continue;
      x.moveTo(sx(a1[0]), sy(a1[1]));
      x.lineTo(sx(b1[0]), sy(b1[1]));
    }
  }
}

function tracePoints(x: CanvasRenderingContext2D, a: Ann, r: number, visibleOnly?: boolean): void {
  if (a.geometry_type !== 'keypoints') return;
  for (const q of a.geometry.points) {
    if (!q[2] || (visibleOnly !== undefined && (q[2] === 2) !== visibleOnly)) continue;
    const px = sx(q[0]), py = sy(q[1]);
    x.moveTo(px + r, py);
    x.arc(px, py, r, 0, Math.PI * 2);
  }
}

// draws a list of annotations of one class: fill, dark halo, colour stroke (one path each); dashed: the colour
// stroke is dashed over the solid halo (model labels always, every shape when settings.line_style = 'dashed')
function drawGroup(x: CanvasRenderingContext2D, list: Ann[], c: ClassLook, sw: number, fill: string | null, dashed: number[] | true | null): void {
  x.beginPath();
  let kp = false;
  for (const a of list) { tracePath(x, a); if (a.geometry_type === 'keypoints') kp = true; }
  if (fill) { x.fillStyle = fill; x.fill('evenodd'); }
  const a0 = x.globalAlpha, so = STYLE.so;
  if (so < 1) x.globalAlpha = a0 * so;
  x.strokeStyle = C.col.halo; x.lineWidth = sw + 2; x.stroke();
  if (so < 1) x.globalAlpha = a0;
  if (dashed) x.setLineDash(dashed === true ? MODEL_DASH : dashed);
  x.strokeStyle = c.stroke || c.color; x.lineWidth = sw; x.stroke();
  if (dashed) x.setLineDash([]);
  if (kp) {
    const r = Math.max(3, sw + 1.5);
    const ink = c.ink || c.color;
    x.beginPath();
    for (const a of list) if (a.geometry_type === 'keypoints') tracePoints(x, a, r, true);
    x.fillStyle = ink; x.fill();
    x.strokeStyle = C.col.halo; x.lineWidth = 1.5; x.stroke();
    x.beginPath();
    for (const a of list) if (a.geometry_type === 'keypoints') tracePoints(x, a, r - 0.5, false);
    x.fillStyle = C.col.halo; x.fill();
    x.strokeStyle = ink; x.lineWidth = 2; x.stroke();
  }
}

const isModel = (a: Ann): boolean => a.source === 'model';
const dashOf = (a: Ann): number[] | null => (isModel(a) ? MODEL_DASH : STYLE.dash ? USER_DASH : null);

// the selected style 'halo': a soft light ring under a selected shape (outline only)
function drawSelHalo(x: CanvasRenderingContext2D, a: Ann, sw: number): void {
  x.beginPath();
  tracePath(x, a);
  const a0 = x.globalAlpha;
  x.globalAlpha = a0 * 0.55;
  x.strokeStyle = C.col.sel; x.lineWidth = sw + 6; x.stroke();
  x.globalAlpha = a0;
}

function drawShapes(): void {
  const x = layer(1);
  x.setTransform(C.dpr, 0, 0, C.dpr, 0, 0);
  x.clearRect(0, 0, C.w, C.h);
  if (L.hideAll || !C.iw) return;
  const sw = STYLE.sw;
  const dim = L.settings.dim_other_tasks;
  // batches: class id + model (dashed) + dimmed -> {list, dim, dash}
  const groups = new Map<string, { list: ShapeAnn[]; dim: boolean; dash: number[] | null }>();
  const top: ShapeAnn[] = [];
  for (const a of L.anns.values()) {
    if (a.geometry_type === 'tag' || L.hidden.has(a.id) || L.editing.has(a.id)) continue;
    const b = bounds(a);
    if (!b || !onScreen(b)) continue;
    if (a.id === L.hoverId || L.selected.has(a.id)) { top.push(a); continue; }
    const dm = dim && a.task_id !== L.taskId;
    const md = isModel(a);
    const k = `${a.class_id}|${md ? 1 : 0}${dm ? 1 : 0}`;
    const g = groups.get(k);
    if (g) g.list.push(a); else groups.set(k, { list: [a], dim: dm, dash: md ? MODEL_DASH : STYLE.dash ? USER_DASH : null });
  }
  for (const g of groups.values()) {
    const first = g.list[0];
    if (!first) continue;
    const c = clsOf(first);
    x.globalAlpha = g.dim ? 0.35 : 1;
    drawGroup(x, g.list, c, sw, c.fill, g.dash);
  }
  x.globalAlpha = 1;
  for (const a of top) {
    const c = clsOf(a);
    const sel = L.selected.has(a.id);
    x.globalAlpha = dim && a.task_id !== L.taskId && !sel ? 0.6 : 1;
    const w = sw + (sel ? 1 : 0.5);
    if (sel && STYLE.halo && a.geometry_type !== 'keypoints') drawSelHalo(x, a, w);
    drawGroup(x, [a], c, w, c.fill2, dashOf(a));
  }
  x.globalAlpha = 1;
  if (C.sug.length) drawSuggestions(x);
  // mid-pan the tags keep their layout (D.TAGS waits for the release) and move with the view
  const ox = T.tx - C.tagTx, oy = T.ty - C.tagTy;
  if (ox || oy) x.translate(ox, oy);
  const t0 = now();
  drawTags(x, C.tags, C.col.tagDark, C.col.tagLight, null, STYLE.tagA);
  rec(perf.tagDraw, t0);
}

// ---------------------------------------------------------------- ◆ assistant suggestions
// list: [{x1, y1, x2, y2 (normalised), text}]; the drawer owns the list, the canvas only draws it (dashed, gem colour)
export function setSuggestions(list: Suggestion[] | null): void { C.sug = list || []; if (C.sugHover >= C.sug.length) C.sugHover = -1; invalidate(D.SHAPES); }
export function hoverSuggestion(i: number): void { if (i !== C.sugHover) { C.sugHover = i; invalidate(D.SHAPES); } }

function drawSuggestions(x: CanvasRenderingContext2D): void {
  const col = C.col.gem;
  x.font = `600 11px ${C.col.font}`;
  x.textBaseline = 'middle';
  for (let i = 0; i < C.sug.length; i++) {
    const s = C.sug[i];
    if (!s) continue;
    const b = { x0: s.x1, y0: s.y1, x1: s.x2, y1: s.y2 };
    if (!onScreen(b)) continue;
    const hot = i === C.sugHover;
    const px = sx(s.x1), py = sy(s.y1), w = (s.x2 - s.x1) * T.ax, h = (s.y2 - s.y1) * T.ay;
    if (hot) { x.globalAlpha = 0.16; x.fillStyle = col; x.fillRect(px, py, w, h); x.globalAlpha = 1; }
    x.beginPath();
    x.rect(px, py, w, h);
    x.strokeStyle = C.col.halo; x.lineWidth = (hot ? 3 : 2) + 2; x.stroke();
    x.setLineDash([6, 4]);
    x.strokeStyle = col; x.lineWidth = hot ? 3 : 2; x.stroke();
    x.setLineDash([]);
    if (!s.text) continue;
    // the label sits above the box (inside it when the box touches the top edge)
    const tw = Math.min(x.measureText(s.text).width, 260) + 10, th = 16;
    const ly = py - th >= 0 ? py - th : py;
    x.globalAlpha = hot ? 1 : 0.9;
    x.fillStyle = col; x.fillRect(px - 1, ly, tw, th);
    x.globalAlpha = 1;
    x.fillStyle = '#fff';
    x.fillText(s.text, px + 4, ly + th / 2, tw - 8);
  }
}

// ---------------------------------------------------------------- tags
function visible(): Rect4 {
  if (C.vis) return C.vis;
  const r = C.rect || wrapEl().getBoundingClientRect();
  const V: Rect4 = { x0: 0, y0: 0, x1: C.w, y1: C.h };
  const obstacles: Obstacle[] = [];
  for (const el of H.overlays()) {
    const o = el.getBoundingClientRect();
    if (!o.width || !o.height) continue;
    const x0 = Math.max(0, o.left - r.left), y0 = Math.max(0, o.top - r.top);
    const x1 = Math.min(C.w, o.right - r.left), y1 = Math.min(C.h, o.bottom - r.top);
    if (x1 <= x0 || y1 <= y0) continue;
    // spans (most of) one side: shrink the visible rect from that side; otherwise: an obstacle tags avoid
    if (y1 - y0 >= 0.6 * C.h && x0 <= 1) V.x0 = Math.max(V.x0, x1);
    else if (y1 - y0 >= 0.6 * C.h && x1 >= C.w - 1) V.x1 = Math.min(V.x1, x0);
    else if (x1 - x0 >= 0.6 * C.w && y0 <= 1) V.y0 = Math.max(V.y0, y1);
    else if (x1 - x0 >= 0.6 * C.w && y1 >= C.h - 1) V.y1 = Math.min(V.y1, y0);
    else obstacles.push({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
  }
  if (V.x1 - V.x0 < 40 || V.y1 - V.y0 < tagH()) { V.x0 = 0; V.y0 = 0; V.x1 = C.w; V.y1 = C.h; }
  C.vis = V; C.obstacles = obstacles;
  return V;
}
export const visibleRect = (): Rect4 => ({ ...visible() });

function computeTags(): void {
  const mode = L.settings.tags;
  C.tags = [];
  C.tagTx = T.tx; C.tagTy = T.ty;
  if (mode === 'off' || L.hideAll || !C.iw) return;
  syncTagSize();
  const V = visible();
  const items: Array<TagItem & { prio: number }> = [];
  const counter = new Map<string, number>();
  const withIdx = L.settings.tag_content === 'name_index';
  const push = (a: ShapeAnn, prio: number): void => {
    const b = bounds(a);
    if (!b) return;
    const s = { x0: b.x0 * T.ax + T.tx, y0: b.y0 * T.ay + T.ty, x1: b.x1 * T.ax + T.tx, y1: b.y1 * T.ay + T.ty };
    if (s.x1 < V.x0 || s.x0 > V.x1 || s.y1 < V.y0 || s.y0 > V.y1) return;
    const c = clsOf(a);
    let text = c.name;
    if (withIdx) { const n = (counter.get(a.class_id) || 0) + 1; counter.set(a.class_id, n); text = `${c.name} ${n}`; }
    if (isModel(a)) text = modelTagText(text, a.score);
    items.push({ id: a.id, ...s, text, color: c.ink || c.color, prio });
  };
  for (const a of L.anns.values()) {
    if (a.geometry_type === 'tag' || L.hidden.has(a.id) || L.editing.has(a.id)) continue;
    const sel = L.selected.has(a.id), hov = a.id === L.hoverId;
    if (mode === 'hover_selected' && !sel && !hov) { if (withIdx && a.task_id === L.taskId) counter.set(a.class_id, (counter.get(a.class_id) || 0) + 1); continue; }
    if (a.task_id !== L.taskId && !sel) continue;
    push(a, sel ? 0 : hov ? 1 : 2);
  }
  // selected first, then hovered, then the top-most (last drawn) first
  items.reverse();
  items.sort((a, b) => a.prio - b.prio);
  C.tags = layoutTags(items, V, C.obstacles, STYLE.sw);
}
export const tagRects = (): TagRect[] => C.tags;

// ---------------------------------------------------------------- L2 interaction
export function drawAnn(x: CanvasRenderingContext2D, a: Ann, emphasis?: boolean): void {
  const c = clsOf(a);
  const sw = STYLE.sw + (emphasis ? 1 : 0);
  drawGroup(x, [a], c, sw, c.fill2, dashOf(a));
}

// r: the radius a tool asks for at the default handle size (8 px); settings.handle_size scales it (no r: half of it)
export function drawHandle(x: CanvasRenderingContext2D, px: number, py: number, rIn?: number | null, round = false): void {
  const r = rIn == null ? STYLE.hs / 2 : rIn * STYLE.hs / 8;
  x.beginPath();
  if (round) { x.moveTo(px + r, py); x.arc(px, py, r, 0, Math.PI * 2); } else x.rect(px - r, py - r, 2 * r, 2 * r);
  x.fillStyle = C.col.handle; x.fill();
  x.strokeStyle = C.col.handleLine; x.lineWidth = 1.5; x.stroke();
}

function drawUI(): void {
  const x = layer(2);
  x.setTransform(C.dpr, 0, 0, C.dpr, 0, 0);
  x.clearRect(0, 0, C.w, C.h);
  if (!C.iw) return;
  for (const a of L.editing.values()) drawAnn(x, a, true);
  const tool = C.down ? C.down.tool : H.tool();
  if (tool && tool.drawPreview && !L.hideAll) tool.drawPreview(x);
  if (L.settings.crosshair && C.ptr.in && !C.pan) {
    const px = Math.round(C.ptr.sx) + 0.5, py = Math.round(C.ptr.sy) + 0.5;
    x.lineWidth = 1;
    x.beginPath();
    x.moveTo(0, py + 1); x.lineTo(C.w, py + 1); x.moveTo(px + 1, 0); x.lineTo(px + 1, C.h);
    x.strokeStyle = C.col.crossShadow; x.stroke();
    x.beginPath();
    x.moveTo(0, py); x.lineTo(C.w, py); x.moveTo(px, 0); x.lineTo(px, C.h);
    x.strokeStyle = C.col.cross; x.stroke();
  }
}

// ---------------------------------------------------------------- hit index
function buildIndex(): void {
  C.grid = new Array<ShapeAnn[] | undefined>(G * G);
  C.big = [];
  if (!C.iw) return;
  for (const a of L.anns.values()) {
    if (a.task_id !== L.taskId || a.geometry_type === 'tag' || L.hidden.has(a.id)) continue;
    const b = bounds(a);
    if (!b) continue;
    const cx0 = Math.max(0, Math.floor(b.x0 * G)), cx1 = Math.min(G - 1, Math.floor(b.x1 * G));
    const cy0 = Math.max(0, Math.floor(b.y0 * G)), cy1 = Math.min(G - 1, Math.floor(b.y1 * G));
    if ((cx1 - cx0 + 1) * (cy1 - cy0 + 1) > BIG) { C.big.push(a); continue; }
    for (let cy = cy0; cy <= cy1; cy++) {
      for (let cx = cx0; cx <= cx1; cx++) {
        const k = cy * G + cx;
        (C.grid[k] || (C.grid[k] = [])).push(a);
      }
    }
  }
}

function inPoly(nx: number, ny: number, p: Pt[]): boolean {
  let ins = false;
  for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
    const pi = p[i], pj = p[j];
    if (!pi || !pj) continue;
    const xi = pi[0], yi = pi[1], xj = pj[0], yj = pj[1];
    if ((yi > ny) !== (yj > ny) && nx < (xj - xi) * (ny - yi) / (yj - yi) + xi) ins = !ins;
  }
  return ins;
}

// distance in screen px from (px, py) to the segment (ax, ay)-(bx, by)
export function segDist(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax, dy = by - ay;
  const l = dx * dx + dy * dy;
  let t = l ? ((px - ax) * dx + (py - ay) * dy) / l : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  const ex = ax + t * dx - px, ey = ay + t * dy - py;
  return Math.sqrt(ex * ex + ey * ey);
}

function hits(a: ShapeAnn, nx: number, ny: number, px: number, py: number, tol: number): boolean {
  const b = bounds(a);
  if (!b) return false;
  const tx = tol / T.ax, ty = tol / T.ay;
  if (nx < b.x0 - tx || nx > b.x1 + tx || ny < b.y0 - ty || ny > b.y1 + ty) return false;
  if (a.geometry_type === 'bbox' || a.geometry_type === 'keypoints') return true;
  const p = a.geometry.points;
  if (inPoly(nx, ny, p)) return true;
  for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
    const pi = p[i], pj = p[j];
    if (pi && pj && segDist(px, py, sx(pj[0]), sy(pj[1]), sx(pi[0]), sy(pi[1])) <= tol) return true;
  }
  return false;
}

// every active-task shape under the screen point, smallest first
export function hitAll(px: number, py: number, tol = HIT_TOL): ShapeAnn[] {
  if (!C.grid || !C.iw) return [];
  const nx = (px - T.tx) / T.ax, ny = (py - T.ty) / T.ay;
  const tx = tol / T.ax, ty = tol / T.ay;
  const seen = new Set<ShapeAnn>();
  const out: ShapeAnn[] = [];
  const test = (a: ShapeAnn): void => {
    if (seen.has(a)) return;
    seen.add(a);
    if (!L.editing.has(a.id) && hits(a, nx, ny, px, py, tol)) out.push(a);
  };
  const cx0 = Math.max(0, Math.floor((nx - tx) * G)), cx1 = Math.min(G - 1, Math.floor((nx + tx) * G));
  const cy0 = Math.max(0, Math.floor((ny - ty) * G)), cy1 = Math.min(G - 1, Math.floor((ny + ty) * G));
  for (let cy = cy0; cy <= cy1; cy++) {
    for (let cx = cx0; cx <= cx1; cx++) { const cell = C.grid[cy * G + cx]; if (cell) for (const a of cell) test(a); }
  }
  for (const a of C.big) test(a);
  const area = (a: ShapeAnn): number => { const b = bounds(a); return b ? b.area : 0; };
  out.sort((a, b) => area(a) - area(b));
  return out;
}

export function hitTest(px: number, py: number, tol = HIT_TOL): string | null {
  const h = hitAll(px, py, tol)[0];
  return h ? h.id : null;
}

// ---------------------------------------------------------------- pointer
function pt(e: MouseEvent): PointerPos {
  const r = C.rect || (C.rect = wrapEl().getBoundingClientRect());
  const px = e.clientX - r.left, py = e.clientY - r.top;
  return { sx: px, sy: py, nx: (px - T.tx) / T.ax, ny: (py - T.ty) / T.ay };
}
export const pointer = (): { sx: number; sy: number; in: boolean } => C.ptr;

function setCursor(c: string): void { if (c !== C.cursor) { C.cursor = c; wrapEl().style.cursor = c; } }

function onDown(e: PointerEvent): void {
  if (C.pan) return;
  const wrap = wrapEl();
  wrap.focus({ preventScroll: true });
  const p = pt(e);
  const panGesture = e.button === 1 || e.button === 2 || (e.button === 0 && keys.space);
  try { wrap.setPointerCapture(e.pointerId); } catch (err) { /* synthetic */ }
  if (panGesture) {
    e.preventDefault();
    C.pan = { x: e.clientX, y: e.clientY, tx: L.view.tx, ty: L.view.ty };
    setCursor('grabbing');
    invalidate(D.UI);
    return;
  }
  if (e.button !== 0 || L.loading || !C.iw) return;
  // the tool is fixed for the whole press: holding the area-select key at press time routes it to the area tool
  const tool = keys.area && H.areaTool ? H.areaTool() : H.tool();
  C.down = { id: e.pointerId, tool };
  if (tool && tool.onDown) tool.onDown(p, e);
}

function onMove(e: PointerEvent): void {
  const p = pt(e);
  C.ptr.sx = p.sx; C.ptr.sy = p.sy; C.ptr.in = true;
  if (C.pan) {
    // redraw image + shapes at the new offset (one rAF per frame); tags are relaid on release
    L.view.tx = C.pan.tx + e.clientX - C.pan.x; L.view.ty = C.pan.ty + e.clientY - C.pan.y; L.view.fitted = false;
    syncT();
    invalidate(D.IMG | D.SHAPES | D.UI);
    H.pointer(p);
    return;
  }
  L.cursor = { nx: p.nx, ny: p.ny };
  const tool = C.down ? C.down.tool : H.tool();
  if (C.down && tool && tool.onMove && !L.loading) {
    if (tool.coalesced && e.getCoalescedEvents) {
      const evs = e.getCoalescedEvents();
      for (const ev of evs.length ? evs : [e]) tool.onMove(pt(ev), e);
    } else tool.onMove(p, e);
  } else if (!C.down && !L.loading) {
    const id = tool && tool.busy && tool.busy() ? null : hitTest(p.sx, p.sy);
    if (id !== L.hoverId) { L.hoverId = id; H.hover(); }
  }
  if (tool && tool.cursor) setCursor(keys.space ? 'grab' : keys.area && !C.down ? 'crosshair' : tool.cursor(p));
  H.pointer(p);
  invalidate(D.UI);
}

function onUp(e: PointerEvent): void {
  try { wrapEl().releasePointerCapture(e.pointerId); } catch (err) { /* not captured */ }
  if (C.pan) {
    C.pan = null;
    setCursor('');
    viewChanged();
    return;
  }
  if (!C.down) return;
  const tool = C.down.tool;
  C.down = null;
  if (tool && tool.onUp && !L.loading) tool.onUp(pt(e), e);
  invalidate(D.UI);
}

// Wheel: a mouse wheel zooms at the pointer, a trackpad two-finger scroll pans, a trackpad pinch (ctrlKey, also
// Safari's synthesized one) zooms. Trackpads send pixel deltas (deltaMode 0) that are fractional, small or carry
// deltaX; once seen, the trackpad flag sticks until a line-mode event or a large integer notch (a real wheel).
// Deltas are summed and applied once per frame.
let padSticky = false;
function wheelKind(e: WheelEvent): 'pinch' | 'zoom' | 'pan' {
  if (e.ctrlKey) return 'pinch';
  if (e.deltaMode !== 0) { padSticky = false; return 'zoom'; }
  const dx = e.deltaX, dy = e.deltaY;
  const ix = dx % 1 === 0, iy = dy % 1 === 0;
  if (ix && iy && (Math.abs(dy) >= 100 || Math.abs(dx) >= 100)) { padSticky = false; return 'zoom'; }
  if (dx !== 0 || !iy || Math.abs(dy) < 40) { padSticky = true; return 'pan'; }
  return padSticky ? 'pan' : 'zoom';
}

const W = { zoom: 0, pinch: 0, px: 0, py: 0, sx: 0, sy: 0, raf: 0 };
function wheelFrame(): void {
  W.raf = 0;
  if (W.px || W.py) {
    const v = L.view;
    v.tx -= W.px; v.ty -= W.py; v.fitted = false;
    W.px = W.py = 0;
    syncT();
    invalidate(D.IMG | D.SHAPES | D.UI);
    tagsSoon();
  }
  if (W.zoom || W.pinch) {
    // 1.15 per 100 px notch; trackpad pinches are finer, so they zoom faster per px
    const f = Math.pow(1.15, -W.zoom / 100 - W.pinch / 25);
    W.zoom = W.pinch = 0;
    zoomAt(W.sx, W.sy, f);
  }
}

// a wheel pan relays the tags (and tells the page about the view) once the scrolling pauses
let tagsTimer: ReturnType<typeof setTimeout> | undefined;
function tagsSoon(): void {
  clearTimeout(tagsTimer);
  tagsTimer = setTimeout(() => { if (!C.pan) viewChanged(); }, 120);
}

function onWheel(e: WheelEvent): void {
  e.preventDefault();
  if (C.pan || !C.iw) return;
  const k = wheelKind(e);
  if (k === 'pan') {
    W.px += e.deltaX; W.py += e.deltaY;
  } else {
    const r = C.rect || (C.rect = wrapEl().getBoundingClientRect());
    W.sx = e.clientX - r.left; W.sy = e.clientY - r.top;
    const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
    if (k === 'pinch') W.pinch += dy; else W.zoom += dy;
  }
  if (!W.raf) W.raf = requestAnimationFrame(wheelFrame);
}

export const isDown = (): boolean => !!C.down;
export const canvasEl = (): HTMLElement | null => C.wrap;
