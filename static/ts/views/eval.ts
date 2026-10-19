// Evaluation tab: every model that ran on this dataset side by side, one model explained with the dataset
// (class balance, train/val gap, sessions, calibration, box fit, kNN agreement, box sizes, worst images), a
// ranked list of plain-language findings, and a report / CSV / JSON export. Data: GET api/eval/overview.

import { errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { setBar, waitJob } from '../core/jobs.js';
import { S } from '../core/state.js';
import { clsColor, THEME } from '../core/theme.js';
import { openSettings } from '../settings/dialog.js';
import type {
  Compare, CompareKey, EvalOverview, Finding, FindingAction, Insights, KnnCell, LbKey, LbRow, Leaderboard, ModelInfo, ModelsPayload, PredictResult,
} from '../types-views.js';
import { askAI } from './assistant.js';
import { at, ax, base, cfg, customOf, withNested } from './chart-util.js';
import { closestOf } from './ev-util.js';
import { setSelection } from './gallery.js';
import { curTab, switchTab } from './layout.js';
import { openModal, openPredModal } from './modal.js';
import { modelsChanged, openModelClass, useModel } from './model.js';

// Plotly is loaded as a classic script by main.ts (window.Plotly)
declare const Plotly: typeof import('plotly.js');

export const EVAL_PLOTS = ['e-cmp', 'e-speed', 'e-heat', 'e-balance', 'e-gap', 'e-sess', 'e-calib', 'e-loc'];

interface EvalState {
  cmpSeq: number; split: string; mode: string; conf: number; focus: string | null; data: EvalOverview | null;
  cmp: (Compare & { key: string }) | null; cmpA: string | null; cmpB: string | null; models: ModelInfo[];
  running: Set<string>; sortKey: LbCol; seq: number;
}
const E: EvalState = { cmpSeq: 0, split: 'val', mode: 'best', conf: 0.25, focus: null, data: null, cmp: null, cmpA: null, cmpB: null, models: [], running: new Set(), sortKey: 'map', seq: 0 };
let drawn = false, bound = false;

export function invalidateEval(): void { drawn = false; }

const pct = (v: number | null | undefined, d = 1): string => (v == null ? '–' : (v * 100).toFixed(d));
const SEV: Record<Finding['severity'], [string, string]> = { 3: ['act on it', 'danger'], 2: ['worth a look', 'warn'], 1: ['good to know', 'info'] };

// ---------------------------------------------------------------- data
export async function showEvalTab(): Promise<void> {
  bindEval();
  if (E.data && drawn) { renderEval(); return; }
  await refresh();
}

async function refresh(): Promise<void> {
  const seq = ++E.seq;
  $req('e-sub').textContent = 'scoring…';
  try {
    const [d, ml] = await Promise.all([
      getJSON<EvalOverview>(`api/eval/overview?split=${E.split}&conf_mode=${E.mode}&conf=${E.conf}${E.focus ? `&focus=${encodeURIComponent(E.focus)}` : ''}`),
      getJSON<ModelsPayload>('api/models')]);
    if (seq !== E.seq) return;
    E.data = d;
    E.models = ml.models;
    E.focus = d.focus;
    drawn = false;
    renderEval();
  } catch (e) {
    if (seq !== E.seq) return;
    renderEmpty(`Could not evaluate: ${errText(e)}`);
  }
}

async function runModels(ids: string[]): Promise<void> {
  for (const id of ids) {
    if (E.running.has(id)) continue;
    E.running.add(id);
    renderModelList();
    try {
      const r = await postJSON<PredictResult>(`api/models/${encodeURIComponent(id)}/predict`, {});
      if (!r.ready) {
        await waitJob(r.job_id, (j) => {
          const box = document.querySelector(`#e-models [data-mid="${id}"] .m-prog`);
          if (!box) return;
          const fill = box.querySelector<HTMLElement>('.bar > div');
          if (fill) setBar(fill, j);
          const num = box.querySelector('.num');
          if (num) num.textContent = j.total ? `${j.done}/${j.total}` : j.stage;
        });
      }
    } catch (e) { alert(`${id}: ${errText(e)}`); }
    E.running.delete(id);
  }
  void modelsChanged();
  await refresh();
}

// ---------------------------------------------------------------- left panel
function renderEmpty(msg?: string): void {
  $req('e-grid').classList.add('hidden');
  $req('e-empty').classList.remove('hidden');
  const n = E.models.length;
  $req('e-empty').innerHTML = msg ? `${icon('alert')}<h3>Nothing to show</h3><p>${esc(msg)}</p>` : !n
    ? `${icon('eval')}<h3>No models yet</h3><p>Add one or more YOLO, RF-DETR or custom models (or an API) to compare them on this dataset and see what limits them.</p>` +
      `<button class="primary" data-eact="add">${icon('plus')}Add a model</button>`
    : `${icon('eval')}<h3>No model has run on this dataset yet</h3><p>Running a model predicts every image once (cached). Then the comparison and every chart here are instant.</p>` +
      `<button class="primary" data-eact="run-all">${icon('play')}Run all ${n} model${n === 1 ? '' : 's'}</button>`;
  renderModelList();
}

function renderModelList(): void {
  const lb: Pick<Leaderboard, 'rows' | 'best'> = E.data ? E.data.leaderboard : { rows: [], best: null };
  const done = new Set(lb.rows.map((r) => r.id));
  const notRun = E.models.filter((m) => !done.has(m.id));
  $req('e-run-all').classList.toggle('hidden', !notRun.length);
  $req('e-run-all').textContent = `Run ${notRun.length} not run`;
  $req('e-models').innerHTML = E.models.map((m) => {
    const row = lb.rows.find((r) => r.id === m.id);
    const run = E.running.has(m.id);
    return `<div class="e-model${m.id === E.focus ? ' focus' : ''}" data-mid="${esc(m.id)}">` +
      `<div class="e-mname">${m.id === lb.best ? `<span class="tagc ok">${icon('check')}best</span>` : ''}<b>${esc(m.name)}</b>` +
      `<span class="muted">${esc(m.family)} · ${esc(m.format)}</span></div>` +
      (row ? `<span class="num">${pct(row.map)}<span class="muted"> mAP</span></span>` : run
        ? '<div class="m-prog"><div class="bar"><div></div></div><span class="num muted"></span></div>'
        : `<button class="ghost" data-eact="run" title="Predict every image of this dataset">${icon('play')}Run</button>`) + '</div>';
  }).join('') || '<p class="muted hint">No models. <button class="ghost" data-eact="add">Add one</button></p>';
  $req('e-focus').innerHTML = lb.rows.map((r) => `<option value="${esc(r.id)}" ${r.id === E.focus ? 'selected' : ''}>${esc(r.name)}</option>`).join('');
}

// ---------------------------------------------------------------- right panel
export function renderEval(): void {
  if (curTab !== 'eval') return;
  const d = E.data;
  if (!d) return;
  const lb = d.leaderboard, ins = d.insights;
  const r0 = lb.rows[0];
  if (!r0) { renderEmpty(); return; }
  $req('e-empty').classList.add('hidden');
  $req('e-grid').classList.remove('hidden');
  $req('e-sub').textContent = `${lb.split} · ${r0.images} images · ${r0.labels.toLocaleString()} labels`;
  document.querySelectorAll<HTMLElement>('#e-split .view-btn').forEach((b) => { const on = b.dataset.split === lb.split; b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on)); });
  document.querySelectorAll<HTMLElement>('#e-mode .view-btn').forEach((b) => { const on = b.dataset.mode === E.mode; b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on)); });
  $req('e-conf-row').classList.toggle('hidden', E.mode !== 'fixed');
  renderModelList();
  renderFindings();
  renderLeaderboard();
  void loadCompare();
  if (!drawn) { renderCharts(lb, ins); drawn = true; bindChartClicks(); }
  else EVAL_PLOTS.forEach((id) => { if ($<Plotly.PlotlyHTMLElement>(id)?.data) Plotly.Plots.resize(id); });
}

