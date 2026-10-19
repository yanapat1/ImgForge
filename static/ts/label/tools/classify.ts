// Classification: nothing is drawn. The class rows (and the class hotkeys) toggle image-level tags of the active
// task, one tag annotation per class ({geometry_type: 'tag', geometry: {}}); single-label tasks keep at most one
// (task.config.multi_label = true allows several). The page shows the tags as a banner over the image.

import type { Change, Tool } from '../../types-label.js';
import { TASKS } from '../canvas.js';
import { edit } from '../history.js';
import { L } from '../state.js';
import { makeAnn } from './select.js';

export function toggleTag(classId: string | null | undefined): boolean {
  const t = L.taskId ? TASKS.get(L.taskId) : undefined;
  if (!t || !classId) return false;
  const multi = !!(t.config && t.config.multi_label);
  const mine = [...L.anns.values()].filter((a) => a.task_id === t.id && a.geometry_type === 'tag');
  const has = mine.find((a) => a.class_id === classId);
  const changes: Change[] = [];
  if (has) changes.push([has.id, has, null]);
  else {
    if (!multi) for (const a of mine) changes.push([a.id, a, null]);
    const a = makeAnn('tag', {}, { class_id: classId });
    changes.push([a.id, null, a]);
  }
  edit(changes);
  return true;
}

export const classifyTool = {
  name: 'Classify',
  busy: () => false,
  onDown() {},
  onMove() {},
  onUp() {},
  drawPreview() {},
  cursor: () => 'default',
  hint: () => 'click a class (or press its key) to tag the image',
} satisfies Tool;
