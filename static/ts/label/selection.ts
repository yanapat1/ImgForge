// Multi-shape actions on L.selected: the area-select tool (hold `area_select` + left-drag), copy to an in-page
// clipboard, paste onto the image on screen (new ids, manual, any image). Delete / duplicate of a selection are the
// select.js actions (one undo step each). app.js wires these: hotkeys copy / paste, canvas handler areaTool.
//
// Area select: a dashed rectangle on the interaction layer; on release every active-task shape FULLY inside it is
// selected (Shift: also the ones it touches; Ctrl / Cmd: added to the selection). The test runs on the cached
// normalised extents (canvas.js bounds(), a WeakMap per immutable Ann): a linear pass, ~0.1 ms for 500 shapes;
// "touching" polygons / keypoints get an exact check after the extent test.

import type { Ann, Change, KpPoint, Pt, ShapeAnn, Tool } from '../types-label.js';
import { bounds, colors, D, invalidate, TASKS, CLS } from './canvas.js';
import { edit } from './history.js';
import { L } from './state.js';
import type { Rect4 } from './tags.js';
import { annsOf, clamp01, newId, setSelection, toast } from './tools/select.js';

// ---------------------------------------------------------------- area select
interface AreaDrag { sx: number; sy: number; ex: number; ey: number; nx0: number; ny0: number; nx1: number; ny1: number }
let drag: AreaDrag | null = null;               // screen px of the press and the pointer + the normalised corners

const MIN_DRAG = 3;            // px: a shorter drag is a click (clears the selection unless Ctrl)

function segHitsRect(ax: number, ay: number, bx: number, by: number, r: Rect4): boolean {
  // Liang–Barsky: does the segment a-b cross the rectangle r?
  let t0 = 0, t1 = 1;
  const dx = bx - ax, dy = by - ay;
  const p = [-dx, dx, -dy, dy], q = [ax - r.x0, r.x1 - ax, ay - r.y0, r.y1 - ay];
  for (let i = 0; i < 4; i++) {
    const pi = p[i] ?? 0, qi = q[i] ?? 0;
    if (pi === 0) { if (qi < 0) return false; continue; }
    const t = qi / pi;
    if (pi < 0) { if (t > t1) return false; if (t > t0) t0 = t; } else { if (t < t0) return false; if (t < t1) t1 = t; }
  }
  return true;
}

function inPoly(x: number, y: number, pts: Pt[]): boolean {
  let ins = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const pi = pts[i], pj = pts[j];
    if (!pi || !pj) continue;
    const xi = pi[0], yi = pi[1], xj = pj[0], yj = pj[1];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) ins = !ins;
  }
  return ins;
}

// the shape really touches the rectangle (its extent already overlaps it)
function touches(a: ShapeAnn, r: Rect4): boolean {
  if (a.geometry_type === 'keypoints') return a.geometry.points.some((q) => q[2] && q[0] >= r.x0 && q[0] <= r.x1 && q[1] >= r.y0 && q[1] <= r.y1);
  if (a.geometry_type !== 'polygon') return true;
  const p = a.geometry.points;
  for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
    const pi = p[i], pj = p[j];
    if (pi && pj && segHitsRect(pj[0], pj[1], pi[0], pi[1], r)) return true;
  }
  return inPoly((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, p);          // the rectangle lies inside the polygon
}

// ids of the active task's visible shapes in the normalised rectangle r
export function shapesInRect(r: Rect4, touching: boolean): string[] {
  const out: string[] = [];
  for (const a of L.anns.values()) {
    if (a.task_id !== L.taskId || a.geometry_type === 'tag' || L.hidden.has(a.id)) continue;
    const b = bounds(a);
    if (!b) continue;
    if (touching) {
      if (b.x1 < r.x0 || b.x0 > r.x1 || b.y1 < r.y0 || b.y0 > r.y1) continue;
      if (touches(a, r)) out.push(a.id);
    } else if (b.x0 >= r.x0 && b.x1 <= r.x1 && b.y0 >= r.y0 && b.y1 <= r.y1) out.push(a.id);
  }
  return out;
}

