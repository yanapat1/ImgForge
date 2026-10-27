// The folder view (centre pane, a bucket open): toolbar (breadcrumb, Thumbnails / Names only, sort, name filter, New
// folder, Upload, Send to…), the folder strip (counts, tags, Edit tags, the format-change banner, upload progress),
// the bulk action bar, and the entries as a thumbnail grid or a names list. Big folders stay fast: the listing comes
// in pages of 500 (the next page when the scroll nears the end), thumbnails (…/file?w=256) load only when they scroll
// into view (IntersectionObserver), one innerHTML per change (a next page is appended) and one delegated listener
// for everything (app.ts). Selection: click, Ctrl / Cmd toggles, Shift selects a range, the checkbox toggles; a
// selection change only flips classes (selChanged), it never re-renders the tiles. Drag the selection onto a folder
// (a tile, a breadcrumb, a tree row) to move it (Ctrl / Alt: copy); drop files from the desktop to upload them.
// Also the header search results (renderSearch).

import { errText } from '../core/api.js';
import { rememberPref } from '../core/auth.js';
import { $, esc, icon } from '../core/dom.js';
import { closeFloat, floatPopover } from '../core/popover.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { copySel, deleteSel, moveSel, moveTo, newFolder, renameSel, undoRetag } from './ops.js';
import { openSend } from './send.js';
import {
  baseName, bucketOf, curBucket, D, fbadge, fdot, fmtDate, fmtKey, fmtLabel, fmtN, fmtSize, hooks, isImage, parentOf,
  PREF, tagChip, writable,
} from './state.js';
import { openTagEditor } from './tags.js';
import { uploadFiles } from './upload.js';
import type { Entry, LsSort } from '../types-datacenter.js';
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
  try {
    const r = await api.ls(bid, D.path, 0, Math.min(2000, Math.max(api.PAGE, had)), D.sort, D.nameQ);
    if (seq !== D.seq) return;
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
    const el = document.querySelector<HTMLElement>(`#dc-grid [data-path="${CSS.escape(reveal)}"]`);
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
    const grid = $('dc-grid');
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
  const body = $('dc-body');
  if (body && D.listing && D.listing.entries.length < D.listing.total && body.scrollHeight <= body.clientHeight + 400) void loadMore();
}

function setMore(): void {
  const el = $('dc-more');
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
  return icon('file');
}

function tile(e: Entry): string {
  const bid = D.bid ?? '';
  const sel = D.sel.has(e.path);
  const drag = writable() ? ' draggable="true"' : '';
  const cap = e.type === 'dir'
    ? `<span class="meta">${e.items != null ? `${fmtN(e.items)} items` : 'folder'}</span>`
    : `<span class="meta">${fmtSize(e.size)}${e.w && e.h ? ` · ${e.w}×${e.h}` : ''}</span>`;
  let media: string;
  if (isImage(e)) {
    const u = thumbUrl(bid, e.path);
    media = seen.has(u) ? `<img src="${esc(u)}" alt="" decoding="async">` : `<img data-src="${esc(u)}" alt="" decoding="async">`;
  } else media = `<span class="ph${e.type === 'dir' ? ' dir' : ''}">${fileIcon(e)}</span>`;
  return `<div class="dc-th${sel ? ' sel' : ''}${e.type === 'dir' ? ' dir' : ''}" role="option" aria-selected="${sel}" tabindex="-1" data-act="pick" data-path="${esc(e.path)}"${e.type === 'dir' ? ' data-drop="1"' : ''}${drag} title="${esc(e.name)}">` +
    `<div class="im">${media}${labelBadge(e)}<i class="ck" data-ck="1" aria-hidden="true"></i></div>` +
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
  return `<div class="dc-lrow${sel ? ' sel' : ''}" role="row" aria-selected="${sel}" data-act="pick" data-path="${esc(e.path)}"${e.type === 'dir' ? ' data-drop="1"' : ''}${drag}>` +
    `<span class="c-ck"><i class="cb${sel ? ' on' : ''}" data-ck="1" aria-hidden="true"></i></span>` +
    `<span class="c-n${e.type === 'dir' ? ' dir' : ''}">${fileIcon(e)}<span>${esc(e.name)}${e.type === 'dir' ? '/' : ''}</span></span>` +
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
  return `<nav class="dc-crumbs" aria-label="Folder">${out.join('')}</nav>`;
}

