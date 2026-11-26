// The visualizer's ◆ AI assistant, in the right-side dock (core/dock.ts: #ai-side is its host section; the dock's
// head shows the page, the model, the context ring and settings; the ◆ activity icon and Ctrl+. open / collapse it).
// Context chips (Dataset / Evaluation / Selection · n) · the shared ◆ chat
// (core/chat.ts, role 'visual': session header + History, the agent's trace, the composer with model / mode /
// effort / think / web chips and slash commands, hold-to-talk mic). The page's own turns go into the same
// transcript: label-check verdict cards, auto-tag job progress, voice notes. The visual agent's UI actions
// (set_projection, set_color_by, apply_filter, open_tab, show_issue, fit_view) run through the page's own controls
// (applyUi). The scope sent with every message is the page state (viewScope).
// Nothing here runs until the dock is first opened; every piece is drawn only when its feature is on (core/llm.js),
// and a Settings change (window event `llm-state`) re-renders live. Chats are saved on the server; an old
// localStorage `ai-chat:<dataset>:<user id>` history is shown read-only once, then removed.

import { api, errMessage, errText, isTransient, postJSON } from '../core/api.js';
import { userKey } from '../core/auth.js';
import { mountChat, type ChatHandle } from '../core/chat.js';
import { dockHooks, openDock, refreshDock, setDockHost, withNav } from '../core/dock.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { setBar, waitJob } from '../core/jobs.js';
import { applyLLM, llmOn } from '../core/llm.js';
import { meta, pts, S } from '../core/state.js';
import type { Job } from '../types.js';
import type { UiAction, UiResult } from '../types-chat.js';
import { canRecord, cancelRecording, startRecording, stopRecording } from '../core/voice.js';
import { fitToVisible } from '../plot/interactions.js';
import { openSettings } from '../settings/dialog.js';
import { applyRecipe, loadView, onGroupControls, refreshTagList, runLabelQuality, saveSettings, takeViewError, updateMethodControls, updateTextSearch } from './controls.js';
import { writePrep } from './prep.js';
import type { PrepSpec } from '../types.js';
import { scan as curationScan } from './curation.js';
import { runAllNotRun } from './eval.js';
import { runModelOnDataset } from './model.js';
import { applyFilterSpec, filterSpec, runNLFilter, runNow } from './filter-bar.js';
import { setSelection } from './gallery.js';
import { curTab, resizePlots, switchTab } from './layout.js';
import { setAugRecipe } from './augment.js';
import type { AugRecipe } from './augment.js';
import { openModal } from './modal.js';

const CHECK_MAX = 96, SEL_SEND_MAX = 500;
const EXPLAIN_EVAL = 'Explain this evaluation: what is good, what is wrong, and the 3 most useful next steps.';
const TABS = ['explore', 'stats', 'curation', 'aug', 'model', 'eval', 'compare'];
const TAB_NAMES: Record<string, string> = { explore: 'Explore', stats: 'Class stats', curation: 'Curation', aug: 'Augment', model: 'Model', eval: 'Evaluation', compare: 'Compare' };

/** One verdict card of a label check. `id` is a box id. */
interface CheckItem { id: number; verdict: string; suggest?: string | undefined; why?: string | undefined }
type TagBody = { scope: 'selection'; ids: number[] } | { scope: 'all' };
type MicState = 'idle' | 'starting' | 'on' | 'sending';
interface Usage { in?: number | null | undefined; out?: number | null | undefined; ms?: number | null | undefined }

const A = {
  chat: null as ChatHandle | null,
  root: null as HTMLElement | null,
  chips: { dataset: true, eval: false, sel: null as boolean | null },   // sel: null = automatic (on when there is a selection)
  busy: null as AbortController | null,                                 // a label check (the chat has its own)
  last: null as Usage | null,
  rec: 'idle' as MicState,
  stopAfterStart: false,
  imported: new Set<string>(),
};
let built = false;

const isRec = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const strOr = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined);
const isAbort = (e: unknown): boolean => isRec(e) && e['name'] === 'AbortError';

function toCheckItem(x: unknown): CheckItem | null {
  if (!isRec(x)) return null;
  const id = Number(x['id']);
  if (!Number.isFinite(id)) return null;
  return { id, verdict: strOr(x['verdict']) ?? '', suggest: strOr(x['suggest']), why: strOr(x['why']) };
}

