// The folder view (centre pane, a bucket open): toolbar (breadcrumb, Thumbnails / Names only, sort, name filter, New
// folder, Upload, Send to…), the folder strip (counts, tags, Edit tags, the format-change banner),
// the bulk action bar, and the entries as a thumbnail grid or a names list. Big folders stay fast: the listing comes
// in pages of 500 (the next page when the scroll nears the end), thumbnails (…/file?w=256) load only when they scroll
// into view (IntersectionObserver), one innerHTML per change (a next page is appended) and one delegated listener
// for everything (app.ts). Selection: click, Ctrl / Cmd toggles, Shift selects a range, the checkbox toggles; a
// selection change only flips classes (selChanged), it never re-renders the tiles. Drag the selection onto a folder
// (a tile, a breadcrumb, a tree row) to move it (Ctrl / Alt: copy); drop files from the desktop to upload them.
// Also the header search results (renderSearch).

import { errText } from '../core/api.js';
import { rememberPref, zoneOn } from '../core/auth.js';
import { $, copyText, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { closeFloat, floatPopover } from '../core/popover.js';
import * as api from './api.js';
import { previewStarted } from './busy.js';
import type { Action } from './app.js';
import { copySel, deleteSel, moveSel, moveTo, newFolder, renameSel, undoRetag } from './ops.js';
import { downloadZip, openExportDialog } from './export.js';
import { openFile, TEXTY } from './editor.js';
import { openFrames } from './frames.js';
import { localBadge, localStrip, streamed } from './localcopy.js';
import { openSend } from './send.js';
import {
  baseName, bucketOf, curBucket, D, fbadge, fdot, fmtDate, fmtKey, fmtLabel, fmtN, fmtSize, hooks, isImage, parentOf,
  locationOf, PREF, selEntries, tagChip, writable,
} from './state.js';
import { openTagEditor } from './tags.js';
import { walk } from './upload.js';
import { queueUpload } from './uploads.js';
import type { Entry, LsSort } from '../types-bucket.js';
import type { ViewMode } from './state.js';

const THUMB_W = 256;
const SORTS: Array<[LsSort, string]> = [['name', 'Name'], ['-mtime', 'Newest first'], ['size', 'Largest first']];
/** thumbnail URLs already shown once: a re-render puts them in src at once (the browser has them cached) */
const seen = new Set<string>();
let io: IntersectionObserver | null = null;
/** a path to select once its folder is listed (a search result's "show in folder") */
let reveal: string | null = null;
/** the paths being dragged inside the page (dataTransfer is unreadable during dragover) */
let dragging: { bid: string; paths: string[] } | null = null;
/** the next listing asks S3 again at once (Retry S3 after it was unreachable) */
let retryS3 = false;
/** the next listing lists S3 again instead of the listing the server keeps for a minute (Reload) */
let refreshNext = false;
/** a streamed S3 folder still being listed: asked again shortly */
let partialTimer = 0;
/** the listing request in flight */
let listAbort: AbortController | null = null;

// ---------------------------------------------------------------- loading

/** List the open folder (first page). keep: a refresh after a change (the entries stay until the answer, as many
 *  entries as were loaded come back, the selection keeps what still exists). */
export async function loadListing(keep = false): Promise<void> {
  const bid = D.bid;
  if (!bid) return;
  const seq = ++D.seq;
  const had = D.listing && keep ? D.listing.entries.length : 0;
  if (!keep) { D.listing = null; D.listErr = null; }
  D.loading = true;
  if (!keep) renderFolder();
  clearTimeout(partialTimer);
  const retry = retryS3, refresh = refreshNext;
  retryS3 = false;
  refreshNext = false;
  listAbort?.abort();                              // a newer listing replaces it: stop the old request on the server too
  const ac = new AbortController();
  listAbort = ac;
  try {
    const r = await api.ls(bid, D.path, 0, Math.min(2000, Math.max(api.PAGE, had)), D.sort, D.nameQ, retry, refresh, ac.signal);
    if (seq !== D.seq) return;
    if (r.partial) partialTimer = window.setTimeout(() => { if (seq === D.seq) void loadListing(true); }, 1000);
    // a streamed S3 folder still being listed asks again every second: while the answer only adds entries after
    // the ones shown, append their tiles instead of re-rendering the page (focus, hover, a playing video stay)
    const prev = keep ? D.listing : null, grid = $('bk-grid');
    if (r.partial && prev && grid && !D.listErr && r.entries.length >= prev.entries.length &&
        prev.entries.every((e, i) => r.entries[i]?.path === e.path)) {
      const added = r.entries.slice(prev.entries.length);
      D.listing = r;
      D.loading = false;
      if (added.length) { grid.insertAdjacentHTML('beforeend', added.map(D.view === 'thumbs' ? tile : lrow).join('')); observeThumbs(); }
      renderStripCount();
      return;
    }
    D.listing = r;
    D.listErr = null;
    const have = new Set(r.entries.map((e) => e.path));
    for (const p of [...D.sel]) if (!have.has(p)) D.sel.delete(p);
    if (reveal && have.has(reveal)) { D.sel.clear(); D.sel.add(reveal); D.anchor = reveal; }
  } catch (e) {
    if (seq !== D.seq) return;
    D.listErr = errText(e);
  } finally {
    if (seq === D.seq) D.loading = false;
  }
  hooks.render();
  if (reveal) {
    const el = document.querySelector<HTMLElement>(`#bk-grid [data-path="${CSS.escape(reveal)}"]`);
    el?.scrollIntoView({ block: 'center' });
    reveal = null;
  }
  fillViewport();
}

/** The next page of a big folder, appended to the grid / list. */
async function loadMore(): Promise<void> {
  const L = D.listing, bid = D.bid;
  if (!L || !bid || D.more || L.entries.length >= L.total) return;
  D.more = true;
  const seq = D.seq;
  setMore();
  try {
    const r = await api.ls(bid, D.path, L.entries.length, api.PAGE, D.sort, D.nameQ);
    if (seq !== D.seq || D.listing !== L) return;
    L.entries.push(...r.entries);
    L.total = r.total;
    const grid = $('bk-grid');
    if (grid) {
      grid.insertAdjacentHTML('beforeend', r.entries.map(D.view === 'thumbs' ? tile : lrow).join(''));
      observeThumbs();
    }
    renderStripCount();
  } catch (e) {
    console.warn(`load more: ${errText(e)}`);
  } finally {
    D.more = false;
    setMore();
  }
  fillViewport();
}

/** Keep loading pages while the grid does not fill the pane (a tall screen, a names list). */
function fillViewport(): void {
  const body = $('bk-body');
  if (body && D.listing && D.listing.entries.length < D.listing.total && body.scrollHeight <= body.clientHeight + 400) void loadMore();
}

function setMore(): void {
  const el = $('bk-more');
  const L = D.listing;
  if (!el || !L) return;
  const left = L.total - L.entries.length;
  el.innerHTML = left > 0 ? (D.more ? '<span class="spin sm" aria-hidden="true"></span>Loading more…' : `<button type="button" class="ghost sm" data-act="more">${fmtN(left)} more</button>`) : '';
}

// ---------------------------------------------------------------- markup

const thumbUrl = (bid: string, path: string): string => api.fileUrl(bid, path, THUMB_W);

function labelBadge(e: Entry): string {
  if (!isImage(e) || e.labels == null) return '';
  if (e.labels === 0) return '<span class="lb miss">no label</span>';
  return `<span class="lb">${e.labels} box${e.labels === 1 ? '' : 'es'}</span>`;
}

function fileIcon(e: Entry): string {
  if (e.type === 'dir') return icon('folder');
  if (isImage(e)) return icon('file-img');
  if (e.kind === 'video') return icon('film');
  return icon('file');
}

/** A video's poster frame can be made (a local file; a bucket streamed from S3 has none). */
const posterOk = (bid: string, e?: Entry): boolean => { const b = bucketOf(bid); return !!b && (b.kind !== 's3' || !!b.local_ready || e?.local === 'fresh'); };

function tile(e: Entry): string {
  const bid = D.bid ?? '';
  const sel = D.sel.has(e.path);
  const drag = writable() ? ' draggable="true"' : '';
  const cap = e.type === 'dir'
    ? `<span class="meta">${e.items != null ? `${fmtN(e.items)} items` : 'folder'}</span>`
    : `<span class="meta">${fmtSize(e.size)}${e.w && e.h ? ` · ${e.w}×${e.h}` : ''}</span>`;
  let media: string;
  if (isImage(e) || (e.kind === 'video' && posterOk(bid, e))) {
    const u = thumbUrl(bid, e.path);
    media = (seen.has(u) ? `<img src="${esc(u)}" alt="" decoding="async">` : `<img data-src="${esc(u)}" alt="" decoding="async">`) +
      (e.kind === 'video' ? `<span class="vb" aria-hidden="true">${icon('play')}</span>` : '');
  } else media = `<span class="ph${e.type === 'dir' ? ' dir' : ''}">${fileIcon(e)}</span>`;
  return `<div class="bk-th${sel ? ' sel' : ''}${e.type === 'dir' ? ' dir' : ''}" role="option" aria-selected="${sel}" tabindex="-1" data-act="pick" data-path="${esc(e.path)}"${e.type === 'dir' ? ' data-drop="1"' : ''}${drag} title="${esc(e.name)}">` +
    `<div class="im">${media}${labelBadge(e)}${localBadge(e)}<i class="ck" data-ck="1" aria-hidden="true"></i></div>` +
    `<div class="cap"><span class="fn">${esc(e.name)}</span>${cap}</div></div>`;
}

function labelsCell(e: Entry): string {
  if (e.type === 'dir') return `<span class="muted">${e.items != null ? `${fmtN(e.items)} items` : ''}</span>`;
  if (!isImage(e) || e.labels == null) return '<span class="muted">—</span>';
  if (e.labels === 0) return `<span class="miss">${icon('alert')}no label file</span>`;
  return `<span class="ok">${icon('check')}${e.labels} box${e.labels === 1 ? '' : 'es'}</span>`;
}

function lrow(e: Entry): string {
  const sel = D.sel.has(e.path);
  const drag = writable() ? ' draggable="true"' : '';
  return `<div class="bk-lrow${sel ? ' sel' : ''}" role="row" tabindex="-1" aria-selected="${sel}" data-act="pick" data-path="${esc(e.path)}"${e.type === 'dir' ? ' data-drop="1"' : ''}${drag}>` +
    `<span class="c-ck"><i class="cb${sel ? ' on' : ''}" data-ck="1" aria-hidden="true"></i></span>` +
    `<span class="c-n${e.type === 'dir' ? ' dir' : ''}">${fileIcon(e)}<span>${esc(e.name)}${e.type === 'dir' ? '/' : ''}</span>${localBadge(e)}</span>` +
    `<span class="r num">${e.type === 'dir' ? '' : fmtSize(e.size)}</span>` +
    `<span class="r num">${e.w && e.h ? `${e.w}×${e.h}` : ''}</span>` +
    `<span class="c-l">${labelsCell(e)}</span>` +
    `<span class="c-m num">${esc(fmtDate(e.mtime))}</span></div>`;
}

function crumbs(): string {
  const b = curBucket();
  const parts = D.path ? D.path.split('/') : [];
  const out = [`${fdot(fmtKey(D.listing?.format ?? b?.format ?? null))}<button type="button" class="crumb${parts.length ? '' : ' cur'}" data-act="go" data-bid="${esc(D.bid ?? '')}" data-path="" data-drop="1">${esc(b?.name ?? D.bid ?? '')}</button>`];
  parts.forEach((p, i) => {
    const path = parts.slice(0, i + 1).join('/');
    const cur = i === parts.length - 1;
    out.push(`${icon('chev-r')}<button type="button" class="crumb${cur ? ' cur' : ''}" data-act="go" data-bid="${esc(D.bid ?? '')}" data-path="${esc(path)}" data-drop="1"${cur ? ' aria-current="page"' : ''}>${esc(p)}</button>`);
  });
  const loc = locationOf(D.bid ?? '', D.path);
  return `<nav class="bk-crumbs" aria-label="Folder">${out.join('')}</nav>` +
    `<button type="button" class="icon-btn ghost sm bk-copypath" data-act="copy-path" data-bid="${esc(D.bid ?? '')}" data-path="${esc(D.path)}" aria-label="Copy the path of this folder" title="Copy path${loc ? `: ${esc(loc)}` : ''}">${icon('link')}</button>`;
}

function renderBar(): void {
  const bar = $('bk-bar');
  if (!bar) return;
  const w = writable();
  const nm = D.nameQ ? `<span class="dot-on" aria-hidden="true"></span>` : '';
  bar.innerHTML = crumbs() + '<span class="grow"></span>' +
    `<div class="segmented bk-viewseg" role="group" aria-label="Show">` +
    `<button type="button" class="view-btn${D.view === 'thumbs' ? ' active' : ''}" data-act="view" data-mode="thumbs" aria-pressed="${D.view === 'thumbs'}" id="bk-view-thumbs">${icon('grid')}<span class="bk-wide-txt">Thumbnails</span></button>` +
    `<button type="button" class="view-btn${D.view === 'names' ? ' active' : ''}" data-act="view" data-mode="names" aria-pressed="${D.view === 'names'}" id="bk-view-names">${icon('list')}<span class="bk-wide-txt">Names only</span></button></div>` +
    `<button type="button" class="icon-btn" data-act="sort-menu" aria-haspopup="menu" aria-expanded="false" aria-label="Sort: ${esc(SORTS.find(([k]) => k === D.sort)?.[1] ?? '')}" title="Sort">${icon('sort')}</button>` +
    `<button type="button" class="bk-dd" data-act="filter-menu" aria-haspopup="dialog" aria-expanded="false" title="Filter by name">${icon('filter')}<span class="bk-wide-txt">${D.nameQ ? esc(D.nameQ) : 'Filter'}</span>${nm}${icon('chev-d')}</button>` +
    `<button type="button" class="icon-btn" data-act="refresh" aria-label="Reload this folder" title="Reload">${icon('refresh')}</button>` +
    '<span class="tb-sep" aria-hidden="true"></span>' +
    `<button type="button" class="icon-btn" data-act="mkdir" aria-label="New folder" title="New folder"${w ? '' : ' disabled'}>${icon('folder-plus')}</button>` +
    `<button type="button" class="icon-btn" data-act="upload" aria-label="Upload files here" title="Upload files here"${w ? '' : ' disabled'}>${icon('upload')}</button>` +
    `<button type="button" class="primary" data-act="send-menu" aria-haspopup="menu" aria-expanded="false">${icon('send')}<span class="bk-wide-txt">Send to…</span>${icon('chev-d')}</button>` +
    `<button type="button" class="icon-btn bk-only-overlay" data-act="show-details" aria-label="Details" title="Details">${icon('eye')}</button>`;
}

function stripCount(): string {
  const L = D.listing;
  if (!L) return '';
  const n = L.entries.length;
  return n >= L.total ? `${fmtN(L.total)} item${L.total === 1 ? '' : 's'}` : `1–${fmtN(n)} of ${fmtN(L.total)}`;
}
function renderStripCount(): void { const el = $('bk-strip-n'); if (el) el.textContent = stripCount(); }

function renderStrip(): void {
  const el = $('bk-strip');
  if (!el) return;
  const b = curBucket(), L = D.listing;
  const out: string[] = [];
  const rt = D.bid ? D.retag.get(D.bid) : undefined;
  if (rt) {
    out.push(`<div class="bk-banner" role="status">${icon('alert')}<span><b>Format: ${esc(fmtLabel(rt.to))}.</b> ` +
      `${esc(fmtLabel(rt.from))} → ${esc(fmtLabel(rt.to))}${rt.reason ? ` (${esc(rt.reason)})` : ''}${rt.path ? ` in <code>${esc(rt.path)}/</code>` : ''}.</span><span class="grow"></span>` +
      (rt.undo_id ? `<button type="button" class="sm" data-act="undo-retag">${icon('undo')}Move it back</button>` : '') +
      `<button type="button" class="ghost sm" data-act="redetect" data-path="${esc(rt.path)}">Re-detect</button>` +
      `<button type="button" class="icon-btn ghost sm" data-act="dismiss-retag" aria-label="Dismiss">${icon('x')}</button></div>`);
  }
  const loc = localStrip();
  if (loc) out.push(loc);
  if (L && b) {
    const st = D.stats.get(`${b.id}\u0000${D.path}`);
    const imgs = st && typeof st === 'object' ? `${fmtN(st.images)} img · ${fmtSize(st.size)}` : `${fmtN(L.total)} items`;
    const tags = L.tags.map((t) => tagChip(t.tag, t.auto, 'on')).join('');
    out.push(`<div class="bk-strip-row"><span class="lbl">${icon('folder')}<b class="mono">${esc(D.path ? `${baseName(D.path)}/` : `${b.name}/`)}</b><span class="num">${imgs}</span></span>` +
      `<span class="tb-sep" aria-hidden="true"></span><span class="lbl">${icon('tag')}Tags</span><span class="bk-tagrow">${tags || '<span class="faint">none</span>'}</span>` +
      `<button type="button" class="ghost sm" data-act="edit-tags" data-path="${esc(D.path)}" aria-haspopup="dialog">${icon('edit')}Edit tags</button>` +
      `<span class="grow"></span><span id="bk-strip-n" class="num faint">${stripCount()}</span></div>`);
  }
  el.innerHTML = out.join('');
  el.classList.toggle('hidden', !out.length);
}

function renderSelbar(): void {
  const el = $('bk-selbar');
  if (!el) return;
  const n = D.sel.size;
  el.classList.toggle('hidden', !n);
  if (!n) { el.innerHTML = ''; return; }
  const L = D.listing;
  const all = !!L && n >= L.entries.length;
  const w = writable();
  const dis = w ? '' : ' disabled title="This bucket is read-only"';
  el.innerHTML = `<button type="button" class="bk-cbbtn" data-act="selall" aria-label="${all ? 'Clear the selection' : 'Select all loaded'}"><i class="cb ${all ? 'on' : 'part'}" aria-hidden="true"></i></button>` +
    `<b>${fmtN(n)} selected</b>` +
    `<button type="button" class="ghost" data-act="move"${dis}>${icon('move')}<span class="bk-wide-txt">Move</span></button>` +
    `<button type="button" class="ghost" data-act="copy"${dis}>${icon('copy')}<span class="bk-wide-txt">Copy</span></button>` +
    `<button type="button" class="ghost" data-act="rename"${n === 1 && w ? '' : ' disabled'}>${icon('edit')}<span class="bk-wide-txt">Rename</span></button>` +
    `<button type="button" class="ghost" data-act="tag-sel">${icon('tag')}<span class="bk-wide-txt">Tag</span></button>` +
    (streamed(curBucket()) && !L?.offline
      ? `<button type="button" class="ghost" data-act="dl-local" title="Download an editable copy of just these: fast, readable while S3 is unreachable, changes stay local">${icon('hdd')}<span class="bk-wide-txt">Download to local</span></button>` +
        (selEntries().some((x) => x.local && x.local !== 'new')
          ? `<button type="button" class="ghost" data-act="drop-local">${icon('x-c')}<span class="bk-wide-txt">Remove local copy</span></button>` : '')
      : '') +
    `<button type="button" class="ghost danger-ghost" data-act="delete"${dis}>${icon('trash')}<span class="bk-wide-txt">Delete</span></button>` +
    `<span class="grow"></span><span class="hint bk-wide">Shift-click for a range · <kbd>Ctrl</kbd> <kbd>A</kbd> all ${L ? fmtN(L.entries.length) : ''}</span>` +
    `<button type="button" class="ghost sm" data-act="clear-sel">Clear</button>`;
}

function renderBody(): void {
  const body = $('bk-body');
  if (!body) return;
  const L = D.listing;
  if (!L) {
    body.innerHTML = D.listErr
      ? `<div class="bk-empty">${icon('alert')}<b>Could not open this folder</b><span class="muted">${esc(D.listErr)}</span><button type="button" data-act="${streamed(curBucket()) ? 'retry-s3' : 'reload-folder'}">${icon('refresh')}Retry</button></div>`
      : `<div class="bk-pad"><div class="${D.view === 'thumbs' ? 'bk-grid' : 'bk-rows'} skel" aria-busy="true">${'<div class="sk"></div>'.repeat(D.view === 'thumbs' ? 18 : 14)}</div></div>`;
    return;
  }
  if (!L.entries.length) {
    body.innerHTML = `<div class="bk-pad" data-act="desel"><div class="bk-empty">${icon(D.nameQ ? 'filter' : 'folder-o')}<b>${D.nameQ ? `Nothing named like “${esc(D.nameQ)}”` : 'This folder is empty'}</b>` +
      (writable() && !D.nameQ ? `<span class="muted">Drop files here, or</span><div class="row"><button type="button" class="primary" data-act="upload">${icon('upload')}Upload files</button><button type="button" data-act="mkdir">${icon('folder-plus')}New folder</button></div>` : '') +
      (D.nameQ ? '<button type="button" data-act="clear-nameq">Clear the filter</button>' : '') + '</div></div>';
    return;
  }
  const items = L.entries.map(D.view === 'thumbs' ? tile : lrow).join('');
  const sortCol = (k: LsSort, label: string, cls = ''): string =>
    `<button type="button" class="hcol${cls}${D.sort === k ? ' sorted' : ''}" data-act="sort" data-sort="${k}">${label}${D.sort === k ? icon('chev-d') : ''}</button>`;
  const allSel = D.sel.size > 0 && D.sel.size >= L.entries.length;
  body.innerHTML = `<div class="bk-pad" data-act="desel">` + (D.view === 'thumbs'
    ? `<div id="bk-grid" class="bk-grid" role="listbox" aria-multiselectable="true" aria-label="Files: arrow keys move, Space selects, Enter opens">${items}</div>`
    : `<div class="bk-list" role="grid" aria-multiselectable="true" aria-label="Files"><div class="bk-lrow head" role="row">` +
      `<span class="c-ck"><button type="button" class="bk-cbbtn" data-act="selall" aria-label="Select all"><i class="cb${allSel ? ' on' : D.sel.size ? ' part' : ''}" aria-hidden="true"></i></button></span>` +
      `${sortCol('name', 'Name')}${sortCol('size', 'Size', ' r')}<span class="r">Dims</span><span>Labels</span>${sortCol('-mtime', 'Modified')}</div>` +
      `<div id="bk-grid" class="bk-rows" aria-label="Files: arrow keys move, Space selects, Enter opens">${items}</div></div>`) +
    `<div id="bk-more" class="bk-more"></div></div>`;
  setMore();
  observeThumbs();
  rove();
}

function observeThumbs(): void {
  const body = $('bk-body');
  if (!body) return;
  if (!io) {
    io = new IntersectionObserver((list) => {
      for (const x of list) {
        if (!x.isIntersecting) continue;
        const img = x.target as HTMLImageElement;
        const u = img.dataset['src'];
        io?.unobserve(img);
        if (!u) continue;
        img.src = u;
        img.removeAttribute('data-src');
        seen.add(u);
        previewStarted(img);
      }
    }, { root: body, rootMargin: '600px 0px' });
  }
  for (const img of body.querySelectorAll<HTMLImageElement>('img[data-src]')) io.observe(img);
}

export function renderFolder(): void {
  renderBar();
  renderStrip();
  renderSelbar();
  renderBody();
  ensureFolderStats();
}

function ensureFolderStats(): void {
  const bid = D.bid;
  if (!bid) return;
  const k = `${bid}\u0000${D.path}`;
  if (D.stats.has(k)) return;
  D.stats.set(k, 'loading');
  const path = D.path;
  api.stats(bid, path).then((s) => { D.stats.set(k, s); }, () => { D.stats.set(k, 'error'); }).finally(() => {
    if (D.bid === bid && D.path === path && !D.search) { renderStrip(); hooks.details(); }
  });
}

/** The selection changed: flip classes, repaint the bulk bar and the details (no tile is re-rendered). */
// The grid element the marks were last painted on and the paths marked there: a click then touches only the rows
// whose state can have changed (was or is selected), not all of a 2,000-tile folder. A new grid element (a full
// render) is painted in one pass.
let paintedGrid: Element | null = null;
let painted = new Set<string>();

function markRow(el: HTMLElement, on: boolean): void {
  if (el.classList.contains('sel') === on) return;
  el.classList.toggle('sel', on);
  el.setAttribute('aria-selected', String(on));
  el.querySelector('.cb')?.classList.toggle('on', on);
}

export function selChanged(): void {
  const grid = $('bk-grid');
  if (grid) {
    const touch = grid === paintedGrid ? new Set([...painted, ...D.sel]) : null;
    if (!touch || touch.size > 300) {
      for (const el of grid.querySelectorAll<HTMLElement>('[data-path]')) markRow(el, D.sel.has(el.dataset['path'] ?? ''));
    } else {
      for (const p of touch) {
        for (const el of grid.querySelectorAll<HTMLElement>(`[data-path="${CSS.escape(p)}"]`)) markRow(el, D.sel.has(p));
      }
    }
    paintedGrid = grid;
    painted = new Set(D.sel);
    rove();
  }
  const L = D.listing;
  const head = document.querySelector('#bk-body .bk-lrow.head .cb');
  if (head && L) {
    head.classList.toggle('on', D.sel.size > 0 && D.sel.size >= L.entries.length);
    head.classList.toggle('part', D.sel.size > 0 && D.sel.size < L.entries.length);
  }
  renderSelbar();
  hooks.details();
}

// ---------------------------------------------------------------- search results (header search)

export function renderSearch(): void {
  const s = D.search;
  const bar = $('bk-bar'), body = $('bk-body');
  if (!s || !bar || !body) return;
  $('bk-strip')?.classList.add('hidden');
  $('bk-selbar')?.classList.add('hidden');
  bar.innerHTML = `<span class="bk-here">${icon('search')}Search: <b>${esc(s.q)}</b></span><span class="grow"></span>` +
    `<span class="hint bk-wide">names contain the words · <code>*</code> globs · <code>#tag</code></span>` +
    `<button type="button" class="ghost" data-act="clear-search">${icon('x')}Close</button>`;
  if (s.err) { body.innerHTML = `<div class="bk-empty">${icon('alert')}<b>Search failed</b><span class="muted">${esc(s.err)}</span></div>`; return; }
  if (!s.res) { body.innerHTML = `<div class="bk-empty"><span class="spin" aria-hidden="true"></span>Searching…</div>`; return; }
  const { buckets, files } = s.res;
  if (!buckets.length && !files.length) { body.innerHTML = `<div class="bk-empty">${icon('search')}<b>Nothing found</b></div>`; return; }
  body.innerHTML = `<div class="bk-pad">` +
    (buckets.length ? `<h3 class="bk-h">Buckets <span class="num faint">${buckets.length}</span></h3><div class="bk-hits">${buckets.map((b) =>
      `<button type="button" class="bk-hit" data-act="go" data-bid="${esc(b.id)}" data-path="">${fdot(fmtKey(b.format))}<b>${esc(b.name)}</b>${fbadge(b.format)}<span class="faint num">${fmtN(b.stats?.images ?? null)} img</span></button>`).join('')}</div>` : '') +
    (files.length ? `<h3 class="bk-h">Files and folders <span class="num faint">${files.length}${files.length >= 200 ? '+' : ''}</span></h3><div class="bk-list"><div id="bk-grid" class="bk-rows">${files.map((f) => {
      const b = bucketOf(f.bucket);
      return `<div class="bk-lrow hit" role="row" data-act="reveal" data-bid="${esc(f.bucket)}" data-path="${esc(f.path)}" data-type="${esc(f.type)}" tabindex="0">` +
        `<span class="c-ck"></span><span class="c-n${f.type === 'dir' ? ' dir' : ''}">${icon(f.type === 'dir' ? 'folder' : 'file')}<span>${esc(f.name)}</span></span>` +
        `<span class="r num">${f.type === 'dir' ? '' : fmtSize(f.size)}</span><span class="c-p mono" title="${esc(f.path)}">${esc(b?.name ?? f.bucket)}/${esc(parentOf(f.path))}</span></div>`;
    }).join('')}</div></div>` : '') + '</div>';
}

// ---------------------------------------------------------------- open, menus

/** Open an entry as a double click does: a folder navigates, an image opens in a tab, a video in Extract frames, text in the editor. */
export async function openEntry(e: Entry): Promise<void> {
  const bid = D.bid;
  if (!bid) return;
  if (e.type === 'dir') { hooks.navigate(bid, e.path); return; }
  if (isImage(e)) { window.open(api.fileUrl(bid, e.path), '_blank', 'noopener'); return; }
  if (e.kind === 'video') { await openFrames(bid, [e.path]); return; }
  // text-like files (and small unknown ones, often config without an extension) open in the editor
  if (TEXTY.has(e.kind ?? '') || (e.kind === 'other' && e.size <= 65536) || (!e.kind && e.size <= 65536)) { await openFile(bid, e.path); return; }
  window.open(api.fileUrl(bid, e.path), '_blank', 'noopener');
}

function menu(from: HTMLElement, html: string, align: 'left' | 'right' = 'right'): void {
  const m = $('bk-menu');
  if (!m) return;
  m.innerHTML = html;
  floatPopover(m, from, undefined, align);
  m.querySelector<HTMLElement>('button, input')?.focus();
}

const sendPaths = (): string[] => (D.sel.size ? [...D.sel] : [D.path]);
/** The Send menu offers Extract frames: the selection holds a video, or (nothing selected) the folder does. */
const videosHere = (): boolean => (D.sel.size ? selEntries() : D.listing?.entries ?? []).some((e) => e.kind === 'video');

export const BROWSER_ACTIONS: Record<string, Action> = {
  pick: (el, ev) => {
    const p = el.dataset['path'] ?? '';
    const me = ev as MouseEvent;
    const L = D.listing;
    if (!L) return;
    if (me.detail >= 2 && !me.shiftKey && !me.ctrlKey && !me.metaKey) {
      const e = L.entries.find((x) => x.path === p);
      if (e) void openEntry(e);
      return;
    }
    const onCk = me.target instanceof Element && !!me.target.closest('[data-ck]');
    if (me.shiftKey && D.anchor) {
      const idx = L.entries.findIndex((x) => x.path === D.anchor), j = L.entries.findIndex((x) => x.path === p);
      if (idx >= 0 && j >= 0) {
        if (!me.ctrlKey && !me.metaKey) D.sel.clear();
        for (const x of L.entries.slice(Math.min(idx, j), Math.max(idx, j) + 1)) D.sel.add(x.path);
      }
    } else if (me.ctrlKey || me.metaKey || onCk) {
      if (D.sel.has(p)) D.sel.delete(p); else D.sel.add(p);
      D.anchor = p;
    } else {
      D.sel.clear();
      D.sel.add(p);
      D.anchor = p;
    }
    selChanged();
  },
  desel: (_el, ev) => {
    if (ev.target !== ev.currentTarget && !(ev.target instanceof Element && ev.target.matches('.bk-pad, .bk-grid, .bk-rows'))) return;
    if (D.sel.size) { D.sel.clear(); D.anchor = null; selChanged(); }
  },
  selall: () => {
    const L = D.listing;
    if (!L) return;
    if (D.sel.size >= L.entries.length) D.sel.clear(); else for (const x of L.entries) D.sel.add(x.path);
    selChanged();
  },
  'clear-sel': () => { D.sel.clear(); D.anchor = null; selChanged(); },
  view: (el) => {
    const v: ViewMode = el.dataset['mode'] === 'names' ? 'names' : 'thumbs';
    if (v === D.view) return;
    D.view = v;
    rememberPref(PREF.view, v);
    renderBar();
    renderBody();
    fillViewport();
  },
  sort: (el) => { D.sort = (el.dataset['sort'] as LsSort | undefined) ?? 'name'; rememberPref(PREF.sort, D.sort); closeFloat(); void loadListing(); },
  'sort-menu': (el) => menu(el, `<div class="bk-menu-h">Sort by</div>${SORTS.map(([k, l]) =>
    `<button type="button" role="menuitemradio" aria-checked="${D.sort === k}" data-act="sort" data-sort="${k}">${D.sort === k ? icon('check') : '<span class="ico"></span>'}${l}</button>`).join('')}`),
  'filter-menu': (el) => {
    menu(el, `<div class="bk-menu-h">Names containing</div><label class="bk-input">${icon('search')}<input type="search" value="${esc(D.nameQ)}" data-input="nameq" placeholder="e.g. cam03_ or .png" aria-label="Filter this folder by name" autocomplete="off"></label>` +
      (D.nameQ ? `<button type="button" class="ghost sm" data-act="clear-nameq">Clear</button>` : ''), 'left');
  },
  nameq: (el) => {
    const v = (el as HTMLInputElement).value.trim();
    clearTimeout(nameTimer);
    nameTimer = setTimeout(() => { if (v !== D.nameQ) { D.nameQ = v; D.sel.clear(); void loadListing(); } }, 250);
  },
  'clear-nameq': () => { D.nameQ = ''; closeFloat(); void loadListing(); },
  'send-menu': (el) => menu(el, (zoneOn('visualizer') || zoneOn('labeling') ? `<div class="bk-menu-h">Send ${D.sel.size ? `${fmtN(D.sel.size)} selected` : 'this folder'} to</div>` : '') +
    (zoneOn('visualizer') ? `<button type="button" role="menuitem" data-act="send" data-target="visualize">${icon('scatter')}Visualize<span class="faint">explore embeddings</span></button>` : '') +
    (zoneOn('labeling') ? `<button type="button" role="menuitem" data-act="send" data-target="label">${icon('edit')}Label<span class="faint">a labeling project</span></button>` : '') +
    (videosHere() ? `<button type="button" role="menuitem" data-act="extract-frames">${icon('film')}Extract frames…<span class="faint">${D.sel.size ? 'from the selected videos' : 'from this folder’s videos'}</span></button>` : '') +
    (D.sel.size ? '' : `<div class="bk-menu-h">This folder</div>` +
      (!zoneOn('labeling') ? '' : `<button type="button" role="menuitem" data-act="autolabel" data-path="${esc(D.path)}">${icon('robot')}Label with model…<span class="faint">auto-label every image</span></button>`) +
      `<button type="button" role="menuitem" data-act="export-zip">${icon('download')}Export (zip)<span class="faint">download this folder</span></button>` +
      `<button type="button" role="menuitem" data-act="export-to">${icon('upload')}Export to…<span class="faint">S3 · RustFS, FTP, SFTP</span></button>`)),
  'export-zip': () => { closeFloat(); if (D.bid) void downloadZip(D.bid, D.path); },
  'export-to': () => { closeFloat(); if (D.bid) void openExportDialog(D.bid, D.path); },
  send: (el) => {
    closeFloat();
    const bid = el.dataset['bid'] || D.bid || D.focus;
    if (!bid) return;
    const paths = el.dataset['paths'] !== undefined ? (el.dataset['paths'] ? el.dataset['paths'].split('\n') : ['']) : D.bid === bid ? sendPaths() : [''];
    void openSend(bid, paths, el.dataset['target'] === 'label' ? 'label' : 'visualize');
  },
  more: () => { void loadMore(); },
  'reload-folder': () => { void loadListing(); },
  /** Reload: the buckets, the tree and the open folder (an S3 folder is listed from S3 again). */
  refresh: () => { closeFloat(); refreshNext = true; void hooks.reload().then(() => toast('Reloaded', 'ok')); },
  'copy-path': (el) => {
    closeFloat();
    const bid = el.dataset['bid'] || D.bid || '';
    const paths = (el.dataset['paths'] ?? el.dataset['path'] ?? D.path).split('\n');
    const text = paths.map((p) => locationOf(bid, p)).filter(Boolean).join('\n');
    if (!text) return;
    void copyText(text).then((ok) => toast(ok ? `Copied ${paths.length === 1 ? text : `${paths.length} paths`}` : 'Could not copy: your browser blocked the clipboard', ok ? 'ok' : 'err'));
  },
  'retry-s3': () => { retryS3 = true; void loadListing(true); },
  mkdir: () => { void newFolder(); },
  move: () => { void moveSel(); },
  copy: () => { void copySel(); },
  rename: () => { void renameSel(); },
  delete: () => { void deleteSel(); },
  'tag-sel': (el) => { openTagEditor(el, D.bid ?? '', [...D.sel]); },
  'edit-tags': (el) => { if (D.bid) openTagEditor(el, D.bid, [el.dataset['path'] ?? D.path]); },
  'undo-retag': () => { if (D.bid) void undoRetag(D.bid); },
  'dismiss-retag': () => { if (D.bid) { D.retag.delete(D.bid); renderStrip(); } },
  reveal: (el) => {
    const bid = el.dataset['bid'] ?? '', p = el.dataset['path'] ?? '';
    if (el.dataset['type'] === 'dir') { hooks.navigate(bid, p); return; }
    reveal = p;
    const q = $<HTMLInputElement>('bk-q');
    if (q) q.value = '';
    hooks.navigate(bid, parentOf(p));
  },
};
let nameTimer: ReturnType<typeof setTimeout> | undefined;

// ---------------------------------------------------------------- drag and drop, scroll paging

const dropTarget = (t: EventTarget | null): HTMLElement | null => {
  const el = t instanceof Element ? t.closest<HTMLElement>('[data-drop]') : null;
  if (!el) return null;
  const bid = el.dataset['bid'] ?? D.bid;
  return bid === D.bid ? el : null;
};
// the one element marked as the drop target (dragover fires every few ms: no document query each time)
let dropOn: Element | null = null;
const clearDrop = (): void => { dropOn?.classList.remove('drop-on'); dropOn = null; };
const markDrop = (el: Element): void => { if (dropOn !== el) { clearDrop(); el.classList.add('drop-on'); dropOn = el; } };

// ---------------------------------------------------------------- keyboard in the file grid / list
/** Roving tab stop: exactly one item of the grid is in the Tab order (`to`, else the one already there, else the
 *  selection anchor, else the first), so Tab enters the grid once and Shift+Tab leaves it. */
function rove(to?: HTMLElement | null): void {
  const grid = $('bk-grid');
  if (!grid) return;
  const now = grid.querySelector<HTMLElement>(':scope > [tabindex="0"]');
  const want = to || now || (D.anchor && grid.querySelector<HTMLElement>(`:scope > [data-path="${CSS.escape(D.anchor)}"]`)) || grid.querySelector<HTMLElement>(':scope > [data-path]');
  if (!want || want === now) return;
  now?.setAttribute('tabindex', '-1');
  want.setAttribute('tabindex', '0');
}

// The grid is one Tab stop; inside it the arrow keys move the focus (Up / Down by a row of tiles), Shift+arrow
// extends the selection, Space toggles the focused item, Enter opens it, the ContextMenu key opens its menu. The
// actions go through the same click handler as the mouse (synthetic clicks), so both behave the same.
function gridKey(e: KeyboardEvent): void {
  const grid = $('bk-grid');
  if (!grid || !(e.target instanceof HTMLElement) || !grid.contains(e.target)) return;
  const items = [...grid.querySelectorAll<HTMLElement>(':scope > [data-path]')];
  if (!items.length) return;
  const cur = e.target === grid ? -1 : items.indexOf(e.target.closest<HTMLElement>('[data-path]') ?? e.target);
  const click = (el: HTMLElement, o: MouseEventInit): void => { el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ...o })); };
  let cols = 1;
  if (D.view === 'thumbs') { const top = items[0]?.offsetTop; cols = Math.max(1, items.findIndex((x) => x.offsetTop !== top)); if (cols <= 0) cols = items.length; }
  const step: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols, Home: -1e9, End: 1e9 };
  const k = e.key;
  if (k in step && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    const to = items[Math.max(0, Math.min(items.length - 1, (cur < 0 ? 0 : cur) + (cur < 0 ? 0 : step[k] ?? 0)))];
    if (!to) return;
    rove(to);
    to.focus();
    to.scrollIntoView({ block: 'nearest' });
    if (e.shiftKey) click(to, { shiftKey: true });
    return;
  }
  const at = cur >= 0 ? items[cur] : null;
  if (!at) return;
  if (k === ' ') { e.preventDefault(); click(at, { ctrlKey: true }); return; }
  if (k === 'Enter') { e.preventDefault(); click(at, { detail: 2 }); return; }
  if (k === 'ContextMenu' || (k === 'F10' && e.shiftKey)) {
    e.preventDefault();
    if (!D.sel.has(at.dataset['path'] ?? '')) click(at, {});
    const r = at.getBoundingClientRect();
    at.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: r.left + 12, clientY: r.top + 12 }));
  }
}

