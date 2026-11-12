// The Augment tab's compare viewer (Preview cells and Boxes › Examples): a large dialog in the page with the original
// and the augmented image stacked, each with its boxes. Hold the mouse on the image and drag left / right to move the
// wipe line (original on the left, augmented on the right); ← → move it too, Space held shows the original. It opens
// at once with the small images it is given, then `load` swaps in a sharp version (POST api/augment/one {json: true}).

import { errText } from '../core/api.js';
import { esc } from '../core/dom.js';
import { clsColor } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { openDialog } from './dialog.js';

export interface Side { img: string; w: number; h: number; boxes: number[][] }
export interface CompareOpts {
  title: string;
  orig: Side;
  aug: Side;
  /** a line under the image (which steps ran, boxes lost); HTML, escaped by the caller */
  info: string;
  boxes: boolean;
  /** a sharper pair, fetched after the viewer opens */
  load?: () => Promise<{ orig: Side; aug: Side; info?: string }>;
}

/** The answer of POST api/augment/one {json: true} as the viewer's two sides. */
export interface OneJSON { w: number; h: number; orig: { img: string; boxes: number[][] }; img: string; iw: number; ih: number; boxes: number[][]; dropped: number; applied: string[] }
export const sidesOf = (j: OneJSON): { orig: Side; aug: Side } =>
  ({ orig: { img: j.orig.img, w: j.w, h: j.h, boxes: j.orig.boxes }, aug: { img: j.img, w: j.iw, h: j.ih, boxes: j.boxes } });

const rects = (s: Side, on: boolean): string => (on ? s.boxes.map(([c, x1, y1, x2, y2]) =>
  `<rect x="${x1}" y="${y1}" width="${(x2 ?? 0) - (x1 ?? 0)}" height="${(y2 ?? 0) - (y1 ?? 0)}" stroke="${clsColor(c ?? 0)}"/>`).join('') : '');

function layer(s: Side, top: boolean, on: boolean): string {
  return `<div class="ag-ly${top ? ' top' : ''}"><img alt="${top ? 'Augmented' : 'Original'}" src="${s.img}" draggable="false">` +
    `<svg viewBox="0 0 ${s.w} ${s.h}" preserveAspectRatio="xMidYMid meet" aria-hidden="true">${rects(s, on)}</svg></div>`;
}

export async function compareView(o: CompareOpts): Promise<void> {
  const ar = `${o.orig.w} / ${o.orig.h}`;
  const body = `<div class="ag-lb"><div class="ag-wipe" style="--wipe:50%;--ar:${o.orig.w / o.orig.h};aspect-ratio:${ar}" role="slider" tabindex="0"` +
    ` aria-label="Wipe: original on the left, augmented on the right" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50">` +
    layer(o.orig, false, o.boxes) + layer(o.aug, true, o.boxes) +
    '<span class="ag-wl">Original</span><span class="ag-wr">Augmented</span><span class="ag-hd" aria-hidden="true"><i></i></span></div>' +
    `<p class="faint ag-lbi">Drag on the image to compare · <kbd>←</kbd> <kbd>→</kbd> move the line · hold <kbd>Space</kbd> for the original · <span class="ag-lbt">${o.info}</span></p></div>`;
  await openDialog({
    title: o.title, ok: 'Close', noCancel: true, wide: true, body, onOk: () => null, focus: '.ag-wipe',
    bind: (root) => {
      const wipe = root.querySelector<HTMLElement>('.ag-wipe');
      if (!wipe) return;
      let pct = 50, frame = 0, drag = false, lastX = 0;
      const paint = (): void => { frame = 0; wipe.style.setProperty('--wipe', `${pct}%`); wipe.setAttribute('aria-valuenow', String(Math.round(pct))); };
      const set = (v: number): void => { pct = Math.max(0, Math.min(100, v)); if (!frame) frame = requestAnimationFrame(paint); };
      const fromX = (x: number): void => { const r = wipe.getBoundingClientRect(); if (r.width) set(((x - r.left) / r.width) * 100); };
      wipe.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        drag = true;
        lastX = e.clientX;
        wipe.setPointerCapture(e.pointerId);
        wipe.classList.add('drag');
        fromX(e.clientX);
      });
      wipe.addEventListener('pointermove', (e) => { if (drag && e.clientX !== lastX) { lastX = e.clientX; fromX(e.clientX); } });
      const end = (): void => { drag = false; wipe.classList.remove('drag'); };
      wipe.addEventListener('pointerup', end);
      wipe.addEventListener('pointercancel', end);
      const top = wipe.querySelector<HTMLElement>('.ag-ly.top');
      root.addEventListener('keydown', (e) => {
        if (e.code === 'Space' && !(e.target as Element).closest('button, input, select, textarea')) { e.preventDefault(); top?.classList.add('hide'); return; }
        if (e.target !== wipe) return;
        const step = e.shiftKey ? 20 : 5;
        if (e.key === 'ArrowLeft') set(pct - step);
        else if (e.key === 'ArrowRight') set(pct + step);
        else if (e.key === 'Home') set(0);
        else if (e.key === 'End') set(100);
        else return;
        e.preventDefault();
      });
      root.addEventListener('keyup', (e) => { if (e.code === 'Space') { e.preventDefault(); top?.classList.remove('hide'); } });
      if (!o.load) return;
      wipe.classList.add('loading');
      o.load().then((x) => {
        if (!wipe.isConnected) return;
        const ly = wipe.querySelectorAll<HTMLElement>('.ag-ly');
        [x.orig, x.aug].forEach((s, i) => {
          const el = ly[i];
          if (!el) return;
          const img = el.querySelector('img'), svg = el.querySelector('svg');
          if (img) img.src = s.img;
          if (svg) { svg.setAttribute('viewBox', `0 0 ${s.w} ${s.h}`); svg.innerHTML = rects(s, o.boxes); }
        });
        wipe.style.aspectRatio = `${x.orig.w} / ${x.orig.h}`;
        wipe.style.setProperty('--ar', String(x.orig.w / x.orig.h));
        const t = root.querySelector('.ag-lbt');
        if (t && x.info !== undefined) t.innerHTML = x.info;
      }).catch((err: unknown) => { if (wipe.isConnected) toast(errText(err), 'err'); })
        .finally(() => wipe.classList.remove('loading'));
    },
  });
}

/** "Ran: Mirror, Motion blur" (escaped). */
export const ranText = (applied: string[], name: (t: string) => string): string => `Ran: ${esc(applied.map(name).join(', ') || 'nothing')}`;
