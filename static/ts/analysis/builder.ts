// The Visual builder. It edits a Query Model; the server turns the model into SQL for the connection's database, so the
// text in the editor always follows the canvas. Editing here never queries the user's database: results wait for Run.
// While typed SQL has not been run the canvas is read-only, so it can never overwrite what the user wrote. After a typed
// query runs, the server parses it and the canvas redraws; what the model cannot hold locks the canvas and says why.
//
// Mouse: left-drag the background selects, right-drag moves the board, wheel zooms, a click on a column selects it, a
// click on a header shows or hides the table, drag a column onto another table to join, right-click a line removes it.

import { errText } from '../core/api.js';
import { bindCombo, comboHTML } from '../core/combobox.js';
import { $, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog, promptDialog } from '../views/dialog.js';
import { chip, saveTabs } from './app.js';
import { addMenu, bindBoard, clearNoteSel, layerHTML, marqueeNotes, noteDown, noteKey, noteMenu, noteMove, noteUp, pinnedTo, placeNoteBar, renderNotes } from './board.js';
import { cardOf, cardTitle, ENDS_LABEL, ENDS_NEXT, endsMode, markerDefs, setEndsMode, sideMarks, statKey } from './cardinality.js';
import { countJoin } from './joins.js';
import { MENU_SEP, menuHead, menuItem, openMenu } from './menu.js';
import { A, api, sourceOf, tab, tableKey } from './state.js';
import type { Calc, Card, Join, Model, MTable, Note, Tab } from './state.js';

const WINDOW_FNS = ['ROW_NUMBER', 'RANK', 'DENSE_RANK', 'NTILE', 'PERCENT_RANK', 'CUME_DIST', 'LAG', 'LEAD', 'FIRST_VALUE', 'LAST_VALUE', 'NTH_VALUE', 'SUM', 'AVG', 'COUNT', 'MIN', 'MAX', 'STDEV', 'VAR'];
const NO_ARG = new Set(['ROW_NUMBER', 'RANK', 'DENSE_RANK', 'PERCENT_RANK', 'CUME_DIST']);
/** CAST targets by dialect, grouped as the Change type dialog shows them (any other type can be typed). */
const TYPES: Record<string, [string, string[]][]> = {
  mssql: [['Number', ['int', 'bigint', 'smallint', 'decimal(18,2)', 'float', 'money']], ['Text', ['varchar(100)', 'nvarchar(100)', 'nvarchar(max)']],
    ['Date and time', ['date', 'datetime2', 'datetime', 'time']], ['Yes / no', ['bit']]],
  postgres: [['Number', ['integer', 'bigint', 'smallint', 'numeric(18,2)', 'double precision']], ['Text', ['text', 'varchar(100)']],
    ['Date and time', ['date', 'timestamp', 'timestamptz', 'time']], ['Yes / no', ['boolean']]],
  mongo: [['Number', ['int', 'long', 'decimal', 'double']], ['Text', ['string']], ['Date and time', ['date']], ['Yes / no', ['bool']]],
};
const TYPE_RE = /^[A-Za-z][\w ]*(\(\s*(\d+|max)(\s*,\s*\d+)?\s*\))?$/i;
const ORDER = ['INNER', 'LEFT', 'RIGHT', 'FULL'];

const emptyModel = (): Model => ({ tables: [], joins: [], calc: [], parts: {}, locked: [], adv: '' });
export const modelOf = (t: Tab): Model => (t.model ??= emptyModel());
const alias = (m: MTable): string => m.alias || m.id;
/** joins between the same two tables are one ON … AND …: they share one type */
const samePair = (x: Join, y: Join): boolean => (x.a[0] === y.a[0] && x.b[0] === y.b[0]) || (x.a[0] === y.b[0] && x.b[0] === y.a[0]);
const CARD_LABEL: Record<string, string> = { sub: 'SUB', cte: 'CTE', remote: 'REMOTE' };
const readOnly = (t: Tab): boolean => t.dirty && t.typed || !!modelOf(t).locked.length || t.notDrawn;

function columnsOf(t: Tab, m: MTable): { name: string; type: string; key: string }[] {
  if (m.cols?.length) return m.cols.map((c) => ({ name: c, type: '', key: '' }));
  const info = t.conn ? A.info.get(tableKey(t.conn, m.database || t.db || '', m.schema, m.name)) : undefined;
  if (info) return info.columns;
  return [...m.sel, ...Object.keys(m.cast)].filter((c, i, a) => a.indexOf(c) === i).map((c) => ({ name: c, type: '', key: '' }));
}

async function needInfo(t: Tab, m: MTable): Promise<void> {
  if (!t.conn || m.kind !== 'table' || !m.name || m.name.startsWith('#')) return;   // no name: a table function such as generate_series()
  const key = tableKey(t.conn, m.database || t.db || '', m.schema, m.name);
  if (A.info.has(key)) return;
  try { A.info.set(key, await api.describe(t.conn, m.database || t.db || '', m.schema, m.name)); } catch { /* the card shows the columns it knows */ }
}

/** Cards drawn from a saved tab (after a reload) have no columns yet: fetch the missing ones once, together, then
 *  draw again. A failed fetch is not retried on every draw (the card keeps the columns it knows). */
const asked = new Set<string>();
function loadMissing(t: Tab): void {
  const want = modelOf(t).tables.filter((x) => {
    if (!t.conn || x.kind !== 'table' || x.name.startsWith('#')) return false;
    const key = tableKey(t.conn, x.database || t.db || '', x.schema, x.name);
    if (A.info.has(key) || asked.has(key)) return false;
    asked.add(key);
    return true;
  });
  if (want.length) void Promise.all(want.map((x) => needInfo(t, x))).then(() => { if (tab() === t) renderBuilder(); });
}

// ---------------------------------------------------------------- undo
const snap = (t: Tab): string => JSON.stringify({ model: modelOf(t), sql: t.sql, typed: t.typed });
export function record(t: Tab): void {
  t.undo.push(snap(t));
  if (t.undo.length > 50) t.undo.shift();
  t.redo = [];
}
function restore(t: Tab, s: string): void {
  const v = JSON.parse(s) as { model: Model; sql: string; typed: boolean };
  t.model = v.model;
  t.sql = v.sql;
  t.typed = v.typed;
  S.sel = [];
  document.dispatchEvent(new CustomEvent('an-sql-changed'));
  renderBuilder();
}
function undo(): void { const t = tab(); const s = t.undo.pop(); if (!s) { toast('Nothing to undo'); return; } t.redo.push(snap(t)); restore(t, s); }
function redo(): void { const t = tab(); const s = t.redo.pop(); if (!s) { toast('Nothing to redo'); return; } t.undo.push(snap(t)); restore(t, s); }

/** The model as the server needs it for SQL: without the board's notes. */
function sqlModel(m: Model): Model { const c = { ...m }; delete c.notes; return c; }

/** The model changed: the server writes the SQL, the results stay as they are until Run. */
async function changed(t: Tab): Promise<void> {
  const src = sourceOf(t.conn);
  t.typed = false;
  t.dirty = true;
  renderBuilder();
  try {
    t.sql = await api.generate(sqlModel(modelOf(t)), src?.kind ?? 'postgres');
    document.dispatchEvent(new CustomEvent('an-sql-changed'));
    saveTabs();
  } catch (e) { toast(errText(e), 'err'); }
}

// ---------------------------------------------------------------- what is on the canvas
const S = { sel: [] as string[], calcOpen: false, settle: [] as string[] };   // settle: new cards to move off the others once drawn

