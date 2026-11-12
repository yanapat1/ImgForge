// The Data page (?data[=<session>]): the zone header, the icon rail, the object explorer and one workspace at a time.
// Markup is rebuilt per change, listeners are one delegated set on the root. Query lives in query.ts.

import { uuid } from '../core/uuid.js';
import { errText, isStatus } from '../core/api.js';
import { isAdmin, zoneOn } from '../core/auth.js';
import { openDialog } from '../views/dialog.js';
import { $req, esc, icon } from '../core/dom.js';
import { setTheme, THEME } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { openSettings } from '../settings/dialog.js';
import { mountUserMenu } from '../views/usermenu.js';
import { appHeader } from '../core/header.js';
import { A_ACTIONS, analyzeChange, renderAnalyze } from './analyze.js';
import { mountDock } from './dock.js';
import { N_ACTIONS, noteEdit, noteInput } from './board.js';
import { catalogChange, catalogKey, openCatalog, renderCatalog } from './catalog.js';
import { D_ACTIONS, dashDrag, dashDrop, dashInput, renderDash } from './dash.js';
import { renderScripts, S_ACTIONS, scriptInput } from './scripts.js';
import { addTable, builderContext, builderDown, builderKey, builderMove, builderUp, builderWheel, renderBuilder } from './builder.js';
import { gridClick, gridFit, gridPointer } from './grid.js';
import { applyLayout, Q_ACTIONS, queryChange, renderQuery, runQuery, saveLayout, setMax } from './query.js';
import { aiBadge, loadAiAccess } from './aiaccess.js';
import { F_ACTIONS, favDrop, favMenu, isFav, loadFavs, renderFavs } from './favorites.js';
import { A, api, clampExp, EXP_MIN, EXP_W, expMax, newTab, saveExplorer, tableKey } from './state.js';
import type { Tab, Workspace } from './state.js';

const WS: [Workspace, string, string][] = [
  ['query', 'search', 'Query'], ['catalog', 'db', 'Catalog'], ['scripts', 'text', 'Scripts'], ['analyze', 'bars', 'Analyze'], ['dash', 'grid', 'Dashboards'], ['history', 'list', 'History'],
];
const CHIP: Record<string, string> = { mssql: 'MS', postgres: 'PG', mongo: 'MG' };
const TONE: Record<string, string> = { mssql: 'accent', postgres: 'ok', mongo: 'alt' };
/** The source of a table or tab in a few letters (kit .badge): readable without its colour. */
export const chip = (kind: string): string => `<span class="badge ${TONE[kind] ?? ''}" title="${esc(kind)}">${CHIP[kind] ?? kind.toUpperCase()}</span>`;
const SECTIONS: [string, string][] = [['table', 'Tables'], ['view', 'Views'], ['system', 'System Tables'], ['file', 'FileTables'], ['external', 'External Tables'], ['graph', 'Graph Tables']];

