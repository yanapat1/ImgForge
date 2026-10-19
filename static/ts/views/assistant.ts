// The ◆ AI assistant tab (left panel #ai-side; the plot stays on the right so you can lasso and ask).
// Header (model, token meter, clear, settings) · context chips (Dataset / Evaluation / Selection · n) · transcript
// (Markdown answers streamed from POST api/llm/chat as NDJSON, label-check verdict cards, report download, auto-tag
// job progress) · composer (Enter sends, Shift+Enter new line, Stop, hold-to-talk mic).
// Nothing here runs until the tab is first shown; every piece is drawn only when its feature is on (core/llm.js),
// and a Settings change (window event `llm-state`) re-renders live. History: localStorage `ai-chat:<session>:<user id>` (core/auth.ts userKey).

import { api, errMessage, errText } from '../core/api.js';
import { userKey } from '../core/auth.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { setBar, waitJob } from '../core/jobs.js';
import { applyLLM, LLM, llmOn } from '../core/llm.js';
import { md } from '../core/md.js';
import { meta, pts, S } from '../core/state.js';
import type { Job } from '../types.js';
import { canRecord, cancelRecording, startRecording, stopRecording } from '../core/voice.js';
import { openSettings } from '../settings/dialog.js';
import { refreshTagList } from './controls.js';
import { runNLFilter, runNow } from './filter-bar.js';
import { setSelection } from './gallery.js';
import { curTab, switchTab } from './layout.js';
import { openModal } from './modal.js';

const MAX_SAVED = 40, MAX_TURNS = 12, CHECK_MAX = 96, SEL_SEND_MAX = 500;
const EXPLAIN_EVAL = 'Explain this evaluation: what is good, what is wrong, and the 3 most useful next steps.';
const TABS = ['explore', 'stats', 'curation', 'model', 'eval', 'ai'];

type MsgKind = 'chat' | 'report' | 'check' | 'tag' | 'note';
/** Token / time figures of one answer (the server's `usage` + our estimate + the elapsed time). */
interface Usage { in?: number | null | undefined; out?: number | null | undefined; est?: number | null | undefined; ms?: number | undefined }
/** One verdict card of a label check. `id` is a box id. */
interface CheckItem { id: number; verdict: string; suggest?: string | undefined; why?: string | undefined }
interface ChatOpts { ev?: boolean; task?: string | null; kind?: MsgKind; label?: string | null }
type TagBody = { scope: 'selection'; ids: number[] } | { scope: 'all' };
/** What Retry re-runs. */
type Retry =
  | { type: 'chat'; text: string; ev: boolean; task: string | null; kind: MsgKind; label: string | null }
  | { type: 'check'; ids: number[] }
  | { type: 'tag'; body: TagBody };
/** Progress line of a running auto-tag job (a job snapshot + the text shown under the bar). */
type JobView = Partial<Job> & { text: string };
interface Msg {
  role: 'user' | 'assistant';
  text: string;
  kind?: MsgKind | undefined;
  meta?: Usage | null | undefined;
  err?: string | null | undefined;
  streaming?: boolean | undefined;
  pending?: boolean | undefined;
  items?: CheckItem[] | undefined;
  job?: JobView | null | undefined;
  retry?: Retry | undefined;
  voice?: boolean | undefined;
  stopped?: boolean | undefined;
  ids?: number[] | undefined;      // check: the boxes being checked
  est?: number | null | undefined; // chat: the server's input-token estimate
}
type MicState = 'idle' | 'starting' | 'on' | 'sending';
interface AssistantState {
  msgs: Msg[];
  key: string | null;                                       // localStorage key of the loaded history
  chips: { dataset: boolean; eval: boolean; sel: boolean | null };   // sel: null = automatic (on when there is a selection)
  busy: AbortController | null;                             // the running request (Send turns into Stop)
  last: Usage | null;                                       // the last answer, for the meter
  rec: MicState;
  stopAfterStart: boolean;
}

const A: AssistantState = {
  msgs: [],
  key: null,
  chips: { dataset: true, eval: false, sel: null },
  busy: null,
  last: null,
  rec: 'idle',
  stopAfterStart: false,
};
let built = false, frame = 0;

const isRec = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isAbort = (e: unknown): boolean => isRec(e) && e['name'] === 'AbortError';
const numOrNull = (v: unknown): number | null | undefined => (typeof v === 'number' ? v : v === null ? null : undefined);
const strOr = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined);

function toUsage(v: unknown): Usage | null {
  if (!isRec(v)) return null;
  const u: Usage = {};
  const i = numOrNull(v['in']), o = numOrNull(v['out']), e = numOrNull(v['est']);
  if (i !== undefined) u.in = i;
  if (o !== undefined) u.out = o;
  if (e !== undefined) u.est = e;
  if (typeof v['ms'] === 'number') u.ms = v['ms'];
  return u;
}

