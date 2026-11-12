// Notes on the visual builder's board: zones, text, sticky notes and shapes, one kind of element (Note) for all of them.
// A zone is a shape behind the table cards (layer back), a text label a shape with no fill or border, a sticky note a
// filled shape in front. Notes never change the SQL: they are saved with the tab (its model, so undo covers them) and the
// server never reads them. A note (a zone too) drags from anywhere on it; the table cards sit above zones, so they stay
// easy to pick. With "move cards with zone" a zone carries the cards inside it. A locked note does not move or resize
// (a press only selects it, so the format bar can unlock it). Any note rotates about its centre (the round handle above it,
// the degrees box on the format bar, [ and ]); x, y, w, h stay the unrotated box.

import { esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import { saveTabs } from './app.js';
import { MENU_SEP, menuHead, menuItem, openMenu } from './menu.js';
import { tab } from './state.js';
import type { MTable, Model, Note, NoteShape, Tab, TableInfo } from './state.js';

export const BOARD_W = 2400, BOARD_H = 1600, MAX_NOTES = 300, MAX_TEXT = 2000;
/** Notes go anywhere on the board, like the table cards (left of and above its first corner too), within this. */
export const SPAN = 20000;
export const PALETTE = ['blue', 'green', 'amber', 'red', 'violet', 'teal', 'grey', 'yellow'] as const;
const SHAPES: [NoteShape, string][] = [['rect', 'Rectangle'], ['round', 'Rounded'], ['pill', 'Pill'], ['ellipse', 'Ellipse'], ['diamond', 'Diamond'],
  ['hexagon', 'Hexagon'], ['callout', 'Callout'], ['none', 'Text only']];
const SVG_PATH: Partial<Record<NoteShape, string>> = {
  ellipse: 'M50 1A49 49 0 1 1 49.99 1Z',
  diamond: 'M50 1L99 50L50 99L1 50Z',
  hexagon: 'M25 1H75L99 50L75 99H25L1 50Z',
  callout: 'M4 1H96Q99 1 99 4V72Q99 75 96 75H40L22 99L27 75H4Q1 75 1 72V4Q1 1 4 1Z',
};
const STRIP = 28, SNAP = 8, MIN_W = 40, MIN_H = 24;

/** The hooks the builder gives: its model, undo snapshot, a redraw of the join lines, and the card selection. */
interface Host {
  modelOf: (t: Tab) => Model; record: (t: Tab) => void; drawLines: () => void; clearCards: () => void; cards: () => MTable[];
  info: (m: MTable) => TableInfo | undefined; redraw: () => void;
}
let H: Host | null = null;
export function bindBoard(h: Host): void { H = h; }

/** The notes selected (ids); the builder's own selection is of table cards. */
export const NS = { sel: [] as string[], sliding: false };
const notesOf = (t: Tab): Note[] => (H ? (H.modelOf(t).notes ??= []) : []);
const byId = (t: Tab, id: string): Note | undefined => notesOf(t).find((n) => n.id === id);
const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v));
const snap = (v: number, on: boolean): number => (on ? Math.round(v / SNAP) * SNAP : Math.round(v));
/** An angle in degrees as -180..180, one decimal; 0 for anything not a number. */
export const normRot = (r: number): number => { if (!Number.isFinite(r)) return 0; const v = Math.round((((r + 180) % 360 + 360) % 360 - 180) * 10) / 10; return v === -180 ? 180 : v; };
const RAD = Math.PI / 180;
/** The box a note covers on the board, rotation included (the format bar, carried cards). */
function bounds(n: Note): { x: number; y: number; w: number; h: number } {
  if (!n.rot) return { x: n.x, y: n.y, w: n.w, h: n.h };
  const c = Math.abs(Math.cos(n.rot * RAD)), s = Math.abs(Math.sin(n.rot * RAD)), w = n.w * c + n.h * s, h = n.w * s + n.h * c;
  return { x: n.x + n.w / 2 - w / 2, y: n.y + n.h / 2 - h / 2, w, h };
}
/** Is board point (px, py) inside the note (its rotated shape's box)? */
function contains(n: Note, px: number, py: number): boolean {
  let dx = px - (n.x + n.w / 2), dy = py - (n.y + n.h / 2);
  if (n.rot) { const c = Math.cos(n.rot * RAD), s = Math.sin(n.rot * RAD); [dx, dy] = [dx * c + dy * s, -dx * s + dy * c]; }
  return Math.abs(dx) <= n.w / 2 && Math.abs(dy) <= n.h / 2;
}
const newId = (): string => (globalThis.crypto && 'randomUUID' in globalThis.crypto ? crypto.randomUUID() : `n${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`);

export function colorOf(f: string): string {
  if ((PALETTE as readonly string[]).includes(f)) return `var(--note-${f})`;
  return /^#[0-9a-f]{6}$/i.test(f) ? f : 'transparent';
}

