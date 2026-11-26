// Right-click menus, as in a desktop file manager. One `contextmenu` listener on the page picks the menu from what
// was clicked: a file / folder of the open folder (it joins the selection, or becomes it), the empty space of the
// folder (the selection is cleared), a tree row (bucket or folder), a bucket card / list row of the overview, or the
// overview's empty space. The items are the page's normal data-act buttons (run by app.ts's delegated click
// listener) plus a few ctx-* actions below, with the same rules as the toolbar (read-only buckets: disabled, with
// why). Like a desktop menu, the common actions stay at the top (Open; Cut / Copy / Paste / Rename) and the rest sit
// in sub-menus: Organize, Send to, Export, Cloud (S3), Tools (core/menu.ts: hover, arrow keys, flipped at the edge).
// The menu opens at the pointer (keyboard: the ContextMenu key / Shift+F10, under the focused item), closes after an
// item ran, on Escape (an open sub-menu first) or a click outside. Inputs, the assistant and the file editor keep the
// browser menu.

import { zoneOn } from '../core/auth.js';
import { esc } from '../core/dom.js';
import { bindMenu, item, menuHtml, sub } from '../core/menu.js';
import { closeFloat, floatPopover } from '../core/popover.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { openEntry, selChanged } from './browser.js';
import { clip } from './clip.js';
import { canPush } from './push.js';
import { fileOpen } from './editor.js';
import { downloadZip } from './export.js';
import { downloadPaths, streamed } from './localcopy.js';
import { baseName, bucketOf, curBucket, D, fmtN, isImage, key, parentOf, selEntries, writable } from './state.js';
import { openReformat } from './reformat.js';
import { openTagEditor } from './tags.js';
import type { Bucket, Entry } from '../types-bucket.js';

const M = {
  open: false,
  /** the element right-clicked: where a follow-up popover (the tag editor) opens */
  at: null as HTMLElement | null,
};

/** Why a change is not possible in the open folder ('' when it is). */
function readOnlyWhy(): string {
  if (writable()) return '';
  return streamed(curBucket()) ? 'Streamed from S3: download this folder to local to change it' : 'This bucket is read-only';
}

const clipWhat = (): string => { const c = clip(); return !c ? '' : c.paths.length === 1 ? `“${baseName(c.paths[0] ?? '') || 'bucket'}”` : `${c.paths.length} items`; };
/** a tree row: paste needs a writable bucket (a streamed link: inside its downloaded folders, checked by the server) */
const pasteWhy = (b: Bucket): string => (b.writable || streamed(b) ? '' : 'This bucket is read-only');

const sendItems = (bid: string, paths: string[]): string[] => {
  const data = { bid, paths: paths.join('\n') };
  return [...(zoneOn('visualizer') ? [item({ act: 'send', ico: 'scatter', label: 'Visualize', data: { ...data, target: 'visualize' } })] : []),
    ...(zoneOn('labeling') ? [item({ act: 'send', ico: 'edit', label: 'Label', data: { ...data, target: 'label' } })] : [])];
};
/** Send to ▸ : Visualize / Label, and for one folder Create session… (both linked, with the format check) */
const sendSub = (bid: string, paths: string[], folder: string | null): string => sub({ ico: 'send', label: 'Send to', groups: [
  sendItems(bid, paths),
  [folder !== null && (zoneOn('visualizer') || zoneOn('labeling')) &&
    item({ act: 'create-session', ico: 'plus', label: 'Create session…', hint: 'Visualize + Label', data: { bid, path: folder } })],
] });
const autolabelItem = (bid: string, path: string): string | false =>
  zoneOn('labeling') && item({ act: 'autolabel', ico: 'robot', label: 'Label with model…', data: { bid, path } });
const reformatItem = (bid: string, path: string): string =>
  item({ act: 'reformat', ico: 'layers', label: 'Re-format to…', hint: 'YOLO, COCO…', data: { bid, path } });

// ---------------------------------------------------------------- the menus

