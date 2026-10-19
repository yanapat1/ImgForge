// Keypoints tool. Point names come from task.config.keypoints; each click places the next unplaced point of the
// instance being filled (the status bar shows "next: <name>"). A new instance links to the selected box
// (parent_id) when one is selected. Drag a point of the selected instance to move it, Alt+click removes it,
// V cycles visible / occluded on the hovered (or last placed) point, Backspace un-places the last one,
// Esc / Enter stop filling. Geometry {points: [[x, y, v], …]}, v: 0 not placed, 1 occluded, 2 visible.

import type { KeypointsAnn, KpPoint, PointerPos, Tool } from '../../types-label.js';
import { colors, drawHandle, invalidate, D, T, TASKS } from '../canvas.js';
import { edit } from '../history.js';
import { L } from '../state.js';
import { cancelMove, clamp01, clearSelection, endMove, makeAnn, moveTo, moving, pressShape, setSelection, soleSelected, toast } from './select.js';

const R = 7;
let placing: string | null = null;     // id of the instance being filled
let drag: { a: KeypointsAnn; i: number } | null = null;
let cur: PointerPos | null = null;

export function kpNames(taskId: string | null = L.taskId): string[] {
  const t = taskId ? TASKS.get(taskId) : undefined;
  const n = t && t.config && t.config.keypoints;
  return n && n.length ? n : ['point'];
}

const live = (a: KeypointsAnn | null): KeypointsAnn | null => {
  const e = a && L.editing.get(a.id);
  return e && e.geometry_type === 'keypoints' ? e : a;
};
const kpAnn = (id: string | null): KeypointsAnn | null => {
  const a = id ? L.anns.get(id) : undefined;
  return a && a.geometry_type === 'keypoints' ? a : null;
};
const target = (): KeypointsAnn | null => (placing ? kpAnn(placing) : soleSelected('keypoints'));
const nextIdx = (a: KeypointsAnn | null): number => (a ? a.geometry.points.findIndex((q) => !q[2]) : -1);

function pointAt(p: PointerPos, a: KeypointsAnn | null): number {
  if (!a) return -1;
  const pts = a.geometry.points;
  for (let i = 0; i < pts.length; i++) {
    const q = pts[i];
    if (!q || !q[2]) continue;
    if (Math.abs(p.sx - (q[0] * T.ax + T.tx)) <= R && Math.abs(p.sy - (q[1] * T.ay + T.ty)) <= R) return i;
  }
  return -1;
}

function setPoint(a: KeypointsAnn, i: number, q: KpPoint): KeypointsAnn {
  const points = a.geometry.points.slice();
  points[i] = q;
  const b: KeypointsAnn = { ...a, geometry: { ...a.geometry, points } };
  edit([[a.id, a, b]]);
  return b;
}

