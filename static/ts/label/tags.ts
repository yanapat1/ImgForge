// Smart name tags. layoutTags() runs only when shapes, the view, the selection or the canvas size change (never
// on mousemove). Each shape tries 6 anchors around its screen bbox (above-left out, below-left out, inside
// top-left, inside bottom-left, above-right out, inside top-right); the first one inside the visible rect (the canvas
// minus the UI overlapping it) that hits no placed tag / overlay wins, else the least-overlapping clamped candidate.
// Selected shapes are placed first. Placed tags live in a 64 px grid, so a layout is ~O(n).

import { rgba, textOn } from '../core/dom.js';
import type { TagRect } from '../types-label.js';
import { L } from './state.js';

/** A shape's screen extent + the text of its tag (layoutTags input). */
export interface TagItem { id: string; x0: number; y0: number; x1: number; y1: number; text: string; color: string; prio?: number }
export interface Rect4 { x0: number; y0: number; x1: number; y1: number }
export interface Obstacle { x: number; y: number; w: number; h: number }

// the font size comes from settings.tag_size (8–16 px); the tag height and padding follow it. syncTagSize() is
// called before every layout: a size change re-fonts and clears the width cache, so the edge placement (which uses
// the measured width and tagH()) stays exact at any size.
const CELL = 64;
let measureCtx: CanvasRenderingContext2D | null = null, font = '', family = 'sans-serif', fontPx = 0, H = 14, PAD = 4;
const widths = new Map<string, number>();          // text -> px (cleared when the font changes)

export const tagH = (): number => H;

function refont(px: number): CanvasRenderingContext2D {
  fontPx = px;
  H = Math.round(px * 1.4);
  PAD = Math.max(3, Math.round(px * 0.42));
  font = `600 ${px}px ${family}`;
  widths.clear();
  if (!measureCtx) {
    measureCtx = document.createElement('canvas').getContext('2d');
    if (!measureCtx) throw new Error('2d canvas is not available');
  }
  measureCtx.font = font;
  return measureCtx;
}

const sizeSetting = (): number => { const v = +L.settings.tag_size; return v >= 6 && v <= 32 ? v : 10; };

export function setTagFont(fam?: string): void {
  family = fam || 'sans-serif';
  refont(sizeSetting());
}

export function syncTagSize(): void { const px = sizeSetting(); if (px !== fontPx || !measureCtx) refont(px); }

// a model label's tag: robot glyph, class name, score with 2 decimals (the width cache keys on this full text)
export const modelTagText = (name: string, score: number | null | undefined): string => `\u{1F916} ${name}${score != null && isFinite(score) ? ` ${(+score).toFixed(2)}` : ''}`;

function textW(t: string): number {
  let w = widths.get(t);
  if (w === undefined) {
    const ctx = measureCtx || refont(sizeSetting());
    w = Math.ceil(ctx.measureText(t).width) + 2 * PAD;
    if (widths.size > 4000) widths.clear();
    widths.set(t, w);
  }
  return w;
}

// grid of placed rects
let grid = new Map<number, Obstacle[]>();
const cellKey = (cx: number, cy: number): number => cx * 4096 + cy;
function gridAdd(r: Obstacle): void {
  for (let cx = Math.floor(r.x / CELL), cx1 = Math.floor((r.x + r.w) / CELL); cx <= cx1; cx++) {
    for (let cy = Math.floor(r.y / CELL), cy1 = Math.floor((r.y + r.h) / CELL); cy <= cy1; cy++) {
      const k = cellKey(cx, cy);
      const a = grid.get(k);
      if (a) a.push(r); else grid.set(k, [r]);
    }
  }
}
// overlap area of (x, y, w, h) with everything placed (0 = free); stops early when only "free?" is asked
function overlap(x: number, y: number, w: number, h: number, any: boolean): number {
  let s = 0;
  for (let cx = Math.floor(x / CELL), cx1 = Math.floor((x + w) / CELL); cx <= cx1; cx++) {
    for (let cy = Math.floor(y / CELL), cy1 = Math.floor((y + h) / CELL); cy <= cy1; cy++) {
      const a = grid.get(cellKey(cx, cy));
      if (!a) continue;
      for (const r of a) {
        const ox = Math.min(x + w, r.x + r.w) - Math.max(x, r.x);
        if (ox <= 0) continue;
        const oy = Math.min(y + h, r.y + r.h) - Math.max(y, r.y);
        if (oy <= 0) continue;
        if (any) return 1;
        s += ox * oy;          // a rect spanning 2 cells may count twice: fine for ranking
      }
    }
  }
  return s;
}