export function render(): void {
  const root = $req('data');
  if (!root.querySelector('.dt-main')) {
    // the same header as every page (core/header.ts); the session name sits after the links, like the visualizer's dataset
    root.innerHTML = appHeader({ page: 'data', cls: 'dt-header', actionsId: 'dt-hact' }).replace('<span class="grow"></span>', '<div class="ds-info"><span class="pill name"><b id="dt-sess"></b></span></div>') +
      `<div class="dt-main"><nav class="dt-rail activity-bar" id="dt-rail" aria-label="Workspaces"></nav><aside class="dt-explorer left-panel" id="dt-explorer" aria-label="Object explorer"></aside><main class="dt-work" id="dt-work"></main>` +
      `<button type="button" class="dt-expopen" id="dt-expopen" data-act="explorer" aria-label="Open the Objects panel (Ctrl+B)" title="Open the Objects panel (Ctrl+B)">${icon('chev-r')}<span class="dt-expopen-lbl">${icon('layers')}<span>Objects</span></span></button>` +
      `<div class="dt-expsz" data-sp="e" role="separator" aria-orientation="vertical" aria-controls="dt-explorer" aria-label="Objects panel width" aria-valuemin="${EXP_MIN}" tabindex="0" title="Drag to resize · double-click: fit the longest name"></div></div>`;
    mountUserMenu($req('dt-hact'));
  }
  $req('dt-sess').textContent = A.name;
  $req('dt-rail').innerHTML = WS.map(([k, ic, t], i) => `<button type="button" class="act${A.ws === k ? ' active' : ''}" data-act="ws" data-k="${k}" aria-label="${t}" title="${t} · Alt+${i + 1}"${A.ws === k ? ' aria-current="page"' : ''}>${icon(ic)}</button>`).join('') +
    `<button type="button" class="act dt-rail-end" data-act="explorer" aria-label="Objects panel">${icon('layers')}</button>`;
  applyExplorer();
  const main = document.querySelector<HTMLElement>('.dt-main');
  main?.style.setProperty('--dt-exp', `${clampExp(A.layout.exp)}px`);
  const sz = document.querySelector('.dt-expsz');
  sz?.setAttribute('aria-valuenow', String(clampExp(A.layout.exp)));
  sz?.setAttribute('aria-valuemax', String(expMax()));
  renderTree();
  if (A.ws === 'query') renderQuery();
  else if (A.ws === 'history') void renderHistory();
  else if (A.ws === 'catalog') renderCatalog();
  else if (A.ws === 'scripts') void renderScripts();
  else if (A.ws === 'analyze') void renderAnalyze();
  else if (A.ws === 'dash') void renderDash();
}

// ---------------------------------------------------------------- object explorer
const tw = (k: string): string => `<span class="dt-tw">${A.open.has(k) ? '▾' : '▸'}</span>`;
const row = (k: string, label: string, pad: number, extra = ''): string =>
  `<button type="button" class="dt-tn" style="padding-left:${pad}px" data-act="tog" data-k="${esc(k)}"${extra}>${tw(k)}${label}</button>`;
const hit = (t: { schema: string; name: string }): boolean => !A.filter || `${t.schema}.${t.name}`.toLowerCase().includes(A.filter);

function renderTree(): void {
  const el = $req('dt-explorer');
  let h = renderFavs(hit);
  if (!A.sources.length) h = `<p class="dt-empty empty">No database connection yet. Add a PostgreSQL, SQL Server or MongoDB connection in Settings.</p><button type="button" class="sm" data-act="settings-conn">Add connection</button>`;
  for (const s of A.sources) {
    const k = `c|${s.id}`;
    h += row(k, `${chip(s.kind)}<span class="lbl"><b>${esc(s.name)}</b></span>${s.example ? '<span class="pill faint dt-ex" title="An example database to practise on: query it, change it, drop it">example</span>' : ''}`, 4);
    if (!A.open.has(k)) continue;
    const dbs = A.dbs.get(s.id);
    if (!dbs) { h += `<div class="dt-tload">Loading…</div>`; continue; }
    for (const db of dbs) {
      const dk = `d|${s.id}|${db}`;
      h += row(dk, `<span class="lbl">${esc(db)}</span>`, 18);
      if (!A.open.has(dk)) continue;
      const list = A.tables.get(`${s.id}|${db}`);
      if (!list) { h += `<div class="dt-tload">Loading…</div>`; continue; }
      for (const [kind, title] of SECTIONS) {
        const rows = list.filter((t) => t.kind === kind && hit(t));
        if (!rows.length) continue;
        const fk = `f|${s.id}|${db}|${kind}`;
        const shown = A.open.has(fk) || kind === 'table' && !A.open.has(`x${fk}`);
        h += `<button type="button" class="dt-tn" style="padding-left:32px" data-act="tog-sec" data-k="${esc(fk)}" data-table="${kind === 'table' ? 1 : 0}"><span class="dt-tw">${shown ? '▾' : '▸'}</span><span class="lbl">${title}</span><span class="faint dt-n">${rows.length}</span></button>`;
        if (!shown) continue;
        for (const t of rows) h += tableRow(s.id, db, t);
      }
    }
  }
  if (A.temps.length) {
    h += `<div class="dt-thead">Temp tables <span class="faint">· removed when you leave</span></div>`;
    h += A.temps.map((t) => `<div class="dt-tn temp"><span class="badge warn dashed">⏱</span><span class="lbl">${esc(t.name)}</span><span class="faint dt-n">${esc(CHIP[t.source] ?? '')}</span></div>`).join('');
  }
  const scroll = el.querySelector('.dt-tree')?.scrollTop ?? 0;
  el.innerHTML = `<div class="side-head"><h2>Objects</h2><span class="grow"></span><button type="button" class="icon-btn ghost xs" data-act="refresh-tree" aria-label="Refresh" title="Refresh">${icon('refresh')}</button><button type="button" class="icon-btn ghost xs" data-act="explorer" aria-label="Fold the Objects panel" title="Fold away (Ctrl+B)">${icon('chev-l')}</button></div>` +
    `<div class="dt-tfind"><input class="txt" id="dt-tfind" aria-label="Find a table" placeholder="Find table or collection" value="${esc(A.filter)}"></div><div class="dt-tree">${h}</div>`;
  const t = el.querySelector('.dt-tree');
  if (t) t.scrollTop = scroll;
}

