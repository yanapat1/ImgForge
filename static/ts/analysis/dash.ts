// Dashboards: a blank board of widgets. Drag a field from the last result onto a chart, drag a widget to swap places,
// and pick its kind, width, height, colour, order and how many groups it shows. Numbers come from the saved result; a
// chart says so when its result has gone and needs a new run. Each chart's data is fetched once per result (a cache
// keyed by the result and the widget's settings), so selecting or restyling a widget does not ask the server again.

import { uuid } from '../core/uuid.js';
import { errText } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { confirmDialog, promptDialog } from '../views/dialog.js';
import { currentResult } from './analyze.js';
import { PALETTE, colorOf } from './board.js';
import { forecastSvg } from './forecast.js';
import { api } from './state.js';
import type { Dashboard, Widget, WidgetKind } from './state.js';

/** Every kind: [kind, name, icon, what it is for]. */
const KINDS: [WidgetKind, string, string, string][] = [
  ['kpi', 'KPI number', 'target', 'One number over the whole result'],
  ['bar', 'Bar', 'bars', 'Compare groups side by side'],
  ['hbar', 'Ranking', 'sort', 'Groups ranked, largest first'],
  ['line', 'Line', 'wave', 'A trend over dates or numbers'],
  ['area', 'Area', 'wave', 'A trend with its volume filled'],
  ['donut', 'Donut', 'circle', 'Each group\'s share of the whole'],
  ['forecast', 'Forecast', 'eval', 'A trend and its forecast band'],
  ['table', 'Table', 'list', 'Groups and their values as rows'],
  ['text', 'Text box', 'text', 'A heading or a note'],
];
const KIND_OF = new Map(KINDS.map((k) => [k[0], k]));
const AGGS = ['sum', 'avg', 'min', 'max', 'count'];
const WIDTHS: [number, string][] = [[3, 'Quarter'], [4, 'Third'], [6, 'Half'], [8, 'Two thirds'], [12, 'Full']];
const SIZES: [NonNullable<Widget['size']>, string][] = [['s', 'Short'], ['m', 'Medium'], ['l', 'Tall']];
const SORTS: [NonNullable<Widget['sort']>, string][] = [['key', 'Natural order'], ['desc', 'Largest first'], ['asc', 'Smallest first']];
const TOPS = [5, 10, 20, 30, 50, 100];
/** Kinds whose groups have no natural order to keep (a ranking, shares, rows): they read best largest first. */
const RANKED = new Set<WidgetKind>(['hbar', 'donut']);
const SMALL = new Set<WidgetKind>(['kpi', 'text']);
const D = { boards: [] as Dashboard[], cur: 0, sel: '' };

const board = (): Dashboard | undefined => D.boards[D.cur];
let saveTimer: ReturnType<typeof setTimeout> | undefined;
function save(): void {
  const b = board();
  if (!b) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { void api.saveDashboard(b).catch((e: unknown) => toast(errText(e), 'err')); }, 500);
}

// ---------------------------------------------------------------- a widget with every field inside its limits
const sizeOf = (w: Widget): NonNullable<Widget['size']> => w.size ?? (SMALL.has(w.kind) ? 's' : 'm');
const rowsOf = (w: Widget): number => (SMALL.has(w.kind) ? { s: 2, m: 3, l: 4 } : { s: 3, m: 4, l: 6 })[sizeOf(w)];
const sortOf = (w: Widget): NonNullable<Widget['sort']> => w.sort ?? (RANKED.has(w.kind) ? 'desc' : 'key');
const topOf = (w: Widget): number => w.top ?? (w.kind === 'donut' ? 6 : w.kind === 'hbar' ? 10 : 30);

