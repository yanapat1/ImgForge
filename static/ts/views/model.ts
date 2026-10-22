// Model tab: pick a saved model, run it over the dataset, and compare its predictions with the labels.
// Left: threshold / split controls, headline numbers, per-class table. Right: likely label errors, confusion
// matrix, errors per class, PR + F1 curves, confidence histogram, recall by box property, false positives.
// The per-label outcome is copied into S.P.m_status / m_pred / m_score so Explore can colour and sort by it.

import { errMessage, errText, getJSON, postJSON } from '../core/api.js';
import { userKey } from '../core/auth.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { setBar, waitJob } from '../core/jobs.js';
import { pts, S } from '../core/state.js';
import { clsColor, clsName, ERR, THEME } from '../core/theme.js';
import { invalidateHover, renderPlot } from '../plot/render.js';
import { openSettings } from '../settings/dialog.js';
import type { DetectionChecks } from '../types.js';
import type { CardRef, ClassRow, EvalFp, EvalIssue, ModelEvalFull, ModelInfo, ModelsPayload, PredictResult } from '../types-views.js';
import { at, ax, base, cfg, customOf, tbody } from './chart-util.js';
import { closestOf } from './ev-util.js';
import { renderGallery, setSelection } from './gallery.js';
import { curTab, switchTab } from './layout.js';
import { openCard } from './modal.js';

// Plotly is loaded as a classic script by main.ts (window.Plotly)
declare const Plotly: typeof import('plotly.js');

export const MODEL_PLOTS = ['m-cm', 'm-errs', 'm-pr', 'm-f1', 'm-hist', 'm-recall'];
const M = S.model;
const FP_KIND: Record<string, string> = { fp_bg: 'nothing there (missing label?)', fp_dup: 'second box on one object', fp_loc: 'badly placed' };
let loaded = false, drawn = false, evalSeq = 0, bound = false;

// view state that is not part of the shared S.model: the issue filter, the cards on screen, the recall bins
let issueKind: string | null = null;
let issueList: EvalIssue[] = [];
let fpList: EvalFp[] = [];
let recallBins: Array<{ lo: number; hi: number; n: number; r: number; ids: number[] }> = [];

const store = { get: (k: string): string | null => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k: string, v: string): void => { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } } };

export function invalidateModel(): void { drawn = false; }

/** The scored model (GET api/models/{id}/eval), typed in full; null until a model was scored. */
export const modelEval = (): ModelEvalFull | null => M.ev as ModelEvalFull | null;
const models = (): ModelInfo[] => M.list as ModelInfo[];
const current = (): ModelInfo | undefined => models().find((m) => m.id === M.id);
const cname = (c: number): string => clsName(c) ?? String(c);

// ---------------------------------------------------------------- data
export async function loadModels(): Promise<void> {
  const r = await getJSON<ModelsPayload>('api/models');
  M.list = r.models.filter((m) => m.family !== 'hf');      // Hugging Face backbones only embed (the plot's feature list)
  const saved = store.get(userKey('dataviz.model'));
  if (!M.list.some((m) => m.id === M.id)) {
    const pick = M.list.find((m) => m.id === saved) || M.list.find((m) => m.predicted) || M.list[0];
    M.id = pick ? pick.id : null;
    M.ev = null;
  }
  loaded = true;
  $req('m-model').innerHTML = models().map((m) => `<option value="${esc(m.id)}" ${m.id === M.id ? 'selected' : ''}>` +
    `${esc(m.name)} · ${esc(m.family)} ${esc(m.format)}${m.predicted ? '' : ' (not run yet)'}</option>`).join('');
}

export async function showModelTab(): Promise<void> {
  bindModel();
  if (!loaded) {
    try { await loadModels(); } catch (e) { renderEmpty(`Could not load models: ${errText(e)}`); return; }
  }
  const ev = modelEval();
  if (ev && ev.model.id === M.id) { renderModel(); return; }
  await refreshEval();
}

// model-only controls exist only while a model's outcome is loaded (data views stay model-free otherwise)
function syncModelControls(): void {
  const on = !!pts().m_status;
  const colorBy = $req<HTMLSelectElement>('color-by'), sort = $req<HTMLSelectElement>('sort');
  const og = colorBy.querySelector<HTMLOptGroupElement>('optgroup[data-needs="model"]');
  if (og) { og.hidden = !on; og.disabled = !on; }
  if (!on && colorBy.value.startsWith('m_')) { colorBy.value = 'class'; if (S.view) renderPlot(); }
  for (const v of ['m-status', 'm-score']) {
    const o = sort.querySelector<HTMLOptionElement>(`option[value="${v}"]`);
    if (o) { o.hidden = !on; o.disabled = !on; }
  }
  if (!on && /^m-/.test(sort.value)) { sort.value = 'none'; renderGallery(); }
  // cleanlab's detection checks (Find issues › Detection, three colourings): only once they are loaded
  const od = !!pts().cl_swap;
  $req('od-sec').classList.toggle('hidden', !od);
  colorBy.querySelectorAll<HTMLOptionElement>('option[data-od]').forEach((o) => { o.hidden = !od; o.disabled = !od; });
  if (!od && ['cl_swap', 'cl_badloc', 'cl_image_quality'].includes(colorBy.value)) { colorBy.value = 'class'; if (S.view) renderPlot(); }
}

