// The Data Center assistant: a drawer docked under the centre and right panes (img1 collapsed: a one-line prompt bar;
// img4 expanded: the chat + a side column with the scope and which tools run on their own / need Confirm). Shown only
// while an LLM is connected and the 'data_center' feature is on (data-llm, core/llm.ts). A message goes to
// POST /api/llm/dc/chat with the scope (bucket, folder, selection) and streams NDJSON events (types-datacenter.ts
// AiEvent): text deltas; read-only tool calls (one compact collapsible card per run of them); a plan of changes as a
// checklist with Confirm / Cancel (POST /api/llm/dc/confirm runs only the checked actions, then the page reloads
// and a format change gets its toast); a clarifying question as option chips (a click sends the option); "can't do
// that" as a red-edged card with the alternative. History lives in memory, one chat per bucket.

import { api as apiReq, errText, postJSON } from '../core/api.js';
import { pref, rememberPref } from '../core/auth.js';
import { $, esc, icon } from '../core/dom.js';
import { applyLLM, LLM, llmOn } from '../core/llm.js';
import { md } from '../core/md.js';
import { toast } from '../core/toast.js';
import type { Action } from './app.js';
import { selectPaths } from './browser.js';
import { showRetag } from './ops.js';
import { bucketOf, D, fmtN, hooks, PREF } from './state.js';
import type { AiAction, AiEvent, ConfirmResult } from '../types-datacenter.js';

const READ_TOOLS = ['list_buckets', 'list_folder', 'search', 'detect_format', 'get_stats', 'read_text'];
const CHANGE_TOOLS = ['create_folder', 'move', 'copy', 'rename', 'delete', 'add_tags', 'remove_tags', 'send_to_session'];

interface ToolRun { id: string; name: string; args: Record<string, unknown>; summary: string; ok: boolean }
type Part =
  | { k: 'text'; text: string }
  | { k: 'tools'; items: ToolRun[]; open: boolean }
  | { k: 'plan'; id: string; actions: AiAction[]; on: Set<string>; state: 'pending' | 'running' | 'done' | 'cancelled'; results: ConfirmResult['results'] | null; bucket: string | null }
  | { k: 'ask'; question: string; options: string[]; answered: boolean }
  | { k: 'cannot'; reason: string; alternative: string | null };
interface Turn { role: 'user' | 'ai'; text: string; parts: Part[]; streaming: boolean; err: string | null; model: string | null }

const A = {
  open: false,
  chats: new Map<string, Turn[]>(),
  busy: null as AbortController | null,
  frame: 0,
  draft: '',
};
const chatKey = (): string => D.bid ?? '';
const chat = (): Turn[] => {
  let c = A.chats.get(chatKey());
  if (!c) { c = []; A.chats.set(chatKey(), c); }
  return c;
};
const enabled = (): boolean => llmOn('data_center');

// ---------------------------------------------------------------- markup

function argsText(a: Record<string, unknown>): string {
  return Object.entries(a).filter(([k]) => k !== 'bucket' || !D.bid).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ');
}

function toolsCard(p: Extract<Part, { k: 'tools' }>, ti: number, pi: number): string {
  const n = p.items.length;
  return `<div class="ai-tools${p.open ? ' open' : ''}"><button type="button" class="ai-tools-h" data-act="ai-tools" data-t="${ti}" data-p="${pi}" aria-expanded="${p.open}">` +
    `${icon('search')}<span>${n} read-only tool call${n === 1 ? '' : 's'} · ran automatically</span><span class="grow"></span>${icon(p.open ? 'chev-u' : 'chev-d')}</button>` +
    `${p.items.map((t) => `<div class="ai-tool">${icon(t.ok ? 'check-c' : 'x-c').replace('class="ico"', `class="ico ${t.ok ? 'okc' : 'badc'}"`)}` +
      `<span class="fnn">${esc(t.name)}</span><span class="args">(${esc(argsText(t.args))})</span><span class="res">${esc(t.summary)}</span></div>`).join('')}</div>`;
}