/** A table of the tree, or of Favorites (`pre` = its own open key for the columns, `label` = its own label). */
export function tableRow(conn: string, db: string, t: { schema: string; name: string }, pad = 46, pre = 't', label?: string): string {
  const full = t.schema ? `${t.schema}.${t.name}` : t.name;
  const k = tableKey(conn, db, t.schema, t.name), ok = `${pre}|${k}`;
  const info = A.info.get(k), on = isFav(k);
  const data = `data-conn="${esc(conn)}" data-db="${esc(db)}" data-schema="${esc(t.schema)}" data-name="${esc(t.name)}"`;
  return `<div class="dt-trow"><button type="button" class="dt-tn" draggable="true" style="padding-left:${pad}px" data-act="table" ${data} data-pre="${pre}" title="${esc(db ? `${db} · ${full}` : full)}&#10;Click for its columns, double-click to query it, right-click for favorites and AI access">${tw(ok)}${label ?? `<span class="lbl">${esc(full)}</span>`}${aiBadge({ c: conn, d: db, s: t.schema, t: t.name })}</button>` +
    `<button type="button" class="icon-btn ghost xs dt-star${on ? ' on' : ''}" data-act="fav" ${data} aria-pressed="${on}" aria-label="${on ? 'Remove from' : 'Add to'} favorites: ${esc(full)}" title="${on ? 'Remove from' : 'Add to'} favorites">${icon(on ? 'star-on' : 'star')}</button></div>` +
    (A.open.has(ok) ? (info ? info.columns.map((c) => `<div class="dt-tcol" style="padding-left:${pad + 20}px" title="${esc(c.description)}"><span class="lbl">${esc(c.name)}</span>${c.key ? `<span class="dt-key">${esc(c.key)}</span>` : ''}<span class="faint dt-n">${esc(c.type)}</span></div>`).join('') : '<div class="dt-tload">Loading…</div>') : '');
}

async function toggle(k: string): Promise<void> {
  if (A.open.has(k)) { A.open.delete(k); render(); return; }
  A.open.add(k);
  renderTree();
  const [kind, conn = '', db = ''] = k.split('|');
  try {
    if (kind === 'c' && !A.dbs.has(conn)) {
      const r = await api.databases(conn);
      A.dbs.set(conn, r.databases);
      A.version.set(conn, r.version);
    } else if (kind === 'd' && !A.tables.has(`${conn}|${db}`)) {
      A.tables.set(`${conn}|${db}`, (await api.tables(conn, db)).tables);
    }
  } catch (e) {
    A.open.delete(k);
    if (isStatus(e, 410)) void sampleGone(conn); else toast(errText(e), 'err');
  }
  renderTree();
}