/** A note with every field set and inside its limits (a saved tab, the assistant's edit, an old version). */
export function cleanNote(x: Partial<Note>, id?: string): Note {
  const shape = SHAPES.some(([k]) => k === x.shape) ? x.shape as NoteShape : 'rect';
  const w = clamp(Number(x.w) || 240, MIN_W, BOARD_W), h = clamp(Number(x.h) || 140, MIN_H, BOARD_H);
  const n: Note = {
    id: String(x.id || id || newId()).slice(0, 64), shape,
    x: clamp(Math.round(Number(x.x) || 0), -SPAN, SPAN), y: clamp(Math.round(Number(x.y) || 0), -SPAN, SPAN), w: Math.round(w), h: Math.round(h),
    fill: typeof x.fill === 'string' && ((PALETTE as readonly string[]).includes(x.fill) || /^#[0-9a-f]{6}$/i.test(x.fill)) ? x.fill : '',
    alpha: clamp(x.alpha === undefined ? 0.14 : Number(x.alpha), 0, 1),
    border: x.border === 'dashed' || x.border === 'none' ? x.border : 'solid',
    text: String(x.text ?? '').slice(0, MAX_TEXT), size: clamp(Math.round(Number(x.size) || 14), 11, 48),
    ha: x.ha === 'center' || x.ha === 'right' ? x.ha : 'left', va: x.va === 'middle' || x.va === 'bottom' ? x.va : 'top',
    layer: x.layer === 'front' ? 'front' : 'back',
  };
  const rot = normRot(Number(x.rot) || 0);
  if (rot) n.rot = rot;
  if (x.bold) n.bold = true;
  if (x.locked) n.locked = true;
  if (x.carry) n.carry = true;
  if (x.auto) n.auto = true;
  if (typeof x.pin === 'string' && x.pin) n.pin = x.pin.slice(0, 64);
  return n;
}

// ---------------------------------------------------------------- presets
type Preset = Partial<Note> & { label: string; ic: string };
const PRESETS: Record<string, Preset> = {
  zone: { label: 'Zone', ic: 'grid', shape: 'rect', layer: 'back', fill: 'blue', alpha: 0.14, border: 'dashed', text: 'Zone', size: 15, bold: true, carry: true, w: 420, h: 300 },
  text: { label: 'Text', ic: 'text', shape: 'none', layer: 'front', fill: '', alpha: 0, border: 'none', text: 'Text', size: 14, w: 200, h: 48 },
  title: { label: 'Title', ic: 'text', shape: 'none', layer: 'front', fill: '', alpha: 0, border: 'none', text: 'Title', size: 28, bold: true, w: 360, h: 56 },
  sticky: { label: 'Sticky note', ic: 'edit', shape: 'round', layer: 'front', fill: 'yellow', alpha: 1, border: 'none', text: 'Note', size: 13, w: 180, h: 120 },
};
const SHAPE_PRESETS: NoteShape[] = ['ellipse', 'diamond', 'hexagon', 'callout', 'pill'];

/** Add a note of a preset (or a shape) at board point (x, y) = its centre, or the middle of what is on screen. */
export function addNote(kind: string, at?: { x: number; y: number }): Note | null {
  const t = tab(), list = notesOf(t);
  if (list.length >= MAX_NOTES) { toast(`A board holds ${MAX_NOTES} notes`, 'err'); return null; }
  const p: Partial<Note> = PRESETS[kind] ?? { shape: kind as NoteShape, layer: 'front', fill: 'teal', alpha: 0.22, border: 'solid', text: '', size: 14, ha: 'center', va: 'middle', w: 200, h: 120 };
  const c = at ?? screenMiddle(t);
  const n = cleanNote({ ...p, x: (c.x - (p.w ?? 200) / 2), y: (c.y - (p.h ?? 120) / 2) });
  H?.record(t);
  list.push(n);
  NS.sel = [n.id];
  H?.clearCards();
  saveTabs();
  renderNotes();
  return n;
}

function screenMiddle(t: Tab): { x: number; y: number } {
  const cv = document.getElementById('dt-canvas'), v = t.view;
  const w = cv?.clientWidth ?? 800, h = cv?.clientHeight ?? 500;
  return { x: (w / 2 - v.x) / v.z, y: (h / 2 - v.y) / v.z };
}

export function addMenu(from: HTMLElement, at?: { x: number; y: number }, point?: { x: number; y: number }): void {
  const html = menuHead('Add to the board') + Object.entries(PRESETS).map(([k, p]) => menuItem(k, esc(p.label), p.ic)).join('') + MENU_SEP +
    SHAPE_PRESETS.map((k) => menuItem(k, esc(SHAPES.find((s) => s[0] === k)?.[1] ?? k), 'box')).join('') + MENU_SEP +
    menuItem('auto', 'Auto zones <span class="faint">· one per group of joined tables</span>', 'layers');
  openMenu(html, from, (k) => { if (k === 'auto') autoZones(); else addNote(k, point); }, at);
}

// ---------------------------------------------------------------- drawing
function noteHTML(n: Note, sel: boolean): string {
  const path = SVG_PATH[n.shape];
  const jc = { top: 'flex-start', middle: 'center', bottom: 'flex-end' }[n.va], ai = { left: 'flex-start', center: 'center', right: 'flex-end' }[n.ha];
  const cls = `dt-note s-${n.shape} l-${n.layer} b-${n.border}${n.locked ? ' locked' : ''}${sel ? ' sel' : ''}${path ? ' svg' : ''}`;
  const style = `left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px;--nf:${colorOf(n.fill)};--na:${n.fill ? n.alpha : 0}${n.rot ? `;transform:rotate(${n.rot}deg)` : ''}`;
  const shape = path ? `<svg class="dt-nshape" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><path d="${path}" vector-effect="non-scaling-stroke"/></svg>` : '';
  const text = `<div class="dt-ntext" style="justify-content:${jc};align-items:${ai};text-align:${n.ha};font-size:${n.size}px;font-weight:${n.bold ? 650 : 400}">${n.text ? `<span>${esc(n.text)}</span>` : ''}</div>`;
  const handles = sel && !n.locked ? ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].map((h) => `<span class="dt-nh h-${h}" data-h="${h}"></span>`).join('') +
    `<span class="dt-nh h-rot" data-h="rot" title="Drag to rotate (Shift: 15° steps). Double-click: straight" data-deg="${n.rot ?? 0}°"></span>` : '';
  const lock = n.locked ? `<span class="dt-nlock" title="Locked: it does not move. Select it and click the lock to unlock.">${icon('lock')}</span>` : '';
  return `<div class="${cls}" data-n="${esc(n.id)}" style="${style}" role="note" aria-label="${esc(n.text.slice(0, 60) || `${n.shape} shape`)}">${shape}${text}${lock}${handles}</div>`;
}

/** Draw the notes of the open tab into both layers and place the format bar; nothing else on the board is touched. */
export function renderNotes(): void {
  const back = document.getElementById('dt-nback'), front = document.getElementById('dt-nfront');
  if (!back || !front) return;
  const list = notesOf(tab());
  NS.sel = NS.sel.filter((id) => list.some((n) => n.id === id));
  let b = '', f = '';
  for (const n of list) { const h = noteHTML(n, NS.sel.includes(n.id)); if (n.layer === 'back') b += h; else f += h; }
  back.innerHTML = b;
  front.innerHTML = f;
  placeBar();
}

/** The layers' markup for renderBuilder (filled by renderNotes once on the page). */
export const layerHTML = (where: 'back' | 'front'): string => `<div class="dt-nl ${where}" id="dt-n${where}"></div>`;

// ---------------------------------------------------------------- the format bar (screen space, above the selection)
function selected(t: Tab): Note[] { return notesOf(t).filter((n) => NS.sel.includes(n.id)); }

