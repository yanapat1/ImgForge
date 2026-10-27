// The image browser: what the left panel shows while nothing is selected. Every image of the dataset (background
// images with no box too), filtered by the filter bar, sorted by name (numbers by value), paged. Tiles are server
// thumbnails with the boxes drawn (api/img/thumb, cached on disk per box set, so an edit shows at once). The images
// with boxes come from the points payload the page already has; only the background ones are listed by the server.

import { getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { errMessage } from '../core/api.js';
import { bgCanPass, namePasses } from '../core/filter.js';
import type { Agg, FilterResult } from '../core/filter.js';
import { S, UI } from '../core/state.js';
import type { ImagesPayload } from '../types.js';
import { FB, filterAgg } from './filter-bar.js';
import { TIPS_HTML } from './gallery.js';
import { isImageUnit } from './preview.js';

/** GET api/edits/status: what a re-index would place in the plot. */
export interface EditStatus { pending_new: number; moved: number; images?: unknown }
/** Every image name in display order, with its position. */
interface Ranks { names: string[]; of: Map<string, number> }
interface ImageGridState {
  bg: string[]; pending: Record<string, unknown>; total: number; loaded: boolean; loading: Promise<void> | null;
  page: number; list: string[] | null; listRes: FilterResult | null; listRank: Ranks | null; fKey: string;
  rank: Ranks | null; rankKey: string | null; ver: Record<string, number>; status: EditStatus;
}
const IG: ImageGridState = { bg: [], pending: {}, total: 0, loaded: false, loading: null, page: 0, list: null, listRes: null,
  listRank: null, fKey: '', rank: null, rankKey: null, ver: {}, status: { pending_new: 0, moved: 0 } };
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

// a named list of images shown instead of every image (the Compare tab's "Only in A", "Labels differ"…); the
// filter bar still applies. Cleared by its × chip, or when a box selection fills the panel.
let subset: { names: string[]; title: string; set: Set<string> } | null = null;

/** Show only `names` (in display order) in the image browser, titled `title`; null goes back to every image. */
export function setImageSubset(names: string[] | null, title = ''): void {
  subset = names ? { names: names.slice().sort(collator.compare), title, set: new Set(names) } : null;
  IG.page = 0;
  IG.list = null;
  renderImageGrid();
}
export const imageSubset = (): { names: string[]; title: string } | null => subset;
window.addEventListener('selection-changed', () => { if (S.sel.length && subset) { subset = null; IG.list = null; } });

export async function loadImages(): Promise<void> {
  if (IG.loading) return IG.loading;
  IG.loading = (async () => {
    try {
      const r = await getJSON<ImagesPayload>('api/images');
      IG.bg = r.bg;
      IG.pending = r.pending || {};
      IG.total = r.total;
      IG.loaded = true;
      IG.list = null;
      IG.rank = null;
      const st = await getJSON<EditStatus>('api/edits/status').catch(() => null);
      if (st) IG.status = livePending === null ? st : { ...st, pending_new: livePending };
    } finally { IG.loading = null; }
  })();
  return IG.loading;
}

// every image name in display order (cached per payload / background list)
function ranks(): Ranks {
  const agg: Agg | null = S.P ? filterAgg() : null;
  const key = `${agg ? agg.nImg : 0}:${IG.bg.length}:${agg && agg.n}`;
  if (IG.rank && IG.rankKey === key) return IG.rank;
  const names = (agg ? agg.names.slice() : [] as string[]).concat(IG.bg.filter((n) => !(agg && agg.imgOf.has(n))));
  names.sort(collator.compare);
  IG.rank = { names, of: new Map(names.map((n, i): [string, number] => [n, i])) };
  IG.rankKey = key;
  return IG.rank;
}

// the images that pass the filter bar, in display order
export function imageOrder(): string[] {
  if (subset) {
    const res: FilterResult | null = FB.res;
    if (!res || !res.mask || !res.active) return subset.names;
    const all = new Set(baseOrder());                // the filter's images (+ background ones that pass)
    return subset.names.filter((n) => all.has(n));
  }
  return baseOrder();
}

function baseOrder(): string[] {
  const R = ranks();
  const res: FilterResult | null = FB.res, F = FB.F;
  if (!res || !res.mask || !res.active) {
    IG.list = R.names;
    return R.names;
  }
  if (IG.list && IG.listRes === res && IG.listRank === R) return IG.list;
  const agg: Agg | null = filterAgg();
  if (!agg) throw new Error('image index not built');
  const hit = new Uint8Array(agg.nImg), mask = res.mask, ii = agg.imgIdx;
  for (let i = 0; i < mask.length; i++) if (mask[i]) hit[ii[i] ?? 0] = 1;
  const out: string[] = [];
  for (let j = 0; j < agg.nImg; j++) if (hit[j]) out.push(agg.names[j] ?? '');
  if (IG.bg.length && bgCanPass(F)) {
    const bg = IG.bg.filter((n) => !agg.imgOf.has(n));
    let ok: Uint8Array | null = null;
    if (F.name.q.trim()) {
      const m = namePasses(bg, F.name);
      ok = m instanceof Uint8Array ? m : null;
    }
    bg.forEach((n, k) => { if (!ok || ok[k]) out.push(n); });
  }
  out.sort((a, b) => (R.of.get(a) ?? 0) - (R.of.get(b) ?? 0));
  IG.list = out;
  IG.listRes = res;
  IG.listRank = R;
  return out;
}

const thumbSize = () => Math.round((+$req<HTMLInputElement>('thumb-size').value || 110) * (window.devicePixelRatio || 1));

function tileHTML(name: string): string {
  const agg: Agg | null = S.P ? filterAgg() : null;
  const j = agg ? agg.imgOf.get(name) : undefined;
  const n = (agg && j !== undefined ? agg.nbox[j] ?? 0 : 0) + (Number(IG.pending[name]) || 0);
  const v = IG.ver[name] ? `&v=${IG.ver[name]}` : '';
  return `<div class="card img-card${n ? '' : ' bg'}" data-img="${esc(name)}" role="button" tabindex="0" title="${esc(name)}">` +
    `<img loading="lazy" decoding="async" alt="" src="api/img/thumb?name=${encodeURIComponent(name)}&size=${thumbSize()}${v}">` +
    (isImageUnit() ? '' : `<span class="badge">${n ? `${n} box${n === 1 ? '' : 'es'}` : 'background'}</span>`) +
    `<span class="img-name">${esc(name)}</span></div>`;
}

// the left panel with no selection: the grid, or (while the image list loads) a placeholder
export function renderImageGrid(): void {
  const g = $req('gallery');
  if (!IG.loaded) {
    g.innerHTML = '<div class="empty"><h3>Loading images…</h3></div>';
    $req('pager').innerHTML = '';
    loadImages().then(() => { if (!S.sel.length) renderImageGrid(); }).catch((e) => {
      g.innerHTML = `<div class="empty"><h3>Could not list the images</h3><div>${esc(errMessage(e))}</div></div>`;
    });
    return;
  }
  const list = imageOrder();
  const fres: FilterResult | null = FB.res;
  const filtered = fres && fres.active;
  $req('gallery-title').textContent = subset ? subset.title : filtered ? 'Filtered images' : 'All images';

  $req('gallery-count').textContent = `${list.length.toLocaleString()} image${list.length === 1 ? '' : 's'}`;
  renderBanner();
  if (!list.length) {
    g.innerHTML = `<div class="empty"><h3>No image matches the filter</h3><div>Change or clear the filter above the plot.</div></div>`;
    $req('pager').innerHTML = '';
    return;
  }
  const size = UI.page_size;
  const pages = Math.ceil(list.length / size);
  IG.page = Math.max(0, Math.min(IG.page, pages - 1));
  const start = IG.page * size;
  if (!g.dataset['subsetBound']) {                // the × of a subset: back to every image (one delegated listener)
    g.dataset['subsetBound'] = '1';
    g.addEventListener('click', (e) => {
      if (e.target instanceof Element && e.target.closest('[data-subset-clear]')) setImageSubset(null);
    });
  }
  const head = subset ? `<div class="img-subset"><span>${esc(subset.title)}</span>` +
    `<button type="button" class="ghost" data-subset-clear title="Show every image again">${icon('x')}All images</button></div>` : '';
  g.innerHTML = head + `<details class="img-hint muted"><summary>${icon('image')}No selection: every image is listed. Lasso dots in the plot to see their box crops · click an image to view, copy or edit it</summary>${TIPS_HTML}</details>` +
    list.slice(start, start + size).map(tileHTML).join('');
  g.scrollTop = 0;
  $req('pager').innerHTML = pages > 1
    ? `<button data-ip="${IG.page - 1}" aria-label="Previous page" ${IG.page === 0 ? 'disabled' : ''}>${icon('chev-l')}</button>` +
      `<span class="muted">page <span class="num">${IG.page + 1} / ${pages}</span></span>` +
      `<button data-ip="${IG.page + 1}" aria-label="Next page" ${IG.page >= pages - 1 ? 'disabled' : ''}>${icon('chev-r')}</button>`
    : '';
}

export function setImagePage(p: number): void { IG.page = p; renderImageGrid(); }

// the filter ran (debounced by the filter bar): a new spec goes back to page 1, a re-run (after an edit) stays
export function onFilterChanged(): void {
  const k = JSON.stringify(FB.F);
  if (k !== IG.fKey) { IG.fKey = k; IG.page = 0; }
  IG.list = null;
  if (!S.sel.length && IG.loaded) renderImageGrid();
}

// after a save in the viewer: new thumbnail, pending count, banner
export function onImageEdited(name: string, status?: EditStatus | null): void {
  IG.ver[name] = (IG.ver[name] || 0) + 1;
  if (status) IG.status = livePending === null ? status : { ...status, pending_new: livePending };
  void loadImages().then(() => {
    if (!S.sel.length) renderImageGrid();
    else renderBanner();
  });
}

// boxes the editor added / moved are not in the plot until a re-index (crops, features, projection)
function renderBanner(): void {
  const el = $('edit-banner');
  if (!el) return;
  const { pending_new: n, moved: m } = IG.status;
  el.classList.toggle('hidden', !n && !m);
  if (!n && !m) return;
  el.innerHTML = `${icon('alert')}<span>${n ? `<b>${n}</b> new box${n === 1 ? '' : 'es'}` : ''}${n && m ? ' and ' : ''}` +
    `${m ? `<b>${m}</b> moved` : ''} since the index${livePending !== null && n
      ? ': shown as provisional dots (rings), re-index to place them exactly.' : ': re-index to place them in the plot.'}</span>` +
    `<button id="edit-reindex" class="ghost">${icon('refresh')}Re-index</button>`;
  $req('edit-reindex').onclick = reindex;
}

async function reindex(): Promise<void> {
  const ds = S.status && S.status.dataset;
  if (!ds || !ds.id) return;
  $req<HTMLButtonElement>('edit-reindex').disabled = true;
  try {
    await postJSON(`api/datasets/${encodeURIComponent(ds.id)}/open`, {});
    location.reload();
  } catch (e) { $req<HTMLButtonElement>('edit-reindex').disabled = false; alert(`Re-index failed: ${errMessage(e)}`); }
}

export const bannerStatus = (): EditStatus => IG.status;

// a session with a live feed (core/live.ts): a box drawn in this page's editor is also a live `new` entry, so the
// count of not-yet-indexed boxes comes from the feed's pending_new alone (never api/edits/status + the feed)
let livePending: number | null = null;
export function setLivePending(n: number | null): void {
  livePending = n;
  if (n !== null) IG.status = { ...IG.status, pending_new: n };
  renderBanner();
}
