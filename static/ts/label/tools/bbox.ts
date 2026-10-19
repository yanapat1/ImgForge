// Box tool (object detection): drag to draw (or two clicks when settings.bbox_mode = 'two_click'), Shift = square,
// 8 resize handles on the selected box, drag the body to move (Alt = duplicate), arrows nudge 1 px (Shift 10).
// Boxes under 3 screen px are ignored. Geometry {x, y, w, h}, normalised, clamped to the image.

import type { BBoxAnn, BBoxGeom, PointerPos, Tool } from '../../types-label.js';
import { clsOf, colors, drawHandle, imageSize, invalidate, D, T } from '../canvas.js';
import { edit } from '../history.js';
import { L } from '../state.js';
import { clamp01, endMove, makeAnn, moveTo, moving, pressShape, cancelMove, clearSelection, setSelection, soleSelected, toast } from './select.js';

interface NPoint { nx: number; ny: number }
interface Start extends NPoint { sx: number; sy: number }
type BoxGesture =
  | { kind: 'create' | 'create2'; start: Start; cur: PointerPos; shift?: boolean }
  | { kind: 'resize'; a: BBoxAnn; h: number };

const H8: Array<[number, number]> = [[0, 0], [0.5, 0], [1, 0], [1, 0.5], [1, 1], [0.5, 1], [0, 1], [0, 0.5]];
const CURSORS = ['nwse-resize', 'ns-resize', 'nesw-resize', 'ew-resize', 'nwse-resize', 'ns-resize', 'nesw-resize', 'ew-resize'];
const MIN_PX = 3;
let g: BoxGesture | null = null;

const live = (a: BBoxAnn | null): BBoxAnn | null => {
  const e = a && L.editing.get(a.id);
  return e && e.geometry_type === 'bbox' ? e : a;
};

function handleAt(p: PointerPos, a: BBoxAnn | null): number {
  if (!a) return -1;
  const b = a.geometry;
  for (let i = 0; i < 8; i++) {
    const f = H8[i];
    if (!f) continue;
    const hx = (b.x + b.w * f[0]) * T.ax + T.tx, hy = (b.y + b.h * f[1]) * T.ay + T.ty;
    if (Math.abs(p.sx - hx) <= 7 && Math.abs(p.sy - hy) <= 7) return i;
  }
  return -1;
}

