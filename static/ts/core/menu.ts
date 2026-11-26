// Menus with sub-menus, as in a desktop right-click menu. `item()` / `menuHtml()` build the rows (separators between
// non-empty groups); `sub()` a row that opens a nested menu (hidden when none of its rows is shown). `bindMenu(root)`
// makes the nested menus work once per menu element:
//   - pointer: hovering a sub-menu row opens it after a short delay; leaving it closes it after a longer one, unless
//     the pointer is travelling towards the open sub-menu (the "safe triangle" from the last pointer point to the
//     sub-menu's near corners), so a diagonal move across other rows does not close it. A click / tap toggles at once.
//   - keyboard: ArrowRight / Enter / Space on a sub-menu row opens it and focuses its first row; ArrowLeft / Escape
//     inside a sub-menu closes it and focuses its row again; ArrowUp / ArrowDown / Home / End move within one level.
//   - placement: to the right of its row, flipped to the left near the screen edge, clamped vertically.
// The nested menus are children of `root`, so a popover's "press outside closes" test treats them as inside.

import { esc, icon } from './dom.js';

export interface MenuItem {
  act: string; ico: string; label: string; data?: Record<string, string>; dis?: string | false;
  kbd?: string; hint?: string; danger?: boolean; checked?: boolean;
}
type Row = string | false | null | undefined;

export const SEP = '<div class="sep" role="separator"></div>';

export function item(o: MenuItem): string {
  const data = Object.entries(o.data ?? {}).map(([k, v]) => ` data-${k}="${esc(v)}"`).join('');
  const role = o.checked === undefined ? 'menuitem' : 'menuitemradio';
  const ico = o.checked === undefined ? icon(o.ico) : o.checked ? icon('check') : '<span class="ico"></span>';
  return `<button type="button" role="${role}"${o.checked !== undefined ? ` aria-checked="${o.checked}"` : ''} data-act="${o.act}"${data}` +
    `${o.danger ? ' class="danger"' : ''}${o.dis ? ` disabled title="${esc(o.dis)}"` : ''}>${ico}${esc(o.label)}` +
    `${o.kbd ? `<span class="kbd">${esc(o.kbd)}</span>` : o.hint ? `<span class="faint">${esc(o.hint)}</span>` : ''}</button>`;
}

/** Groups of rows -> HTML: empty rows dropped, a separator between the groups that are left. */
export const menuHtml = (groups: Row[][]): string => groups.map((g) => g.filter(Boolean).join('')).filter(Boolean).join(SEP);

let subSeq = 0;
/** A row that opens a nested menu of `groups`; '' when no row of it is shown (the whole entry is left out). A disabled
 *  row inside keeps its reason (title), so the sub-menu still opens to show why. */
export function sub(o: { ico: string; label: string; groups: Row[][]; hint?: string }): string {
  const html = menuHtml(o.groups);
  if (!html) return '';
  const id = `mi-sub-${++subSeq}`;
  return `<div class="mi-sub" role="none"><button type="button" role="menuitem" aria-haspopup="menu" aria-expanded="false" aria-controls="${id}" data-sub>` +
    `${icon(o.ico)}${esc(o.label)}${o.hint ? `<span class="faint">${esc(o.hint)}</span>` : ''}<span class="mi-chev">${icon('chev-r')}</span></button>` +
    `<div id="${id}" class="bk-menu menu-sub hidden" role="menu" aria-label="${esc(o.label)}">${html}</div></div>`;
}

interface Pt { x: number; y: number }
interface Box { left: number; right: number; top: number; bottom: number }

/** Where a sub-menu of size w x h opens beside its row `row` (inside the menu `menu`) in a vw x vh viewport: to the
 *  right, or flipped to the left of the menu when there is no room; its top at the row, clamped on screen. */
export function placeSub(row: Box, menu: Box, w: number, h: number, vw: number, vh: number): { left: number; top: number; flipped: boolean } {
  const right = row.right - 2;
  const flipped = right + w > vw - 8 && menu.left - w + 2 >= 8;
  const left = flipped ? menu.left - w + 2 : Math.max(8, Math.min(right, vw - 8 - w));
  const top = Math.max(8, Math.min(row.top - 4, vh - 8 - h));
  return { left, top, flipped };
}

/** Whether point p lies inside the triangle a, b, c (edges included). */
export function inTriangle(p: Pt, a: Pt, b: Pt, c: Pt): boolean {
  const s = (p1: Pt, p2: Pt, p3: Pt): number => (p1.x - p3.x) * (p2.y - p3.y) - (p2.x - p3.x) * (p1.y - p3.y);
  const d1 = s(p, a, b), d2 = s(p, b, c), d3 = s(p, c, a);
  const neg = d1 < 0 || d2 < 0 || d3 < 0, pos = d1 > 0 || d2 > 0 || d3 > 0;
  return !(neg && pos);
}

const OPEN_MS = 120, CLOSE_MS = 300;
const bound = new WeakMap<HTMLElement, MenuCtl>();

export interface MenuCtl {
  /** Close the deepest open sub-menu (focus back on its row) -> whether one was open (Escape closes that first). */
  closeDeepest(): boolean;
  /** Close every sub-menu (the menu was filled again or closed). */
  reset(): void;
}