function toCheckItem(x: unknown): CheckItem | null {
  if (!isRec(x)) return null;
  const id = Number(x['id']);
  if (!Number.isFinite(id)) return null;
  return { id, verdict: strOr(x['verdict']) ?? '', suggest: strOr(x['suggest']), why: strOr(x['why']) };
}

const isMsgKind = (k: string): k is MsgKind => k === 'chat' || k === 'report' || k === 'check' || k === 'tag' || k === 'note';
function toMsg(v: unknown): Msg | null {
  if (!isRec(v) || (v['role'] !== 'user' && v['role'] !== 'assistant')) return null;
  const m: Msg = { role: v['role'], text: strOr(v['text']) ?? '' };
  const kind = strOr(v['kind']);
  if (kind !== undefined && isMsgKind(kind)) m.kind = kind;
  const meta = toUsage(v['meta']);
  if (meta) m.meta = meta;
  const err = strOr(v['err']);
  if (err) m.err = err;
  if (Array.isArray(v['items'])) m.items = v['items'].map(toCheckItem).filter((x): x is CheckItem => x !== null);
  if (v['voice']) m.voice = true;
  if (v['stopped']) m.stopped = true;
  return m;
}

// ---------------------------------------------------------------- helpers

const fmtK = (n: number | null | undefined): string => (n == null ? '–' : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n)));
const clsOf = (id: number): string => { const c = S.P && S.P.cls[id]; return c === undefined || c === null ? '?' : (meta().classes[c] ?? String(c)); };

function sample(ids: number[], n: number): number[] {               // evenly spread, keeps order
  if (ids.length <= n) return ids.slice();
  const out: number[] = [];
  for (let k = 0; k < n; k++) out.push(ids[Math.floor((k * ids.length) / n)] ?? 0);
  return out;
}

const liveSel = (): number[] => (S.sel || []).filter((id) => !(S.P && S.P.deleted && S.P.deleted[id]));
const selOn = (): boolean => llmOn('ask_selection') && S.sel.length > 0 && A.chips.sel !== false;

function chatKey(): string {
  const ds = S.status && S.status.dataset;
  return userKey(`ai-chat:${(ds && ds.id) || 'default'}`);
}

function save(): void {
  if (A.key === null) return;
  const key = A.key;
  const keep = A.msgs.filter((m) => !m.streaming && !m.pending && !m.job).slice(-MAX_SAVED)
    .map(({ role, text, kind, meta, err, items, voice, stopped }) => ({ role, text, kind, meta, err, items, voice, stopped }));
  try { localStorage.setItem(key, JSON.stringify(keep)); } catch (e) { /* private window / full */ }
}

function load(key: string): Msg[] {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(v) ? v.map(toMsg).filter((m): m is Msg => m !== null) : [];
  } catch (e) { return []; }
}

// POST JSON with an abort signal (api() throws "path: status body" → errText gives the server's detail)
const post = (path: string, body: unknown, signal?: AbortSignal): Promise<Response> => {
  const init: RequestInit = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  if (signal) init.signal = signal;
  return api(path, init);
};
const postJ = async <T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> => { const r: T = await (await post(path, body, signal)).json(); return r; };

// ---------------------------------------------------------------- entry points

export function showAiTab(): void {
  build();
  if (!$req<HTMLTextAreaElement>('ai-q').value) requestAnimationFrame(() => { if (curTab === 'ai') $req<HTMLTextAreaElement>('ai-q').focus({ preventScroll: true }); });
}

function openAi(): void {
  if (curTab !== 'ai') switchTab('ai'); else build();
}

// from other views (Evaluation ◆ Explain): open the tab and ask
export function askAI(text: string, { eval: ev = false }: { eval?: boolean } = {}): void {
  if (!llmOn('assistant')) return;
  openAi();
  if (ev) A.chips.eval = true;
  renderChips();
  void send(text, { ev });
}

// gallery ◆ "Check labels"
export function checkLabelsAI(): void {
  if (!llmOn('label_check')) return;
  openAi();
  void runCheck();
}

// ---------------------------------------------------------------- build + render

const GEM_PATHS = '<path class="gem-body" d="M6.5 4h10l4.5 5.5L11.5 21 2 9.5z"/><path class="gem-facet" d="M2 9.5h19M9.3 4 7.2 9.5l4.3 11.5 4.3-11.5L13.7 4M7.2 9.5 11.5 4l4.3 5.5"/>';
const heroGem = () => '<svg class="ai-hero-gem" viewBox="0 0 24 24" aria-hidden="true"><defs><linearGradient id="ai-gem-g" x1="0" y1="0" x2="1" y2="1">' +
  '<stop offset="0" style="stop-color:var(--gem-1, #7dd3fc)"/><stop offset=".55" style="stop-color:var(--gem-2, #a78bfa)"/><stop offset="1" style="stop-color:var(--gem-3, #f0abfc)"/></linearGradient></defs>' +
  GEM_PATHS + '</svg>';