function renderBar(): void {
  const bar = $('dc-bar');
  if (!bar) return;
  const w = writable();
  const nm = D.nameQ ? `<span class="dot-on" aria-hidden="true"></span>` : '';
  bar.innerHTML = crumbs() + '<span class="grow"></span>' +
    `<div class="segmented dc-viewseg" role="group" aria-label="Show">` +
    `<button type="button" class="view-btn${D.view === 'thumbs' ? ' active' : ''}" data-act="view" data-mode="thumbs" aria-pressed="${D.view === 'thumbs'}" id="dc-view-thumbs">${icon('grid')}<span class="dc-wide-txt">Thumbnails</span></button>` +
    `<button type="button" class="view-btn${D.view === 'names' ? ' active' : ''}" data-act="view" data-mode="names" aria-pressed="${D.view === 'names'}" id="dc-view-names">${icon('list')}<span class="dc-wide-txt">Names only</span></button></div>` +
    `<button type="button" class="icon-btn" data-act="sort-menu" aria-haspopup="menu" aria-expanded="false" aria-label="Sort: ${esc(SORTS.find(([k]) => k === D.sort)?.[1] ?? '')}" title="Sort">${icon('sort')}</button>` +
    `<button type="button" class="dc-dd" data-act="filter-menu" aria-haspopup="dialog" aria-expanded="false" title="Filter by name">${icon('filter')}<span class="dc-wide-txt">${D.nameQ ? esc(D.nameQ) : 'Filter'}</span>${nm}${icon('chev-d')}</button>` +
    '<span class="tb-sep" aria-hidden="true"></span>' +
    `<button type="button" class="icon-btn" data-act="mkdir" aria-label="New folder" title="New folder"${w ? '' : ' disabled'}>${icon('folder-plus')}</button>` +
    `<button type="button" class="icon-btn" data-act="upload" aria-label="Upload files here" title="Upload files here"${w ? '' : ' disabled'}>${icon('upload')}</button>` +
    `<button type="button" class="primary" data-act="send-menu" aria-haspopup="menu" aria-expanded="false">${icon('send')}<span class="dc-wide-txt">Send to…</span>${icon('chev-d')}</button>` +
    `<button type="button" class="icon-btn dc-only-overlay" data-act="show-details" aria-label="Details" title="Details">${icon('eye')}</button>`;
}

function stripCount(): string {
  const L = D.listing;
  if (!L) return '';
  const n = L.entries.length;
  return n >= L.total ? `${fmtN(L.total)} item${L.total === 1 ? '' : 's'}` : `1–${fmtN(n)} of ${fmtN(L.total)}`;
}
function renderStripCount(): void { const el = $('dc-strip-n'); if (el) el.textContent = stripCount(); }