function entryMenu(): string {
  const bid = D.bid ?? '';
  const es = selEntries();
  const n = es.length;
  const e = n === 1 ? es[0] : undefined;
  const dir = e?.type === 'dir' ? e : undefined;
  const ro = readOnlyWhy();
  const paths = es.map((x) => x.path);
  const b = curBucket();
  const s3 = streamed(b) && !D.listing?.offline;
  const notLocal = es.filter((x) => !x.local || x.local === 'new' || x.local === 'partial');
  const local = es.filter((x) => x.local && x.local !== 'new' && x.local !== 'partial' && x.local !== 'fetching');
  return (n > 1 ? `<div class="bk-menu-h">${fmtN(n)} selected</div>` : '') + menuHtml([
    [e && item({ act: 'ctx-open', ico: e.type === 'dir' ? 'folder' : 'eye', label: 'Open', kbd: 'dbl-click' }),
      e && e.type === 'file' && (isImage(e) || e.kind === 'video') && item({ act: 'ctx-newtab', ico: 'share', label: 'Open in a new tab', data: { path: e.path } })],
    [item({ act: 'clip-cut', ico: 'scissors', label: 'Cut', kbd: 'Ctrl+X', dis: ro }),
      item({ act: 'clip-copy', ico: 'copy', label: 'Copy', kbd: 'Ctrl+C' }),
      dir && clip() && item({ act: 'clip-paste', ico: 'paste', label: `Paste into ${dir.name}`, kbd: 'Ctrl+V', data: { bid, path: dir.path }, dis: ro }),
      item({ act: 'rename', ico: 'text', label: 'Rename…', kbd: 'F2', dis: ro || (n !== 1 && 'Pick one item to rename') })],
    [sub({ ico: 'folder', label: 'Organize', groups: [
      [item({ act: 'move', ico: 'move', label: 'Move to…', dis: ro }), item({ act: 'copy', ico: 'copy', label: 'Copy to…', dis: ro })],
      [item({ act: 'ctx-tag', ico: 'tag', label: 'Tags…', data: { paths: paths.join('\n') } }),
        es.some((x) => x.changed) && item({ act: 'changes-seen', ico: 'check', label: 'Mark as seen', hint: 'clear the change mark', data: { paths: es.filter((x) => x.changed).map((x) => x.path).join('\n') } })],
    ] }),
    sendSub(bid, paths, dir ? dir.path : null),
    sub({ ico: 'download', label: 'Export', groups: [
      [e && item({ act: 'ctx-zip', ico: 'download', label: 'Download (zip)', data: { path: e.path } }),
        e && item({ act: 'export-to', ico: 'upload', label: 'Export to…', hint: 'S3, FTP, SFTP', data: { path: e.path } })],
      [dir && reformatItem(bid, dir.path)],
    ] }),
    b?.kind === 's3' && !D.listing?.offline && sub({ ico: 'cloud', label: 'Cloud (S3)', groups: [
      [s3 && notLocal.length > 0 && item({ act: 'dl-local', ico: 'hdd', label: 'Download to local', hint: 'editable copy' }),
        canPush(es) && item({ act: 'push', ico: 'cloud-up', label: 'Upload to S3', hint: 'same keys', data: { bid, paths: paths.join('\n') } })],
      [s3 && local.length > 0 && item({ act: 'check-local', ico: 'refresh', label: 'Check for updates' }),
        e && local.length > 0 && item({ act: 'compare-cloud', ico: 'cloud', label: 'Compare with cloud…', data: { path: e.path } })],
      [s3 && local.length > 0 && item({ act: 'drop-local', ico: 'x-c', label: 'Remove local copy' })],
    ] }),
    sub({ ico: 'robot', label: 'Tools', groups: [
      [es.some((x) => x.kind === 'video') && item({ act: 'extract-frames', ico: 'film', label: 'Extract frames…' }),
        dir && autolabelItem(bid, dir.path)],
    ] })],
    [item({ act: 'copy-path', ico: 'link', label: n > 1 ? 'Copy paths' : 'Copy path', data: { bid, paths: paths.join('\n') } })],
    [item({ act: 'delete', ico: 'trash', label: 'Delete', kbd: 'Del', danger: true, dis: ro })],
  ]);
}

