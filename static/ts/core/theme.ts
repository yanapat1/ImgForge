// Theme: CSS tokens read for Plotly (THEME), colour scales per theme (SCALE), class colours, and the
// light / dark / system switch and the palette (data-palette) + look (data-look) choices. THEME and SCALE are exported live bindings: only this module reassigns them.

import type { ColorScale } from '../types.js';
import { $req, rgba } from './dom.js';
import { pref, rememberPref } from './auth.js';
import { S, meta } from './state.js';
import { invalidateHover, renderPlot } from '../plot/render.js';
import { renderSettings } from '../settings/dialog.js';
import { renderGallery } from '../views/gallery.js';
import { buildClassFilter, curTab, syncClassFilter } from '../views/layout.js';
import { invalidateEval, renderEval } from '../views/eval.js';
import { invalidateModel, renderModel } from '../views/model.js';
import { invalidateStats, renderClassTable, renderStats } from '../views/stats.js';

export type ThemeName = 'dark' | 'light';
/** CSS tokens (css/tokens.css) resolved for the current theme; what Plotly needs as literal colours. */
export interface Theme {
  name: ThemeName; palette: string; bg: string; plot: string; surface: string; surface2: string; surface3: string;
  border: string; grid: string; text: string; muted: string; danger: string; accent: string; highlight: string; ring: string;
  ungrouped: string; mixed: string; font: string; mono: string;
  errTp: string; errCls: string; errLoc: string; errMiss: string; errFp: string;
}
/** Colour scales of the current theme (keys of CONTINUOUS[...].scale), plus `heat(theme)` for heatmaps. */
export interface ScaleSet { seq: ColorScale; alarm: ColorScale; diverge: ColorScale; grey: ColorScale; heat: (t: Theme) => ColorScale }
export type ScaleName = Exclude<keyof ScaleSet, 'heat'>;

// colour scales per theme: the low end always keeps ~3:1 against --plot-bg, so no dot vanishes
const SCALES: Record<ThemeName, ScaleSet> = {
  dark: {
    seq: [[0, '#5a7cb0'], [0.35, '#9a7bc8'], [0.7, '#ee8a8a'], [1, '#ffd36e']],
    alarm: [[0, '#6a7a8e'], [0.5, '#e8a23c'], [0.8, '#ff7b5c'], [1, '#ffe08a']],
    diverge: [[0, '#5aa9ff'], [0.5, '#9098a3'], [1, '#ff9f43']],
    grey: [[0, '#6e7682'], [1, '#f4f6f8']],
    heat: (t) => [[0, t.surface2], [0.15, '#34507a'], [0.5, t.accent], [1, '#e3eeff']],
  },
  light: {
    seq: [[0, '#8aa3cc'], [0.35, '#8461c2'], [0.7, '#d24f4f'], [1, '#8f5f00']],
    alarm: [[0, '#a3adba'], [0.5, '#d18a12'], [0.8, '#df4f2c'], [1, '#9c1c0c']],
    diverge: [[0, '#1f6fd6'], [0.5, '#a0a8b2'], [1, '#cc6500']],
    grey: [[0, '#1f252d'], [1, '#bcc4ce']],
    heat: (t) => [[0, t.surface2], [0.15, '#c6d8f3'], [0.5, t.accent], [1, '#0f3570']],
  },
};

// colour-blind-safe variants (cbsafe + contrast palettes): luminance-ordered blue -> yellow ramps (cividis-like), blue / orange
// diverging; no red-green pairs. Same keys as SCALES, so SCALE is a drop-in.
const CB_SCALES: Record<ThemeName, ScaleSet> = {
  dark: {
    ...SCALES.dark,
    seq: [[0, '#5f7fae'], [0.35, '#8c97a8'], [0.7, '#cdb865'], [1, '#fff08a']],
    alarm: [[0, '#6a7a8e'], [0.5, '#e6c85a'], [0.8, '#ee9a2a'], [1, '#fff2c0']],
  },
  light: {
    ...SCALES.light,
    seq: [[0, '#8aa3cc'], [0.35, '#5d78a3'], [0.7, '#8c7a2a'], [1, '#4d3f00']],
    alarm: [[0, '#a3adba'], [0.5, '#b08a00'], [0.8, '#b85a00'], [1, '#5a2a00']],
  },
};

