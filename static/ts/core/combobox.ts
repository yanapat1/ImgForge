// Themed combobox: a text input + chevron whose suggestions open in a floating list styled like the app's menus, in
// place of <input list> + <datalist> (the browser draws that popup itself: its own font, colours and corners, no theme).
// Free text stays allowed; the list filters as you type (case-insensitive, the match in <mark>), ↑ / ↓ / Home / End
// move, Enter / Tab / a click pick, Escape closes, and an optional "Create …" row offers a typed value not in the list.
// The listeners are delegated once on the document in the capture phase: a host that re-renders its markup keeps
// working, and the host's own key handlers (Enter commits a tag, Escape closes the dialog) only see the keys the open
// list did not use. A pick sets the input's value and fires a bubbling `input` event first, so a host's delegated
// data-f listener stores it like a typed value, then calls onPick.

import { esc, icon } from './dom.js';

export interface ComboOpts { placeholder?: string; mono?: boolean; disabled?: boolean; attrs?: string /* extra raw attributes for the <input>, e.g. data-f="bucket" */ }

/** Markup: a themed text input + chevron button inside <div class="cbx" data-cbx="${key}">. */
export function comboHTML(key: string, value: string, opts: ComboOpts = {}): string {
  const dis = opts.disabled ? ' disabled' : '';
  return `<div class="cbx${opts.mono ? ' is-mono' : ''}" data-cbx="${esc(key)}">` +
    `<input type="text" class="cbx-in${opts.mono ? ' mono' : ''}" value="${esc(value)}" role="combobox" aria-autocomplete="list" ` +
    `aria-expanded="false" aria-controls="cbx-list-${esc(key)}" autocomplete="off" spellcheck="false"` +
    `${opts.placeholder ? ` placeholder="${esc(opts.placeholder)}"` : ''}${dis}${opts.attrs ? ` ${opts.attrs}` : ''}>` +
    `<button type="button" class="cbx-btn" tabindex="-1" aria-label="Show the options" title="Show the options"${dis}></button></div>`;
}

export interface ComboCfg {
  options: () => readonly string[];            // read on every open/filter, so it can change after async loads
  onPick: (value: string, created: boolean) => void;   // pick from list, or Enter on a typed value / the Create row
  onInput?: (value: string) => void;           // every keystroke (no re-render expected)
  create?: (q: string) => string | null;       // label of a "Create …" row for a typed value not in options (null = no row)
  empty?: () => string;                        // text when no options (e.g. 'loading…' / 'no buckets'; '' = no list)
}

// key -> the roots it is bound in (one key can live in several dialogs; the root that holds the element wins)
const bound = new Map<string, Array<{ root: ParentNode; cfg: ComboCfg }>>();

/** Wire the combobox `key` inside `root` (call again after each re-render of root; it is idempotent per element). */
export function bindCombo(root: ParentNode, key: string, cfg: ComboCfg): void {
  const live = (bound.get(key) || []).filter((b) => b.root !== root && (!(b.root instanceof Node) || b.root.isConnected));
  live.push({ root, cfg });
  bound.set(key, live);
  listen();
}

interface Item { v: string; created: boolean; label: string }
interface Open {
  w: HTMLElement; inp: HTMLInputElement; cfg: ComboCfg; id: string; list: HTMLElement;
  items: Item[]; act: number; q: string; sig: string; at: number; raf: number; checked: number; rect: string;
}
let O: Open | null = null;
let picking = false;                           // the input event a pick fires is not a keystroke

const fold = (s: string): string => s.toLowerCase();
const sigOf = (cfg: ComboCfg): string => cfg.options().join('\u0001') + '\u0002' + (cfg.empty?.() ?? '');

function find(t: EventTarget | null): { w: HTMLElement; inp: HTMLInputElement; cfg: ComboCfg; key: string } | null {
  const w = t instanceof Element ? t.closest<HTMLElement>('[data-cbx]') : null;
  const key = w?.dataset['cbx'];
  if (!w || key == null) return null;
  const b = (bound.get(key) || []).find((x) => x.root instanceof Node && x.root.contains(w));
  const inp = w.querySelector<HTMLInputElement>('input');
  return b && inp ? { w, inp, cfg: b.cfg, key } : null;
}

function hl(v: string, q: string): string {
  const i = q ? fold(v).indexOf(q) : -1;
  return i < 0 ? esc(v) : `${esc(v.slice(0, i))}<mark>${esc(v.slice(i, i + q.length))}</mark>${esc(v.slice(i + q.length))}`;
}

