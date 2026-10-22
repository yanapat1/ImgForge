// The projection controls: method parameters, saved views, remembered UI settings, loading a view
// (projection + scores, waiting on their jobs), cleanlab label quality and the Find issues actions.

import { api, errMessage, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, syncRanges } from '../core/dom.js';
import { hideWait, showWait, waitJob } from '../core/jobs.js';
import { toast } from '../core/toast.js';
import { featureLabel, S, status, UI } from '../core/state.js';
import { refreshGroups } from '../plot/groups.js';
import { invalidateHover, renderPlot, visibleIndices } from '../plot/render.js';
import type { LabelQualityReady, LabelQualityResult, PointsColumn, ProjectionRequest, ProjectionResult, SavedView, ScoresResult, Status, Tags, UiDefaults } from '../types.js';
import type { ProjectionsPayload, SettingHook } from '../types-views.js';
import { at } from './chart-util.js';
import { galleryOrder, renderGallery, setSelection, sortedSelection } from './gallery.js';
import { setCollapsed, setLeftWidth, setMapDirty, syncClassFilter, syncIssuesBtn, updateBarSummary, updateGroupsBadge } from './layout.js';
import { invalidateStats, renderClassTable, renderStats, showConfusion } from './stats.js';

/** [label, hint, step] of a method parameter's number input. */
const PARAM_INFO: Record<string, [string, string, number]> = {
  n_neighbors: ['nbrs', 'neighbours each point looks at: larger = more global structure', 1],
  min_dist: ['min_dist', 'smaller = tighter clusters', 0.05],
  perplexity: ['perplexity', '~ number of close neighbours each point considers', 1],
};

export function buildParamInputs(): void {
  $req('params').innerHTML = Object.entries(status().methods).map(([m, def]) =>
    `<span id="params-${m}" class="params hidden">` + Object.entries(def.params).map(([k, v]) => {
      const [label, hint, step] = PARAM_INFO[k] || [k, '', 1];
      SETTING_IDS.push(`p-${m}-${k}`);
      return `<label title="${esc(hint)}">${label} <input id="p-${m}-${k}" type="number" min="0" step="${step}" value="${v}"></label>`;
    }).join('') + '</span>').join('');
}

export const SETTING_IDS: string[] = ['feature', 'method', 'mode', 'class-a', 'class-b',
  'color-by', 'dot-size', 'g-bubbles', 'g-outliers', 'g-dim', 'g-mode', 'g-min', 'sort', 'thumb-size', 'show-dist'];

// these pick WHICH projection is shown: not restored at page load, which always opens the startup view
// (an uncomputed saved choice used to start a long job on every load); earlier views are in "Saved views"
const VIEW_IDS = new Set(['feature', 'method', 'mode', 'class-a', 'class-b']);

// views with their own remembered state (the filter bar, curation) plug in here: collect(out) adds keys to the
// saved settings, apply(st) restores them. Every key must come back through collect, or the next save drops it.
export const SETTING_HOOKS: SettingHook[] = [];

type SettingEl = HTMLInputElement | HTMLSelectElement;
const rec = (v: unknown): Record<string, unknown> => (typeof v === 'object' && v !== null ? Object.fromEntries(Object.entries(v)) : {});
const str = (v: unknown): string => (v == null ? '' : String(v));

function collectSettings(): Record<string, unknown> {
  const out: Record<string, unknown> = { hidden: [...S.hidden], left_w: S.leftW || null, collapsed: S.collapsed || null };
  for (const id of SETTING_IDS) {
    const el = $req<SettingEl>(id);
    out[id] = el instanceof HTMLInputElement && el.type === 'checkbox' ? el.checked : el.value;
  }
  for (const h of SETTING_HOOKS) h.collect(out);
  return out;
}

export function applySettings(st: Record<string, unknown>): void {
  for (const id of SETTING_IDS) {
    if (!(id in st) || VIEW_IDS.has(id) || id.startsWith('p-')) continue;
    const el = $req<SettingEl>(id);
    if (el instanceof HTMLSelectElement) { if ([...el.options].some((o) => o.value === String(st[id]))) el.value = String(st[id]); }
    else if (el.type === 'checkbox') el.checked = !!st[id];
    else el.value = str(st[id]);
  }
  if (st.left_w) setLeftWidth(Number(st.left_w));
  if (st.collapsed) setCollapsed(String(st.collapsed), false);
  $req('gallery').style.setProperty('--thumb', `${$req<HTMLInputElement>('thumb-size').value}px`);
  syncRanges();
  for (const h of SETTING_HOOKS) { try { h.apply(st); } catch (e) { console.warn('settings hook', e); } }
}

