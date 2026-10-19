// DOM and formatting helpers with no app state: $ (getElementById), HTML escaping, the icon sprite,
// colour maths, slider fill, ETA text.

/** Element by id (null when absent). Pass the element type: `$<HTMLInputElement>('q')`. */
export const $ = <T extends HTMLElement = HTMLElement>(id: string): T | null => document.getElementById(id) as T | null;

/** Element by id that must exist: throws a readable error instead of a later "null.value" TypeError. */
export function $req<T extends HTMLElement = HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing element #${id}`);
  return el as T;
}

/** querySelector with a type parameter. */
export const $q = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document): T | null => root.querySelector<T>(sel);

/** querySelectorAll as a real array. */
export const $qa = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document): T[] => [...root.querySelectorAll<T>(sel)];

export const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

// sliders paint their filled part from --fill (see input[type=range] in css/base.css)
export function syncRanges(): void {
  for (const el of document.querySelectorAll<HTMLInputElement>('input[type=range]')) {
    const min = Number(el.min), max = Number(el.max);
    el.style.setProperty('--fill', `${(100 * (Number(el.value) - min)) / ((max - min) || 1)}%`);
  }
}

// static/assets/ as an absolute URL, from this module's own URL (static/js/core/dom.js): a relative
// 'static/assets/…' would point into s/<id>/ on the visualizer. Icons live in the SVG sprite icons.svg there.
export const ASSET_BASE = new URL('../../assets/', import.meta.url).href;
export const ICONS = `${ASSET_BASE}icons.svg`;

export const icon = (name: string): string => `<svg class="ico" aria-hidden="true"><use href="${ICONS}#i-${name}"/></svg>`;

const HTML_ESC: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
/** Escape &, <, > and " for HTML text / double-quoted attributes. */
export function esc(s: unknown): string { return String(s).replace(/[&<>"]/g, (c) => HTML_ESC[c] ?? c); }

type RGB = [number, number, number];
const rgbOf = (h: string): RGB => { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };

// blend two #rrggbb colours: t = 0 -> a, t = 1 -> b
export function mixHex(a: string, b: string, t: number): string {
  const x = rgbOf(a), y = rgbOf(b);
  return '#' + x.map((v, i) => Math.round(v + ((y[i] ?? 0) - v) * t).toString(16).padStart(2, '0')).join('');
}

export function rgba(hex: string, a: number): string {
  const [r, g, b] = rgbOf(hex);
  return `rgba(${r},${g},${b},${a})`;
}

// 'dark' or 'light': black or white text on a hex colour, whichever has the higher WCAG contrast
export type TextTone = 'dark' | 'light';
const fgCache = new Map<string, TextTone>();
export function textOn(hex: string): TextTone {
  const cached = fgCache.get(hex);
  if (cached !== undefined) return cached;
  const n = parseInt(hex.slice(1), 16);
  const lin = (v0: number): number => { const v = v0 / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const Y = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  const r: TextTone = (Y + 0.05) / 0.05 >= 1.05 / (Y + 0.05) ? 'dark' : 'light';
  fgCache.set(hex, r);
  return r;
}

export function fmtEta(s: number | null | undefined): string {
  if (s == null) return '';
  return s >= 60 ? `~${Math.floor(s / 60)}m ${Math.round(s % 60)}s left` : `~${Math.round(s)}s left`;
}