function planCard(p: Extract<Part, { k: 'plan' }>, ti: number, pi: number): string {
  const b = bucketOf(p.bucket);
  const res = new Map((p.results ?? []).map((r) => [r.id, r]));
  const live = p.state === 'pending';
  const paths = planPaths(p);
  return `<div class="ai-plan ${p.state}"><div class="plan-h">${icon('check')}<b>Plan</b><span>${p.actions.length} action${p.actions.length === 1 ? '' : 's'}${b ? ` on ${esc(b.name)}` : ''}</span>` +
    `<span class="pill">${live ? `${icon('lock')}needs your confirmation` : p.state === 'running' ? 'running…' : p.state === 'done' ? 'done' : 'cancelled'}</span></div>` +
    `<div class="steps">${p.actions.map((a) => {
      const r = res.get(a.id);
      const mark = r ? icon(r.ok ? 'check-c' : 'x-c').replace('class="ico"', `class="ico ${r.ok ? 'okc' : 'badc'}"`)
        : `<input type="checkbox" data-change="ai-step" data-t="${ti}" data-p="${pi}" value="${esc(a.id)}"${p.on.has(a.id) ? ' checked' : ''}${live ? '' : ' disabled'} aria-label="${esc(a.summary)}">`;
      return `<label class="step${p.on.has(a.id) ? '' : ' opt'}">${mark}<span class="op">${esc(a.tool)}</span><span class="d">${esc(a.summary)}</span>` +
        `${r && !r.ok ? `<span class="sub err">${esc(r.error ?? 'failed')}</span>` : r ? `<span class="sub">${esc(r.summary)}</span>` : ''}</label>`;
    }).join('')}</div>` +
    (live ? `<div class="plan-f"><span class="hint">${icon('undo')}Nothing changes until you confirm</span>` +
      (paths.length && p.bucket === D.bid ? `<button type="button" class="ghost sm" data-act="ai-show" data-t="${ti}" data-p="${pi}">Show in folder</button>` : '') +
      `<button type="button" class="sm" data-act="ai-cancel" data-t="${ti}" data-p="${pi}">Cancel</button>` +
      `<button type="button" class="primary sm" data-act="ai-confirm" data-t="${ti}" data-p="${pi}"${p.on.size ? '' : ' disabled'}>${icon('check')}Confirm ${p.on.size} action${p.on.size === 1 ? '' : 's'}</button></div>` : '') + '</div>';
}

function turnHtml(t: Turn, ti: number): string {
  if (t.role === 'user') return `<div class="ai-msg user"><div class="bubble">${esc(t.text)}</div></div>`;
  const parts = t.parts.map((p, pi) => {
    if (p.k === 'text') return `<div class="ai-md">${md(p.text)}</div>`;
    if (p.k === 'tools') return toolsCard(p, ti, pi);
    if (p.k === 'plan') return planCard(p, ti, pi);
    if (p.k === 'ask') return `<div class="ai-ask"><p>${esc(p.question)}</p><div class="choices">${p.options.map((o) =>
      `<button type="button" class="choice" data-act="ai-opt" data-text="${esc(o)}"${p.answered ? ' disabled' : ''}>${esc(o)}</button>`).join('')}` +
      `<button type="button" class="choice other" data-act="ai-focus"${p.answered ? ' disabled' : ''}>Something else…</button></div></div>`;
    return `<div class="ai-cant"><div class="h">${icon('x-c')}<b>I can't do that.</b><span>${esc(p.reason)}</span></div>` +
      (p.alternative ? `<div class="alt">${icon('sparkle')}<span>${esc(p.alternative)}</span><button type="button" class="sm" data-act="ai-opt" data-text="${esc(p.alternative)}">Plan that</button></div>` : '') + '</div>';
  }).join('');
  const busy = t.streaming && !t.parts.length ? '<div class="ai-typing"><i></i><i></i><i></i></div>' : '';
  const err = t.err ? `<div class="status-line err">${icon('alert')}${esc(t.err)}</div>` : '';
  return `<div class="ai-msg"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><div class="mbody">${parts}${busy}${err}</div></div>`;
}

function scopeHtml(): string {
  const b = bucketOf(D.bid);
  const n = D.listing?.total;
  return `<div class="scope">${b ? `<div class="s">${icon('db')}<span>${esc(b.name)}</span><span class="num">bucket</span></div>` : `<div class="s">${icon('grid')}<span>All buckets</span></div>`}` +
    (b && D.path ? `<div class="s">${icon('folder')}<span>${esc(D.path)}</span><span class="num">${n != null ? fmtN(n) : ''}</span></div>` : '') +
    (D.sel.size ? `<div class="s">${icon('check')}<span>${fmtN(D.sel.size)} selected</span></div>` : '') + '</div>';
}

