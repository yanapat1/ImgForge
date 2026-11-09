// Favorite tables at the top of the Objects panel, in the user's own categories. Kept in the user's prefs
// (/api/me/prefs key analysis_favorites: every session, every browser). A row is the same table button as in the
// tree (click = columns, double-click = query, drag = builder), with its own open key (ft|...) for the columns.
// The star beside a table, its right-click menu, or a drag onto a category header add / move it.

import { pref, setPrefs } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { promptDialog } from '../views/dialog.js';
import { chip, renderTree, tableRow } from './app.js';
import { menuItem, openMenu } from './menu.js';
import { A, tableKey } from './state.js';

/** One favorite: connection, database, schema, table, category ('' = no category). */
export interface Fav { c: string; d: string; s: string; t: string; g: string }

const KEY = 'analysis_favorites';
export const FAV_LIMITS = { items: 300, groups: 40, name: 60 };
const ALL = '';                                   // the closed-set entry of the whole section (a category is never '')
const F = { groups: [] as string[], items: [] as Fav[], closed: new Set<string>() };
let index = new Map<string, Fav>();

const keyOf = (f: { c: string; d: string; s: string; t: string }): string => tableKey(f.c, f.d, f.s, f.t);
const str = (v: unknown, n = 200): string => (typeof v === 'string' ? v.slice(0, n) : '');
export const isFav = (k: string): boolean => index.has(k);

/** Read the stored favorites (validated: unknown categories, duplicates and extras are dropped). */
export function loadFavs(): void {
  const raw = pref(KEY);
  const r = (raw && typeof raw === 'object' ? raw : {}) as { groups?: unknown; items?: unknown; closed?: unknown };
  const groups = Array.isArray(r.groups) ? r.groups.map((g) => str(g).trim().slice(0, FAV_LIMITS.name)).filter(Boolean) : [];
  F.groups = [...new Set(groups)].slice(0, FAV_LIMITS.groups);
  const known = new Set(F.groups);
  F.items = [];
  index = new Map();
  for (const x of Array.isArray(r.items) ? r.items as Record<string, unknown>[] : []) {
    if (!x || typeof x !== 'object' || F.items.length >= FAV_LIMITS.items) continue;
    const g = str(x['g']);
    const f: Fav = { c: str(x['c'], 64), d: str(x['d']), s: str(x['s']), t: str(x['t']), g: known.has(g) ? g : '' };
    if (!f.c || !f.t || index.has(keyOf(f))) continue;
    F.items.push(f);
    index.set(keyOf(f), f);
  }
  F.closed = new Set(Array.isArray(r.closed) ? (r.closed as unknown[]).map((g) => str(g)).filter((g) => g === ALL || known.has(g)) : []);
}

function save(): void {
  renderTree();
  setPrefs({ [KEY]: { groups: F.groups, items: F.items, closed: [...F.closed] } })
    .catch((e: unknown) => toast(`Favorites were not saved: ${e instanceof Error ? e.message : String(e)}`, 'err'));
}

const refOf = (el: HTMLElement): Fav =>
  ({ c: el.dataset['conn'] ?? '', d: el.dataset['db'] ?? '', s: el.dataset['schema'] ?? '', t: el.dataset['name'] ?? '', g: '' });

/** Add `f` to category `g`, or move it there when it is already a favorite. */
function put(f: Fav, g: string): boolean {
  const cur = index.get(keyOf(f));
  if (cur) { cur.g = g; return true; }
  if (F.items.length >= FAV_LIMITS.items) { toast(`Favorites hold up to ${FAV_LIMITS.items} tables: remove one first`, 'err'); return false; }
  const n = { ...f, g };
  F.items.push(n);
  index.set(keyOf(n), n);
  return true;
}

function drop(k: string): void {
  const f = index.get(k);
  if (!f) return;
  F.items.splice(F.items.indexOf(f), 1);
  index.delete(k);
}

function nameCheck(cur: string | null) {
  return (v: string): string | null => {
    if (v.length > FAV_LIMITS.name) return `Keep it to ${FAV_LIMITS.name} characters`;
    if (v !== cur && F.groups.some((g) => g.toLowerCase() === v.toLowerCase())) return 'There is a category with this name';
    return null;
  };
}

