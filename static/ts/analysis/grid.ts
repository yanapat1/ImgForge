// The results grid: only the rows on screen exist in the page, and rows arrive one page at a time from the saved result,
// so a million rows scroll like ten. Sort and filter run on the saved result on the server: no new query reaches the
// user's database. Column widths are the user's; a click on a cell shows its full value. Drag across cells (or Shift+click,
// Shift+arrows) selects a block that Ctrl+C copies as tab-separated text, so it pastes into a spreadsheet as cells; text
// inside one cell still selects as usual.

import { copyText, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { MENU_SEP, menuHead, menuItem, openMenu } from './menu.js';
import { api } from './state.js';
import type { ResultInfo } from './state.js';

const ROW = 28, PAGE = 200, HEAD = 32, OVERSCAN = 8;              // ROW / HEAD: the kit's .data-grid row heights

interface Grid {
  host: HTMLElement; res: ResultInfo; total: number; pages: Map<number, unknown[][]>; loading: Set<number>;
  sort: string | null; desc: boolean; q: string; widths: number[]; cell: { r: number; c: number } | null; gen: number;
  range: Range | null;           // the selected block (corners as clicked; norm() orders them)
  ver: number; painted: string;  // ver: bumped when the rows change; painted: what the rows on screen show
  dragged: boolean;              // the last pointer press selected a block: its click must not reset it
}
interface Range { r0: number; c0: number; r1: number; c1: number }

const MAX_COPY = 100_000;                                        // cells one copy takes; more: Export

let G: Grid | null = null;

const fmt = (v: unknown): string => (typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toLocaleString('en-US', { maximumFractionDigits: 6 })) : String(v));
const cellHTML = (v: unknown, r: number, c: number, mark: string): string =>
  v === null || v === undefined ? `<td class="an-null null${mark}" data-r="${r}" data-c="${c}">NULL</td>`
    : v === '' ? `<td class="an-null null${mark}" data-r="${r}" data-c="${c}">(empty text)</td>`
      : `<td class="${typeof v === 'number' ? 'n' : ''}${mark}" data-r="${r}" data-c="${c}">${esc(fmt(v))}</td>`;

function norm(x: Range): Range {
  return { r0: Math.min(x.r0, x.r1), r1: Math.max(x.r0, x.r1), c0: Math.min(x.c0, x.c1), c1: Math.max(x.c0, x.c1) };
}
function markOf(g: Grid, r: number, c: number, n: Range | null): string {
  const cur = g.cell?.r === r && g.cell.c === c;
  const sel = !!n && (n.r0 !== n.r1 || n.c0 !== n.c1) && r >= n.r0 && r <= n.r1 && c >= n.c0 && c <= n.c1;
  return `${cur ? ' cur' : ''}${sel ? ' sel' : ''}`;
}
/** The selection changed: set the marks on the cells on screen, without rebuilding them (that would drop a text selection). */
function markSel(g: Grid): void {
  const n = g.range ? norm(g.range) : null;
  g.host.querySelectorAll<HTMLElement>('.an-rows td[data-c]').forEach((td) => {
    const m = markOf(g, Number(td.dataset['r']), Number(td.dataset['c']), n);
    td.classList.toggle('cur', m.includes('cur'));
    td.classList.toggle('sel', m.includes('sel'));
  });
}

function autoWidth(name: string): number { return Math.min(260, Math.max(84, Math.round(name.length * 8 + 34))); }

function rowAt(g: Grid, i: number): unknown[] | null {
  const p = g.pages.get(Math.floor(i / PAGE));
  return p ? p[i % PAGE] ?? null : null;
}

async function load(g: Grid, n: number): Promise<void> {
  if (g.pages.has(n) || g.loading.has(n)) return;
  g.loading.add(n);
  const gen = g.gen;
  try {
    const p = await api.page(g.res.id, n * PAGE, PAGE, g.sort, g.desc, g.q);
    if (gen !== g.gen) return;
    g.total = p.total;
    g.pages.set(n, p.rows);
    g.ver++;
    paint(g);
  } catch (e) {
    toast(e instanceof Error ? e.message : 'Could not read the result', 'err');
  } finally { g.loading.delete(n); }
}