function barHTML(sel: Note[]): string {
  const one = sel[0] as Note, all = (f: (n: Note) => boolean): boolean => sel.every(f);
  const locked = all((n) => !!n.locked), zone = sel.some((n) => n.layer === 'back');
  const sw = PALETTE.map((c) => `<button type="button" class="dt-sw${all((n) => n.fill === c) ? ' on' : ''}" data-act="n-fill" data-k="${c}" style="--nf:var(--note-${c})" aria-label="${c}" title="${c}"></button>`).join('');
  const align = (['top', 'middle', 'bottom'] as const).flatMap((va) => (['left', 'center', 'right'] as const).map((ha) =>
    `<button type="button" class="dt-al${all((n) => n.ha === ha && n.va === va) ? ' on' : ''}" data-act="n-align" data-ha="${ha}" data-va="${va}" aria-label="Text ${va} ${ha}" title="Text ${va} ${ha}"><i></i></button>`)).join('');
  return `<select class="sm" data-nf="shape" aria-label="Shape">${SHAPES.map(([k, l]) => `<option value="${k}" ${one.shape === k ? 'selected' : ''}>${l}</option>`).join('')}</select>` +
    `<span class="dt-sws">${sw}<label class="dt-swc" title="Any colour"><input type="color" data-nf="fillc" value="${/^#/.test(one.fill) ? one.fill : '#4f7cff'}" aria-label="Any colour"></label><button type="button" class="dt-sw none${all((n) => !n.fill) ? ' on' : ''}" data-act="n-fill" data-k="" aria-label="No fill" title="No fill"></button></span>` +
    `<input type="range" class="dt-nop" min="0" max="100" step="2" value="${Math.round(one.alpha * 100)}" data-nf="alpha" aria-label="Fill opacity" title="Fill opacity">` +
    `<select class="sm" data-nf="border" aria-label="Border">${(['solid', 'dashed', 'none'] as const).map((k) => `<option value="${k}" ${one.border === k ? 'selected' : ''}>${k === 'none' ? 'No border' : `${k[0]?.toUpperCase()}${k.slice(1)} border`}</option>`).join('')}</select>` +
    `<span class="dt-nsz"><button type="button" class="icon-btn ghost xs" data-act="n-size" data-k="-2" aria-label="Smaller text">A−</button><span class="num">${one.size}</span><button type="button" class="icon-btn ghost xs" data-act="n-size" data-k="2" aria-label="Bigger text">A+</button></span>` +
    `<button type="button" class="icon-btn ghost xs${all((n) => !!n.bold) ? ' on' : ''}" data-act="n-bold" aria-pressed="${all((n) => !!n.bold)}" aria-label="Bold" title="Bold"><b>B</b></button>` +
    `<span class="dt-algrid" role="group" aria-label="Text position">${align}</span>` +
    `<button type="button" class="sm" data-act="n-layer" data-k="${one.layer === 'back' ? 'front' : 'back'}" title="${one.layer === 'back' ? 'In front of the tables' : 'Behind the tables, as a zone'}">${one.layer === 'back' ? 'Bring to front' : 'Send to back'}</button>` +
    (zone ? `<label class="check sm" title="Dragging the zone by its title moves the tables inside it too (Alt+drag: the zone alone)"><input type="checkbox" data-nf="carry" ${all((n) => !!n.carry) ? 'checked' : ''}> Move cards with zone</label>` : '') +
    `<span class="dt-nrot" role="group" aria-label="Rotation"><button type="button" class="icon-btn ghost xs" data-act="n-rot" data-k="-15" aria-label="Rotate left 15°" title="Rotate left 15° ([)">↺</button><input type="number" class="sm num" data-nf="rot" min="-180" max="180" step="1" value="${one.rot ?? 0}" aria-label="Rotation in degrees" title="Rotation, degrees clockwise"><span class="faint">°</span><button type="button" class="icon-btn ghost xs" data-act="n-rot" data-k="15" aria-label="Rotate right 15°" title="Rotate right 15° (])">↻</button></span>` +
    `<button type="button" class="icon-btn ghost xs${locked ? ' on' : ''}" data-act="n-lock" aria-pressed="${locked}" aria-label="${locked ? 'Unlock' : 'Lock'}" title="${locked ? 'Unlock (Ctrl+L)' : 'Lock: it cannot move or resize (Ctrl+L)'}">${icon('lock')}</button>` +
    `<button type="button" class="icon-btn ghost xs" data-act="n-dup" aria-label="Duplicate" title="Duplicate (Ctrl+D)">${icon('copy')}</button>` +
    `<button type="button" class="icon-btn ghost xs" data-act="n-del" aria-label="Delete" title="Delete">${icon('trash')}</button>`;
}

function placeBar(): void {
  const cv = document.getElementById('dt-canvas');
  if (!cv) return;
  let bar = document.getElementById('dt-nbar');
  const t = tab(), sel = selected(t);
  if (!sel.length) { bar?.remove(); return; }
  if (!bar) { bar = document.createElement('div'); bar.id = 'dt-nbar'; bar.className = 'dt-nbar'; bar.setAttribute('role', 'toolbar'); bar.setAttribute('aria-label', 'Note format'); cv.append(bar); }
  bar.innerHTML = barHTML(sel);
  bar.classList.remove('hide');                                           // hidden while a drag ran
  const bx = sel.map(bounds), v = t.view, x0 = Math.min(...bx.map((b) => b.x)), y0 = Math.min(...bx.map((b) => b.y)) - (sel.some((n) => !n.locked) ? 30 : 0), y1 = Math.max(...bx.map((b) => b.y + b.h));
  const bw = bar.offsetWidth, bh = bar.offsetHeight;
  let top = v.y + y0 * v.z - bh - 8;
  if (top < 44) top = v.y + y1 * v.z + 8;                                 // no room above: below the selection
  bar.style.top = `${clamp(top, 4, Math.max(4, cv.clientHeight - bh - 4))}px`;
  bar.style.left = `${clamp(v.x + x0 * v.z, 4, Math.max(4, cv.clientWidth - bw - 4))}px`;
}
export function placeNoteBar(): void { if (NS.sel.length) placeBar(); }

/** Change every selected note (one undo step), then save and redraw. */
function change(f: (n: Note) => void, undo = true): void {
  const t = tab(), sel = selected(t);
  if (!sel.length) return;
  if (undo) H?.record(t);
  sel.forEach(f);
  saveTabs();
  renderNotes();
}

