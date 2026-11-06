// The Query workspace: query tabs, the SQL editor and the result. Nothing runs while typing: Run (or Ctrl+Enter) asks the
// server what the text would do, and a statement that is not a plain read waits for the user's confirmation.

import { errText } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog, promptDialog } from '../views/dialog.js';
import { chip, render, renderTree, saveTabs } from './app.js';
import { B_ACTIONS, fitView, renderBuilder, syncFromSql } from './builder.js';
import { mountGrid } from './grid.js';
import { A, api, newTab, sourceOf, tab } from './state.js';
import type { RunState, Statement, Tab } from './state.js';
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
const isRunning = (t: Tab): boolean => t.run?.status === 'running';

function dbOptions(t: Tab): string {
  const dbs = t.conn ? A.dbs.get(t.conn) : undefined;
  const list = dbs ?? (t.db ? [t.db] : []);
  return `<option value="">Default database</option>${list.map((d) => `<option ${d === t.db ? 'selected' : ''}>${esc(d)}</option>`).join('')}`;
}

export function renderQuery(): void {
  const w = $req('an-work');
  const t = tab();
  if (!w.querySelector('.an-q')) {
    w.innerHTML = `<div class="an-q"><div class="an-tabs" id="an-qtabs"></div><div class="an-toolbar" id="an-qbar"></div>` +
      `<div class="an-qbody" id="an-qbody"><div class="an-split" id="an-pair"><div class="an-pane" data-pane="sql"><div class="an-sqlhead">SQL <span class="faint">Type your query. Ctrl+Enter runs it.</span><span class="grow"></span><button type="button" class="pb" data-act="fill" data-k="sql" aria-label="Fill the window" title="Fill the window">⤢</button></div><div class="an-sql" id="an-sql"></div></div>` +
      `<div class="an-spv" data-sp="v" role="separator" aria-orientation="vertical" aria-label="Resize panels" tabindex="0"></div>` +
      `<div class="an-pane" data-pane="viz"><div class="an-sqlhead">Visual builder <span class="faint lbl">Left-drag: select. Right-drag: move the board. Wheel: zoom. Right-click a line: remove join.</span><span class="grow"></span><button type="button" class="pb" data-act="fill" data-k="viz" aria-label="Fill the window" title="Fill the window">⤢</button></div><div class="an-canvas" id="an-canvas"></div></div></div>` +
      `<div class="an-sph" data-sp="h" role="separator" aria-orientation="horizontal" aria-label="Resize panels" tabindex="0"></div>` +
      `<div class="an-pane an-res" data-pane="res"><div class="an-tabs" id="an-rtabs"></div><div class="an-rc" id="an-rc"></div></div></div></div>`;
    editorTab = '';
    editor = null;
  }
  $req('an-qtabs').innerHTML = A.tabs.map((x, i) => `<span class="an-tabw"><button type="button" class="an-tab ${i === A.active ? 'on' : ''}" data-act="qtab" data-i="${i}">${esc(x.name)}</button>${i === A.active ? `<button type="button" class="pb" data-act="tab-rename" aria-label="Rename ${esc(x.name)}" title="Rename">✎</button><button type="button" class="pb" data-act="tab-dup" aria-label="Duplicate ${esc(x.name)}" title="Duplicate">⧉</button>` : ''}${A.tabs.length > 1 ? `<button type="button" class="pb" data-act="tab-close" data-i="${i}" aria-label="Close ${esc(x.name)}" title="Close">×</button>` : ''}</span>`).join('') +
    `<button type="button" class="an-tab" data-act="tab-new" aria-label="New query tab">+</button>`;
  const src = sourceOf(t.conn);
  $req('an-qbar').innerHTML = `<button type="button" class="an-btn pri" data-act="run"${isRunning(t) ? ' disabled' : ''}>${icon('play')}Run</button><button type="button" class="an-btn" data-act="stop"${isRunning(t) ? '' : ' disabled'}>Stop</button>` +
    `<select class="an-sel" id="an-conn" aria-label="Connection"><option value="">Choose a connection</option>${A.sources.map((s) => `<option value="${esc(s.id)}" ${s.id === t.conn ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>` +
    `<select class="an-sel" id="an-db" aria-label="Database"${t.conn ? '' : ' disabled'}>${dbOptions(t)}</select>` +
    (src ? `${chip(src.kind)}<span class="pill faint">${esc(A.version.get(src.id) ?? '')}</span>` : '') + `<span class="grow"></span><span class="faint" style="font-size:12.5px">Layout</span><span class="an-seg">${[['std', 'Standard'], ['top', 'Table on top'], ['only', 'Table only']].map(([k, n]) => `<button type="button" data-act="layout" data-lay="${k}" data-k="${k}">${n}</button>`).join('')}</span>`;
  void mountEditor(t);
  renderBuilder();
  applyLayout();
  renderResults();
}

export function applyLayout(): void {
  const b = $('an-qbody'), pair = $('an-pair'), L = A.layout;
  if (!b || !pair) return;
  b.className = `an-qbody${L.flip ? ' flip' : ''}${L.max ? ' one' : ''}`;
  b.style.setProperty('--r1', `${L.h}fr`);
  b.style.setProperty('--r2', `${100 - L.h}fr`);
  pair.style.setProperty('--a', `${L.w}fr`);
  pair.style.setProperty('--b', `${100 - L.w}fr`);
  document.querySelector('.an-main')?.classList.toggle('filled', !!L.max);
  ['sql', 'viz', 'res'].forEach((k) => { const p = document.querySelector<HTMLElement>(`[data-pane="${k}"]`); if (p) p.hidden = !!L.max && L.max !== k; });
  pair.hidden = L.max === 'res';
  const pres = $('an-rtabs');
  if (pres && !pres.querySelector('[data-act="fill"]')) pres.insertAdjacentHTML('beforeend', '<button type="button" class="pb" data-act="fill" data-k="res" aria-label="Fill the window" title="Fill the window">⤢</button>');
  document.querySelectorAll<HTMLElement>('[data-act="fill"]').forEach((x) => { const on = L.max === x.dataset['k']; x.textContent = on ? '⤡' : '⤢'; x.title = on ? 'Restore layout (Esc)' : 'Fill the window'; });
  document.querySelectorAll<HTMLElement>('[data-lay]').forEach((x) => x.classList.toggle('on', x.dataset['lay'] === layoutName()));
}

const layoutName = (): string => (A.layout.max === 'res' ? 'only' : A.layout.flip ? 'top' : A.layout.max ? '' : 'std');

async function mountEditor(t: Tab): Promise<void> {
  const host = $('an-sql');
  if (!host) return;
  if (editorTab === t.id) return;                  // mounted, or on its way: renders can arrive while it loads
  if (editor) { editor.destroy(); editor = null; }
  editorTab = t.id;
  const v = await vendor();
  if (editorTab !== t.id || !$('an-sql')) return;
  host.innerHTML = '';
  const made = await v.createEditor(host, {
    text: t.sql, lang: 'sql',
    onChange: (text) => {
      if (text === t.sql) return;
      const wasTyped = t.typed && t.dirty;
      t.sql = text;
      t.dirty = true;
      t.typed = true;
      saveTabs();
      markStale();
      if (!wasTyped) renderBuilder();
    },
  });
  if (editorTab === t.id) editor = made; else made.destroy();
}
document.addEventListener('an-sql-changed', () => { editor?.setText(tab().sql); markStale(); });

function markStale(): void {
  const n = $('an-stale');
  if (n) n.hidden = false;
}

function renderResults(): void {
  const t = tab();
  const res = t.run?.results ?? [];
  $req('an-rtabs').innerHTML = (['results', 'cost', 'messages'] as const).map((k) => `<button type="button" class="an-tab ${t.rt === k ? 'on' : ''}" data-act="rt" data-k="${k}">${k[0]?.toUpperCase()}${k.slice(1)}</button>`).join('') +
    `<span class="grow"></span><span class="pill" id="an-time">${t.run ? timeText(t.run) : ''}</span>`;
  const rc = $req('an-rc');
  const stale = `<div class="an-hint" id="an-stale" ${t.dirty && t.run ? '' : 'hidden'}><span>The query changed. These results are from the last run.</span><button type="button" class="an-btn sm pri" data-act="run">Run to update</button></div>`;
  if (t.run?.error) rc.dataset['err'] = '1'; else delete rc.dataset['err'];
  if (t.rt === 'messages') { rc.innerHTML = messages(t); return; }
  if (t.rt === 'cost') { rc.innerHTML = stale + cost(t); return; }
  if (!t.run) { rc.innerHTML = '<div class="an-empty">Press Run to see rows here.</div>'; return; }
  if (isRunning(t) && !res.length) { rc.innerHTML = '<div class="an-empty">Running…</div>'; return; }
  if (t.run.error && !res.length) { rc.innerHTML = `<div class="an-hint bad" role="alert"><span><b>Run failed.</b> ${esc(t.run.error)}</span></div>`; return; }
  if (!res.length) { rc.innerHTML = '<div class="an-empty">Done. This statement returned no rows.</div>'; return; }
  const i = Math.min(t.result, res.length - 1), r = res[i];
  if (!r) return;
  rc.innerHTML = stale + (res.length > 1 ? `<div class="an-rtabs" role="tablist" aria-label="Result sets">${res.map((x, n) => `<button type="button" class="an-tab ${n === i ? 'on' : ''}" role="tab" aria-selected="${n === i}" data-act="rset" data-i="${n}">Result ${n + 1} <span class="faint num">${x.rows.toLocaleString()}</span></button>`).join('')}</div>` : '') +
    `<div class="an-sum" id="an-sum"><div><b>${r.rows.toLocaleString()}</b>rows${r.truncated ? ' (limit reached)' : ''}</div><div><b>${r.columns.length}</b>columns</div></div>` +
    (t.run.status === 'stopped' ? `<div class="an-hint"><span>You stopped this query. These are the ${r.rows.toLocaleString()} rows received so far.</span></div>` : '') +
    (t.run.error ? `<div class="an-hint bad" role="alert"><span><b>A later statement failed.</b> ${esc(t.run.error)}</span></div>` : '') + `<div class="an-gridhost" id="an-grid"></div>`;
  mountGrid($req('an-grid'), r);
  void summary(r.id);
}

async function summary(rid: string): Promise<void> {
  try {
    const s = await api.stats(rid);
    const el = $('an-sum');
    if (!el) return;
    const cols = Object.values(s.columns);
    const rows = tab().run?.results.find((r) => r.id === rid)?.rows ?? 0;
    const empty = rows && cols.length ? cols.reduce((a, c) => a + c.nulls, 0) / (rows * cols.length) * 100 : 0;
    el.insertAdjacentHTML('beforeend', `<div><b>${empty.toFixed(1)}%</b>empty cells</div><div><b>${s.duplicates.toLocaleString()}</b>duplicate rows</div>`);
  } catch { /* statistics are extra: the rows are there */ }
}

const timeText = (r: RunState): string => (r.status === 'running' ? `Running ${(r.ms / 1000).toFixed(1)} s` : `Ran in ${(r.ms / 1000).toFixed(2)} s`);

function messages(t: Tab): string {
  if (!t.run) return '<div class="an-empty">Nothing has run in this tab yet.</div>';
  const r = t.run, src = sourceOf(t.conn);
  return `<div class="an-msg"><div>Connection: ${esc(src?.name ?? '')}${t.db ? ` / ${esc(t.db)}` : ''}</div><div>Status: ${esc(r.status)}</div><div>Statements: ${r.statement} of ${r.total}</div>${r.results.map((x, i) => `<div>Result ${i + 1}: ${x.rows.toLocaleString()} rows in ${(x.ms / 1000).toFixed(2)} s</div>`).join('')}${r.error ? `<div class="an-err">${esc(r.error)}</div>` : ''}</div>`;
}

function cost(t: Tab): string {
  if (!t.plan) return `<div class="an-empty"><p>See which step takes the most work. This asks the database for its plan and does not run the query.</p><button type="button" class="an-btn pri" data-act="explain">Show cost</button></div>`;
  const p = t.plan, max = Math.max(...p.nodes.map((n) => n.pct), 1);
  return `<div class="an-pad"><span class="pill">${p.estimated ? 'Estimated cost share' : 'Measured time share'}</span>${[...p.nodes].sort((a, b) => b.pct - a.pct).map((n) => `<div class="an-cost"><span class="lbl" title="${esc(n.label)}">${esc(n.label)}</span><div class="an-bar"><i style="width:${n.pct / max * 100}%"></i></div><b class="num">${n.pct}%</b><span class="faint num">${n.rows !== null ? `${Math.round(n.rows).toLocaleString()} rows` : ''}</span></div>`).join('')}</div>`;
}

function confirmBody(list: Statement[]): string {
  return `<div class="an-confirm"><p class="muted">Read each statement. Nothing runs until you press the button.</p>${list.map((s) => `<div class="an-cs"><span class="an-tag ${s.kind}">${s.kind === 'write' ? 'change' : 'temp'}</span><div><code>${esc(s.sql)}</code>${s.reason ? `<div class="faint">${esc(s.reason)}</div>` : ''}</div></div>`).join('')}</div>`;
}

export async function runQuery(): Promise<void> {
  const t = tab();
  if (isRunning(t)) return;
  const text = t.sql;
  if (!t.conn) { toast('Choose a connection first', 'err'); return; }
  if (!text.trim()) { toast('The query is empty', 'err'); return; }
  const chk = await api.check(t.conn, t.db, text);
  if (chk.confirm) {
    const list = chk.statements.filter((s) => s.kind !== 'read');
    const writes = list.some((s) => s.kind === 'write');
    const ok = await openDialog({ title: writes ? 'Confirm this change' : 'Confirm this temp table', wide: true, okClass: writes ? 'danger-btn' : 'primary', onOk: () => null, ok: writes ? `Run ${list.length} statement${list.length > 1 ? 's' : ''}` : 'Create temp table', body: confirmBody(list) });
    if (!ok) return;
  }
  t.run = await api.run(t.conn, t.db, text, chk.confirm, t.name);
  t.result = 0;
  t.plan = null;
  const typed = t.typed && t.dirty;
  t.dirty = false;
  ranTyped.set(t.id, typed);
  renderIfActive(t);
  await follow(t);
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
  layout: (el) => {
    const L = A.layout, k = el.dataset['k'];
    L.max = k === 'only' ? 'res' : '';
    if (k === 'std') { L.flip = false; L.h = 46; }
    if (k === 'top') { L.flip = true; L.h = 62; }
    saveLayout();
    applyLayout();
    renderBuilderAfterLayout();
  },
  fill: (el) => { const k = el.dataset['k'] as 'sql' | 'viz' | 'res'; A.layout.max = A.layout.max === k ? '' : k; applyLayout(); renderBuilderAfterLayout(); },
  rt: (el) => { tab().rt = el.dataset['k'] as Tab['rt']; renderQuery(); },
  rset: (el) => { tab().result = Number(el.dataset['i']); renderQuery(); },
  qtab: (el) => { A.active = Number(el.dataset['i']); renderQuery(); saveTabs(); },
  'tab-new': () => { const c = tab(); A.tabs.push(newTab(`query ${A.tabs.length + 1}`, '', c.conn, c.db)); A.active = A.tabs.length - 1; renderQuery(); saveTabs(); },
  'tab-dup': () => { const c = tab(); A.tabs.push(newTab(`${c.name} copy`, c.sql, c.conn, c.db)); A.active = A.tabs.length - 1; renderQuery(); saveTabs(); },
  'tab-rename': async () => { const c = tab(); const n = await promptDialog({ title: 'Rename query tab', label: 'Name', value: c.name, ok: 'Rename' }); if (n) { c.name = n; renderQuery(); saveTabs(); } },
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
export function saveLayout(): void { try { localStorage.setItem('dataviz.analysis.layout', JSON.stringify({ ...A.layout, max: '' })); } catch { /* a convenience only */ } }

export function queryChange(t: HTMLInputElement | HTMLSelectElement): boolean {
  const c = tab();
  if (t.id === 'an-conn') { c.conn = t.value || null; c.db = sourceOf(c.conn)?.database || null; if (c.conn && !A.dbs.has(c.conn)) void api.databases(c.conn).then((r) => { A.dbs.set(c.conn as string, r.databases); renderQuery(); }).catch((e: unknown) => toast(errText(e), 'err')); renderQuery(); saveTabs(); return true; }
  if (t.id === 'an-db') { c.db = t.value || null; saveTabs(); return true; }
  return false;
}