/** The sample database was dropped: an admin can make it again here; anyone can remove the connection in Settings. */
async function sampleGone(conn: string): Promise<void> {
  const admin = isAdmin();
  const ok = await openDialog({
    title: 'The sample database was removed', ok: admin ? 'Restore sample database' : 'OK', onOk: () => null,
    body: `<p>Someone dropped <code>sample_shop</code>. ${admin ? 'Restore it to get back the first day\'s tables and rows (it is shared by everyone on this server).' : 'An admin can restore it.'}</p><p class="faint">Not needed any more? Remove the connection in Settings › Connections.</p>`,
  });
  if (!ok || !admin) return;
  try {
    await api.restoreSample();
    A.dbs.delete(conn);
    [...A.tables.keys()].filter((x) => x.startsWith(`${conn}|`)).forEach((x) => A.tables.delete(x));
    toast('The sample database is back');
    await toggle(`c|${conn}`);
  } catch (e) { toast(errText(e), 'err'); }
}

/** The first tab of a user's first session: on the example database, with a query to run. */
const WELCOME_SQL = `-- Welcome! sample_shop is an example database you can query, change and delete.
-- Ctrl+Enter runs the query. Drag tables from Objects into the Visual builder.
SELECT o.id, c.name, o.ordered_at, o.status, o.total
FROM orders o JOIN customers c ON c.id = o.customer_id
ORDER BY o.ordered_at DESC;

-- Remove one table:      DROP TABLE order_items;
-- Remove the whole database (pick database "postgres" above first):
--   DROP DATABASE sample_shop WITH (FORCE);
`;

async function openTable(el: HTMLElement): Promise<void> {
  const { conn = '', db = '', schema = '', name = '', pre = 't' } = el.dataset;
  const key = tableKey(conn, db, schema, name), k = `${pre}|${key}`;
  if (A.open.has(k)) { A.open.delete(k); renderTree(); return; }
  A.open.add(k);
  renderTree();
  if (A.info.has(key)) return;
  try { A.info.set(key, await api.describe(conn, db, schema, name)); } catch (e) { A.open.delete(k); toast(errText(e), 'err'); }
  renderTree();
}

export function queryTable(el: HTMLElement): void {
  const { conn = null, db = null, schema = '', name = '' } = el.dataset;
  const src = A.sources.find((s) => s.id === conn);
  const q = (n: string): string => (src?.kind === 'mssql' ? `[${n}]` : `"${n}"`);
  const full = schema ? `${q(schema)}.${q(name)}` : q(name);
  const sql = src?.kind === 'mongo' ? `db.getCollection("${name}").find({}).limit(1000)`
    : src?.kind === 'mssql' ? `SELECT TOP (1000) *\nFROM ${full};` : `SELECT *\nFROM ${full}\nLIMIT 1000;`;
  A.tabs.push(newTab(name, sql, conn, db));
  A.active = A.tabs.length - 1;
  A.ws = 'query';
  render();
  saveTabs();
}

// ---------------------------------------------------------------- the Objects panel's width
/** Set the width now (a CSS variable on .dt-main: no re-render), clamped. */
function setExplorer(px: number): void {
  A.layout.exp = clampExp(px);
  document.querySelector<HTMLElement>('.dt-main')?.style.setProperty('--dt-exp', `${A.layout.exp}px`);
  document.querySelector('.dt-expsz')?.setAttribute('aria-valuenow', String(A.layout.exp));
}

function dragExplorer(e: PointerEvent, handle: HTMLElement): void {
  const x0 = e.clientX, w0 = A.layout.exp;
  handle.classList.add('on');
  document.documentElement.classList.add('dt-resizing');
  let raf = 0, x = x0;
  const move = (m: PointerEvent): void => {
    x = m.clientX;
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; setExplorer(w0 + x - x0); });     // one layout per frame
  };
  const up = (): void => {
    cancelAnimationFrame(raf);
    const w = w0 + x - x0;
    if (w < EXP_MIN - 40) { setExplorer(w0); setExplorerOpen(false); } else setExplorer(w);   // dragged shut: closes, keeps its width
    handle.classList.remove('on');
    document.documentElement.classList.remove('dt-resizing');
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', up);
    saveLayout();
    if (A.ws === 'query') renderBuilder();
  };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', up);
}