export const N_ACTIONS: Record<string, (el: HTMLElement) => void> = {
  'n-add': (el) => addMenu(el),
  'n-fill': (el) => { const k = el.dataset['k'] ?? ''; change((n) => { n.fill = k; if (k && n.alpha === 0) n.alpha = n.layer === 'back' ? 0.14 : 1; }); },
  'n-size': (el) => { const d = Number(el.dataset['k']); change((n) => { n.size = clamp(n.size + d, 11, 48); }); },
  'n-bold': () => { const on = !selected(tab()).every((n) => n.bold); change((n) => { if (on) n.bold = true; else delete n.bold; }); },
  'n-align': (el) => change((n) => { n.ha = el.dataset['ha'] as Note['ha']; n.va = el.dataset['va'] as Note['va']; }),
  'n-layer': (el) => change((n) => { n.layer = el.dataset['k'] === 'front' ? 'front' : 'back'; }),
  'n-rot': (el) => rotateBy(Number(el.dataset['k']) || 0),
  'n-lock': () => toggleLock(),
  'n-dup': () => duplicate(),
  'n-del': () => { void removeSelected(); },
};

/** The format bar's inputs (shape, colour, opacity, border, carry): `input` previews, `change` saves. */
export function noteInput(t: HTMLInputElement | HTMLSelectElement, done: boolean): boolean {
  const k = t.dataset['nf'];
  if (!k || !t.closest('#dt-nbar')) return false;
  const v = t.value;
  if (k === 'alpha') {                                  // a slide is one undo step: recorded when it starts
    const first = !NS.sliding;
    NS.sliding = !done;
    const tb = tab();
    if (first) H?.record(tb);
    selected(tb).forEach((n) => { n.alpha = clamp(Number(v) / 100, 0, 1); });
    notesOf(tb).forEach((n) => { if (NS.sel.includes(n.id)) document.querySelector<HTMLElement>(`.dt-note[data-n="${CSS.escape(n.id)}"]`)?.style.setProperty('--na', String(n.fill ? n.alpha : 0)); });
    if (done) saveTabs();
    return true;
  }
  if (!done) return true;
  if (k === 'shape') change((n) => { n.shape = v as NoteShape; });
  else if (k === 'border') change((n) => { n.border = v as Note['border']; });
  else if (k === 'fillc') change((n) => { n.fill = v.toLowerCase(); if (n.alpha === 0) n.alpha = 0.3; });
  else if (k === 'rot') change((n) => { if (n.locked) return; const r = normRot(Number(v) || 0); if (r) n.rot = r; else delete n.rot; });
  else if (k === 'carry') change((n) => { if ((t as HTMLInputElement).checked) n.carry = true; else delete n.carry; });
  return true;
}

/** Turn every selected, unlocked note by d degrees (0 = straighten). */
function rotateBy(d: number): void {
  change((n) => { if (n.locked) return; const r = d ? normRot((n.rot ?? 0) + d) : 0; if (r) n.rot = r; else delete n.rot; });
}

function toggleLock(): void {
  const on = !selected(tab()).every((n) => n.locked);
  change((n) => { if (on) n.locked = true; else delete n.locked; });
  toast(on ? 'Locked: it stays where it is' : 'Unlocked');
}

function duplicate(): void {
  const t = tab(), list = notesOf(t), sel = selected(t);
  if (!sel.length) return;
  if (list.length + sel.length > MAX_NOTES) { toast(`A board holds ${MAX_NOTES} notes`, 'err'); return; }
  H?.record(t);
  const copies = sel.map((n) => { const c = cleanNote({ ...n, id: newId(), x: n.x + 16, y: n.y + 16 }); delete c.locked; delete c.auto; return c; });
  list.push(...copies);
  NS.sel = copies.map((c) => c.id);
  saveTabs();
  renderNotes();
}

async function removeSelected(): Promise<void> {
  const t = tab(), sel = selected(t);
  if (!sel.length) return;
  const locked = sel.filter((n) => n.locked);
  if (locked.length && !await openDialog({ title: `Delete ${locked.length === 1 ? 'a locked note' : `${locked.length} locked notes`}?`, ok: 'Delete', okClass: 'danger-btn', onOk: () => null, body: '<p>Locked notes are usually kept on purpose.</p>' })) return;
  H?.record(t);
  const gone = new Set(sel.map((n) => n.id));
  const m = H?.modelOf(t);
  if (m) m.notes = notesOf(t).filter((n) => !gone.has(n.id));
  NS.sel = [];
  saveTabs();
  renderNotes();
}

// ---------------------------------------------------------------- pointer: select, move, resize
type NDrag =
  | { kind: 'move'; sx: number; sy: number; moved: boolean; lead: Note; notes: { n: Note; ox: number; oy: number }[]; cards: { m: MTable; ox: number; oy: number }[] }
  | { kind: 'size'; sx: number; sy: number; moved: boolean; n: Note; h: string; o: { x: number; y: number; w: number; h: number } }
  | { kind: 'rot'; sx: number; sy: number; moved: boolean; n: Note; cx: number; cy: number; a0: number; r0: number };
let D: NDrag | null = null;
const angleAt = (x: number, y: number, cx: number, cy: number): number => Math.atan2(y - cy, x - cx) / RAD;
/** A pointer press that only selected (no drag): the click after it must not clear the selection. */
export const noteBusy = (): boolean => D !== null;

