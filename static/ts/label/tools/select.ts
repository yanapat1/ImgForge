// Shared tool behaviour: the selection, new annotations, moving (Alt = duplicate), nudging, deleting, reassigning,
// Tab through overlapping shapes, and geometry helpers. A tool is {onDown, onMove, onUp, onDbl?, onKey?,
// drawPreview(ctx), cursor(p), busy()}; each geometry tool calls pressShape() for clicks on existing shapes.

import type { ToastKind } from '../../core/toast.js';
import type { Ann, AnnBase, BBoxGeom, Change, GeometryType, KpGeom, KpPoint, PointerPos, PolyGeom, Pt, TagGeom } from '../../types-label.js';
import { bounds, hitAll, hitTest, imageSize, selectionChanged, T } from '../canvas.js';
import { edit } from '../history.js';
import { L } from '../state.js';

interface SelectHandlers { selection: () => void; toast: (msg: string, kind?: ToastKind) => void }
let H: SelectHandlers = { selection: () => {}, toast: () => {} };
export function initSelect(handlers: Partial<SelectHandlers>): void { H = { ...H, ...handlers }; }
export const toast = (m: string, kind?: ToastKind): void => H.toast(m, kind);

/** The annotation type of a geometry type. */
export type AnnOf<T extends GeometryType> = Extract<Ann, { geometry_type: T }>;
interface GeomMap { bbox: BBoxGeom; polygon: PolyGeom; keypoints: KpGeom; tag: TagGeom }
const isType = <T extends GeometryType>(a: Ann, type: T): a is AnnOf<T> => a.geometry_type === type;
/** The annotations of these ids (gone ids skipped), optionally filtered. */
export function annsOf(ids: Iterable<string>, keep: (a: Ann) => boolean = () => true): Ann[] {
  const out: Ann[] = [];
  for (const id of ids) { const a = L.anns.get(id); if (a && keep(a)) out.push(a); }
  return out;
}

export const clamp01 = (v: number): number => (v < 0 ? 0 : v > 1 ? 1 : v);

