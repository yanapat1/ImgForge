// The two splitters of the Data Center: the right edge of the bucket tree (its width) and the top edge of the open
// assistant drawer (its height). Drag with the mouse / pen / touch (pointer capture; the size is written once per
// animation frame as a CSS variable on #dc-app, so the grid re-flows without re-rendering any pane), or focus the
// handle and use the arrow keys (Shift: bigger steps), Home / End. A double click goes back to the default size.
// Sizes are saved in the user's preferences and clamped again when the window changes.

import { pref, rememberPref } from '../core/auth.js';
import { $ } from '../core/dom.js';
import { PREF } from './state.js';

const TREE = { min: 180, max: 560 };

interface Split {
  handle: HTMLElement;
  /** dragging right / up makes it bigger: +1, else -1 */
  sign: 1 | -1;
  axis: 'x' | 'y';
  now: () => number;
  clamp: (px: number) => number;
  apply: (px: number | null) => void;
  save: string;
}

function bind(s: Split): void {
  const { handle } = s;
  let frame = 0, want = 0;
  const paint = (): void => { frame = 0; s.apply(want); setAria(); };
  const setAria = (): void => { handle.setAttribute('aria-valuenow', String(Math.round(s.now()))); };
  const set = (px: number, save: boolean): void => {
    want = s.clamp(px);
    if (!frame) frame = requestAnimationFrame(paint);
    if (save) rememberPref(s.save, Math.round(want));
  };
  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add('resizing');
    const start = s.now(), p0 = s.axis === 'x' ? e.clientX : e.clientY;
    let moved = false;
    const move = (ev: PointerEvent): void => {
      const d = (s.axis === 'x' ? ev.clientX : ev.clientY) - p0;
      if (!moved && Math.abs(d) < 2) return;
      moved = true;
      set(start + s.sign * d, false);
    };
    const up = (): void => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing');
      if (moved) rememberPref(s.save, Math.round(want));
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('dblclick', () => { s.apply(null); rememberPref(s.save, null); setAria(); });
  handle.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 64 : 16;
    const grow: Record<string, number> = s.axis === 'x' ? { ArrowRight: 1, ArrowLeft: -1 } : { ArrowUp: 1, ArrowDown: -1 };
    const g = grow[e.key];
    if (g !== undefined) { e.preventDefault(); set(s.now() + g * step, true); }
    else if (e.key === 'Home') { e.preventDefault(); set(0, true); }
    else if (e.key === 'End') { e.preventDefault(); set(1e6, true); }
  });
  const saved = pref(s.save);
  if (typeof saved === 'number' && saved > 0) s.apply(s.clamp(saved));
}

/** Bind both splitters (once, after the page skeleton is in place). */
export function bindResize(): void {
  const app = $('dc-app'), tree = $('dc-tree');
  const th = $('dc-split-tree');
  if (!app || !tree || !th) return;
  const treeMax = (): number => Math.max(TREE.min, Math.min(TREE.max, app.clientWidth - 480));
  bind({
    handle: th, sign: 1, axis: 'x', save: PREF.treeW,
    now: () => tree.getBoundingClientRect().width,
    clamp: (px) => Math.round(Math.max(TREE.min, Math.min(treeMax(), px))),
    apply: (px) => { if (px == null) app.style.removeProperty('--dc-tree-w'); else app.style.setProperty('--dc-tree-w', `${px}px`); },
  });
  th.setAttribute('aria-valuemin', String(TREE.min));
  th.setAttribute('aria-valuemax', String(TREE.max));
}
