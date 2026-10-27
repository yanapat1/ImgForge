// The left pane: "All buckets", every bucket (format dot, name, source icon, image count) with its folders expandable
// (children listed on demand through ls and cached in D.children until the next reload), the tag list (a click
// filters the All buckets overview), and at the bottom the storage meter, the link counts and Import / Link source.
// Folder rows of the open bucket are drop targets for moving the selection (browser.ts drag and drop).

import { errText } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { compact, D, fdot, fmtKey, fmtN, fmtSize, hooks, isImage, key, SOURCE, srcIcon } from './state.js';
import type { Bucket, Entry } from '../types-datacenter.js';

const TREE_FILES = 40;           // non-image files shown under a folder (data.yaml, README.md …); images never are
const TAGS_SHOWN = 7;

const row = (o: { depth: number; act: string; bid: string; path: string; sel: boolean; twist: 'open' | 'shut' | 'none';
  body: string; n?: string; drop?: boolean; title?: string }): string =>
  `<div class="t-row${o.sel ? ' sel' : ''}" style="--d:${o.depth}" role="treeitem" tabindex="0" data-act="${o.act}" data-bid="${esc(o.bid)}" data-path="${esc(o.path)}"` +
  `${o.twist !== 'none' ? ` aria-expanded="${o.twist === 'open'}"` : ''}${o.sel ? ' aria-selected="true"' : ''}${o.drop ? ' data-drop="1"' : ''}${o.title ? ` title="${esc(o.title)}"` : ''}>` +
  (o.twist === 'none' ? '<span class="tw none"></span>'
    : `<button type="button" class="tw" data-act="twist" data-bid="${esc(o.bid)}" data-path="${esc(o.path)}" tabindex="-1" aria-label="${o.twist === 'open' ? 'Collapse' : 'Expand'}">${icon(o.twist === 'open' ? 'chev-d' : 'chev-r')}</button>`) +
  `${o.body}${o.n ? `<span class="n num">${o.n}</span>` : ''}</div>`;

function childrenOf(b: Bucket, path: string, depth: number): string {
  const k = key(b.id, path);
  const ch = D.children.get(k);
  if (ch === undefined) { void loadChildren(b.id, path); return `<div class="t-row t-note" style="--d:${depth}"><span class="spin sm" aria-hidden="true"></span></div>`; }
  if (ch === 'loading') return `<div class="t-row t-note" style="--d:${depth}"><span class="spin sm" aria-hidden="true"></span></div>`;
  if (ch === 'error') return `<div class="t-row t-note err" style="--d:${depth}">${icon('alert')}could not list</div>`;
  const dirs = ch.filter((e) => e.type === 'dir');
  const files = ch.filter((e) => e.type === 'file' && !isImage(e)).slice(0, TREE_FILES);
  const out: string[] = [];
  for (const e of dirs) {
    const open = D.expanded.has(key(b.id, e.path));
    const sel = D.bid === b.id && D.path === e.path;
    out.push(row({ depth, act: 'go', bid: b.id, path: e.path, sel, twist: open ? 'open' : 'shut', drop: b.writable,
      body: `${icon(sel ? 'folder' : 'folder-o').replace('class="ico"', 'class="ico fi"')}<span class="nm">${esc(e.name)}</span>${sel ? '<i class="here" aria-hidden="true"></i>' : ''}`,
      n: e.items != null ? fmtN(e.items) : '' }));
    if (open) out.push(childrenOf(b, e.path, depth + 1));
  }
  for (const e of files) {
    out.push(row({ depth, act: 'go', bid: b.id, path: path, sel: false, twist: 'none', title: e.path,
      body: `${icon('file').replace('class="ico"', 'class="ico fi"')}<span class="nm">${esc(e.name)}</span>` }));
  }
  return out.join('');
}

async function loadChildren(bid: string, path: string): Promise<void> {
  const k = key(bid, path);
  D.children.set(k, 'loading');
  try {
    const r = await api.ls(bid, path, 0, 300, 'name');
    D.children.set(k, r.entries);
  } catch (e) {
    D.children.set(k, 'error');
    console.warn(`tree ${path}: ${errText(e)}`);
  }
  renderTree();
}

