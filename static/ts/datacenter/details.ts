// The right pane: details of what is in focus. A bucket (the focused card on All buckets, or a bucket's root): source
// and root, the detected format (badge, confidence, the checks, Re-detect), its tags (auto tags locked), contents
// (images / label files / boxes / size, the train / val / test split), the sessions made from it, Send to Visualize /
// Label. A folder: the same for that folder. A selection: a preview stack, totals (size, dimensions, labeled, boxes),
// a pairing check, actions (Move / Copy / Tag / Rename / Delete) and Visualize N / Label N. Below ~1100px the panel
// is an overlay (css), opened by a focus and closed by its × or the scrim.

import { esc, icon } from '../core/dom.js';
import { errText } from '../core/api.js';
import { floatPopover } from '../core/popover.js';
import { toast } from '../core/toast.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { openTagEditor } from './tags.js';
import {
  bucketOf, curBucket, D, fbadge, fmtKey, fmtLabel, fmtN, fmtSize, hooks, isImage, isYolo, key, selEntries,
  SOURCE, srcIcon, tagChip, timeAgo, writable,
} from './state.js';
import type { Bucket, FolderStats, Format, Tag } from '../types-datacenter.js';

const SPLIT_COLORS = ['var(--accent)', 'var(--err-tp)', 'var(--err-cls)', 'var(--err-loc)', 'var(--prog-review)'];

function statsFor(bid: string, path: string): FolderStats | null {
  const k = key(bid, path);
  const s = D.stats.get(k);
  if (s === undefined) {
    D.stats.set(k, 'loading');
    api.stats(bid, path).then((r) => { D.stats.set(k, r); }, () => { D.stats.set(k, 'error'); }).finally(() => hooks.details());
    return null;
  }
  return typeof s === 'object' ? s : null;
}

function checks(f: Format | null): string {
  if (!f || !f.checks.length) return '';
  return `<div class="dc-checks">${f.checks.map((c) => {
    const cls = c.ok ? (c.warn ? 'wn' : 'ok') : 'bad';
    return `<span class="${cls}">${icon(c.ok ? (c.warn ? 'warn-c' : 'check-c') : 'x-c')}</span><span class="k">${esc(c.name)}${c.detail ? ` <span class="faint">${esc(c.detail)}</span>` : ''}</span>` +
      `<span class="v num">${c.count != null ? fmtN(c.count) : c.ok ? 'ok' : ''}</span>`;
  }).join('')}</div>`;
}

function formatSec(bid: string, path: string, f: Format | null): string {
  const rt = D.retag.get(bid);
  const was = rt && rt.path === path ? `<span class="faint">was ${esc(fmtLabel(rt.from))}</span>` : '';
  const conf = f && f.confidence != null ? Math.round(f.confidence * 100) : null;
  const bad = f && (fmtKey(f) === 'other' || fmtKey(f) === 'empty');
  return `<section class="d-sec"><h4 class="d-h">Detected format<span class="grow"></span>` +
    `<button type="button" class="linkish" data-act="redetect" data-bid="${esc(bid)}" data-path="${esc(path)}">Re-detect</button></h4>` +
    `<div class="conf-row">${fbadge(f, { conf: true, lg: true })}${was}${conf != null && !bad ? `<div class="conf-bar" aria-hidden="true"><i style="width:${conf}%"></i></div>` : ''}</div>` +
    checks(f) +
    (bad ? `<div class="note bad">${icon('alert')}<span>${esc(f?.reason || 'No known layout matched.')}${rt?.undo_id ? ' Undo the last change to bring it back, or fix the layout.' : ''}</span></div>`
      : f?.reason ? `<p class="hint">${esc(f.reason)}</p>` : '') + '</section>';
}

function tagsSec(bid: string, path: string, tags: Tag[], canEdit: boolean): string {
  const sorted = [...tags.filter((t) => !t.auto), ...tags.filter((t) => t.auto)];
  return `<section class="d-sec"><h4 class="d-h">Tags</h4><div class="tags-wrap">${sorted.map((t) => tagChip(t.tag, t.auto, 'on')).join('')}` +
    (canEdit ? `<button type="button" class="dc-tag add" data-act="edit-tags-d" data-bid="${esc(bid)}" data-path="${esc(path)}">${icon('plus')}${sorted.some((t) => !t.auto) ? 'Edit' : 'Add tag'}</button>` : '') +
    '</div></section>';
}