// ---------------------------------------------------------------- helpers

const clsOf = (id: number): string => { const c = S.P && S.P.cls[id]; return c === undefined || c === null ? '?' : (meta().classes[c] ?? String(c)); };

function sample(ids: number[], n: number): number[] {               // evenly spread, keeps order
  if (ids.length <= n) return ids.slice();
  const out: number[] = [];
  for (let k = 0; k < n; k++) out.push(ids[Math.floor((k * ids.length) / n)] ?? 0);
  return out;
}

const liveSel = (): number[] => (S.sel || []).filter((id) => !(S.P && S.P.deleted && S.P.deleted[id]));
const selOn = (): boolean => llmOn('ask_selection') && S.sel.length > 0 && A.chips.sel !== false;
const datasetId = (): string => (S.status && S.status.dataset && S.status.dataset.id) || '';
const selVal = (id: string): string => $<HTMLSelectElement>(id)?.value ?? '';

/** What the visual agent is told about the page (roles.visual_scope reads scope.view). */
function viewScope(): Record<string, unknown> {
  const method = selVal('method');
  const params: Record<string, number> = {};
  for (const k of Object.keys(S.status?.methods[method]?.params ?? {})) {
    const inp = $<HTMLInputElement>(`p-${method}-${k}`);
    if (inp) params[k] = +inp.value;
  }
  const names = S.meta?.classes ?? [];
  const mode = selVal('mode');
  const clsName = (v: string): string => names[+v] ?? v;
  const classes = mode === 'single' ? [clsName(selVal('class-a'))] : mode === 'compare' ? [clsName(selVal('class-a')), clsName(selVal('class-b'))]
    : S.hidden.size ? { hidden: [...S.hidden].map((i) => names[i] ?? String(i)) } : 'all';
  const sel = liveSel();
  const view: Record<string, unknown> = {
    tab: curTab, dataset: S.meta?.dataset ?? S.status?.dataset?.name ?? null, feature: selVal('feature'), method, params,
    dims: S.view?.z ? 3 : 2,
    color_by: selVal('color-by'), classes, filter: filterSpec(), issue: S.activeView ?? null,
    selection: { count: sel.length, ids: selOn() ? sample(sel, SEL_SEND_MAX) : [], title: S.selTitle },
  };
  return { view, context: { dataset: A.chips.dataset && llmOn('dataset_qa'), eval: A.chips.eval && llmOn('explain_eval'), selection: selOn() } };
}

// ---------------------------------------------------------------- the agent's page actions

// Find issues buttons the agent may press (show_issue {issue}); `view` is S.activeView once it is on
const ISSUES: Record<string, { btn: string; view: string }> = {
  suspicious: { btn: 'btn-suspicious', view: 'suspicious' }, far: { btn: 'btn-outliers', view: 'far' },
  outliers: { btn: 'btn-outliers', view: 'far' }, far2d: { btn: 'btn-outliers-2d', view: 'far2d' },
  'sel-outliers': { btn: 'btn-sel-outliers', view: 'sel-outliers' }, 'group-outliers': { btn: 'btn-sel-outliers', view: 'sel-outliers' },
  dups: { btn: 'btn-dups', view: 'dups' }, duplicates: { btn: 'btn-dups', view: 'dups' },
  'cl-issues': { btn: 'btn-cl-issues', view: 'cl-issues' }, 'label-issues': { btn: 'btn-cl-issues', view: 'cl-issues' },
  'cl-low': { btn: 'btn-cl-low', view: 'cl-low' }, 'low-quality': { btn: 'btn-cl-low', view: 'cl-low' },
  'cl-ood': { btn: 'btn-cl-ood', view: 'cl-ood' }, ood: { btn: 'btn-cl-ood', view: 'cl-ood' },
  'cl-swap': { btn: 'btn-cl-swap', view: 'cl-swap' }, swap: { btn: 'btn-cl-swap', view: 'cl-swap' },
  'cl-badloc': { btn: 'btn-cl-badloc', view: 'cl-badloc' }, badloc: { btn: 'btn-cl-badloc', view: 'cl-badloc' },
  missed: { btn: 'btn-cl-missed', view: 'cl-missed' }, confusion: { btn: 'btn-cl-confusion', view: '' },
};