// rebuild the rows for query q; keepAct: the same value stays active across an options refresh
function fill(o: Open, q: string, keepAct = false): void {
  const opts = o.cfg.options(), fq = fold(q.trim());
  const prev = keepAct ? o.items[o.act]?.v : undefined;
  o.q = q;
  o.sig = sigOf(o.cfg);
  o.items = [...new Set(opts)].filter((v) => !fq || fold(v).includes(fq)).map((v) => ({ v, created: false, label: v }));
  const label = fq && o.cfg.create && !opts.some((v) => fold(v) === fq) ? o.cfg.create(q.trim()) : null;
  if (label) o.items.push({ v: q.trim(), created: true, label });
  o.act = prev != null ? o.items.findIndex((it) => it.v === prev) : fq ? -1 : o.items.findIndex((it) => it.v === o.inp.value);
  const cur = o.inp.value;
  // nothing typed matches: no panel (free text is fine); no options at all: empty()'s note, or no panel when it is ''
  const note = o.items.length || fq ? '' : o.cfg.empty ? o.cfg.empty() : 'no options';
  o.list.classList.toggle('hidden', !o.items.length && !note);
  o.list.innerHTML = o.items.map((it, i) => `<div class="cbx-opt${it.created ? ' cbx-new' : ''}" role="option" id="${o.id}-${i}" data-i="${i}" ` +
    `aria-selected="${!it.created && it.v === cur}">${it.created ? `${icon('plus')}<span>${esc(it.label)}</span>` : `<span>${hl(it.v, fq)}</span>`}</div>`).join('') ||
    (note ? `<div class="cbx-empty">${esc(note)}</div>` : '');
  setAct(o, o.act);
  place(o);
}

function setAct(o: Open, i: number): void {
  o.act = i;
  o.list.querySelector('.cbx-opt.on')?.classList.remove('on');
  const row = i >= 0 ? o.list.querySelector<HTMLElement>(`#${CSS.escape(`${o.id}-${i}`)}`) : null;
  if (row) { row.classList.add('on'); row.scrollIntoView({ block: 'nearest' }); o.inp.setAttribute('aria-activedescendant', row.id); }
  else o.inp.removeAttribute('aria-activedescendant');
}

// under the field at its width, flipped up when there is more room above; at most 260px tall (then it scrolls)
function place(o: Open): void {
  const r = o.w.getBoundingClientRect();
  o.rect = `${r.left},${r.top},${r.width}`;
  const below = innerHeight - r.bottom - 12, above = r.top - 12;
  const s = o.list.style;
  s.width = `${Math.min(r.width, innerWidth - 16)}px`;
  s.left = `${Math.max(8, Math.min(innerWidth - r.width - 8, r.left))}px`;
  s.maxHeight = '260px';
  const h = o.list.offsetHeight;
  const up = h > below && above > below;
  s.maxHeight = `${Math.max(60, Math.min(260, up ? above : below))}px`;
  s.top = `${up ? Math.max(8, r.top - 4 - o.list.offsetHeight) : r.bottom + 4}px`;
  o.list.classList.toggle('up', up);
}

function open(w: HTMLElement, inp: HTMLInputElement, cfg: ComboCfg, key: string, q = ''): void {
  if (inp.matches(':disabled') || inp.readOnly) return;
  if (O && O.inp === inp) { fill(O, q); return; }
  close();
  const list = document.createElement('div');
  const id = `cbx-list-${key}`;
  list.id = id;
  list.className = `cbx-list${w.classList.contains('is-mono') ? ' is-mono' : ''}`;
  list.setAttribute('role', 'listbox');
  document.body.appendChild(list);
  const o: Open = { w, inp, cfg, id, list, items: [], act: -1, q, sig: '', at: performance.now(), raf: 0, checked: 0, rect: '' };
  O = o;
  inp.setAttribute('aria-controls', id);
  inp.setAttribute('aria-expanded', 'true');
  fill(o, q);
  // follow the field (layout shifts), close when the host re-rendered it away, refresh when the options load later
  const tick = (now: number): void => {
    if (O !== o) return;
    if (!inp.isConnected || !w.isConnected) { close(); return; }
    const r = w.getBoundingClientRect();
    if (`${r.left},${r.top},${r.width}` !== o.rect) place(o);
    if (now - o.checked > 300) { o.checked = now; if (sigOf(cfg) !== o.sig) fill(o, o.q, true); }
    o.raf = requestAnimationFrame(tick);
  };
  o.raf = requestAnimationFrame(tick);
}

