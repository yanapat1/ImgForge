// The image viewer inside the full-image dialog: the plain image (api/img/raw) with the boxes drawn on top as SVG,
// so labels, class names and model predictions are toggled without a new image request. Right-click copies or
// saves the image with or without the boxes (drawn again on a canvas at full resolution). Edit mode hands the
// boxes to views/box-editor.js. The box data comes from the label file itself (api/img/boxes), so it is always
// what is on disk, edits included.

import { errMessage, getJSON } from '../core/api.js';
import { $, $req, textOn } from '../core/dom.js';
import { S } from '../core/state.js';
import { clsColor, THEME } from '../core/theme.js';
import type { EditResult, ImagePred, ImgBox, ImgBoxes, ViewerState } from '../types-views.js';
import * as ED from './box-editor.js';
import { closestOf } from './ev-util.js';

const NS = 'http://www.w3.org/2000/svg';
const PREF_KEY = 'viewer-show';

// what is drawn; kept per viewer (localStorage, never required)
const show = { labels: true, names: true };
try {
  const saved: unknown = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
  if (typeof saved === 'object' && saved !== null) {
    if ('labels' in saved) show.labels = !!saved.labels;
    if ('names' in saved) show.names = !!saved.names;
  }
} catch (e) { /* private mode */ }

// the image on screen: name, size the label coordinates refer to, label boxes, focus box, predictions
export const V: ViewerState = { name: null, W: 1, H: 1, boxes: [], sha1: null, label: null, writable: false, focus: null,
  preds: [], seq: 0, fixed: false, focusPid: null };

let onSaved: (r: EditResult) => void = () => {};

// zoom / pan of the image (CSS transform on #vw): s = zoom, x / y = shift in screen px
const Z = { s: 1, x: 0, y: 0 };
const ZMAX = 16;

const savePrefs = (): void => { try { localStorage.setItem(PREF_KEY, JSON.stringify(show)); } catch (e) { /* ignore */ } };
const svgEl = (tag: string, attrs: Record<string, string | number>): SVGElement => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
};
const color = (c: number): string => (c >= 0 ? clsColor(c) : THEME.ungrouped);
const nameOf = (c: number): string => (S.meta && S.meta.classes[c] !== undefined ? S.meta.classes[c] ?? `class ${c}` : `class ${c}`);

/** The <svg id="vw-svg"> the boxes are drawn in. */
function svgRoot(): SVGSVGElement {
  const el = document.getElementById('vw-svg');
  if (!(el instanceof SVGSVGElement)) throw new Error('missing element #vw-svg');
  return el;
}

export interface ViewerHandlers { onSaved?: (r: EditResult) => void }

