// The ◆ assistant dock: one right-side column on every screen (home, Bucket, visualizer, labeling). The page
// keeps its own layout; the dock is position:fixed on the right and <body> gets padding-right of its width
// (html[data-dock] = open | rail | off), so every screen reflows the same way without knowing about it.
//
//   head   ◆ Assistant · page · model ……… context ring (share of the input budget, tick at the 80% auto-compact) ·
//          settings · collapse
//   tasks  while the agent works: "◆ is driving › Model tab › Run yolov8s" + Stop, its checklist (todo), running
//          jobs, confirmations waiting for you; after the answer the checklist stays, folded to "Tasks n/m"
//   body   the page's host section (#ai-side, #bk-ai, #ai-home): the shared ◆ chat (core/chat.ts)
//
// Collapsed it is a 44 px rail (◆ to open, the ring, a pending badge). Below 900 px it is a full-screen sheet over
// the page, opened from a floating ◆ button. The left edge drags the width (320 – min(640, 55% of the window)).
// Ctrl+. opens / collapses it. Open state and width are a per-browser convenience (localStorage, try/catch).
// Each screen registers one DockHost (setDockHost); the host mounts its chat on the first open and passes
// dockHooks() into mountChat so the tasks strip and the ring follow the agent's events, and withNav(page handler)
// as its onUiAction: the agent's `navigate` opens another page.
//
// One chat, every page: the dock remembers ONE current chat for the whole app (localStorage CURRENT), so Home,
// the Bucket page, every visualizer session and the labeling page open the same chat; the page only decides which
// specialist answers (the server's router). When the agent navigates, the turn ends; the goal, the chat id and the
// mode go into sessionStorage (HANDOFF, with the target page), and the dock of that page opens the same chat and
// sends the goal as a continued message. A page with unsaved work (an edited file, uploads) asks first. Other tabs
// showing the same chat reload it when a turn ends (BroadcastChannel), and the server answers one turn at a time.

import { continuedLabel, type ChatHandle } from './chat.js';
import { stepClosed, stepState } from './chat-reduce.js';
import { esc, icon, morphInner } from './dom.js';
import { LLM } from './llm.js';
import { bucketUrl, dataUrl, homeUrl, imageUrl, labelUrl, vizUrl } from './nav.js';
import type { AiTurn, ChatEvent, UiAction, UiResult } from '../types-chat.js';

export interface DockHost {
  /** the host section's id (tests and other modules find it by id) */
  id: string;
  /** the page's name in the head ("Visualizer", "Bucket"…) */
  label: () => string;
  /** the assistant is usable here (an LLM is connected and the page's feature is on) */
  enabled: () => boolean;
  /** build the chat into `el` once (first open); return its handle */
  mount: (el: HTMLElement) => ChatHandle;
  /** the dock was opened (focus, refresh the page's chips) */
  onShow?: () => void;
  /** the page's width changed (plots resize) */
  onResize?: () => void;
  /** Settings › AI assistant */
  settings?: () => void;
  /** the assistant arrived from another page (e.g. open the tab it asked for) */
  onHandoff?: (h: Handoff) => void;
  /** work on this page that leaving would lose (null = none): the agent's navigate asks the user first */
  unsaved?: () => string | null;
  /** the user agreed to leave: drop that work (so the browser does not ask a second time) */
  discard?: () => void;
}

/** What the old page left for the new one when the agent navigated: the same chat continues there. */
export interface Handoff { sid: string | null; goal: string; to: string; tab?: string; mode?: string; at: number }

type DockState = 'open' | 'rail';
interface TaskStep { text: string; state: string; page?: string }
const PAGE_NAME: Record<string, string> = { home: 'Home', bucket: 'Bucket', image: 'Image', viz: 'Visualizer', label: 'Labeling', data: 'Data' };

