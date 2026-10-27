// The All buckets overview (centre pane, no bucket open): its toolbar (name filter, Tags filter menu, sort,
// grid / list, Link source, Import data), the summary line, the format filter chips and one card per bucket: a
// mosaic of sample thumbnails (GET …/file?path=&w=, found through search once per bucket), the format badge, a
// warning line (unlabeled images, no layout matched), counts and tags. A click shows its details on the right,
// a double click (or Enter, or its name) opens it.

import { rememberPref } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import { floatPopover } from '../core/popover.js';
import * as api from './api.js';
import type { Action } from './app.js';
import {
  D, fbadge, fmtKey, fmtN, fmtSize, FORMATS, hooks, IMG_RE, PREF, SOURCE, srcIcon,
  tagChip, timeAgo, timeOf,
} from './state.js';
import type { BucketSort } from './state.js';
import type { Bucket } from '../types-datacenter.js';

const MOSAIC = 5;
const SORTS: Array<[BucketSort, string]> = [['recent', 'Recently changed'], ['name', 'Name'], ['size', 'Size'], ['images', 'Images']];

function visible(): Bucket[] {
  const q = D.q.toLowerCase();
  const list = D.buckets.filter((b) =>
    (!q || b.name.toLowerCase().includes(q) || b.tags.some((t) => t.tag.toLowerCase().includes(q))) &&
    (!D.fmtFilter || fmtKey(b.format) === D.fmtFilter) &&
    [...D.tagFilter].every((t) => b.tags.some((x) => x.tag === t)));
  const by: Record<BucketSort, (a: Bucket, b: Bucket) => number> = {
    recent: (a, b) => timeOf(b.updated_at ?? b.created_at) - timeOf(a.updated_at ?? a.created_at),
    name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }),
    size: (a, b) => (b.stats?.size ?? 0) - (a.stats?.size ?? 0),
    images: (a, b) => (b.stats?.images ?? 0) - (a.stats?.images ?? 0),
  };
  return list.sort(by[D.bucketSort]);
}

/** A thumbnail mosaic: sample image paths per bucket, found once (Bucket.samples when the server sends them). */
function mosaic(b: Bucket): string {
  const s = b.samples?.length ? b.samples : D.samples.get(b.id);
  if (s === undefined) { void loadSamples(b); }
  const imgs = Array.isArray(s) ? s.slice(0, MOSAIC) : [];
  if (!imgs.length) {
    const vec = b.format?.format === 'embeddings' || (b.stats?.vectors ?? 0) > 0;
    const what = s === 'loading' || s === undefined ? '' : vec ? `${fmtN(b.stats?.vectors ?? null)} vectors · no images` : 'no images';
    return `<div class="dc-mosaic ph">${what ? `${icon(vec ? 'vectors' : 'image')}<span>${esc(what)}</span>` : ''}</div>`;
  }
  return `<div class="dc-mosaic n${imgs.length}">${imgs.map((p, i) =>
    `<img src="${esc(api.fileUrl(b.id, p, i ? 160 : 320))}" alt="" loading="lazy" decoding="async">`).join('')}</div>`;
}

async function loadSamples(b: Bucket): Promise<void> {
  D.samples.set(b.id, 'loading');
  try {
    const r = await api.search({ bucket: b.id, glob: '*.jpg' });
    let files = r.files.filter((f) => IMG_RE.test(f.name));
    if (!files.length) files = (await api.search({ bucket: b.id, glob: '*.png' })).files;
    // spread over the bucket, not the first 5 names of one folder
    const step = Math.max(1, Math.floor(files.length / MOSAIC));
    D.samples.set(b.id, files.filter((_, i) => i % step === 0).slice(0, MOSAIC).map((f) => f.path));
  } catch (e) {
    D.samples.set(b.id, []);
  }
  if (!D.bid && !D.search) renderBody();
}