function renderFindings(): void {
  const d = E.data;
  if (!d) return;
  const f = d.findings;
  $req('e-find-sub').textContent = `· ${f.length} finding${f.length === 1 ? '' : 's'} for ${d.insights ? d.insights.model.name : 'these models'}`;
  $req('e-findings').innerHTML = f.map((x, i) => `<button class="finding sev-${SEV[x.severity][1]}" data-f="${i}" ${x.action ? '' : 'disabled'}>` +
    `<span class="sev">${SEV[x.severity][0]}${x.tag ? ` · ${esc(x.tag)}` : ''}</span><b>${esc(x.title)}</b><span class="d">${esc(x.detail)}</span>` +
    (x.action ? `<span class="go">${icon('arrow-r')}</span>` : '') + '</button>').join('') || '<p class="muted hint">Nothing stands out: the model and the labels agree well.</p>';
}

/** A leaderboard column: row field, header, and which direction is better (1 high, -1 low, 0 / null none). */
type LbCol = 'name' | LbKey;
const LB_COLS: ReadonlyArray<readonly [LbCol, string, 1 | 0 | -1 | null]> = [
  ['name', 'model', null], ['map', 'mAP50-95', 1], ['map50', 'mAP50', 1], ['p', 'P', 1], ['r', 'R', 1], ['f1', 'F1', 1],
  ['conf', 'conf', 0], ['ms', 'ms/img', -1], ['size_mb', 'MB', -1], ['missed', 'missed', -1], ['wrong_cls', 'wrong cls', -1],
  ['fp_bg', 'FP', -1], ['fp_dup', 'dup', -1]];
const PCT_COLS: readonly LbCol[] = ['map', 'map50', 'p', 'r', 'f1'];

function renderLeaderboard(): void {
  const d = E.data;
  if (!d) return;
  const lb = d.leaderboard;
  const rows = lb.rows.slice().sort((a, b) => {
    const dir = (LB_COLS.find((c) => c[0] === E.sortKey) || [0, 0, 1])[2] || 1;
    const va = a[E.sortKey], vb = b[E.sortKey];
    if (typeof va === 'string') return va.localeCompare(String(vb));
    const na = typeof va === 'number' ? va : null, nb = typeof vb === 'number' ? vb : null;
    return dir > 0 ? (nb ?? -1) - (na ?? -1) : (na ?? 1e9) - (nb ?? 1e9);
  });
  const bestOf: Partial<Record<LbCol, number>> = {};
  for (const [k, , dir] of LB_COLS) {
    if (!dir) continue;
    const vals = lb.rows.map((r) => r[k]).filter((v): v is number => typeof v === 'number');
    if (vals.length > 1) bestOf[k] = dir > 0 ? Math.max(...vals) : Math.min(...vals);
  }
  const fmt = (k: LbCol, v: LbRow[LbCol]): string => {
    if (v == null) return '–';
    if (typeof v !== 'number') return esc(v);
    return PCT_COLS.includes(k) ? pct(v) : k === 'conf' ? v.toFixed(2) : k === 'ms' ? v.toFixed(1) : v.toLocaleString();
  };
  $req('e-lb-sub').textContent = `· ${lb.split} images · ${E.mode === 'best' ? 'each at its best-F1 confidence' : `all at confidence ${E.conf}`} · click a header to sort`;
  $req('e-lb').innerHTML = `<thead><tr>${LB_COLS.map(([k, l]) => `<th class="${k === 'name' ? '' : 'r'}${E.sortKey === k ? ' sorted' : ''}" data-sort="${k}" role="button" tabindex="0">${l}</th>`).join('')}</tr></thead><tbody>` +
    rows.map((r) => `<tr data-mid="${esc(r.id)}" tabindex="0" role="button" title="explain this model below">` + LB_COLS.map(([k]) => {
      if (k === 'name') {
        return `<td><span class="cname">${r.id === lb.best ? `<span class="tagc ok">${icon('check')}best</span>` : ''}` +
          `${r.id === lb.fastest_near_best && r.id !== lb.best ? '<span class="tagc on">fast</span>' : ''}${esc(r.name)}` +
          `<span class="muted">${esc(r.family)} ${esc(r.format)}</span></span></td>`;
      }
      const best = bestOf[k];
      return `<td class="r${best != null && r[k] === best ? ' top' : ''}">${fmt(k, r[k])}</td>`;
    }).join('') + '</tr>').join('') + '</tbody>';
}

