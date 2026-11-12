// The Augment tab's checks: Boxes (do the labels survive the recipe: lost, too small, cut at the edge, per class and
// per step) and Coverage (augment-cover.ts adds it). Each runs as a server job (routes/augment.py) and is cached by
// the recipe, so switching back and forth costs nothing.

import { api, errText, getJSON, postJSON } from '../core/api.js';
import { $, esc } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import type { Job } from '../types.js';
import { toast } from '../core/toast.js';
import { AG, AUG_HOOKS, augName } from './augment.js';

interface BoxRow { cls: number; name: string; in: number; kept: number; lost: number; tiny: number; edge: number; lost_pct: number; tiny_pct: number }
interface StepRow { t: string; label: string; variants: number; in: number; lost: number; tiny: number; lost_pct: number; tiny_pct: number }
interface BoxCheck {
  images: number; variants: number; boxes_in: number; kept: number; lost: number; tiny: number; edge: number; lost_pct: number; tiny_pct: number;
  classes: BoxRow[]; transforms: StepRow[]; warnings: string[]; examples: { name: string; seed: number; lost: number; tiny: number; applied: string[] }[];
  min_px: number; train_size: number; k: number;
}

const B = { m: 200, k: 2, minPx: 8, size: 640, res: null as BoxCheck | null, key: '', busy: false, err: '', progress: '', forRecipe: '' };
const sig = (): string => JSON.stringify(AG.recipe);

function boxesHTML(): string {
  const opts = (id: string, cur: number, vals: number[], label: string): string => `<label class="m-lbl" for="${id}">${label}</label><select id="${id}" data-agc="${id}">${vals.map((v) => `<option ${v === cur ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
  const head = `<div class="ag-target">${opts('agc-m', B.m, [50, 100, 200, 500], 'Images')}${opts('agc-k', B.k, [1, 2, 4], 'variants each')}` +
    `${opts('agc-size', B.size, [416, 640, 1024, 1280], 'train size')}${opts('agc-min', B.minPx, [4, 8, 12, 16], 'smallest box px')}` +
    `<button type="button" class="primary" data-agc-act="box-run" ${B.busy ? 'disabled' : ''}>${B.res && B.forRecipe === sig() ? 'Check again' : 'Check boxes'}</button><span class="faint">${esc(B.busy ? B.progress || 'Working…' : '')}</span></div>`;
  if (B.err) return `${head}<div class="empty">${esc(B.err)}</div>`;
  const r = B.res;
  if (!r) return `${head}<p class="faint">Runs the recipe on images of every class and counts what happens to their boxes: lost (less than 30 % left inside), too small to learn at the train size, or cut at the edge.</p>`;
  const stale = B.forRecipe !== sig() ? '<p class="notice">The recipe changed since this check. Check again to update it.</p>' : '';
  const card = (label: string, v: string, sub: string, bad: boolean): string => `<div class="ag-card"><div class="faint">${label}</div><div class="big${bad ? ' down' : ''}">${v}</div><div class="faint">${sub}</div></div>`;
  const bar = (pct: number, warn: boolean): string => `<span><i class="${warn ? 'warn' : ''}" style="width:${Math.min(100, pct)}%"></i></span>`;
  return head + stale +
    `<div class="ag-cards">${card('Boxes kept', `${(100 - r.lost_pct).toFixed(1)}%`, `${r.kept.toLocaleString()} of ${r.boxes_in.toLocaleString()} in ${r.variants.toLocaleString()} variants`, r.lost_pct >= 5)}` +
    card('Lost', `${r.lost_pct}%`, `${r.lost.toLocaleString()} boxes`, r.lost_pct >= 5) +
    card(`Under ${r.min_px} px at ${r.train_size}`, `${r.tiny_pct}%`, `${r.tiny.toLocaleString()} boxes`, r.tiny_pct >= 10) +
    card('Newly at the edge', r.edge.toLocaleString(), 'cut by a crop or a shift', false) + '</div>' +
    (r.warnings.length ? `<div class="ag-verdict">${r.warnings.map((w) => `<div>${esc(w)}</div>`).join('')}</div>` : '<div class="ag-verdict">No step loses many boxes. The labels survive this recipe.</div>') +
    `<h4 class="ag-sec">By step <span class="faint">(in the variants each one ran on)</span></h4><div class="ag-bars">${r.transforms.map((t) =>
      `<div class="ag-barrow" title="${esc(`${t.variants} variants · ${t.lost} lost · ${t.tiny} too small`)}"><span>${esc(t.label)}</span>${bar(t.lost_pct + t.tiny_pct, t.lost_pct >= 5 || t.tiny_pct >= 10)}<span class="num">${t.lost_pct}% · ${t.tiny_pct}%</span></div>`).join('') || '<p class="faint">No step ran.</p>'}</div>` +
    `<h4 class="ag-sec">By class</h4><table class="list-table ag-tbl"><thead><tr><th>Class</th><th>Boxes</th><th>Lost</th><th>Too small</th><th>At the edge</th></tr></thead><tbody>${r.classes.map((c) =>
      `<tr><td>${esc(c.name)}</td><td class="num">${c.in.toLocaleString()}</td><td class="num${c.lost_pct >= 10 ? ' down' : ''}">${c.lost_pct}%</td><td class="num">${c.tiny_pct}%</td><td class="num">${c.edge}</td></tr>`).join('')}</tbody></table>` +
    (r.examples.length ? `<h4 class="ag-sec">Examples that lost boxes</h4><div class="ag-strip">${r.examples.map((x, i) => `<figure data-agc-act="box-ex" data-i="${i}" tabindex="0" title="${esc(x.applied.map(augName).join(', '))}"><span class="ag-exph" data-ex="${i}" aria-hidden="true"></span><figcaption>${esc(x.name.split('/').pop() ?? x.name)} · −${x.lost}${x.tiny ? ` · ${x.tiny} tiny` : ''}</figcaption></figure>`).join('')}</div>` : '');
}

async function exampleThumbs(): Promise<void> {
  const r = B.res;
  if (!r) return;
  await Promise.all(r.examples.map(async (x, i) => {
    const ph = document.querySelector<HTMLElement>(`#ag-main [data-ex="${i}"]`);
    if (!ph) return;
    try {
      const res = await api('api/augment/one', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ recipe: AG.recipe, name: x.name, seed: x.seed }) });
      const img = document.createElement('img');
      img.alt = `${x.name}, augmented`;
      img.src = URL.createObjectURL(await res.blob());
      ph.replaceWith(img);
    } catch { /* the placeholder stays; the caption says which image */ }
  }));
}