function contentsSec(s: FolderStats | null, b: Bucket | undefined, title = 'Contents'): string {
  const st = b?.stats ?? {};
  const v = (n: number | null | undefined, f = fmtN): string => (n == null ? '<span class="faint">…</span>' : f(n));
  const kv = `<div class="kv"><div><span>Images</span><b>${v(s?.images ?? st.images)}</b></div><div><span>Label files</span><b>${v(s?.labels ?? st.labels)}</b></div>` +
    `<div><span>Boxes</span><b>${s ? (s.boxes ? fmtN(s.boxes) : '–') : v(null)}</b></div><div><span>Size</span><b>${v(s?.size ?? st.size, fmtSize)}</b></div></div>`;
  const splits = s ? Object.entries(s.splits ?? {}).filter(([, n]) => n > 0) : [];
  const split = splits.length > 1 ? `<div class="split" aria-hidden="true">${splits.map(([, n], i) => `<i style="flex:${n};background:${SPLIT_COLORS[i % SPLIT_COLORS.length]}"></i>`).join('')}</div>` +
    `<div class="legend">${splits.map(([k, n], i) => `<span><i style="background:${SPLIT_COLORS[i % SPLIT_COLORS.length]}"></i>${esc(k)} <span class="num">${fmtN(n)}</span></span>`).join('')}</div>` : '';
  return `<section class="d-sec"><h4 class="d-h">${title}</h4>${kv}${split}</section>`;
}

function sessionsSec(b: Bucket): string {
  if (!b.sessions?.length) return '';
  return `<section class="d-sec"><h4 class="d-h">Used by sessions<span class="num">${b.sessions.length}</span></h4>${b.sessions.map((s) =>
    `<a class="sess" href="${esc(s.url)}"><span class="k ${s.kind === 'label' ? 'lab' : 'viz'}">${s.kind === 'label' ? 'Label' : 'Visualize'}</span>` +
    `<span class="t">${esc(s.name)}</span><span class="w">${s.progress != null ? `${Math.round(s.progress * 100)}% done` : esc(timeAgo(s.when ?? null))}</span></a>`).join('')}</section>`;
}

const sendFoot = (bid: string, paths: string[], n?: number): string => {
  const p = esc(paths.join('\n'));
  return `<div class="details-foot"><div class="send-row">` +
    `<button type="button" class="primary" data-act="send" data-target="visualize" data-bid="${esc(bid)}" data-paths="${p}">${icon('scatter')}${n ? `Visualize ${fmtN(n)}` : 'Send to Visualize'}</button>` +
    `<button type="button" data-act="send" data-target="label" data-bid="${esc(bid)}" data-paths="${p}">${icon('edit')}${n ? `Label ${fmtN(n)}` : 'Send to Label'}</button></div></div>`;
};

const head = (ico: string, title: string, extra = ''): string =>
  `<div class="d-title">${ico}<h3 title="${esc(title)}">${esc(title)}</h3><span class="grow"></span>${extra}` +
  `<button type="button" class="icon-btn ghost sm dc-only-overlay" data-act="close-overlays" aria-label="Close details">${icon('x')}</button></div>`;

function bucketDetails(b: Bucket): string {
  const s = statsFor(b.id, '');
  const f = D.bid === b.id && D.path === '' && D.listing ? D.listing.format ?? b.format : b.format;
  const tags = D.bid === b.id && D.path === '' && D.listing ? D.listing.tags : b.tags;
  const ro = b.writable ? 'read-write' : 'read-only';
  return `<div class="d-scroll"><section class="d-sec">` +
    head(srcIcon(b), b.name, `<button type="button" class="icon-btn ghost sm" data-act="bucket-menu" data-bid="${esc(b.id)}" aria-haspopup="menu" aria-label="Bucket actions" title="Bucket actions">${icon('more')}</button>`) +
    `<div class="d-path">${icon(b.kind === 's3' ? 'cloud' : 'folder')}<span title="${esc(b.root)}">${esc(b.root)}</span></div>` +
    `<div class="d-meta">${esc(SOURCE[b.kind]?.[1] ?? b.kind)} · ${ro}${b.updated_at ? ` · changed ${esc(timeAgo(b.updated_at))}` : ''}</div></section>` +
    formatSec(b.id, '', f) + tagsSec(b.id, '', tags, true) + contentsSec(s, b) + sessionsSec(b) + '</div>' +
    sendFoot(b.id, ['']);
}

