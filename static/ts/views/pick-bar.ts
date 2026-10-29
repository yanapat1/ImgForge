// The pick bar in the gallery head: how many thumbnails are picked (ctrl+click, or a drag over them) and the bulk
// edits on their boxes: change them all to one class, Delete (button or the Delete key). Label files are edited through one curation fix batch,
// so a toast offers Undo.

import { $req, esc } from '../core/dom.js';
import { meta, pts, S } from '../core/state.js';
import { clsName } from '../core/theme.js';
import { clearPicked } from '../plot/marks.js';
import { fixBoxes } from './curation.js';
import { openDialog } from './dialog.js';
import { unit } from './preview.js';

const canEdit = (): boolean => S.status?.my_role !== 'viewer' && (S.status?.caps?.curation ?? true) &&
  S.status?.caps?.unit !== 'image';

/** Picked boxes still on the map (a fix may have deleted some since). */
const pickedIds = (): number[] => S.picked.filter((id) => !S.P?.deleted?.[id]);

function sync(): void {
  const n = pickedIds().length;
  $req('pick-bar').classList.toggle('hidden', !n);
  $req('pick-count').textContent = `${n.toLocaleString()} picked`;
}

/** Delete the picked boxes, after a confirmation (the Delete key and the button). */
export async function deletePicked(): Promise<void> {
  const ids = pickedIds();
  if (!ids.length || !canEdit()) return;
  const n = `${ids.length.toLocaleString()} ${unit(ids.length)}`;
  await openDialog({
    title: `Delete ${n}?`,
    body: `<p>The ${esc(n)} you picked are removed from their label files. A backup is kept: undo from the message ` +
      'that follows, or in Curation › History.</p>',
    ok: 'Delete', okClass: 'danger-btn',
    onOk: async () => { if (await fixBoxes(ids, 'delete')) clearPicked(); return null; },
  });
}

/** Change every picked box to the class chosen in the bar (asks first above 20 boxes). */
export async function setPickedClass(): Promise<void> {
  const cls = +$req<HTMLSelectElement>('pick-cls').value, P = pts();
  const ids = pickedIds().filter((id) => P.cls[id] !== cls);
  if (!canEdit() || !Number.isInteger(cls) || cls < 0) return;
  if (!ids.length) { $req('pick-count').textContent = `all already ${clsName(cls)}`; return; }
  const run = async (): Promise<null> => { await fixBoxes(ids, 'set_class', cls); return null; };
  if (ids.length <= 20) { await run(); return; }
  const n = `${ids.length.toLocaleString()} ${unit(ids.length)}`;
  await openDialog({
    title: `Change ${n} to ${clsName(cls)}?`,
    body: `<p>Their label files are edited. A backup is kept: undo from the message that follows, or in Curation › History.</p>`,
    ok: 'Change class', onOk: run,
  });
}

export function initPickBar(): void {
  $req('pick-cls').innerHTML = meta().classes.map((c, i) => `<option value="${i}">${esc(c)}</option>`).join('');
  window.addEventListener('picks-changed', sync);
  $req('pick-setcls').onclick = () => { void setPickedClass(); };
  $req('pick-del').onclick = () => { void deletePicked(); };
  $req('pick-clear').onclick = clearPicked;
  // Delete / Backspace anywhere on the page (not while typing, nor with a dialog open) deletes the picks
  document.addEventListener('keydown', (e) => {
    if ((e.key !== 'Delete' && e.key !== 'Backspace') || !S.picked.length || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target instanceof HTMLElement ? e.target : null;
    if (t && (t.isContentEditable || t.closest('input, textarea, select, .modal:not(.hidden), .ui-dlg-wrap, .popover:not(.hidden)'))) return;
    if (!$req('modal').classList.contains('hidden') || !$req('settings').classList.contains('hidden')) return;
    e.preventDefault();
    void deletePicked();
  });
  sync();
}
