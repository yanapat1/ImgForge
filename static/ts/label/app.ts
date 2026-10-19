// The labeling page (?label=<pid>): markup, loading, navigation, the per-image LRU (bitmap + annotations + versions
// + undo history), prefetch of the ±3 neighbours, the hotkey actions, commit / skip / copy previous, the conflict
// banner and the empty / importing states, the model runs on one image (M, auto on open) and their merge.
// main.js calls start(pid) through a dynamic import.

import { getJSON, errText, postJSON } from '../core/api.js';
import { $req, esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { applyLLM, llmAny } from '../core/llm.js';
import { setTheme, THEME } from '../core/theme.js';
import type { Job, JobsPayload } from '../types.js';
import type { ProjectStatsRow } from '../types-label-ui.js';
import type { Ann, AnnotationsPayload, Change, History, LabelProject, LabelSettings, LabelTask, Media, MediaStatus, PointerPos, SaveResult, TagsMode, Tool } from '../types-label.js';
import { openSettings } from '../settings/dialog.js';
import * as cv from './canvas.js';
import { openExportDialog } from './export.js';
import { edit, newHistory, onEdit, redo, undo } from './history.js';
import { bindHotkeys, combosFor, prettyCombo, rebuildHotkeys } from './hotkeys.js';
import * as P from './panels.js';
import * as M from './model.js';
import type { PredictResult } from './model.js';
import { initSettings, setSettings } from './settings.js';
import { CLASS_KEYS, L, N_TASK_KEYS, TASK_TYPES } from './state.js';
import { bboxTool } from './tools/bbox.js';
import { classifyTool, toggleTag } from './tools/classify.js';
import { keypointsTool } from './tools/keypoints.js';
import { polygonTool } from './tools/polygon.js';
import { cancelMove, clearSelection, cycleOverlap, deleteSelected, duplicateSelected, initSelect, newId, nudge, reassignSelected, setSelection } from './tools/select.js';
import { areaTool, copySelection, pasteClipboard } from './selection.js';
import { busy, conflictsOf, flush, flushMid, initSync, postStatus, resolveConflict, touch } from './sync.js';
import type { SyncEntry } from './sync.js';
import { asEl, closestEl } from './ui-util.js';
import type * as AssistModule from './assist.js';
import type { AssistHooks } from './assist.js';

/** A geometry tool as the page drives it (the optional members exist on some tools only). */
type AppTool = Tool & { reset?(): void; visibility?(): boolean };
/** The Media row plus the model-run marks the server keeps per task. */
type PageMedia = Media & { model_run?: Record<string, number> };
interface PageAnnotations extends Omit<AnnotationsPayload, 'media'> { media: PageMedia }
/** What a model run (or the assistant's accept) returns. */
type RunResult = Pick<PredictResult, 'version' | 'annotations' | 'n_model'>;
/** One image of the LRU: its annotations, versions, undo history and decoded bitmap (loads in flight in pA / pB). */
interface LruEntry {
  mid: string; media: PageMedia | null; anns: Map<string, Ann> | null; versions: Record<string, number>; history: History;
  hidden: Set<string>; bitmap: ImageBitmap | null; pA: Promise<LruEntry> | null; pB: Promise<ImageBitmap> | null;
  ctrl: AbortController; error: unknown;
}
type Action = () => boolean | void;

/** The canvas pointer as a PointerPos (normalised coordinates from the current view transform). */
const pointerPos = (p: { sx: number; sy: number }): PointerPos => ({ sx: p.sx, sy: p.sy, nx: (p.sx - cv.T.tx) / cv.T.ax, ny: (p.sy - cv.T.ty) / cv.T.ay });
const isAbort = (e: unknown): boolean => e instanceof DOMException && e.name === 'AbortError';
const msgOf = (e: unknown): string => (e instanceof Error ? e.message : String(e));

const LRU_MAX = 20;
const PIXEL_BUDGET = 160e6;        // decoded pixels kept in the LRU (~640 MB RGBA); older bitmaps are closed first
const PREFETCH = [1, -1, 2, -2, 3, -3];
const BIG_SIDE = 16384;            // larger images are fetched as a ?max=8192 preview (coordinates stay normalised)
const TOOLS: Record<string, AppTool> = { bbox: bboxTool, polygon: polygonTool, keypoints: keypointsTool, tag: classifyTool };

const cache = new Map<string, LruEntry>();           // media id -> entry (Map order = LRU order, oldest first)
let navTok = 0;
const lastClass = new Map<string, string>();       // task id -> class id picked last
const posKey = (): string => `label-pos-${L.pid}`;
const pid = (): string => encodeURIComponent(L.pid ?? '');

export const tool = (): AppTool | null => {
  const t = P.activeTask();
  return t ? TOOLS[TASK_TYPES[t.task_type]?.geometry ?? ''] || null : null;
};

// ---------------------------------------------------------------- markup
function markup(): string {
  const ib = (id: string, ic: string, label: string, extra = ''): string => `<button id="${id}" class="icon-btn ghost" aria-label="${label}" title="${label}" ${extra}>${icon(ic)}</button>`;
  return `<div id="lb-app" class="lb-app">
  <header class="lb-top">
    <a id="lb-back" class="lb-back" href="./" title="All sessions" aria-label="Back to all sessions">${icon('chev-l')}<span>Sessions</span></a>
    <h1 id="lb-name" class="lb-name"></h1>
    <nav class="lb-nav" aria-label="Images">
      ${ib('lb-prev', 'chev-l', 'Previous image')}
      <label class="lb-count num"><input id="lb-jump" type="number" min="1" aria-label="Go to image number"><span>/</span><span id="lb-total"></span></label>
      ${ib('lb-next', 'chev-r', 'Next image')}
    </nav>
    <span id="lb-pill" class="lb-pill" role="status"></span>
    <span class="grow"></span>
    <div class="lb-actions">
      ${ib('lb-undo', 'undo', 'Undo')}${ib('lb-redo', 'redo', 'Redo')}
      <button id="lb-review" class="lb-review hidden" aria-pressed="false">${icon('check')}<span>Reviewed</span></button>
      <button id="lb-skip" class="lb-skip">${icon('skip')}<span>Skip</span></button>
      <button id="lb-commit" class="primary">${icon('check')}<span>Commit</span><kbd id="lb-commit-key"></kbd></button>
      <button id="lb-ai" class="icon-btn ghost gem hidden" data-llm="label_assist prelabel" aria-haspopup="dialog" aria-expanded="false" aria-controls="lb-assist"
        aria-label="AI label assistant" title="◆ Label assistant: pre-label this image, ask how to label it">${icon('gem')}</button>
      <div id="lb-model" class="lb-model"><button id="lb-model-btn" class="lb-model-btn" aria-haspopup="menu" aria-expanded="false"></button><button id="lb-model-cancel" class="icon-btn ghost hidden" aria-label="Cancel labeling all images" title="Cancel labeling all images">${icon('x')}</button></div>
      ${ib('lb-export', 'download', 'Export labels')}
      <button id="lb-theme" class="icon-btn ghost" aria-label="Switch light / dark theme" title="Light / dark theme">${icon('sun').replace('class="ico"', 'class="ico only-dark"')}${icon('moon').replace('class="ico"', 'class="ico only-light"')}</button>
      ${ib('lb-gear', 'gear', 'Labeling settings')}
    </div>
  </header>
  <aside id="lb-left" class="lb-left" aria-label="Tasks and classes">
    <div class="lb-head"><span>Tasks</span><span class="grow"></span>${ib('lb-hide-l', 'chev-l', 'Hide this panel')}</div>
    <div id="lb-tasks" class="lb-tasks" role="tablist" aria-label="Tasks (Alt+1…9)"></div>
    <div class="lb-head"><span>Classes</span></div>
    <div class="lb-inputs">
      <div class="lb-field">${icon('search')}<input id="lb-filter" type="search" placeholder="Filter classes" aria-label="Filter classes" autocomplete="off"></div>
      <div class="lb-field">${icon('plus')}<input id="lb-add" type="text" placeholder="Add class, Enter" aria-label="Add a class (Enter)" autocomplete="off" maxlength="120"></div>
    </div>
    <div id="lb-classes" class="lb-classes" role="listbox" aria-label="Classes of the active task"></div>
  </aside>
  <div id="lb-split-l" class="lb-split" role="separator" aria-orientation="vertical" aria-label="Resize the classes panel" tabindex="0"></div>
  <main id="lb-stage" class="lb-stage">
    <div id="lb-canvas" class="lb-canvas" tabindex="0" role="application" aria-label="Image canvas: draw and edit annotations"></div>
    <div id="lb-banner" class="lb-banner hidden" data-overlay aria-label="Image tags"></div>
    <div id="lb-conflict" class="lb-conflict hidden" data-overlay role="alert">
      ${icon('alert')}<span id="lb-conflict-msg">This image was changed in another tab.</span>
      <button id="lb-reload">Reload this image</button><button id="lb-keep" class="ghost">Keep mine</button>
    </div>
    <div id="lb-msg" class="lb-msg hidden" role="status" aria-live="polite"></div>
    <button id="lb-show-l" class="lb-show lb-show-l hidden" aria-label="Show the classes panel" title="Show classes">${icon('chev-r')}</button>
    <button id="lb-show-r" class="lb-show lb-show-r hidden" aria-label="Show the objects panel" title="Show objects">${icon('chev-l')}</button>
    <div id="lb-toast" class="lb-toast" role="status" aria-live="polite"></div>
    <aside id="lb-assist" class="lb-assist hidden" data-overlay aria-label="Label assistant"></aside>
  </main>
  <div id="lb-split-r" class="lb-split" role="separator" aria-orientation="vertical" aria-label="Resize the objects panel" tabindex="0"></div>
  <aside id="lb-right" class="lb-right" aria-label="Objects and images">
    <section class="lb-objects-sec">
      <div class="lb-head"><span>Objects</span><span id="lb-objects-n" class="count num"></span><span class="grow"></span>${ib('lb-hide-r', 'chev-r', 'Hide this panel')}</div>
      <div id="lb-objects" class="lb-objects"></div>
    </section>
    <section class="lb-strip-sec">
      <div class="lb-head"><span>Images</span><span id="lb-strip-n" class="count num"></span><span class="grow"></span>
        <select id="lb-strip-filter" aria-label="Show images with this status (active task)">
          <option value="all">All</option><option value="todo">To do</option><option value="in_progress">In progress</option>
          <option value="done">Done</option><option value="skipped">Skipped</option>
          <optgroup id="lb-strip-rv" label="Review" class="hidden" disabled><option value="review:none">Not reviewed</option><option value="review:manual">Reviewed by hand</option><option value="review:bulk">Reviewed with Review all</option></optgroup></select>
        <button id="lb-review-all" class="icon-btn ghost hidden" aria-label="Review all done images" title="Review all: mark every done image as reviewed (shown apart from ones you review by hand)">${icon('check')}</button>
        <button id="lb-strip-more" class="icon-btn ghost lb-strip-more" aria-expanded="false" aria-controls="lb-strip-pop"
          aria-label="Filter images by class and object count" title="Filter by class / object count">${icon('tag')}<span id="lb-strip-badge" class="lb-badge hidden"></span></button></div>
      <div class="lb-inputs lb-strip-inputs"><div class="lb-field">${icon('search')}<input id="lb-strip-q" type="search"
        placeholder="Name: text, cam1*, /regex/" aria-label="Filter images by name: text, wildcard (*, ?) or /regular expression/" autocomplete="off" spellcheck="false"></div></div>
      <div id="lb-strip-pop" class="lb-strip-pop hidden" role="group" aria-label="Image filters"></div>
      <div id="lb-strip" class="lb-strip" aria-label="Images"><div id="lb-strip-sizer" class="lb-strip-sizer"></div>
        <div id="lb-strip-empty" class="lb-empty hidden">No images match</div></div>
    </section>
  </aside>
  <footer class="lb-status" aria-label="Status">
    <span id="lb-xy" class="num lb-xy"></span><span id="lb-zoom" class="num"></span><span id="lb-tool" class="lb-tool-st"></span>
    <span id="lb-cls" class="lb-cls-st"></span><span class="grow"></span><span id="lb-model-chip" class="lb-model-chip hidden" role="status">${icon('robot')}Model labeling…</span><span id="lb-save" class="lb-save"></span>
  </footer>
</div>`;
}

// ---------------------------------------------------------------- LRU entries
function entryOf(mid: string): LruEntry {
  let e = cache.get(mid);
  if (!e) {
    e = { mid, media: null, anns: null, versions: {}, history: newHistory(), hidden: new Set(), bitmap: null, pA: null, pB: null,
      ctrl: new AbortController(), error: null };
    cache.set(mid, e);
  }
  return e;
}
function touchLRU(e: LruEntry): void { cache.delete(e.mid); cache.set(e.mid, e); }

function loadAnns(e: LruEntry, priority: RequestPriority): Promise<LruEntry> {
  if (e.anns) return Promise.resolve(e);
  if (e.pA) return e.pA;
  const signal = e.ctrl.signal;
  e.pA = (async () => {
    if (busy(e.mid)) await flushMid(e.mid);           // a save of this image is still on its way: read after it
    const r = await fetch(`api/label/projects/${pid()}/annotations/${e.mid}`, { signal, priority });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `annotations: ${r.status}`);
    const d: PageAnnotations = await r.json();
    e.media = d.media;
    Object.assign(e.versions, d.versions || {});
    e.anns = new Map((d.annotations || []).map((a) => [a.id, a]));
    P.mediaUpdated(e.media);
    return e;
  })();
  const pA = e.pA;
  pA.catch(() => { if (!e.anns) e.pA = null; });
  return pA;
}