const mark = (cls = 'ai-mark'): string => `<span class="${cls}" aria-hidden="true">${icon('gem')}</span>`;

function build(): void {
  const root = $req('ai-side');
  if (!built) {
    built = true;
    root.innerHTML =
      `<div class="side-head ai-head">${mark()}<h2>AI assistant</h2><span id="ai-model" class="ai-model muted"></span>` +
      '<span class="grow"></span><span id="ai-meter" class="ai-meter num" title="tokens of the last answer: sent · received / input budget"></span>' +
      `<button type="button" class="icon-btn ghost" data-aact="clear" aria-label="Clear the chat" title="Clear the chat">${icon('trash')}</button>` +
      `<button type="button" class="icon-btn ghost" data-aact="settings" aria-label="AI settings" title="Settings › AI assistant">${icon('gear')}</button></div>` +
      '<div id="ai-chips" class="ai-chips" role="group" aria-label="What the assistant can see"></div>' +
      '<div id="ai-log" class="ai-log" role="log" aria-live="polite" aria-relevant="additions"></div>' +
      '<form id="ai-form" class="ai-composer">' +
      '<textarea id="ai-q" rows="1" placeholder="Ask about this dataset…" aria-label="Message the AI assistant"></textarea>' +
      '<div class="ai-cbar">' +
      `<button type="button" id="ai-mic" class="icon-btn ghost ai-mic hidden" data-llm="voice" aria-pressed="false" aria-label="Hold to talk (or click to start / stop)" title="Hold to talk · click to start / stop">${icon('mic')}</button>` +
      '<span id="ai-hint" class="ai-hint muted">Enter to send · Shift+Enter for a new line</span>' +
      `<button type="submit" id="ai-send" class="primary icon-btn ai-send" aria-label="Send" title="Send">${icon('send')}</button>` +
      '</div></form>';
    bind();
  }
  const key = chatKey();
  if (A.key !== key) {
    A.key = key;
    A.msgs = load(key);
    const lastBot = [...A.msgs].reverse().find((m) => m.role === 'assistant' && m.meta);
    A.last = lastBot?.meta ?? null;
  }
  renderAll();
}

function renderAll(): void {
  renderHead();
  renderChips();
  renderLog();
  renderSend();
  applyLLM($req('ai-side'));
  if (!canRecord()) $req('ai-mic').classList.add('hidden');
}

function renderHead(): void {
  const p = LLM.provider;
  $req('ai-model').textContent = p ? (p.model || p.name || '') : '';
  $req('ai-model').title = p ? `${p.name || ''} · ${p.kind || ''}` : '';
  const L = A.last, budget = (LLM.budget && LLM.budget.max_input_tokens) || 100000;
  $req('ai-meter').textContent = L ? `in ${fmtK(L.in ?? L.est)} · out ${fmtK(L.out)} / ${fmtK(budget)}` : '';
}

function renderChips(): void {
  const el = $('ai-chips');
  if (!el) return;
  const chip = (k: string, label: string, ic: string, on: boolean, dis = ''): string =>
    `<button type="button" class="ai-chip" data-aact="chip" data-k="${k}" aria-pressed="${on}" ${dis}>${icon(ic)}<span>${esc(label)}</span></button>`;
  const out: string[] = [];
  if (llmOn('dataset_qa')) out.push(chip('dataset', 'Dataset', 'db', A.chips.dataset));
  if (llmOn('explain_eval')) out.push(chip('eval', 'Evaluation', 'eval', A.chips.eval));
  if (llmOn('ask_selection')) {
    const n = S.sel.length;
    out.push(chip('sel', `Selection · ${n.toLocaleString()}`, 'layers', selOn(), n ? 'title="The assistant sees a mosaic of up to 24 selected boxes"' : 'disabled title="Lasso some dots on the plot first"'));
  }
  el.innerHTML = out.length ? '<span class="ai-chips-lbl muted">Context</span>' + out.join('') : '';
  el.classList.toggle('hidden', !out.length);
}

interface Quick { id: string; f: string; ic: string; t: string; q?: string; ev?: boolean; sel?: boolean }
const QUICK: Quick[] = [
  { id: 'sum', f: 'dataset_qa', ic: 'bars', t: 'Summarize this dataset', q: 'Summarize this dataset: size, classes and their balance, image quality, and anything unusual.' },
  { id: 'more', f: 'dataset_qa', ic: 'target', t: 'Which class needs more data?', q: 'Which class needs more data, and what kind of images should I collect for it?' },
  { id: 'eval', f: 'explain_eval', ic: 'eval', t: 'Explain the evaluation', q: EXPLAIN_EVAL, ev: true },
  { id: 'common', f: 'ask_selection', sel: true, ic: 'layers', t: 'What do the selected boxes have in common?', q: 'What do the selected boxes have in common? Point out any that look different or mislabeled.' },
  { id: 'check', f: 'label_check', sel: true, ic: 'check', t: 'Check the selected labels' },
  { id: 'report', f: 'report', ic: 'download', t: 'Write a dataset report' },
  { id: 'tag', f: 'auto_tag', ic: 'tag', t: 'Auto-tag images (night, rain, blur…)' },
];

