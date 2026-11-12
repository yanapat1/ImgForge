// The Query workspace: query tabs, the SQL editor and the result. Nothing runs while typing: Run (or Ctrl+Enter) asks the
// server what the text would do, and a statement that is not a plain read waits for the user's confirmation.

import { errText } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog, promptDialog } from '../views/dialog.js';
import { applyExplorer, chip, render, renderTree, saveTabs } from './app.js';
import { B_ACTIONS, fitView, renderBuilder, syncFromSql } from './builder.js';
import { mountGrid } from './grid.js';
import { countAll, countJoin, joinsHtml } from './joins.js';
import { menuHead, openMenu } from './menu.js';
import { A, api, newTab, saveExplorer, sourceOf, tab } from './state.js';
import type { Pane, RunState, Statement, Tab } from './state.js';
import type { EditorHandle, EditorModule } from '../types-editor.js';

const VENDOR_URL = new URL('../vendor/editor.js', import.meta.url).href;
let V: EditorModule | null = null;
let editor: EditorHandle | null = null;
let editorTab = '';

async function vendor(): Promise<EditorModule> {
  if (!V) V = await import(VENDOR_URL) as EditorModule;
  return V;
}

const ranTyped = new Map<string, boolean>();
let lastTab = { i: -1, at: 0 };
export const isRunning = (t: Tab): boolean => t.run?.status === 'running';

function dbOptions(t: Tab): string {
  const dbs = t.conn ? A.dbs.get(t.conn) : undefined;
  const list = dbs ?? (t.db ? [t.db] : []);
  return `<option value="">Default database</option>${list.map((d) => `<option ${d === t.db ? 'selected' : ''}>${esc(d)}</option>`).join('')}`;
}