function cvTop(t: Tab): string {
  if (t.dirty && t.typed) return `<div class="an-cvt tl"><span>SQL changed. Press Run to update the builder.</span></div>`;
  const m = modelOf(t);
  if (t.notDrawn) return `<div class="an-cvt tl"><span>Change statements stay in SQL. The builder does not draw them.</span></div>`;
  if (m.locked.length) return `<div class="an-cvt tl"><span class="pill warn">Locked</span><span>This query uses ${esc(m.locked.join(', '))}. It still runs. Edit it in SQL.</span></div>`;
  const n = S.sel.length;
  return `<div class="an-cvt tl"><button type="button" class="sm" data-act="b-calc">+ Column</button><button type="button" class="sm" data-act="b-sub">+ Subquery</button><button type="button" class="sm" data-act="b-filters">Filters</button>` +
    `<button type="button" class="sm" data-act="b-del" ${n ? '' : 'disabled'} title="Delete key">Delete${n ? ` ${n}` : ''}</button><button type="button" class="sm" data-act="b-unused" title="Removes tables that are hidden and have no join">Remove unused</button>` +
    `<button type="button" class="sm" data-act="b-undo" ${t.undo.length ? '' : 'disabled'} title="Ctrl+Z">Undo</button><button type="button" class="sm" data-act="b-redo" ${t.redo.length ? '' : 'disabled'} title="Ctrl+Y">Redo</button></div>`;
}

function panel(t: Tab): string {
  const m = modelOf(t), p = m.parts, cl = ([['WHERE', p.where], ['GROUP BY', p.group], ['HAVING', p.having], ['ORDER BY', p.order]] as [string, string | undefined][]).filter((x) => x[1]);
  if (!m.calc.length && !cl.length) return '';
  const edit = !readOnly(t);
  return `<div class="an-cvt bl"><button type="button" class="an-cr" data-act="b-panel" aria-expanded="${S.calcOpen}"><b>${S.calcOpen ? '▾' : '▸'} Computed and filters</b><span class="faint num">${m.calc.length + cl.length}</span></button>` +
    (S.calcOpen ? m.calc.map((c, i) => `<div class="an-cr"><span class="badge solid temp">${esc(c.kind.toUpperCase())}</span><span class="lbl" title="${esc(c.expr)}">${esc(c.alias)}</span>${edit ? `<button type="button" class="icon-btn ghost xs" data-act="b-calc-edit" data-i="${i}" aria-label="Edit ${esc(c.alias)}">✎</button><button type="button" class="icon-btn ghost xs" data-act="b-calc-rm" data-i="${i}" aria-label="Remove ${esc(c.alias)}">×</button>` : ''}</div>`).join('') +
      cl.map((x) => `<div class="an-cr"><span class="badge solid">${x[0]}</span><span class="lbl" title="${esc(x[1])}">${esc(x[1])}</span></div>`).join('') : '') + '</div>';
}

export function renderBuilder(): void {
  const host = $('an-canvas');
  if (!host) return;
  const t = tab(), m = modelOf(t), v = t.view;
  const cards = m.tables.map((x) => {
    const cols = columnsOf(t, x), on = (c: string): boolean => x.star || x.sel.includes(c);
    const full = x.schema ? `${x.schema}.${x.name}` : x.name, name = x.label || x.name;
    const tip = `${x.label ? `${x.label} = ` : ''}${full} AS ${alias(x)}\n${x.show ? 'Shown in results. Click to hide.' : 'Hidden from results. Click to show.'} Right-click for more.`;
    return `<div class="an-tb${x.show ? '' : ' off'}${x.slim ? ' slim' : ''}${S.sel.includes(x.id) ? ' picked' : ''}${x.kind !== 'table' ? ' sub' : ''}" data-t="${esc(x.id)}" tabindex="0" aria-label="${esc(name)} table" style="left:${x.x}px;top:${x.y}px">` +
      `<div class="an-th" data-mv="${esc(x.id)}" title="${esc(tip)}">${x.kind === 'table' ? chip(sourceOf(t.conn)?.kind ?? '') : `<span class="badge alt" title="${esc(x.kind === 'remote' ? 'OPENQUERY: runs on the linked server' : x.kind === 'cte' ? 'a WITH query' : 'a subquery')}">${CARD_LABEL[x.kind] ?? 'SUB'}</span>`}<span class="lbl">${esc(name)}</span><button type="button" class="an-sl icon-btn ghost xs" data-act="b-slim" data-id="${esc(x.id)}" aria-label="${x.slim ? 'Show the full name' : 'Collapse'}: ${esc(name)}" title="${x.slim ? 'Show the full name' : 'Collapse the card'}">${icon(x.slim ? 'chev-r' : 'chev-l')}</button><button type="button" class="an-x icon-btn ghost xs" data-act="b-rm" data-id="${esc(x.id)}" aria-label="Remove ${esc(name)}">×</button></div>` +
      cols.map((c) => { const cast = x.cast[c.name]; return `<div class="an-col${on(c.name) ? ' sel' : ''}" tabindex="0" role="checkbox" aria-checked="${on(c.name)}" data-t="${esc(x.id)}" data-c="${esc(c.name)}"><span class="an-dot"></span><span class="lbl">${esc(c.name)}</span>${c.key ? `<span class="an-key">${esc(c.key)}</span>` : ''}${x.kind === 'table' ? `<button type="button" class="an-ty${cast ? ' cast' : ''}" data-act="b-cast" data-t="${esc(x.id)}" data-c="${esc(c.name)}" title="Change type with CAST">${esc(cast?.type ?? c.type ?? 'type')}</button>` : ''}</div>`; }).join('') + (x.star && x.kind !== 'table' && !cols.length ? '<div class="an-col sel an-allcols"><span class="an-dot"></span><span class="lbl faint">all columns (*)</span></div>' : '') + '</div>';
  }).join('');
  host.classList.toggle('ro', readOnly(t));
  host.innerHTML = cvTop(t) + panel(t) + `<div class="an-cvt br"><button type="button" class="sm" data-act="n-add" aria-haspopup="menu" title="Add a zone, text, sticky note or shape. Right-click the board adds one there.">${icon('plus')}Note</button><button type="button" class="icon-btn ghost xs" data-act="b-zoom" data-k="-1" aria-label="Zoom out">−</button><span class="num an-z" id="an-zl">${Math.round(v.z * 100)}%</span><button type="button" class="icon-btn ghost xs" data-act="b-zoom" data-k="1" aria-label="Zoom in">+</button><button type="button" class="sm" data-act="b-fit">Fit</button><button type="button" class="sm" data-act="b-ends" title="How a join line shows 1:1, 1:N, N:1 or N:M. Click to change.">${ENDS_LABEL[endsMode()]}</button></div>` +
    `<div class="an-board" id="an-board" style="transform:translate(${v.x}px,${v.y}px) scale(${v.z})">${layerHTML('back')}<svg class="an-lines" id="an-lines" width="2400" height="1600" aria-hidden="true"></svg>${cards}<span id="an-jl"></span>${layerHTML('front')}${m.adv ? `<div class="an-remote" style="left:${m.tables[0]?.x ?? 24}px;top:${Math.max(60, ...m.tables.map((x) => x.y)) + 240}px"><span class="pill warn">Locked part</span><pre class="an-hq">${esc(m.adv)}</pre></div>` : ''}</div>` +
    (!m.tables.length && !t.notDrawn ? '<div class="an-cvhint">Drag a table here from the object explorer.</div>' : '');
  host.style.backgroundSize = `${18 * v.z}px ${18 * v.z}px`;
  host.style.backgroundPosition = `${v.x}px ${v.y}px`;
  if (S.settle.length) settle();
  drawLines();
  renderNotes();
  loadMissing(t);
}

/** Move the new cards (S.settle) right of any card they cover: cards are as wide as their names now. One read of the
 *  drawn sizes (board px, so the zoom does not matter), then one write per moved card. */