/** Left press on the board: on a note (or its handle), anywhere on it -> handled (select, then move or resize). */
export function noteDown(e: PointerEvent, target: Element): boolean {
  if (target.closest('[contenteditable]')) return true;               // typing in a note: the caret goes where clicked
  const el = target.closest<HTMLElement>('.dt-note');
  if (!el) return false;
  const t = tab(), n = byId(t, el.dataset['n'] ?? '');
  if (!n) return false;
  const h = target.closest<HTMLElement>('.dt-nh')?.dataset['h'];
  if (h === 'rot' && !n.locked) {
    const r = el.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;   // the centre, on screen
    D = { kind: 'rot', sx: e.clientX, sy: e.clientY, moved: false, n, cx, cy, a0: angleAt(e.clientX, e.clientY, cx, cy), r0: n.rot ?? 0 };
    e.preventDefault();
    return true;
  }
  if (h && !n.locked) {
    D = { kind: 'size', sx: e.clientX, sy: e.clientY, moved: false, n, h, o: { x: n.x, y: n.y, w: n.w, h: n.h } };
    e.preventDefault();
    return true;
  }
  const was = NS.sel.join('\n');               // redraw only when the selection changes: a redraw between the two
  const paint = (): void => { if (NS.sel.join('\n') !== was) renderNotes(); };   // clicks of a double click loses it
  if (e.shiftKey || e.ctrlKey || e.metaKey) NS.sel = NS.sel.includes(n.id) ? NS.sel.filter((x) => x !== n.id) : [...NS.sel, n.id];
  else if (!NS.sel.includes(n.id)) NS.sel = [n.id];
  H?.clearCards();
  e.preventDefault();
  const a = document.activeElement;                    // the board's keys (Delete, arrows) must not go to the SQL editor
  if (a instanceof HTMLElement && !a.closest('#dt-canvas')) a.blur();
  if (n.locked || !NS.sel.includes(n.id)) { paint(); return true; }
  const moving = selected(t).filter((x) => !x.locked);
  const cards: { m: MTable; ox: number; oy: number }[] = [];
  const carried = new Set(moving.map((x) => x.id));
  if (!e.altKey) {
    for (const zn of moving.filter((x) => x.layer === 'back' && x.carry)) {
      const inside = (cx: number, cy: number): boolean => contains(zn, cx, cy);
      for (const m of H?.cards() ?? []) {
        const c = document.querySelector<HTMLElement>(`.dt-tb[data-t="${CSS.escape(m.id)}"]`);
        if (c && inside(m.x + c.offsetWidth / 2, m.y + c.offsetHeight / 2) && !cards.some((k) => k.m === m)) cards.push({ m, ox: m.x, oy: m.y });
      }
      for (const o of notesOf(t)) if (!carried.has(o.id) && !o.locked && inside(o.x + o.w / 2, o.y + o.h / 2)) { carried.add(o.id); moving.push(o); }
    }
  }
  D = { kind: 'move', sx: e.clientX, sy: e.clientY, moved: false, lead: n, notes: moving.map((x) => ({ n: x, ox: x.x, oy: x.y })), cards };
  paint();
  return true;
}

export function noteMove(e: PointerEvent): boolean {
  if (!D) return false;
  const t = tab(), z = t.view.z, dx = (e.clientX - D.sx) / z, dy = (e.clientY - D.sy) / z;
  if (!D.moved && Math.hypot(e.clientX - D.sx, e.clientY - D.sy) <= 3) return true;
  if (!D.moved) { D.moved = true; H?.record(t); document.getElementById('dt-nbar')?.classList.add('hide'); }
  const on = !e.shiftKey;
  if (D.kind === 'move') {
    const lead0 = D.lead, lead = D.notes.find((x) => x.n === lead0) ?? D.notes[0];
    if (!lead) return true;
    const ddx = snap(lead.ox + dx, on) - lead.ox, ddy = snap(lead.oy + dy, on) - lead.oy;
    for (const g of D.notes) {
      g.n.x = clamp(g.ox + ddx, -SPAN, SPAN);
      g.n.y = clamp(g.oy + ddy, -SPAN, SPAN);
      const el = document.querySelector<HTMLElement>(`.dt-note[data-n="${CSS.escape(g.n.id)}"]`);
      if (el) { el.style.left = `${g.n.x}px`; el.style.top = `${g.n.y}px`; }
    }
    for (const g of D.cards) {
      g.m.x = g.ox + ddx;
      g.m.y = g.oy + ddy;
      const el = document.querySelector<HTMLElement>(`.dt-tb[data-t="${CSS.escape(g.m.id)}"]`);
      if (el) { el.style.left = `${g.m.x}px`; el.style.top = `${g.m.y}px`; }
    }
    if (D.cards.length) H?.drawLines();
  } else if (D.kind === 'rot') {
    const { n } = D;
    let r = D.r0 + angleAt(e.clientX, e.clientY, D.cx, D.cy) - D.a0;
    if (e.shiftKey) r = Math.round(r / 15) * 15;                          // Shift: 15° steps
    else { r = Math.round(r); const q = Math.round(r / 90) * 90; if (Math.abs(r - q) <= 3) r = q; }   // free, held at 0 / 90 / 180
    r = normRot(r);
    if (r) n.rot = r; else delete n.rot;
    const el = document.querySelector<HTMLElement>(`.dt-note[data-n="${CSS.escape(n.id)}"]`);
    if (el) { el.style.transform = r ? `rotate(${r}deg)` : ''; el.querySelector<HTMLElement>('.h-rot')?.setAttribute('data-deg', `${r}°`); el.classList.add('turning'); }
  } else if (D.n.rot) {
    // a rotated note: the pointer's move in the note's own axes; the side (or corner) across from the handle stays put
    const { o, h, n } = D, a = (n.rot ?? 0) * RAD, c = Math.cos(a), s = Math.sin(a);
    const lx = dx * c + dy * s, ly = -dx * s + dy * c;
    const sxd = h.includes('e') ? 1 : h.includes('w') ? -1 : 0, syd = h.includes('s') ? 1 : h.includes('n') ? -1 : 0;
    const w = clamp(Math.round(o.w + sxd * lx), MIN_W, BOARD_W), hh = clamp(Math.round(o.h + syd * ly), MIN_H, BOARD_H);
    const mx = sxd * (w - o.w) / 2, my = syd * (hh - o.h) / 2;              // how far the centre moves, in the note's axes
    const cx = o.x + o.w / 2 + mx * c - my * s, cy = o.y + o.h / 2 + mx * s + my * c;
    Object.assign(n, { x: clamp(Math.round(cx - w / 2), -SPAN, SPAN), y: clamp(Math.round(cy - hh / 2), -SPAN, SPAN), w, h: hh });
    const el = document.querySelector<HTMLElement>(`.dt-note[data-n="${CSS.escape(n.id)}"]`);
    if (el) Object.assign(el.style, { left: `${n.x}px`, top: `${n.y}px`, width: `${n.w}px`, height: `${n.h}px` });
  } else {
    const { o, h, n } = D;
    let x = o.x, y = o.y, w = o.w, hh = o.h;
    if (h.includes('e')) w = snap(o.w + dx, on);
    if (h.includes('s')) hh = snap(o.h + dy, on);
    if (h.includes('w')) { x = snap(o.x + dx, on); w = o.w + (o.x - x); }
    if (h.includes('n')) { y = snap(o.y + dy, on); hh = o.h + (o.y - y); }
    if (w < MIN_W) { if (h.includes('w')) x -= MIN_W - w; w = MIN_W; }
    if (hh < MIN_H) { if (h.includes('n')) y -= MIN_H - hh; hh = MIN_H; }
    Object.assign(n, { x: clamp(x, -SPAN, SPAN), y: clamp(y, -SPAN, SPAN), w: Math.min(w, BOARD_W), h: Math.min(hh, BOARD_H) });
    const el = document.querySelector<HTMLElement>(`.dt-note[data-n="${CSS.escape(n.id)}"]`);
    if (el) Object.assign(el.style, { left: `${n.x}px`, top: `${n.y}px`, width: `${n.w}px`, height: `${n.h}px` });
  }
  return true;
}