const KEY = 'dataviz.dock';
const CURRENT = 'dataviz.chat.current';                  // the one chat every page opens
const HANDOFF = 'dataviz.dock.handoff', HANDOFF_TTL = 3 * 60 * 1000, NAV_FALLBACK_MS = 5000;
const CHANNEL = 'dataviz-chat';
const MIN_W = 320, DEF_W = 400, NARROW = 900, AUTO_COMPACT = 0.8;          // the rail is --dock-rail-w (dock.css)

const D = {
  host: null as DockHost | null,
  chat: null as ChatHandle | null,
  root: null as HTMLElement | null,
  state: 'open' as DockState,
  width: DEF_W,
  narrow: false,
  frame: 0,
  // the tasks strip of the current / last answer
  running: false,
  steps: [] as TaskStep[],
  driving: [] as string[],
  jobs: new Map<string, { label: string; pct: number | null; done: boolean; ok: boolean }>(),
  pending: 0,
  tasksOpen: true,
  navTo: null as string | null,              // the agent navigated: go there when its turn has ended
  ready: false,                              // the remembered chat was opened (or there was none): sid changes count
  sid: null as string | null,                // the chat this dock shows (what CURRENT holds)
  stale: false,                              // another tab answered in this chat while this one was hidden
  bc: null as BroadcastChannel | null,
};

const maxW = (): number => Math.max(MIN_W, Math.min(640, Math.round(window.innerWidth * 0.55)));
const clampW = (w: number): number => Math.max(MIN_W, Math.min(maxW(), Math.round(w)));

