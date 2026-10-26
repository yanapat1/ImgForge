// The selection gallery (left panel): cards, sorting, paging, breakdown chips.

import { $, $req, $qa, esc, icon } from '../core/dom.js';
import { pts, S, UI } from '../core/state.js';
import type { DuplicatePair, SelEntry } from '../types.js';
import { clsColor, clsName, ERR, errColor } from '../core/theme.js';
import { isOutlier, showOutlierMarks } from '../plot/groups.js';
import { clearLasso } from '../plot/interactions.js';
import { pickNumber, updateMarks } from '../plot/marks.js';
import { updateHighlight } from '../plot/render.js';
import { renderImageGrid } from './image-grid.js';
import { allRows, cardMedia, isRows, loadVisibleMedia, rowId, syncPlaying, unit } from './preview.js';

/** Options of setSelection. `highlight: null` leaves the plot highlight alone. */
export interface SelectionOpts {
  kind?: 'samples' | 'pairs';
  pairs?: DuplicatePair[];
  highlight?: boolean | null;
  view?: string | null | undefined;
  keepLasso?: boolean;
  keepStack?: boolean;
}

export function setSelection(ids: number[], title: string, { kind = 'samples', pairs = [], highlight = true, view = null, keepLasso = false, keepStack = false }: SelectionOpts = {}): void {
  if (!keepLasso) clearLasso();
  if (!keepStack) S.selStack = [];
  // boxes a curation fix removed are never shown (the server filters them too: this is the safety net)
  const del = S.P && S.P.deleted;
  if (del) {
    ids = ids.filter((id) => !del[id]);
    if (pairs.length) pairs = pairs.filter((p) => !del[p.a] && !del[p.b]);
  }
  S.activeView = view;
  $qa('.view-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset['view'] === view);
    b.setAttribute('aria-pressed', String(b.dataset['view'] === view));
  });
  $('issues-btn')?.classList.toggle('on', !!$('issues-pop')?.querySelector('.view-btn.active'));
  S.sel = ids;
  S.selKind = kind;
  S.pairs = pairs;
  S.selTitle = title;
  S.page = 0;
  syncClFix();
  S.picked = [];
  if (highlight !== null) S.highlight = highlight ? ids.slice(0, 5000) : [];
  renderGallery();
  if (highlight !== null) updateHighlight();
  updateMarks();
  window.dispatchEvent(new CustomEvent('selection-changed'));   // the ◆ assistant's "Selection · n" chip
}

/** Boxes of the selection cleanlab would give another class (a cleanlab issue view is on screen). */
export function clFixIds(): number[] {
  const P = S.P;
  if (!P?.cl_suggested || !(S.activeView === 'cl-issues' || S.activeView === 'cl-low')) return [];
  const sug = P.cl_suggested, q = P.cl_quality ?? [];
  return S.sel.filter((id) => q[id] != null && sug[id] !== P.cls[id]);
}

// "Change N to cleanlab's class" in the gallery head: on cleanlab's issue views, for people who may edit labels
export function syncClFix(): void {
  const b = $('btn-cl-fix');
  if (!b) return;
  const n = S.status?.my_role === 'viewer' ? 0 : clFixIds().length;
  b.classList.toggle('hidden', !n);
  const t = $('btn-cl-fix-t');
  if (t) t.textContent = `Change ${n.toLocaleString()} to cleanlab's class`;
}

// narrowing the selection (breakdown chips, "in groups / outliers") keeps the previous one: a "back" chip undoes it
const STACK_MAX = 20;

export function narrowSelection(ids: number[], title: string): void {
  const entry: SelEntry = { ids: S.sel, title: S.selTitle, kind: S.selKind, pairs: S.pairs, view: S.activeView };
  const stack = (S.selStack || []).concat([entry]);
  setSelection(ids, title, { keepStack: true });
  S.selStack = stack.slice(-STACK_MAX);
  renderGallery();
}

export function popSelection(): void {
  const prev = S.selStack && S.selStack.pop();
  if (!prev) return;
  const stack = S.selStack ?? [];
  setSelection(prev.ids, prev.title, { kind: prev.kind, pairs: prev.pairs, view: prev.view, keepStack: true });
  S.selStack = stack;
  renderGallery();
}

type Cmp = (a: number, b: number) => number;
/** Value of a numeric column at a box id (0 when the column is not loaded: callers check that first). */
const at = (col: ArrayLike<number> | undefined, id: number): number => col?.[id] ?? 0;