function folderMenu(): string {
  const bid = D.bid ?? '';
  const ro = readOnlyWhy();
  const L = D.listing;
  const b = curBucket();
  const s3 = streamed(b) && !L?.offline;
  const here = L?.here;
  const videos = (L?.entries ?? []).some((x) => x.kind === 'video');
  return menuHtml([
    [sub({ ico: 'plus', label: 'New', groups: [
      [item({ act: 'mkdir', ico: 'folder-plus', label: 'Folder…', dis: ro }), item({ act: 'mkfile', ico: 'file-plus', label: 'File…', dis: ro })],
      [item({ act: 'upload', ico: 'upload', label: 'Upload files…', dis: ro })],
    ] }),
    clip() && item({ act: 'clip-paste', ico: 'paste', label: `Paste ${clipWhat()}`, kbd: 'Ctrl+V', dis: ro }),
    L && L.entries.length > 0 && item({ act: 'selall', ico: 'check', label: 'Select all', kbd: 'Ctrl+A' }),
    item({ act: 'refresh', ico: 'refresh', label: 'Refresh' })],
    [sub({ ico: 'grid', label: 'View', groups: [
      [item({ act: 'view', ico: 'grid', label: 'Thumbnails', data: { mode: 'thumbs' }, checked: D.view === 'thumbs' }),
        item({ act: 'view', ico: 'list', label: 'Names only', data: { mode: 'names' }, checked: D.view === 'names' })],
      [item({ act: 'sort', ico: 'sort', label: 'Sort by name', data: { sort: 'name' }, checked: D.sort === 'name' }),
        item({ act: 'sort', ico: 'sort', label: 'Newest first', data: { sort: '-mtime' }, checked: D.sort === '-mtime' }),
        item({ act: 'sort', ico: 'sort', label: 'Largest first', data: { sort: 'size' }, checked: D.sort === 'size' })],
    ] }),
    item({ act: 'ctx-tag', ico: 'tag', label: 'Tags…', data: { paths: D.path } }),
    sendSub(bid, [D.path], D.path),
    sub({ ico: 'download', label: 'Export', groups: [
      [item({ act: 'export-zip', ico: 'download', label: 'Download (zip)' }),
        item({ act: 'export-to', ico: 'upload', label: 'Export to…', hint: 'S3, FTP, SFTP' })],
      [reformatItem(bid, D.path)],
    ] }),
    b?.kind === 's3' && !L?.offline && sub({ ico: 'cloud', label: 'Cloud (S3)', groups: [
      s3 && here ? [item({ act: 'update-local', ico: 'download', label: 'Update from S3…' }), item({ act: 'check-local', ico: 'refresh', label: 'Check for updates' }),
        item({ act: 'compare-cloud', ico: 'cloud', label: 'Compare with cloud…', hint: 'every file' })]
        : [s3 && D.path ? item({ act: 'ctx-dl', ico: 'hdd', label: 'Download this folder to local', data: { bid, paths: D.path }, hint: 'editable copy' }) : null],
      [(here || !s3) && item({ act: 'push', ico: 'cloud-up', label: 'Upload this folder to S3', hint: 'same keys', data: { bid, paths: D.path } })],
    ] }),
    sub({ ico: 'robot', label: 'Tools', groups: [
      [autolabelItem(bid, D.path), videos && item({ act: 'extract-frames', ico: 'film', label: 'Extract frames…', hint: 'the videos here', data: { bid, paths: D.path } })],
      [item({ act: 'redetect', ico: 'refresh', label: 'Re-detect format', data: { bid, path: D.path } })],
    ] })],
    [item({ act: 'copy-path', ico: 'link', label: 'Copy path', data: { bid, path: D.path } })],
  ]);
}