const padRange = (v: number[]): [number, number] => { const lo = Math.min(...v), hi = Math.max(...v), p = Math.max(0.01, (hi - lo) * 0.25); return [lo - p, hi + p]; };

function renderCharts(lb: Leaderboard, ins: Insights | null): void {
  const classes = S.meta?.classes ?? [];
  const rows = lb.rows;
  // speed vs accuracy
  const maxMb = Math.max(1, ...rows.map((r) => r.size_mb || 1));
  void Plotly.react('e-speed', [{
    type: 'scatter', mode: 'text+markers', x: rows.map((r) => r.ms), y: rows.map((r) => r.map), text: rows.map((r) => r.name),
    textposition: 'top center', textfont: { color: THEME.text, size: 11 }, customdata: rows.map((r) => [r.map50, r.size_mb]),
    marker: { size: rows.map((r) => 12 + 30 * Math.sqrt((r.size_mb || 1) / maxMb)), color: rows.map((r) => (r.id === lb.best ? THEME.errTp : THEME.accent)),
      opacity: 0.75, line: { color: THEME.surface, width: 1 } }, cliponaxis: false,
    hovertemplate: '<b>%{text}</b><br>mAP50-95 %{y:.3f} · mAP50 %{customdata[0]:.3f}<br>%{x:.1f} ms/img · %{customdata[1]} MB<extra></extra>',
  }], { ...base(), xaxis: { ...ax(), title: { text: 'ms per image (lower = faster)' }, rangemode: 'tozero' },
    yaxis: { ...ax(), title: { text: 'mAP50-95' }, range: padRange(rows.map((r) => r.map)) }, margin: { t: 30, b: 50, l: 60, r: 30 }, showlegend: false }, cfg);

  // AP50 per class × model
  const pc = lb.per_class;
  const present = classes.map((_, c) => c).filter((c) => pc.ap50.some((r) => r[c] != null));
  const z = pc.ap50.map((r) => present.map((c) => r[c] ?? null));
  const names = pc.models.map((id) => rows.find((r) => r.id === id)?.name ?? id);
  const heat: Partial<Plotly.PlotData> = {
    type: 'heatmap', z, x: present.map((c) => classes[c] ?? ''), y: names,
    colorscale: [[0, THEME.errMiss], [0.5, THEME.errLoc], [1, THEME.errTp]],
    zmin: Math.max(0, Math.min(0.9, ...z.flat().filter((v): v is number => v != null)) - 0.02), zmax: 1,
    texttemplate: '%{text}', textfont: { size: 10 },
    colorbar: { thickness: 10, outlinewidth: 0, tickformat: '.0%', tickfont: { color: THEME.muted } },
    hovertemplate: '<b>%{y}</b><br>%{x}: AP50 %{z:.3f}<extra></extra>',
  };
  void Plotly.react('e-heat', [withNested(heat, {
    text: z.map((r) => r.map((v) => (v == null ? '' : (v * 100).toFixed(0)))),
    customdata: pc.models.map((id) => present.map((c) => [id, c])),
  })],
    { ...base(), xaxis: { ...ax(), tickangle: -40 }, yaxis: { ...ax(), automargin: true }, margin: { t: 10, b: 110, l: 10, r: 10 } }, cfg);

  if (!ins) return;
  // training labels vs AP50
  const pcl = ins.per_class.filter((c) => c.n);
  const tl = ins.balance.train_labels;
  const aps = pcl.map((c) => c.ap50 ?? 0).sort((a, b) => a - b);
  const apCut = Math.min(0.985, aps[Math.floor(aps.length / 3)] ?? 1);
  void Plotly.react('e-balance', [{
    type: 'scatter', mode: 'text+markers', x: pcl.map((c) => Math.max(1, tl[c.cls] ?? 1)), y: pcl.map((c) => c.ap50),
    text: pcl.map((c) => ((c.ap50 ?? 0) < apCut ? classes[c.cls] ?? '' : '')), hovertext: pcl.map((c) => classes[c.cls] ?? ''),
    textposition: 'top center', textfont: { size: 10, color: THEME.muted }, customdata: pcl.map((c) => [c.cls, c.r, c.n]),
    marker: { size: 11, color: pcl.map((c) => clsColor(c.cls)), line: { color: THEME.surface, width: 1 } },
    hovertemplate: '<b>%{hovertext}</b><br>%{x} training labels<br>AP50 %{y:.3f} · recall %{customdata[1]:.1%} (%{customdata[2]} labels here)<br>click: its mistakes<extra></extra>',
  }], { ...base(), xaxis: { ...ax(), type: 'log', title: { text: ins.balance.from_train_split ? 'labels in the training split (log)' : 'labels (log)' } },
    yaxis: { ...ax(), title: { text: `AP50 (${ins.split})` } }, margin: { t: 20, b: 50, l: 60, r: 20 }, showlegend: false }, cfg);
  const corr = ins.balance.corr_log_count_ap50;
  $req('e-bal-sub').textContent = corr == null ? '' : `· correlation ${corr.toFixed(2)}${corr > 0.4 ? ': more data = better here' : corr < -0.2 ? ': count is not what limits it' : ''}`;

  // train vs val (dumbbell)
  if (ins.gap) {
    const g = ins.gap;
    const gapOf = (c: number): number => (g.train_ap50[c] ?? 0) - (g.val_ap50[c] ?? 0);
    const cs = classes.map((_, c) => c).filter((c) => g.n_val[c]).sort((a, b) => gapOf(b) - gapOf(a));
    const lines: Plotly.Data = { type: 'scatter', mode: 'lines', x: cs.flatMap((c) => [g.val_ap50[c] ?? null, g.train_ap50[c] ?? null, null]), y: cs.flatMap((c) => [classes[c] ?? '', classes[c] ?? '', null]),
      line: { color: THEME.border, width: 3 }, hoverinfo: 'skip', showlegend: false };
    void Plotly.react('e-gap', [lines,
      { type: 'scatter', mode: 'markers', name: 'val', x: cs.map((c) => g.val_ap50[c] ?? null), y: cs.map((c) => classes[c] ?? ''), marker: { size: 10, color: THEME.errCls }, customdata: cs, hovertemplate: '%{y}: val %{x:.3f}<extra></extra>' },
      { type: 'scatter', mode: 'markers', name: 'train', x: cs.map((c) => g.train_ap50[c] ?? null), y: cs.map((c) => classes[c] ?? ''), marker: { size: 10, color: THEME.accent }, customdata: cs, hovertemplate: '%{y}: train %{x:.3f}<extra></extra>' },
    ], { ...base(), xaxis: { ...ax(), title: { text: 'AP50' } }, yaxis: { ...ax(), autorange: 'reversed', automargin: true },
      legend: { orientation: 'h', y: 1.08, font: { color: THEME.text } }, margin: { t: 20, b: 50, l: 10, r: 20 } }, cfg);
    $req('e-gap-sub').textContent = `· mAP50-95 train ${g.map[0].toFixed(3)} vs val ${g.map[1].toFixed(3)}`;
  } else {
    Plotly.purge('e-gap');
    $req('e-gap').innerHTML = '<p class="muted hint">Needs both train and val images (config models.split_source).</p>';
  }

  // sessions
  const ss = ins.sessions;
  void Plotly.react('e-sess', [
    { type: 'bar', name: 'recall', x: ss.map((s) => s.session), y: ss.map((s) => s.recall), marker: { color: THEME.errTp }, customdata: ss.map((s, i) => [i, s.labels, s.images]),
      hovertemplate: '%{x}: recall %{y:.1%}<br>%{customdata[1]} labels in %{customdata[2]} images<extra></extra>' },
    { type: 'bar', name: 'precision', x: ss.map((s) => s.session), y: ss.map((s) => s.precision), marker: { color: THEME.accent }, customdata: ss.map((s, i) => [i, s.labels, s.images]),
      hovertemplate: '%{x}: precision %{y:.1%}<extra></extra>' },
  ], { ...base(), barmode: 'group', yaxis: { ...ax(), tickformat: '.0%', range: [Math.max(0, Math.min(...ss.flatMap((s) => [s.recall ?? 1, s.precision ?? 1])) - 0.05), 1.01] },
    xaxis: { ...ax(), tickangle: -30 }, legend: { orientation: 'h', y: 1.08, font: { color: THEME.text } }, margin: { t: 20, b: 80, l: 50, r: 10 } }, cfg);

  // calibration (reliability diagram)
  const cal = ins.calibration;
  const cpts: Array<{ c: number; p: number; n: number }> = [];
  cal.conf.forEach((c, i) => { const p = cal.precision[i]; if (p != null) cpts.push({ c, p, n: cal.count[i] ?? 0 }); });
  void Plotly.react('e-calib', [
    { type: 'scatter', mode: 'lines', x: [0, 1], y: [0, 1], name: 'perfect', line: { color: THEME.muted, dash: 'dot', width: 1 }, hoverinfo: 'skip' },
    { type: 'scatter', mode: 'lines+markers', name: 'model', x: cpts.map((p) => p.c), y: cpts.map((p) => p.p), customdata: cpts.map((p) => p.n),
      marker: { size: cpts.map((p) => 5 + 3 * Math.log10(p.n + 1)), color: THEME.accent }, line: { color: THEME.accent, width: 2 },
      hovertemplate: 'score ≈ %{x:.2f}: %{y:.1%} right (%{customdata} predictions)<extra></extra>' },
  ], { ...base(), xaxis: { ...ax(), title: { text: 'confidence score' }, range: [0, 1] }, yaxis: { ...ax(), title: { text: 'share that is right' }, range: [0, 1.02], tickformat: '.0%' },
    legend: { orientation: 'h', y: 1.08, font: { color: THEME.text } }, margin: { t: 20, b: 50, l: 60, r: 10 } }, cfg);
  $req('e-cal-sub').textContent = cal.ece == null ? '' : `· calibration error ${(cal.ece * 100).toFixed(1)}%${(cal.bias ?? 0) > 0.05 ? ' · over-confident' : (cal.bias ?? 0) < -0.05 ? ' · under-confident' : ''}`;

  // box fit
  const loc = ins.localization.per_class.filter((c) => c.n);
  void Plotly.react('e-loc', [{
    type: 'bar', orientation: 'h', x: loc.map((c) => c.mean), y: loc.map((c) => classes[c.cls] ?? ''), customdata: loc.map((c) => [c.p10, c.loose, c.n]),
    marker: { color: loc.map((c) => ((c.mean ?? 0) < 0.8 ? THEME.errMiss : (c.mean ?? 0) < 0.88 ? THEME.errLoc : THEME.errTp)) },
    hovertemplate: '%{y}: mean IoU %{x:.3f}<br>worst 10% below %{customdata[0]:.2f} · %{customdata[1]:.0%} under 0.75<br>%{customdata[2]} found<extra></extra>',
  }], { ...base(), xaxis: { ...ax(), title: { text: 'mean IoU with the label' }, range: [Math.min(0.75, ...loc.map((c) => c.mean ?? 0)) - 0.02, 1] },
    yaxis: { ...ax(), autorange: 'reversed', automargin: true }, margin: { t: 10, b: 50, l: 10, r: 10 } }, cfg);
  $req('e-loc-sub').textContent = ins.localization.mean ? `· mean ${ins.localization.mean.toFixed(3)}` : '';

  // model vs kNN
  const k = ins.knn;
  if (k) {
    const cell = (key: KnnCell, title: string, cls: string): string => `<button class="knn-cell ${cls}" data-knn="${key}"><b>${k[key].n.toLocaleString()}</b><span>${title}</span></button>`;
    $req('e-knn').innerHTML = '<span></span><span class="h">neighbours: label looks wrong</span><span class="h">neighbours: label fine</span>' +
      `<span class="h v">model: wrong</span>${cell('both', 'both disagree: relabel these first', 'hot')}${cell('model_only', 'only the model', 'warm')}` +
      `<span class="h v">model: right</span>${cell('knn_only', 'only the neighbours', 'warm')}${cell('neither', 'both fine', 'ok')}`;
    $req('e-knn-sub').textContent = `· features: ${k.feature}`;
  } else {
    $req('e-knn').innerHTML = '<p class="muted hint">Compute a feature space in Explore first (the kNN label check needs it).</p>';
  }
  // box sizes
  $req('e-sizes').innerHTML = ins.sizes.map((s) => `<button class="size-row" data-size="${s.size}" ${s.labels ? '' : 'disabled'}>` +
    `<span class="lbl">${s.size} <span class="muted">${s.size === 'small' ? '< 32²' : s.size === 'medium' ? '32²–96²' : '> 96²'} px · ${s.labels}</span></span>` +
    `<span class="track"><span style="width:${s.recall == null ? 0 : (s.recall * 100).toFixed(1)}%;background:${s.recall == null ? 'transparent' : s.recall < 0.9 ? THEME.errMiss : s.recall < 0.97 ? THEME.errLoc : THEME.errTp}"></span></span>` +
    `<span class="num">${s.recall == null ? 'none' : pct(s.recall)}</span></button>`).join('');
  // worst images
  $req('e-worst').innerHTML = ins.worst.map((w, i) => `<button class="issue worst" data-w="${i}" title="${esc(w.image)}">` +
    `<img loading="lazy" src="${w.sid != null ? `api/crop/${w.focus ?? w.sid}` : `api/models/${encodeURIComponent(ins.model.id)}/pred/${w.pid}/crop`}" alt="">` +
    `<span class="k" style="--c:${THEME.errMiss}">${w.errors} mistake${w.errors === 1 ? '' : 's'}</span>` +
    `<span class="d">${w.missed} label${w.missed === 1 ? '' : 's'} wrong · ${w.fp} FP · ${w.labels} labels<br>${esc(w.image)}</span></button>`).join('') ||
    '<p class="muted hint">No image has a mistake at this threshold.</p>';
}

