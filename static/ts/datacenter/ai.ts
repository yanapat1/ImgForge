// The Data Center assistant, in the right-side dock (core/dock.ts: #dc-ai is its host section). On top: what the
// assistant sees (bucket, folder, selection) and, folded, which tools run on their own / need your OK (read from
// the agent's catalogue); below it the shared ◆ chat (core/chat.ts, role 'data'). Shown only while an LLM is
// connected and the 'data_center' feature is on. The scope sent with every message is the open bucket, folder and
// selection. A change the agent ran (a change / danger tool_end, an Undo) reloads the folder view, and a format
// change gets its toast.

import { mountChat, type ChatHandle } from '../core/chat.js';
import { dockHooks, openDock, refreshDock, setDockHost, toggleDock } from '../core/dock.js';
import { esc, icon } from '../core/dom.js';
import { llmOn } from '../core/llm.js';
import { openSettings } from '../settings/dialog.js';
import type { ChatEvent } from '../types-chat.js';
import type { Retag } from '../types-datacenter.js';
import type { Action } from './app.js';
import { showRetag } from './ops.js';
import { bucketOf, D, fmtN, hooks } from './state.js';

const A = {
  chat: null as ChatHandle | null,
  ctx: null as HTMLElement | null,
  toolsOpen: false,
  reloadTimer: 0,
};
const enabled = (): boolean => llmOn('data_center');

const SUGGEST = ['Find images without labels in val/', 'Which buckets are not YOLO yet?', 'Tag this folder by camera'];

function emptyHTML(): string {
  return `<div class="dc-ai-hello"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><div><b>Ask about your data, or tell me what to change.</b>` +
    `<p class="faint">I read freely (list, search, stats, detect). In <b>ask</b> mode every change waits for your OK; Shift+Tab switches to auto or plan.</p>` +
    `<div class="dc-ai-sugg">${SUGGEST.map((s) => `<button type="button" class="ct-choice" data-ca="say" data-text="${esc(s)}">${esc(s)}</button>`).join('')}</div></div></div>`;
}

function isRetag(v: unknown): v is Retag {
  return typeof v === 'object' && v !== null && typeof (v as Record<string, unknown>)['from'] === 'string' && typeof (v as Record<string, unknown>)['to'] === 'string';
}

/** A change ran: show a format change, reload the folder view (coalesced: a run of changes reloads once). */
function onChange(ev: ChatEvent): void {
  const args = typeof ev['args'] === 'object' && ev['args'] !== null ? ev['args'] as Record<string, unknown> : {};
  const bid = typeof args['bucket'] === 'string' ? args['bucket'] : D.bid;
  if (bid && isRetag(ev['retagged'])) showRetag(bid, ev['retagged'], typeof ev['undo_id'] === 'string' ? ev['undo_id'] : null, 'The assistant changed this bucket');
  clearTimeout(A.reloadTimer);
  A.reloadTimer = window.setTimeout(() => { void hooks.reload(); }, 250);
}

function mount(el: HTMLElement): ChatHandle {
  el.innerHTML = '<div class="dc-ai-ctx"></div><div class="dc-chat"></div>';
  A.ctx = el.querySelector<HTMLElement>('.dc-ai-ctx');
  const hk = dockHooks();
  A.chat = mountChat(el.querySelector<HTMLElement>('.dc-chat')!, {
    role: 'data',
    inputId: 'dc-ai-q',
    placeholder: 'Ask or tell the assistant…  / for commands',
    scope: () => ({ bucket: D.bid, path: D.path, selection: [...D.sel].slice(0, 500) }),
    sessionScope: () => (D.bid ? { bucket_id: D.bid } : null),
    scopeName: 'bucket',
    scopeLabel: (s) => { const id = typeof s?.['bucket_id'] === 'string' ? s['bucket_id'] : null; const n = s?.['name']; return typeof n === 'string' && n ? n : id ? bucketOf(id)?.name ?? 'bucket' : 'all buckets'; },
    onChange,
    onEvent: hk.onEvent,
    onDone: hk.onDone,
    onUpdate: () => { renderCtx(); refreshDock(); },
    settings: (sec) => { void openSettings(sec === 'llm' ? 'llm' : 'prompts').then(() => { if (sec === 'plugins') document.querySelector<HTMLElement>('[data-act="pr-tab"][data-v="plugins"]')?.click(); }); },
    emptyHTML,
    exportName: () => `data-center-${bucketOf(D.bid)?.name ?? 'chat'}`,
  });
  el.addEventListener('click', (e) => {
    if ((e.target as HTMLElement).closest('[data-dcai="tools"]')) { A.toolsOpen = !A.toolsOpen; renderCtx(); }
  });
  renderCtx();
  return A.chat;
}