export const keypointsTool = {
  name: 'Keypoints',
  busy: () => !!drag || moving(),
  onDown(p, e) {
    const sel = soleSelected('keypoints');
    const i = pointAt(p, sel);
    if (sel && i >= 0) {
      if (e.altKey) { setPoint(sel, i, [0, 0, 0]); return; }
      drag = { a: sel, i };
      return;
    }
    const pl = kpAnn(placing);
    if (pl) {
      const k = nextIdx(pl);
      if (k >= 0) {
        const b = setPoint(pl, k, [clamp01(p.nx), clamp01(p.ny), 2]);
        if (nextIdx(b) < 0) placing = null;
        return;
      }
      placing = null;
    }
    if (!e.ctrlKey && !e.metaKey && pressShape(p, e)) return;
    if (!L.classId) { toast('Add a class to this task first', 'warn'); return; }
    // a selected box (of any task) becomes the parent of the new instance
    let parent: string | null = null;
    for (const id of L.selected) { const s = L.anns.get(id); if (s && s.geometry_type === 'bbox') { parent = id; break; } }
    if (!e.shiftKey) clearSelection();
    const names = kpNames();
    const points = names.map((): KpPoint => [0, 0, 0]);
    points[0] = [clamp01(p.nx), clamp01(p.ny), 2];
    const a = makeAnn('keypoints', { points }, { parent_id: parent });
    edit([[a.id, null, a]]);
    setSelection([a.id]);
    placing = names.length > 1 ? a.id : null;
  },
  onMove(p) {
    cur = p;
    if (moving()) { moveTo(p); return; }
    if (!drag) return;
    const points = drag.a.geometry.points.slice();
    points[drag.i] = [clamp01(p.nx), clamp01(p.ny), drag.a.geometry.points[drag.i]?.[2] || 2];
    if (!L.editing.has(drag.a.id)) invalidate(D.SHAPES | D.TAGS);
    L.editing.set(drag.a.id, { ...drag.a, geometry: { ...drag.a.geometry, points } });
  },
  onUp() {
    if (moving()) { endMove(); return; }
    if (!drag) return;
    const after = L.editing.get(drag.a.id);
    L.editing.delete(drag.a.id);
    if (after) edit([[drag.a.id, L.anns.get(drag.a.id) ?? null, after]]);
    else invalidate(D.SHAPES | D.TAGS);
    drag = null;
  },
  onKey(combo) {
    if (combo === 'escape' && (drag || moving())) {
      if (drag) L.editing.delete(drag.a.id);
      drag = null; cancelMove();
      invalidate(D.SHAPES | D.TAGS | D.UI);
      return true;
    }
    const pl = kpAnn(placing);
    if (!pl) { placing = null; return false; }
    if (combo === 'escape' || combo === 'enter') { placing = null; invalidate(D.UI); return true; }
    if (combo === 'backspace') {
      const pts = pl.geometry.points;
      let last = -1;
      for (let i = pts.length - 1; i >= 0; i--) if (pts[i]?.[2]) { last = i; break; }
      if (last > 0) setPoint(pl, last, [0, 0, 0]);
      return true;
    }
    return false;
  },
  // V: visible <-> occluded on the hovered point, else the last placed one
  visibility() {
    const a = target();
    if (!a) return false;
    let i = cur ? pointAt(cur, a) : -1;
    if (i < 0) { const pts = a.geometry.points; for (let k = pts.length - 1; k >= 0; k--) if (pts[k]?.[2]) { i = k; break; } }
    const q = a.geometry.points[i];
    if (i < 0 || !q) return false;
    setPoint(a, i, [q[0], q[1], q[2] === 2 ? 1 : 2]);
    return true;
  },
  finish() { if (!placing) return false; placing = null; invalidate(D.UI); return true; },
  cancel() { const had = !!placing; placing = null; return had; },
  reset() { placing = null; drag = null; },
  drawPreview(x) {
    const a = live(target());
    if (a && !moving()) {
      const names = kpNames(a.task_id);
      const pts = a.geometry.points;
      for (const q of pts) {
        if (!q[2]) continue;
        drawHandle(x, q[0] * T.ax + T.tx, q[1] * T.ay + T.ty, 3.5, true);
      }
      const h = cur ? pointAt(cur, a) : -1;
      if (cur && h >= 0) label(x, cur.sx + 12, cur.sy - 10, `${names[h]}${pts[h]?.[2] === 1 ? ' (occluded)' : ''}`);
    }
    const task = L.taskId ? TASKS.get(L.taskId) : undefined;
    if (cur && task && task.task_type === 'keypoints') {
      const pl = kpAnn(placing);
      const names = kpNames();
      const k = pl ? nextIdx(pl) : 0;
      if (k >= 0 && !drag) label(x, cur.sx + 12, cur.sy + 14, names[k] ?? '');
    }
  },
  cursor(p) {
    if (drag) return 'grabbing';
    if (moving()) return 'move';
    if (pointAt(p, soleSelected('keypoints')) >= 0) return 'grab';
    return 'crosshair';
  },
  hint() {
    const pl = kpAnn(placing);
    const names = kpNames();
    const k = pl ? nextIdx(pl) : 0;
    return k >= 0 ? `next: ${names[k]}${pl ? ` (${k + 1}/${names.length})` : ''}` : '';
  },
} satisfies Tool;

function label(x: CanvasRenderingContext2D, px: number, py: number, text: string): void {
  x.font = '600 12px sans-serif';
  x.textBaseline = 'middle';
  const w = x.measureText(text).width + 8;
  x.fillStyle = colors().halo;
  x.fillRect(px - 4, py - 9, w, 18);
  x.fillStyle = colors().tagLight;
  x.fillText(text, px, py);
}
