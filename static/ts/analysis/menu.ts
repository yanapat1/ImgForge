// The page's one floating menu (#an-menu): Favorites' table / category menus and the builder's card menu. Built once,
// placed with floatPopover (a point for a right-click, else under the button), arrow keys move, a pick closes it and
// calls the opener's onPick with the item's data-m / data-g.

import { esc, icon } from '../core/dom.js';
import { arrowNav, closeFloat, floatPopover } from '../core/popover.js';

type Pick = (m: string, g: string) => void | Promise<void>;
let onPick: Pick | null = null;

function menuEl(): HTMLElement {
  let m = document.getElementById('an-menu');
  if (m) return m;
  m = document.createElement('div');
  m.id = 'an-menu';
  m.className = 'an-menu hidden';
  m.setAttribute('role', 'menu');
  m.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLButtonElement>('button[data-m]') : null;
    if (!b || b.disabled) return;
    const fn = onPick;
    closeFloat();
    void fn?.(b.dataset['m'] ?? '', b.dataset['g'] ?? '');
  });
  document.body.appendChild(m);
  arrowNav('an-menu', 'button:not([disabled])');
  return m;
}

/** One item: `label` is HTML (escape it), `m` the action, `g` an argument. */
export const menuItem = (m: string, label: string, ic: string, g = '', cls = ''): string =>
  `<button type="button" role="menuitem" class="${cls}" data-m="${m}" data-g="${esc(g)}">${icon(ic)}<span class="lbl">${label}</span></button>`;
export const menuHead = (text: string): string => `<div class="an-menu-h">${esc(text)}</div>`;
export const MENU_SEP = '<div class="sep"></div>';

/** Open the menu with `html` beside `from` (or at `at`, a right-click's point); the first item takes the focus. */
export function openMenu(html: string, from: HTMLElement, pick: Pick, at?: { x: number; y: number }): void {
  const m = menuEl();
  m.innerHTML = html;
  onPick = pick;
  floatPopover(m, from, () => { onPick = null; }, 'right', at);
  m.querySelector<HTMLElement>('button:not([disabled])')?.focus();
}