// defaults from scripts/image-tools/config.yaml (ui + analysis.groups)
export function applyUiDefaults(ui: UiDefaults): void {
  Object.assign(UI, ui);
  if (ui.page_size) UI.page_size = +ui.page_size;
  if (ui.thumb_size) { $req<HTMLInputElement>('thumb-size').value = str(ui.thumb_size); $req('gallery').style.setProperty('--thumb', `${ui.thumb_size}px`); }
  if (ui.dot_size) $req<HTMLInputElement>('dot-size').value = str(ui.dot_size);
  const g = rec(ui.groups);
  if (g.min_cluster_size) $req<HTMLInputElement>('g-min').value = str(g.min_cluster_size);
  if (g.per_class !== undefined) $req<HTMLSelectElement>('g-mode').value = g.per_class ? 'class' : 'all';
  syncRanges();
}

let saveTimer: ReturnType<typeof setTimeout> | undefined;

export function saveSettings(): void {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { postJSON('api/settings', collectSettings()).catch(() => {}); }, 500);
}

function sameClasses(a: number[] | null | undefined, b: number[] | null | undefined): boolean {
  const x = (a || []).slice().sort((p, q) => p - q), y = (b || []).slice().sort((p, q) => p - q);
  return x.length === y.length && x.every((v, i) => v === y[i]);
}

function sameParams(a: Record<string, number>, b: Record<string, number>): boolean {
  const ka = Object.keys(a), kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => Math.abs(+(a[k] ?? NaN) - +(b[k] ?? NaN)) < 1e-9);
}

export async function refreshSavedViews(): Promise<void> {
  try {
    const { views } = await getJSON<ProjectionsPayload>('api/projections');
    S.views = views;
    $req('saved-views').innerHTML = `<option value="">Saved views (${views.length})…</option>` +
      views.map((v, i) => `<option value="${i}" title="computed ${esc(v.created || '')}${v['compute_s'] ? ` in ${v['compute_s']}s` : ''}">✓ ${esc(v.label)}</option>`).join('');
    markMethods();
  } catch (e) { /* index not ready */ }
}

export function markMethods(): void {
  if (!S.views || !S.status) return;
  const req = currentRequest();
  for (const opt of $req<HTMLSelectElement>('method').options) {
    const m = opt.value;
    const params: Record<string, number> = {};
    for (const k of Object.keys(S.status.methods[m]?.params ?? {})) params[k] = +$req<HTMLInputElement>(`p-${m}-${k}`).value;
    const saved = S.views.some((v) => v.feature === req.feature && v.method === m && sameClasses(v.classes, req.classes) && sameParams(v.params, params));
    opt.textContent = `${saved ? '✓ ' : ''}${S.status.methods[m]?.label}`;
  }
}

export function applyView(v: SavedView): void {
  $req<HTMLSelectElement>('feature').value = v.feature;
  updateTextSearch();
  $req<HTMLSelectElement>('method').value = v.method;
  for (const [k, val] of Object.entries(v.params)) {
    const inp = $<HTMLInputElement>(`p-${v.method}-${k}`);
    if (inp) inp.value = String(val);
  }
  const cls = v.classes || [];
  if (cls.length === 1) { $req<HTMLSelectElement>('mode').value = 'single'; $req<HTMLSelectElement>('class-a').value = String(cls[0]); }
  else if (cls.length === 2) { $req<HTMLSelectElement>('mode').value = 'compare'; $req<HTMLSelectElement>('class-a').value = String(cls[0]); $req<HTMLSelectElement>('class-b').value = String(cls[1]); }
  else {
    $req<HTMLSelectElement>('mode').value = 'all';
    S.hidden = cls.length ? new Set((S.meta?.classes ?? []).map((_, i) => i).filter((i) => !cls.includes(i))) : new Set();
    syncClassFilter();
  }
  updateModeControls();
  updateMethodControls();
  saveSettings();
  void loadView();
}

let viewSeq = 0;