function load(): void {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || 'null') as { state?: unknown; width?: unknown } | null;
    if (v && (v.state === 'open' || v.state === 'rail')) D.state = v.state;
    if (v && typeof v.width === 'number' && Number.isFinite(v.width)) D.width = v.width;
  } catch (e) { /* private window / blocked storage: the defaults */ }
}
function save(): void {
  try { localStorage.setItem(KEY, JSON.stringify({ state: D.state, width: D.width })); } catch (e) { /* ignore */ }
}
// the one current chat of the app: a reload, another page or another session continues it
function lastSid(): string | null {
  try {
    const v = localStorage.getItem(CURRENT);
    if (v !== null) return v || null;                   // '' = the user started a new chat
    // before one chat followed the user, each page kept its own: continue the newest of them once
    const old = Object.keys(localStorage).filter((k) => k.startsWith(`${KEY}.sid.`)).map((k) => localStorage.getItem(k)).filter(Boolean);
    return old.length ? String(old[old.length - 1]) : null;
  } catch (e) { return null; }
}
/** keep CURRENT in step with the chat on screen (a new chat, one opened from History, the first answer) */
function syncSid(): void {
  if (!D.ready || !D.chat) return;
  const sid = D.chat.sessionId();
  if (sid === D.sid) return;
  D.sid = sid;
  try { localStorage.setItem(CURRENT, sid ?? ''); } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------- markup

function ringHTML(): string {
  const ctx = D.chat?.lastCtx() ?? null;           // the chat on screen: live while it answers, its last answer's after a reload
  const set = LLM.budget?.max_input_tokens, ceil = LLM.limits?.ceiling || 100000;
  const budget = ctx?.limit || (typeof set === 'number' ? Math.min(set, ceil) : ceil);
  const pct = ctx == null ? null : Math.min(100, Math.round((ctx.used / budget) * 100));
  const c = 94.25;                                    // 2πr, r = 15
  const tip = pct == null ? 'Context used: shown after the first answer' : `Context used: ${pct}% of ${Math.round(budget / 1000)}k tokens · older messages are compacted at 80% (or type /compact)`;
  return `<span class="dock-ring${pct != null && pct >= 80 ? ' hot' : ''}" title="${esc(tip)}" aria-label="${esc(tip)}"><svg viewBox="0 0 36 36" aria-hidden="true">` +
    `<circle class="tr" cx="18" cy="18" r="15"/><circle class="tick" cx="18" cy="18" r="15" stroke-dasharray="1.2 ${c}" stroke-dashoffset="${-(c * AUTO_COMPACT)}"/>` +
    `<circle class="v" cx="18" cy="18" r="15" stroke-dasharray="${((pct ?? 0) / 100) * c} ${c}"/></svg><b class="num">${pct == null ? '' : pct}</b></span>`;
}

function headHTML(): string {
  const m = D.chat?.modelLabel();
  const sub = [D.host?.label() ?? '', m && m !== 'model' ? m : ''].filter(Boolean).join(' · ');
  return `<span class="dock-gem" aria-hidden="true">${icon('gem')}</span><div class="dock-t"><b>Assistant</b><span class="dock-sub">${esc(sub)}</span></div>` +
    `<span class="grow"></span>${ringHTML()}` +
    (D.host?.settings ? `<button type="button" class="icon-btn ghost" data-dock="settings" aria-label="AI settings" title="Settings › AI assistant">${icon('gear')}</button>` : '') +
    `<button type="button" class="icon-btn ghost" data-dock="rail" aria-label="Collapse the assistant" title="Collapse (Ctrl+.)">${icon(D.narrow ? 'x' : 'chev-r')}</button>`;
}

function railHTML(): string {
  const n = D.pending;
  return `<button type="button" class="dock-rail-btn" data-dock="open" aria-label="Open the assistant (Ctrl+.)" title="Open the assistant (Ctrl+.)">` +
    `<span class="dock-gem" aria-hidden="true">${icon('gem')}</span>${n ? `<span class="dock-badge num">${n}</span>` : ''}</button>` +
    (D.running ? '<span class="ct-spin" title="The assistant is working"></span>' : '') + ringHTML();
}

function tasksHTML(): string {
  const jobs = [...D.jobs.entries()];
  if (!D.running && !D.steps.length && !jobs.some(([, j]) => !j.done)) return '';
  const done = D.steps.filter((s) => stepClosed(s.state)).length;
  const head = `<button type="button" class="dock-tasks-h" data-dock="tasks" aria-expanded="${D.tasksOpen}">` +
    `<span>Tasks</span>${D.steps.length ? `<span class="num">${done} / ${D.steps.length}</span>` : ''}${D.pending ? `<span class="dock-wait">${D.pending} waiting for you</span>` : ''}` +
    `<span class="grow"></span>${icon(D.tasksOpen ? 'chev-u' : 'chev-d')}</button>`;
  if (!D.tasksOpen) return head;
  const drive = D.running ? `<div class="dock-drive"><span class="dock-gem sm" aria-hidden="true">${icon('gem')}</span><b>is driving</b>` +
    (D.driving.length ? D.driving.map((d) => `<span class="sep">›</span><span class="d">${esc(d)}</span>`).join('') : '<span class="sep">›</span><span class="d faint">thinking</span>') +
    `<span class="grow"></span><button type="button" class="ghost sm" data-dock="stop" title="Stop the assistant (Esc in the chat box)">${icon('stop')}Stop</button></div>` : '';
  const steps = D.steps.map((s) => {
    const st = stepState(s.state);
    const pg = s.page ? PAGE_NAME[s.page] ?? s.page : '';
    return `<li class="${st}"${st === 'skipped' ? ' title="skipped"' : ''}><span class="dot" aria-hidden="true">${st === 'done' ? icon('check') : st === 'skipped' ? icon('x') : ''}</span><span>${esc(s.text)}</span>` +
      (pg ? `<span class="grow"></span><span class="dock-page">${esc(pg)}</span>` : '') + '</li>';
  }).join('');
  const jobRows = jobs.map(([, j]) => `<li class="job${j.done ? (j.ok ? ' done' : ' bad') : ''}"><span class="dot" aria-hidden="true">${j.done ? icon(j.ok ? 'check' : 'x') : ''}</span>` +
    `<span>${esc(j.label)}</span><span class="grow"></span><span class="num">${j.done ? '' : j.pct == null ? '…' : `${Math.round(j.pct)}%`}</span>` +
    (j.done ? '' : `<span class="dock-bar"><i style="width:${Math.max(2, j.pct ?? 0)}%"></i></span>`) + '</li>').join('');
  return head + drive + (steps || jobRows ? `<ol class="dock-steps">${steps}${jobRows}</ol>` : '');
}

function render(): void {
  const r = D.root;
  syncSid();
  if (!r) return;
  const on = !!D.host && D.host.enabled();
  document.documentElement.dataset['dock'] = on ? D.state : 'off';
  document.documentElement.style.setProperty('--dock-w', `${clampW(D.width)}px`);
  if (!on) return;
  // morphed, not replaced: this runs on every streamed event, and new nodes would restart the ring / spinners and
  // make a click on Stop or a task miss
  morphInner(r.querySelector<HTMLElement>('.dock-head')!, headHTML());
  morphInner(r.querySelector<HTMLElement>('.dock-rail')!, railHTML());
  renderTasks();
}

function renderTasks(): void {
  const el = D.root?.querySelector<HTMLElement>('.dock-tasks');
  if (!el) return;
  const html = tasksHTML();
  morphInner(el, html);
  el.classList.toggle('hidden', !html);
}

/** at most one repaint per frame (streamed events arrive many per frame) */
function schedule(): void {
  if (D.frame) return;
  D.frame = requestAnimationFrame(() => { D.frame = 0; render(); });
}

function resized(): void {
  requestAnimationFrame(() => D.host?.onResize?.());
}

// ---------------------------------------------------------------- open / collapse

function ensureMounted(): void {
  if (D.chat || !D.host || !D.root) return;
  const body = D.root.querySelector<HTMLElement>('.dock-body')!;
  let el = document.getElementById(D.host.id);
  if (!el) { el = document.createElement('section'); el.id = D.host.id; }
  el.classList.add('dock-host');
  el.setAttribute('aria-label', 'AI assistant');
  body.append(el);
  D.chat = D.host.mount(el);
  const chat = D.chat;
  const h = takeHandoff();
  const sid = h ? h.sid : lastSid();
  void (sid ? chat.open(sid) : Promise.resolve(false)).then((ok) => {
    D.ready = true;
    D.sid = ok ? sid : null;
    if (!ok && sid) { try { localStorage.setItem(CURRENT, ''); } catch (e) { /* ignore */ } }
    if (h) {                                   // the agent came from another page: the same chat goes on here
      D.host?.onHandoff?.(h);
      const extra: Record<string, unknown> = ok ? { continued: true } : {};
      if (h.mode) extra['mode'] = h.mode;
      void chat.send(h.goal, { label: continuedLabel(h.goal), extra });
    }
    schedule();
  });
}

// ---------------------------------------------------------------- other tabs on the same chat

function initChannel(): void {
  if (D.bc || typeof BroadcastChannel === 'undefined') return;
  try { D.bc = new BroadcastChannel(CHANNEL); } catch (e) { return; }
  D.bc.onmessage = (e: MessageEvent) => {
    const sid = (e.data as { sid?: unknown } | null)?.sid;
    if (typeof sid !== 'string' || !D.chat || sid !== D.chat.sessionId()) return;
    if (document.visibilityState === 'visible') reloadChat(); else D.stale = true;
  };
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && D.stale) reloadChat(); });
}

