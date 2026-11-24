// Change marks (bucket/changes.py): a file the Bucket page, Visualize (a label fix), a labeling export or a tool
// outside the app changed since it was downloaded / marked seen carries `changed` in ls; a folder carries the count
// of changed files below it. Shown as a dot on tiles, list rows and tree rows; the folder toolbar's "N changed"
// opens the list (newest first, a click opens the file's folder) with Mark as seen; right-click › Mark as seen.

import { errText } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { closeFloat, floatPopover } from '../core/popover.js';
import { toast } from '../core/toast.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { busy } from './busy.js';
import { D, hooks, parentOf, selEntries } from './state.js';
import type { Change, ChangeMark, Entry } from '../types-bucket.js';

const WHAT: Record<string, string> = { edited: 'edited', created: 'new', copied: 'copied here', moved: 'moved here', deleted: 'deleted',
  uploaded: 'uploaded', external: 'changed outside the app' };
const WHO: Record<string, string> = { bucket: 'on the Bucket page', visual: 'in Visualize', label: 'by a labeling export', external: '' };

const ago = (iso: string | null): string => {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

export function markTitle(m: ChangeMark, dir: boolean): string {
  const own = m.self ? `${WHAT[m.kind] ?? m.kind} ${WHO[m.source] ?? ''}`.trim() : '';
  const below = dir ? `${m.n - (m.self ? 1 : 0)} changed inside` : '';
  return [own, dir && m.n - (m.self ? 1 : 0) > 0 ? below : '', ago(m.at)].filter(Boolean).join(' · ');
}

/** The dot of one entry ('' when nothing changed): a folder shows its count. */
export function changeDot(e: Entry): string {
  const m = e.changed;
  if (!m) return '';
  const dir = e.type === 'dir';
  const n = dir ? m.n : 0;
  return `<span class="bk-chg k-${esc(m.kind)} s-${esc(m.source)}" title="${esc(markTitle(m, dir))}" aria-label="${esc(markTitle(m, dir))}">` +
    `<i aria-hidden="true"></i>${n > 0 ? `<span class="num">${n > 999 ? '999+' : n}</span>` : ''}</span>`;
}

/** The folder toolbar's "N changed" button ('' when none). */
export function changesBtn(): string {
  const n = D.listing?.changes ?? 0;
  if (!n) return '';
  return `<button type="button" class="bk-dd bk-chg-btn" data-act="changes-menu" aria-haspopup="dialog" aria-expanded="false" title="What changed in this folder">` +
    `<span class="bk-chg"><i aria-hidden="true"></i></span><span class="num">${n > 9999 ? '9999+' : n}</span><span class="bk-wide-txt">changed</span>${icon('chev-d')}</button>`;
}

function listHTML(items: Change[], total: number): string {
  if (!items.length) return '<p class="muted">Nothing changed here.</p>';
  return `<ul class="bk-chg-list">${items.map((c) => `<li><button type="button" class="ghost" data-act="changes-go" data-path="${esc(c.path)}">` +
    `<span class="bk-chg k-${esc(c.kind)}"><i aria-hidden="true"></i></span><code>${esc(c.path)}</code>` +
    `<span class="faint">${esc(`${WHAT[c.kind] ?? c.kind} ${WHO[c.source] ?? ''}`.trim())} · ${esc(ago(c.at))}</span></button></li>`).join('')}</ul>` +
    (total > items.length ? `<p class="faint">and ${total - items.length} more</p>` : '');
}

async function openList(anchor: HTMLElement): Promise<void> {
  const bid = D.bid;
  if (!bid) return;
  let r: { total: number; items: Change[] };
  try { r = await busy('Reading the changes', api.changes(bid, D.path, 0, 200)); } catch (e) { toast(`Could not read the changes: ${errText(e)}`, 'err'); return; }
  const m = document.getElementById('bk-menu');
  if (!m) return;
  m.innerHTML = `<div class="bk-chg-pop"><div class="row"><b>${r.total} changed</b><span class="grow"></span>` +
    `<button type="button" class="sm" data-act="changes-seen" data-path="${esc(D.path)}" data-all="1">${icon('check')}Mark all as seen</button></div>` +
    `${listHTML(r.items, r.total)}<p class="hint">Since they were downloaded or last marked seen. Upload to S3 clears them too.</p></div>`;
  floatPopover(m, anchor, undefined, 'left');
}

async function seen(paths: string[] | null, prefix: string | null): Promise<void> {
  const bid = D.bid;
  if (!bid) return;
  try {
    const r = await api.clearChanges(bid, paths, prefix);
    toast(r.cleared ? `Marked ${r.cleared} as seen` : 'Nothing to mark', 'ok');
  } catch (e) { toast(`Not marked: ${errText(e)}`, 'err'); return; }
  closeFloat();
  await hooks.reload();
}

export const CHANGES_ACTIONS: Record<string, Action> = {
  'changes-menu': (el) => { void openList(el); },
  'changes-go': (el) => { closeFloat(); const p = el.dataset['path'] ?? ''; hooks.navigate(D.bid, parentOf(p)); },
  'changes-seen': (el) => {
    if (el.dataset['all']) { void seen(null, el.dataset['path'] ?? ''); return; }
    const ps = el.dataset['paths'] !== undefined ? el.dataset['paths'].split('\n') : selEntries().map((e) => e.path);
    void seen(ps, null);
  },
};