function currentRequest(): ProjectionRequest {
  const method = $req<HTMLSelectElement>('method').value;
  const params: Record<string, number> = {};
  for (const k of Object.keys(status().methods[method]?.params ?? {})) params[k] = +$req<HTMLInputElement>(`p-${method}-${k}`).value;
  return { feature: $req<HTMLSelectElement>('feature').value, method, params, classes: null };   // always all classes; display filter is client-side
}

export async function loadView(): Promise<void> {
  const req = currentRequest();
  const seq = ++viewSeq;
  $req('plot-idle').classList.add('hidden');
  try {
    let r = await postJSON<ProjectionResult>('api/projection', req);
    while (!r.ready) {
      await waitJob(r.job_id, (j) => { if (seq === viewSeq) showWait(j); });
      if (seq !== viewSeq) return;
      r = await postJSON<ProjectionResult>('api/projection', req);
    }
    await ensureScores(req.feature, seq);
    if (seq !== viewSeq) return;
    await loadLabelQuality(req.feature);
    if (seq !== viewSeq) return;
    hideWait();
    S.view = r;
    S.viewPos = new Map(r.id.map((id, i): [number, number] => [id, i]));
    S.viewReq = req;
    setMapDirty(false);
    updateBarSummary();
    S.groups = null;
    invalidateHover();
    const ev = r.info && r.info.explained_variance;
    $req('pca-info').textContent = ev
      ? `explained variance: PC1 ${((ev[0] ?? 0) * 100).toFixed(1)}%, PC2 ${((ev[1] ?? 0) * 100).toFixed(1)}%` : '';
    await refreshGroups();
    renderPlot();
    renderGallery();
    void refreshStatus();
    void refreshSavedViews();
  } catch (e) {
    if (seq !== viewSeq) return;
    hideWait();
    alert(errMessage(e));
  }
}

async function ensureScores(feature: string, seq: number): Promise<void> {
  if (S.scoresFeature === feature) return;
  let r = await getJSON<ScoresResult>(`api/scores?feature=${encodeURIComponent(feature)}`);
  while (!r.ready) {
    await waitJob(r.job_id, (j) => { if (seq === viewSeq) showWait(j); });
    r = await getJSON<ScoresResult>(`api/scores?feature=${encodeURIComponent(feature)}`);
  }
  const P = S.P;
  if (!P) throw new Error('points not loaded');
  P.suspicion = r.suspicion;
  P.suggested = r.suggested;
  P.outlier = r.outlier;
  S.scoresFeature = feature;
  invalidateHover();
  invalidateStats();
  if (!$req('tab-stats').classList.contains('hidden')) { renderClassTable(); renderStats(); }
  if (S.sel.length) renderGallery();
}

export function updateModeControls(): void {
  const mode = $req<HTMLSelectElement>('mode').value;
  $req('lbl-a').classList.toggle('hidden', mode === 'all');
  $req('lbl-b').classList.toggle('hidden', mode !== 'compare');
  $req('class-filter').classList.toggle('hidden', mode !== 'all');
}

export function updateMethodControls(): void {
  const m = $req<HTMLSelectElement>('method').value;
  for (const k of Object.keys(status().methods)) $req(`params-${k}`).classList.toggle('hidden', k !== m);
  $req('pca-info').classList.toggle('hidden', m !== 'pca');
}

export function viewIdsVisible(): number[] {
  const v = S.view;
  if (!v) return [];
  return visibleIndices().map((i) => at(v.id, i));
}

// a check button that is already on turns itself off (clears its selection and rings)
export function toggleOff(view: string): boolean {
  if (S.activeView !== view) return false;
  setSelection([], 'No selection');
  return true;
}

// a class chip changed: run the active Inspect check again so it follows the visible classes
const INSPECT_BTN: Record<string, string> = { suspicious: 'btn-suspicious', far: 'btn-outliers', dups: 'btn-dups', 'sel-outliers': 'btn-sel-outliers',
  'cl-issues': 'btn-cl-issues', 'cl-low': 'btn-cl-low', 'cl-ood': 'btn-cl-ood', 'cl-swap': 'btn-cl-swap', 'cl-badloc': 'btn-cl-badloc' };
export function reapplyInspect(): void {
  const id = INSPECT_BTN[S.activeView ?? ''];
  if (!id) return;
  S.activeView = null;           // so the button re-runs instead of toggling off
  $req(id).click();
}