// ---------------------------------------------------------------- model A vs B, label by label
async function loadCompare(): Promise<void> {
  const d = E.data;
  if (!d) return;
  const rows = d.leaderboard.rows;
  $req('e-cmp-card').classList.toggle('hidden', rows.length < 2);
  if (rows.length < 2) { E.cmp = null; return; }
  const ids = rows.map((r) => r.id);
  // A = the focus model, B = the best other one (rows come sorted by mAP)
  if (E.cmpA === null || !ids.includes(E.cmpA)) E.cmpA = E.focus && ids.includes(E.focus) ? E.focus : at(ids, 0);
  if (E.cmpB === null || !ids.includes(E.cmpB) || E.cmpB === E.cmpA) E.cmpB = ids.find((id) => id !== E.cmpA) ?? null;
  const opts = rows.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');
  $req('e-cmp-a').innerHTML = $req('e-cmp-b').innerHTML = opts;
  $req<HTMLSelectElement>('e-cmp-a').value = E.cmpA;
  $req<HTMLSelectElement>('e-cmp-b').value = E.cmpB ?? '';
  const key = [E.cmpA, E.cmpB, E.split, E.mode, E.conf].join('|');
  if (E.cmp && E.cmp.key === key) { d.compare = E.cmp; renderCompare(); return; }
  const seq = ++E.cmpSeq;
  $req('e-cmp-sub').textContent = 'scoring…';
  try {
    const c = await getJSON<Compare>(`api/eval/compare?a=${encodeURIComponent(E.cmpA)}&b=${encodeURIComponent(E.cmpB ?? '')}&split=${E.split}&conf_mode=${E.mode}&conf=${E.conf}`);
    if (seq !== E.cmpSeq) return;
    E.cmp = { key, ...c };
    if (E.data) E.data.compare = c;      // part of the JSON export
    renderCompare();
  } catch (e) {
    if (seq === E.cmpSeq) $req('e-cmp-sub').textContent = errText(e);
  }
}