function emptyHTML(): string {
  const qs = QUICK.filter((q) => llmOn(q.f));
  const card = (q: Quick): string => {
    const dis = q.sel && !S.sel.length;
    return `<button type="button" class="ai-qa" data-aact="quick" data-q="${q.id}" ${dis ? 'disabled title="Select boxes on the plot first"' : ''}>` +
      `${icon(q.ic)}<span>${esc(q.t)}</span></button>`;
  };
  return `<div class="ai-empty">${heroGem()}<h3>Ask about this dataset</h3>` +
    '<p class="muted">Ask in plain words. Lasso dots on the plot to ask about exactly those boxes.</p>' +
    (qs.length ? `<div class="ai-qgrid">${qs.map(card).join('')}</div>` : '') + '</div>';
}

function metaText(m: Msg): string {
  const t = m.meta;
  if (!t) return '';
  const parts: string[] = [];
  if (t.in != null || t.out != null) parts.push(`in ${fmtK(t.in)} · out ${fmtK(t.out)}`);
  else if (t.est != null) parts.push(`~${fmtK(t.est)} in`);
  if (t.ms != null) parts.push(t.ms >= 1000 ? `${(t.ms / 1000).toFixed(1)} s` : `${t.ms} ms`);
  return parts.join(' · ');
}

const VERDICT: Record<string, string> = { wrong: 'likely wrong', unsure: 'unsure', ok: 'ok' };

function checkHTML(m: Msg, i: number): string {
  if (m.pending) return `<div class="ai-wait">${icon('gem')}Checking ${m.ids?.length ?? 0} boxes…</div>`;
  const items = (m.items || []).slice().sort((a, b) => ['wrong', 'unsure', 'ok'].indexOf(a.verdict) - ['wrong', 'unsure', 'ok'].indexOf(b.verdict));
  if (!items.length) return '';
  const n = (v: string): number => items.filter((x) => x.verdict === v).length;
  const cards = items.map((x) => {
    const v = VERDICT[x.verdict] ? x.verdict : 'unsure';
    const id = x.id;
    return `<div class="ai-vc v-${v}" data-aact="open" data-id="${id}" role="button" tabindex="0" title="Open box #${id}">` +
      `<img loading="lazy" decoding="async" src="api/crop/${id}" alt="">` +
      `<div class="ai-vc-body"><div class="ai-vc-top"><b>${esc(clsOf(id))}</b><span class="ai-pill p-${v}">${esc(VERDICT[v])}</span></div>` +
      (x.suggest ? `<div class="ai-vc-sug">${icon('arrow-r')}${esc(x.suggest)}</div>` : '') +
      (x.why ? `<div class="ai-vc-why muted">${esc(x.why)}</div>` : '') + '</div></div>';
  }).join('');
  return `<div class="ai-vlist">${cards}</div><div class="ai-actions">` +
    `<button type="button" data-aact="sel-v" data-v="wrong" data-i="${i}" ${n('wrong') ? '' : 'disabled'}>Select the wrong ones <span class="num muted">${n('wrong')}</span></button>` +
    `<button type="button" data-aact="sel-v" data-v="unsure" data-i="${i}" ${n('unsure') ? '' : 'disabled'}>Select unsure <span class="num muted">${n('unsure')}</span></button></div>`;
}

function msgHTML(m: Msg, i: number): string {
  if (m.role === 'user') {
    return `<div class="ai-msg user" data-i="${i}"><div class="ai-bubble">${m.voice ? icon('mic') : ''}${esc(m.text)}</div></div>`;
  }
  const body = m.text ? `<div class="ai-md${m.streaming ? ' streaming' : ''}">${md(m.text)}</div>`
    : m.streaming ? '<div class="ai-typing" aria-label="thinking"><i></i><i></i><i></i></div>' : '';
  const job = m.job ? `<div class="ai-job"><div class="bar" role="progressbar" aria-label="Auto-tag progress"><div></div></div><span class="muted num">${esc(m.job.text || '')}</span></div>` : '';
  const err = m.err ? `<div class="status-line err">${icon('alert')}<span>${esc(m.err)}</span>` +
    (m.retry ? `<button type="button" class="ghost" data-aact="retry" data-i="${i}">${icon('refresh')}Retry</button>` : '') + '</div>' : '';
  const done = !m.streaming && !m.pending && !m.job;
  const foot = done && (m.text || m.meta) ? `<div class="ai-foot"><span class="num">${esc(metaText(m))}${m.stopped ? ' · stopped' : ''}</span>` +
    (m.text ? `<button type="button" class="ghost ai-mini" data-aact="copy" data-i="${i}" title="Copy the answer as Markdown">Copy</button>` : '') +
    (m.kind === 'report' && m.text && !m.err ? `<button type="button" class="ghost ai-mini" data-aact="dl" data-i="${i}">${icon('download')}Download .md</button>` : '') + '</div>' : '';
  return `<div class="ai-msg bot${m.kind === 'note' ? ' note' : ''}" data-i="${i}">${mark('ai-av')}<div class="ai-body">` +
    body + (m.kind === 'check' ? checkHTML(m, i) : '') + job + err + foot + '</div></div>';
}