export function topBy(field: PointsColumn, n: number, title: string, view: string): void {
  const col = S.P?.[field];
  if (!col || toggleOff(view)) return;
  const sus = S.P?.suspicion;
  const v = (id: number): number => col[id] ?? NaN;
  const ids = viewIdsVisible().filter((id) => (field !== 'suspicion' || (sus?.[id] ?? 0) > 0) && Number.isFinite(v(id)));
  ids.sort((a, b) => v(b) - v(a));
  $req<HTMLSelectElement>('sort').value = field;
  setSelection(ids.slice(0, n), `${title} · ${featureLabel(S.scoresFeature)}`, { view });
}

// ---- method parameters edited but not applied yet: the dot on the projection trigger
export function updateMapDirty(): void {
  const r = S.viewReq;
  if (!r) { setMapDirty(false); return; }
  const cur = currentRequest();
  setMapDirty(cur.method === r.method && cur.feature === r.feature && !sameParams(cur.params, r.params));
}

// ---- cleanlab label quality: k-fold out-of-fold class probabilities of the current feature, then cleanlab
const LQ_COLS = ['cl_quality', 'cl_given_prob', 'cl_ood_feat', 'cl_ood_prob', 'cl_issue', 'cl_rank', 'cl_suggested'] as const;
let lqSeq = 0;
let lqRunning: string | null = null;        // feature whose check runs now: a view load leaves its columns alone
let lqStale: string | null = null;         // feature whose results were dropped because the labels changed

function setLabelQuality(r: LabelQualityReady | null): void {
  const P = S.P;
  if (!P) return;
  if (r) {
    P.cl_quality = r.quality; P.cl_given_prob = r.given_prob; P.cl_ood_feat = r.ood_feat; P.cl_ood_prob = r.ood_prob;
    P.cl_issue = r.issue; P.cl_rank = r.rank; P.cl_suggested = r.suggested;
    S.lq = { feature: r.feature, summary: r.summary };
    lqStale = null;
  } else {
    for (const k of LQ_COLS) delete P[k];
    S.lq = null;
  }
  invalidateHover();
  invalidateStats();
  syncLqControls();
  if (S.view && $req<HTMLSelectElement>('color-by').value.startsWith('cl_')) renderPlot();
  if (!$req('tab-stats').classList.contains('hidden')) { renderClassTable(); renderStats(); }
  if (S.sel.length) renderGallery();
}

/** Colour-by / sort / Find issues entries that need label quality follow whether it is loaded. */
export function syncLqControls(): void {
  const on = !!S.lq;
  const colorBy = $req<HTMLSelectElement>('color-by'), sort = $req<HTMLSelectElement>('sort');
  const og = colorBy.querySelector<HTMLOptGroupElement>('optgroup[data-needs="lq"]');
  if (og) { og.hidden = !on; og.disabled = !on; }
  const lqField = !!og?.querySelector(`option[value="${colorBy.value}"]`);     // detection colourings are not ours
  if (!on && lqField) { colorBy.value = 'class'; if (S.view) renderPlot(); }
  const o = sort.querySelector<HTMLOptionElement>('option[value="cl-quality"]');
  if (o) { o.hidden = !on; o.disabled = !on; }
  if (!on && sort.value === 'cl-quality') sort.value = 'none';
  for (const id of ['btn-cl-issues', 'btn-cl-low', 'btn-cl-ood', 'btn-cl-confusion']) $req<HTMLButtonElement>(id).disabled = !on;
  const sm = S.lq?.summary;
  $req('cl-issues-n').textContent = sm ? `${(sm.n_issues ?? 0).toLocaleString()} labels flagged` : 'run the check first';
  $req('btn-cl-ood').classList.toggle('hidden', on && !S.P?.cl_ood_feat?.some((v) => v != null));
  $req('btn-lq-run-t').textContent = on ? 'Run again' : 'Run label-quality check';
  const clf = sm?.settings?.['classifier'];
  if (typeof clf === 'string') $req<HTMLSelectElement>('lq-clf').value = clf;
  const stale = !sm && lqStale !== null && lqStale === (S.viewReq?.feature ?? null);
  $req('lq-info').innerHTML = sm ? lqInfo(sm) : stale ? 'The labels changed since the last check: run it again.' : '';
}