function loadBitmap(e: LruEntry, priority: RequestPriority, dims: { width?: number; height?: number } | null | undefined): Promise<ImageBitmap> {
  if (e.bitmap) return Promise.resolve(e.bitmap);
  if (e.pB) return e.pB;
  const signal = e.ctrl.signal;
  const big = dims && Math.max(dims.width || 0, dims.height || 0) > BIG_SIDE;
  e.pB = fetch(`api/label/projects/${pid()}/image/${e.mid}${big ? '?max=8192' : ''}`, { signal, priority })
    .then((r) => { if (!r.ok) throw new Error(r.status === 404 ? 'The image file is missing from its source' : `image: ${r.status}`); return r.blob(); })
    .then((b) => createImageBitmap(b, { imageOrientation: 'from-image' }))
    .then((bm) => {
      if (signal.aborted || !cache.has(e.mid)) { bm.close(); throw new DOMException('evicted', 'AbortError'); }
      e.bitmap = bm;
      return bm;
    });
  const pB = e.pB;
  pB.catch(() => { if (!e.bitmap) e.pB = null; });
  return pB;
}

// stop the unfinished loads of an entry (it left the prefetch window)
function abortEntry(e: LruEntry): void {
  if ((e.anns || !e.pA) && (e.bitmap || !e.pB)) return;
  e.ctrl.abort();
  e.ctrl = new AbortController();
  if (!e.anns) e.pA = null;
  if (!e.bitmap) e.pB = null;
}