function renderCompare(): void {
  const c = E.cmp;
  if (!c || curTab !== 'eval') return;
  const A = c.a.name, B = c.b.name, n = c.counts;
  const lostTxt = (l: Record<string, number>): string => Object.entries(l).filter(([, v]) => v).map(([k, v]) => `${v} ${k.replace('_cls', ' class')}`).join(', ');
  $req('e-cmp-sub').textContent = `${c.labels.toLocaleString()} labels · A ${pct(n.tp_a / c.labels)}% found · B ${pct(n.tp_b / c.labels)}% found` +
    (c.conf_mode === 'best' ? ` · each at its best-F1 conf (${c.conf_a.toFixed(2)} / ${c.conf_b.toFixed(2)})` : ` · conf ${c.conf_a.toFixed(2)}`);
  const chip = (k: CompareKey, label: string, color: string, title: string): string => `<span class="chip" role="button" tabindex="0" data-cmp="${k}" title="${esc(title)}">` +
    `<span class="dot" style="background:${color}"></span>${esc(label)} <span class="n">${n[k].toLocaleString()}</span></span>`;
  $req('e-cmp-chips').innerHTML =
    chip('only_a', `Only ${A} finds`, THEME.accent, `${B} on these: ${lostTxt(c.b_lost) || '–'}`) +
    chip('only_b', `Only ${B} finds`, THEME.errLoc, `${A} on these: ${lostTxt(c.a_lost) || '–'}`) +
    chip('both_miss', 'Both miss', THEME.errMiss, 'Neither model finds these labels: hard cases or label errors');

  // per class: AP50 difference, and how many labels each one alone finds
  const pc = c.per_class;
  const cls = pc.classes.map((_, i) => i).filter((i) => pc.labels[i] || pc.fp_a[i] || pc.fp_b[i]);
  const d = cls.map((i) => { const a = pc.ap50_a[i], b = pc.ap50_b[i]; return a == null || b == null ? null : a - b; });
  const x = cls.map((i) => pc.classes[i] ?? '');
  const cd = cls.map((i) => [pc.ap50_a[i] ?? null, pc.ap50_b[i] ?? null, pc.only_a[i] ?? null, pc.only_b[i] ?? null, pc.both_miss[i] ?? null, pc.fp_a[i] ?? null, pc.fp_b[i] ?? null, i]);
  void Plotly.react('e-cmp', [{
    type: 'bar', x, y: d, customdata: cd, name: 'AP50 A − B',
    marker: { color: d.map((v) => (v == null ? THEME.muted : v >= 0 ? THEME.accent : THEME.errLoc)) },
    hovertemplate: `<b>%{x}</b><br>AP50 ${esc(A)} %{customdata[0]:.3f} · ${esc(B)} %{customdata[1]:.3f}<br>` +
      `only ${esc(A)} finds %{customdata[2]} · only ${esc(B)} %{customdata[3]} · both miss %{customdata[4]}<br>` +
      `false positives ${esc(A)} %{customdata[5]} · ${esc(B)} %{customdata[6]}<extra></extra>`,
  }], { ...base(), showlegend: false, margin: { t: 10, b: 110, l: 60, r: 10 }, xaxis: { ...ax(), tickangle: -40 },
    yaxis: { ...ax(), title: { text: `AP50  ▲ ${A} better · ▼ ${B} better` }, tickformat: '+.0%', zeroline: true } }, cfg);
}