export function noteUp(): boolean {
  const d = D;
  D = null;
  if (!d) return false;
  if (d.moved) { saveTabs(); renderNotes(); }         // a plain click changed nothing: no redraw
  return true;
}

/** Marquee: the notes fully inside the rectangle (screen px) join the selection. */
export function marqueeNotes(r: DOMRect, add: boolean): void {
  const hit = [...document.querySelectorAll<HTMLElement>('#dt-board .dt-note')].filter((el) => {
    const b = el.getBoundingClientRect();
    return b.left >= r.left && b.right <= r.right && b.top >= r.top && b.bottom <= r.bottom;
  }).map((el) => el.dataset['n'] ?? '');
  NS.sel = add ? [...new Set([...NS.sel, ...hit])] : hit;
  renderNotes();
}

export function clearNoteSel(): void { if (NS.sel.length) { NS.sel = []; renderNotes(); } }

// ---------------------------------------------------------------- text: double-click to write
export function noteEdit(target: Element): boolean {
  const el = target.closest<HTMLElement>('.dt-note');
  if (el && target.closest('.h-rot')) { NS.sel = [el.dataset['n'] ?? '']; rotateBy(0); return true; }   // double-click the turn handle: straight
  if (!el || target.closest('.dt-nh')) return false;
  const t = tab(), n = byId(t, el.dataset['n'] ?? '');
  const box = el.querySelector<HTMLElement>('.dt-ntext');
  if (!n || !box) return false;
  NS.sel = [n.id];
  const span = document.createElement('span');
  span.textContent = n.text;
  box.replaceChildren(span);
  span.setAttribute('contenteditable', 'plaintext-only');
  if (span.contentEditable !== 'plaintext-only') span.contentEditable = 'true';   // a browser without plaintext-only
  span.spellcheck = true;
  el.classList.add('editing');
  span.focus();
  const range = document.createRange();
  range.selectNodeContents(span);
  getSelection()?.removeAllRanges();
  getSelection()?.addRange(range);
  let cancel = false;
  span.addEventListener('keydown', (e) => {
    e.stopPropagation();                                 // the builder's keys (Delete, arrows, Ctrl+Z) stay out of the text
    if (e.key === 'Escape') { cancel = true; span.blur(); }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); span.blur(); }
  });
  span.addEventListener('blur', () => {
    const v = span.innerText.replace(/\n$/, '').slice(0, MAX_TEXT);
    if (!cancel && v !== n.text) { H?.record(t); n.text = v; saveTabs(); }
    renderNotes();
  }, { once: true });
  return true;
}

// ---------------------------------------------------------------- keys
export function noteKey(e: KeyboardEvent): boolean {
  if (!NS.sel.length) return false;
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); void removeSelected(); return true; }
  if (mod && k === 'd') { e.preventDefault(); duplicate(); return true; }
  if (mod && k === 'l') { e.preventDefault(); toggleLock(); return true; }
  if (e.key === 'Escape') { clearNoteSel(); return true; }
  if (!mod && (e.code === 'BracketLeft' || e.code === 'BracketRight')) {   // [ ]: turn 15° (Shift: 1°)
    e.preventDefault();
    rotateBy((e.code === 'BracketLeft' ? -1 : 1) * (e.shiftKey ? 1 : 15));
    return true;
  }
  const d = ({ ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] } as Record<string, [number, number]>)[e.key];
  if (d && !mod) {
    e.preventDefault();
    const s = e.shiftKey ? SNAP : 1;
    change((n) => { if (!n.locked) { n.x = clamp(n.x + d[0] * s, -SPAN, SPAN); n.y = clamp(n.y + d[1] * s, -SPAN, SPAN); } });
    return true;
  }
  return false;
}

/** Right-click on a note: its own menu. */
export function noteMenu(id: string, at: { x: number; y: number }): boolean {
  const t = tab(), n = byId(t, id);
  if (!n) return false;
  if (!NS.sel.includes(id)) NS.sel = [id];
  renderNotes();
  const html = menuItem('edit', 'Edit text', 'edit') + menuItem('lock', n.locked ? 'Unlock' : 'Lock', 'lock') + menuItem('dup', 'Duplicate', 'copy') +
    menuItem('layer', n.layer === 'back' ? 'Bring to front' : 'Send to back', 'layers') +
    (n.locked ? '' : menuItem('rot90', 'Rotate 90°', 'refresh') + (n.rot ? menuItem('rot0', 'Straighten', 'undo') : '')) + MENU_SEP + menuItem('del', 'Delete', 'trash', '', 'danger');
  openMenu(html, document.body, (k) => {
    if (k === 'edit') { const el = document.querySelector(`.dt-note[data-n="${CSS.escape(id)}"] .dt-ntext`); if (el) noteEdit(el); } else if (k === 'lock') toggleLock();
    else if (k === 'dup') duplicate();
    else if (k === 'rot90') rotateBy(90);
    else if (k === 'rot0') rotateBy(0);
    else if (k === 'layer') change((x) => { x.layer = x.layer === 'back' ? 'front' : 'back'; });
    else if (k === 'del') void removeSelected();
  }, at);
  return true;
}

