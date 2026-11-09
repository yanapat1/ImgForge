// The Data Center page (?data[=<bucket>&path=…], design "Explorer"): its header (Sessions | Data tabs, file search,
// settings, theme, user menu) and the 3-pane layout: tree (tree.ts) | the All buckets overview (buckets.ts) or a
// folder (browser.ts) | details (details.ts), with the AI drawer (ai.ts) docked under the centre and right panes.
// It owns the routing (bucket + path live in the URL: pushState on navigate, popstate goes back), the one delegated
// click / change / input listener of the page (elements carry data-act="<action>"; each pane module exports its
// actions), the keyboard (/ search, Delete, F2 rename, Ctrl+A, Esc) and the loads (buckets, summary, tags).

import { errText } from '../core/api.js';
import { pref } from '../core/auth.js';
import { $, $req, icon } from '../core/dom.js';
import { dataUrl } from '../core/nav.js';
import { closeFloat } from '../core/popover.js';
import { setTheme, THEME } from '../core/theme.js';
import { openSettings } from '../settings/dialog.js';
import { toast } from '../core/toast.js';
import { mountUserMenu } from '../views/usermenu.js';
import { appHeader } from '../core/header.js';
import * as api from './api.js';
import { ACTIVITY_ACTIONS, watchJobs } from './activity.js';
import { AI_ACTIONS, aiScopeChanged, bindAi, renderAi } from './ai.js';
import { closeFile, EDITOR_ACTIONS, fileDirty, fileOpen, leaveFile } from './editor.js';
import { BROWSER_ACTIONS, bindBrowser, loadListing, renderFolder, renderSearch, selChanged } from './browser.js';
import { BUCKET_ACTIONS, renderOverview } from './buckets.js';
import { AUTOLABEL_ACTIONS } from './autolabel.js';
import { FRAMES_ACTIONS } from './frames.js';
import { DETAIL_ACTIONS, renderDetails } from './details.js';
import { EDIT_ACTIONS } from './edit.js';
import { deleteSel, OPS_ACTIONS, renameSel } from './ops.js';
import { bucketOf, D, hooks, key, parentOf, PREF } from './state.js';
import { LOCAL_ACTIONS } from './localcopy.js';
import { renderTree, TREE_ACTIONS } from './tree.js';
import { bindCtxMenu, CTX_ACTIONS } from './ctxmenu.js';
import { bindResize } from './resize.js';
import { bindUpload, UPLOAD_ACTIONS } from './upload.js';
import { hasUploads } from './uploads.js';
import { UPLOADS_ACTIONS } from './uploads.js';
import type { BucketSort, CardMode, ViewMode } from './state.js';
import type { LsSort } from '../types-datacenter.js';

/** A delegated action: the element that carries data-act and the event. */
export type Action = (el: HTMLElement, ev: Event) => void;

const ACTIONS: Record<string, Action> = {};

const SKELETON = (): string => `
${appHeader({ page: 'data', cls: 'dc-header', actionsId: 'dc-hactions',
    middle: `<div class="dc-hsearch" role="search">${icon('search')}<input id="dc-q" type="search" placeholder="Search files, folders, tags…" aria-label="Search files, folders and tags" autocomplete="off" spellcheck="false"><kbd aria-hidden="true">/</kbd></div>`,
    actions: `<button type="button" class="icon-btn ghost dc-only-narrow" data-act="toggle-tree" aria-label="Show the bucket tree" title="Buckets">${icon('list')}</button>` })}
<div id="dc-activity" class="dc-activity hidden" role="status" aria-live="polite" aria-label="Running jobs"></div>
<div id="dc-app" class="dc-app">
  <aside id="dc-tree" class="dc-tree" aria-label="Buckets and folders"></aside>
  <section id="dc-main" class="dc-main" role="main" tabindex="-1" aria-label="Files">
    <div class="dc-head">
      <div id="dc-bar" class="dc-bar"></div>
      <div id="dc-selbar" class="dc-selbar hidden" role="toolbar" aria-label="Selection"></div>
    </div>
    <div id="dc-strip" class="dc-strip hidden"></div>
    <div id="dc-body" class="dc-body" tabindex="-1"></div>
    <div id="dc-drop" class="dc-drop hidden" aria-hidden="true">${icon('upload')}<b>Drop files to upload here</b><span id="dc-drop-to"></span></div>
  </section>
  <aside id="dc-details" class="dc-details" aria-label="Details"></aside>
  <div id="dc-split-tree" class="dc-split v" role="separator" aria-orientation="vertical" aria-controls="dc-tree" aria-label="Resize the bucket tree (double-click: default)" title="Drag to resize · double-click: default" tabindex="0"></div>
  <div id="dc-scrim" class="dc-scrim" data-act="close-overlays"></div>
</div>
<div id="dc-menu" class="dc-pop dc-menu hidden" role="menu"></div>
<input id="dc-file" type="file" multiple class="hidden" aria-hidden="true" tabindex="-1">`;