export function sortedSelection(from: number[] = S.sel): number[] {
  const P = pts();
  const ids = from.slice();
  const sort = $req<HTMLSelectElement>('sort').value;
  const by: Record<string, Cmp> = {
    suspicion: (a, b) => at(P.suspicion, b) - at(P.suspicion, a),
    outlier: (a, b) => at(P.outlier, b) - at(P.outlier, a),
    'area-asc': (a, b) => at(P.area, a) - at(P.area, b),
    'area-desc': (a, b) => at(P.area, b) - at(P.area, a),
    blur: (a, b) => at(P.blur, a) - at(P.blur, b),
    dark: (a, b) => at(P.brightness, a) - at(P.brightness, b),
    bright: (a, b) => at(P.brightness, b) - at(P.brightness, a),
    class: (a, b) => at(P.cls, a) - at(P.cls, b) || a - b,
    'group-out': (a, b) => (isOutlier(b) ? 1 : 0) - (isOutlier(a) ? 1 : 0) || a - b,
    image: (a, b) => { const x = P.image[a] ?? '', y = P.image[b] ?? ''; return x < y ? -1 : x > y ? 1 : a - b; },
    // embeddings sessions: a numeric metadata column, largest first (blanks last)
    ...Object.fromEntries(Object.entries(P.meta_types ?? {}).filter(([, t]) => t === 'num').map(([c]): [string, Cmp] => {
      const col = P.meta?.[c] ?? [];
      const v = (id: number): number => { const x = col[id]; return typeof x === 'number' ? x : -Infinity; };
      return [`meta:${c}`, (a, b) => v(b) - v(a) || a - b];
    })),
    // mistakes first (missed, misplaced, wrong class), then found boxes least confident first
    'm-status': (a, b) => (MISTAKE_RANK[at(P.m_status, b) + 1] ?? 0) - (MISTAKE_RANK[at(P.m_status, a) + 1] ?? 0) || at(P.m_score, a) - at(P.m_score, b),
    'm-score': (a, b) => at(P.m_score, a) - at(P.m_score, b),
    // cleanlab: lowest label quality first, unscored boxes last
    'cl-quality': (a, b) => (P.cl_quality?.[a] ?? 2) - (P.cl_quality?.[b] ?? 2),
  };
  const cmp = by[sort];
  const ready = sort.startsWith('m-') ? !!P.m_status : sort === 'cl-quality' ? !!P.cl_quality
    : (P.suspicion || !['suspicion', 'outlier'].includes(sort));
  return cmp && ready ? ids.sort(cmp) : ids;
}

const MISTAKE_RANK = [0, 1, 3, 2, 4];         // by m_status + 1: not scored, found, wrong class, badly placed, missed

function scoreLabel(id: number): string {
  const P = pts();
  switch ($req<HTMLSelectElement>('sort').value) {
    case 'suspicion': return P.suspicion ? `${Math.round(at(P.suspicion, id) * 100)}%` : '';
    case 'outlier': return P.outlier ? at(P.outlier, id).toFixed(3) : '';
    case 'cl-quality': { const q = P.cl_quality?.[id]; return q == null ? '' : q.toFixed(2); }
    case 'area-asc': case 'area-desc': return `${(at(P.area, id) * 100).toFixed(2)}%`;
    case 'blur': return at(P.blur, id).toFixed(0);
    case 'dark': case 'bright': return at(P.brightness, id).toFixed(0);
    case 'm-status': case 'm-score': return P.m_status && at(P.m_status, id) >= 0 ? (at(P.m_score, id) ? at(P.m_score, id).toFixed(2) : '–') : '';
    default: return '';
  }
}

