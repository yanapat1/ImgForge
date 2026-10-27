// The side-by-side compare viewer: one image of the current compare run as A and as B (GET api/compare/image),
// each with its boxes as SVG coloured by diff kind (theme DIFF_STYLE). Both panes share ONE transform (wheel zoom at
// the cursor, drag to pan, double-click to fit), so the same spot is always under the same place in each. Hovering a
// box rings its partner on the other side. Modes: side by side, flicker (A / B alternate in one pane) and swipe
// (B over A, revealed up to the mouse). ← / → step through the list it was opened with, Esc closes.
// A separate component from viewer.ts (that one is a singleton tied to the full-image dialog); it reuses its tag().

import { errText, getJSON } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { S } from '../core/state.js';
import { clsName, DIFF_STYLE, diffColor, diffKey, originColor } from '../core/theme.js';
import type { CmpImage, CmpImgBox, CmpImgSide } from '../types.js';
import { closestOf } from './ev-util.js';
import { svgEl, tagIn } from './viewer.js';

type Side = 'a' | 'b';
type Mode = 'side' | 'flicker' | 'swipe';
const IMG_STATUS: Record<number, string> = { 1: 'only in A', 2: 'only in B', 3: 'identical', 4: 'labels differ', 5: 'pixels differ' };
const ZMAX = 16;

interface CvState {
  list: string[]; i: number; mode: Mode; seq: number;
  T: { s: number; x: number; y: number };             // shared zoom / pan, in pane pixels
  data: CmpImage | null;
  flick: ReturnType<typeof setInterval> | undefined; flickB: boolean;
  swipe: number;                                       // 0..1: B is shown left of this fraction (swipe mode)
  back: Element | null;
}
const CV: CvState = { list: [], i: 0, mode: 'side', seq: 0, T: { s: 1, x: 0, y: 0 }, data: null, flick: undefined, flickB: false, swipe: 0.5, back: null };

let root: HTMLElement | null = null;
const q = <T extends Element = HTMLElement>(sel: string): T => {
  const el = root?.querySelector<T>(sel);
  if (!el) throw new Error(`compare viewer: missing ${sel}`);
  return el;
};
const pane = (s: Side): HTMLElement => q(`.cmpv-pane[data-side="${s}"]`);

function build(): HTMLElement {
  const el = document.createElement('div');
  el.id = 'cmpv';
  el.className = 'modal cmpv-wrap hidden';
  const paneHtml = (s: Side): string => `<div class="cmpv-pane" data-side="${s}"><div class="cmpv-label"></div>` +
    `<div class="cmpv-view"><div class="cmpv-layer hidden"><img alt="" draggable="false"><svg class="cmpv-svg" aria-hidden="true"></svg></div>` +
    '<div class="cmpv-empty hidden"></div></div></div>';
  el.innerHTML = `<div class="cmpv panel" role="dialog" aria-modal="true" aria-labelledby="cmpv-title" tabindex="-1">
    <div class="modal-head">
      <div id="cmpv-title" class="modal-title"></div>
      <div class="modal-actions">
        <div class="segmented"><button type="button" class="icon-btn" data-cv="prev" aria-label="Previous image" title="Previous (←)">${icon('chev-l')}</button>
          <button type="button" class="icon-btn" data-cv="next" aria-label="Next image" title="Next (→)">${icon('chev-r')}</button></div>
        <span class="cmpv-count muted num"></span>
        <div class="segmented" role="group" aria-label="Mode">
          <button type="button" class="view-btn" data-mode="side" title="A and B next to each other (1)">Side by side</button>
          <button type="button" class="view-btn" data-mode="flicker" title="A and B alternate in one place (2) · Space pauses">Flicker</button>
          <button type="button" class="view-btn" data-mode="swipe" title="B over A up to the mouse (3)">Swipe</button></div>
        <button type="button" class="view-btn" data-cv="fit" title="Fit (0 or double-click)">Fit</button>
        <button type="button" class="icon-btn" data-cv="close" aria-label="Close" title="Close (Esc)">${icon('x')}</button>
      </div>
    </div>
    <div class="cmpv-stage" data-mode="side">${paneHtml('a')}${paneHtml('b')}<div class="cmpv-swipe-line hidden"></div></div>
    <div class="cmpv-foot"><div class="cmpv-legend"></div><div class="cmpv-info muted" aria-live="polite"></div>
      <span class="hint muted">Wheel to zoom · drag to move · hover a box to see its partner</span></div>
  </div>`;
  document.body.appendChild(el);
  bind(el);
  return el;
}