// ---------------------------------------------------------------- routing

function readLocation(): void {
  const q = new URLSearchParams(location.search);
  const b = q.get('data');
  D.bid = b ? b : null;
  D.path = D.bid ? (q.get('path') ?? '').replace(/^\/+|\/+$/g, '') : '';
}

/** Open a bucket folder (bid null = All buckets). push: a new history entry (else replace). */
function navigate(bid: string | null, path = '', push = true): void {
  if (fileDirty()) {                                // an open file with unsaved changes may be kept: ask first
    void leaveFile().then((ok) => { if (ok) navigate(bid, path, push); });
    return;
  }
  closeFile();
  const same = bid === D.bid && path === D.path;
  D.bid = bid;
  D.path = bid ? path : '';
  D.search = null;
  ($('dc-q') as HTMLInputElement | null)?.blur();
  if (!same) {
    D.sel.clear();
    D.anchor = null;
    D.nameQ = '';
    D.listing = null;
    D.listErr = null;
  }
  D.treeOpen = false;
  D.detailsOpen = false;
  const url = dataUrl(bid, D.path);
  if (push && !same) history.pushState({ dc: 1 }, '', url);
  else history.replaceState({ dc: 1 }, '', url);
  if (bid) {                                       // the tree shows where we are
    D.expanded.add(key(bid, ''));
    let p = D.path;
    while (p) { D.expanded.add(key(bid, p)); p = parentOf(p); }
  }
  document.title = `${bid ? `${bucketOf(bid)?.name ?? 'Bucket'}${D.path ? `/${D.path}` : ''}` : 'Data'} · Dataset Visualizer`;
  render();
  if (bid) void loadListing();
  $('dc-body')?.scrollTo(0, 0);
}

/** Buckets, the summary and the tag counts (in parallel), then the open folder again. */
async function reload(): Promise<void> {
  await Promise.all([loadBuckets(), D.bid ? loadListing(true) : Promise.resolve()]);
  render();
}

async function loadBuckets(): Promise<void> {
  const [b, s, t] = await Promise.allSettled([api.listBuckets(), api.summary(), api.allTags()]);
  if (b.status === 'fulfilled') { D.buckets = b.value; D.loadErr = null; } else D.loadErr = errText(b.reason);
  if (s.status === 'fulfilled') D.summary = s.value;
  if (t.status === 'fulfilled') D.tags = t.value;
  D.stats.clear();                                 // counts may have changed with the op that asked for this
  D.samples.forEach((v, k) => { if (v === 'loading') D.samples.delete(k); });
  // the tree's open folders are listed again on the next paint
  D.children.clear();
}

// ---------------------------------------------------------------- paint

function render(): void {
  const n = $('dc-nav-n');
  if (n) n.textContent = D.buckets.length ? String(D.buckets.length) : '';
  renderTree();
  if (D.search) renderSearch();
  else if (D.bid) renderFolder();
  else renderOverview();
  renderDetails();
  aiScopeChanged();
  const app = $req('dc-app');
  app.classList.toggle('tree-open', D.treeOpen);
  app.classList.toggle('details-open', D.detailsOpen);
}

// ---------------------------------------------------------------- events

/** A header link (Sessions, the brand) clicked while this tab uploads: leaving would stop the upload, so the page
 *  opens in a new tab instead (a blocked pop-up falls back to the normal leave-page prompt). */
function leaveKeepingUploads(e: MouseEvent): boolean {
  const a = e.target instanceof Element ? e.target.closest<HTMLAnchorElement>('.dc-header a[href]') : null;
  if (!a || !hasUploads() || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey
      || a.target === '_blank' || a.getAttribute('aria-current') === 'page') return false;
  if (!window.open(a.href, '_blank')) return false;
  e.preventDefault();
  toast('Opened in a new tab: the upload keeps going in this one', 'ok');
  return true;
}