/** A curation fix / undo / image edit changed labels: the cleanlab results were for the old ones, drop them. */
export function labelsChanged(): void {
  if (!S.lq) return;
  lqStale = S.lq.feature;
  lqSeq++;                                   // an answer in flight is for the old labels too
  setLabelQuality(null);
}

function lqInfo(sm: LabelQualityReady['summary']): string {
  if (sm.note) return esc(sm.note);
  const pct = (v: number | null | undefined): string => (v == null ? '–' : `${(v * 100).toFixed(1)}%`);
  const left = sm.left_out_classes?.length ? ` · ${sm.left_out_classes.length} small class(es) left out` : '';
  return `${esc(featureLabel(sm.feature ?? S.scoresFeature))}: health <b>${pct(sm.health_score)}</b> · ` +
    `held-out accuracy ${pct(sm.oof_accuracy)} · ${sm.folds_used ?? 0} folds${left}`;
}

/** Load the label quality of `feature` when it is computed already (never starts the job). */
export async function loadLabelQuality(feature: string): Promise<void> {
  if (S.lq?.feature === feature || lqRunning === feature) return;
  const seq = ++lqSeq;
  try {
    const r = await getJSON<LabelQualityResult>(`api/label-quality?feature=${encodeURIComponent(feature)}`);
    if (seq === lqSeq) setLabelQuality(r.ready ? r : null);
  } catch (e) { if (seq === lqSeq) setLabelQuality(null); }
}

/** Compute it (a background job with progress in the plot), for the feature on screen. */
export async function runLabelQuality(): Promise<boolean> {
  const feature = S.viewReq?.feature ?? $req<HTMLSelectElement>('feature').value;
  const body = { feature, classifier: $req<HTMLSelectElement>('lq-clf').value };
  const btn = $req<HTMLButtonElement>('btn-lq-run');
  btn.disabled = true;
  const seq = ++lqSeq;
  lqRunning = feature;
  try {
    let r = await postJSON<LabelQualityResult>('api/label-quality', body);
    while (!r.ready && r.job_id != null) {
      await waitJob(r.job_id, (j) => showWait(j));
      r = await postJSON<LabelQualityResult>('api/label-quality', body);
    }
    hideWait();
    // the run is the newest answer for its feature, even if a view load asked (and got "not computed") meanwhile
    if (seq === lqSeq || (r.ready && r.feature === (S.viewReq?.feature ?? feature))) { lqSeq++; setLabelQuality(r.ready ? r : null); }
    return r.ready && !!S.lq;
  } catch (e) {
    hideWait();
    alert(errMessage(e));
    return false;
  } finally { btn.disabled = false; lqRunning = null; }
}

/** Find issues › cleanlab: label issues (most likely wrong first), lowest quality, out-of-distribution. */
export function clView(view: 'cl-issues' | 'cl-low' | 'cl-ood'): void {
  const P = S.P;
  if (!P || !S.lq || toggleOff(view)) return;
  const q = P.cl_quality ?? [], rank = P.cl_rank ?? [], ood = P.cl_ood_feat ?? [];
  let ids = viewIdsVisible().filter((id) => q[id] != null);
  let title: string;
  if (view === 'cl-issues') {
    ids = ids.filter((id) => (rank[id] ?? -1) >= 0).sort((a, b) => (rank[a] ?? 0) - (rank[b] ?? 0));
    title = 'Label issues (cleanlab)';
  } else if (view === 'cl-low') {
    ids = ids.sort((a, b) => (q[a] ?? 1) - (q[b] ?? 1)).slice(0, UI.check_list_size);
    title = 'Lowest label quality (cleanlab)';
  } else {
    ids = ids.filter((id) => ood[id] != null).sort((a, b) => (ood[b] ?? 0) - (ood[a] ?? 0)).slice(0, UI.check_list_size);
    title = 'Out-of-distribution (cleanlab)';
  }
  if (!ids.length) {              // an empty selection would show every image instead: say so
    toast(view === 'cl-issues' ? 'cleanlab found no label issues among the classes shown' : 'nothing to show for the classes shown');
    return;
  }
  $req<HTMLSelectElement>('sort').value = view === 'cl-ood' ? 'none' : 'cl-quality';
  setSelection(ids, `${title} · ${featureLabel(S.lq.feature)}`, { view });
  syncIssuesBtn();
}

