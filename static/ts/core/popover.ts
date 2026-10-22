// Anchored popovers: a trigger button + a .popover panel (in a .popover-anchor). Click toggles, a click outside or
// Escape closes (focus back on the trigger), aria-expanded follows. Used by the bar above the plot and the filter bar.

import { $req } from './dom.js';

export interface Popover { open: () => void; close: () => void; isOpen: () => boolean }

const all: Popover[] = [];

/** Bind trigger `btnId` to panel `popId`; onOpen runs before it shows (fill it), onClose after it hides. */
export function bindPopover(btnId: string, popId: string, onOpen?: () => void, onClose?: () => void): Popover {
  const btn = $req(btnId), pop = $req(popId);
  const isOpen = (): boolean => !pop.classList.contains('hidden');
  const setOpen = (open: boolean): void => {
    if (open === isOpen()) return;
    if (open) {
      for (const p of all) if (p !== self) p.close();       // one popover at a time
      if (onOpen) onOpen();
    }
    pop.classList.toggle('hidden', !open);
    btn.setAttribute('aria-expanded', String(open));
    if (!open && onClose) onClose();
  };
  const self: Popover = { open: () => setOpen(true), close: () => setOpen(false), isOpen };
  all.push(self);
  btn.addEventListener('click', () => setOpen(!isOpen()));
  document.addEventListener('mousedown', (e) => {
    const t = e.target;
    if (isOpen() && t instanceof Node && !pop.contains(t) && !btn.contains(t)) setOpen(false);
  });
  pop.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); btn.focus(); } });
  btn.addEventListener('keydown', (e) => { if (e.key === 'Escape' && isOpen()) { e.stopPropagation(); setOpen(false); } });
  return self;
}

/** Arrow keys move between the enabled buttons of a menu-like popover (Home / End jump). */
export function arrowNav(popId: string, selector = 'button:not([disabled]):not(.hidden)'): void {
  const pop = $req(popId);
  pop.addEventListener('keydown', (e) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
    const t = e.target;
    if (t instanceof HTMLSelectElement || (t instanceof HTMLInputElement && t.type !== 'checkbox')) return;
    const items = [...pop.querySelectorAll<HTMLElement>(selector)].filter((b) => b.offsetParent !== null);
    if (!items.length) return;
    e.preventDefault();
    const i = items.indexOf(document.activeElement as HTMLElement);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1
      : (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  });
}
