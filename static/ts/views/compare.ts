// The Compare tab of a combined dataset (a union of 2+ visualizer sessions, data/union.py): pick members A and B,
// run the server's diff (POST api/compare/run, cached per key) and read its per-box columns (binary DVC1:
// status / pair / iou per box id). Summary cards load the image browser (image categories) or the gallery (box
// categories); the map shows A / B / both / only the changes, coloured by origin or by diff kind, over an optional
// density-difference layer. Below: the shared map (Quick = A's own layout, or the Joint projection), the boxes of B
// unlike anything in A, the class table with its reclass confusion, stat histograms with a KS distance, the members
// with stale badges and "Promote to labeling". S.cmp is owned here; plot/render.ts only reads it.

import type * as PL from 'plotly.js';
import type { CmpListKey, CmpParams, CmpResult, CmpRunResult, CmpState, CmpSummary, Job, Points, QuickMap, QuickMapResult, UnionMember, UnionStatus } from '../types.js';
import { api, errText, getJSON, isStatus, postJSON } from '../core/api.js';
import { BOX, decodeColumns, histPair, originMask, selectIds, sharedEdges } from '../core/compare-core.js';
import type { OriginMode } from '../core/compare-core.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { hideWait, showWait, waitJob } from '../core/jobs.js';
import { labelUrl, vizUrl } from '../core/nav.js';
import { meta, pts, S } from '../core/state.js';
import { clsColor, clsName, DIFF_STYLE, diffColor, originColor, plotBase, THEME } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { applyFilterMask, invalidateHover, renderPlot } from '../plot/render.js';
import { at, cfg, withNested } from './chart-util.js';
import { loadView } from './controls.js';
import { openDialog } from './dialog.js';
import { closestOf } from './ev-util.js';
import { setSelection } from './gallery.js';
import { setImageSubset } from './image-grid.js';
import { switchTab } from './layout.js';

declare const Plotly: typeof import('plotly.js');

/** The charts of this tab (layout.ts resizes them). */
export const CMP_PLOTS = ['cmp-confusion', 'cmp-h-area', 'cmp-h-aspect', 'cmp-h-brightness', 'cmp-h-blur'];

interface CmpUi {
  members: UnionMember[];
  params: CmpParams;
  seq: number;                        // bumps per run: a late answer of an older run is dropped
  running: boolean;
  quick: QuickMap | null;             // the last Quick shared map (its `novel` scores the Novel-in-B list)
  novelThr: number;
  chartsDrawn: boolean;               // the confusion heatmap of the current run
  histDrawn: boolean;
  union: UnionStatus | null;
  rerun: ReturnType<typeof setTimeout> | undefined;
}
const U: CmpUi = { members: [], params: {}, seq: 0, running: false, quick: null, novelThr: 0.8, chartsDrawn: false, histDrawn: false,
  union: null, rerun: undefined };

const cmp = (): CmpState => { if (!S.cmp) throw new Error('not a combined dataset'); return S.cmp; };
const alias = (k: number): string => U.members[k]?.alias ?? `#${k}`;
const n = (v: number | undefined): string => (v ?? 0).toLocaleString();

// ---------------------------------------------------------------- setup

/** A combined dataset was opened (api/meta has members): S.cmp, the tab button, the colour options, the panel. */
export function initCompare(points: Points): void {
  const mem = meta().members ?? [];
  if (mem.length < 2) return;
  U.members = mem;
  const origin = points.origin ? Uint8Array.from(points.origin) : new Uint8Array(points.id.length);
  S.cmp = { origin, a: 0, b: 1, show: 'both', mask: null, run: null,
    density: { on: false, G: 64, blur: 1, novelty: null, nA: 0, nB: 0 } };
  syncMask();
  $req('act-compare').classList.remove('hidden');
  const og = $req<HTMLSelectElement>('color-by').querySelector<HTMLOptGroupElement>('optgroup[data-needs="union"]');
  if (og) { og.hidden = false; og.disabled = false; }
  $req('cmp-body').innerHTML = skeleton();
  bindPanel();
  syncPickers();
  window.addEventListener('labels-patched', scheduleRerun);
  window.addEventListener('cmp-density', renderDensityLegend);
  window.addEventListener('theme-changed', () => { U.chartsDrawn = false; U.histDrawn = false; renderAll(); });
}

const seg = (attr: string, items: Array<[string, string, string]>, cur: string): string =>
  `<div class="segmented" role="group">${items.map(([v, label, title]) =>
    `<button type="button" class="view-btn${v === cur ? ' active' : ''}" data-${attr}="${v}" aria-pressed="${v === cur}" title="${esc(title)}">${label}</button>`).join('')}</div>`;