function reloadChat(): void {
  D.stale = false;
  const sid = D.chat?.sessionId();
  if (sid && D.chat && !D.chat.busy()) void D.chat.open(sid);
}

// ---------------------------------------------------------------- navigate + hand-off

/** which page a URL is (the path, plus ?bucket / ?image / ?data[=<session>] / ?label=<id>): a hand-off is only taken on the page it was for */
function pageKey(u: URL): string {
  const q = u.searchParams;
  const where = q.has('label') ? `label:${q.get('label') ?? ''}` : q.has('bucket') ? 'bucket' : q.has('image') ? 'image'
    : q.has('data') ? 'data' : '';
  return `${u.pathname.replace(/\/?$/, '/')}|${where}`;
}

function peekHandoff(): Handoff | null {
  try {
    const h = JSON.parse(sessionStorage.getItem(HANDOFF) || 'null') as Handoff | null;
    if (!h || typeof h.goal !== 'string' || !h.goal || Date.now() - Number(h.at) >= HANDOFF_TTL) return null;
    return h.to === pageKey(new URL(location.href)) ? h : null;      // the user stayed (or went elsewhere): not here
  } catch (e) { return null; }
}
function takeHandoff(): Handoff | null {
  const h = peekHandoff();
  try { sessionStorage.removeItem(HANDOFF); } catch (e) { /* ignore */ }
  return h;
}

