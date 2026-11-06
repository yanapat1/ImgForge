// The Visual builder. It edits a Query Model; the server turns the model into SQL for the connection's database, so the
// text in the editor always follows the canvas. Editing here never queries the user's database: results wait for Run.
// While typed SQL has not been run the canvas is read-only, so it can never overwrite what the user wrote. After a typed
// query runs, the server parses it and the canvas redraws; what the model cannot hold locks the canvas and says why.
//
// Mouse: left-drag the background selects, right-drag moves the board, wheel zooms, a click on a column selects it, a
// click on a header shows or hides the table, drag a column onto another table to join, right-click a line removes it.

import { errText } from '../core/api.js';
import { $, esc } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import { chip, saveTabs } from './app.js';
import { A, api, sourceOf, tab, tableKey } from './state.js';
import type { Calc, Model, MTable, Tab } from './state.js';

const WINDOW_FNS = ['ROW_NUMBER', 'RANK', 'DENSE_RANK', 'NTILE', 'PERCENT_RANK', 'CUME_DIST', 'LAG', 'LEAD', 'FIRST_VALUE', 'LAST_VALUE', 'NTH_VALUE', 'SUM', 'AVG', 'COUNT', 'MIN', 'MAX', 'STDEV', 'VAR'];
const NO_ARG = new Set(['ROW_NUMBER', 'RANK', 'DENSE_RANK', 'PERCENT_RANK', 'CUME_DIST']);
const TYPES: Record<string, string[]> = {
  mssql: ['int', 'bigint', 'decimal(18,2)', 'float', 'varchar(100)', 'nvarchar(100)', 'date', 'datetime2', 'bit'],
  postgres: ['integer', 'bigint', 'numeric(18,2)', 'double precision', 'text', 'date', 'timestamp', 'boolean'],
  mongo: ['int', 'long', 'decimal', 'double', 'string', 'date', 'bool'],
};
const ORDER = ['INNER', 'LEFT', 'RIGHT', 'FULL'];

const emptyModel = (): Model => ({ tables: [], joins: [], calc: [], parts: {}, locked: [], adv: '' });
export const modelOf = (t: Tab): Model => (t.model ??= emptyModel());
const alias = (m: MTable): string => m.alias || m.id;
const readOnly = (t: Tab): boolean => t.dirty && t.typed || !!modelOf(t).locked.length || t.notDrawn;

function columnsOf(t: Tab, m: MTable): { name: string; type: string; key: string }[] {
  if (m.cols?.length) return m.cols.map((c) => ({ name: c, type: '', key: '' }));
  const info = t.conn ? A.info.get(tableKey(t.conn, m.database || t.db || '', m.schema, m.name)) : undefined;
  if (info) return info.columns;
  return [...m.sel, ...Object.keys(m.cast)].filter((c, i, a) => a.indexOf(c) === i).map((c) => ({ name: c, type: '', key: '' }));
}

async function needInfo(t: Tab, m: MTable): Promise<void> {
  if (!t.conn || m.kind !== 'table' || m.name.startsWith('#')) return;
  const key = tableKey(t.conn, m.database || t.db || '', m.schema, m.name);
  if (A.info.has(key)) return;
  try { A.info.set(key, await api.describe(t.conn, m.database || t.db || '', m.schema, m.name)); } catch { /* the card shows the columns it knows */ }
}