/** Find issues › Detection: label boxes cleanlab scores low against the Model tab's predictions, worst first. */
export function odView(view: 'cl-swap' | 'cl-badloc'): void {
  const P = S.P;
  const col = view === 'cl-swap' ? P?.cl_swap : P?.cl_badloc;
  if (!P || !col || toggleOff(view)) return;
  const v = (id: number): number => col[id] ?? NaN;
  const ids = viewIdsVisible().filter((id) => v(id) < 0.1).sort((a, b) => v(a) - v(b));
  if (!ids.length) {
    toast(view === 'cl-swap' ? 'no box looks like another class to the model' : 'no box looks badly placed to the model');
    return;
  }
  $req<HTMLSelectElement>('sort').value = 'none';
  setSelection(ids, `${view === 'cl-swap' ? 'Wrong class' : 'Badly placed'} (cleanlab detection)`, { view });
  syncIssuesBtn();
}

/** Boxes labelled `given` that cleanlab would call `suggested` (a confident-joint cell). */
export function selectConfusion(given: number, suggested: number): void {
  const P = S.P;
  if (!P?.cl_suggested) return;
  const sug = P.cl_suggested, q = P.cl_quality ?? [];
  const ids = viewIdsVisible().filter((id) => P.cls[id] === given && sug[id] === suggested && q[id] != null)
    .sort((a, b) => (q[a] ?? 1) - (q[b] ?? 1));
  $req<HTMLSelectElement>('sort').value = 'cl-quality';
  const name = (c: number): string => S.meta?.classes[c] ?? String(c);
  setSelection(ids, `Labelled ${name(given)}, cleanlab says ${name(suggested)}`);
}

export function openConfusion(): void { showConfusion(); }

export async function refreshTagList(): Promise<void> {
  const tags = await getJSON<Tags>('api/tags');
  $req('tag-list').innerHTML = '<option value="">Load tag…</option>' +
    Object.entries(tags).map(([t, ids]) => `<option value="${esc(t)}">${esc(t)} (${ids.length})</option>`).join('');
  S.tags = tags;
}

export async function exportCSV(): Promise<void> {
  const ids = S.selKind === 'pairs' ? [...new Set(galleryOrder())] : sortedSelection();
  if (!ids.length) return;
  const r = await api('api/export', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, feature: S.scoresFeature }) });
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `selection_${ids.length}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

export async function onGroupControls(): Promise<void> {
  updateGroupsBadge();
  try {
    await refreshGroups();
  } catch (e) { alert(errMessage(e)); return; }
  renderPlot();
  renderGallery();     // outlier frames/badges follow the "mark outliers" toggle
}

// ---- the Features dropdown (✓ = computed already); refreshed after every job
export function renderFeatureSelect(selected?: string): void {
  const sel = $req<HTMLSelectElement>('feature');
  const cur = selected || sel.value;
  const groups = { model: 'My models (Settings › Models)', deep: 'Deep features (GPU)', simple: 'Simple features (fast, CPU)' };
  const features = status().features ?? [];
  sel.innerHTML = Object.entries(groups).filter(([g]) => features.some((f) => f.group === g)).map(([g, title]) =>
    `<optgroup label="${title}">` + features.filter((f) => f.group === g).map((f) =>
      `<option value="${esc(f.key)}" title="${esc(f.note || '')}">${f.cached ? '✓ ' : '  '}${esc(f.label)}</option>`
    ).join('') + '</optgroup>').join('');
  sel.value = cur;
  updateTextSearch();
  updateBarSummary();
}

// text search needs a feature with a text tower (OpenCLIP / SigLIP)
export function updateTextSearch(): void {
  const f = S.status && S.status.features && S.status.features.find((x) => x.key === $req<HTMLSelectElement>('feature').value);
  const ok = !!(f && f.text);
  $req<HTMLInputElement>('text-q').disabled = !ok;
  $req<HTMLButtonElement>('text-go').disabled = !ok;
  // the bar shows the search box only when this feature can do it (an OpenCLIP / SigLIP feature)
  $req('text-search').classList.toggle('no-text', !ok);
}

export async function refreshStatus(): Promise<void> {
  try {
    const st = await getJSON<Status>('api/status');
    if (!st.dataset) return;                 // the session was closed (removed on the Sessions home)
    S.status = st;
    if (S.meta) renderFeatureSelect();
  } catch (e) { /* ignore */ }
}
