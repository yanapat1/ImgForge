// The Augment tab: build an Albumentations recipe (presets, steps with their own knobs, a strength slider) and see what it
// does to this dataset: original | variants side by side with the boxes drawn, which steps ran, which boxes it lost.
// The left panel is the recipe, the right one the preview (and the box check / coverage of data/augment_cover.py).
// Server: server/routes/augment.py on data/augment.py. Every change previews again after a short pause; a newer
// request cancels the older one.

import { api, errText, getJSON, postJSON, sendJSON } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { S } from '../core/state.js';
import { clsColor } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { initAugCheck } from './augment-check.js';
import { initAugCover } from './augment-cover.js';
import { openDialog } from './dialog.js';

export interface AugArg { k: string; kind: 'range' | 'float' | 'int' | 'choice'; min?: number; max?: number; step?: number; default: unknown; choices?: string[]; label: string; int?: boolean }
export interface AugTransform { t: string; group: string; label: string; help: string; covers: string; args: AugArg[]; p_default: number }
export interface AugStep { t: string; p: number; on: boolean; args: Record<string, unknown> }
export interface AugRecipe { version: 1; mode: 'all' | 'one_of' | 'some_of'; n: number; strength: number; steps: AugStep[] }
interface Catalog { groups: { id: string; label: string }[]; transforms: AugTransform[]; presets: { id: string; label: string; help: string; recipe: AugRecipe }[] }
interface Variant { img: string; w: number; h: number; boxes: number[][]; dropped: number; applied: string[] }
interface Item { name: string; w: number; h: number; orig: { img: string; boxes: number[][] }; variants: Variant[] }

const KEY = 'dataviz.augment';
export const AG = {
  cat: null as Catalog | null,
  recipe: { version: 1, mode: 'all', n: 2, strength: 1, steps: [] } as AugRecipe,
  seed: 1, pick: 'random' as 'random' | 'per_class' | 'selection', nImages: 6, variants: 4, boxes: true,
  items: [] as Item[], busy: false, err: '', solo: -1, view: 'preview' as 'preview' | 'boxes' | 'coverage',
  saved: [] as { name: string; recipe: AugRecipe }[],
};
let ready = false, timer: ReturnType<typeof setTimeout> | undefined, ctl: AbortController | null = null;
const thumbs = new Map<string, string>();
/** Other parts of the tab (the box check, coverage) redraw when the recipe changes. */
export const AUG_HOOKS: Array<() => void> = [];

const meta = (t: string): AugTransform | undefined => AG.cat?.transforms.find((x) => x.t === t);
const num = (v: unknown, d = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : d);
const fmtN = (v: number): string => (Math.abs(v) >= 10 || Number.isInteger(v) ? String(Math.round(v * 100) / 100) : v.toFixed(3).replace(/0+$/, '').replace(/\.$/, ''));

function remember(): void {
  try { localStorage.setItem(KEY, JSON.stringify({ recipe: AG.recipe, seed: AG.seed, pick: AG.pick, nImages: AG.nImages, variants: AG.variants, boxes: AG.boxes })); } catch { /* a convenience only */ }
}
function recall(): void {
  try { Object.assign(AG, JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<typeof AG>); } catch { /* a convenience only */ }
  if (AG.pick === 'selection') AG.pick = 'random';
}

export function newStep(t: string): AugStep {
  const m = meta(t);
  return { t, p: m?.p_default ?? 0.5, on: true, args: Object.fromEntries((m?.args ?? []).map((a) => [a.k, structuredClone(a.default)])) };
}

