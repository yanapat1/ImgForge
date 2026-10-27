// File operations of the open bucket, all through POST …/ops (one batch, one undo id): New folder, Rename (F2),
// Move / Copy (a folder picker dialog, or a drag onto a folder), Delete (confirm; files go to the trash), and the bucket
// itself (rename, unlink / delete). After each change: an action toast with Undo (POST …/undo), and when the change
// made the format detection switch (`retagged` in the answer) the amber "Format changed: YOLO detect → Other
// (labels/ missing)" toast with Undo, remembered for the folder banner (browser.ts) until undone or dismissed.

import { errText } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { actionToast, toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { baseName, bucketOf, D, fmtLabel, fmtN, hooks, isImage, joinPath, parentOf, selEntries, SOURCE } from './state.js';
import type { Entry, Op, OpsResult, Retag } from '../types-datacenter.js';

const items = (n: number, what = 'item'): string => `${fmtN(n)} ${what}${n === 1 ? '' : 's'}`;
const describe = (es: Entry[]): string => {
  if (es.length === 1 && es[0]) return es[0].name;
  const img = es.filter(isImage).length;
  return img === es.length ? items(img, 'image') : items(es.length);
};
const badName = (s: string): string | null =>
  !s ? 'Enter a name' : /[/\\]/.test(s) ? 'A name cannot contain / or \\' : s === '.' || s === '..' ? 'Not a valid name' : null;

/** The format-change toast (img5): from → to, why, Undo when the change can be undone. */
export function showRetag(bid: string, rt: Retag, undoId: string | null, what: string): void {
  D.retag.set(bid, { ...rt, undo_id: undoId });
  const code = (s: string): string => `<code>${esc(s)}</code>`;
  actionToast({
    kind: 'warn', timeout: 12000,
    title: `Format changed: ${fmtLabel(rt.from)} → ${fmtLabel(rt.to)}${rt.reason ? ` (${rt.reason})` : ''}`,
    body: `${esc(what)}. ${rt.to === 'other' ? 'Nothing matches a known layout now, so' : 'The detection changed:'} the badge and the auto tag switched to ${code(`format:${rt.to}`)}.`,
    actions: undoId ? [{ label: 'Undo', icon: 'undo', run: () => undoRetag(bid) }] : [],
  });
}

/** Undo the change that switched this bucket's format (the folder banner's "Move it back"). */
export async function undoRetag(bid: string): Promise<void> {
  const rt = D.retag.get(bid);
  if (!rt?.undo_id) return;
  await undoOp(bid, rt.undo_id);
}

async function undoOp(bid: string, undoId: string): Promise<void> {
  try {
    const r = await api.undo(bid, undoId);
    const rt = D.retag.get(bid);
    if (rt?.undo_id === undoId) D.retag.delete(bid);
    const n = Array.isArray(r.restored) ? r.restored.length : r.restored;
    toast(`Undone${n ? `: ${items(Number(n))} restored` : ''}${r.retagged ? ` · format: ${fmtLabel(r.retagged.to)}` : ''}`, 'ok');
  } catch (e) {
    toast(`Could not undo: ${errText(e)}`, 'err');
  }
  await hooks.reload();
}

/** Run one batch of ops; `what` is the past-tense sentence for the toasts ("Moved 4 images to night/"). */
export async function runOps(bid: string, list: Op[], what: string): Promise<OpsResult | null> {
  if (!list.length) return null;
  let r: OpsResult;
  try {
    r = await api.ops(bid, list);
  } catch (e) {
    toast(`Not done (${what.toLowerCase()}): ${errText(e)}`, 'err');
    return null;
  }
  const failed = r.errors?.length ?? 0;
  if (r.retagged) showRetag(bid, r.retagged, r.undo_id, what);
  else if (failed && failed >= list.length) toast(`Nothing changed: ${r.errors[0]?.error ?? 'error'}`, 'err');
  else {
    const undo = r.undo_id;
    actionToast({
      kind: failed ? 'warn' : 'ok', title: failed ? `${what} (${failed} failed)` : what,
      ...(failed ? { body: esc(r.errors.slice(0, 3).map((x) => x.error).join(' · ')) } : {}),
      actions: undo ? [{ label: 'Undo', icon: 'undo', primary: false, run: () => undoOp(bid, undo) }] : [],
    });
  }
  D.sel.clear();
  D.anchor = null;
  await hooks.reload();
  return r;
}

// ---------------------------------------------------------------- dialogs

const nameField = (label: string, value: string, hint = ''): string =>
  `<label class="fld"><span>${esc(label)}</span><input type="text" class="txt" id="dc-name" value="${esc(value)}" autocomplete="off" spellcheck="false"></label>${hint ? `<p class="hint">${hint}</p>` : ''}`;

export async function newFolder(): Promise<void> {
  const bid = D.bid;
  if (!bid) return;
  const base = D.path;
  await openDialog({
    title: 'New folder', ok: 'Create', focus: '#dc-name',
    body: nameField('Name', '', `In <code>${esc(bucketOf(bid)?.name ?? '')}/${esc(base)}</code>`),
    onOk: async (root) => {
      const v = root.querySelector<HTMLInputElement>('#dc-name')?.value.trim() ?? '';
      const bad = !v ? 'Enter a name' : v.split('/').map(badName).find((x) => x) ?? null;   // a/b makes both
      if (bad) return bad;
      const r = await runOps(bid, [{ op: 'mkdir', path: joinPath(base, v) }], `Created ${v}/`);
      return r ? null : 'The folder was not created';
    },
  });
}

export async function renameSel(): Promise<void> {
  const bid = D.bid;
  const e = selEntries()[0];
  if (!bid || !e || D.sel.size !== 1) return;
  await openDialog({
    title: `Rename ${e.type === 'dir' ? 'folder' : 'file'}`, ok: 'Rename', focus: '#dc-name',
    body: nameField('New name', e.name, isImage(e) && e.labels != null ? 'Its label file is renamed with it.' : ''),
    bind: (root) => {
      const i = root.querySelector<HTMLInputElement>('#dc-name');
      const dot = e.type === 'file' ? e.name.lastIndexOf('.') : -1;
      if (i) { i.focus(); i.setSelectionRange(0, dot > 0 ? dot : e.name.length); }
    },
    onOk: async (root) => {
      const v = root.querySelector<HTMLInputElement>('#dc-name')?.value.trim() ?? '';
      const bad = badName(v);
      if (bad) return bad;
      if (v === e.name) return null;
      await runOps(bid, [{ op: 'rename', path: e.path, name: v }], `Renamed ${e.name} to ${v}`);
      return null;
    },
  });
}

export async function deleteSel(): Promise<void> {
  const bid = D.bid;
  const es = selEntries();
  if (!bid || !es.length || !D.listing?.writable) return;
  const dirs = es.filter((e) => e.type === 'dir').length;
  const labels = es.filter((e) => isImage(e) && (e.labels ?? 0) > 0).length;
  await openDialog({
    title: `Delete ${describe(es)}?`, ok: 'Delete', okClass: 'danger-btn',
    body: `<p class="muted">${dirs ? `${items(dirs, 'folder')} and everything in ${dirs === 1 ? 'it' : 'them'}. ` : ''}` +
      `${labels ? `The label files of ${items(labels, 'image')} go with them. ` : ''}They go to the trash: Undo brings them back.</p>` +
      (es.length <= 6 ? `<ul class="dc-plist">${es.map((e) => `<li>${icon(e.type === 'dir' ? 'folder' : 'file')}<code>${esc(e.path)}</code></li>`).join('')}</ul>` : ''),
    onOk: async () => {
      await runOps(bid, es.map((e) => ({ op: 'delete', path: e.path })), `Deleted ${describe(es)}`);
      return null;
    },
  });
}

/** Move / copy paths into folder `to` of the same bucket (drag and drop, the picker, the AI plan preview). */
export async function moveTo(bid: string, paths: string[], to: string, kind: 'move' | 'copy'): Promise<void> {
  const src = paths.filter((p) => p !== to && !to.startsWith(`${p}/`));
  if (!src.length) { toast('A folder cannot go into itself', 'err'); return; }
  const n = src.length === 1 && src[0] ? baseName(src[0]) : items(src.length);
  await runOps(bid, src.map((p) => ({ op: kind, path: p, to })), `${kind === 'move' ? 'Moved' : 'Copied'} ${n} to ${to ? `${to}/` : 'the bucket root'}`);
}

export const moveSel = (): Promise<void> => pickAndRun('move');
export const copySel = (): Promise<void> => pickAndRun('copy');

/** The folder picker: the bucket's folders (listed on demand), plus an optional new subfolder. */
async function pickAndRun(kind: 'move' | 'copy'): Promise<void> {
  const bid = D.bid;
  const es = selEntries();
  if (!bid || !es.length) return;
  const b = bucketOf(bid);
  const kids = new Map<string, Entry[] | 'loading'>();
  const open = new Set<string>(['']);
  let dest = parentOf(es[0]?.path ?? '') === D.path ? D.path : '';
  let root: HTMLElement | null = null;
  const moving = new Set(es.map((e) => e.path));
  const rowsOf = (path: string, depth: number): string => {
    const k = kids.get(path);
    if (k === undefined) { void load(path); return `<div class="pk-row" style="--d:${depth}"><span class="spin sm"></span></div>`; }
    if (k === 'loading') return `<div class="pk-row" style="--d:${depth}"><span class="spin sm"></span></div>`;
    return k.filter((e) => e.type === 'dir').map((e) => {
      const off = moving.has(e.path);
      const o = open.has(e.path);
      return `<button type="button" class="pk-row${dest === e.path ? ' sel' : ''}" style="--d:${depth}" data-pk="${esc(e.path)}"${off ? ' disabled' : ''}>` +
        `<span class="tw" data-tw="${esc(e.path)}">${icon(o ? 'chev-d' : 'chev-r')}</span>${icon('folder')}<span class="nm">${esc(e.name)}</span></button>` +
        (o && !off ? rowsOf(e.path, depth + 1) : '');
    }).join('');
  };
  const paint = (): void => {
    const t = root?.querySelector('#dc-pk');
    if (t) t.innerHTML = `<button type="button" class="pk-row${dest === '' ? ' sel' : ''}" style="--d:0" data-pk="">${icon('db')}<span class="nm">${esc(b?.name ?? 'bucket')}</span><span class="faint">root</span></button>${rowsOf('', 1)}`;
    const d = root?.querySelector('#dc-pk-dest');
    if (d) d.textContent = `${b?.name ?? ''}/${dest}${dest ? '/' : ''}`;
  };
  const load = async (path: string): Promise<void> => {
    kids.set(path, 'loading');
    try { kids.set(path, (await api.ls(bid, path, 0, 1000, 'name')).entries); } catch (e) { kids.set(path, []); }
    paint();
  };
  const verb = kind === 'move' ? 'Move' : 'Copy';
  await openDialog({
    title: `${verb} ${describe(es)}`, ok: `${verb} here`, wide: true,
    body: `<div class="dc-pk" id="dc-pk" role="tree" aria-label="Destination folder"></div>` +
      `<div class="dc-pk-foot"><span class="muted">To</span><code id="dc-pk-dest"></code></div>` +
      nameField('Into a new folder (optional)', '', es.some((e) => isImage(e) && e.labels != null)
        ? 'YOLO label files move with their images (labels/… mirrors images/…).' : ''),
    bind: (r) => {
      root = r;
      paint();
      r.querySelector('#dc-pk')?.addEventListener('click', (e) => {
        const t = e.target instanceof Element ? e.target : null;
        const tw = t?.closest<HTMLElement>('[data-tw]');
        const row = t?.closest<HTMLElement>('[data-pk]');
        if (tw) { const p = tw.dataset['tw'] ?? ''; if (open.has(p)) open.delete(p); else open.add(p); }
        if (row && !tw) dest = row.dataset['pk'] ?? '';
        paint();
      });
    },
    focus: '#dc-pk .pk-row.sel',
    onOk: async (r) => {
      const sub = r.querySelector<HTMLInputElement>('#dc-name')?.value.trim() ?? '';
      if (sub && badName(sub)) return badName(sub);
      let to = dest;
      if (sub) {
        to = joinPath(dest, sub);
        try { await api.ops(bid, [{ op: 'mkdir', path: to }]); } catch (e) { return `Could not create ${sub}/: ${errText(e)}`; }
      }
      if (es.every((e) => parentOf(e.path) === to) && kind === 'move') return 'They are already in that folder';
      await moveTo(bid, es.map((e) => e.path), to, kind);
      return null;
    },
  });
}

// ---------------------------------------------------------------- the bucket itself

async function renameBucket(bid: string): Promise<void> {
  const b = bucketOf(bid);
  if (!b) return;
  await openDialog({
    title: 'Rename bucket', ok: 'Rename', focus: '#dc-name', body: nameField('Name', b.name),
    onOk: async (root) => {
      const v = root.querySelector<HTMLInputElement>('#dc-name')?.value.trim() ?? '';
      if (!v) return 'Enter a name';
      try { await api.renameBucket(bid, v); } catch (e) { return errText(e); }
      await hooks.reload();
      return null;
    },
  });
}

async function deleteBucket(bid: string): Promise<void> {
  const b = bucketOf(bid);
  if (!b) return;
  const managed = b.kind === 'managed';
  await openDialog({
    title: managed ? `Delete ${b.name}?` : `Unlink ${b.name}?`, ok: managed ? 'Delete' : 'Unlink', okClass: 'danger-btn',
    body: managed
      ? `<p class="muted">The bucket and its files under the managed store are removed. Sessions made from it keep working until their data is needed again.</p>` +
        `<label class="check"><input type="checkbox" id="dc-delfiles" checked> Also delete the ${esc(fmtN(b.stats?.images ?? null))} files</label>`
      : `<p class="muted">Only the link goes: the ${esc(SOURCE[b.kind]?.[1] ?? '')} source <code>${esc(b.root)}</code> is not touched.</p>`,
    onOk: async (root) => {
      const files = managed && !!root.querySelector<HTMLInputElement>('#dc-delfiles')?.checked;
      try { await api.deleteBucket(bid, files); } catch (e) { return errText(e); }
      D.focus = null;
      toast(`${managed ? 'Deleted' : 'Unlinked'} ${b.name}`, 'ok');
      if (D.bid === bid) hooks.navigate(null, '');
      await hooks.reload();
      return null;
    },
  });
}

export const OPS_ACTIONS: Record<string, Action> = {
  'rename-bucket': (el) => { void renameBucket(el.dataset['bid'] ?? ''); },
  'delete-bucket': (el) => { void deleteBucket(el.dataset['bid'] ?? ''); },
};