function paint(g: Grid): void {
  const box = g.host.querySelector<HTMLElement>('.an-gridbox');
  const body = g.host.querySelector<HTMLElement>('.an-rows');
  if (!box || !body) return;
  const top = Math.max(0, Math.floor(box.scrollTop / ROW) - OVERSCAN);
  const n = Math.ceil(box.clientHeight / ROW) + OVERSCAN * 2;
  const end = Math.min(g.total, top + n);
  const key = `${top}:${end}:${g.ver}`;
  if (key === g.painted) return;                    // the same rows: keep the cells (and any text selected in them)
  g.painted = key;
  const sel = g.range ? norm(g.range) : null;
  let html = '';
  for (let i = top; i < end; i++) {
    const r = rowAt(g, i);
    if (!r) { void load(g, Math.floor(i / PAGE)); html += `<tr style="height:${ROW}px"><td colspan="${g.res.columns.length}" class="an-null null">…</td></tr>`; continue; }
    html += `<tr style="height:${ROW}px">${r.map((v, c) => cellHTML(v, i, c, markOf(g, i, c, sel))).join('')}</tr>`;
  }
  body.innerHTML = `<tr style="height:${top * ROW}px" aria-hidden="true"></tr>${html}<tr style="height:${Math.max(0, g.total - end) * ROW}px" aria-hidden="true"></tr>`;
  const c = g.host.querySelector('.an-gcount');
  if (c) c.textContent = g.q ? `${g.total.toLocaleString()} of ${g.res.rows.toLocaleString()} rows` : '';
}

function head(g: Grid): string {
  return `<colgroup>${g.res.columns.map((_, i) => `<col data-i="${i}" style="width:${g.widths[i]}px">`).join('')}</colgroup>` +
    `<thead><tr style="height:${HEAD}px">${g.res.columns.map((n, i) => `<th data-sort="${esc(n)}" title="${esc(n)}. Click to sort.">${esc(n)}${g.sort === n ? (g.desc ? ' ▼' : ' ▲') : ''}<span class="an-crs" data-i="${i}" title="Drag to resize. Double-click to fit."></span></th>`).join('')}</tr></thead>`;
}

function refreshHead(g: Grid): void {
  const t = g.host.querySelector('table.an-grid');
  if (!t) return;
  const body = t.querySelector('tbody');
  t.innerHTML = head(g);
  if (body) t.append(body);
  (t as HTMLElement).style.width = `${g.widths.reduce((a, b) => a + b, 0)}px`;
}

function requery(g: Grid): void {
  g.gen++;
  g.ver++;
  g.range = null;
  g.cell = null;
  g.pages.clear();
  g.loading.clear();
  const box = g.host.querySelector('.an-gridbox');
  if (box) box.scrollTop = 0;
  void load(g, 0);
}

export function mountGrid(host: HTMLElement, res: ResultInfo): void {
  const widths = res.columns.map(autoWidth);
  const g: Grid = { host, res, total: res.rows, pages: new Map(), loading: new Set(), sort: null, desc: false, q: '', widths, cell: null, gen: 0, range: null, ver: 0, painted: '', dragged: false };
  G = g;
  host.innerHTML = `<div class="an-gbar toolbar"><input class="txt sm" id="an-gfilter" aria-label="Filter these rows" placeholder="Filter these rows"><span class="an-gcount faint"></span><span class="grow"></span>` +
    `<button type="button" class="sm" data-act="copy-col">Copy column</button><button type="button" class="sm" data-act="copy-row">Copy row</button><button type="button" class="sm" data-act="export" aria-haspopup="menu">${icon('download')}Export</button></div>` +
    `<div id="an-cell" class="an-cell" hidden></div>` +
    `<div class="an-gridbox" tabindex="0"><table class="an-grid data-grid" style="width:${widths.reduce((a, b) => a + b, 0)}px">${head(g)}<tbody class="an-rows"></tbody></table></div>`;
  const box = host.querySelector<HTMLElement>('.an-gridbox');
  box?.addEventListener('scroll', () => { requestAnimationFrame(() => paint(g)); }, { passive: true });
  box?.addEventListener('keydown', (e) => gridKey(g, e));
  box?.addEventListener('contextmenu', (e) => cellMenu(g, e));
  host.querySelector<HTMLInputElement>('#an-gfilter')?.addEventListener('input', (e) => {
    g.q = (e.target as HTMLInputElement).value;
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => requery(g), 200);
  });
  void load(g, 0);
}
let filterTimer: ReturnType<typeof setTimeout> | undefined;

