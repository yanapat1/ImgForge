// Polygon tool (segmentation). Drawing: click adds a vertex, click-and-drag samples points freehand (every 4 screen
// px); Enter, a double-click or a click on the first vertex (8 px) closes it; Backspace removes the last point,
// Esc cancels. Editing the selected polygon: drag a vertex, click an edge midpoint to insert one, Alt+click a vertex
// to delete it. Self-intersection is allowed (a warning is shown). Geometry {points: [[x, y], …]}.

import type { Pt, PointerPos, PolygonAnn, Tool } from '../../types-label.js';
import { clsOf, colors, drawHandle, invalidate, D, T } from '../canvas.js';
import { edit } from '../history.js';
import { L } from '../state.js';
import { cancelMove, clamp01, clearSelection, endMove, makeAnn, moveTo, moving, pressShape, setSelection, soleSelected, toast } from './select.js';

const CLOSE_PX = 8, SAMPLE_PX = 4, HANDLE_PX = 7;
let draft: { pts: Pt[]; down: boolean; last: PointerPos } | null = null;
let drag: { a: PolygonAnn; i: number } | null = null;       // vertex drag of the selected polygon
let cur: PointerPos | null = null;        // last pointer position

const scr = (q: Pt): Pt => [q[0] * T.ax + T.tx, q[1] * T.ay + T.ty];
const near = (p: PointerPos, q: Pt, r: number): boolean => { const s = scr(q); return Math.abs(p.sx - s[0]) <= r && Math.abs(p.sy - s[1]) <= r; };
const live = (a: PolygonAnn | null): PolygonAnn | null => {
  const e = a && L.editing.get(a.id);
  return e && e.geometry_type === 'polygon' ? e : a;
};

function vertexAt(p: PointerPos, a: PolygonAnn | null): number {
  if (!a) return -1;
  const pts = a.geometry.points;
  for (let i = 0; i < pts.length; i++) { const q = pts[i]; if (q && near(p, q, HANDLE_PX)) return i; }
  return -1;
}
function midAt(p: PointerPos, a: PolygonAnn | null): number {
  if (!a) return -1;
  const pts = a.geometry.points;
  for (let i = 0; i < pts.length; i++) {
    const q = pts[(i + 1) % pts.length], q0 = pts[i];
    if (q && q0 && near(p, [(q0[0] + q[0]) / 2, (q0[1] + q[1]) / 2], HANDLE_PX - 1)) return i;
  }
  return -1;
}

function segsCross(a: Pt, b: Pt, c: Pt, d: Pt): boolean {
  const o = (p: Pt, q: Pt, r: Pt): number => Math.sign((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]));
  return o(a, b, c) * o(a, b, d) < 0 && o(c, d, a) * o(c, d, b) < 0;
}
function selfIntersects(p: Pt[]): boolean {
  const n = p.length;
  if (n < 4 || n > 2000) return false;
  for (let i = 0; i < n; i++) {
    for (let j = i + 2; j < n; j++) {
      if (i === 0 && j === n - 1) continue;
      const a = p[i], b = p[(i + 1) % n], c = p[j], d = p[(j + 1) % n];
      if (a && b && c && d && segsCross(a, b, c, d)) return true;
    }
  }
  return false;
}

function close(): boolean {
  if (!draft) return false;
  // drop consecutive near-duplicates (a double-click adds the same point twice)
  const pts: Pt[] = [];
  for (const q of draft.pts) {
    const l = pts[pts.length - 1];
    if (!l || Math.abs((q[0] - l[0]) * T.ax) + Math.abs((q[1] - l[1]) * T.ay) > 1.5) pts.push(q);
  }
  if (pts.length < 3) { toast('A polygon needs at least 3 points', 'warn'); return true; }
  draft = null;
  invalidate(D.UI);
  if (!L.classId) { toast('Add a class to this task first', 'warn'); return true; }
  const a = makeAnn('polygon', { points: pts });
  edit([[a.id, null, a]]);
  setSelection([a.id]);
  if (selfIntersects(pts)) toast('Polygon edges cross each other', 'warn');
  return true;
}