function settle(): void {
  const b = $('an-board'), m = modelOf(tab()), ids = new Set(S.settle);
  S.settle = [];
  if (!b) return;
  const size = new Map<string, { w: number; h: number }>();
  b.querySelectorAll<HTMLElement>('.an-tb').forEach((e) => size.set(e.dataset['t'] ?? '', { w: e.offsetWidth, h: e.offsetHeight }));
  const GAP = 24, placed = m.tables.filter((x) => !ids.has(x.id));
  let moved = false;
  for (const x of m.tables.filter((y) => ids.has(y.id))) {
    const s = size.get(x.id);
    if (!s) continue;
    for (let i = 0; i < 60; i++) {
      const hit = placed.find((o) => { const q = size.get(o.id); return q && x.x < o.x + q.w + GAP && x.x + s.w + GAP > o.x && x.y < o.y + q.h + GAP && x.y + s.h + GAP > o.y; });
      if (!hit) break;
      x.x = hit.x + (size.get(hit.id)?.w ?? 200) + 80;                        // room for a join's label between them
      if (x.x + s.w > 2300) { x.x = 24; x.y += 200; }                          // the board's edge: the next row
      moved = true;
    }
    placed.push(x);
    const el = b.querySelector<HTMLElement>(`.an-tb[data-t="${CSS.escape(x.id)}"]`);
    if (el) { el.style.left = `${x.x}px`; el.style.top = `${x.y}px`; }
  }
  if (moved) saveTabs();
}

function drawLines(): void {
  const b = $('an-board'), t = tab();
  const lines = $('an-lines'), labels = $('an-jl');
  if (!b || !lines || !labels) return;
  const br = b.getBoundingClientRect(), z = t.view.z;
  const pt = (id: string, c: string): { l: number; r: number; y: number } | null => {
    const e = b.querySelector(`.an-col[data-t="${CSS.escape(id)}"][data-c="${CSS.escape(c)}"]`);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return { l: (r.left - br.left) / z, r: (r.right - br.left) / z, y: (r.top - br.top + r.height / 2) / z };
  };
  let p = markerDefs(), lb = '';
  const m = modelOf(t), ends = endsMode();
  m.joins.forEach((j, i) => {
    const A1 = pt(j.a[0], j.a[1]), B1 = pt(j.b[0], j.b[1]);
    if (!A1 || !B1) return;
    const ltr = A1.l < B1.l, x1 = ltr ? A1.r : A1.l, x2 = ltr ? B1.l : B1.r, dx = Math.max(34, Math.abs(x2 - x1) / 2) * (ltr ? 1 : -1);
    const d = `M${x1} ${A1.y}C${x1 + dx} ${A1.y} ${x2 - dx} ${B1.y} ${x2} ${B1.y}`;
    const c = ends === 'off' ? null : cardOf(t, m.tables, j), many = c?.card === 'N:M', w = many ? 'w' : '';
    const [ma, mb] = c ? sideMarks(c) : ['', ''];
    const mk = c && ends === 'crow' ? ` marker-start="url(#an-m${ma}${w})" marker-end="url(#an-m${mb}${w})"` : '';
    const na = m.tables.find((x) => x.id === j.a[0]), nb = m.tables.find((x) => x.id === j.b[0]);
    const tip = c ? cardTitle(c, na?.label || na?.name || 'left', nb?.label || nb?.name || 'right') : '';
    p += `<path class="an-jline${many ? ' many' : ''}" data-card="${c?.card ?? ''}" d="${d}" fill="none" stroke="var(${many ? '--warn' : '--accent'})" stroke-width="2"${mk}/><path class="an-jhit" data-j="${i}" d="${d}" fill="none" stroke="transparent" stroke-width="14"><title>${esc(tip ? `${tip}\n` : '')}Right-click to remove this join</title></path>`;
    lb += `<button type="button" class="an-jl${many ? ' many' : ''}" style="left:${(x1 + x2) / 2}px;top:${(A1.y + B1.y) / 2}px" data-act="b-join" data-i="${i}" data-j="${i}" title="${esc(tip ? `${tip}. ` : '')}Change this join. Right-click removes it.">${esc(j.type)}${c ? ` · ${c.card}` : ''}</button>`;
  });
  lines.innerHTML = p;
  labels.innerHTML = lb;
}

function applyView(): void {
  const b = $('an-board'), c = $('an-canvas'), v = tab().view;
  if (!b || !c) return;
  b.style.transform = `translate(${v.x}px,${v.y}px) scale(${v.z})`;
  c.style.backgroundSize = `${18 * v.z}px ${18 * v.z}px`;
  c.style.backgroundPosition = `${v.x}px ${v.y}px`;
  const l = $('an-zl');
  if (l) l.textContent = `${Math.round(v.z * 100)}%`;
  placeNoteBar();
}

function zoomAt(f: number, cx: number, cy: number): void {
  const v = tab().view, z = Math.max(0.3, Math.min(2, v.z * f));
  v.x = cx - (cx - v.x) * z / v.z;
  v.y = cy - (cy - v.y) * z / v.z;
  v.z = z;
  applyView();
}

export function fitView(): void {
  const c = $('an-canvas'), els = [...document.querySelectorAll<HTMLElement>('#an-board .an-tb, #an-board .an-remote, #an-board .an-note')], t = tab();
  if (!c || !els.length) return;
  const x0 = Math.min(...els.map((e) => e.offsetLeft)), y0 = Math.min(...els.map((e) => e.offsetTop));
  const x1 = Math.max(...els.map((e) => e.offsetLeft + e.offsetWidth)), y1 = Math.max(...els.map((e) => e.offsetTop + e.offsetHeight));
  const w = x1 - x0, h = y1 - y0, z = Math.max(0.3, Math.min(1, (c.clientWidth - 32) / w, (c.clientHeight - 96) / h));
  t.view = { z, x: (c.clientWidth - w * z) / 2 - x0 * z, y: (c.clientHeight - h * z) / 2 + 20 - y0 * z };
  applyView();
}

function syncSel(): void {
  document.querySelectorAll<HTMLElement>('#an-board .an-tb').forEach((e) => e.classList.toggle('picked', S.sel.includes(e.dataset['t'] ?? '')));
  const top = document.querySelector('#an-canvas .an-cvt.tl');
  if (top) top.outerHTML = cvTop(tab());
}

// ---------------------------------------------------------------- changes
export async function addTable(conn: string, db: string, schema: string, name: string): Promise<void> {
  const t = tab();
  if (readOnly(t)) { toast(t.dirty && t.typed ? 'Press Run first, so your typed SQL is not lost' : 'This query is locked: edit it in SQL'); return; }
  const m = modelOf(t);
  if (t.conn && t.conn !== conn) { toast('This tab uses another connection: open a new query tab for it'); return; }
  if (m.tables.some((x) => x.name === name && x.schema === schema)) { toast('This table is already in the builder'); return; }
  if (!t.conn) t.conn = conn;
  if (!t.db) t.db = db;
  record(t);
  const n = m.tables.length, v = t.view, used = new Set(m.tables.map((x) => x.id));
  let id = (name.replace(/\W/g, '')[0] ?? 't').toLowerCase(), i = 1;
  const base = id;
  while (used.has(id)) id = `${base}${++i}`;
  const card: MTable = { id, schema, name, database: db === t.db ? '' : db, alias: id, kind: 'table', show: true, sel: [], star: false, cast: {}, x: Math.round((24 + (n % 3) * 216 - v.x) / v.z), y: Math.round((60 + (n % 2) * 160 - v.y) / v.z) };
  m.tables.push(card);
  S.settle.push(id);
  await needInfo(t, card);
  await changed(t);
  fitIfOutside(id);
}

function fitIfOutside(id: string): void {
  const c = $('an-canvas')?.getBoundingClientRect(), e = document.querySelector(`.an-tb[data-t="${CSS.escape(id)}"]`)?.getBoundingClientRect();
  if (c && e && (e.right > c.right || e.bottom > c.bottom)) fitView();
}