export function gridClick(t: Element): boolean {
  const g = G;
  if (!g || !g.host.contains(t)) return false;
  const sorted = t.closest<HTMLElement>('th[data-sort]');
  if (sorted && !t.closest('.an-crs')) {
    const n = sorted.dataset['sort'] ?? '';
    if (g.sort !== n) { g.sort = n; g.desc = false; } else if (!g.desc) g.desc = true; else { g.sort = null; g.desc = false; }
    refreshHead(g);
    requery(g);
    return true;
  }
  const td = t.closest<HTMLElement>('td[data-c]');
  if (g.dragged) { g.dragged = false; if (td || t.closest('.an-rows')) return true; }
  if (td) {
    const r = Number(td.dataset['r']), c = Number(td.dataset['c']);
    g.range = { r0: r, c0: c, r1: r, c1: c };
    setCell(g, r, c);
    return true;
  }
  const act = t.closest<HTMLElement>('[data-act]')?.dataset['act'];
  if (act === 'copy-cell' || act === 'copy-row' || act === 'copy-col') { void copy(g, act); return true; }
  if (act === 'export') { exportMenu(g, t.closest<HTMLElement>('[data-act="export"]') as HTMLElement); return true; }
  return false;
}

function setCell(g: Grid, r: number, c: number): void {
  g.cell = { r, c };
  markSel(g);
  const v = rowAt(g, r)?.[c];
  const bar = g.host.querySelector<HTMLElement>('#an-cell');
  if (bar) {
    bar.hidden = false;
    bar.innerHTML = `<b>${esc(g.res.columns[c] ?? '')}</b><span>${v === null || v === undefined ? 'NULL' : v === '' ? '(empty text)' : esc(fmt(v))}</span><button type="button" class="sm" data-act="copy-cell">Copy value</button>`;
  }
}

// ---------------------------------------------------------------- block selection and copy
const cellAt = (x: number, y: number): HTMLElement | null => document.elementFromPoint(x, y)?.closest<HTMLElement>('.an-rows td[data-r]') ?? null;

/** A press on a cell: native text selection while the pointer stays in it; leaving the cell turns the drag into a block. */
function cellDown(g: Grid, e: PointerEvent, td: HTMLElement): void {
  const r = Number(td.dataset['r']), c = Number(td.dataset['c']);
  g.dragged = false;
  if (e.shiftKey && g.cell) {                         // Shift+click: the block from the current cell to here
    e.preventDefault();
    g.range = { r0: g.cell.r, c0: g.cell.c, r1: r, c1: c };
    g.dragged = true;
    markSel(g);
    return;
  }
  const box = g.host.querySelector<HTMLElement>('.an-gridbox');
  let last = { x: e.clientX, y: e.clientY }, raf = 0;
  const extend = (): void => {
    const h = cellAt(last.x, last.y);
    if (!h) return;
    const r1 = Number(h.dataset['r']), c1 = Number(h.dataset['c']);
    if (!g.dragged && r1 === r && c1 === c) return;
    if (!g.dragged) {
      g.dragged = true;
      getSelection()?.removeAllRanges();
      document.documentElement.classList.add('an-gsel');
      g.cell = { r, c };
    }
    const x = g.range;
    if (x && x.r0 === r && x.c0 === c && x.r1 === r1 && x.c1 === c1) return;
    g.range = { r0: r, c0: c, r1, c1 };
    markSel(g);
  };
  const scroll = (): void => {                        // near the top or bottom edge: scroll on while the button is down
    raf = 0;
    if (!box || !g.dragged) return;
    const b = box.getBoundingClientRect(), edge = 28;
    const dy = last.y < b.top + HEAD + edge ? -ROW / 2 : last.y > b.bottom - edge ? ROW / 2 : 0;
    if (!dy) return;
    box.scrollTop += dy;
    paint(g);
    extend();
    raf = requestAnimationFrame(scroll);
  };
  const move = (m: PointerEvent): void => { last = { x: m.clientX, y: m.clientY }; extend(); if (!raf) raf = requestAnimationFrame(scroll); };
  const up = (): void => {
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', up);
    document.documentElement.classList.remove('an-gsel');
    if (raf) cancelAnimationFrame(raf);
  };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', up);
}