const hasOption = (sel: HTMLSelectElement | null, v: string): boolean => !!sel && [...sel.options].some((o) => o.value === v);

/** The touched control pulses once (the user sees what the assistant pressed). */
function touch(el: Element | null): void {
  if (!(el instanceof HTMLElement)) return;
  el.classList.remove('ai-touch');
  void el.offsetWidth;                           // restart the animation
  el.classList.add('ai-touch');
  window.setTimeout(() => el.classList.remove('ai-touch'), 1700);
}
const touchTab = (tab: string): void => touch(document.querySelector(`#activity .act[data-tab="${CSS.escape(tab)}"]`));

/** A map is on screen: what it is and how faithful (the agent's result of set_projection). */
function mapDetail(): string {
  const r = S.viewReq, q = S.view?.info?.quality;
  if (!r) return 'the map is on screen';
  return `map on screen: ${r.feature} · ${r.method} · ${S.view?.z ? '3D' : '2D'}` +
    (q ? ` · trustworthiness ${q.trustworthiness.toFixed(3)}, continuity ${q.continuity.toFixed(3)}` : '');
}
const viewFailed = (): UiResult => { const e = takeViewError(); return { ok: false, detail: e ? errText(e) : 'the map was not generated (a newer request replaced it)', transient: e ? isTransient(e) : false }; };

/** The agent's run actions: the page's own flows (buttons, job polling, refresh), their outcome reported back. */
async function applyRun(a: UiAction): Promise<UiResult | null> {
  const g = a.args;
  const s = (k: string): string => (typeof g[k] === 'string' ? String(g[k]) : '');
  switch (a.action) {
    case 'run_model': {
      touchTab('model');
      const r = await runModelOnDataset(s('model'), typeof g['conf'] === 'number' ? g['conf'] : null);
      touch($('m-run'));
      return r;
    }
    case 'run_eval_all': {
      touchTab('eval');
      const r = await runAllNotRun();
      if (!r.ran) return { ok: true, detail: 'every model already ran on this dataset: the leaderboard is on the Evaluation tab' };
      return r.failed.length ? { ok: r.failed.length < r.ran, detail: `${r.ran - r.failed.length} of ${r.ran} ran; failed: ${r.failed.join(' · ')}` }
        : { ok: true, detail: `${r.ran} model${r.ran === 1 ? '' : 's'} ran: the leaderboard is on the Evaluation tab` };
    }
    case 'run_scan': {
      if (s('kind') === 'cleanlab') {
        if (!S.view && !await loadView()) return viewFailed();
        touch($('issues-btn'));
        return await runLabelQuality() ? { ok: true, detail: 'label-quality check done: the cleanlab views of Find issues are ready' }
          : { ok: false, detail: 'the label-quality check did not finish (see the page message)' };
      }
      switchTab('curation');
      touchTab('curation');
      touch($('cur-scan'));
      return curationScan(s('model') || null);
    }
    case 'compute_stats': {
      if (!S.view && !await loadView()) return viewFailed();
      switchTab('stats');
      touchTab('stats');
      return { ok: true, detail: 'Class stats is open' };
    }
    default: return null;
  }
}