// ---------------------------------------------------------------- markup

function scopeHtml(): string {
  const b = bucketOf(D.bid);
  const n = D.listing?.total;
  return (b ? `<span class="chip">${icon('db')}<b>${esc(b.name)}</b></span>` : `<span class="chip">${icon('grid')}<b>All buckets</b></span>`) +
    (b && D.path ? `<span class="chip">${icon('folder')}<b>${esc(D.path)}</b>${n != null ? `<span class="num faint">${fmtN(n)}</span>` : ''}</span>` : '') +
    (D.sel.size ? `<span class="chip">${icon('check')}<b>${fmtN(D.sel.size)} selected</b></span>` : '');
}

function toolsHtml(): string {
  const cat = A.chat?.catalog();
  if (!cat) return '<p class="faint">Loading the tools…</p>';
  const auto = A.chat?.mode() === 'auto';
  const tools = cat.tools.filter((t) => t.risk !== 'control' && t.domain !== 'control');
  const own = tools.filter((t) => t.risk === 'read' || (auto && t.risk === 'change'));
  const ask = tools.filter((t) => !own.includes(t) && (t.risk === 'change' || t.risk === 'danger'));
  const chipT = (t: { name: string; risk: string; description: string }, cls = ''): string => `<span class="tchip${cls}" title="${esc(t.description)}">${esc(t.name)}</span>`;
  return `<h4 class="d-h">Run on their own</h4><div class="toolcat">${own.map((t) => chipT(t)).join('') || '<span class="faint">none</span>'}</div>` +
    `<h4 class="d-h">Need your OK${auto ? ' (auto: only dangerous ones)' : ''}</h4><div class="toolcat">${ask.map((t) => chipT(t, t.risk === 'danger' ? ' danger' : ' mut')).join('') || '<span class="faint">none</span>'}</div>` +
    `<div class="note info">${icon('alert')}<span>The assistant sees file names, folder stats and tags, never pixels.</span></div>`;
}

function renderCtx(): void {
  if (!A.ctx) return;
  A.ctx.innerHTML = `<div class="dc-ai-scope"><span class="faint">Sees</span>${scopeHtml()}<span class="grow"></span>` +
    `<button type="button" class="ghost sm" data-dcai="tools" aria-expanded="${A.toolsOpen}">${icon('list')}Tools${icon(A.toolsOpen ? 'chev-u' : 'chev-d')}</button></div>` +
    (A.toolsOpen ? `<div class="dc-ai-tools">${toolsHtml()}</div>` : '');
}

/** The Data Center's dock host (the chat is built on the dock's first open). */
export function renderAi(): void {
  setDockHost({
    id: 'dc-ai',
    label: () => { const b = bucketOf(D.bid); return b ? `Data Center · ${b.name}` : 'Data Center'; },
    enabled,
    mount,
    onShow: () => renderCtx(),
    settings: () => { void openSettings('llm'); },
  });
}

export const AI_ACTIONS: Record<string, Action> = {
  'ai-toggle': () => toggleDock(),
  'ai-open': () => openDock(),
};

export function bindAi(): void {
  window.addEventListener('llm-state', () => { renderCtx(); refreshDock(); });
}

/** The scope changed (another folder, a new selection): repaint what the assistant sees. */
export function aiScopeChanged(): void { if (enabled()) { renderCtx(); refreshDock(); } }