export function renderQuery(): void {
  const w = $req('an-work');
  const t = tab();
  if (!w.querySelector('.an-q')) {
    w.innerHTML = `<div class="an-q"><div class="an-tabs tabbar sm" role="tablist" id="an-qtabs"></div><div class="an-toolbar toolbar" id="an-qbar"></div>` +
      `<div class="an-qbody" id="an-qbody"><div class="an-split" id="an-pair"><div class="an-pane" data-pane="viz"><div class="an-sqlhead">Visual builder <span class="faint lbl" title="Left-drag: select. Right-drag: move the board. Wheel: zoom. Right-click a line: remove join.">Left-drag: select. Right-drag: move the board. Wheel: zoom. Right-click a line: remove join.</span><span class="grow"></span><button type="button" class="icon-btn ghost xs" data-act="fill" data-k="viz" aria-label="Fill the window" title="Fill the window">⤢</button><button type="button" class="icon-btn ghost xs" data-act="viz-hide" aria-label="Close the visual builder" title="Close the visual builder. Builder on the toolbar brings it back.">×</button></div><div class="an-canvas" id="an-canvas"></div></div>` +
      `<div class="an-spv" data-sp="v" role="separator" aria-orientation="vertical" aria-label="Resize panels" tabindex="0"></div>` +
      `<div class="an-pane" data-pane="sql"><div class="an-sqlhead">SQL <span class="faint lbl" title="Type your query. Ctrl+Enter runs it.">Type your query. Ctrl+Enter runs it.</span><span class="grow"></span><button type="button" class="icon-btn ghost xs" data-act="fill" data-k="sql" aria-label="Fill the window" title="Fill the window">⤢</button><button type="button" class="icon-btn ghost xs" data-act="pane-hide" data-k="sql" aria-label="Close the SQL pane" title="Close the SQL pane. SQL on the toolbar brings it back.">×</button></div><div class="an-sql" id="an-sql"></div></div></div>` +
      `<div class="an-sph" data-sp="h" role="separator" aria-orientation="horizontal" aria-label="Resize panels" tabindex="0"></div>` +
      `<div class="an-pane an-res" data-pane="res"><div class="an-tabs tabbar sm" role="tablist" id="an-rtabs"></div><div class="an-rc" id="an-rc"></div></div></div></div>`;
    editorTab = '';
    editor = null;
  }
  $req('an-qtabs').innerHTML = A.tabs.map((x, i) => `<span class="an-tabw${i === A.active ? ' on' : ''}"><button type="button" class="an-tab ${i === A.active ? 'on' : ''}" role="tab" aria-selected="${i === A.active}" data-act="qtab" data-i="${i}" title="${esc(x.name)} · double-click to rename">${esc(x.name)}</button>${i === A.active ? `<button type="button" class="icon-btn ghost xs" data-act="tab-rename" data-i="${i}" aria-label="Rename ${esc(x.name)}" title="Rename">✎</button><button type="button" class="icon-btn ghost xs" data-act="tab-dup" data-i="${i}" aria-label="Duplicate ${esc(x.name)}" title="Duplicate">⧉</button>` : ''}${A.tabs.length > 1 ? `<button type="button" class="icon-btn ghost xs" data-act="tab-close" data-i="${i}" aria-label="Close ${esc(x.name)}" title="Close">×</button>` : ''}</span>`).join('') +
    `<button type="button" class="an-tab" role="tab" aria-selected="false" data-act="tab-new" aria-label="New query tab">+</button>`;
  const src = sourceOf(t.conn);
  $req('an-qbar').innerHTML = `<button type="button" class="primary" data-act="run"${isRunning(t) ? ' disabled' : ''}>${icon('play')}Run</button><button type="button" data-act="stop"${isRunning(t) ? '' : ' disabled'}>Stop</button>` +
    `<select id="an-conn" aria-label="Connection"><option value="">Choose a connection</option>${A.sources.map((s) => `<option value="${esc(s.id)}" ${s.id === t.conn ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>` +
    `<select id="an-db" aria-label="Database"${t.conn ? '' : ' disabled'}>${dbOptions(t)}</select>` +
    (src ? `${chip(src.kind)}${A.version.get(src.id) ? `<span class="pill faint an-ver" title="${esc(A.version.get(src.id) ?? '')}">${esc(A.version.get(src.id) ?? '')}</span>` : ''}` : '') + `<span class="grow"></span><label class="check" title="The assistant gets at most 20 rows, each cell cut short. Off: it only sees totals."><input type="checkbox" id="an-airows" ${A.aiRows ? 'checked' : ''}> Let AI read sample rows</label><span class="an-lay"><span class="faint">Show</span><span class="an-seg segmented" role="group" aria-label="Panes">${PANES.map(([k, n]) => `<button type="button" class="view-btn an-pbtn${k === 'viz' ? ' an-vizbtn' : ''}" data-act="pane" data-k="${k}">${n}</button>`).join('')}</span><button type="button" class="view-btn" data-act="layout-menu" aria-haspopup="menu" title="Layout presets">Layout ▾</button></span>`;
  void mountEditor(t);
  renderBuilder();
  applyLayout();
  renderResults();
}

export function applyLayout(): void {
  const b = $('an-qbody'), pair = $('an-pair'), L = A.layout;
  if (!b || !pair) return;
  if (L.max && !L[L.max]) L.max = '';
  if (!L.viz && !L.sql && !L.res) L.res = true;                     // never all closed (an old saved layout)
  const vis = (k: Pane): boolean => L[k] && (!L.max || L.max === k);
  const top = vis('viz') || vis('sql');
  b.className = `an-qbody${L.flip ? ' flip' : ''}${top && vis('res') ? '' : ' one'}`;
  b.style.setProperty('--r1', `${L.h}fr`);
  b.style.setProperty('--r2', `${100 - L.h}fr`);
  pair.style.setProperty('--a', `${L.w}fr`);
  pair.style.setProperty('--b', `${100 - L.w}fr`);
  (['sql', 'viz', 'res'] as const).forEach((k) => { const p = document.querySelector<HTMLElement>(`[data-pane="${k}"]`); if (p) p.hidden = !vis(k); });
  pair.classList.toggle('nobuild', !vis('viz'));
  pair.classList.toggle('nosql', !vis('sql'));
  pair.hidden = !top;
  const open = PANES.filter(([k]) => L[k]).length;
  document.querySelectorAll<HTMLElement>('.an-pbtn').forEach((x) => {
    const k = x.dataset['k'] as Pane, on = L[k], name = PANES.find((p) => p[0] === k)?.[1] ?? k;
    x.classList.toggle('active', on);
    x.setAttribute('aria-pressed', String(on));
    (x as HTMLButtonElement).disabled = on && open === 1;
    x.title = on && open === 1 ? 'At least one pane stays open' : `${on ? 'Close' : 'Open'} the ${name} pane`;
    const ready = k === 'res' && !on && !!tab().run;                 // a result waits in the closed pane
    x.classList.toggle('an-ready', ready);
    if (ready) x.title += ' (a result is ready)';
  });
  const pres = $('an-rtabs');
  if (pres && !pres.querySelector('[data-act="fill"]')) pres.insertAdjacentHTML('beforeend', '<button type="button" class="icon-btn ghost xs" data-act="fill" data-k="res" aria-label="Fill the window" title="Fill the window">⤢</button>');
  document.querySelectorAll<HTMLElement>('[data-act="fill"]').forEach((x) => { const on = L.max === x.dataset['k']; x.textContent = on ? '⤡' : '⤢'; x.title = on ? 'Restore layout (Esc)' : 'Fill the window'; });
}

const PANES: [Pane, string][] = [['viz', 'Builder'], ['sql', 'SQL'], ['res', 'Results']];

/** Open or close one pane; the last open one stays. */
export function setPane(k: Pane, on: boolean): void {
  const L = A.layout;
  if (!on && PANES.filter(([p]) => L[p]).length === 1 && L[k]) { toast('At least one pane stays open'); return; }
  L[k] = on;
  if (!on && L.max === k) setMax('');
  saveLayout();
  applyLayout();
  if (on || k !== 'viz') renderBuilderAfterLayout();
}

/** Fill the window with one pane ('' = restore). Filling also closes the Objects panel, and restoring brings it back. */
export function setMax(k: '' | Pane): void {
  const L = A.layout, was = L.max;
  L.max = k;
  if (k && !was) {
    L.expBefore = A.explorer;
    if (A.explorer) { A.explorer = false; applyExplorer(); }
  } else if (!k && was) {
    if (L.expBefore && !A.explorer) { A.explorer = true; applyExplorer(); renderTree(); }
    delete L.expBefore;
  }
  saveExplorer();
  applyLayout();
  renderBuilderAfterLayout();
}


async function mountEditor(t: Tab): Promise<void> {
  const host = $('an-sql');
  if (!host) return;
  if (editorTab === t.id) return;                  // mounted, or on its way: renders can arrive while it loads
  if (editor) { editor.destroy(); editor = null; }
  editorTab = t.id;
  const v = await vendor();
  if (editorTab !== t.id || !$('an-sql')) return;
  host.innerHTML = '';
  const id = t.id;
  const made = await v.createEditor(host, {
    text: t.sql, lang: 'sql',
    onChange: (text) => {
      // by id, not the captured object: the tab list can be rebuilt (a reloaded session) while this editor lives
      const cur = A.tabs.find((x) => x.id === id);
      if (!cur || text === cur.sql) return;
      const wasTyped = cur.typed && cur.dirty;
      cur.sql = text;
      cur.dirty = true;
      cur.typed = true;
      saveTabs();
      markStale();
      if (!wasTyped) renderBuilder();
    },
  });
  if (editorTab === t.id) editor = made; else made.destroy();
}
// only into the open tab's own editor: while a new tab's editor is still loading, the old one must not get its text
document.addEventListener('an-sql-changed', () => { if (editorTab === tab().id) editor?.setText(tab().sql); markStale(); });

/** What the user sees in the editor now (the assistant is sent this, never an older copy). */
export const editorText = (): string => (editor && editorTab === tab().id ? editor.getText() : tab().sql);

function markStale(): void {
  const n = $('an-stale');
  if (n) n.hidden = false;
}

function renderResults(): void {
  const t = tab();
  const res = t.run?.results ?? [];
  $req('an-rtabs').innerHTML = (['results', 'joins', 'cost', 'messages'] as const).map((k) => `<button type="button" class="an-tab ${t.rt === k ? 'on' : ''}" role="tab" aria-selected="${t.rt === k}" data-act="rt" data-k="${k}">${k[0]?.toUpperCase()}${k.slice(1)}</button>`).join('') +
    `<span class="grow"></span><span class="pill" id="an-time">${t.run ? timeText(t.run) : ''}</span>`;
  const rc = $req('an-rc');
  const stale = `<div class="an-hint notice" id="an-stale" ${t.dirty && t.run ? '' : 'hidden'}><span>The query changed. These results are from the last run.</span><button type="button" class="sm primary" data-act="run">Run to update</button></div>`;
  if (t.run?.error) rc.dataset['err'] = '1'; else delete rc.dataset['err'];
  if (t.rt === 'messages') { rc.innerHTML = messages(t); return; }
  if (t.rt === 'cost') { rc.innerHTML = stale + cost(t); return; }
  if (t.rt === 'joins') { rc.innerHTML = stale + joinsHtml(t); return; }
  if (!t.run) { rc.innerHTML = '<div class="an-empty empty">Press Run to see rows here.</div>'; return; }
  if (isRunning(t) && !res.length) { rc.innerHTML = '<div class="an-empty empty">Running…</div>'; return; }
  if (t.run.error && !res.length) { rc.innerHTML = `<div class="an-hint notice bad" role="alert"><span><b>Run failed.</b> ${esc(t.run.error)}</span></div>`; return; }
  if (!res.length) { rc.innerHTML = '<div class="an-empty empty">Done. This statement returned no rows.</div>'; return; }
  const i = Math.min(t.result, res.length - 1), r = res[i];
  if (!r) return;
  rc.innerHTML = stale + (res.length > 1 ? `<div class="an-rtabs tabbar sm" role="tablist" aria-label="Result sets">${res.map((x, n) => `<button type="button" class="an-tab ${n === i ? 'on' : ''}" role="tab" aria-selected="${n === i}" data-act="rset" data-i="${n}">Result ${n + 1} <span class="faint num">${x.rows.toLocaleString()}</span></button>`).join('')}</div>` : '') +
    `<div class="an-sum stat-strip" id="an-sum"><div><b>${r.rows.toLocaleString()}</b>rows${r.truncated ? ' (limit reached)' : ''}</div><div><b>${r.columns.length}</b>columns</div></div>` +
    (t.run.status === 'stopped' ? `<div class="an-hint notice"><span>You stopped this query. These are the ${r.rows.toLocaleString()} rows received so far.</span></div>` : '') +
    (t.run.error ? `<div class="an-hint notice bad" role="alert"><span><b>A later statement failed.</b> ${esc(t.run.error)}</span></div>` : '') + `<div class="an-gridhost" id="an-grid"></div>`;
  mountGrid($req('an-grid'), r);
  void summary(r.id);
}

async function summary(rid: string): Promise<void> {
  try {
    const s = await api.stats(rid);
    const el = $('an-sum');
    if (!el || el.dataset['stats'] === rid) return;
    el.dataset['stats'] = rid;
    const cols = Object.values(s.columns);
    const rows = tab().run?.results.find((r) => r.id === rid)?.rows ?? 0;
    const empty = rows && cols.length ? cols.reduce((a, c) => a + c.nulls, 0) / (rows * cols.length) * 100 : 0;
    el.insertAdjacentHTML('beforeend', `<div><b>${empty.toFixed(1)}%</b>empty cells</div><div><b>${s.duplicates.toLocaleString()}</b>duplicate rows</div>`);
  } catch { /* statistics are extra: the rows are there */ }
}

const timeText = (r: RunState): string => (r.status === 'running' ? `Running ${(r.ms / 1000).toFixed(1)} s` : `Ran in ${(r.ms / 1000).toFixed(2)} s`);

function messages(t: Tab): string {
  if (!t.run) return '<div class="an-empty empty">Nothing has run in this tab yet.</div>';
  const r = t.run, src = sourceOf(t.conn);
  return `<div class="an-msg"><div>Connection: ${esc(src?.name ?? '')}${t.db ? ` / ${esc(t.db)}` : ''}</div><div>Status: ${esc(r.status)}</div><div>Statements: ${r.statement} of ${r.total}</div>${r.results.map((x, i) => `<div>Result ${i + 1}: ${x.rows.toLocaleString()} rows in ${(x.ms / 1000).toFixed(2)} s</div>`).join('')}${r.error ? `<div class="an-err">${esc(r.error)}</div>` : ''}</div>`;
}

function cost(t: Tab): string {
  if (!t.plan) return `<div class="an-empty empty"><p>See which step takes the most work. This asks the database for its plan and does not run the query.</p><button type="button" class="primary" data-act="explain">Show cost</button></div>`;
  const p = t.plan, max = Math.max(...p.nodes.map((n) => n.pct), 1);
  return `<div class="an-pad"><span class="pill">${p.estimated ? 'Estimated cost share' : 'Measured time share'}</span>${[...p.nodes].sort((a, b) => b.pct - a.pct).map((n) => `<div class="an-cost"><span class="lbl" title="${esc(n.label)}">${esc(n.label)}</span><div class="an-bar"><i style="width:${n.pct / max * 100}%"></i></div><b class="num">${n.pct}%</b><span class="faint num">${n.rows !== null ? `${Math.round(n.rows).toLocaleString()} rows` : ''}</span></div>`).join('')}</div>`;
}

function confirmBody(list: Statement[]): string {
  return `<div class="an-confirm"><p class="muted">Read each statement. Nothing runs until you press the button.</p>${list.map((s) => `<div class="an-cs"><span class="badge solid ${s.kind}">${s.kind === 'write' ? 'change' : 'temp'}</span><div><code>${esc(s.sql)}</code>${s.reason ? `<div class="faint">${esc(s.reason)}</div>` : ''}</div></div>`).join('')}</div>`;
}

export async function runQuery(): Promise<void> { await runTab(tab()); }

/** Run a tab's SQL and follow it to the end: the rows land in its Results tab (and a typed query redraws the builder).
 * For the assistant (ai), a write or temp table is refused instead of asking: it never opens the confirm dialog.
 * Returns how the run ended, or null when nothing started. */
export async function runTab(t: Tab, o: { ai?: boolean } = {}): Promise<RunState | null> {
  if (isRunning(t)) { if (o.ai) throw new Error('this tab is still running a query: wait, or use mode new_tab'); return null; }
  const text = t.sql;
  if (!t.conn) { if (o.ai) throw new Error('no connection on this tab'); toast('Choose a connection first', 'err'); return null; }
  if (!text.trim()) { if (o.ai) throw new Error('the query is empty'); toast('The query is empty', 'err'); return null; }
  const chk = await api.check(t.conn, t.db, text);
  if (chk.confirm && o.ai) throw new Error('this is not a plain read: use run_change, and the user confirms it');
  if (chk.confirm) {
    const list = chk.statements.filter((s) => s.kind !== 'read');
    const writes = list.some((s) => s.kind === 'write');
    const ok = await openDialog({ title: writes ? 'Confirm this change' : 'Confirm this temp table', wide: true, okClass: writes ? 'danger-btn' : 'primary', onOk: () => null, ok: writes ? `Run ${list.length} statement${list.length > 1 ? 's' : ''}` : 'Create temp table', body: confirmBody(list) });
    if (!ok) return null;
  }
  t.run = await api.run(t.conn, t.db, text, chk.confirm, t.name);
  t.result = 0;
  t.plan = null;
  const typed = t.typed && t.dirty;
  t.dirty = false;
  ranTyped.set(t.id, typed);
  renderIfActive(t);
  await follow(t);
  return t.run;
}

async function follow(t: Tab): Promise<void> {
  while (t.run && t.run.status === 'running') {
    await new Promise((r) => setTimeout(r, 300));
    const id = t.run.id;
    t.run = await api.runState(id);
    if (tab() === t) { const el = $('an-time'); if (el && t.run) el.textContent = timeText(t.run); }
  }
  if (t.run?.status === 'error') { t.rt = 'messages'; t.dirty = true; toast('Query failed. See Messages.', 'err'); }
  else if (ranTyped.get(t.id) && t.run?.status === 'done') {
    await syncFromSql(t);                       // run first, draw after: the database decided it is valid
    renderIfActive(t);
    if (tab() === t) fitView();
    toast('Query ran. The builder now shows your SQL.');
  }
  renderIfActive(t);
  const beat = await api.heartbeat();
  A.temps = beat.temps;
  renderTree();
}

function renderIfActive(t: Tab): void {
  if (A.ws === 'query' && tab() === t) { renderQuery(); }
}

export const Q_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  ...B_ACTIONS,
  run: () => runQuery(),
  stop: async () => { const t = tab(); if (t.run) t.run = await api.stop(t.run.id); },
  explain: async () => {
    const t = tab();
    if (!t.conn) { toast('Choose a connection first', 'err'); return; }
    t.plan = await api.explain(t.conn, t.db, t.sql);
    renderQuery();
  },
  'join-step': (el) => { tab().joinStep = Number(el.dataset['i']); renderQuery(); },
  'join-count': async (el) => { await countJoin(Number(el.dataset['i'])); renderQuery(); },
  'join-count-all': async () => { await countAll(); renderQuery(); },
  'layout-menu': (el) => {
    const L = A.layout, now = !L.viz && !L.sql ? 'only' : L.flip ? 'top' : 'std';
    const item = (k: string, n: string, hint: string): string => `<button type="button" role="menuitem" data-m="${k}"${k === now ? ' class="on"' : ''}>${icon(k === now ? 'check' : 'grid')}<span class="lbl">${n} <span class="faint">· ${hint}</span></span></button>`;
    openMenu(menuHead('Layout') + item('std', 'Standard', 'builder and SQL above the results') + item('top', 'Table on top', 'results above') + item('only', 'Table only', 'just the results'), el, (k) => {
      L.max = '';
      if (k === 'std') Object.assign(L, { flip: false, h: 46, viz: true, sql: true, res: true });
      if (k === 'top') Object.assign(L, { flip: true, h: 62, viz: true, sql: true, res: true });
      if (k === 'only') Object.assign(L, { viz: false, sql: false, res: true });
      saveLayout();
      applyLayout();
      renderBuilderAfterLayout();
    });
  },
  pane: (el) => { const k = el.dataset['k'] as Pane; setPane(k, !A.layout[k]); },
  'pane-hide': (el) => setPane(el.dataset['k'] as Pane, false),
  'viz-hide': () => setPane('viz', false),
  'viz-toggle': () => setPane('viz', !A.layout.viz),
  fill: (el) => { const k = el.dataset['k'] as Pane; setMax(A.layout.max === k ? '' : k); },
  rt: (el) => { tab().rt = el.dataset['k'] as Tab['rt']; renderQuery(); },
  rset: (el) => { tab().result = Number(el.dataset['i']); renderQuery(); },
  qtab: (el) => {                   // a click rebuilds the tab bar, so a double click is told from two clicks (no dblclick event)
    const i = Number(el.dataset['i']), twice = i === lastTab.i && Date.now() - lastTab.at < 400;
    lastTab = { i, at: twice ? 0 : Date.now() };
    if (twice) return Q_ACTIONS['tab-rename']?.(el);
    if (i === A.active) return;
    A.active = i; renderQuery(); saveTabs();
  },
  'tab-new': () => { const c = tab(); A.tabs.push(newTab(`query ${A.tabs.length + 1}`, '', c.conn, c.db)); A.active = A.tabs.length - 1; renderQuery(); saveTabs(); },
  'tab-dup': (el) => { const c = A.tabs[Number(el.dataset['i'] ?? A.active)] ?? tab(); A.tabs.push(newTab(`${c.name} copy`, c.sql, c.conn, c.db)); A.active = A.tabs.length - 1; renderQuery(); saveTabs(); },
  'tab-rename': async (el) => { const c = A.tabs[Number(el.dataset['i'] ?? A.active)] ?? tab(); const n = await promptDialog({ title: 'Rename query tab', label: 'Name', value: c.name, ok: 'Rename' }); if (n) { c.name = n; renderQuery(); saveTabs(); } },
  'tab-close': async (el) => {
    const i = Number(el.dataset['i']), c = A.tabs[i];
    if (!c || A.tabs.length < 2) return;
    if (c.dirty && !await openDialog({ title: 'Close this tab?', ok: 'Close tab', onOk: () => null, body: '<p>Its query has changes that have not been run.</p>' })) return;
    A.tabs.splice(i, 1);
    if (A.active >= i) A.active = Math.max(0, A.active - 1);
    editorTab = '';
    render();
    saveTabs();
  },
};

function renderBuilderAfterLayout(): void { requestAnimationFrame(() => renderBuilder()); }
export function saveLayout(): void { try { localStorage.setItem('dataviz.analysis.layout', JSON.stringify({ ...A.layout, max: '', expBefore: undefined })); } catch { /* a convenience only */ } }

export function queryChange(t: HTMLInputElement | HTMLSelectElement): boolean {
  const c = tab();
  if (t.id === 'an-conn') { c.conn = t.value || null; c.db = sourceOf(c.conn)?.database || null; if (c.conn && !A.dbs.has(c.conn)) void api.databases(c.conn).then((r) => { A.dbs.set(c.conn as string, r.databases); renderQuery(); }).catch((e: unknown) => toast(errText(e), 'err')); renderQuery(); saveTabs(); return true; }
  if (t.id === 'an-db') { c.db = t.value || null; saveTabs(); return true; }
  if (t.id === 'an-airows') { A.aiRows = (t as HTMLInputElement).checked; saveTabs(); return true; }
  return false;
}