function dropEntry(e: LruEntry): void {
  e.ctrl.abort();
  if (e.bitmap) e.bitmap.close();
  e.bitmap = null;
  cache.delete(e.mid);
}

function evict(keep: Set<string>): void {
  // the image being opened is L.ids[L.idx]: L.media is still the previous one until attach()
  const cur = L.media && L.media.id, target = L.ids[L.idx];
  const kept = (e: LruEntry): boolean => e.mid === cur || e.mid === target || keep.has(e.mid) || predicting(e.mid);
  for (const e of [...cache.values()]) {
    if (cache.size <= LRU_MAX) break;
    if (!kept(e) && !busy(e.mid)) dropEntry(e);
  }
  let px = 0;
  for (const e of cache.values()) if (e.bitmap) px += e.bitmap.width * e.bitmap.height;
  for (const e of cache.values()) {
    if (px <= PIXEL_BUDGET) break;
    if (e.bitmap && !kept(e)) { px -= e.bitmap.width * e.bitmap.height; e.bitmap.close(); e.bitmap = null; e.pB = null; }
  }
}

function windowMids(i: number): Set<string> {
  const s = new Set<string>();
  for (const d of PREFETCH) { const m = L.ids[i + d]; if (i + d >= 0 && m !== undefined) s.add(m); }
  return s;
}

function prefetchAround(i: number): void {
  const want = windowMids(i);
  const cur = L.ids[i];
  for (const e of cache.values()) if (e.mid !== cur && !want.has(e.mid)) abortEntry(e);
  for (const mid of want) {
    const e = entryOf(mid);
    loadAnns(e, 'low').catch(() => {});
    loadBitmap(e, 'low', P.mediaCached(P.idxOf.get(mid)) || e.media).catch(() => {});
  }
  evict(want);
}

// ---------------------------------------------------------------- navigation
function resetTool(): void {
  for (const t of Object.values(TOOLS)) { if (t.cancel) t.cancel(); if (t.reset) t.reset(); }
  cancelMove();
}

export async function show(index: number, force = false): Promise<void> {
  if (!L.ids.length) return;
  const i = Math.max(0, Math.min(L.ids.length - 1, index));
  const mid = L.ids[i];
  if (mid === undefined) return;
  if (!force && L.media && L.media.id === mid && !L.loading) return;
  resetTool();
  flush();                                      // background: never blocks the switch
  const tok = ++navTok;
  L.idx = i;
  L.loading = true;
  try { localStorage.setItem(posKey(), mid); } catch (e) { /* private mode */ }
  P.schedule('top');
  P.revealCurrent();
  const e = entryOf(mid);
  touchLRU(e);
  const ready = e.anns && e.bitmap;
  if (!ready) msg('<div class="spin" aria-hidden="true"></div>Loading…', 'loading');
  const pa = loadAnns(e, 'high');
  const pb = loadBitmap(e, 'high', P.mediaCached(i) || e.media);
  prefetchAround(i);
  let err: unknown = null;
  try { await pa; } catch (x) { err = x; }
  let imgErr: unknown = null;
  try { await pb; } catch (x) { imgErr = x; }
  if (tok !== navTok) return;
  if (err) {
    if (isAbort(err)) return;
    attach(e, null);
    msg(`${icon('alert')}<div><b>Could not load this image's annotations</b><div class="muted">${esc(errText(err))}</div></div><button id="lb-retry">Retry</button>`, 'err');
    return;
  }
  if (imgErr && !isAbort(imgErr)) {
    attach(e, null);
    msg(`${icon('alert')}<div><b>${esc(msgOf(imgErr))}</b><div class="muted">${esc(e.media ? e.media.file_name : '')}: skip it, or fix the source and retry.</div></div>` +
      '<button id="lb-retry">Retry</button><button id="lb-msg-skip">Skip image</button>', 'err');
    return;
  }
  if (!e.bitmap) return;
  attach(e, e.bitmap);
}

function attach(e: LruEntry, bitmap: ImageBitmap | null): void {
  L.media = e.media || { id: e.mid, file_name: '', width: 0, height: 0, status: {}, n: {} };
  L.anns = e.anns || new Map();
  L.versions = e.versions;
  L.history = e.history;
  L.hidden = e.hidden;
  L.selected = new Set();
  L.hoverId = null;
  L.editing.clear();
  L.dirty = new Set();
  L.loading = !bitmap || !e.anns;
  cv.setImage(bitmap, bitmap ? L.media.width : 0, bitmap ? L.media.height : 0);
  cv.shapesChanged();
  if (bitmap) msg('');
  showConflict();
  P.renderAll();
  if (AI) AI.imageChanged();
  if (bitmap && e.anns) autoRun(e);
}

export function go(i: number): void {
  if (i < 0 || i >= L.ids.length) { P.toast(i < 0 ? 'This is the first image' : 'This is the last image'); return; }
  void show(i);
}

export function goToId(mid: string): void { const i = P.idxOf.get(mid); if (i != null) void show(i); }

// the image on screen is fully attached (not mid-switch: L.media would still be the previous image)
const settled = (): boolean => !L.loading && !!L.media && L.media.id === L.ids[L.idx];

async function nextUnlabeled(): Promise<void> {
  if (!settled()) return;
  const tids = L.settings.commit_scope === 'all' ? (L.project?.tasks ?? []).map((t) => t.id) : L.taskId === null ? [] : [L.taskId];
  const i = await P.nextUnfinished(L.idx, tids);
  if (i < 0) P.toast('Every image is done for this task'); else void show(i);
}

function advance(): void {
  if (!L.settings.auto_advance) return;
  if (L.settings.advance_to === 'unlabeled') void nextUnlabeled();
  else if (L.idx < L.ids.length - 1) void show(L.idx + 1);
  else P.toast('That was the last image');
}

function msg(html: string, kind = ''): void {
  const el = $req('lb-msg');
  el.className = `lb-msg ${kind}${html ? '' : ' hidden'}`;
  el.innerHTML = html;
}

