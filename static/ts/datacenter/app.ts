// The Data Center page (?data[=<bucket>&path=…], design "Explorer"): its header (Sessions | Data tabs, file search,
// settings, theme, user menu) and the 3-pane layout: tree (tree.ts) | the All buckets overview (buckets.ts) or a
// folder (browser.ts) | details (details.ts), with the AI drawer (ai.ts) docked under the centre and right panes.
// It owns the routing (bucket + path live in the URL: pushState on navigate, popstate goes back), the one delegated
// click / change / input listener of the page (elements carry data-act="<action>"; each pane module exports its
// actions), the keyboard (/ search, Delete, F2 rename, Ctrl+A, Esc) and the loads (buckets, summary, tags).

import { errText } from '../core/api.js';
import { pref } from '../core/auth.js';
import { $, $req, esc, icon, ICONS } from '../core/dom.js';
import { dataUrl, homeUrl } from '../core/nav.js';
import { closeFloat } from '../core/popover.js';
import { setTheme, THEME } from '../core/theme.js';
import { openSettings } from '../settings/dialog.js';
import { mountUserMenu } from '../views/usermenu.js';
import * as api from './api.js';
import { AI_ACTIONS, aiScopeChanged, bindAi, renderAi } from './ai.js';
import { BROWSER_ACTIONS, bindBrowser, loadListing, renderFolder, renderSearch, selChanged } from './browser.js';
import { BUCKET_ACTIONS, renderOverview } from './buckets.js';
import { DETAIL_ACTIONS, renderDetails } from './details.js';
import { deleteSel, OPS_ACTIONS, renameSel } from './ops.js';
import { bucketOf, D, hooks, key, parentOf, PREF } from './state.js';
import { renderTree, TREE_ACTIONS } from './tree.js';
import { bindUpload, UPLOAD_ACTIONS } from './upload.js';
import type { BucketSort, CardMode, ViewMode } from './state.js';
import type { LsSort } from '../types-datacenter.js';

/** A delegated action: the element that carries data-act and the event. */
export type Action = (el: HTMLElement, ev: Event) => void;

const ACTIONS: Record<string, Action> = {};

const SKELETON = (): string => `
<header class="app-header dc-header">
  <a class="brand" href="${esc(homeUrl())}" aria-label="Dataset Visualizer: Sessions">
    <svg class="brand-mark" viewBox="0 0 32 32" fill="none" stroke="currentColor" aria-hidden="true"><use href="${ICONS}#i-brand"/></svg>
    <h1>Dataset Visualizer</h1>
  </a>
  <nav class="hdr-nav" aria-label="Pages">
    <a href="${esc(homeUrl())}">${icon('layers')}Sessions</a>
    <a class="on" href="${esc(dataUrl())}" aria-current="page">${icon('db')}Data<span id="dc-nav-n" class="count num"></span></a>
  </nav>
  <span class="grow"></span>
  <div class="dc-hsearch" role="search">
    ${icon('search')}<input id="dc-q" type="search" placeholder="Search files, folders, tags…" aria-label="Search files, folders and tags" autocomplete="off" spellcheck="false">
    <kbd aria-hidden="true">/</kbd>
  </div>
  <div id="dc-hactions" class="header-actions">
    <button type="button" class="icon-btn ghost dc-only-narrow" data-act="toggle-tree" aria-label="Show the bucket tree" title="Buckets">${icon('list')}</button>
    <button type="button" class="icon-btn ghost" data-act="settings" aria-label="Settings: connections, labeling, appearance" title="Settings">${icon('gear')}</button>
    <button type="button" class="icon-btn ghost" data-act="theme" aria-label="Switch light / dark theme" title="Switch light / dark theme">
      <svg class="ico only-dark" aria-hidden="true"><use href="${ICONS}#i-sun"/></svg><svg class="ico only-light" aria-hidden="true"><use href="${ICONS}#i-moon"/></svg></button>
  </div>
</header>
<div id="dc-app" class="dc-app">
  <aside id="dc-tree" class="dc-tree" aria-label="Buckets and folders"></aside>
  <section id="dc-main" class="dc-main" aria-label="Files">
    <div id="dc-bar" class="dc-bar"></div>
    <div id="dc-strip" class="dc-strip hidden"></div>
    <div id="dc-selbar" class="dc-selbar hidden" role="toolbar" aria-label="Selection"></div>
    <div id="dc-body" class="dc-body" tabindex="-1"></div>
    <div id="dc-drop" class="dc-drop hidden" aria-hidden="true">${icon('upload')}<b>Drop files to upload here</b><span id="dc-drop-to"></span></div>
  </section>
  <aside id="dc-details" class="dc-details" aria-label="Details"></aside>
  <section id="dc-ai" class="dc-drawer hidden" data-llm="data_center" aria-label="Data Center assistant"></section>
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

function bind(root: HTMLElement): void {
  root.addEventListener('click', (e) => {
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-act]') : null;
    if (!el || !root.contains(el) || (el instanceof HTMLButtonElement && el.disabled)) return;
    const a = ACTIONS[el.dataset['act'] ?? ''];
    if (a) a(el, e);
  });
  // Enter / Space on a role=button element acts like a click
  root.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const t = e.target;
    if (!(t instanceof HTMLElement) || t.getAttribute('role') !== 'button' || !t.dataset['act']) return;
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
    readLocation();
    navigate(D.bid, D.path, false);
  });
  document.addEventListener('me-changed', () => render());
}

async function runSearch(text: string): Promise<void> {
  if (!text) { if (D.search) { D.search = null; render(); } return; }
  const tags = [...text.matchAll(/(?:^|\s)#(\S+)/g)].map((m) => m[1] ?? '').filter(Boolean);
  const qtext = text.replace(/(?:^|\s)#\S+/g, ' ').trim();
  const glob = /[*?]/.test(qtext) ? qtext : '';
  D.search = { q: text, res: null, err: null };
  render();
  try {
    const res = await api.search({ q: glob ? '' : qtext, tags, glob });
    if (D.search?.q === text) D.search.res = res;
  } catch (e) {
    if (D.search?.q === text) D.search.err = errText(e);
  }
  if (D.search?.q === text) render();
}

const typing = (t: EventTarget | null): boolean =>
  t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement || (t instanceof HTMLElement && t.isContentEditable);

function onKey(e: KeyboardEvent): void {
  if (document.querySelector('.ui-dlg-wrap, #settings:not(.hidden), .dc-modal')) return;   // a dialog has the keys
  if (typing(e.target)) return;
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
  Object.assign(ACTIONS, APP_ACTIONS, TREE_ACTIONS, BUCKET_ACTIONS, BROWSER_ACTIONS, DETAIL_ACTIONS, OPS_ACTIONS, UPLOAD_ACTIONS, AI_ACTIONS);
  hooks.navigate = navigate;
  hooks.reload = reload;
  hooks.render = render;
  hooks.selChanged = selChanged;
  hooks.details = renderDetails;
  bind(root);
  bindBrowser();
  bindUpload();
  bindAi();
  readLocation();
  history.replaceState({ dc: 1 }, '', dataUrl(D.bid, D.path));
  render();                                        // the frame at once (skeleton rows), then the data
  await loadBuckets();
  navigate(D.bid, D.path, false);
  renderAi();
}
