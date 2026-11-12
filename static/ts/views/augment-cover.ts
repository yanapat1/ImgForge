// Augment › Coverage: does this dataset, with the recipe, look like another set of images (another place, a new camera)?
// Pick the images to compare against (an upload, or a folder of a Data Center bucket), run the job
// (routes/augment.py on data/augment_run.py + augment_cover.py), and read: the share of those images covered before
// and after the recipe, how far apart the sets are, which steps helped, the images still not covered and what they
// differ in, with steps to add. A second saved recipe can be run against the same target for an A/B.

import { api, errText, getJSON, postJSON } from '../core/api.js';
import { $, esc } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { THEME } from '../core/theme.js';
import { toast } from '../core/toast.js';
import type { Job } from '../types.js';
import { AG, AUG_HOOKS, augName, renderRecipe } from './augment.js';
import type { AugRecipe, AugStep } from './augment.js';
import { base, cfg } from './chart-util.js';
import { openDialog } from './dialog.js';

declare const Plotly: typeof import('plotly.js');

interface Target { id: string; name: string; n: number; skipped?: string[] }
interface Gap { key: string; z: number; text: string; step: AugStep }
interface Report {
  key: string; feature: string; target_label: string; parts: { train: string; aug: string; target: string };
  n: { train: number; aug: number; target: number }; coverage: { before: number; after: number; gain: number };
  frechet: { before: number | null; after: number | null }; domain_auc: { before: number | null; after: number | null };
  off_domain: number | null; rescued: number; n_uncovered: number; uncovered: number[]; verdict: string;
  credit: { t: string; label: string; rescued: number; share: number }[]; gap?: Gap[]; skipped: string[];
  target_names: Record<string, string>; nearest: { target: number; train: number; aug: number | null; via: string }[];
  map: { train: { i: number[]; xy: number[][] }; aug: { i: number[]; xy: number[][] }; target: { i: number[]; xy: number[][]; covered: boolean[] } };
  recipe: AugRecipe;
}
type TargetSpec = { kind: 'upload'; id: string; label: string } | { kind: 'dc'; bucket: string; path: string; label: string };

const C = {
  targets: [] as Target[], target: null as TargetSpec | null, quality: 'quick' as 'quick' | 'deep', m: 300, k: 2,
  res: null as Report | null, ab: null as Report | null, abName: '', busy: false, err: '', progress: '', loaded: false,
};
const pct = (v: number | null | undefined): string => (v === null || v === undefined ? '–' : `${Math.round(v * 100)}%`);
const fmt = (v: number | null | undefined, d = 3): string => (v === null || v === undefined ? '–' : v.toFixed(d));
const thumbUrl = (part: string, i: number): string => `api/augment/thumb?part=${encodeURIComponent(part)}&i=${i}`;

async function loadTargets(): Promise<void> {
  try { C.targets = (await getJSON<{ targets: Target[] }>('api/augment/targets')).targets; } catch { C.targets = []; }
  C.loaded = true;
}