function nearBottom(): boolean { const l = $req('ai-log'); return l.scrollHeight - l.scrollTop - l.clientHeight < 80; }
function toBottom(): void { const l = $req('ai-log'); l.scrollTop = l.scrollHeight; }

function renderLog(): void {
  const log = $('ai-log');
  if (!log) return;
  log.innerHTML = A.msgs.length ? A.msgs.map(msgHTML).join('') : emptyHTML();
  log.classList.toggle('is-empty', !A.msgs.length);
  toBottom();
}

// one message re-drawn in place (streaming deltas, job progress)
function renderMsg(m: Msg): void {
  const i = A.msgs.indexOf(m), log = $('ai-log');
  if (i < 0 || !log) return;
  const stick = nearBottom();
  const el = log.querySelector(`.ai-msg[data-i="${i}"]`);
  if (!el) { renderLog(); return; }
  el.outerHTML = msgHTML(m, i);
  if (m.job) { const fill = log.querySelector<HTMLElement>(`.ai-msg[data-i="${i}"] .ai-job .bar > div`); if (fill) setBar(fill, m.job); }
  if (stick) toBottom();
}

function scheduleMsg(m: Msg): void {               // at most one Markdown re-render per frame while streaming
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; renderMsg(m); });
}

function renderSend(): void {
  const b = $('ai-send');
  if (!b) return;
  const busy = !!A.busy;
  b.innerHTML = icon(busy ? 'stop' : 'send');
  b.classList.toggle('stop', busy);
  b.setAttribute('aria-label', busy ? 'Stop' : 'Send');
  b.title = busy ? 'Stop (Esc)' : 'Send (Enter)';
  $req('ai-side').setAttribute('aria-busy', String(busy));
}

function renderMic(): void {
  const b = $req('ai-mic');
  const on = A.rec === 'on' || A.rec === 'starting';
  b.classList.toggle('recording', on);
  b.classList.toggle('sending', A.rec === 'sending');
  b.setAttribute('aria-pressed', String(on));
  $req('ai-hint').textContent = A.rec === 'on' ? 'Listening… release (or click) to send' : A.rec === 'starting' ? 'Starting the microphone…'
    : A.rec === 'sending' ? 'Understanding…' : 'Enter to send · Shift+Enter for a new line';
}

// ---------------------------------------------------------------- requests

function push(...ms: Msg[]): void { A.msgs.push(...ms); renderLog(); }

function note(text: string, err = false): void {
  push({ role: 'assistant', kind: 'note', text: err ? '' : text, err: err ? text : null });
  save();
}

// earlier turns for the model: plain text only, oldest dropped, roles alternating (some providers insist)
function history(): Array<{ role: Msg['role']; text: string }> {
  const out: Array<{ role: Msg['role']; text: string }> = [];
  for (const m of A.msgs) {
    if (m.streaming || m.pending || m.job || m.kind === 'note' || !m.text || m.err) continue;
    const prev = out[out.length - 1];
    if (prev && prev.role === m.role) prev.text += `\n\n${m.text}`;
    else out.push({ role: m.role, text: m.text });
  }
  const turns = out.slice(-MAX_TURNS);
  while (turns.length && turns[0]?.role !== 'user') turns.shift();
  return turns;
}

function context(ev: boolean): { dataset?: true; eval?: true; selection?: number[] } {
  const c: { dataset?: true; eval?: true; selection?: number[] } = {};
  if (A.chips.dataset && llmOn('dataset_qa')) c.dataset = true;
  if ((ev || A.chips.eval) && llmOn('explain_eval')) c.eval = true;
  if (selOn()) c.selection = sample(liveSel(), SEL_SEND_MAX);      // the server picks ≤ 24 for its mosaic
  return c;
}