/** Open the viewer on image `name` (a union name like 'A/x.jpg'); ← / → walk `list`. Needs a compare run. */
export async function openCompareViewer(name: string, list: string[] = [name]): Promise<void> {
  if (!S.cmp?.run) return;
  root ??= build();
  if (root.classList.contains('hidden')) CV.back = document.activeElement;
  CV.list = list.length ? list : [name];
  CV.i = Math.max(0, CV.list.indexOf(name));
  root.classList.remove('hidden');
  q('.cmpv').focus();
  setMode(CV.mode);
  q('.cmpv-legend').innerHTML = DIFF_STYLE.map((d) => `<span class="cmp-li"><span class="dot" style="background:${d.color}"></span>${esc(d.label)}</span>`).join('');
  await load(CV.list[CV.i] ?? name);
}

export function closeCompareViewer(): void {
  if (!root || root.classList.contains('hidden')) return;
  root.classList.add('hidden');
  clearInterval(CV.flick);
  CV.seq++;
  if (CV.back instanceof HTMLElement && document.contains(CV.back)) CV.back.focus();
}

export const compareViewerOpen = (): boolean => !!root && !root.classList.contains('hidden');

async function load(name: string): Promise<void> {
  const run = S.cmp?.run;
  if (!run) return;
  const seq = ++CV.seq;
  CV.T = { s: 1, x: 0, y: 0 };
  q('.cmpv-count').textContent = CV.list.length > 1 ? `${CV.i + 1} / ${CV.list.length}` : '';
  q('#cmpv-title').innerHTML = `<span class="muted">${esc(name)}</span>`;
  q('.cmpv-info').textContent = '';
  let d: CmpImage;
  try {
    d = await getJSON<CmpImage>(`api/compare/image?key=${encodeURIComponent(run.key)}&name=${encodeURIComponent(name)}`);
  } catch (e) {
    if (seq !== CV.seq) return;
    q('#cmpv-title').innerHTML = `<span class="txt-danger">${esc(errText(e))}</span>`;
    return;
  }
  if (seq !== CV.seq) return;
  CV.data = d;
  const st = IMG_STATUS[d.status] ?? '';
  q('#cmpv-title').innerHTML = `<span class="chip">${esc(st)}${d.dup ? ' · duplicate' : ''}</span><span class="muted">${esc(name)}</span>`;
  await Promise.all((['a', 'b'] as const).map((s) => showSide(s, d[s], seq)));
  if (seq !== CV.seq) return;
  apply();
}

function showSide(s: Side, side: CmpImgSide | null, seq: number): Promise<void> {
  const p = pane(s), C = S.cmp;
  const k = C ? (s === 'a' ? C.a : C.b) : 0;
  const m = S.meta?.members?.[k];
  const label = p.querySelector<HTMLElement>('.cmpv-label');
  if (label) label.innerHTML = `<span class="cmp-tag" style="--c:${originColor(k)}">${s.toUpperCase()}</span>${esc(m?.alias ?? '')}` +
    (side ? ` <span class="muted">${esc(side.name)} · ${side.boxes.length} box${side.boxes.length === 1 ? '' : 'es'}</span>` : '');
  const img = p.querySelector<HTMLImageElement>('img'), empty = p.querySelector<HTMLElement>('.cmpv-empty'), layer = p.querySelector<HTMLElement>('.cmpv-layer');
  if (!img || !empty || !layer) return Promise.resolve();
  empty.classList.toggle('hidden', !!side);
  layer.classList.toggle('hidden', !side);
  p.querySelector('svg')?.replaceChildren();
  if (!side) { empty.textContent = `Not in ${m?.alias ?? s.toUpperCase()}`; return Promise.resolve(); }
  return new Promise<void>((resolve) => {
    const done = (): void => { if (seq === CV.seq) { fit(p); draw(s); } resolve(); };
    img.onload = done;
    img.onerror = done;
    img.src = `api/img/raw?name=${encodeURIComponent(side.name)}`;
    if (img.complete && img.naturalWidth) done();
  });
}

// the image fitted into its pane (contain), as the layer's own box; the shared transform scales around the pane origin
function fit(p: HTMLElement): void {
  const view = p.querySelector<HTMLElement>('.cmpv-view'), layer = p.querySelector<HTMLElement>('.cmpv-layer'), img = p.querySelector<HTMLImageElement>('img');
  if (!view || !layer || !img || !img.naturalWidth) return;
  const W = view.clientWidth, H = view.clientHeight, r = img.naturalWidth / img.naturalHeight;
  const w = Math.min(W, H * r), h = w / r;
  const l = (W - w) / 2, t = (H - h) / 2;
  Object.assign(layer.style, { left: `${l}px`, top: `${t}px`, width: `${w}px`, height: `${h}px`, transformOrigin: `${-l}px ${-t}px` });
}