// ---------------------------------------------------------------- Auto zones: one zone per group of joined tables
export function autoZones(): void {
  const t = tab(), m = H?.modelOf(t);
  if (!m?.tables.length) { toast('Add tables to the board first'); return; }
  const parent = new Map(m.tables.map((x) => [x.id, x.id]));
  const find = (x: string): string => { let r = x; while (parent.get(r) !== r) r = parent.get(r) ?? r; parent.set(x, r); return r; };
  m.joins.forEach((j) => { if (parent.has(j.a[0]) && parent.has(j.b[0])) parent.set(find(j.a[0]), find(j.b[0])); });
  let groups = [...m.tables.reduce((acc, x) => acc.set(find(x.id), [...(acc.get(find(x.id)) ?? []), x]), new Map<string, MTable[]>()).values()];
  let bySchema = false;
  if (groups.length === 1) {                                         // all joined (or one table): group by schema instead
    const s = [...m.tables.reduce((acc, x) => acc.set(x.schema || '', [...(acc.get(x.schema || '') ?? []), x]), new Map<string, MTable[]>()).values()];
    if (s.length > 1) { groups = s; bySchema = true; }
  }
  const kept = notesOf(t).filter((n) => !n.auto || n.locked);
  if (kept.length + groups.length > MAX_NOTES) { toast(`A board holds ${MAX_NOTES} notes`, 'err'); return; }
  H?.record(t);
  const pad = 24;
  const zones = groups.map((g, i) => {
    const box = g.map((x) => { const el = document.querySelector<HTMLElement>(`.dt-tb[data-t="${CSS.escape(x.id)}"]`); return { x: x.x, y: x.y, w: el?.offsetWidth ?? 200, h: el?.offsetHeight ?? 160 }; });
    const x0 = Math.min(...box.map((b) => b.x)) - pad, y0 = Math.min(...box.map((b) => b.y)) - pad - STRIP;
    const x1 = Math.max(...box.map((b) => b.x + b.w)) + pad, y1 = Math.max(...box.map((b) => b.y + b.h)) + pad;
    const label = bySchema ? (g[0]?.schema || 'default') : groups.length === 1 ? 'Tables' : `Group ${i + 1}: ${g.map((x) => x.label || x.name).slice(0, 3).join(', ')}${g.length > 3 ? '…' : ''}`;
    return cleanNote({ ...PRESETS['zone'], text: label, fill: PALETTE[i % PALETTE.length] ?? 'blue', x: x0, y: y0, w: x1 - x0, h: y1 - y0, auto: true });
  });
  const mm = H?.modelOf(t);
  if (mm) mm.notes = [...zones, ...kept];
  NS.sel = [];
  saveTabs();
  renderNotes();
  toast(`${zones.length} zone${zones.length === 1 ? '' : 's'} added. Drag a zone by its title; Lock keeps it in place.`);
}

// ---------------------------------------------------------------- the assistant: a summary to read, edits to apply
/** What the assistant is told of the board (kept short). */
export function boardScope(): Record<string, unknown> | null {
  const t = tab(), m = H?.modelOf(t);
  if (!m || (!m.tables.length && !m.notes?.length)) return null;
  const out = {
    tables: m.tables.slice(0, 40).map((x) => {
      const el = document.querySelector<HTMLElement>(`.dt-tb[data-t="${CSS.escape(x.id)}"]`);
      const d = H?.info(x)?.description?.trim();
      return { id: x.id, name: x.schema ? `${x.schema}.${x.name}` : x.name, x: Math.round(x.x), y: Math.round(x.y), w: el?.offsetWidth ?? 200, h: el?.offsetHeight ?? 160, ...(d ? { description: d.slice(0, 160) } : {}) };
    }),
    joins: m.joins.slice(0, 60).map((j) => ({ a: j.a.join('.'), b: j.b.join('.'), ...(j.card ? { card: j.card } : {}) })),
    notes: (m.notes ?? []).slice(0, 80).map((n) => ({ id: n.id, shape: n.shape, layer: n.layer, x: n.x, y: n.y, w: n.w, h: n.h, fill: n.fill, text: n.text.slice(0, 80), ...(n.rot ? { rot: n.rot } : {}), ...(n.pin ? { describes: n.pin } : {}), ...(n.locked ? { locked: true } : {}) })),
  };
  let s = JSON.stringify(out);
  while (s.length > 4000 && out.notes.length) { out.notes.pop(); s = JSON.stringify(out); }
  return out;
}

export type BoardOp =
  | { op: 'add' | 'update' | 'remove'; id?: string; note?: Partial<Note> }
  | { op: 'zone'; id?: string; label: string; tables: string[]; fill?: string }
  | { op: 'describe'; table: string; text: string };
export interface BoardResult { added: number; changed: number; removed: number; moved: number; refused: string[]; overlap: string[] }

/** The notes that describe these table cards (they move with them). */
export function pinnedTo(ids: Set<string>): Note[] {
  return notesOf(tab()).filter((n) => n.pin && ids.has(n.pin) && !n.locked);
}

const PAD = 24, GAP = 36, DESC_GAP = 6, COL_MAX = 1100;
const cardBox = (m: MTable): { w: number; h: number } => {
  const el = document.querySelector<HTMLElement>(`.dt-tb[data-t="${CSS.escape(m.id)}"]`);
  return { w: el?.offsetWidth ?? 220, h: el?.offsetHeight ?? 160 };
};
/** The height a note needs for its text at this width (measured with the notes' own text style). */
function textHeight(text: string, w: number, size: number): number {
  const host = document.getElementById('dt-nfront');
  if (!host) return Math.max(MIN_H, Math.ceil(text.length * size * 0.55 / Math.max(40, w - 20)) * size * 1.3 + 14);
  const el = document.createElement('div');
  el.className = 'dt-ntext';
  Object.assign(el.style, { position: 'absolute', visibility: 'hidden', width: `${w}px`, height: 'auto', fontSize: `${size}px` });
  el.textContent = text;
  host.append(el);
  const h = el.offsetHeight;
  el.remove();
  return Math.max(MIN_H, Math.ceil(h) + 4);
}
const inside = (a: Note, b: Note): boolean => a.x >= b.x && a.y >= b.y && a.x + a.w <= b.x + b.w && a.y + a.h <= b.y + b.h;   // a nested zone is meant
const hit = (a: Note, b: Note): boolean => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** The assistant's edit_board in one undo step: notes added / changed / removed by id; `zone` draws a zone around
 * tables (the page measures them); `describe` pins a sticky note with a table's description above its card;
 * `arrange` first lays the tables out zone by zone, side by side, so no two zones overlap. Locked notes stay. */