/** Apply one page action through the page's own controls; false / {ok: false} when it cannot be done here. */
async function applyUi(a: UiAction): Promise<boolean | UiResult> {
  const run = await applyRun(a);
  if (run) return run;
  const g = a.args;
  const s = (k: string): string => (typeof g[k] === 'string' ? String(g[k]) : '');
  switch (a.action) {
    case 'set_projection': {
      const fEl = $<HTMLSelectElement>('feature'), mEl = $<HTMLSelectElement>('method');
      const f = s('feature'), m = s('method') || mEl?.value || '';
      if ((f && !hasOption(fEl, f)) || (m && !hasOption(mEl, m)) || !fEl || !mEl) return false;
      if (f) { fEl.value = f; updateTextSearch(); }
      mEl.value = m;
      if (isRec(g['params'])) for (const [k, v] of Object.entries(g['params'])) { const inp = $<HTMLInputElement>(`p-${m}-${k}`); if (inp && v != null) inp.value = String(v); }
      const dims = g['dims'], dEl = $<HTMLSelectElement>('proj-dims');
      if ((dims === 2 || dims === 3) && dEl) dEl.value = String(dims);
      if (isRec(g['prep'])) writePrep(g['prep'] as PrepSpec);     // validated on the server ({} = Default)
      updateMethodControls();
      saveSettings();
      if (curTab !== 'explore') switchTab('explore');
      touch($('map-btn'));
      return await loadView() ? { ok: true, detail: mapDetail() } : viewFailed();
    }
    case 'apply_recipe': {
      if (curTab !== 'explore') switchTab('explore');
      touch($('map-btn'));
      return await applyRecipe(s('id')) ? { ok: true, detail: mapDetail() } : viewFailed();
    }
    case 'set_color_by': {
      const el = $<HTMLSelectElement>('color-by'), by = s('by');
      if (!hasOption(el, by) || !el) return false;
      el.value = by;
      saveSettings();
      await onGroupControls();
      return true;
    }
    case 'apply_filter':
      applyFilterSpec(g['spec']);
      return true;
    case 'set_augment_recipe': {
      const r = g['recipe'];
      if (!r || typeof r !== 'object') return false;
      setAugRecipe(r as AugRecipe);
      touchTab('aug');
      if (curTab !== 'aug') switchTab('aug');
      return { ok: true, detail: 'the Augment tab shows it on the user\'s images now' };
    }
    case 'open_tab': {
      const tab = s('tab');
      if (tab === 'ai') { openDock(false); return true; }           // the assistant itself: the dock
      const btn = document.querySelector<HTMLElement>(`#activity .act[data-tab="${CSS.escape(tab)}"]`);
      if (!TABS.includes(tab) || !btn || btn.classList.contains('hidden')) return false;
      if (tab !== curTab) switchTab(tab);
      touchTab(tab);
      return { ok: true, detail: `${TAB_NAMES[tab] ?? tab} is open` };
    }
    case 'show_issue': {
      const it = ISSUES[s('issue').toLowerCase().replace(/_/g, '-')];
      const btn = it ? $<HTMLButtonElement>(it.btn) : null;
      if (!it || !btn || btn.disabled) return false;
      if (!it.view || S.activeView !== it.view) btn.click();          // pressed again it would turn itself off
      return true;
    }
    case 'fit_view': {
      const fit = $<HTMLButtonElement>('btn-fit');      // the plot's Fit button; the same zoom without it
      if (fit && !fit.disabled) fit.click(); else fitToVisible();
      return true;
    }
    default:
      return false;
  }
}

// ---------------------------------------------------------------- entry points

/** The visualizer's dock host: the dock's own ◆ rail and Ctrl+. open / collapse it; plots resize with it. */
export function initAssistantDock(): void {
  setDockHost({
    id: 'ai-side',
    label: () => `Visualizer · ${TAB_NAMES[curTab] ?? curTab}`,
    enabled: () => llmOn('assistant'),
    mount: (el) => { build(el); return A.chat as ChatHandle; },
    onShow: () => { renderAll(); },
    onResize: () => { resizePlots(); },
    settings: () => { void openSettings('llm'); },
    onHandoff: (h) => { if (h.tab && TABS.includes(h.tab) && h.tab !== curTab) switchTab(h.tab); },
  });
}

function openAi(): void {
  openDock(false);
}

// from other views (Evaluation ◆ Explain): open the tab and ask
export function askAI(text: string, { eval: ev = false }: { eval?: boolean } = {}): void {
  if (!llmOn('assistant')) return;
  openAi();
  if (ev) A.chips.eval = true;
  renderChips();
  void A.chat?.send(text);
}

// gallery ◆ "Check labels"
export function checkLabelsAI(): void {
  if (!llmOn('label_check')) return;
  openAi();
  void runCheck();
}

// ---------------------------------------------------------------- build + render