// class colours for the colour-blind palettes: Okabe-Ito first, then lighter / darker tints (hue repeats are told apart by
// lightness). Both sets keep >= 3:1 against their plot background; they replace config.yaml ui.palette / palette_light.
const CB_CLASS: Record<ThemeName, string[]> = {
  dark: ['#e69f00', '#56b4e9', '#2fc79a', '#f0e442', '#e08fc0', '#ff7a3d', '#b8c0cc', '#8fa8ff',
         '#ffd27a', '#a5dcf7', '#8fe0c4', '#f7f08a', '#f2bfdc', '#ffb08a', '#ffffff', '#c5d0ff', '#b86f00', '#3d8fc0', '#1fa07a', '#9a9a9a'],
  light: ['#b36b00', '#0072b2', '#007a5a', '#8a7600', '#a8467f', '#c24e00', '#5b6470', '#2a4d9b',
          '#8a5400', '#005a8c', '#005e45', '#6a5a00', '#823566', '#9a3d00', '#3f4752', '#1f3a78', '#d08000', '#3d95c8', '#2da080', '#808890'],
};
const isCb = (p: string): boolean => p === 'cbsafe' || p === 'contrast';

/** Colour-by fields that are numbers (columns of the points payload): label, transform, colour scale. */
export type ContinuousKey = 'suspicion' | 'outlier' | 'area' | 'aspect' | 'brightness' | 'blur' | 'm_score' |
  'cl_quality' | 'cl_given_prob' | 'cl_ood_feat' | 'cl_ood_prob' | 'cl_swap' | 'cl_badloc' | 'cl_image_quality';
export interface ContinuousField { label: string; f: (v: number) => number; scale: ScaleName }
export const CONTINUOUS: Record<ContinuousKey, ContinuousField> = {
  suspicion: { label: 'label suspicion', f: (v: number) => v, scale: 'alarm' },
  outlier: { label: 'distance to class centre', f: (v: number) => v, scale: 'seq' },
  area: { label: 'log10 box area', f: (v: number) => Math.log10(Math.max(v, 1e-6)), scale: 'seq' },
  aspect: { label: 'log2 aspect (w/h)', f: (v: number) => Math.log2(Math.max(v, 1e-3)), scale: 'diverge' },
  brightness: { label: 'brightness', f: (v: number) => v, scale: 'grey' },
  blur: { label: 'log10 sharpness', f: (v: number) => Math.log10(v + 1), scale: 'seq' },
  m_score: { label: 'model confidence', f: (v: number) => v, scale: 'seq' },
  // cleanlab (out-of-fold probabilities): drawn so that bright = suspicious, like label suspicion
  cl_quality: { label: '1 − label quality', f: (v: number) => 1 - v, scale: 'alarm' },
  cl_given_prob: { label: 'P(given class)', f: (v: number) => v, scale: 'seq' },
  cl_ood_feat: { label: 'out-of-distribution', f: (v: number) => v, scale: 'alarm' },
  cl_ood_prob: { label: 'out-of-distribution (probs)', f: (v: number) => v, scale: 'alarm' },
  cl_swap: { label: '1 − class score (detection)', f: (v: number) => 1 - v, scale: 'alarm' },
  cl_badloc: { label: '1 − placement score', f: (v: number) => 1 - v, scale: 'alarm' },
  cl_image_quality: { label: '1 − image label quality', f: (v: number) => 1 - v, scale: 'alarm' },
};

/** Is `k` one of the CONTINUOUS colour-by fields? */
export const isContinuous = (k: string): k is ContinuousKey => Object.prototype.hasOwnProperty.call(CONTINUOUS, k);

