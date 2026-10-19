// Settings › Labeling: the labeling page's options (label/state.js SETTINGS_DEFAULTS) and the hotkey editor.
// Stored on the server (GET / PUT api/label/settings, label_settings.json) with only the hotkeys that differ from
// DEFAULT_HOTKEYS, mirrored to localStorage 'label-settings' (the label page's first paint), and announced with the
// document event 'label-settings-changed' (detail = the full settings) so an open label page applies them live.

import { errText, getJSON } from '../core/api.js';
import { esc, icon, rgba } from '../core/dom.js';
import { comboOf } from '../label/hotkeys.js';
import { BROWSER_RESERVED, DEFAULT_HOTKEYS, HOLD_ACTIONS, HOTKEY_LABELS, SETTINGS_DEFAULTS } from '../label/state.js';
import type { LabSettings, SetAction, StatusLine } from '../types-settings.js';
import { renderSettings } from './dialog.js';
import { dd, statusHTML } from './forms.js';

const LS_KEY = 'label-settings';
const SLOTS = 2;
// the label page's constants, viewed by key (the hotkey editor looks actions up by name)
const DEFAULTS: LabSettings = SETTINGS_DEFAULTS;
const DEFAULT_KEYS: Record<string, string[]> = DEFAULT_HOTKEYS;
const LABELS: Record<string, string> = HOTKEY_LABELS;
const RESERVED: readonly string[] = BROWSER_RESERVED;
const HOLD: ReadonlySet<string> = HOLD_ACTIONS;

interface Conflict { a: string; i: number; combo: string; b: string; j?: number; refused?: string }
interface Lab {
  s: LabSettings | null; rec: { a: string; i: number } | null; conflict: Conflict | null; status: StatusLine | null; saveTimer: number; classOpen: boolean;
}
// rec = the cell recording the next key press {a, i}; conflict = {a, i, combo, b, j}: the combo is already action b's slot j
// classOpen: the 88-row "Class hotkeys" group is expanded (collapsed by default)
const LAB: Lab = { s: null, rec: null, conflict: null, status: null, saveTimer: 0, classOpen: false };

/** The loaded settings (the section only renders after loadLabSettings). */
function lab(): LabSettings {
  if (!LAB.s) throw new Error('the labeling settings are not loaded');
  return LAB.s;
}
const isRecord = (x: unknown): x is Record<string, unknown> => typeof x === 'object' && x !== null;
/** One option by key / write one option by key (the data-lab attribute names it). */
const optGet = (k: string): unknown => { const rec: Record<string, unknown> = lab(); return rec[k]; };
const optSet = (k: string, v: unknown): void => { const rec: Record<string, unknown> = lab(); rec[k] = v; };

function readLocal(): unknown {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch (e) { return null; }
}

function normalise(s: unknown): LabSettings {
  const o: LabSettings = Object.assign({}, DEFAULTS, isRecord(s) ? s : {});
  o.hotkeys = { ...(o.hotkeys || {}) };
  return o;
}

export async function loadLabSettings(): Promise<void> {
  LAB.rec = LAB.conflict = null;
  try {
    LAB.s = normalise(await getJSON('api/label/settings'));
    LAB.status = null;
  } catch (e) {
    LAB.s = normalise(readLocal());
    LAB.status = { kind: 'err', text: `Could not read the saved settings (${errText(e)}); showing this browser's copy.` };
  }
}