/** Widen (or narrow) the panel to the longest name shown: one read pass over the labels, then one write. */
function fitExplorer(): void {
  let over = -Infinity;
  for (const l of document.querySelectorAll<HTMLElement>('#dt-explorer .dt-tree .lbl')) over = Math.max(over, l.scrollWidth - l.clientWidth);
  if (over === -Infinity) return;
  setExplorer(A.layout.exp + over + 8);
  saveLayout();
  if (A.ws === 'query') renderBuilder();
}

// ---------------------------------------------------------------- history
async function renderHistory(): Promise<void> {
  const w = $req('dt-work');
  w.innerHTML = '<div class="dt-empty empty">Loading…</div>';
  try { A.history = (await api.history()).history; } catch (e) { w.innerHTML = `<div class="dt-empty empty">${esc(errText(e))}</div>`; return; }
  w.innerHTML = `<div class="dt-toolbar toolbar"><b>History</b><span class="grow"></span><input class="txt" id="dt-hfind" aria-label="Search history" placeholder="Search name or query"></div>` +
    `<div class="dt-scroll"><table class="dt-doc list-table" id="dt-hist"><thead><tr><th>Time</th><th style="width:100%">Query</th><th>Result</th><th>Took</th><th>Type</th><th></th></tr></thead><tbody>${A.history.map((h, i) => `<tr data-h="${esc(`${h.name} ${h.sql}`.toLowerCase())}"><td class="num nowrap">${esc(new Date(h.at).toLocaleTimeString())}</td><td><b>${esc(h.name || 'Query')}</b><pre class="dt-hq">${esc(h.sql)}</pre>${h.error ? `<div class="dt-err">${esc(h.error)}</div>` : ''}</td><td class="num nowrap">${h.status === 'done' ? `${(h.rows ?? 0).toLocaleString()} rows` : esc(h.status)}</td><td class="num nowrap">${h.ms !== null ? `${(h.ms / 1000).toFixed(2)} s` : ''}</td><td><span class="badge solid ${esc(h.kind)}">${h.kind === 'write' ? 'change' : esc(h.kind)}</span></td><td><button type="button" class="sm" data-act="hist-open" data-i="${i}">Open</button></td></tr>`).join('') || '<tr><td colspan="6" class="dt-empty empty">Your queries appear here after they run.</td></tr>'}</tbody></table></div>`;
}

// ---------------------------------------------------------------- saving
let saveTimer: ReturnType<typeof setTimeout> | undefined;
export function saveTabs(): void {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    void api.saveSession({ active: A.active, ai_rows: A.aiRows, tabs: A.tabs.map((t: Tab) => ({ id: t.id, name: t.name, sql: t.sql, conn: t.conn, db: t.db, model: t.model, typed: t.typed })) }).catch(() => {});
  }, 800);
}

/** Show or hide the Objects panel as A.explorer says, without rebuilding the page. */
export function applyExplorer(): void {
  document.querySelector('.dt-main')?.classList.toggle('noexp', !A.explorer);
  const b = document.querySelector<HTMLElement>('.dt-rail-end');
  if (b) {
    b.classList.toggle('active', A.explorer);
    b.setAttribute('aria-pressed', String(A.explorer));
    b.title = `${A.explorer ? 'Close' : 'Open'} the Objects panel (Ctrl+B)`;
  }
}

/** Open or close the Objects panel (the rail button, its fold button, the folded strip, Ctrl+B); remembered in this browser. */
export function setExplorerOpen(on: boolean): void {
  if (A.explorer === on) return;
  A.explorer = on;
  delete A.layout.expBefore;             // the user chose: Fill the window's restore must not undo it
  saveExplorer();
  applyExplorer();
  if (on) renderTree();
  if (A.ws === 'query') renderBuilder();  // the board's width changed
}

let lastTable = { key: '', at: 0 };

const ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  ws: (el) => { A.ws = el.dataset['k'] as Workspace; render(); },
  explorer: () => setExplorerOpen(!A.explorer),
  tog: (el) => toggle(el.dataset['k'] ?? ''),
  'tog-sec': (el) => {
    const k = el.dataset['k'] ?? '';
    if (el.dataset['table'] === '1') { if (A.open.has(`x${k}`)) A.open.delete(`x${k}`); else A.open.add(`x${k}`); } else if (A.open.has(k)) A.open.delete(k); else A.open.add(k);
    renderTree();
  },
  table: (el) => {                  // the first click rebuilds the tree, so a double click is told from the two clicks
    const key = `${el.dataset['conn']}|${el.dataset['db']}|${el.dataset['schema']}|${el.dataset['name']}`;
    const twice = key === lastTable.key && Date.now() - lastTable.at < 400;
    lastTable = { key, at: twice ? 0 : Date.now() };
    if (A.ws === 'catalog') return openCatalog(el.dataset['conn'] ?? '', el.dataset['db'] ?? '', el.dataset['schema'] ?? '', el.dataset['name'] ?? '');
    return twice ? queryTable(el) : openTable(el);
  },
  'refresh-tree': async () => {
    A.dbs.clear(); A.tables.clear(); A.info.clear();
    A.sources = (await api.sources()).sources;
    const open = [...A.open].filter((k) => k.startsWith('c|') || k.startsWith('d|'));
    open.forEach((k) => A.open.delete(k));
    render();
    for (const k of open) await toggle(k);
  },
  theme: () => { setTheme(THEME.name === 'dark' ? 'light' : 'dark'); },
  settings: () => openSettings('connections'),
  'settings-conn': () => openSettings('connections'),
  'hist-open': (el) => {
    const h = A.history[Number(el.dataset['i'])];
    if (!h) return;
    A.tabs.push(newTab(h.name || 'Query', h.sql, h.connection, h.database));
    A.active = A.tabs.length - 1;
    A.ws = 'query';
    render();
    saveTabs();
  },
  'cat-query': () => {
    const c = A.catalog;
    if (c) queryTable({ dataset: { conn: c.conn, db: c.db, schema: c.schema, name: c.table } } as unknown as HTMLElement);
  },
  ...F_ACTIONS,
  ...S_ACTIONS,
  ...A_ACTIONS,
  ...D_ACTIONS,
  ...Q_ACTIONS,
  ...N_ACTIONS,
};