/** A saved widget, an old one or the assistant's: kind, agg, width, height, colour, order and top made valid. */
export function cleanWidget(x: Partial<Widget>): Widget {
  const kind = KIND_OF.has(x.kind as WidgetKind) ? x.kind as WidgetKind : 'bar';
  const w: Widget = {
    id: String(x.id || uuid()).slice(0, 64), kind, title: String(x.title ?? KIND_OF.get(kind)?.[1] ?? '').slice(0, 120),
    x: String(x.x ?? ''), y: String(x.y ?? ''), agg: AGGS.includes(String(x.agg)) ? String(x.agg) : 'sum',
    span: Math.max(3, Math.min(12, Math.round(Number(x.span) || (kind === 'kpi' ? 3 : 6)))),
  };
  if (kind === 'text') w.text = String(x.text ?? '').slice(0, 4000);
  if (typeof x.color === 'string' && (PALETTE as readonly string[]).includes(x.color)) w.color = x.color;
  if (x.size === 's' || x.size === 'm' || x.size === 'l') w.size = x.size;
  if (x.sort === 'key' || x.sort === 'desc' || x.sort === 'asc') w.sort = x.sort;
  const top = Math.round(Number(x.top));
  if (Number.isFinite(top) && top > 0) w.top = Math.max(3, Math.min(100, top));
  return w;
}

// ---------------------------------------------------------------- data, fetched once per result and settings
type Series = { labels: string[]; values: number[] };
const cache = new Map<string, Promise<unknown>>();
function once<T>(key: string, f: () => Promise<T>): Promise<T> {
  let p = cache.get(key) as Promise<T> | undefined;
  if (!p) {
    if (cache.size > 300) cache.clear();
    p = f();
    cache.set(key, p);
    p.catch(() => cache.delete(key));                       // an error is not kept: the next draw asks again
  }
  return p;
}
const series = (rid: string, x: string, y: string, agg: string, top: number, order: string): Promise<Series> =>
  once(`c|${rid}|${x}|${y}|${agg}|${top}|${order}`, () => api.chart(rid, x, y || null, agg, top, order));
const totalOf = (rid: string, y: string, agg: string): Promise<{ value: number | null }> => once(`t|${rid}|${y}|${agg}`, () => api.total(rid, y || null, agg));

const nf = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const cf = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const fmt = (v: number): string => nf.format(v);
const short = (v: number): string => (Math.abs(v) >= 10000 ? cf.format(v) : nf.format(Math.round(v * 100) / 100));
const gid = (w: Widget): string => `wg-${w.id.replace(/[^a-z0-9-]/gi, '')}`;
/** The fill gradient of a widget (its colour, fading down). */
const grad = (w: Widget, a = 0.95, b = 0.35): string =>
  `<defs><linearGradient id="${gid(w)}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--wc);stop-opacity:${a}"/><stop offset="1" style="stop-color:var(--wc);stop-opacity:${b}"/></linearGradient></defs>`;

