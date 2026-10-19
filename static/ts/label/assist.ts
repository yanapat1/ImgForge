// ◆ Label assistant: the drawer on the label page (button #lb-ai, loaded by app.js on its first click).
// Pre-label this image / ask a question about it → an answer plus suggested boxes, drawn as a dashed ◆ overlay on
// the canvas (canvas.js setSuggestions). Accepted boxes go to POST api/llm/label/{pid}/accept and come back as
// source='model' rows, merged by app.js exactly like a model run (reviewed with the model-label UI).
// Voice (mic) maps a spoken command to the same actions the toolbar / hotkeys use.
// Nothing here runs unless label_assist or prelabel is on (app.js never loads this module otherwise).

import { apiFetch, errText } from '../core/api.js';
import { $req, esc, icon } from '../core/dom.js';
import { LLM, applyLLM, llmAny, llmOn } from '../core/llm.js';
import { md } from '../core/md.js';
import type { ToastKind } from '../core/toast.js';
import type { AssistBox, AssistItem, AssistResponse, VoiceCommand } from '../types-label-ui.js';
import type { LabelTask, TaskClass } from '../types-label.js';
import * as cv from './canvas.js';
import { L, TASK_TYPES } from './state.js';
import { asEl, closestEl } from './ui-util.js';

type VoiceModule = typeof import('../core/voice.js');

/** What app.ts gives the drawer. */
export interface AssistHooks {
  /** The image the suggestions are for (only a fully attached one), else null. */
  mid(): string | null;
  flush(mid: string): Promise<unknown>;
  /** The toolbar / hotkey actions (a voice command runs one). */
  actions: Record<string, () => unknown>;
  selectClass(classId: string): void;
  toast(msg: string, kind?: ToastKind): void;
  overlaysChanged(): void;
  /** Add accepted boxes through the model-run path; null when refused. */
  accept(mid: string, tid: string, boxes: AssistBoxBody[]): Promise<unknown>;
}
/** A box as POST api/llm/label/{pid}/accept takes it. */
interface AssistBoxBody { class_name: string | undefined; class_id: string | null; x1: number; y1: number; x2: number; y2: number; note: string }

interface AssistState {
  el: HTMLElement | null; open: boolean; mid: string | null; tid: string | null; reqMid?: string | null;
  items: AssistItem[]; answer: string; usage: { in: number; out: number } | null;
  ctrl: AbortController | null; busy: string; rec: boolean; seq: number; err: string;
}
const S: AssistState = { el: null, open: false, mid: null, tid: null, items: [], answer: '', usage: null, ctrl: null, busy: '', rec: false, seq: 0, err: '' };
let H: AssistHooks = { mid: () => null, flush: async () => {}, actions: {}, selectClass: () => {}, toast: () => {}, overlaysChanged: () => {}, accept: async () => null };

/** The drawer element (exists once initAssist ran). */
const root = (): HTMLElement => { if (!S.el) throw new Error('The label assistant is not initialised'); return S.el; };
const isAbort = (e: unknown): boolean => e instanceof DOMException && e.name === 'AbortError';

const pid = (): string => encodeURIComponent(L.pid ?? '');
const on = (): boolean => llmAny('label_assist', 'prelabel');
const task = (tid: string | null = L.taskId): LabelTask | null => (tid === null ? null : cv.TASKS.get(tid) || null);
const isBox = (t: LabelTask | null): boolean => !!t && TASK_TYPES[t.task_type]?.geometry === 'bbox';
const fmt = (n: number | null | undefined): string => (n == null ? '?' : Number(n).toLocaleString());

const GEM = '<svg class="ico lba-gem" viewBox="0 0 24 24" aria-hidden="true"><defs><linearGradient id="lba-gem-grad" x1="0" y1="0" x2="1" y2="1">' +
  '<stop offset="0" style="stop-color:var(--gem-1)"/><stop offset=".55" style="stop-color:var(--gem-2)"/><stop offset="1" style="stop-color:var(--gem-3)"/></linearGradient></defs>' +
  '<path d="M6.5 4h10l4.5 5.5L11.5 21 2 9.5z" fill="url(#lba-gem-grad)" stroke="none"/>' +
  '<path d="M2 9.5h19M9.3 4 7.2 9.5l4.3 11.5 4.3-11.5L13.7 4" stroke="rgba(255,255,255,.55)" stroke-width="1" fill="none"/></svg>';