// cleanlab object detection on this model's predictions: bad placement / wrong class per label box, missed objects
let odSeq = 0;
async function loadDetectionChecks(m: ModelInfo): Promise<void> {
  const seq = ++odSeq;
  try {
    const r = await getJSON<DetectionChecks>(`api/models/${encodeURIComponent(m.id)}/cleanlab`);
    if (seq !== odSeq || M.id !== m.id) return;
    const P = pts();
    P.cl_swap = r.cl_swap; P.cl_badloc = r.cl_badloc; P.cl_image_quality = r.cl_image_quality;
    $req('od-model').textContent = `· cleanlab on ${m.name}`;
    const n = r.overlooked.filter((o) => o.quality < 0.1).length;
    $req('od-missed-n').textContent = n ? `${n} likely unlabelled object${n === 1 ? '' : 's'}` : 'objects nobody labelled';
  } catch (e) {               // cleanlab missing on the server (501) or not predicted: the other model views still work
    if (seq !== odSeq) return;
    const P = pts();
    delete P.cl_swap; delete P.cl_badloc; delete P.cl_image_quality;
  }
  invalidateHover();
  syncModelControls();
  if (S.view && $req<HTMLSelectElement>('color-by').value.startsWith('cl_')) renderPlot();
}

/** Find issues › Missed objects: the Model tab's list of objects the model finds where nothing is labelled. */
export async function showMissedObjects(): Promise<void> {
  issueKind = 'fp_bg';
  switchTab('model');
  await showModelTab();
}

function clearModelColumns(): void {
  const P = pts();
  delete P.m_status; delete P.m_pred; delete P.m_score;
  delete P.cl_swap; delete P.cl_badloc; delete P.cl_image_quality;
  odSeq++;
  invalidateHover();
  syncModelControls();
}

export async function refreshEval(): Promise<void> {
  const m = current();
  $req('m-run-box').classList.toggle('hidden', !m || m.predicted);
  if (!m) { M.ev = null; clearModelColumns(); renderEmpty(); return; }
  if (!m.predicted) { M.ev = null; clearModelColumns(); renderEmpty(); return; }
  const seq = ++evalSeq;
  $req('m-sub').textContent = 'scoring…';
  try {
    const ev = await getJSON<ModelEvalFull>(`api/models/${encodeURIComponent(m.id)}/eval?conf=${M.conf}&iou=${M.iou}&split=${M.split}`);
    if (seq !== evalSeq) return;
    M.ev = ev;
    M.split = ev.split;
    const P = pts();
    P.m_status = ev.gt.status;
    P.m_pred = ev.gt.pred_cls;
    P.m_score = ev.gt.pred_score;
    invalidateHover();
    syncModelControls();
    void loadDetectionChecks(m);
    drawn = false;
    renderModel();
    if (S.view && $req<HTMLSelectElement>('color-by').value.startsWith('m_')) renderPlot();
    if (/^m-/.test($req<HTMLSelectElement>('sort').value)) renderGallery();
  } catch (e) {
    if (seq !== evalSeq) return;
    if (/ 409/.test(errMessage(e))) { m.predicted = false; await refreshEval(); return; }
    renderEmpty(`Scoring failed: ${errText(e)}`);
  }
}

async function runPredictions(): Promise<void> {
  const m = current();
  if (!m) return;
  const btn = $req<HTMLButtonElement>('m-run'), prog = $req('m-run-prog');
  btn.disabled = true;
  prog.classList.remove('hidden');
  const numEl = (): Element | null => prog.querySelector('.num');
  try {
    const r = await postJSON<PredictResult>(`api/models/${encodeURIComponent(m.id)}/predict`, {});
    if (!r.ready) {
      await waitJob(r.job_id, (j) => {
        const fill = prog.querySelector<HTMLElement>('.bar > div');
        if (fill) setBar(fill, j);
        const num = numEl();
        if (num) num.textContent = j.total ? `${j.done}/${j.total} images ${j.extra || ''}` : j.stage;
      });
    }
    m.predicted = true;
    await loadModels();
    await refreshEval();
  } catch (e) {
    const num = numEl();
    if (num) num.textContent = errText(e);
  } finally {
    btn.disabled = false;
    const cur = current();
    if (cur && cur.predicted) prog.classList.add('hidden');
  }
}