async function send(text0: string, { ev = false, task = null, kind = 'chat', label = null }: ChatOpts = {}): Promise<void> {
  const text = String(text0 || '').trim();
  if (!text || A.busy || !llmOn('assistant')) return;
  const msgs = history();
  if (msgs.length && msgs[msgs.length - 1]?.role === 'user') msgs.pop();   // an unanswered question: replaced by this one
  msgs.push({ role: 'user', text });
  const body: { messages: typeof msgs; ctx: ReturnType<typeof context>; task?: string } = { messages: msgs, ctx: context(ev) };
  if (task) body.task = task;
  const m: Msg = { role: 'assistant', kind, text: '', streaming: true, retry: { type: 'chat', text, ev, task, kind, label } };
  push({ role: 'user', text: label || text }, m);
  await stream(m, body);
}

function onLine(m: Msg, line: string): void {
  let x: unknown;
  try { x = JSON.parse(line); } catch (e) { return; }
  if (!isRec(x)) return;
  const text = strOr(x['text']);
  if (x['t'] === 'meta') m.est = numOrNull(x['in_est']);
  else if (x['t'] === 'delta' && text) { m.text += text; scheduleMsg(m); }
  else if (x['t'] === 'done') {
    const u = isRec(x['usage']) ? x['usage'] : null;
    m.meta = { in: numOrNull(u?.['in']), out: numOrNull(u?.['out']), est: m.est };
    if (typeof x['ms'] === 'number') m.meta.ms = x['ms'];
  } else if (x['t'] === 'error') m.err = strOr(x['error']) || 'The model returned an error.';
}

async function stream(m: Msg, body: unknown): Promise<void> {
  const ac = new AbortController();
  A.busy = ac;
  renderSend();
  const t0 = performance.now();
  try {
    const r = await post('api/llm/chat', body, ac.signal);
    if (!r.body) throw new Error('the server sent no body');
    const rd = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, k).trim();
        buf = buf.slice(k + 1);
        if (line) onLine(m, line);
      }
    }
    if (buf.trim()) onLine(m, buf.trim());
    if (!m.text && !m.err) m.err = 'The model sent an empty answer.';
  } catch (e) {
    if (isAbort(e)) m.stopped = true; else m.err = errText(e);
  } finally {
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    m.streaming = false;
    if (!m.meta) m.meta = { est: m.est, ms: Math.round(performance.now() - t0) };
    if (m.meta.in != null || m.meta.out != null || m.meta.est != null) A.last = m.meta;
    if (A.busy === ac) A.busy = null;
    renderMsg(m);
    renderSend();
    renderHead();
    save();
  }
}

async function runCheck(ids0: number[] | null = null): Promise<void> {
  if (A.busy || !llmOn('label_check')) return;
  const all = liveSel();
  const ids = ids0 || sample(all, CHECK_MAX);
  if (!ids.length) { note('Select some boxes first (lasso dots on the plot), then ask me to check them.'); return; }
  const m: Msg = { role: 'assistant', kind: 'check', text: '', pending: true, ids, retry: { type: 'check', ids } };
  push({ role: 'user', text: `Check the labels of ${ids.length} selected boxes${all.length > ids.length && !ids0 ? ` (a sample of ${all.length.toLocaleString()})` : ''}` }, m);
  const ac = new AbortController();
  A.busy = ac;
  renderSend();
  const t0 = performance.now();
  try {
    const r = await postJ<{ items?: unknown[]; usage?: { in?: number | null; out?: number | null } | null }>('api/llm/label-check', { ids }, ac.signal);
    const items = (r.items || []).map(toCheckItem).filter((x): x is CheckItem => x !== null);
    m.items = items;
    const n = (v: string): number => items.filter((x) => x.verdict === v).length;
    m.text = `Checked **${items.length}** boxes: **${n('wrong')}** likely wrong, ${n('unsure')} unsure, ${n('ok')} look fine.` +
      (n('wrong') ? ' Click a card to open the box.' : '');
    m.meta = { in: r.usage?.in, out: r.usage?.out, ms: Math.round(performance.now() - t0) };
    A.last = m.meta;
  } catch (e) {
    if (isAbort(e)) { m.stopped = true; m.text = '_Stopped._'; } else m.err = errText(e);
  } finally {
    m.pending = false;
    if (A.busy === ac) A.busy = null;
    renderMsg(m);
    renderSend();
    renderHead();
    save();
  }
}

function runReport(): void {
  void send('Write a dataset report in Markdown: overview, classes and balance, image quality, label problems, model results if there are any, and the most useful next steps.',
    { task: 'report', kind: 'report', label: 'Write a dataset report' });
}