export const areaTool = {
  name: 'Area select',
  onDown(p) {
    drag = { sx: p.sx, sy: p.sy, ex: p.sx, ey: p.sy, nx0: p.nx, ny0: p.ny, nx1: p.nx, ny1: p.ny };
    if (L.hoverId) { L.hoverId = null; invalidate(D.SHAPES); }
  },
  onMove(p) {
    if (!drag) return;
    drag.ex = p.sx; drag.ey = p.sy; drag.nx1 = p.nx; drag.ny1 = p.ny;
  },
  onUp(p, e) {
    if (!drag) return;
    const d = drag;
    drag = null;
    d.nx1 = p.nx; d.ny1 = p.ny;
    const add = !!(e && (e.ctrlKey || e.metaKey));
    if (Math.abs(p.sx - d.sx) + Math.abs(p.sy - d.sy) < MIN_DRAG) {
      if (!add && L.selected.size) setSelection([]);
      invalidate(D.UI);
      return;
    }
    const r = { x0: clamp01(Math.min(d.nx0, d.nx1)), x1: clamp01(Math.max(d.nx0, d.nx1)),
      y0: clamp01(Math.min(d.ny0, d.ny1)), y1: clamp01(Math.max(d.ny0, d.ny1)) };
    const ids = shapesInRect(r, !!(e && e.shiftKey));
    if (add) setSelection(new Set([...L.selected, ...ids]));
    else setSelection(ids);
    invalidate(D.UI);
  },
  drawPreview(x) {
    if (!drag) return;
    const c = colors();
    const x0 = Math.round(Math.min(drag.sx, drag.ex)) + 0.5, y0 = Math.round(Math.min(drag.sy, drag.ey)) + 0.5;
    const w = Math.round(Math.abs(drag.ex - drag.sx)), h = Math.round(Math.abs(drag.ey - drag.sy));
    x.save();
    x.fillStyle = c.preview || 'rgba(128,128,128,.12)';
    x.globalAlpha = 0.18;
    x.fillRect(x0, y0, w, h);
    x.globalAlpha = 1;
    x.lineWidth = 1;
    x.setLineDash([]);
    x.strokeStyle = c.halo || '#000';
    x.strokeRect(x0, y0, w, h);
    x.setLineDash([5, 4]);
    x.strokeStyle = c.sel || '#fff';
    x.strokeRect(x0, y0, w, h);
    x.restore();
  },
  cursor: () => 'crosshair',
  busy: () => !!drag,
  cancel() { if (!drag) return false; drag = null; return true; },
} satisfies Tool;

// ---------------------------------------------------------------- clipboard (in-page, survives image switches)
interface Clip { items: Ann[]; mid: string | undefined; pastes: number }
let clip: Clip | null = null;               // {items: [Ann-like], mid, pastes}

export const clipboardSize = (): number => (clip ? clip.items.length : 0);

export function copySelection(): boolean {
  const sel = annsOf(L.selected, (a) => a.geometry_type !== 'tag');
  if (!sel.length) return false;
  const ids = new Set(sel.map((a) => a.id));
  clip = {
    mid: L.media ? L.media.id : undefined,
    pastes: 0,
    items: sel.map((a): Ann => {
      // the model score / source / version do not travel: a paste is a new manual shape
      const item: Ann = { ...structuredClone(a), attributes: structuredClone(a.attributes || {}),
        parent_id: a.parent_id && ids.has(a.parent_id) ? a.parent_id : null, z_order: a.z_order || 0 };
      delete item.source; delete item.score; delete item.version;
      return item;
    }),
  };
  toast(`Copied ${sel.length} shape${sel.length === 1 ? '' : 's'}`);
  return true;
}

function shift(a: Ann, d: number): Ann {
  if (!d) return a;
  const mv = (p: Pt): Pt => [clamp01(p[0] + d), clamp01(p[1] + d)];
  if (a.geometry_type === 'bbox') {
    const g = a.geometry;
    return { ...a, geometry: { ...g, x: Math.min(g.x + d, 1 - g.w), y: Math.min(g.y + d, 1 - g.h) } };
  }
  if (a.geometry_type === 'polygon') {
    const g = a.geometry;
    return { ...a, geometry: { ...g, points: g.points.map(mv), ...(g.holes ? { holes: g.holes.map((h) => h.map(mv)) } : {}) } };
  }
  if (a.geometry_type === 'keypoints') {
    return { ...a, geometry: { ...a.geometry, points: a.geometry.points.map((p): KpPoint => (p[2] ? [clamp01(p[0] + d), clamp01(p[1] + d), p[2]] : p)) } };
  }
  return a;
}

export function pasteClipboard(): boolean {
  if (!clip || !clip.items.length || !L.media || !L.history) return false;
  const ok = clip.items.filter((a) => TASKS.has(a.task_id) && CLS.get(a.class_id)?.task_id === a.task_id);
  const skipped = clip.items.length - ok.length;
  if (!ok.length) { toast('Nothing to paste: the copied shapes’ tasks or classes no longer exist', 'err'); return true; }
  // onto the image they came from: offset each paste a little, so the copies don't hide the originals
  const same = L.media.id === clip.mid;
  const d = same ? 0.01 * ++clip.pastes : 0;
  const ids = new Map(ok.map((a) => [a.id, newId()]));
  const added = ok.map((a): Ann => ({ ...shift(structuredClone(a), d), id: ids.get(a.id) ?? newId(), attributes: structuredClone(a.attributes),
    parent_id: a.parent_id && ids.has(a.parent_id) ? ids.get(a.parent_id) ?? null : null, z_order: a.z_order,
    source: 'manual', score: null }));
  edit(added.map((a): Change => [a.id, null, a]));
  setSelection(added.map((a) => a.id));
  toast(`Pasted ${added.length} shape${added.length === 1 ? '' : 's'}${skipped ? ` (${skipped} skipped: task or class gone)` : ''}`);
  return true;
}