// ---------------------------------------------------------------- left panel
function renderEmpty(err?: string): void {
  const m = current();
  $req('m-grid').classList.add('hidden');
  $req('m-empty').classList.remove('hidden');
  $req('m-kpis').innerHTML = '';
  $req('m-warn').innerHTML = err ? `<div class="m-note warn">${esc(err)}</div>` : '';
  tbody($req<HTMLTableElement>('m-class-table')).innerHTML = '';
  $req('m-sub').textContent = '';
  if (!M.list.length) {
    $req('m-empty').innerHTML = `${icon('model')}<h3>No models yet</h3><p>Add a YOLO, RF-DETR or custom model (a .pt / .pth / .pkl / ` +
      '.safetensors / ONNX / OpenVINO file, or an HTTP API) to see where it disagrees with your labels.</p>' +
      `<button class="primary" data-mact="add">${icon('plus')}Add a model</button>`;
  } else if (m && !m.predicted) {
    $req('m-empty').innerHTML = `${icon('target')}<h3>${esc(m.name)} has not run on this dataset yet</h3>` +
      '<p>Running it predicts every image once (cached in this dataset’s workspace). Then every chart here is instant, ' +
      'for any confidence threshold.</p>' +
      `<button class="primary" data-mact="run">${icon('play')}Run on this dataset</button>`;
  } else {
    $req('m-empty').innerHTML = `${icon('alert')}<h3>Nothing to show</h3><p>${esc(err || '')}</p>`;
  }
}

const pct = (v: number | null | undefined): string => (v == null ? '–' : (v * 100).toFixed(1));

function renderLeft(ev: ModelEvalFull): void {
  const t = ev.totals, ap = ev.ap;
  $req<HTMLInputElement>('m-conf').value = String(M.conf);
  $req('m-conf-v').textContent = (+M.conf).toFixed(2);
  $req<HTMLSelectElement>('m-iou').value = String(M.iou);
  document.querySelectorAll<HTMLButtonElement>('#m-split .view-btn').forEach((b) => {
    const sp = b.dataset.split ?? '';
    const n = sp === 'all' ? ev.n_images : ev.splits[sp] || 0;
    b.disabled = sp !== 'all' && !n;
    b.classList.toggle('active', sp === ev.split);
    b.setAttribute('aria-pressed', String(sp === ev.split));
    b.title = `${sp}: ${n} images`;
  });
  $req('m-sub').textContent = `${t.images} images · ${t.labels.toLocaleString()} labels`;
  const kpi = (v: string, l: string, title: string): string => `<div class="kpi" title="${esc(title)}"><b>${v}</b><span>${l}</span></div>`;
  $req('m-kpis').innerHTML = kpi(pct(t.p), 'precision %', 'share of predictions (at this confidence) that match a label') +
    kpi(pct(t.r), 'recall %', 'share of labels the model found with the right class') +
    kpi(pct(ap.map50), 'mAP50 %', 'mean AP at IoU 0.5 over all confidences (same as ultralytics val)') +
    kpi(pct(ap.map), 'mAP50-95 %', 'mean AP over IoU 0.5…0.95') +
    kpi(ap.best_conf.toFixed(2), 'best conf', `highest mean F1 (${ap.best_f1.toFixed(3)})`) +
    kpi(ev.ms ? ev.ms.toFixed(1) : '–', 'ms / image', 'mean inference time while predicting (incl. API round trip)');
  const notes: string[] = [];
  if (ev.split === 'all' && ev.splits.train) {
    notes.push(`<div class="m-note warn">${icon('alert')} ${ev.splits.train} of these images were used for training: numbers look better than on new images. Choose <b>val</b> for honest ones.</div>`);
  } else if (ev.split === 'train') {
    notes.push(`<div class="m-note warn">${icon('alert')} Training images: the model has seen them, so these numbers are optimistic.</div>`);
  } else if (!ev.split_source) {
    notes.push('<div class="m-note">No train/val split found (config <code>models.split_source</code>): scoring every image.</div>');
  }
  if (ev.unmatched.length) {
    notes.push(`<div class="m-note warn">${icon('alert')} Model classes not in this dataset (ignored): ${ev.unmatched.map(esc).join(', ')}</div>`);
  }
  if (ev.model.format === 'api') notes.push('<div class="m-note">API model: boxes below the server’s own confidence cut are never returned, so low-confidence curves stop early.</div>');
  $req('m-warn').innerHTML = notes.join('');
  const rows = ev.per_class.filter((c) => c.n).sort((a, b) => (a.ap50 ?? 1) - (b.ap50 ?? 1));
  tbody($req<HTMLTableElement>('m-class-table')).innerHTML = rows.map((c) => {
    const miss = c.missed + c.loc + c.wrong_cls;
    const fp = c.fp_bg + c.fp_dup;
    return `<tr data-c="${c.cls}" tabindex="0" role="button" title="load the ${esc(clsName(c.cls))} labels the model got wrong">` +
      `<td><span class="cname"><span class="dot" style="background:${clsColor(c.cls)}"></span>${esc(clsName(c.cls))}</span></td>` +
      `<td class="r">${pct(c.ap50)}</td><td class="r">${pct(c.p)}</td><td class="r">${pct(c.r)}</td>` +
      `<td class="r${miss ? ' bad' : ''}">${miss}</td><td class="r${fp ? ' bad' : ''}">${fp}</td></tr>`;
  }).join('');
}