function navUrl(a: Record<string, unknown>): string | null {
  const s = (k: string): string => (typeof a[k] === 'string' ? String(a[k]) : '');
  switch (s('screen')) {
    case 'home': return homeUrl();
    case 'image': return imageUrl();
    case 'bucket': return bucketUrl(s('bucket') || null, s('path') || null);
    case 'viz': return s('session_id') ? vizUrl(s('session_id')) : null;
    case 'label': return s('session_id') ? labelUrl(s('session_id')) : null;
    case 'data': return dataUrl();
    default: return null;
  }
}

function go(): void {
  const url = D.navTo;
  if (!url) return;
  D.navTo = null;
  location.assign(url);
}

function navigate(a: Record<string, unknown>): UiResult {
  const url = navUrl(a);
  if (!url) return { ok: false, detail: 'that page needs a session_id (list_sessions finds it)' };
  const why = D.host?.unsaved?.() ?? null;
  if (why) {
    if (!window.confirm(`The assistant wants to open another page.\n\nLeave this page? ${why}`)) {
      return { ok: false, detail: `the user stayed on this page (${why}): do the rest here or ask them to finish first` };
    }
    D.host?.discard?.();
  }
  const to = pageKey(new URL(url, location.href));
  const h: Handoff = { sid: D.chat?.sessionId() ?? null, goal: String(a['goal'] || '').slice(0, 500), to, at: Date.now() };
  if (typeof a['tab'] === 'string') h.tab = a['tab'];
  const m = D.chat?.mode();
  if (m) h.mode = m;
  try { sessionStorage.setItem(HANDOFF, JSON.stringify(h)); } catch (e) { /* the new page opens the current chat */ }
  D.navTo = url;
  drive(`Opening ${String(a['screen'])}`);
  schedule();
  window.setTimeout(go, NAV_FALLBACK_MS);        // the turn normally ends first (dockDone goes there)
  return { ok: true, detail: `opening ${url}` };
}

/** A host's onUiAction with the agent's navigate added (other actions go to the page's own handler). */
export function withNav(page?: (a: UiAction) => boolean | UiResult | Promise<boolean | UiResult>):
  (a: UiAction) => boolean | UiResult | Promise<boolean | UiResult> {
  return (a) => {
    if (a.action === 'navigate') return navigate(a.args);
    if (page) return page(a);
    return { ok: false, detail: `${a.action} is not available on this page` };
  };
}

export function openDock(focus = true): void {
  if (!D.host?.enabled()) return;
  ensureMounted();
  D.state = 'open';
  save();
  render();
  resized();
  D.host.onShow?.();
  if (focus) requestAnimationFrame(() => D.chat?.focus());
}

export function collapseDock(): void {
  D.state = 'rail';
  save();
  render();
  resized();
}

export const dockOpen = (): boolean => D.state === 'open' && !!D.host?.enabled();
export const toggleDock = (): void => { if (dockOpen()) collapseDock(); else openDock(); };
export const dockChat = (): ChatHandle | null => D.chat;
/** repaint the head (model, page label) after the page or the chat changed */
export const refreshDock = (): void => schedule();