function targetRow(): string {
  const cur = C.target, val = cur ? (cur.kind === 'upload' ? `u:${cur.id}` : `dc:${cur.bucket}|${cur.path}`) : '';
  const dcOpt = cur?.kind === 'dc' ? `<option value="${esc(val)}" selected>${esc(cur.label)}</option>` : '';
  return `<div class="ag-target"><label class="m-lbl" for="agv-t">Compare against</label><select id="agv-t" data-agv="t"><option value="">pick the new images…</option>` +
    C.targets.map((t) => `<option value="u:${esc(t.id)}" ${val === `u:${t.id}` ? 'selected' : ''}>${esc(t.name)} · ${t.n} images</option>`).join('') + dcOpt + '</select>' +
    `<button type="button" class="sm" data-agv-act="upload">Upload images…</button><button type="button" class="sm" data-agv-act="dc">Data Center folder…</button>` +
    `<label class="m-lbl" for="agv-q" title="Quick: colour + edge features on the CPU, in seconds. Deep: the session's deep features (DINOv2 / CLIP), better at what things look like; slow without a GPU.">Features</label>` +
    `<select id="agv-q" data-agv="q"><option value="quick" ${C.quality === 'quick' ? 'selected' : ''}>quick</option><option value="deep" ${C.quality === 'deep' ? 'selected' : ''}>deep</option></select>` +
    `<label class="m-lbl" for="agv-m">images</label><select id="agv-m" data-agv="m">${[100, 300, 600, 1000].map((n) => `<option ${n === C.m ? 'selected' : ''}>${n}</option>`).join('')}</select>` +
    `<label class="m-lbl" for="agv-k">× variants</label><select id="agv-k" data-agv="k">${[1, 2, 4].map((n) => `<option ${n === C.k ? 'selected' : ''}>${n}</option>`).join('')}</select>` +
    `<button type="button" class="primary" data-agv-act="run" ${C.busy || !C.target ? 'disabled' : ''}>${C.res ? 'Run again' : 'Check coverage'}</button>` +
    `<span class="faint" id="agv-st">${esc(C.busy ? C.progress || 'Working…' : '')}</span></div>` +
    `<div class="ag-drop" id="agv-drop">Or drop images of the new place / camera here</div>`;
}

function cards(r: Report): string {
  const card = (label: string, v: string, sub: string, cls = ''): string => `<div class="ag-card"><div class="faint">${label}</div><div class="big ${cls}">${v}</div><div class="faint">${sub}</div></div>`;
  const g = r.coverage.gain;
  return `<div class="ag-cards">${card('Coverage of the new images', `${pct(r.coverage.before)} → ${pct(r.coverage.after)}`, `${g >= 0 ? '+' : ''}${Math.round(g * 100)} points with the recipe`, g > 0.02 ? 'up' : '')}` +
    card('Distance (Fréchet)', `${fmt(r.frechet.before, 2)} → ${fmt(r.frechet.after, 2)}`, 'lower = the sets are closer', (r.frechet.after ?? 0) < (r.frechet.before ?? 0) ? 'up' : '') +
    card('Tell-apart score (AUC)', `${fmt(r.domain_auc.before, 2)} → ${fmt(r.domain_auc.after, 2)}`, '0.5 = cannot tell them apart', (r.domain_auc.after ?? 1) < (r.domain_auc.before ?? 1) ? 'up' : '') +
    card('Unreal variants', pct(r.off_domain), 'augmented images like neither set', (r.off_domain ?? 0) >= 0.2 ? 'down' : '') + '</div>';
}

function resultHTML(r: Report): string {
  const stale = JSON.stringify(r.recipe) !== JSON.stringify(AG.recipe) ? '<p class="notice">The recipe changed since this run. Run again to update it.</p>' : '';
  const gaps = r.gap ?? [];
  const steps = r.credit.length ? `<div class="ag-bars">${r.credit.map((c) => `<div class="ag-barrow"><span>${esc(c.label)}</span><span><i style="width:${Math.round(c.share * 100)}%"></i></span><span class="num">${c.rescued}</span></div>`).join('')}</div>`
    : '<p class="faint">No new image is covered thanks to an augmented one.</p>';
  const unc = r.uncovered.slice(0, 60);
  return stale + `<div class="ag-verdict">${esc(r.verdict)}</div>` + cards(r) +
    (C.ab ? abHTML(r, C.ab) : '') +
    '<div id="ag-cov-plot" role="img" aria-label="Map of the dataset, the augmented images and the new images"></div>' +
    `<h4 class="ag-sec">Which steps helped <span class="faint">(new images covered thanks to an augmented image made with it)</span></h4>${steps}` +
    (gaps.length ? `<h4 class="ag-sec">What the images still not covered differ in</h4><div class="ag-bars">${gaps.map((g, i) =>
      `<div class="ag-barrow"><span>${esc(g.text)}</span><span></span><button type="button" class="sm" data-agv-act="add-gap" data-i="${i}" title="Add ${esc(augName(g.step.t))} to the recipe">+ ${esc(augName(g.step.t))}</button></div>`).join('')}</div>` : '') +
    `<h4 class="ag-sec">Still not covered <span class="faint">${r.n_uncovered} of ${r.n.target}</span></h4>` +
    (unc.length ? `<div class="ag-strip">${unc.map((t, j) => `<figure data-agv-act="near" data-j="${j}" tabindex="0" title="Click: beside its nearest dataset and augmented image"><img loading="lazy" alt="${esc(r.target_names[String(t)] ?? 'target image')}" src="${thumbUrl(r.parts.target, t)}"><figcaption>${esc(r.target_names[String(t)] ?? '')}</figcaption></figure>`).join('')}</div>` : '<p class="faint">Every new image is covered.</p>') +
    (r.skipped.length ? `<p class="faint">Could not read: ${esc(r.skipped.slice(0, 10).join(', '))}${r.skipped.length > 10 ? '…' : ''}</p>` : '') +
    `<p class="faint">Features: ${esc(r.feature)} · ${r.n.train} dataset images, ${r.n.aug} augmented, ${r.n.target} new. A new image counts as covered when it is as close to one of them as the dataset's own images are to each other (95 % of them).</p>` +
    `<div class="ag-target"><label class="m-lbl" for="agv-ab">Compare with a saved recipe</label><select id="agv-ab" data-agv="ab"><option value="">…</option>${AG.saved.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join('')}</select></div>`;
}