function renderStrip(): void {
  const el = $('dc-strip');
  if (!el) return;
  const b = curBucket(), L = D.listing;
  const out: string[] = [];
  const rt = D.bid ? D.retag.get(D.bid) : undefined;
  if (rt) {
    out.push(`<div class="dc-banner" role="status">${icon('alert')}<span><b>Format: ${esc(fmtLabel(rt.to))}.</b> ` +
      `${esc(fmtLabel(rt.from))} → ${esc(fmtLabel(rt.to))}${rt.reason ? ` (${esc(rt.reason)})` : ''}${rt.path ? ` in <code>${esc(rt.path)}/</code>` : ''}.</span><span class="grow"></span>` +
      (rt.undo_id ? `<button type="button" class="sm" data-act="undo-retag">${icon('undo')}Move it back</button>` : '') +
      `<button type="button" class="ghost sm" data-act="redetect" data-path="${esc(rt.path)}">Re-detect</button>` +
      `<button type="button" class="icon-btn ghost sm" data-act="dismiss-retag" aria-label="Dismiss">${icon('x')}</button></div>`);
  }
  if (D.upload) {
    const u = D.upload;
    const pct = u.total ? Math.round((100 * u.done) / u.total) : 0;
    out.push(`<div class="dc-upline${u.err ? ' err' : ''}" role="status">${icon(u.err ? 'alert' : 'upload')}<span>${u.err ? esc(u.err) : `Uploading ${fmtN(u.files)} file${u.files === 1 ? '' : 's'}… ${pct}%`}</span>` +
      `<div class="bar"><div style="width:${pct}%"></div></div>${u.err ? `<button type="button" class="icon-btn ghost sm" data-act="dismiss-upload" aria-label="Dismiss">${icon('x')}</button>` : ''}</div>`);
  }
  if (L && b) {
    const st = D.stats.get(`${b.id}\u0000${D.path}`);
    const imgs = st && typeof st === 'object' ? `${fmtN(st.images)} img · ${fmtSize(st.size)}` : `${fmtN(L.total)} items`;
    const tags = L.tags.map((t) => tagChip(t.tag, t.auto, 'on')).join('');
    out.push(`<div class="dc-strip-row"><span class="lbl">${icon('folder')}<b class="mono">${esc(D.path ? `${baseName(D.path)}/` : `${b.name}/`)}</b><span class="num">${imgs}</span></span>` +
      `<span class="tb-sep" aria-hidden="true"></span><span class="lbl">${icon('tag')}Tags</span><span class="dc-tagrow">${tags || '<span class="faint">none</span>'}</span>` +
      `<button type="button" class="ghost sm" data-act="edit-tags" data-path="${esc(D.path)}" aria-haspopup="dialog">${icon('edit')}Edit tags</button>` +
      `<span class="grow"></span><span id="dc-strip-n" class="num faint">${stripCount()}</span></div>`);
  }
  el.innerHTML = out.join('');
  el.classList.toggle('hidden', !out.length);
}

function renderSelbar(): void {
  const el = $('dc-selbar');
  if (!el) return;
  const n = D.sel.size;
  el.classList.toggle('hidden', !n);
  if (!n) { el.innerHTML = ''; return; }
  const L = D.listing;
  const all = !!L && n >= L.entries.length;
  const w = writable();
  const dis = w ? '' : ' disabled title="This bucket is read-only"';
  el.innerHTML = `<button type="button" class="dc-cbbtn" data-act="selall" aria-label="${all ? 'Clear the selection' : 'Select all loaded'}"><i class="cb ${all ? 'on' : 'part'}" aria-hidden="true"></i></button>` +
    `<b>${fmtN(n)} selected</b>` +
    `<button type="button" class="ghost" data-act="move"${dis}>${icon('move')}<span class="dc-wide-txt">Move</span></button>` +
    `<button type="button" class="ghost" data-act="copy"${dis}>${icon('copy')}<span class="dc-wide-txt">Copy</span></button>` +
    `<button type="button" class="ghost" data-act="rename"${n === 1 && w ? '' : ' disabled'}>${icon('edit')}<span class="dc-wide-txt">Rename</span></button>` +
    `<button type="button" class="ghost" data-act="tag-sel">${icon('tag')}<span class="dc-wide-txt">Tag</span></button>` +
    `<button type="button" class="ghost danger-ghost" data-act="delete"${dis}>${icon('trash')}<span class="dc-wide-txt">Delete</span></button>` +
    `<span class="grow"></span><span class="hint dc-wide">Shift-click for a range · <kbd>Ctrl</kbd> <kbd>A</kbd> all ${L ? fmtN(L.entries.length) : ''}</span>` +
    `<button type="button" class="ghost sm" data-act="clear-sel">Clear</button>`;
}