// ---------------------------------------------------------------- drawing
function chartSvg(w: Widget, labels: string[], values: number[]): string {
  const W = 420, H = 190, L = 46, B = 24, T = 16, R = 8, top = Math.max(...values, 0) * 1.12 || 1, low = Math.min(0, ...values);
  const iw = W - L - R, ih = H - T - B, y = (v: number): number => T + ih - ((v - low) / (top - low)) * ih, st = iw / Math.max(labels.length, 1);
  const ticks = [low, (low + top) / 2, top];
  const grid = ticks.map((t, i) => `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}" class="an-cgrid${i ? '' : ' base'}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end" class="an-ctick">${esc(short(t))}</text>`).join('');
  // as many x labels as fit side by side (about 6.5 px a character at this size), evenly spread
  const chars = Math.min(12, Math.max(1, ...labels.map((v) => v.length))), fit = Math.max(2, Math.floor(iw / (chars * 6.5 + 10)));
  const every = Math.ceil(labels.length / fit);
  const lab = labels.map((v, i) => (i % every ? '' : `<text x="${L + st * i + st / 2}" y="${H - 7}" text-anchor="middle" class="an-clab">${esc(v.slice(0, 12))}</text>`)).join('');
  const tip = (i: number): string => `<title>${esc(labels[i])}: ${esc(fmt(values[i] ?? 0))}</title>`;
  let marks: string;
  if (w.kind === 'line' || w.kind === 'area') {
    const pts = values.map((v, i) => [L + st * i + st / 2, y(v)] as const);
    const line = pts.map(([px, py]) => `${px},${py}`).join(' ');
    const fill = w.kind === 'area' && pts.length ? `<polygon points="${pts[0]?.[0]},${y(Math.max(low, 0))} ${line} ${pts[pts.length - 1]?.[0]},${y(Math.max(low, 0))}" fill="url(#${gid(w)})"/>` : '';
    const dots = pts.length <= 60 ? pts.map(([px, py], i) => `<circle cx="${px}" cy="${py}" r="3.4" class="an-cdot">${tip(i)}</circle>`).join('') : '';
    marks = `${w.kind === 'area' ? grad(w, 0.45, 0.02) : ''}${fill}<polyline points="${line}" class="an-cline"/>${dots}`;
  } else {
    const showVals = labels.length <= 12;
    marks = grad(w) + values.map((v, i) => {
      const bx = L + st * i + st * 0.16, by = Math.min(y(v), y(0)), bh = Math.max(1, Math.abs(y(0) - y(v)));
      return `<g class="an-cbar"><rect x="${bx}" y="${by}" width="${st * 0.68}" height="${bh}" rx="3" fill="url(#${gid(w)})">${tip(i)}</rect>` +
        (showVals ? `<text x="${bx + st * 0.34}" y="${by - 4}" text-anchor="middle" class="an-cval">${esc(short(v))}</text>` : '') + '</g>';
    }).join('');
  }
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(w.title)}">${grid}${marks}${lab}</svg>`;
}

/** A ranking: one row per group, its bar as long as its share of the largest. */
function rankHTML(labels: string[], values: number[]): string {
  const max = Math.max(...values.map(Math.abs), 0) || 1;
  return `<ol class="an-rank">${labels.map((l, i) => { const v = values[i] ?? 0; return `<li title="${esc(l)}: ${esc(fmt(v))}"><span class="k">${esc(l)}</span><span class="t"><i style="width:${(Math.abs(v) / max) * 100}%"></i></span><span class="v num">${esc(short(v))}</span></li>`; }).join('')}</ol>`;
}

/** A donut: each group's share; the colours start at the widget's own and go round the palette. */
function donutHTML(w: Widget, labels: string[], values: number[]): string {
  const vals = values.map((v) => Math.max(0, v)), sum = vals.reduce((a, b) => a + b, 0);
  if (!sum) return '<div class="faint">No positive values to share out.</div>';
  const start = Math.max(0, PALETTE.indexOf((w.color ?? 'blue') as typeof PALETTE[number]));
  const col = (i: number): string => colorOf(PALETTE[(start + i) % PALETTE.length] ?? 'blue');
  const R = 42, C = 2 * Math.PI * R;
  let acc = 0;
  const arcs = vals.map((v, i) => {
    const len = (v / sum) * C, s = `<circle r="${R}" cx="60" cy="60" fill="none" stroke="${col(i)}" stroke-width="18" stroke-dasharray="${Math.max(0, len - 1.2)} ${C}" stroke-dashoffset="${-acc}" class="an-darc"><title>${esc(labels[i])}: ${esc(fmt(values[i] ?? 0))} (${((v / sum) * 100).toFixed(1)}%)</title></circle>`;
    acc += len;
    return s;
  }).join('');
  const legend = labels.map((l, i) => `<li><i style="background:${col(i)}"></i><span class="k">${esc(l)}</span><span class="v num">${(((vals[i] ?? 0) / sum) * 100).toFixed(0)}%</span></li>`).join('');
  return `<div class="an-donut"><svg viewBox="0 0 120 120" role="img" aria-label="${esc(w.title)}"><g transform="rotate(-90 60 60)">${arcs}</g><text x="60" y="61" text-anchor="middle" class="an-dsum">${esc(short(sum))}</text><text x="60" y="74" text-anchor="middle" class="an-dcap">total</text></svg><ul class="an-dleg">${legend}</ul></div>`;
}

