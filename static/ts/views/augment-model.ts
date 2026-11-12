// Augment › Model: a detection model on each step of the recipe at full chance (and the whole recipe), scored against
// the labels as the step moved them (routes/augment.py → data/augment_model.py). A step the model loses much on is
// worth training with; one it already handles adds little.

import { errText, getJSON, postJSON } from '../core/api.js';
import { $, esc } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import type { Job } from '../types.js';
import type { ModelsPayload } from '../types-views.js';
import { AG, AUG_HOOKS } from './augment.js';

interface Row { key: string; label: string; map50?: number; map?: number; p?: number; r?: number; drop?: number; kept?: number | null; handled?: boolean }
interface Result { n: number; conf: number; clean: Row; steps: Row[]; all?: Row | null; recipe: Row | null; verdict: string; model: { id: string; name: string }; recipe_used?: unknown }

const M = { models: [] as { id: string; name: string }[], model: '', n: 100, res: null as Result | null, busy: false, err: '', progress: '', loaded: false };
const pc = (v: number | undefined): string => (v === undefined ? '–' : `${(v * 100).toFixed(1)}`);

async function load(): Promise<void> {
  try {
    M.models = (await getJSON<ModelsPayload>('api/models')).models.filter((m) => m.family !== 'hf').map((m) => ({ id: m.id, name: m.name }));
  } catch { M.models = []; }
  if (!M.model && M.models[0]) M.model = M.models[0].id;
  M.loaded = true;
}

function html(): string {
  if (!M.models.length) return '<p class="faint">No detection model yet. Add one in Settings › Models, then come back: this view runs it on each step of the recipe.</p>';
  const head = `<div class="ag-target"><label class="m-lbl" for="agm-m">Model</label><select id="agm-m" data-agm="m">${M.models.map((m) => `<option value="${esc(m.id)}" ${m.id === M.model ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select>` +
    `<label class="m-lbl" for="agm-n">images</label><select id="agm-n" data-agm="n">${[50, 100, 200, 300].map((n) => `<option ${n === M.n ? 'selected' : ''}>${n}</option>`).join('')}</select>` +
    `<button type="button" class="primary" data-agm-act="run" ${M.busy ? 'disabled' : ''}>Test the model</button><span class="faint" id="agm-st">${esc(M.busy ? M.progress || 'Working…' : '')}</span></div>`;
  if (M.err) return `${head}<div class="empty">${esc(M.err)}</div>`;
  const r = M.res;
  if (!r) return `${head}<p class="faint">Runs the model on images of every class with each step at full chance, and scores it against the labels the step moved. A step that costs it a lot is worth training with.</p>`;
  const row = (x: Row): string => `<tr><td>${esc(x.label)}</td><td class="num">${pc(x.map50)}</td><td class="num">${pc(x.p)}</td><td class="num">${pc(x.r)}</td>` +
    `<td class="num${x.handled === false ? ' down' : ''}">${x.drop === undefined ? '' : `−${pc(x.drop)}`}</td><td>${x.handled === undefined ? '' : x.handled ? 'handles it' : 'struggles'}</td></tr>`;
  return head + `<div class="ag-verdict">${esc(r.verdict)}</div>` +
    `<table class="list-table ag-tbl"><thead><tr><th>Images</th><th>mAP50</th><th>P</th><th>R</th><th>Drop (points)</th><th></th></tr></thead><tbody>${row(r.clean)}${r.steps.map(row).join('')}${r.all || r.recipe ? `<tr class="ag-mix"><th colspan="6">Mixed</th></tr>${r.all ? row(r.all) : ''}${r.recipe ? row(r.recipe) : ''}` : ''}</tbody></table>` +
    `<p class="faint">${esc(r.model.name)} · ${r.n} images · P and R at confidence ${r.conf}. "Handles it" = keeps 95 % of its mAP50 without augmentation.</p>`;
}

function paint(): void {
  const host = $('ag-main');
  if (!host || AG.view !== 'model') return;
  if (!M.loaded) { host.innerHTML = '<div class="empty">Loading…</div>'; void load().then(paint); return; }
  host.innerHTML = html();
}

async function run(): Promise<void> {
  M.busy = true;
  M.err = '';
  paint();
  try {
    const r = await postJSON<{ ready: boolean; key: string; result?: Result; job_id?: number }>('api/augment/model', { recipe: AG.recipe, model: M.model, n: M.n, seed: AG.seed });
    if (r.ready) M.res = r.result ?? null;
    else if (r.job_id !== undefined) {
      await waitJob<Job>(r.job_id, (j) => { M.progress = `${j.stage ?? ''}${j.total ? ` ${j.done}/${j.total}` : ''}`; const s = $('agm-st'); if (s) s.textContent = M.progress; });
      M.res = (await getJSON<{ result: Result }>(`api/augment/coverage/result?key=${encodeURIComponent(r.key)}`)).result;
    }
  } catch (e) { M.err = errText(e); } finally { M.busy = false; }
  paint();
}

let bound = false;
export function initAugModel(): void {
  AUG_HOOKS.push(() => { if (AG.view === 'model') paint(); });
  if (bound) return;
  bound = true;
  const main = $('tab-aug');
  main?.addEventListener('change', (e) => {
    const t = e.target as HTMLSelectElement;
    if (t.dataset['agm'] === 'm') M.model = t.value;
    else if (t.dataset['agm'] === 'n') M.n = Number(t.value);
  });
  main?.addEventListener('click', (e) => {
    if ((e.target as Element).closest('[data-agm-act="run"]')) void run();
  });
}