// model outcome of each label box (models/evaluate.py STATUS) + false positives; colours are tokens
export type ErrToken = 'errTp' | 'errCls' | 'errLoc' | 'errMiss';
export interface ErrKind { key: string; label: string; token: ErrToken }
export const ERR: readonly ErrKind[] = [
  { key: 'tp', label: 'found', token: 'errTp' },
  { key: 'wrong_cls', label: 'wrong class', token: 'errCls' },
  { key: 'loc', label: 'badly placed', token: 'errLoc' },
  { key: 'missed', label: 'missed', token: 'errMiss' },
];
/** Colour of model outcome `k` (index into ERR; anything else = ungrouped grey). */
export const errColor = (k: number): string => { const e = ERR[k]; return k >= 0 && e ? THEME[e.token] : THEME.ungrouped; };

// ---- compare (views/compare.ts): one colour per member of a combined dataset, one per box diff kind
// (same = muted grey so the changes stand out; the "only in one image" kinds are darker variants of removed / added).
// Every colour keeps >= 3:1 on its --plot-bg; the colour-blind palettes swap red / green for orange / blue.
/** A box diff kind: its key, legend label and colour. Indices are what diffKey() returns. */
export interface DiffKind { key: string; label: string; color: string }
type DiffColors = [same: string, moved: string, reclass: string, removed: string, added: string, onlyA: string, onlyB: string];
const DIFF_COLORS: Record<ThemeName, { std: DiffColors; cb: DiffColors }> = {
  dark: { std: ['#79828e', '#ffc24b', '#b98cff', '#ff7b7b', '#6fe08a', '#d9443a', '#2fa84f'],
    cb: ['#79828e', '#f0e442', '#e08fc0', '#ffb04a', '#5ab8ff', '#c26f00', '#2f7fc0'] },
  light: { std: ['#a3abb5', '#c27c00', '#7c4ddb', '#e53935', '#2e9e4a', '#9b1c1c', '#17602c'],
    cb: ['#a3abb5', '#8a7600', '#a8467f', '#d17a00', '#1f78c8', '#8a4a00', '#0b4a80'] },
};
const DIFF_LABELS: ReadonlyArray<readonly [string, string]> = [['same', 'same'], ['moved', 'moved'], ['reclass', 'class changed'],
  ['removed', 'removed (A only)'], ['added', 'added (B only)'], ['only_a', 'image only in A'], ['only_b', 'image only in B']];
const ORIGIN: Record<ThemeName, string[]> = {
  dark: ['#5aa9ff', '#ff9f43', '#2fc79a', '#e08fc0', '#f0e442', '#b8c0cc', '#8fa8ff', '#ffd27a'],
  light: ['#1f6fd6', '#cc6500', '#007a5a', '#a8467f', '#8a7600', '#5b6470', '#2a4d9b', '#8a5400'],
};
const diffFor = (t: Theme): DiffKind[] => {
  const c = DIFF_COLORS[t.name][isCb(t.palette) ? 'cb' : 'std'];
  return DIFF_LABELS.map(([key, label], i) => ({ key, label, color: c[i] ?? t.ungrouped }));
};

/**
 * Diff kind of a box (index into DIFF_STYLE) from its compare status (compare-core BOX) and its side; -1 = not
 * compared (another member, or no status). Side A: unmatched = removed, image only = only in A; side B: added / only in B.
 */
export function diffKey(status: number, sideB: boolean): number {
  switch (status) {
    case 1: return 0;                       // BOX.SAME
    case 2: return 1;                       // MOVED
    case 3: case 4: return 2;               // RECLASS, RECLASS_MOVED
    case 5: return sideB ? 4 : 3;           // UNMATCHED
    case 6: return sideB ? 6 : 5;           // IMAGE_ONLY
    default: return -1;
  }
}

// colours come from the CSS tokens in css/tokens.css, so the plots match the page chrome; re-read on a theme switch
function readTheme(): Theme {
  const css = getComputedStyle(document.documentElement);
  const v = (n: string): string => css.getPropertyValue(n).trim();
  const name = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  const palette = document.documentElement.dataset.palette || 'default';
  return { name, palette, bg: v('--bg'), plot: v('--plot-bg'), surface: v('--surface'), surface2: v('--surface-2'), surface3: v('--surface-3'),
    border: v('--border-strong'), grid: v('--border'), text: v('--text'), muted: v('--text-muted'), danger: v('--danger'),
    accent: v('--accent'), highlight: v('--highlight'), ring: v('--ring'), ungrouped: v('--dot-ungrouped'),
    mixed: v('--bubble-mixed'), font: v('--font'), mono: v('--mono'),
    errTp: v('--err-tp'), errCls: v('--err-cls'), errLoc: v('--err-loc'), errMiss: v('--err-miss'), errFp: v('--err-fp') };
}