function skeleton(): string {
  return `<div class="cmp-pick">
      <label class="cmp-sel"><span class="cmp-tag" data-side="a">A</span><select id="cmp-a" aria-label="Dataset A"></select></label>
      <button type="button" id="cmp-swap" class="icon-btn ghost" title="Swap A and B" aria-label="Swap A and B">⇄</button>
      <label class="cmp-sel"><span class="cmp-tag" data-side="b">B</span><select id="cmp-b" aria-label="Dataset B"></select></label>
      <button type="button" id="cmp-run" class="primary">${icon('refresh')}Compare</button>
    </div>
    <details class="cmp-params"><summary class="muted">Matching</summary>
      <div class="m-ctl"><span class="m-lbl" title="Boxes of one image pair are matched when their IoU reaches this">match IoU</span>
        <input id="cmp-iou-match" type="range" min="0.1" max="0.9" step="0.05" value="0.5"><span id="cmp-iou-match-v" class="num">0.50</span></div>
      <div class="m-ctl"><span class="m-lbl" title="A matched box with an IoU at least this counts as the same box (else moved)">same IoU</span>
        <input id="cmp-iou-same" type="range" min="0.5" max="1" step="0.01" value="0.9"><span id="cmp-iou-same-v" class="num">0.90</span></div>
      <div class="m-ctl"><span class="m-lbl" title="Images with different content are paired by name: which part of the name">names by</span>
        <select id="cmp-name-key"><option value="rel">path</option><option value="stem">name without extension</option><option value="basename">file name</option></select></div>
    </details>
    <div id="cmp-status" class="cmp-status muted hint" role="status" aria-live="polite"></div>
    <h3 class="cmp-h">Images</h3>
    <div id="cmp-img-cards" class="cmp-cards"></div>
    <h3 class="cmp-h">Boxes</h3>
    <div id="cmp-box-cards" class="cmp-cards"></div>
    <h3 class="cmp-h">On the map</h3>
    <div class="m-ctl"><span class="m-lbl">Show</span>${seg('show', [['a', 'A', 'Only the boxes of A'], ['b', 'B', 'Only the boxes of B'],
      ['both', 'Both', 'Every box of A and B'], ['diff', 'Diff only', 'Only boxes that changed: moved, class changed, removed, added, image in one only']], 'both')}</div>
    <div class="m-ctl"><span class="m-lbl">Colour</span>${seg('color', [['origin', 'Dataset', 'A in one colour, B in another'],
      ['diff', 'Diff', 'Same / moved / class changed / removed / added'], ['class', 'Class', 'The class colours']], '')}</div>
    <div id="cmp-legend" class="cmp-legend"></div>
    <div class="m-ctl"><label class="cmp-check"><input id="cmp-dens" type="checkbox"> Density difference</label></div>
    <div id="cmp-dens-ctl" class="cmp-dens hidden">
      <div class="m-ctl"><span class="m-lbl">grid</span><input id="cmp-dens-g" type="range" min="32" max="160" step="8" value="64"><span id="cmp-dens-g-v" class="num">64</span></div>
      <div class="m-ctl"><span class="m-lbl">blur</span><input id="cmp-dens-blur" type="range" min="0" max="3" step="1" value="1"><span id="cmp-dens-blur-v" class="num">1</span></div>
      <div id="cmp-dens-legend" class="cmp-dens-legend"></div>
    </div>
    <h3 class="cmp-h">Shared map</h3>
    <div class="m-ctl"><span class="m-lbl">Layout</span>${seg('map', [['quick', 'Quick (A’s layout)', 'A keeps its own saved map; every other box is placed next to its nearest A boxes (no new projection). Also scores how novel each B box is.'],
      ['joint', 'Joint', 'One projection of A and B together (the Explore projection controls)']], '')}</div>
    <div id="cmp-map-st" class="muted hint"></div>
    <div id="cmp-novel" class="cmp-novel"></div>
    <h3 class="cmp-h">Classes <span class="muted">click a row: its changed boxes</span></h3>
    <div class="cmp-table-wrap"><table id="cmp-classes" class="class-table cmp-classes"><thead><tr>
      <th>class</th><th class="r">A</th><th class="r">B</th><th class="r">Δ</th><th class="r">Δ%</th><th class="cmp-bar-h" aria-label="change"></th>
      <th class="r" title="boxes B has that A does not">+</th><th class="r" title="boxes A has that B does not">−</th><th class="r" title="boxes that changed class into / out of this class">reclass</th></tr></thead><tbody></tbody></table></div>
    <details id="cmp-conf-d" class="cmp-sec"><summary>Class changes A → B <span class="muted">matched boxes only</span></summary>
      <div id="cmp-confusion" class="cmp-chart"></div></details>
    <details id="cmp-hist-d" class="cmp-sec"><summary>Box statistics <span class="muted">A vs B, KS distance</span></summary>
      <div class="cmp-hists"><div id="cmp-h-area" class="cmp-chart sm"></div><div id="cmp-h-aspect" class="cmp-chart sm"></div>
      <div id="cmp-h-brightness" class="cmp-chart sm"></div><div id="cmp-h-blur" class="cmp-chart sm"></div></div></details>
    <h3 class="cmp-h">Members</h3>
    <div id="cmp-members" class="cmp-members"></div>
    <div class="cmp-foot"><button type="button" id="cmp-promote" class="needs-labeler">${icon('upload')}Promote to labeling…</button></div>`;
}

function syncPickers(): void {
  const C = cmp();
  const opts = U.members.map((m, i) => `<option value="${i}">${esc(m.alias)} · ${esc(m.name)}</option>`).join('');
  $req('cmp-a').innerHTML = $req('cmp-b').innerHTML = opts;
  $req<HTMLSelectElement>('cmp-a').value = String(C.a);
  $req<HTMLSelectElement>('cmp-b').value = String(C.b);
  for (const [side, k] of [['a', C.a], ['b', C.b]] as const) {
    document.querySelectorAll<HTMLElement>(`.cmp-tag[data-side="${side}"]`).forEach((t) => t.style.setProperty('--c', originColor(k)));
  }
}

