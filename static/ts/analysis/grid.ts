// The results grid: only the rows on screen exist in the page, and rows arrive one page at a time from the saved result,
// so a million rows scroll like ten. Sort and filter run on the saved result on the server: no new query reaches the
// user's database. Column widths are the user's; a click on a cell shows its full value.

import { copyText, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { menuHead, openMenu } from './menu.js';
import { api } from './state.js';
import type { ResultInfo } from './state.js';

const ROW = 28, PAGE = 200, HEAD = 32, OVERSCAN = 8;              // ROW / HEAD: the kit's .data-grid row heights

interface Grid {
  host: HTMLElement; res: ResultInfo; total: number; pages: Map<number, unknown[][]>; loading: Set<number>;
  sort: string | null; desc: boolean; q: string; widths: number[]; cell: { r: number; c: number } | null; gen: number;
}

let G: Grid | null = null;

const fmt = (v: unknown): string => (typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toLocaleString('en-US', { maximumFractionDigits: 6 })) : String(v));
const cellHTML = (v: unknown, r: number, c: number, cur: boolean): string =>
  v === null || v === undefined ? `<td class="an-null null${cur ? ' cur' : ''}" data-r="${r}" data-c="${c}">NULL</td>`
    : v === '' ? `<td class="an-null null${cur ? ' cur' : ''}" data-r="${r}" data-c="${c}">(empty text)</td>`
      : `<td class="${typeof v === 'number' ? 'n' : ''}${cur ? ' cur' : ''}" data-r="${r}" data-c="${c}">${esc(fmt(v))}</td>`;

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
  let html = '';
  for (let i = top; i < end; i++) {
    const r = rowAt(g, i);
    if (!r) { void load(g, Math.floor(i / PAGE)); html += `<tr style="height:${ROW}px"><td colspan="${g.res.columns.length}" class="an-null null">…</td></tr>`; continue; }
    html += `<tr style="height:${ROW}px">${r.map((v, c) => cellHTML(v, i, c, g.cell?.r === i && g.cell.c === c)).join('')}</tr>`;
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
  g.pages.clear();
  g.loading.clear();
  const box = g.host.querySelector('.an-gridbox');
  if (box) box.scrollTop = 0;
  void load(g, 0);
}

export function mountGrid(host: HTMLElement, res: ResultInfo): void {
  const widths = res.columns.map(autoWidth);
  const g: Grid = { host, res, total: res.rows, pages: new Map(), loading: new Set(), sort: null, desc: false, q: '', widths, cell: null, gen: 0 };
  G = g;
  host.innerHTML = `<div class="an-gbar toolbar"><input class="txt sm" id="an-gfilter" aria-label="Filter these rows" placeholder="Filter these rows"><span class="an-gcount faint"></span><span class="grow"></span>` +
    `<button type="button" class="sm" data-act="copy-col">Copy column</button><button type="button" class="sm" data-act="copy-row">Copy row</button><button type="button" class="sm" data-act="export" aria-haspopup="menu">${icon('download')}Export</button></div>` +
    `<div id="an-cell" class="an-cell" hidden></div>` +
    `<div class="an-gridbox" tabindex="0"><table class="an-grid data-grid" style="width:${widths.reduce((a, b) => a + b, 0)}px">${head(g)}<tbody class="an-rows"></tbody></table></div>`;
  host.querySelector('.an-gridbox')?.addEventListener('scroll', () => { requestAnimationFrame(() => paint(g)); }, { passive: true });
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
  if (td) {
    const r = Number(td.dataset['r']), c = Number(td.dataset['c']);
    g.cell = { r, c };
    paint(g);
    const v = rowAt(g, r)?.[c];
    const bar = g.host.querySelector<HTMLElement>('#an-cell');
    if (bar) {
      bar.hidden = false;
      bar.innerHTML = `<b>${esc(g.res.columns[c] ?? '')}</b><span>${v === null || v === undefined ? 'NULL' : v === '' ? '(empty text)' : esc(fmt(v))}</span><button type="button" class="sm" data-act="copy-cell">Copy value</button>`;
    }
    return true;
  }
  const act = t.closest<HTMLElement>('[data-act]')?.dataset['act'];
  if (act === 'copy-cell' || act === 'copy-row' || act === 'copy-col') { void copy(g, act); return true; }
  if (act === 'export') { exportMenu(g, t.closest<HTMLElement>('[data-act="export"]') as HTMLElement); return true; }
  return false;
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
  const g = G, t = e.target instanceof Element ? e.target.closest<HTMLElement>('.an-crs') : null;
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