const clamp = (v: number, a: number, b: number): number => (v < a ? a : v > b ? b : v);

// items: [{id, x0, y0, x1, y1 (screen css px), text, color}] in priority order (selected first)
// V: visible rect {x0, y0, x1, y1}; obstacles: [{x, y, w, h}] (overlapping UI that does not span a whole side)
// sw: stroke width (outside tags sit flush with the outer edge of the stroke)
export function layoutTags(items: TagItem[], V: Rect4, obstacles: Obstacle[], sw: number): TagRect[] {
  grid = new Map();
  for (const o of obstacles) gridAdd(o);
  const out: TagRect[] = [];
  syncTagSize();
  const h = H, half = sw / 2;
  const vw = V.x1 - V.x0, vh = V.y1 - V.y0;
  const cand = new Float64Array(12);
  for (const it of items) {
    const w = textW(it.text);
    if (w > vw || h > vh) continue;
    const L0 = it.x0 - half, R0 = it.x1 + half, T0 = it.y0 - half, B0 = it.y1 + half;
    cand[0] = L0; cand[1] = T0 - h;              // above-left, outside
    cand[2] = L0; cand[3] = B0;                  // below-left, outside
    cand[4] = L0 + sw; cand[5] = T0 + sw;        // inside top-left
    cand[6] = L0 + sw; cand[7] = B0 - sw - h;    // inside bottom-left
    cand[8] = R0 - w; cand[9] = T0 - h;          // above-right, outside
    cand[10] = R0 - sw - w; cand[11] = T0 + sw;  // inside top-right
    let px = NaN, py = NaN;
    for (let i = 0; i < 12; i += 2) {
      const x = cand[i] ?? 0, y = cand[i + 1] ?? 0;
      if (x < V.x0 || y < V.y0 || x + w > V.x1 || y + h > V.y1) continue;
      if (!overlap(x, y, w, h, true)) { px = x; py = y; break; }
    }
    if (px !== px) {                             // nothing free: the least-overlapping clamped candidate
      let best = Infinity;
      for (let i = 0; i < 12; i += 2) {
        const x = clamp(cand[i] ?? 0, V.x0, V.x1 - w), y = clamp(cand[i + 1] ?? 0, V.y0, V.y1 - h);
        const s = overlap(x, y, w, h, false);
        if (s < best) { best = s; px = x; py = y; if (!s) break; }
      }
    }
    const r = { x: Math.round(px), y: Math.round(py), w, h, id: it.id, text: it.text, color: it.color, fg: textOn(it.color) };
    gridAdd(r);
    out.push(r);
  }
  return out;
}

// bgAlpha: settings.tag_opacity (the background only; the text stays opaque); alphaOf(t): extra alpha per tag
export function drawTags(ctx: CanvasRenderingContext2D, tags: TagRect[], colDark: string, colLight: string,
  alphaOf: ((t: TagRect) => number) | null, bgAlpha = 1): void {
  if (!tags.length) return;
  ctx.font = font;
  ctx.textBaseline = 'middle';
  const bg = bgAlpha >= 1 ? null : new Map<string, string>();     // colour -> rgba string
  for (const t of tags) {
    ctx.globalAlpha = alphaOf ? alphaOf(t) : 1;
    let f = t.color;
    if (bg) { f = bg.get(t.color) ?? ''; if (!f) { f = rgba(t.color, bgAlpha); bg.set(t.color, f); } }
    ctx.fillStyle = f;
    ctx.fillRect(t.x, t.y, t.w, t.h);
    // a see-through background shows the image: the text colour follows the class colour only when mostly opaque
    const tx = t.x + PAD, ty = t.y + t.h / 2 + 0.5;
    if (bgAlpha < 0.5) {
      ctx.strokeStyle = colDark; ctx.lineWidth = 3; ctx.lineJoin = 'round';
      ctx.strokeText(t.text, tx, ty);
      ctx.fillStyle = colLight;
    } else ctx.fillStyle = t.fg === 'dark' ? colDark : colLight;
    ctx.fillText(t.text, tx, ty);
  }
  ctx.globalAlpha = 1;
}