// ---------------------------------------------------------------- the left panel: the recipe
function argHTML(i: number, a: AugArg, v: unknown): string {
  const id = `ag-${i}-${a.k}`;
  if (a.kind === 'choice') return `<label class="ag-arg"><span>${esc(a.label)}</span><select data-ag="arg" data-i="${i}" data-k="${esc(a.k)}">${(a.choices ?? []).map((c) => `<option ${c === v ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></label>`;
  const at = `min="${a.min}" max="${a.max}" step="${a.step}"`;
  if (a.kind === 'range') {
    const [lo, hi] = Array.isArray(v) ? v as number[] : [0, 0];
    return `<div class="ag-arg"><span>${esc(a.label)}</span><span class="ag-rng"><input type="number" ${at} value="${num(lo)}" data-ag="lo" data-i="${i}" data-k="${esc(a.k)}" aria-label="${esc(a.label)} from"><span class="faint">to</span><input type="number" ${at} value="${num(hi)}" data-ag="hi" data-i="${i}" data-k="${esc(a.k)}" aria-label="${esc(a.label)} to"></span></div>`;
  }
  return `<label class="ag-arg" for="${id}"><span>${esc(a.label)}</span><input id="${id}" type="range" ${at} value="${num(v)}" data-ag="val" data-i="${i}" data-k="${esc(a.k)}"><b class="num">${fmtN(num(v))}</b></label>`;
}

function stepHTML(s: AugStep, i: number, n: number): string {
  const m = meta(s.t);
  return `<div class="ag-step${s.on ? '' : ' off'}${AG.solo === i ? ' solo' : ''}" data-i="${i}">` +
    `<div class="ag-sh"><label class="check" title="${s.on ? 'Turn this step off (kept in the recipe)' : 'Turn this step on'}"><input type="checkbox" data-ag="on" data-i="${i}" ${s.on ? 'checked' : ''}><b>${esc(m?.label ?? s.t)}</b></label><span class="grow"></span>` +
    `<button type="button" class="icon-btn ghost xs${AG.solo === i ? ' on' : ''}" data-ag-act="solo" data-i="${i}" aria-pressed="${AG.solo === i}" title="Preview only this step, every time">${icon('eye')}</button>` +
    `<button type="button" class="icon-btn ghost xs" data-ag-act="up" data-i="${i}" ${i ? '' : 'disabled'} aria-label="Move up" title="Move up">${icon('chev-u')}</button>` +
    `<button type="button" class="icon-btn ghost xs" data-ag-act="down" data-i="${i}" ${i < n - 1 ? '' : 'disabled'} aria-label="Move down" title="Move down">${icon('chev-d')}</button>` +
    `<button type="button" class="icon-btn ghost xs" data-ag-act="rm" data-i="${i}" aria-label="Remove ${esc(m?.label ?? s.t)}" title="Remove">${icon('x')}</button></div>` +
    `<div class="ag-sb"><p class="faint ag-cov" title="${esc(m?.help ?? '')}">${esc(m?.covers ?? '')}</p>` +
    `<label class="ag-arg"><span>chance</span><input type="range" min="0" max="1" step="0.05" value="${s.p}" data-ag="p" data-i="${i}"><b class="num">${Math.round(s.p * 100)}%</b></label>` +
    (m?.args ?? []).map((a) => argHTML(i, a, s.args[a.k])).join('') + '</div></div>';
}

export function renderRecipe(): void {
  const side = $('aug-body');
  if (!side || !AG.cat) return;
  const r = AG.recipe;
  side.innerHTML = `<div class="ag-ctl"><label class="m-lbl" for="ag-preset">Start from</label><select id="ag-preset" data-ag="preset"><option value="">a preset…</option>${AG.cat.presets.map((p) => `<option value="${esc(p.id)}" title="${esc(p.help)}">${esc(p.label)}</option>`).join('')}</select>` +
    `<button type="button" class="sm" data-ag-act="menu" aria-haspopup="menu">Recipe ▾</button></div>` +
    `<div class="ag-ctl"><label class="m-lbl" for="ag-mode" title="All: each step on its own chance. One of: one step per image. Some of: n steps per image.">Apply</label><select id="ag-mode" data-ag="mode">${[['all', 'every step (by its chance)'], ['one_of', 'one step per image'], ['some_of', 'some steps per image']].map(([k, l]) => `<option value="${k}" ${r.mode === k ? 'selected' : ''}>${l}</option>`).join('')}</select>` +
    (r.mode === 'some_of' ? `<input type="number" min="1" max="${Math.max(1, r.steps.length)}" value="${r.n}" data-ag="n" aria-label="How many steps" class="ag-n">` : '') + '</div>' +
    `<div class="ag-ctl"><label class="m-lbl" for="ag-str" title="Every step gentler (left) or harsher (right) at once">Strength</label><input id="ag-str" type="range" min="0" max="2" step="0.05" value="${r.strength}" data-ag="strength"><b class="num">${r.strength.toFixed(2)}×</b></div>` +
    `<div class="ag-steps">${r.steps.map((s, i) => stepHTML(s, i, r.steps.length)).join('') || '<p class="faint ag-none">No steps yet. Start from a preset or add an augmentation.</p>'}</div>` +
    `<button type="button" class="primary block" data-ag-act="add">${icon('plus')}Add augmentation</button>`;
}

function changed(preview = true): void {
  remember();
  if (preview) schedule();
  AUG_HOOKS.forEach((f) => f());
}

function onInput(t: HTMLInputElement | HTMLSelectElement, done: boolean): void {
  const k = t.dataset['ag'], i = Number(t.dataset['i']), s = AG.recipe.steps[i];
  if (k === 'strength') { AG.recipe.strength = Number(t.value); const b = t.nextElementSibling; if (b) b.textContent = `${Number(t.value).toFixed(2)}×`; changed(); return; }
  if (k === 'p' && s) { s.p = Number(t.value); const b = t.nextElementSibling; if (b) b.textContent = `${Math.round(s.p * 100)}%`; changed(); return; }
  if (k === 'val' && s) { s.args[t.dataset['k'] ?? ''] = Number(t.value); const b = t.nextElementSibling; if (b) b.textContent = fmtN(Number(t.value)); changed(); return; }
  if (!done) return;
  if (k === 'preset') {
    const p = AG.cat?.presets.find((x) => x.id === t.value);
    if (p) { AG.recipe = structuredClone(p.recipe); AG.solo = -1; renderRecipe(); changed(); toast(`${p.label}: ${p.help}`); }
    return;
  }
  if (k === 'mode') { AG.recipe.mode = t.value as AugRecipe['mode']; renderRecipe(); changed(); return; }
  if (k === 'n') { AG.recipe.n = Math.max(1, Number(t.value) || 1); changed(); return; }
  if (!s) return;
  if (k === 'on') { s.on = (t as HTMLInputElement).checked; t.closest('.ag-step')?.classList.toggle('off', !s.on); changed(); return; }
  if (k === 'arg') { s.args[t.dataset['k'] ?? ''] = t.value; changed(); return; }
  if (k === 'lo' || k === 'hi') {
    const cur = (Array.isArray(s.args[t.dataset['k'] ?? '']) ? s.args[t.dataset['k'] ?? ''] : [0, 0]) as number[];
    const v = Number(t.value);
    s.args[t.dataset['k'] ?? ''] = k === 'lo' ? [v, Math.max(v, num(cur[1]))] : [Math.min(v, num(cur[0])), v];
    changed();
  }
}

async function addDialog(): Promise<void> {
  const cat = AG.cat;
  if (!cat) return;
  const html = `<input type="search" class="txt" id="ag-find" placeholder="Find an augmentation (fog, blur, camera…)" aria-label="Find an augmentation">` +
    `<div class="ag-pick">${cat.groups.map((g) => `<section data-g="${esc(g.id)}"><h4>${esc(g.label)}</h4>${cat.transforms.filter((t) => t.group === g.id).map((t) =>
      `<button type="button" class="ag-pi" data-t="${esc(t.t)}" data-q="${esc(`${t.label} ${t.t} ${t.help} ${t.covers}`.toLowerCase())}"><span class="ag-th" data-th="${esc(t.t)}"></span><span><b>${esc(t.label)}</b><span class="faint"> · ${esc(t.help)}</span><br><span class="ag-cov">${esc(t.covers)}</span></span></button>`).join('')}</section>`).join('')}</div>`;
  let chosen = '';
  await openDialog({
    title: 'Add an augmentation', ok: 'Close', wide: true, body: html, focus: '#ag-find', onOk: () => null,
    bind: (root) => {
      root.querySelector<HTMLInputElement>('#ag-find')?.addEventListener('input', (e) => {
        const q = (e.target as HTMLInputElement).value.toLowerCase().trim();
        root.querySelectorAll<HTMLElement>('.ag-pi').forEach((b) => { b.hidden = !!q && !(b.dataset['q'] ?? '').includes(q); });
        root.querySelectorAll<HTMLElement>('.ag-pick section').forEach((s) => { s.hidden = ![...s.querySelectorAll<HTMLElement>('.ag-pi')].some((b) => !b.hidden); });
      });
      root.addEventListener('click', (e) => {
        const b = (e.target as Element).closest<HTMLElement>('.ag-pi');
        if (!b) return;
        chosen = b.dataset['t'] ?? '';
        root.closest('.ui-dlg-wrap')?.querySelector<HTMLElement>('[data-dlg="ok"]')?.click();
      });
      const io = new IntersectionObserver((es) => es.forEach((en) => { if (en.isIntersecting) { io.unobserve(en.target); void thumb(en.target as HTMLElement); } }), { root: root.querySelector('.ag-pick') });
      root.querySelectorAll<HTMLElement>('.ag-th').forEach((x) => io.observe(x));
    },
  });
  if (!chosen) return;
  AG.recipe.steps.push(newStep(chosen));
  renderRecipe();
  changed();
}

/** A small example of one transform on the first previewed image (cached for the session). */
async function thumb(el: HTMLElement): Promise<void> {
  const t = el.dataset['th'] ?? '', name = AG.items[0]?.name;
  if (!name || !t) return;
  const key = `${name}|${t}`;
  let src = thumbs.get(key);
  if (!src) {
    try {
      const r = await postJSON<{ items: Item[] }>('api/augment/preview', { recipe: { version: 1, mode: 'all', n: 1, strength: 1, steps: [{ ...newStep(t), p: 1 }] }, names: [name], n_images: 1, variants: 1, size: 160, seed: 3 });
      src = r.items[0]?.variants[0]?.img ?? '';
      thumbs.set(key, src);
    } catch { return; }
  }
  if (src) el.style.backgroundImage = `url("${src}")`;
}

/** A small menu under `from`; a click outside or Esc closes it. */
function openMenu(from: HTMLElement, items: [string, string][], pick: (k: string) => void): void {
  document.getElementById('ag-menu')?.remove();
  const m = document.createElement('div');
  m.id = 'ag-menu';
  m.className = 'ag-menu';
  m.setAttribute('role', 'menu');
  m.innerHTML = items.map(([k, l]) => `<button type="button" role="menuitem" data-k="${esc(k)}">${esc(l)}</button>`).join('');
  const r = from.getBoundingClientRect();
  m.style.left = `${Math.max(4, Math.min(r.left, innerWidth - 260))}px`;
  m.style.top = `${r.bottom + 4}px`;
  document.body.append(m);
  const close = (): void => { m.remove(); document.removeEventListener('pointerdown', out, true); document.removeEventListener('keydown', esc_, true); };
  const out = (e: Event): void => { if (!m.contains(e.target as Node)) close(); };
  const esc_ = (e: KeyboardEvent): void => { if (e.key === 'Escape') { e.stopPropagation(); close(); from.focus(); } };
  document.addEventListener('pointerdown', out, true);
  document.addEventListener('keydown', esc_, true);
  m.addEventListener('click', (e) => { const b = (e.target as Element).closest<HTMLElement>('[data-k]'); if (b) { close(); pick(b.dataset['k'] ?? ''); } });
  m.querySelector<HTMLElement>('button')?.focus();
}

function recipeMenu(from: HTMLElement): void {
  openMenu(from, [
    ['save', 'Save as…'], ...AG.saved.map((s, i): [string, string] => [`load:${i}`, `Open “${s.name}”`]),
    ['import', 'Import Albumentations JSON…'], ['export:albumentations', 'Export Albumentations JSON'], ['export:python', 'Export Python code'],
    ['export:yaml', 'Export YAML'], ['export:ultralytics', 'Export for Ultralytics training'],
    ['copy', 'Make an augmented copy in the Data Center…'], ['clear', 'Clear the recipe'],
  ], (k) => { void recipeAct(k); });
}

async function recipeAct(k: string): Promise<void> {
  if (k === 'clear') { AG.recipe.steps = []; AG.solo = -1; renderRecipe(); changed(); return; }
  if (k === 'copy') { await copyDialog(); return; }
  if (k.startsWith('load:')) { const s = AG.saved[Number(k.slice(5))]; if (s) { AG.recipe = structuredClone(s.recipe); renderRecipe(); changed(); } return; }
  if (k === 'save') {
    let name = '';
    const ok = await openDialog({ title: 'Save the recipe', ok: 'Save', body: '<label class="fld"><span>Name</span><input type="text" class="txt" id="ag-sname" maxlength="80"></label>', focus: '#ag-sname',
      onOk: (root) => { name = root.querySelector<HTMLInputElement>('#ag-sname')?.value.trim() ?? ''; return name ? null : 'Give it a name'; } });
    if (!ok) return;
    const list = [...AG.saved.filter((s) => s.name !== name), { name, recipe: structuredClone(AG.recipe) }];
    try { AG.saved = (await sendJSON<{ recipes: typeof AG.saved }>('PUT', 'api/augment/recipes', { recipes: list })).recipes; toast(`Saved “${name}”`); } catch (e) { toast(errText(e), 'err'); }
    return;
  }
  if (k === 'import') {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = '.json,application/json';
    inp.onchange = async () => {
      const f = inp.files?.[0];
      if (!f) return;
      try {
        const r = await postJSON<{ recipe: AugRecipe; skipped: string[] }>('api/augment/import', { data: JSON.parse(await f.text()) as unknown });
        AG.recipe = r.recipe;
        renderRecipe();
        changed();
        toast(r.skipped.length ? `Imported. Not in this tab, left out: ${r.skipped.join(', ')}` : 'Imported');
      } catch (e) { toast(errText(e), 'err'); }
    };
    inp.click();
    return;
  }
  if (k.startsWith('export:')) {
    try {
      const r = await postJSON<{ text: string; file: string }>('api/augment/export', { recipe: AG.recipe, format: k.slice(7) });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([r.text], { type: 'text/plain' }));
      a.download = r.file;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch (e) { toast(errText(e), 'err'); }
  }
}

/** Write k augmented variants of every image (+ the originals) with YOLO labels into a new Data Center bucket. */
async function copyDialog(): Promise<void> {
  if (!AG.recipe.steps.some((s) => s.on)) { toast('The recipe has no step to apply', 'err'); return; }
  let body: Record<string, unknown> | null = null;
  await openDialog({
    title: 'Make an augmented copy', ok: 'Make the copy', focus: '#ag-cname',
    body: '<p>A new managed bucket in the Data Center gets every image of this session run through the recipe, with YOLO labels that follow the boxes (lost boxes are left out) and the recipe saved beside them.</p>' +
      '<label class="fld"><span>Bucket name</span><input type="text" class="txt" id="ag-cname" maxlength="80" value="augmented copy"></label>' +
      '<label class="fld"><span>Variants per image</span><select id="ag-ck">' + [1, 2, 3, 5, 10].map((n) => `<option ${n === 2 ? 'selected' : ''}>${n}</option>`).join('') + '</select></label>' +
      '<label class="check"><input type="checkbox" id="ag-corig" checked> Also copy the originals</label>',
    onOk: (root) => {
      const name = root.querySelector<HTMLInputElement>('#ag-cname')?.value.trim() ?? '';
      if (!name) return 'Give the bucket a name';
      body = { recipe: AG.recipe, name, k: Number(root.querySelector<HTMLSelectElement>('#ag-ck')?.value ?? 2), originals: !!root.querySelector<HTMLInputElement>('#ag-corig')?.checked };
      return null;
    },
  });
  if (!body) return;
  try {
    const r = await postJSON<{ job_id: number; bucket: { id: string; name: string }; images: number }>('api/augment/copy', body);
    toast(`Writing ${r.images.toLocaleString()} images into “${r.bucket.name}”…`);
    await waitJob(r.job_id);
    toast(`“${r.bucket.name}” is ready in the Data Center`);
  } catch (e) { toast(errText(e), 'err'); }
}

function stepAct(k: string, i: number): void {
  const st = AG.recipe.steps;
  if (k === 'rm') { st.splice(i, 1); if (AG.solo === i) AG.solo = -1; else if (AG.solo > i) AG.solo--; }
  else if (k === 'up' && i > 0) { [st[i - 1], st[i]] = [st[i] as AugStep, st[i - 1] as AugStep]; }
  else if (k === 'down' && i < st.length - 1) { [st[i + 1], st[i]] = [st[i] as AugStep, st[i + 1] as AugStep]; }
  else if (k === 'solo') AG.solo = AG.solo === i ? -1 : i;
  renderRecipe();
  changed();
}

/** The recipe the preview runs: a solo step alone at full chance, else the recipe. */
export function previewRecipe(): AugRecipe {
  const s = AG.recipe.steps[AG.solo];
  return s ? { ...AG.recipe, mode: 'all', steps: [{ ...s, on: true, p: 1 }] } : AG.recipe;
}

// ---------------------------------------------------------------- the right panel: preview
function schedule(): void { clearTimeout(timer); timer = setTimeout(() => { void runPreview(); }, 300); }

export async function runPreview(): Promise<void> {
  if (AG.view !== 'preview') return;
  ctl?.abort();
  const c = new AbortController();
  ctl = c;
  AG.busy = true;
  paintBar();
  const body: Record<string, unknown> = { recipe: previewRecipe(), pick: AG.pick === 'selection' ? 'names' : AG.pick, n_images: AG.nImages, variants: AG.variants, seed: AG.seed, size: 384 };
  if (AG.pick === 'selection') body['sids'] = (S.sel as number[]).slice(0, AG.nImages);
  try {
    const r = await api('api/augment/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: c.signal });
    const j = await r.json() as { items: Item[] };
    if (ctl !== c) return;
    AG.items = j.items;
    AG.err = '';
  } catch (e) {
    if (c.signal.aborted) return;
    AG.err = errText(e);
  } finally { if (ctl === c) { AG.busy = false; ctl = null; } }
  paintPreview();
}

function paintBar(): void {
  const bar = $('ag-bar');
  if (!bar) return;
  const nSel = (S.sel as number[]).length;
  bar.innerHTML = `<span class="segmented" role="tablist">${[['preview', 'Preview'], ['boxes', 'Boxes'], ['coverage', 'Coverage']].map(([k, l]) => `<button type="button" class="view-btn${AG.view === k ? ' active' : ''}" role="tab" aria-selected="${AG.view === k}" data-ag-act="view" data-k="${k}">${l}</button>`).join('')}</span>` +
    (AG.view === 'preview' ? `<label class="m-lbl" for="ag-pick">Images</label><select id="ag-pick" data-ag="pick"><option value="random" ${AG.pick === 'random' ? 'selected' : ''}>random</option><option value="per_class" ${AG.pick === 'per_class' ? 'selected' : ''}>one per class</option><option value="selection" ${AG.pick === 'selection' ? 'selected' : ''} ${nSel ? '' : 'disabled'}>my selection${nSel ? ` (${nSel})` : ''}</option></select>` +
      `<label class="m-lbl" for="ag-ni">×</label><select id="ag-ni" data-ag="ni">${[3, 6, 9, 12].map((n) => `<option ${n === AG.nImages ? 'selected' : ''}>${n}</option>`).join('')}</select>` +
      `<label class="m-lbl" for="ag-nv">variants</label><select id="ag-nv" data-ag="nv">${[2, 4, 6, 8].map((n) => `<option ${n === AG.variants ? 'selected' : ''}>${n}</option>`).join('')}</select>` +
      `<label class="check"><input type="checkbox" data-ag="boxes" ${AG.boxes ? 'checked' : ''}> boxes</label>` +
      `<button type="button" class="sm" data-ag-act="reroll" title="New random draws (other images, other variants)">🎲 Again</button><span class="grow"></span><span class="faint" id="ag-st">${AG.busy ? 'Working…' : ''}</span>` : '<span class="grow"></span>');
}

function paintPreview(): void {
  paintBar();
  const host = $('ag-main');
  if (!host || AG.view !== 'preview') return;
  if (AG.err) { host.innerHTML = `<div class="empty">${esc(AG.err)}</div>`; return; }
  if (!AG.items.length) { host.innerHTML = '<div class="empty">No images to show.</div>'; return; }
  const on = AG.recipe.steps.filter((s) => s.on).length;
  host.innerHTML = (on || AG.solo >= 0 ? '' : '<p class="notice">The recipe is empty: the variants are the original. Start from a preset on the left.</p>') +
    `<div class="ag-grid" style="--ag-cols:${AG.variants + 1}">${AG.items.map((it, r) => `<div class="ag-row"><figure class="ag-cell orig" data-r="${r}" data-v="-1"><img alt="" src="${it.orig.img}"><canvas></canvas><figcaption title="${esc(it.name)}">${esc(it.name.split('/').pop() ?? it.name)}</figcaption></figure>` +
      it.variants.map((v, j) => `<figure class="ag-cell" data-r="${r}" data-v="${j}" tabindex="0" title="${esc(v.applied.length ? `Ran: ${v.applied.join(', ')}` : 'Nothing ran on this one (each step has its chance)')}"><img alt="" src="${v.img}"><canvas></canvas>` +
        `<figcaption>${v.applied.length ? esc(v.applied.map((a) => meta(a)?.label ?? a).join(' · ')) : '<span class="faint">unchanged</span>'}${v.dropped ? ` <span class="pill danger" title="Boxes lost: less than 30% of them stayed in the image">−${v.dropped} box${v.dropped > 1 ? 'es' : ''}</span>` : ''}</figcaption></figure>`).join('') + '</div>').join('')}</div>`;
  host.querySelectorAll<HTMLElement>('.ag-cell').forEach((f) => {
    const img = f.querySelector('img');
    if (img?.complete) drawBoxes(f); else img?.addEventListener('load', () => drawBoxes(f), { once: true });
  });
}

function boxesOf(r: number, v: number): { boxes: number[][]; w: number; h: number } | null {
  const it = AG.items[r];
  if (!it) return null;
  const x = v < 0 ? null : it.variants[v];
  return x ? { boxes: x.boxes, w: x.w, h: x.h } : { boxes: it.orig.boxes, w: it.w, h: it.h };
}

function drawBoxes(f: HTMLElement): void {
  const cv = f.querySelector('canvas'), img = f.querySelector('img'), b = boxesOf(Number(f.dataset['r']), Number(f.dataset['v']));
  if (!cv || !img || !b) return;
  const w = img.clientWidth, h = img.clientHeight, dpr = devicePixelRatio || 1;
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(h * dpr);
  cv.style.width = `${w}px`;
  cv.style.height = `${h}px`;
  const g = cv.getContext('2d');
  if (!g) return;
  g.clearRect(0, 0, cv.width, cv.height);
  if (!AG.boxes) return;
  const sx = cv.width / b.w, sy = cv.height / b.h;
  g.lineWidth = 1.5 * dpr;
  for (const [c, x1, y1, x2, y2] of b.boxes) {
    g.strokeStyle = clsColor(c ?? 0);
    g.strokeRect((x1 ?? 0) * sx, (y1 ?? 0) * sy, ((x2 ?? 0) - (x1 ?? 0)) * sx, ((y2 ?? 0) - (y1 ?? 0)) * sy);
  }
}

/** The big view: a variant beside its original, a wipe between them, Space held = flicker to the original. */
async function lightbox(r: number, v: number): Promise<void> {
  const it = AG.items[r], x = it?.variants[v];
  if (!it || !x) return;
  const body = `<div class="ag-lb"><div class="ag-wipe" style="--wipe:50%"><img alt="Original" src="${it.orig.img}"><img alt="Augmented" class="top" src="${x.img}"></div>` +
    `<input type="range" min="0" max="100" value="50" aria-label="Wipe between the original and the augmented image" id="ag-wipe">` +
    `<p class="faint">Left: original · right: augmented. Hold <kbd>Space</kbd> to see the original. Ran: ${esc(x.applied.join(', ') || 'nothing')}. <a href="#" id="ag-full">Full size</a></p></div>`;
  await openDialog({
    title: it.name, ok: 'Close', wide: true, body, onOk: () => null,
    bind: (root) => {
      const wipe = root.querySelector<HTMLElement>('.ag-wipe'), top = root.querySelector<HTMLElement>('.ag-wipe .top');
      root.querySelector<HTMLInputElement>('#ag-wipe')?.addEventListener('input', (e) => wipe?.style.setProperty('--wipe', `${(e.target as HTMLInputElement).value}%`));
      const key = (on: boolean) => (e: KeyboardEvent): void => { if (e.code === 'Space') { e.preventDefault(); top?.classList.toggle('hide', on); } };
      root.addEventListener('keydown', key(true));
      root.addEventListener('keyup', key(false));
      root.querySelector('#ag-full')?.addEventListener('click', (e) => {
        e.preventDefault();
        void api('api/augment/one', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ recipe: previewRecipe(), name: it.name, seed: AG.seed * 1000 + r * 10 + v }) })
          .then((res) => res.blob()).then((b) => { window.open(URL.createObjectURL(b), '_blank', 'noopener'); }).catch((err: unknown) => toast(errText(err), 'err'));
      });
    },
  });
}

// ---------------------------------------------------------------- wiring
export async function showAugTab(): Promise<void> {
  if (!ready) {
    ready = true;
    recall();
    try {
      AG.cat = await getJSON<Catalog>('api/augment/catalog');
      AG.saved = (await getJSON<{ recipes: typeof AG.saved }>('api/augment/recipes')).recipes;
    } catch (e) {
      $req('aug-body').innerHTML = `<div class="empty">${esc(errText(e))}</div>`;
      ready = false;
      return;
    }
    AG.recipe.steps = AG.recipe.steps.filter((s) => meta(s.t));
    if (!AG.recipe.steps.length && AG.cat.presets[0]) AG.recipe = structuredClone(AG.cat.presets.find((p) => p.id === 'camera')?.recipe ?? AG.cat.presets[0].recipe);
  }
  renderRecipe();
  paintBar();
  if (AG.view === 'preview') await runPreview();
  else AUG_HOOKS.forEach((f) => f());
}

export function augName(t: string): string { return meta(t)?.label ?? t; }

export function setAugRecipe(r: AugRecipe): void { AG.recipe = r; AG.solo = -1; if (ready) { renderRecipe(); changed(); } else remember(); }

export function initAug(): void {
  const side = $('aug-side'), main = $('tab-aug');
  if (!side || !main) return;
  initAugCheck();
  initAugCover();
  side.addEventListener('input', (e) => onInput(e.target as HTMLInputElement, false));
  side.addEventListener('change', (e) => onInput(e.target as HTMLInputElement, true));
  side.addEventListener('click', (e) => {
    const b = (e.target as Element).closest<HTMLElement>('[data-ag-act]');
    if (!b) return;
    const k = b.dataset['agAct'] ?? '';
    if (k === 'add') void addDialog();
    else if (k === 'menu') recipeMenu(b);
    else stepAct(k, Number(b.dataset['i']));
  });
  main.addEventListener('change', (e) => {
    const t = e.target as HTMLInputElement, k = t.dataset['ag'];
    if (k === 'pick') AG.pick = t.value as typeof AG.pick;
    else if (k === 'ni') AG.nImages = Number(t.value);
    else if (k === 'nv') AG.variants = Number(t.value);
    else if (k === 'boxes') { AG.boxes = t.checked; remember(); main.querySelectorAll<HTMLElement>('.ag-cell').forEach(drawBoxes); return; }
    else return;
    remember();
    void runPreview();
  });
  main.addEventListener('click', (e) => {
    const b = (e.target as Element).closest<HTMLElement>('[data-ag-act]');
    if (b?.dataset['agAct'] === 'reroll') { AG.seed = (AG.seed * 7919 + 13) % 1000003; remember(); void runPreview(); return; }
    if (b?.dataset['agAct'] === 'view') { AG.view = b.dataset['k'] as typeof AG.view; paintBar(); if (AG.view === 'preview') paintPreview(); AUG_HOOKS.forEach((f) => f()); if (AG.view === 'preview') void runPreview(); return; }
    const f = (e.target as Element).closest<HTMLElement>('.ag-cell:not(.orig)');
    if (f) void lightbox(Number(f.dataset['r']), Number(f.dataset['v']));
  });
  main.addEventListener('keydown', (e) => {
    const f = (e.target as Element).closest<HTMLElement>('.ag-cell:not(.orig)');
    if (f && e.key === 'Enter') void lightbox(Number(f.dataset['r']), Number(f.dataset['v']));
  });
  addEventListener('resize', () => { if (main.offsetParent) main.querySelectorAll<HTMLElement>('.ag-cell').forEach(drawBoxes); });
}

export const AUG_PLOTS: string[] = ['ag-cov-plot'];