function tableHTML(labels: string[], values: number[]): string {
  const max = Math.max(...values.map(Math.abs), 0) || 1;
  return `<table class="an-doc list-table an-wtable"><tbody>${labels.map((l, i) => { const v = values[i] ?? 0; return `<tr><td>${esc(l)}</td><td class="num" style="--p:${(Math.abs(v) / max) * 100}%">${esc(fmt(v))}</td></tr>`; }).join('')}</tbody></table>`;
}

function sparkSvg(values: number[]): string {
  if (values.length < 3) return '';
  const lo = Math.min(...values), hi = Math.max(...values), d = hi - lo || 1, W = 200, H = 40;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * W},${H - 3 - ((v - lo) / d) * (H - 6)}`).join(' ');
  return `<svg class="an-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}"/></svg>`;
}

const subtitle = (w: Widget): string => {
  if (w.kind === 'text') return '';
  const what = w.y ? `${w.agg} of ${w.y}` : 'rows';
  return w.kind === 'kpi' ? '' : w.x ? `${what} by ${w.x}` : '';             // a KPI says it under its number
};

async function fill(w: Widget): Promise<void> {
  const el = document.querySelector<HTMLElement>(`.an-w[data-w="${CSS.escape(w.id)}"] .an-wb`);
  if (!el || w.kind === 'text') return;
  const r = currentResult();
  if (!r) { el.innerHTML = '<div class="an-wmsg faint">Run a query to fill this.</div>'; return; }
  const y = w.y && r.columns.includes(w.y) ? w.y : '';
  try {
    if (w.kind === 'kpi') {
      const t = await totalOf(r.id, y, y ? w.agg : 'count');
      const spark = w.x && r.columns.includes(w.x) ? sparkSvg((await series(r.id, w.x, y, w.agg, 60, 'key')).values) : '';
      el.innerHTML = `<div class="an-kpiw"><div class="an-kpi num">${esc(t.value === null ? '–' : fmt(t.value))}</div><div class="faint">${esc(y ? `${w.agg} of ${y}` : 'rows')}${spark ? ` · by ${esc(w.x)}` : ''}</div>${spark}</div>`;
      return;
    }
    if (!w.x) { el.innerHTML = '<div class="an-wmsg faint">Drag a field onto this chart.</div>'; return; }
    if (!r.columns.includes(w.x)) { el.innerHTML = `<div class="an-wmsg faint">${esc(w.x)} is not in the last result. Drag another field here.</div>`; return; }
    if (w.kind === 'forecast') {
      const f = await once(`f|${r.id}|${w.x}|${y}|${w.agg}`, () => api.forecast(r.id, { date: w.x, value: y, agg: w.agg }));
      el.innerHTML = forecastSvg(f, true);
      return;
    }
    const c = await series(r.id, w.x, y, w.agg, topOf(w), sortOf(w));
    if (!c.labels.length) { el.innerHTML = '<div class="an-wmsg faint">No values to show.</div>'; return; }
    el.innerHTML = w.kind === 'table' ? tableHTML(c.labels, c.values) : w.kind === 'hbar' ? rankHTML(c.labels, c.values)
      : w.kind === 'donut' ? donutHTML(w, c.labels, c.values) : chartSvg(w, c.labels, c.values);
  } catch (e) { el.innerHTML = `<div class="an-wmsg faint">${esc(errText(e))}</div>`; }
}

// ---------------------------------------------------------------- the page
const opts = <T extends string | number>(list: [T, string][], cur: T): string => list.map(([v, n]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${esc(n)}</option>`).join('');