function bucketMenu(b: Bucket): string {
  const s3 = streamed(b);
  return `<div class="bk-menu-h">${esc(b.name)}</div>` + menuHtml([
    [item({ act: 'go', ico: 'folder', label: 'Open', data: { bid: b.id, path: '' } }),
      item({ act: 'refresh', ico: 'refresh', label: 'Refresh' })],
    [sendSub(b.id, [''], ''),
      s3 && sub({ ico: 'cloud', label: 'Cloud (S3)', groups: [
        [item({ act: 'edit-bucket', ico: 'hdd', label: 'Load the whole bucket into local…', data: { bid: b.id }, hint: 'load mode' })],
      ] }),
      sub({ ico: 'robot', label: 'Tools', groups: [
        [autolabelItem(b.id, ''), item({ act: 'redetect', ico: 'refresh', label: 'Re-detect format', data: { bid: b.id, path: '' } })],
      ] })],
    [item({ act: 'copy-path', ico: 'link', label: 'Copy path', data: { bid: b.id, path: '' } })],
    [item({ act: 'edit-bucket', ico: 'edit', label: 'Edit…', data: { bid: b.id }, hint: b.kind === 's3' ? 'name, tags, load mode' : 'name, tags' }),
      item({ act: 'rename-bucket', ico: 'text', label: 'Rename…', data: { bid: b.id } })],
    [item({ act: 'delete-bucket', ico: 'trash', label: b.kind === 'managed' ? 'Delete…' : 'Unlink…', data: { bid: b.id }, danger: true })],
  ]);
}

/** A folder row of the tree (bucket `b`, folder `path`). */
function treeFolderMenu(b: Bucket, path: string): string {
  const e = (() => { const ch = D.children.get(key(b.id, parentOf(path))); return Array.isArray(ch) ? ch.find((x: Entry) => x.path === path) : undefined; })();
  const s3 = streamed(b);
  const here = b.id === D.bid && path === D.path;
  const ro = here ? readOnlyWhy() : 'Open the folder first';
  return menuHtml([
    [item({ act: 'go', ico: 'folder', label: 'Open', data: { bid: b.id, path } }),
      item({ act: 'refresh', ico: 'refresh', label: 'Refresh' })],
    [here && sub({ ico: 'plus', label: 'New', groups: [
      [item({ act: 'mkdir', ico: 'folder-plus', label: 'Folder…', dis: ro }), item({ act: 'mkfile', ico: 'file-plus', label: 'File…', dis: ro })],
    ] }),
    clip() && item({ act: 'clip-paste', ico: 'paste', label: `Paste ${clipWhat()} here`, data: { bid: b.id, path }, dis: pasteWhy(b) }),
    item({ act: 'ctx-tag', ico: 'tag', label: 'Tags…', data: { bid: b.id, paths: path } }),
    sendSub(b.id, [path], path),
    sub({ ico: 'download', label: 'Export', groups: [
      [item({ act: 'ctx-zip', ico: 'download', label: 'Download (zip)', data: { bid: b.id, path } })],
      [reformatItem(b.id, path)],
    ] }),
    s3 && (!e?.local || e.local === 'new' || e.local === 'partial') && sub({ ico: 'cloud', label: 'Cloud (S3)', groups: [
      [item({ act: 'ctx-dl', ico: 'hdd', label: 'Download to local', data: { bid: b.id, paths: path }, hint: 'editable copy' })],
    ] })],
    [item({ act: 'copy-path', ico: 'link', label: 'Copy path', data: { bid: b.id, path } })],
  ]);
}

const overviewMenu = (): string => menuHtml([
  [item({ act: 'refresh', ico: 'refresh', label: 'Refresh' })],
  [item({ act: 'import', ico: 'upload', label: 'Import data…' }), item({ act: 'link', ico: 'link', label: 'Link source…' }),
    item({ act: 'import-web', ico: 'globe', label: 'From the web…' })],
]);

// ---------------------------------------------------------------- which menu

function pick(t: Element): { html: string; el: HTMLElement } | null {
  if (t.closest('input, textarea, select, [contenteditable="true"], .bk-drawer, .bk-pop, .bk-split, .ui-dlg-wrap')) return null;
  const ent = t.closest<HTMLElement>('#bk-grid [data-act="pick"][data-path]');
  if (ent && D.bid && D.listing && !D.search) {
    const p = ent.dataset['path'] ?? '';
    if (!D.sel.has(p)) { D.sel.clear(); D.sel.add(p); D.anchor = p; selChanged(); }
    return { html: entryMenu(), el: ent };
  }
  const tr = t.closest<HTMLElement>('#bk-tree .t-row[data-bid]');
  if (tr) {
    const b = bucketOf(tr.dataset['bid'] ?? '');
    if (!b) return tr.classList.contains('t-all') ? { html: overviewMenu(), el: tr } : null;
    const path = tr.dataset['path'] ?? '';
    return { html: path ? treeFolderMenu(b, path) : bucketMenu(b), el: tr };
  }
  const card = t.closest<HTMLElement>('#bk-body [data-act="focus-bucket"][data-bid]');
  if (card) {
    const b = bucketOf(card.dataset['bid'] ?? '');
    return b ? { html: bucketMenu(b), el: card } : null;
  }
  const body = t.closest<HTMLElement>('#bk-body');
  if (!body || D.search) return null;
  if (!D.bid) return { html: overviewMenu(), el: body };
  if (!D.listing) return null;
  if (D.sel.size) { D.sel.clear(); D.anchor = null; selChanged(); }
  return { html: folderMenu(), el: body };
}