// ---------------------------------------------------------------- right panel
// legends sit in their own strip above the plot area, under the card's <h3> (not on top of it)
const topLegend = (): Partial<Plotly.Legend> => ({ orientation: 'h', x: 0, xanchor: 'left', y: 1, yanchor: 'bottom', font: { color: THEME.text } });

// pixel width of the longest tick label: the confusion matrix margins fit the class names (60–220 px)
let measureCtx: CanvasRenderingContext2D | null = null;
function labelWidth(labels: string[], size = 12): number {
  const ctx = measureCtx || (measureCtx = document.createElement('canvas').getContext('2d'));
  if (!ctx) return 0;
  ctx.font = `${size}px ${THEME.font || 'sans-serif'}`;
  return labels.reduce((w, t) => Math.max(w, ctx.measureText(String(t)).width), 0);
}

const issueColor = (k: string): string => (k === 'wrong_cls' ? THEME.errCls : k === 'fp_bg' ? THEME.errFp : THEME.errMiss);

function renderIssues(ev: ModelEvalFull): void {
  const kinds: Record<EvalIssue['kind'], string> = { wrong_cls: 'wrong class?', fp_bg: 'missing label?', missed: 'model never finds it' };
  const counts: Record<string, number> = {};
  ev.issues.forEach((it) => { counts[it.kind] = (counts[it.kind] || 0) + 1; });
  const want = issueKind && counts[issueKind] ? issueKind : null;
  $req('m-issue-filter').innerHTML = `<span class="chip chip-all${want ? ' off' : ''}" role="button" tabindex="0" data-kind="">all <span class="n">${ev.issues.length}</span></span>` +
    (Object.entries(kinds) as Array<[EvalIssue['kind'], string]>).filter(([k]) => counts[k]).map(([k, l]) =>
      `<span class="chip${want && want !== k ? ' off' : ''}" role="button" tabindex="0" data-kind="${k}">` +
      `<span class="dot" style="background:${issueColor(k)}"></span>${l} <span class="n">${counts[k]}</span></span>`).join('');
  $req('m-issues-sub').textContent = `· score ≥ ${Math.max(0.5, M.conf).toFixed(2)}`;
  const list = ev.issues.filter((it) => !want || it.kind === want).slice(0, 200);
  $req('m-issues').innerHTML = list.map((it, i) => {
    const img = it.sid != null ? `api/crop/${it.sid}` : `api/models/${encodeURIComponent(String(M.id))}/pred/${it.pid}/crop`;
    const d = it.kind === 'wrong_cls' ? `label <b>${esc(cname(it.cls ?? -1))}</b>, model says <b>${esc(cname(it.pred_cls ?? -1))}</b>`
      : it.kind === 'fp_bg' ? `model sees <b>${esc(cname(it.pred_cls ?? -1))}</b> where nothing is labelled`
        : `label <b>${esc(cname(it.cls ?? -1))}</b>: no prediction${it.score ? ` (best nearby ${it.score.toFixed(2)})` : ''}`;
    return `<button class="issue${S.edited.has(it.image) ? ' edited' : ''}" data-i="${i}" data-image="${esc(it.image)}" title="open the image">` +
      `<img loading="lazy" src="${img}" alt=""><span class="k" style="--c:${issueColor(it.kind)}">${kinds[it.kind]}</span>` +
      `<span class="d">${d}</span>${it.kind !== 'missed' ? `<span class="s">${it.score.toFixed(2)}</span>` : ''}</button>`;
  }).join('') || '<p class="muted hint">No confident disagreements at this threshold.</p>';
  issueList = list;
}