function abHTML(a: Report, b: Report): string {
  const row = (label: string, x: string, y: string): string => `<tr><td>${label}</td><td class="num">${x}</td><td class="num">${y}</td></tr>`;
  return `<table class="list-table ag-tbl"><thead><tr><th></th><th>This recipe</th><th>${esc(C.abName)}</th></tr></thead><tbody>` +
    row('Coverage after', pct(a.coverage.after), pct(b.coverage.after)) + row('Fréchet after', fmt(a.frechet.after, 2), fmt(b.frechet.after, 2)) +
    row('AUC after', fmt(a.domain_auc.after, 2), fmt(b.domain_auc.after, 2)) + row('Unreal variants', pct(a.off_domain), pct(b.off_domain)) + '</tbody></table>';
}

function drawMap(r: Report): void {
  const el = $('ag-cov-plot');
  if (!el) return;
  const tr = (name: string, xy: number[][], color: string, extra: Partial<Plotly.PlotData> = {}): Partial<Plotly.PlotData> =>
    ({ type: 'scattergl', mode: 'markers', name, x: xy.map((p) => p[0] ?? 0), y: xy.map((p) => p[1] ?? 0), marker: { size: 5, color, opacity: 0.7 }, hoverinfo: 'name', ...extra });
  const t = r.map.target, cov = t.xy.filter((_, i) => t.covered[i]), unc = t.xy.filter((_, i) => !t.covered[i]);
  const data = [tr('dataset', r.map.train.xy, THEME.muted), tr('augmented', r.map.aug.xy, THEME.accent),
    tr('new, covered', cov, THEME.highlight, { marker: { size: 7, color: THEME.highlight } }),
    tr('new, not covered', unc, THEME.danger, { marker: { size: 9, color: 'rgba(0,0,0,0)', line: { width: 2, color: THEME.danger } } })];
  void Plotly.react(el, data as Plotly.Data[], { ...base(), margin: { l: 10, r: 10, t: 10, b: 10 }, xaxis: { visible: false }, yaxis: { visible: false }, legend: { orientation: 'h', font: { color: THEME.text } } }, cfg);
}

export function paintCoverage(): void {
  const host = $('ag-main');
  if (!host || AG.view !== 'coverage') return;
  if (!C.loaded) { void loadTargets().then(paintCoverage); host.innerHTML = '<div class="empty">Loading…</div>'; return; }
  host.innerHTML = targetRow() + (C.err ? `<div class="empty">${esc(C.err)}</div>` : C.res ? resultHTML(C.res)
    : '<p class="faint">Pick images from the new place or camera (upload them, or a Data Center folder). The check embeds this dataset, its augmented copies and the new images, and says how much of the new images the recipe covers, and what is still missing.</p>');
  if (C.res && !C.err) drawMap(C.res);
  bindDrop();
}

