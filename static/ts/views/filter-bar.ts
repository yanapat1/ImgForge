// The filter bar above the plot: image name (contains / wildcard / regex / related), "image has class"
// (any / all / none), more filters (box count, size, aspect, light, session, tags, model outcome, scores,
// curation issues, CLIP text), image / box scope, active-filter chips, presets and the live count.
// The engine is core/filter.js; the result is S.filterMask (1 = shown), which plot/render.js applies.

import { errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { bindPopover } from '../core/popover.js';
import type { Agg, FilterExt, FilterResult } from '../core/filter.js';
import { buildAgg, emptyFilter, filterActive, idMask, normalizeFilter, prepareTokens, refreshAgg, runFilter } from '../core/filter.js';
import { waitJob } from '../core/jobs.js';
import { featureLabel, meta, pts, S } from '../core/state.js';
import { clsColor, clsName, ERR } from '../core/theme.js';
import { applyFilterMask } from '../plot/render.js';
import type { ClsMode, FilterSpec, NameMode, Tags } from '../types.js';
import type { CurIds, CurSummary } from '../types-views.js';
import { isImageUnit, isRows } from './preview.js';
import { saveSettings, SETTING_HOOKS } from './controls.js';
import { closestOf } from './ev-util.js';
import { setSelection } from './gallery.js';
import { onFilterChanged } from './image-grid.js';

const DEBOUNCE_MS = 120;
type RangeKey = 'nbox' | 'area' | 'aspect' | 'brightness' | 'blur';
const RANGES: ReadonlyArray<readonly [RangeKey, string, number]> = [
  ['nbox', 'Boxes per image', 1],
  ['area', 'Box size (% of image)', 0.01],
  ['aspect', 'Aspect ratio (w / h)', 0.01],
  ['brightness', 'Brightness (0–255)', 1],
  ['blur', 'Sharpness', 1],
];
const isRangeKey = (k: string | undefined): k is RangeKey => RANGES.some((r) => r[0] === k);

/** The server-side predicates' masks (core/filter.ts FilterExt) plus the keys that tell when they are stale. */
type FbExt = FilterExt & { tagKey: string | null; tagSrc?: Tags | undefined; issueKey: string | null; clipKey: string | null };

interface FilterBarState {
  F: FilterSpec;
  presets: Record<string, FilterSpec>;
  agg: Agg | null;
  res: FilterResult | null;
  issueTypes: CurSummary;      // curation summary {type: {n, label, severity}}
  ext: FbExt;
  ms: number;                  // last run time (performance.now)
  chips: Array<[string, string]>;   // active-filter chips [[key, text]] (fitChips)
}

export const FB: FilterBarState = {
  F: emptyFilter(),
  presets: {},
  agg: null,
  res: null,
  issueTypes: {},
  ext: { tagMask: null, tagKey: null, issueBox: null, issueImg: null, issueKey: null, clipMask: null, clipKey: null },
  ms: 0,
  chips: [],
};

let timer: ReturnType<typeof setTimeout> | undefined;

export function filterAgg(): Agg {
  const P = pts();
  if (!FB.agg || FB.agg.n !== P.cls.length) {
    const a = FB.agg = buildAgg(P, meta().classes.length);
    // name tokens for "related" matching (~35 ms on 50k names): built when the page is idle, not on a keystroke
    const idle = (f: () => void): void => { if (window.requestIdleCallback) window.requestIdleCallback(f); else setTimeout(f, 200); };
    idle(() => { if (!a.tokens) prepareTokens(a); });
  }
  return FB.agg;
}

// ---------------------------------------------------------------- running

export function scheduleFilter(save = true): void {
  clearTimeout(timer);
  timer = setTimeout(() => runNow(save), DEBOUNCE_MS);
}

export function runNow(save = false): void {
  clearTimeout(timer);
  if (!S.P) return;
  const F = FB.F, a = filterAgg();
  if (F.tags.length) {
    const key = F.tags.join('\u0000');
    if (FB.ext.tagKey !== key || FB.ext.tagSrc !== S.tags) {
      FB.ext.tagMask = idMask(a.n, F.tags.flatMap((t) => S.tags[t] || []));
      FB.ext.tagKey = key;
      FB.ext.tagSrc = S.tags;
    }
  }
  const t0 = performance.now();
  const res = runFilter(F, S.P, a, FB.ext);
  FB.ms = performance.now() - t0;
  FB.res = res;
  S.filterMask = res.mask;
  renderStatus();
  applyFilterMask();
  onFilterChanged();
  if (save) saveSettings();
  if (F.issues.length) void ensureIssues();
  if (F.clip.q.trim()) void ensureClip();
}

// after curation changed classes / deleted boxes: class bitsets and box counts per image, then re-run
export function onDataPatched(): void {
  if (FB.agg) refreshAgg(FB.agg, pts());
  FB.ext.issueKey = null;                      // the issue lists changed too
  runNow(false);
}

// the Curation view: "show these issues on the plot"
export function setIssueFilter(types: string[]): void {
  FB.F.issues = types.slice();
  FB.ext.issueKey = null;
  syncInputs();
  runNow(true);
}

export function setIssueTypes(summary: CurSummary | null | undefined): void {
  FB.issueTypes = summary || {};
  if (!$req('fb-more-pop').classList.contains('hidden')) renderMore();
}

async function ensureIssues(): Promise<void> {
  const key = FB.F.issues.slice().sort().join(',');
  if (FB.ext.issueKey === key) return;
  FB.ext.issueKey = key;
  try {
    const r = await getJSON<CurIds>(`api/curation/ids?types=${encodeURIComponent(key)}`);
    if (FB.ext.issueKey !== key) return;
    const a = filterAgg();
    FB.ext.issueBox = idMask(a.n, r.boxes);
    const img = new Uint8Array(a.nImg);
    for (const name of r.images) { const j = a.imgOf.get(name); if (j !== undefined) img[j] = 1; }
    FB.ext.issueImg = img;
    runNow(false);
  } catch (e) { FB.ext.issueKey = null; }
}

type TextSearchResult = { ready: true; id: number[]; score: number[] } | { ready: false; job_id: number };

async function ensureClip(): Promise<void> {
  const q = FB.F.clip.q.trim(), k = FB.F.clip.k, feature = $req<HTMLSelectElement>('feature').value;
  const key = `${feature}\u0000${q}\u0000${k}`;
  if (FB.ext.clipKey === key) return;
  FB.ext.clipKey = key;
  FB.ext.clipMask = new Uint8Array(filterAgg().n);        // nothing matches until the server answered
  setErr('');
  try {
    const body = { query: q, feature, k };
    let r = await postJSON<TextSearchResult>('api/text-search', body);
    if (!r.ready) { await waitJob(r.job_id); r = await postJSON<TextSearchResult>('api/text-search', body); }
    if (!r.ready) throw new Error('the text search did not finish');
    if (FB.ext.clipKey !== key) return;
    FB.ext.clipMask = idMask(filterAgg().n, r.id);
    runNow(false);
  } catch (e) {
    if (FB.ext.clipKey === key) { FB.ext.clipKey = null; setErr(`Text filter: ${errText(e)}`); }
  }
}

// ---------------------------------------------------------------- status: count, error, badges, chips

function setErr(msg: string): void {
  $req('fb-err').textContent = msg;
  $req('fb-err').classList.toggle('hidden', !msg);
  $req('fb-q').setAttribute('aria-invalid', String(!!msg && FB.F.name.mode === 'regex'));
}

const fmt = (n: number): string => Number(n).toLocaleString();

function rangeText(r: FilterSpec['nbox'], unit = ''): string {
  if (r[0] !== null && r[1] !== null) return `${r[0]}–${r[1]}${unit}`;
  return r[0] !== null ? `≥ ${r[0]}${unit}` : `≤ ${r[1]}${unit}`;
}

function activeChips(): Array<[string, string]> {
  const F = FB.F, out: Array<[string, string]> = [];
  const modeTxt: Record<NameMode, string> = { substr: 'contains', glob: 'matches', regex: 'regex', fuzzy: 'related to' };
  if (F.name.q.trim()) out.push(['name', `name ${F.name.neg ? 'not ' : ''}${modeTxt[F.name.mode]} “${F.name.q.trim()}”`]);
  if (F.cls.ids.length) out.push(['cls', `${F.cls.mode} of ${F.cls.ids.map((c) => clsName(c)).join(', ')}`]);
  for (const [k, label] of RANGES) {
    if (F[k][0] !== null || F[k][1] !== null) out.push([k, `${label.replace(/ \(.*\)$/, '').toLowerCase()} ${rangeText(F[k], k === 'area' ? '%' : '')}`]);
  }
  if (F.session.length) out.push(['session', `session: ${F.session.length > 2 ? `${F.session.length} sessions` : F.session.join(', ')}`]);
  if (F.tags.length) out.push(['tags', `tag: ${F.tags.join(', ')}`]);
  if (F.m_status.length) out.push(['m_status', `model: ${F.m_status.map((s) => (s < 0 ? 'not scored' : ERR[s]?.label)).join(', ')}`]);
  if (F.suspicion_min !== null) out.push(['suspicion_min', `suspicion ≥ ${F.suspicion_min}`]);
  if (F.outlier_min !== null) out.push(['outlier_min', `outlier ≥ ${F.outlier_min}`]);
  if (F.cl_quality_max !== null) out.push(['cl_quality_max', `cleanlab quality ≤ ${F.cl_quality_max}`]);
  if (F.cl_issue) out.push(['cl_issue', 'cleanlab label issues']);
  if (F.issues.length) out.push(['issues', `issue: ${F.issues.map((t) => FB.issueTypes[t]?.label || t).join(', ')}`]);
  if (F.clip.q.trim()) out.push(['clip', `text “${F.clip.q.trim()}” (top ${F.clip.k})`]);
  return out;
}

function renderStatus(): void {
  const F = FB.F, res = FB.res;
  if (!res) return;
  setErr(res.error || '');
  const a = filterAgg();
  const active = filterActive(F);
  // an embeddings session: a row is its own "image", so one count says it all
  $req('fb-count').textContent = isRows()
    ? (active ? `${fmt(res.nBox)} of ${fmt(a.n)} rows` : `${fmt(res.nBox)} rows`)
    : isImageUnit() ? (active ? `${fmt(res.nImg)} of ${fmt(a.nImg)} images` : `${fmt(res.nImg)} images`)
    : active ? `${fmt(res.nImg)} of ${fmt(a.nImg)} images · ${fmt(res.nBox)} boxes`
      : `${fmt(res.nImg)} images · ${fmt(res.nBox)} boxes`;
  $req('fb-count').title = `filter ran in ${FB.ms.toFixed(1)} ms`;
  $req<HTMLButtonElement>('fb-show').disabled = !active || !res.nBox;
  const nCls = F.cls.ids.length;
  $req('fb-cls-badge').textContent = String(nCls);
  $req('fb-cls-badge').classList.toggle('hidden', !nCls);
  const nMore = activeChips().filter(([k]) => !['name', 'cls'].includes(k)).length;
  $req('fb-more-badge').textContent = String(nMore);
  $req('fb-more-badge').classList.toggle('hidden', !nMore);
  FB.chips = activeChips();
  fitChips();
  $req('filter-bar').classList.toggle('active', active);
}

// The active-filter chips stay on one line: the ones that do not fit move (last first) into the "+N" popover.
// One layout read per render (and per filter-bar resize), never per frame.
function fitChips(): void {
  const chips = FB.chips || [];
  const row = $req('fb-chips'), pop = $req('fb-chips-pop'), more = $req('fb-chips-more');
  const html = ([k, t]: [string, string]): string =>
    `<span class="chip fb-chip">${esc(t)}<button type="button" class="fb-x" data-clear="${k}" aria-label="Remove filter: ${esc(t)}" title="Remove">${icon('x')}</button></span>`;
  $req('fb-chips-line').classList.toggle('hidden', !chips.length);
  row.innerHTML = chips.map(html).join('');
  $req('fb-chips-clear').classList.toggle('hidden', chips.length < 2);
  let n = 0;
  more.classList.add('hidden');
  if (chips.length && row.scrollWidth > row.clientWidth) {
    more.classList.remove('hidden');
    more.textContent = `+${chips.length}`;               // widest label while measuring
    const els = [...row.querySelectorAll('.fb-chip')];
    while (n < els.length && row.scrollWidth > row.clientWidth) els[els.length - 1 - n++]?.remove();
    more.textContent = `+${n}`;
    more.setAttribute('aria-label', `${n} more active filter${n > 1 ? 's' : ''}`);
  }
  pop.innerHTML = n ? '<div class="pop-title">More active filters</div><div class="chips">' + chips.slice(chips.length - n).map(html).join('') + '</div>' : '';
  if (!n && !pop.classList.contains('hidden')) { pop.classList.add('hidden'); more.setAttribute('aria-expanded', 'false'); }
}

function resetField<K extends keyof FilterSpec>(F: FilterSpec, d: FilterSpec, k: K): void { F[k] = d[k]; }
const isFilterKey = (k: string): k is keyof FilterSpec => k in emptyFilter();

function clearKey(k: string): void {
  const F = FB.F, d = emptyFilter();
  if (k === 'all') { FB.F = { ...d, scope: F.scope }; return; }
  if (k === 'name') F.name.q = '';
  else if (k === 'clip') F.clip.q = '';
  else if (isFilterKey(k)) resetField(F, d, k);
}

// ---------------------------------------------------------------- UI

// the name field's options trigger says the mode only when it is not the default ("contains", matching)
const MODE_SHORT: Record<NameMode, string> = { substr: 'contains', glob: 'wildcard', regex: 'regex', fuzzy: 'related' };
function syncNameOpt(): void {
  const { mode, neg } = FB.F.name;
  const t = [neg ? 'not' : '', mode !== 'substr' ? MODE_SHORT[mode] : ''].filter(Boolean).join(' · ');
  const el = $req('fb-name-mode');
  el.textContent = t;
  el.classList.toggle('hidden', !t);
  $req('fb-name-btn').classList.toggle('on', !!t);
}

function syncInputs(): void {
  const F = FB.F;
  $req<HTMLInputElement>('fb-q').value = F.name.q;
  $req<HTMLSelectElement>('fb-mode').value = F.name.mode;
  $req<HTMLInputElement>('fb-neg').checked = F.name.neg;
  syncNameOpt();
  for (const b of $req('fb-cls-mode').querySelectorAll<HTMLElement>('button')) b.setAttribute('aria-pressed', String(b.dataset.m === F.cls.mode));
  for (const b of $req('fb-scope').querySelectorAll<HTMLElement>('button')) b.setAttribute('aria-pressed', String(b.dataset.scope === F.scope));
  for (const ch of $req('fb-cls-chips').querySelectorAll<HTMLElement>('.chip')) {
    const on = F.cls.ids.includes(+(ch.dataset.c ?? NaN));
    ch.classList.toggle('off', !on);
    ch.setAttribute('aria-pressed', String(on));
  }
  if (!$req('fb-more-pop').classList.contains('hidden')) renderMore();
}

function buildClassChips(): void {
  $req('fb-cls-chips').innerHTML = meta().classes.map((c, i) =>
    `<span class="chip off" data-c="${i}" role="button" tabindex="0" aria-pressed="false"><span class="dot" style="background:${clsColor(i)}"></span>${esc(c)}</span>`).join('');
}

const num = (v: number | null | undefined): string => (v === null || v === undefined ? '' : String(v));

function renderMore(): void {
  const F = FB.F, P = pts();
  const rows = RANGES.map(([k, label, step]) => `<div class="fb-range"><span id="fb-l-${k}">${label}</span>` +
    `<input type="number" step="${step}" data-k="${k}" data-i="0" value="${num(F[k][0])}" placeholder="min" aria-labelledby="fb-l-${k}" aria-label="minimum">` +
    `<span class="muted">–</span><input type="number" step="${step}" data-k="${k}" data-i="1" value="${num(F[k][1])}" placeholder="max" aria-label="maximum"></div>`).join('');
  const sessions = [...(S.sessionIdx ? S.sessionIdx.keys() : [])].filter((s) => !s.startsWith('__singleton__')).slice(0, 1000);
  const sess = sessions.length > 1 ? `<label class="fb-lbl" for="fb-sess">Session</label><select id="fb-sess" multiple size="${Math.min(5, sessions.length)}" data-k="session">` +
    sessions.map((s) => `<option value="${esc(s)}" ${F.session.includes(s) ? 'selected' : ''}>${esc(s)}</option>`).join('') + '</select>' : '';
  const tags = Object.keys(S.tags || {});
  const tagHtml = tags.length ? `<div class="fb-lbl">Tags</div><div class="fb-checks">` + tags.map((t) =>
    `<label class="check"><input type="checkbox" data-k="tags" value="${esc(t)}" ${F.tags.includes(t) ? 'checked' : ''}> ${esc(t)} <span class="muted num">${(S.tags[t] ?? []).length}</span></label>`).join('') + '</div>' : '';
  const model = P.m_status ? `<div class="fb-lbl">Model outcome</div><div class="fb-checks">` + [-1, 0, 1, 2, 3].map((s) =>
    `<label class="check"><input type="checkbox" data-k="m_status" value="${s}" ${F.m_status.includes(s) ? 'checked' : ''}> ${s < 0 ? 'not scored' : ERR[s]?.label}</label>`).join('') + '</div>' : '';
  const scores = P.suspicion ? `<div class="fb-range"><span id="fb-l-sus">Label suspicion ≥</span><input type="number" min="0" max="1" step="0.05" data-k="suspicion_min" value="${num(F.suspicion_min)}" aria-labelledby="fb-l-sus"></div>` +
    `<div class="fb-range"><span id="fb-l-out">Outlier score ≥</span><input type="number" min="0" step="0.01" data-k="outlier_min" value="${num(F.outlier_min)}" aria-labelledby="fb-l-out"></div>` : '';
  const cl = P.cl_quality ? `<div class="fb-lbl">cleanlab <span class="muted">(${esc(S.lq ? featureLabel(S.lq.feature) : '')})</span></div>` +
    `<div class="fb-range"><span id="fb-l-clq">Label quality ≤</span><input type="number" min="0" max="1" step="0.05" data-k="cl_quality_max" value="${num(F.cl_quality_max)}" aria-labelledby="fb-l-clq"></div>` +
    `<div class="fb-checks"><label class="check"><input type="checkbox" data-k="cl_issue" ${F.cl_issue ? 'checked' : ''}> only label issues</label></div>` : '';
  const types = Object.entries(FB.issueTypes);
  const issues = `<div class="fb-lbl">Curation issues</div>` + (types.length ? '<div class="fb-checks">' + types.map(([t, v]) =>
    `<label class="check"><input type="checkbox" data-k="issues" value="${t}" ${F.issues.includes(t) ? 'checked' : ''}> ${esc(v.label)} <span class="muted num">${fmt(v.n)}</span></label>`).join('') + '</div>'
    : '<p class="hint">Scan the dataset in the Curation view to filter by issue.</p>');
  const f = S.status && S.status.features && S.status.features.find((x) => x.key === $req<HTMLSelectElement>('feature').value);
  const clip = f && f.text ? `<div class="fb-lbl" id="fb-l-clip">Text (${esc(f.label)})</div><div class="fb-clip"><input type="search" data-k="clip" value="${esc(F.clip.q)}" placeholder="e.g. person on a bike" aria-labelledby="fb-l-clip">` +
    `<input type="number" min="1" max="20000" data-k="clip_k" value="${F.clip.k}" aria-label="How many boxes" title="How many closest boxes"></div>` : '';
  $req('fb-more-pop').innerHTML = `<div class="pop-title">More filters</div>${rows}${sess}${tagHtml}${model}${scores}${cl}${issues}${clip}` +
    '<button type="button" class="ghost fb-clear" data-clear="more">Clear these filters</button>';
}

function onMoreInput(e: Event): void {
  const el = e.target;
  if (!(el instanceof HTMLInputElement || el instanceof HTMLSelectElement)) return;
  const k = el.dataset.k, F = FB.F;
  if (!k) return;
  if (el.type === 'number' && el.dataset.i !== undefined && isRangeKey(k)) {
    const v = el.value === '' ? null : +el.value;
    const i = +el.dataset.i;
    if (i === 0 || i === 1) F[k][i] = v !== null && Number.isFinite(v) ? v : null;
  } else if (k === 'cl_issue') {
    F.cl_issue = el instanceof HTMLInputElement && el.checked;
  } else if (k === 'suspicion_min' || k === 'outlier_min' || k === 'cl_quality_max') {
    F[k] = el.value === '' || !Number.isFinite(+el.value) ? null : +el.value;
  } else if (k === 'session') {
    if (el instanceof HTMLSelectElement) F.session = [...el.selectedOptions].map((o) => o.value);
  } else if (k === 'tags' || k === 'issues') {
    const v = el.value, on = el instanceof HTMLInputElement && el.checked;
    F[k] = on ? [...new Set([...F[k], v])] : F[k].filter((x) => x !== v);
    if (k === 'issues') FB.ext.issueKey = null;
  } else if (k === 'm_status') {
    const v = +el.value, on = el instanceof HTMLInputElement && el.checked;
    F.m_status = on ? [...new Set([...F.m_status, v])] : F.m_status.filter((x) => x !== v);
  } else if (k === 'clip') {
    if (e.type !== 'change') return;            // a server search: on Enter / blur, not per keystroke
    F.clip.q = el.value;
  } else if (k === 'clip_k') {
    F.clip.k = Math.max(1, Math.min(20000, +el.value || 500));
  }
  scheduleFilter();
}

function renderPresets(): void {
  const names = Object.keys(FB.presets).sort();
  $req('fb-preset-pop').innerHTML = '<div class="pop-title">Filter presets</div>' +
    (names.length ? '<ul class="fb-preset-list">' + names.map((n) =>
      `<li><button type="button" class="ghost fb-preset" data-apply="${esc(n)}">${esc(n)}</button>` +
      `<button type="button" class="icon-btn ghost" data-del="${esc(n)}" aria-label="Delete preset ${esc(n)}" title="Delete">${icon('trash')}</button></li>`).join('') + '</ul>'
      : '<p class="hint">No presets yet.</p>') +
    '<form class="fb-preset-save"><input id="fb-preset-name" type="text" placeholder="Preset name" aria-label="Preset name" maxlength="60" required>' +
    `<button type="submit" ${filterActive(FB.F) ? '' : 'disabled title="Set a filter first"'}>Save current</button></form>`;
}

const NAME_MODES: readonly NameMode[] = ['substr', 'glob', 'regex', 'fuzzy'];
const CLS_MODES: readonly ClsMode[] = ['any', 'all', 'none'];
const SCOPES: ReadonlyArray<FilterSpec['scope']> = ['image', 'box'];

export function initFilterBar(): void {
  buildClassChips();
  SETTING_HOOKS.push({
    collect: (out) => { out.filter = FB.F; out.filter_presets = FB.presets; },
    apply: (st) => {
      FB.F = normalizeFilter(st.filter);
      const pre = st.filter_presets;
      FB.presets = pre && typeof pre === 'object' ? Object.fromEntries(Object.entries(pre).map(([k, v]) => [k, normalizeFilter(v)])) : {};
      syncInputs();
      runNow(false);
    },
  });
  const q = $req<HTMLInputElement>('fb-q');
  q.addEventListener('input', () => { FB.F.name.q = q.value; scheduleFilter(); });
  q.addEventListener('keydown', (e) => { if (e.key === 'Escape' && q.value) { e.stopPropagation(); q.value = ''; FB.F.name.q = ''; runNow(true); } });
  const mode = $req<HTMLSelectElement>('fb-mode');
  mode.addEventListener('change', () => { FB.F.name.mode = NAME_MODES.find((m) => m === mode.value) ?? FB.F.name.mode; syncNameOpt(); runNow(true); });
  const neg = $req<HTMLInputElement>('fb-neg');
  neg.addEventListener('change', () => { FB.F.name.neg = neg.checked; syncNameOpt(); runNow(true); });
  bindPopover('fb-name-btn', 'fb-name-pop');
  syncNameOpt();
  $req('fb-cls-mode').addEventListener('click', (e) => {
    const b = closestOf(e, 'button[data-m]');
    const m = CLS_MODES.find((x) => x === b?.dataset.m);
    if (!b || !m) return;
    FB.F.cls.mode = m;
    syncInputs();
    scheduleFilter();
  });
  $req('fb-cls-chips').addEventListener('click', (e) => {
    const ch = closestOf(e, '.chip[data-c]');
    if (!ch) return;
    const c = +(ch.dataset.c ?? NaN), ids = FB.F.cls.ids;
    FB.F.cls.ids = ids.includes(c) ? ids.filter((x) => x !== c) : [...ids, c];
    syncInputs();
    scheduleFilter();
  });
  $req('fb-scope').addEventListener('click', (e) => {
    const b = closestOf(e, 'button[data-scope]');
    const sc = SCOPES.find((x) => x === b?.dataset.scope);
    if (!b || !sc) return;
    FB.F.scope = sc;
    syncInputs();
    runNow(true);
  });
  bindPopover('fb-cls-btn', 'fb-cls-pop');
  bindPopover('fb-more-btn', 'fb-more-pop', renderMore);
  bindPopover('fb-preset-btn', 'fb-preset-pop', renderPresets);
  $req('fb-more-pop').addEventListener('input', onMoreInput);
  $req('fb-more-pop').addEventListener('change', onMoreInput);
  const clear = (e: Event): void => {
    const b = closestOf(e, '[data-clear]');
    if (!b) return;
    if (b.dataset.clear === 'more') {
      const keep = { name: FB.F.name, cls: FB.F.cls, scope: FB.F.scope };
      FB.F = { ...emptyFilter(), ...keep };
    } else clearKey(b.dataset.clear ?? '');
    syncInputs();
    runNow(true);
  };
  $req('fb-chips-line').addEventListener('click', clear);     // the chips, their "+N" popover, Clear all
  bindPopover('fb-chips-more', 'fb-chips-pop');
  if (window.ResizeObserver) {                         // a narrower bar fits fewer chips
    let w = 0;
    new ResizeObserver((ents) => {
      const nw = Math.round(ents[0]?.contentRect.width ?? 0);
      if (nw !== w) { w = nw; if (FB.chips && FB.chips.length) fitChips(); }
    }).observe($req('filter-bar'));
  }
  $req('fb-more-pop').addEventListener('click', clear);
  $req('fb-preset-pop').addEventListener('click', (e) => {
    const a = closestOf(e, '[data-apply]'), d = closestOf(e, '[data-del]');
    const preset = a && a.dataset.apply !== undefined ? FB.presets[a.dataset.apply] : undefined;
    if (preset) {
      FB.F = normalizeFilter(preset);
      FB.ext.issueKey = null;
      syncInputs();
      runNow(true);
    } else if (d && d.dataset.del !== undefined) {
      delete FB.presets[d.dataset.del];
      renderPresets();
      saveSettings();
    }
  });
  $req('fb-preset-pop').addEventListener('submit', (e) => {
    e.preventDefault();
    const name = $req<HTMLInputElement>('fb-preset-name').value.trim();
    if (!name) return;
    FB.presets[name] = structuredClone(FB.F);
    renderPresets();
    saveSettings();
  });
  $req('fb-show').addEventListener('click', () => {
    const m = S.filterMask;
    if (!m) return;
    const ids: number[] = [];
    for (let i = 0; i < m.length; i++) if (m[i]) ids.push(i);
    setSelection(ids, `Filter: ${activeChips().map(([, t]) => t).join(' · ')}`);
  });
  bindNLFilter();
  runNow(false);
}

export function filterSpec(): FilterSpec { return FB.F; }

// ---------------------------------------------------------------- ◆ "Describe…": a sentence → a filter spec (LLM)
// The server answers in the emptyFilter() shape; normalizeFilter() makes any answer safe. The spec it replaced is
// kept, so Undo puts it back.
let nlPrev: FilterSpec | null = null;
let nlTimer: ReturnType<typeof setTimeout> | undefined;

// replace the whole filter (presets, the AI filter): same path as applying a preset
export function applyFilterSpec(spec: unknown): void {
  FB.F = normalizeFilter(spec);
  FB.ext.issueKey = null;
  syncInputs();
  runNow(true);
}

function nlMsg(text: string, err = false): void {
  const el = $req('fb-ai-msg');
  clearTimeout(nlTimer);
  el.textContent = text;
  el.classList.toggle('err', err);
  el.classList.toggle('hidden', !text);
  if (text && !err) nlTimer = setTimeout(() => el.classList.add('hidden'), 9000);
}

// also used by voice commands ("filter: small dark cars"); resolves to the server's explanation
export async function runNLFilter(raw: string | null | undefined): Promise<string> {
  const text = String(raw || '').trim();
  if (!text) return '';
  const input = $<HTMLInputElement>('fb-ai-q');
  if (input && input.value !== text) input.value = text;
  const go = $<HTMLButtonElement>('fb-ai-go');
  if (go) go.disabled = true;
  nlMsg('Thinking…');
  try {
    const r = await postJSON<{ spec: unknown; explain?: string }>('api/llm/filter', { text });
    nlPrev = structuredClone(FB.F);
    applyFilterSpec(r.spec);
    $req('fb-ai-undo').classList.remove('hidden');
    nlMsg(r.explain || 'Filter applied.');
    return r.explain || '';
  } catch (e) {
    nlMsg(errText(e), true);
    throw e;
  } finally { if (go) go.disabled = false; }
}

function bindNLFilter(): void {
  const form = $('fb-ai-form');
  if (!form) return;
  form.addEventListener('submit', (e) => { e.preventDefault(); runNLFilter($req<HTMLInputElement>('fb-ai-q').value).catch(() => {}); });
  $req('fb-ai-undo').addEventListener('click', () => {
    if (!nlPrev) return;
    applyFilterSpec(nlPrev);
    nlPrev = null;
    $req('fb-ai-undo').classList.add('hidden');
    nlMsg('');
  });
}