// ---------------------------------------------------------------- clicks
function toGallery(ids: number[], title: string): void {
  $req<HTMLSelectElement>('sort').value = 'none';
  setSelection(ids, title);
  switchTab('explore');
}

// the threshold this tab scored a model at, so the Model tab / gallery show the same mistakes
const confOf = (mid: string | null): number | null => { const r = E.data?.leaderboard.rows.find((x) => x.id === mid); return r ? r.conf : null; };

function runAction(a: FindingAction | null | undefined): void {
  if (!a) return;
  if (a.kind === 'select') toGallery(a.ids ?? [], a.title || 'finding');
  else if (a.kind === 'model' && a.mid !== undefined) void openModelClass(a.mid, a.cls ?? null, confOf(a.mid));
  else if (a.kind === 'image' && a.sid !== undefined) void openModal(a.sid);
}

function bindChartClicks(): void {
  const once = (id: string, fn: (ev: Plotly.PlotMouseEvent) => void): void => {
    const el = $<Plotly.PlotlyHTMLElement>(id);
    if (el && el.on && !el.dataset.bound) { el.dataset.bound = '1'; el.on('plotly_click', fn); }
  };
  once('e-heat', (ev) => {
    const [mid, c] = customOf(ev.points[0]);
    if (typeof mid === 'string' && typeof c === 'number') void openModelClass(mid, c, confOf(mid));
  });
  once('e-balance', (ev) => {
    const [c] = customOf(ev.points[0]);
    if (E.focus && typeof c === 'number') void openModelClass(E.focus, c, confOf(E.focus));
  });
  once('e-gap', (ev) => {
    const cd = ev.points[0]?.customdata;
    if (E.focus && typeof cd === 'number') void openModelClass(E.focus, cd, confOf(E.focus));
  });
  once('e-sess', (ev) => {
    const [i] = customOf(ev.points[0]);
    const ins = E.data?.insights;
    const s = ins && typeof i === 'number' ? ins.sessions[i] : undefined;
    if (ins && s) toGallery(s.ids, `session ${s.session}: mistakes of ${ins.model.name}`);
  });
  once('e-speed', (ev) => {
    const pt = ev.points[0];
    const r = pt ? E.data?.leaderboard.rows[pt.pointIndex] : undefined;
    if (r) setFocus(r.id);
  });
}

function setFocus(id: string | null | undefined): void {
  if (!id || id === E.focus) return;
  E.focus = id;
  void refresh();
}

const isCompareKey = (k: string | undefined): k is CompareKey => k === 'only_a' || k === 'only_b' || k === 'both_miss';
const isKnnCell = (k: string | undefined): k is KnnCell => k === 'both' || k === 'model_only' || k === 'knn_only' || k === 'neither';
const isLbCol = (k: string | undefined): k is LbCol => LB_COLS.some((c) => c[0] === k);