// rect from two normalised corners; square (in image px) with shift
function rectOf(a: NPoint, b: NPoint, square?: boolean): BBoxGeom {
  let dx = b.nx - a.nx, dy = b.ny - a.ny;
  if (square) {
    const { w, h } = imageSize();
    const s = Math.max(Math.abs(dx * w), Math.abs(dy * h));
    dx = Math.sign(dx || 1) * s / w; dy = Math.sign(dy || 1) * s / h;
  }
  const x0 = clamp01(Math.min(a.nx, a.nx + dx)), x1 = clamp01(Math.max(a.nx, a.nx + dx));
  const y0 = clamp01(Math.min(a.ny, a.ny + dy)), y1 = clamp01(Math.max(a.ny, a.ny + dy));
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

const bigEnough = (r: BBoxGeom): boolean => r.w * T.ax >= MIN_PX && r.h * T.ay >= MIN_PX;

function finishCreate(r: BBoxGeom): void {
  g = null;
  invalidate(D.UI);
  if (!bigEnough(r)) return;
  if (!L.classId) { toast('Add a class to this task first', 'warn'); return; }
  const a = makeAnn('bbox', r);
  edit([[a.id, null, a]]);
  setSelection([a.id]);
}

export const bboxTool = {
  name: 'Box',
  busy: () => !!g || moving(),
  onDown(p, e) {
    if (g && g.kind === 'create2') { finishCreate(rectOf(g.start, p, e.shiftKey)); return; }
    const sel = soleSelected('bbox');
    const h = handleAt(p, sel);
    if (sel && h >= 0) { g = { kind: 'resize', a: sel, h }; return; }
    if (!e.ctrlKey && !e.metaKey && pressShape(p, e)) return;      // Ctrl+press: draw over existing shapes
    if (!e.shiftKey) clearSelection();
    g = { kind: 'create', start: { nx: clamp01(p.nx), ny: clamp01(p.ny), sx: p.sx, sy: p.sy }, cur: p };
  },
  onMove(p, e) {
    if (moving()) { moveTo(p); return; }
    if (!g) return;
    if (g.kind === 'resize') {
      const o = g.a.geometry, f = H8[g.h];
      if (!f) return;
      let x0 = o.x, y0 = o.y, x1 = o.x + o.w, y1 = o.y + o.h;
      if (f[0] === 0) x0 = clamp01(p.nx); else if (f[0] === 1) x1 = clamp01(p.nx);
      if (f[1] === 0) y0 = clamp01(p.ny); else if (f[1] === 1) y1 = clamp01(p.ny);
      if (e.shiftKey && f[0] !== 0.5 && f[1] !== 0.5) {        // corner + shift: keep the aspect ratio
        const r = o.w / (o.h || 1);
        const w = Math.abs(x1 - x0), hgt = Math.abs(y1 - y0);
        if (w / (hgt || 1e-9) > r) { const nh = w / r; if (f[1] === 0) y0 = y1 - nh; else y1 = y0 + nh; } else { const nw = hgt * r; if (f[0] === 0) x0 = x1 - nw; else x1 = x0 + nw; }
      }
      const r = { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
      if (!L.editing.has(g.a.id)) invalidate(D.SHAPES | D.TAGS);
      L.editing.set(g.a.id, { ...g.a, geometry: r });
      return;
    }
    g.cur = p;
    g.shift = e.shiftKey;
  },
  onUp(p, e) {
    if (moving()) { endMove(); return; }
    if (!g) return;
    if (g.kind === 'resize') {
      const after = L.editing.get(g.a.id);
      L.editing.delete(g.a.id);
      const before = g.a;
      g = null;
      if (after && after.geometry_type === 'bbox' && bigEnough(after.geometry)) edit([[before.id, before, after]]);
      else invalidate(D.SHAPES | D.TAGS);
      return;
    }
    if (g.kind === 'create') {
      const moved = Math.abs(p.sx - g.start.sx) + Math.abs(p.sy - g.start.sy) >= MIN_PX;
      if (!moved && L.settings.bbox_mode === 'two_click') { g = { kind: 'create2', start: g.start, cur: p }; return; }
      finishCreate(rectOf(g.start, p, e.shiftKey));
    }
  },
  onKey(combo) {
    if (combo === 'escape' && (g || moving())) {
      if (g && g.kind === 'resize') L.editing.delete(g.a.id);
      g = null; cancelMove();
      invalidate(D.SHAPES | D.TAGS | D.UI);
      return true;
    }
    return false;
  },
  cancel() { const had = !!g; g = null; return had; },
  drawPreview(x) {
    if (g && (g.kind === 'create' || g.kind === 'create2')) {
      const r = rectOf(g.start, g.cur, g.shift);
      const c = L.classId ? clsOf({ class_id: L.classId }) : { color: colors().preview };
      x.beginPath();
      x.rect(r.x * T.ax + T.tx, r.y * T.ay + T.ty, r.w * T.ax, r.h * T.ay);
      x.strokeStyle = colors().halo; x.lineWidth = 4; x.stroke();
      x.strokeStyle = c.color; x.lineWidth = 2; x.stroke();
    }
    const sel = live(soleSelected('bbox'));
    if (sel && !moving()) {
      const b = sel.geometry;
      for (const f of H8) drawHandle(x, (b.x + b.w * f[0]) * T.ax + T.tx, (b.y + b.h * f[1]) * T.ay + T.ty);
    }
  },
  cursor(p) {
    if (g) return g.kind === 'resize' ? CURSORS[g.h] ?? 'crosshair' : 'crosshair';
    if (moving()) return 'move';
    const h = handleAt(p, soleSelected('bbox'));
    if (h >= 0) return CURSORS[h] ?? 'crosshair';
    return L.hoverId ? 'move' : 'crosshair';
  },
} satisfies Tool;
