// The user menu: a round avatar button in each screen's header (the home's .header-actions, the visualizer's
// activity bar above the gear, the label page after its gear) and its popup menu (#user-menu, the .lb-menu
// pattern: role=menu, arrows move, Esc closes and gives the focus back): name + @username, Account settings,
// Users & admin (admins only), Sign out. The avatar follows account edits (`me-changed`).

import { avatarHTML } from '../core/avatar.js';
import { displayName, isAdmin, ME, signOut } from '../core/auth.js';
import { $, esc, icon } from '../core/dom.js';
import { openSettings } from '../settings/dialog.js';

const buttons = new Set<HTMLButtonElement>();
let openFrom: HTMLButtonElement | null = null;

function paint(b: HTMLButtonElement): void {
  if (!ME) return;
  const size = b.classList.contains('act') ? 28 : 26;
  b.innerHTML = avatarHTML(ME, size, { title: false });
  const label = `Account: ${displayName(ME)} (@${ME.username})`;
  b.setAttribute('aria-label', label);
  b.title = label;
}

/** Add the avatar button to `parent` (before `before` when given). cls: the button classes of that header. */
export function mountUserMenu(parent: HTMLElement, before: Node | null = null, cls = 'icon-btn ghost user-btn'): HTMLButtonElement | null {
  if (!ME) return null;
  const b = document.createElement('button');
  b.className = cls;
  b.type = 'button';
  b.setAttribute('aria-haspopup', 'menu');
  b.setAttribute('aria-expanded', 'false');
  paint(b);
  parent.insertBefore(b, before);
  b.addEventListener('click', () => { if (openFrom === b) closeMenu(true); else openMenu(b); });
  buttons.add(b);
  return b;
}

/** Paint an existing button (the visualizer's #act-user in index.html) as the user menu button. */
export function bindUserButton(b: HTMLButtonElement): void {
  if (!ME) { b.classList.add('hidden'); return; }
  b.classList.remove('hidden');
  paint(b);
  b.addEventListener('click', () => { if (openFrom === b) closeMenu(true); else openMenu(b); });
  buttons.add(b);
}

function menuEl(): HTMLElement {
  let m = $('user-menu');
  if (m) return m;
  const menu = m = document.createElement('div');
  menu.id = 'user-menu';
  menu.className = 'lb-menu panel user-menu hidden';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Account');
  document.body.appendChild(menu);
  menu.addEventListener('click', (e) => {
    const it = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-um]') : null;
    if (!it) return;
    const what = it.dataset['um'];
    closeMenu(false);
    if (what === 'account') void openSettings('account');
    else if (what === 'users') void openSettings('users');
    else if (what === 'logout') void signOut();
  });
  menu.addEventListener('keydown', (e) => {
    const its = [...menu.querySelectorAll<HTMLElement>('[role=menuitem]')];
    const i = its.findIndex((x) => x === document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
    else if (e.key === 'Tab') closeMenu(false);
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); its[(i + (e.key === 'ArrowDown' ? 1 : -1) + its.length) % its.length]?.focus(); }
    else if (e.key === 'Home' || e.key === 'End') { e.preventDefault(); its[e.key === 'Home' ? 0 : its.length - 1]?.focus(); }
  });
  document.addEventListener('mousedown', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (openFrom && t && !t.closest('#user-menu') && !openFrom.contains(t)) closeMenu(false);
  });
  window.addEventListener('resize', () => closeMenu(false));
  return menu;
}

function openMenu(from: HTMLButtonElement): void {
  if (!ME) return;
  closeMenu(false);
  const m = menuEl();
  m.innerHTML = `<div class="um-head">${avatarHTML(ME, 36, { title: false })}<div class="um-who"><b>${esc(displayName(ME))}</b>` +
    `<span class="muted">@${esc(ME.username)}${!ME.is_admin && ME.tier ? ` · ${esc(ME.tier.name)}` : ''}</span></div>${ME.is_admin ? `<span class="um-admin">${icon('shield')}admin</span>` : ''}</div>` +
    `<button role="menuitem" data-um="account">${icon('user')}Account settings</button>` +
    (isAdmin() ? `<button role="menuitem" data-um="users">${icon('users')}Users &amp; admin</button>` : '') +
    `<div class="um-sep" role="separator"></div><button role="menuitem" data-um="logout">${icon('logout')}Sign out</button>`;
  m.classList.remove('hidden');
  const r = from.getBoundingClientRect();
  const w = m.offsetWidth, h = m.offsetHeight;
  if (r.left < 80 && r.top > 80) {                 // the visualizer's activity bar (left edge): open to its right
    m.style.left = `${r.right + 6}px`;
    m.style.top = `${Math.max(8, Math.min(innerHeight - h - 8, r.bottom - h))}px`;
  } else {
    m.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.right - w))}px`;
    m.style.top = `${r.bottom + 4 + h <= innerHeight - 8 ? r.bottom + 4 : Math.max(8, r.top - 4 - h)}px`;
  }
  openFrom = from;
  from.setAttribute('aria-expanded', 'true');
  m.querySelector<HTMLElement>('[role=menuitem]')?.focus();
}

function closeMenu(refocus: boolean): void {
  const m = $('user-menu');
  if (!m || m.classList.contains('hidden')) return;
  m.classList.add('hidden');
  const from = openFrom;
  openFrom = null;
  if (!from) return;
  from.setAttribute('aria-expanded', 'false');
  if (refocus && document.contains(from)) from.focus();
}

document.addEventListener('me-changed', () => { for (const b of buttons) if (document.contains(b)) paint(b); });