function bindEval(): void {
  if (bound) return;
  bound = true;
  $req('e-split').onclick = (e) => { const b = closestOf(e, '[data-split]'); if (b && b.dataset.split) { E.split = b.dataset.split; void refresh(); } };
  $req('e-mode').onclick = (e) => { const b = closestOf(e, '[data-mode]'); if (b && b.dataset.mode) { E.mode = b.dataset.mode; void refresh(); } };
  let t: ReturnType<typeof setTimeout> | undefined;
  const conf = $req<HTMLInputElement>('e-conf');
  conf.oninput = () => { E.conf = +conf.value; $req('e-conf-v').textContent = E.conf.toFixed(2); clearTimeout(t); t = setTimeout(() => { void refresh(); }, 300); };
  const focus = $req<HTMLSelectElement>('e-focus');
  focus.onchange = () => setFocus(focus.value);
  // picking the other side's model swaps the two
  const cmpA = $req<HTMLSelectElement>('e-cmp-a'), cmpB = $req<HTMLSelectElement>('e-cmp-b');
  cmpA.onchange = () => { if (cmpA.value === E.cmpB) E.cmpB = E.cmpA; E.cmpA = cmpA.value; void loadCompare(); };
  cmpB.onchange = () => { if (cmpB.value === E.cmpA) E.cmpA = E.cmpB; E.cmpB = cmpB.value; void loadCompare(); };
  $req('e-cmp-chips').onclick = (e) => {
    const ch = closestOf(e, '[data-cmp]');
    const key = ch?.dataset.cmp;
    if (!ch || !E.cmp || !isCompareKey(key)) return;
    const A = E.cmp.a.name, B = E.cmp.b.name;
    const title = { only_a: `Found by ${A}, missed by ${B}`, only_b: `Found by ${B}, missed by ${A}`, both_miss: `Missed by ${A} and ${B}` }[key];
    toGallery(E.cmp.ids[key], title);
  };
  $req('e-run-all').onclick = () => {
    const done = new Set((E.data?.leaderboard.rows ?? []).map((r) => r.id));
    void runModels(E.models.filter((m) => !done.has(m.id)).map((m) => m.id));
  };
  $req('eval-side').addEventListener('click', (e) => {
    const b = closestOf(e, '[data-eact]');
    if (b) {
      if (b.dataset.eact === 'add') void openSettings('models');
      else if (b.dataset.eact === 'run') { const mid = b.closest<HTMLElement>('[data-mid]')?.dataset.mid; if (mid) void runModels([mid]); }
      return;
    }
    const m = closestOf(e, '.e-model[data-mid]');
    if (m && E.data && E.data.leaderboard.rows.some((r) => r.id === m.dataset.mid)) setFocus(m.dataset.mid);
  });
  $req('e-html').onclick = () => { void exportHTML(); };
  // ◆ Explain (only shown when explain_eval is on): the AI tab, with the evaluation as context
  $req('e-ai-explain').onclick = () => askAI('Explain this evaluation: what is good, what is wrong, and the 3 most useful next steps.', { eval: true });
  $req('e-csv').onclick = exportCSV;
  $req('e-json').onclick = () => download(`evaluation-${stamp()}.json`, JSON.stringify(E.data, null, 1), 'application/json');
  $req('tab-eval').addEventListener('click', (e) => {
    const ea = closestOf(e, '[data-eact]');
    if (ea) { if (ea.dataset.eact === 'add') void openSettings('models'); else void runModels(E.models.map((m) => m.id)); return; }
    const d = E.data;
    const f = closestOf(e, '.finding[data-f]');
    if (f) { runAction(d?.findings[+(f.dataset.f ?? NaN)]?.action); return; }
    const th = closestOf(e, 'th[data-sort]');
    if (th) { if (isLbCol(th.dataset.sort)) { E.sortKey = th.dataset.sort; renderLeaderboard(); } return; }
    const tr = closestOf(e, '#e-lb tr[data-mid]');
    if (tr) { setFocus(tr.dataset.mid); return; }
    const ins = d?.insights;
    const k = closestOf(e, '[data-knn]');
    if (k) {
      const cell = k.dataset.knn;
      const c = ins && ins.knn && isKnnCell(cell) ? ins.knn[cell] : undefined;
      if (c) toGallery(c.ids, `model vs neighbours: ${k.querySelector('span')?.textContent ?? ''}`);
      return;
    }
    const sz = closestOf(e, '[data-size]');
    if (sz) {
      const s = ins?.sizes.find((x) => x.size === sz.dataset.size);
      if (s) toGallery(s.ids, `${s.size} boxes the model got wrong`);
      return;
    }
    const w = closestOf(e, '[data-w]');
    if (w && ins) {
      const it = ins.worst[+(w.dataset.w ?? NaN)];
      if (!it) return;
      void useModel(ins.model.id, ins.conf).then(() => {
        S.model.showPreds = true;
        if (it.sid != null) void openModal(it.focus ?? it.sid); else void openPredModal(it.pid ?? -1);
      });
    }
  });
}

// ---------------------------------------------------------------- export
const stamp = (): string => new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');

function download(name: string, text: string, type: string): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function csvCell(v: unknown): string { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; }

function exportCSV(): void {
  const d = E.data;
  if (!d) return;
  const lb = d.leaderboard, classes = S.meta?.classes ?? [];
  const head = ['model', 'family', 'format', 'split', 'conf', 'mAP50-95', 'mAP50', 'precision', 'recall', 'F1', 'ms_per_image', 'size_mb',
    'labels', 'tp', 'missed', 'wrong_class', 'fp_background', 'fp_duplicate', ...classes.map((c) => `AP50 ${c}`)];
  const lines = [head.map(csvCell).join(',')];
  lb.rows.forEach((r, i) => lines.push([r.name, r.family, r.format, lb.split, r.conf.toFixed(2), r.map.toFixed(4), r.map50.toFixed(4), r.p.toFixed(4),
    r.r.toFixed(4), r.f1.toFixed(4), r.ms.toFixed(2), r.size_mb, r.labels, r.tp, r.missed, r.wrong_cls, r.fp_bg, r.fp_dup,
    ...(lb.per_class.ap50[i] ?? []).map((v) => (v == null ? '' : v.toFixed(4)))].map(csvCell).join(',')));
  download(`evaluation-${stamp()}.csv`, lines.join('\n'), 'text/csv');
}