/** The warning line of a card: what is off about the bucket, else when it changed. */
function warnLine(b: Bucket): string {
  const k = fmtKey(b.format);
  const st = b.stats ?? {};
  let w = '';
  if (k === 'other') w = 'no layout matched';
  else if (k.startsWith('yolo') && st.images != null && st.labels != null && st.labels < st.images) w = `${fmtN(st.images - st.labels)} unlabeled`;
  else { const bad = b.format?.checks.find((c) => !c.ok); if (bad) w = bad.name; }
  if (w) return `<span class="warnline">${icon('alert')}${esc(w)}</span>`;
  const t = timeAgo(b.updated_at ?? b.created_at);
  return t ? `<span class="when">${esc(t)}</span>` : '';
}

function stats(b: Bucket): string {
  const st = b.stats ?? {};
  const v = (n: number | null | undefined, u: string): string => `<span><b>${n == null ? '–' : fmtN(n)}</b> ${u}</span>`;
  if (fmtKey(b.format) === 'embeddings' || (!st.images && st.vectors)) {
    return `${v(st.vectors, 'vectors')}${st.dims ? v(st.dims, 'dims') : ''}<span><b>${fmtSize(st.size)}</b></span>`;
  }
  return `${v(st.images, 'img')}${v(st.labels, 'labels')}${v(st.classes, 'cls')}<span><b>${fmtSize(st.size)}</b></span>`;
}

function card(b: Bucket): string {
  const user = b.tags.filter((t) => !t.auto);
  return `<article class="dc-bcard${D.focus === b.id ? ' sel' : ''}" data-act="focus-bucket" data-bid="${esc(b.id)}" role="button" tabindex="0" aria-pressed="${D.focus === b.id}" aria-label="${esc(b.name)}">` +
    mosaic(b) +
    `<div class="bc-body"><div class="bc-title"><button type="button" class="bc-name" data-act="go" data-bid="${esc(b.id)}" data-path="" title="Open ${esc(b.name)}">${esc(b.name)}</button>` +
    `<span class="src">${srcIcon(b)}${esc(SOURCE[b.kind]?.[1] ?? b.kind)}</span></div>` +
    `<div class="bc-row">${fbadge(b.format)}${warnLine(b)}</div>` +
    `<div class="bc-stats num">${stats(b)}</div>` +
    `<div class="bc-tags">${user.map((t) => tagChip(t.tag)).join('') || '<span class="muted">no tags</span>'}</div></div></article>`;
}

function listRow(b: Bucket): string {
  const st = b.stats ?? {};
  return `<div class="dc-brow${D.focus === b.id ? ' sel' : ''}" data-act="focus-bucket" data-bid="${esc(b.id)}" role="button" tabindex="0">` +
    `<span class="c-n">${srcIcon(b)}<button type="button" class="bc-name" data-act="go" data-bid="${esc(b.id)}" data-path="">${esc(b.name)}</button></span>` +
    `<span>${fbadge(b.format)}</span><span class="r num">${fmtN(st.images ?? st.vectors ?? null)}</span><span class="r num">${fmtN(st.labels ?? null)}</span>` +
    `<span class="r num">${fmtSize(st.size)}</span><span class="c-t">${b.tags.filter((t) => !t.auto).map((t) => tagChip(t.tag)).join('')}</span>` +
    `<span class="c-m num">${esc(timeAgo(b.updated_at ?? b.created_at))}</span></div>`;
}