/** Live binding: re-assigned only by applyTheme (import it, never copy it). */
export let THEME: Theme = readTheme();

const scalesFor = (t: Theme): ScaleSet => (isCb(t.palette) ? CB_SCALES : SCALES)[t.name];

/** Live binding, like THEME. */
export let SCALE: ScaleSet = scalesFor(THEME);

/** Box diff kinds of the current theme (live binding, like THEME): same, moved, reclass, removed, added, only A, only B. */
export let DIFF_STYLE: readonly DiffKind[] = diffFor(THEME);
/** Colour of member `k` of a combined dataset (live binding, wraps around). */
export let ORIGIN_COLORS: readonly string[] = ORIGIN[THEME.name];
export const originColor = (k: number): string => (k < 0 ? THEME.ungrouped : ORIGIN_COLORS[k % ORIGIN_COLORS.length] ?? THEME.ungrouped);
export const diffColor = (k: number): string => (k < 0 ? THEME.ungrouped : DIFF_STYLE[k]?.color ?? THEME.ungrouped);

/** Density-difference colour scale: A denser (z < 0) in A's colour, B denser in B's, ~0 transparent. */
export const densityScale = (): ColorScale => [[0, rgba(originColor(0), 0.8)], [0.38, rgba(originColor(0), 0.22)], [0.5, rgba(THEME.plot, 0)],
  [0.62, rgba(originColor(1), 0.22)], [1, rgba(originColor(1), 0.8)]];

/** Layout fields every Plotly chart shares (background, font, hover label). */
export const plotBase = (bg: string, size = 12): Partial<Plotly.Layout> => ({
  paper_bgcolor: bg, plot_bgcolor: bg, font: { color: THEME.text, size, family: THEME.font },
  hoverlabel: { bgcolor: THEME.surface2, bordercolor: THEME.border, font: { color: THEME.text, family: THEME.font, size: 13 } },
});

function palette(): string[] {
  if (isCb(THEME.palette)) return CB_CLASS[THEME.name];
  return (THEME.name === 'light' && meta().palette_light && meta().palette_light!.length) ? meta().palette_light! : meta().palette;
}

/** Colour of class id `c` in the current palette (wraps around). */
export const clsColor = (c: number): string => { const p = palette(); return p[((c % p.length) + p.length) % p.length] as string; };

/** Name of class id `c` (undefined for an unknown id). */
export const clsName = (c: number): string | undefined => meta().classes[c];

const darkQuery = matchMedia('(prefers-color-scheme: dark)');

/** 'light' | 'dark' | 'system' as stored (any other stored string passes through untouched). */
export type ThemeMode = string;
export function themeMode(): ThemeMode { try { return localStorage.getItem('theme') || 'system'; } catch (e) { return 'system'; } }

export const LOOKS: readonly string[] = ['default', 'editorial', 'blueprint', 'brutalist', 'terminal', 'soft'];
// looks and palettes that were retired, mapped to their closest replacement (also in theme-boot.ts for the first paint)
const OLD_LOOKS: Record<string, string> = { handdrawn: 'editorial', glass: 'soft' };
const OLD_PALETTES: Record<string, string> = { nord: 'fjord', solarized: 'fjord', sepia: 'ember', dusk: 'orchid', dracula: 'orchid', mono: 'default', paper: 'default' };
export function lookName(): string {
  try { const l = localStorage.getItem('look'); const v = l !== null ? OLD_LOOKS[l] ?? l : 'default'; return LOOKS.includes(v) ? v : 'default'; } catch (e) { return 'default'; }
}