// ---------------------------------------------------------------- status changes: commit / skip
function setStatus(kind: 'commit' | 'skip'): void {
  if (!L.media || !L.media.id || !L.project || !settled()) return;   // a repeat press while the next image loads
  const media = L.media;
  const tids = kind === 'skip' || L.settings.commit_scope !== 'all' ? (L.taskId === null ? [] : [L.taskId]) : L.project.tasks.map((t) => t.id);
  const before = { ...media.status };
  media.status = { ...media.status };
  for (const t of tids) media.status[t] = kind === 'commit' ? 'done' : 'skipped';
  if (kind === 'skip') dropReview(media, tids);                       // the server clears it too
  P.mediaUpdated(media);
  postStatus(media.id, kind, tids).then((res) => {
    if (res && res.status) media.status = { ...media.status, ...statusesOf(res.status) };
    P.mediaUpdated(media);
    refreshStats();
  }).catch((err) => {
    media.status = before;
    P.mediaUpdated(media);
    P.toast(`${kind === 'commit' ? 'Commit' : 'Skip'} failed: ${errText(err)}`, 'err');
  });
  advance();
}

// ---------------------------------------------------------------- optional review (a done image approved by hand / by Review all)
/** Keep the entries of a server status map that are known statuses. */
function statusesOf(raw: Record<string, string>): Record<string, MediaStatus> {
  const out: Record<string, MediaStatus> = {};
  for (const [t, v] of Object.entries(raw)) if (v === 'todo' || v === 'in_progress' || v === 'done' || v === 'skipped') out[t] = v;
  return out;
}

function dropReview(m: Media, tids: string[]): void {
  if (!m.review) return;
  m.review = { ...m.review };
  for (const t of tids) delete m.review[t];
}

// toggles: every task in scope already approved by hand -> back to not reviewed, otherwise approve by hand
function setReview(): void {
  if (!L.project || !L.project.review || !L.media || !L.media.id || !settled()) return;
  const media = L.media;
  const scope = L.settings.commit_scope === 'all' ? L.project.tasks.map((t) => t.id) : L.taskId === null ? [] : [L.taskId];
  const tids = scope.filter((t) => media.status && media.status[t] === 'done');
  if (!tids.length) { P.toast('Commit the image first: only done images can be reviewed'); return; }
  const clear = tids.every((t) => media.review && media.review[t] === 'manual');
  const before = media.review || {};
  const review = { ...before };
  media.review = review;
  for (const t of tids) { if (clear) delete review[t]; else review[t] = 'manual'; }
  P.mediaUpdated(media);
  postJSON(`api/label/projects/${L.pid}/review`, { task_ids: tids, mode: clear ? 'clear' : 'manual', media_ids: [media.id] })
    .then(() => refreshStats())
    .catch((err) => { media.review = before; P.mediaUpdated(media); P.toast(`Review failed: ${errText(err)}`, 'err'); });
}

let reviewingAll = false;
async function reviewAll(): Promise<void> {
  if (!L.project || !L.project.review || reviewingAll) return;
  const tids = L.project.tasks.map((t) => t.id);
  if (!window.confirm('Review all: mark every done image that is not reviewed yet as reviewed?\n\nThey are shown apart from the ones you review by hand, and any later edit puts an image back to not reviewed.')) return;
  reviewingAll = true;
  $req<HTMLButtonElement>('lb-review-all').disabled = true;
  try {
    const r = await postJSON<{ changed?: Record<string, number> }>(`api/label/projects/${L.pid}/review`, { task_ids: tids, mode: 'bulk' });
    const n = Object.values(r.changed || {}).reduce((a, b) => a + b, 0);
    for (const e of cache.values()) {                          // the cached copies: done and not reviewed -> bulk
      const m = e.media;
      if (!m || !m.status) continue;
      for (const t of tids) if (m.status[t] === 'done' && !(m.review && m.review[t])) m.review = { ...(m.review || {}), [t]: 'bulk' };
    }
    P.resetMedia();                                            // the strip pages refetch with the new marks
    P.toast(n ? `Reviewed ${n.toLocaleString()} image${n === 1 ? '' : 's'} with Review all` : 'Nothing to review: every done image is reviewed already');
    refreshStats();
  } catch (err) {
    P.toast(`Review all failed: ${errText(err)}`, 'err');
  } finally {
    reviewingAll = false;
    $req<HTMLButtonElement>('lb-review-all').disabled = false;
  }
}

let statsTimer = 0;
function refreshStats(): void {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(() => {
    getJSON<{ projects?: ProjectStatsRow[] }>('api/label/projects').then((d) => P.setStats((d.projects || []).find((p) => p.id === L.pid))).catch(() => {});
  }, 1200);
}

// ---------------------------------------------------------------- classes / tasks
function selectClass(cid: string): void {
  const t = P.activeTask();
  if (!t || !t.classes.some((c) => c.id === cid)) return;
  L.classId = cid;
  lastClass.set(t.id, cid);
  if (t.task_type === 'classification') { if (!L.loading) toggleTag(cid); }
  else if (!L.loading && L.selected.size) reassignSelected(cid);     // a class key on a selection reassigns it
  P.schedule('classes', 'status');
}

function selectClassN(n: number): boolean {
  const t = P.activeTask();
  const c = t && t.classes[n];
  if (!c) return false;
  selectClass(c.id);
  return true;
}

function switchTask(tid: string): void {
  const t = cv.TASKS.get(tid);
  if (!t || tid === L.taskId) return;
  resetTool();
  L.taskId = tid;
  try { localStorage.setItem(`label-task-${L.pid}`, tid); } catch (e) { /* private mode */ }
  const want = lastClass.get(tid);
  L.classId = want !== undefined && t.classes.some((c) => c.id === want) ? want : t.classes[0]?.id ?? null;
  const keep = [...L.selected].filter((id) => { const a = L.anns.get(id); return a && a.task_id === tid; });
  L.selected = new Set(keep);
  cv.shapesChanged();
  P.refreshFilteredStrip();
  P.renderAll();
  M.renderButton();
}

function classesChanged(reload?: boolean): void {
  cv.indexClasses();
  if (reload) {                                 // the server reassigned / deleted annotations: reload them
    // entries with unsaved, held-back or conflicting edits stay (dropping them would lose the edits); they are
    // re-read once those settle (refreshLater), the image on screen through reloadCurrent's own guards
    for (const e of [...cache.values()]) {
      if (e.mid === (L.media && L.media.id)) continue;
      if (busy(e.mid) || predicting(e.mid)) refreshLater(e.mid); else dropEntry(e);
    }
    void reloadCurrent();
  }
  cv.shapesChanged();
  P.renderAll();
}