async function runTag(retry: TagBody | null = null): Promise<void> {
  if (!llmOn('auto_tag')) return;
  const useSel = retry ? retry.scope === 'selection' : selOn();
  const body: TagBody = retry || (useSel ? { scope: 'selection', ids: liveSel().slice(0, 20000) } : { scope: 'all' });
  const m: Msg = { role: 'assistant', kind: 'tag', text: '', job: { text: 'starting…' }, retry: { type: 'tag', body } };
  push({ role: 'user', text: body.scope === 'selection' ? `Auto-tag the images of the ${body.ids.length.toLocaleString()} selected boxes` : 'Auto-tag every image' }, m);
  const t0 = performance.now();
  try {
    const r = await postJ<{ job_id: number }>('api/llm/tag', body);
    const j = await waitJob(r.job_id, (jj) => {
      m.job = { ...jj, text: jj.total ? `${jj.done}/${jj.total} images${jj.extra ? ` · ${jj.extra}` : ''}` : (jj.stage || jj.status) };
      renderMsg(m);
    });
    const res: unknown = j.result;
    const resRec = isRec(res) ? res : {};
    const tagRec = isRec(resRec['tags']) ? resRec['tags'] : {};
    const tags = Object.entries(tagRec).map(([t, n]): [string, number] => [t, Number(n)]).sort((a, b) => b[1] - a[1]);
    m.text = `Tagged **${resRec['images'] ?? '?'}** images.` + (tags.length
      ? `\n\n${tags.map(([t, n]) => `- \`${t}\` · ${n}`).join('\n')}\n\nThey are in the filter bar under **More filters › Tags**, and in **Load tag…**.`
      : ' No tag matched.');
    m.meta = { ms: Math.round(performance.now() - t0) };
    try { await refreshTagList(); runNow(false); } catch (e) { /* the tags show after a reload */ }
  } catch (e) {
    m.err = errText(e);
  } finally {
    m.job = null;
    renderMsg(m);
    save();
  }
}

// ---------------------------------------------------------------- voice

async function micStart(): Promise<void> {
  if (A.rec !== 'idle') return;
  A.rec = 'starting';
  A.stopAfterStart = false;
  renderMic();
  try {
    await startRecording({ onLimit: () => { void micStop(); } });
    A.rec = 'on';
    renderMic();
    if (A.stopAfterStart) void micStop();
  } catch (e) {
    A.rec = 'idle';
    renderMic();
    note(errMessage(e), true);
  }
}

async function micStop(): Promise<void> {
  if (A.rec === 'starting') { A.stopAfterStart = true; return; }
  if (A.rec !== 'on') return;
  A.rec = 'sending';
  renderMic();
  try {
    const wav = await stopRecording();
    if (!wav) return;
    const r = await postJ<VoiceResult>('api/llm/voice', { wav_b64: wav, screen: 'viz' });
    await voiceAction(r);
  } catch (e) {
    note(`Voice: ${errText(e)}`, true);
  } finally {
    A.rec = 'idle';
    renderMic();
  }
}

interface VoiceResult { transcript?: string; action?: string; args?: Record<string, unknown> | null }

async function voiceAction(r: VoiceResult): Promise<void> {
  const args = r.args || {}, said = String(r.transcript || '').trim(), text = String(args['text'] || said).trim();
  const heard = (): void => { A.msgs.push({ role: 'user', text: said || text, voice: true }); };
  switch (r.action) {
    case 'ask':
      if (!text) break;
      if (A.busy) { $req<HTMLTextAreaElement>('ai-q').value = text; autosize(); return; }
      await send(text);
      { const asked = A.msgs[A.msgs.length - 2]; if (asked) asked.voice = true; }
      renderLog();
      return;
    case 'tab': {
      const tab = typeof args['tab'] === 'string' ? args['tab'] : '';
      if (TABS.includes(tab)) { heard(); note(`Opening **${tab}**.`); if (tab !== curTab) switchTab(tab); return; }
      break;
    }
    case 'filter':
      if (!text || !llmOn('nl_filter')) break;
      heard();
      try { const ex = await runNLFilter(text); note(`Filter applied${ex ? `: ${ex}` : '.'}`); } catch (e) { note(`Filter: ${errText(e)}`, true); }
      return;
    case 'search': {
      const q = $<HTMLInputElement>('text-q'), form = $<HTMLFormElement>('text-search');
      if (!text || !q || !form || q.disabled || form.closest('.hidden')) break;
      heard();
      q.value = text;
      form.requestSubmit();
      note(`Searching boxes for “${text}”.`);
      return;
    }
    default: break;
  }
  if (!said && !text) { note('I did not catch that. Hold the mic button and try again.'); return; }
  heard();
  note('That is not a command I can run. It is in the box below: edit it and press Enter to ask it.');
  $req<HTMLTextAreaElement>('ai-q').value = said || text;
  autosize();
}

// ---------------------------------------------------------------- events

function autosize(): void {
  const t = $req<HTMLTextAreaElement>('ai-q');
  t.style.height = 'auto';
  t.style.height = `${t.scrollHeight + 2}px`;     // css max-height caps it at 6 lines
}

function submit(): void {
  if (A.busy) { A.busy.abort(); return; }
  const t = $req<HTMLTextAreaElement>('ai-q'), text = t.value.trim();
  if (!text) return;
  t.value = '';
  autosize();
  void send(text);
}

async function copy(text: string, btn: HTMLElement): Promise<void> {
  try { await navigator.clipboard.writeText(text); btn.textContent = 'Copied'; } catch (e) { btn.textContent = 'Copy failed'; }
  setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
}

function download(text: string): void {
  const name = String((S.meta && S.meta.dataset) || 'dataset').replace(/[^\w.-]+/g, '_').slice(0, 80) || 'dataset';
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `${name}-report.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function retry(i: number): void {
  const m = A.msgs[i];
  if (!m || !m.retry || A.busy) return;
  const r = m.retry;
  const userBefore = A.msgs[i - 1]?.role === 'user';
  A.msgs.splice(userBefore ? i - 1 : i, userBefore ? 2 : 1);
  if (r.type === 'chat') void send(r.text, r);
  else if (r.type === 'check') void runCheck(r.ids);
  else void runTag(r.body);
}

function quick(id: string): void {
  const q = QUICK.find((x) => x.id === id);
  if (!q || !llmOn(q.f)) return;
  if (id === 'check') { void runCheck(); return; }
  if (id === 'report') { runReport(); return; }
  if (id === 'tag') { void runTag(); return; }
  if (q.f === 'dataset_qa') A.chips.dataset = true;
  if (q.sel) A.chips.sel = true;
  if (q.ev) A.chips.eval = true;
  renderChips();
  void send(q.q ?? '', { ev: !!q.ev, label: q.t });
}

function onClick(e: MouseEvent): void {
  const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-aact]') : null;
  if (!b || (b instanceof HTMLButtonElement && b.disabled)) return;
  const i = +(b.dataset['i'] ?? NaN);
  switch (b.dataset['aact']) {
    case 'clear':
      if (A.busy) A.busy.abort();
      A.msgs = [];
      A.last = null;
      save();
      renderLog();
      renderHead();
      break;
    case 'settings': void openSettings('llm'); break;
    case 'chip': {
      const k = b.dataset['k'];
      if (k === 'sel') A.chips.sel = !selOn();
      else if (k === 'dataset') A.chips.dataset = !A.chips.dataset;
      else if (k === 'eval') A.chips.eval = !A.chips.eval;
      renderChips();
      break;
    }
    case 'quick': quick(b.dataset['q'] ?? ''); break;
    case 'retry': retry(i); break;
    case 'copy': { const m = A.msgs[i]; if (m) void copy(m.text, b); break; }
    case 'dl': { const m = A.msgs[i]; if (m) download(m.text); break; }
    case 'open': void openModal(+(b.dataset['id'] ?? NaN)); break;
    case 'sel-v': {
      const m = A.msgs[i], v = b.dataset['v'];
      if (!m || !m.items) break;
      const del = pts().deleted;
      const ids = m.items.filter((x) => x.verdict === v).map((x) => x.id).filter((id) => !(del && del[id]));
      setSelection(ids, v === 'wrong' ? 'AI: likely wrong labels' : 'AI: unsure labels');
      break;
    }
    default: break;
  }
}

