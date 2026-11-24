// Copy / Cut / Paste of the Bucket page (Ctrl+C / Ctrl+X / Ctrl+V on the grid, the right-click menus): the clipboard
// holds {bucket, paths}, not the files. Paste into the open folder (or the folder right-clicked):
//  - the same bucket: Cut = a move, Copy of small files = ops copy (instant, undoable); a folder or a big copy goes to
//    the bk-copy job so the page never blocks
//  - another bucket: the bk-copy job (any source: local, downloaded or streamed S3; bytes + ETA in the status bar);
//    Cut then deletes the originals (to the trash: undoable) once every file was copied.

import { errText } from '../core/api.js';
import { waitJob } from '../core/jobs.js';
import { actionToast, toast } from '../core/toast.js';
import { watchJobs } from './activity.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { moveTo, runOps, undoOp } from './ops.js';
import { baseName, bucketOf, D, hooks, parentOf, selEntries, writable } from './state.js';
import type { Job } from '../types.js';

interface Clip { bid: string; paths: string[]; cut: boolean; bytes: number; dirs: number }
let CLIP: Clip | null = null;
const BIG = 200 * 1024 * 1024;                  // a same-bucket copy over this (or of a folder) runs as a job

export const clip = (): Clip | null => CLIP;
const what = (c: Clip): string => (c.paths.length === 1 && c.paths[0] !== undefined ? baseName(c.paths[0]) || 'the bucket' : `${c.paths.length} items`);

export function clipSel(cut: boolean): void {
  const es = selEntries();
  if (!D.bid || !es.length) return;
  if (cut && !writable()) { toast('This folder is read-only: Copy instead (Ctrl+C)', 'err'); return; }
  CLIP = { bid: D.bid, paths: es.map((e) => e.path), cut, bytes: es.reduce((n, e) => n + (e.type === 'file' ? e.size || 0 : 0), 0), dirs: es.filter((e) => e.type === 'dir').length };
  toast(`${cut ? 'Cut' : 'Copied'} ${what(CLIP)}: open a folder and paste (Ctrl+V)`, 'ok');
  hooks.render();
}

export function clearClip(): void { CLIP = null; hooks.render(); }

/** Paste the clipboard into folder `to` of bucket `bid` (default: the open folder). */
export async function paste(bid = D.bid ?? '', to = D.path): Promise<void> {
  const c = CLIP;
  if (!c || !bid) return;
  if (bid === c.bid && c.cut && c.paths.every((p) => parentOf(p) === to)) { toast('They are already in this folder', 'err'); return; }
  if (bid === c.bid && c.paths.some((p) => to === p || to.startsWith(`${p}/`))) { toast('A folder cannot go into itself', 'err'); return; }
  if (bid === c.bid && (c.cut || (!c.dirs && c.bytes < BIG))) {
    await moveTo(bid, c.paths, to, c.cut ? 'move' : 'copy');
    if (c.cut) CLIP = null;
    return;
  }
  let r: Awaited<ReturnType<typeof api.copyTo>>;
  try { r = await api.copyTo(c.bid, c.paths, bid, to); } catch (e) { toast(`Not pasted: ${errText(e)}`, 'err'); return; }
  watchJobs();                                  // the status bar shows the copy (bytes, ETA, Cancel)
  let res = r.result;
  if (r.job_id) {
    try { res = ((await waitJob<Job & { result?: api.CopyResult }>(r.job_id)).result ?? null); } catch (e) { toast(`Copy failed: ${errText(e)}`, 'err'); await hooks.reload(); return; }
  }
  if (!res) return;
  const dest = `${bucketOf(bid)?.name ?? ''}/${to}${to ? '/' : ''}`;
  if (res.cancelled) toast(`Copy stopped: ${res.files} files copied to ${dest}`, 'err');
  else if (c.cut && c.bid !== bid) {
    const gone = await runOps(c.bid, c.paths.map((p) => ({ op: 'delete', path: p })), `Moved ${what(c)} to ${dest}`);
    if (gone) CLIP = null;
  } else {
    const undo = res.undo_id;
    actionToast({ kind: 'ok', title: `Copied ${what(c)} to ${dest}`, body: `${res.files} files`,
      actions: undo ? [{ label: 'Undo', icon: 'undo', primary: false, run: () => undoOp(bid, undo) }] : [] });
  }
  await hooks.reload();
}

export const CLIP_ACTIONS: Record<string, Action> = {
  'clip-copy': () => clipSel(false),
  'clip-cut': () => clipSel(true),
  'clip-paste': (el) => { void paste(el.dataset['bid'] || D.bid || '', el.dataset['path'] ?? D.path); },
  'clip-clear': () => clearClip(),
};