function renderFps(ev: ModelEvalFull): void {
  const fps = ev.fps.filter((f) => f.pred_cls >= 0).slice(0, 120);
  $req('m-fp-sub').textContent = `· ${ev.totals.fp_bg} with nothing there, ${ev.totals.fp_dup} duplicates`;
  $req('m-fps').innerHTML = fps.map((f, i) => `<button class="issue${S.edited.has(f.image) ? ' edited' : ''}" data-fp="${i}" data-image="${esc(f.image)}" title="${esc(f.image)}">` +
    `<img loading="lazy" src="api/models/${encodeURIComponent(String(M.id))}/pred/${f.pid}/crop" alt="">` +
    `<span class="k" style="--c:${clsColor(f.pred_cls)}">${esc(clsName(f.pred_cls))}</span>` +
    `<span class="d">${esc(FP_KIND[f.kind] || f.kind)}</span><span class="s">${f.score.toFixed(2)}</span></button>`).join('') ||
    '<p class="muted hint">No false positives at this threshold.</p>';
  fpList = fps;
}

function renderCharts(ev: ModelEvalFull): void {
  const ap = ev.ap;
  const names = S.meta?.classes ?? [];
  const nc = names.length;
  const present = ev.per_class.filter((c) => c.n).map((c) => c.cls);

  // confusion matrix, rows normalised; last row / column = background (FP) / nothing found (FN)
  const lab = [...names, 'background'], pred = [...names, 'nothing found'];
  const rowsIdx = [...present, nc], cm = ev.confusion;
  const z = rowsIdx.map((r) => cm[r] ?? []);
  const colsIdx = [...present, nc];
  const zc = z.map((row) => colsIdx.map((c) => row[c] ?? 0));
  const zn = zc.map((row, i) => { const s = row.reduce((a, b) => a + b, 0) || 1; return row.map((v) => (rowsIdx[i] === nc ? (v ? 1 : 0) : v / s)); });
  void Plotly.react('m-cm', [{
    type: 'heatmap', z: zn, customdata: zc, x: colsIdx.map((c) => pred[c] ?? ''), y: rowsIdx.map((r) => lab[r] ?? ''),
    colorscale: [[0, THEME.surface2], [0.02, THEME.surface3], [0.5, THEME.accent], [1, THEME.errTp]], zmin: 0, zmax: 1, showscale: false,
    hovertemplate: 'label <b>%{y}</b><br>model <b>%{x}</b><br>%{customdata} boxes<extra></extra>',
  }], { ...base(), xaxis: { ...ax(), tickangle: -40 }, yaxis: { ...ax(), autorange: 'reversed' },
    margin: cmMargins(colsIdx.map((c) => pred[c] ?? ''), rowsIdx.map((r) => lab[r] ?? '')) }, cfg);

  // what happened to each label, per class (stacked) + false positives
  const per = ev.per_class.filter((c) => c.n);
  const x = per.map((c) => cname(c.cls));
  const series: Array<[string, (c: ClassRow) => number, string, number]> = [['tp', (c) => c.tp, THEME.errTp, 0], ['wrong class', (c) => c.wrong_cls, THEME.errCls, 1],
    ['badly placed', (c) => c.loc, THEME.errLoc, 2], ['missed', (c) => c.missed, THEME.errMiss, 3],
    ['false positive', (c) => c.fp_bg + c.fp_dup, THEME.errFp, -1]];
  const errTraces: Plotly.Data[] = series.map(([n, f, col, st]) => ({
    type: 'bar', name: n === 'tp' ? 'found' : n, x, y: per.map(f), marker: { color: col }, customdata: per.map((c) => [c.cls, st]),
    hovertemplate: `%{x}: %{y} ${n}<extra></extra>`,
  }));
  void Plotly.react('m-errs', errTraces, { ...base(), barmode: 'stack', xaxis: { ...ax(), tickangle: -40 },
    margin: { t: 40, b: cmMargins(x, []).b, l: 50, r: 10 }, legend: topLegend() }, cfg);

  // PR curves: all classes bold, each class thin
  const rgrid = Array.from({ length: 101 }, (_, i) => i / 100);
  const prTraces: Plotly.Data[] = [
    ...present.map((c): Plotly.Data => ({ type: 'scatter', mode: 'lines', x: rgrid, y: ap.pr[c] ?? [], name: `${cname(c)} ${(ap.ap50[c] ?? 0).toFixed(3)}`,
      line: { color: clsColor(c), width: 1 }, opacity: 0.7, hovertemplate: `${esc(clsName(c))}<br>R %{x:.2f} P %{y:.3f}<extra></extra>` })),
    { type: 'scatter', mode: 'lines', x: rgrid, y: ap.pr_all, name: `all ${ap.map50.toFixed(3)}`, line: { color: THEME.text, width: 3 } },
  ];
  void Plotly.react('m-pr', prTraces, { ...base(), xaxis: { ...ax(), title: { text: 'recall' }, range: [0, 1] }, yaxis: { ...ax(), title: { text: 'precision' }, range: [0, 1.02] },
    showlegend: false, margin: { t: 10, b: 50, l: 60, r: 10 } }, cfg);

  // P / R / F1 vs confidence, current threshold marked
  const cg = ap.conf_grid;
  void Plotly.react('m-f1', [
    { type: 'scatter', mode: 'lines', x: cg, y: ap.p_conf_all, name: 'precision', line: { color: THEME.accent, width: 2 } },
    { type: 'scatter', mode: 'lines', x: cg, y: ap.r_conf_all, name: 'recall', line: { color: THEME.errCls, width: 2 } },
    { type: 'scatter', mode: 'lines', x: cg, y: ap.f1_conf_all, name: 'F1', line: { color: THEME.text, width: 3 } },
  ], { ...base(), xaxis: { ...ax(), title: { text: 'confidence threshold' }, range: [0, 1] }, yaxis: { ...ax(), range: [0, 1.02] },
    shapes: [vline(M.conf, THEME.highlight), vline(ap.best_conf, THEME.muted, 'dot')],
    legend: topLegend(), margin: { t: 40, b: 50, l: 50, r: 10 } }, cfg);

  // confidence of correct vs wrong predictions
  const mid = ap.hist_bins.slice(0, -1).map((b, i) => (b + (ap.hist_bins[i + 1] ?? b)) / 2);
  void Plotly.react('m-hist', [
    { type: 'bar', x: mid, y: ap.hist_tp, name: 'correct', marker: { color: THEME.errTp } },
    { type: 'bar', x: mid, y: ap.hist_fp, name: 'wrong', marker: { color: THEME.errFp } },
  ], { ...base(), barmode: 'overlay', bargap: 0.05, xaxis: { ...ax(), title: { text: 'confidence' }, range: [0, 1] },
    yaxis: { ...ax(), type: 'log', title: { text: 'predictions (log)' } }, shapes: [vline(M.conf, THEME.highlight)],
    legend: topLegend(), margin: { t: 40, b: 50, l: 60, r: 10 } }, cfg);

  renderRecall();
}