async function removeTables(ids: string[]): Promise<void> {
  if (!ids.length) return;
  const t = tab(), m = modelOf(t);
  record(t);
  m.tables = m.tables.filter((x) => !ids.includes(x.id));
  m.joins = m.joins.filter((j) => !ids.includes(j.a[0]) && !ids.includes(j.b[0]));
  m.calc = m.calc.filter((c) => !ids.includes(c.t));
  S.sel = S.sel.filter((x) => !ids.includes(x));
  await changed(t);
  toast(`${ids.length} table${ids.length > 1 ? 's' : ''} removed from the builder. Your database is not changed.`);
}

function toggleColumn(id: string, c: string): void {
  const t = tab(), m = modelOf(t), x = m.tables.find((y) => y.id === id);
  if (!x) return;
  record(t);
  if (x.star) { x.star = false; x.sel = columnsOf(t, x).map((k) => k.name).filter((k) => k !== c); } else x.sel = x.sel.includes(c) ? x.sel.filter((k) => k !== c) : [...x.sel, c];
  void changed(t);
}

function toggleShow(id: string): void {
  const t = tab(), m = modelOf(t), x = m.tables.find((y) => y.id === id);
  if (!x) return;
  record(t);
  const group = new Set([id]);
  for (let grew = true; grew;) {
    grew = false;
    for (const j of m.joins) {
      if (group.has(j.a[0]) !== group.has(j.b[0])) { group.add(j.a[0]); group.add(j.b[0]); grew = true; }
    }
  }
  const next = !x.show;
  m.tables.forEach((y) => { if (group.has(y.id)) y.show = next; });
  void changed(t);
}

function joinDialog(i: number): void {
  const t = tab(), m = modelOf(t), j = m.joins[i];
  if (!j) return;
  const a = m.tables.find((x) => x.id === j.a[0]), b = m.tables.find((x) => x.id === j.b[0]);
  if (!a || !b) return;
  const opts = (x: MTable, cur: string): string => columnsOf(t, x).map((c) => `<option ${c.name === cur ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
  void openDialog({
    title: `Join ${a.name} and ${b.name}`, ok: 'Done', body: `<div class="an-form form-stack"><label>Join type<select id="an-j-type">${ORDER.map((k) => `<option ${k === j.type ? 'selected' : ''}>${k}</option>`).join('')}</select></label><label>${esc(a.name)} column<select id="an-j-a">${opts(a, j.a[1])}</select></label><label>${esc(b.name)} column<select id="an-j-b">${opts(b, j.b[1])}</select></label>` +
      `<label>Relationship (${esc(a.name)} : ${esc(b.name)})<select id="an-j-card">${relOptions(t, j)}</select></label><div class="an-jcheck"><button type="button" class="sm" id="an-j-check" title="Counts both sides in your database (read-only): is each join value there once, or many times?">Check with data</button><span class="faint" id="an-j-cstat"></span></div>` +
      `<label class="check"><input type="checkbox" id="an-j-rm"> Remove this join</label><p class="faint">Press Run to see the new result.</p></div>`,
    bind: (root) => {
      const btn = root.querySelector<HTMLButtonElement>('#an-j-check'), st = root.querySelector<HTMLElement>('#an-j-cstat');
      btn?.addEventListener('click', () => {
        btn.disabled = true;
        if (st) st.textContent = 'Counting…';
        void checkCard(t, i).then((c) => {
          if (st) st.textContent = c ? `The data says ${c}.` : 'Could not count this join.';
          const sel = root.querySelector<HTMLSelectElement>('#an-j-card');
          if (sel) sel.innerHTML = relOptions(t, j);
        }).catch((e: unknown) => { if (st) st.textContent = errText(e); }).finally(() => { btn.disabled = false; });
      });
    },
    onOk: (root) => {
      record(t);
      if (root.querySelector<HTMLInputElement>('#an-j-rm')?.checked) m.joins.splice(i, 1);
      else {
        const rel = root.querySelector<HTMLSelectElement>('#an-j-card')?.value ?? '';
        if (rel === 'auto') { delete j.card; delete j.cardFrom; } else if (rel !== 'keep') { j.card = rel as Card; j.cardFrom = 'you'; }
        const type = root.querySelector<HTMLSelectElement>('#an-j-type')?.value as typeof j.type;
        m.joins.forEach((x) => { if (samePair(x, j)) x.type = type; });
        j.a[1] = root.querySelector<HTMLSelectElement>('#an-j-a')?.value ?? j.a[1];
        j.b[1] = root.querySelector<HTMLSelectElement>('#an-j-b')?.value ?? j.b[1];
      }
      void changed(t);
      return null;
    },
  });
}

/** The Relationship choices of the join dialog: Auto (what the keys or a count say), the four relations, and the current one. */
function relOptions(t: Tab, j: Join): string {
  const auto = cardOf(t, modelOf(t).tables, { a: j.a, b: j.b, type: j.type });
  const now = j.card ? (j.cardFrom === 'data' ? 'keep' : j.card) : 'auto';
  const autoLabel = auto ? `Auto: ${auto.card} (${auto.from === 'data' ? 'counted in the data' : 'from the keys'})` : 'Auto (not known: no keys)';
  return (j.card && j.cardFrom === 'data' ? `<option value="keep" selected>${esc(j.card)} (counted in the data)</option>` : '') +
    `<option value="auto" ${now === 'auto' ? 'selected' : ''}>${esc(autoLabel)}</option>` +
    (['1:1', '1:N', 'N:1', 'N:M'] as const).map((k) => `<option value="${k}" ${now === k ? 'selected' : ''}>${k} (set by you)</option>`).join('');
}

/** Count the join in the data (read-only) and keep what it says as the join's relation -> the relation, or null. */
export async function checkCard(t: Tab, i: number): Promise<Card | null> {
  await countJoin(i);
  const j = modelOf(t).joins[i], s = j ? t.joinStats[statKey(j)] : undefined;
  if (!j || !s || s.a_unique === undefined || s.b_unique === undefined) return null;
  j.card = s.a_unique ? (s.b_unique ? '1:1' : '1:N') : (s.b_unique ? 'N:1' : 'N:M');
  j.cardFrom = 'data';
  saveTabs();
  if (tab() === t) drawLines();
  return j.card;
}

function castDialog(id: string, c: string): void {
  const t = tab(), m = modelOf(t), x = m.tables.find((y) => y.id === id);
  if (!x) return;
  const kind = sourceOf(t.conn)?.kind ?? 'postgres', now = x.cast[c], groups = TYPES[kind] ?? TYPES['postgres'] ?? [];
  const all = groups.flatMap(([, ts]) => ts), was = columnsOf(t, x).find((k) => k.name === c)?.type ?? '';
  const preview = (v: string, safe: boolean): string => (kind === 'mongo' ? `${c} → ${v || '…'}` : `${safe ? 'TRY_CAST' : 'CAST'}(${alias(x)}.${c} AS ${v || '…'})`);
  void openDialog({
    title: `Change the type of ${c}`, ok: 'Apply', focus: '.an-cast .chip.on, .an-cast .chip',
    body: `<div class="an-form form-stack an-cast"><p class="faint">This changes the query only. The table in your database stays the same.</p>` +
      `<div class="an-cast-now"><span class="faint">Now</span><code>${esc(was || 'unknown')}</code>${now ? `<span class="faint">→ cast to</span><code>${esc(now.type)}</code>` : ''}</div>` +
      groups.map(([g, ts]) => `<div class="an-cast-grp"><span class="an-cast-gl">${esc(g)}</span><div class="chips">${ts.map((y) =>
        `<button type="button" class="chip${y === now?.type ? ' on' : ''}" data-ty="${esc(y)}" aria-pressed="${y === now?.type}">${esc(y)}</button>`).join('')}</div></div>`).join('') +
      `<label class="fld"><span>Type</span>${comboHTML('an-cast', now?.type ?? '', { mono: true, placeholder: 'pick one above, or type e.g. varchar(50)', attrs: 'id="an-c-type"' })}</label>` +
      (kind === 'mssql' ? `<label class="check"><input type="checkbox" id="an-c-safe" ${now?.safe ? 'checked' : ''}> Use TRY_CAST, so a bad value becomes NULL instead of failing the query</label>` : '') +
      `<pre class="an-prev" id="an-c-prev">${esc(preview(now?.type ?? '', !!now?.safe))}</pre>` +
      (now ? '<label class="check"><input type="checkbox" id="an-c-rm"> Remove this CAST</label>' : '') + '</div>',
    bind: (root) => {
      const inp = root.querySelector<HTMLInputElement>('#an-c-type'), safe = root.querySelector<HTMLInputElement>('#an-c-safe');
      const sync = (): void => {
        const v = inp?.value.trim() ?? '';
        root.querySelectorAll<HTMLElement>('[data-ty]').forEach((b) => { const on = b.dataset['ty'] === v; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); });
        const pv = root.querySelector('#an-c-prev');
        if (pv) pv.textContent = preview(v, !!safe?.checked);
      };
      bindCombo(root, 'an-cast', { options: () => all, onPick: sync });
      root.addEventListener('input', sync);
      root.addEventListener('change', sync);
      root.addEventListener('click', (e) => {
        const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-ty]') : null;
        if (!b || !inp) return;
        inp.value = b.dataset['ty'] ?? '';
        sync();                                    // the focus stays on the chip: the input's list opens only when asked
      });
    },
    onOk: (root) => {
      const v = root.querySelector<HTMLInputElement>('#an-c-type')?.value.trim() ?? '';
      const rm = root.querySelector<HTMLInputElement>('#an-c-rm')?.checked;
      if (!rm && !TYPE_RE.test(v)) return 'Pick a type, or write one such as int or varchar(100).';
      record(t);
      if (rm) delete x.cast[c];
      else { x.cast[c] = { type: v, safe: !!root.querySelector<HTMLInputElement>('#an-c-safe')?.checked }; if (!x.sel.includes(c) && !x.star) x.sel.push(c); }
      void changed(t);
      return null;
    },
  });
}

const allColumns = (t: Tab): string[] => modelOf(t).tables.filter((x) => x.show).flatMap((x) => columnsOf(t, x).map((c) => `${alias(x)}.${c.name}`));

interface Spec { tab: 'case' | 'window' | 'expr'; alias: string; whens: { col: string; op: string; val: string; then: string }[]; els: string; fn: string; arg: string; n: string; part: string; ord: string; dir: string; frame: string; expr: string }

function specExpr(c: Spec, kind: string): string {
  const q = (v: string): string => (v === '' ? "''" : /^-?\d+(\.\d+)?$/.test(v) ? v : `'${v.replace(/'/g, "''")}'`);
  if (c.tab === 'expr') return c.expr;
  if (c.tab === 'case') return `CASE ${c.whens.map((w) => `WHEN ${w.col} ${w.op === 'IS NULL' ? 'IS NULL' : `${w.op} ${q(w.val)}`} THEN ${q(w.then)}`).join(' ')}${c.els !== '' ? ` ELSE ${q(c.els)}` : ''} END`;
  const F = c.fn, pg = kind === 'postgres', name = pg && F === 'STDEV' ? 'STDDEV' : pg && F === 'VAR' ? 'VARIANCE' : F;
  const arg = NO_ARG.has(F) ? '' : F === 'NTILE' ? c.n : F === 'LAG' || F === 'LEAD' || F === 'NTH_VALUE' ? `${c.arg}, ${c.n}` : F === 'COUNT' && !c.arg ? '*' : c.arg;
  const over = [c.part && `PARTITION BY ${c.part}`, c.ord && `ORDER BY ${c.ord} ${c.dir}`, c.ord && c.frame].filter(Boolean).join(' ');
  return `${name}(${arg}) OVER (${over})`;
}