// ---------------------------------------------------------------- init (once)
export function initAssist(el: HTMLElement, hooks: Partial<AssistHooks>): void {
  H = { ...H, ...hooks };
  S.el = el;
  el.innerHTML = `<div class="lba-head">${GEM}<b>Label assistant</b><span id="lba-model" class="lba-model"></span><span class="grow"></span>
    <button class="icon-btn ghost" data-a="close" aria-label="Close the label assistant" title="Close (Esc)">${icon('x')}</button></div>
  <div class="lba-body">
    <button class="primary lba-pre hidden" data-a="prelabel" data-llm="prelabel">${icon('gem')}<span>Pre-label this image</span></button>
    <div class="lba-ask hidden" data-llm="label_assist">
      <div id="lba-chips" class="lba-chips"></div>
      <div class="lba-input">
        <textarea id="lba-q" rows="2" maxlength="2000" placeholder="e.g. How should I label the hands in this image?" aria-label="Ask about this image (Enter sends, Shift+Enter: new line)"></textarea>
        <div class="lba-input-btns">
          <button class="icon-btn ghost lba-mic hidden" data-a="mic" data-llm="voice" aria-pressed="false" aria-label="Speak a command or a question" title="Voice: next, previous, skip, commit, undo, a class name, or a question">${icon('mic')}</button>
          <button class="icon-btn lba-send" data-a="send" aria-label="Ask" title="Ask (Enter)">${icon('arrow-r')}</button>
        </div>
      </div>
    </div>
    <div id="lba-busy" class="lba-busy hidden" role="status" aria-live="polite"><div class="spin" aria-hidden="true"></div><span id="lba-busy-t"></span><span class="grow"></span>
      <button class="ghost" data-a="cancel">Cancel</button></div>
    <div id="lba-err" class="status-line err hidden" role="alert"></div>
    <div id="lba-answer" class="lba-answer md hidden"></div>
    <div id="lba-boxes" class="lba-boxes" role="list" aria-label="Suggested boxes"></div>
    <div id="lba-usage" class="lba-usage"></div>
  </div>
  <div id="lba-foot" class="lba-foot hidden"><button class="primary" data-a="accept-all"></button><button class="ghost" data-a="clear">Clear</button></div>`;
  el.addEventListener('click', onClick);
  el.addEventListener('change', (e) => {
    const s = closestEl<HTMLSelectElement>(e.target, 'select[data-map]');
    if (!s) return;
    const it = S.items.find((x) => x.key === Number(s.dataset['map']));
    if (it) { it.class_id = s.value || null; renderList(); }
  });
  const list = $req('lba-boxes');
  list.addEventListener('pointerover', (e) => hoverRow(e.target));
  list.addEventListener('focusin', (e) => hoverRow(e.target));
  list.addEventListener('pointerleave', () => cv.hoverSuggestion(-1));
  list.addEventListener('focusout', () => cv.hoverSuggestion(-1));
  $req('lba-q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); }
  });
}

function hoverRow(t: EventTarget | null): void {
  const row = closestEl(t, '[data-row]');
  cv.hoverSuggestion(row ? S.items.findIndex((x) => x.key === Number(row.dataset['row'])) : -1);
}

// ---------------------------------------------------------------- open / close / state
export function toggle(): void { if (S.open) close(); else open(); }

function open(): void {
  if (!on()) return;
  S.open = true;
  root().classList.remove('hidden');
  $req('lb-ai').setAttribute('aria-expanded', 'true');
  renderStatic();
  if (S.mid && S.mid !== H.mid()) clearResults();
  renderResults();
  H.overlaysChanged();
  const q = $req('lba-q');
  const first = !q.closest('.hidden') ? q : root().querySelector<HTMLElement>('.lba-pre:not(.hidden)') || root().querySelector<HTMLElement>('[data-a="close"]');
  if (first) first.focus();
}

function close(): void {
  if (!S.open) return;
  S.open = false;
  stopRec();
  root().classList.add('hidden');
  $req('lb-ai').setAttribute('aria-expanded', 'false');
  cv.setSuggestions([]);                    // the overlay shows only with the drawer
  H.overlaysChanged();
}

export function stateChanged(): void {
  if (!on()) { abort(); close(); return; }
  if (S.open) renderStatic();
}