function folderDetails(b: Bucket): string {
  const L = D.listing;
  const s = statsFor(b.id, D.path);
  const name = D.path;
  return `<div class="d-scroll"><section class="d-sec">${head(icon('folder'), name)}` +
    `<div class="d-path">${icon('db')}<span>${esc(b.name)}${L?.format ? ` · ${esc(L.format.label)}` : ''}</span></div></section>` +
    (L ? formatSec(b.id, D.path, L.format) : '') +
    (L ? tagsSec(b.id, D.path, L.tags, true) : '') + contentsSec(s, undefined, 'Folder') + sessionsSec(b) + '</div>' +
    sendFoot(b.id, [D.path]);
}

function selectionDetails(b: Bucket): string {
  const es = selEntries();
  const n = D.sel.size;
  const imgs = es.filter(isImage);
  const dirs = es.filter((e) => e.type === 'dir');
  const size = es.reduce((a, e) => a + (e.type === 'file' ? e.size : 0), 0);
  const dims = [...new Set(imgs.filter((e) => e.w && e.h).map((e) => `${e.w}×${e.h}`))];
  const withL = imgs.filter((e) => e.labels != null);
  const labeled = withL.filter((e) => (e.labels ?? 0) > 0).length;
  const boxes = withL.reduce((a, e) => a + (e.labels ?? 0), 0);
  const what = imgs.length === n ? `${fmtN(n)} image${n === 1 ? '' : 's'}` : dirs.length === n ? `${fmtN(n)} folder${n === 1 ? '' : 's'}` : `${fmtN(n)} items`;
  const stack = imgs.slice(0, 3).map((e) => `<img src="${esc(api.fileUrl(b.id, e.path, 256))}" alt="" decoding="async">`).join('') +
    (n > 3 ? `<span class="more num">+${fmtN(n - 3)}</span>` : '');
  const one = n === 1 ? es[0] : undefined;
  const yolo = isYolo(D.listing?.format ?? b.format);
  const w = writable();
  const dis = w ? '' : ' disabled';
  return `<div class="d-scroll"><section class="d-sec">` +
    `<div class="d-title">${icon(imgs.length ? 'file-img' : 'folder')}<h3>${one ? esc(one.name) : `${what} selected`}</h3><span class="grow"></span>` +
    `<button type="button" class="ghost sm" data-act="clear-sel">Clear</button>` +
    `<button type="button" class="icon-btn ghost sm dc-only-overlay" data-act="close-overlays" aria-label="Close details">${icon('x')}</button></div>` +
    (stack ? `<div class="stack">${stack}</div>` : '') +
    `<div class="d-path"><i class="dc-fdot" style="--fc:var(--accent)" aria-hidden="true"></i><span>${esc(b.name)}/${esc(D.path)}${D.path ? '/' : ''}</span></div></section>` +
    `<section class="d-sec"><h4 class="d-h">Selection</h4><div class="kv">` +
    `<div><span>Total size</span><b>${dirs.length ? '–' : fmtSize(size)}</b></div>` +
    `<div><span>${dirs.length && !imgs.length ? 'Items' : 'Dimensions'}</span><b>${dirs.length && !imgs.length ? fmtN(dirs.reduce((a, e) => a + (e.items ?? 0), 0)) : dims.length === 1 ? dims[0] : dims.length ? 'mixed' : '–'}</b></div>` +
    `<div><span>Labeled</span><b>${withL.length ? `${labeled} / ${imgs.length}` : '–'}</b></div>` +
    `<div><span>Boxes</span><b>${withL.length ? fmtN(boxes) : '–'}</b></div></div>` +
    (yolo && imgs.length ? `<div class="dc-checks">` +
      `<span class="${labeled === imgs.length ? 'ok' : 'wn'}">${icon(labeled === imgs.length ? 'check-c' : 'warn-c')}</span><span class="k">each image has a label file</span><span class="v num">${labeled}/${imgs.length}</span>` +
      `<span class="ok">${icon('check-c')}</span><span class="k">moving keeps label pairs together</span><span class="v">auto</span></div>` : '') +
    '</section>' +
    `<section class="d-sec"><h4 class="d-h">Actions</h4><div class="act-list">` +
    `<button type="button" data-act="move"${dis}>${icon('move')}Move to…</button><button type="button" data-act="copy"${dis}>${icon('copy')}Copy to…</button>` +
    `<button type="button" data-act="tag-sel">${icon('tag')}Tag…</button><button type="button" data-act="rename"${n === 1 && w ? '' : ' disabled'}>${icon('edit')}Rename</button></div>` +
    `<button type="button" class="ghost danger-ghost" data-act="delete"${dis}>${icon('trash')}Delete ${esc(what)}${yolo && imgs.length ? ' + labels' : ''}</button></section></div>` +
    sendFoot(b.id, [...D.sel], n) + `<p class="d-tip hint">Ctrl-click adds to the selection · Shift-click selects a range</p>`;
}