function bindPanel(): void {
  const body = $req('cmp-body');
  $req('cmp-run').onclick = () => { void runCompare(); };
  $req('cmp-swap').onclick = () => { const C = cmp(); [C.a, C.b] = [C.b, C.a]; syncPickers(); void runCompare(); };
  for (const side of ['a', 'b'] as const) {
    $req<HTMLSelectElement>(`cmp-${side}`).onchange = (e) => {
      const C = cmp(), v = +(e.target as HTMLSelectElement).value;
      if (side === 'a') { if (v === C.b) C.b = C.a; C.a = v; } else { if (v === C.a) C.a = C.b; C.b = v; }
      syncPickers();
      void runCompare();
    };
  }
  const slider = (id: string, digits: number, set: (v: number) => void): void => {
    const el = $req<HTMLInputElement>(id);
    el.addEventListener('input', () => { $req(`${id}-v`).textContent = (+el.value).toFixed(digits); set(+el.value); });
  };
  slider('cmp-iou-match', 2, (v) => { U.params.iou_match = v; });
  slider('cmp-iou-same', 2, (v) => { U.params.iou_same = v; });
  for (const id of ['cmp-iou-match', 'cmp-iou-same']) $req(id).addEventListener('change', () => { void runCompare(); });
  $req<HTMLSelectElement>('cmp-name-key').onchange = (e) => {
    U.params.name_key = (e.target as HTMLSelectElement).value as CmpParams['name_key'] & string;
    void runCompare();
  };
  slider('cmp-dens-g', 0, (v) => { cmp().density.G = v; applyFilterMask(); });
  slider('cmp-dens-blur', 0, (v) => { cmp().density.blur = v; applyFilterMask(); });
  $req<HTMLInputElement>('cmp-dens').onchange = (e) => {
    const on = (e.target as HTMLInputElement).checked;
    cmp().density.on = on;
    $req('cmp-dens-ctl').classList.toggle('hidden', !on);
    if (S.view) renderPlot();
    renderDensityLegend();
  };
  $req('cmp-promote').onclick = () => { void openPromote(); };
  // one delegated listener for the segmented groups, the cards and the lists
  body.addEventListener('click', (e) => {
    const b = closestOf(e, '[data-show], [data-color], [data-map], [data-img], [data-box], [data-sbs], [data-novel], [data-member-refresh]');
    if (!b) return;
    if (b.dataset['show']) { setShow(b.dataset['show'] as OriginMode); return; }
    if (b.dataset['color']) { setColorBy(b.dataset['color']); return; }
    if (b.dataset['map']) { void sharedMap(b.dataset['map']); return; }
    if (b.dataset['sbs'] !== undefined) { e.stopPropagation(); void openSideBySide(b.dataset['sbs'] as CmpListKey); return; }
    if (b.dataset['img']) { openImageList(b.dataset['img'] as CmpListKey); return; }
    if (b.dataset['box']) { openBoxes(b.dataset['box']); return; }
    if (b.dataset['novel'] !== undefined) { showNovel(); return; }
    if (b.dataset['memberRefresh'] !== undefined) void refreshUnion(b as HTMLButtonElement);
  });
  body.addEventListener('keydown', (e) => {
    const t = e.target instanceof HTMLElement ? e.target : null;
    if (t && (e.key === 'Enter' || e.key === ' ') && t.matches('.cmp-card, tr[data-c]')) { e.preventDefault(); t.click(); }
  });
  $req<HTMLTableElement>('cmp-classes').addEventListener('click', (e) => {
    const row = closestOf(e, 'tr[data-c]');
    if (row) selectClassChanges(+(row.dataset['c'] ?? -1));
  });
  $req<HTMLDetailsElement>('cmp-conf-d').addEventListener('toggle', () => { if ($req<HTMLDetailsElement>('cmp-conf-d').open) renderConfusion(); });
  $req<HTMLDetailsElement>('cmp-hist-d').addEventListener('toggle', () => { if ($req<HTMLDetailsElement>('cmp-hist-d').open) renderHists(); });
  body.addEventListener('input', (e) => {
    if (e.target instanceof HTMLInputElement && e.target.id === 'cmp-novel-thr') {
      U.novelThr = +e.target.value;
      renderNovel();
    }
  });
}

/** The tab was opened (layout.ts switchTab): the first run, the members' state. */
export async function showCompareTab(): Promise<void> {
  if (!S.cmp) return;
  syncColorSeg();
  if (!S.cmp.run && !U.running) await runCompare();
  else renderAll();
  void loadMembers();
}

// ---------------------------------------------------------------- run

/** POST api/compare/run (the server caches by key: a re-run after an edit is cheap), then the lists + columns. */
export async function runCompare(): Promise<void> {
  const C = cmp();
  const seq = ++U.seq;
  U.running = true;
  $req('cmp-status').textContent = `Comparing ${alias(C.a)} with ${alias(C.b)}…`;
  $req<HTMLButtonElement>('cmp-run').disabled = true;
  try {
    const params = Object.keys(U.params).length ? U.params : undefined;
    const r = await postJSON<CmpRunResult>('api/compare/run', { a: alias(C.a), b: alias(C.b), ...(params ? { params } : {}) });
    if (seq !== U.seq) return;
    const [res, buf] = await Promise.all([
      getJSON<CmpResult>(`api/compare/result?key=${encodeURIComponent(r.key)}`),
      api(`api/compare/columns?key=${encodeURIComponent(r.key)}`).then((x) => x.arrayBuffer()),
    ]);
    if (seq !== U.seq) return;
    const { cols } = decodeColumns(buf);
    const N = C.origin.length;
    const status = cols['status'] instanceof Uint8Array ? cols['status'] : new Uint8Array(N);
    const pair = cols['pair'] instanceof Int32Array ? cols['pair'] : new Int32Array(N).fill(-1);
    const iou = cols['iou'] instanceof Float32Array ? cols['iou'] : new Float32Array(N);
    if (status.length !== N) throw new Error(`compare columns: ${status.length} boxes, the page has ${N} (reload the page)`);
    C.run = { key: r.key, summary: r.summary, lists: res.image_lists, status, pair, iou };
    syncParams(r.summary);
    U.chartsDrawn = false;
    U.histDrawn = false;
    $req('cmp-status').textContent = '';
    afterRun();
  } catch (e) {
    if (seq === U.seq) {
      $req('cmp-status').innerHTML = `<span class="txt-danger">${icon('alert')}${esc(errText(e))}</span>`;
      toast(errText(e), 'err');
    }
  } finally {
    if (seq === U.seq) { U.running = false; $req<HTMLButtonElement>('cmp-run').disabled = false; }
  }
}