// ---------------------------------------------------------------- the agent's events (tasks strip, ring)

/** Pass into mountChat: the tasks strip and the context ring follow the agent. */
export function dockHooks(): { onEvent: (ev: ChatEvent) => void; onDone: (t: AiTurn) => void } {
  return { onEvent: dockEvent, onDone: dockDone };
}

export function dockEvent(ev: ChatEvent): void {
  switch (ev.type) {
    case 'meta':
      D.running = true; D.steps = []; D.driving = []; D.pending = 0;
      for (const [k, j] of D.jobs) if (j.done) D.jobs.delete(k);
      break;
    case 'context': case 'compact': break;          // the ring reads the chat's turns (lastCtx): only repaint
    case 'todo':
      D.steps = (Array.isArray(ev['items']) ? ev['items'] : []).map((x) => {
        const r = (typeof x === 'object' && x !== null ? x : {}) as Record<string, unknown>;
        const page = typeof r['page'] === 'string' ? r['page'] : '';
        return { text: String(r['text'] ?? ''), state: String(r['state'] ?? 'todo'), ...(page ? { page } : {}) };
      }).filter((s) => s.text).slice(0, 30);
      break;
    case 'ui_action': drive(String(ev['label'] || ev['action'] || '')); break;
    case 'tool_start':
      if (ev['name'] === 'delegate') drive('Sub-agents are looking it up');
      else if (ev['name'] === 'consult') {
        const pg = (typeof ev['args'] === 'object' && ev['args'] !== null ? (ev['args'] as Record<string, unknown>)['page'] : '') as string;
        drive(`Asking the ${PAGE_NAME[pg] ?? 'other page'}`);
      } else if (ev['risk'] !== 'read' && ev['risk'] !== 'control') drive(String(ev['summary'] || ev['name'] || ''));
      break;
    case 'job': {
      const id = String(ev['id'] ?? '');
      if (!id) break;
      const state = String(ev['state'] ?? 'running');
      D.jobs.set(id, { label: String(ev['label'] || 'Job'), pct: typeof ev['pct'] === 'number' ? ev['pct'] : null,
        done: state === 'done' || state === 'error' || state === 'cancelled', ok: state === 'done' });
      break;
    }
    case 'confirm': D.pending++; break;
    case 'confirmed': D.pending = Math.max(0, D.pending - 1); break;
    case 'done': case 'error': D.running = false; break;
    default: return;
  }
  schedule();
}

function drive(label: string): void {
  const l = label.trim().slice(0, 48);
  if (!l || D.driving[D.driving.length - 1] === l) return;
  D.driving = [...D.driving, l].slice(-3);
}

export function dockDone(): void {
  D.running = false;
  D.pending = D.chat?.pending() ?? 0;
  syncSid();
  const sid = D.chat?.sessionId();
  if (sid) { try { D.bc?.postMessage({ sid }); } catch (e) { /* ignore */ } }
  if (D.steps.length && D.steps.every((s) => stepClosed(s.state))) D.tasksOpen = false;
  schedule();
  if (D.navTo) window.setTimeout(go, 150);       // the answer is saved: leave for the page the agent opened
}

// ---------------------------------------------------------------- setup

function bindGrip(grip: HTMLElement): void {
  let x0 = 0, w0 = 0, raf = 0, next = 0;
  const move = (e: PointerEvent): void => {
    next = clampW(w0 + (x0 - e.clientX));
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; document.documentElement.style.setProperty('--dock-w', `${next}px`); });
  };
  const up = (): void => {
    grip.removeEventListener('pointermove', move);
    document.documentElement.classList.remove('dock-dragging');
    if (next) { D.width = next; save(); resized(); }
  };
  grip.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || D.narrow) return;
    e.preventDefault();
    x0 = e.clientX; w0 = clampW(D.width); next = 0;
    grip.setPointerCapture(e.pointerId);
    document.documentElement.classList.add('dock-dragging');
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up, { once: true });
    grip.addEventListener('pointercancel', up, { once: true });
  });
  grip.addEventListener('dblclick', () => { D.width = DEF_W; save(); render(); resized(); });
  grip.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    D.width = clampW(D.width + (e.key === 'ArrowLeft' ? 24 : -24));
    save(); render(); resized();
  });
}

