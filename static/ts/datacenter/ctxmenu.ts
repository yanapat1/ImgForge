// Right-click menus, as in a desktop file manager. One `contextmenu` listener on the page picks the menu from what
// was clicked: a file / folder of the open folder (it joins the selection, or becomes it), the empty space of the
// folder (the selection is cleared), a tree row (bucket or folder), a bucket card / list row of the overview, or the
// overview's empty space. The items are the page's normal data-act buttons (run by app.ts's delegated click
// listener) plus a few ctx-* actions below, with the same rules as the toolbar (read-only buckets: disabled, with
// why). The menu opens at the pointer (keyboard: the ContextMenu key / Shift+F10, under the focused item), closes
// after an item ran, on Escape or a click outside. Inputs, the assistant and the file editor keep the browser menu.

import { zoneOn } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import { closeFloat, floatPopover } from '../core/popover.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { openEntry, selChanged } from './browser.js';
import { bucketMenuItems } from './details.js';
import { fileOpen } from './editor.js';
import { downloadZip } from './export.js';
import { downloadPaths, streamed } from './localcopy.js';
import { bucketOf, curBucket, D, fmtN, isImage, key, parentOf, selEntries, writable } from './state.js';
import { openTagEditor } from './tags.js';
import type { Bucket, Entry } from '../types-datacenter.js';

const M = {
  open: false,
  /** the element right-clicked: where a follow-up popover (the tag editor) opens */
  at: null as HTMLElement | null,
};

interface Item { act: string; ico: string; label: string; data?: Record<string, string>; dis?: string | false;
  kbd?: string; hint?: string; danger?: boolean; checked?: boolean }

const SEP = '<div class="sep" role="separator"></div>';

function item(o: Item): string {
  const data = Object.entries(o.data ?? {}).map(([k, v]) => ` data-${k}="${esc(v)}"`).join('');
  const role = o.checked === undefined ? 'menuitem' : 'menuitemradio';
  const ico = o.checked === undefined ? icon(o.ico) : o.checked ? icon('check') : '<span class="ico"></span>';
  return `<button type="button" role="${role}"${o.checked !== undefined ? ` aria-checked="${o.checked}"` : ''} data-act="${o.act}"${data}` +
    `${o.danger ? ' class="danger"' : ''}${o.dis ? ` disabled title="${esc(o.dis)}"` : ''}>${ico}${esc(o.label)}` +
    `${o.kbd ? `<span class="kbd">${esc(o.kbd)}</span>` : o.hint ? `<span class="faint">${esc(o.hint)}</span>` : ''}</button>`;
}
const menuHtml = (groups: Array<Array<string | false | null | undefined>>): string =>
  groups.map((g) => g.filter(Boolean).join('')).filter(Boolean).join(SEP);

/** Why a change is not possible in the open folder ('' when it is). */
function readOnlyWhy(): string {
  if (writable()) return '';
  return streamed(curBucket()) ? 'Streamed from S3: download this folder to local to change it' : 'This bucket is read-only';
}

const sendItems = (bid: string, paths: string[]): string[] => {
  const data = { bid, paths: paths.join('\n') };
  return [...(zoneOn('visualizer') ? [item({ act: 'send', ico: 'scatter', label: 'Send to Visualize', data: { ...data, target: 'visualize' } })] : []),
    ...(zoneOn('labeling') ? [item({ act: 'send', ico: 'edit', label: 'Send to Label', data: { ...data, target: 'label' } })] : [])];
};

// ---------------------------------------------------------------- the menus

function entryMenu(): string {
  const bid = D.bid ?? '';
  const es = selEntries();
  const n = es.length;
  const e = n === 1 ? es[0] : undefined;
  const ro = readOnlyWhy();
  const paths = es.map((x) => x.path);
  const s3 = streamed(curBucket()) && !D.listing?.offline;
  const notLocal = es.filter((x) => !x.local || x.local === 'new' || x.local === 'partial');
  const local = es.filter((x) => x.local && x.local !== 'new' && x.local !== 'partial' && x.local !== 'fetching');
  return (n > 1 ? `<div class="dc-menu-h">${fmtN(n)} selected</div>` : '') + menuHtml([
    [e && item({ act: 'ctx-open', ico: e.type === 'dir' ? 'folder' : 'eye', label: 'Open', kbd: 'dbl-click' }),
      e && e.type === 'file' && (isImage(e) || e.kind === 'video') && item({ act: 'ctx-newtab', ico: 'share', label: 'Open in a new tab', data: { path: e.path } })],
    [item({ act: 'rename', ico: 'text', label: 'Rename…', kbd: 'F2', dis: ro || (n !== 1 && 'Pick one item to rename') }),
      item({ act: 'move', ico: 'move', label: 'Move to…', dis: ro }),
      item({ act: 'copy', ico: 'copy', label: 'Copy to…', dis: ro }),
      item({ act: 'ctx-tag', ico: 'tag', label: 'Tags…', data: { paths: paths.join('\n') } }),
      item({ act: 'copy-path', ico: 'link', label: n > 1 ? 'Copy paths' : 'Copy path', data: { bid, paths: paths.join('\n') } })],
    [...sendItems(bid, paths),
      es.some((x) => x.kind === 'video') && item({ act: 'extract-frames', ico: 'film', label: 'Extract frames…' }),
      e && item({ act: 'ctx-zip', ico: 'download', label: 'Export (zip)', data: { path: e.path }, hint: 'download' })],
    s3 ? [notLocal.length > 0 && item({ act: 'dl-local', ico: 'hdd', label: 'Download to local', hint: 'editable copy' }),
      local.length > 0 && item({ act: 'drop-local', ico: 'x-c', label: 'Remove local copy' })] : [],
    [item({ act: 'delete', ico: 'trash', label: 'Delete', kbd: 'Del', danger: true, dis: ro })],
  ]);
}

