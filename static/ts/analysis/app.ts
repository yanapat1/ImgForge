// The Data Analysis page (?analysis): the zone header, the icon rail, the object explorer and one workspace at a time.
// Markup is rebuilt per change, listeners are one delegated set on the root. Query lives in query.ts.

import { errText } from '../core/api.js';
import { hasSessions, zoneOn } from '../core/auth.js';
import { $req, esc, icon } from '../core/dom.js';
import { dataUrl, homeUrl } from '../core/nav.js';
import { setTheme, THEME } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { openSettings } from '../settings/dialog.js';
import { mountUserMenu } from '../views/usermenu.js';
import { A_ACTIONS, analyzeChange, renderAnalyze } from './analyze.js';
import { mountDock } from './dock.js';
import { catalogChange, openCatalog, renderCatalog } from './catalog.js';
import { D_ACTIONS, dashDrag, dashDrop, dashInput, renderDash } from './dash.js';
import { renderScripts, S_ACTIONS, scriptInput } from './scripts.js';
import { addTable, builderContext, builderDown, builderKey, builderMove, builderUp, builderWheel, renderBuilder } from './builder.js';
import { gridClick, gridFit, gridPointer } from './grid.js';
import { applyLayout, Q_ACTIONS, queryChange, renderQuery, runQuery, saveLayout } from './query.js';
import { A, api, newTab, tableKey } from './state.js';
import type { Tab, Workspace } from './state.js';

const WS: [Workspace, string, string][] = [
  ['query', 'search', 'Query'], ['catalog', 'db', 'Catalog'], ['scripts', 'text', 'Scripts'], ['analyze', 'bars', 'Analyze'], ['dash', 'grid', 'Dashboards'], ['history', 'list', 'History'],
];
const CHIP: Record<string, string> = { mssql: 'MS', postgres: 'PG', mongo: 'MG' };
export const chip = (kind: string): string => `<span class="an-chip ${esc(kind)}" title="${esc(kind)}">${CHIP[kind] ?? kind.toUpperCase()}</span>`;
const SECTIONS: [string, string][] = [['table', 'Tables'], ['view', 'Views'], ['system', 'System Tables'], ['file', 'FileTables'], ['external', 'External Tables'], ['graph', 'Graph Tables']];

export function render(): void {
  const root = $req('analysis');
  if (!root.querySelector('.an-main')) {
    root.innerHTML = `<header class="an-zonebar"><a class="an-brand" href="${esc(hasSessions() ? homeUrl() : dataUrl())}">Data Platform</a><nav class="an-zones" aria-label="Zones">` +
      (hasSessions() ? `<a href="${esc(homeUrl())}">Sessions</a>` : '') + `<a href="${esc(dataUrl())}">Data Center</a><a class="on" aria-current="page">Data Analysis</a></nav><span class="grow"></span>` +
      `<span class="an-sess" id="an-sess"></span><button type="button" class="icon-btn ghost" data-act="theme" aria-label="Switch light / dark theme">${icon('sun')}</button><button type="button" class="icon-btn ghost" data-act="settings" aria-label="Settings">${icon('gear')}</button><span id="an-user"></span></header>` +
      `<div class="an-main"><nav class="an-rail" id="an-rail" aria-label="Workspaces"></nav><aside class="an-explorer" id="an-explorer" aria-label="Object explorer"></aside><main class="an-work" id="an-work"></main></div>`;
    mountUserMenu($req('an-user'));
  }
  $req('an-sess').textContent = A.name;
  $req('an-rail').innerHTML = WS.map(([k, ic, t], i) => `<button type="button" class="${A.ws === k ? 'on' : ''}" data-act="ws" data-k="${k}" aria-label="${t}" title="${t} · Alt+${i + 1}"${A.ws === k ? ' aria-current="page"' : ''}>${icon(ic)}</button>`).join('') +
    `<span class="grow"></span><button type="button" class="${A.explorer ? 'on' : ''}" data-act="explorer" aria-label="Object explorer" title="${A.explorer ? 'Close' : 'Open'} object explorer">${icon('folder')}</button>`;
  const main = document.querySelector<HTMLElement>('.an-main');
  main?.classList.toggle('noexp', !A.explorer);
  renderTree();
  if (A.ws === 'query') renderQuery();
  else if (A.ws === 'history') void renderHistory();
  else if (A.ws === 'catalog') renderCatalog();
  else if (A.ws === 'scripts') void renderScripts();
  else if (A.ws === 'analyze') void renderAnalyze();
  else if (A.ws === 'dash') void renderDash();
}