// Re-read the image on screen from the server (a bulk action or a class change rewrote it). Never over edits that
// are not on the server yet: an open conflict is left to the banner (its Reload resolves the conflict first, then
// calls this), a model run in flight defers the reload to its end (runModel), and pending saves are flushed first;
// if that flush meets a 409, the reload is dropped and the banner shows.
async function reloadCurrent(): Promise<void> {
  if (!L.media) return;
  const mid = L.media.id;
  const deferred = (): boolean => {
    if (conflictsOf(mid).length) return true;
    if (predicting(mid)) { reloadWanted.add(mid); return true; }
    return false;
  };
  if (deferred()) return;
  await flushMid(mid).catch(() => {});
  if (deferred()) return;
  const e = cache.get(mid);
  if (!e) { void show(L.idx, true); return; }
  const old = e.anns;
  e.anns = null; e.pA = null;
  L.loading = true;                             // tools ignore input: an edit now would go to the replaced set
  try { await loadAnns(e, 'high'); } catch (err) {
    if (!e.anns) e.anns = old;
    if (L.media && L.media.id === mid) L.loading = false;
    P.toast(errText(err), 'err');
    return;
  }
  e.history = newHistory();
  if (L.media && L.media.id === mid) {
    const view = { ...L.view };
    attach(e, e.bitmap);
    Object.assign(L.view, view);
    cv.panBy(0, 0);
  }
}

// a cached image that was not on screen when the server rewrote it, but could not be dropped (unsaved / held-back
// edits): drop its annotations once those are saved (the next visit re-reads them). A conflict keeps them.
const refreshing = new Set<string>();
function refreshLater(mid: string): void {
  if (predicting(mid)) { reloadWanted.add(mid); return; }
  if (refreshing.has(mid)) return;                   // one wait per image (staleAnns repeats during a label-all job)
  refreshing.add(mid);
  void flushMid(mid).catch(() => {}).then(() => {
    refreshing.delete(mid);
    const e = cache.get(mid);
    if (!e || conflictsOf(mid).length) return;
    if (predicting(mid)) { reloadWanted.add(mid); return; }
    if (L.media && L.media.id === mid) { if (settled()) void reloadCurrent(); return; }
    if (!busy(mid)) staleEntry(e);
  });
}

function staleEntry(e: LruEntry): void {
  if (e.anns || e.pA) { e.ctrl.abort(); e.ctrl = new AbortController(); e.anns = null; e.pA = null; if (!e.bitmap) e.pB = null; }
}

// ---------------------------------------------------------------- conflicts
function showConflict(): void {
  const c = L.media ? conflictsOf(L.media.id) : [];
  const el = $req('lb-conflict');
  const was = !el.classList.contains('hidden');
  el.classList.toggle('hidden', !c.length);
  if (c.length) {
    const names = c.map((x) => cv.TASKS.get(x.tid)?.name || 'a task').join(', ');
    $req('lb-conflict-msg').textContent = `Another tab saved this image first (${names}). Your latest changes are not saved.`;
  }
  if (was !== !!c.length) cv.overlaysChanged();
}

// ---------------------------------------------------------------- copy previous
async function copyPrev(): Promise<void> {
  const prevMid = L.ids[L.idx - 1];
  if (L.idx <= 0 || L.loading || prevMid === undefined) { P.toast('There is no previous image'); return; }
  const e = entryOf(prevMid);
  try { await loadAnns(e, 'high'); } catch (err) { P.toast(errText(err), 'err'); return; }
  const src = [...(e.anns ?? new Map<string, Ann>()).values()].filter((a) => a.task_id === L.taskId);
  if (!src.length) { P.toast('The previous image has no shapes in this task'); return; }
  const ids = new Map(src.map((a): [string, string] => [a.id, newId()]));
  const copies = src.map((a): Ann => {
    const { version: _version, ...rest } = a;
    return { ...rest, id: ids.get(a.id) ?? newId(), parent_id: a.parent_id && ids.has(a.parent_id) ? ids.get(a.parent_id) ?? null : null, source: 'manual' };
  });
  edit(copies.map((a): Change => [a.id, null, a]));
  setSelection(copies.map((a) => a.id));
  P.toast(`Copied ${copies.length} shape${copies.length === 1 ? '' : 's'} from the previous image`);
}

// ---------------------------------------------------------------- the model on one image (M, auto on open)
// While a request runs, edits of that image are kept local (their autosave waits): the server bumps the version
// when it writes the predictions, so a save sent meanwhile would come back as a 409. The result is merged, not
// attached: only the task's model rows are swapped, every shape the user made meanwhile stays, then the waiting
// edits are saved on top of the new version. A result for an image no longer on screen only updates its LRU entry.
const runs = new Map<string, { deferred: Set<string> }>();            // 'mid|tid' -> {deferred: Set(task ids of image mid edited during the run)}
const predicting = (mid: string): boolean => { for (const k of runs.keys()) if (k.startsWith(mid + '|')) return true; return false; };
const heldAny = (): boolean => { for (const r of runs.values()) if (r.deferred.size) return true; return false; };
const reloadWanted = new Set<string>();    // mids whose server-side rewrite (bulk / class change) waits for their model run
const autoTried = new Set<string>();       // 'mid|tid' auto-run once per page visit (model_run marks it on the server too)

function autoRun(e: LruEntry): void {
  if (!L.project || M.jobRunning()) return;
  // checked once: busy(mid) turns true as soon as the first task's run starts (held edits count as busy)
  if (L.dirty.size || busy(e.mid) || conflictsOf(e.mid).length) return;
  for (const t of L.project.tasks) {
    const link = M.linkOf(t);
    if (!link || !link.auto_on_open || !M.isActive(t)) continue;      // auto-labels only once activated
    const k = `${e.mid}|${t.id}`;
    const ran = e.media && e.media.model_run && e.media.model_run[t.id];
    if (ran || autoTried.has(k)) continue;
    autoTried.add(k);
    void runModel(e, t.id, true);
  }
}

// req: another request that returns the same {version, annotations, n_model} (the ◆ assistant's accept); default the model
async function runModel(e: LruEntry, tid: string, auto: boolean, req: (() => Promise<RunResult>) | null = null): Promise<RunResult | null> {
  const k = `${e.mid}|${tid}`;
  if (runs.has(k)) return null;
  const run = { deferred: new Set<string>() };
  runs.set(k, run);
  M.setChip(runs.size);
  let res: RunResult | null = null, err: unknown = null;
  try {
    if (busy(e.mid)) await flushMid(e.mid);
    res = await (req ? req() : M.predict(e.mid, tid));
  } catch (x) { err = x; }
  runs.delete(k);
  M.setChip(runs.size);
  if (res) mergePredictions(e, tid, res, !req);
  // the edits made meanwhile (deferred by onEdited) are saved now, on top of the new version, unless another run
  // on the same image is still in flight: then they are handed to it (its result bumps the version again) and the
  // last run on the image saves them all. Runs on other images do not matter (their deferred sets are their own).
  const same: Array<{ deferred: Set<string> }> = [];
  for (const [k2, r] of runs) if (k2.startsWith(e.mid + '|')) same.push(r);
  if (same.length) { for (const r of same) for (const t of run.deferred) r.deferred.add(t); }
  else {
    if (run.deferred.size) touch(e.mid, [...run.deferred]);
    if (reloadWanted.delete(e.mid)) {                 // a bulk action / class change waited for this run
      if (L.media && L.media.id === e.mid) { if (settled()) void reloadCurrent(); } else refreshLater(e.mid);
    }
  }
  if (err) P.toast(`${req ? 'Adding the suggestions' : auto ? 'Auto-labeling' : 'The model'} failed: ${errText(err)}`, 'err');
  else if (res && !auto && !req && L.media && L.media.id === e.mid) P.toast(res.n_model ? `The model added ${res.n_model} label${res.n_model === 1 ? '' : 's'}` : 'The model found nothing on this image');
  return res;
}