function bind(root: HTMLElement): void {
  root.addEventListener('click', (e) => {
    if (leaveKeepingUploads(e)) return;
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-act]') : null;
    if (!el || !root.contains(el) || (el instanceof HTMLButtonElement && el.disabled)) return;
    const a = ACTIONS[el.dataset['act'] ?? ''];
    if (a) a(el, e);
  });
  // Enter / Space on a role=button / treeitem / row element acts like a click; the tree moves with the arrows
  root.addEventListener('keydown', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.matches('.t-row[role="treeitem"]') && treeKey(e, t)) return;
    if (e.key !== 'Enter' && e.key !== ' ') return;
    if (!/^(button|treeitem|row)$/.test(t.getAttribute('role') ?? '') || !t.dataset['act']) return;
    e.preventDefault();
    t.click();
  });
  for (const type of ['change', 'input'] as const) {
    root.addEventListener(type, (e) => {
      const el = e.target instanceof HTMLElement ? e.target.closest<HTMLElement>(`[data-${type}]`) : null;
      const a = el ? ACTIONS[el.dataset[type] ?? ''] : undefined;
      if (el && a) a(el, e);
    });
  }
  // header search: results replace the centre pane; Esc / empty goes back
  const q = $req<HTMLInputElement>('dc-q');
  let t: ReturnType<typeof setTimeout> | undefined;
  q.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => { void runSearch(q.value.trim()); }, 220);
  });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); q.value = ''; void runSearch(''); q.blur(); }
    if (e.key === 'Enter') { clearTimeout(t); void runSearch(q.value.trim()); }
  });
  document.addEventListener('keydown', onKey);
  window.addEventListener('popstate', () => {
    if (!fileDirty()) { readLocation(); navigate(D.bid, D.path, false); return; }
    const back = dataUrl(D.bid, D.path);
    void leaveFile().then((ok) => {
      if (!ok) { history.pushState({ dc: 1 }, '', back); return; }
      readLocation();
      navigate(D.bid, D.path, false);
    });
  });
  document.addEventListener('me-changed', () => render());
}

let searchAbort: AbortController | null = null;