// left: the longest row name; bottom: the longest column name tilted 40° (plus one line of text)
function cmMargins(cols: string[], rows: string[]): Partial<Plotly.Margin> & { b: number } {
  const clamp = (v: number): number => Math.round(Math.max(60, Math.min(220, v)));
  return { t: 10, r: 10, l: clamp(labelWidth(rows) + 16), b: clamp(labelWidth(cols) * Math.sin(40 * Math.PI / 180) + 24) };
}

const vline = (x: number, color: string, dash: Plotly.Dash = 'solid'): Partial<Plotly.Shape> =>
  ({ type: 'line', x0: x, x1: x, y0: 0, y1: 1, yref: 'paper', line: { color, width: 1.5, dash } });

type RecallBy = 'area' | 'aspect' | 'brightness' | 'blur';
const isRecallBy = (v: string): v is RecallBy => v === 'area' || v === 'aspect' || v === 'brightness' || v === 'blur';
const RECALL_TITLE: Record<RecallBy, string> = { area: 'box area (% of image)', brightness: 'brightness', blur: 'sharpness', aspect: 'aspect w/h' };

// recall per quantile bin of a box property: where (small / dark / blurry boxes) the model fails
function renderRecall(): void {
  const by = $req<HTMLSelectElement>('m-recall-by').value, P = pts(), st = P.m_status;
  if (!st || !isRecallBy(by)) return;
  const ids = P.id.filter((id) => (st[id] ?? -1) >= 0);
  if (!ids.length) { Plotly.purge('m-recall'); return; }
  const val = (id: number): number => (by === 'area' ? at(P.area, id) * 100 : by === 'aspect' ? Math.log2(Math.max(at(P.aspect, id), 1e-3)) : at(P[by], id));
  const sorted = ids.slice().sort((a, b) => val(a) - val(b));
  const nb = Math.min(10, Math.max(2, Math.floor(sorted.length / 30)));
  const bins: typeof recallBins = [];
  for (let b = 0; b < nb; b++) {
    const part = sorted.slice(Math.floor(b * sorted.length / nb), Math.floor((b + 1) * sorted.length / nb));
    if (!part.length) continue;
    const found = part.filter((id) => st[id] === 0).length;
    bins.push({ lo: val(at(part, 0)), hi: val(at(part, part.length - 1)), n: part.length, r: found / part.length, ids: part });
  }
  recallBins = bins;
  const fmt = (v: number): string => (by === 'area' ? `${v.toFixed(v < 1 ? 2 : 1)}%` : by === 'aspect' ? `${Math.pow(2, v).toFixed(2)}` : v.toFixed(0));
  void Plotly.react('m-recall', [{
    type: 'bar', x: bins.map((b) => `${fmt(b.lo)}–${fmt(b.hi)}`), y: bins.map((b) => b.r), customdata: bins.map((b) => [b.n, Math.round(b.n * (1 - b.r))]),
    marker: { color: bins.map((b) => (b.r < 0.9 ? THEME.errMiss : b.r < 0.97 ? THEME.errLoc : THEME.errTp)) },
    hovertemplate: 'recall %{y:.1%}<br>%{customdata[0]} labels, %{customdata[1]} not found<br>click: load the misses<extra></extra>',
  }], { ...base(), xaxis: { ...ax(), title: { text: RECALL_TITLE[by] }, tickangle: -30 },
    yaxis: { ...ax(), title: { text: 'recall' }, tickformat: '.0%', range: [Math.min(0.5, ...bins.map((b) => b.r)) - 0.02, 1.01] },
    margin: { t: 10, b: 80, l: 60, r: 10 } }, cfg);
}