// ---- hotkeys: combo strings come from label/hotkeys.js comboOf (the page's own matcher), so a recorded combo
// is exactly what the labeling page will look up
const KEY_TEXT: Record<string, string> = { arrowright: '→', arrowleft: '←', arrowup: '↑', arrowdown: '↓', escape: 'Esc', delete: 'Del', backspace: 'Backspace',
  enter: 'Enter', tab: 'Tab', space: 'Space', ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', pageup: 'PgUp', pagedown: 'PgDn', home: 'Home', end: 'End' };

export const comboHTML = (c: string): string => c.split('+').map((p) => `<kbd>${esc(KEY_TEXT[p] || (p.length === 1 ? p.toUpperCase() : p))}</kbd>`).join('');

const reservedText = (c: string): string => (/\+\d$/.test(c) ? 'Your browser may switch tabs instead; the plain digit alias still works.'
  : 'Your browser may keep this shortcut for itself (close / open a tab or window).');

const effective = (): Record<string, string[]> => ({ ...DEFAULT_KEYS, ...lab().hotkeys });
const same = (x: string[], y: string[]): boolean => x.length === y.length && x.every((v, i) => v === y[i]);

function setCombos(a: string, combos: string[]): void {
  const list = [...new Set(combos.filter(Boolean))].slice(0, SLOTS);
  if (same(list, DEFAULT_KEYS[a] || [])) delete lab().hotkeys[a];
  else lab().hotkeys[a] = list;
}

function setSlot(a: string, i: number, combo: string): void {
  const cur = [...(effective()[a] || [])];
  cur[i] = combo;
  setCombos(a, cur);
}

function owner(combo: string, a: string, i: number): { b: string; j: number } | null {
  for (const [b, list] of Object.entries(effective())) {
    const j = list.indexOf(combo);
    if (j >= 0 && !(b === a && j === i)) return { b, j };
  }
  return null;
}

// the key without its modifiers ('shift+q' -> 'q'); hold keys (area_select) are matched by it on the page
const bareKey = (c: string | null | undefined): string => String(c || '').replace(/^((ctrl|alt|shift)\+)+(?=.)/, '');

// a hold key engages on its bare key whatever modifiers are down, so it would take any other combo on that key:
// refuse such a pair (either way round) instead of letting one of them silently stop working
function holdClash(a: string, combo: string): string | null {
  const bk = bareKey(combo);
  for (const [b, list] of Object.entries(effective())) {
    if (b === a || (!HOLD.has(a) && !HOLD.has(b))) continue;
    if ((list || []).some((c) => c && bareKey(c) === bk)) return b;
  }
  return null;
}

function assign(a: string, i: number, combo: string): void {
  const hb = holdClash(a, combo);
  if (hb) {
    const [hold, other] = HOLD.has(a) ? [a, hb] : [hb, a];
    LAB.conflict = { a, i, combo, b: hb, refused: `${bareKey(combo).toUpperCase()} is the hold key of ${LABELS[hold ?? ''] || hold}, ` +
      `which takes every combo on that key; it cannot also be used for ${LABELS[other ?? ''] || other}. Pick another key.` };
    LAB.rec = null;
    renderSettings();
    focusCell(a, i);
    return;
  }
  const o = owner(combo, a, i);
  if (o && o.b === a) { LAB.rec = null; renderSettings(); return; }       // already this action's other slot
  if (o) LAB.conflict = { a, i, combo, b: o.b, j: o.j };
  else { setSlot(a, i, combo); save(); }
  LAB.rec = null;
  renderSettings();
  focusCell(a, i);
}

function focusCell(a: string, i: number): void {
  const el = document.querySelector<HTMLElement>(`.hk-cell[data-a="${CSS.escape(a)}"][data-i="${i}"]`);
  if (el) el.focus();
}

// window capture listener (bound in dialog.js before any page hotkeys): while a cell records, every key goes here
export function labKeydown(e: KeyboardEvent): boolean {
  if (!LAB.rec) return false;
  e.preventDefault();
  e.stopImmediatePropagation();
  const { a, i } = LAB.rec;
  const plain = !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey;
  if (e.key === 'Escape' && plain) { LAB.rec = null; renderSettings(); focusCell(a, i); return true; }
  if (e.key === 'Backspace' && plain) { setSlot(a, i, ''); save(); LAB.rec = null; renderSettings(); focusCell(a, i); return true; }
  const combo = comboOf(e);
  if (combo) assign(a, i, combo);
  return true;
}

// a click anywhere else stops the recording
export function labCancelRec(target: Element | null): void {
  if (!LAB.rec || (target && target.closest('.hk-cell'))) return;
  LAB.rec = null;
  renderSettings();
}

// ---- saving: localStorage + the live event at once, the server write debounced
function snapshot(): LabSettings { return { ...lab(), hotkeys: { ...lab().hotkeys } }; }

function save(): void {
  const s = snapshot();
  try { localStorage.setItem(LS_KEY, JSON.stringify(s)); } catch (e) { /* private window */ }
  document.dispatchEvent(new CustomEvent('label-settings-changed', { detail: s }));
  clearTimeout(LAB.saveTimer);
  LAB.saveTimer = setTimeout(async () => {
    try {
      const r = await fetch('api/label/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(snapshot()) });
      if (!r.ok) throw new Error(`api/label/settings: ${r.status} ${await r.text()}`);
      setStatus({ kind: 'ok', text: 'Saved' });
    } catch (e) { setStatus({ kind: 'err', text: `Not saved on the server: ${errText(e)}` }); }
  }, 350);
}

function setStatus(st: StatusLine): void {
  LAB.status = st;
  const el = document.getElementById('lab-status');
  if (el) el.outerHTML = `<div id="lab-status">${statusHTML(st)}</div>`;
}

// inputs with data-lab="<key>": written on input (sliders show their value live), saved on change
export function labInput(el: HTMLInputElement | HTMLSelectElement, committed: boolean): void {
  const k = el.dataset.lab;
  if (!k || !LAB.s) return;
  const checkbox = el instanceof HTMLInputElement && el.type === 'checkbox';
  const v = checkbox ? el.checked : el.type === 'range' ? Number(el.value) : el.value;
  optSet(k, v);
  if (el.type === 'range') {
    const out = el.parentElement?.querySelector('output');
    if (out) out.textContent = rangeText(k, Number(v));
  }
  if (APPEARANCE.some((x) => x === k)) {
    drawPreview();
    const rb = document.querySelector<HTMLButtonElement>('[data-act="lab-appear-reset"]');
    if (rb) rb.disabled = !APPEARANCE.some((x) => optGet(x) !== DEFAULTS[x]);
  }
  if (committed) {
    save();
    if (k === 'auto_advance' || k === 'color_mode') renderSettings();   // dependent rows follow the choice
  }
}

const PCT = new Set(['fill_opacity', 'tag_opacity', 'stroke_opacity']);
const rangeText = (k: string, v: number): string => (PCT.has(k) ? `${Math.round(v * 100)}%` : `${v} px`);

// ---- Appearance: how shapes and name tags are drawn (label/canvas.js + label/tags.js read the same keys)
const APPEARANCE: Array<Exclude<keyof LabSettings, 'hotkeys'>> = ['tag_size', 'tag_opacity', 'stroke_width', 'stroke_opacity', 'fill_opacity', 'line_style', 'handle_size',
  'color_mode', 'single_color', 'selected_style'];

// a small stand-alone drawing of the label page's look: 3 boxes (yours, a model label, a selected one) with tags
function drawPreview(): void {
  const cv = document.getElementById('lab-preview');
  if (!(cv instanceof HTMLCanvasElement) || !LAB.s) return;
  const s = LAB.s;
  const dpr = window.devicePixelRatio || 1;
  const W = 240, Hh = 170;
  if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = Hh * dpr; }
  const x = cv.getContext('2d');
  if (!x) return;
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  const css = getComputedStyle(document.documentElement);
  const tok = (n: string, d: string) => css.getPropertyValue(n).trim() || d;
  const halo = tok('--lb-halo', 'rgba(0,0,0,.6)'), sel = tok('--lb-sel', '#fff'), hFill = tok('--lb-handle', '#fff'), hLine = tok('--lb-handle-line', '#111');
  const dark = tok('--lb-tag-dark', '#0d1117'), light = tok('--lb-tag-light', '#fff');
  // a soft "photo" background
  const g = x.createLinearGradient(0, 0, W, Hh);
  g.addColorStop(0, '#5b6f86'); g.addColorStop(0.55, '#8a9a7b'); g.addColorStop(1, '#c9b48a');
  x.fillStyle = g; x.fillRect(0, 0, W, Hh);
  x.fillStyle = 'rgba(255,255,255,.18)'; x.beginPath(); x.arc(190, 40, 22, 0, Math.PI * 2); x.fill();
  const single = s.color_mode === 'single' && /^#[0-9a-f]{6}$/i.test(s.single_color || '') ? s.single_color : null;
  const sw = +s.stroke_width || 1.5, so = +s.stroke_opacity || 1, fo = +s.fill_opacity || 0, px = +s.tag_size || 10;
  const th = Math.round(px * 1.4), pad = Math.max(3, Math.round(px * 0.42));
  const lum = (h: string) => { const n = parseInt(h.slice(1), 16); return (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255; };
  const shapes: Array<{ b: [number, number, number, number]; c: string; t: string; model?: boolean; sel?: boolean }> = [
    { b: [18, 34, 82, 70], c: '#ff9f1c', t: 'cat' },
    { b: [118, 70, 100, 76], c: '#3fa7ff', t: '\u{1F916} dog 0.87', model: true },
    { b: [40, 116, 58, 40], c: '#5fd17a', t: 'bird', sel: true },
  ];
  x.font = `600 ${px}px ${tok('--font', 'sans-serif')}`;
  x.textBaseline = 'middle';
  for (const sh of shapes) {
    const col = single || sh.c;
    const [bx, by, bw, bh] = sh.b;
    const w = sw + (sh.sel ? 1 : 0);
    x.beginPath(); x.rect(bx, by, bw, bh);
    x.fillStyle = rgba(col, sh.sel ? Math.min(0.6, fo * 2 + 0.12) : fo); x.fill();
    if (sh.sel && s.selected_style !== 'thick') { x.globalAlpha = 0.55; x.strokeStyle = sel; x.lineWidth = w + 6; x.stroke(); }
    x.globalAlpha = so; x.strokeStyle = halo; x.lineWidth = w + 2; x.stroke();
    x.globalAlpha = 1;
    if (sh.model) x.setLineDash([5, 4]); else if (s.line_style === 'dashed') x.setLineDash([9, 5]);
    x.strokeStyle = so >= 1 ? col : rgba(col, so); x.lineWidth = w; x.stroke();
    x.setLineDash([]);
    if (sh.sel) {
      const r = (+s.handle_size || 7) / 2;
      const handles: Array<[number, number]> = [[0, 0], [0.5, 0], [1, 0], [1, 0.5], [1, 1], [0.5, 1], [0, 1], [0, 0.5]];
      for (const [fx, fy] of handles) {
        x.beginPath(); x.rect(bx + bw * fx - r, by + bh * fy - r, 2 * r, 2 * r);
        x.fillStyle = hFill; x.fill(); x.strokeStyle = hLine; x.lineWidth = 1.5; x.stroke();
      }
    }
    if (s.tags === 'off') continue;
    const tw = Math.ceil(x.measureText(sh.t).width) + 2 * pad;
    const ty = by - sw / 2 - th >= 0 ? by - sw / 2 - th : by + sw;
    const ta = s.tag_opacity == null ? 0.85 : +s.tag_opacity;
    x.fillStyle = rgba(col, ta);
    x.fillRect(Math.round(bx - sw / 2), Math.round(ty), tw, th);
    const tx = Math.round(bx - sw / 2) + pad, tyy = Math.round(ty) + th / 2 + 0.5;
    if (ta < 0.5) { x.strokeStyle = dark; x.lineWidth = 3; x.lineJoin = 'round'; x.strokeText(sh.t, tx, tyy); x.fillStyle = light; }
    else x.fillStyle = lum(col) > 0.55 ? dark : light;
    x.fillText(sh.t, tx, tyy);
  }
}

export const LAB_ACTIONS: Record<string, SetAction> = {
  'hk-rec': (b) => {
    const a = dd(b, 'a'), i = Number(b.dataset.i);
    LAB.conflict = null;
    LAB.rec = LAB.rec && LAB.rec.a === a && LAB.rec.i === i ? null : { a, i };
    renderSettings();
    focusCell(a, i);
  },
  'hk-swap': () => {
    const c = LAB.conflict;
    if (!c || c.refused) return;
    const prev = (effective()[c.a] || [])[c.i] || '';
    setSlot(c.b, c.j ?? 0, prev);                             // the other action gets this cell's old combo (or loses it)
    setSlot(c.a, c.i, c.combo);
    LAB.conflict = null;
    save();
    renderSettings();
    focusCell(c.a, c.i);
  },
  'lab-appear-reset': () => {
    for (const k of APPEARANCE) optSet(k, DEFAULTS[k]);
    save();
    renderSettings();
  },
  'hk-fold': () => { LAB.classOpen = !LAB.classOpen; renderSettings(); },
  'hk-cancel': () => { const c = LAB.conflict; LAB.conflict = null; renderSettings(); if (c) focusCell(c.a, c.i); },
  'hk-reset': (b) => { delete lab().hotkeys[dd(b, 'a')]; LAB.conflict = null; save(); renderSettings(); },
  'hk-reset-all': () => {
    if (!confirm('Reset every labeling hotkey to its default?')) return;
    lab().hotkeys = {};
    LAB.conflict = LAB.rec = null;
    save();
    renderSettings();
  },
};

// ---- render
function rowHTML(a: string): string {
  const list = effective()[a] || [];
  const changed = !!lab().hotkeys[a];
  let cells = '';
  for (let i = 0; i < SLOTS; i++) {
    const c = list[i] || '';
    const rec = LAB.rec && LAB.rec.a === a && LAB.rec.i === i;
    const warn = c && RESERVED.includes(c)
      ? `<span class="hk-warn" title="${esc(reservedText(c))}" aria-label="${esc(reservedText(c))}">${icon('alert')}</span>` : '';
    cells += `<button class="hk-cell${rec ? ' rec' : ''}${c ? '' : ' unset'}" data-act="hk-rec" data-a="${esc(a)}" data-i="${i}" ` +
      `aria-label="${esc(`${LABELS[a]}, shortcut ${i + 1}: ${c || 'none'}. Click, then press the new keys`)}" aria-pressed="${rec}">` +
      (rec ? '<span class="hk-recording">Press keys… <small>Esc cancels · Backspace clears</small></span>' : c ? comboHTML(c) : '<span class="hk-none">—</span>') +
      `</button>${warn}`;
  }
  let conflict = '';
  const cf = LAB.conflict;
  if (cf && cf.a === a && cf.refused) {
    conflict = `<div class="hk-conflict" role="alert">${icon('alert')}<span>${esc(cf.refused)}</span>` +
      '<button class="ghost" data-act="hk-cancel">OK</button></div>';
  } else if (cf && cf.a === a) {
    conflict = `<div class="hk-conflict" role="alert">${icon('alert')}<span>${comboHTML(cf.combo)} is also used by <b>${esc(LABELS[cf.b] || cf.b)}</b>.</span>` +
      `<button data-act="hk-swap">Swap</button><button class="ghost" data-act="hk-cancel">Cancel</button></div>`;
  }
  const hold = HOLD.has(a) ? '<span class="hk-hold" title="Hold the key down while you drag">hold</span>' : '';
  return `<div class="hk-row${changed ? ' changed' : ''}" role="row"><span class="hk-name" role="rowheader">${esc(LABELS[a] || a)}${hold}</span>` +
    `<span class="hk-cells" role="cell">${cells}</span>` +
    `<button class="icon-btn ghost hk-reset" data-act="hk-reset" data-a="${esc(a)}" ${changed ? '' : 'disabled'} aria-label="Reset ${esc(LABELS[a] || a)}" ` +
    `title="Reset to ${esc((DEFAULT_KEYS[a] || []).join(', ') || 'none')}">${icon('refresh')}</button>${conflict}</div>`;
}

function groupHTML(title: string, ids: string[]): string {
  return `<div class="hk-group" role="rowgroup"><div class="hk-ghead" role="row"><span role="columnheader">${esc(title)}</span></div>${ids.map(rowHTML).join('')}</div>`;
}

// the class rows (88) fold away: open on demand, or while one of them records / shows a conflict
function classGroupHTML(ids: string[]): string {
  const busy = (x: { a: string } | null) => !!x && ids.includes(x.a);
  const open = LAB.classOpen || busy(LAB.rec) || busy(LAB.conflict);
  const nChanged = ids.filter((a) => lab().hotkeys[a]).length;
  return `<div class="hk-group hk-fold" role="rowgroup"><div class="hk-ghead" role="row"><button class="hk-fold-btn" data-act="hk-fold" ` +
    `aria-expanded="${!!open}" role="columnheader">${icon(open ? 'chev-d' : 'chev-r')}<span>Class hotkeys (${ids.length})</span>` +
    `${nChanged ? `<span class="hk-fold-n">${nChanged} changed</span>` : ''}</button></div>` +
    (open ? ids.map(rowHTML).join('') : '') + '</div>';
}

const sel = (k: string, opts: Array<[string, string]>) => `<select data-lab="${k}">${opts.map(([v, t]) => `<option value="${v}" ${optGet(k) === v ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`;
const check = (k: string, title: string, help?: string) => `<label class="check lab-check"><input type="checkbox" data-lab="${k}" ${optGet(k) ? 'checked' : ''}> ` +
  `<span><b>${esc(title)}</b>${help ? `<br><span class="muted">${esc(help)}</span>` : ''}</span></label>`;
const row = (title: string, ctl: string, help?: string) => `<label class="lab-row"><span><b>${esc(title)}</b>${help ? `<span class="muted">${esc(help)}</span>` : ''}</span>${ctl}</label>`;
const range = (k: string, min: number, max: number, step: number) => `<span class="lab-range"><input type="range" data-lab="${k}" min="${min}" max="${max}" step="${step}" value="${esc(optGet(k))}">` +
  `<output class="num">${esc(rangeText(k, Number(optGet(k))))}</output></span>`;

function appearanceHTML(): string {
  const s = lab();
  const changed = APPEARANCE.some((k) => s[k] !== DEFAULTS[k]);
  requestAnimationFrame(drawPreview);                    // the canvas exists once the section's HTML is in place
  return '<div class="form-card done lab-appear-card"><div class="step-head">Appearance<span class="grow"></span>' +
    `<button class="ghost" data-act="lab-appear-reset" ${changed ? '' : 'disabled'}>${icon('refresh')}Reset to defaults</button></div>` +
    '<div class="lab-appear"><div class="lab-appear-ctl">' +
    row('Tag size', range('tag_size', 8, 16, 1), 'Name tag text; the tag height follows') +
    row('Tag background', range('tag_opacity', 0.2, 1, 0.05)) +
    row('Line width', range('stroke_width', 0.5, 6, 0.5)) +
    row('Line opacity', range('stroke_opacity', 0.1, 1, 0.05), 'Lower = fainter lines') +
    row('Fill opacity', range('fill_opacity', 0, 0.6, 0.05)) +
    row('Line style', sel('line_style', [['solid', 'solid'], ['dashed', 'dashed']]), 'Model labels are always dashed') +
    row('Resize handles', range('handle_size', 4, 12, 1)) +
    row('Colours', sel('color_mode', [['class', 'each class its colour'], ['single', 'one colour for every shape']])) +
    (s.color_mode === 'single' ? row('Shape colour', `<input type="color" class="lab-color" data-lab="single_color" value="${esc(s.single_color || '#ff3b30')}" aria-label="Colour for every shape">`) : '') +
    row('Selected shape', sel('selected_style', [['halo', 'thicker + halo'], ['thick', 'thicker only']])) +
    '</div><div class="lab-preview"><canvas id="lab-preview" width="240" height="170" role="img" aria-label="Preview: your box, a model label (dashed, robot and score) and a selected box"></canvas>' +
    '<span class="muted">Yours · a model label (dashed, 🤖 + score) · selected</span></div></div></div>';
}

export function renderLabelingSec(): string {
  if (!LAB.s) return '<section><div class="status-line busy"><span class="spin"></span>loading…</div></section>';
  const s = LAB.s;
  const ids = Object.keys(DEFAULT_KEYS);
  const general = ids.filter((a) => !/^(class|task)_\d+$/.test(a));
  const nChanged = Object.keys(s.hotkeys).length;
  return '<section class="lab-sec">' +
    '<div class="form-card done"><div class="step-head">Drawing</div>' +
    check('crosshair', 'Crosshair', 'Horizontal and vertical lines follow the mouse over the image') +
    check('dim_other_tasks', 'Fade the other tasks’ shapes', 'Shapes of the tasks not on screen stay visible, faded (off: they are hidden)') +
    row('Boxes are drawn by', sel('bbox_mode', [['drag', 'dragging corner to corner'], ['two_click', 'two clicks (corner, then corner)']])) +
    '</div>' + appearanceHTML() + '<div class="form-card done"><div class="step-head">Name tags</div>' +
    row('Show name tags', sel('tags', [['all', 'on every shape'], ['hover_selected', 'only on the hovered / selected shape'], ['off', 'off']])) +
    row('Tag text', sel('tag_content', [['name', 'class name'], ['name_index', 'class name + number (cat 2)']])) +
    '</div><div class="form-card done"><div class="step-head">Committing an image</div>' +
    row('Commit marks done', sel('commit_scope', [['active', 'the task on screen'], ['all', 'every task of the image']])) +
    check('auto_advance', 'Go to the next image after a commit') +
    (s.auto_advance ? row('Next image is', sel('advance_to', [['next', 'the next one'], ['unlabeled', 'the next unfinished one']])) : '') +
    '</div><div class="form-card done hk-card"><div class="step-head">Keyboard shortcuts' +
    `<span class="grow"></span><button class="ghost" data-act="hk-reset-all" ${nChanged ? '' : 'disabled'}>${icon('refresh')}Reset all to defaults</button></div>` +
    '<p class="sec-note">Click a shortcut, then press the new keys (Esc cancels, Backspace clears). Two shortcuts per action. ' +
    'Classes 1–10 are the digits (0 = class 10), 11–44 the letter and punctuation keys, 45–88 Shift + the same keys; ' +
    'Ctrl+digit is often taken by the browser, so classes 1–9 also have their plain digit.</p>' +
    '<div class="hk-table" role="table" aria-label="Labeling keyboard shortcuts">' +
    groupHTML('Images and editing', general) +
    classGroupHTML(ids.filter((a) => a.startsWith('class_'))) +
    groupHTML('Tasks', ids.filter((a) => a.startsWith('task_'))) +
    '</div></div>' +
    `<div id="lab-status">${statusHTML(LAB.status)}</div></section>`;
}