function calcDialog(edit: number): void {
  const t = tab(), m = modelOf(t), cols = allColumns(t), kind = sourceOf(t.conn)?.kind ?? 'postgres', first = cols[0] ?? '', old: Calc | undefined = m.calc[edit];
  if (!m.tables.length) { toast('Add a table first'); return; }
  const c: Spec = old?.spec ? JSON.parse(JSON.stringify(old.spec)) as Spec : { tab: old ? 'expr' : 'case', alias: old?.alias ?? '', whens: [{ col: first, op: '>=', val: '', then: '' }], els: '', fn: 'ROW_NUMBER', arg: first, n: '1', part: '', ord: first, dir: 'DESC', frame: '', expr: old?.expr ?? '' };
  const opt = (v: string, none = ''): string => `${none ? `<option value="">${none}</option>` : ''}${cols.map((x) => `<option ${x === v ? 'selected' : ''}>${esc(x)}</option>`).join('')}`;
  const fns = WINDOW_FNS.filter((x) => !(kind === 'mssql' && x === 'NTH_VALUE'));
  const body = (): string => {
    let b = '';
    if (c.tab === 'case') b = `${c.whens.map((w, i) => `<div class="an-when"><select aria-label="Column" data-w="${i}" data-f="col">${opt(w.col)}</select><select aria-label="Condition" data-w="${i}" data-f="op">${['=', '<>', '>', '>=', '<', '<=', 'LIKE', 'IS NULL'].map((o) => `<option ${o === w.op ? 'selected' : ''}>${o}</option>`).join('')}</select><input aria-label="Value" placeholder="value" data-w="${i}" data-f="val" value="${esc(w.val)}"><input aria-label="Then" placeholder="then" data-w="${i}" data-f="then" value="${esc(w.then)}"></div>`).join('')}<button type="button" class="sm" data-add="1">+ WHEN</button><label>ELSE<input data-c="els" value="${esc(c.els)}"></label>`;
    if (c.tab === 'window') b = `<label>Function<select data-c="fn" data-re="1">${fns.map((x) => `<option ${x === c.fn ? 'selected' : ''}>${x}</option>`).join('')}</select></label>${NO_ARG.has(c.fn) || c.fn === 'NTILE' ? '' : `<label>Column<select data-c="arg">${opt(c.arg)}</select></label>`}${['NTILE', 'LAG', 'LEAD', 'NTH_VALUE'].includes(c.fn) ? `<label>${c.fn === 'NTILE' ? 'Number of groups' : 'Offset'}<input data-c="n" type="number" min="1" value="${esc(c.n)}"></label>` : ''}<label>Partition by<select data-c="part">${opt(c.part, 'No partition')}</select></label><label>Order by<select data-c="ord">${opt(c.ord, 'No order')}</select></label><label>Direction<select data-c="dir"><option ${c.dir === 'DESC' ? 'selected' : ''}>DESC</option><option ${c.dir === 'ASC' ? 'selected' : ''}>ASC</option></select></label><label>Frame<select data-c="frame">${[['', 'Default'], ['ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW', 'Running, from the first row'], ['ROWS BETWEEN 6 PRECEDING AND CURRENT ROW', 'Last 7 rows'], ['ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING', 'All rows in the partition']].map(([v, n]) => `<option value="${v}" ${v === c.frame ? 'selected' : ''}>${n}</option>`).join('')}</select></label><p class="faint">Anything else, such as PERCENTILE_CONT, goes in the Expression tab.</p>`;
    if (c.tab === 'expr') b = `<label>SQL expression<textarea data-c="expr">${esc(c.expr)}</textarea></label>`;
    return `<div class="an-seg segmented">${([['case', 'CASE WHEN'], ['window', 'Window function'], ['expr', 'Expression']] as const).map(([k, n]) => `<button type="button" class="view-btn ${c.tab === k ? 'active' : ''}" data-tab="${k}">${n}</button>`).join('')}</div><label>Column name<input id="an-k-alias" data-c="alias" value="${esc(c.alias)}"></label>${b}<pre class="an-prev" id="an-k-prev">${esc(specExpr(c, kind))}</pre>`;
  };
  void openDialog({
    title: `${old ? 'Edit' : 'Add'} computed column`, wide: true, ok: 'Apply', body: `<div class="an-form form-stack" id="an-k">${body()}</div>`, focus: '#an-k-alias',
    bind: (root) => {
      const form = root.querySelector<HTMLElement>('#an-k');
      if (!form) return;
      const repaint = (): void => { form.innerHTML = body(); };
      form.addEventListener('click', (e) => {
        const b = (e.target as Element).closest<HTMLElement>('button');
        if (b?.dataset['tab']) { c.tab = b.dataset['tab'] as Spec['tab']; repaint(); }
        if (b?.dataset['add']) { c.whens.push({ col: first, op: '>=', val: '', then: '' }); repaint(); }
      });
      const input = (e: Event): void => {
        const i = e.target as HTMLInputElement, w = i.dataset['w'], k = i.dataset['c'];
        if (w !== undefined) { const row = c.whens[Number(w)]; if (row) row[i.dataset['f'] as 'col'] = i.value; } else if (k) (c as unknown as Record<string, string>)[k] = i.value;
        if (i.dataset['re']) { repaint(); return; }
        const p = form.querySelector('#an-k-prev');
        if (p) p.textContent = specExpr(c, kind);
      };
      form.addEventListener('input', input);
      form.addEventListener('change', input);
    },
    onOk: () => {
      const name = c.alias.trim(), e = specExpr(c, kind);
      if (!/^[A-Za-z_]\w*$/.test(name)) return 'Give the column a name, using letters, digits and _.';
      if (!e.trim()) return 'The expression is empty.';
      if (c.tab === 'window' && !c.ord && /RANK|ROW_NUMBER|NTILE|LAG|LEAD|CUME|VALUE/.test(c.fn)) return `${c.fn} needs an Order by column.`;
      const ref = /\b(\w+)\.\w+/.exec(e), owner = m.tables.find((x) => alias(x) === ref?.[1]) ?? m.tables[0];
      record(t);
      const rec: Calc = { alias: name, t: owner?.id ?? '', kind: c.tab === 'expr' ? (old?.kind ?? 'expr') : c.tab, expr: e, spec: c };
      if (old) m.calc[edit] = rec; else m.calc.push(rec);
      void changed(t);
      return null;
    },
  });
}