// ---------------------------------------------------------------- object explorer
const tw = (k: string): string => `<span class="an-tw">${A.open.has(k) ? '▾' : '▸'}</span>`;
const row = (k: string, label: string, pad: number, extra = ''): string =>
  `<button type="button" class="an-tn" style="padding-left:${pad}px" data-act="tog" data-k="${esc(k)}"${extra}>${tw(k)}${label}</button>`;
const hit = (t: { schema: string; name: string }): boolean => !A.filter || `${t.schema}.${t.name}`.toLowerCase().includes(A.filter);

function renderTree(): void {
  const el = $req('an-explorer');
  let h = '';
  if (!A.sources.length) h = `<p class="an-empty">No database connection yet. Add a PostgreSQL, SQL Server or MongoDB connection in Settings.</p><button type="button" class="an-btn sm" data-act="settings-conn">Add connection</button>`;
  for (const s of A.sources) {
    const k = `c|${s.id}`;
    h += row(k, `${chip(s.kind)}<span class="lbl"><b>${esc(s.name)}</b></span>`, 4);
    if (!A.open.has(k)) continue;
    const dbs = A.dbs.get(s.id);
    if (!dbs) { h += `<div class="an-tload">Loading…</div>`; continue; }
    for (const db of dbs) {
      const dk = `d|${s.id}|${db}`;
      h += row(dk, `<span class="lbl">${esc(db)}</span>`, 18);
      if (!A.open.has(dk)) continue;
      const list = A.tables.get(`${s.id}|${db}`);
      if (!list) { h += `<div class="an-tload">Loading…</div>`; continue; }
      for (const [kind, title] of SECTIONS) {
        const rows = list.filter((t) => t.kind === kind && hit(t));
        if (!rows.length) continue;
        const fk = `f|${s.id}|${db}|${kind}`;
        const shown = A.open.has(fk) || kind === 'table' && !A.open.has(`x${fk}`);
        h += `<button type="button" class="an-tn" style="padding-left:32px" data-act="tog-sec" data-k="${esc(fk)}" data-table="${kind === 'table' ? 1 : 0}"><span class="an-tw">${shown ? '▾' : '▸'}</span><span class="lbl">${title}</span><span class="faint an-n">${rows.length}</span></button>`;
        if (!shown) continue;
        for (const t of rows) h += tableRow(s.id, db, t);
      }
    }
  }
  if (A.temps.length) {
    h += `<div class="an-thead">Temp tables <span class="faint">· removed when you leave</span></div>`;
    h += A.temps.map((t) => `<div class="an-tn temp"><span class="an-chip tp">⏱</span><span class="lbl">${esc(t.name)}</span><span class="faint an-n">${esc(CHIP[t.source] ?? '')}</span></div>`).join('');
  }
  const scroll = el.querySelector('.an-tree')?.scrollTop ?? 0;
  el.innerHTML = `<div class="an-ph">Object explorer<span class="grow"></span><button type="button" class="pb" data-act="refresh-tree" aria-label="Refresh" title="Refresh">⟳</button><button type="button" class="pb" data-act="explorer" aria-label="Close object explorer" title="Close">×</button></div>` +
    `<input class="an-search" id="an-tfind" aria-label="Find a table" placeholder="Find table" value="${esc(A.filter)}"><div class="an-tree">${h}</div>`;
  const t = el.querySelector('.an-tree');
  if (t) t.scrollTop = scroll;
}