export function renderModel(): void {
  if (curTab !== 'model') return;
  const ev = modelEval();
  if (!ev) { if (loaded) void refreshEval(); return; }
  $req('m-empty').classList.add('hidden');
  $req('m-grid').classList.remove('hidden');
  $req('m-run-box').classList.add('hidden');
  renderLeft(ev);
  renderIssues(ev);
  renderFps(ev);
  if (!drawn) { renderCharts(ev); drawn = true; bindChartClicks(); } else MODEL_PLOTS.forEach((id) => { if ($<Plotly.PlotlyHTMLElement>(id)?.data) Plotly.Plots.resize(id); });
}

// ---------------------------------------------------------------- clicks → gallery / image
function toGallery(ids: number[], title: string): void {
  $req<HTMLSelectElement>('sort').value = 'm-status';
  setSelection(ids, title);
  switchTab('explore');
}

function bindChartClicks(): void {
  const P = pts();
  const bindOnce = (id: string, fn: (ev: Plotly.PlotMouseEvent) => void): void => {
    const el = $req<Plotly.PlotlyHTMLElement>(id);
    if (!el.dataset.bound) { el.dataset.bound = '1'; el.on('plotly_click', fn); }
  };
  const cls = (id: number): number => at(P.cls, id);
  const status = (id: number): number => P.m_status?.[id] ?? -1;
  bindOnce('m-cm', (ev) => {
    const pt = ev.points[0];
    if (!pt) return;
    const classes = S.meta?.classes ?? [];
    const r = classes.indexOf(String(pt.y)), c = classes.indexOf(String(pt.x));
    if (r < 0) {                                       // background row: false positives of class c
      issueKind = null;
      document.getElementById('m-fps')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    const pc = P.m_pred;
    const ids = P.id.filter((id) => cls(id) === r && (c < 0 ? status(id) >= 2 : (status(id) === 0 || status(id) === 1) && pc?.[id] === c));
    toGallery(ids, `label ${cname(r)}, model ${c < 0 ? 'found nothing' : cname(c)}`);
  });
  bindOnce('m-errs', (ev) => {
    const [c, st] = customOf(ev.points[0]);
    if (typeof c !== 'number' || typeof st !== 'number') return;
    if (st < 0) { document.getElementById('m-fps')?.scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
    toGallery(P.id.filter((id) => cls(id) === c && status(id) === st), `${cname(c)}: ${ERR[st]?.label ?? ''}`);
  });
  bindOnce('m-recall', (ev) => {
    const pt = ev.points[0];
    const b = pt ? recallBins[pt.pointIndex] : undefined;
    if (!pt || !b) return;
    toGallery(b.ids.filter((id) => status(id) > 0), `not found · ${$req<HTMLSelectElement>('m-recall-by').selectedOptions[0]?.text ?? ''} ${pt.x}`);
  });
  const conf = (ev: Plotly.PlotMouseEvent): void => { const x = ev.points[0]?.x; if (typeof x === 'number') setConf(x); };
  bindOnce('m-f1', conf);
  bindOnce('m-hist', conf);
}

function setConf(v: number): void {
  M.conf = Math.round(Math.min(0.99, Math.max(0.01, v)) * 100) / 100;
  $req<HTMLInputElement>('m-conf').value = String(M.conf);
  $req('m-conf-v').textContent = M.conf.toFixed(2);
  void refreshEval();
}

// an edit was saved: ring every card of that image (no re-render, the lists keep their scroll)
export function markEdited(name: string): void {
  S.edited.add(name);
  document.querySelectorAll<HTMLElement>('#m-issues .issue[data-image], #m-fps .issue[data-image]').forEach((el) => {
    if (el.dataset.image === name) el.classList.add('edited');
  });
}

function openIssue(it: EvalIssue): void {
  M.showPreds = true;
  $req<HTMLSelectElement>('sort').value = 'm-status';
  // prev / next walk the cards of this grid, whatever mix of labelled boxes and bare predictions
  openCard(it.sid != null ? { sid: it.sid } : { pid: it.pid ?? -1 },
    issueList.map((x): CardRef => (x.sid != null ? { sid: x.sid } : { pid: x.pid })));
}

function bindModel(): void {
  if (bound) return;
  bound = true;
  const sel = $req<HTMLSelectElement>('m-model');
  sel.onchange = () => {
    M.id = sel.value;
    store.set(userKey('dataviz.model'), M.id);
    const m = current();
    M.conf = m && m.conf ? m.conf : M.conf;
    M.ev = null;
    void refreshEval();
  };
  $req('m-add').onclick = () => { void openSettings('models'); };
  $req('m-run').onclick = () => { void runPredictions(); };
  let t: ReturnType<typeof setTimeout> | undefined;
  const conf = $req<HTMLInputElement>('m-conf');
  conf.oninput = () => {
    M.conf = +conf.value;
    $req('m-conf-v').textContent = M.conf.toFixed(2);
    clearTimeout(t);
    t = setTimeout(() => { void refreshEval(); }, 200);
  };
  $req('m-best').onclick = () => { const ev = modelEval(); if (ev) setConf(ev.ap.best_conf); };
  const iou = $req<HTMLSelectElement>('m-iou');
  iou.onchange = () => { M.iou = +iou.value; void refreshEval(); };
  $req('m-split').onclick = (e) => {
    const b = closestOf<HTMLButtonElement>(e, '[data-split]');
    if (!b || b.disabled) return;
    M.split = b.dataset.split ?? M.split;
    void refreshEval();
  };
  $req('m-recall-by').onchange = () => { renderRecall(); };
  $req('m-class-table').onclick = (e) => {
    const row = closestOf(e, 'tr[data-c]');
    if (!row) return;
    const c = +(row.dataset.c ?? NaN);
    const P = pts();
    toGallery(P.id.filter((id) => at(P.cls, id) === c && (P.m_status?.[id] ?? -1) > 0), `${cname(c)}: model mistakes`);
  };
  $req('tab-model').addEventListener('click', (e) => {
    const act = closestOf(e, '[data-mact]');
    if (act) { if (act.dataset.mact === 'add') void openSettings('models'); else void runPredictions(); return; }
    const chip = closestOf(e, '#m-issue-filter .chip');
    if (chip) { issueKind = chip.dataset.kind || null; const ev = modelEval(); if (ev) renderIssues(ev); return; }
    const is = closestOf(e, '.issue[data-i]');
    const issue = is ? issueList[+(is.dataset.i ?? NaN)] : undefined;
    if (issue) { openIssue(issue); return; }
    const fp = closestOf(e, '.issue[data-fp]');
    const f = fp ? fpList[+(fp.dataset.fp ?? NaN)] : undefined;
    if (f) openCard({ pid: f.pid }, fpList.map((x): CardRef => ({ pid: x.pid })));
  });
}

// Settings › Models changed the list: reload it, and re-score when the shown model changed
export async function modelsChanged(): Promise<void> {
  loaded = false;
  if (!S.P) return;                     // Settings › Models on the home / label page: no dataset to refresh
  try { await loadModels(); } catch (e) { return; }
  M.ev = null;
  if (curTab === 'model') void refreshEval(); else if (S.P.m_status) clearModelColumns();
}

// from the Evaluation tab: make `mid` the Model tab's model, then show that tab, or one class's mistakes in Explore
export async function useModel(mid: string, conf: number | null = null): Promise<void> {
  if (!loaded) await loadModels();
  if (conf != null && conf !== M.conf) {          // score at the same threshold the Evaluation tab used
    M.conf = conf;
    M.ev = null;
  }
  if (M.id !== mid) {
    M.id = mid;
    M.ev = null;
    store.set(userKey('dataviz.model'), mid);
    $req<HTMLSelectElement>('m-model').value = mid;
  }
  if (!M.ev) await refreshEval();
}

export async function openModelClass(mid: string, cls: number | null = null, conf: number | null = null): Promise<void> {
  if (cls == null) { await useModel(mid, conf); switchTab('model'); return; }
  await useModel(mid, conf);
  const P = pts();
  if (!P.m_status) return;
  toGallery(P.id.filter((id) => at(P.cls, id) === cls && (P.m_status?.[id] ?? -1) > 0), `${cname(cls)}: mistakes of ${current()?.name ?? ''}`);
}

export function predParam(): string {
  return M.showPreds && M.id && M.ev ? `model=${encodeURIComponent(M.id)}&conf=${M.conf}` : '';
}