// colour palettes (css/palettes.css): label + [page, surface, accent] swatch colours per tone for the Settings picker
export interface PaletteSpec { id: string; label: string; group: string; dark?: [string, string, string]; light?: [string, string, string] }
export const PALETTES: readonly PaletteSpec[] = [
  { id: 'default', label: 'Default', group: 'Standard' },
  { id: 'cbsafe', label: 'Colour-blind safe', group: 'Accessible', dark: ['#181c22', '#262c36', '#56b4e9'], light: ['#eceff3', '#ffffff', '#0072b2'] },
  { id: 'contrast', label: 'High contrast', group: 'Accessible', dark: ['#000000', '#141414', '#ffd60a'], light: ['#ffffff', '#f2f2f2', '#0033cc'] },
  { id: 'fjord', label: 'Fjord', group: 'Mood', dark: ['#20262f', '#2e3643', '#7fc8d6'], light: ['#e4e9ef', '#f6f8fb', '#2c6a80'] },
  { id: 'ember', label: 'Ember', group: 'Mood', dark: ['#1d1814', '#2d2620', '#e9a35a'], light: ['#efe5d3', '#faf4e8', '#94500f'] },
  { id: 'orchid', label: 'Orchid', group: 'Mood', dark: ['#1e1820', '#2d2531', '#e59ad0'], light: ['#f1eaf0', '#fcf8fb', '#9a2d78'] },
];
export function paletteName(): string {
  try { const p = localStorage.getItem('palette'); const v = p !== null ? OLD_PALETTES[p] ?? p : 'default'; return PALETTES.some((x) => x.id === v) ? v : 'default'; } catch (e) { return 'default'; }
}

function resolveTheme(mode: ThemeMode): string { return mode === 'system' ? (darkQuery.matches ? 'dark' : 'light') : mode; }

// the user's choice lives in their prefs (/api/me/prefs); localStorage is this browser's mirror for theme-boot's
// first paint
export function setTheme(mode: ThemeMode): void {
  try { localStorage.setItem('theme', mode); } catch (e) { /* private window: this page only */ }
  rememberPref('theme', mode);
  applyTheme();
}

export function setPalette(id: string): void {
  try { localStorage.setItem('palette', id); } catch (e) { /* private window: this page only */ }
  rememberPref('palette', id);
  applyTheme();
}

export function setLook(look: string): void {
  try { localStorage.setItem('look', look); } catch (e) { /* private window: this page only */ }
  rememberPref('look', look);
  applyTheme();
}

/** After loadMe(): the user's saved theme / look / palette win over this browser's mirror (another device set them). */
export function applyThemePrefs(): void {
  const want: Array<[string, unknown]> = [['theme', pref('theme')], ['look', pref('look')], ['palette', pref('palette')]];
  let changed = false;
  for (const [k, v] of want) {
    if (typeof v !== 'string' || !v) continue;
    try { if (localStorage.getItem(k) !== v) { localStorage.setItem(k, v); changed = true; } } catch (e) { /* private window */ }
  }
  if (changed) applyTheme();
}

function applyTheme(): void {
  const name = resolveTheme(themeMode());
  const look = lookName();
  const pal = paletteName();
  const html = document.documentElement;
  if (html.dataset.theme === name && html.dataset.look === look && html.dataset.palette === pal && THEME.name === name) return;
  html.dataset.theme = name;
  html.dataset.look = look;
  html.dataset.palette = pal;
  THEME = readTheme();
  SCALE = scalesFor(THEME);
  DIFF_STYLE = diffFor(THEME);
  ORIGIN_COLORS = ORIGIN[THEME.name];
  if (!S.meta) return;
  invalidateHover();                     // hover text carries the outlier colour
  buildClassFilter();
  syncClassFilter();
  if (S.view) renderPlot();
  renderGallery();
  invalidateStats();
  if (curTab === 'stats') { renderClassTable(); renderStats(); }
  invalidateModel();
  if (curTab === 'model') renderModel();
  invalidateEval();
  if (curTab === 'eval') renderEval();
  if (!$req('settings').classList.contains('hidden')) renderSettings();
  window.dispatchEvent(new CustomEvent('theme-changed'));     // views/compare.ts redraws its charts and chips
}

darkQuery.addEventListener('change', () => { if (themeMode() === 'system') applyTheme(); });