async function newGroup(f: Fav | null): Promise<void> {
  if (F.groups.length >= FAV_LIMITS.groups) { toast(`Up to ${FAV_LIMITS.groups} categories`, 'err'); return; }
  const name = await promptDialog({ title: 'New category', label: 'Name', ok: 'Create', check: nameCheck(null),
    hint: f ? `${f.s ? `${f.s}.` : ''}${f.t} goes into it.` : 'Then star tables, right-click them or drag them onto it.' });
  if (!name) return;
  F.groups.push(name);
  if (f) put(f, name);
  F.closed.delete(ALL);
  save();
}

async function renameGroup(g: string): Promise<void> {
  const name = await promptDialog({ title: 'Rename category', label: 'Name', value: g, ok: 'Rename', check: nameCheck(g) });
  if (!name || name === g) return;
  F.groups[F.groups.indexOf(g)] = name;
  for (const f of F.items) if (f.g === g) f.g = name;
  if (F.closed.delete(g)) F.closed.add(name);
  save();
}

// ---------------------------------------------------------------- the section
type Hit = (t: { schema: string; name: string }) => boolean;

function favRow(f: Fav, pad: number, live: Set<string>): string {
  const full = f.s ? `${f.s}.${f.t}` : f.t;
  if (!live.has(f.c)) {
    return `<div class="an-trow an-fmiss" title="Its connection was removed"><span class="an-tn" style="padding-left:${pad}px"><span class="an-tw"></span><span class="lbl">${esc(full)}</span><span class="faint an-n">connection removed</span></span>` +
      `<button type="button" class="icon-btn ghost xs an-star on" data-act="fav" data-conn="${esc(f.c)}" data-db="${esc(f.d)}" data-schema="${esc(f.s)}" data-name="${esc(f.t)}" aria-pressed="true" aria-label="Remove ${esc(full)} from favorites" title="Remove from favorites">${icon('star-on')}</button></div>`;
  }
  const kind = A.sources.find((s) => s.id === f.c)?.kind ?? '';
  return tableRow(f.c, f.d, { schema: f.s, name: f.t }, pad, 'ft',
    `${chip(kind)}<span class="lbl">${esc(full)}</span>${f.d ? `<span class="faint an-n">${esc(f.d)}</span>` : ''}`);
}

const head = (g: string, label: string, n: number, pad: number, open: boolean, extra: string): string =>
  `<div class="an-trow an-fhead an-fdrop" data-g="${esc(g)}"><button type="button" class="an-tn" style="padding-left:${pad}px" data-act="fav-tog" data-g="${esc(g)}" aria-expanded="${open}">` +
  `<span class="an-tw">${open ? '▾' : '▸'}</span>${label}<span class="faint an-n">${n}</span></button>${extra}</div>`;

/** The Favorites section of the Objects panel ('' when there is nothing to show yet and no connection either). */
export function renderFavs(hit: Hit): string {
  if (!F.items.length && !F.groups.length && !A.sources.length) return '';
  const live = new Set(A.sources.map((s) => s.id));
  const by = new Map<string, Fav[]>([[ALL, []], ...F.groups.map((g): [string, Fav[]] => [g, []])]);
  const name = (f: Fav): string => `${f.s}.${f.t}`.toLowerCase();
  for (const f of F.items) if (hit({ schema: f.s, name: f.t })) by.get(f.g)?.push(f);
  for (const list of by.values()) list.sort((a, b) => name(a).localeCompare(name(b)));
  const open = !F.closed.has(ALL) || !!A.filter;
  const shown = [...by.values()].reduce((n, l) => n + l.length, 0);
  let h = head(ALL, `${icon('star-on')}<span class="lbl"><b>Favorites</b></span>`, shown, 4, open,
    `<button type="button" class="icon-btn ghost xs" data-act="fav-new" aria-label="New category" title="New category">${icon('folder-plus')}</button>`);
  if (open) {
    if (!F.items.length) h += '<p class="an-fnote faint">Star a table (☆ beside it) or right-click it to keep it here.</p>';
    h += (by.get(ALL) ?? []).map((f) => favRow(f, 18, live)).join('');
    for (const g of F.groups) {
      const list = by.get(g) ?? [];
      if (A.filter && !list.length) continue;
      const go = !F.closed.has(g) || !!A.filter;
      h += head(g, `${icon(go ? 'folder' : 'folder-o')}<span class="lbl">${esc(g)}</span>`, list.length, 18, go,
        `<button type="button" class="icon-btn ghost xs an-fmore" data-act="fav-gmenu" data-g="${esc(g)}" aria-label="Category ${esc(g)}: options" title="Rename, move, delete">${icon('more')}</button>`);
      if (go) h += list.length ? list.map((f) => favRow(f, 32, live)).join('') : '<p class="an-fnote faint" style="padding-left:52px">Drag tables here.</p>';
    }
  }
  return `<div class="an-fsec">${h}</div>`;
}