function renderLog(): void {
  const log = $('dc-ai-log');
  if (!log) return;
  const c = chat();
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  log.innerHTML = c.length ? c.map(turnHtml).join('')
    : `<div class="ai-hello"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><div><b>Ask about your data, or tell me what to change.</b>` +
      `<p class="muted">I read freely (list, search, stats, detect); anything that changes files or tags comes back as a plan you confirm.</p>` +
      `<div class="choices">${['Find images without labels in val/', 'Which buckets are not YOLO yet?', 'Tag this folder by camera'].map((s) =>
        `<button type="button" class="choice" data-act="ai-opt" data-text="${esc(s)}">${esc(s)}</button>`).join('')}</div></div></div>`;
  if (stick || A.busy) log.scrollTop = log.scrollHeight;
}

function modelPill(): string {
  const p = LLM.provider;
  return p ? `<span class="model"><i class="ok-dot"></i>${esc(p.model || p.name)}${p.kind ? ` · ${esc(p.name || p.kind)}` : ''}</span>` : '';
}

export function renderAi(): void {
  const el = $('dc-ai');
  if (!el) return;
  applyLLM(el.parentElement ?? document);
  const on = enabled();
  $('dc-app')?.classList.toggle('ai-open', on && A.open);
  if (!on) return;
  el.classList.toggle('open', A.open);
  const sendBtn = A.busy
    ? `<button type="button" class="icon-btn" data-act="ai-stop" aria-label="Stop" title="Stop">${icon('stop')}</button>`
    : `<button type="button" class="icon-btn primary" data-act="ai-send" aria-label="Send" title="Send (Enter)">${icon('send')}</button>`;
  if (!A.open) {
    el.innerHTML = `<div class="dr-bar"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><b>Assistant</b>${modelPill()}` +
      `<input id="dc-ai-q" class="ask" type="text" placeholder="Ask or tell… e.g. “find images without labels in val/”" aria-label="Ask the Data Center assistant" value="${esc(A.draft)}" data-input="ai-draft" autocomplete="off">` +
      `<span class="hintline dc-wide">${icon('lock')}reads freely · changes need your OK</span>` +
      `<button type="button" class="icon-btn ghost sm" data-act="ai-toggle" aria-expanded="false" aria-label="Open the assistant" title="Open the assistant">${icon('chev-u')}</button></div>`;
    return;
  }
  const pending = [...A.chats.values()].flat().flatMap((t) => t.parts).filter((p): p is Extract<Part, { k: 'plan' }> => p.k === 'plan' && p.state === 'pending');
  el.innerHTML = `<div class="dr-bar"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><b>Assistant</b>${modelPill()}` +
    `<span class="hintline dc-wide">${icon('lock')}Read-only tools run automatically · anything that changes data waits for Confirm</span><span class="grow"></span>` +
    `<button type="button" class="ghost sm" data-act="ai-new">${icon('plus')}New chat</button>` +
    `<button type="button" class="icon-btn ghost sm" data-act="ai-toggle" aria-expanded="true" aria-label="Collapse the assistant" title="Collapse">${icon('chev-d')}</button></div>` +
    `<div class="dr-body"><div class="dr-main"><div id="dc-ai-log" class="log" aria-live="polite"></div>` +
    `<div class="composer"><textarea id="dc-ai-q" rows="1" placeholder="Ask or tell the assistant…" aria-label="Message" data-input="ai-draft">${esc(A.draft)}</textarea>` +
    `<span class="faint scope-line dc-wide">scope: ${esc(bucketOf(D.bid)?.name ?? 'all buckets')}${D.path ? ` / ${esc(D.path)}` : ''}${D.sel.size ? ` · ${fmtN(D.sel.size)} selected` : ''}</span>${sendBtn}</div></div>` +
    `<aside class="dr-side"><h4 class="d-h">Scope</h4>${scopeHtml()}` +
    `<h4 class="d-h">Tools · run automatically</h4><div class="toolcat">${READ_TOOLS.map((t) => `<span class="tchip">${t}</span>`).join('')}</div>` +
    `<h4 class="d-h">Tools · need confirm</h4><div class="toolcat">${CHANGE_TOOLS.map((t) => `<span class="tchip mut">${t}</span>`).join('')}</div>` +
    `<div class="note info">${icon('alert')}<span>The assistant sees file names, folder stats and tags, never pixels.</span></div>` +
    (pending.length ? `<h4 class="d-h">Pending</h4>${pending.map((p) => `<div class="ai-pend">Plan · ${p.actions.length} action${p.actions.length === 1 ? '' : 's'}<span class="faint">awaiting you</span></div>`).join('')}` : '') +
    '</aside></div>';
  renderLog();
}

