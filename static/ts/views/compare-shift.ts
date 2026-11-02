// Compare › Image conditions: will a model trained on A meet the same pictures in B (a new camera, new lighting)?
// POST api/compare/shift measures A's and B's images (a job, cached per member) and answers with a report
// (data/shift.py): a verdict, per-metric drift (light, sharpness, noise, colour, capture: KS / PSI / B outside A's
// range), the light curve and RGB curves, scatter plots, the cameras, the shift-detector AUC, the fit of every model
// that already ran here, recommendations with an Ultralytics hyp snippet, and a preview of an A image as B's
// conditions would make it (api/compare/shift/preview). Loaded on demand by views/compare.ts, which owns A / B.

import type * as PL from 'plotly.js';
import type { Job, ShiftCurve, ShiftMetric, ShiftModel, ShiftReport, ShiftResult, ShiftSeverity } from '../types.js';
import { api, errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { hideWait, showWait, waitJob } from '../core/jobs.js';
import { originColor, plotBase, THEME } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { cfg } from './chart-util.js';
import { closestOf } from './ev-util.js';
import { setSelection } from './gallery.js';
import { setImageSubset } from './image-grid.js';
import { switchTab } from './layout.js';

declare const Plotly: typeof import('plotly.js');

/** The charts of this section (compare.ts adds them to CMP_PLOTS for resizing). */
export const SHIFT_PLOTS = ['shf-metric-chart', 'shf-light', 'shf-scatter', 'shf-model-conf', 'shf-model-vs'];

export interface ShiftHost {
  pair: () => { a: number; b: number };     // member indices of A and B
  alias: (k: number) => string;
  feature: () => string;                    // the Explore feature (its appearance AUC when computed)
}

type Group = 'all' | 'light' | 'sharpness' | 'noise' | 'colour' | 'capture';
interface ShiftUi {
  host: ShiftHost | null; report: ShiftReport | null; seq: number; running: boolean; group: Group; metric: string;
  curve: 'luma' | 'rgb'; scatter: 'light' | 'blur' | 'colour'; model: number; vs: string; strength: number;
  previewA: string; previewB: string; previewUrl: string; sample: number;
}
const H: ShiftUi = { host: null, report: null, seq: 0, running: false, group: 'all', metric: 'brightness', curve: 'luma',
  scatter: 'light', model: 0, vs: '', strength: 1, previewA: '', previewB: '', previewUrl: '', sample: 3000 };

const GROUPS: Array<[Group, string]> = [['all', 'All'], ['light', 'Light'], ['sharpness', 'Blur'], ['noise', 'Noise'], ['colour', 'Colour'], ['capture', 'Capture']];
const SEV_LABEL: Record<ShiftSeverity, string> = { ok: 'ok', minor: 'minor', moderate: 'moderate', major: 'major', 'n/a': 'n/a' };
const VERDICT_ICON = { fits: 'check-c', check: 'warn-c', shifted: 'alert' } as const;
const SCATTERS: Record<ShiftUi['scatter'], [string, string, string, boolean, boolean]> = {
  light: ['brightness', 'contrast', 'Light: brightness × contrast', false, false],
  blur: ['sharpness', 'noise', 'Sharpness × noise (log)', true, true],
  colour: ['saturation', 'cast_b', 'Colour: saturation × blue(−) / yellow(+)', false, false],
};

const A = (): number => H.host?.pair().a ?? 0;
const B = (): number => H.host?.pair().b ?? 1;
const nm = (k: number): string => H.host?.alias(k) ?? `#${k}`;
const n = (v: number | undefined | null): string => (v ?? 0).toLocaleString();

/** A number as people read it: 3 significant digits, thousands grouped. */
export function fmt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '–';
  const a = Math.abs(v);
  if (a >= 1000) return Math.round(v).toLocaleString();
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  if (a === 0) return '0';
  return v.toPrecision(2);
}
const pct = (v: number | null | undefined, digits = 0): string => (v == null || !Number.isFinite(v) ? '–' : `${(v * 100).toFixed(digits)}%`);
const signed = (v: number | null | undefined): string => (v == null || !Number.isFinite(v) ? '–' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(Math.abs(v) >= 10 ? 0 : 1)}%`);
/** B vs A: a percentage, or a factor once it passes ×2 / ÷2 ("+10788%" reads worse than "×109"). */
const delta = (m: ShiftMetric): string => {
  const r = m.ratio;
  if (r != null && Number.isFinite(r) && r > 0 && (r >= 2 || r <= 0.5)) return r >= 2 ? `×${fmt(r)}` : `÷${fmt(1 / r)}`;
  return signed(m.delta_pct);
};
const chip = (s: ShiftSeverity): string => `<span class="shf-sev" data-sev="${s === 'n/a' ? 'na' : s}">${SEV_LABEL[s]}</span>`;
const css = (name: string): string => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || THEME.muted;
const ax = (): Partial<PL.LayoutAxis> => ({ gridcolor: THEME.grid, linecolor: THEME.border, zerolinecolor: THEME.border, tickfont: { color: THEME.muted, size: 10 } });
const layout = (title: string, extra: Partial<PL.Layout> = {}): Partial<PL.Layout> => ({
  ...plotBase(THEME.surface, 10), showlegend: false, margin: { t: 22, b: 28, l: 38, r: 8 },
  title: { text: title, font: { size: 11, color: THEME.muted }, x: 0.02, y: 0.97 }, xaxis: ax(), yaxis: ax(), ...extra,
});
const plotCfg = { ...cfg, displayModeBar: false };

// ---------------------------------------------------------------- setup

/** Render the controls into `el` and bind them (once per combined dataset). */
export function initShift(el: HTMLElement, host: ShiftHost): void {
  H.host = host;
  el.innerHTML = `<p class="muted hint">Light, darkness, blur, noise, colour and camera of A's images vs B's: does a model trained on A fit B?</p>
    <div class="shf-run">
      <label class="m-ctl"><span class="m-lbl" title="Images measured per dataset (a fixed sample: bigger samples reuse what was measured)">images</span>
        <select id="shf-sample"><option value="1000">1,000</option><option value="3000" selected>3,000</option><option value="10000">10,000</option><option value="0">all</option></select></label>
      <button type="button" id="shf-go" class="primary">${icon('sun')}Analyse conditions</button>
    </div>
    <div id="shf-status" class="cmp-status muted hint" role="status" aria-live="polite"></div>
    <div id="shf-body"></div>`;
  $req('shf-go').onclick = () => { void runShift(); };
  $req<HTMLSelectElement>('shf-sample').onchange = (e) => { H.sample = +(e.target as HTMLSelectElement).value; };
  el.addEventListener('click', onClick);
  el.addEventListener('keydown', (e) => {
    const t = e.target instanceof HTMLElement ? e.target : null;
    if (t && (e.key === 'Enter' || e.key === ' ') && t.matches('tr[data-shf-metric]')) { e.preventDefault(); t.click(); }
  });
  el.addEventListener('change', onChange);
  window.addEventListener('theme-changed', () => { if (H.report) renderCharts(); });
}

/** A or B changed: the report is of another pair now. */
export function onPair(): void {
  if (!H.report) return;
  const r = H.report;
  if (r.a === nm(A()) && r.b === nm(B())) return;
  H.report = null;
  H.seq++;
  $req('shf-body').innerHTML = '';
  $req('shf-status').textContent = `A and B changed: Analyse conditions again for ${nm(A())} vs ${nm(B())}.`;
}

/** The report shown (the assistant's page context). */
export const shiftReport = (): ShiftReport | null => H.report;

// ---------------------------------------------------------------- run

export async function runShift(): Promise<void> {
  if (H.running) return;
  const seq = ++H.seq;
  const body = { a: nm(A()), b: nm(B()), sample: H.sample, feature: H.host?.feature() ?? '' };
  H.running = true;
  const go = $req<HTMLButtonElement>('shf-go');
  go.disabled = true;
  $req('shf-status').textContent = `Measuring the images of ${body.a} and ${body.b}…`;
  try {
    let r = await postJSON<ShiftResult>('api/compare/shift', body);
    for (let tries = 0; !r.ready && tries < 3; tries++) {
      await waitJob(r.job_id, (j: Job) => showWait(j));
      r = await postJSON<ShiftResult>('api/compare/shift', body);
    }
    hideWait();
    if (seq !== H.seq) return;
    if (!r.ready) throw new Error('the measuring job was cancelled');
    H.report = r.report;
    H.model = 0;
    H.vs = '';
    H.previewA = H.previewB = '';
    const st = r.report.n;
    $req('shf-status').textContent = `${n(st.a)} of ${n(st.total_a)} images of ${body.a} and ${n(st.b)} of ${n(st.total_b)} of ${body.b} measured`
      + `${st.failed ? ` · ${n(st.failed)} unreadable` : ''}.`;
    render();
  } catch (e) {
    hideWait();
    if (seq === H.seq) {
      $req('shf-status').innerHTML = `<span class="txt-danger">${icon('alert')}${esc(errText(e))}</span>`;
      toast(errText(e), 'err');
    }
  } finally {
    H.running = false;
    go.disabled = false;
  }
}

// ---------------------------------------------------------------- render

function render(): void {
  const r = H.report;
  if (!r) return;
  const v = r.verdict, sep = r.separability, app = sep.appearance;
  const mdl = r.models[0];
  const kpi = (val: string, label: string, title: string, sev = ''): string =>
    `<div class="shf-kpi"${sev ? ` data-sev="${sev}"` : ''} title="${esc(title)}"><b>${val}</b><span>${esc(label)}</span></div>`;
  const aucSev = (x: number | null | undefined): string => (x == null ? '' : x >= 0.9 ? 'major' : x >= 0.75 ? 'moderate' : x >= 0.6 ? 'minor' : 'ok');
  const kpis = [
    kpi(String(v.score), 'shift score / 100', 'How far B is from A overall (0 = alike, 100 = a different domain)', v.level === 'fits' ? 'ok' : v.level === 'check' ? 'moderate' : 'major'),
    kpi(sep.auc == null ? '–' : sep.auc.toFixed(2), 'shift detector AUC', `A classifier told to say "A or B" from light, blur, noise and colour alone: 0.5 = cannot tell (alike), 1 = every image gives its side away. ${sep.words}`, aucSev(sep.auc)),
    app ? kpi(app.auc.toFixed(2), `appearance AUC · ${app.feature}`, `The same test on the ${app.feature} features (what a model sees): ${app.words}`, aucSev(app.auc)) : '',
    mdl && mdl.a && mdl.b ? kpi(`${pct(mdl.a.mean_top)} → ${pct(mdl.b.mean_top)}`, `confidence · ${mdl.name}`, 'Mean top detection confidence per image on A vs on B (no labels needed): a drop says the model is less sure on B', mdl.drop != null && mdl.drop >= 0.15 ? 'major' : mdl.drop != null && mdl.drop >= 0.05 ? 'moderate' : 'ok') : '',
  ].join('');
  const drivers = sep.drivers.length ? `<p class="muted hint">What gives B away: ${sep.drivers.map((d) => `<button type="button" class="linkish" data-shf-goto="${esc(d.key)}">${esc(d.label)}</button>`).join(', ')}</p>` : '';
  const groupSeg = `<div class="segmented shf-groups" role="group">${GROUPS.map(([g, label]) => {
    const w = g === 'all' ? '' : r.groups[g]?.worst ?? 'ok';
    return `<button type="button" class="view-btn${g === H.group ? ' active' : ''}" data-shf-group="${g}" aria-pressed="${g === H.group}">${label}${w && w !== 'ok' && w !== 'n/a' ? `<i class="shf-dot" data-sev="${w}"></i>` : ''}</button>`;
  }).join('')}</div>`;
  $req('shf-body').innerHTML = `
    <div class="shf-verdict" data-level="${v.level}">${icon(VERDICT_ICON[v.level])}<div><b>${esc(v.title)}</b><p>${esc(v.text)}</p></div></div>
    <div class="shf-kpis">${kpis}</div>${drivers}
    ${groupSeg}
    <div class="cmp-table-wrap"><table class="class-table shf-table"><thead><tr><th>metric</th>
      <th class="r"><span class="cmp-tag" style="--c:${originColor(A())}">A</span></th><th class="r"><span class="cmp-tag" style="--c:${originColor(B())}">B</span></th>
      <th class="r" title="B's median vs A's">Δ</th><th class="r" title="share of B's images outside the range of 98% of A's">B out</th><th></th></tr></thead>
      <tbody id="shf-rows"></tbody></table></div>
    <div id="shf-metric-chart" class="cmp-chart sm"></div>
    <div id="shf-metric-st" class="muted hint shf-metric-st"></div>
    <h4 class="shf-h">Light curve <span class="muted">share of pixels per brightness level</span></h4>
    <div class="m-ctl"><div class="segmented" role="group">
      <button type="button" class="view-btn${H.curve === 'luma' ? ' active' : ''}" data-shf-curve="luma">Luma</button>
      <button type="button" class="view-btn${H.curve === 'rgb' ? ' active' : ''}" data-shf-curve="rgb">R G B</button></div></div>
    <div id="shf-light" class="cmp-chart sm"></div>
    <h4 class="shf-h">Image by image</h4>
    <div class="m-ctl"><div class="segmented" role="group">${(Object.keys(SCATTERS) as Array<ShiftUi['scatter']>).map((k) =>
      `<button type="button" class="view-btn${H.scatter === k ? ' active' : ''}" data-shf-scatter="${k}">${k === 'light' ? 'Light' : k === 'blur' ? 'Blur × noise' : 'Colour'}</button>`).join('')}</div></div>
    <div id="shf-scatter" class="cmp-chart"></div>
    ${renderCameras(r)}
    ${renderModels(r)}
    <h4 class="shf-h">Preview <span class="muted">an image of A as B's conditions would make it</span></h4>
    <div class="m-ctl"><span class="m-lbl" title="0 = the A image unchanged, 1 = moved by the whole A → B change of the medians">strength</span>
      <input id="shf-strength" type="range" min="0" max="1.5" step="0.1" value="${H.strength}"><span id="shf-strength-v" class="num">${H.strength.toFixed(1)}</span>
      <button type="button" class="ghost" data-shf-preview="next">${icon('refresh')}Other image</button></div>
    <div id="shf-preview" class="shf-preview"></div>
    <div id="shf-preview-p" class="muted hint"></div>
    <h4 class="shf-h">What to do</h4>
    <ul class="shf-recs">${r.recommendations.length ? r.recommendations.map((x) => `<li>${chip(x.severity)}<span>${esc(x.text)}</span></li>`).join('')
      : '<li><span>Nothing stands out: keep the usual augmentation and validate on a few labelled B images.</span></li>'}</ul>
    <details class="shf-yaml"><summary>Training augmentation (Ultralytics hyp)</summary>
      <pre id="shf-yaml">${esc(r.hyp_yaml)}</pre><button type="button" class="ghost" data-shf-copy>${icon('copy')}Copy</button></details>
    <div class="shf-actions">
      <button type="button" data-shf-list="most_b" title="B's images the shift detector is surest are B: label these first">${icon('tag')}Most B-like images</button>
      <button type="button" class="ghost" data-shf-export="json">${icon('download')}JSON</button>
      <button type="button" class="ghost" data-shf-export="csv">${icon('download')}CSV</button>
    </div>`;
  renderRows();
  renderCharts();
  void loadPreview(false);
}

function rowsOf(r: ShiftReport): ShiftMetric[] {
  return r.metrics.filter((m) => (H.group === 'all' ? m.available : m.group === H.group));
}

function renderRows(): void {
  const r = H.report;
  if (!r) return;
  const rows = rowsOf(r);
  const tb = $('shf-rows');
  if (!tb) return;
  tb.innerHTML = rows.map((m) => {
    if (!m.available) {
      return `<tr class="shf-na"><td colspan="6">${esc(m.label)} <span class="muted">· no value (A ${n(m.n_a)}, B ${n(m.n_b)} images)</span></td></tr>`;
    }
    const tip = `KS ${fmt(m.ks)} · PSI ${fmt(m.psi)} · effect ${fmt(m.effect)} · A outside its own p1–p99 ${pct(m.out_a, 1)}${m.low_n ? ' · few images' : ''}`;
    return `<tr data-shf-metric="${esc(m.key)}" tabindex="0" class="${m.key === H.metric ? 'sel' : ''}" title="${esc(tip)}">
      <td>${esc(m.label)}${m.unit ? ` <span class="muted">${esc(m.unit)}</span>` : ''}</td>
      <td class="r num">${fmt(m.a?.med)}</td><td class="r num">${fmt(m.b?.med)}</td>
      <td class="r num">${delta(m)}</td><td class="r num">${pct(m.out_b)}</td><td>${chip(m.severity)}</td></tr>`;
  }).join('') || '<tr><td colspan="6" class="muted">no metric in this group</td></tr>';
}

function renderCameras(r: ShiftReport): string {
  const ca = new Map(r.cameras.a), cb = new Map(r.cameras.b);
  const names = [...new Set([...ca.keys(), ...cb.keys()])].sort((x, y) => ((cb.get(y) ?? 0) + (ca.get(y) ?? 0)) - ((cb.get(x) ?? 0) + (ca.get(x) ?? 0))).slice(0, 12);
  const only = names.length === 1 && names[0] === 'unknown';
  const fmts = (side: Array<[string, number]>): string => side.map(([f, k]) => `${esc(f || '?')} ${n(k)}`).join(' · ');
  return `<h4 class="shf-h">Cameras <span class="muted">EXIF make / model</span></h4>
    ${only ? '<p class="muted hint">No image has EXIF camera facts (often stripped by export / resize tools).</p>' : `<div class="cmp-table-wrap"><table class="class-table shf-cams">
      <thead><tr><th>camera</th><th class="r">A</th><th class="r">B</th></tr></thead><tbody>${names.map((c) => `<tr>
      <td>${esc(c)}${!ca.has(c) && c !== 'unknown' ? ' <span class="shf-sev" data-sev="moderate">new in B</span>' : ''}</td>
      <td class="r num">${n(ca.get(c))}</td><td class="r num">${n(cb.get(c))}</td></tr>`).join('')}</tbody></table></div>`}
    <p class="muted hint">Formats: A ${fmts(r.formats.a)} · B ${fmts(r.formats.b)}</p>`;
}

function renderModels(r: ShiftReport): string {
  if (!r.models.length) {
    return `<h4 class="shf-h">Model fit</h4><p class="muted hint">No model has run on this combined dataset yet. Run one in the Model tab
      (it predicts A's and B's images once), then Analyse again: its confidence and accuracy on A vs B show here.</p>`;
  }
  const m = r.models[Math.min(H.model, r.models.length - 1)]!;
  const sel = r.models.length > 1 ? `<select id="shf-model" aria-label="Model">${r.models.map((x, i) => `<option value="${i}"${i === H.model ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}</select>` : `<b>${esc(m.name)}</b>`;
  const row = (label: string, f: (s: NonNullable<ShiftModel['a']>) => string, title = ''): string =>
    `<tr${title ? ` title="${esc(title)}"` : ''}><td>${label}</td><td class="r num">${m.a ? f(m.a) : '–'}</td><td class="r num">${m.b ? f(m.b) : '–'}</td></tr>`;
  const labelled = !!(m.a?.map50 != null && m.b?.map50 != null);
  const vsKeys = Object.keys(m.conf_vs ?? {});
  if (!H.vs || !vsKeys.includes(H.vs)) H.vs = vsKeys[0] ?? '';
  const label = (k: string): string => r.metrics.find((x) => x.key === k)?.label ?? k;
  return `<h4 class="shf-h">Model fit</h4>
    <div class="m-ctl"><span class="m-lbl">model</span>${sel}<span class="muted hint">conf ≥ ${m.conf}</span></div>
    <div class="cmp-table-wrap"><table class="class-table shf-model"><thead><tr><th></th><th class="r">A</th><th class="r">B</th></tr></thead><tbody>
      ${row('mean top confidence', (s) => pct(s.mean_top), 'The highest detection score per image, averaged')}
      ${row('images with nothing found', (s) => pct(s.none_found), `No detection at confidence ≥ ${m.conf}`)}
      ${row('detections / image', (s) => fmt(s.per_image))}
      ${labelled ? row('mAP50', (s) => fmt(s.map50)) + row('precision · recall', (s) => `${fmt(s.p)} · ${fmt(s.r)}`) : ''}
      ${row('labelled boxes', (s) => n(s.labels))}
    </tbody></table></div>
    ${!labelled ? '<p class="muted hint">mAP needs labels on both sides; confidence works without them.</p>' : ''}
    <div id="shf-model-conf" class="cmp-chart sm"></div>
    ${vsKeys.length ? `<div class="m-ctl"><span class="m-lbl">confidence vs</span><select id="shf-vs">${vsKeys.map((k) => `<option value="${esc(k)}"${k === H.vs ? ' selected' : ''}>${esc(label(k))}</option>`).join('')}</select></div>
    <div id="shf-model-vs" class="cmp-chart sm"></div>` : ''}`;
}

// ---------------------------------------------------------------- charts

function renderCharts(): void {
  renderMetricChart();
  renderLight();
  renderScatter();
  renderModelCharts();
}

function renderMetricChart(): void {
  const r = H.report, el = $('shf-metric-chart');
  if (!r || !el) return;
  const m = r.metrics.find((x) => x.key === H.metric && x.available) ?? r.metrics.find((x) => x.available);
  if (!m || !m.edges || !m.ha || !m.hb) { Plotly.purge(el); return; }
  H.metric = m.key;
  const e = m.edges;
  const mids = e.slice(0, -1).map((v, k) => (m.log ? Math.sqrt(v * e[k + 1]!) : (v + e[k + 1]!) / 2));
  const line = (h: number[], k: number, side: string): Partial<PL.PlotData> => ({
    type: 'scatter', mode: 'lines', line: { shape: 'hvh', width: 2, color: originColor(k) }, name: side,
    x: mids, y: h, fill: 'tozeroy', fillcolor: `${originColor(k)}22`, hovertemplate: `${esc(side)}: %{y:.1%} at %{x:.3g}<extra></extra>`,
  });
  const band = m.band ?? [e[0]!, e[e.length - 1]!];
  const vline = (x: number | undefined, k: number): Partial<PL.Shape> => ({ type: 'line', xref: 'x', yref: 'paper', x0: x ?? 0, x1: x ?? 0, y0: 0, y1: 1,
    line: { color: originColor(k), width: x == null ? 0 : 1, dash: 'dot' } });
  void Plotly.react(el, [line(m.ha, A(), nm(A())), line(m.hb, B(), nm(B()))], layout(`${m.label} · KS ${fmt(m.ks)} · PSI ${fmt(m.psi)}`, {
    xaxis: { ...ax(), type: m.log ? 'log' : 'linear' }, yaxis: { ...ax(), tickformat: '.0%' },
    shapes: [{ type: 'rect', xref: 'x', yref: 'paper', x0: band[0], x1: band[1], y0: 0, y1: 1, fillcolor: `${originColor(A())}14`, line: { width: 0 }, layer: 'below' },
      vline(m.a?.med, A()), vline(m.b?.med, B())],
  }), plotCfg);
  const st = $('shf-metric-st');
  if (st) {
    st.innerHTML = `Shaded: 98% of ${esc(nm(A()))}'s images. ${pct(m.out_b)} of ${esc(nm(B()))} falls outside (A itself ${pct(m.out_a)}).
      <span class="shf-links"><button type="button" class="linkish" data-shf-list="outside">B outside A's range</button> ·
      <button type="button" class="linkish" data-shf-list="low">lowest B</button> · <button type="button" class="linkish" data-shf-list="high">highest B</button> ·
      <button type="button" class="linkish" data-shf-list="low" data-side="a">lowest A</button></span>`;
  }
}

function renderLight(): void {
  const r = H.report, el = $('shf-light');
  if (!r || !el) return;
  const xs = Array.from({ length: r.curves.luma.a.length || r.curves.luma.b.length }, (_, k) => k * 8 + 4);
  const tr = (y: number[], color: string, name: string, dash: PL.Dash = 'solid', fill = false): Partial<PL.PlotData> => ({
    type: 'scatter', mode: 'lines', x: xs, y, name, line: { color, width: 2, dash, shape: 'spline' },
    ...(fill ? { fill: 'tozeroy' as const, fillcolor: `${color}1f` } : {}), hovertemplate: `${esc(name)}: %{y:.1%} at %{x}<extra></extra>`,
  });
  const traces = H.curve === 'luma'
    ? [tr(r.curves.luma.a, originColor(A()), nm(A()), 'solid', true), tr(r.curves.luma.b, originColor(B()), nm(B()), 'solid', true)]
    : (['r', 'g', 'b'] as const).flatMap((c) => [tr(r.curves[c].a, css(`--ch-${c}`), `${c.toUpperCase()} · ${nm(A())}`),
      tr(r.curves[c].b, css(`--ch-${c}`), `${c.toUpperCase()} · ${nm(B())}`, 'dot')]);
  void Plotly.react(el, traces, layout(H.curve === 'luma' ? 'luma: dark ← → bright' : `R G B · ${nm(A())} solid, ${nm(B())} dotted`, {
    xaxis: { ...ax(), range: [0, 255] }, yaxis: { ...ax(), tickformat: '.0%' },
  }), plotCfg);
}

function renderScatter(): void {
  const r = H.report, el = $('shf-scatter');
  if (!r || !el) return;
  const [kx, ky, title, lx, ly] = SCATTERS[H.scatter];
  const tr = (side: 'a' | 'b', k: number): Partial<PL.PlotData> => ({
    type: 'scattergl', mode: 'markers', name: nm(k), x: r.scatter[side][kx] ?? [], y: r.scatter[side][ky] ?? [],
    marker: { color: originColor(k), size: 4, opacity: 0.55 }, hovertemplate: `${esc(nm(k))}: %{x:.3g} × %{y:.3g}<extra></extra>`,
  });
  const lbl = (k: string): string => r.metrics.find((x) => x.key === k)?.label ?? k;
  void Plotly.react(el, [tr('a', A()), tr('b', B())], layout(title, {
    showlegend: true, legend: { orientation: 'h', x: 1, xanchor: 'right', y: 1.08, font: { size: 10, color: THEME.muted } },
    xaxis: { ...ax(), type: lx ? 'log' : 'linear', title: { text: lbl(kx), font: { size: 10, color: THEME.muted } } },
    yaxis: { ...ax(), type: ly ? 'log' : 'linear', title: { text: lbl(ky), font: { size: 10, color: THEME.muted } } },
    margin: { t: 26, b: 36, l: 44, r: 8 },
  }), plotCfg);
}

function renderModelCharts(): void {
  const r = H.report;
  const m = r?.models[Math.min(H.model, (r?.models.length ?? 1) - 1)];
  const el = $('shf-model-conf');
  if (!r || !m || !el) return;
  const bins = m.hist_bins, mids = bins.slice(0, -1).map((v, k) => (v + bins[k + 1]!) / 2);
  const tr = (s: ShiftModel['a'], k: number): Partial<PL.PlotData> => ({
    type: 'scatter', mode: 'lines', line: { shape: 'hvh', width: 2, color: originColor(k) }, name: nm(k), x: mids, y: s?.conf_hist ?? [],
    fill: 'tozeroy', fillcolor: `${originColor(k)}22`, hovertemplate: `${esc(nm(k))}: %{y:.1%} at %{x:.2f}<extra></extra>`,
  });
  void Plotly.react(el, [tr(m.a, A()), tr(m.b, B())], layout('detection confidence', { xaxis: { ...ax(), range: [0, 1] }, yaxis: { ...ax(), tickformat: '.0%' } }), plotCfg);
  const vs = $('shf-model-vs'), c: ShiftCurve | undefined = m.conf_vs?.[H.vs];
  if (!vs || !c) return;
  const line = (y: Array<number | null>, cnt: number[], k: number): Partial<PL.PlotData> => ({
    type: 'scatter', mode: 'lines+markers', x: c.x, y, name: nm(k), connectgaps: false, customdata: cnt,
    line: { color: originColor(k), width: 2 }, marker: { size: 5, color: originColor(k) },
    hovertemplate: `${esc(nm(k))}: %{y:.0%} confidence · %{customdata} images<extra></extra>`,
  });
  const lbl = r.metrics.find((x) => x.key === H.vs)?.label ?? H.vs;
  void Plotly.react(vs, [line(c.a, c.n_a, A()), line(c.b, c.n_b, B())], layout(`mean top confidence by ${lbl}`, {
    yaxis: { ...ax(), tickformat: '.0%', rangemode: 'tozero' }, xaxis: { ...ax(), type: r.metrics.find((x) => x.key === H.vs)?.log ? 'log' : 'linear' },
  }), plotCfg);
}

// ---------------------------------------------------------------- preview

async function names(which: string, metric: string, side: 'a' | 'b'): Promise<string[]> {
  const r = H.report;
  if (!r) return [];
  const q = new URLSearchParams({ key: r.key, which, metric, side });
  return (await getJSON<{ names: string[] }>(`api/compare/shift/images?${q.toString()}`)).names;
}

const pick = <T>(xs: T[]): T | undefined => xs[Math.floor(Math.random() * xs.length)];

async function loadPreview(next: boolean): Promise<void> {
  const r = H.report, box = $('shf-preview');
  if (!r || !box) return;
  const seq = H.seq;
  try {
    if (next || !H.previewA) {
      const [la, lb] = await Promise.all([names('high', 'brightness', 'a'), names('high', 'brightness', 'b')]);
      // the middle half of each side: typical images, not the extremes
      const mid = <T>(xs: T[]): T[] => xs.slice(Math.floor(xs.length / 4), Math.max(1, Math.ceil(xs.length * 3 / 4)));
      H.previewA = pick(mid(la)) ?? '';
      H.previewB = pick(mid(lb)) ?? '';
    }
    if (seq !== H.seq || !H.previewA) { if (!H.previewA) box.innerHTML = '<p class="muted hint">No image of A to preview.</p>'; return; }
    const q = new URLSearchParams({ key: r.key, name: H.previewA, strength: String(H.strength) });
    const res = await api(`api/compare/shift/preview?${q.toString()}`);
    const blob = await res.blob();
    if (seq !== H.seq) return;
    if (H.previewUrl) URL.revokeObjectURL(H.previewUrl);
    H.previewUrl = URL.createObjectURL(blob);
    const fig = (src: string, cap: string, k: number): string => `<figure><img src="${esc(src)}" alt="${esc(cap)}" loading="lazy"><figcaption><span class="cmp-tag" style="--c:${originColor(k)}">${k === A() ? 'A' : 'B'}</span>${esc(cap)}</figcaption></figure>`;
    box.innerHTML = fig(`api/img/raw?name=${encodeURIComponent(H.previewA)}&max_w=640`, H.previewA.split('/').slice(1).join('/'), A())
      + fig(H.previewUrl, `as ${nm(B())} would shoot it`, B())
      + (H.previewB ? fig(`api/img/raw?name=${encodeURIComponent(H.previewB)}&max_w=640`, `a real ${nm(B())} image`, B()) : '');
    let prm: Record<string, number> = {};
    try { prm = JSON.parse(res.headers.get('X-Shift-Params') || '{}') as Record<string, number>; } catch { prm = {}; }
    const p = $('shf-preview-p');
    if (p) {
      p.textContent = `Applied: brightness ×${fmt(prm['brightness_gain'])}, contrast ×${fmt(prm['contrast_gain'])}, saturation ×${fmt(prm['saturation_gain'])}, `
        + `tint a ${fmt(prm['tint_a'])} / b ${fmt(prm['tint_b'])}, blur σ ${fmt(prm['blur_sigma_px'])} px, noise σ ${fmt(prm['noise_sigma'])}. `
        + 'Run the model on such images to test it before B arrives.';
    }
  } catch (e) {
    if (seq === H.seq) box.innerHTML = `<p class="txt-danger hint">${esc(errText(e))}</p>`;
  }
}

// ---------------------------------------------------------------- events

async function openList(which: string, side: 'a' | 'b' = 'b'): Promise<void> {
  const r = H.report;
  if (!r) return;
  try {
    const list = await names(which, H.metric, side);
    if (!list.length) { toast('No image in that list', 'info'); return; }
    const lbl = r.metrics.find((m) => m.key === H.metric)?.label ?? H.metric;
    const who = side === 'a' ? r.a : r.b;
    const title = which === 'most_b' ? `Most ${r.b}-like images (label these first)` : which === 'outside' ? `${who} outside ${r.a}'s range of ${lbl}`
      : `${who} by ${lbl}, ${which === 'low' ? 'lowest' : 'highest'} first`;
    setSelection([], 'No selection');
    setImageSubset(list, title);
    switchTab('explore');
  } catch (e) {
    toast(errText(e), 'err');
  }
}

function download(name: string, text: string, type: string): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The metric table as CSV (one row per metric). */
export function reportCsv(r: ShiftReport): string {
  const head = ['metric', 'group', 'unit', 'severity', 'a_median', 'b_median', 'delta', 'delta_pct', 'ks', 'psi', 'effect', 'b_outside_a', 'a_outside_a', 'n_a', 'n_b'];
  const q = (v: unknown): string => { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const rows = r.metrics.map((m) => [m.key, m.group, m.unit, m.severity, m.a?.med, m.b?.med, m.delta, m.delta_pct, m.ks, m.psi, m.effect, m.out_b, m.out_a, m.n_a, m.n_b].map(q).join(','));
  return [head.join(','), ...rows].join('\n') + '\n';
}

function onClick(e: Event): void {
  const t = closestOf(e, '[data-shf-metric], [data-shf-group], [data-shf-curve], [data-shf-scatter], [data-shf-list], [data-shf-export], [data-shf-copy], [data-shf-preview], [data-shf-goto]');
  const r = H.report;
  if (!t || !r) return;
  const d = t.dataset;
  if (d['shfMetric']) { H.metric = d['shfMetric']; renderRows(); renderMetricChart(); return; }
  if (d['shfGoto']) {
    H.metric = d['shfGoto'];
    H.group = 'all';
    render();
    $('shf-metric-chart')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    return;
  }
  if (d['shfGroup']) {
    H.group = d['shfGroup'] as Group;
    document.querySelectorAll<HTMLElement>('[data-shf-group]').forEach((b) => { b.classList.toggle('active', b.dataset['shfGroup'] === H.group); b.setAttribute('aria-pressed', String(b.dataset['shfGroup'] === H.group)); });
    const first = rowsOf(r).find((m) => m.available);
    if (first && !rowsOf(r).some((m) => m.key === H.metric)) H.metric = first.key;
    renderRows();
    renderMetricChart();
    return;
  }
  if (d['shfCurve']) { H.curve = d['shfCurve'] as ShiftUi['curve']; segActive(t); renderLight(); return; }
  if (d['shfScatter']) { H.scatter = d['shfScatter'] as ShiftUi['scatter']; segActive(t); renderScatter(); return; }
  if (d['shfList']) { void openList(d['shfList'], d['side'] === 'a' ? 'a' : 'b'); return; }
  if (d['shfPreview'] !== undefined) { void loadPreview(true); return; }
  if (d['shfCopy'] !== undefined) {
    void navigator.clipboard?.writeText(r.hyp_yaml).then(() => toast('Copied', 'ok'), () => toast('Copy failed: select the text', 'err'));
    return;
  }
  if (d['shfExport'] === 'json') download(`image-conditions-${r.a}-vs-${r.b}.json`, JSON.stringify(r, null, 1), 'application/json');
  else if (d['shfExport'] === 'csv') download(`image-conditions-${r.a}-vs-${r.b}.csv`, reportCsv(r), 'text/csv');
}

function segActive(btn: HTMLElement): void {
  btn.parentElement?.querySelectorAll<HTMLElement>('.view-btn').forEach((b) => {
    b.classList.toggle('active', b === btn);
    b.setAttribute('aria-pressed', String(b === btn));
  });
}

let strengthTimer: ReturnType<typeof setTimeout> | undefined;
function onChange(e: Event): void {
  const t = e.target;
  if (!(t instanceof HTMLInputElement || t instanceof HTMLSelectElement)) return;
  if (t.id === 'shf-model') { H.model = +t.value; H.vs = ''; render(); return; }
  if (t.id === 'shf-vs') { H.vs = t.value; renderModelCharts(); return; }
  if (t.id === 'shf-strength') {
    H.strength = +t.value;
    $req('shf-strength-v').textContent = H.strength.toFixed(1);
    clearTimeout(strengthTimer);
    strengthTimer = setTimeout(() => { void loadPreview(false); }, 150);
  }
}