// markRun: a model run (the assistant's accepted boxes are not one: auto-on-open may still run here)
function mergePredictions(e: LruEntry, tid: string, res: RunResult, markRun = true): void {
  e.versions[tid] = res.version;
  if (e.media) {
    if (markRun) e.media.model_run = { ...(e.media.model_run || {}), [tid]: Date.now() / 1000 };
    if (res.n_model && (!e.media.status || !e.media.status[tid] || e.media.status[tid] === 'todo')) e.media.status = { ...(e.media.status || {}), [tid]: 'in_progress' };
    P.mediaUpdated(e.media);
  }
  const anns = e.anns;
  if (!anns) return;                                    // being reloaded: it reads the new set
  const removed: string[] = [];
  for (const [id, a] of anns) if (a.task_id === tid && a.source === 'model') { anns.delete(id); removed.push(id); }
  for (const a of res.annotations || []) if (a.source === 'model' && a.task_id === tid && !anns.has(a.id)) anns.set(a.id, a);
  if (!(L.media && L.media.id === e.mid && L.anns === anns)) return;
  for (const id of removed) { if (!anns.has(id)) { L.selected.delete(id); L.hidden.delete(id); L.editing.delete(id); if (L.hoverId === id) L.hoverId = null; } }
  cv.shapesChanged();
  P.schedule('objects', 'classes', 'top', 'banner', 'status');
}

function labelThis(): void {
  const t = P.activeTask();
  if (!t || !M.linkOf(t)) { void M.openLinkDialog(); return; }
  if (!M.isActive(t)) { P.toast(`The model is off: activate it first (model menu, or ${M.activateKey()})`, 'err'); return; }
  if (!settled() || !L.media) return;
  if (conflictsOf(L.media.id).length) { P.toast('Resolve the conflict on this image first', 'err'); return; }
  const e = cache.get(L.media.id);
  if (e) void runModel(e, t.id, false);
}

function removeModelHere(): void {
  if (!settled()) return;
  const list = [...L.anns.values()].filter((a) => a.task_id === L.taskId && a.source === 'model');
  if (!list.length) { P.toast('No model labels on this image'); return; }
  const ids = new Set(list.map((a) => a.id));
  const ch = list.map((a): Change => [a.id, a, null]);
  for (const b of L.anns.values()) if (b.parent_id && ids.has(b.parent_id) && !ids.has(b.id)) ch.push([b.id, b, { ...b, parent_id: null }]);
  edit(ch);
  P.toast(`Removed ${list.length} model label${list.length === 1 ? '' : 's'} (${undoKey()} brings them back)`);
}
const undoKey = (): string => prettyCombo(combosFor('undo')[0] || 'ctrl+z');

// a task changed on the server (model link saved, classes added): take the new copy
function taskUpdated(task: LabelTask): void {
  if (!task || !L.project) return;
  const i = L.project.tasks.findIndex((t) => t.id === task.id);
  if (i < 0) return;
  L.project.tasks[i] = task;
  if (L.taskId === task.id && !task.classes.some((c) => c.id === L.classId)) L.classId = task.classes[0]?.id ?? null;
  cv.indexClasses();
  cv.shapesChanged();
  P.renderAll();
  M.renderButton();
}

// labels changed on the server for many images (label all, remove from all): drop the annotations of the cached
// images that have nothing unsaved (bitmaps stay; the next visit re-reads them)
function staleAnns(): void {
  const cur = L.media && L.media.id;
  for (const e of cache.values()) {
    if (e.mid === cur) continue;
    if (busy(e.mid) || predicting(e.mid)) { if (e.anns) refreshLater(e.mid); continue; }
    staleEntry(e);
  }
}

function afterBulk(): void {
  staleAnns();
  if (L.media && L.media.id && settled()) void reloadCurrent();     // guarded: conflict / run in flight / pending saves
  refreshStats();
  P.refreshFilteredStrip();
}

// ---------------------------------------------------------------- hotkey actions
function toggleSetting(key: 'crosshair', label: string): void {
  setSettings({ [key]: !L.settings[key] });
  P.toast(`${label} ${L.settings[key] ? 'on' : 'off'}`);
}

const TAG_MODES: TagsMode[] = ['all', 'hover_selected', 'off'];
const TAG_TEXT: Record<TagsMode, string> = { all: 'Name tags: all', hover_selected: 'Name tags: hovered + selected', off: 'Name tags: off' };

function escape(): boolean {
  if (areaTool.cancel && areaTool.cancel()) { cv.invalidate(cv.D.UI); return true; }   // an area drag mid-press
  const t = tool();
  if (t && t.cancel && t.cancel()) { cv.invalidate(cv.D.UI); return true; }
  if (cancelMove()) return true;
  if (P.closeMenu()) return true;
  if (L.selected.size) { clearSelection(); return true; }
  return false;
}

const ACTIONS: Record<string, Action> = {
  commit: () => setStatus('commit'),
  skip: () => setStatus('skip'),
  review: () => setReview(),
  next: () => go(L.idx + 1),
  prev: () => go(L.idx - 1),
  next_unlabeled: () => { void nextUnlabeled(); },
  undo: () => { if (!undo()) P.toast('Nothing to undo'); },
  redo: () => { if (!redo()) P.toast('Nothing to redo'); },
  delete: () => !L.loading && deleteSelected(),
  cancel: () => escape(),
  crosshair: () => toggleSetting('crosshair', 'Crosshair'),
  tags: () => { const m = TAG_MODES[(TAG_MODES.indexOf(L.settings.tags) + 1) % 3] ?? 'all'; setSettings({ tags: m }); P.toast(TAG_TEXT[m]); },
  fit: () => cv.fit(),
  hide_all: () => { L.hideAll = !L.hideAll; cv.shapesChanged(); P.toast(L.hideAll ? 'All shapes hidden' : 'Shapes shown'); },
  copy_prev: () => { void copyPrev(); },
  duplicate: () => !L.loading && duplicateSelected(),
  cycle_overlap: () => {
    const p = cv.pointer();
    if (!p.in || L.loading) return false;
    const ok = cycleOverlap(pointerPos(p));
    if (ok) { cv.hoverChanged(); P.syncObjectMarks(true); }
    return ok;
  },
  finish_shape: () => { const t = tool(); return !!(t && t.finish && t.finish()); },
  kp_visibility: () => { const t = tool(); return !!(t && t.visibility && t.visibility()); },
  panel_left: () => P.togglePanel('left'),
  panel_right: () => P.togglePanel('right'),
  copy: () => !L.loading && copySelection(),
  paste: () => !L.loading && pasteClipboard(),
  model_this: () => { labelThis(); },
  model_remove: () => { removeModelHere(); },
  model_activate: () => { void M.toggleActive(); },
  theme: () => toggleTheme(),
};
for (let i = 0; i < CLASS_KEYS.length; i++) ACTIONS[`class_${i + 1}`] = () => selectClassN(i);
for (let i = 0; i < N_TASK_KEYS; i++) {
  ACTIONS[`task_${i + 1}`] = () => { const t = L.project && L.project.tasks[i]; if (!t) return false; switchTask(t.id); return true; };
}