// one transform for both panes (the test reads the two style.transform strings: they are always equal)
function apply(): void {
  const view = pane('a').querySelector<HTMLElement>('.cmpv-view');
  const w = view?.clientWidth ?? 0, h = view?.clientHeight ?? 0, T = CV.T;
  T.s = Math.min(ZMAX, Math.max(1, T.s));
  T.x = Math.min(0, Math.max(w * (1 - T.s), T.x));
  T.y = Math.min(0, Math.max(h * (1 - T.s), T.y));
  if (T.s === 1) { T.x = 0; T.y = 0; }
  const tr = T.s === 1 ? '' : `translate(${T.x}px, ${T.y}px) scale(${T.s})`;
  for (const s of ['a', 'b'] as const) {
    const layer = pane(s).querySelector<HTMLElement>('.cmpv-layer');
    if (layer) { layer.style.transform = tr; layer.style.setProperty('--zs', String(T.s)); }   // strokes keep their screen width
    draw(s);
  }
}

function zoomAt(f: number, cx: number, cy: number, p: HTMLElement): void {
  const r = (p.querySelector<HTMLElement>('.cmpv-view') ?? p).getBoundingClientRect();
  const px = cx - r.left, py = cy - r.top, T = CV.T;
  const s2 = Math.min(ZMAX, Math.max(1, T.s * f));
  T.x += px * (1 - s2 / T.s);
  T.y += py * (1 - s2 / T.s);
  T.s = s2;
  apply();
}

const sideOf = (s: Side): CmpImgSide | null => (CV.data ? CV.data[s] : null);

// boxes in image pixels; k = image pixels per screen pixel, so strokes and tags keep their screen size at any zoom
function draw(s: Side): void {
  const side = sideOf(s), p = pane(s);
  const svg = p.querySelector('svg'), img = p.querySelector<HTMLImageElement>('img');
  if (!svg || !img) return;
  if (!side || !img.naturalWidth) { svg.replaceChildren(); return; }
  const W = img.naturalWidth, H = img.naturalHeight;
  const k = W / (img.getBoundingClientRect().width || W);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const g = svgEl('g', {}), tags = svgEl('g', { class: 'vw-tags' });
  for (const b of side.boxes) {
    const [x1, y1, x2, y2] = b.xyxy;
    const kind = diffKey(b.status, s === 'b'), col = diffColor(kind);
    g.append(svgEl('rect', { x: x1 * W, y: y1 * H, width: Math.max(0, (x2 - x1) * W), height: Math.max(0, (y2 - y1) * H),
      stroke: col, class: `cmpv-box k${kind}`, 'data-sid': b.sid, 'data-pair': b.pair }));
    tags.append(...tagIn(H, x1 * W, y1 * H, clsName(b.cls) ?? `class ${b.cls}`, col, k));
  }
  svg.replaceChildren(g, tags);
}

function boxBy(s: Side, sid: number): CmpImgBox | undefined { return sideOf(s)?.boxes.find((b) => b.sid === sid); }

function hover(rect: SVGElement | null): void {
  root?.querySelectorAll('.cmpv-box.hl').forEach((r) => r.classList.remove('hl'));
  if (!rect) { q('.cmpv-info').textContent = ''; return; }
  const s = (rect.closest<HTMLElement>('.cmpv-pane')?.dataset['side'] ?? 'a') as Side, o: Side = s === 'a' ? 'b' : 'a';
  const sid = +(rect.getAttribute('data-sid') ?? -1), pair = +(rect.getAttribute('data-pair') ?? -1);
  rect.classList.add('hl');
  if (pair >= 0) pane(o).querySelector(`.cmpv-box[data-sid="${pair}"]`)?.classList.add('hl');
  const b = boxBy(s, sid), pb = pair >= 0 ? boxBy(o, pair) : undefined;
  if (!b) return;
  const kind = DIFF_STYLE[diffKey(b.status, s === 'b')]?.label ?? '';
  q('.cmpv-info').textContent = `${s.toUpperCase()} #${sid} ${clsName(b.cls) ?? b.cls}` +
    (pb ? ` ↔ ${o.toUpperCase()} #${pair} ${clsName(pb.cls) ?? pb.cls} · IoU ${b.iou.toFixed(2)}` : '') + (kind ? ` · ${kind}` : '');
}