function renderBody(): void {
  const body = $('dc-body');
  if (!body) return;
  const L = D.listing;
  if (!L) {
    body.innerHTML = D.listErr
      ? `<div class="dc-empty">${icon('alert')}<b>Could not open this folder</b><span class="muted">${esc(D.listErr)}</span><button type="button" data-act="reload-folder">${icon('refresh')}Retry</button></div>`
      : `<div class="dc-pad"><div class="${D.view === 'thumbs' ? 'dc-grid' : 'dc-rows'} skel" aria-busy="true">${'<div class="sk"></div>'.repeat(D.view === 'thumbs' ? 18 : 14)}</div></div>`;
    return;
  }
  if (!L.entries.length) {
    body.innerHTML = `<div class="dc-pad" data-act="desel"><div class="dc-empty">${icon(D.nameQ ? 'filter' : 'folder-o')}<b>${D.nameQ ? `Nothing named like “${esc(D.nameQ)}”` : 'This folder is empty'}</b>` +
      (writable() && !D.nameQ ? `<span class="muted">Drop files here, or</span><div class="row"><button type="button" class="primary" data-act="upload">${icon('upload')}Upload files</button><button type="button" data-act="mkdir">${icon('folder-plus')}New folder</button></div>` : '') +
      (D.nameQ ? '<button type="button" data-act="clear-nameq">Clear the filter</button>' : '') + '</div></div>';
    return;
  }
  const items = L.entries.map(D.view === 'thumbs' ? tile : lrow).join('');
  const sortCol = (k: LsSort, label: string, cls = ''): string =>
    `<button type="button" class="hcol${cls}${D.sort === k ? ' sorted' : ''}" data-act="sort" data-sort="${k}">${label}${D.sort === k ? icon('chev-d') : ''}</button>`;
  const allSel = D.sel.size > 0 && D.sel.size >= L.entries.length;
  body.innerHTML = `<div class="dc-pad" data-act="desel">` + (D.view === 'thumbs'
    ? `<div id="dc-grid" class="dc-grid" role="listbox" aria-multiselectable="true" aria-label="Files">${items}</div>`
    : `<div class="dc-list" role="grid" aria-multiselectable="true" aria-label="Files"><div class="dc-lrow head" role="row">` +
      `<span class="c-ck"><button type="button" class="dc-cbbtn" data-act="selall" aria-label="Select all"><i class="cb${allSel ? ' on' : D.sel.size ? ' part' : ''}" aria-hidden="true"></i></button></span>` +
      `${sortCol('name', 'Name')}${sortCol('size', 'Size', ' r')}<span class="r">Dims</span><span>Labels</span>${sortCol('-mtime', 'Modified')}</div>` +
      `<div id="dc-grid" class="dc-rows">${items}</div></div>`) +
    `<div id="dc-more" class="dc-more"></div></div>`;
  setMore();
  observeThumbs();
}

