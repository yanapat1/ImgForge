// Model tab on a one-point-per-image session (no labels to score against): the model names each image. Its top class
// per image goes into S.P.ip_cls / ip_score (+ ip_names) so Explore can colour, hover and filter by it; the right
// panel shows images per predicted class (per dataset in a combined one), the top-confidence histogram and the
// agreement with a second model (GET api/image-models/agree: table, agreement, Cohen's kappa).

import { errText, getJSON } from '../core/api.js';
import { $req, esc } from '../core/dom.js';
import { pts, S } from '../core/state.js';
import { clsColor, originColor, THEME } from '../core/theme.js';
import { invalidateHover, renderPlot } from '../plot/render.js';
import type { ModelInfo } from '../types-views.js';
import { ax, base, cfg, tbody } from './chart-util.js';
import { closestOf } from './ev-util.js';
import { setSelection } from './gallery.js';
import { curTab } from './layout.js';

declare const Plotly: typeof import('plotly.js');

export interface ImageClasses {
  model: ModelInfo; conf: number; names: string[]; ms: number;
  pred_cls: number[]; pred_score: number[]; n_det: number[];
  images_per_class: number[]; detections_per_class: number[]; no_detection: number; n_images: number;
  by_origin?: Array<{ alias: string; images_per_class: number[]; no_detection: number; n_images: number }>;
}
interface Agreement {
  a: ModelInfo; b: ModelInfo; conf: number; rows: string[]; cols: string[]; table: number[][];
  agree: number | null; kappa: number | null; n: number; disagree_ids: number[];
}

export const MI_PLOTS = ['mi-bar', 'mi-hist', 'mi-agree'];
let res: ImageClasses | null = null;
let seq = 0, agreeSeq = 0, bound = false;
let models: () => ModelInfo[] = () => [];
let agreeRes: Agreement | null = null;

const pct = (v: number): string => `${(100 * v).toFixed(1)}`;

/** Forget the loaded answer (another model / threshold): the plot's model colourings go with it. */
export function clearImageModel(): void {
  res = null;
  const P = pts();
  delete P.ip_cls; delete P.ip_score; delete P.ip_names;
  syncColorBy();
  invalidateHover();
}

function syncColorBy(): void {
  const on = !!pts().ip_cls;
  const colorBy = $req<HTMLSelectElement>('color-by');
  const og = colorBy.querySelector<HTMLOptGroupElement>('optgroup[data-needs="ipred"]');
  if (og) { og.hidden = !on; og.disabled = !on; }
  const union = !!S.cmp;
  colorBy.querySelectorAll<HTMLOptionElement>('optgroup[data-needs="ipred"] option[data-union]').forEach((o) => { o.hidden = !union; o.disabled = !union; });
  if (!on && ['ip_cls', 'ip_score', 'origin_ip'].includes(colorBy.value)) { colorBy.value = 'class'; if (S.view) renderPlot(); }
}

/** Load model `m`'s answer at `conf` (it has predicted already), fill the plot columns, draw the tab. */
export async function refreshImageModel(m: ModelInfo, conf: number, list: () => ModelInfo[]): Promise<void> {
  models = list;
  bind();
  const my = ++seq;
  $req('m-sub').textContent = 'loading…';
  try {
    const r = await getJSON<ImageClasses>(`api/models/${encodeURIComponent(m.id)}/image-classes?conf=${conf}`);
    if (my !== seq) return;
    res = r;
    const P = pts();
    P.ip_cls = r.pred_cls; P.ip_score = r.pred_score; P.ip_names = r.names;
    syncColorBy();
    invalidateHover();
    renderImageModel(true);
    if (S.view && ['ip_cls', 'ip_score', 'origin_ip'].includes($req<HTMLSelectElement>('color-by').value)) renderPlot();
    void loadAgreement();
  } catch (e) {
    if (my !== seq) return;
    $req('m-warn').innerHTML = `<div class="m-note warn">${esc(errText(e))}</div>`;
    $req('m-sub').textContent = '';
  }
}