function widgetHTML(x: Widget): string {
  const k = KIND_OF.get(x.kind), sub = subtitle(x);
  return `<article class="an-w k-${x.kind}${x.id === D.sel ? ' on' : ''}" draggable="true" data-w="${esc(x.id)}" style="grid-column:span ${x.span};grid-row:span ${rowsOf(x)};--wc:${x.color ? colorOf(x.color) : 'var(--accent)'}">` +
    `<div class="an-wh" data-act="d-sel" data-id="${esc(x.id)}" title="Click to edit, drag to move"><span class="an-wic">${icon(k?.[2] ?? 'bars')}</span><span class="an-wt"><span class="lbl">${esc(x.title)}</span>${sub ? `<span class="an-wsub">${esc(sub)}</span>` : ''}</span><span class="grow"></span>` +
    `<button type="button" class="icon-btn ghost xs" data-act="d-dup" data-id="${esc(x.id)}" aria-label="Duplicate ${esc(x.title)}" title="Duplicate">${icon('copy')}</button>` +
    `<button type="button" class="icon-btn ghost xs" data-act="d-rm" data-id="${esc(x.id)}" aria-label="Remove ${esc(x.title)}" title="Remove">${icon('x')}</button></div>` +
    `<div class="an-wb">${x.kind === 'text' ? `<textarea data-text="${esc(x.id)}" aria-label="Text box" placeholder="Write a heading or a note">${esc(x.text ?? '')}</textarea>` : '<div class="an-wload" aria-label="Loading"></div>'}</div></article>`;
}

function sideHTML(sel: Widget | undefined, cols: string[]): string {
  const tools = `<h4>Add to dashboard</h4><div class="an-tools">${KINDS.map(([k, n, ic, tip]) => `<button type="button" class="an-tool" data-act="d-add" data-k="${k}" title="${esc(tip)}">${icon(ic)}<span>${esc(n)}</span></button>`).join('')}</div>`;
  const fields = `<h4>Fields from your query</h4><div class="an-fields">${cols.map((c) => `<span class="an-fld chip" draggable="true" data-f="${esc(c)}">${esc(c)}</span>`).join('') || '<span class="faint">Run a query first.</span>'}</div>`;
  if (!sel) return tools + fields;
  const chart = sel.kind !== 'text' && sel.kind !== 'kpi', grouped = chart && sel.kind !== 'forecast';
  const sw = [['', 'Accent'] as [string, string], ...PALETTE.map((c) => [c, c] as [string, string])].map(([c, n]) =>
    `<button type="button" class="an-sw${(sel.color ?? '') === c ? ' on' : ''}" data-act="d-color" data-k="${c}" style="--nf:${c ? colorOf(c) : 'var(--accent)'}" aria-label="${n}" title="${n}"></button>`).join('');
  return `<section class="an-dedit" aria-label="Widget settings"><h4>${icon(KIND_OF.get(sel.kind)?.[2] ?? 'bars')} This widget</h4>` +
    `<label>Title<input type="text" id="an-d-title" value="${esc(sel.title)}" maxlength="120"></label>` +
    `<label>Kind<select id="an-d-kind">${opts(KINDS.map(([k, n]) => [k, n] as [WidgetKind, string]), sel.kind)}</select></label>` +
    (sel.kind !== 'text' ? `<label class="an-well" data-well="x"><span class="faint">${sel.kind === 'forecast' ? 'Date' : sel.kind === 'kpi' ? 'Trend by' : 'X'}</span><b>${esc(sel.x || 'Drop a field')}</b></label>` +
      `<label class="an-well" data-well="y"><span class="faint">Y</span><b>${esc(sel.y || 'Row count')}</b></label>` +
      `<label>Using<select id="an-d-agg">${opts(AGGS.map((a) => [a, a] as [string, string]), sel.agg)}</select></label>` : '') +
    (grouped ? `<div class="an-two-sm"><label>Order<select id="an-d-sort">${opts(SORTS, sortOf(sel))}</select></label><label>Show<select id="an-d-top">${opts(TOPS.map((n) => [n, `Top ${n}`] as [number, string]), topOf(sel))}</select></label></div>` : '') +
    `<div class="an-two-sm"><label>Width<select id="an-d-span">${opts(WIDTHS, sel.span)}</select></label><label>Height<select id="an-d-size">${opts(SIZES, sizeOf(sel))}</select></label></div>` +
    (sel.kind !== 'text' ? `<div class="an-dcol"><span class="faint">Colour</span><span class="an-sws">${sw}</span></div>` : '') +
    `<button type="button" class="sm" data-act="d-sel" data-id="">Done</button></section>` + tools + fields;
}