function subDialog(): void {
  const t = tab(), m = modelOf(t);
  void openDialog({
    title: 'Add a subquery', ok: 'Add to builder', wide: true, body: '<div class="an-form form-stack"><label>Name<input id="an-s-name" value="sub1"></label><label>SELECT query<textarea id="an-s-sql" placeholder="SELECT id, COUNT(*) AS n FROM ..."></textarea></label></div>',
    onOk: async (root) => {
      const name = root.querySelector<HTMLInputElement>('#an-s-name')?.value.trim() ?? '', sql = root.querySelector<HTMLTextAreaElement>('#an-s-sql')?.value.trim() ?? '';
      if (!/^[A-Za-z_]\w*$/.test(name)) return 'Give it a name, using letters, digits and _.';
      if (m.tables.some((x) => alias(x) === name)) return 'That name is already used.';
      const p = await api.parseModel(t.conn ?? '', sql);
      if (p.error) return p.error;
      if (p.statement === 'write') return 'A subquery must only read data.';
      record(t);
      const n = m.tables.length, v = t.view;
      m.tables.push({ id: name, schema: '', name, database: '', alias: name, kind: 'sub', sql: sql.replace(/;\s*$/, ''), cols: p.output ?? [], show: true, sel: [], star: false, cast: {}, x: Math.round((24 + (n % 3) * 216 - v.x) / v.z), y: Math.round((60 + (n % 2) * 160 - v.y) / v.z) });
      void changed(t);
      return null;
    },
  });
}

function filterDialog(): void {
  const t = tab(), m = modelOf(t), p = m.parts;
  const rows: [string, string][] = [['where', 'WHERE'], ['group', 'GROUP BY'], ['having', 'HAVING'], ['order', 'ORDER BY']];
  void openDialog({
    title: 'Filters and grouping', ok: 'Apply', body: `<div class="an-form form-stack">${rows.map(([k, n]) => `<label>${n}<input id="an-p-${k}" value="${esc((p as Record<string, unknown>)[k] ?? '')}"></label>`).join('')}<p class="faint">Subqueries work here too, such as id IN (SELECT ...).</p></div>`,
    onOk: (root) => {
      record(t);
      for (const [k] of rows) {
        const v = root.querySelector<HTMLInputElement>(`#an-p-${k}`)?.value.trim();
        if (v) (p as Record<string, unknown>)[k] = v; else delete (p as Record<string, unknown>)[k];
      }
      void changed(t);
      return null;
    },
  });
}

export const B_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  'b-calc': () => calcDialog(-1),
  'b-calc-edit': (el) => calcDialog(Number(el.dataset['i'])),
  'b-calc-rm': (el) => { const t = tab(); record(t); modelOf(t).calc.splice(Number(el.dataset['i']), 1); void changed(t); },
  'b-sub': () => subDialog(),
  'b-filters': () => filterDialog(),
  'b-panel': () => { S.calcOpen = !S.calcOpen; renderBuilder(); },
  'b-del': () => removeTables([...S.sel]),
  'b-unused': () => {
    const m = modelOf(tab()), used = new Set(m.joins.flatMap((j) => [j.a[0], j.b[0]]));
    const ids = m.tables.filter((x) => !x.show && !used.has(x.id)).map((x) => x.id);
    if (!ids.length) { toast('No unused tables. Unused means hidden and not joined.'); return undefined; }
    return removeTables(ids);
  },
  'b-rm': (el) => removeTables([el.dataset['id'] ?? '']),
  'b-undo': () => undo(),
  'b-redo': () => redo(),
  'b-zoom': (el) => { const c = $('an-canvas')?.getBoundingClientRect(); if (c) zoomAt(Number(el.dataset['k']) > 0 ? 1.2 : 1 / 1.2, c.width / 2, c.height / 2); },
  'b-fit': () => fitView(),
  'b-join': (el) => joinDialog(Number(el.dataset['i'])),
  'b-ends': (el) => { const v = ENDS_NEXT[endsMode()]; setEndsMode(v); el.textContent = ENDS_LABEL[v]; drawLines(); },
  'b-cast': (el) => castDialog(el.dataset['t'] ?? '', el.dataset['c'] ?? ''),
  'b-slim': (el) => cardAct(el.dataset['id'] ?? '', 'slim'),
};

// ---------------------------------------------------------------- a card's menu (right-click, or the menu key)
function cardMenu(id: string, at?: { x: number; y: number }): void {
  const t = tab(), x = modelOf(t).tables.find((y) => y.id === id);
  const el = document.querySelector<HTMLElement>(`#an-board .an-tb[data-t="${CSS.escape(id)}"]`);
  if (!x || !el) return;
  const full = x.schema ? `${x.schema}.${x.name}` : x.name, ro = readOnly(t);
  let h = menuHead(x.label ? `${x.label} · ${full}` : full) + menuItem('rename', 'Rename…', 'edit') +
    (x.label ? menuItem('unname', 'Use the table name', 'undo') : '') +
    menuItem('slim', x.slim ? 'Show the full name' : 'Collapse the card', x.slim ? 'chev-r' : 'chev-l');
  if (!ro) {
    h += MENU_SEP + menuItem('show', x.show ? 'Hide from results' : 'Show in results', x.show ? 'eye-off' : 'eye') +
      menuItem('all', 'Select all columns', 'check-c') + menuItem('none', 'Clear the columns', 'circle') +
      MENU_SEP + menuItem('rm', 'Remove from the builder', 'trash', '', 'danger');
  }
  if (!at) { const r = el.getBoundingClientRect(); at = { x: r.left + 16, y: r.top + 32 }; }
  openMenu(h, el, (k) => cardAct(id, k), at);
}