export function renderImageModel(redraw = false): void {
  if (curTab !== 'model' || !res) return;
  const r = res;
  $req('m-empty').classList.add('hidden');
  $req('m-grid').classList.add('hidden');
  $req('mi-grid').classList.remove('hidden');
  $req('m-run-box').classList.add('hidden');
  $req<HTMLInputElement>('m-conf').value = String(r.conf);
  $req('m-conf-v').textContent = r.conf.toFixed(2);
  const found = r.n_images - r.no_detection;
  const scored = r.pred_score.filter((_, i) => (r.pred_cls[i] ?? -1) >= 0);
  const mean = scored.length ? scored.reduce((a, b) => a + b, 0) / scored.length : 0;
  $req('m-sub').textContent = `${r.n_images.toLocaleString()} images · no labels: the model names each image`;
  const kpi = (v: string, l: string, title: string): string => `<div class="kpi" title="${esc(title)}"><b>${v}</b><span>${l}</span></div>`;
  $req('m-kpis').innerHTML = kpi(r.n_images ? pct(found / r.n_images) : '–', 'images with a detection %', `at confidence ≥ ${r.conf.toFixed(2)}`) +
    kpi(scored.length ? mean.toFixed(2) : '–', 'mean top confidence', 'mean score of the most confident detection per image') +
    kpi(String(r.images_per_class.filter((n) => n).length), 'classes found', 'predicted classes that are the top class of at least one image') +
    kpi(r.ms ? r.ms.toFixed(1) : '–', 'ms / image', 'mean inference time while predicting');
  $req('m-warn').innerHTML = '<div class="m-note">This dataset has no labels, so nothing is scored right or wrong: ' +
    'each image gets its <b>most confident detection</b> as its class.</div>';
  const order = r.names.map((_, c) => c).filter((c) => (r.images_per_class[c] ?? 0) || (r.detections_per_class[c] ?? 0))
    .sort((a, b) => (r.images_per_class[b] ?? 0) - (r.images_per_class[a] ?? 0));
  tbody($req<HTMLTableElement>('mi-class-table')).innerHTML = order.map((c) =>
    `<tr data-c="${c}" tabindex="0" role="button" title="load these images into the gallery">` +
    `<td><span class="cname"><span class="dot" style="background:${clsColor(c)}"></span>${esc(r.names[c] ?? '')}</span></td>` +
    `<td class="r">${r.images_per_class[c] ?? 0}</td><td class="r">${r.detections_per_class[c] ?? 0}</td></tr>`).join('') +
    (r.no_detection ? `<tr data-c="-1" tabindex="0" role="button"><td><span class="cname"><span class="dot" style="background:${THEME.ungrouped}"></span>nothing found</span></td>` +
      `<td class="r">${r.no_detection}</td><td class="r">–</td></tr>` : '');
  if (redraw) drawCharts(r, order);
  else MI_PLOTS.forEach((id) => { const el = document.getElementById(id) as Plotly.PlotlyHTMLElement | null; if (el?.data) Plotly.Plots.resize(id); });
}

function drawCharts(r: ImageClasses, order: number[]): void {
  const x = [...order.map((c) => r.names[c] ?? ''), 'nothing found'];
  const traces: Plotly.Data[] = r.by_origin && r.by_origin.length > 1
    ? r.by_origin.map((o, k) => ({ type: 'bar', name: o.alias, x, marker: { color: originColor(k) },
      y: [...order.map((c) => o.images_per_class[c] ?? 0), o.no_detection],
      hovertemplate: `${esc(o.alias)} · %{x}: %{y} images<extra></extra>` }))
    : [{ type: 'bar', x, y: [...order.map((c) => r.images_per_class[c] ?? 0), r.no_detection],
      marker: { color: [...order.map((c) => clsColor(c)), THEME.ungrouped] }, hovertemplate: '%{x}: %{y} images<extra></extra>' }];
  $req('mi-bar-sub').textContent = r.by_origin && r.by_origin.length > 1 ? '· per dataset' : '';
  void Plotly.react('mi-bar', traces, { ...base(), barmode: 'group', showlegend: traces.length > 1,
    xaxis: { ...ax(), tickangle: -30 }, yaxis: { ...ax(), title: { text: 'images' } }, margin: { l: 50, r: 10, t: 10, b: 90 } }, cfg);
  const scores = r.pred_score.filter((_, i) => (r.pred_cls[i] ?? -1) >= 0);
  void Plotly.react('mi-hist', [{ type: 'histogram', x: scores, xbins: { start: 0, end: 1, size: 0.05 }, marker: { color: THEME.accent },
    hovertemplate: '%{x}: %{y} images<extra></extra>' }],
  { ...base(), xaxis: { ...ax(), range: [0, 1], title: { text: 'top confidence' } }, yaxis: { ...ax(), title: { text: 'images' } },
    margin: { l: 50, r: 10, t: 10, b: 45 } }, cfg);
}

// ---------------------------------------------------------------- a second model
function fillVs(): void {
  const cur = res?.model.id;
  const others = models().filter((m) => m.id !== cur && m.predicted);
  const sel = $req<HTMLSelectElement>('mi-vs');
  const keep = sel.value;
  sel.innerHTML = '<option value="">pick a model…</option>' + others.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  if (others.some((m) => m.id === keep)) sel.value = keep;
  sel.disabled = !others.length;
  if (!others.length) $req('mi-agree-sub').textContent = '· run a second model on this dataset first';
}