function moveTo(g: Grid, r: number, c: number, extend: boolean): void {
  r = Math.max(0, Math.min(g.total - 1, r));
  c = Math.max(0, Math.min(g.res.columns.length - 1, c));
  if (extend && g.range) g.range = { ...g.range, r1: r, c1: c };
  else g.range = { r0: r, c0: c, r1: r, c1: c };
  const box = g.host.querySelector<HTMLElement>('.an-gridbox');
  if (box) {                                          // keep the moving corner on screen
    const y = r * ROW, h = box.clientHeight - HEAD;
    if (y < box.scrollTop) box.scrollTop = y;
    else if (y + ROW > box.scrollTop + h) box.scrollTop = y + ROW - h;
    paint(g);
  }
  if (extend) markSel(g); else setCell(g, r, c);
}

function gridKey(g: Grid, e: KeyboardEvent): void {
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (mod && k === 'c') {
    const s = getSelection();
    if (s && !s.isCollapsed && g.host.contains(s.anchorNode)) return;   // text selected in a cell: the browser copies it
    if (!g.range) return;
    e.preventDefault();
    void copyRange(g, e.shiftKey);
  } else if (mod && k === 'a') {
    if (!g.total) return;
    e.preventDefault();
    g.range = { r0: 0, c0: 0, r1: g.total - 1, c1: g.res.columns.length - 1 };
    g.cell = { r: 0, c: 0 };
    markSel(g);
  } else {
    const d = ({ ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] } as Record<string, [number, number]>)[e.key];
    if (!d || !g.cell || mod) return;
    e.preventDefault();
    const from = e.shiftKey && g.range ? { r: g.range.r1, c: g.range.c1 } : g.cell;
    moveTo(g, from.r + d[0], from.c + d[1], e.shiftKey);
  }
  e.stopPropagation();
}