function bind(root: HTMLElement): void {
  root.addEventListener('click', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    if (gridClick(t)) return;
    const el = t.closest<HTMLElement>('[data-act]');
    const fn = el ? ACTIONS[el.dataset['act'] ?? ''] : undefined;
    if (el && fn) Promise.resolve(fn(el)).catch((err: unknown) => toast(errText(err), 'err'));
  });
  root.addEventListener('dblclick', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (t?.closest('#dt-board') && noteEdit(t)) return;
    if (t?.closest('.dt-expsz')) { fitExplorer(); return; }
    // double-click on a pane's header (not on its buttons): that pane fills the window, again: back
    const head = t && !t.closest('button, input, select') ? t.closest<HTMLElement>('.dt-sqlhead, #dt-rtabs') : null;
    const pane = head?.closest<HTMLElement>('[data-pane]')?.dataset['pane'] as 'viz' | 'sql' | 'res' | undefined;
    if (pane) { setMax(A.layout.max === pane ? '' : pane); return; }
    if (t) gridFit(t);
  });
  root.addEventListener('pointerdown', (e) => {
    if (gridPointer(e) || builderDown(e)) return;
    const sp = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-sp]') : null;
    if (!sp) return;
    e.preventDefault();
    if (sp.dataset['sp'] === 'e') { dragExplorer(e, sp); return; }
    const dir = sp.dataset['sp'], box = document.getElementById(dir === 'h' ? 'dt-qbody' : 'dt-pair')?.getBoundingClientRect();
    if (!box) return;
    const move = (m: PointerEvent): void => {
      const v = dir === 'h' ? (m.clientY - box.top) / box.height * 100 : (m.clientX - box.left) / box.width * 100;
      if (dir === 'h') A.layout.h = Math.round(Math.max(15, Math.min(85, v))); else A.layout.w = Math.round(Math.max(15, Math.min(85, v)));
      applyLayout();
    };
    const up = (): void => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', up); saveLayout(); renderBuilder(); };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  });
  document.addEventListener('pointermove', builderMove);
  document.addEventListener('pointerup', builderUp);
  document.addEventListener('contextmenu', builderContext);
  root.addEventListener('wheel', builderWheel, { passive: false });
  // right-click (or the context-menu key) on a table of the Objects panel: its favorites menu
  root.addEventListener('contextmenu', (e) => {
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>('#dt-explorer [data-act="table"], #dt-explorer .dt-fmiss .dt-star') : null;
    if (!el) return;
    e.preventDefault();
    favMenu(el, e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : undefined);
  });
  root.addEventListener('dragstart', (e) => {
    dashDrag(e);
    const el = e.target instanceof HTMLElement ? e.target.closest<HTMLElement>('[data-act="table"]') : null;
    if (el) e.dataTransfer?.setData('text/dt-table', JSON.stringify(el.dataset));
  });
  const dropAt = (e: DragEvent): HTMLElement | null =>
    (e.dataTransfer?.types.includes('text/dt-table') && e.target instanceof Element ? e.target.closest<HTMLElement>('.dt-fdrop') : null);
  const unmark = (): void => { document.querySelectorAll('.dt-fdrop.over').forEach((x) => x.classList.remove('over')); };
  root.addEventListener('dragover', (e) => {
    const fd = dropAt(e);
    if (fd) { e.preventDefault(); if (!fd.classList.contains('over')) { unmark(); fd.classList.add('over'); } return; }
    unmark();
    if ((e.target as Element).closest?.('#dt-canvas, .dt-w, .dt-well')) e.preventDefault();
  });
  root.addEventListener('dragend', unmark);
  root.addEventListener('drop', (e) => {
    unmark();
    if (dashDrop(e)) return;
    const raw = e.dataTransfer?.getData('text/dt-table');
    const fd = dropAt(e);
    if (raw && fd) { e.preventDefault(); favDrop(JSON.parse(raw) as Record<string, string>, fd.dataset['g'] ?? ''); return; }
    if (!raw || !(e.target as Element).closest?.('#dt-canvas')) return;
    e.preventDefault();
    const d = JSON.parse(raw) as Record<string, string>;
    void addTable(d['conn'] ?? '', d['db'] ?? '', d['schema'] ?? '', d['name'] ?? '');
  });
  root.addEventListener('change', (e) => {
    const t = e.target as HTMLInputElement;
    if (noteInput(t, true) || queryChange(t) || scriptInput(t) || analyzeChange(t) || dashInput(t)) return;
    catalogChange(t);
  });
  root.addEventListener('input', (e) => {
    const t = e.target as HTMLInputElement;
    if (noteInput(t, false) || scriptInput(t) || t.dataset['text'] && dashInput(t)) return;
    if (t.id === 'dt-tfind') {
      A.filter = t.value.toLowerCase();
      renderTree();
      const i = $req('dt-explorer').querySelector<HTMLInputElement>('#dt-tfind');
      i?.focus();
      i?.setSelectionRange(t.value.length, t.value.length);
    }
    if (t.id === 'dt-hfind') {
      const q = t.value.toLowerCase();
      document.querySelectorAll<HTMLElement>('#dt-hist tbody tr').forEach((r) => { r.hidden = !(r.dataset['h'] ?? '').includes(q); });
    }
  });
  // Ctrl+Enter runs the query: caught on the way down, before the SQL editor adds a new line for it
  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.key !== 'Enter' || A.ws !== 'query' || document.documentElement.dataset['screen'] !== 'data') return;
    if (document.querySelector('.ui-dlg-wrap')) return;               // a dialog's own Enter
    if (e.target instanceof Element && e.target.closest('.dt-note')) return;   // Ctrl+Enter ends a note's text
    e.preventDefault();
    e.stopPropagation();
    void runQuery().catch((err: unknown) => toast(errText(err), 'err'));
  }, true);
  root.addEventListener('keydown', (e) => {          // a join's numbered dot in the Joins picture (an SVG <g>)
    if ((e.key !== 'Enter' && e.key !== ' ') || !(e.target instanceof SVGElement) || !e.target.classList.contains('dt-vj')) return;
    e.preventDefault();
    e.target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  root.addEventListener('keydown', (e) => {
    if (!(e.target instanceof HTMLElement) || !e.target.classList.contains('dt-expsz')) return;
    const d = ({ ArrowLeft: -20, ArrowRight: 20 } as Record<string, number>)[e.key];
    if (d) setExplorer(A.layout.exp + (e.shiftKey ? d * 4 : d));
    else if (e.key === 'Home') setExplorer(EXP_W);
    else if (e.key === 'Enter') fitExplorer();
    else return;
    e.preventDefault();
    saveLayout();
  });
  document.addEventListener('keydown', (e) => {
    if (A.ws === 'catalog' && catalogKey(e)) return;
    if (e.key === 'Escape' && A.layout.max) { setMax(''); return; }
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b' && document.documentElement.dataset['screen'] === 'data' &&
        !(e.target instanceof HTMLElement && e.target.isContentEditable && !e.target.closest('.cm-editor'))) {
      e.preventDefault();
      setExplorerOpen(!A.explorer);
      return;
    }
    if (A.ws === 'query' && builderKey(e)) return;
    if (e.altKey && /^[1-9]$/.test(e.key)) { const w = WS[Number(e.key) - 1]; if (w) { e.preventDefault(); A.ws = w[0]; render(); } }
  });
}