// ---------------------------------------------------------------- undo
const snap = (t: Tab): string => JSON.stringify({ model: modelOf(t), sql: t.sql, typed: t.typed });
function record(t: Tab): void {
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

/** The model changed: the server writes the SQL, the results stay as they are until Run. */
async function changed(t: Tab): Promise<void> {
  const src = sourceOf(t.conn);
  t.typed = false;
  t.dirty = true;
  renderBuilder();
  try {
    t.sql = await api.generate(modelOf(t), src?.kind ?? 'postgres');
    document.dispatchEvent(new CustomEvent('an-sql-changed'));
    saveTabs();
  } catch (e) { toast(errText(e), 'err'); }
}

// ---------------------------------------------------------------- what is on the canvas
const S = { sel: [] as string[], calcOpen: false };

function cvTop(t: Tab): string {
  if (t.dirty && t.typed) return `<div class="an-cvt tl"><span>SQL changed. Press Run to update the builder.</span></div>`;
  const m = modelOf(t);
  if (t.notDrawn) return `<div class="an-cvt tl"><span>Change statements stay in SQL. The builder does not draw them.</span></div>`;
  if (m.locked.length) return `<div class="an-cvt tl"><span class="pill warn">Locked</span><span>This query uses ${esc(m.locked.join(', '))}. It still runs. Edit it in SQL.</span></div>`;
  const n = S.sel.length;
  return `<div class="an-cvt tl"><button type="button" class="an-btn sm" data-act="b-calc">+ Column</button><button type="button" class="an-btn sm" data-act="b-sub">+ Subquery</button><button type="button" class="an-btn sm" data-act="b-filters">Filters</button>` +
    `<button type="button" class="an-btn sm" data-act="b-del" ${n ? '' : 'disabled'} title="Delete key">Delete${n ? ` ${n}` : ''}</button><button type="button" class="an-btn sm" data-act="b-unused" title="Removes tables that are hidden and have no join">Remove unused</button>` +
    `<button type="button" class="an-btn sm" data-act="b-undo" ${t.undo.length ? '' : 'disabled'} title="Ctrl+Z">Undo</button><button type="button" class="an-btn sm" data-act="b-redo" ${t.redo.length ? '' : 'disabled'} title="Ctrl+Y">Redo</button></div>`;
}

function panel(t: Tab): string {
  const m = modelOf(t), p = m.parts, cl = ([['WHERE', p.where], ['GROUP BY', p.group], ['HAVING', p.having], ['ORDER BY', p.order]] as [string, string | undefined][]).filter((x) => x[1]);
  if (!m.calc.length && !cl.length) return '';
  const edit = !readOnly(t);
  return `<div class="an-cvt bl"><button type="button" class="an-cr" data-act="b-panel" aria-expanded="${S.calcOpen}"><b>${S.calcOpen ? '▾' : '▸'} Computed and filters</b><span class="faint num">${m.calc.length + cl.length}</span></button>` +
    (S.calcOpen ? m.calc.map((c, i) => `<div class="an-cr"><span class="an-tag temp">${esc(c.kind.toUpperCase())}</span><span class="lbl" title="${esc(c.expr)}">${esc(c.alias)}</span>${edit ? `<button type="button" class="pb" data-act="b-calc-edit" data-i="${i}" aria-label="Edit ${esc(c.alias)}">✎</button><button type="button" class="pb" data-act="b-calc-rm" data-i="${i}" aria-label="Remove ${esc(c.alias)}">×</button>` : ''}</div>`).join('') +
      cl.map((x) => `<div class="an-cr"><span class="an-tag">${x[0]}</span><span class="lbl" title="${esc(x[1])}">${esc(x[1])}</span></div>`).join('') : '') + '</div>';
}

export function renderBuilder(): void {
  const host = $('an-canvas');
  if (!host) return;
  const t = tab(), m = modelOf(t), v = t.view;
  const cards = m.tables.map((x) => {
    const cols = columnsOf(t, x), on = (c: string): boolean => x.star || x.sel.includes(c);
    return `<div class="an-tb${x.show ? '' : ' off'}${S.sel.includes(x.id) ? ' picked' : ''}${x.kind !== 'table' ? ' sub' : ''}" data-t="${esc(x.id)}" tabindex="0" aria-label="${esc(x.name)} table" style="left:${x.x}px;top:${x.y}px">` +
      `<div class="an-th" data-mv="${esc(x.id)}" title="${x.show ? 'Shown in results. Click to hide.' : 'Hidden from results. Click to show.'}">${chip(x.kind === 'table' ? (sourceOf(t.conn)?.kind ?? '') : 'sub')}<span class="lbl">${esc(x.name)}</span><button type="button" class="an-x" data-act="b-rm" data-id="${esc(x.id)}" aria-label="Remove ${esc(x.name)}">×</button></div>` +
      cols.map((c) => { const cast = x.cast[c.name]; return `<div class="an-col${on(c.name) ? ' sel' : ''}" tabindex="0" role="checkbox" aria-checked="${on(c.name)}" data-t="${esc(x.id)}" data-c="${esc(c.name)}"><span class="an-dot"></span><span class="lbl">${esc(c.name)}</span>${c.key ? `<span class="an-key">${esc(c.key)}</span>` : ''}${x.kind === 'table' ? `<button type="button" class="an-ty${cast ? ' cast' : ''}" data-act="b-cast" data-t="${esc(x.id)}" data-c="${esc(c.name)}" title="Change type with CAST">${esc(cast?.type ?? c.type ?? 'type')}</button>` : ''}</div>`; }).join('') + '</div>';
  }).join('');
  host.classList.toggle('ro', readOnly(t));
  host.innerHTML = cvTop(t) + panel(t) + `<div class="an-cvt br"><button type="button" class="pb" data-act="b-zoom" data-k="-1" aria-label="Zoom out">−</button><span class="num an-z" id="an-zl">${Math.round(v.z * 100)}%</span><button type="button" class="pb" data-act="b-zoom" data-k="1" aria-label="Zoom in">+</button><button type="button" class="an-btn sm" data-act="b-fit">Fit</button></div>` +
    `<div class="an-board" id="an-board" style="transform:translate(${v.x}px,${v.y}px) scale(${v.z})"><svg class="an-lines" id="an-lines" width="2400" height="1600" aria-hidden="true"></svg>${cards}<span id="an-jl"></span>${m.adv ? `<div class="an-remote" style="left:${m.tables[0]?.x ?? 24}px;top:${Math.max(60, ...m.tables.map((x) => x.y)) + 240}px"><span class="pill warn">Locked part</span><pre class="an-hq">${esc(m.adv)}</pre></div>` : ''}</div>` +
    (!m.tables.length && !t.notDrawn ? '<div class="an-cvhint">Drag a table here from the object explorer.</div>' : '');
  host.style.backgroundSize = `${18 * v.z}px ${18 * v.z}px`;
  host.style.backgroundPosition = `${v.x}px ${v.y}px`;
  drawLines();
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
  let p = '', lb = '';
  modelOf(t).joins.forEach((j, i) => {
    const A1 = pt(j.a[0], j.a[1]), B1 = pt(j.b[0], j.b[1]);
    if (!A1 || !B1) return;
    const ltr = A1.l < B1.l, x1 = ltr ? A1.r : A1.l, x2 = ltr ? B1.l : B1.r, dx = Math.max(34, Math.abs(x2 - x1) / 2) * (ltr ? 1 : -1);
    const d = `M${x1} ${A1.y}C${x1 + dx} ${A1.y} ${x2 - dx} ${B1.y} ${x2} ${B1.y}`;
    p += `<path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2"/><path class="an-jhit" data-j="${i}" d="${d}" fill="none" stroke="transparent" stroke-width="14"><title>Right-click to remove this join</title></path>`;
    lb += `<button type="button" class="an-jl" style="left:${(x1 + x2) / 2}px;top:${(A1.y + B1.y) / 2}px" data-act="b-join" data-i="${i}" data-j="${i}" title="Change this join. Right-click removes it.">${esc(j.type)}</button>`;
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
}

function zoomAt(f: number, cx: number, cy: number): void {
  const v = tab().view, z = Math.max(0.3, Math.min(2, v.z * f));
  v.x = cx - (cx - v.x) * z / v.z;
  v.y = cy - (cy - v.y) * z / v.z;
  v.z = z;
  applyView();
}

export function fitView(): void {
  const c = $('an-canvas'), els = [...document.querySelectorAll<HTMLElement>('#an-board .an-tb, #an-board .an-remote')], t = tab();
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
    title: `Join ${a.name} and ${b.name}`, ok: 'Done', body: `<div class="an-form"><label>Join type<select id="an-j-type">${ORDER.map((k) => `<option ${k === j.type ? 'selected' : ''}>${k}</option>`).join('')}</select></label><label>${esc(a.name)} column<select id="an-j-a">${opts(a, j.a[1])}</select></label><label>${esc(b.name)} column<select id="an-j-b">${opts(b, j.b[1])}</select></label><label class="an-chk"><input type="checkbox" id="an-j-rm"> Remove this join</label><p class="faint">Press Run to see the new result.</p></div>`,
    onOk: (root) => {
      record(t);
      if (root.querySelector<HTMLInputElement>('#an-j-rm')?.checked) m.joins.splice(i, 1);
      else {
        j.type = root.querySelector<HTMLSelectElement>('#an-j-type')?.value as typeof j.type;
        j.a[1] = root.querySelector<HTMLSelectElement>('#an-j-a')?.value ?? j.a[1];
        j.b[1] = root.querySelector<HTMLSelectElement>('#an-j-b')?.value ?? j.b[1];
      }
      void changed(t);
      return null;
    },
  });
}

function castDialog(id: string, c: string): void {
  const t = tab(), m = modelOf(t), x = m.tables.find((y) => y.id === id);
  if (!x) return;
  const kind = sourceOf(t.conn)?.kind ?? 'postgres', now = x.cast[c], types = TYPES[kind] ?? [];
  void openDialog({
    title: `Change type of ${c}`, ok: 'Apply', body: `<div class="an-form"><p class="faint">This changes the query only. The table in your database stays the same.</p><label>New type<input id="an-c-type" list="an-c-list" value="${esc(now?.type ?? '')}" placeholder="${esc(types[0] ?? '')}"><datalist id="an-c-list">${types.map((y) => `<option value="${esc(y)}">`).join('')}</datalist></label>${kind === 'mssql' ? `<label class="an-chk"><input type="checkbox" id="an-c-safe" ${now?.safe ? 'checked' : ''}> Use TRY_CAST, so a bad value becomes NULL instead of failing the query</label>` : ''}<label class="an-chk"><input type="checkbox" id="an-c-rm"> Remove this CAST</label></div>`,
    onOk: (root) => {
      const v = root.querySelector<HTMLInputElement>('#an-c-type')?.value.trim() ?? '';
      const rm = root.querySelector<HTMLInputElement>('#an-c-rm')?.checked;
      if (!rm && !/^[A-Za-z][\w ]*(\(\s*(\d+|max)(\s*,\s*\d+)?\s*\))?$/i.test(v)) return 'Write a type such as int or varchar(100).';
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
    if (c.tab === 'case') b = `${c.whens.map((w, i) => `<div class="an-when"><select aria-label="Column" data-w="${i}" data-f="col">${opt(w.col)}</select><select aria-label="Condition" data-w="${i}" data-f="op">${['=', '<>', '>', '>=', '<', '<=', 'LIKE', 'IS NULL'].map((o) => `<option ${o === w.op ? 'selected' : ''}>${o}</option>`).join('')}</select><input aria-label="Value" placeholder="value" data-w="${i}" data-f="val" value="${esc(w.val)}"><input aria-label="Then" placeholder="then" data-w="${i}" data-f="then" value="${esc(w.then)}"></div>`).join('')}<button type="button" class="an-btn sm" data-add="1">+ WHEN</button><label>ELSE<input data-c="els" value="${esc(c.els)}"></label>`;
    if (c.tab === 'window') b = `<label>Function<select data-c="fn" data-re="1">${fns.map((x) => `<option ${x === c.fn ? 'selected' : ''}>${x}</option>`).join('')}</select></label>${NO_ARG.has(c.fn) || c.fn === 'NTILE' ? '' : `<label>Column<select data-c="arg">${opt(c.arg)}</select></label>`}${['NTILE', 'LAG', 'LEAD', 'NTH_VALUE'].includes(c.fn) ? `<label>${c.fn === 'NTILE' ? 'Number of groups' : 'Offset'}<input data-c="n" type="number" min="1" value="${esc(c.n)}"></label>` : ''}<label>Partition by<select data-c="part">${opt(c.part, 'No partition')}</select></label><label>Order by<select data-c="ord">${opt(c.ord, 'No order')}</select></label><label>Direction<select data-c="dir"><option ${c.dir === 'DESC' ? 'selected' : ''}>DESC</option><option ${c.dir === 'ASC' ? 'selected' : ''}>ASC</option></select></label><label>Frame<select data-c="frame">${[['', 'Default'], ['ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW', 'Running, from the first row'], ['ROWS BETWEEN 6 PRECEDING AND CURRENT ROW', 'Last 7 rows'], ['ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING', 'All rows in the partition']].map(([v, n]) => `<option value="${v}" ${v === c.frame ? 'selected' : ''}>${n}</option>`).join('')}</select></label><p class="faint">Anything else, such as PERCENTILE_CONT, goes in the Expression tab.</p>`;
    if (c.tab === 'expr') b = `<label>SQL expression<textarea data-c="expr">${esc(c.expr)}</textarea></label>`;
    return `<div class="an-seg">${([['case', 'CASE WHEN'], ['window', 'Window function'], ['expr', 'Expression']] as const).map(([k, n]) => `<button type="button" class="${c.tab === k ? 'on' : ''}" data-tab="${k}">${n}</button>`).join('')}</div><label>Column name<input id="an-k-alias" data-c="alias" value="${esc(c.alias)}"></label>${b}<pre class="an-prev" id="an-k-prev">${esc(specExpr(c, kind))}</pre>`;
  };
  void openDialog({
    title: `${old ? 'Edit' : 'Add'} computed column`, wide: true, ok: 'Apply', body: `<div class="an-form" id="an-k">${body()}</div>`, focus: '#an-k-alias',
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
    title: 'Add a subquery', ok: 'Add to builder', wide: true, body: '<div class="an-form"><label>Name<input id="an-s-name" value="sub1"></label><label>SELECT query<textarea id="an-s-sql" placeholder="SELECT id, COUNT(*) AS n FROM ..."></textarea></label></div>',
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
    title: 'Filters and grouping', ok: 'Apply', body: `<div class="an-form">${rows.map(([k, n]) => `<label>${n}<input id="an-p-${k}" value="${esc((p as Record<string, unknown>)[k] ?? '')}"></label>`).join('')}<p class="faint">Subqueries work here too, such as id IN (SELECT ...).</p></div>`,
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
  'b-cast': (el) => castDialog(el.dataset['t'] ?? '', el.dataset['c'] ?? ''),
};

// ---------------------------------------------------------------- pointer, keyboard, drops
type Drag =
  | { kind: 'pan'; sx: number; sy: number; ox: number; oy: number; moved: boolean; join: string | null }
  | { kind: 'marq'; sx: number; sy: number; el: HTMLElement; r: DOMRect; add: boolean; moved: boolean }
  | { kind: 'move'; id: string; sx: number; sy: number; moved: boolean; group: { m: MTable; ox: number; oy: number }[] }
  | { kind: 'col'; t: string; c: string; sx: number; sy: number; moved: boolean };
let P: Drag | null = null;
let panEnd = 0;

export function builderDown(e: PointerEvent): boolean {
  const target = e.target instanceof Element ? e.target : null;
  const cv = target?.closest<HTMLElement>('#an-canvas');
  if (!target || !cv) return false;
  if (target.closest('.an-cvt')) return false;
  const t = tab();
  if (e.button === 1 || e.button === 2 || (e.pointerType === 'touch' && !target.closest('.an-tb'))) {
    P = { kind: 'pan', sx: e.clientX, sy: e.clientY, ox: t.view.x, oy: t.view.y, moved: false, join: target.closest<HTMLElement>('[data-j]')?.dataset['j'] ?? null };
    cv.classList.add('panning');
    e.preventDefault();
    return true;
  }
  if (e.button !== 0 || target.closest('.an-x,.an-ty')) return false;
  const head = target.closest<HTMLElement>('.an-th[data-mv]'), col = target.closest<HTMLElement>('.an-col');
  if (head) {
    const id = head.dataset['mv'] ?? '', ids = S.sel.includes(id) ? S.sel : [id];
    const group = ids.flatMap((x) => { const m = modelOf(t).tables.find((y) => y.id === x); return m ? [{ m, ox: m.x, oy: m.y }] : []; });
    P = { kind: 'move', id, sx: e.clientX, sy: e.clientY, moved: false, group };
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
  if (!P) return;
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
  const p = P;
  P = null;
  if (!p) return;
  const t = tab(), m = modelOf(t);
  if (p.kind === 'pan') {
    $('an-canvas')?.classList.remove('panning');
    panEnd = Date.now();
    if (!p.moved && p.join !== null && e.button === 2 && !readOnly(t)) { record(t); m.joins.splice(Number(p.join), 1); void changed(t); toast('Join removed. Press Run to update the result.'); }
    return;
  }
  if (p.kind === 'marq') {
    const r = p.el.getBoundingClientRect();
    p.el.remove();
    const hit = [...document.querySelectorAll<HTMLElement>('#an-board .an-tb')].filter((el) => { const b = el.getBoundingClientRect(); return p.moved && b.left < r.right && b.right > r.left && b.top < r.bottom && b.bottom > r.top; }).map((el) => el.dataset['t'] ?? '');
    S.sel = p.add ? [...new Set([...S.sel, ...hit])] : hit;
    syncSel();
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
    m.joins.push({ a: [p.t, p.c], b: [o.dataset['t'] ?? '', o.dataset['c'] ?? ''], type: 'INNER' });
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
  if (!target || /INPUT|TEXTAREA|SELECT/.test(target.tagName) || target.closest('.cm-editor')) return false;
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (mod && k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); return true; }
  if (mod && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); redo(); return true; }
  const t = tab();
  if ((e.key === 'Delete' || e.key === 'Backspace') && S.sel.length && !readOnly(t)) { e.preventDefault(); void removeTables([...S.sel]); return true; }
  const col = target.closest<HTMLElement>('.an-col');
  if (col && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); toggleColumn(col.dataset['t'] ?? '', col.dataset['c'] ?? ''); return true; }
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
    x.x = o?.x ?? 24 + (i % 3) * 226;
    x.y = o?.y ?? 60 + Math.floor(i / 3) * 190;
  });
  t.model = p as Model;
  await Promise.all(p.tables.map((x) => needInfo(t, x)));
  const loose = p.loose ?? [];
  for (const c of loose) {
    const owner = p.tables.find((x) => columnsOf(t, x).some((k) => k.name.toLowerCase() === c.toLowerCase()));
    if (owner) owner.sel.push(c);
  }
}
