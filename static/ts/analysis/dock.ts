// The ◆ assistant on this page: the app's one dock and chat, with the Analysis specialist. It is told what is open, and
// may fill the editor or add charts; running and changing stay with the user's own Run and confirm.

import { mountChat } from '../core/chat.js';
import { dockHooks, refreshDock, setDockHost, withNav } from '../core/dock.js';
import { esc, icon } from '../core/dom.js';
import { llmOn } from '../core/llm.js';
import { openSettings } from '../settings/dialog.js';
import { diffLines, type DiffOp } from '../core/linediff.js';
import { render, saveTabs } from './app.js';
import { currentResult } from './analyze.js';
import { applyBoardOps, boardScope } from './board.js';
import type { BoardOp } from './board.js';
import { addCharts } from './dash.js';
import { editorText } from './query.js';
import { A, newTab, tab } from './state.js';
import type { UiAction, UiResult } from '../types-chat.js';

/** the editor text the assistant is sent with each message (read_editor shows it; longer is cut and says so) */
const EDITOR_CHARS = 20000;
const DIFF_MAX_LINES = 3000, DIFF_SHOW = 400, CONTEXT = 2;

function editorScope(): Record<string, unknown> {
  const t = tab(), sql = editorText();
  return { tab_name: t.name, sql: sql.slice(0, EDITOR_CHARS), sql_truncated: sql.length > EDITOR_CHARS, last_error: t.run?.error ? t.run.error.slice(0, 1000) : null };
}

/** The diff's lines as HTML; long unchanged runs fold to their first and last lines. */
function diffHTML(ops: DiffOp[]): string {
  const out: string[] = [];
  let shown = 0;
  for (let i = 0; i < ops.length && shown < DIFF_SHOW;) {
    const op = ops[i] as DiffOp;
    if (op.t !== ' ') { out.push(`<span class="d-${op.t === '+' ? 'add' : 'del'}">${op.t} ${esc(op.s)}</span>`); i++; shown++; continue; }
    let j = i;
    while (j < ops.length && (ops[j] as DiffOp).t === ' ') j++;
    const run = ops.slice(i, j), keepHead = i === 0 ? 0 : CONTEXT, keepTail = j === ops.length ? 0 : CONTEXT;
    if (run.length > keepHead + keepTail + 1) {
      run.slice(0, keepHead).forEach((o) => out.push(`<span>  ${esc(o.s)}</span>`));
      out.push(`<span class="d-fold">⋯ ${run.length - keepHead - keepTail} unchanged lines</span>`);
      run.slice(run.length - keepTail).forEach((o) => out.push(`<span>  ${esc(o.s)}</span>`));
    } else run.forEach((o) => out.push(`<span>  ${esc(o.s)}</span>`));
    shown += Math.min(run.length, keepHead + keepTail + 1);
    i = j;
  }
  if (shown >= DIFF_SHOW) out.push('<span class="d-fold">⋯ more changes: see SQL</span>');
  return out.join('\n');
}

// the dock re-renders a turn while it streams: the diff of a proposal is kept until the editor text changes
const bodies = new Map<string, { cur: string; html: string }>();

/** set_editor_query's card body: what changes against the editor, the SQL itself, and the other way to apply it. */
function uiBody(a: UiAction, ref: string): string {
  if (a.action !== 'set_editor_query') return '';
  const sql = String(a.args['sql'] ?? ''), newTabMode = a.args['mode'] === 'new_tab';
  const cur = a.applied || newTabMode ? '' : tab().sql;
  const key = `${a.id}|${a.applied ? 1 : 0}|${newTabMode ? 1 : 0}`, hit = bodies.get(key);
  if (hit && hit.cur === cur) return hit.html;
  const lines = sql.split('\n').length;
  let changes = '';
  if (cur.trim()) {
    const a1 = cur.split('\n'), b1 = sql.split('\n');
    const ops = a1.length > DIFF_MAX_LINES || b1.length > DIFF_MAX_LINES ? null : diffLines(a1, b1);
    if (!ops) changes = '<div class="ct-dim ct-uin">Too large to compare: see the SQL.</div>';
    else {
      const add = ops.filter((o) => o.t === '+').length, del = ops.filter((o) => o.t === '-').length;
      changes = add + del === 0 ? '<div class="ct-dim ct-uin">Same as the editor.</div>'
        : `<details class="ct-uid" open><summary>Changes against the editor <span class="d-add">+${add}</span> <span class="d-del">−${del}</span></summary><pre class="ct-diff">${diffHTML(ops)}</pre></details>`;
    }
  }
  const html = `<details class="ct-uid"${changes || a.applied ? '' : ' open'}><summary>SQL · ${lines} line${lines > 1 ? 's' : ''}</summary><pre class="ct-sql">${esc(sql)}</pre></details>${changes}` +
    `<div class="ct-uif">${a.applied ? '' : `<button type="button" class="ghost ct-mini" data-ca="ui-apply" data-mode="${newTabMode ? 'replace' : 'new_tab'}" ${ref}>${newTabMode ? 'Put in this tab instead' : 'Apply in a new tab'}</button>`}` +
    `<button type="button" class="ghost ct-mini" data-ca="ui-copy" data-copy-of="pre.ct-sql">Copy</button></div>`;
  if (bodies.size > 60) bodies.delete(bodies.keys().next().value as string);
  bodies.set(key, { cur, html });
  return html;
}