export async function renderDash(reload = true): Promise<void> {
  const w = $req('an-work');
  if (reload) {
    D.boards = (await api.dashboards()).dashboards.map((b) => ({ ...b, layout: (b.layout ?? []).map(cleanWidget) }));
    if (!D.boards.length) D.boards = [{ id: uuid(), name: 'Dashboard', layout: [] }];
    D.cur = Math.min(D.cur, D.boards.length - 1);
  }
  const b = board(), r = currentResult();
  if (!b) return;
  const sel = b.layout.find((x) => x.id === D.sel);
  const empty = `<div class="an-dempty"><span class="an-dempty-ic">${icon('grid')}</span><b>This dashboard is blank</b><p class="faint">Add a widget on the right and drag fields onto it, or let a first draft be built from your last result.</p><div class="row"><button type="button" class="primary" data-act="d-auto"${r ? '' : ' disabled'}>${icon('sparkle')}Build from this result</button><button type="button" data-act="d-add" data-k="kpi">${icon('target')}Add a KPI</button></div></div>`;
  w.innerHTML = `<div class="an-toolbar toolbar"><select id="an-d-pick" aria-label="Dashboard">${D.boards.map((x, i) => `<option value="${i}" ${i === D.cur ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select>` +
    `<button type="button" class="sm" data-act="d-rename">Rename</button><button type="button" class="sm" data-act="d-new">${icon('plus')}New</button><button type="button" class="icon-btn ghost xs" data-act="d-del" aria-label="Delete this dashboard" title="Delete this dashboard">${icon('trash')}</button>` +
    `<span class="an-dmeta faint">${b.layout.length} widget${b.layout.length === 1 ? '' : 's'}${r ? ` · ${r.rows.toLocaleString()} rows` : ' · no result yet'}</span><span class="grow"></span>` +
    `<button type="button" data-act="d-auto">${icon('sparkle')}Build from this result</button><button type="button" class="icon-btn ghost" data-act="d-refresh" aria-label="Refresh the charts" title="Refresh the charts">${icon('refresh')}</button>` +
    `<button type="button" class="icon-btn ghost" data-act="d-present" aria-label="Present full screen" title="Present full screen (Esc to leave)">${icon('fit')}</button><button type="button" data-act="d-clear">Clear</button></div>` +
    `<div class="an-dash" id="an-dash"><div class="an-dgrid" id="an-dgrid">${b.layout.map(widgetHTML).join('') || empty}</div><aside class="an-dside">${sideHTML(sel, r?.columns ?? [])}</aside></div>`;
  await Promise.all(b.layout.map(fill));
}

const mk = (kind: WidgetKind, title: string, o: Partial<Widget> = {}): Widget => cleanWidget({ kind, title, agg: 'sum', ...o });
const selected = (): Widget | undefined => board()?.layout.find((x) => x.id === D.sel);
/** Change the selected widget, save and redraw (its data comes from the cache when the settings it reads did not change). */
function edit(f: (w: Widget) => void): Promise<void> {
  const w = selected();
  if (!w) return Promise.resolve();
  f(w);
  Object.assign(w, cleanWidget(w));
  save();
  return renderDash(false);
}

export const D_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  'd-add': (el) => {
    const b = board(), k = el.dataset['k'] as WidgetKind, n = KIND_OF.get(k)?.[1] ?? k;
    if (!b) return undefined;
    const w = mk(k, n, k === 'text' ? { text: '' } : {});
    b.layout.push(w);
    D.sel = w.id;
    save();
    return renderDash(false);
  },
  'd-sel': (el) => { D.sel = el.dataset['id'] ?? ''; return renderDash(false); },
  'd-rm': (el) => { const b = board(); if (b) b.layout = b.layout.filter((x) => x.id !== el.dataset['id']); save(); return renderDash(false); },
  'd-dup': (el) => {
    const b = board(), i = b?.layout.findIndex((x) => x.id === el.dataset['id']) ?? -1, w = b?.layout[i];
    if (!b || !w) return undefined;
    const c = cleanWidget({ ...w, id: uuid(), title: `${w.title} (copy)` });
    b.layout.splice(i + 1, 0, c);
    D.sel = c.id;
    save();
    return renderDash(false);
  },
  'd-color': (el) => edit((w) => { const k = el.dataset['k'] ?? ''; if (k) w.color = k; else delete w.color; }),
  'd-clear': async () => {
    const b = board();
    if (!b?.layout.length || !await confirmDialog({ title: 'Clear this dashboard?', text: `Its ${b.layout.length} widgets are removed. The dashboard itself stays.`, ok: 'Clear', danger: true })) return;
    b.layout = [];
    D.sel = '';
    save();
    await renderDash(false);
  },
  'd-del': async () => {
    const b = board();
    if (!b || !await confirmDialog({ title: `Delete ${b.name}?`, text: 'The dashboard and its widgets are deleted. Your queries and results stay.', ok: 'Delete', danger: true })) return;
    clearTimeout(saveTimer);
    await api.deleteDashboard(b.id);
    D.boards.splice(D.cur, 1);
    if (!D.boards.length) D.boards = [{ id: uuid(), name: 'Dashboard', layout: [] }];
    D.cur = Math.min(D.cur, D.boards.length - 1);
    D.sel = '';
    await renderDash(false);
  },
  'd-refresh': () => { cache.clear(); return renderDash(false); },
  'd-present': () => {
    const el = $('an-dash');
    if (document.fullscreenElement) void document.exitFullscreen();
    else el?.requestFullscreen().catch(() => toast('This browser did not allow full screen', 'err'));
  },
  'd-new': async () => { const n = await promptDialog({ title: 'New dashboard', label: 'Name', value: 'Dashboard', ok: 'Create' }); if (n) { D.boards.push({ id: uuid(), name: n, layout: [] }); D.cur = D.boards.length - 1; D.sel = ''; save(); await renderDash(false); } },
  'd-rename': async () => { const b = board(); if (!b) return; const n = await promptDialog({ title: 'Rename dashboard', label: 'Name', value: b.name, ok: 'Rename' }); if (n) { b.name = n; save(); await renderDash(false); } },
  'd-auto': async () => {
    const b = board(), r = currentResult();
    if (!b || !r) { toast('Run a query first', 'err'); return; }
    const st = await api.stats(r.id), cols = Object.entries(st.columns);
    // numbers worth adding up: not a key (id, x_id)
    const num = cols.filter(([k, c]) => c.numeric && c.distinct > 10 && !/(^|_)id$|^id_/i.test(k)).map(([k]) => k), cat = cols.filter(([, c]) => !c.numeric && c.distinct >= 2 && c.distinct <= 24).map(([k]) => k);
    const when = cols.filter(([, c]) => /DATE|TIMESTAMP/.test(c.type)).map(([k]) => k);
    const kpis = num.slice(0, 3), ks = 12 / (kpis.length + 1);                   // the KPIs fill their row
    b.layout = [mk('kpi', 'Rows', { color: 'blue', span: ks }), ...kpis.map((n, i) => mk('kpi', `Sum of ${n}`, { y: n, span: ks, color: PALETTE[(i + 1) % PALETTE.length] ?? 'green', ...(when[0] ? { x: when[0] } : {}) })),
      ...(when[0] ? [mk('area', `By ${when[0]}`, { x: when[0], y: num[0] ?? '', span: 8 })] : []),
      ...(cat[0] ? [mk('donut', `Share by ${cat[0]}`, { x: cat[0], y: num[0] ?? '', span: when[0] ? 4 : 6, color: 'violet' })] : []),
      // a forecast needs 8 steps of history (analysis/forecast.py MIN_POINTS): fewer dates than that cannot give them
      ...(when[0] && (st.columns[when[0]]?.distinct ?? 0) >= 8 ? [mk('forecast', `Forecast by ${when[0]}`, { x: when[0], y: num[0] ?? '', span: 12 })] : []),
      ...cat.slice(0, 2).map((c, i) => mk(i ? 'bar' : 'hbar', i ? `By ${c}` : `Top ${c}`, { x: c, y: num[0] ?? '', color: i ? 'teal' : 'green' }))];
    D.sel = '';
    save();
    await renderDash(false);
  },
};

/** The assistant's draft: added to the open board, or a new one. */
export function addCharts(charts: Partial<Widget>[]): void {
  if (!D.boards.length) D.boards = [{ id: uuid(), name: 'Dashboard', layout: [] }];
  const b = board();
  if (!b) return;
  for (const c of charts) b.layout.push(cleanWidget({ ...c, id: '' }));
  save();
}

export function dashInput(t: HTMLInputElement): boolean {
  const b = board();
  if (!b) return false;
  if (t.dataset['text']) { const w = b.layout.find((x) => x.id === t.dataset['text']); if (w) w.text = t.value; save(); return true; }
  if (t.id === 'an-d-pick') { D.cur = Number(t.value); D.sel = ''; void renderDash(false); return true; }
  const set: Record<string, (w: Widget, v: string) => void> = {
    'an-d-title': (w, v) => { w.title = v.trim() || (KIND_OF.get(w.kind)?.[1] ?? 'Widget'); },
    'an-d-kind': (w, v) => { w.kind = v as WidgetKind; delete w.size; delete w.sort; delete w.top; if (v === 'text') w.text ??= ''; },
    'an-d-agg': (w, v) => { w.agg = v; },
    'an-d-span': (w, v) => { w.span = Number(v); },
    'an-d-size': (w, v) => { w.size = v as NonNullable<Widget['size']>; },
    'an-d-sort': (w, v) => { w.sort = v as NonNullable<Widget['sort']>; },
    'an-d-top': (w, v) => { w.top = Number(v); },
  };
  const f = set[t.id];
  if (!f || !selected()) return false;
  void edit((w) => f(w, t.value));
  return true;
}

export function dashDrag(e: DragEvent): void {
  const t = e.target instanceof HTMLElement ? e.target : null;
  if (t?.dataset['f']) e.dataTransfer?.setData('text/an-field', t.dataset['f']);
  if (t?.dataset['w']) e.dataTransfer?.setData('text/an-widget', t.dataset['w']);
}

export function dashDrop(e: DragEvent): boolean {
  const b = board(), el = e.target instanceof Element ? e.target : null;
  if (!b || !el || !$('an-dgrid')) return false;
  const field = e.dataTransfer?.getData('text/an-field'), widget = e.dataTransfer?.getData('text/an-widget');
  const well = el.closest<HTMLElement>('[data-well]'), on = el.closest<HTMLElement>('.an-w');
  if (field && well) { const w = b.layout.find((x) => x.id === D.sel); if (w) w[well.dataset['well'] as 'x' | 'y'] = field; }
  else if (field && on) {
    const w = b.layout.find((x) => x.id === on.dataset['w']);
    if (w) { if (w.kind === 'kpi' || w.x) w.y = field; else w.x = field; D.sel = w.id; }
  } else if (widget && on && on.dataset['w'] !== widget) {
    const i = b.layout.findIndex((x) => x.id === widget), k = b.layout.findIndex((x) => x.id === on.dataset['w']);
    const [p, q] = [b.layout[i], b.layout[k]];
    if (p && q) { b.layout[i] = q; b.layout[k] = p; }
  } else return false;
  e.preventDefault();
  save();
  void renderDash(false);
  return true;
}