function cardHTML(id: number): string {
  const P = pts();
  const c = at(P.cls, id);
  const sug = at(P.suggested, id);
  const clSug = P.cl_issue?.[id] ? P.cl_suggested?.[id] ?? -1 : -1;
  const warn = clSug >= 0 && clSug !== c
    ? `<span class="warn" title="cleanlab: likely a ${esc(clsName(clSug))} (label quality ${(P.cl_quality?.[id] ?? 0).toFixed(2)})">${icon('arrow-r')}${esc(clsName(clSug))}</span>`
    : P.suggested && sug !== c && at(P.suspicion, id) >= 0.5
      ? `<span class="warn" title="most neighbours are ${esc(clsName(sug))}">${icon('arrow-r')}${esc(clsName(sug))}</span>` : '';
  const score = scoreLabel(id);
  const ms = P.m_status ? at(P.m_status, id) : -1;
  const errLabel = ERR[ms]?.label ?? '';
  const model = ms > 0 ? `<span class="m-badge" style="--c:${errColor(ms)}" title="model: ${errLabel}` +
    `${at(P.m_pred, id) >= 0 ? ` (says ${esc(clsName(at(P.m_pred, id)))})` : ''}">${errLabel}</span>` : '';
  const out = showOutlierMarks() && isOutlier(id);
  const desc = `#${id} ${esc(clsName(c))} · ${esc(isRows() ? rowId(id) : P.image[id])}${out ? ' · OUTLIER (no group)' : ''}`;
  const picked = S.picked.includes(id);
  return `<div class="card${out ? ' is-outlier' : ''}${picked ? ' picked' : ''}" data-id="${id}" role="button" tabindex="0" ` +
    `aria-pressed="${picked}" title="${desc}\nclick: details · ctrl+click: select (pin on map)" aria-label="${desc}">` +
    `<span class="pick-badge"><span class="pick-n">${pickNumber(id)}</span>${icon('check')}</span>` +
    (out ? `<span class="out-badge">${icon('x')}outlier</span>` : '') +
    cardMedia(id) +
    `<span class="badge" style="--c:${clsColor(c)}">${esc(clsName(c))}</span>` +
    warn + model + (score ? `<span class="score">${score}</span>` : '') + '</div>';
}

// mouse / keyboard tips (also under the image browser's hint)
export const TIPS_HTML =
  '<ul>' +
  '<li><span><kbd>hover</kbd></span><span>a dot to preview it</span></li>' +
  '<li><span><kbd>click</kbd></span><span>a thumbnail (or a dot) to open the full image</span></li>' +
  '<li><span><kbd>Ctrl</kbd>+<kbd>click</kbd></span><span>thumbnails to select them (their dots are pinned on the map)</span></li>' +
  '<li><span><kbd>right-drag</kbd></span><span>pan the plot (or drag with two fingers on a touch screen)</span></li>' +
  '<li><span><kbd>middle-drag</kbd></span><span>move the lasso (re-selects the dots under it)</span></li>' +
  '<li><span><kbd>Shift</kbd>+<kbd>drag</kbd></span><span>with the circle tool: a round circle instead of an oval</span></li>' +
  '<li><span><kbd>click</kbd></span><span>outside the lasso / box / circle to clear it</span></li>' +
  '<li><span><kbd>Esc</kbd></span><span>unpin thumbnails, then clear the lasso</span></li>' +
  '<li><span><kbd>scroll</kbd></span><span>mouse wheel: zoom the plot · trackpad: two-finger scroll pans</span></li>' +
  '<li><span><kbd>pinch</kbd> / <kbd>Ctrl</kbd>+<kbd>scroll</kbd></span><span>zoom the plot (Settings › Appearance: wheel mode)</span></li>' +
  '<li><span><kbd>Shift</kbd>+<kbd>click</kbd></span><span>a dot (or click a bubble) to select its group</span></li>' +
  '</ul>';

const EMPTY_HTML = '<div class="empty">' +
  '<svg class="illus" viewBox="0 0 120 90" fill="none" aria-hidden="true">' +
  '<g style="fill:var(--border-hover)"><circle cx="22" cy="20" r="3"/><circle cx="96" cy="16" r="3"/><circle cx="104" cy="70" r="3"/><circle cx="14" cy="66" r="3"/><circle cx="60" cy="82" r="3"/></g>' +
  '<path d="M38 30c10-14 44-16 52-2 7 12-2 30-18 34-17 4-36-2-38-14-1-7 2-13 4-18z" style="stroke:var(--accent)" stroke-width="2" stroke-dasharray="5 4" stroke-linecap="round"/>' +
  '<path d="M38 30c10-14 44-16 52-2 7 12-2 30-18 34-17 4-36-2-38-14-1-7 2-13 4-18z" style="fill:var(--accent)" fill-opacity=".1"/>' +
  '<g style="fill:var(--accent)"><circle cx="52" cy="36" r="3.5"/><circle cx="66" cy="32" r="3.5"/><circle cx="76" cy="44" r="3.5"/><circle cx="58" cy="50" r="3.5"/><circle cx="46" cy="46" r="3.5"/></g>' +
  '</svg>' +
  '<h3>Nothing selected yet</h3>' +
  '<div>Draw a lasso around dots in the plot to see their images here.</div>' +
  TIPS_HTML + '</div>';