function folderMenu(): string {
  const bid = D.bid ?? '';
  const ro = readOnlyWhy();
  const L = D.listing;
  const s3 = streamed(curBucket()) && !L?.offline;
  const here = L?.here;
  return menuHtml([
    [item({ act: 'mkdir', ico: 'folder-plus', label: 'New folder…', dis: ro }),
      item({ act: 'upload', ico: 'upload', label: 'Upload files…', dis: ro }),
      item({ act: 'refresh', ico: 'refresh', label: 'Refresh' })],
    [L && L.entries.length > 0 && item({ act: 'selall', ico: 'check', label: 'Select all', kbd: 'Ctrl+A' }),
      item({ act: 'view', ico: 'grid', label: 'Thumbnails', data: { mode: 'thumbs' }, checked: D.view === 'thumbs' }),
      item({ act: 'view', ico: 'list', label: 'Names only', data: { mode: 'names' }, checked: D.view === 'names' })],
    [item({ act: 'sort', ico: 'sort', label: 'Sort by name', data: { sort: 'name' }, checked: D.sort === 'name' }),
      item({ act: 'sort', ico: 'sort', label: 'Newest first', data: { sort: '-mtime' }, checked: D.sort === '-mtime' }),
      item({ act: 'sort', ico: 'sort', label: 'Largest first', data: { sort: 'size' }, checked: D.sort === 'size' })],
    [item({ act: 'copy-path', ico: 'link', label: 'Copy path', data: { bid, path: D.path } }),
      item({ act: 'ctx-tag', ico: 'tag', label: 'Edit tags…', data: { paths: D.path } }),
      item({ act: 'export-zip', ico: 'download', label: 'Export (zip)', hint: 'download' }),
      item({ act: 'export-to', ico: 'upload', label: 'Export to…', hint: 'S3, FTP, SFTP' })],
    sendItems(bid, [D.path]),
    s3 ? (here
      ? [item({ act: 'update-local', ico: 'download', label: 'Update from S3…' }), item({ act: 'check-local', ico: 'refresh', label: 'Check for updates' })]
      : [D.path ? item({ act: 'ctx-dl', ico: 'hdd', label: 'Download this folder to local', data: { bid, paths: D.path }, hint: 'editable copy' }) : null]) : [],
  ]);
}

function bucketMenu(b: Bucket): string {
  const s3 = streamed(b);
  return `<div class="dc-menu-h">${esc(b.name)}</div>` + menuHtml([
    [item({ act: 'go', ico: 'folder', label: 'Open', data: { bid: b.id, path: '' } }),
      item({ act: 'refresh', ico: 'refresh', label: 'Refresh' }),
      item({ act: 'copy-path', ico: 'link', label: 'Copy path', data: { bid: b.id, path: '' } })],
    sendItems(b.id, ['']),
    [s3 ? item({ act: 'edit-bucket', ico: 'hdd', label: 'Load the whole bucket into local…', data: { bid: b.id }, hint: 'load mode' }) : null],
    [bucketMenuItems(b)],
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
      item({ act: 'refresh', ico: 'refresh', label: 'Refresh' }),
      here && item({ act: 'mkdir', ico: 'folder-plus', label: 'New folder…', dis: ro })],
    [item({ act: 'copy-path', ico: 'link', label: 'Copy path', data: { bid: b.id, path } }),
      item({ act: 'ctx-tag', ico: 'tag', label: 'Tags…', data: { bid: b.id, paths: path } }),
      item({ act: 'ctx-zip', ico: 'download', label: 'Export (zip)', data: { bid: b.id, path }, hint: 'download' })],
    sendItems(b.id, [path]),
    s3 && (!e?.local || e.local === 'new' || e.local === 'partial')
      ? [item({ act: 'ctx-dl', ico: 'hdd', label: 'Download to local', data: { bid: b.id, paths: path }, hint: 'editable copy' })] : [],
  ]);
}