function close(): void {
  const o = O;
  if (!o) return;
  O = null;
  cancelAnimationFrame(o.raf);
  o.list.remove();
  o.inp.setAttribute('aria-expanded', 'false');
  o.inp.removeAttribute('aria-activedescendant');
}

function pick(it: Item): void {
  const o = O;
  if (!o) return;
  close();
  o.inp.value = it.v;
  picking = true;
  try { o.inp.dispatchEvent(new Event('input', { bubbles: true })); } finally { picking = false; }
  o.cfg.onPick(it.v, it.created);
}

// Enter with no row active: the typed value (an option spelled another case picks that option)
function commitTyped(o: Open): void {
  const v = o.inp.value.trim();
  if (!v) { close(); return; }
  const hit = o.cfg.options().find((x) => fold(x) === fold(v));
  pick(hit != null ? { v: hit, created: false, label: hit } : { v, created: o.items.some((it) => it.created), label: v });
}

let on = false;
function listen(): void {
  if (on) return;
  on = true;
  const opt: AddEventListenerOptions = { capture: true };
  document.addEventListener('focusin', (e) => {
    const f = find(e.target);
    if (f && e.target === f.inp) open(f.w, f.inp, f.cfg, f.key);
  }, opt);
  document.addEventListener('focusout', (e) => { if (O && e.target === O.inp) close(); }, opt);
  document.addEventListener('mousedown', (e) => {
    const t = e.target;
    if (O && t instanceof Node && O.list.contains(t)) { e.preventDefault(); return; }    // keep the focus (no blur / change)
    const f = find(t);
    const chev = t instanceof Element && t.closest('.cbx-btn');
    if (f && chev) {
      e.preventDefault();
      if (O && O.inp === f.inp) { close(); return; }
      if (document.activeElement !== f.inp) f.inp.focus();      // its focusin opens the list
      if (!O) open(f.w, f.inp, f.cfg, f.key);
      return;
    }
    if (O && !(t instanceof Node && O.w.contains(t))) close();
  }, opt);
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (O && t instanceof Element && O.list.contains(t)) {
      const row = t.closest<HTMLElement>('.cbx-opt');
      const it = row ? O.items[+(row.dataset['i'] ?? -1)] : undefined;
      if (it) pick(it);
      return;
    }
    const f = find(t);
    if (f && t === f.inp && !O) open(f.w, f.inp, f.cfg, f.key);
  }, opt);
  document.addEventListener('input', (e) => {
    if (picking) return;
    const f = find(e.target);
    if (!f || e.target !== f.inp) return;
    open(f.w, f.inp, f.cfg, f.key, f.inp.value);
    f.cfg.onInput?.(f.inp.value);
  }, opt);
  document.addEventListener('keydown', (e) => {
    const f = find(e.target);
    if (!f || e.target !== f.inp || e.isComposing) return;
    const o = O && O.inp === f.inp ? O : null;
    const n = o ? o.items.length : 0;
    const stop = (): void => { e.preventDefault(); e.stopPropagation(); };
    switch (e.key) {
      case 'ArrowDown':
        stop();
        if (!o) open(f.w, f.inp, f.cfg, f.key);
        else if (n && !e.altKey) setAct(o, (o.act + 1) % n);
        return;
      case 'ArrowUp':
        if (!o) return;
        stop();
        if (n) setAct(o, o.act <= 0 ? n - 1 : o.act - 1);
        return;
      case 'Home': case 'End':                 // only while moving in the list; otherwise the caret moves
        if (!o || o.act < 0 || !n) return;
        stop();
        setAct(o, e.key === 'Home' ? 0 : n - 1);
        return;
      case 'Enter': {
        if (!o) return;
        if (e.ctrlKey || e.metaKey) { close(); return; }        // the host's own shortcut (save)
        stop();
        const it = o.items[o.act];
        if (it) pick(it); else commitTyped(o);
        return;
      }
      case 'Escape':
        if (!o) return;
        stop();
        close();
        return;
      case 'Tab': {
        if (!o) return;
        const it = o.items[o.act];
        if (it && it.v !== f.inp.value) pick(it); else close();
        return;
      }
    }
  }, opt);
  document.addEventListener('scroll', (e) => {
    if (!O || (e.target instanceof Node && O.list.contains(e.target))) return;
    if (performance.now() - O.at < 400) place(O);   // the focus that opened it scrolled the field into view
    else close();
  }, opt);
  window.addEventListener('resize', close);
}