async function loadAgreement(): Promise<void> {
  fillVs();
  const b = $req<HTMLSelectElement>('mi-vs').value;
  const my = ++agreeSeq;
  agreeRes = null;
  $req('mi-disagree').classList.add('hidden');
  if (!res || !b) { Plotly.purge('mi-agree'); if (b === '' && models().length > 1) $req('mi-agree-sub').textContent = ''; return; }
  try {
    const g = await getJSON<Agreement>(`api/image-models/agree?a=${encodeURIComponent(res.model.id)}&b=${encodeURIComponent(b)}&conf=${res.conf}`);
    if (my !== agreeSeq) return;
    agreeRes = g;
    $req('mi-agree-sub').textContent = g.agree == null ? '' :
      `· agree on ${pct(g.agree)} % of ${g.n.toLocaleString()} images · κ = ${g.kappa?.toFixed(2) ?? '–'} (${kappaWord(g.kappa)})`;
    const dis = $req('mi-disagree');
    dis.classList.toggle('hidden', !g.disagree_ids.length);
    dis.textContent = `Show the ${g.disagree_ids.length.toLocaleString()} images they disagree on`;
    const rows = g.rows.map((_, i) => i).filter((i) => g.table[i]?.some((v) => v));
    const cols = g.cols.map((_, j) => j).filter((j) => g.table.some((row) => row[j]));
    const z = rows.map((i) => cols.map((j) => g.table[i]?.[j] ?? 0));
    const mx = Math.max(1, ...z.flat());
    void Plotly.react('mi-agree', [{ type: 'heatmap', z: z.map((row) => row.map((v) => v / mx)), text: z.map((row) => row.map(String)) as unknown as string[],
      x: cols.map((j) => g.cols[j] ?? ''), y: rows.map((i) => g.rows[i] ?? ''), customdata: z as unknown as Plotly.Datum[],
      texttemplate: '%{text}', colorscale: [[0, THEME.surface2], [0.02, THEME.surface3], [1, THEME.accent]], zmin: 0, zmax: 1, showscale: false,
      hovertemplate: `${esc(g.a.name)}: <b>%{y}</b><br>${esc(g.b.name)}: <b>%{x}</b><br>%{text} images<extra></extra>` } as Plotly.Data],
    { ...base(), xaxis: { ...ax(), tickangle: -30, title: { text: g.b.name } }, yaxis: { ...ax(), autorange: 'reversed', title: { text: g.a.name } },
      margin: { l: 120, r: 10, t: 10, b: 100 } }, cfg).then(hookAgree);
  } catch (e) {
    if (my === agreeSeq) $req('mi-agree-sub').textContent = `· ${errText(e)}`;
  }
}

// cell click: the images with that (a, b) pair (Plotly's .on exists once the plot is drawn: hooked once)
let agreeHooked = false;
function hookAgree(): void {
  const el = document.getElementById('mi-agree') as Plotly.PlotlyHTMLElement | null;
  if (agreeHooked || !el?.on) return;
  agreeHooked = true;
  el.on('plotly_click', (d) => {
    const p = d.points[0], g = agreeRes;
    if (!p || !g) return;
    const ai = g.rows.indexOf(String(p.y)), bi = g.cols.indexOf(String(p.x));
    if (ai < 0 || bi < 0) return;
    void pairIds(g, ai, bi).then((ids) => setSelection(ids, `${g.a.name}: ${g.rows[ai]} · ${g.b.name}: ${g.cols[bi]}`));
  });
}

function kappaWord(k: number | null): string {
  if (k == null) return 'n/a';
  return k >= 0.8 ? 'almost perfect' : k >= 0.6 ? 'substantial' : k >= 0.4 ? 'moderate' : k >= 0.2 ? 'fair' : k > 0 ? 'slight' : 'none';
}

function bind(): void {
  if (bound) return;
  bound = true;
  $req('mi-vs').onchange = () => { void loadAgreement(); };
  $req('mi-disagree').onclick = () => {
    if (agreeRes) setSelection(agreeRes.disagree_ids, `${agreeRes.a.name} vs ${agreeRes.b.name}: disagree`);
  };
  $req('mi-class-table').addEventListener('click', (e) => {
    const row = closestOf(e, 'tr[data-c]');
    if (!row || !res) return;
    const c = +(row.dataset['c'] ?? NaN), r = res;
    const ids = pts().id.filter((id) => (r.pred_cls[id] ?? -1) === c);
    setSelection(ids, `model ${r.model.name}: ${c < 0 ? 'nothing found' : r.names[c] ?? c}`);
  });
}

/** Points where model a said rows[ai] and model b said cols[bi] (b's per-image answer fetched once, cached). */
const bCache = new Map<string, ImageClasses>();
async function pairIds(g: Agreement, ai: number, bi: number): Promise<number[]> {
  const r = res;
  if (!r) return [];
  const key = `${g.b.id}|${g.conf}`;
  let rb = bCache.get(key);
  if (!rb) {
    rb = await getJSON<ImageClasses>(`api/models/${encodeURIComponent(g.b.id)}/image-classes?conf=${g.conf}`);
    bCache.set(key, rb);
  }
  const na = g.rows.length - 1, nb = g.cols.length - 1, b = rb;
  const ids: number[] = [];
  for (let id = 0; id < r.pred_cls.length; id++) {
    const a0 = r.pred_cls[id] ?? -1, b0 = b.pred_cls[id] ?? -1;
    if ((a0 < 0 ? na : a0) === ai && (b0 < 0 ? nb : b0) === bi) ids.push(id);
  }
  return ids;
}