function onClick(e: MouseEvent): void {
  // buttons only: <html data-dock="open|rail|off"> carries the dock's state, and matching it made every click in the
  // dock (the end of a text drag too) re-open it and focus the chat box, which dropped the selection
  const b = (e.target as Element).closest<HTMLElement>('button[data-dock]');
  if (!b) return;
  switch (b.dataset['dock']) {
    case 'open': openDock(); break;
    case 'rail': collapseDock(); break;
    case 'settings': D.host?.settings?.(); break;
    case 'stop': D.chat?.stop(); D.running = false; schedule(); break;
    case 'tasks': D.tasksOpen = !D.tasksOpen; renderTasks(); break;
    default: break;
  }
}

/** The screen's assistant: builds the dock once (the chat itself only on the first open). */
export function setDockHost(host: DockHost): void {
  D.host = host;
  if (!D.root) {
    load();
    const root = document.createElement('aside');
    root.id = 'ai-dock';
    root.className = 'ai-dock';
    root.setAttribute('aria-label', 'AI assistant');
    root.innerHTML = '<div class="dock-grip" role="separator" aria-orientation="vertical" aria-label="Resize the assistant (double-click: default)" tabindex="0"></div>' +
      '<div class="dock-panel"><div class="dock-head"></div><div class="dock-tasks hidden" aria-live="polite"></div><div class="dock-body"></div></div>' +
      '<div class="dock-rail"></div>';
    document.body.append(root);
    const fab = document.createElement('button');
    fab.type = 'button';
    fab.id = 'ai-fab';
    fab.className = 'ai-fab';
    fab.dataset['dock'] = 'open';
    fab.setAttribute('aria-label', 'Open the assistant');
    fab.innerHTML = icon('gem');
    document.body.append(fab);
    D.root = root;
    initChannel();
    window.addEventListener('storage', (e) => {             // another tab started / opened a chat: follow it when idle
      if (e.key !== CURRENT || !D.chat || D.chat.busy() || !D.ready) return;
      const sid = e.newValue || null;
      if (sid === D.chat.sessionId()) return;
      D.sid = sid;
      if (!sid) { D.chat.newChat(); return; }
      D.ready = false;                                      // syncSid waits: the old id is still on screen meanwhile
      void D.chat.open(sid).then(() => { D.ready = true; schedule(); });
    });
    root.addEventListener('click', onClick);
    fab.addEventListener('click', onClick);
    bindGrip(root.querySelector<HTMLElement>('.dock-grip')!);
    const mq = window.matchMedia(`(max-width: ${NARROW - 1}px)`);
    const narrow = (): void => {
      const was = D.narrow;
      D.narrow = mq.matches;
      if (D.narrow && !was) D.state = 'rail';          // a phone starts with the page, the ◆ button opens the sheet
      render();
    };
    mq.addEventListener('change', narrow);
    D.narrow = mq.matches;
    if (D.narrow) D.state = 'rail';
    document.addEventListener('keydown', (e) => {
      if (e.key === '.' && (e.ctrlKey || e.metaKey) && !e.altKey && D.host?.enabled()) { e.preventDefault(); toggleDock(); }
    });
    window.addEventListener('llm-state', () => {
      if (peekHandoff() && D.host?.enabled()) D.state = 'open';     // the agent arrived from another page
      if (dockOpen()) ensureMounted();
      render(); resized();
    });
  }
  if (peekHandoff() && host.enabled()) D.state = 'open';
  if (D.state === 'open' && host.enabled()) ensureMounted();
  render();
}
