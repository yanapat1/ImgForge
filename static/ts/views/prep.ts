// The projection's Preprocess section (dataviz/ml/prep.py) and the Recipe menu (dataviz/ml/recipes.yaml) of the Map
// popover: reading / writing the controls as a PrepSpec (only what differs from the Default is sent, so a default map
// keeps its old cache key), the one-line summary, the Fuse-with list, and the recipes grouped by category (one not
// possible on this session is disabled with why). views/controls.ts puts the spec into the projection request.

import { getJSON } from '../core/api.js';
import { $req, esc } from '../core/dom.js';
import { status } from '../core/state.js';
import type { PrepSpec, Recipe } from '../types.js';

type El = HTMLInputElement | HTMLSelectElement;
const sel = (id: string): HTMLSelectElement => $req<HTMLSelectElement>(id);
const inp = (id: string): HTMLInputElement => $req<HTMLInputElement>(id);
const num = (id: string): number | null => { const v = inp(id).value.trim(); return v === '' ? null : Number(v); };

/** every control of the section (a change marks the map as changed) */
export const PREP_IDS = ['prep-context', 'prep-fit', 'prep-tone', 'prep-mask', 'prep-tta', 'prep-scale', 'prep-pca', 'prep-debias',
  'prep-fuse', 'prep-fuse-w', 'prep-min-px', 'prep-min-blur', 'prep-br-lo', 'prep-br-hi', 'prep-show'];

/** The controls as a spec, defaults left out ({} = Default). */
export function readPrep(): PrepSpec {
  const image: NonNullable<PrepSpec['image']> = {};
  if (sel('prep-context').value !== 'square10') image.context = sel('prep-context').value;
  if (sel('prep-fit').value !== 'square') image.fit = sel('prep-fit').value;
  if (sel('prep-tone').value !== 'none') image.tone = sel('prep-tone').value;
  if (inp('prep-mask').checked) image.mask = 'polygon';
  if (inp('prep-tta').checked) image.tta = 'hflip';
  const feature: NonNullable<PrepSpec['feature']> = {};
  if (sel('prep-scale').value !== 'none') feature.scale = sel('prep-scale').value;
  if (sel('prep-pca').value !== '50') feature.pca = Number(sel('prep-pca').value);
  if (sel('prep-debias').value) feature.debias = sel('prep-debias').value;
  if (sel('prep-fuse').value) feature.fuse = { feature: sel('prep-fuse').value, weight: num('prep-fuse-w') ?? 0.3 };
  const filter: NonNullable<NonNullable<PrepSpec['feature']>['filter']> = {};
  const px = num('prep-min-px'), blur = num('prep-min-blur'), lo = num('prep-br-lo'), hi = num('prep-br-hi');
  if (px) filter.min_px = px;
  if (blur) filter.min_blur = blur;
  if ((lo != null && lo > 0) || (hi != null && hi < 255)) filter.brightness = [lo ?? 0, hi ?? 255];
  if (Object.keys(filter).length) {
    feature.filter = filter;
    if (sel('prep-show').value === 'grey') feature.show = 'grey';
  }
  const out: PrepSpec = {};
  if (Object.keys(image).length) out.image = image;
  if (Object.keys(feature).length) out.feature = feature;
  return out;
}

/** Set the controls from a spec (missing keys = their default). */
export function writePrep(p: PrepSpec | null | undefined): void {
  const im = p?.image ?? {}, fe = p?.feature ?? {}, fl = fe.filter ?? {};
  sel('prep-context').value = im.context ?? 'square10';
  sel('prep-fit').value = im.fit ?? 'square';
  sel('prep-tone').value = im.tone ?? 'none';
  inp('prep-mask').checked = !!im.mask;
  inp('prep-tta').checked = !!im.tta;
  sel('prep-scale').value = fe.scale ?? 'none';
  sel('prep-pca').value = String(fe.pca ?? 50);
  sel('prep-debias').value = fe.debias ?? '';
  if (fe.fuse && ![...sel('prep-fuse').options].some((o) => o.value === fe.fuse?.feature)) fillFuse();
  sel('prep-fuse').value = fe.fuse?.feature ?? '';
  inp('prep-fuse-w').value = String(fe.fuse?.weight ?? 0.3);
  inp('prep-min-px').value = fl.min_px ? String(fl.min_px) : '';
  inp('prep-min-blur').value = fl.min_blur ? String(fl.min_blur) : '';
  inp('prep-br-lo').value = fl.brightness ? String(fl.brightness[0]) : '';
  inp('prep-br-hi').value = fl.brightness ? String(fl.brightness[1]) : '';
  sel('prep-show').value = fe.show ?? 'hide';
  showSummary();
}