const rowsOf = (menu: Element): HTMLElement[] =>
  [...menu.querySelectorAll<HTMLElement>(':scope > button:not([disabled]), :scope > .mi-sub > button')];

/** Make the sub-menus of menu element `root` work (once per element; the rows may be replaced any time). */
export function bindMenu(root: HTMLElement): MenuCtl {
  const had = bound.get(root);
  if (had) return had;
  let openT = 0, closeT = 0;
  let last: Pt | null = null;

  const subOf = (btn: Element): HTMLElement | null => btn.nextElementSibling instanceof HTMLElement ? btn.nextElementSibling : null;
  const levelOf = (el: Element): HTMLElement => el.closest<HTMLElement>('.menu-sub') ?? root;
  const openRows = (): HTMLElement[] => [...root.querySelectorAll<HTMLElement>('[data-sub][aria-expanded="true"]')];

  const close = (btn: HTMLElement): void => {
    const m = subOf(btn);
    if (m) for (const b of m.querySelectorAll<HTMLElement>('[data-sub][aria-expanded="true"]')) close(b);
    btn.setAttribute('aria-expanded', 'false');
    m?.classList.add('hidden');
  };
  const open = (btn: HTMLElement, focus = false): void => {
    const m = subOf(btn);
    if (!m) return;
    const level = levelOf(btn);
    for (const b of level.querySelectorAll<HTMLElement>(':scope > .mi-sub > [data-sub][aria-expanded="true"]')) if (b !== btn) close(b);
    btn.setAttribute('aria-expanded', 'true');
    m.classList.remove('hidden');
    const p = placeSub(btn.getBoundingClientRect(), level.getBoundingClientRect(), m.offsetWidth, m.offsetHeight, innerWidth, innerHeight);
    m.style.left = `${p.left}px`;
    m.style.top = `${p.top}px`;
    m.classList.toggle('flipped', p.flipped);
    if (focus) rowsOf(m)[0]?.focus();
  };
  const clear = (): void => { clearTimeout(openT); clearTimeout(closeT); };

  /** the pointer heads for the open sub-menu of `btn`: keep it open a while longer */
  const heading = (btn: HTMLElement, p: Pt): boolean => {
    const m = subOf(btn);
    if (!m || !last || m.classList.contains('hidden')) return false;
    const r = m.getBoundingClientRect();
    const nearX = m.classList.contains('flipped') ? r.right : r.left;
    return inTriangle(p, last, { x: nearX, y: r.top }, { x: nearX, y: r.bottom });
  };

  root.addEventListener('pointermove', (e) => {
    const p = { x: e.clientX, y: e.clientY };
    const t = e.target instanceof Element ? e.target : null;
    const btn = t?.closest<HTMLElement>('button');
    const level = t ? levelOf(t) : root;
    const openHere = [...level.querySelectorAll<HTMLElement>(':scope > .mi-sub > [data-sub][aria-expanded="true"]')][0];
    if (e.pointerType !== 'touch' && btn && root.contains(btn)) {
      if (btn.hasAttribute('data-sub')) {
        clear();
        if (btn.getAttribute('aria-expanded') !== 'true') openT = window.setTimeout(() => open(btn), OPEN_MS);
      } else if (openHere && levelOf(btn) === level && !heading(openHere, p)) {
        clear();
        closeT = window.setTimeout(() => close(openHere), CLOSE_MS);
      } else if (openHere && heading(openHere, p)) {
        clearTimeout(closeT);
      }
    }
    last = p;
  });
  root.addEventListener('click', (e) => {
    const btn = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-sub]') : null;
    if (!btn || !root.contains(btn)) return;
    e.preventDefault();
    clear();
    if (btn.getAttribute('aria-expanded') === 'true') close(btn); else open(btn, e.detail === 0);
  });
  root.addEventListener('keydown', (e) => {
    const t = document.activeElement instanceof HTMLElement && root.contains(document.activeElement) ? document.activeElement : null;
    if (!t) return;
    const level = levelOf(t);
    if (t.hasAttribute('data-sub') && ['ArrowRight', 'Enter', ' '].includes(e.key)) {
      e.preventDefault();
      e.stopPropagation();
      clear();
      open(t, true);
      return;
    }
    if (e.key === 'ArrowLeft' && level !== root) {
      e.preventDefault();
      e.stopPropagation();
      const row = level.previousElementSibling as HTMLElement | null;
      if (row) { close(row); row.focus(); }
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
    const rows = rowsOf(level);
    if (!rows.length) return;
    e.preventDefault();
    e.stopPropagation();
    const i = rows.indexOf(t);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length - 1 : (i + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
    rows[next]?.focus();
  });

  const ctl: MenuCtl = {
    closeDeepest() {
      const rows = openRows();
      const deepest = rows[rows.length - 1];
      if (!deepest) return false;
      close(deepest);
      deepest.focus();
      return true;
    },
    reset() { clear(); for (const b of openRows()) close(b); last = null; },
  };
  bound.set(root, ctl);
  return ctl;
}