function syncParams(sm: CmpSummary): void {
  const p = sm.params || {};
  if (p.iou_match != null) { $req<HTMLInputElement>('cmp-iou-match').value = String(p.iou_match); $req('cmp-iou-match-v').textContent = p.iou_match.toFixed(2); }
  if (p.iou_same != null) { $req<HTMLInputElement>('cmp-iou-same').value = String(p.iou_same); $req('cmp-iou-same-v').textContent = p.iou_same.toFixed(2); }
  if (p.name_key) $req<HTMLSelectElement>('cmp-name-key').value = p.name_key;
}

// a new run (or a patched one): mask, hover, plot, cards, table, open charts
function afterRun(): void {
  syncMask();
  invalidateHover();
  const by = $req<HTMLSelectElement>('color-by').value;
  if (S.view) { if (by === 'diff' || by === 'origin') renderPlot(); else applyFilterMask(); }
  renderAll();
}

function renderAll(): void {
  renderCards();
  renderLegend();
  renderClassTable();
  renderNovel();
  renderDensityLegend();
  syncShowSeg();
  syncColorSeg();
  if ($req<HTMLDetailsElement>('cmp-conf-d').open) renderConfusion();
  if ($req<HTMLDetailsElement>('cmp-hist-d').open) renderHists();
  const r = S.cmp?.run;
  $req('cmp-sub').textContent = r ? `${alias(r.summary.a)} vs ${alias(r.summary.b)}` : '';
}

// a label edit (curation, the image editor, the live feed): the server keeps its diff current, ask again
function scheduleRerun(): void {
  if (!S.cmp?.run) return;
  clearTimeout(U.rerun);
  U.rerun = setTimeout(() => { void runCompare(); }, 600);
}

/** The live feed's diff patch of one compare key: new statuses for some boxes and the new summary. */
export function patchDiff(key: string, status: Record<string, number> | undefined, summary: CmpSummary | undefined): void {
  const r = S.cmp?.run;
  if (!r || r.key !== key) return;
  for (const [sid, st] of Object.entries(status ?? {})) { const i = +sid; if (i >= 0 && i < r.status.length) r.status[i] = st; }
  if (summary) r.summary = summary;
  U.chartsDrawn = false;
  U.histDrawn = false;
  afterRun();
}

// ---------------------------------------------------------------- map: show / colour

function syncMask(): void {
  const C = S.cmp;
  if (!C) return;
  // "both" with exactly two members hides nothing: no mask, so visibleIndices skips the test
  if (C.show === 'both' && U.members.length === 2) { C.mask = null; return; }
  C.mask = originMask(C.origin, C.run ? C.run.status : null, C.show, C.a, C.b, C.mask ?? undefined);
}

export function setShow(mode: OriginMode): void {
  const C = cmp();
  C.show = mode;
  syncMask();
  syncShowSeg();
  if (S.view) applyFilterMask();          // a restyle of the existing traces, no full render
}

function syncShowSeg(): void {
  const cur = S.cmp?.show ?? 'both';
  document.querySelectorAll<HTMLElement>('#cmp-body [data-show]').forEach((b) => {
    b.classList.toggle('active', b.dataset['show'] === cur);
    b.setAttribute('aria-pressed', String(b.dataset['show'] === cur));
  });
}

function setColorBy(v: string): void {
  const sel = $req<HTMLSelectElement>('color-by');
  sel.value = v;
  sel.dispatchEvent(new Event('change', { bubbles: true }));    // main.ts: re-render + remembered settings
  syncColorSeg();
  renderLegend();
}

function syncColorSeg(): void {
  const cur = $req<HTMLSelectElement>('color-by').value;
  document.querySelectorAll<HTMLElement>('#cmp-body [data-color]').forEach((b) => {
    b.classList.toggle('active', b.dataset['color'] === cur);
    b.setAttribute('aria-pressed', String(b.dataset['color'] === cur));
  });
}

function renderLegend(): void {
  const el = $('cmp-legend');
  if (!el || !S.cmp) return;
  const by = $req<HTMLSelectElement>('color-by').value, C = S.cmp;
  const item = (c: string, t: string): string => `<span class="cmp-li"><span class="dot" style="background:${c}"></span>${esc(t)}</span>`;
  el.innerHTML = by === 'origin' ? [C.a, C.b].map((k) => item(originColor(k), alias(k))).join('')
    : by === 'diff' ? DIFF_STYLE.map((d) => item(d.color, d.label)).join('') : '';
}

function renderDensityLegend(): void {
  const el = $('cmp-dens-legend');
  if (!el || !S.cmp) return;
  const C = S.cmp, d = C.density;
  if (!d.on) { el.innerHTML = ''; return; }
  const pct = d.novelty == null ? '–' : `${(d.novelty * 100).toFixed(1)}%`;
  el.innerHTML = `<div class="cmp-grad" style="--ca:${originColor(C.a)};--cb:${originColor(C.b)}"></div>` +
    `<div class="cmp-grad-l"><span>${esc(alias(C.a))} denser</span><span>same</span><span>${esc(alias(C.b))} denser</span></div>` +
    `<div class="muted hint" title="share of B's boxes that fall in grid cells with no A box at all">novelty: <b class="num">${pct}</b> of ${esc(alias(C.b))} where ${esc(alias(C.a))} has nothing` +
    ` · ${n(d.nA)} / ${n(d.nB)} boxes</div>`;
}

// ---------------------------------------------------------------- summary cards

