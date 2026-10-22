// The projection controls: method parameters, saved views, remembered UI settings, loading a view
// (projection + scores, waiting on their jobs) and the Inspect actions.

import { api, errMessage, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, syncRanges } from '../core/dom.js';
import { hideWait, showWait, waitJob } from '../core/jobs.js';
import { featureLabel, S, status, UI } from '../core/state.js';
import { refreshGroups } from '../plot/groups.js';
import { invalidateHover, renderPlot, visibleIndices } from '../plot/render.js';
import type { PointsColumn, ProjectionRequest, ProjectionResult, SavedView, ScoresResult, Status, Tags, UiDefaults } from '../types.js';
import type { ProjectionsPayload, SettingHook } from '../types-views.js';
import { at } from './chart-util.js';
import { galleryOrder, renderGallery, setSelection, sortedSelection } from './gallery.js';
import { setCollapsed, setLeftWidth, syncClassFilter, updateGroupsBadge } from './layout.js';
import { invalidateStats, renderClassTable, renderStats } from './stats.js';

/** [label, hint, step] of a method parameter's number input. */
const PARAM_INFO: Record<string, [string, string, number]> = {
  n_neighbors: ['nbrs', 'neighbours each point looks at: larger = more global structure', 1],
  min_dist: ['min_dist', 'smaller = tighter clusters', 0.05],
  perplexity: ['perplexity', '~ number of close neighbours each point considers', 1],
  gamma: ['gamma', 'RBF kernel width: larger = more local; 0 = auto (1 / dims)', 0.1],
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
    hideWait();
    S.view = r;
    S.viewPos = new Map(r.id.map((id, i): [number, number] => [id, i]));
    S.viewReq = req;
    S.groups = null;
    invalidateHover();
    const ev = r.info?.explained_variance, q = r.info?.quality;
    const pct = (v: number): string => `${(v * 100).toFixed(1)}%`;
    $req('pca-info').textContent = [
      ev ? `explained variance: PC1 ${pct(ev[0] ?? 0)}, PC2 ${pct(ev[1] ?? 0)}` : '',
      q ? `map quality (${q.n.toLocaleString()} pts, k=${q.k}): trustworthiness ${q.trustworthiness.toFixed(3)} · ` +
        `continuity ${q.continuity.toFixed(3)} · neighbours kept ${pct(q.knn_preserved)}` : '',
    ].filter(Boolean).join('  ·  ');
    $req('pca-info').title = q ? 'trustworthiness: no false neighbours in 2D (1 = best) · continuity: true neighbours ' +
      'not torn apart (1 = best) · neighbours kept: share of each point\'s k nearest that stay its k nearest in 2D' : '';
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
const INSPECT_BTN: Record<string, string> = { suspicious: 'btn-suspicious', far: 'btn-outliers', dups: 'btn-dups', 'sel-outliers': 'btn-sel-outliers' };
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
  const ids = viewIdsVisible().filter((id) => field !== 'suspicion' || (sus?.[id] ?? 0) > 0);
  ids.sort((a, b) => at(col, b) - at(col, a));
  $req<HTMLSelectElement>('sort').value = field;
  setSelection(ids.slice(0, n), `${title} · ${featureLabel(S.scoresFeature)}`, { view });
}

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
  const groups = { deep: 'Deep features (GPU)', simple: 'Simple features (fast, CPU)' };
  const features = status().features ?? [];
  sel.innerHTML = Object.entries(groups).map(([g, title]) =>
    `<optgroup label="${title}">` + features.filter((f) => f.group === g).map((f) =>
      `<option value="${esc(f.key)}" title="${esc(f.note || '')}">${f.cached ? '✓ ' : '  '}${esc(f.label)}</option>`
    ).join('') + '</optgroup>').join('');
  sel.value = cur;
  updateTextSearch();
}

// text search needs a feature with a text tower (OpenCLIP / SigLIP)
export function updateTextSearch(): void {
  const f = S.status && S.status.features && S.status.features.find((x) => x.key === $req<HTMLSelectElement>('feature').value);
  const ok = !!(f && f.text);
  $req<HTMLInputElement>('text-q').disabled = !ok;
  $req<HTMLButtonElement>('text-go').disabled = !ok;
  $req('text-search').title = ok ? '' : 'Text search needs an OpenCLIP / SigLIP feature';
}

export async function refreshStatus(): Promise<void> {
  try {
    const st = await getJSON<Status>('api/status');
    if (!st.dataset) return;                 // the session was closed (removed on the Sessions home)
    S.status = st;
    if (S.meta) renderFeatureSelect();
  } catch (e) { /* ignore */ }
}