/** One value in tab-separated text: quoted when it holds a tab, a line break or a quote (the way spreadsheets read it). */
function tsv(v: unknown): string {
  if (v === null || v === undefined) return '';
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v as string | number | boolean);
  return /[\t\n\r"]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** The rows of the block: from the pages already here, the others read now (one request per missing page, together). */
async function blockRows(g: Grid, n: Range): Promise<unknown[][]> {
  const gen = g.gen, want: number[] = [];
  for (let p = Math.floor(n.r0 / PAGE); p <= Math.floor(n.r1 / PAGE); p++) if (!g.pages.has(p)) want.push(p);
  const got = new Map(await Promise.all(want.map((p) => api.page(g.res.id, p * PAGE, PAGE, g.sort, g.desc, g.q).then((x) => [p, x.rows] as const))));
  if (gen === g.gen) got.forEach((rows, p) => g.pages.set(p, rows));
  const out: unknown[][] = [];
  for (let i = n.r0; i <= n.r1; i++) {
    const page = got.get(Math.floor(i / PAGE)) ?? g.pages.get(Math.floor(i / PAGE));
    out.push((page?.[i % PAGE] ?? []).slice(n.c0, n.c1 + 1));
  }
  return out;
}

async function copyRange(g: Grid, headers: boolean): Promise<void> {
  if (!g.range) { toast('Select cells first'); return; }
  const n = norm(g.range), rows = n.r1 - n.r0 + 1, cols = n.c1 - n.c0 + 1, cells = rows * cols;
  if (cells > MAX_COPY) { toast(`That is ${cells.toLocaleString()} cells, too many to copy. Use Export.`, 'err'); return; }
  const lines = (await blockRows(g, n)).map((r) => r.map(tsv).join('\t'));
  if (headers) lines.unshift(g.res.columns.slice(n.c0, n.c1 + 1).map(tsv).join('\t'));
  const what = cells === 1 ? 'the value' : `${rows.toLocaleString()} × ${cols} cells`;
  toast(await copyText(lines.join('\n')) ? `Copied ${what}` : 'Copy was blocked here', '');
}

/** One column's values as a SQL IN list: numbers as they are, text quoted, NULL left out, each value once. */
async function copyIn(g: Grid): Promise<void> {
  if (!g.range) return;
  const n = norm(g.range);
  if (n.r1 - n.r0 + 1 > MAX_COPY) { toast('Too many rows for an IN list. Use Export.', 'err'); return; }
  const seen = new Set<string>();
  for (const [v] of await blockRows(g, { ...n, c1: n.c0 })) {
    if (v === null || v === undefined) continue;
    seen.add(typeof v === 'number' || typeof v === 'boolean' ? String(v) : `'${tsvText(v).replace(/'/g, "''")}'`);
  }
  toast(await copyText(`IN (${[...seen].join(', ')})`) ? `Copied ${seen.size} value${seen.size === 1 ? '' : 's'} as IN (…)` : 'Copy was blocked here', '');
}
const tsvText = (v: unknown): string => (typeof v === 'object' ? JSON.stringify(v) : String(v as string));

function cellMenu(g: Grid, e: MouseEvent): void {
  const td = e.target instanceof Element ? e.target.closest<HTMLElement>('.an-rows td[data-c]') : null;
  if (!td) return;
  e.preventDefault();
  const r = Number(td.dataset['r']), c = Number(td.dataset['c']);
  let n = g.range ? norm(g.range) : null;
  if (!n || r < n.r0 || r > n.r1 || c < n.c0 || c > n.c1) {        // outside the block: this cell becomes the selection
    g.range = { r0: r, c0: c, r1: r, c1: c };
    setCell(g, r, c);
    n = norm(g.range);
  }
  const html = menuItem('copy', 'Copy <span class="faint">Ctrl+C</span>', 'copy') +
    menuItem('copyh', 'Copy with headers <span class="faint">Ctrl+Shift+C</span>', 'copy') +
    (n.c0 === n.c1 ? MENU_SEP + menuItem('in', 'Copy as IN (…) list', 'text') : '');
  openMenu(html, td, (k) => { if (k === 'in') void copyIn(g); else void copyRange(g, k === 'copyh'); }, { x: e.clientX, y: e.clientY });
}

// ---------------------------------------------------------------- export: pick the format (analysis/spool.FORMATS)
const FORMATS: Array<{ k: string; label: string; ic: string; max?: number }> = [
  { k: 'csv', label: 'CSV', ic: 'file' }, { k: 'xlsx', label: 'Excel (.xlsx)', ic: 'grid', max: 1_048_575 },
  { k: 'parquet', label: 'Parquet', ic: 'db' }, { k: 'json', label: 'JSON', ic: 'file' }, { k: 'jsonl', label: 'JSON Lines', ic: 'list' },
  { k: 'tsv', label: 'TSV (tab-separated)', ic: 'file' }, { k: 'yaml', label: 'YAML', ic: 'file' },
  { k: 'md', label: 'Markdown table', ic: 'text', max: 100_000 }, { k: 'html', label: 'HTML table', ic: 'globe', max: 100_000 },
];
const LAST_FMT = 'dataviz.analysis.export';

function exportMenu(g: Grid, from: HTMLElement): void {
  let last = '';
  try { last = localStorage.getItem(LAST_FMT) ?? ''; } catch { /* a convenience only */ }
  const order = [...FORMATS].sort((a, b) => Number(b.k === last) - Number(a.k === last));
  const n = g.res.rows;
  const html = menuHead(`Export ${n.toLocaleString()} row${n === 1 ? '' : 's'} as`) + order.map((f) => {
    const over = f.max !== undefined && n > f.max;
    // Excel cannot hold more rows; Markdown / HTML would only take the first part: say so before the download
    const why = over ? (f.k === 'xlsx' ? `Excel holds ${f.max?.toLocaleString()} rows` : `only the first ${f.max?.toLocaleString()} rows`) : '';
    return `<button type="button" role="menuitem" data-m="${f.k}"${over && f.k === 'xlsx' ? ' disabled' : ''}${why ? ` title="${esc(why)}"` : ''}>` +
      `${icon(f.ic)}<span class="lbl">${esc(f.label)}${why ? ` <span class="faint">· ${esc(why)}</span>` : ''}</span></button>`;
  }).join('');
  openMenu(html, from, (k) => {
    try { localStorage.setItem(LAST_FMT, k); } catch { /* a convenience only */ }
    const a = document.createElement('a');
    a.href = api.exportUrl(g.res.id, k);
    a.download = '';
    document.body.append(a);
    a.click();
    a.remove();
  });
}

async function copy(g: Grid, act: string): Promise<void> {
  if (!g.cell) { toast('Click a cell first'); return; }
  const { r, c } = g.cell;
  let text = '';
  if (act === 'copy-cell') text = String(rowAt(g, r)?.[c] ?? '');
  else if (act === 'copy-row') text = (rowAt(g, r) ?? []).map((v) => v ?? '').join('\t');
  else {                                                 // the whole column, up to one page of the server's limit
    const p = await api.page(g.res.id, 0, 1000, g.sort, g.desc, g.q);
    text = p.rows.map((x) => x[c] ?? '').join('\n');
  }
  toast(await copyText(text) ? 'Copied' : 'Copy was blocked here', '');
}

// column widths: drag the edge of a header, double-click to fit
export function gridPointer(e: PointerEvent): boolean {
  const g = G;
  const td = g && e.button === 0 && e.target instanceof Element ? e.target.closest<HTMLElement>('.an-rows td[data-c]') : null;
  if (g && td && g.host.contains(td)) { cellDown(g, e, td); return true; }
  const t = e.target instanceof Element ? e.target.closest<HTMLElement>('.an-crs') : null;
  if (!g || !t) return false;
  const i = Number(t.dataset['i']), x0 = e.clientX, w0 = g.widths[i] ?? 100;
  const table = g.host.querySelector<HTMLElement>('table.an-grid');
  const col = g.host.querySelector<HTMLElement>(`col[data-i="${i}"]`);
  e.preventDefault();
  const move = (m: PointerEvent): void => {
    const w = Math.max(44, w0 + m.clientX - x0);
    g.widths[i] = w;
    if (col) col.style.width = `${w}px`;
    if (table) table.style.width = `${g.widths.reduce((a, b) => a + b, 0)}px`;
  };
  const up = (): void => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', up); };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', up);
  return true;
}

export function gridFit(t: Element): boolean {
  const g = G, h = t.closest<HTMLElement>('.an-crs');
  if (!g || !h) return false;
  const i = Number(h.dataset['i']);
  const longest = Math.max((g.res.columns[i] ?? '').length, ...[...g.pages.values()].flat().map((r) => String(r[i] ?? '').length));
  g.widths[i] = Math.min(420, Math.max(60, Math.round(longest * 7.6 + 24)));
  refreshHead(g);
  return true;
}