type BoxCard = [key: string, label: string, count: (s: CmpSummary) => number, kind: number, status: number[], side: 'a' | 'b' | null, title: string];
const BOX_CARDS: BoxCard[] = [
  ['same', 'Same', (s) => s.boxes.same, 0, [BOX.SAME], null, 'Matched with an IoU at or above "same IoU" and the same class (both sides)'],
  ['moved', 'Moved', (s) => s.boxes.moved, 1, [BOX.MOVED], null, 'Matched, same class, but the box moved or changed size (both sides)'],
  ['reclass', 'Class changed', (s) => s.boxes.reclass, 2, [BOX.RECLASS, BOX.RECLASS_MOVED], null, 'Matched, another class in B (both sides)'],
  ['removed', 'Removed', (s) => s.boxes.removed, 3, [BOX.UNMATCHED], 'a', 'A box of A with no partner in B’s copy of the image'],
  ['added', 'Added', (s) => s.boxes.added, 4, [BOX.UNMATCHED], 'b', 'A box of B with no partner in A’s copy of the image'],
  ['only_a', 'Image only in A', (s) => s.boxes.image_only_a, 5, [BOX.IMAGE_ONLY], 'a', 'Boxes of images B does not have'],
  ['only_b', 'Image only in B', (s) => s.boxes.image_only_b, 6, [BOX.IMAGE_ONLY], 'b', 'Boxes of images A does not have'],
];
type ImgCard = [key: CmpListKey, label: string, count: (s: CmpSummary) => number, color: () => string, title: string];
const IMG_CARDS: ImgCard[] = [
  ['only_a', 'Only in A', (s) => s.images.only_a, () => diffColor(5), 'Images of A with no match in B (by content, then by name)'],
  ['only_b', 'Only in B', (s) => s.images.only_b, () => diffColor(6), 'Images of B with no match in A'],
  ['same', 'Identical', (s) => s.images.same, () => diffColor(0), 'Same pixels and the same boxes'],
  ['labels_changed', 'Labels differ', (s) => s.images.labels_changed, () => diffColor(2), 'Same pixels, different boxes or classes'],
  ['pixels_changed', 'Pixels differ', (s) => s.images.pixels_changed, () => diffColor(1), 'Same name, different image content'],
  ['dup', 'Duplicates', (s) => s.images.dup_a + s.images.dup_b, () => THEME.muted, 'Images that appear more than once inside one side (same content)'],
];

function renderCards(): void {
  const r = S.cmp?.run;
  const sm = r?.summary;
  const card = (attr: string, key: string, label: string, v: number | null, color: string, title: string, extra = ''): string =>
    `<div class="cmp-card${v ? '' : ' zero'}" role="button" tabindex="0" data-${attr}="${key}" style="--c:${color}" title="${esc(title)}">` +
    `<b class="num">${v == null ? '–' : n(v)}</b><span>${esc(label)}</span>${extra}</div>`;
  $req('cmp-img-cards').innerHTML = IMG_CARDS.map(([k, label, f, col, title]) => {
    const v = sm ? f(sm) : null;
    const sbs = v
      ? `<button type="button" class="cmp-sbs icon-btn ghost" data-sbs="${k}" title="Open these images side by side (A | B)" aria-label="Side by side">${icon('layers')}</button>` : '';
    return card('img', k, label, v, col(), `${title}${sm ? ' · click: these images in the browser' : ''}`, sbs);
  }).join('');
  $req('cmp-box-cards').innerHTML = BOX_CARDS.map(([k, label, f, kind, , , title]) =>
    card('box', k, label, sm ? f(sm) : null, diffColor(kind), `${title}${sm ? ' · click: these boxes in the gallery' : ''}`)).join('');
}

function openImageList(k: CmpListKey): void {
  const r = S.cmp?.run;
  const names = r?.lists?.[k];
  if (!r || !names) return;
  const card = IMG_CARDS.find((c) => c[0] === k);
  const cut = names.length < (card ? card[2](r.summary) : 0);      // the server sends at most a few thousand names
  setSelection([], 'No selection');
  setImageSubset(names, `${card?.[1] ?? k}: ${alias(r.summary.a)} vs ${alias(r.summary.b)}${cut ? ` (first ${n(names.length)})` : ''}`);
  switchTab('explore');
}

function openBoxes(key: string): void {
  const C = S.cmp, r = C?.run;
  const c = BOX_CARDS.find((x) => x[0] === key);
  if (!C || !r || !c) return;
  const side = c[5] === 'a' ? C.a : c[5] === 'b' ? C.b : null;
  let ids = selectIds(r.status, C.origin, c[4], side);
  if (side === null) ids = ids.filter((id) => C.origin[id] === C.a || C.origin[id] === C.b);
  $req<HTMLSelectElement>('sort').value = 'image';
  setSelection(ids, `${c[1]}: ${alias(C.a)} vs ${alias(C.b)}`);
  switchTab('explore');
}

async function openSideBySide(k: CmpListKey): Promise<void> {
  const names = S.cmp?.run?.lists?.[k];
  if (!names || !names.length) return;
  const { openCompareViewer } = await import('./compare-viewer.js');
  await openCompareViewer(names[0] ?? '', names);
}

// ---------------------------------------------------------------- shared map + novel in B

async function sharedMap(kind: string): Promise<void> {
  const C = cmp();
  document.querySelectorAll<HTMLElement>('#cmp-body [data-map]').forEach((b) => {
    b.classList.toggle('active', b.dataset['map'] === kind);
    b.setAttribute('aria-pressed', String(b.dataset['map'] === kind));
  });
  const st = $req('cmp-map-st');
  if (kind === 'joint') {
    st.textContent = 'Joint projection of every member (the projection controls above the map).';
    U.quick = null;
    renderNovel();
    await loadView();
    return;
  }
  const feature = $req<HTMLSelectElement>('feature').value;
  const body = { ref: alias(C.a), feature };
  st.textContent = `Placing every box on ${alias(C.a)}'s own map…`;
  try {
    let r = await postJSON<QuickMapResult>('api/compare/quickmap', body);
    while (!r.ready) {
      await waitJob(r.job_id, (j: Job) => showWait(j));
      r = await postJSON<QuickMapResult>('api/compare/quickmap', body);
    }
    hideWait();
    U.quick = r;
    S.view = r;
    S.viewPos = new Map(r.id.map((id, i): [number, number] => [id, i]));
    S.groups = null;
    invalidateHover();
    renderPlot();
    st.textContent = `${alias(C.a)}'s layout · ${n(r.id.length)} boxes placed`;
    renderNovel();
  } catch (e) {
    hideWait();
    // no saved map of this feature in A's own session: the joint projection is the way
    st.innerHTML = isStatus(e, 400)
      ? `<span class="txt-danger">${esc(errText(e))}</span> · open ${esc(alias(C.a))}'s session and compute a map of this feature, or use <button type="button" class="linkish" data-map="joint">Joint</button>.`
      : `<span class="txt-danger">${esc(errText(e))}</span>`;
  }
}

