// The shared ◆ chat of both assistants (Data Center drawer: datacenter/ai.ts, role 'data'; visualizer tab:
// views/assistant.ts, role 'visual'). mountChat(root, opts) draws a session header, the transcript and the composer
// into `root` and talks to POST api/llm/agent/<role>/chat (NDJSON, dataviz/llm/engine/events.py), /confirm, /stop,
// /catalog and /models, and to the chat sessions (api/llm/sessions*).
//
// - reduce(turn, event) is PURE (unit-tested under node, tests/llm/test_chat_js.py): it folds the events of one answer
//   into blocks (thinking, text, tool groups, search, skill, todo, confirm, ask / cannot, plan, UI action, retry,
//   notice). Page-only events start with `x_` (a confirm was sent, a UI action applied, …).
// - Rendering is template strings + esc(); one delegated click / change / input / keydown listener per mount on the
//   root (elements carry data-ca / data-cc / data-ci), and one per floating popover (model, slash commands).
//   While a turn streams only that turn is re-rendered, at most once per animation frame; live timers
//   ([data-since]) tick without a re-render.
// - Classes are scoped under `.chat` (static/css/chat.css). The answer container also carries `ai-msg bot`.

import type {
  AiTurn, Block, ChatCatalog, ChatEffort, ChatEvent, ChatMode, ChatPrefs, ChatRole, ChatSession, SessionDetail, SessionList, SessionRef, ThinkLevel, ToolItem, UiAction, UiResult, WebLevel,
} from '../types-chat.js';
import { errText, getJSON, isStatus, postJSON, sendJSON } from './api.js';
import { copyText, esc, icon } from './dom.js';
import { LLM } from './llm.js';
import { md } from './md.js';
import { arrowNav, floatPopover } from './popover.js';
import { isAbort, streamNDJSON } from './stream.js';
import { actionToast, toast } from './toast.js';
import { saveFile } from './format.js';
import {
  approvedMessage, arr, cycle, dateGroup, EFFORTS, isEffort, isMode, isRec, isThink, isWeb, newTurn, nextMode, num, parseCommand, recOf, reduce, str, strOrNull, THINKS, turnText, WEBS,
} from './chat-reduce.js';

// the pure reducer and its helpers live in chat-reduce.ts; these are part of this module's public API too
export { approvedMessage, dateGroup, EFFORTS, MODES, newTurn, nextMode, parseCommand, reduce, THINKS, turnText, WEBS } from './chat-reduce.js';

