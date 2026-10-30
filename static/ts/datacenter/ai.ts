// The Data Center assistant: a drawer docked under the centre and right panes (collapsed: a one-line prompt bar;
// expanded: the chat + a side column with the scope and which tools run on their own / need your OK, read from the
// agent's catalogue). Shown only while an LLM is connected and the 'data_center' feature is on (data-llm,
// core/llm.ts). The transcript, composer, History and the trace are the shared ◆ chat (core/chat.ts, role 'data'),
// mounted once and kept across re-renders: only the bar and the side column are redrawn. The scope sent with
// every message is the open bucket, folder and selection. A change the agent ran (a change / danger tool_end, an
// Undo) reloads the folder view, and a format change gets its toast.

import { pref, rememberPref } from '../core/auth.js';
import { mountChat, type ChatHandle } from '../core/chat.js';
import { $, esc, icon } from '../core/dom.js';
import { applyLLM, llmOn } from '../core/llm.js';
import { openSettings } from '../settings/dialog.js';
import type { ChatEvent } from '../types-chat.js';
import type { Retag } from '../types-datacenter.js';
import type { Action } from './app.js';
import { showRetag } from './ops.js';
import { bucketOf, D, fmtN, hooks, PREF } from './state.js';

const A = {
  open: false,
  draft: '',
  chat: null as ChatHandle | null,
  node: null as HTMLElement | null,
  built: '' as '' | 'bar' | 'open',
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

function chat(): ChatHandle {
  if (A.chat) return A.chat;
  const node = document.createElement('div');
  node.className = 'dc-chat';
  A.node = node;
  A.chat = mountChat(node, {
    role: 'data',
    inputId: 'dc-ai-q',
    placeholder: 'Ask or tell the assistant…  / for commands',
    scope: () => ({ bucket: D.bid, path: D.path, selection: [...D.sel].slice(0, 500) }),
    sessionScope: () => (D.bid ? { bucket_id: D.bid } : null),
    scopeName: 'bucket',
    scopeLabel: (s) => { const id = typeof s?.['bucket_id'] === 'string' ? s['bucket_id'] : null; const n = s?.['name']; return typeof n === 'string' && n ? n : id ? bucketOf(id)?.name ?? 'bucket' : 'all buckets'; },
    onChange,
    onUpdate: () => renderSide(),
    settings: (sec) => { void openSettings(sec === 'llm' ? 'llm' : 'prompts').then(() => { if (sec === 'plugins') document.querySelector<HTMLElement>('[data-act="pr-tab"][data-v="plugins"]')?.click(); }); },
    emptyHTML,
    exportName: () => `data-center-${bucketOf(D.bid)?.name ?? 'chat'}`,
  });
  return A.chat;
}

// ---------------------------------------------------------------- markup

function scopeHtml(): string {
  const b = bucketOf(D.bid);
  const n = D.listing?.total;
  return `<div class="scope">${b ? `<div class="s">${icon('db')}<span>${esc(b.name)}</span><span class="num">bucket</span></div>` : `<div class="s">${icon('grid')}<span>All buckets</span></div>`}` +
    (b && D.path ? `<div class="s">${icon('folder')}<span>${esc(D.path)}</span><span class="num">${n != null ? fmtN(n) : ''}</span></div>` : '') +
    (D.sel.size ? `<div class="s">${icon('check')}<span>${fmtN(D.sel.size)} selected</span></div>` : '') + '</div>';
}

function toolsHtml(): string {
  const cat = A.chat?.catalog();
  if (!cat) return '<p class="faint">Loading the tools…</p>';
  const auto = A.chat?.mode() === 'auto';
  const tools = cat.tools.filter((t) => t.risk !== 'control' && t.domain !== 'control');
  const own = tools.filter((t) => t.risk === 'read' || (auto && t.risk === 'change'));
  const ask = tools.filter((t) => !own.includes(t) && (t.risk === 'change' || t.risk === 'danger'));
  const chipT = (t: { name: string; risk: string; description: string }, cls = ''): string => `<span class="tchip${cls}" title="${esc(t.description)}">${esc(t.name)}</span>`;
  return `<h4 class="d-h">Tools · run on their own</h4><div class="toolcat">${own.map((t) => chipT(t)).join('') || '<span class="faint">none</span>'}</div>` +
    `<h4 class="d-h">Tools · need your OK${auto ? ' (auto: only dangerous ones)' : ''}</h4><div class="toolcat">${ask.map((t) => chipT(t, t.risk === 'danger' ? ' danger' : ' mut')).join('') || '<span class="faint">none</span>'}</div>`;
}

function renderSide(): void {
  const side = $('dc-ai')?.querySelector('.dr-side');
  if (!side || !A.chat) return;
  const n = A.chat.pending();
  side.innerHTML = `<h4 class="d-h">Scope</h4>${scopeHtml()}${toolsHtml()}` +
    `<div class="note info">${icon('alert')}<span>The assistant sees file names, folder stats and tags, never pixels.</span></div>` +
    (n ? `<h4 class="d-h">Pending</h4><div class="ai-pend">${n} confirmation${n === 1 ? '' : 's'}<span class="faint">waiting for you</span></div>` : '');
  renderBar();
}

function modelPill(): string {
  const m = A.chat?.modelLabel();
  return m && m !== 'model' ? `<span class="model"><i class="ok-dot"></i>${esc(m)}</span>` : '';
}

function renderBar(): void {
  const bar = $('dc-ai')?.querySelector('.dr-bar');
  if (!bar || !A.open) return;
  const mode = A.chat?.mode() ?? 'ask';
  const hint = mode === 'auto' ? 'auto: changes run · dangerous ones wait for your OK' : mode === 'plan' ? 'plan: read-only, then a plan to approve' : 'Read-only tools run automatically · anything that changes data waits for your OK';
  bar.innerHTML = `<span class="ai-mark" aria-hidden="true">${icon('gem')}</span><b>Assistant</b>${modelPill()}` +
    `<span class="hintline dc-wide">${icon('lock')}${esc(hint)}</span><span class="grow"></span>` +
    `<button type="button" class="icon-btn ghost sm" data-act="ai-toggle" aria-expanded="true" aria-label="Collapse the assistant" title="Collapse">${icon('chev-d')}</button>`;
}

export function renderAi(): void {
  const el = $('dc-ai');
  if (!el) return;
  applyLLM(el.parentElement ?? document);
  const on = enabled();
  $('dc-app')?.classList.toggle('ai-open', on && A.open);
  if (!on) { if (A.built) { A.node?.remove(); el.innerHTML = ''; A.built = ''; } return; }
  el.classList.toggle('open', A.open);
  if (!A.open) {
    if (A.built !== 'bar') {
      A.node?.remove();
      el.innerHTML = `<div class="dr-bar"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><b>Assistant</b>${modelPill()}` +
        `<input id="dc-ai-q" class="ask" type="text" placeholder="Ask or tell… e.g. “find images without labels in val/”" aria-label="Ask the Data Center assistant" value="${esc(A.draft)}" data-input="ai-draft" autocomplete="off">` +
        `<span class="hintline dc-wide">${icon('lock')}reads freely · changes need your OK</span>` +
        `<button type="button" class="icon-btn ghost sm" data-act="ai-toggle" aria-expanded="false" aria-label="Open the assistant" title="Open the assistant">${icon('chev-u')}</button></div>`;
      A.built = 'bar';
    }
    return;
  }
  if (A.built !== 'open') {
    const c = chat();
    el.innerHTML = '<div class="dr-bar"></div><div class="dr-body"><div class="dr-main"></div><aside class="dr-side"></aside></div>';
    if (A.node) el.querySelector('.dr-main')?.append(A.node);
    A.built = 'open';
    if (A.draft) { c.setDraft(A.draft); A.draft = ''; }
  }
  renderSide();
}

function openDrawer(focus = true): void {
  A.open = true;
  rememberPref(PREF.ai, true);
  renderAi();
  if (focus) A.chat?.focus();
}

export const AI_ACTIONS: Record<string, Action> = {
  'ai-toggle': () => {
    if (A.open) { A.open = false; rememberPref(PREF.ai, false); renderAi(); return; }
    openDrawer();
  },
  'ai-draft': (el) => { A.draft = (el as HTMLInputElement).value; },
};

export function bindAi(): void {
  A.open = pref(PREF.ai) === true;
  window.addEventListener('llm-state', () => renderAi());
  // the collapsed bar: Enter opens the drawer and sends
  $('dc-ai')?.addEventListener('keydown', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement) || t.id !== 'dc-ai-q' || t.closest('.chat')) return;
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const text = t.value.trim();
    A.draft = '';
    openDrawer(!text);
    if (text) void chat().send(text);
  });
}

/** The scope changed (another folder, a new selection): repaint the scope column of an open drawer. */
export function aiScopeChanged(): void { if (A.open && enabled()) renderSide(); }