const overviewMenu = (): string => menuHtml([
  [item({ act: 'refresh', ico: 'refresh', label: 'Refresh' })],
  [item({ act: 'import', ico: 'upload', label: 'Import data…' }), item({ act: 'link', ico: 'link', label: 'Link source…' }),
    item({ act: 'import-web', ico: 'globe', label: 'From the web…' })],
]);

// ---------------------------------------------------------------- which menu

function pick(t: Element): { html: string; el: HTMLElement } | null {
  if (t.closest('input, textarea, select, [contenteditable="true"], .dc-drawer, .dc-pop, .dc-split, .ui-dlg-wrap')) return null;
  const ent = t.closest<HTMLElement>('#dc-grid [data-act="pick"][data-path]');
  if (ent && D.bid && D.listing && !D.search) {
    const p = ent.dataset['path'] ?? '';
    if (!D.sel.has(p)) { D.sel.clear(); D.sel.add(p); D.anchor = p; selChanged(); }
    return { html: entryMenu(), el: ent };
  }
  const tr = t.closest<HTMLElement>('#dc-tree .t-row[data-bid]');
  if (tr) {
    const b = bucketOf(tr.dataset['bid'] ?? '');
    if (!b) return tr.classList.contains('t-all') ? { html: overviewMenu(), el: tr } : null;
    const path = tr.dataset['path'] ?? '';
    return { html: path ? treeFolderMenu(b, path) : bucketMenu(b), el: tr };
  }
  const card = t.closest<HTMLElement>('#dc-body [data-act="focus-bucket"][data-bid]');
  if (card) {
    const b = bucketOf(card.dataset['bid'] ?? '');
    return b ? { html: bucketMenu(b), el: card } : null;
  }
  const body = t.closest<HTMLElement>('#dc-body');
  if (!body || D.search) return null;
  if (!D.bid) return { html: overviewMenu(), el: body };
  if (!D.listing) return null;
  if (D.sel.size) { D.sel.clear(); D.anchor = null; selChanged(); }
  return { html: folderMenu(), el: body };
}

function show(html: string, el: HTMLElement, at: { x: number; y: number }): void {
  const m = document.getElementById('dc-menu');
  if (!m || !html) return;
  m.innerHTML = html;
  M.at = el;
  M.open = true;
  floatPopover(m, el, () => { M.open = false; }, 'left', at);
  m.querySelector<HTMLElement>('button:not([disabled])')?.focus();
}

/** Bind the right-click menus (once, after the page skeleton is in place). */
export function bindCtxMenu(): void {
  const app = document.getElementById('dc-app');
  const m = document.getElementById('dc-menu');
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
  // an item ran: close the menu (after app.ts's click handler; an item that opened its own popover closed it already)
  m.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest('button[data-act]') : null;
    if (M.open && b && !(b as HTMLButtonElement).disabled) setTimeout(() => { if (M.open) closeFloat(); }, 0);
  });
  // arrow keys move through the items
  m.addEventListener('keydown', (e) => {
    if (!M.open || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
    const items = [...m.querySelectorAll<HTMLElement>('button:not([disabled])')];
    if (!items.length) return;
    e.preventDefault();
    const i = items.indexOf(document.activeElement as HTMLElement);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  });
}

const anchor = (): HTMLElement => (M.at && document.contains(M.at) ? M.at : document.getElementById('dc-bar') ?? document.body);
const pathsOf = (el: HTMLElement): string[] => (el.dataset['paths'] !== undefined ? el.dataset['paths'].split('\n') : [...D.sel]);

export const CTX_ACTIONS: Record<string, Action> = {
  'ctx-open': () => { const e = selEntries()[0]; if (e) void openEntry(e); },
  'ctx-newtab': (el) => { if (D.bid) window.open(api.fileUrl(D.bid, el.dataset['path'] ?? ''), '_blank', 'noopener'); },
  'ctx-tag': (el) => { const bid = el.dataset['bid'] || D.bid; if (bid) openTagEditor(anchor(), bid, pathsOf(el)); },
  'ctx-zip': (el) => { const bid = el.dataset['bid'] || D.bid; if (bid) void downloadZip(bid, el.dataset['path'] ?? ''); },
  'ctx-dl': (el) => { const bid = el.dataset['bid'] || D.bid; if (bid) void downloadPaths(bid, pathsOf(el)); },
};