function bind(): void {
  const root = $req('ai-side'), ta = $req('ai-q'), mic = $req('ai-mic');
  root.addEventListener('click', onClick);
  $req('ai-form').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
  ta.addEventListener('input', autosize);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!A.busy) submit(); }
    else if (e.key === 'Escape' && A.busy) { e.preventDefault(); e.stopPropagation(); A.busy.abort(); }
  });
  // mic: hold to talk; a short press toggles (press again to send); keyboard click toggles too
  let down = 0, wasOn = false;
  mic.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    down = Date.now();
    wasOn = A.rec !== 'idle';
    if (!wasOn) void micStart();
  });
  const up = (): void => {
    if (!down) return;
    const held = Date.now() - down;
    down = 0;
    if (wasOn || held > 350) void micStop();
  };
  mic.addEventListener('pointerup', up);
  mic.addEventListener('pointercancel', up);
  mic.addEventListener('click', (e) => { if (e.detail === 0) { if (A.rec === 'idle') void micStart(); else void micStop(); } });
  window.addEventListener('llm-state', () => {
    if (!llmOn('assistant')) {
      if (A.busy) A.busy.abort();
      if (A.rec !== 'idle') { cancelRecording(); A.rec = 'idle'; renderMic(); }
      if (curTab === 'ai') switchTab('explore');
      return;
    }
    renderAll();
  });
  window.addEventListener('selection-changed', () => {
    A.chips.sel = null;                          // a new selection: the chip follows it again
    renderChips();
    if (!A.msgs.length) renderLog();             // quick actions that need a selection
  });
}