export function newId(): string {
  if (crypto.randomUUID) return crypto.randomUUID().replaceAll('-', '');
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = ((b[6] ?? 0) & 0x0f) | 0x40; b[8] = ((b[8] ?? 0) & 0x3f) | 0x80;
  return [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
}

export function makeAnn<T extends GeometryType>(geometry_type: T, geometry: GeomMap[T], extra: Partial<AnnBase> = {}): AnnOf<T> {
  const base: AnnBase = { id: newId(), task_id: L.taskId ?? '', class_id: L.classId ?? '', attributes: {}, parent_id: null,
    z_order: 0, ...extra };
  return { ...base, geometry_type, geometry } as AnnOf<T>;
}

// ---------------------------------------------------------------- selection
export function setSelection(ids: Iterable<string>): void {
  L.selected = new Set(ids);
  selectionChanged();
  H.selection();
}
export const clearSelection = (): void => { if (L.selected.size) setSelection([]); };
export function toggleSelection(id: string): void {
  const s = new Set(L.selected);
  if (s.has(id)) s.delete(id); else s.add(id);
  setSelection(s);
}
// the single selected annotation of the active task with this geometry, if exactly one is selected
export function soleSelected<T extends GeometryType>(type: T): AnnOf<T> | null {
  if (L.selected.size !== 1) return null;
  const id = L.selected.values().next().value;
  const a = id === undefined ? undefined : L.anns.get(id);
  return a && a.task_id === L.taskId && isType(a, type) ? a : null;
}

// ---------------------------------------------------------------- geometry
export function translate<A extends Ann>(a: A, dx: number, dy: number): Ann {
  const b = bounds(a);
  if (b) {                      // keep the whole shape inside the image
    dx = Math.min(Math.max(dx, -b.x0), 1 - b.x1);
    dy = Math.min(Math.max(dy, -b.y0), 1 - b.y1);
  }
  const mv = (p: Pt): Pt => [p[0] + dx, p[1] + dy];
  if (a.geometry_type === 'bbox') return { ...a, geometry: { ...a.geometry, x: a.geometry.x + dx, y: a.geometry.y + dy } };
  if (a.geometry_type === 'polygon') {
    const g = a.geometry;
    const ng: PolyGeom = { ...g, points: g.points.map(mv) };
    if (g.holes) ng.holes = g.holes.map((h) => h.map(mv));
    return { ...a, geometry: ng };
  }
  if (a.geometry_type === 'keypoints') {
    const points = a.geometry.points.map((p): KpPoint => (p[2] ? [p[0] + dx, p[1] + dy, p[2]] : p));
    return { ...a, geometry: { ...a.geometry, points } };
  }
  return a;
}

// ---------------------------------------------------------------- pressing on a shape: select + move / duplicate
interface MoveState { start: PointerPos; orig: Ann[]; dup: boolean; moved: boolean; ids?: string[] }
let mv: MoveState | null = null;

// returns true when the press landed on a shape of the active task (and the gesture is now a move)
export function pressShape(p: PointerPos, e: PointerEvent): boolean {
  const id = hitTest(p.sx, p.sy);
  if (!id) return false;
  if (e.shiftKey) { toggleSelection(id); return true; }
  if (!L.selected.has(id)) setSelection([id]);
  const orig = annsOf(L.selected, (a) => a.task_id === L.taskId && a.geometry_type !== 'tag');
  mv = { start: p, orig, dup: e.altKey, moved: false };
  return true;
}

export const moving = (): boolean => !!mv;

export function moveTo(p: PointerPos): void {
  const m = mv;
  if (!m) return;
  const dsx = p.sx - m.start.sx, dsy = p.sy - m.start.sy;
  if (!m.moved && Math.abs(dsx) + Math.abs(dsy) < 3) return;
  if (!m.moved) {
    m.moved = true;
    if (m.dup) m.ids = m.orig.map(() => newId());
    selectionChanged();        // L1 drops the dragged shapes once (they are drawn on L2 while moving)
  }
  const dx = dsx / T.ax, dy = dsy / T.ay;
  const ids = m.ids;
  m.orig.forEach((a, i) => {
    const moved = translate(a, dx, dy);
    const nid = ids && ids[i];
    if (m.dup && nid) L.editing.set(nid, { ...moved, id: nid, source: 'manual', score: null });
    else L.editing.set(a.id, moved);
  });
}

export function endMove(): void {
  if (!mv) return;
  const m = mv;
  mv = null;
  if (!m.moved) return;
  const changes: Change[] = [];
  for (const [id, a] of L.editing) changes.push([id, m.dup ? null : L.anns.get(id) ?? null, a]);
  L.editing.clear();
  edit(changes);
  if (m.dup) setSelection(m.ids ?? []);
  else selectionChanged();
}

export function cancelMove(): boolean {
  if (!mv) return false;
  mv = null;
  L.editing.clear();
  selectionChanged();
  return true;
}

// ---------------------------------------------------------------- keyboard edits
export function nudge(dxPx: number, dyPx: number): boolean {
  const { w, h } = imageSize();
  if (!w) return false;
  const sel = annsOf(L.selected, (a) => a.task_id === L.taskId && a.geometry_type !== 'tag');
  if (!sel.length) return false;
  edit(sel.map((a): Change => [a.id, a, translate(a, dxPx / w, dyPx / h)]), 'nudge');
  return true;
}

export function deleteSelected(): boolean {
  const sel = annsOf(L.selected);
  if (!sel.length) return false;
  // deleting a bbox also unlinks keypoint instances that pointed at it
  const ids = new Set(sel.map((a) => a.id));
  const changes = sel.map((a): Change => [a.id, a, null]);
  for (const a of L.anns.values()) if (a.parent_id && ids.has(a.parent_id) && !ids.has(a.id)) changes.push([a.id, a, { ...a, parent_id: null }]);
  edit(changes);
  setSelection([]);
  return true;
}

export function duplicateSelected(): boolean {
  const sel = annsOf(L.selected, (a) => a.geometry_type !== 'tag');
  if (!sel.length) return false;
  // copies are manual shapes (no model score); a keypoint instance copied with its box stays linked to the copy
  const ids = new Map(sel.map((a) => [a.id, newId()]));
  const copies = sel.map((a): Ann => ({ ...translate(a, 0.01, 0.01), id: ids.get(a.id) ?? newId(), source: 'manual', score: null,
    parent_id: a.parent_id && ids.has(a.parent_id) ? ids.get(a.parent_id) ?? null : a.parent_id || null }));
  edit(copies.map((a): Change => [a.id, null, a]));
  setSelection(copies.map((a) => a.id));
  return true;
}

export function reassignSelected(classId: string): boolean {
  const sel = annsOf(L.selected, (a) => a.task_id === L.taskId && a.class_id !== classId);
  if (!sel.length) return false;
  edit(sel.map((a): Change => [a.id, a, { ...a, class_id: classId }]));
  return true;
}

// Tab: the next shape under the pointer (smallest first, wrapping)
export function cycleOverlap(p: PointerPos | null | undefined): boolean {
  if (!p) return false;
  const list = hitAll(p.sx, p.sy);
  if (list.length < 2) return false;
  const cur = list.findIndex((a) => L.selected.has(a.id));
  const next = list[(cur + 1) % list.length];
  if (!next) return false;
  setSelection([next.id]);
  L.hoverId = next.id;
  return true;
}