async function run(recipe: AugRecipe = AG.recipe): Promise<Report | null> {
  if (!C.target) return null;
  const t = C.target, target = t.kind === 'upload' ? { kind: t.kind, id: t.id } : { kind: t.kind, bucket: t.bucket, path: t.path };
  const r = await postJSON<{ ready: boolean; key: string; result?: Report; job_id?: number }>('api/augment/coverage',
    { recipe, target, quality: C.quality, m: C.m, k: C.k, seed: AG.seed });
  if (r.ready) return r.result ?? null;
  if (r.job_id === undefined) return null;
  await waitJob<Job>(r.job_id, (j) => {
    C.progress = j.stage ? `${j.stage}${j.total ? ` ${j.done}/${j.total}` : ''}` : 'Queued…';
    const s = $('agv-st');
    if (s) s.textContent = C.progress;
  });
  return (await getJSON<{ result: Report }>(`api/augment/coverage/result?key=${encodeURIComponent(r.key)}`)).result;
}

async function runMain(): Promise<void> {
  C.busy = true;
  C.err = '';
  C.ab = null;
  paintCoverage();
  try { C.res = await run(); } catch (e) { C.err = errText(e); } finally { C.busy = false; }
  paintCoverage();
}

async function upload(files: FileList | File[]): Promise<void> {
  const list = [...files].filter((f) => /\.(jpe?g|png|bmp|webp|tiff?)$/i.test(f.name));
  if (!list.length) { toast('Pick image files', 'err'); return; }
  if (list.length > 1000) { toast('At most 1000 images at once', 'err'); return; }
  const fd = new FormData();
  list.forEach((f) => fd.append('files', f, f.name));
  fd.append('name', list.length === 1 ? (list[0]?.name ?? 'image') : `${list.length} images, ${new Date().toLocaleDateString()}`);
  C.progress = `Uploading ${list.length} images…`;
  C.busy = true;
  paintCoverage();
  try {
    const t = await (await api('api/augment/targets', { method: 'POST', body: fd })).json() as Target;
    await loadTargets();
    C.target = { kind: 'upload', id: t.id, label: t.name };
    toast(`${t.n} images ready to compare${t.skipped?.length ? ` (${t.skipped.length} left out)` : ''}`);
  } catch (e) { toast(errText(e), 'err'); } finally { C.busy = false; }
  paintCoverage();
}

function pickFiles(): void {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.accept = 'image/*';
  inp.onchange = () => { if (inp.files?.length) void upload(inp.files); };
  inp.click();
}

async function pickDc(): Promise<void> {
  let buckets: { id: string; name: string; kind: string }[] = [];
  try {
    const r = await getJSON<{ id: string; name: string; kind: string }[] | { buckets: { id: string; name: string; kind: string }[] }>('api/dc/buckets');
    buckets = Array.isArray(r) ? r : r.buckets;
  } catch (e) { toast(errText(e), 'err'); return; }
  if (!buckets.length) { toast('No Data Center bucket yet', 'err'); return; }
  let spec: TargetSpec | null = null;
  await openDialog({
    title: 'Compare against a Data Center folder', ok: 'Use this folder', focus: '#agv-b',
    body: `<label class="fld"><span>Bucket</span><select id="agv-b">${buckets.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join('')}</select></label>` +
      '<label class="fld"><span>Folder in it (empty = all of it)</span><input type="text" class="txt" id="agv-p" placeholder="e.g. camera2/images"></label>' +
      '<p class="hint">Images in the folder and its subfolders, up to 2000. A streamed S3 folder must be downloaded first.</p>',
    onOk: (root) => {
      const b = root.querySelector<HTMLSelectElement>('#agv-b'), p = root.querySelector<HTMLInputElement>('#agv-p')?.value.trim() ?? '';
      const name = b?.selectedOptions[0]?.textContent ?? '';
      spec = { kind: 'dc', bucket: b?.value ?? '', path: p, label: `${name}${p ? ` / ${p}` : ''}` };
      return null;
    },
  });
  if (spec) { C.target = spec; paintCoverage(); }
}