function observeThumbs(): void {
  const body = $('dc-body');
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
export function selChanged(): void {
  const grid = $('dc-grid');
  if (grid) {
    for (const el of grid.querySelectorAll<HTMLElement>('[data-path]')) {
      const on = D.sel.has(el.dataset['path'] ?? '');
      if (el.classList.contains('sel') !== on) {
        el.classList.toggle('sel', on);
        el.setAttribute('aria-selected', String(on));
        el.querySelector('.cb')?.classList.toggle('on', on);
      }
    }
  }
  const L = D.listing;
  const head = document.querySelector('#dc-body .dc-lrow.head .cb');
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
  const bar = $('dc-bar'), body = $('dc-body');
  if (!s || !bar || !body) return;
  $('dc-strip')?.classList.add('hidden');
  $('dc-selbar')?.classList.add('hidden');
  bar.innerHTML = `<span class="dc-here">${icon('search')}Search: <b>${esc(s.q)}</b></span><span class="grow"></span>` +
    `<span class="hint dc-wide">names contain the words · <code>*</code> globs · <code>#tag</code></span>` +
    `<button type="button" class="ghost" data-act="clear-search">${icon('x')}Close</button>`;
  if (s.err) { body.innerHTML = `<div class="dc-empty">${icon('alert')}<b>Search failed</b><span class="muted">${esc(s.err)}</span></div>`; return; }
  if (!s.res) { body.innerHTML = `<div class="dc-empty"><span class="spin" aria-hidden="true"></span>Searching…</div>`; return; }
  const { buckets, files } = s.res;
  if (!buckets.length && !files.length) { body.innerHTML = `<div class="dc-empty">${icon('search')}<b>Nothing found</b></div>`; return; }
  body.innerHTML = `<div class="dc-pad">` +
    (buckets.length ? `<h3 class="dc-h">Buckets <span class="num faint">${buckets.length}</span></h3><div class="dc-hits">${buckets.map((b) =>
      `<button type="button" class="dc-hit" data-act="go" data-bid="${esc(b.id)}" data-path="">${fdot(fmtKey(b.format))}<b>${esc(b.name)}</b>${fbadge(b.format)}<span class="faint num">${fmtN(b.stats?.images ?? null)} img</span></button>`).join('')}</div>` : '') +
    (files.length ? `<h3 class="dc-h">Files and folders <span class="num faint">${files.length}${files.length >= 200 ? '+' : ''}</span></h3><div class="dc-list"><div id="dc-grid" class="dc-rows">${files.map((f) => {
      const b = bucketOf(f.bucket);
      return `<div class="dc-lrow hit" role="row" data-act="reveal" data-bid="${esc(f.bucket)}" data-path="${esc(f.path)}" data-type="${esc(f.type)}" tabindex="0">` +
        `<span class="c-ck"></span><span class="c-n${f.type === 'dir' ? ' dir' : ''}">${icon(f.type === 'dir' ? 'folder' : 'file')}<span>${esc(f.name)}</span></span>` +
        `<span class="r num">${f.type === 'dir' ? '' : fmtSize(f.size)}</span><span class="c-p mono" title="${esc(f.path)}">${esc(b?.name ?? f.bucket)}/${esc(parentOf(f.path))}</span></div>`;
    }).join('')}</div></div>` : '') + '</div>';
}

// ---------------------------------------------------------------- open, menus

async function openEntry(e: Entry): Promise<void> {
  const bid = D.bid;
  if (!bid) return;
  if (e.type === 'dir') { hooks.navigate(bid, e.path); return; }
  if (isImage(e)) { window.open(api.fileUrl(bid, e.path), '_blank', 'noopener'); return; }
  if (e.kind === 'archive' || e.kind === 'npy' || (e.kind === 'other' && e.size > 65536)) { window.open(api.fileUrl(bid, e.path), '_blank', 'noopener'); return; }
  let text = '';
  try {
    const r = await api.readText(bid, e.path);
    text = r.text + (r.truncated ? '\n…' : '');
  } catch (err) { text = `Could not read it: ${errText(err)}`; }
  await openDialog({ title: e.name, wide: true, ok: 'Close', okClass: 'ghost',
    body: `<pre class="dc-text">${esc(text)}</pre>`, onOk: () => null });
}

function menu(from: HTMLElement, html: string, align: 'left' | 'right' = 'right'): void {
  const m = $('dc-menu');
  if (!m) return;
  m.innerHTML = html;
  floatPopover(m, from, undefined, align);
  m.querySelector<HTMLElement>('button, input')?.focus();
}

const sendPaths = (): string[] => (D.sel.size ? [...D.sel] : [D.path]);

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
    if (ev.target !== ev.currentTarget && !(ev.target instanceof Element && ev.target.matches('.dc-pad, .dc-grid, .dc-rows'))) return;
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
  'sort-menu': (el) => menu(el, `<div class="dc-menu-h">Sort by</div>${SORTS.map(([k, l]) =>
    `<button type="button" role="menuitemradio" aria-checked="${D.sort === k}" data-act="sort" data-sort="${k}">${D.sort === k ? icon('check') : '<span class="ico"></span>'}${l}</button>`).join('')}`),
  'filter-menu': (el) => {
    menu(el, `<div class="dc-menu-h">Names containing</div><label class="dc-input">${icon('search')}<input type="search" value="${esc(D.nameQ)}" data-input="nameq" placeholder="e.g. cam03_ or .png" aria-label="Filter this folder by name" autocomplete="off"></label>` +
      (D.nameQ ? `<button type="button" class="ghost sm" data-act="clear-nameq">Clear</button>` : ''), 'left');
  },
  nameq: (el) => {
    const v = (el as HTMLInputElement).value.trim();
    clearTimeout(nameTimer);
    nameTimer = setTimeout(() => { if (v !== D.nameQ) { D.nameQ = v; D.sel.clear(); void loadListing(); } }, 250);
  },
  'clear-nameq': () => { D.nameQ = ''; closeFloat(); void loadListing(); },
  'send-menu': (el) => menu(el, `<div class="dc-menu-h">Send ${D.sel.size ? `${fmtN(D.sel.size)} selected` : 'this folder'} to</div>` +
    `<button type="button" role="menuitem" data-act="send" data-target="visualize">${icon('scatter')}Visualize<span class="faint">explore embeddings</span></button>` +
    `<button type="button" role="menuitem" data-act="send" data-target="label">${icon('edit')}Label<span class="faint">a labeling project</span></button>`),
  send: (el) => {
    closeFloat();
    const bid = el.dataset['bid'] || D.bid || D.focus;
    if (!bid) return;
    const paths = el.dataset['paths'] !== undefined ? (el.dataset['paths'] ? el.dataset['paths'].split('\n') : ['']) : D.bid === bid ? sendPaths() : [''];
    void openSend(bid, paths, el.dataset['target'] === 'label' ? 'label' : 'visualize');
  },
  more: () => { void loadMore(); },
  'reload-folder': () => { void loadListing(); },
  mkdir: () => { void newFolder(); },
  move: () => { void moveSel(); },
  copy: () => { void copySel(); },
  rename: () => { void renameSel(); },
  delete: () => { void deleteSel(); },
  'tag-sel': (el) => { openTagEditor(el, D.bid ?? '', [...D.sel]); },
  'edit-tags': (el) => { if (D.bid) openTagEditor(el, D.bid, [el.dataset['path'] ?? D.path]); },
  'undo-retag': () => { if (D.bid) void undoRetag(D.bid); },
  'dismiss-retag': () => { if (D.bid) { D.retag.delete(D.bid); renderStrip(); } },
  'dismiss-upload': () => { D.upload = null; renderStrip(); },
  reveal: (el) => {
    const bid = el.dataset['bid'] ?? '', p = el.dataset['path'] ?? '';
    if (el.dataset['type'] === 'dir') { hooks.navigate(bid, p); return; }
    reveal = p;
    const q = $<HTMLInputElement>('dc-q');
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
const clearDrop = (): void => { for (const x of document.querySelectorAll('.drop-on')) x.classList.remove('drop-on'); };

export function bindBrowser(): void {
  const app = $('dc-app'), body = $('dc-body'), main = $('dc-main');
  if (!app || !body || !main) return;
  body.addEventListener('scroll', () => {
    if (D.listing && !D.search && body.scrollTop + body.clientHeight > body.scrollHeight - 900) void loadMore();
  }, { passive: true });
  app.addEventListener('dragstart', (e) => {
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>('#dc-grid [data-path][draggable="true"]') : null;
    if (!el || !D.bid || !e.dataTransfer) return;
    const p = el.dataset['path'] ?? '';
    if (!D.sel.has(p)) { D.sel.clear(); D.sel.add(p); D.anchor = p; selChanged(); }
    dragging = { bid: D.bid, paths: [...D.sel] };
    e.dataTransfer.effectAllowed = 'copyMove';
    e.dataTransfer.setData('text/plain', dragging.paths.join('\n'));
    const n = dragging.paths.length;
    const ghost = document.createElement('div');
    ghost.className = 'dc-dragghost';
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
      clearDrop();
      if (!t || dragging.paths.includes(t.dataset['path'] ?? '') || (t.dataset['path'] ?? '') === parentOf(dragging.paths[0] ?? '') && dragging.paths.every((p) => parentOf(p) === (t.dataset['path'] ?? ''))) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = e.ctrlKey || e.altKey ? 'copy' : 'move';
      t.classList.add('drop-on');
    } else if (files && D.bid && D.listing?.writable && !D.search) {
      e.preventDefault();
      const t = dropTarget(e.target);
      clearDrop();
      t?.classList.add('drop-on');
      const ov = $('dc-drop');
      if (ov) {
        ov.classList.remove('hidden');
        const to = $('dc-drop-to');
        if (to) to.textContent = `into ${curBucket()?.name ?? ''}/${t ? t.dataset['path'] ?? '' : D.path}`;
      }
    }
  });
  app.addEventListener('dragleave', (e) => {
    if (e.relatedTarget instanceof Node && app.contains(e.relatedTarget)) return;
    clearDrop();
    $('dc-drop')?.classList.add('hidden');
  });
  app.addEventListener('drop', (e) => {
    $('dc-drop')?.classList.add('hidden');
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
    const files = e.dataTransfer ? [...e.dataTransfer.files] : [];
    if (!files.length || !D.bid || !D.listing?.writable) return;
    e.preventDefault();
    void uploadFiles(D.bid, t ? t.dataset['path'] ?? '' : D.path, files);
  });
}

/** Select these paths of the open folder (the AI drawer highlights what a plan touches). */
export function selectPaths(paths: string[]): void {
  D.sel = new Set(paths);
  selChanged();
}