export async function start(): Promise<void> {
  const root = $req('data');
  document.title = 'Data · DataViz';
  if (!zoneOn('data')) { root.innerHTML = '<p class="dt-empty empty">This install has no Data zone.</p>'; return; }
  bind(root);
  try { Object.assign(A.layout, JSON.parse(localStorage.getItem('dataviz.data.layout') ?? '{}')); } catch { /* a convenience only */ }
  const [{ sources }, { sessions }] = await Promise.all([api.sources(), api.sessions()]);
  A.sources = sources;
  loadFavs();
  void loadAiAccess().then(() => { if (document.getElementById('dt-explorer')) renderTree(); });     // the ✎ / ⚙ marks
  const wanted = new URLSearchParams(location.search).get('data');
  const first = !sessions.length;
  const s = sessions.find((x) => x.id === wanted) ?? sessions[0] ?? await api.newSession('My workspace');
  A.sid = s.id;
  A.name = s.name;
  const st = s.state as { tabs?: Partial<Tab>[]; active?: number };
  A.tabs = (st.tabs ?? []).map((t) => ({ ...newTab(t.name ?? 'Query', t.sql ?? '', t.conn ?? null, t.db ?? null), id: t.id ?? uuid(), model: t.model ?? null, typed: t.typed ?? !!t.sql }));
  const ex = A.sources.find((x) => x.example);
  if (!A.tabs.length && first && ex) A.tabs = [newTab('Welcome', WELCOME_SQL, ex.id, ex.database || null)];
  if (!A.tabs.length) A.tabs = [newTab('query 1', '', A.sources[0]?.id ?? null, A.sources[0]?.database || null)];
  A.active = Math.min(st.active ?? 0, A.tabs.length - 1);
  A.aiRows = !!(s.state as { ai_rows?: boolean }).ai_rows;
  render();
  const beat = (): void => { void api.heartbeat().then((r) => {
    const changed = JSON.stringify(r.temps) !== JSON.stringify(A.temps);
    A.temps = r.temps;
    if (changed) renderTree();
  }).catch(() => {}); };
  beat();
  setInterval(beat, 60000);
  mountDock();
}
export { renderTree };