function show(html: string, el: HTMLElement, at: { x: number; y: number }): void {
  const m = document.getElementById('bk-menu');
  if (!m || !html) return;
  const ctl = bindMenu(m);
  ctl.reset();
  m.innerHTML = html;
  M.at = el;
  M.open = true;
  floatPopover(m, el, () => { M.open = false; ctl.reset(); }, 'left', at, () => ctl.closeDeepest());
  m.querySelector<HTMLElement>(':scope > button:not([disabled]), :scope > .mi-sub > button')?.focus();
}

/** Bind the right-click menus (once, after the page skeleton is in place). */
export function bindCtxMenu(): void {
  const app = document.getElementById('bk-app');
  const m = document.getElementById('bk-menu');
  if (!app || !m) return;
  app.addEventListener('contextmenu', (e) => {
    if (fileOpen() || !(e.target instanceof Element)) return;
    const hit = pick(e.target);
    if (!hit) return;
    e.preventDefault();
    let at = { x: e.clientX, y: e.clientY };
    if (!e.clientX && !e.clientY) {                 // the ContextMenu key / Shift+F10: under the focused item
      const r = (document.activeElement instanceof HTMLElement && hit.el.contains(document.activeElement) ? document.activeElement : hit.el).getBoundingClientRect();
      at = { x: r.left + 8, y: Math.min(r.bottom, r.top + 32) };
    }
    show(hit.html, hit.el, at);
  });
  // an item ran: close the menu (after app.ts's click handler; an item that opened its own popover closed it already).
  // A sub-menu row (data-sub) only opens its sub-menu; arrow keys, hover and Escape are core/menu.ts's (bindMenu).
  m.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest('button[data-act]') : null;
    if (M.open && b && !(b as HTMLButtonElement).disabled) setTimeout(() => { if (M.open) closeFloat(); }, 0);
  });
}

const anchor = (): HTMLElement => (M.at && document.contains(M.at) ? M.at : document.getElementById('bk-bar') ?? document.body);
const pathsOf = (el: HTMLElement): string[] => (el.dataset['paths'] !== undefined ? el.dataset['paths'].split('\n') : [...D.sel]);

export const CTX_ACTIONS: Record<string, Action> = {
  'create-session': (el) => {
    const bid = el.dataset['bid'] || D.bid;
    if (bid) void import('../views/session-new.js').then((m) => m.openCreateSession({ bucket: bid, path: el.dataset['path'] ?? '' }));
  },
  reformat: (el) => { const bid = el.dataset['bid'] || D.bid; if (bid) void openReformat(bid, el.dataset['path'] ?? ''); },
  'ctx-open': () => { const e = selEntries()[0]; if (e) void openEntry(e); },
  'ctx-newtab': (el) => { if (D.bid) window.open(api.fileUrl(D.bid, el.dataset['path'] ?? ''), '_blank', 'noopener'); },
  'ctx-tag': (el) => { const bid = el.dataset['bid'] || D.bid; if (bid) openTagEditor(anchor(), bid, pathsOf(el)); },
  'ctx-zip': (el) => { const bid = el.dataset['bid'] || D.bid; if (bid) void downloadZip(bid, el.dataset['path'] ?? ''); },
  'ctx-dl': (el) => { const bid = el.dataset['bid'] || D.bid; if (bid) void downloadPaths(bid, pathsOf(el)); },
};