// the canvas re-reads its colour tokens on the data-theme change (canvas.js MutationObserver)
function toggleTheme(): void { setTheme(THEME.name === 'dark' ? 'light' : 'dark'); }

// one-time note for people who learned the old keys (v2: classes took the letters, navigation moved)
function hotkeyNote(): void {
  try {
    if (localStorage.getItem('lb-hk-v2')) return;
    localStorage.setItem('lb-hk-v2', '1');
  } catch (e) { return; }
  P.toast('Hotkeys changed: classes now use letters; navigation moved to Ctrl+←/→ / PgUp/PgDn (Settings › Labeling)');
}

const ARROWS: Record<string, [number, number]> = { arrowleft: [-1, 0], arrowright: [1, 0], arrowup: [0, -1], arrowdown: [0, 1] };

function pre(combo: string, e: KeyboardEvent): boolean {
  if (AI && AI.key(combo, e)) return true;
  if (M.modelMenuKey(combo, e)) return true;
  if (P.menuKey(combo, e)) return true;
  const t = asEl(e.target);
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) {
    if (combo === 'escape') { t.blur(); return true; }
    return false;
  }
  const tl = tool();
  if (tl && tl.onKey && !L.loading && tl.onKey(combo)) { cv.invalidate(cv.D.UI); P.schedule('status'); return true; }
  const task = P.activeTask();
  const c = task && task.classes.find((x) => x.hotkey === combo);
  if (c) { selectClass(c.id); return true; }
  const base = combo.replace(/^shift\+/, '');
  const arrow = ARROWS[base];
  if (arrow && !combo.startsWith('ctrl') && !combo.startsWith('alt')) {
    if (t && /^(BUTTON|A)$/.test(t.tagName) && !t.closest('#lb-canvas')) return false;
    const k = combo.startsWith('shift+') ? 10 : 1;
    const [dx, dy] = arrow;
    if (!L.loading && nudge(dx * k, dy * k)) return true;
    cv.panBy(-dx * 60 * k, -dy * 60 * k);
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- handlers from the modules
function onEdited(tasks: Set<string>): void {
  cv.shapesChanged();
  if (L.media) {
    const mid = L.media.id;
    let deferred = false;
    for (const [k, r] of runs) if (k.startsWith(mid + '|')) { for (const t of tasks) r.deferred.add(t); deferred = true; }
    if (!deferred) touch(mid, [...tasks]);
  }
  P.schedule('objects', 'classes', 'top', 'banner', 'status');
}

function onSettings(s: LabelSettings, prev: LabelSettings): void {
  rebuildHotkeys();
  cv.indexClasses();
  cv.invalidate(cv.D.ALL);
  if (prev && prev.commit_scope !== s.commit_scope) P.refreshFilteredStrip();
  if (L.project) P.renderAll();
}

function onSaved(mid: string, tid: string, res: SaveResult | null, error?: string): void {
  if (error) { P.toast(`Not saved: ${error}`, 'err'); return; }
  const e = cache.get(mid);
  const m = e && e.media;
  if (m) {
    const st = res ? statusesOf({ [tid]: res.status }) : {};
    m.status = { ...m.status, ...st };
    if (res && res.n != null) m.n = { ...(m.n || {}), [tid]: res.n };
    dropReview(m, [tid]);
    P.mediaUpdated(m);
  }
  if (L.media && L.media.id === mid) L.dirty.delete(tid);
  refreshStats();
}

// ---------------------------------------------------------------- empty project / running import
async function emptyState(): Promise<void> {
  let job: Job | null | undefined = null;
  const lpid = L.pid ?? '';
  try {
    const { active } = await getJSON<JobsPayload>('api/jobs');
    job = (active || []).find((j) => String(j.key || '').includes(lpid) || String(j.label || '').includes(lpid));
  } catch (e) { /* no jobs api */ }
  if (job) { await followImport(job.id); return; }
  const canRescan = !!(L.project && L.project.source && L.project.source.kind !== 'upload');
  msg(`${icon('image')}<div><b>No images in this session yet</b><div class="muted">Add images from the Sessions home (edit this session),` +
    `${canRescan ? ' or rescan its source folder.' : ' by uploading a folder there.'}</div></div>` +
    `<a class="lb-msg-btn" href="./">${icon('chev-l')}Sessions</a>${canRescan ? `<button id="lb-rescan">${icon('refresh')}Rescan source</button>` : ''}`, 'empty');
}

async function followImport(jid: number): Promise<void> {
  const show1 = (j: Job): void => {
    const pct = j.total ? Math.round(j.pct || 0) : null;
    msg(`<div class="spin" aria-hidden="true"></div><div class="lb-import"><b>Importing images…</b><div class="muted">${esc(j.stage || j.status || '')}` +
      `${j.total ? ` · ${j.done}/${j.total}` : ''}</div><div class="bar${pct == null ? ' indeterminate' : ''}" role="progressbar" aria-label="Import progress"` +
      `${pct != null ? ` aria-valuenow="${pct}"` : ''}><div style="width:${pct != null ? pct : 30}%"></div></div></div>`, 'loading');
  };
  try {
    await waitJob(jid, show1);
  } catch (e) {
    msg(`${icon('alert')}<div><b>The import failed</b><div class="muted">${esc(errText(e))}</div></div>`, 'err');
    return;
  }
  await loadProject();
}

// ---------------------------------------------------------------- ◆ AI label assistant (label/assist.js)
// loaded on the first click: no LLM configured = no download, no fetch
let AI: typeof AssistModule | null = null;
const aiOn = (): boolean => llmAny('label_assist', 'prelabel');

async function toggleAssist(): Promise<void> {
  if (!aiOn()) return;
  if (!AI) {
    try {
      const m = await import('./assist.js');
      m.initAssist($req('lb-assist'), assistHooks);
      AI = m;
    } catch (err) { P.toast(`Could not load the assistant: ${errText(err)}`, 'err'); return; }
  }
  AI.toggle();
}

const assistHooks: AssistHooks = {
  // the image the suggestions are for: only a fully attached one
  mid: () => (settled() && L.media ? L.media.id : null),
  flush: (mid) => flushMid(mid).catch(() => {}),
  actions: ACTIONS,
  selectClass,
  toast: (t, kind) => P.toast(t, kind),
  overlaysChanged: () => cv.overlaysChanged(),
  // accepted boxes go through the model-run path: edits made meanwhile are held, the result is merged as model rows
  accept: async (mid, tid, boxes) => {
    const e = cache.get(mid);
    if (!e || !settled() || L.media?.id !== mid) return null;
    if (conflictsOf(mid).length) { P.toast('Resolve the conflict on this image first', 'err'); return null; }
    return runModel(e, tid, false, () => postJSON<RunResult>(`api/llm/label/${pid()}/accept`, { mid, task_id: tid, boxes }));
  },
};

function bindAssist(): void {
  $req('lb-ai').addEventListener('click', () => { void toggleAssist(); });
  applyLLM($req('lb-app'));                        // the state may have loaded before this markup existed
  window.addEventListener('llm-state', () => { applyLLM($req('lb-app')); if (AI) AI.stateChanged(); });
}

// ---------------------------------------------------------------- start
async function loadProject(): Promise<void> {
  const [project, ids] = await Promise.all([getJSON<LabelProject>(`api/label/projects/${pid()}`), getJSON<{ ids?: string[] }>(`api/label/projects/${pid()}/media-ids`)]);
  L.project = project;
  L.ids = ids.ids || [];
  cv.indexClasses();
  const savedTask = (() => { try { return localStorage.getItem(`label-task-${L.pid}`); } catch (e) { return null; } })();
  const tasks = project.tasks || [];
  L.taskId = (tasks.find((t) => t.id === savedTask) || tasks[0])?.id ?? null;
  const t = P.activeTask();
  L.classId = t?.classes[0]?.id ?? null;
  P.resetMedia();
  P.renderAll();
  M.renderButton();
  refreshStats();
  if (!L.ids.length) { void emptyState(); return; }
  let start = 0;
  try { const m = localStorage.getItem(posKey()); const mi = m ? P.idxOf.get(m) : undefined; if (mi !== undefined) start = mi; } catch (e) { /* private mode */ }
  const q = new URLSearchParams(location.search).get('i');
  if (q && +q >= 1) start = Math.min(L.ids.length - 1, +q - 1);
  msg('');
  void show(start, true);
  hotkeyNote();
}

function bindTop(): void {
  $req('lb-prev').onclick = () => go(L.idx - 1);
  $req('lb-next').onclick = () => go(L.idx + 1);
  const j = $req<HTMLInputElement>('lb-jump');
  j.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const n = parseInt(j.value, 10);
    if (n >= 1 && n <= L.ids.length) { void show(n - 1); j.blur(); } else P.toast(`Enter a number from 1 to ${L.ids.length}`);
  });
  j.addEventListener('blur', () => P.schedule('top'));
  $req('lb-commit').onclick = () => setStatus('commit');
  $req('lb-skip').onclick = () => setStatus('skip');
  $req('lb-review').onclick = () => setReview();
  $req('lb-review-all').onclick = () => { void reviewAll(); };
  $req('lb-undo').onclick = () => undo();
  $req('lb-redo').onclick = () => redo();
  $req('lb-export').onclick = () => { if (L.pid) void openExportDialog(L.pid, L.project); };
  $req('lb-gear').onclick = () => { void openSettings('labeling'); };
  $req('lb-theme').onclick = () => toggleTheme();
  $req('lb-reload').onclick = async () => { if (!L.media) return; const mid = L.media.id; resolveConflict(mid, false); showConflict(); await reloadCurrent(); P.toast('Reloaded the saved version'); };
  $req('lb-keep').onclick = () => { if (L.media) { resolveConflict(L.media.id, true); showConflict(); } };
  $req('lb-msg').addEventListener('click', (e) => {
    const b = closestEl<HTMLButtonElement>(e.target, 'button');
    if (!b) return;
    if (b.id === 'lb-retry') { const e2 = cache.get(L.ids[L.idx] ?? ''); if (e2) { e2.pA = e2.anns ? e2.pA : null; e2.pB = null; } void show(L.idx, true); }
    else if (b.id === 'lb-msg-skip') setStatus('skip');
    else if (b.id === 'lb-rescan') {
      b.disabled = true;
      fetch(`api/label/projects/${pid()}/import`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then((r) => r.json().then((d: { job_id: number; detail?: string }) => (r.ok ? d : Promise.reject(new Error(d.detail || String(r.status))))))
        .then((d) => followImport(d.job_id))
        .catch((err) => { b.disabled = false; P.toast(errText(err), 'err'); });
    }
  });
}