export function applyBoardOps(ops: BoardOp[], arrange = false): BoardResult {
  const t = tab(), list = notesOf(t), out: BoardResult = { added: 0, changed: 0, removed: 0, moved: 0, refused: [], overlap: [] };
  const tables = H?.cards() ?? [], byTable = new Map(tables.map((m) => [m.id, m]));
  const find = (k: string): MTable | undefined => byTable.get(k) ?? tables.find((m) => m.alias === k || m.name === k || `${m.schema}.${m.name}` === k);
  H?.record(t);
  const room = (): boolean => { if (list.length < MAX_NOTES) return true; out.refused.push(`the board holds ${MAX_NOTES} notes`); return false; };
  // 1. descriptions: one pinned note per table (written again when the table has one)
  const desc = new Map<string, Note>();
  for (const o of ops) {
    if (o.op !== 'describe') continue;
    const m = find(o.table);
    if (!m) { out.refused.push(`describe: no table ${o.table} on the board`); continue; }
    const w = cardBox(m).w, text = String(o.text ?? '').slice(0, MAX_TEXT);
    const cur = list.find((n) => n.pin === m.id);
    if (cur?.locked) { out.refused.push(`describe: the note of ${m.name} is locked`); continue; }
    const h = textHeight(text, w, 12);
    if (cur) { Object.assign(cur, { text, w, h }); out.changed++; desc.set(m.id, cur); continue; }
    if (!room()) continue;
    const n = cleanNote({ ...PRESETS['sticky'], text, size: 12, w, h, x: m.x, y: m.y - h - DESC_GAP, pin: m.id });
    list.push(n);
    desc.set(m.id, n);
    out.added++;
  }
  for (const n of list) if (n.pin && !desc.has(n.pin) && byTable.has(n.pin)) desc.set(n.pin, n);
  // 2. the zones' tables (a table goes to the first zone that names it)
  const zones = ops.filter((o): o is Extract<BoardOp, { op: 'zone' }> => o.op === 'zone');
  const seen = new Set<string>();
  const groups = zones.map((z) => {
    const ms: MTable[] = [];
    for (const k of z.tables ?? []) {
      const m = find(k);
      if (!m) out.refused.push(`zone ${z.label}: no table ${k} on the board`);
      else if (!seen.has(m.id)) { seen.add(m.id); ms.push(m); }
    }
    return ms;
  });
  // 3. arrange: each zone's tables in a column (a second column past COL_MAX px), zones side by side, the rest after
  if (arrange && tables.length) {
    const rest = tables.filter((m) => !seen.has(m.id));
    let x = Math.min(...tables.map((m) => m.x)), top = Math.min(...tables.map((m) => m.y - (desc.get(m.id)?.h ?? 0)));
    top += STRIP;
    for (const g of [...groups, rest]) {
      if (!g.length) continue;
      x += PAD;
      let y = top + PAD, colW = 0, right = x;
      for (const m of g) {
        const b = cardBox(m), d = desc.get(m.id), dh = d ? d.h + DESC_GAP : 0;
        if (y > top + PAD && y + dh + b.h > top + COL_MAX) { x += colW + GAP; y = top + PAD; colW = 0; }
        if (d && !d.locked) { d.x = x; d.y = y; d.w = b.w; }
        m.x = x; m.y = y + dh;
        y += dh + b.h + GAP;
        colW = Math.max(colW, b.w);
        right = Math.max(right, x + colW);
        out.moved++;
      }
      x = right + PAD + GAP * 2;
    }
  }
  // 4. a zone around each group's cards and their descriptions
  zones.forEach((z, i) => {
    const g = groups[i] ?? [];
    if (!g.length) { out.refused.push(`zone ${z.label}: none of its tables are on the board`); return; }
    const box = g.flatMap((m) => { const b = cardBox(m), d = desc.get(m.id); return [{ x: m.x, y: m.y, w: b.w, h: b.h }, ...(d ? [d] : [])]; });
    const x0 = Math.min(...box.map((b) => b.x)) - PAD, y0 = Math.min(...box.map((b) => b.y)) - PAD - STRIP;
    const x1 = Math.max(...box.map((b) => b.x + b.w)) + PAD, y1 = Math.max(...box.map((b) => b.y + b.h)) + PAD;
    const rect = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }, label = String(z.label ?? '').slice(0, 120);
    const cur = (z.id ? list.find((n) => n.id === z.id) : undefined) ?? list.find((n) => n.layer === 'back' && n.text.trim().toLowerCase() === label.trim().toLowerCase());
    if (cur?.locked) { out.refused.push(`zone ${label}: ${cur.id} is locked (only the user unlocks it)`); return; }
    if (cur) { delete cur.rot; Object.assign(cur, cleanNote({ ...cur, ...rect, rot: 0, text: label, ...(z.fill ? { fill: z.fill } : {}), auto: false }, cur.id)); out.changed++; return; }
    if (!room()) return;
    list.push(cleanNote({ ...PRESETS['zone'], ...rect, text: label, fill: z.fill || (PALETTE[i % PALETTE.length] ?? 'blue') }, z.id));
    out.added++;
  });
  // 5. notes by id
  for (const o of ops) {
    if (o.op !== 'add' && o.op !== 'update' && o.op !== 'remove') continue;
    const cur = o.id ? list.find((n) => n.id === o.id) : undefined;
    if (o.op === 'add') {
      if (cur) { out.refused.push(`add: ${o.id} exists`); continue; }
      if (!room()) continue;
      list.push(cleanNote({ ...(o.note ?? {}), auto: false }, o.id));
      out.added++;
    } else if (!cur) out.refused.push(`${o.op}: no note ${o.id ?? ''}`);
    else if (cur.locked) out.refused.push(`${o.op}: ${cur.id} is locked (only the user unlocks it)`);
    else if (o.op === 'remove') { list.splice(list.indexOf(cur), 1); out.removed++; }
    else {
      const next = cleanNote({ ...cur, ...(o.note ?? {}), locked: false, auto: false }, cur.id);
      for (const k of ['bold', 'locked', 'carry', 'auto', 'pin', 'rot'] as const) delete cur[k];
      Object.assign(cur, next);
      out.changed++;
    }
  }
  const back = list.filter((n) => n.layer === 'back');
  back.forEach((a, i) => back.slice(i + 1).forEach((b) => { if (hit(a, b) && !inside(a, b) && !inside(b, a)) out.overlap.push(`${a.text.split('\n')[0] || a.id} / ${b.text.split('\n')[0] || b.id}`); }));
  NS.sel = [];
  saveTabs();
  if (out.moved) H?.redraw(); else renderNotes();
  return out;
}