// app.js attach(): a new image (or the same one reloaded: setImage cleared the overlay, redraw it)
export function imageChanged(): void {
  const mid = H.mid();
  if (S.mid && mid === S.mid) { pushOverlay(); return; }
  if (S.ctrl && S.reqMid !== mid) abort();
  clearResults();
  if (S.open) { renderStatic(); renderResults(); }
}

// hotkeys (app.js pre): Esc closes the drawer from inside it; Ctrl+Enter in the question box asks (not commit)
export function key(combo: string, e: KeyboardEvent): boolean {
  const t = e.target;
  if (!S.open || !(t instanceof Node) || !root().contains(t)) return false;
  if (combo === 'escape') { close(); $req('lb-ai').focus(); return true; }
  if (combo === 'ctrl+enter' && asEl(t)?.id === 'lba-q') { ask(); return true; }
  return false;
}

// ---------------------------------------------------------------- rendering
function renderStatic(): void {
  applyLLM(root());
  const p = LLM.provider;
  $req('lba-model').textContent = p ? p.model || p.name || '' : '';
  $req('lba-model').title = p ? `${p.name || ''} · ${p.model || ''}` : '';
  const c = task()?.classes ?? [];
  const chips = ['What is missing a label here?', c[0] ? `How do I label ${c[0].name}?` : null, 'Are my boxes correct?'].filter((q): q is string => q !== null);
  $req('lba-chips').innerHTML = chips.map((q) => `<button class="lba-chip" data-a="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('');
}

function setBusy(text: string, cancellable = true): void {
  S.busy = text || '';
  $req('lba-busy').classList.toggle('hidden', !S.busy);
  $req('lba-busy-t').textContent = S.busy;
  root().querySelector('[data-a="cancel"]')?.classList.toggle('hidden', !cancellable);
  for (const b of root().querySelectorAll<HTMLButtonElement>('[data-a="prelabel"], [data-a="send"], [data-a="chip"], [data-a="accept"], [data-a="accept-all"]')) b.disabled = !!S.busy;
}

function setErr(t: string): void {
  S.err = t || '';
  const el = $req('lba-err');
  el.classList.toggle('hidden', !S.err);
  el.innerHTML = S.err ? `${icon('alert')}<span>${esc(S.err)}</span>` : '';
}

function clearResults(): void {
  S.mid = null; S.items = []; S.answer = ''; S.usage = null;
  setErr('');
  cv.setSuggestions([]);
  if (S.open) renderResults();
}

// the class a suggestion maps to in the task it was made for (the server's id, else an exact name match)
function classOf(it: AssistBox): TaskClass | null {
  const cs = task(S.tid)?.classes ?? [];
  if (it.class_id != null) { const c = cs.find((x) => x.id === it.class_id); if (c) return c; }
  const n = String(it.class_name || '').trim().toLowerCase();
  return cs.find((x) => x.name.toLowerCase() === n) || null;
}

function pushOverlay(): void {
  if (!S.open) return;
  cv.setSuggestions(S.items.map((it) => {
    const c = classOf(it);
    const note = it.note ? ` · ${String(it.note).slice(0, 40)}` : '';
    return { x1: it.x1, y1: it.y1, x2: it.x2, y2: it.y2, text: `◆ ${c ? c.name : it.class_name || '?'}${note}` };
  }));
}

function renderResults(): void {
  const a = $req('lba-answer');
  a.classList.toggle('hidden', !S.answer);
  a.innerHTML = S.answer ? md(S.answer) : '';
  $req('lba-usage').textContent = S.usage ? `${fmt(S.usage.in)} tokens in · ${fmt(S.usage.out)} out` : '';
  renderList();
}

function renderList(): void {
  const t = task(S.tid);
  const box = isBox(t);
  const cs = t?.classes ?? [];
  $req('lba-boxes').innerHTML = S.items.map((it) => {
    const c = classOf(it);
    const pill = c ? `<span class="lba-pill" style="--c:${esc(c.color || '#888888')}"><i></i>${esc(c.name)}</span>`
      : `<span class="lba-pill unknown" title="Not a class of this task: map it to one to accept it">unknown: ${esc(it.class_name || '?')}</span>` +
        `<select data-map="${it.key}" aria-label="Map ${esc(it.class_name || 'this box')} to a class"><option value="">Map to…</option>` +
        cs.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('') + '</select>';
    const why = !box ? 'Only object-detection tasks take boxes' : !c ? 'Map it to a class first' : 'Accept this box';
    return `<div class="lba-row" role="listitem" data-row="${it.key}" tabindex="-1">${pill}<span class="lba-note">${esc(it.note || '')}</span>` +
      `<button class="icon-btn ghost lba-ok" data-a="accept" data-k="${it.key}" ${c && box && !S.busy ? '' : 'disabled'} aria-label="${esc(why)}" title="${esc(why)}">${icon('check')}</button>` +
      `<button class="icon-btn ghost lba-no" data-a="reject" data-k="${it.key}" aria-label="Reject this box" title="Reject">${icon('x')}</button></div>`;
  }).join('');
  const ok = box ? S.items.filter((it) => classOf(it)) : [];
  $req('lba-foot').classList.toggle('hidden', !S.items.length);
  const all = root().querySelector<HTMLButtonElement>('[data-a="accept-all"]');
  if (all) {
    all.textContent = `Accept all (${ok.length})`;
    all.disabled = !ok.length || !!S.busy;
  }
  pushOverlay();
}

// ---------------------------------------------------------------- requests
async function post<T>(path: string, body: unknown, signal: AbortSignal): Promise<T> {
  const r = await apiFetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
  const d: unknown = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = d && typeof d === 'object' && 'detail' in d ? d.detail : undefined;
    throw new Error(typeof detail === 'string' ? detail : `${path}: ${r.status}`);
  }
  return d as T;
}

function abort(): void {
  if (S.ctrl) S.ctrl.abort();
  S.ctrl = null;
  if (S.open) setBusy('');
}

async function run(kind: 'prelabel' | 'assist', question?: string): Promise<void> {
  const feature = kind === 'prelabel' ? 'prelabel' : 'label_assist';
  if (!llmOn(feature)) return;
  const mid = H.mid();
  if (!mid) { H.toast('Wait for the image to load'); return; }
  const tid = L.taskId;
  abort();
  const ctrl = new AbortController();
  S.ctrl = ctrl; S.reqMid = mid;
  setErr('');
  setBusy(kind === 'prelabel' ? 'Looking for objects…' : 'Thinking…');
  try {
    await H.flush(mid);                         // the model should see the boxes as they are now
    const body: { mid: string; task_id: string | null; question?: string } = { mid, task_id: tid };
    if (question) body.question = question;
    const d = await post<AssistResponse>(`api/llm/label/${pid()}/${kind}`, body, ctrl.signal);
    if (S.ctrl !== ctrl) return;
    if (H.mid() !== mid) return;                // the image changed meanwhile: the answer is about another one
    S.mid = mid; S.tid = tid;
    S.answer = d.answer || '';
    S.usage = d.usage || null;
    S.items = (d.boxes || []).filter((b) => b && b.x2 > b.x1 && b.y2 > b.y1).map((b): AssistItem => ({ ...b, key: ++S.seq }));
    if (kind === 'prelabel' && !S.items.length && !S.answer) S.answer = 'No objects found.';
    if (question) $req<HTMLTextAreaElement>('lba-q').value = '';
  } catch (err) {
    if (isAbort(err) || S.ctrl !== ctrl) return;
    setErr(errText(err));
  } finally {
    if (S.ctrl === ctrl) { S.ctrl = null; setBusy(''); }
  }
  if (S.open) renderResults();
}

function ask(text?: string): void {
  const input = $req<HTMLTextAreaElement>('lba-q');
  const q = (text != null ? text : input.value).trim();
  if (!q) { input.focus(); return; }
  if (text != null) input.value = q;
  void run('assist', q);
}

const boxBody = (it: AssistItem): AssistBoxBody => {
  const c = classOf(it);
  return { class_name: c ? c.name : it.class_name, class_id: c ? c.id : null, x1: it.x1, y1: it.y1, x2: it.x2, y2: it.y2, note: it.note || '' };
};

async function accept(all: AssistItem[]): Promise<void> {
  const items = all.filter((it) => classOf(it));
  if (!items.length || S.busy || !S.mid) return;
  setErr('');
  setBusy(`Adding ${items.length} box${items.length === 1 ? '' : 'es'}…`, false);
  let res: unknown = null;
  try { res = S.tid === null ? null : await H.accept(S.mid, S.tid, items.map(boxBody)); } finally { setBusy(''); }
  if (!res) { renderList(); return; }             // app.js toasted the reason
  const done = new Set(items.map((it) => it.key));
  S.items = S.items.filter((it) => !done.has(it.key));
  renderList();
  H.toast(`Added ${items.length} label${items.length === 1 ? '' : 's'}: review them like model labels`);
}

// ---------------------------------------------------------------- voice
let V: VoiceModule | null = null;
async function voice(): Promise<void> {
  if (!llmOn('voice')) return;
  if (!V) {
    try { V = await import('../core/voice.js'); } catch (e) { H.toast('Voice input is not available', 'err'); return; }
  }
  const btn = root().querySelector('[data-a="mic"]');
  if (!btn) return;
  if (!S.rec) {
    if (!V.canRecord()) { H.toast('This browser cannot record here (it needs https or localhost and a microphone)', 'err'); return; }
    // the 30 s limit ends the take like a second click
    try { await V.startRecording({ onLimit: () => { if (S.rec) void voice(); } }); } catch (err) { H.toast(errText(err), 'err'); return; }
    S.rec = true;
    btn.classList.add('rec'); btn.setAttribute('aria-pressed', 'true');
    return;
  }
  S.rec = false;
  btn.classList.remove('rec'); btn.setAttribute('aria-pressed', 'false');
  const ctrl = new AbortController();
  abort();
  S.ctrl = ctrl;
  setErr('');
  setBusy('Listening…');
  try {
    const wav = await V.stopRecording();
    if (!wav) { H.toast('Nothing was recorded'); return; }
    const classes = (task()?.classes ?? []).map((c) => c.name);
    const d = await post<VoiceCommand>('api/llm/voice', { wav_b64: wav, screen: 'label', classes }, ctrl.signal);
    if (S.ctrl !== ctrl) return;
    S.ctrl = null; setBusy('');
    command(d);
  } catch (err) {
    if (isAbort(err) || S.ctrl !== ctrl) return;
    setErr(errText(err));
  } finally {
    if (S.ctrl === ctrl) { S.ctrl = null; setBusy(''); }
  }
}

function stopRec(): void {
  if (!S.rec || !V) return;
  S.rec = false;
  const btn = root().querySelector('[data-a="mic"]');
  if (btn) { btn.classList.remove('rec'); btn.setAttribute('aria-pressed', 'false'); }
  V.cancelRecording();                      // discard the take, release the microphone
}

const ACTION_TEXT: Record<string, string> = { next: 'next image', prev: 'previous image', skip: 'skip', commit: 'commit', undo: 'undo' };

function command(d: VoiceCommand): void {
  const said = d.transcript ? `“${d.transcript}”` : 'Voice';
  const args = d.args || {};
  const act = d.action ? H.actions[d.action] : undefined;
  const text = d.action ? ACTION_TEXT[d.action] : undefined;
  if (text && act) {
    act();
    H.toast(`◆ ${said} → ${text}`);
  } else if (d.action === 'class') {
    const n = String(args['name'] || args['class'] || args['class_name'] || args['text'] || '').trim().toLowerCase();
    const c = (task()?.classes ?? []).find((x) => x.name.toLowerCase() === n);
    if (c) { H.selectClass(c.id); H.toast(`◆ ${said} → class ${c.name}`); } else H.toast(`◆ ${said}: no class by that name in this task`, 'err');
  } else if (d.action === 'ask' && llmOn('label_assist')) {
    ask(String(args['text'] || d.transcript || ''));
  } else H.toast(`◆ ${said}: not a command here`);
}

// ---------------------------------------------------------------- clicks
function onClick(e: MouseEvent): void {
  const b = closestEl<HTMLButtonElement>(e.target, '[data-a]');
  if (!b || b.disabled) return;
  const a = b.dataset['a'];
  const k = b.dataset['k'];
  const it = k ? S.items.find((x) => x.key === Number(k)) : null;
  if (a === 'close') { close(); $req('lb-ai').focus(); }
  else if (a === 'prelabel') void run('prelabel');
  else if (a === 'send') ask();
  else if (a === 'chip') ask(b.dataset['q']);
  else if (a === 'mic') void voice();
  else if (a === 'cancel') { abort(); stopRec(); }
  else if (a === 'accept' && it) void accept([it]);
  else if (a === 'reject' && it) { S.items = S.items.filter((x) => x !== it); cv.hoverSuggestion(-1); renderList(); }
  else if (a === 'accept-all') void accept(S.items);
  else if (a === 'clear') { clearResults(); }
}