export function renderTree(): void {
  const el = document.getElementById('dc-tree');
  if (!el) return;
  const total = D.buckets.length;
  const rows: string[] = [];
  rows.push(row({ depth: 0, act: 'go', bid: '', path: '', sel: !D.bid && !D.search, twist: 'none',
    body: `${icon('grid').replace('class="ico"', 'class="ico fi"')}<span class="nm">All buckets</span>`, n: total ? String(total) : '' }).replace('class="t-row', 'class="t-row t-all'));
  rows.push('<div class="t-sec">Buckets</div>');
  if (D.loadErr) rows.push(`<div class="t-row t-note err">${icon('alert')}<span class="nm">${esc(D.loadErr)}</span><button type="button" class="sm" data-act="retry">Retry</button></div>`);
  else if (!D.buckets.length) rows.push('<div class="t-row t-note muted">No buckets yet</div>');
  for (const b of D.buckets) {
    const open = D.expanded.has(key(b.id, ''));
    const sel = D.bid === b.id && D.path === '';
    const fk = fmtKey(b.format);
    rows.push(row({ depth: 0, act: 'go', bid: b.id, path: '', sel, twist: open ? 'open' : 'shut', drop: sel && b.writable,
      title: `${b.name} · ${SOURCE[b.kind]?.[1] ?? b.kind}${b.format ? ` · ${b.format.label}` : ''}`,
      body: `${fdot(fk)}<span class="nm">${esc(b.name)}</span>${srcIcon(b).replace('class="ico"', 'class="ico src"')}`,
      n: compact(b.stats?.images ?? b.stats?.vectors ?? null) }));
    if (open) rows.push(childrenOf(b, '', 1));
  }
  const tags = D.tags.filter((t) => !t.auto);
  if (tags.length) {
    const shown = D.allTags ? tags : tags.slice(0, TAGS_SHOWN);
    rows.push(`<div class="t-sec">Tags<span class="num">${tags.length}</span></div><div class="t-tags">` +
      shown.map((t) => `<button type="button" class="dc-tag${D.tagFilter.has(t.tag) ? ' on' : ''}" data-act="tagf" data-tag="${esc(t.tag)}" aria-pressed="${D.tagFilter.has(t.tag)}" title="${esc(`${t.tag}: ${t.count}`)}">${esc(t.tag)}</button>`).join('') +
      (tags.length > TAGS_SHOWN ? `<button type="button" class="dc-tag more" data-act="tags-all">${D.allTags ? 'less' : `+${tags.length - TAGS_SHOWN}`}</button>` : '') + '</div>');
  }
  const s = D.summary;
  const used = s?.managed.used ?? 0, free = s?.managed.free ?? null;
  const pct = free != null && used + free > 0 ? Math.min(100, (100 * used) / (used + free)) : 0;
  el.innerHTML = `<div class="dc-tree-head"><h2>Data Center</h2>` +
    `<button type="button" class="icon-btn ghost sm" data-act="import" aria-label="Import data" title="Import data">${icon('plus')}</button></div>` +
    `<div class="dc-tree-scroll" role="tree" aria-label="Buckets">${rows.join('')}</div>` +
    `<div class="dc-tree-foot">` +
    `<div class="src-line">${icon('hdd')}Managed store<span class="num">${s ? `${fmtSize(used)}${free != null ? ` / ${fmtSize(used + free)}` : ''}` : '—'}</span></div>` +
    `<div class="dc-meter" role="meter" aria-label="Managed storage used" aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct.toFixed(1)}%"></i></div>` +
    `<div class="src-line">${icon('link')}Local links<span class="num">${s ? s.links.local : '—'}</span></div>` +
    `<div class="src-line">${icon('cloud')}S3 links<span class="num">${s ? s.links.s3 : '—'}</span></div>` +
    `<div class="btns"><button type="button" class="primary" data-act="import">${icon('upload')}Import</button>` +
    `<button type="button" data-act="link">${icon('link')}Link source</button></div></div>`;
}

export const TREE_ACTIONS: Record<string, Action> = {
  twist: (el, ev) => {
    ev.stopPropagation();
    const k = key(el.dataset['bid'] ?? '', el.dataset['path'] ?? '');
    if (D.expanded.has(k)) D.expanded.delete(k); else D.expanded.add(k);
    renderTree();
  },
  tagf: (el) => {
    const t = el.dataset['tag'] ?? '';
    if (D.tagFilter.has(t)) D.tagFilter.delete(t); else D.tagFilter.add(t);
    if (D.bid || D.search) hooks.navigate(null, '');
    else hooks.render();
  },
  'tags-all': () => { D.allTags = !D.allTags; renderTree(); },
};

/** The children of a folder as the tree knows them (the folder picker of ops.ts reuses the cache). */
export const knownChildren = (bid: string, path: string): Entry[] | null => {
  const c = D.children.get(key(bid, path));
  return Array.isArray(c) ? c : null;
};
