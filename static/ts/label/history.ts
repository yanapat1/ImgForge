// Every change to the annotations of the image on screen goes through edit(): it applies the change to L.anns,
// records it on the image's undo stack (L.history, kept per image in the LRU) and tells the page (redraw, list,
// autosave). Annotations are immutable objects: an edit replaces the Ann, so history entries hold plain references.
// A change is a list of [id, before | null, after | null] (null before = added, null after = deleted).
// Becomes manual: a 'model' shape whose geometry or class changes is saved as source 'manual', score null (the
// change keeps the model `before`, so undo restores the model state; redo re-applies the manual one).

import type { Change, History } from '../types-label.js';
import { L } from './state.js';

const CAP = 200;
const MERGE_MS = 800;
type Notify = (tasks: Set<string>) => void;
let notify: Notify = () => {};

export function onEdit(fn: Notify): void { notify = fn; }

export const newHistory = (): History => ({ undo: [], redo: [] });

function apply(changes: Change[], forward: boolean): void {
  const tasks = new Set<string>();
  for (const [id, before, after] of changes) {
    const v = forward ? after : before;
    if (v) L.anns.set(id, v); else L.anns.delete(id);
    if (!v) { L.selected.delete(id); L.hidden.delete(id); }
    const ref = after || before;
    if (ref) tasks.add(ref.task_id);
  }
  for (const t of tasks) L.dirty.add(t);
  notify(tasks);
}

const sameGeom = (a: unknown, b: unknown): boolean => a === b || JSON.stringify(a) === JSON.stringify(b);

function manualize(changes: Change[]): Change[] {
  for (const c of changes) {
    const [, before, after] = c;
    if (!before || !after || before.source !== 'model' || after.source !== 'model') continue;
    if (after.class_id !== before.class_id || after.geometry_type !== before.geometry_type || !sameGeom(after.geometry, before.geometry)) {
      c[2] = { ...after, source: 'manual', score: null };
    }
  }
  return changes;
}

// merge: consecutive edits with the same key within MERGE_MS become one undo step (arrow-key nudges)
export function edit(changes: Change[], merge: string | null = null): void {
  if (!changes.length || !L.history || L.readOnly) return;      // a viewer's page never changes a shape
  manualize(changes);
  const h = L.history;
  const top = h.undo[h.undo.length - 1];
  const now = performance.now();
  if (merge && top && top.merge === merge && now - top.t < MERGE_MS) {
    const byId = new Map(top.changes.map((c) => [c[0], c]));
    for (const c of changes) {
      const old = byId.get(c[0]);
      if (old) old[2] = c[2]; else top.changes.push(c);
    }
    top.t = now;
  } else {
    h.undo.push({ changes, merge, t: now });
    if (h.undo.length > CAP) h.undo.shift();
  }
  h.redo.length = 0;
  apply(changes, true);
}

export function undo(): boolean {
  const h = L.history;
  const e = h && h.undo.pop();
  if (!h || !e) return false;
  h.redo.push(e);
  apply(e.changes, false);
  return true;
}

export function redo(): boolean {
  const h = L.history;
  const e = h && h.redo.pop();
  if (!h || !e) return false;
  h.undo.push(e);
  apply(e.changes, true);
  return true;
}