const GEM_PATHS = '<path class="gem-body" d="M6.5 4h10l4.5 5.5L11.5 21 2 9.5z"/><path class="gem-facet" d="M2 9.5h19M9.3 4 7.2 9.5l4.3 11.5 4.3-11.5L13.7 4M7.2 9.5 11.5 4l4.3 5.5"/>';
const heroGem = (): string => '<svg class="ai-hero-gem" viewBox="0 0 24 24" aria-hidden="true"><defs><linearGradient id="ai-gem-g" x1="0" y1="0" x2="1" y2="1">' +
  '<stop offset="0" style="stop-color:var(--gem-1)"/><stop offset=".55" style="stop-color:var(--gem-2)"/><stop offset="1" style="stop-color:var(--gem-3)"/></linearGradient></defs>' +
  GEM_PATHS + '</svg>';

function build(root: HTMLElement): void {
  A.root = root;
  if (!built) {
    built = true;
    root.innerHTML =
      '<div id="ai-chips" class="ai-chips" role="group" aria-label="What the assistant can see"></div>' +
      '<div id="ai-chat"></div>';
    A.chat = mountChat($req('ai-chat'), {
      role: 'visual',
      inputId: 'ai-q',
      placeholder: 'Ask about this dataset…  / for commands',
      composerExtra: `<button type="button" id="ai-mic" class="icon-btn ghost ai-mic hidden" data-llm="voice" aria-pressed="false" aria-label="Hold to talk (or click to start / stop)" title="Hold to talk · click to start / stop">${icon('mic')}</button>`,
      scope: viewScope,
      sessionScope: () => (datasetId() ? { dataset_id: datasetId() } : null),
      scopeName: 'dataset',
      scopeLabel: (sc) => { const n = sc?.['name']; return typeof n === 'string' && n ? n : sc?.['dataset_id'] === datasetId() ? 'this dataset' : 'dataset'; },
      onUiAction: withNav(applyUi),
      onEvent: dockHooks().onEvent,
      onDone: (t) => { if (t.usage) A.last = { in: t.usage.in, out: t.usage.out, ms: t.ms }; dockHooks().onDone(t); renderHead(); },
      onUpdate: () => refreshDock(),
      settings: (sec) => { void openSettings(sec === 'llm' ? 'llm' : 'prompts').then(() => { if (sec === 'plugins') document.querySelector<HTMLElement>('[data-act="pr-tab"][data-v="plugins"]')?.click(); }); },
      emptyHTML,
      exportName: () => String((S.meta && S.meta.dataset) || 'dataset'),
    });
    bind();
  }
  importOld();
  renderAll();
}

/** The old browser-only history (localStorage ai-chat:*): a session is created to stand for it, the transcript is
 *  shown read-only once, then the key is removed (the server cannot import its messages). */