async function runSearch(text: string): Promise<void> {
  if (!text) { if (D.search) { D.search = null; render(); } return; }
  const tags = [...text.matchAll(/(?:^|\s)#(\S+)/g)].map((m) => m[1] ?? '').filter(Boolean);
  const qtext = text.replace(/(?:^|\s)#\S+/g, ' ').trim();
  const glob = /[*?]/.test(qtext) ? qtext : '';
  D.search = { q: text, res: null, err: null };
  render();
  searchAbort?.abort();                            // the previous search is superseded
  const ac = new AbortController();
  searchAbort = ac;
  try {
    const res = await api.search({ q: glob ? '' : qtext, tags, glob }, ac.signal);
    if (D.search?.q === text) D.search.res = res;
  } catch (e) {
    if (ac.signal.aborted) return;
    if (D.search?.q === text) D.search.err = errText(e);
  }
  if (D.search?.q === text) render();
}

const typing = (t: EventTarget | null): boolean =>
  t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement || (t instanceof HTMLElement && t.isContentEditable);

// The bucket tree (WAI-ARIA tree pattern): Up / Down move between visible rows, Right opens a folder (or moves into
// it when open), Left closes it (or moves to its parent), Home / End jump. A toggle re-renders the tree, so the focus is
// put back on the row with the same bucket + path after the paint.
function treeKey(e: KeyboardEvent, row: HTMLElement): boolean {
  const tree = $('dc-tree');
  if (!tree || e.ctrlKey || e.metaKey || e.altKey) return false;
  const rows = [...tree.querySelectorAll<HTMLElement>('.t-row[role="treeitem"]')];
  const i = rows.indexOf(row);
  const depth = (r: HTMLElement): number => Number(r.style.getPropertyValue('--d')) || 0;
  const go = (r: HTMLElement | undefined): void => {
    if (!r) return;
    for (const x of rows) if (x.tabIndex === 0) x.tabIndex = -1;
    r.tabIndex = 0;
    r.focus();
    r.scrollIntoView({ block: 'nearest' });
  };
  const refocus = (): void => {
    const sel = `.t-row[role="treeitem"][data-bid="${CSS.escape(row.dataset['bid'] ?? '')}"][data-path="${CSS.escape(row.dataset['path'] ?? '')}"]`;
    requestAnimationFrame(() => tree.querySelector<HTMLElement>(sel)?.focus());
  };
  const exp = row.getAttribute('aria-expanded');
  switch (e.key) {
    case 'ArrowDown': go(rows[i + 1]); break;
    case 'ArrowUp': go(rows[i - 1]); break;
    case 'Home': go(rows[0]); break;
    case 'End': go(rows[rows.length - 1]); break;
    case 'ArrowRight':
      if (exp === 'false') { row.querySelector<HTMLElement>('.tw')?.click(); refocus(); } else if (exp === 'true') go(rows[i + 1]);
      break;
    case 'ArrowLeft':
      if (exp === 'true') { row.querySelector<HTMLElement>('.tw')?.click(); refocus(); } else {
        const d = depth(row);
        go(rows.slice(0, i).reverse().find((r) => depth(r) < d));
      }
      break;
    default: return false;
  }
  e.preventDefault();
  return true;
}

function onKey(e: KeyboardEvent): void {
  if (document.querySelector('.ui-dlg-wrap, #settings:not(.hidden), .dc-modal')) return;   // a dialog has the keys
  if (typing(e.target) || fileOpen()) return;       // the file view handles its own keys (Ctrl+S)
  const mod = e.ctrlKey || e.metaKey;
  if (e.key === '/' && !mod) { e.preventDefault(); $('dc-q')?.focus(); return; }
  if (e.key === 'Escape') {
    if (D.treeOpen || D.detailsOpen) { D.treeOpen = false; D.detailsOpen = false; render(); return; }
    if (D.sel.size) { D.sel.clear(); D.anchor = null; selChanged(); return; }
    if (D.search) { D.search = null; ($('dc-q') as HTMLInputElement).value = ''; render(); }
    return;
  }
  if (!D.listing || D.search) return;
  if (mod && e.key.toLowerCase() === 'a') {
    e.preventDefault();
    for (const x of D.listing.entries) D.sel.add(x.path);
    selChanged();
    return;
  }
  if ((e.key === 'Delete' || (e.key === 'Backspace' && mod)) && D.sel.size) { e.preventDefault(); void deleteSel(); return; }
  if (e.key === 'F2' && D.sel.size === 1) { e.preventDefault(); void renameSel(); }
}

// ---------------------------------------------------------------- entry

const APP_ACTIONS: Record<string, Action> = {
  settings: () => openSettings('datasets'),
  theme: () => { setTheme(THEME.name === 'dark' ? 'light' : 'dark'); render(); },
  'toggle-tree': () => { D.treeOpen = !D.treeOpen; D.detailsOpen = false; render(); },
  'show-details': () => { D.detailsOpen = true; D.treeOpen = false; render(); },
  'close-overlays': () => { D.treeOpen = false; D.detailsOpen = false; render(); },
  go: (el) => { closeFloat(); navigate(el.dataset['bid'] || null, el.dataset['path'] ?? ''); },
  'clear-search': () => { D.search = null; ($('dc-q') as HTMLInputElement).value = ''; render(); },
  retry: () => { void reload(); },
};

export async function start(): Promise<void> {
  const root = $req('datacenter');
  root.innerHTML = SKELETON();
  mountUserMenu($req('dc-hactions'));
  const v = pref(PREF.view), c = pref(PREF.cards), s = pref(PREF.sort), bs = pref(PREF.bucketSort);
  if (v === 'thumbs' || v === 'names') D.view = v as ViewMode;
  if (c === 'grid' || c === 'list') D.cards = c as CardMode;
  if (s === 'name' || s === '-mtime' || s === 'size') D.sort = s as LsSort;
  if (bs === 'recent' || bs === 'name' || bs === 'size' || bs === 'images') D.bucketSort = bs as BucketSort;
  Object.assign(ACTIONS, APP_ACTIONS, TREE_ACTIONS, BUCKET_ACTIONS, BROWSER_ACTIONS, DETAIL_ACTIONS, OPS_ACTIONS, UPLOAD_ACTIONS, UPLOADS_ACTIONS, AI_ACTIONS, EDIT_ACTIONS, AUTOLABEL_ACTIONS, ACTIVITY_ACTIONS, EDITOR_ACTIONS, FRAMES_ACTIONS, LOCAL_ACTIONS, CTX_ACTIONS);
  hooks.navigate = navigate;
  hooks.reload = reload;
  hooks.render = render;
  hooks.selChanged = selChanged;
  hooks.details = renderDetails;
  bind(root);
  bindBrowser();
  bindUpload();
  bindAi();
  bindCtxMenu();
  bindResize();
  readLocation();
  history.replaceState({ dc: 1 }, '', dataUrl(D.bid, D.path));
  render();                                        // the frame at once (skeleton rows), then the data
  await loadBuckets();
  navigate(D.bid, D.path, false);
  renderAi();
  watchJobs();
}