const SUGGEST = ['What tables do I have about customers?', 'Why is this query slow?', 'Explain the last result in plain words'];

function pageAction(a: UiAction): UiResult {
  if (a.action === 'set_editor_query') {
    const sql = String(a.args['sql'] ?? '');
    if (!sql.trim()) return { ok: false, detail: 'the query is empty' };
    let replaced = 0;
    if (a.args['mode'] === 'new_tab') {
      const cur = tab();
      A.tabs.push(newTab(`AI ${A.tabs.length + 1}`, sql, cur.conn, cur.db));
      A.active = A.tabs.length - 1;
    } else {
      const t = tab();
      replaced = t.sql ? t.sql.split('\n').length : 0;
      t.sql = sql;
      t.dirty = true;
      t.typed = true;
    }
    A.ws = 'query';
    render();
    document.dispatchEvent(new CustomEvent('an-sql-changed'));
    saveTabs();
    return { ok: true, result: a.args['mode'] === 'new_tab' ? { tab: tab().name } : { replaced_lines: replaced, undo: 'Ctrl+Z in the editor' } };
  }
  if (a.action === 'edit_board') {
    if (A.ws !== 'query') { A.ws = 'query'; render(); }
    if (!A.layout.viz) { A.layout.viz = true; render(); }
    const r = applyBoardOps((a.args['ops'] ?? []) as BoardOp[]);
    const parts = [r.added && `added ${r.added}`, r.changed && `changed ${r.changed}`, r.removed && `removed ${r.removed}`].filter(Boolean);
    return { ok: r.added + r.changed + r.removed > 0 || !r.refused.length, result: { done: parts.join(', ') || 'nothing', refused: r.refused.slice(0, 20), undo: 'Ctrl+Z on the board' } };
  }
  if (a.action === 'build_dashboard') {
    addCharts((a.args['charts'] ?? []) as { kind: string; title: string; x?: string; y?: string; agg?: string }[]);
    A.ws = 'dash';
    render();
    return { ok: true };
  }
  return { ok: false, detail: `${a.action} is not available on this page` };
}

export function mountDock(): void {
  // the same markup as the other pages' welcome (views/dock-page.ts): mark | text column; a suggestion is sent as is (data-ca="say")
  const emptyHTML = (): string => `<div class="dc-ai-hello"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><div>` +
    '<b>Ask about your data.</b><p class="faint">I read your tables and statistics. I see real rows only if you allow it on the Query toolbar (Let AI read sample rows). Changes wait for your OK.</p>' +
    `<div class="dc-ai-sugg">${SUGGEST.map((s) => `<button type="button" class="ct-choice" data-ca="say" data-text="${esc(s)}">${esc(s)}</button>`).join('')}</div></div></div>`;
  setDockHost({
    id: 'ai-analysis',
    label: () => 'Data Analysis',
    enabled: () => llmOn('data_center'),
    mount: (el) => {
      const hk = dockHooks();
      return mountChat(el, {
        role: 'analysis',
        inputId: 'ai-analysis-q',
        placeholder: 'Ask about your data…  / for commands',
        scope: () => ({ page: 'analysis', session: A.sid, connection: tab().conn, database: tab().db, result: currentResult()?.id ?? null, rows_ok: A.aiRows, ...editorScope(),
          board: A.ws === 'query' && A.layout.viz ? boardScope() : null }),
        onUiAction: withNav(pageAction),
        uiBody,
        onEvent: hk.onEvent,
        onDone: hk.onDone,
        onUpdate: () => refreshDock(),
        settings: (sec) => { void openSettings(sec === 'llm' ? 'llm' : 'prompts'); },
        emptyHTML,
        exportName: () => 'analysis-chat',
      });
    },
    settings: () => { void openSettings('llm'); },
  });
}