function renderBar(): void {
  const bar = document.getElementById('dc-bar');
  if (!bar) return;
  const tf = D.tagFilter.size ? [...D.tagFilter].join(', ') : 'any';
  bar.innerHTML = `<span class="dc-here">${icon('grid')}All buckets</span>` +
    `<label class="dc-input dc-bfilter">${icon('search')}<input id="dc-bq" type="search" placeholder="Filter buckets…" aria-label="Filter buckets by name or tag" value="${esc(D.q)}" data-input="bfilter" autocomplete="off"></label>` +
    `<button type="button" class="dc-dd" data-act="tags-menu" aria-haspopup="menu" aria-expanded="false">${icon('tag')}<span>Tags: ${esc(tf)}</span>${icon('chev-d')}</button>` +
    `<label class="dc-dd dc-sortsel">${icon('sort')}<select data-change="bsort" aria-label="Sort buckets">${SORTS.map(([k, l]) => `<option value="${k}"${D.bucketSort === k ? ' selected' : ''}>${l}</option>`).join('')}</select></label>` +
    `<div class="segmented" role="group" aria-label="Cards or list">` +
    `<button type="button" class="icon-btn view-btn${D.cards === 'grid' ? ' active' : ''}" data-act="cards" data-mode="grid" aria-pressed="${D.cards === 'grid'}" aria-label="Cards" title="Cards">${icon('grid')}</button>` +
    `<button type="button" class="icon-btn view-btn${D.cards === 'list' ? ' active' : ''}" data-act="cards" data-mode="list" aria-pressed="${D.cards === 'list'}" aria-label="List" title="List">${icon('list')}</button></div>` +
    `<span class="grow"></span>` +
    `<button type="button" data-act="link" class="dc-wide">${icon('link')}Link source</button>` +
    `<button type="button" class="primary" data-act="import">${icon('upload')}<span class="dc-wide-txt">Import data</span></button>` +
    `<button type="button" class="icon-btn dc-only-overlay" data-act="show-details" aria-label="Details" title="Details">${icon('eye')}</button>`;
}