/** One short line of a spec (as dataviz/ml/prep.py summary), '' for the Default. */
export function prepSummary(p: PrepSpec | null | undefined): string {
  const im = p?.image ?? {}, fe = p?.feature ?? {};
  const w: string[] = [];
  if (im.context) w.push(im.context === 'box' ? 'box only' : im.context === 'image' ? 'whole image' : `${im.context.replace('pad', '+')}%`);
  if (im.fit) w.push(im.fit);
  if (im.mask) w.push('masked');
  if (im.tone) w.push(im.tone);
  if (im.tta) w.push('flip-avg');
  if (fe.fuse) w.push(`+ ${fe.fuse.feature} ×${fe.fuse.weight}`);
  if (fe.debias) w.push(`− ${fe.debias} bias`);
  if (fe.scale) w.push(fe.scale);
  if (fe.pca != null) w.push(fe.pca ? `PCA ${fe.pca}` : 'no PCA');
  if (fe.filter) w.push(`filtered${fe.show === 'grey' ? ' (faint)' : ''}`);
  return w.join(', ');
}

export function showSummary(): void {
  const s = prepSummary(readPrep());
  $req('prep-sum').textContent = s || 'Default';
  $req('prep').toggleAttribute('data-on', !!s);
}

/** The Fuse-with list: the session's feature spaces (the picked one is skipped server-side). */
export function fillFuse(): void {
  const s = sel('prep-fuse');
  const cur = s.value;
  const feats = (status().features ?? []).filter((f) => f.group !== 'variant');
  s.innerHTML = '<option value="">None (default)</option>' + feats.map((f) => `<option value="${esc(f.key)}">${esc(f.label)}</option>`).join('');
  s.value = feats.some((f) => f.key === cur) ? cur : '';
}

/** Specs are compared by their canonical JSON (keys sorted). */
export function samePrep(a: PrepSpec | null | undefined, b: PrepSpec | null | undefined): boolean {
  const canon = (v: unknown): unknown => (Array.isArray(v) ? v.map(canon) : v && typeof v === 'object'
    ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon((v as Record<string, unknown>)[k])])) : v);
  return JSON.stringify(canon(a ?? {})) === JSON.stringify(canon(b ?? {}));
}

let recipes: Recipe[] = [];
export const recipeById = (id: string): Recipe | undefined => recipes.find((r) => r.id === id);

/** Load the recipes of this session into the Recipe menu (grouped by category; unavailable ones disabled with why). */
export async function loadRecipes(): Promise<void> {
  try {
    const r = await getJSON<{ categories: string[]; recipes: Recipe[] }>('api/projection/recipes');
    recipes = r.recipes;
    const s = sel('recipe');
    s.innerHTML = '<option value="">Recommended projections…</option>' + r.categories.map((c) => {
      const rows = r.recipes.filter((x) => x.category === c);
      return rows.length ? `<optgroup label="${esc(c)}">` + rows.map((x) =>
        `<option value="${esc(x.id)}"${x.available ? '' : ' disabled'} title="${esc(x.available ? x.use_for : `Not here: ${x.why}`)}">` +
        `${x.instant ? '✓ ' : ''}${esc(x.name)}</option>`).join('') + '</optgroup>' : '';
    }).join('');
  } catch { /* the menu stays empty: the Map works without it */ }
}

/** The spec after a recipe: its keys replace the current ones part by part, a filter replaces the filter
 *  (as dataviz/ml/recipes.merge). */
export function mergePrep(cur: PrepSpec, add: PrepSpec | undefined): PrepSpec {
  if (!add) return cur;
  const out: PrepSpec = {};
  const im = { ...(cur.image ?? {}), ...(add.image ?? {}) };
  const fe = { ...(cur.feature ?? {}), ...(add.feature ?? {}) };
  if (Object.keys(im).length) out.image = im;
  if (Object.keys(fe).length) out.feature = fe;
  return out;
}

export const prepEls = (): El[] => PREP_IDS.map((id) => $req<El>(id));