function scheduleLog(): void {
  if (A.frame) return;
  A.frame = requestAnimationFrame(() => { A.frame = 0; renderLog(); });
}

// ---------------------------------------------------------------- talking

function history(): Array<{ role: string; text: string }> {
  return chat().filter((t) => !t.streaming).map((t) => ({
    role: t.role === 'user' ? 'user' : 'assistant',
    text: t.role === 'user' ? t.text : t.parts.map((p) => p.k === 'text' ? p.text : p.k === 'ask' ? `${p.question} (${p.options.join(' / ')})`
      : p.k === 'cannot' ? `Cannot: ${p.reason}${p.alternative ? ` Instead: ${p.alternative}` : ''}`
        : p.k === 'plan' ? `[plan of ${p.actions.length} actions: ${p.actions.map((a) => a.summary).join('; ')} — ${p.state}]` : '').filter(Boolean).join('\n'),
  })).filter((m) => m.text);
}

function onEvent(t: Turn, ev: AiEvent): void {
  const last = t.parts[t.parts.length - 1];
  switch (ev.type) {
    case 'meta': t.model = ev.model ?? null; break;
    case 'delta':
      if (last?.k === 'text') last.text += ev.text; else t.parts.push({ k: 'text', text: ev.text });
      break;
    case 'tool': {
      const run: ToolRun = { id: ev.id, name: ev.name, args: ev.args ?? {}, summary: ev.summary ?? '', ok: ev.ok !== false };
      if (last?.k === 'tools') last.items.push(run); else t.parts.push({ k: 'tools', items: [run], open: false });
      break;
    }
    case 'plan':
      t.parts.push({ k: 'plan', id: ev.plan_id, actions: ev.actions, on: new Set(ev.actions.map((a) => a.id)), state: 'pending', results: null, bucket: D.bid });
      break;
    case 'ask': t.parts.push({ k: 'ask', question: ev.question, options: ev.options ?? [], answered: false }); break;
    case 'cannot': t.parts.push({ k: 'cannot', reason: ev.reason, alternative: ev.alternative ?? null }); break;
    case 'error': t.err = ev.detail || 'The assistant failed.'; break;
    case 'done': break;
  }
}

async function send(text0: string): Promise<void> {
  const text = text0.trim();
  if (!text || A.busy || !enabled()) return;
  for (const t of chat()) for (const p of t.parts) if (p.k === 'ask') p.answered = true;
  const messages = [...history(), { role: 'user', text }];
  const t: Turn = { role: 'ai', text: '', parts: [], streaming: true, err: null, model: null };
  chat().push({ role: 'user', text, parts: [], streaming: false, err: null, model: null }, t);
  A.draft = '';
  A.open = true;
  rememberPref(PREF.ai, true);
  const ac = new AbortController();
  A.busy = ac;
  renderAi();
  try {
    const r = await apiReq('api/llm/dc/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: ac.signal,
      body: JSON.stringify({ messages, scope: { bucket: D.bid, path: D.path, selection: [...D.sel].slice(0, 500) } }),
    });
    if (!r.body) throw new Error('the server sent no body');
    const rd = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    const line = (s: string): void => {
      if (!s) return;
      try { onEvent(t, JSON.parse(s) as AiEvent); } catch (e) { return; }
      scheduleLog();
    };
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n')) >= 0) { line(buf.slice(0, k).trim()); buf = buf.slice(k + 1); }
    }
    line(buf.trim());
    if (!t.parts.length && !t.err) t.err = 'The assistant sent an empty answer.';
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') t.parts.push({ k: 'text', text: '_stopped_' });
    else t.err = errText(e);
  } finally {
    t.streaming = false;
    if (A.busy === ac) A.busy = null;
    if (A.frame) { cancelAnimationFrame(A.frame); A.frame = 0; }
    renderAi();
  }
}