export function bindBrowser(): void {
  const app = $('bk-app'), body = $('bk-body'), main = $('bk-main');
  body?.addEventListener('keydown', gridKey);
  if (!app || !body || !main) return;
  body.addEventListener('scroll', () => {
    if (D.listing && !D.search && body.scrollTop + body.clientHeight > body.scrollHeight - 900) void loadMore();
  }, { passive: true });
  app.addEventListener('dragstart', (e) => {
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>('#bk-grid [data-path][draggable="true"]') : null;
    if (!el || !D.bid || !e.dataTransfer) return;
    const p = el.dataset['path'] ?? '';
    if (!D.sel.has(p)) { D.sel.clear(); D.sel.add(p); D.anchor = p; selChanged(); }
    dragging = { bid: D.bid, paths: [...D.sel] };
    e.dataTransfer.effectAllowed = 'copyMove';
    e.dataTransfer.setData('text/plain', dragging.paths.join('\n'));
    const n = dragging.paths.length;
    const ghost = document.createElement('div');
    ghost.className = 'bk-dragghost';
    ghost.textContent = n === 1 ? baseName(p) : `${n} items`;
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, -8, -8);
    setTimeout(() => ghost.remove(), 0);
  });
  app.addEventListener('dragend', () => { dragging = null; clearDrop(); });
  app.addEventListener('dragover', (e) => {
    const files = !dragging && !!e.dataTransfer?.types.includes('Files');
    if (dragging) {
      const t = dropTarget(e.target);
      if (!t || dragging.paths.includes(t.dataset['path'] ?? '') || (t.dataset['path'] ?? '') === parentOf(dragging.paths[0] ?? '') && dragging.paths.every((p) => parentOf(p) === (t.dataset['path'] ?? ''))) { clearDrop(); return; }
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = e.ctrlKey || e.altKey ? 'copy' : 'move';
      markDrop(t);
    } else if (files && D.bid && D.listing?.writable && !D.search) {
      e.preventDefault();
      const t = dropTarget(e.target);
      if (t) markDrop(t); else clearDrop();
      const ov = $('bk-drop');
      if (ov) {
        ov.classList.remove('hidden');
        const to = $('bk-drop-to');
        if (to) to.textContent = `into ${curBucket()?.name ?? ''}/${t ? t.dataset['path'] ?? '' : D.path}`;
      }
    }
  });
  app.addEventListener('dragleave', (e) => {
    if (e.relatedTarget instanceof Node && app.contains(e.relatedTarget)) return;
    clearDrop();
    $('bk-drop')?.classList.add('hidden');
  });
  app.addEventListener('drop', (e) => {
    $('bk-drop')?.classList.add('hidden');
    const t = dropTarget(e.target);
    clearDrop();
    if (dragging) {
      e.preventDefault();
      const d = dragging;
      dragging = null;
      if (!t) return;
      void moveTo(d.bid, d.paths, t.dataset['path'] ?? '', e.ctrlKey || e.altKey ? 'copy' : 'move');
      return;
    }
    const dt = e.dataTransfer, plain = dt ? [...dt.files] : [];
    if (!plain.length || !D.bid || !D.listing?.writable) return;
    e.preventDefault();
    const bid = D.bid, dest = t ? t.dataset['path'] ?? '' : D.path;
    const walked = dt ? walk(dt.items) : Promise.resolve([]);     // started inside the event: entries expire after it
    void walked.then((fs) => queueUpload(bid, dest, fs.length ? fs : plain), () => queueUpload(bid, dest, plain));
  });
}

/** Select these paths of the open folder (the AI drawer highlights what a plan touches). */
export function selectPaths(paths: string[]): void {
  D.sel = new Set(paths);
  selChanged();
}