async function runBoxes(): Promise<void> {
  B.busy = true;
  B.err = '';
  B.progress = '';
  paint();
  const want = sig();
  try {
    const r = await postJSON<{ ready: boolean; key: string; result?: BoxCheck; job_id?: number }>('api/augment/boxes', { recipe: AG.recipe, m: B.m, k: B.k, seed: AG.seed, min_px: B.minPx, train_size: B.size });
    if (!r.ready && r.job_id !== undefined) {
      await waitJob<Job>(r.job_id, (j) => { B.progress = j.total ? `${j.done} of ${j.total} images` : 'Queued…'; const s = document.querySelector('#ag-main .ag-target .faint'); if (s) s.textContent = B.progress; });
      B.res = (await getJSON<{ result: BoxCheck }>(`api/augment/boxes/result?key=${encodeURIComponent(r.key)}`)).result;
    } else B.res = r.result ?? null;
    B.key = r.key;
    B.forRecipe = want;
  } catch (e) { B.err = errText(e); } finally { B.busy = false; }
  paint();
}

function paint(): void {
  const host = $('ag-main');
  if (!host || AG.view !== 'boxes') return;
  host.innerHTML = boxesHTML();
  void exampleThumbs();
}

let bound = false;
export function initAugCheck(): void {
  AUG_HOOKS.push(() => { if (AG.view === 'boxes') paint(); });
  if (bound) return;
  bound = true;
  const main = $('tab-aug');
  main?.addEventListener('change', (e) => {
    const t = e.target as HTMLSelectElement, k = t.dataset['agc'];
    if (k === 'agc-m') B.m = Number(t.value);
    else if (k === 'agc-k') B.k = Number(t.value);
    else if (k === 'agc-size') B.size = Number(t.value);
    else if (k === 'agc-min') B.minPx = Number(t.value);
  });
  main?.addEventListener('click', (e) => {
    const b = (e.target as Element).closest<HTMLElement>('[data-agc-act]');
    if (!b) return;
    const k = b.dataset['agcAct'];
    if (k === 'box-run') void runBoxes();
    if (k === 'box-ex') {
      const x = B.res?.examples[Number(b.dataset['i'])];
      const img = b.querySelector('img');
      if (x && img?.src) window.open(img.src, '_blank', 'noopener');
      else if (x) toast(x.name);
    }
  });
}