function overviewDetails(): string {
  const s = D.summary;
  const n = D.buckets.length;
  return `<div class="d-scroll"><section class="d-sec">${head(icon('db'), 'Data Center')}` +
    `<p class="muted">Every image dataset in one place. Pick a bucket card to see its format, tags, contents and the sessions that use it.</p></section>` +
    `<section class="d-sec"><h4 class="d-h">Totals</h4><div class="kv"><div><span>Buckets</span><b>${fmtN(n)}</b></div><div><span>Images</span><b>${fmtN(s?.images ?? null)}</b></div>` +
    `<div><span>Size</span><b>${fmtSize(s?.size ?? null)}</b></div><div><span>Links</span><b>${s ? fmtN(s.links.local + s.links.s3) : '–'}</b></div></div></section>` +
    `<section class="d-sec"><h4 class="d-h">Keys</h4><div class="dc-keys hint"><span><kbd>/</kbd> search</span><span><kbd>Del</kbd> delete</span><span><kbd>F2</kbd> rename</span><span><kbd>Ctrl</kbd> <kbd>A</kbd> select all</span><span><kbd>Esc</kbd> clear</span></div></section></div>`;
}

export function renderDetails(): void {
  const el = document.getElementById('dc-details');
  if (!el) return;
  let html: string;
  if (D.search) html = overviewDetails();
  else if (D.bid) {
    const b = curBucket();
    if (!b) html = `<div class="d-scroll"><section class="d-sec"><span class="spin" aria-hidden="true"></span></section></div>`;
    else if (D.sel.size) html = selectionDetails(b);
    else if (D.path) html = folderDetails(b);
    else html = bucketDetails(b);
  } else {
    const b = D.focus ? bucketOf(D.focus) : undefined;
    html = b ? bucketDetails(b) : overviewDetails();
  }
  el.innerHTML = html;
}

export const DETAIL_ACTIONS: Record<string, Action> = {
  redetect: (el) => {
    const bid = el.dataset['bid'] || D.bid || '';
    const path = el.dataset['path'] ?? '';
    if (!bid) return;
    el.setAttribute('aria-busy', 'true');
    api.detect(bid, path, true).then(async (f) => {
      toast(`Format: ${f.label}${f.confidence != null ? ` (${Math.round(f.confidence * 100)}%)` : ''}`, 'ok');
      D.retag.delete(bid);
      await hooks.reload();
    }, (e: unknown) => toast(`Could not detect: ${errText(e)}`, 'err'));
  },
  'edit-tags-d': (el) => {
    openTagEditor(el, el.dataset['bid'] ?? '', [el.dataset['path'] ?? '']);
  },
  'bucket-menu': (el) => {
    const bid = el.dataset['bid'] ?? '';
    const b = bucketOf(bid);
    const m = document.getElementById('dc-menu');
    if (!b || !m) return;
    m.innerHTML = `<button type="button" role="menuitem" data-act="go" data-bid="${esc(bid)}" data-path="">${icon('folder')}Open</button>` +
      `<button type="button" role="menuitem" data-act="rename-bucket" data-bid="${esc(bid)}">${icon('edit')}Rename…</button>` +
      `<button type="button" role="menuitem" data-act="redetect" data-bid="${esc(bid)}" data-path="">${icon('refresh')}Re-detect format</button>` +
      `<button type="button" role="menuitem" class="danger" data-act="delete-bucket" data-bid="${esc(bid)}">${icon('trash')}${b.kind === 'managed' ? 'Delete…' : 'Unlink…'}</button>`;
    floatPopover(m, el);
  },
};