function novelIds(): number[] {
  const C = S.cmp, q = U.quick;
  if (!C || !q) return [];
  const out: Array<[number, number]> = [];
  for (let i = 0; i < q.id.length; i++) {
    const id = q.id[i] ?? -1, v = q.novel[i] ?? 1;
    if (C.origin[id] === C.b && v < U.novelThr && !S.P?.deleted?.[id]) out.push([id, v]);
  }
  out.sort((x, y) => x[1] - y[1]);
  return out.map((x) => x[0]);
}

function renderNovel(): void {
  const el = $('cmp-novel');
  if (!el || !S.cmp) return;
  const B = alias(S.cmp.b), A = alias(S.cmp.a);
  if (!U.quick) {
    el.innerHTML = `<p class="muted hint">The Quick layout also scores how novel each box of ${esc(B)} is (its similarity to the nearest box of ${esc(A)}).</p>`;
    return;
  }
  const ids = novelIds();
  el.innerHTML = `<div class="m-ctl"><span class="m-lbl" title="cosine similarity to the nearest box of A">Novel in ${esc(B)}</span>` +
    `<input id="cmp-novel-thr" type="range" min="0.3" max="1" step="0.01" value="${U.novelThr}" aria-label="Similarity threshold">` +
    `<span class="num">&lt; ${U.novelThr.toFixed(2)}</span></div>` +
    `<div class="cmp-novel-row"><b class="num">${n(ids.length)}</b> boxes unlike anything in ${esc(A)}` +
    (ids.length ? ` <button type="button" class="ghost" data-novel>${icon('search')}Show in gallery</button>` : '') + '</div>' +
    `<div class="cmp-strip">${ids.slice(0, 12).map((id) => `<img loading="lazy" alt="" src="api/crop/${id}" title="#${id} ${esc(clsName(pts().cls[id] ?? -1))}">`).join('')}</div>`;
}

function showNovel(): void {
  const ids = novelIds();
  if (!ids.length || !S.cmp) return;
  $req<HTMLSelectElement>('sort').value = 'none';
  setSelection(ids, `Novel in ${alias(S.cmp.b)} (similarity to ${alias(S.cmp.a)} < ${U.novelThr.toFixed(2)})`);
  switchTab('explore');
}

// ---------------------------------------------------------------- classes: table + confusion

function renderClassTable(): void {
  const tb = $req<HTMLTableElement>('cmp-classes').tBodies[0];
  const r = S.cmp?.run;
  if (!tb) return;
  if (!r) { tb.innerHTML = '<tr><td colspan="9" class="muted">Run the comparison first.</td></tr>'; return; }
  const rows = r.summary.classes.filter((c) => c.a || c.b).sort((x, y) => Math.abs(y.b - y.a) - Math.abs(x.b - x.a) || y.a + y.b - x.a - x.b);
  const maxD = Math.max(1, ...rows.map((c) => Math.abs(c.b - c.a)));
  tb.innerHTML = rows.map((c) => {
    const d = c.b - c.a, pct = c.a ? (d / c.a) * 100 : null;
    const w = (Math.abs(d) / maxD) * 50;
    const bar = `<span class="cmp-dbar"><span class="${d < 0 ? 'neg' : 'pos'}" style="width:${w.toFixed(1)}%;--c:${d < 0 ? originColor(r.summary.a) : originColor(r.summary.b)}"></span></span>`;
    const name = clsName(c.cls) ?? `class ${c.cls}`;
    return `<tr data-c="${c.cls}" tabindex="0" role="button" title="load the changed ${esc(name)} boxes into the gallery">` +
      `<td><span class="cname"><span class="dot" style="background:${clsColor(c.cls)}"></span>${esc(name)}</span></td>` +
      `<td class="r">${n(c.a)}</td><td class="r">${n(c.b)}</td><td class="r ${d > 0 ? 'up' : d < 0 ? 'down' : ''}">${d > 0 ? '+' : ''}${n(d)}</td>` +
      `<td class="r">${pct == null ? (c.b ? 'new' : '–') : `${pct > 0 ? '+' : ''}${pct.toFixed(0)}%`}</td><td class="cmp-bar-c">${bar}</td>` +
      `<td class="r">${c.added ? n(c.added) : ''}</td><td class="r">${c.removed ? n(c.removed) : ''}</td>` +
      `<td class="r">${c.reclass_in || c.reclass_out ? `+${n(c.reclass_in)} / −${n(c.reclass_out)}` : ''}</td></tr>`;
  }).join('') || '<tr><td colspan="9" class="muted">No boxes in either dataset.</td></tr>';
}

const CHANGED = [BOX.MOVED, BOX.RECLASS, BOX.RECLASS_MOVED, BOX.UNMATCHED, BOX.IMAGE_ONLY];

function selectClassChanges(c: number): void {
  const C = S.cmp, r = C?.run;
  if (!C || !r) return;
  const cls = pts().cls;
  const ids = selectIds(r.status, C.origin, CHANGED, null).filter((id) => (C.origin[id] === C.a || C.origin[id] === C.b) && cls[id] === c);
  $req<HTMLSelectElement>('sort').value = 'image';
  setSelection(ids, `${clsName(c) ?? c}: changed boxes, ${alias(C.a)} vs ${alias(C.b)}`);
  switchTab('explore');
}