async function exportHTML(): Promise<void> {
  const st = $req('e-export-st');
  st.textContent = 'rendering charts…';
  const d = E.data;
  if (!d) return;
  const lb = d.leaderboard, ins = d.insights, classes = S.meta?.classes ?? [];
  const r0 = lb.rows[0];
  const imgs: Record<string, string> = {};
  const titles: Record<string, string> = { 'e-speed': 'Speed vs accuracy', 'e-heat': 'AP50 per class, every model', 'e-balance': 'Training labels vs AP50',
    'e-gap': 'Train vs val AP50', 'e-sess': 'Recall and precision per session', 'e-calib': 'Calibration', 'e-loc': 'Box fit (mean IoU)' };
  for (const id of EVAL_PLOTS) {
    const el = $<Plotly.PlotlyHTMLElement>(id);
    if (!el || !el.data) continue;
    // a light, print-friendly copy of each chart, whatever theme the page is in
    try {
      imgs[id] = await Plotly.toImage({ data: el.data, layout: { ...el.layout, paper_bgcolor: '#ffffff', plot_bgcolor: '#ffffff',
        font: { ...(el.layout.font || {}), color: '#1b222c' } } }, { format: 'png', width: 900, height: 480, scale: 1.5 });
    } catch (e) { /* chart skipped */ }
  }
  const e = (v: unknown): string => esc(v == null ? '' : String(v));
  const sevName: Record<Finding['severity'], string> = { 3: 'Act on it', 2: 'Worth a look', 1: 'Good to know' };
  const lbRows = lb.rows.map((r) => `<tr><td>${e(r.name)}${r.id === lb.best ? ' <span class="b">best</span>' : ''}</td><td>${e(r.family)} ${e(r.format)}</td>` +
    `<td>${pct(r.map)}</td><td>${pct(r.map50)}</td><td>${pct(r.p)}</td><td>${pct(r.r)}</td><td>${pct(r.f1)}</td><td>${r.conf.toFixed(2)}</td>` +
    `<td>${r.ms.toFixed(1)}</td><td>${r.size_mb ?? '–'}</td><td>${r.missed}</td><td>${r.wrong_cls}</td><td>${r.fp_bg + r.fp_dup}</td></tr>`).join('');
  const pcRows = ins ? ins.per_class.filter((c) => c.n).map((c) => `<tr><td>${e(classes[c.cls])}</td><td>${c.n}</td><td>${ins.balance.train_labels[c.cls]}</td>` +
    `<td>${pct(c.ap50)}</td><td>${pct(c.ap)}</td><td>${pct(c.p)}</td><td>${pct(c.r)}</td><td>${c.missed + c.loc}</td><td>${c.wrong_cls}</td><td>${c.fp_bg + c.fp_dup}</td></tr>`).join('') : '';
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Model evaluation · ${e(d.dataset.name)}</title><style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#1b222c;background:#fff;max-width:1100px;margin:0 auto;padding:24px 16px;line-height:1.45}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px;border-bottom:1px solid #d5dbe3;padding-bottom:6px}.muted{color:#5b6573}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid #e3e7ed;text-align:right}th:first-child,td:first-child{text-align:left}
th{background:#f3f5f8;font-weight:600}.b{background:#23833a;color:#fff;border-radius:4px;padding:1px 5px;font-size:11px}
.f{border:1px solid #d5dbe3;border-left:5px solid #9aa3ad;border-radius:6px;padding:8px 12px;margin:8px 0}.f b{display:block}.s3{border-left-color:#cf3328}.s2{border-left-color:#b85a00}.s1{border-left-color:#2766c4}
.f small{color:#5b6573;text-transform:uppercase;font-size:11px;letter-spacing:.04em}.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}
figure{margin:0}figure img{width:100%;border:1px solid #e3e7ed;border-radius:6px}figcaption{font-size:13px;color:#5b6573;margin-top:4px}.wrap{overflow-x:auto}
</style></head><body>
<h1>Model evaluation</h1><div class="muted">${e(d.dataset.name)} (${e(d.dataset.uri)}) · ${e(lb.split)} images (${r0?.images ?? 0}) · ${(r0?.labels ?? 0).toLocaleString()} labels ·
${E.mode === 'best' ? 'each model at its best-F1 confidence' : `confidence ${E.conf}`} · split from ${e(d.split_source || 'none: all images scored')} · ${new Date().toLocaleString()}</div>
<h2>What matters</h2>${d.findings.map((f) => `<div class="f s${f.severity}"><small>${sevName[f.severity]}${f.tag ? ` · ${e(f.tag)}` : ''}</small><b>${e(f.title)}</b>${e(f.detail)}</div>`).join('') || '<p>Nothing stands out.</p>'}
<h2>Leaderboard</h2><div class="wrap"><table><thead><tr><th>model</th><th>type</th><th>mAP50-95</th><th>mAP50</th><th>P</th><th>R</th><th>F1</th><th>conf</th><th>ms/img</th><th>MB</th><th>missed</th><th>wrong cls</th><th>FP</th></tr></thead><tbody>${lbRows}</tbody></table></div>
${ins ? `<h2>Per class · ${e(ins.model.name)}</h2><div class="wrap"><table><thead><tr><th>class</th><th>labels</th><th>train labels</th><th>AP50</th><th>AP50-95</th><th>P</th><th>R</th><th>missed</th><th>wrong cls</th><th>FP</th></tr></thead><tbody>${pcRows}</tbody></table></div>` : ''}
<h2>Charts</h2><div class="charts">${Object.entries(imgs).map(([id, src]) => `<figure><img src="${src}" alt="${e(titles[id])}"><figcaption>${e(titles[id])}</figcaption></figure>`).join('')}</div>
</body></html>`;
  download(`evaluation-${d.dataset.name}-${stamp()}.html`, html, 'text/html');
  st.textContent = `report saved (${Object.keys(imgs).length} charts)`;
}