// ---------------------------------------------------------------- menus
let menuFor: Fav | null = null;
let menuGroup = '';

const item = menuItem;

/** The favorites menu of a table row (right-click, or the context-menu key). */
export function favMenu(el: HTMLElement, at?: { x: number; y: number }): void {
  const f = refOf(el);
  if (!f.c || !f.t) return;
  menuFor = f;
  const cur = index.get(keyOf(f));
  const full = esc(f.s ? `${f.s}.${f.t}` : f.t);
  let h = `<div class="an-menu-h">${full}</div>`;
  h += cur ? '' : item('add', 'Add to Favorites', 'star', ALL);
  if (F.groups.length) {
    h += `<div class="an-menu-h">${cur ? 'Move to category' : 'Add to category'}</div>`;
    if (cur && cur.g) h += item('add', 'No category', 'star', ALL);
    h += F.groups.map((g) => item('add', esc(g), cur?.g === g ? 'check' : 'folder', g, cur?.g === g ? 'on' : '')).join('');
  }
  h += item('new', 'New category…', 'folder-plus');
  if (cur) h += `<div class="sep"></div>${item('remove', 'Remove from Favorites', 'x', '', 'danger')}`;
  if (!at) { const r = el.getBoundingClientRect(); at = { x: r.left + 24, y: r.bottom }; }
  openMenu(h, el, menuAct, at);
}

async function menuAct(m: string, g: string): Promise<void> {
  const f = menuFor;
  const i = F.groups.indexOf(menuGroup);
  if (m === 'add' && f) { if (put(f, g)) save(); }
  else if (m === 'new') await newGroup(f);
  else if (m === 'remove' && f) { drop(keyOf(f)); save(); }
  else if (m === 'rename') await renameGroup(menuGroup);
  else if ((m === 'up' || m === 'down') && i >= 0) {
    const j = m === 'up' ? i - 1 : i + 1;
    if (j < 0 || j >= F.groups.length) return;
    [F.groups[i], F.groups[j]] = [F.groups[j] as string, F.groups[i] as string];
    save();
  } else if (m === 'delete' && i >= 0) {
    F.groups.splice(i, 1);
    F.closed.delete(menuGroup);
    for (const x of F.items) if (x.g === menuGroup) x.g = '';
    save();
  }
}

/** A table dragged onto the Favorites header or a category header. */
export function favDrop(d: Record<string, string>, g: string): void {
  const f: Fav = { c: d['conn'] ?? '', d: d['db'] ?? '', s: d['schema'] ?? '', t: d['name'] ?? '', g: '' };
  if (!f.c || !f.t || (g && !F.groups.includes(g))) return;
  F.closed.delete(ALL);
  if (g) F.closed.delete(g);
  if (put(f, g)) save();
}

export const F_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  fav: (el) => {
    const f = refOf(el), k = keyOf(f);
    if (isFav(k)) drop(k); else if (!put(f, ALL)) return;
    save();
  },
  'fav-new': () => newGroup(null),
  'fav-tog': (el) => {
    const g = el.dataset['g'] ?? '';
    if (A.filter) return;                                     // a search shows every match, open
    if (!F.closed.delete(g)) F.closed.add(g);
    save();
  },
  'fav-gmenu': (el) => {
    menuGroup = el.dataset['g'] ?? '';
    menuFor = null;
    const i = F.groups.indexOf(menuGroup);
    openMenu(`<div class="an-menu-h">${esc(menuGroup)}</div>` + item('rename', 'Rename…', 'edit') +
      (i > 0 ? item('up', 'Move up', 'chev-u') : '') + (i < F.groups.length - 1 ? item('down', 'Move down', 'chev-d') : '') +
      `<div class="sep"></div>${item('delete', 'Delete category', 'trash', '', 'danger')}<div class="an-menu-note faint">Its tables stay in Favorites.</div>`, el, menuAct);
  },
};
