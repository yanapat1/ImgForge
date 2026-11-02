// Compare › Image conditions › Stress test: a model run on member A's images under graded light, blur, noise, colour,
// JPEG and resolution changes (POST api/compare/stress, a job; data/stress.py). One small chart per axis: the score
// (mAP50 with labels, else consistency with the clean predictions) against the level, the safe window (>= 95 % of the
// clean score) shaded, A's own range shaded, and B's position from the Image conditions report with its expected
// score; a table, advice, a strip of one image at every level of an axis, CSV. Loaded by compare-shift.ts when its
// <details> opens.

import type * as PL from 'plotly.js';
import type { Job, ShiftSeverity, StressAxisInfo, StressResult, StressRun, StressSummary } from '../types.js';
import type { ModelInfo, ModelsPayload } from '../types-views.js';
import { errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { hideWait, showWait, waitJob } from '../core/jobs.js';
import { originColor, plotBase, THEME } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { cfg } from './chart-util.js';
import { closestOf } from './ev-util.js';
import { saveFile } from '../core/format.js';

declare const Plotly: typeof import('plotly.js');

export const STRESS_PLOTS = ['shf-stress-chart'];

export interface StressHost {
  memberA: () => string;                 // alias of A (the training-like data the model runs on)
  colorA: () => number; colorB: () => number;
  aliasB: () => string;
  reportKey: () => string;               // the Image conditions report on screen ('' = none): B's position
}

interface StressUi {
  host: StressHost | null; models: ModelInfo[]; axes: StressAxisInfo[]; model: string; n: number; pick: Set<string>;
  run: StressRun | null; sum: StressSummary | null; seq: number; running: boolean; strip: string;
}
const S: StressUi = { host: null, models: [], axes: [], model: '', n: 150, pick: new Set(), run: null, sum: null, seq: 0, running: false, strip: '' };

const STATUS_SEV: Record<string, ShiftSeverity> = { ok: 'ok', moderate: 'moderate', major: 'major' };
const chip = (s: ShiftSeverity, text: string = s): string => `<span class="shf-sev" data-sev="${s === 'n/a' ? 'na' : s}">${esc(text)}</span>`;
const num = (v: number | null | undefined, d = 2): string => (v == null || !Number.isFinite(v) ? '–' : v.toFixed(d));
const pct = (v: number | null | undefined): string => (v == null || !Number.isFinite(v) ? '–' : `${Math.round(v * 100)}%`);
const lvl = (v: number | null | undefined, unit: string): string => {
  if (v == null || !Number.isFinite(v)) return '–';
  if (unit === '×' || unit === 'γ') return `${v.toFixed(2)}${unit}`;
  if (unit === 'K') return `${v > 0 ? '+' : ''}${Math.round(v)} K`;
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${unit}`;
};
const scoreLabel = (): string => (S.run?.score_key === 'map50' ? (S.run.labelled ? 'mAP50' : 'consistency') : 'confidence');
const ax = (): Partial<PL.LayoutAxis> => ({ gridcolor: THEME.grid, linecolor: THEME.border, zerolinecolor: THEME.border, tickfont: { color: THEME.muted, size: 9 } });

/** Render the controls into `el` and bind them (once). */
export async function initStress(el: HTMLElement, host: StressHost): Promise<void> {
  S.host = host;
  el.innerHTML = `<p class="muted hint">Runs a model on ${esc(host.memberA())}'s images made darker, brighter, blurrier, noisier,
      warmer / cooler, more compressed and smaller, step by step, and plots its score at every step. With an Image conditions
      report on screen, ${esc(host.aliasB())}'s measured light / blur / colour is marked on each axis: the expected score on B before B is labelled.</p>
    <div class="shf-run stress-run">
      <label class="m-ctl"><span class="m-lbl">model</span><select id="str-model" aria-label="Model"><option value="">loading…</option></select></label>
      <label class="m-ctl"><span class="m-lbl" title="Images of A the model runs on at every step (labelled ones when A has labels)">images</span>
        <select id="str-n"><option value="50">50</option><option value="150" selected>150</option><option value="500">500</option></select></label>
      <button type="button" id="str-go" class="primary">${icon('play')}Run stress test</button>
    </div>
    <div id="str-axes" class="str-axes" role="group" aria-label="Axes"></div>
    <div id="str-status" class="cmp-status muted hint" role="status" aria-live="polite"></div>
    <div id="str-body"></div>`;
  $req('str-go').onclick = () => { void runStress(); };
  el.addEventListener('change', onChange);
  el.addEventListener('click', onClick);
  window.addEventListener('theme-changed', () => { if (S.run) renderChart(); });
  try {
    const [m, a] = await Promise.all([getJSON<ModelsPayload>('api/models'),
      getJSON<{ axes: StressAxisInfo[]; sample: number; max: number }>('api/compare/stress/axes')]);
    S.models = m.models.filter((x) => x.family !== 'hf');
    S.axes = a.axes;
    S.pick = new Set(a.axes.map((x) => x.key));
    S.model = S.models[0]?.id ?? '';
  } catch (e) {
    $req('str-status').innerHTML = `<span class="txt-danger">${esc(errText(e))}</span>`;
  }
  renderControls();
}

function renderControls(): void {
  const sel = $<HTMLSelectElement>('str-model');
  if (sel) {
    sel.innerHTML = S.models.length ? S.models.map((m) => `<option value="${esc(m.id)}"${m.id === S.model ? ' selected' : ''}>${esc(m.name)}</option>`).join('')
      : '<option value="">no detection model: add one in Settings › Models</option>';
  }
  const go = $<HTMLButtonElement>('str-go');
  if (go) go.disabled = !S.models.length || S.running;
  const axes = $('str-axes');
  if (axes) {
    axes.innerHTML = S.axes.map((a) => `<label class="str-ax"><input type="checkbox" data-str-axis="${esc(a.key)}"${S.pick.has(a.key) ? ' checked' : ''}>${esc(a.label)}</label>`).join('');
  }
}

/** The Image conditions report changed (B's medians): re-read the summary of the run on screen. */
export function onReport(): void {
  if (S.run && !S.running) void runStress(true);
}

export const stressRun = (): { run: StressRun; summary: StressSummary } | null => (S.run && S.sum ? { run: S.run, summary: S.sum } : null);

async function runStress(quiet = false): Promise<void> {
  const h = S.host;
  if (!h || S.running || !S.model) return;
  if (!S.pick.size) { toast('Pick at least one axis', 'info'); return; }
  const seq = ++S.seq;
  const body = { member: h.memberA(), model: S.model, n: S.n, axes: [...S.pick], key: h.reportKey() };
  S.running = true;
  renderControls();
  const st = $req('str-status');
  if (!quiet) st.textContent = `Running ${S.models.find((m) => m.id === S.model)?.name ?? 'the model'} on ${S.n} images of ${body.member} at every step…`;
  try {
    let r = await postJSON<StressResult>('api/compare/stress', body);
    for (let tries = 0; !r.ready && tries < 3; tries++) {
      await waitJob(r.job_id, (j: Job) => showWait(j));
      r = await postJSON<StressResult>('api/compare/stress', body);
    }
    hideWait();
    if (seq !== S.seq) return;
    if (!r.ready) throw new Error('the stress test was cancelled');
    S.run = r.result;
    S.sum = r.summary;
    S.strip = '';
    st.textContent = `${S.run.model.name} on ${S.run.n} images of ${S.run.member}`
      + `${S.run.labelled ? ` (${S.run.n_labels.toLocaleString()} labels)` : ' (no labels: scored against its own clean predictions)'} · conf ≥ ${S.run.conf}.`;
    render();
  } catch (e) {
    hideWait();
    if (seq === S.seq) {
      st.innerHTML = `<span class="txt-danger">${icon('alert')}${esc(errText(e))}</span>`;
      toast(errText(e), 'err');
    }
  } finally {
    S.running = false;
    renderControls();
  }
}

function render(): void {
  const r = S.run, s = S.sum, h = S.host;
  if (!r || !s || !h) return;
  const lab = scoreLabel();
  const hd = s.headline;
  const head = hd
    ? `<div class="shf-verdict" data-level="${hd.status === 'ok' ? 'fits' : hd.status === 'moderate' ? 'check' : 'shifted'}">${icon(hd.status === 'ok' ? 'check-c' : 'alert')}<div>
        <b>Expected ${lab} on ${esc(h.aliasB())} ≈ ${num(hd.expected)} (clean ${num(hd.clean)}, ${hd.drop > 0 ? '−' : '+'}${pct(Math.abs(hd.drop))})</b>
        <p>${hd.status === 'ok' ? `${esc(h.aliasB())} sits inside the safe window on every axis tested.` : `Limited by ${esc(hd.label)}: ${esc(h.aliasB())} sits where the model has already lost ${pct(hd.drop)}.`}
        The other changes of B (objects, backgrounds) are not in this number: validate on a few labelled B images.</p></div></div>`
    : `<p class="muted hint">Clean ${lab} ${num(s.clean)}. ${h.reportKey() ? '' : 'Analyse conditions above to mark where B sits on each axis.'}</p>`;
  const rows = s.axes.map((a) => {
    const w = `${a.open[0] ? '≤ ' : ''}${lvl(a.window[0], a.unit)} … ${a.open[1] ? '≥ ' : ''}${lvl(a.window[1], a.unit)}`;
    const b = a.b ? `${lvl(a.b.level, a.unit)}${a.b.beyond ? ' <span class="muted" title="beyond the tested range: the score there is extrapolated from the last step">beyond</span>' : ''}` : '–';
    return `<tr data-str-strip="${esc(a.key)}" tabindex="0" title="Show one image at every step"><td>${esc(a.label)}</td><td class="num">${w}</td>
      <td class="r num">${num(a.worst)}</td><td class="r num">${b}</td><td class="r num">${num(a.expected)}</td>
      <td>${a.status ? chip(STATUS_SEV[a.status] ?? 'n/a', a.status === 'ok' ? 'ok' : `−${pct(a.drop)}`) : ''}</td></tr>`;
  }).join('');
  $req('str-body').innerHTML = `${head}
    <div id="shf-stress-chart" class="cmp-chart str-chart"></div>
    <p class="muted hint">Line: ${lab} at each step · dashed: clean · green band: the safe window (≥ 95% of clean) · grey band: ${esc(r.member)}'s own
      p5–p95 · <span class="str-b" style="--c:${originColor(h.colorB())}">▲</span> ${esc(h.aliasB())} and the score expected there.</p>
    <div class="cmp-table-wrap"><table class="class-table shf-table str-table"><thead><tr><th>change</th><th>safe window</th>
      <th class="r" title="the lowest score over the steps tested">worst</th><th class="r">${esc(h.aliasB())} at</th><th class="r">expected</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div id="str-strip" class="shf-preview str-strip"></div>
    ${s.advice.length ? `<ul class="shf-recs">${s.advice.map((x) => `<li>${chip(x.severity)}<span>${esc(x.text)}</span></li>`).join('')}</ul>` : ''}
    <div class="shf-actions"><button type="button" class="ghost" data-str-export>${icon('download')}CSV</button></div>`;
  renderChart();
  if (S.strip) renderStrip(S.strip);
}

function renderChart(): void {
  const r = S.run, s = S.sum, h = S.host, el = $('shf-stress-chart');
  if (!r || !s || !h || !el) return;
  const sk = r.score_key;
  const n = r.axes.length, cols = n > 1 ? 2 : 1, rows = Math.ceil(n / cols);
  const gx = 0.1, gy = 0.55 / Math.max(rows, 1);
  const cw = (1 - gx * (cols - 1)) / cols, rh = (1 - gy * (rows - 1)) / rows;
  el.style.height = `${Math.max(220, rows * 150)}px`;
  const traces: Array<Partial<PL.PlotData>> = [];
  const shapes: Array<Partial<PL.Shape>> = [];
  const ann: Array<Partial<PL.Annotations>> = [];
  const lay: Record<string, unknown> = {};
  const cA = originColor(h.colorA()), cB = originColor(h.colorB());
  const okC = getComputedStyle(document.documentElement).getPropertyValue('--ok').trim() || '#3fb950';
  r.axes.forEach((a, i) => {
    const k = i + 1, xa = k === 1 ? 'x' : `x${k}`, ya = k === 1 ? 'y' : `y${k}`;
    const c = i % cols, row = Math.floor(i / cols);
    const x0 = c * (cw + gx), y1 = 1 - row * (rh + gy);
    lay[`xaxis${k === 1 ? '' : k}`] = { ...ax(), domain: [x0, x0 + cw], anchor: ya, type: a.key === 'jpeg' ? 'log' : 'linear' };
    lay[`yaxis${k === 1 ? '' : k}`] = { ...ax(), domain: [y1 - rh, y1], anchor: xa, rangemode: 'tozero' };
    const xs = a.rows.map((x) => x.level), ys = a.rows.map((x) => (x[sk] ?? null) as number | null);
    traces.push({ type: 'scatter', mode: 'lines+markers', x: xs, y: ys, xaxis: xa, yaxis: ya, name: a.label,
      line: { color: cA, width: 2 }, marker: { size: 5, color: cA }, customdata: xs.map(() => a.key),
      hovertemplate: `${esc(a.label)} %{x}: ${scoreLabel()} %{y:.3f}<extra></extra>` });
    const sm = s.axes.find((x) => x.key === a.key);
    shapes.push({ type: 'line', xref: `${xa} domain` as PL.XAxisName, yref: ya as PL.YAxisName, x0: 0, x1: 1, y0: s.clean, y1: s.clean,
      line: { color: THEME.muted, width: 1, dash: 'dash' } });
    if (sm) {
      shapes.push({ type: 'rect', xref: xa as PL.XAxisName, yref: `${ya} domain` as PL.YAxisName, x0: sm.window[0], x1: sm.window[1], y0: 0, y1: 1,
        fillcolor: `${okC}1c`, line: { width: 0 }, layer: 'below' });
      const bb = sm.b;
      if (bb?.band) {
        shapes.push({ type: 'rect', xref: xa as PL.XAxisName, yref: `${ya} domain` as PL.YAxisName, x0: bb.band[0], x1: bb.band[1], y0: 0, y1: 0.06,
          fillcolor: `${THEME.muted}55`, line: { width: 0 } });
      }
      if (bb) {
        shapes.push({ type: 'line', xref: xa as PL.XAxisName, yref: `${ya} domain` as PL.YAxisName, x0: bb.level, x1: bb.level, y0: 0, y1: 1,
          line: { color: cB, width: 1.5, dash: 'dot' } });
        if (sm.expected != null) {
          traces.push({ type: 'scatter', mode: 'markers', x: [bb.level], y: [sm.expected], xaxis: xa, yaxis: ya, name: h.aliasB(),
            marker: { symbol: 'triangle-up', size: 10, color: cB, line: { color: THEME.surface, width: 1 } },
            hovertemplate: `${esc(h.aliasB())} at ${lvl(bb.level, a.unit)}: expected ${num(sm.expected, 3)}<extra></extra>` });
        }
      }
    }
    ann.push({ text: `${esc(a.label)} <span style="color:${THEME.muted}">(${esc(a.unit)})</span>`, xref: `${xa} domain` as PL.XAxisName,
      yref: `${ya} domain` as PL.YAxisName, x: 0, y: 1.02, xanchor: 'left', yanchor: 'bottom', showarrow: false, font: { size: 10, color: THEME.text } });
  });
  const layout: Partial<PL.Layout> = { ...plotBase(THEME.surface, 10), showlegend: false, margin: { t: 18, b: 22, l: 34, r: 8 },
    shapes, annotations: ann, ...lay } as Partial<PL.Layout>;
  void Plotly.react(el, traces, layout, { ...cfg, displayModeBar: false }).then(() => {
    const gd = el as unknown as PL.PlotlyHTMLElement;
    gd.removeAllListeners?.('plotly_click');
    gd.on('plotly_click', (ev: PL.PlotMouseEvent) => {
      const key = ev.points[0]?.customdata as unknown as string | undefined;
      if (key) { S.strip = key; renderStrip(key); }
    });
  });
}

/** One image of the run at every step of `axis` (api/compare/stress/preview). */
function renderStrip(axis: string): void {
  const r = S.run, box = $('str-strip');
  if (!r || !box) return;
  const a = r.axes.find((x) => x.key === axis);
  const name = r.names[0];
  if (!a || !name) { box.innerHTML = ''; return; }
  const sk = r.score_key;
  box.innerHTML = a.rows.map((x) => {
    const q = new URLSearchParams({ name, axis, level: String(x.level) });
    return `<figure${x.level === a.neutral ? ' class="str-clean"' : ''}><img src="api/compare/stress/preview?${q.toString()}" alt="${esc(a.label)} ${x.level}" loading="lazy">
      <figcaption>${esc(lvl(x.level, a.unit))} · ${num(x[sk] ?? null)}</figcaption></figure>`;
  }).join('');
  document.querySelectorAll<HTMLElement>('[data-str-strip]').forEach((t) => t.classList.toggle('sel', t.dataset['strStrip'] === axis));
}

/** The run as CSV: one row per axis step. */
export function stressCsv(r: StressRun, s: StressSummary | null): string {
  const head = ['axis', 'level', 'unit', 'map50', 'map', 'p', 'r', 'mean_top', 'none_found', 'per_image', 'window_lo', 'window_hi', 'b_level', 'b_expected'];
  const rows = r.axes.flatMap((a) => {
    const sm = s?.axes.find((x) => x.key === a.key);
    return a.rows.map((x) => [a.key, x.level, a.unit, x.map50, x.map, x.p, x.r, x.mean_top, x.none_found, x.per_image,
      sm?.window[0], sm?.window[1], sm?.b?.level, sm?.expected].map((v) => (v == null ? '' : String(v))).join(','));
  });
  return [head.join(','), ...rows].join('\n') + '\n';
}

function onChange(e: Event): void {
  const t = e.target;
  if (!(t instanceof HTMLInputElement || t instanceof HTMLSelectElement)) return;
  if (t.id === 'str-model') S.model = t.value;
  else if (t.id === 'str-n') S.n = +t.value;
  else if (t instanceof HTMLInputElement && t.dataset['strAxis']) {
    if (t.checked) S.pick.add(t.dataset['strAxis']); else S.pick.delete(t.dataset['strAxis']);
  }
}

function onClick(e: Event): void {
  const t = closestOf(e, '[data-str-strip], [data-str-export]');
  if (!t) return;
  if (t.dataset['strStrip']) { S.strip = t.dataset['strStrip']; renderStrip(S.strip); return; }
  if (t.dataset['strExport'] !== undefined && S.run) {
    saveFile(stressCsv(S.run, S.sum), `stress-${S.run.model.name}-${S.run.member}.csv`, 'text/csv');
  }
}