function renderBody(): void {
  const body = document.getElementById('dc-body');
  if (!body) return;
  if (D.loadErr && !D.buckets.length) {
    body.innerHTML = `<div class="dc-empty">${icon('alert')}<b>Could not load the buckets</b><span class="muted">${esc(D.loadErr)}</span><button type="button" data-act="retry">${icon('refresh')}Retry</button></div>`;
    return;
  }
  const list = visible();
  const s = D.summary;
  const images = D.buckets.reduce((a, b) => a + (b.stats?.images ?? 0), 0);
  const vectors = D.buckets.reduce((a, b) => a + (b.stats?.vectors ?? 0), 0);
  const size = D.buckets.reduce((a, b) => a + (b.stats?.size ?? 0), 0);
  const kinds = { managed: 0, local: 0, s3: 0 };
  for (const b of D.buckets) kinds[b.kind] = (kinds[b.kind] ?? 0) + 1;
  const sumLine = [`${fmtN(s?.images ?? images)} images`, vectors ? `${fmtN(vectors)} vectors` : '', fmtSize(s?.size ?? size),
    `${kinds.managed} managed, ${kinds.local} local link${kinds.local === 1 ? '' : 's'}, ${kinds.s3} S3 link${kinds.s3 === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
  const counts = new Map<string, number>();
  for (const b of D.buckets) counts.set(fmtKey(b.format), (counts.get(fmtKey(b.format)) ?? 0) + 1);
  const chips = [`<button type="button" class="dc-fchip${!D.fmtFilter ? ' on' : ''}" data-act="fmtf" data-fmt="" aria-pressed="${!D.fmtFilter}">All<span class="n">${D.buckets.length}</span></button>`,
    ...[...FORMATS.map(([k]) => k), 'empty'].filter((k) => counts.has(k)).map((k) =>
      `<button type="button" class="dc-fchip${D.fmtFilter === k ? ' on' : ''}" data-act="fmtf" data-fmt="${k}" title="${counts.get(k)} bucket${counts.get(k) === 1 ? '' : 's'}" aria-pressed="${D.fmtFilter === k}">${fbadge(k).replace('class="dc-fb', 'class="dc-fb bare')}</button>`)];
  const n = D.buckets.length;
  let content: string;
  if (!n) {
    content = `<div class="dc-empty big">${icon('db')}<b>No data yet</b><span class="muted">Import images or a zip into a managed bucket, or link a local folder or an S3 prefix you already have. ` +
      `Data Center detects the format (YOLO, COCO, VOC, class folders…) and tags it for you.</span>` +
      `<div class="row"><button type="button" class="primary" data-act="import">${icon('upload')}Import data</button><button type="button" data-act="link">${icon('link')}Link source</button></div></div>`;
  } else if (!list.length) {
    content = `<div class="dc-empty">${icon('filter')}<b>No bucket matches</b><button type="button" data-act="clear-bfilters">Clear filters</button></div>`;
  } else if (D.cards === 'list') {
    content = `<div class="dc-blist" role="list"><div class="dc-brow head"><span>Name</span><span>Format</span><span class="r">Images</span><span class="r">Labels</span><span class="r">Size</span><span>Tags</span><span>Changed</span></div>${list.map(listRow).join('')}</div>`;
  } else content = `<div class="dc-cards">${list.map(card).join('')}</div>`;
  body.innerHTML = `<div class="dc-pad">${n ? `<div class="dc-sum"><h2>${n} bucket${n === 1 ? '' : 's'}</h2><span class="muted num">${esc(sumLine)}</span></div>` +
    `<div class="dc-fchips" role="group" aria-label="Filter by format">${chips.join('')}</div>` : ''}${content}</div>`;
}

export function renderOverview(): void {
  renderBar();
  document.getElementById('dc-strip')?.classList.add('hidden');
  document.getElementById('dc-selbar')?.classList.add('hidden');
  renderBody();
}

function tagsMenu(from: HTMLElement): void {
  const m = document.getElementById('dc-menu');
  if (!m) return;
  const tags = D.tags.filter((t) => !t.auto);
  m.innerHTML = `<div class="dc-menu-h">Show buckets tagged with all of</div>` + (tags.length ? tags.map((t) =>
    `<label class="check"><input type="checkbox" data-change="tagcheck" value="${esc(t.tag)}"${D.tagFilter.has(t.tag) ? ' checked' : ''}>${esc(t.tag)}<span class="n num">${t.count}</span></label>`).join('')
    : '<div class="muted dc-menu-note">No tags yet: add them from the details panel.</div>') +
    (D.tagFilter.size ? `<button type="button" class="ghost sm" data-act="clear-tagf">Any tag</button>` : '');
  floatPopover(m, from, undefined, 'left');
}

export const BUCKET_ACTIONS: Record<string, Action> = {
  'focus-bucket': (el, ev) => {
    const bid = el.dataset['bid'] ?? '';
    if (ev instanceof MouseEvent && ev.detail >= 2) { hooks.navigate(bid, ''); return; }
    D.focus = bid;
    D.detailsOpen = true;
    for (const c of document.querySelectorAll<HTMLElement>('#dc-body [data-act="focus-bucket"]')) {
      c.classList.toggle('sel', c.dataset['bid'] === bid);
      c.setAttribute('aria-pressed', String(c.dataset['bid'] === bid));
    }
    hooks.details();
    document.getElementById('dc-app')?.classList.toggle('details-open', true);
  },
  fmtf: (el) => { D.fmtFilter = el.dataset['fmt'] || null; renderBody(); },
  bfilter: (el) => { D.q = (el as HTMLInputElement).value.trim(); renderBody(); },
  bsort: (el) => { D.bucketSort = (el as HTMLSelectElement).value as BucketSort; rememberPref(PREF.bucketSort, D.bucketSort); renderBody(); },
  cards: (el) => { D.cards = el.dataset['mode'] === 'list' ? 'list' : 'grid'; rememberPref(PREF.cards, D.cards); renderOverview(); },
  'tags-menu': (el) => tagsMenu(el),
  tagcheck: (el) => {
    const i = el as HTMLInputElement;
    if (i.checked) D.tagFilter.add(i.value); else D.tagFilter.delete(i.value);
    hooks.render();
  },
  'clear-tagf': () => { D.tagFilter.clear(); document.getElementById('dc-menu')?.classList.add('hidden'); hooks.render(); },
  'clear-bfilters': () => { D.tagFilter.clear(); D.fmtFilter = null; D.q = ''; hooks.render(); },
};