function bindDrop(): void {
  const d = $('agv-drop');
  if (!d) return;
  d.addEventListener('dragover', (e) => { e.preventDefault(); d.classList.add('over'); });
  d.addEventListener('dragleave', () => d.classList.remove('over'));
  d.addEventListener('drop', (e) => { e.preventDefault(); d.classList.remove('over'); if (e.dataTransfer?.files.length) void upload(e.dataTransfer.files); });
}

async function nearest(j: number): Promise<void> {
  const r = C.res, n = r?.nearest[j];
  if (!r || !n) return;
  const fig = (src: string, cap: string): string => `<figure class="ag-cell"><img alt="${esc(cap)}" src="${src}"><figcaption>${esc(cap)}</figcaption></figure>`;
  const augPart = r.parts.aug, k = Math.max(1, Math.round(r.n.aug / Math.max(1, r.n.train)));
  await openDialog({
    title: r.target_names[String(n.target)] ?? 'Not covered', ok: 'Close', wide: true, onOk: () => null,
    body: `<div class="ag-row" style="--ag-cols:3">${fig(thumbUrl(r.parts.target, n.target), 'the new image')}${fig(thumbUrl(r.parts.train, n.train), 'nearest dataset image')}` +
      (n.aug !== null ? fig(thumbUrl(augPart, n.aug), 'nearest augmented image') : '') + `</div><p class="hint">Still farther from both than the dataset's images are from each other. ${k > 1 ? `${k} variants per dataset image.` : ''}</p>`,
  });
}

let bound = false;
export function initAugCover(): void {
  AUG_HOOKS.push(() => { if (AG.view === 'coverage') paintCoverage(); });
  if (bound) return;
  bound = true;
  const main = $('tab-aug');
  main?.addEventListener('change', (e) => {
    const t = e.target as HTMLSelectElement, k = t.dataset['agv'];
    if (!k) return;
    if (k === 't') {
      const v = t.value;
      if (v.startsWith('u:')) { const x = C.targets.find((y) => y.id === v.slice(2)); C.target = x ? { kind: 'upload', id: x.id, label: x.name } : null; } else if (!v) C.target = null;
      paintCoverage();
    } else if (k === 'q') C.quality = t.value === 'deep' ? 'deep' : 'quick';
    else if (k === 'm') C.m = Number(t.value);
    else if (k === 'k') C.k = Number(t.value);
    else if (k === 'ab' && t.value !== '') {
      const s = AG.saved[Number(t.value)];
      if (!s || !C.res) return;
      C.abName = s.name;
      C.busy = true;
      void run(s.recipe).then((r) => { C.ab = r; }).catch((err: unknown) => toast(errText(err), 'err')).finally(() => { C.busy = false; paintCoverage(); });
    }
  });
  main?.addEventListener('click', (e) => {
    const b = (e.target as Element).closest<HTMLElement>('[data-agv-act]');
    if (!b) return;
    const k = b.dataset['agvAct'];
    if (k === 'run') void runMain();
    else if (k === 'upload') pickFiles();
    else if (k === 'dc') void pickDc();
    else if (k === 'near') void nearest(Number(b.dataset['j']));
    else if (k === 'add-gap') {
      const g = C.res?.gap?.[Number(b.dataset['i'])];
      if (!g) return;
      AG.recipe.steps.push(structuredClone(g.step));
      renderRecipe();
      AUG_HOOKS.forEach((f) => f());
      toast(`${augName(g.step.t)} added. Run again to see the new cover.`);
    }
  });
}

/** The newest report, for the assistant's page context. */
export const coverageSummary = (): Record<string, unknown> | null => (C.res ? { verdict: C.res.verdict, coverage: C.res.coverage, gaps: (C.res.gap ?? []).map((g) => g.text) } : null);