export function initViewer(handlers: ViewerHandlers = {}): void {
  onSaved = handlers.onSaved || onSaved;
  $req('vw-labels').onclick = () => toggle('labels');
  $req('vw-names').onclick = () => toggle('names');
  $req('vw-edit').onclick = () => setEdit(!ED.active());
  ED.initEditor({ svg: svgRoot(), V, redraw: draw, onSaved: (r) => { onSaved(r); syncButtons(); } });
  // right-click: our menu (copy / save with or without boxes); Shift + right-click keeps the browser's own
  $req('vw').addEventListener('contextmenu', (e) => {
    if (e.shiftKey || !V.name) return;
    e.preventDefault();
    openMenu(e.clientX, e.clientY);
  });
  $req('vw-menu').addEventListener('click', (e) => {
    const b = closestOf<HTMLButtonElement>(e, 'button[data-vm]');
    if (!b) return;
    closeMenu();
    void menuAction(b.dataset.vm ?? '');
  });
  document.addEventListener('pointerdown', (e) => { if (!(e.target instanceof Node) || !$req('vw-menu').contains(e.target)) closeMenu(); }, true);
  document.addEventListener('keydown', (e) => {
    if ($req('vw-menu').classList.contains('hidden')) return;
    if (e.key === 'Escape') { e.preventDefault(); closeMenu(); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {            // roving focus in the menu
      e.preventDefault();
      const items = [...$req('vw-menu').querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const i = items.indexOf(document.activeElement as HTMLButtonElement);
      items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
    }
  });
  // class names / handles keep their screen size whatever the zoom of the image
  new ResizeObserver(() => { syncZoomUi(); draw(); }).observe($req('modal-img'));
  $req('modal-img').addEventListener('load', () => {
    const im = $req<HTMLImageElement>('modal-img');
    if (im.naturalWidth && im.naturalHeight) $req('vw').style.setProperty('--ar', String(im.naturalWidth / im.naturalHeight));
    syncZoomUi();
    draw();
  });
  initZoom();
  syncButtons();
}

function applyZoom(): void {
  const vw = $req('vw'), w = vw.offsetWidth, h = vw.offsetHeight;
  Z.s = Math.min(ZMAX, Math.max(1, Z.s));
  Z.x = Math.min(0, Math.max(w * (1 - Z.s), Z.x));       // the image always covers its unzoomed box
  Z.y = Math.min(0, Math.max(h * (1 - Z.s), Z.y));
  if (Z.s === 1) { Z.x = 0; Z.y = 0; }
  vw.style.transformOrigin = '0 0';
  vw.style.transform = Z.s === 1 ? '' : `translate(${Z.x}px, ${Z.y}px) scale(${Z.s})`;
  // clip to the box the image had unzoomed (in its own, unscaled coordinates): whatever the browser does with the
  // ancestors' overflow, the zoomed image never paints over the zoom bar, the side panel or the header
  const l = -Z.x / Z.s, t = -Z.y / Z.s, r = w - (w - Z.x) / Z.s, b = h - (h - Z.y) / Z.s;
  vw.style.clipPath = Z.s === 1 ? '' : `inset(${t}px ${r}px ${b}px ${l}px)`;
  vw.classList.toggle('zoomed', Z.s > 1);
  syncZoomUi();
  draw();
}

// 100% = one image pixel per screen pixel (what "1:1" shows); the fitted view is usually below that
function syncZoomUi(): void {
  const lab = $('vw-zoom-lv'), im = $req<HTMLImageElement>('modal-img');
  if (lab) lab.textContent = im.naturalWidth ? `${Math.round(Z.s * $req('vw').offsetWidth / im.naturalWidth * 100)}%` : '–';
  const bar = $('vw-zoombar');
  if (!bar) return;
  const q = (z: string): HTMLButtonElement | null => bar.querySelector(`button[data-z="${z}"]`);
  const on = !!V.name, zoomed = Z.s > 1;
  for (const z of ['in']) { const b = q(z); if (b) b.disabled = !on || Z.s >= ZMAX; }
  for (const z of ['out', 'fit']) { const b = q(z); if (b) b.disabled = !on || !zoomed; }
}

// 1:1: one image pixel per screen pixel, around the centre of the stage
function actualSize(): void {
  const im = $req<HTMLImageElement>('modal-img'), w = $req('vw').offsetWidth;
  if (!im.naturalWidth || !w) return;
  zoomBy(Math.max(1, im.naturalWidth / w) / Z.s);
}

// zoom by factor f around a screen point (default: the centre of the stage)
function zoomBy(f: number, cx?: number, cy?: number): void {
  const r = $req('vw').getBoundingClientRect();
  const px = (cx ?? (r.left + r.width / 2)) - r.left, py = (cy ?? (r.top + r.height / 2)) - r.top;
  const s2 = Math.min(ZMAX, Math.max(1, Z.s * f));
  Z.x += px * (1 - s2 / Z.s);
  Z.y += py * (1 - s2 / Z.s);
  Z.s = s2;
  applyZoom();
}

const panBy = (dx: number, dy: number): void => { Z.x += dx; Z.y += dy; applyZoom(); };
export function resetZoom(): void { Z.s = 1; Z.x = 0; Z.y = 0; applyZoom(); }

function initZoom(): void {
  const view = $req('vw').parentElement, stage = view?.parentElement;
  if (!view || !stage) throw new Error('#vw is not inside a stage');
  stage.classList.add('zoomable');
  const bar = $req('vw-zoombar');
  for (const [id, cls, d, label] of [['m-prev', 'prev', 'M15 5l-7 7 7 7', 'Previous'], ['m-next', 'next', 'M9 5l7 7-7 7', 'Next']] as const) {
    const b = document.createElement('button');
    b.className = `vw-side ${cls}`;
    b.tabIndex = -1;
    b.title = `${label} (${cls === 'prev' ? '←' : '→'})`;
    b.setAttribute('aria-label', label);
    b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    b.onclick = () => $req(id).click();
    view.append(b);
  }
  bar.addEventListener('click', (e) => {
    const b = closestOf<HTMLButtonElement>(e, 'button[data-z]');
    if (!b) return;
    const actions: Record<string, () => void> = { in: () => zoomBy(1.4), out: () => zoomBy(1 / 1.4), fit: resetZoom, actual: actualSize };
    actions[b.dataset.z ?? '']?.();
  });
  stage.addEventListener('wheel', (e) => {
    if (!V.name || closestOf(e, '.vw-editbar')) return;
    e.preventDefault();
    zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
  }, { passive: false });
  $req('vw').addEventListener('dblclick', (e) => { if (!ED.active()) { e.preventDefault(); resetZoom(); } });
  // drag to move: left button on a zoomed image (viewing), middle button or Space + drag (editing)
  let pan: { x: number; y: number } | null = null, space = false;
  addEventListener('keydown', (e) => { if (e.code === 'Space' && V.name) space = true; });
  addEventListener('keyup', (e) => { if (e.code === 'Space') space = false; });
  $req('vw').addEventListener('pointerdown', (e) => {
    const ok = e.button === 1 || (e.button === 0 && (ED.active() ? space : Z.s > 1));
    if (!ok || Z.s <= 1 && e.button !== 1) return;
    e.preventDefault();
    e.stopPropagation();
    pan = { x: e.clientX, y: e.clientY };
    $req('vw').setPointerCapture(e.pointerId);
    $req('vw').classList.add('panning');
  }, true);
  $req('vw').addEventListener('pointermove', (e) => {
    if (!pan) return;
    panBy(e.clientX - pan.x, e.clientY - pan.y);
    pan = { x: e.clientX, y: e.clientY };
  });
  const end = (): void => { pan = null; $req('vw').classList.remove('panning'); };
  $req('vw').addEventListener('pointerup', end);
  $req('vw').addEventListener('pointercancel', end);
}

function toggle(k: keyof typeof show): void {
  show[k] = !show[k];
  savePrefs();
  syncButtons();
  draw();
}

function syncButtons(): void {
  for (const [id, k] of [['vw-labels', 'labels'], ['vw-names', 'names']] as const) {
    $req(id).classList.toggle('active', show[k]);
    $req(id).setAttribute('aria-pressed', String(show[k]));
  }
  const on = ED.active();
  $req('vw-edit').classList.toggle('active', on);
  $req('vw-edit').setAttribute('aria-pressed', String(on));
  $req<HTMLButtonElement>('vw-edit').disabled = V.fixed || !V.name;
  $req('vw-edit').title = !V.writable && V.name ? 'This source is read-only: boxes can be viewed, not changed'
    : 'Fix boxes or draw missing ones (E) · right-click the image to copy it';
  $req('vw-toggles').classList.toggle('hidden', V.fixed);
}

// ---------------------------------------------------------------- loading
export interface ShowImageOpts { focus?: number | null; focusPid?: number | null; fixedSrc?: string | null }

// name: image file; focus: box id to ring (or null); fixed: an image the server drew (prediction mode)
export async function showImage(name: string, { focus = null, focusPid = null, fixedSrc = null }: ShowImageOpts = {}): Promise<ImgBoxes | null> {
  const seq = ++V.seq;
  ED.reset();
  V.name = name;
  V.focus = focus;
  V.focusPid = focusPid;
  V.fixed = !!fixedSrc;
  Z.s = 1; Z.x = 0; Z.y = 0;
  $req('vw').style.transform = '';
  $req('vw').style.clipPath = '';
  $req('vw').classList.remove('zoomed');
  syncZoomUi();
  V.boxes = [];
  V.preds = [];
  $req('vw-svg').replaceChildren();
  $req<HTMLImageElement>('modal-img').src = fixedSrc || `api/img/raw?name=${encodeURIComponent(name)}`;
  syncButtons();
  if (fixedSrc) return null;
  const [b] = await Promise.all([getJSON<ImgBoxes>(`api/img/boxes?name=${encodeURIComponent(name)}`), loadPreds(seq)]);
  if (seq !== V.seq) return null;
  setBoxes(b);
  return b;
}

export type BoxesLike = Pick<ImgBoxes, 'boxes' | 'sha1' | 'label' | 'img_w' | 'img_h'> & { writable?: boolean };

export function setBoxes(b: BoxesLike): void {
  V.W = b.img_w || 1;
  V.H = b.img_h || 1;
  V.boxes = b.boxes;
  V.sha1 = b.sha1;
  V.label = b.label;
  if (b.writable !== undefined) V.writable = b.writable;
  syncButtons();
  draw();
}

export async function reloadBoxes(): Promise<void> {
  if (!V.name || V.fixed) return;
  const seq = V.seq;
  const b = await getJSON<ImgBoxes>(`api/img/boxes?name=${encodeURIComponent(V.name)}`);
  if (seq === V.seq) setBoxes(b);
}

async function loadPreds(seq: number): Promise<void> {
  const M = S.model;
  if (!M.showPreds || !M.id || !M.ev) { V.preds = []; return; }
  try {
    const r = await getJSON<{ preds: ImagePred[] }>(`api/models/${encodeURIComponent(M.id)}/image-preds?name=${encodeURIComponent(V.name ?? '')}&conf=${M.conf}`);
    if (seq === V.seq) V.preds = r.preds;
  } catch (e) { V.preds = []; }
}

export async function refreshPreds(): Promise<void> {
  if (!V.name || V.fixed) return;
  await loadPreds(V.seq);
  draw();
}

export function closeViewer(): void {
  ED.reset();
  Z.s = 1; Z.x = 0; Z.y = 0;
  $req('vw').style.transform = '';
  $req('vw').style.clipPath = '';
  closeMenu();
  V.name = null;
  V.seq++;
}

// ---------------------------------------------------------------- drawing (SVG in image pixels)
// k: image pixels per screen pixel, so strokes / text / handles keep a constant screen size
function scale(): number {
  const w = $req('modal-img').getBoundingClientRect().width;      // on-screen width, zoom included
  return w ? V.W / w : 1;
}

export function draw(): void {
  const svg = $req('vw-svg');
  if (!V.name || V.fixed) { svg.replaceChildren(); return; }
  const k = scale();
  svg.setAttribute('viewBox', `0 0 ${V.W} ${V.H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const frag = document.createDocumentFragment();
  // tags go in their own group, drawn last, so no box outline crosses a tag
  const tags = svgEl('g', { class: 'vw-tags' });
  if (V.preds.length) {
    const g = svgEl('g', { class: 'vw-preds' });
    for (const p of V.preds) {
      const [x1, y1, x2, y2] = p.box;
      const r = svgEl('rect', { x: x1, y: y1, width: Math.max(x2 - x1, 0), height: Math.max(y2 - y1, 0), stroke: color(p.cls),
        'data-pid': p.pid, class: 'vw-pred' });
      if (p.pid === V.focusPid) g.append(svgEl('rect', { x: x1, y: y1, width: Math.max(x2 - x1, 0), height: Math.max(y2 - y1, 0), class: 'vw-focus' }));
      g.append(r);
      // below the box: a matching label's tag sits at the same top-left corner and would hide the score
      if (show.names) tags.append(...tag(x1, y1, `${p.cls >= 0 ? nameOf(p.cls) : '?'} ${p.score.toFixed(2)}`, color(p.cls), k, true, y2));
    }
    frag.append(g);
  }
  if (ED.active()) frag.append(ED.render(k, { names: show.names, color, nameOf, tag }));
  else if (show.labels) {
    const g = svgEl('g', { class: 'vw-labels' });
    for (const b of V.boxes) {
      const [x, y, w, h] = px(b);
      if (b.sid != null && b.sid === V.focus) g.append(svgEl('rect', { x, y, width: w, height: h, class: 'vw-focus' }));
      g.append(svgEl('rect', { x, y, width: w, height: h, stroke: color(b.cls), class: 'vw-box' }));
      if (show.names) tags.append(...tag(x, y, nameOf(b.cls), color(b.cls), k));
    }
    frag.append(g);
  }
  frag.append(tags);
  svg.replaceChildren(frag);
}

export const px = (b: Pick<ImgBox, 'cx' | 'cy' | 'w' | 'h'>): [number, number, number, number] =>
  [(b.cx - b.w / 2) * V.W, (b.cy - b.h / 2) * V.H, b.w * V.W, b.h * V.H];

// tag text width in screen px at 12px, measured with the page font (a per-character guess overflows on wide glyphs)
let measureCtx: CanvasRenderingContext2D | null = null;
function textWidth(text: string): number {
  measureCtx ??= document.createElement('canvas').getContext('2d');
  if (!measureCtx) return text.length * 0.6 * 12;
  measureCtx.font = `600 12px ${THEME.font || 'sans-serif'}`;
  return measureCtx.measureText(text).width;
}

// a class tag above the box (inside it when the box touches the top edge); with `bottom` (the box's lower edge)
// it goes below the box instead (inside it when the box touches the bottom edge)
export function tag(x: number, y: number, text: string, col: string, k: number, dashed = false, bottom?: number): [SVGElement, SVGElement] {
  const fs = 12 * k, pad = 3 * k, h = fs + 2 * pad;
  const w = textWidth(text) * k + 2 * pad;
  const ty = bottom != null ? (bottom + h > V.H ? bottom - h : bottom) : (y - h < 0 ? y : y - h);
  const bg = svgEl('rect', { x, y: ty, width: w, height: h, fill: col, class: dashed ? 'vw-tag-bg dashed' : 'vw-tag-bg' });
  const t = svgEl('text', { x: x + pad, y: ty + pad + fs * 0.85, 'font-size': fs, class: `vw-tag ${textOn(col)}` });
  t.textContent = text;
  return [bg, t];
}

// ---------------------------------------------------------------- edit mode
export function setEdit(on: boolean): void {
  if (on && (!V.writable || V.fixed)) return;
  if (!on && ED.dirty() && !confirm('Discard the unsaved box changes?')) return;
  ED.setActive(on);
  syncButtons();
  draw();
}

export const editing = (): boolean => ED.active();
export const dirty = (): boolean => ED.dirty();

// keys while the dialog is open: true = handled here (main.js then skips its own)
export function viewerKey(e: KeyboardEvent): boolean {
  if (!V.name) return false;
  const t = e.target;
  if (t instanceof Element && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) return false;
  if (!e.ctrlKey && !e.metaKey && !e.altKey) {
    const kk = e.key.toLowerCase(), st = Math.max(40, $req('vw').offsetWidth * 0.15);
    if (kk === '+' || kk === '=') { zoomBy(1.4); return true; }
    if (kk === '-' || kk === '_') { zoomBy(1 / 1.4); return true; }
    if (kk === '0') { resetZoom(); return true; }
    if (kk === '1') { actualSize(); return true; }
    if (Z.s > 1 && e.shiftKey && e.key.startsWith('Arrow')) {         // Shift + arrows move the zoomed image (plain arrows = prev / next)
      panBy(e.key === 'ArrowLeft' ? st : e.key === 'ArrowRight' ? -st : 0, e.key === 'ArrowUp' ? st : e.key === 'ArrowDown' ? -st : 0);
      return true;
    }
    if (Z.s > 1 && 'wasd'.includes(kk) && kk.length === 1) {
      panBy(kk === 'a' ? st : kk === 'd' ? -st : 0, kk === 'w' ? st : kk === 's' ? -st : 0);
      return true;
    }
  }
  if (ED.active() && ED.key(e)) return true;
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  const k = e.key.toLowerCase();
  if (k === 'b') { toggle('labels'); return true; }
  if (k === 'l') { toggle('names'); return true; }
  if (k === 'e' && !V.fixed) { setEdit(!ED.active()); return true; }
  if (k === 'p' && !$req('m-preds').classList.contains('hidden')) { $req('m-preds').click(); return true; }
  return false;
}

// ---------------------------------------------------------------- right-click: copy / save
function openMenu(x: number, y: number): void {
  const m = $req('vw-menu');
  m.querySelectorAll<HTMLButtonElement>('[data-vm$="-plain"]').forEach((b) => { b.disabled = V.fixed; });
  m.classList.remove('hidden');
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.max(4, Math.min(x, innerWidth - r.width - 4))}px`;
  m.style.top = `${Math.max(4, Math.min(y, innerHeight - r.height - 4))}px`;
  m.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
}

function closeMenu(): void { $req('vw-menu').classList.add('hidden'); }

async function menuAction(a: string): Promise<void> {
  if (a === 'copy-name') { await copyText(V.name ?? ''); return; }
  const withBoxes = a.endsWith('-boxes');
  let blob: Blob;
  try { blob = await composite(withBoxes); } catch (e) { flash(`Could not build the image: ${errMessage(e)}`); return; }
  if (a.startsWith('save')) { download(blob); return; }
  // the async clipboard needs a secure context (https or localhost): plain http from another machine falls back
  if (window.isSecureContext && navigator.clipboard && window.ClipboardItem) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      flash(`Copied ${withBoxes ? 'with' : 'without'} boxes`);
      return;
    } catch (e) { /* permission refused: fall through to the download */ }
  }
  download(blob);
  flash('The browser blocks image copy on this page (http): saved as PNG instead');
}

async function copyText(text: string): Promise<void> {
  try { await navigator.clipboard.writeText(text); flash('File name copied'); } catch (e) {
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    document.body.append(ta); ta.select();
    try { document.execCommand('copy'); flash('File name copied'); } catch (e2) { flash(text); }
    ta.remove();
  }
}

function download(blob: Blob): void {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${(V.name || 'image').replace(/\.[^.]+$/, '')}.png`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

let flashTimer: ReturnType<typeof setTimeout> | undefined;
function flash(text: string): void {
  const el = $req('vw-edit-st');
  if (ED.active()) { el.textContent = text; return; }
  const t = $('vw-flash') || Object.assign(document.createElement('div'), { id: 'vw-flash', className: 'vw-flash panel' });
  t.textContent = text;
  $req('vw').append(t);
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => t.remove(), 2600);
}

// the full-resolution image with what is shown on screen drawn on it (labels / editor boxes, names, predictions)
async function composite(withBoxes: boolean): Promise<Blob> {
  let bmp: ImageBitmap;
  if (V.fixed) bmp = await createImageBitmap(await (await fetch($req<HTMLImageElement>('modal-img').src)).blob());
  else bmp = await createImageBitmap(await (await fetch(`api/img/raw?name=${encodeURIComponent(V.name ?? '')}&max_w=8192`)).blob());
  const cv = document.createElement('canvas');
  cv.width = bmp.width;
  cv.height = bmp.height;
  const g = cv.getContext('2d');
  if (!g) throw new Error('canvas is not available');
  g.drawImage(bmp, 0, 0);
  if (withBoxes && !V.fixed) {
    const sx = cv.width / V.W, sy = cv.height / V.H;
    const lw = Math.max(2, Math.round(cv.width / 500));
    const fs = Math.max(12, Math.round(cv.width / 70));
    // same placement as tag(): above the box, or below it when `bottom` is given (predictions)
    const label = (x: number, y: number, text: string, col: string, bottom?: number): void => {
      g.font = `600 ${fs}px ${THEME.font || 'sans-serif'}`;
      const w = g.measureText(text).width + fs * 0.5, h = fs * 1.35;
      const ty = bottom != null ? (bottom + h > cv.height ? bottom - h : bottom) : (y - h < 0 ? y : y - h);
      g.fillStyle = col;
      g.fillRect(x, ty, w, h);
      g.fillStyle = '#000';
      g.fillText(text, x + fs * 0.25, ty + fs * 1.05);
    };
    // outlines first, then every tag, so no outline crosses a tag
    const labels: Array<() => void> = [];
    for (const p of V.preds) {
      const [x1, y1, x2, y2] = p.box;
      g.setLineDash([lw * 4, lw * 3]);
      g.lineWidth = lw;
      g.strokeStyle = color(p.cls);
      g.strokeRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
      g.setLineDash([]);
      if (show.names) labels.push(() => label(x1 * sx, y1 * sy, `${p.cls >= 0 ? nameOf(p.cls) : '?'} ${p.score.toFixed(2)}`, color(p.cls), y2 * sy));
    }
    const boxes: Array<Pick<ImgBox, 'cls' | 'cx' | 'cy' | 'w' | 'h'>> = ED.active() ? ED.liveBoxes() : (show.labels ? V.boxes : []);
    for (const b of boxes) {
      const [x, y, w, h] = px(b);
      g.lineWidth = lw;
      g.strokeStyle = color(b.cls);
      g.strokeRect(x * sx, y * sy, w * sx, h * sy);
      if (show.names) labels.push(() => label(x * sx, y * sy, nameOf(b.cls), color(b.cls)));
    }
    for (const draw of labels) draw();
  }
  bmp.close();
  return new Promise((res, rej) => cv.toBlob((b) => (b ? res(b) : rej(new Error('canvas is empty'))), 'image/png'));
}