function tableRow(conn: string, db: string, t: { schema: string; name: string; kind: string }): string {
  const full = t.schema ? `${t.schema}.${t.name}` : t.name;
  const k = tableKey(conn, db, t.schema, t.name);
  const info = A.info.get(k);
  return `<button type="button" class="an-tn" draggable="true" style="padding-left:46px" data-act="table" data-conn="${esc(conn)}" data-db="${esc(db)}" data-schema="${esc(t.schema)}" data-name="${esc(t.name)}" title="Click for its columns, double-click to query it">${tw(`t|${k}`)}<span class="lbl">${esc(full)}</span></button>` +
    (A.open.has(`t|${k}`) ? (info ? info.columns.map((c) => `<div class="an-tcol" style="padding-left:66px" title="${esc(c.description)}"><span class="lbl">${esc(c.name)}</span>${c.key ? `<span class="an-key">${esc(c.key)}</span>` : ''}<span class="faint an-n">${esc(c.type)}</span></div>`).join('') : '<div class="an-tload">Loading…</div>') : '');
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
    toast(errText(e), 'err');
  }
  renderTree();
}

async function openTable(el: HTMLElement): Promise<void> {
  const { conn = '', db = '', schema = '', name = '' } = el.dataset;
  const key = tableKey(conn, db, schema, name), k = `t|${key}`;
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

// ---------------------------------------------------------------- history
async function renderHistory(): Promise<void> {
  const w = $req('an-work');
  w.innerHTML = '<div class="an-empty">Loading…</div>';
  try { A.history = (await api.history()).history; } catch (e) { w.innerHTML = `<div class="an-empty">${esc(errText(e))}</div>`; return; }
  w.innerHTML = `<div class="an-toolbar"><b>History</b><span class="grow"></span><input class="an-search" id="an-hfind" style="margin:0;width:240px" aria-label="Search history" placeholder="Search name or query"></div>` +
    `<div class="an-scroll"><table class="an-doc" id="an-hist"><thead><tr><th>Time</th><th style="width:100%">Query</th><th>Result</th><th>Took</th><th>Type</th><th></th></tr></thead><tbody>${A.history.map((h, i) => `<tr data-h="${esc(`${h.name} ${h.sql}`.toLowerCase())}"><td class="num">${esc(new Date(h.at).toLocaleTimeString())}</td><td><b>${esc(h.name || 'Query')}</b><pre class="an-hq">${esc(h.sql)}</pre>${h.error ? `<div class="an-err">${esc(h.error)}</div>` : ''}</td><td class="num">${h.status === 'done' ? `${(h.rows ?? 0).toLocaleString()} rows` : esc(h.status)}</td><td class="num">${h.ms !== null ? `${(h.ms / 1000).toFixed(2)} s` : ''}</td><td><span class="an-tag ${esc(h.kind)}">${h.kind === 'write' ? 'change' : esc(h.kind)}</span></td><td><button type="button" class="an-btn sm" data-act="hist-open" data-i="${i}">Open</button></td></tr>`).join('') || '<tr><td colspan="6" class="an-empty">Your queries appear here after they run.</td></tr>'}</tbody></table></div>`;
}

// ---------------------------------------------------------------- saving
let saveTimer: ReturnType<typeof setTimeout> | undefined;
export function saveTabs(): void {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    void api.saveSession({ active: A.active, ai_rows: A.aiRows, tabs: A.tabs.map((t: Tab) => ({ id: t.id, name: t.name, sql: t.sql, conn: t.conn, db: t.db, model: t.model, typed: t.typed })) }).catch(() => {});
  }, 800);
}

let lastTable = { key: '', at: 0 };

const ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  ws: (el) => { A.ws = el.dataset['k'] as Workspace; render(); },
  explorer: () => { A.explorer = !A.explorer; render(); },
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
  ...S_ACTIONS,
  ...A_ACTIONS,
  ...D_ACTIONS,
  ...Q_ACTIONS,
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
    if (t) gridFit(t);
  });
  root.addEventListener('pointerdown', (e) => {
    if (gridPointer(e) || builderDown(e)) return;
    const sp = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-sp]') : null;
    if (!sp) return;
    e.preventDefault();
    const dir = sp.dataset['sp'], box = document.getElementById(dir === 'h' ? 'an-qbody' : 'an-pair')?.getBoundingClientRect();
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
  root.addEventListener('dragstart', (e) => {
    dashDrag(e);
    const el = e.target instanceof HTMLElement ? e.target.closest<HTMLElement>('[data-act="table"]') : null;
    if (el) e.dataTransfer?.setData('text/an-table', JSON.stringify(el.dataset));
  });
  root.addEventListener('dragover', (e) => { if ((e.target as Element).closest?.('#an-canvas, .an-w, .an-well')) e.preventDefault(); });
  root.addEventListener('drop', (e) => {
    if (dashDrop(e)) return;
    const raw = e.dataTransfer?.getData('text/an-table');
    if (!raw || !(e.target as Element).closest?.('#an-canvas')) return;
    e.preventDefault();
    const d = JSON.parse(raw) as Record<string, string>;
    void addTable(d['conn'] ?? '', d['db'] ?? '', d['schema'] ?? '', d['name'] ?? '');
  });
  root.addEventListener('change', (e) => {
    const t = e.target as HTMLInputElement;
    if (queryChange(t) || scriptInput(t) || analyzeChange(t) || dashInput(t)) return;
    catalogChange(t);
  });
  root.addEventListener('input', (e) => {
    const t = e.target as HTMLInputElement;
    if (scriptInput(t) || t.dataset['text'] && dashInput(t)) return;
    if (t.id === 'an-tfind') {
      A.filter = t.value.toLowerCase();
      renderTree();
      const i = $req('an-explorer').querySelector<HTMLInputElement>('#an-tfind');
      i?.focus();
      i?.setSelectionRange(t.value.length, t.value.length);
    }
    if (t.id === 'an-hfind') {
      const q = t.value.toLowerCase();
      document.querySelectorAll<HTMLElement>('#an-hist tbody tr').forEach((r) => { r.hidden = !(r.dataset['h'] ?? '').includes(q); });
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && A.layout.max) { A.layout.max = ''; applyLayout(); return; }
    if (A.ws === 'query' && builderKey(e)) return;
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && A.ws === 'query') { e.preventDefault(); void runQuery().catch((err: unknown) => toast(errText(err), 'err')); return; }
    if (e.altKey && /^[1-9]$/.test(e.key)) { const w = WS[Number(e.key) - 1]; if (w) { e.preventDefault(); A.ws = w[0]; render(); } }
  });
}

export async function start(): Promise<void> {
  const root = $req('analysis');
  if (!zoneOn('analysis')) { root.innerHTML = '<p class="an-empty">This install has no Data Analysis zone.</p>'; return; }
  bind(root);
  try { Object.assign(A.layout, JSON.parse(localStorage.getItem('dataviz.analysis.layout') ?? '{}')); } catch { /* a convenience only */ }
  const [{ sources }, { sessions }] = await Promise.all([api.sources(), api.sessions()]);
  A.sources = sources;
  const wanted = new URLSearchParams(location.search).get('analysis');
  const s = sessions.find((x) => x.id === wanted) ?? sessions[0] ?? await api.newSession('My analysis');
  A.sid = s.id;
  A.name = s.name;
  const st = s.state as { tabs?: Partial<Tab>[]; active?: number };
  A.tabs = (st.tabs ?? []).map((t) => ({ ...newTab(t.name ?? 'Query', t.sql ?? '', t.conn ?? null, t.db ?? null), id: t.id ?? crypto.randomUUID(), model: t.model ?? null, typed: t.typed ?? !!t.sql }));
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