// stats.ts pattern: a fresh axis object per axis (Plotly writes the detected type back into it)
const ax = (): Partial<PL.LayoutAxis> => ({ gridcolor: THEME.grid, linecolor: THEME.border, zerolinecolor: THEME.border, tickfont: { color: THEME.muted, size: 10 } });

function renderConfusion(): void {
  const r = S.cmp?.run;
  const el = $req<PL.PlotlyHTMLElement>('cmp-confusion');
  if (!r) { el.innerHTML = '<p class="muted hint">Run the comparison first.</p>'; return; }
  if (U.chartsDrawn) { Plotly.Plots.resize(el); return; }
  U.chartsDrawn = true;
  const M = r.summary.confusion;
  // only the classes that take part in a class change: the diagonal (kept classes) would drown the rest
  const used = M.map((row, i) => row.some((v, j) => i !== j && v > 0) || M.some((rw, j) => j !== i && (rw[i] ?? 0) > 0));
  const idx = used.map((u, i) => (u ? i : -1)).filter((i) => i >= 0);
  if (!idx.length) {
    Plotly.purge(el);
    el.innerHTML = '<p class="muted hint cmp-none">No matched box changed its class.</p>';
    return;
  }
  el.innerHTML = '';
  const names = idx.map((i) => clsName(i) ?? `class ${i}`);
  const z = idx.map((i) => idx.map((j) => (i === j ? null : at(at(M, i), j) || null)));
  void Plotly.react(el, [withNested({
    type: 'heatmap', x: names, y: names, colorscale: [[0, THEME.surface2], [1, diffColor(2)]], zmin: 0, zmax: Math.max(1, ...z.flat().map((v) => v ?? 0)), hoverongaps: false,
    colorbar: { thickness: 8, outlinewidth: 0, tickfont: { color: THEME.muted, size: 10 } },
    hovertemplate: `<b>%{y}</b> in ${esc(alias(r.summary.a))} → <b>%{x}</b> in ${esc(alias(r.summary.b))}<br>%{z} boxes<extra></extra>`,
  } as Partial<PL.PlotData>, { z })], {
    ...plotBase(THEME.surface, 11), margin: { t: 8, b: 70, l: 80, r: 8 },
    xaxis: { ...ax(), title: { text: `class in ${alias(r.summary.b)}`, font: { color: THEME.muted, size: 11 } }, tickangle: -40 },
    yaxis: { ...ax(), autorange: 'reversed', title: { text: `in ${alias(r.summary.a)}`, font: { color: THEME.muted, size: 11 } } },
  }, cfg);
  if (!el.dataset['bound']) {
    el.dataset['bound'] = '1';
    el.on('plotly_click', (ev) => {
      const p = ev.points[0], cur = S.cmp?.run, C = S.cmp;
      if (!p || !cur || !C) return;
      const from = meta().classes.indexOf(String(p.y)), to = meta().classes.indexOf(String(p.x));
      const cls = pts().cls, ids: number[] = [];
      // A's boxes of class `from` whose B partner has class `to`, with those partners
      for (const id of selectIds(cur.status, C.origin, [BOX.RECLASS, BOX.RECLASS_MOVED], C.a)) {
        const q = cur.pair[id] ?? -1;
        if (cls[id] === from && q >= 0 && cls[q] === to) ids.push(id, q);
      }
      setSelection(ids, `${clsName(from)} → ${clsName(to)}: ${alias(C.a)} vs ${alias(C.b)}`);
      switchTab('explore');
    });
  }
}

// ---------------------------------------------------------------- stat histograms (lazy: drawn when opened)

const HISTS: Array<[id: string, col: 'area' | 'aspect' | 'brightness' | 'blur', label: string, log: boolean]> = [
  ['cmp-h-area', 'area', 'box area (share of image, log)', true], ['cmp-h-aspect', 'aspect', 'aspect w/h (log)', true],
  ['cmp-h-brightness', 'brightness', 'brightness', false], ['cmp-h-blur', 'blur', 'sharpness (log)', true]];

function renderHists(): void {
  const C = S.cmp;
  if (!C || U.histDrawn) { if (U.histDrawn) HISTS.forEach(([id]) => { const el = $<PL.PlotlyHTMLElement>(id); if (el?.data) Plotly.Plots.resize(el); }); return; }
  U.histDrawn = true;
  const P = pts();
  const mask = S.filterMask ?? null;
  for (const [id, col, label, log] of HISTS) {
    const v = P[col];
    const edges = sharedEdges(v, 30, log);
    const { ha, hb, ks } = histPair(v, C.origin, C.a, C.b, mask, edges);
    const mids = Array.from({ length: ha.length }, (_, k) => (log ? Math.sqrt(at(edges, k) * at(edges, k + 1)) : (at(edges, k) + at(edges, k + 1)) / 2));
    const line = (h: Float64Array, k: number): Partial<PL.PlotData> => ({
      type: 'scatter', mode: 'lines', line: { shape: 'hvh', width: 2, color: originColor(k) }, name: alias(k),
      x: mids, y: Array.from(h), fill: 'tozeroy', fillcolor: `${originColor(k)}22`,
      hovertemplate: `${esc(alias(k))}: %{y:.1%}<extra></extra>`,
    });
    void Plotly.react(id, [line(ha, C.a), line(hb, C.b)], {
      ...plotBase(THEME.surface, 10), showlegend: false, margin: { t: 22, b: 26, l: 34, r: 6 },
      title: { text: `${label} · KS ${ks.toFixed(2)}`, font: { size: 11, color: ks > 0.2 ? THEME.danger : THEME.muted }, x: 0.02, y: 0.97 },
      xaxis: { ...ax(), type: log ? 'log' : 'linear' }, yaxis: { ...ax(), tickformat: '.0%' },
    }, { ...cfg, displayModeBar: false });
  }
}

// ---------------------------------------------------------------- members + promote