export const polygonTool = {
  name: 'Polygon',
  coalesced: true,
  busy: () => !!draft || !!drag || moving(),
  onDown(p, e) {
    const pt: Pt = [clamp01(p.nx), clamp01(p.ny)];
    if (draft) {
      const first = draft.pts[0];
      if (first && draft.pts.length >= 3 && near(p, first, CLOSE_PX)) { close(); return; }
      draft.pts.push(pt);
      draft.down = true; draft.last = p;
      return;
    }
    const sel = soleSelected('polygon');
    const v = vertexAt(p, sel);
    if (sel && v >= 0) {
      if (e.altKey) {
        if (sel.geometry.points.length <= 3) { toast('A polygon needs at least 3 points', 'warn'); return; }
        const points = sel.geometry.points.filter((_, i) => i !== v);
        edit([[sel.id, sel, { ...sel, geometry: { ...sel.geometry, points } }]]);
        return;
      }
      drag = { a: sel, i: v };
      return;
    }
    const m = midAt(p, sel);
    if (sel && m >= 0) {
      const points = sel.geometry.points.slice();
      points.splice(m + 1, 0, pt);
      const a: PolygonAnn = { ...sel, geometry: { ...sel.geometry, points } };
      edit([[sel.id, sel, a]]);
      drag = { a, i: m + 1 };
      return;
    }
    if (!e.ctrlKey && !e.metaKey && pressShape(p, e)) return;      // Ctrl+press: draw over existing shapes
    if (!e.shiftKey) clearSelection();
    draft = { pts: [pt], down: true, last: p };
  },
  onMove(p) {
    cur = p;
    if (moving()) { moveTo(p); return; }
    if (drag) {
      const points = drag.a.geometry.points.slice();
      points[drag.i] = [clamp01(p.nx), clamp01(p.ny)];
      if (!L.editing.has(drag.a.id)) invalidate(D.SHAPES | D.TAGS);
      L.editing.set(drag.a.id, { ...drag.a, geometry: { ...drag.a.geometry, points } });
      return;
    }
    if (draft && draft.down && Math.hypot(p.sx - draft.last.sx, p.sy - draft.last.sy) >= SAMPLE_PX) {
      draft.pts.push([clamp01(p.nx), clamp01(p.ny)]);
      draft.last = p;
    }
  },
  onUp() {
    if (moving()) { endMove(); return; }
    if (drag) {
      const after = L.editing.get(drag.a.id);
      L.editing.delete(drag.a.id);
      if (after) edit([[drag.a.id, L.anns.get(drag.a.id) ?? null, after]]);
      else invalidate(D.SHAPES | D.TAGS);
      drag = null;
      return;
    }
    if (draft) draft.down = false;
  },
  onDbl() { if (draft) close(); },
  onKey(combo) {
    if (!draft) {
      if (combo === 'escape' && (drag || moving())) {
        if (drag) L.editing.delete(drag.a.id);
        drag = null; cancelMove();
        invalidate(D.SHAPES | D.TAGS | D.UI);
        return true;
      }
      return false;
    }
    if (combo === 'enter') return close();
    if (combo === 'escape') { draft = null; invalidate(D.UI); return true; }
    if (combo === 'backspace') {
      draft.pts.pop();
      if (!draft.pts.length) draft = null;
      invalidate(D.UI);
      return true;
    }
    return false;
  },
  finish: () => (draft ? close() : false),
  cancel() { const had = !!draft; draft = null; return had; },
  drawPreview(x) {
    const col = colors();
    if (draft) {
      const c = L.classId ? clsOf({ class_id: L.classId }).color : col.preview;
      x.beginPath();
      const first = draft.pts[0] ?? [0, 0];
      const p0 = scr(first);
      x.moveTo(p0[0], p0[1]);
      for (const q of draft.pts.slice(1)) { const s = scr(q); x.lineTo(s[0], s[1]); }
      if (cur) x.lineTo(cur.sx, cur.sy);
      x.strokeStyle = col.halo; x.lineWidth = 4; x.stroke();
      x.strokeStyle = c; x.lineWidth = 2; x.stroke();
      const closing = cur && draft.pts.length >= 3 && near(cur, first, CLOSE_PX);
      drawHandle(x, p0[0], p0[1], closing ? 6 : 4, true);
      return;
    }
    const sel = live(soleSelected('polygon'));
    if (!sel || moving()) return;
    const pts = sel.geometry.points;
    x.globalAlpha = 0.6;
    for (let i = 0; i < pts.length; i++) {
      const q = pts[(i + 1) % pts.length], q0 = pts[i];
      if (!q || !q0) continue;
      const s = scr([(q0[0] + q[0]) / 2, (q0[1] + q[1]) / 2]);
      drawHandle(x, s[0], s[1], 2.5, true);
    }
    x.globalAlpha = 1;
    for (const q of pts) { const s = scr(q); drawHandle(x, s[0], s[1], 3.5); }
  },
  cursor(p) {
    if (draft) { const first = draft.pts[0]; return first && draft.pts.length >= 3 && near(p, first, CLOSE_PX) ? 'pointer' : 'crosshair'; }
    if (drag) return 'grabbing';
    if (moving()) return 'move';
    const sel = soleSelected('polygon');
    if (vertexAt(p, sel) >= 0) return 'grab';
    if (midAt(p, sel) >= 0) return 'copy';
    return L.hoverId ? 'move' : 'crosshair';
  },
  hint: () => (draft ? `${draft.pts.length} points · Enter closes · Backspace removes the last` : ''),
} satisfies Tool;