function partAt(el: HTMLElement): Part | undefined {
  return chat()[Number(el.dataset['t'])]?.parts[Number(el.dataset['p'])];
}

/** Paths a plan touches (args.path / args.paths) that are in the open folder. */
function planPaths(p: Extract<Part, { k: 'plan' }>): string[] {
  const have = new Set(D.listing?.entries.map((e) => e.path) ?? []);
  const out: string[] = [];
  for (const a of p.actions) {
    const v = a.args['paths'] ?? a.args['path'];
    for (const x of Array.isArray(v) ? v : [v]) if (typeof x === 'string' && have.has(x)) out.push(x);
  }
  return out;
}

async function confirm(p: Extract<Part, { k: 'plan' }>): Promise<void> {
  const ids = p.actions.filter((a) => p.on.has(a.id)).map((a) => a.id);
  if (!ids.length) return;
  p.state = 'running';
  renderAi();
  try {
    const r = await postJSON<ConfirmResult>('api/llm/dc/confirm', { plan_id: p.id, action_ids: ids });
    p.results = r.results;
    p.state = 'done';
    const bad = r.results.filter((x) => !x.ok).length;
    const bid = p.bucket ?? D.bid;
    for (const rt of r.retagged ?? []) if (bid) showRetag(bid, rt, null, 'The assistant changed this bucket');
    if (!r.retagged?.length) toast(bad ? `${r.results.length - bad} done, ${bad} failed` : `Done: ${r.results.length} action${r.results.length === 1 ? '' : 's'}`, bad ? 'err' : 'ok');
  } catch (e) {
    p.state = 'pending';
    toast(`Could not run the plan: ${errText(e)}`, 'err');
  }
  renderAi();
  await hooks.reload();
}

export const AI_ACTIONS: Record<string, Action> = {
  'ai-toggle': () => { A.open = !A.open; rememberPref(PREF.ai, A.open); renderAi(); if (A.open) $('dc-ai-q')?.focus(); },
  'ai-send': () => { void send(($('dc-ai-q') as HTMLInputElement | HTMLTextAreaElement | null)?.value ?? A.draft); },
  'ai-stop': () => { A.busy?.abort(); },
  'ai-new': () => { A.chats.set(chatKey(), []); renderAi(); },
  'ai-draft': (el) => { A.draft = (el as HTMLInputElement).value; },
  'ai-opt': (el) => { void send(el.dataset['text'] ?? ''); },
  'ai-focus': () => { $('dc-ai-q')?.focus(); },
  'ai-tools': (el) => { const p = partAt(el); if (p?.k === 'tools') { p.open = !p.open; renderLog(); } },
  'ai-step': (el) => {
    const p = partAt(el);
    const i = el as HTMLInputElement;
    if (p?.k !== 'plan') return;
    if (i.checked) p.on.add(i.value); else p.on.delete(i.value);
    renderLog();
  },
  'ai-cancel': (el) => { const p = partAt(el); if (p?.k === 'plan') { p.state = 'cancelled'; renderAi(); } },
  'ai-confirm': (el) => { const p = partAt(el); if (p?.k === 'plan') void confirm(p); },
  'ai-show': (el) => { const p = partAt(el); if (p?.k === 'plan') selectPaths(planPaths(p)); },
};

export function bindAi(): void {
  A.open = pref(PREF.ai) === true;
  window.addEventListener('llm-state', () => renderAi());
  const el = $('dc-ai');
  el?.addEventListener('keydown', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement) || t.id !== 'dc-ai-q') return;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(t.value); }
    if (e.key === 'Escape' && A.open && !t.value) { A.open = false; renderAi(); }
  });
}

/** The scope changed (another folder, a new selection): repaint the scope line / column of an open drawer. */
export function aiScopeChanged(): void { if (A.open && enabled() && !A.busy) renderAi(); }