async function loadMembers(): Promise<void> {
  try { U.union = await getJSON<UnionStatus>('api/union/status'); } catch (e) { U.union = null; }
  const st = new Map((U.union?.members ?? []).map((m) => [m.alias, m.stale]));
  const owner = (S.status?.my_role ?? 'owner') === 'owner';
  $req('cmp-members').innerHTML = U.members.map((m, k) =>
    `<div class="cmp-member"><span class="dot" style="background:${originColor(k)}"></span><b>${esc(m.alias)}</b>` +
    `<a href="${esc(vizUrl(m.sid))}" title="open ${esc(m.name)} on its own">${esc(m.name)}</a>` +
    `<span class="muted num">${n(m.n_images)} img · ${n(m.n)} boxes</span>` +
    (st.get(m.alias) ? '<span class="pill stale" title="This dataset changed after the combined dataset was assembled">changed</span>' : '') + '</div>').join('') +
    (U.union?.stale && owner ? `<button type="button" class="ghost" data-member-refresh>${icon('refresh')}Update the combined dataset</button>` : '');
}

async function refreshUnion(b: HTMLButtonElement): Promise<void> {
  const ds = S.status?.dataset;
  if (!ds) return;
  b.disabled = true;
  try { await postJSON(`api/datasets/${encodeURIComponent(ds.id)}/refresh`, {}); location.reload(); }
  catch (e) { b.disabled = false; toast(errText(e), 'err'); }
}

interface PromoteResult { project_id: string; task_id?: string; viz_session_id?: string | null; n_media?: number; n_skipped_dup?: number; n_annotations?: number }

async function openPromote(): Promise<void> {
  const C = cmp();
  const order = [C.a, C.b, ...U.members.map((_, i) => i).filter((i) => i !== C.a && i !== C.b)];
  const prio = (list: number[]): string => list.map((k, i) =>
    `<li data-k="${k}"><span class="dot" style="background:${originColor(k)}"></span><b>${esc(alias(k))}</b><span class="muted">${esc(U.members[k]?.name ?? '')}</span>` +
    `<button type="button" class="icon-btn ghost" data-up="${i}" aria-label="Higher priority" ${i ? '' : 'disabled'}>↑</button></li>`).join('');
  const dsName = S.status?.dataset?.name ?? 'combined';
  const body = `<p class="muted hint">A new labeling session with the images of these datasets: label there, and the map of the new session follows live.</p>
    <label class="field">Name <input data-p="name" type="text" value="${esc(`${dsName} (labeling)`)}"></label>
    <label class="field">Same image in several datasets
      <select data-p="dedup"><option value="hash">keep one copy (same content)</option><option value="none">keep every copy</option></select></label>
    <label class="field">Their labels
      <select data-p="labels"><option value="prefer">take the labels of the first dataset that has the image</option>
        <option value="merge">merge: add boxes the others have that do not overlap</option><option value="all">keep every box of every dataset</option></select></label>
    <div class="field"><span>Priority <span class="muted">(first wins)</span></span><ol class="cmp-prio" data-p="priority">${prio(order)}</ol></div>
    <label class="cmp-check"><input data-p="bg" type="checkbox" checked> Include images with no boxes</label>
    <label class="cmp-check"><input data-p="viz" type="checkbox" checked> Also open a live visualizer session of it</label>
    <div class="cmp-prog muted hint" aria-live="polite"></div>`;
  const out: { r: PromoteResult | null } = { r: null };     // set by the dialog's action (a closure)
  const ok = await openDialog({
    title: 'Promote to labeling', ok: 'Promote', body, wide: true,
    bind: (root) => {
      const ol = root.querySelector<HTMLElement>('.cmp-prio');
      ol?.addEventListener('click', (e) => {
        const b = closestOf<HTMLButtonElement>(e, 'button[data-up]');
        if (!b) return;
        const i = +(b.dataset['up'] ?? 0);
        const t = order[i - 1];
        if (i < 1 || t === undefined) return;
        order[i - 1] = order[i] ?? t;
        order[i] = t;
        ol.innerHTML = prio(order);
      });
    },
    onOk: async (root) => {
      const val = (p: string): string => root.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-p="${p}"]`)?.value ?? '';
      const chk = (p: string): boolean => !!root.querySelector<HTMLInputElement>(`[data-p="${p}"]`)?.checked;
      const name = val('name').trim();
      if (!name) return 'Give the labeling session a name.';
      const prog = root.querySelector<HTMLElement>('.cmp-prog');
      try {
        const { job_id } = await postJSON<{ job_id: number }>('api/promote', {
          target: 'new', name, dedup: val('dedup'), labels: val('labels'), priority: order.map(alias),
          include_background: chk('bg'), create_viz: chk('viz'),
        });
        const j = await waitJob(job_id, (jj) => {
          if (prog) prog.textContent = `${jj.stage || jj.label}${jj.total ? ` · ${jj.done.toLocaleString()} / ${jj.total.toLocaleString()}` : ''}`;
        });
        out.r = (j.result ?? null) as PromoteResult | null;
        if (!out.r?.project_id) return 'The job finished without a labeling session.';
        return null;
      } catch (e) { return errText(e); }
    },
  });
  const res = out.r;
  if (!ok || !res) return;
  const viz = res.viz_session_id;
  await openDialog({
    title: 'Labeling session ready', ok: 'Open labeling',
    body: `<p><b class="num">${n(res.n_media)}</b> images, <b class="num">${n(res.n_annotations)}</b> boxes` +
      `${res.n_skipped_dup ? ` · ${n(res.n_skipped_dup)} duplicate images skipped` : ''}.</p>` +
      `<p><a href="${esc(labelUrl(res.project_id))}">${icon('tag')}Open the labeling page</a></p>` +
      (viz ? `<p><a href="${esc(vizUrl(viz))}">${icon('scatter')}Open its live map</a></p>` : ''),
    onOk: () => { location.href = labelUrl(res.project_id); return null; },
  });
}