export async function start(projectId: string): Promise<void> {
  L.pid = projectId;
  const root = $req('label');
  root.innerHTML = markup();
  initSettings(onSettings);
  cv.initCanvas($req('lb-canvas'), {
    tool,
    areaTool: () => areaTool,
    hover: () => { cv.hoverChanged(); P.syncObjectMarks(false); },
    pointer: (p) => P.renderPointer(p),
    view: () => P.schedule('status'),
    overlays: () => [...document.querySelectorAll('#lb-stage [data-overlay]:not(.hidden), #lb-left:not(.off), #lb-right:not(.off)')],
  });
  P.initPanels({
    tool,
    selectClass, switchTask, goToId,
    selectAnn: (id: string, add: boolean) => {
      const a = L.anns.get(id);
      if (!a) return;
      if (add) { const s = new Set(L.selected); if (s.has(id)) s.delete(id); else s.add(id); setSelection(s); } else setSelection([id]);
      cv.panTo(a);
    },
    hoverAnn: (id: string | null) => { if (id !== L.hoverId) { L.hoverId = id; cv.hoverChanged(); P.syncObjectMarks(false); } },
    reassign: (id: string, cid: string) => { const a = L.anns.get(id); if (a && a.class_id !== cid) edit([[id, a, { ...a, class_id: cid }]]); },
    toggleHidden: (id: string) => { if (L.hidden.has(id)) L.hidden.delete(id); else L.hidden.add(id); cv.shapesChanged(); P.schedule('objects'); },
    deleteAnn: (id: string) => {
      const a = L.anns.get(id);
      if (!a) return;
      const ch: Change[] = [[id, a, null]];
      for (const b of L.anns.values()) if (b.parent_id === id) ch.push([b.id, b, { ...b, parent_id: null }]);
      edit(ch);
    },
    classesChanged,
    flushAll: async () => { flush(); if (L.media) await flushMid(L.media.id); },
    overlaysChanged: () => cv.overlaysChanged(),
  });
  P.applyPanels();
  initSelect({ selection: () => { P.syncObjectMarks(true); P.schedule('status'); }, toast: P.toast });
  onEdit(onEdited);
  initSync({
    entry: (mid): SyncEntry | null => { const e = cache.get(mid); return e && e.anns ? { anns: e.anns, versions: e.versions } : null; },
    saved: onSaved,
    conflict: (mid: string) => { if (L.media && L.media.id === mid) showConflict(); P.toast('Conflict: another tab saved this image', 'err'); },
    state: () => P.schedule('status'),
    held: (mid?: string) => (mid == null ? heldAny() : predicting(mid)),
  });
  bindHotkeys({ actions: ACTIONS, pre, recording: P.recording });
  M.initModel({ task: () => P.activeTask(), labelThis, removeThis: removeModelHere, taskUpdated, staleAnns, afterBulk });
  bindTop();
  bindAssist();
  window.dataviz = window.dataviz || {};
  window.dataviz.label = { L, cache, perf: cv.perf, show, go, canvas: cv, tool, runs };
  try {
    await loadProject();
  } catch (e) {
    msg(`${icon('alert')}<div><b>Could not open this labeling session</b><div class="muted">${esc(errText(e))}</div></div><a class="lb-msg-btn" href="./">${icon('chev-l')}Sessions</a>`, 'err');
  }
}