async function cardAct(id: string, k: string): Promise<void> {
  const t = tab(), x = modelOf(t).tables.find((y) => y.id === id);
  if (!x) return;
  const full = x.schema ? `${x.schema}.${x.name}` : x.name;
  if (k === 'rename') {
    const v = await promptDialog({ title: 'Rename the card', label: 'Name', value: x.label || x.name, ok: 'Rename',
      hint: `Only the card's header changes: the SQL still reads ${full} AS ${alias(x)}.`,
      check: (s) => (s.length > 80 ? 'Keep it to 80 characters' : null) });
    if (v === null) return;
    if (v === x.name) delete x.label; else x.label = v;
  } else if (k === 'unname') delete x.label;
  else if (k === 'slim') { if (x.slim) delete x.slim; else x.slim = true; }
  else if (readOnly(t)) return;
  else if (k === 'show') { toggleShow(id); return; }
  else if (k === 'all' || k === 'none') { record(t); x.star = k === 'all'; x.sel = []; void changed(t); return; }
  else if (k === 'rm') { await removeTables([id]); return; }
  renderBuilder();                                   // the card only: the SQL is the same
  saveTabs();
  document.querySelector<HTMLElement>(`#an-board .an-tb[data-t="${CSS.escape(id)}"]`)?.focus();
}

// ---------------------------------------------------------------- pointer, keyboard, drops
type Drag =
  | { kind: 'pan'; sx: number; sy: number; ox: number; oy: number; moved: boolean; join: string | null; card: string | null; note: string | null }
  | { kind: 'marq'; sx: number; sy: number; el: HTMLElement; r: DOMRect; add: boolean; moved: boolean }
  | { kind: 'move'; id: string; sx: number; sy: number; moved: boolean; group: { m: MTable; ox: number; oy: number }[]; pins: { n: Note; ox: number; oy: number }[] }
  | { kind: 'col'; t: string; c: string; sx: number; sy: number; moved: boolean };
let P: Drag | null = null;
let panEnd = 0;

export function builderDown(e: PointerEvent): boolean {
  const target = e.target instanceof Element ? e.target : null;
  const cv = target?.closest<HTMLElement>('#an-canvas');
  if (!target || !cv) return false;
  if (target.closest('.an-cvt, .an-nbar')) return false;
  const t = tab();
  if (e.button === 1 || e.button === 2 || (e.pointerType === 'touch' && !target.closest('.an-tb'))) {
    P = { kind: 'pan', sx: e.clientX, sy: e.clientY, ox: t.view.x, oy: t.view.y, moved: false, join: target.closest<HTMLElement>('[data-j]')?.dataset['j'] ?? null, card: target.closest<HTMLElement>('.an-tb')?.dataset['t'] ?? null, note: target.closest<HTMLElement>('.an-note')?.dataset['n'] ?? null };
    cv.classList.add('panning');
    e.preventDefault();
    return true;
  }
  if (e.button !== 0 || target.closest('.an-x,.an-ty,.an-sl')) return false;
  if (noteDown(e, target)) return true;
  const head = target.closest<HTMLElement>('.an-th[data-mv]'), col = target.closest<HTMLElement>('.an-col');
  if (head && !e.shiftKey && !e.ctrlKey && !e.metaKey) clearNoteSel();
  if (head) {
    const id = head.dataset['mv'] ?? '', ids = S.sel.includes(id) ? S.sel : [id];
    const group = ids.flatMap((x) => { const m = modelOf(t).tables.find((y) => y.id === x); return m ? [{ m, ox: m.x, oy: m.y }] : []; });
    const pins = pinnedTo(new Set(group.map((g) => g.m.id))).map((n) => ({ n, ox: n.x, oy: n.y }));   // their descriptions
    P = { kind: 'move', id, sx: e.clientX, sy: e.clientY, moved: false, group, pins };
    e.preventDefault();
    return true;
  }
  if (col) { P = { kind: 'col', t: col.dataset['t'] ?? '', c: col.dataset['c'] ?? '', sx: e.clientX, sy: e.clientY, moved: false }; e.preventDefault(); return true; }
  if (!target.closest('button')) {
    const el = document.createElement('div');
    el.className = 'an-marq';
    cv.append(el);
    P = { kind: 'marq', sx: e.clientX, sy: e.clientY, el, r: cv.getBoundingClientRect(), add: e.shiftKey, moved: false };
    e.preventDefault();
    return true;
  }
  return false;
}

export function builderMove(e: PointerEvent): void {
  if (noteMove(e) || !P) return;
  const t = tab();
  if (P.kind === 'pan') {
    const dx = e.clientX - P.sx, dy = e.clientY - P.sy;
    if (Math.hypot(dx, dy) > 3) P.moved = true;
    t.view.x = P.ox + dx;
    t.view.y = P.oy + dy;
    applyView();
  } else if (P.kind === 'marq') {
    P.moved = true;
    const x = Math.min(P.sx, e.clientX), y = Math.min(P.sy, e.clientY);
    Object.assign(P.el.style, { left: `${x - P.r.left}px`, top: `${y - P.r.top}px`, width: `${Math.abs(e.clientX - P.sx)}px`, height: `${Math.abs(e.clientY - P.sy)}px` });
  } else if (P.kind === 'move') {
    const z = t.view.z, dx = (e.clientX - P.sx) / z, dy = (e.clientY - P.sy) / z;
    if (Math.hypot(e.clientX - P.sx, e.clientY - P.sy) > 4) P.moved = true;
    if (P.moved) {
      for (const g of P.group) {
        g.m.x = g.ox + dx;
        g.m.y = g.oy + dy;
        const el = document.querySelector<HTMLElement>(`.an-tb[data-t="${CSS.escape(g.m.id)}"]`);
        if (el) { el.style.left = `${g.m.x}px`; el.style.top = `${g.m.y}px`; }
      }
      for (const g of P.pins) {
        g.n.x = Math.round(g.ox + dx);
        g.n.y = Math.round(g.oy + dy);
        const el = document.querySelector<HTMLElement>(`.an-note[data-n="${CSS.escape(g.n.id)}"]`);
        if (el) { el.style.left = `${g.n.x}px`; el.style.top = `${g.n.y}px`; }
      }
      drawLines();
    }
  } else {
    if (Math.hypot(e.clientX - P.sx, e.clientY - P.sy) > 5) P.moved = true;
    if (P.moved) {
      const b = $('an-board')?.getBoundingClientRect(), st = document.querySelector(`.an-col[data-t="${CSS.escape(P.t)}"][data-c="${CSS.escape(P.c)}"]`)?.getBoundingClientRect(), z = t.view.z;
      if (!b || !st) return;
      drawLines();
      $('an-lines')?.insertAdjacentHTML('beforeend', `<path d="M${(st.right - b.left) / z} ${(st.top - b.top + st.height / 2) / z}L${(e.clientX - b.left) / z} ${(e.clientY - b.top) / z}" stroke="var(--accent)" stroke-width="2" stroke-dasharray="5 4" fill="none"/>`);
      document.querySelectorAll('.an-col.drop').forEach((x) => x.classList.remove('drop'));
      const o = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>('.an-col');
      if (o && o.dataset['t'] !== P.t) o.classList.add('drop');
    }
  }
}