function importOld(): void {
  const key = userKey(`ai-chat:${datasetId() || 'default'}`);
  if (A.imported.has(key)) return;
  A.imported.add(key);
  let raw: unknown = null;
  try { raw = JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { raw = null; }
  if (!Array.isArray(raw) || !raw.length) return;
  const items = raw.filter(isRec).map((m) => ({ role: m['role'] === 'user' ? 'user' : 'assistant', text: strOr(m['text']) ?? '' })).filter((m) => m.text);
  if (!items.length) { try { localStorage.removeItem(key); } catch (e) { /* ignore */ } return; }
  const first = items.find((m) => m.role === 'user')?.text ?? 'Earlier chat';
  postJSON('api/llm/sessions', { role: 'app', scope: datasetId() ? { dataset_id: datasetId() } : {}, title: `${first.slice(0, 48)} (earlier)` })
    .catch(() => { /* history is best effort */ });
  A.chat?.archive(items);
  try { localStorage.removeItem(key); } catch (e) { /* private window */ }
}

function renderAll(): void {
  renderHead();
  renderChips();
  A.chat?.refresh();
  if (A.root) applyLLM(A.root);
  if (!canRecord()) $req('ai-mic').classList.add('hidden');
}

/** the dock's head shows the model and the context ring */
function renderHead(): void {
  refreshDock();
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
    out.push(chip('sel', `Selection · ${n.toLocaleString()}`, 'layers', selOn(), n ? 'title="The assistant gets the selected box ids (up to 500)"' : 'disabled title="Lasso some dots on the plot first"'));
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
    '<p class="muted">Ask in plain words, or how to see something: the assistant can switch the projection, colour, filter and Find issues view for you. Lasso dots on the plot to ask about exactly those boxes.</p>' +
    (qs.length ? `<div class="ai-qgrid">${qs.map(card).join('')}</div>` : '') + '</div>';
}

const VERDICT: Record<string, string> = { wrong: 'likely wrong', unsure: 'unsure', ok: 'ok' };

function checkHTML(items: CheckItem[], pending: number | null, text: string, err: string | null, ms: number | null): string {
  if (pending !== null) return `<div class="ai-wait">${icon('gem')}Checking ${pending} boxes…</div>`;
  if (err) return `<div class="status-line err">${icon('alert')}<span>${esc(err)}</span></div>`;
  const sorted = items.slice().sort((a, b) => ['wrong', 'unsure', 'ok'].indexOf(a.verdict) - ['wrong', 'unsure', 'ok'].indexOf(b.verdict));
  const n = (v: string): number => sorted.filter((x) => x.verdict === v).length;
  const cards = sorted.map((x) => {
    const v = VERDICT[x.verdict] ? x.verdict : 'unsure';
    return `<div class="ai-vc v-${v}" data-aact="open" data-id="${x.id}" role="button" tabindex="0" title="Open box #${x.id}">` +
      `<img loading="lazy" decoding="async" src="api/crop/${x.id}" alt="">` +
      `<div class="ai-vc-body"><div class="ai-vc-top"><b>${esc(clsOf(x.id))}</b><span class="ai-pill p-${v}">${esc(VERDICT[v])}</span></div>` +
      (x.suggest ? `<div class="ai-vc-sug">${icon('arrow-r')}${esc(x.suggest)}</div>` : '') +
      (x.why ? `<div class="ai-vc-why muted">${esc(x.why)}</div>` : '') + '</div></div>';
  }).join('');
  const ids = (v: string): string => sorted.filter((x) => x.verdict === v).map((x) => x.id).join(',');
  return `<div class="ct-md">${esc(text)}</div>` + (sorted.length ? `<div class="ai-vlist">${cards}</div><div class="ai-actions">` +
    `<button type="button" data-aact="sel-v" data-v="wrong" data-ids="${ids('wrong')}" ${n('wrong') ? '' : 'disabled'}>Select the wrong ones <span class="num muted">${n('wrong')}</span></button>` +
    `<button type="button" data-aact="sel-v" data-v="unsure" data-ids="${ids('unsure')}" ${n('unsure') ? '' : 'disabled'}>Select unsure <span class="num muted">${n('unsure')}</span></button></div>` : '') +
    (ms != null ? `<div class="ct-foot num">${(ms / 1000).toFixed(1)} s</div>` : '');
}

// ---------------------------------------------------------------- page turns: label check, report, auto-tag

async function runCheck(ids0: number[] | null = null): Promise<void> {
  const chat = A.chat;
  if (!chat || A.busy || !llmOn('label_check')) return;
  const all = liveSel();
  const ids = ids0 || sample(all, CHECK_MAX);
  if (!ids.length) { chat.note('Select some boxes first (lasso dots on the plot), then ask me to check them.'); return; }
  const asked = `Check the labels of ${ids.length} selected boxes${all.length > ids.length && !ids0 ? ` (a sample of ${all.length.toLocaleString()})` : ''}`;
  chat.user(asked);
  const st = { items: [] as CheckItem[], pending: ids.length as number | null, text: '', err: null as string | null, ms: null as number | null };
  const turn = chat.custom(() => checkHTML(st.items, st.pending, st.text, st.err, st.ms));
  const ac = new AbortController();
  A.busy = ac;
  const t0 = performance.now();
  try {
    const r = await (await api('api/llm/label-check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }), signal: ac.signal })).json() as
      { items?: unknown[]; usage?: { in?: number | null; out?: number | null } | null };
    st.items = (r.items || []).map(toCheckItem).filter((x): x is CheckItem => x !== null);
    const n = (v: string): number => st.items.filter((x) => x.verdict === v).length;
    st.text = `Checked ${st.items.length} boxes: ${n('wrong')} likely wrong, ${n('unsure')} unsure, ${n('ok')} look fine.` + (n('wrong') ? ' Click a card to open the box.' : '');
    st.ms = Math.round(performance.now() - t0);
    A.last = { in: r.usage?.in, out: r.usage?.out, ms: st.ms };
  } catch (e) {
    st.err = isAbort(e) ? 'Stopped.' : errText(e);
  } finally {
    st.pending = null;
    if (A.busy === ac) A.busy = null;
    turn.update();
    renderHead();
  }
  void chat.record(asked, st.err ? `Label check: ${st.err}` : checkNote(st.text, st.items));
}

/** The label check as the saved chat keeps it: the summary and the boxes found wrong or unsure (ids, why). */
function checkNote(summary: string, items: CheckItem[]): string {
  const rows = items.filter((x) => x.verdict !== 'ok').slice(0, 30)
    .map((x) => `- box ${x.id}: ${x.verdict}${x.suggest ? ` (maybe ${x.suggest})` : ''}${x.why ? `, ${x.why}` : ''}`);
  return [`Label check (shown on the page): ${summary.replace(/ Click a card.*$/, '')}`, ...rows].join('\n');
}

function runReport(): void {
  void A.chat?.send('Write a dataset report in Markdown: overview, classes and balance, image quality, label problems, model results if there are any, and the most useful next steps.',
    { label: 'Write a dataset report', kind: 'report' });
}

async function runTag(): Promise<void> {
  const chat = A.chat;
  if (!chat || !llmOn('auto_tag')) return;
  const body: TagBody = selOn() ? { scope: 'selection', ids: liveSel().slice(0, 20000) } : { scope: 'all' };
  const asked = body.scope === 'selection' ? `Auto-tag the images of the ${body.ids.length.toLocaleString()} selected boxes` : 'Auto-tag every image';
  chat.user(asked);
  let saved = '';
  const st = { job: { text: 'starting…' } as (Partial<Job> & { text: string }) | null, text: '', err: null as string | null };
  const html = (): string => st.job ? `<div class="ai-job"><div class="bar" role="progressbar" aria-label="Auto-tag progress"><div></div></div><span class="muted num">${esc(st.job.text)}</span></div>`
    : st.err ? `<div class="status-line err">${icon('alert')}<span>${esc(st.err)}</span></div>` : `<div class="ct-md">${st.text}</div>`;
  const turn = chat.custom(html);
  const paint = (): void => {
    turn.update();
    const fill = turn.el()?.querySelector<HTMLElement>('.ai-job .bar > div');
    if (fill && st.job) setBar(fill, st.job);
  };
  try {
    const r = await postJSON<{ job_id: number }>('api/llm/tag', body);
    const j = await waitJob(r.job_id, (jj) => {
      st.job = { ...jj, text: jj.total ? `${jj.done}/${jj.total} images${jj.extra ? ` · ${jj.extra}` : ''}` : (jj.stage || jj.status) };
      paint();
    });
    const res = isRec(j.result) ? j.result : {};
    const tagRec = isRec(res['tags']) ? res['tags'] : {};
    const tags = Object.entries(tagRec).map(([t, n]): [string, number] => [t, Number(n)]).sort((a, b) => b[1] - a[1]);
    st.text = `Tagged <b>${esc(res['images'] ?? '?')}</b> images.` + (tags.length
      ? `<ul>${tags.map(([t, n]) => `<li><code>${esc(t)}</code> · ${n}</li>`).join('')}</ul>They are in the filter bar under <b>More filters › Tags</b>, and in <b>Load tag…</b>.`
      : ' No tag matched.');
    saved = `Auto-tag (shown on the page): tagged ${String(res['images'] ?? '?')} images.` +
      (tags.length ? ` Tags: ${tags.slice(0, 30).map(([t, n]) => `${t} (${n})`).join(', ')}.` : ' No tag matched.');
    try { await refreshTagList(); runNow(false); } catch (e) { /* the tags show after a reload */ }
  } catch (e) {
    st.err = errText(e);
    saved = `Auto-tag failed: ${st.err}`;
  } finally {
    st.job = null;
    paint();
  }
  void chat.record(asked, saved);
}

// ---------------------------------------------------------------- voice

function renderMic(): void {
  const b = $req('ai-mic');
  const on = A.rec === 'on' || A.rec === 'starting';
  b.classList.toggle('recording', on);
  b.classList.toggle('sending', A.rec === 'sending');
  b.setAttribute('aria-pressed', String(on));
  if (A.rec !== 'idle') A.chat?.setDraft('');
  const ta = $<HTMLTextAreaElement>('ai-q');
  if (ta) ta.placeholder = A.rec === 'on' ? 'Listening… release (or click) to send' : A.rec === 'starting' ? 'Starting the microphone…'
    : A.rec === 'sending' ? 'Understanding…' : 'Ask about this dataset…  / for commands';
}

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
    A.chat?.note(errMessage(e), 'err');
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
    const r = await postJSON<VoiceResult>('api/llm/voice', { wav_b64: wav, screen: 'viz' });
    await voiceAction(r);
  } catch (e) {
    A.chat?.note(`Voice: ${errText(e)}`, 'err');
  } finally {
    A.rec = 'idle';
    renderMic();
  }
}

interface VoiceResult { transcript?: string; action?: string; args?: Record<string, unknown> | null }

async function voiceAction(r: VoiceResult): Promise<void> {
  const chat = A.chat;
  if (!chat) return;
  const args = r.args || {}, said = String(r.transcript || '').trim(), text = String(args['text'] || said).trim();
  const heard = (): void => { chat.user(said || text, true); };
  switch (r.action) {
    case 'ask':
      if (!text) break;
      if (chat.busy()) { chat.setDraft(text); return; }
      await chat.send(text);
      return;
    case 'tab': {
      const tab = typeof args['tab'] === 'string' ? args['tab'] : '';
      if (TABS.includes(tab)) { heard(); chat.note(`Opening **${tab}**.`); if (tab !== curTab) switchTab(tab); return; }
      break;
    }
    case 'filter':
      if (!text || !llmOn('nl_filter')) break;
      heard();
      try { const ex = await runNLFilter(text); chat.note(`Filter applied${ex ? `: ${ex}` : '.'}`); } catch (e) { chat.note(`Filter: ${errText(e)}`, 'err'); }
      return;
    case 'search': {
      const q = $<HTMLInputElement>('text-q'), form = $<HTMLFormElement>('text-search');
      if (!text || !q || !form || q.disabled || form.closest('.hidden')) break;
      heard();
      q.value = text;
      form.requestSubmit();
      chat.note(`Searching boxes for “${text}”.`);
      return;
    }
    default: break;
  }
  if (!said && !text) { chat.note('I did not catch that. Hold the mic button and try again.'); return; }
  heard();
  chat.note('That is not a command I can run. It is in the box below: edit it and press Enter to ask it.');
  chat.setDraft(said || text);
}

// ---------------------------------------------------------------- events

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
  void A.chat?.send(q.q ?? '', { label: q.t });
}