const fmtK = (n: number | null | undefined): string => (n == null ? '–' : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n)));
const fmtMs = (ms: number | null | undefined): string => (ms == null ? '' : ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`);
const fmtSince = (ms: number): string => (ms < 60000 ? `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s` : `${Math.floor(ms / 60000)} min ${Math.floor((ms % 60000) / 1000)} s`);
/** an onUiAction answer as a result: true / undefined = done, false = could not */
const asResult = (v: boolean | UiResult | void): UiResult => (typeof v === 'object' && v !== null ? v : { ok: v !== false });
const plural = (n: number, w: string): string => `${n} ${w}${n === 1 ? '' : 's'}`;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function fmtWhen(iso: string | null | undefined): string {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return '';
  const g = dateGroup(iso);
  if (g === 'Today') return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return g === 'Yesterday' ? 'yesterday' : `${d.getDate()} ${MONTHS[d.getMonth()] ?? ''}`;
}
const fmtDay = (iso: string | null | undefined): string => { const d = iso ? new Date(iso) : null; return d && !Number.isNaN(d.getTime()) ? `${d.getDate()} ${MONTHS[d.getMonth()] ?? ''}` : '?'; };
function argsShort(a: Record<string, unknown>): string {
  return Object.entries(a).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(', ').slice(0, 160);
}
const safeUrl = (u: string): string | null => (/^https?:\/\//i.test(u) ? u : null);
const hostOf = (u: string): string => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } };

// two small icons the sprite has no symbol for (same 24px grid and stroke as icons.svg)
const svg = (d: string): string => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const IC_CLOCK = svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>');
const IC_PIN = svg('<path d="M9 4h6l-1 5 3 3v1.5H7V12l3-3z"/><path d="M12 13.5V20"/>');
const okIcon = (ok: boolean | null): string => (ok === null ? '<span class="ct-spin" aria-label="running"></span>'
  : icon(ok ? 'check-c' : 'x-c').replace('class="ico"', `class="ico ${ok ? 'ct-okc' : 'ct-badc'}"`));
const badge = (risk: string): string => `<span class="ct-badge r-${esc(risk)}">${esc(risk)}</span>`;
const since = (t0: number): string => `<span class="num" data-since="${t0}">${fmtSince(Math.max(0, performance.now() - t0))}</span>`;

const MODE_HINT: Record<ChatRole, Record<ChatMode, string>> = {
  data: {
    ask: 'ask: every change waits for your OK',
    auto: '⏵⏵ auto: changes run, deletes still ask',
    plan: '⏸ plan: read-only research, then a plan to approve',
  },
  visual: {
    ask: 'ask: every page change is an Apply button',
    auto: '⏵⏵ auto: page changes apply at once',
    plan: '⏸ plan: read-only explanation and steps to approve',
  },
};
const WEB_HINT: Record<WebLevel, string> = {
  off: 'no web search',
  public: 'web: general words only, nothing from your data',
  technical: 'web: technical terms (class names, formats) may be searched',
  full: 'web: queries may contain names from your data',
};
const DEFAULT_COMMANDS = [
  ['model', '', 'pick the provider and model of this chat'], ['mode', 'ask|auto|plan', 'how changes are confirmed (Shift+Tab)'],
  ['effort', 'low|medium|high', 'how hard it works'], ['think', 'off|low|medium|high', 'thinking only'],
  ['web', 'off|public|technical|full', 'web search level'], ['skills', '', 'list the skills'], ['skill', '<name>', 'use a skill for the next message'],
  ['sessions', '', 'open the chat history'], ['new', '', 'start a new chat'], ['clear', '', 'start a new chat'],
  ['resume', '<search>', 'open an earlier chat'], ['rename', '<title>', 'rename this chat'], ['delete', '', 'delete this chat'],
  ['cost', '', 'tokens used by this chat'], ['compact', '[focus]', 'summarise older messages to free context'], ['export', '', 'download this chat as Markdown'],
  ['prompts', '', 'your own instructions and skills (Settings)'], ['plugins', '', 'installed plugins (Settings)'], ['help', '', 'list the commands'],
].map(([name, args, description]) => ({ name: name ?? '', args: args ?? '', description: description ?? '' }));

// ================================================================ the component

interface UserEntry { role: 'user'; text: string; label: string | null; voice: boolean }
interface NoteEntry { role: 'note'; md: string; level: string }
interface CustomEntry { role: 'custom'; html: () => string }
interface ArchiveEntry { role: 'archive'; items: Array<{ role: string; text: string }> }
type Entry = UserEntry | AiTurn | NoteEntry | CustomEntry | ArchiveEntry;

export interface ChatOptions {
  role: ChatRole;
  /** what the agent should know about where the user is (sent with every message) */
  scope: () => Record<string, unknown>;
  /** the session's scope (`{bucket_id}` / `{dataset_id}`): History "This bucket / dataset", new sessions */
  sessionScope?: () => Record<string, string> | null;
  /** "bucket" / "dataset": the History filter's label */
  scopeName?: string;
  /** a short label for a session's scope chip in History */
  scopeLabel?: (scope: Record<string, unknown> | null) => string;
  /** a page action (applied=true: apply it now; else the user clicked Apply). false / {ok: false} = could not.
   *  When the agent waits for it (event `wait`), the outcome is posted to api/llm/agent/ui-result. */
  onUiAction?: (a: UiAction) => boolean | UiResult | Promise<boolean | UiResult>;
  /** a change ran (tool_end of a change / danger tool, an Undo): reload what the page shows */
  onChange?: (ev: ChatEvent) => void;
  /** an answer finished (meter, side panels) */
  onDone?: (t: AiTurn) => void;
  /** every event of the agent's stream, after the transcript took it (the dock's tasks strip and context ring) */
  onEvent?: (ev: ChatEvent) => void;
  /** the catalogue loaded / something the page shows next to the chat changed (pending confirms, mode) */
  onUpdate?: () => void;
  /** open a Settings section ('prompts', 'plugins', 'llm') */
  settings?: (section: string) => void;
  /** extra chips after the built-in ones in the composer bar (HTML; the page handles their clicks) */
  extraChips?: () => string;
  /** HTML placed before the chips in the composer bar (the mic) */
  composerExtra?: string;
  placeholder?: string;
  /** the empty transcript (quick actions); the page handles its clicks */
  emptyHTML?: () => string;
  /** id of the textarea (tests and other modules find it by id) */
  inputId?: string;
  /** file name stem for /export and Download .md */
  exportName?: () => string;
}

export interface ChatHandle {
  root: HTMLElement;
  send: (text: string, o?: { label?: string; kind?: string; extra?: Record<string, unknown> }) => Promise<void>;
  busy: () => boolean;
  stop: () => void;
  newChat: () => void;
  focus: () => void;
  input: () => HTMLTextAreaElement;
  setDraft: (text: string) => void;
  /** a user bubble the model never sees (label check, voice transcripts) */
  user: (text: string, voice?: boolean) => void;
  note: (markdown: string, level?: 'info' | 'err') => void;
  /** a page-drawn turn (label check cards, auto-tag progress): update() redraws it */
  custom: (html: () => string) => { update: () => void; el: () => HTMLElement | null };
  /** an old transcript shown read-only (the localStorage import) */
  archive: (items: Array<{ role: string; text: string }>) => void;
  refresh: () => void;
  catalog: () => ChatCatalog | null;
  mode: () => ChatMode;
  pending: () => number;
  modelLabel: () => string;
  /** the last finished answer's usage */
  lastUsage: () => { in: number; out: number; ms: number | null } | null;
  /** the saved chat on screen (null before the first message) */
  sessionId: () => string | null;
  /** open a saved chat (quiet: no toast / focus; false when it is gone) */
  open: (id: string, quiet?: boolean) => Promise<boolean>;
}

let uidSeq = 0;
const CONFIG_DEBOUNCE = 600;

export function mountChat(root: HTMLElement, opts: ChatOptions): ChatHandle {
  const uid = `chat${++uidSeq}`;
  const role = opts.role;
  const C = {
    turns: [] as Entry[],
    sid: null as string | null,
    title: '',
    pinned: false,
    renaming: false,
    delCur: false,
    busy: null as AbortController | null,
    live: -1,
    runId: null as string | null,
    cat: null as ChatCatalog | null,
    prefs: { mode: 'ask', effort: 'medium', think: null, web: 'off' } as ChatPrefs,
    forced: [] as string[],
    models: new Map<string, string[] | 'loading' | { err: string }>(),
    open: new Map<string, boolean>(),        // fold state overrides, by block key
    picks: new Map<string, Set<string>>(),   // confirm id -> ticked action ids
    edits: new Map<string, string>(),        // `${confirm id}:${action id}` -> edited web query
    sessionsOK: null as boolean | null,
    willRemove: null as SessionRef | null,
    blocked: null as string | null,          // "20 pinned chats…" shown in place of the new chat
    frame: 0,
    ticker: 0,
    hintTimer: 0,
    cfgTimer: 0,
    slash: { open: false, items: [] as Array<{ name: string; args: string; description: string }>, i: 0, close: null as null | (() => void) },
    modelClose: null as null | (() => void),
  };
  const H = {
    open: false, q: '', all: false, items: [] as ChatSession[], count: 0, max: 20, next: null as string | null,
    willRemove: null as SessionRef | null, loading: false, err: null as string | null, select: false,
    picked: new Set<string>(), confirm: null as string | null, includePinned: false, renaming: null as string | null, qTimer: 0,
  };

  // ---------------------------------------------------------------- skeleton (built once)
  root.classList.add('chat');
  root.dataset['role'] = role;
  const inputId = opts.inputId || `${uid}-q`;
  root.innerHTML =
    `<div class="chat-head"></div>` +
    `<div class="chat-log" id="${uid}-log" role="log" aria-live="polite" aria-relevant="additions"></div>` +
    `<div class="chat-hist hidden" id="${uid}-hist" role="dialog" aria-label="Chat history"></div>` +
    '<div class="chat-composer">' +
    '<div class="chat-forced hidden"></div>' +
    '<div class="chat-hint" aria-live="polite" hidden></div>' +
    `<textarea id="${esc(inputId)}" rows="1" placeholder="${esc(opts.placeholder || 'Ask or tell…  / for commands')}" aria-label="Message the assistant" ` +
    `aria-autocomplete="list" aria-controls="${uid}-slash" autocomplete="off"></textarea>` +
    `<div class="chat-cbar">${opts.composerExtra || ''}<div class="chat-chips" role="group" aria-label="Chat options"></div>` +
    '<button type="button" class="primary icon-btn chat-send" data-ca="send"></button></div></div>';
  const q = <T extends Element = HTMLElement>(sel: string): T => root.querySelector<T>(sel) as T;
  const headEl = q('.chat-head'), logEl = q('.chat-log'), histEl = q('.chat-hist'), chipsEl = q('.chat-chips'),
    hintEl = q('.chat-hint'), sendEl = q<HTMLButtonElement>('.chat-send'), forcedEl = q('.chat-forced');
  const ta = q<HTMLTextAreaElement>('textarea');
  const modelPop = document.createElement('div');
  modelPop.id = `${uid}-model`;
  modelPop.className = 'chat-pop chat-model-pop hidden';
  modelPop.setAttribute('role', 'listbox');
  modelPop.setAttribute('aria-label', 'Model');
  const slashPop = document.createElement('div');
  slashPop.id = `${uid}-slash`;
  slashPop.className = 'chat-pop chat-slash-pop hidden';
  slashPop.setAttribute('role', 'listbox');
  slashPop.setAttribute('aria-label', 'Commands');
  document.body.append(modelPop, slashPop);
  arrowNav(modelPop.id);

  // ---------------------------------------------------------------- small getters
  const aiAt = (i: number): AiTurn | null => { const t = C.turns[i]; return t && t.role === 'ai' ? t : null; };
  const mode = (): ChatMode => (isMode(C.prefs.mode) ? C.prefs.mode : 'ask');
  const effort = (): ChatEffort => (isEffort(C.prefs.effort) ? C.prefs.effort : 'medium');
  const think = (): ThinkLevel | null => (isThink(C.prefs.think) ? C.prefs.think : null);
  const web = (): WebLevel => (isWeb(C.prefs.web) ? C.prefs.web : 'off');
  const commands = (): Array<{ name: string; args: string; description: string }> => (C.cat?.commands?.length ? C.cat.commands : DEFAULT_COMMANDS);
  const provider = (): { id: string; name: string; model: string | null } | null => {
    const ps = C.cat?.providers ?? [];
    const want = C.prefs.provider_id || C.cat?.active;
    const p = ps.find((x) => x.id === want) ?? ps.find((x) => x.connected) ?? null;
    if (p) return p;
    return LLM.provider ? { id: '', name: LLM.provider.name || '', model: LLM.provider.model || null } : null;
  };
  const modelLabel = (): string => C.prefs.model || provider()?.model || 'model';
  const scopeKey = (): string | null => { const s = opts.sessionScope?.(); const v = s ? Object.values(s)[0] : null; return v || null; };
  const scopeWord = opts.scopeName || (role === 'data' ? 'bucket' : 'dataset');

  // ---------------------------------------------------------------- render: head
  function renderHead(): void {
    const title = C.title || 'New chat';
    const titleHtml = C.renaming
      ? `<input class="chat-title-in" data-ci="title" value="${esc(C.title)}" aria-label="Chat title" maxlength="120">`
      : `<button type="button" class="chat-title" data-ca="rename" title="${C.sid ? 'Rename this chat' : 'The title is set from your first message'}"${C.sid ? '' : ' disabled'}>` +
        `${C.pinned ? IC_PIN : ''}<span>${esc(title)}</span></button>`;
    const del = C.delCur
      ? `<span class="chat-hconf" role="group" aria-label="Delete this chat?">Delete this chat?<button type="button" class="danger" data-ca="del-cur-yes">Yes</button><button type="button" data-ca="del-cur-no">No</button></span>`
      : (C.sid ? `<button type="button" class="icon-btn ghost" data-ca="del-cur" aria-label="Delete this chat" title="Delete this chat">${icon('trash')}</button>` : '');
    headEl.innerHTML = titleHtml + '<span class="grow"></span>' + del +
      `<button type="button" class="ghost chat-hbtn" data-ca="history" aria-expanded="${H.open}" aria-controls="${uid}-hist" title="Chat history (/sessions)">${IC_CLOCK}<span>History</span></button>` +
      `<button type="button" class="ghost chat-hbtn" data-ca="new" title="New chat (/new)">${icon('plus')}<span>New</span></button>`;
    if (C.renaming) { const i = headEl.querySelector<HTMLInputElement>('input'); i?.focus(); i?.select(); }
  }

  // ---------------------------------------------------------------- render: transcript
  const keyOf = (ti: number, bi: number, sub = ''): string => `${ti}.${bi}${sub ? `.${sub}` : ''}`;
  const isOpen = (key: string, dflt: boolean): boolean => C.open.get(key) ?? dflt;
  const fold = (key: string, open: boolean, inner: string, cls = 'ct-fold'): string =>
    `<button type="button" class="${cls}" data-ca="fold" data-key="${key}" aria-expanded="${open}">${inner}${icon(open ? 'chev-u' : 'chev-d')}</button>`;

  function itemDetail(it: ToolItem): string {
    const args = Object.keys(it.args).length ? JSON.stringify(it.args, null, 2) : '';
    return '<div class="ct-detail">' + (args ? `<div class="ct-lbl">Arguments</div><pre>${esc(args)}</pre>` : '') +
      (it.preview ? `<div class="ct-lbl">${it.ok === false ? 'Error' : 'Result'}</div><pre>${esc(it.preview)}</pre>` : '') +
      (it.url && safeUrl(it.url) ? `<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.url)}</a>` : '') +
      (!args && !it.preview ? '<span class="ct-dim">no arguments</span>' : '') + '</div>';
  }

  function undoBtn(undoId: string | null, undone: boolean, ok: boolean | null, args: Record<string, unknown>): string {
    if (!undoId || ok === false) return '';
    if (undone) return '<span class="ct-dim">undone</span>';
    return `<button type="button" class="ghost ct-mini" data-ca="undo" data-undo="${esc(undoId)}" data-bucket="${esc(str(args['bucket']))}">${icon('undo')}Undo</button>`;
  }

  function toolRow(it: ToolItem, key: string, card: boolean): string {
    const open = isOpen(key, false);
    return `<div class="ct-tool${open ? ' open' : ''}${it.ok === false ? ' bad' : ''}"><div class="ct-row">` +
      `<button type="button" class="ct-rowbtn" data-ca="fold" data-key="${key}" aria-expanded="${open}">${okIcon(it.ok)}` +
      `<code class="ct-fn">${esc(it.name)}</code>${card ? badge(it.risk) : ''}<span class="ct-args">${esc(card && it.summary ? '' : argsShort(it.args))}</span>` +
      `<span class="ct-sum">${esc(it.summary)}</span><span class="ct-ms num">${esc(fmtMs(it.ms))}</span>${icon(open ? 'chev-u' : 'chev-d')}</button>` +
      `${undoBtn(it.undoId, it.undone, it.ok, it.args)}</div>${open ? itemDetail(it) : ''}</div>`;
  }

  /** a change / danger tool call: its own card in the answer (read calls are rows of the activity dropdown) */
  function cardHTML(b: Extract<Block, { k: 'tools' }>, ti: number, bi: number): string {
    return `<div class="ct-card r-${esc(b.items[0]?.risk ?? 'change')}">${b.items.map((it) => toolRow(it, keyOf(ti, bi, it.id), true)).join('')}</div>`;
  }

  // ---- the activity dropdown: one per answer, every step (thinking, reads, searches, skills, retries, the words
  // said between tool calls) as one row of the same shape; its header says what the model is doing right now.
  type Step = { b: Block; bi: number };
  const isStep = (b: Block): boolean => b.k === 'think' || (b.k === 'tools' && b.read) || b.k === 'search' || b.k === 'skill' ||
    b.k === 'retry' || (b.k === 'notice' && b.level === 'info');
  const shortModel = (m: string | null | undefined): string => (m ? m.split('/').pop() || m : 'the model');
  const firstLine = (text: string): string => text.trim().split('\n').find((l) => l.trim())?.trim().slice(0, 200) ?? '';

  /** one row: [icon] label · detail ……… time ▾ ; `body` (when given) opens under it */
  function actRow(key: string | null, ico: string, label: string, detail: string, time: string, body: (() => string) | null, cls = '', dflt = false): string {
    const inner = `<span class="ct-act-ic">${ico}</span><span class="ct-act-l">${label}</span><span class="ct-act-d">${detail}</span>` +
      `<span class="ct-act-ms num">${time}</span>`;
    if (!key || !body) return `<div class="ct-act-row${cls ? ` ${cls}` : ''}"><div class="ct-act-rh">${inner}<span class="ct-act-chev"></span></div></div>`;
    const open = isOpen(key, dflt);
    return `<div class="ct-act-row${cls ? ` ${cls}` : ''}${open ? ' open' : ''}"><button type="button" class="ct-act-rh" data-ca="fold" data-key="${key}" aria-expanded="${open}">` +
      `${inner}${icon(open ? 'chev-u' : 'chev-d')}</button>${open ? `<div class="ct-act-body">${body()}</div>` : ''}</div>`;
  }

  function stepRows(s: Step, t: AiTurn, ti: number): string {
    const { b, bi } = s;
    const key = keyOf(ti, bi);
    switch (b.k) {
      case 'think': {
        const live = b.ms === null && t.status === 'streaming';
        return actRow(key, live ? '<span class="ct-spin"></span>' : icon('sparkle'), live ? 'Thinking…' : 'Thought', esc(firstLine(b.text)),
          live ? since(b.t0) : esc(fmtMs(b.ms)), () => `<div class="ct-act-text">${esc(b.text)}</div>`, `k-think${live ? ' live' : ''}`, live);
      }
      case 'tools': return b.items.map((it) => actRow(keyOf(ti, bi, it.id), okIcon(it.ok), `<code class="ct-fn">${esc(it.name)}</code>`,
        esc(it.summary || argsShort(it.args)), esc(fmtMs(it.ms)), () => itemDetail(it), `k-tool${it.ok === false ? ' bad' : ''}${it.ok === null ? ' live' : ''}`)).join('');
      case 'search': {
        const info = b.blocked ? 'nothing was sent' : [b.engine, plural(b.results.length, 'result')].filter(Boolean).join(' · ');
        const body = (): string => (b.blocked ? `<div class="ct-dim">${esc(b.reasons.length ? `It would have sent: ${b.reasons.join(', ')}. Your web search level keeps that private.` : 'Your web search level keeps this query private.')}</div>` : '') +
          (b.results.length ? `<ul class="ct-src">${b.results.map((r) => { const u = safeUrl(r.url); return `<li>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>` : esc(r.title)}<span class="ct-dim">${esc(hostOf(r.url))}</span></li>`; }).join('')}</ul>` : '') +
          (!b.blocked && !b.results.length ? '<span class="ct-dim">no results</span>' : '');
        return actRow(key, icon(b.blocked ? 'shield' : 'globe'), b.blocked ? 'Search blocked' : 'Searched', `<q>${esc(b.query)}</q> · ${esc(info)}`, '', body, `k-search${b.blocked ? ' warn' : ''}`);
      }
      case 'skill': return actRow(null, icon('layers'), 'Loaded skill', `<b>${esc(b.name)}</b>`, '', null, 'k-skill');
      case 'retry': return actRow(key, icon('refresh'), `Retry ${b.attempt}/${b.max}`, esc(firstLine(b.reason)), `${esc(String(b.waitS))} s`,
        () => `<div class="ct-act-text">${esc(b.reason)}</div>`, 'k-retry warn');
      case 'notice': return actRow(key, icon('circle'), 'Note', esc(firstLine(b.text)), '', () => `<div class="ct-md">${md(b.text)}</div>`, 'k-note');
      case 'text': return actRow(key, icon('text'), 'Said', esc(firstLine(b.text)), '', () => `<div class="ct-md">${md(b.text)}</div>`, 'k-said');
      default: return '';
    }
  }

  /** what the model is doing right now (the live header) */
  function nowDoing(t: AiTurn): string {
    if (t.blocks.some((x) => x.k === 'confirm' && (x.state === 'pending' || x.state === 'sent'))) return 'Waiting for your OK';
    const lb = t.blocks[t.blocks.length - 1];
    if (lb?.k === 'think' && lb.ms === null) return 'Thinking…';
    for (let i = t.blocks.length - 1; i >= 0; i--) {
      const x = t.blocks[i];
      const run = x?.k === 'tools' ? x.items.find((it) => it.ok === null) : undefined;
      if (run) return `Running <code>${esc(run.name)}</code>…${run.summary ? ` <span class="ct-dim">${esc(run.summary)}</span>` : ''}`;
    }
    if (lb?.k === 'retry') return `Retrying ${lb.attempt}/${lb.max}… <span class="ct-dim">${esc(firstLine(lb.reason).slice(0, 80))}</span>`;
    if (lb?.k === 'text') return 'Writing the answer…';
    if (!t.meta) return 'Connecting…';
    return `Waiting for <code>${esc(shortModel(t.meta.model))}</code>…`;
  }

  /** model · step · tokens in / out · speed (estimated while it streams, the real usage once done) */
  function statsHTML(t: AiTurn): string {
    const m = t.meta;
    const live = t.status === 'streaming';
    let out: number | null = t.usage?.out ?? null;
    let est = false;
    if (live || out == null) {
      const chars = t.blocks.reduce((n, x) => n + (x.k === 'text' || x.k === 'think' ? x.text.length : 0), 0);
      out = chars ? Math.round(chars / 4) : null;
      est = true;
    }
    const secs = (t.ms ?? (live && t.t0 ? performance.now() - t.t0 : 0)) / 1000;
    const inN = t.usage?.in || m?.inEst || null;
    const parts = [
      m?.model ? `<code>${esc(shortModel(m.model))}</code>` : '',
      t.step ? `step ${t.step.n}/${t.step.max || '?'}` : '',
      inN ? `in ${t.usage?.in ? '' : '~'}${fmtK(inN)}` : '',
      out ? `out ${est ? '~' : ''}${fmtK(out)}` : '',
      out && secs > 0.5 ? `${(out / secs).toFixed(out / secs < 10 ? 1 : 0)} tok/s` : '',
    ].filter(Boolean);
    return parts.length ? `<div class="ct-stats num">${parts.join('<i aria-hidden="true">·</i>')}</div>` : '';
  }

  function activityHTML(t: AiTurn, ti: number, steps: Step[]): string {
    const live = t.status === 'streaming';
    if (!live && !steps.length) return '';
    const key = `${ti}.act`;
    const open = isOpen(key, false);
    const thinkMs = steps.reduce((n, s) => n + (s.b.k === 'think' ? s.b.ms ?? 0 : 0), 0);
    const nTools = steps.reduce((n, s) => n + (s.b.k === 'tools' ? s.b.items.length : 0), 0);
    const nSearch = steps.filter((s) => s.b.k === 'search').length;
    const bad = steps.reduce((n, s) => n + (s.b.k === 'tools' ? s.b.items.filter((x) => x.ok === false).length : 0), 0);
    const counts = [thinkMs ? `thought ${fmtMs(thinkMs)}` : '', nTools ? plural(nTools, 'tool') : '', nSearch ? `${nSearch} search${nSearch === 1 ? '' : 'es'}` : '']
      .filter(Boolean).join(' · ');
    const head = live
      ? `<span class="ct-spin"></span><span class="ct-act-now">${nowDoing(t)}</span><span class="grow"></span>` +
        `<span class="ct-dim num">${t.step ? `step ${t.step.n}/${t.step.max || '?'} · ` : ''}</span>${since(t.t0)}`
      : `${icon(t.status === 'error' ? 'alert' : 'sparkle')}<span class="ct-act-now">${t.status === 'stopped' ? 'Stopped' : t.status === 'error' ? 'Failed' : 'Worked'}` +
        `${t.ms != null ? ` ${esc(fmtMs(t.ms))}` : ''}</span><span class="ct-dim">${esc(counts)}</span>${bad ? `<span class="ct-badc">${bad} failed</span>` : ''}<span class="grow"></span>`;
    let body = '';
    if (open) {
      const m = t.meta;
      const sent = m ? actRow(null, icon('send'), `Sent to <code>${esc(shortModel(m.model))}</code>`,
        esc([m.provider, m.inEst ? `~${fmtK(m.inEst)} tokens in` : '', m.tools ? plural(m.tools, 'tool') : '', m.mode, m.effort ? `effort ${m.effort}` : ''].filter(Boolean).join(' · ')), '', null, 'k-sent') : '';
      const wait = live && !/^(Thinking|Running|Writing)/.test(nowDoing(t))
        ? actRow(null, '<span class="ct-spin"></span>', nowDoing(t), '', '', null, 'k-wait live') : '';
      body = `<div class="ct-act-list">${sent}${steps.map((s) => stepRows(s, t, ti)).join('')}${wait}</div>${statsHTML(t)}`;
    }
    return `<div class="ct-act${open ? ' open' : ''}${live ? ' live' : ''}">` +
      `<button type="button" class="ct-act-h" data-ca="fold" data-key="${key}" aria-expanded="${open}">${head}${icon(open ? 'chev-u' : 'chev-d')}</button>${body}</div>`;
  }

  function confirmHTML(b: Extract<Block, { k: 'confirm' }>, ti: number, bi: number): string {
    const pending = b.state === 'pending';
    const picks = C.picks.get(b.id) ?? new Set(b.actions.map((a) => a.id));
    const res = new Map(b.results.map((r) => [r.id, r]));
    const danger = b.actions.some((a) => a.risk === 'danger');
    const allWeb = b.actions.length > 0 && b.actions.every((a) => a.risk === 'web');
    const nOn = b.actions.filter((a) => picks.has(a.id)).length;
    const status = pending ? `<span class="ct-wait"><i aria-hidden="true"></i>waiting for you · ${since(b.t0)}</span>`
      : b.state === 'sent' ? '<span class="ct-dim">sent…</span>'
        : b.state === 'expired' ? '<span class="ct-dim">expired</span>'
          : b.state === 'decided' ? `<span class="ct-dim">${b.results.filter((r) => r.ok && !r.declined).length} done · ${b.results.filter((r) => r.declined).length} declined</span>`
            : `<span class="ct-dim">${esc(b.state)}: nothing changed</span>`;
    const rows = b.actions.map((a) => {
      const r = res.get(a.id);
      const mark = r ? (r.declined ? `<span class="ct-dim ct-decl">${icon('x')}</span>` : okIcon(r.ok))
        : `<input type="checkbox" data-cc="pick" data-ti="${ti}" data-bi="${bi}" value="${esc(a.id)}"${picks.has(a.id) ? ' checked' : ''}${pending ? '' : ' disabled'} aria-label="${esc(a.summary || a.tool)}">`;
      const qv = C.edits.get(`${b.id}:${a.id}`) ?? str(a.args['query']);
      const isWebQ = pending && typeof a.args['query'] === 'string' && (a.risk === 'web' || /web|search/.test(a.tool));
      return `<div class="ct-act r-${esc(a.risk)}${picks.has(a.id) || !pending ? '' : ' off'}"><label>${mark}<code class="ct-fn">${esc(a.tool)}</code>${badge(a.risk)}` +
        `<span class="ct-act-sum">${esc(a.summary || argsShort(a.args))}</span></label>` +
        (r ? `<span class="ct-res${r.ok || r.declined ? '' : ' bad'}">${esc(r.declined ? 'declined' : r.summary)}</span>${undoBtn(r.undoId, r.undone, r.ok && !r.declined, a.args)}` : '') +
        (isWebQ ? `<input class="ct-q" type="text" data-ci="q" data-cid="${esc(b.id)}" data-aid="${esc(a.id)}" value="${esc(qv)}" aria-label="Search query (edit before approving)">` : '') + '</div>';
    }).join('');
    return `<div class="ct-confirm ${b.state.replace(' ', '-')}${danger ? ' danger' : ''}" role="group" aria-label="Changes waiting for your confirmation">` +
      `<div class="ct-confirm-h">${icon(danger ? 'alert' : allWeb ? 'globe' : 'lock')}<b>${allWeb ? (pending ? `Search the web? (${b.actions.length})` : 'Web search')
        : `${esc(plural(b.actions.length, 'change'))}${pending ? (b.actions.length === 1 ? ' needs' : ' need') + ' your OK' : ''}`}</b>${status}</div>` +
      `<div class="ct-acts">${rows}</div>` +
      (pending ? `<div class="ct-confirm-f"><span class="ct-dim">${danger ? 'Dangerous changes are marked red' : allWeb ? 'Edit the query before it is sent' : 'Nothing changes until you approve'}</span>` +
        `<button type="button" data-ca="reject" data-ti="${ti}" data-bi="${bi}">Reject</button>` +
        `<button type="button" class="${danger ? 'danger' : 'primary'}" data-ca="approve" data-ti="${ti}" data-bi="${bi}"${nOn ? '' : ' disabled'}>${icon('check')}Approve ${nOn}</button></div>` : '') +
      (b.state === 'expired' ? `<div class="ct-confirm-f"><span class="ct-dim">This confirmation expired: nothing changed</span><button type="button" data-ca="ask-again" data-ti="${ti}" data-bi="${bi}">${icon('refresh')}Ask again</button></div>` : '') + '</div>';
  }

  function blockHTML(b: Block, t: AiTurn, ti: number, bi: number, lastText: boolean): string {
    switch (b.k) {
      case 'text': return lastText && t.status === 'streaming'
        ? `<div class="ct-md streaming">${mdLive(b.text)}</div>` : `<div class="ct-md">${md(b.text)}</div>`;
      case 'tools': return cardHTML(b, ti, bi);
      case 'think': case 'search': case 'skill': return '';                    // rows of the activity dropdown
      case 'todo': {
        const done = b.items.filter((x) => /done|complete/.test(x.state)).length;
        return `<div class="ct-todo"><div class="ct-lbl">Checklist · ${done}/${b.items.length}</div><ul>${b.items.map((x) => {
          const st = /done|complete/.test(x.state) ? 'done' : /progress|doing|active/.test(x.state) ? 'doing' : 'todo';
          return `<li class="${st}">${st === 'done' ? icon('check-c') : st === 'doing' ? '<span class="ct-spin"></span>' : icon('circle')}<span>${esc(x.text)}</span></li>`;
        }).join('')}</ul></div>`;
      }
      case 'confirm': return confirmHTML(b, ti, bi);
      case 'ask': return `<div class="ct-ask"><p>${esc(b.question)}</p><div class="ct-choices">${b.options.map((o) =>
        `<button type="button" class="ct-choice" data-ca="say" data-text="${esc(o)}"${b.answered ? ' disabled' : ''}>${esc(o)}</button>`).join('')}` +
        `<button type="button" class="ct-choice other" data-ca="focus"${b.answered ? ' disabled' : ''}>Something else…</button></div></div>`;
      case 'cannot': return `<div class="ct-cannot"><div class="h">${icon('x-c')}<b>I can't do that.</b><span>${esc(b.reason)}</span></div>` +
        (b.alternative ? `<div class="alt">${icon('sparkle')}<span>${esc(b.alternative)}</span><button type="button" data-ca="say" data-text="${esc(b.alternative)}">Try that</button></div>` : '') + '</div>';
      case 'plan': return `<div class="ct-plan ${b.state}"><div class="ct-plan-h">${icon('list')}<b>Plan</b><span>${esc(b.summary)}</span>` +
        `${b.state === 'approved' ? `<span class="ct-dim">${icon('check')}approved</span>` : ''}</div>` +
        `<ol>${b.steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ol>` +
        (b.state === 'pending' ? `<div class="ct-plan-f"><span class="ct-dim">Nothing has changed yet</span>` +
          `<button type="button" class="ghost" data-ca="plan-edit" data-ti="${ti}" data-bi="${bi}">${icon('edit')}Edit</button>` +
          `<button type="button" data-ca="plan-run" data-mode="ask" data-ti="${ti}" data-bi="${bi}" title="Run it; every change asks first">Approve · ask</button>` +
          `<button type="button" class="primary" data-ca="plan-run" data-mode="auto" data-ti="${ti}" data-bi="${bi}" title="Run it; changes run, dangerous ones still ask">${icon('play')}Approve · auto</button></div>` : '') + '</div>';
      case 'ui': if (b.applied && runCardOf(t, b.id)) return '';      // a run action: its tool card says it all
        return `<div class="ct-ui${b.applied ? ' applied' : ''}">${icon('scatter')}<span class="ct-ui-l">${esc(b.label)}</span><code class="ct-dim">${esc(b.action)}</code><span class="grow"></span>` +
        (b.applied ? `<span class="ct-okc">${icon('check')}applied</span>` : `<button type="button" class="primary" data-ca="ui-apply" data-ti="${ti}" data-bi="${bi}">Apply</button>`) + '</div>';
      case 'retry': return `<div class="ct-retry">${icon('refresh')}<span class="num">retry ${b.attempt}/${b.max}</span><span class="ct-rr">${esc(b.reason.slice(0, 140))}</span><span class="num">${esc(String(b.waitS))} s</span></div>`;
      case 'notice': return `<div class="ct-notice ${esc(b.level)}">${icon(b.level === 'warn' || b.level === 'error' ? 'alert' : 'circle')}<span>${md(b.text)}</span></div>`;
      case 'compact': return compactHTML(b, keyOf(ti, bi));
    }
  }

  /** the run card of a page action (id, or id.rN of a retry) */
  function runCardOf(t: AiTurn, id: string): boolean {
    const base = id.replace(/\.r\d+$/, '');
    return t.blocks.some((x) => x.k === 'tools' && x.items.some((i) => i.id === base && i.risk === 'run'));
  }

  /** the divider where older messages were folded into the chat's summary; the summary opens under it */
  function compactHTML(b: Extract<Block, { k: 'compact' }>, key: string): string {
    if (b.state === 'start') return `<div class="ct-compact live"><span class="ct-spin"></span><span>Compacting older messages…</span></div>`;
    if (b.state === 'failed') return `<div class="ct-compact bad">${icon('alert')}<span>${esc(b.summary || 'Could not compact the chat.')}</span></div>`;
    const open = isOpen(key, false);
    const what = `${b.auto ? 'Context compacted' : 'Compacted'} · ${esc(plural(b.turns, 'message'))}${b.before ? ` · ${fmtK(b.before)} → ${fmtK(b.after)} tokens` : ''}`;
    return `<div class="ct-compact${open ? ' open' : ''}"><button type="button" class="ct-compact-h" data-ca="fold" data-key="${key}" aria-expanded="${open}">` +
      `${icon('layers')}<span>${what}</span><span class="ct-compact-l">${open ? 'hide' : 'view'} summary</span></button>` +
      (open ? `<div class="ct-compact-b ct-md">${md(b.summary)}</div>` : '') + '</div>';
  }

  function aiHTML(t: AiTurn, ti: number): string {
    // steps (and the words said before the last of them) go into the activity dropdown; the answer, the cards that
    // change data and everything that waits for the user stay in the answer, in their order
    let lastStep = -1;
    t.blocks.forEach((b, i) => { if (isStep(b)) lastStep = i; });
    const steps: Step[] = [], rest: Step[] = [];
    t.blocks.forEach((b, bi) => (isStep(b) || (b.k === 'text' && bi < lastStep) ? steps : rest).push({ b, bi }));
    const lastText = rest.reduce((n, s) => (s.b.k === 'text' ? s.bi : n), -1);
    const body = activityHTML(t, ti, steps) +
      rest.map(({ b, bi }) => blockHTML(b, t, ti, bi, bi === lastText && bi === t.blocks.length - 1)).join('');
    const err = t.err ? `<div class="status-line err">${icon('alert')}<span>${esc(t.err)}</span>` +
      `<button type="button" class="ghost ct-mini" data-ca="retry" data-ti="${ti}">${icon('refresh')}Retry</button></div>` : '';
    const words = turnText(t);
    const foot = t.status !== 'streaming' && (words || t.usage || t.meta)
      ? `<div class="ct-foot num">${esc([t.meta?.model, t.usage ? `in ${fmtK(t.usage.in)} · out ${fmtK(t.usage.out)}` : '', fmtMs(t.ms),
        t.steps && t.steps > 1 ? plural(t.steps, 'step') : '', t.status === 'stopped' ? 'stopped' : ''].filter(Boolean).join(' · '))}` +
        (words ? `<button type="button" class="ghost ct-mini" data-ca="copy" data-ti="${ti}" title="Copy the answer as Markdown">Copy</button>` : '') +
        (t.kind === 'report' && words && !t.err ? `<button type="button" class="ghost ct-mini" data-ca="dl" data-ti="${ti}">${icon('download')}Download .md</button>` : '') + '</div>' : '';
    return `<div class="chat-turn ai ai-msg bot" data-ti="${ti}"><span class="chat-av" aria-hidden="true">${icon('gem')}</span>` +
      `<div class="chat-body">${body}${err}${foot}</div></div>`;
  }

  function entryHTML(e: Entry, ti: number): string {
    switch (e.role) {
      case 'user': return `<div class="chat-turn user" data-ti="${ti}"><div class="chat-bubble">${e.voice ? icon('mic') : ''}${esc(e.label || e.text)}</div></div>`;
      case 'ai': return aiHTML(e, ti);
      case 'note': return `<div class="chat-turn note ai-msg bot" data-ti="${ti}"><span class="chat-av" aria-hidden="true">${icon('gem')}</span>` +
        `<div class="chat-body">${e.level === 'err' ? `<div class="status-line err">${icon('alert')}<span>${esc(e.md)}</span></div>` : `<div class="ct-md ct-note">${md(e.md)}</div>`}</div></div>`;
      case 'custom': return `<div class="chat-turn ai ai-msg bot custom" data-ti="${ti}"><span class="chat-av" aria-hidden="true">${icon('gem')}</span><div class="chat-body">${e.html()}</div></div>`;
      case 'archive': {
        const key = `a.${ti}`;
        const open = isOpen(key, false);
        return `<div class="chat-turn archive" data-ti="${ti}"><div class="ct-archive${open ? ' open' : ''}">` +
          fold(key, open, `${IC_CLOCK}<span>Earlier chat from this browser · ${esc(plural(e.items.length, 'message'))} · read-only</span><span class="grow"></span>`) +
          (open ? `<div class="ct-arch-body">${e.items.map((m) => `<div class="ct-arch-m ${m.role === 'user' ? 'u' : 'a'}"><b>${m.role === 'user' ? 'You' : 'Assistant'}</b>${m.role === 'user' ? `<p>${esc(m.text)}</p>` : `<div class="ct-md">${md(m.text)}</div>`}</div>`).join('')}</div>` : '') + '</div></div>';
      }
    }
  }

  function emptyHTML(): string {
    const notes = (C.blocked ? `<div class="ct-notice error">${icon('alert')}<span>${esc(C.blocked)}</span></div>` : '') +
      (!C.blocked && C.willRemove ? `<div class="ct-notice warn">${icon('alert')}<span>Your first message starts a new chat and removes “${esc(C.willRemove.title)}” (not opened since ${esc(fmtDay(C.willRemove.last_opened_at))}).</span></div>` : '');
    const body = opts.emptyHTML ? opts.emptyHTML()
      : `<div class="chat-empty">${icon('gem')}<b>Ask, or tell me what to do.</b><p class="ct-dim">Type <kbd>/</kbd> for commands · <kbd>Shift</kbd>+<kbd>Tab</kbd> switches the mode</p></div>`;
    return notes + body;
  }

  // A streamed answer grows every frame: re-parsing all of it each time costs O(n²) over the answer. The finished
  // part (up to the last blank line outside a code fence) is parsed once and kept; only the open block is parsed
  // per frame. The final render (status done) parses the whole text as usual.
  const live = { prefix: '', html: '' };
  function mdLive(text: string): string {
    let cut = text.lastIndexOf('\n\n');
    while (cut > 0 && (text.slice(0, cut).match(/^```/gm)?.length ?? 0) % 2) cut = text.lastIndexOf('\n\n', cut - 1);
    if (cut <= 0) return md(text);
    const prefix = text.slice(0, cut);
    if (prefix !== live.prefix) { live.prefix = prefix; live.html = md(prefix); }
    return live.html + md(text.slice(cut + 2));
  }

  const nearBottom = (): boolean => logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;
  const toBottom = (): void => { logEl.scrollTop = logEl.scrollHeight; };

  function renderLog(): void {
    const has = C.turns.length > 0;
    logEl.innerHTML = has ? C.turns.map(entryHTML).join('') : emptyHTML();
    logEl.classList.toggle('is-empty', !has);
    toBottom();
  }

  /** Show turns from index `from` on (pushed at the end): appended to the log, the earlier ones stay as they are. */
  function appendLog(from: number): void {
    if (from === 0 || logEl.classList.contains('is-empty') || logEl.querySelectorAll(':scope > .chat-turn').length !== from) { renderLog(); return; }
    logEl.insertAdjacentHTML('beforeend', C.turns.slice(from).map((e, k) => entryHTML(e, from + k)).join(''));
    toBottom();
  }

  // a turn the user is selecting text in is not replaced (that would drop the selection mid-copy): it is held and
  // drawn once the selection leaves it
  const held = new Set<number>();
  const selectedIn = (el: Element): boolean => {
    const sel = document.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    const r = sel.getRangeAt(0);
    return el.contains(r.startContainer) || el.contains(r.endContainer);
  };
  const onSelChange = (): void => {
    for (const i of held) {
      const el = logEl.querySelector(`.chat-turn[data-ti="${i}"]`);
      if (el && selectedIn(el)) continue;
      held.delete(i);
      renderTurn(i);
    }
    if (!held.size) document.removeEventListener('selectionchange', onSelChange);
  };

  function renderTurn(i: number): void {
    const e = C.turns[i];
    const el = logEl.querySelector(`.chat-turn[data-ti="${i}"]`);
    if (!e || !el) { renderLog(); return; }
    if (selectedIn(el)) {
      if (!held.size) document.addEventListener('selectionchange', onSelChange);
      held.add(i);
      return;
    }
    const stick = nearBottom();
    // the open activity list scrolls on its own: keep its place, or follow the newest step when it was at the end
    const list = el.querySelector<HTMLElement>('.ct-act-list');
    const listTop = list ? list.scrollTop : 0, listEnd = !list || list.scrollHeight - list.scrollTop - list.clientHeight < 24;
    el.outerHTML = entryHTML(e, i);
    const next = logEl.querySelector<HTMLElement>(`.chat-turn[data-ti="${i}"] .ct-act-list`);
    if (next) next.scrollTop = listEnd ? next.scrollHeight : listTop;
    if (stick) toBottom();
  }

  function scheduleTurn(i: number): void {
    if (C.frame) return;
    C.frame = requestAnimationFrame(() => { C.frame = 0; renderTurn(i); });
  }

  // ---------------------------------------------------------------- render: composer
  function chip(ca: string, label: string, title: string, cls = '', extra = ''): string {
    return `<button type="button" class="chat-chip${cls ? ` ${cls}` : ''}" data-ca="${ca}" title="${esc(title)}"${extra}>${label}</button>`;
  }
  function renderChips(): void {
    const m = mode(), th = think(), w = web();
    chipsEl.innerHTML =
      chip('model', `${icon('model')}<span class="chat-chip-t">${esc(modelLabel())}</span>${icon('chev-d')}`, `${provider()?.name ?? ''} · pick the model (/model)`, 'model', ` aria-haspopup="listbox" aria-expanded="false" aria-controls="${uid}-model"`) +
      chip('mode', `<span class="chat-dot"></span>${esc(m)}`, `${MODE_HINT[role][m]} · click or Shift+Tab to switch`, `mode m-${m}`) +
      chip('effort', `<span class="ct-dim">effort</span>${esc(effort())}`, 'how hard it works: steps, thinking, a final check (/effort)') +
      chip('think', `<span class="ct-dim">think</span>${esc(th ?? 'auto')}`, th ? `thinking ${th} (/think)` : 'thinking follows the effort (/think)', th && th !== 'off' ? 'on' : '') +
      chip('web', `${icon('globe')}${esc(w)}`, WEB_HINT[w] + ' (/web)', w !== 'off' ? 'on' : '') +
      (opts.extraChips ? opts.extraChips() : '');
  }
  /** a short note above the box after a chip / command changed something (gone after 3.5 s) */
  function flash(text: string): void {
    hintEl.textContent = text;
    hintEl.hidden = false;
    hintEl.classList.remove('flash');
    void hintEl.offsetWidth;                   // restart the fade-in
    hintEl.classList.add('flash');
    clearTimeout(C.hintTimer);
    C.hintTimer = window.setTimeout(() => { hintEl.hidden = true; hintEl.textContent = ''; hintEl.classList.remove('flash'); }, 3500);
  }
  function renderSend(): void {
    const busy = !!C.busy;
    sendEl.innerHTML = icon(busy ? 'stop' : 'send');
    sendEl.classList.toggle('stop', busy);
    sendEl.dataset['ca'] = busy ? 'stop' : 'send';
    sendEl.setAttribute('aria-label', busy ? 'Stop' : 'Send');
    sendEl.title = busy ? 'Stop (Esc)' : 'Send (Enter)';
    root.setAttribute('aria-busy', String(busy));
    logEl.setAttribute('aria-busy', String(busy));
  }
  function renderForced(): void {
    forcedEl.classList.toggle('hidden', !C.forced.length);
    forcedEl.innerHTML = C.forced.map((s) => `<span class="ct-skill">${icon('sparkle')}<span>next message uses skill</span><b>${esc(s)}</b>` +
      `<button type="button" class="icon-btn ghost" data-ca="unforce" data-name="${esc(s)}" aria-label="Do not use ${esc(s)}">${icon('x')}</button></span>`).join('');
  }
  function renderAll(): void { renderHead(); renderLog(); renderChips(); renderSend(); renderForced(); if (H.open) renderHist(); }

  function autosize(): void { ta.style.height = 'auto'; ta.style.height = `${Math.min(ta.scrollHeight + 2, 160)}px`; }

  // ---------------------------------------------------------------- prefs (remembered per role)
  function remember(): void {
    clearTimeout(C.cfgTimer);
    C.cfgTimer = window.setTimeout(() => {
      const p = C.prefs;
      const chat = { provider_id: p.provider_id ?? null, model: p.model ?? null, mode: mode(), effort: effort(), think: think(), web: web() };
      sendJSON('PUT', 'api/llm/config', { chat: { [role]: chat } }).catch(() => { /* not saved: the chips still apply to this page */ });
    }, CONFIG_DEBOUNCE);
  }
  function setMode(m: ChatMode, say = true): void {
    C.prefs.mode = m;
    renderChips();
    if (say) flash(`${MODE_HINT[role][m]} · Shift+Tab to switch`);
    remember();
    opts.onUpdate?.();
  }
  function setEffort(e: ChatEffort): void { C.prefs.effort = e; renderChips(); flash(`effort ${e}`); remember(); }
  function setThink(t: ThinkLevel | null): void { C.prefs.think = t; renderChips(); flash(t ? `thinking ${t}` : 'thinking follows the effort'); remember(); }
  function setWeb(w: WebLevel): void { C.prefs.web = w; renderChips(); flash(WEB_HINT[w]); remember(); }

  async function loadCatalog(): Promise<void> {
    try {
      const cat = await getJSON<ChatCatalog>(`api/llm/agent/${role}/catalog`);
      C.cat = cat;
      const saved = recOf(cat.chat);
      const p: ChatPrefs = { ...C.prefs };
      if (isMode(saved['mode'])) p.mode = saved['mode'];
      if (isEffort(saved['effort'])) p.effort = saved['effort'];
      if (isThink(saved['think'])) p.think = saved['think'];
      if (isWeb(saved['web'])) p.web = saved['web'];
      if (typeof saved['provider_id'] === 'string' && cat.providers.some((x) => x.id === saved['provider_id'] && x.connected)) {
        p.provider_id = saved['provider_id'];
        if (typeof saved['model'] === 'string' && saved['model']) p.model = saved['model'];
      }
      C.prefs = p;
    } catch (e) { /* no catalogue (older server): the built-in commands still work */ }
    renderChips();
    renderSend();
    opts.onUpdate?.();
  }

  // ---------------------------------------------------------------- sessions
  const sessionsGone = (e: unknown): boolean => isStatus(e, 404) || isStatus(e, 405) || isStatus(e, 501);

  function removedToast(r: SessionRef): void {
    actionToast({ title: 'Removed the oldest chat', body: esc(`“${r.title}”`), timeout: 8000, actions: [{ label: 'Undo', icon: 'undo', run: () => restore([r.id]) }] });
  }

  async function restore(ids: string[]): Promise<void> {
    try {
      await postJSON('api/llm/sessions/restore', { ids });
      toast(ids.length === 1 ? 'Chat restored' : `${ids.length} chats restored`, 'ok');
    } catch (e) {
      toast(isStatus(e, 409) ? `Could not restore: ${errText(e) || 'over the chat limit: delete another chat first'}` : `Could not restore: ${errText(e)}`, 'err');
    }
    if (H.open) void loadHist();
  }

  async function peekLimit(): Promise<void> {           // "your first message will remove …"
    if (C.sessionsOK === false) return;
    try {
      const r = await getJSON<SessionList>(`api/llm/sessions?role=${role}&limit=1`);
      C.sessionsOK = true;
      C.willRemove = r.count >= r.max ? r.will_remove : null;
      C.blocked = r.all_pinned ? `All ${r.max} chats are pinned: unpin or delete one to start a new chat.` : null;
      if (!C.turns.length) renderLog();
    } catch (e) { if (sessionsGone(e)) C.sessionsOK = false; }
  }

  function newChat(): void {
    if (C.busy) stop();
    C.turns = [];
    C.sid = null;
    C.title = '';
    C.pinned = false;
    C.renaming = false;
    C.delCur = false;
    C.blocked = null;
    C.forced = [];
    C.open.clear();
    C.picks.clear();
    C.edits.clear();
    renderAll();
    void peekLimit();
    ta.focus();
  }

  function traceTurn(m: { text: string; trace: unknown; usage: unknown }): AiTurn {
    let t = newTurn(0);
    for (const ev of arr(m.trace)) if (isRec(ev) && typeof ev['type'] === 'string') t = reduce(t, { ...ev, type: ev['type'], _t: 0 });
    if (!t.blocks.some((b) => b.k === 'text') && m.text && !t.err) t = { ...t, blocks: [...t.blocks, { k: 'text', text: m.text, step: t.step?.n ?? 0 }] };
    const u = recOf(m.usage);
    if (!t.usage && (u['in'] != null || u['out'] != null)) t = { ...t, usage: { in: num(u['in']), out: num(u['out']) } };
    return reduce(t, { type: 'x_expire' });
  }

  async function openSession(id: string, quiet = false): Promise<boolean> {
    if (C.busy) stop();
    try {
      const r = await getJSON<SessionDetail>(`api/llm/sessions/${encodeURIComponent(id)}`);
      C.turns = r.messages.map((m): Entry => (m.role === 'user' ? { role: 'user', text: m.text, label: null, voice: false } : traceTurn(m)));
      // a /compact (not in any trace): its divider goes after the last message it covered
      const cov = r.session.summary_seq ?? null;
      const shown = C.turns.some((e) => e.role === 'ai' && e.blocks.some((x) => x.k === 'compact' && x.covered === cov));
      if (cov != null && r.session.summary && !shown) {
        const at = r.messages.findIndex((m) => m.seq > cov);
        C.turns.splice(at < 0 ? C.turns.length : at, 0, compactTurn({ type: 'compact', state: 'done', auto: false, summary: r.session.summary, covered: cov }));
      }
      C.sid = r.session.id;
      C.title = r.session.title;
      C.pinned = !!r.session.pinned;
      C.renaming = false;
      C.delCur = false;
      C.blocked = null;
      C.open.clear();
      closeHist();
      renderAll();
      if (!quiet) ta.focus();
      return true;
    } catch (e) {
      if (!quiet) toast(`Could not open the chat: ${errText(e)}`, 'err');
      return false;
    }
  }

  async function renameSession(id: string, title: string): Promise<void> {
    const t = title.trim().slice(0, 120);
    if (!t) return;
    try {
      await sendJSON('PATCH', `api/llm/sessions/${encodeURIComponent(id)}`, { title: t });
      if (id === C.sid) { C.title = t; renderHead(); }
      const row = H.items.find((x) => x.id === id);
      if (row) row.title = t;
    } catch (e) { toast(`Could not rename: ${errText(e)}`, 'err'); }
  }

  async function pinSession(id: string, pinned: boolean): Promise<void> {
    try {
      await sendJSON('PATCH', `api/llm/sessions/${encodeURIComponent(id)}`, { pinned });
      if (id === C.sid) { C.pinned = pinned; renderHead(); }
      const row = H.items.find((x) => x.id === id);
      if (row) row.pinned = pinned;
      renderHistBody();
    } catch (e) { toast(`Could not ${pinned ? 'pin' : 'unpin'}: ${errText(e)}`, 'err'); }
  }

  /** Delete sessions (ids, or every one in this scope / of this role); toast with Undo for 8 s. */
  async function deleteSessions(what: { ids: string[] } | { all: true; scoped: boolean; includePinned: boolean }): Promise<void> {
    let ids: string[] = [];
    try {
      if ('ids' in what && what.ids.length === 1) {
        const one = what.ids[0] ?? '';
        if (one === C.sid && C.busy) stop();
        await sendJSON('DELETE', `api/llm/sessions/${encodeURIComponent(one)}`);
        ids = [one];
      } else {
        const body: Record<string, unknown> = { role };
        if ('ids' in what) body['ids'] = what.ids;
        else {
          if (what.scoped) body['scope'] = opts.sessionScope?.() ?? {};
          body['include_pinned'] = what.includePinned;
        }
        if (C.busy && ('ids' in what ? what.ids.includes(C.sid ?? '') : true)) stop();
        const r = await postJSON<{ deleted: string[] }>('api/llm/sessions/delete', body);
        ids = r.deleted ?? [];
      }
    } catch (e) {
      toast(`Could not delete: ${errText(e)}`, 'err');
      return;
    }
    if (C.sid && ids.includes(C.sid)) newChat();
    H.picked.clear();
    H.confirm = null;
    if (H.open) void loadHist();
    if (ids.length) {
      actionToast({ title: ids.length === 1 ? 'Chat deleted' : `${ids.length} chats deleted`, timeout: 8000,
        actions: [{ label: 'Undo', icon: 'undo', run: () => restore(ids) }] });
    } else toast('Nothing to delete');
  }

  // ---------------------------------------------------------------- history panel
  async function loadHist(more = false): Promise<void> {
    H.loading = true;
    H.err = null;
    if (!more) renderHistBody();
    const p = new URLSearchParams({ role });
    const ss = opts.sessionScope?.();
    if (!H.all && ss) for (const [k, v] of Object.entries(ss)) if (v) p.set(k, v);
    if (H.q) p.set('q', H.q);
    if (more && H.next) p.set('cursor', H.next);
    try {
      const r = await getJSON<SessionList>(`api/llm/sessions?${p.toString()}`);
      C.sessionsOK = true;
      H.items = more ? [...H.items, ...r.sessions] : r.sessions;
      H.count = r.count;
      H.max = r.max || 20;
      H.next = r.next_cursor;
      H.willRemove = r.will_remove;
    } catch (e) {
      if (sessionsGone(e)) { C.sessionsOK = false; H.err = 'Chat history is not available on this server yet.'; } else H.err = errText(e);
    }
    H.loading = false;
    renderHistBody();
  }

  function openHist(qText = ''): void {
    H.open = true;
    H.q = qText;
    H.select = false;
    H.picked.clear();
    H.confirm = null;
    H.renaming = null;
    histEl.classList.remove('hidden');
    renderHist();
    renderHead();
    void loadHist();
    histEl.querySelector<HTMLInputElement>('.chat-hist-q')?.focus();
  }
  function closeHist(): void {
    if (!H.open) return;
    H.open = false;
    histEl.classList.add('hidden');
    renderHead();
    headEl.querySelector<HTMLElement>('[data-ca="history"]')?.focus();
  }

  function renderHist(): void {
    const sk = scopeKey();
    histEl.innerHTML = `<div class="chat-hist-h">${IC_CLOCK}<b>History</b><span class="chat-hist-n ct-dim num"></span><span class="grow"></span>` +
      `<button type="button" class="ghost" data-ca="h-select" aria-pressed="false">Select</button>` +
      `<button type="button" class="icon-btn ghost" data-ca="h-close" aria-label="Close the history" title="Close (Esc)">${icon('x')}</button></div>` +
      `<div class="chat-hist-tools"><span class="chat-hist-search">${icon('search')}<input class="chat-hist-q" type="search" data-ci="h-q" placeholder="Search chats…" value="${esc(H.q)}" aria-label="Search chats"></span>` +
      (sk ? `<span class="chat-seg" role="group" aria-label="Which chats"><button type="button" data-ca="h-scope" data-all="0" aria-pressed="${!H.all}">This ${esc(scopeWord)}</button>` +
        `<button type="button" data-ca="h-scope" data-all="1" aria-pressed="${H.all}">All</button></span>` : '') + '</div>' +
      '<div class="chat-hist-body"></div>';
    renderHistBody();
  }

  function rowHTML(s: ChatSession): string {
    const cur = s.id === C.sid;
    const meta = [opts.scopeLabel ? opts.scopeLabel(s.scope) : '', s.model ?? '', fmtWhen(s.last_opened_at || s.updated_at), s.n_messages ? `${s.n_messages}` : ''].filter(Boolean);
    const main = H.renaming === s.id
      ? `<input class="chat-hren" data-ci="h-ren" data-id="${esc(s.id)}" value="${esc(s.title)}" aria-label="New title" maxlength="120">`
      : `<button type="button" class="chat-hopen" data-ca="h-open" data-id="${esc(s.id)}"${cur ? ' aria-current="true"' : ''}>` +
        `<span class="t">${s.pinned ? IC_PIN : ''}${esc(s.title || 'Untitled chat')}</span><span class="m num">${meta.map(esc).join(' · ')}</span></button>`;
    const acts = H.confirm === `row:${s.id}`
      ? `<span class="chat-hconf" role="group" aria-label="Delete this chat?">Delete this chat?<button type="button" class="danger" data-ca="h-del-yes" data-id="${esc(s.id)}">Yes</button><button type="button" data-ca="h-del-no">No</button></span>`
      : `<span class="chat-hacts"><button type="button" class="icon-btn ghost" data-ca="h-ren" data-id="${esc(s.id)}" aria-label="Rename" title="Rename">${icon('edit')}</button>` +
        `<button type="button" class="icon-btn ghost${s.pinned ? ' on' : ''}" data-ca="h-pin" data-id="${esc(s.id)}" aria-pressed="${s.pinned}" aria-label="${s.pinned ? 'Unpin' : 'Pin'}" title="${s.pinned ? 'Unpin' : 'Pin: never removed automatically'}">${IC_PIN}</button>` +
        `<button type="button" class="icon-btn ghost" data-ca="h-del" data-id="${esc(s.id)}" aria-label="Delete" title="Delete">${icon('trash')}</button></span>`;
    return `<div class="chat-hrow${cur ? ' cur' : ''}${H.picked.has(s.id) ? ' picked' : ''}" data-id="${esc(s.id)}">` +
      (H.select ? `<input type="checkbox" data-cc="h-pick" value="${esc(s.id)}"${H.picked.has(s.id) ? ' checked' : ''} aria-label="Select ${esc(s.title)}">` : '') + main + acts + '</div>';
  }

  function renderHistBody(): void {
    if (!H.open) return;
    const body = histEl.querySelector('.chat-hist-body');
    if (!body) return;
    const n = histEl.querySelector('.chat-hist-n');
    if (n) n.textContent = H.err ? '' : `${H.count} / ${H.max}`;
    const sel = histEl.querySelector('[data-ca="h-select"]');
    sel?.setAttribute('aria-pressed', String(H.select));
    for (const b of histEl.querySelectorAll<HTMLElement>('[data-ca="h-scope"]')) b.setAttribute('aria-pressed', String((b.dataset['all'] === '1') === H.all));
    if (H.err) { body.innerHTML = `<div class="chat-hist-empty">${icon('alert')}<span>${esc(H.err)}</span></div>`; return; }
    const groups = new Map<string, ChatSession[]>();
    for (const s of H.items) {
      const g = s.pinned ? 'Pinned' : dateGroup(s.last_opened_at || s.updated_at);
      const list = groups.get(g) ?? [];
      list.push(s);
      groups.set(g, list);
    }
    const order = ['Pinned', 'Today', 'Yesterday', 'Last 7 days', 'Older'];
    const list = order.filter((g) => groups.has(g)).map((g) => `<div class="chat-hist-g">${g === 'Pinned' ? IC_PIN : ''}${esc(g)}</div>${(groups.get(g) ?? []).map(rowHTML).join('')}`).join('');
    const notice = H.willRemove && H.count >= H.max
      ? `<div class="ct-notice warn">${icon('alert')}<span>New chat will remove “${esc(H.willRemove.title)}” (not opened since ${esc(fmtDay(H.willRemove.last_opened_at))}). Pin the chats you want to keep.</span></div>` : '';
    const nPick = H.picked.size;
    const selBar = H.select ? `<div class="chat-hist-bar"><span class="num">${nPick} selected</span><span class="grow"></span>` +
      (H.confirm === 'sel' ? `<span class="chat-hconf">Delete ${esc(plural(nPick, 'chat'))}?<button type="button" class="danger" data-ca="h-sel-yes">Yes</button><button type="button" data-ca="h-del-no">No</button></span>`
        : `<button type="button" class="danger" data-ca="h-sel-del"${nPick ? '' : ' disabled'}>${icon('trash')}Delete ${nPick}</button>`) + '</div>' : '';
    const sk = scopeKey();
    const allConfirm = H.confirm === 'all' || H.confirm === 'all-scope';
    const foot = H.items.length || allConfirm ? `<div class="chat-hist-f">` + (allConfirm
      ? `<span class="chat-hconf">Delete all chats${H.confirm === 'all-scope' ? ` in this ${esc(scopeWord)}` : ''}?` +
        `<label class="ct-dim"><input type="checkbox" data-cc="h-incl"${H.includePinned ? ' checked' : ''}>include pinned</label>` +
        `<button type="button" class="danger" data-ca="h-all-yes">Yes</button><button type="button" data-ca="h-del-no">No</button></span>`
      : (sk ? `<button type="button" class="ghost ct-danger-t" data-ca="h-all" data-scoped="1">Delete all in this ${esc(scopeWord)}</button>` : '') +
        `<button type="button" class="ghost ct-danger-t" data-ca="h-all" data-scoped="0">Delete all</button>`) + '</div>' : '';
    body.innerHTML = notice +
      (H.loading && !H.items.length ? '<div class="chat-hist-empty"><span class="ct-spin"></span>Loading…</div>'
        : H.items.length ? `<div class="chat-hist-list">${list}</div>` + (H.next ? `<button type="button" class="ghost chat-hist-more" data-ca="h-more">Load more</button>` : '')
          : `<div class="chat-hist-empty">${IC_CLOCK}<span>${H.q ? 'No chat matches.' : 'No chats yet: they appear here after your first message.'}</span></div>`) +
      selBar + foot;
    if (H.renaming) { const i = body.querySelector<HTMLInputElement>('.chat-hren'); i?.focus(); i?.select(); }
  }

  // ---------------------------------------------------------------- talking
  function onSessionEvent(ev: Record<string, unknown>): void {
    const sid = strOrNull(ev['session_id']);
    if (sid) { C.sid = sid; C.sessionsOK = true; }
    if (typeof ev['title'] === 'string' && ev['title']) C.title = ev['title'];
    renderHead();
    const rm = isRec(ev['removed']) ? ev['removed'] : null;
    if (rm && typeof rm['id'] === 'string') removedToast({ id: rm['id'], title: str(rm['title']) });
  }

  async function send(text0: string, o: { label?: string; kind?: string; raw?: boolean; extra?: Record<string, unknown> } = {}): Promise<void> {
    const text = text0.trim();
    if (!text || C.busy) return;
    if (!o.raw) {
      const cmd = parseCommand(text);
      if (cmd && (commands().some((c) => c.name === cmd.name) || DEFAULT_COMMANDS.some((c) => c.name === cmd.name))) { runCommand(cmd.name, cmd.arg); return; }
      if (cmd && text.indexOf(' ') < 0) { note(`Unknown command \`/${cmd.name}\`. Type \`/help\` for the list.`); return; }
    }
    closeSlash();
    const before = C.turns;
    C.turns = C.turns.map((e) => (e.role === 'ai' ? reduce(e, { type: 'x_answered' }) : e));
    const changed = C.turns.flatMap((e, i) => (e !== before[i] ? [i] : []));
    const ac = new AbortController();
    C.busy = ac;
    C.blocked = null;
    renderSend();
    C.turns.push({ role: 'user', text, label: o.label ?? null, voice: false });
    C.turns.push(newTurn(performance.now(), o.kind ?? null));
    const idx = C.turns.length - 1;
    C.live = idx;
    for (const i of changed) renderTurn(i);         // an answered question / confirm folds; the rest stays in place
    appendLog(idx - 1);
    startTicker();
    // the server keeps the history: session_id continues a chat, none starts one (a 'session' event names it)
    const body: Record<string, unknown> = { text, scope: opts.scope(), mode: mode(), effort: effort(), web: web() };
    if (C.sid) body['session_id'] = C.sid;
    if (think()) body['think'] = think();
    if (C.prefs.provider_id) body['model'] = { provider_id: C.prefs.provider_id, model: C.prefs.model ?? '' };
    if (C.forced.length) body['skills'] = C.forced;
    if (opts.onUiAction) body['ui_ack'] = true;     // the page reports how its page actions ended
    if (o.extra) Object.assign(body, o.extra);
    C.forced = [];
    renderForced();
    const apply = (ev: ChatEvent): void => { const t = aiAt(idx); if (t) C.turns[idx] = reduce(t, ev); };
    try {
      await streamNDJSON(`api/llm/agent/${role}/chat`, body, (v) => {
        if (!isRec(v) || typeof v['type'] !== 'string') return;
        const ev: ChatEvent = { ...v, type: v['type'], _t: performance.now() };
        if (ev.type === 'ping') return;
        if (ev.type === 'session') { onSessionEvent(ev); return; }
        if (ev.type === 'meta') C.runId = strOrNull(ev['run_id']);
        apply(ev);
        sideEffects(idx, ev);
        opts.onEvent?.(ev);
        if (ev.type === 'confirm' || ev.type === 'confirmed' || ev.type === 'plan_proposal' || ev.type === 'ask') { renderTurn(idx); opts.onUpdate?.(); } else scheduleTurn(idx);
      }, ac.signal);
      apply({ type: 'x_empty' });
    } catch (e) {
      apply(isAbort(e) ? { type: 'x_stopped' } : { type: 'x_failed', detail: errText(e) });
    } finally {
      if (C.frame) { cancelAnimationFrame(C.frame); C.frame = 0; }
      if (C.busy === ac) C.busy = null;
      C.runId = null;
      C.live = -1;
      stopTicker();
      renderTurn(idx);
      renderSend();
      const t = aiAt(idx);
      if (t) opts.onDone?.(t);
      opts.onUpdate?.();
    }
  }

  function sideEffects(idx: number, ev: ChatEvent): void {
    if (ev.type === 'tool_end' && ev['ok'] !== false && (ev['risk'] === 'change' || ev['risk'] === 'danger')) opts.onChange?.(ev);
    if (ev.type === 'ui_action' && ev['applied'] && opts.onUiAction) {
      const t = aiAt(idx);
      const b = t?.blocks.find((x): x is Extract<Block, { k: 'ui' }> => x.k === 'ui' && x.id === str(ev['id']));
      const runId = C.runId;
      if (b) void Promise.resolve().then(() => opts.onUiAction?.(b)).then(asResult, (e: unknown) => ({ ok: false, detail: errText(e) }))
        .then((r) => { if (ev['wait'] && runId) void postJSON('api/llm/agent/ui-result', { run_id: runId, action_id: b.id, ...r }).catch(() => {}); });
    }
  }

  function stop(): void {
    if (C.runId) postJSON('api/llm/agent/stop', { run_id: C.runId }).catch(() => { /* the abort below ends it anyway */ });
    C.busy?.abort();
  }

  function startTicker(): void {
    stopTicker();
    C.ticker = window.setInterval(() => {
      const now = performance.now();
      const scope = (C.live >= 0 && logEl.querySelector(`.chat-turn[data-ti="${C.live}"]`)) || root;   // only the live turn ticks
      for (const el of scope.querySelectorAll<HTMLElement>('[data-since]')) el.textContent = fmtSince(Math.max(0, now - Number(el.dataset['since'])));
    }, 250);
  }
  function stopTicker(): void { if (C.ticker) { clearInterval(C.ticker); C.ticker = 0; } }

  // ---------------------------------------------------------------- local turns
  function note(markdown: string, level: 'info' | 'err' = 'info'): void {
    C.turns.push({ role: 'note', md: markdown, level });
    appendLog(C.turns.length - 1);
  }

  function exportMd(): string {
    const lines = [`# ${C.title || 'Chat'}`, ''];
    for (const e of C.turns) {
      if (e.role === 'user') lines.push(`**You:** ${e.text}`, '');
      else if (e.role === 'ai') {
        const w = turnText(e);
        const tools = e.blocks.flatMap((b) => (b.k === 'tools' ? b.items.map((i) => `- \`${i.name}\` ${i.summary}`) : []));
        lines.push(`**Assistant${e.meta?.model ? ` (${e.meta.model})` : ''}:**`, '');
        if (tools.length) lines.push(...tools, '');
        if (w) lines.push(w, '');
        if (e.err) lines.push(`> error: ${e.err}`, '');
      }
    }
    return lines.join('\n');
  }

  function download(text: string, suffix: string): void {
    const stem = (opts.exportName?.() || C.title || `${role}-chat`).replace(/[^\w.-]+/g, '_').slice(0, 80) || 'chat';
    saveFile(text, `${stem}${suffix}.md`, 'text/markdown');
  }

  function compactTurn(ev: ChatEvent): AiTurn { return { ...reduce(newTurn(0), ev), status: 'done' }; }

  /** /compact [focus]: fold this chat's older messages into its summary now */
  async function compactNow(focus: string): Promise<void> {
    if (C.busy) { note('Wait for the answer to finish, then compact.'); return; }
    if (!C.sid) { note('Nothing to compact yet: this chat has no saved messages.'); return; }
    C.turns.push(compactTurn({ type: 'compact', state: 'start', auto: false }));
    const idx = C.turns.length - 1;
    appendLog(idx);
    const body: Record<string, unknown> = { session_id: C.sid };
    if (focus) body['focus'] = focus;
    if (C.prefs.provider_id) body['model'] = { provider_id: C.prefs.provider_id, model: C.prefs.model ?? '' };
    try {
      const r = await postJSON<Record<string, unknown>>(`api/llm/agent/${role}/compact`, body);
      C.turns[idx] = r['compacted'] ? compactTurn({ ...r, type: 'compact', state: 'done', auto: false })
        : { role: 'note', md: 'Nothing to compact yet: the chat is still short.', level: 'info' };
    } catch (e) {
      C.turns[idx] = compactTurn({ type: 'compact', state: 'failed', auto: false, summary: `Could not compact: ${errText(e)}` });
    }
    renderTurn(idx);
    opts.onUpdate?.();
  }

  function costText(): string {
    let i = 0, o = 0, ms = 0, n = 0;
    for (const e of C.turns) if (e.role === 'ai' && e.usage) { i += e.usage.in; o += e.usage.out; ms += e.ms ?? 0; n++; }
    return n ? `This chat used **${fmtK(i)}** tokens in and **${fmtK(o)}** out over ${plural(n, 'answer')} (${fmtMs(ms)} in total).`
      : 'No tokens used in this chat yet.';
  }

  // ---------------------------------------------------------------- commands
  function runCommand(name: string, arg: string): void {
    closeSlash();
    ta.value = '';
    autosize();
    const a = arg.toLowerCase();
    switch (name) {
      case 'model': openModel(); break;
      case 'mode': setMode(isMode(a) ? a : nextMode(mode())); break;
      case 'effort': setEffort(isEffort(a) ? a : cycle(EFFORTS, effort())); break;
      case 'think': setThink(a === 'on' ? 'medium' : a === 'auto' ? null : isThink(a) ? a : cycle(THINKS, think())); break;
      case 'web': {
        const lvl = recOf(C.cat?.web)['level'];
        setWeb(a === 'on' ? (isWeb(lvl) && lvl !== 'off' ? lvl : 'public') : isWeb(a) ? a : cycle(WEBS, web()));
        break;
      }
      case 'skills': {
        const sk = C.cat?.skills ?? [];
        note(sk.length ? `**Skills** (use one with \`/skill <name>\`):\n\n${sk.map((s) => `- \`${s.name}\` ${s.description}${s.source && s.source !== 'builtin' ? ` _(${s.source})_` : ''}`).join('\n')}` : 'No skills are available.');
        break;
      }
      case 'skill': {
        const sk = C.cat?.skills ?? [];
        const hit = sk.find((s) => s.name.toLowerCase() === a) ?? (a ? sk.find((s) => s.name.toLowerCase().startsWith(a)) : undefined);
        if (!hit) { note(a ? `No skill \`${arg}\`. \`/skills\` lists them.` : 'Which skill? `/skills` lists them.'); break; }
        C.forced = [hit.name];
        renderForced();
        flash(`the next message uses the skill ${hit.name}`);
        break;
      }
      case 'sessions': openHist(); break;
      case 'resume': openHist(arg); break;
      case 'new': case 'clear': newChat(); break;
      case 'rename':
        if (!C.sid) { note('Send a message first: a chat gets its title from it.'); break; }
        if (arg) void renameSession(C.sid, arg); else { C.renaming = true; renderHead(); }
        break;
      case 'delete':
        if (C.sid) void deleteSessions({ ids: [C.sid] }); else newChat();
        break;
      case 'cost': note(costText()); break;
      case 'compact': void compactNow(arg); break;
      case 'export':
        if (!C.turns.some((e) => e.role === 'user' || e.role === 'ai')) { note('Nothing to export yet.'); break; }
        download(exportMd(), '');
        break;
      case 'prompts': case 'plugins':
        if (opts.settings) opts.settings(name); else note('Settings are not available here.');
        break;
      case 'help':
        note(`**Commands**\n\n| | |\n|---|---|\n${commands().map((c) => `| \`/${c.name}${c.args ? ` ${c.args}` : ''}\` | ${c.description} |`).join('\n')}\n\n` +
          '**Keys:** Enter sends · Shift+Enter new line · Shift+Tab switches ask → auto → plan · Esc stops / leaves the box.');
        break;
      default: note(`Unknown command \`/${name}\`.`);
    }
    ta.focus();
  }

  // ---------------------------------------------------------------- slash popover
  function slashItems(v: string): Array<{ name: string; args: string; description: string }> {
    const w = v.slice(1).toLowerCase();
    const all = commands();
    const starts = all.filter((c) => c.name.startsWith(w));
    return [...starts, ...all.filter((c) => !starts.includes(c) && c.name.includes(w))];
  }
  function renderSlash(): void {
    slashPop.innerHTML = C.slash.items.map((c, i) => `<button type="button" role="option" id="${uid}-cmd-${i}" class="chat-cmd${i === C.slash.i ? ' active' : ''}" aria-selected="${i === C.slash.i}" data-cmd="${esc(c.name)}" tabindex="-1">` +
      `<code>/${esc(c.name)}</code>${c.args ? `<span class="ct-dim">${esc(c.args)}</span>` : ''}<span class="d">${esc(c.description)}</span></button>`).join('');
    ta.setAttribute('aria-activedescendant', `${uid}-cmd-${C.slash.i}`);
    slashPop.querySelector('.active')?.scrollIntoView({ block: 'nearest' });
  }
  function updateSlash(): void {
    const v = ta.value;
    if (!/^\/[\w-]*$/.test(v)) { closeSlash(); return; }
    C.slash.items = slashItems(v);
    if (!C.slash.items.length) { closeSlash(); return; }
    C.slash.i = Math.min(C.slash.i, C.slash.items.length - 1);
    renderSlash();
    if (!C.slash.open) {
      C.slash.open = true;
      slashPop.style.minWidth = `${Math.min(ta.offsetWidth, 520)}px`;
      C.slash.close = floatPopover(slashPop, ta, () => { C.slash.open = false; C.slash.close = null; ta.removeAttribute('aria-activedescendant'); }, 'left');
    }
  }
  function closeSlash(): void { C.slash.close?.(); }
  function pickSlash(i: number, run: boolean): void {
    const c = C.slash.items[i];
    if (!c) return;
    closeSlash();
    if (c.args && !run) { ta.value = `/${c.name} `; autosize(); ta.focus(); return; }
    if (c.args && /^</.test(c.args)) { ta.value = `/${c.name} `; autosize(); ta.focus(); return; }
    runCommand(c.name, '');
  }
  slashPop.addEventListener('mousedown', (e) => e.preventDefault());       // keep the focus in the textarea
  slashPop.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-cmd]') : null;
    if (!b) return;
    const i = C.slash.items.findIndex((c) => c.name === b.dataset['cmd']);
    pickSlash(i, true);
  });

  // ---------------------------------------------------------------- model popover
  function renderModelPop(): void {
    const ps = (C.cat?.providers ?? []).filter((p) => p.connected);
    const cur = provider();
    if (!ps.length) { modelPop.innerHTML = '<div class="chat-pop-empty">No connected provider: add and test one in Settings › AI assistant.</div>'; return; }
    modelPop.innerHTML = ps.map((p) => {
      const ms = C.models.get(p.id);
      const list = ms === 'loading' || ms === undefined ? '<div class="chat-pop-empty"><span class="ct-spin"></span>loading models…</div>'
        : Array.isArray(ms) ? ms.map((m) => {
          const on = cur?.id === p.id && (C.prefs.model || p.model) === m;
          return `<button type="button" role="option" class="chat-mopt${on ? ' on' : ''}" aria-selected="${on}" data-pid="${esc(p.id)}" data-model="${esc(m)}">${on ? icon('check') : '<span class="ico"></span>'}<span>${esc(m)}</span></button>`;
        }).join('') : `<div class="chat-pop-empty">${esc(ms.err)}</div>`;
      return `<div class="chat-pop-h">${esc(p.name)}<span class="ct-dim">${esc(p.kind)}${p.profile && p.profile !== 'auto' ? ` · ${esc(p.profile)}` : ''}</span></div>${list}`;
    }).join('');
  }
  function openModel(): void {
    const btn = chipsEl.querySelector<HTMLElement>('[data-ca="model"]');
    if (!btn) return;
    if (C.modelClose) { C.modelClose(); return; }
    renderModelPop();
    C.modelClose = floatPopover(modelPop, btn, () => { C.modelClose = null; }, 'left');
    modelPop.querySelector<HTMLElement>('.chat-mopt.on, .chat-mopt')?.focus();
    for (const p of (C.cat?.providers ?? []).filter((x) => x.connected)) {
      if (Array.isArray(C.models.get(p.id))) continue;
      C.models.set(p.id, 'loading');
      getJSON<{ models: string[]; current?: string | null; error?: string }>(`api/llm/agent/models?provider_id=${encodeURIComponent(p.id)}`).then((r) => {
        const list = (r.models || []).filter(Boolean);
        if (p.model && !list.includes(p.model)) list.unshift(p.model);
        C.models.set(p.id, list.length ? list : { err: r.error || 'no models' });
      }).catch((e: unknown) => { C.models.set(p.id, p.model ? [p.model] : { err: errText(e) }); }).finally(() => {
        if (!C.modelClose) return;
        const focused = document.activeElement instanceof HTMLElement && modelPop.contains(document.activeElement) ? document.activeElement.dataset['model'] : null;
        renderModelPop();
        (focused ? modelPop.querySelector<HTMLElement>(`[data-model="${CSS.escape(focused)}"]`) : modelPop.querySelector<HTMLElement>('.chat-mopt.on, .chat-mopt'))?.focus();
      });
    }
  }
  modelPop.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-model]') : null;
    if (!b) return;
    C.prefs.provider_id = b.dataset['pid'] ?? null;
    C.prefs.model = b.dataset['model'] ?? null;
    C.modelClose?.();
    renderChips();
    flash(`model: ${C.prefs.model ?? ''}`);
    remember();
    opts.onUpdate?.();
    ta.focus();
  });

  // ---------------------------------------------------------------- actions (one delegated listener)
  async function decide(ti: number, bi: number, approve: boolean): Promise<void> {
    const t = aiAt(ti), b = t?.blocks[bi];
    if (!t || b?.k !== 'confirm' || b.state !== 'pending') return;
    const picks = C.picks.get(b.id) ?? new Set(b.actions.map((a) => a.id));
    const yes = approve ? b.actions.filter((a) => picks.has(a.id)).map((a) => a.id) : [];
    const no = b.actions.map((a) => a.id).filter((id) => !yes.includes(id));
    const edits: Record<string, Record<string, unknown>> = {};
    for (const a of b.actions) {
      const v = C.edits.get(`${b.id}:${a.id}`);
      if (v !== undefined && yes.includes(a.id) && v !== str(a.args['query'])) edits[a.id] = { query: v };
    }
    try {
      await postJSON('api/llm/agent/confirm', { confirm_id: b.id, approve: yes, reject: no, ...(Object.keys(edits).length ? { edits } : {}) });
      const cur = aiAt(ti);
      if (cur) C.turns[ti] = reduce(cur, { type: 'x_confirm_sent', confirm_id: b.id });
      renderTurn(ti);
    } catch (e) {
      toast(`Could not send your answer: ${errText(e)}`, 'err');
    }
  }

  async function undo(undoId: string, bucket: string): Promise<void> {
    const bid = bucket || str(opts.scope()['bucket']);
    if (!bid) { toast('Undo: which bucket?', 'err'); return; }
    try {
      await postJSON(`api/dc/buckets/${encodeURIComponent(bid)}/undo`, { undo_id: undoId });
      C.turns = C.turns.map((e) => (e.role === 'ai' ? reduce(e, { type: 'x_undone', undo_id: undoId }) : e));
      renderLog();
      toast('Undone', 'ok');
      opts.onChange?.({ type: 'x_undone', undo_id: undoId });
    } catch (e) {
      toast(`Could not undo: ${errText(e)}`, 'err');
    }
  }

  async function applyUi(ti: number, bi: number): Promise<void> {
    const t = aiAt(ti), b = t?.blocks[bi];
    if (!t || b?.k !== 'ui' || b.applied || !opts.onUiAction) return;
    let r: UiResult = { ok: false };
    try { r = asResult(await opts.onUiAction(b)); } catch (e) { toast(`Could not apply: ${errText(e)}`, 'err'); return; }
    if (!r.ok) { toast(r.detail ? `Could not apply: ${r.detail}` : 'That change cannot be applied on this page', 'err'); return; }
    const cur = aiAt(ti);
    if (cur) C.turns[ti] = reduce(cur, { type: 'x_ui_applied', id: b.id });
    renderTurn(ti);
  }

  function retry(ti: number): void {
    if (C.busy) return;
    const u = C.turns[ti - 1];
    if (!u || u.role !== 'user') return;
    C.turns.splice(ti - 1, 2);
    void send(u.text, u.label ? { label: u.label, raw: true } : { raw: true });
  }

  async function copy(text: string, btn: HTMLElement): Promise<void> {
    btn.textContent = (await copyText(text)) ? 'Copied' : 'Copy failed';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  }

  function submit(): void {
    if (C.busy) return;
    const text = ta.value.trim();
    if (!text) return;
    ta.value = '';
    autosize();
    void send(text);
  }

  root.addEventListener('click', (e) => {
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-ca]') : null;
    if (!el || !root.contains(el) || (el instanceof HTMLButtonElement && el.disabled)) return;
    const ti = Number(el.dataset['ti']), bi = Number(el.dataset['bi']);
    const id = el.dataset['id'] ?? '';
    switch (el.dataset['ca']) {
      case 'send': submit(); break;
      case 'stop': stop(); break;
      case 'fold': {
        const key = el.dataset['key'] ?? '';
        C.open.set(key, el.getAttribute('aria-expanded') !== 'true');
        const turnEl = el.closest<HTMLElement>('.chat-turn');
        const i = Number(turnEl?.dataset['ti']);
        if (Number.isFinite(i)) { const keep = logEl.scrollTop; renderTurn(i); logEl.scrollTop = keep; logEl.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`)?.focus(); }
        break;
      }
      case 'model': openModel(); break;
      case 'mode': setMode(nextMode(mode())); break;
      case 'effort': setEffort(cycle(EFFORTS, effort())); break;
      case 'think': setThink(cycle(THINKS, think())); break;
      case 'web': setWeb(cycle(WEBS, web())); break;
      case 'unforce': C.forced = C.forced.filter((s) => s !== el.dataset['name']); renderForced(); break;
      case 'say': void send(el.dataset['text'] ?? '', { raw: true }); break;
      case 'focus': ta.focus(); break;
      case 'approve': void decide(ti, bi, true); break;
      case 'reject': void decide(ti, bi, false); break;
      case 'ask-again': {
        const b = aiAt(ti)?.blocks[bi];
        if (b?.k === 'confirm') void send(`Please propose these changes again: ${b.actions.map((a) => a.summary || a.tool).join('; ')}`, { raw: true });
        break;
      }
      case 'undo': void undo(el.dataset['undo'] ?? '', el.dataset['bucket'] ?? ''); break;
      case 'ui-apply': void applyUi(ti, bi); break;
      case 'plan-run': {
        const t = aiAt(ti), b = t?.blocks[bi];
        if (!t || b?.k !== 'plan' || C.busy) break;
        const m: ChatMode = el.dataset['mode'] === 'auto' ? 'auto' : 'ask';
        C.turns[ti] = reduce(t, { type: 'x_plan_approved' });
        setMode(m, false);
        void send(approvedMessage(b.summary, b.steps), { label: `Approved the plan (${m})`, raw: true });
        break;
      }
      case 'plan-edit': {
        const b = aiAt(ti)?.blocks[bi];
        if (b?.k !== 'plan') break;
        setMode('ask');
        ta.value = approvedMessage(b.summary, b.steps);
        autosize();
        ta.focus();
        break;
      }
      case 'retry': retry(ti); break;
      case 'copy': { const t = aiAt(ti); if (t) void copy(turnText(t), el); break; }
      case 'dl': { const t = aiAt(ti); if (t) download(turnText(t), '-report'); break; }
      // session header
      case 'rename': if (C.sid) { C.renaming = true; renderHead(); } break;
      case 'history': if (H.open) closeHist(); else openHist(); break;
      case 'new': newChat(); break;
      case 'del-cur': C.delCur = true; renderHead(); headEl.querySelector<HTMLElement>('[data-ca="del-cur-no"]')?.focus(); break;
      case 'del-cur-no': C.delCur = false; renderHead(); break;
      case 'del-cur-yes': C.delCur = false; if (C.sid) void deleteSessions({ ids: [C.sid] }); break;
      // history panel
      case 'h-close': closeHist(); break;
      case 'h-select': H.select = !H.select; H.picked.clear(); H.confirm = null; renderHistBody(); break;
      case 'h-scope': H.all = el.dataset['all'] === '1'; void loadHist(); break;
      case 'h-open': if (!H.select) void openSession(id); else { if (H.picked.has(id)) H.picked.delete(id); else H.picked.add(id); renderHistBody(); } break;
      case 'h-ren': H.renaming = id; H.confirm = null; renderHistBody(); break;
      case 'h-pin': { const s = H.items.find((x) => x.id === id); if (s) void pinSession(id, !s.pinned); break; }
      case 'h-del': H.confirm = `row:${id}`; renderHistBody(); histEl.querySelector<HTMLElement>('[data-ca="h-del-no"]')?.focus(); break;
      case 'h-del-no': H.confirm = null; renderHistBody(); break;
      case 'h-del-yes': void deleteSessions({ ids: [id] }); break;
      case 'h-sel-del': H.confirm = 'sel'; renderHistBody(); break;
      case 'h-sel-yes': void deleteSessions({ ids: [...H.picked] }); break;
      case 'h-all': H.confirm = el.dataset['scoped'] === '1' ? 'all-scope' : 'all'; H.includePinned = false; renderHistBody(); break;
      case 'h-all-yes': void deleteSessions({ all: true, scoped: H.confirm === 'all-scope', includePinned: H.includePinned }); break;
      case 'h-more': void loadHist(true); break;
      default: break;
    }
  });

  root.addEventListener('change', (e) => {
    const el = e.target instanceof HTMLInputElement ? e.target : null;
    if (!el?.dataset['cc']) return;
    if (el.dataset['cc'] === 'pick') {
      const b = aiAt(Number(el.dataset['ti']))?.blocks[Number(el.dataset['bi'])];
      if (b?.k !== 'confirm') return;
      const s = C.picks.get(b.id) ?? new Set(b.actions.map((a) => a.id));
      if (el.checked) s.add(el.value); else s.delete(el.value);
      C.picks.set(b.id, s);
      renderTurn(Number(el.dataset['ti']));
      logEl.querySelector<HTMLInputElement>(`input[data-cc="pick"][value="${CSS.escape(el.value)}"]`)?.focus();
    } else if (el.dataset['cc'] === 'h-pick') {
      if (el.checked) H.picked.add(el.value); else H.picked.delete(el.value);
      if (H.confirm === 'sel') H.confirm = null;
      renderHistBody();
      histEl.querySelector<HTMLInputElement>(`input[data-cc="h-pick"][value="${CSS.escape(el.value)}"]`)?.focus();
    } else if (el.dataset['cc'] === 'h-incl') H.includePinned = el.checked;
  });

  root.addEventListener('input', (e) => {
    const el = e.target instanceof HTMLInputElement ? e.target : null;
    if (el?.dataset['ci'] === 'q') C.edits.set(`${el.dataset['cid']}:${el.dataset['aid']}`, el.value);
    else if (el?.dataset['ci'] === 'h-q') {
      clearTimeout(H.qTimer);
      H.qTimer = window.setTimeout(() => { H.q = el.value.trim(); void loadHist(); }, 220);
    }
  });

  // rename inputs (header, history row): Enter saves, Esc cancels, blur saves
  root.addEventListener('keydown', (e) => {
    const t = e.target;
    if (t instanceof HTMLInputElement && (t.dataset['ci'] === 'title' || t.dataset['ci'] === 'h-ren')) {
      if (e.key !== 'Enter' && e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      const save = e.key === 'Enter';
      if (t.dataset['ci'] === 'title') {
        C.renaming = false;
        if (save && C.sid && t.value.trim()) void renameSession(C.sid, t.value);
        renderHead();
        headEl.querySelector<HTMLElement>('.chat-title')?.focus();
      } else {
        const id = H.renaming;
        H.renaming = null;
        if (save && id) void renameSession(id, t.value).then(renderHistBody); else renderHistBody();
      }
      return;
    }
    if (!H.open || !histEl.contains(t as Node)) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (H.confirm) { H.confirm = null; renderHistBody(); } else closeHist(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const rows = [...histEl.querySelectorAll<HTMLElement>('.chat-hopen')];
      if (!rows.length) return;
      e.preventDefault();
      const i = rows.indexOf(document.activeElement as HTMLElement);
      const next = i < 0 ? (e.key === 'ArrowDown' ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
      rows[next]?.focus();
    }
  });
  root.addEventListener('focusout', (e) => {
    const t = e.target;
    if (t instanceof HTMLInputElement && t.dataset['ci'] === 'title' && C.renaming && !root.contains(e.relatedTarget as Node | null)) {
      C.renaming = false;
      if (C.sid && t.value.trim() && t.value.trim() !== C.title) void renameSession(C.sid, t.value);
      renderHead();
    }
  });

  // the composer
  ta.addEventListener('input', () => { autosize(); updateSlash(); });
  ta.addEventListener('keydown', (e) => {
    if (C.slash.open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const n = C.slash.items.length;
        C.slash.i = (C.slash.i + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
        renderSlash();
        return;
      }
      if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') { e.preventDefault(); pickSlash(C.slash.i, e.key === 'Enter'); return; }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeSlash(); return; }
    }
    if (e.key === 'Tab' && e.shiftKey) { e.preventDefault(); setMode(nextMode(mode())); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); return; }
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      if (C.busy) stop(); else ta.blur();
    }
  });

  window.addEventListener('llm-state', () => { void loadCatalog(); });

  renderAll();
  void loadCatalog();
  void peekLimit();

  return {
    root,
    send: (text, o = {}) => send(text, { ...o, raw: true }),
    busy: () => !!C.busy,
    stop,
    newChat,
    focus: () => ta.focus({ preventScroll: true }),
    input: () => ta,
    setDraft: (text) => { ta.value = text; autosize(); },
    user: (text, voice = false) => { C.turns.push({ role: 'user', text, label: null, voice }); appendLog(C.turns.length - 1); },
    note,
    custom: (html) => {
      const entry: CustomEntry = { role: 'custom', html };
      C.turns.push(entry);
      appendLog(C.turns.length - 1);
      const at = (): number => C.turns.indexOf(entry);
      return {
        update: () => { const i = at(); if (i >= 0) renderTurn(i); },
        el: () => { const i = at(); return i >= 0 ? logEl.querySelector<HTMLElement>(`.chat-turn[data-ti="${i}"]`) : null; },
      };
    },
    archive: (items) => { if (items.length) { C.turns.unshift({ role: 'archive', items }); C.open.clear(); renderLog(); } },
    refresh: () => { if (!C.turns.length) renderLog(); renderChips(); },
    catalog: () => C.cat,
    mode,
    pending: () => C.turns.reduce((n, e) => n + (e.role === 'ai' ? e.blocks.filter((b) => b.k === 'confirm' && b.state === 'pending').length : 0), 0),
    modelLabel,
    lastUsage: () => {
      for (let i = C.turns.length - 1; i >= 0; i--) { const t = C.turns[i]; if (t && t.role === 'ai' && t.usage) return { ...t.usage, ms: t.ms }; }
      return null;
    },
    sessionId: () => C.sid,
    open: (id: string, quiet = true) => openSession(id, quiet),
  };
}