export function renderGallery(): void {
  const g = $req('gallery');
  // an embeddings session has no image browser: with nothing selected the gallery lists every row
  const browse = isRows() && !!S.P && S.selKind !== 'pairs' && !S.sel.length && S.selTitle === 'No selection';
  const sel = browse ? allRows() : S.sel;
  $req('gallery-title').textContent = browse ? 'All rows' : S.selTitle;
  const total = S.selKind === 'pairs' ? S.pairs.length : sel.length;
  $req('gallery-count').textContent = total ? `${total.toLocaleString()} ${S.selKind === 'pairs' ? 'pairs' : unit(total)}` : '';

  const counts = new Map<number, number>();
  const cls = S.P ? S.P.cls : [];
  for (const id of sel) { const c = cls[id] ?? 0; counts.set(c, (counts.get(c) || 0) + 1); }
  let groupChips = '';
  if (showOutlierMarks() && S.selKind !== 'pairs' && S.sel.length) {
    const nOut = S.sel.filter(isOutlier).length;
    groupChips = `<span class="chip chip-out" data-f="out" role="button" tabindex="0" title="keep only outliers (dots in no group)">${icon('x')}outliers <span class="n">${nOut}</span></span>` +
      `<span class="chip" data-f="in" role="button" tabindex="0" title="keep only dots that are inside a group">${icon('circle')}in groups <span class="n">${S.sel.length - nOut}</span></span>`;
  }
  const lastSel = S.selStack?.[S.selStack.length - 1];
  const back = lastSel
    ? `<span class="chip chip-all" data-undo="1" role="button" tabindex="0" title="back to: ${esc(lastSel.title)}">${icon('undo')}back</span>` : '';
  $req('breakdown').innerHTML = back + groupChips + [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([c, n]) =>
    `<span class="chip" data-c="${c}" role="button" tabindex="0" title="keep only this class"><span class="dot" style="background:${clsColor(c)}"></span>${esc(clsName(c))} <span class="n">${n}</span></span>`
  ).join('');

  if (!total) {
    // nothing selected (cleared, or never): the image browser (every image, filtered); a selection that came out
    // empty (a lasso over no dot, a search with no hit) says so instead
    if (S.selKind !== 'pairs' && S.P && S.selTitle === 'No selection') { renderImageGrid(); return; }
    g.innerHTML = S.P && S.selTitle !== 'No selection'
      ? `<div class="empty"><h3>Nothing in this selection</h3><div>${esc(S.selTitle)}: no ${unit(1)}. <b>Clear</b> lists every ${isRows() ? 'row' : 'image'} again.</div></div>`
      : EMPTY_HTML;
    $req('pager').innerHTML = '';
    return;
  }
  const pages = Math.ceil(total / UI.page_size);
  S.page = Math.min(S.page, pages - 1);
  const start = S.page * UI.page_size;
  if (S.selKind === 'pairs') {
    g.innerHTML = S.pairs.slice(start, start + UI.page_size).map((p) =>
      `<div class="pair">${cardHTML(p.a)}${cardHTML(p.b)}<span class="sim">${p.sim.toFixed(3)}</span></div>`).join('');
  } else {
    g.innerHTML = sortedSelection(sel).slice(start, start + UI.page_size).map(cardHTML).join('');
  }
  g.scrollTop = 0;
  syncPlaying();
  loadVisibleMedia(g);
  $req('pager').innerHTML = pages > 1
    ? `<button data-p="${S.page - 1}" aria-label="Previous page" ${S.page === 0 ? 'disabled' : ''}>${icon('chev-l')}</button>` +
      `<span class="muted">page <span class="num">${S.page + 1} / ${pages}</span></span>` +
      `<button data-p="${S.page + 1}" aria-label="Next page" ${S.page >= pages - 1 ? 'disabled' : ''}>${icon('chev-r')}</button>`
    : '';
}

export function galleryOrder(): number[] {
  if (S.selKind === 'pairs') return S.pairs.flatMap((p) => [p.a, p.b]);
  if (isRows() && !S.sel.length && S.selTitle === 'No selection') return sortedSelection(allRows());
  return sortedSelection();
}