function onClick(e: MouseEvent): void {
  const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-aact]') : null;
  if (!b || (b instanceof HTMLButtonElement && b.disabled)) return;
  switch (b.dataset['aact']) {
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
    case 'open': void openModal(+(b.dataset['id'] ?? NaN)); break;
    case 'sel-v': {
      const del = pts().deleted;
      const ids = (b.dataset['ids'] ?? '').split(',').filter(Boolean).map(Number).filter((id) => !(del && del[id]));
      setSelection(ids, b.dataset['v'] === 'wrong' ? 'AI: likely wrong labels' : 'AI: unsure labels');
      break;
    }
    default: break;
  }
}

function bind(): void {
  const root = A.root ?? $req('ai-side'), mic = $req('ai-mic');
  root.addEventListener('click', onClick);
  root.addEventListener('keydown', (e) => {          // the verdict cards are role=button
    const t = e.target;
    if ((e.key === 'Enter' || e.key === ' ') && t instanceof HTMLElement && t.dataset['aact'] === 'open') { e.preventDefault(); t.click(); }
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
      if (A.chat?.busy()) A.chat.stop();
      A.busy?.abort();
      if (A.rec !== 'idle') { cancelRecording(); A.rec = 'idle'; renderMic(); }
      refreshDock();
      return;
    }
    renderAll();
  });
  window.addEventListener('selection-changed', () => {
    A.chips.sel = null;                          // a new selection: the chip follows it again
    renderChips();
    A.chat?.refresh();                           // quick actions that need a selection
  });
}