export function builderUp(e: PointerEvent): void {
  if (noteUp()) return;
  const p = P;
  P = null;
  if (!p) return;
  const t = tab(), m = modelOf(t);
  if (p.kind === 'pan') {
    $('an-canvas')?.classList.remove('panning');
    panEnd = Date.now();
    if (!p.moved && p.card && p.join === null && e.button === 2) { cardMenu(p.card, { x: e.clientX, y: e.clientY }); return; }
    if (!p.moved && e.button === 2 && p.note && !p.card && p.join === null && noteMenu(p.note, { x: e.clientX, y: e.clientY })) return;
    if (!p.moved && e.button === 2 && !p.card && p.join === null) {               // right-click on the board: add a note there
      const cv = $('an-canvas'), r = cv?.getBoundingClientRect(), v = t.view;
      if (cv && r) addMenu(cv, { x: e.clientX, y: e.clientY }, { x: (e.clientX - r.left - v.x) / v.z, y: (e.clientY - r.top - v.y) / v.z });
      return;
    }
    if (!p.moved && p.join !== null && e.button === 2 && !readOnly(t)) { record(t); m.joins.splice(Number(p.join), 1); void changed(t); toast('Join removed. Press Run to update the result.'); }
    return;
  }
  if (p.kind === 'marq') {
    const r = p.el.getBoundingClientRect();
    p.el.remove();
    const hit = [...document.querySelectorAll<HTMLElement>('#an-board .an-tb')].filter((el) => { const b = el.getBoundingClientRect(); return p.moved && b.left < r.right && b.right > r.left && b.top < r.bottom && b.bottom > r.top; }).map((el) => el.dataset['t'] ?? '');
    S.sel = p.add ? [...new Set([...S.sel, ...hit])] : hit;
    syncSel();
    if (p.moved) marqueeNotes(r, p.add); else if (!p.add) clearNoteSel();
    return;
  }
  if (p.kind === 'move') {
    if (p.moved) { saveTabs(); return; }
    if (e.ctrlKey || e.metaKey || e.shiftKey) { S.sel = S.sel.includes(p.id) ? S.sel.filter((x) => x !== p.id) : [...S.sel, p.id]; syncSel(); return; }
    toggleShow(p.id);
    return;
  }
  document.querySelectorAll('.an-col.drop').forEach((x) => x.classList.remove('drop'));
  if (!p.moved) { toggleColumn(p.t, p.c); return; }
  const o = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>('.an-col');
  if (o && o.dataset['t'] !== p.t) {
    record(t);
    const nj: Join = { a: [p.t, p.c], b: [o.dataset['t'] ?? '', o.dataset['c'] ?? ''], type: 'INNER' };
    nj.type = m.joins.find((x) => samePair(x, nj))?.type ?? 'INNER';
    m.joins.push(nj);
    m.tables.forEach((x) => { if (x.id === p.t || x.id === o.dataset['t']) x.show = true; });
    void changed(t);
    toast('Join added. Press Run to see the result.');
  } else drawLines();
}

export function builderContext(e: MouseEvent): void {
  if ((e.target instanceof Element && e.target.closest('#an-canvas')) || Date.now() - panEnd < 200) e.preventDefault();
}

export function builderWheel(e: WheelEvent): void {
  const c = e.target instanceof Element ? e.target.closest<HTMLElement>('#an-canvas') : null;
  if (!c || (e.target as Element).closest('.an-cvt')) return;
  e.preventDefault();
  const r = c.getBoundingClientRect();
  zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top);
}

export function builderKey(e: KeyboardEvent): boolean {
  const target = e.target instanceof HTMLElement ? e.target : null;
  if (!target || /INPUT|TEXTAREA|SELECT/.test(target.tagName) || target.closest('.cm-editor') || target.isContentEditable) return false;
  if ((target === document.body || target.closest('#an-canvas')) && noteKey(e)) return true;
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (mod && k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); return true; }
  if (mod && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); redo(); return true; }
  const t = tab();
  if ((e.key === 'Delete' || e.key === 'Backspace') && S.sel.length && !readOnly(t)) { e.preventDefault(); void removeTables([...S.sel]); return true; }
  const col = target.closest<HTMLElement>('.an-col');
  if (col && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); toggleColumn(col.dataset['t'] ?? '', col.dataset['c'] ?? ''); return true; }
  if (target.classList.contains('an-tb') && (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10'))) { e.preventDefault(); cardMenu(target.dataset['t'] ?? ''); return true; }
  if (target.classList.contains('an-tb')) {
    const m = modelOf(t).tables.find((x) => x.id === target.dataset['t']), d = ({ ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20] } as Record<string, number[]>)[e.key];
    if (m && d) { e.preventDefault(); m.x += d[0] ?? 0; m.y += d[1] ?? 0; target.style.left = `${m.x}px`; target.style.top = `${m.y}px`; drawLines(); return true; }
    if (m && e.key === 'Enter') { e.preventDefault(); toggleShow(m.id); return true; }
    if (m && e.key === 'Delete') { e.preventDefault(); void removeTables([m.id]); return true; }
  }
  return false;
}

/** After a typed query ran: the canvas follows the text. */
export async function syncFromSql(t: Tab): Promise<void> {
  if (!t.conn) return;
  const kind = sourceOf(t.conn)?.kind;
  if (kind === 'mongo') { t.notDrawn = true; return; }
  const p = await api.parseModel(t.conn, t.sql);
  if (p.error) return;
  t.notDrawn = p.statement === 'write';
  if (t.notDrawn) return;
  const old = new Map(modelOf(t).tables.map((x) => [`${x.schema}.${x.name}`, x]));
  p.tables.forEach((x, i) => {
    const o = old.get(`${x.schema}.${x.name}`);
    // three cards a row, 100 px apart so a join's label sits between them; the middle one a little lower, so a line
    // from the first card to the third does not run under it
    x.x = o?.x ?? 24 + (i % 3) * 300;
    x.y = o?.y ?? 60 + Math.floor(i / 3) * 240 + (i % 3 === 1 ? 110 : 0);
    if (o?.label) x.label = o.label;
    if (o?.slim) x.slim = true;
    if (!o) S.settle.push(x.id);
  });
  // what the SQL cannot hold stays: a join's relation the user set or counted (matched by table and column names)
  const was = modelOf(t), oldName = new Map(was.tables.map((x) => [x.id, `${x.schema}.${x.name}`]));
  const newName = new Map(p.tables.map((x) => [x.id, `${x.schema}.${x.name}`]));
  const keyOf = (names: Map<string, string>, j: Join, flip: boolean): string => {
    const [x, y] = flip ? [j.b, j.a] : [j.a, j.b];
    return `${names.get(x[0]) ?? ''}.${x[1]}|${names.get(y[0]) ?? ''}.${y[1]}`.toLowerCase();
  };
  const kept = new Map<string, Join>();
  was.joins.forEach((j) => { if (j.card) kept.set(keyOf(oldName, j, false), j); });
  if (kept.size) {
    for (const j of p.joins) {
      const same = kept.get(keyOf(newName, j, false)), flip = same ? undefined : kept.get(keyOf(newName, j, true));
      const o = same ?? flip;
      if (!o?.card) continue;
      j.card = flip ? (({ '1:N': 'N:1', 'N:1': '1:N' } as Record<string, Card>)[o.card] ?? o.card) : o.card;
      if (o.cardFrom) j.cardFrom = o.cardFrom;
    }
  }
  if (was.notes?.length) (p as Model).notes = was.notes;           // the board's notes are not in the SQL: they stay
  t.model = p as Model;
  await Promise.all(p.tables.map((x) => needInfo(t, x)));
  const loose = p.loose ?? [];
  for (const c of loose) {
    const owner = p.tables.find((x) => columnsOf(t, x).some((k) => k.name.toLowerCase() === c.toLowerCase()));
    if (owner) owner.sel.push(c);
  }
}

bindBoard({ modelOf, record, drawLines, clearCards: () => { if (S.sel.length) { S.sel = []; syncSel(); } }, cards: () => modelOf(tab()).tables,
  info: (m) => { const t = tab(); return t.conn ? A.info.get(tableKey(t.conn, m.database || t.db || '', m.schema, m.name)) : undefined; }, redraw: renderBuilder });