function setMode(m: Mode): void {
  CV.mode = m;
  const stage = q('.cmpv-stage');
  stage.dataset['mode'] = m;
  root?.querySelectorAll<HTMLElement>('[data-mode]').forEach((b) => {
    if (b === stage) return;
    b.classList.toggle('active', b.dataset['mode'] === m);
    b.setAttribute('aria-pressed', String(b.dataset['mode'] === m));
  });
  clearInterval(CV.flick);
  CV.flickB = false;
  pane('b').classList.remove('off');
  if (m === 'flicker') startFlicker();
  q('.cmpv-swipe-line').classList.toggle('hidden', m !== 'swipe');
  syncSwipe();
  // the panes changed size (one pane per side vs both stacked): fit again, from the whole image (a zoom kept from
  // the narrow panes would point at another spot of the wide one)
  CV.T = { s: 1, x: 0, y: 0 };
  requestAnimationFrame(() => { fit(pane('a')); fit(pane('b')); apply(); });
}

function startFlicker(): void {
  clearInterval(CV.flick);
  CV.flick = setInterval(() => { CV.flickB = !CV.flickB; pane('b').classList.toggle('off', !CV.flickB); }, 700);
  pane('b').classList.add('off');
}

function syncSwipe(): void {
  const b = pane('b');
  if (CV.mode !== 'swipe') { b.style.clipPath = ''; return; }
  b.style.clipPath = `inset(0 ${((1 - CV.swipe) * 100).toFixed(2)}% 0 0)`;
  q<HTMLElement>('.cmpv-swipe-line').style.left = `${(CV.swipe * 100).toFixed(2)}%`;
}

function step(d: number): void {
  if (CV.list.length < 2) return;
  CV.i = (CV.i + d + CV.list.length) % CV.list.length;
  void load(CV.list[CV.i] ?? '');
}

function bind(el: HTMLElement): void {
  el.addEventListener('click', (e) => {
    if (e.target === el) { closeCompareViewer(); return; }
    const b = closestOf<HTMLElement>(e, '[data-cv], button[data-mode]');
    if (!b) return;
    if (b.dataset['mode']) { setMode(b.dataset['mode'] as Mode); return; }
    const act = b.dataset['cv'];
    if (act === 'close') closeCompareViewer();
    else if (act === 'prev') step(-1);
    else if (act === 'next') step(1);
    else if (act === 'fit') { CV.T = { s: 1, x: 0, y: 0 }; apply(); }
  });
  const stage = el.querySelector<HTMLElement>('.cmpv-stage');
  if (!stage) return;
  stage.addEventListener('wheel', (e) => {
    const p = closestOf<HTMLElement>(e, '.cmpv-pane');
    if (!p) return;
    e.preventDefault();
    zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY, p);
  }, { passive: false });
  stage.addEventListener('dblclick', () => { CV.T = { s: 1, x: 0, y: 0 }; apply(); });
  let pan: { x: number; y: number } | null = null;
  stage.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.button !== 1) return;
    e.preventDefault();
    pan = { x: e.clientX, y: e.clientY };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add('panning');
  });
  stage.addEventListener('pointermove', (e) => {
    if (pan) {
      CV.T.x += e.clientX - pan.x;
      CV.T.y += e.clientY - pan.y;
      pan = { x: e.clientX, y: e.clientY };
      apply();
      return;
    }
    if (CV.mode === 'swipe') {
      const r = stage.getBoundingClientRect();
      CV.swipe = Math.min(1, Math.max(0, (e.clientX - r.left) / (r.width || 1)));
      syncSwipe();
    }
  });
  const end = (): void => { pan = null; stage.classList.remove('panning'); };
  stage.addEventListener('pointerup', end);
  stage.addEventListener('pointercancel', end);
  stage.addEventListener('mouseover', (e) => {
    if (pan) return;
    const r = e.target instanceof SVGElement && e.target.classList.contains('cmpv-box') ? e.target : null;
    hover(r);
  });
  new ResizeObserver(() => { if (compareViewerOpen()) { fit(pane('a')); fit(pane('b')); apply(); } }).observe(stage);
  // capture phase: while open, the page's own keys (gallery, lasso Esc) never see these
  document.addEventListener('keydown', (e) => {
    if (!compareViewerOpen()) return;
    const k = e.key;
    if (k === 'Escape') closeCompareViewer();
    else if (k === 'ArrowLeft') step(-1);
    else if (k === 'ArrowRight') step(1);
    else if (k === '1' || k === '2' || k === '3') setMode(k === '1' ? 'side' : k === '2' ? 'flicker' : 'swipe');
    else if (k === '0') { CV.T = { s: 1, x: 0, y: 0 }; apply(); }
    else if (k === ' ' && CV.mode === 'flicker') { if (CV.flick) { clearInterval(CV.flick); CV.flick = undefined; } else startFlicker(); }
    else if (k === 'Tab') { e.preventDefault(); }
    else return;
    e.preventDefault();
    e.stopPropagation();
  }, true);
}
