// The tag editor popover (img3), anchored to its button through core/popover.ts floatPopover: the tags of one bucket
// path (user chips with ×, auto `format:…` / `split:…` chips locked with an "auto" mark), a typeahead input over every
// tag in use (GET /api/dc/tags, ↑ ↓ + Enter, "Create …"), a few suggestions not on it yet, Save (PUT …/tags with the
// difference) / Cancel. With several paths (a selection) it only adds: the chosen tags go on each of them.

import { errText } from '../core/api.js';
import { $, esc, icon } from '../core/dom.js';
import { closeFloat, floatPopover } from '../core/popover.js';
import { toast } from '../core/toast.js';
import * as api from './api.js';
import { baseName, bucketOf, D, fmtN, hooks } from './state.js';
import type { Tag } from '../types-datacenter.js';

const MAX_TAGS = 20;
const clean = (s: string): string => s.trim().toLowerCase().replace(/\s+/g, '-').replace(/[^\p{L}\p{N}:_.-]/gu, '').slice(0, 40);

interface TE { bid: string; paths: string[]; orig: Tag[]; cur: Tag[]; q: string; hi: number; busy: boolean }
let T: TE | null = null;

/** The current tags of a path of the open folder / bucket as the page knows them. */
function tagsOf(bid: string, path: string): Tag[] {
  if (D.listing && D.bid === bid && D.path === path) return D.listing.tags;
  if (path === '') return bucketOf(bid)?.tags ?? [];
  return [];
}

function matches(): Array<{ tag: string; count: number; create?: boolean }> {
  if (!T) return [];
  const q = clean(T.q);
  const on = new Set(T.cur.map((t) => t.tag));
  const all = D.tags.filter((t) => !t.auto && !on.has(t.tag));
  const hit = (q ? all.filter((t) => t.tag.includes(q)) : all).sort((a, b) =>
    Number(b.tag.startsWith(q)) - Number(a.tag.startsWith(q)) || b.count - a.count).slice(0, 6);
  const out: Array<{ tag: string; count: number; create?: boolean }> = hit.map((t) => ({ tag: t.tag, count: t.count }));
  if (q && !on.has(q) && !hit.some((t) => t.tag === q) && !q.startsWith('format:')) out.push({ tag: q, count: 0, create: true });
  return out;
}

function paint(): void {
  const el = $('dc-tagpop');
  if (!el || !T) return;
  const q = clean(T.q);
  const list = matches();
  T.hi = Math.min(T.hi, list.length - 1);
  const mark = (t: string): string => {
    const i = q ? t.indexOf(q) : -1;
    return i < 0 ? esc(t) : `${esc(t.slice(0, i))}<mark>${esc(t.slice(i, i + q.length))}</mark>${esc(t.slice(i + q.length))}`;
  };
  const sugg = T.q ? [] : D.tags.filter((t) => !t.auto && !T?.cur.some((c) => c.tag === t.tag)).sort((a, b) => b.count - a.count).slice(0, 5);
  const what = T.paths.length === 1 ? (T.paths[0] ? `${baseName(T.paths[0])}/` : bucketOf(T.bid)?.name ?? 'bucket') : `${fmtN(T.paths.length)} items`;
  const chips = T.cur.map((t) => t.auto
    ? `<span class="te-chip auto" title="Set by format detection: it cannot be removed">${icon('lock')}${esc(t.tag)}<em>auto</em></span>`
    : `<span class="te-chip">${esc(t.tag)}<button type="button" class="te-x" data-rm="${esc(t.tag)}" aria-label="Remove ${esc(t.tag)}">${icon('x')}</button></span>`).join('');
  el.innerHTML = `<div class="te-head"><b>${T.paths.length > 1 ? 'Add tags to' : 'Tags on'} ${esc(what)}</b><span class="num faint">${T.cur.length} / ${MAX_TAGS}</span></div>` +
    `<div class="te-box">${chips}<input id="dc-te-q" class="te-input" type="text" value="${esc(T.q)}" placeholder="${T.cur.length ? '' : 'Add a tag…'}" aria-label="Add a tag" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="${list.length > 0}" aria-controls="dc-te-list"></div>` +
    (list.length ? `<div id="dc-te-list" class="te-sugg" role="listbox">${list.map((m, i) =>
      `<button type="button" role="option" class="it${i === T?.hi ? ' hi' : ''}" aria-selected="${i === T?.hi}" data-add="${esc(m.tag)}">` +
      (m.create ? `${icon('plus')}<span>Create “<b>${esc(m.tag)}</b>”</span><kbd>Enter</kbd>` : `${icon('tag')}<span>${mark(m.tag)}</span><span class="num faint">used ${m.count}×</span>`) + '</button>').join('')}</div>` : '') +
    (sugg.length ? `<div class="te-sec">Often used</div><div class="te-row">${sugg.map((t) => `<button type="button" class="dc-tag add" data-add="${esc(t.tag)}">${icon('plus')}${esc(t.tag)}</button>`).join('')}</div>` : '') +
    `<p class="te-note">${icon('lock')}Auto tags come from format detection and update themselves. Your tags also apply to the files inside, for filtering.</p>` +
    `<div class="te-foot"><span class="grow"></span><button type="button" class="ghost" data-te="cancel">Cancel</button><button type="button" class="primary" data-te="save"${T.busy ? ' disabled' : ''}>Save tags</button></div>`;
  const i = el.querySelector<HTMLInputElement>('#dc-te-q');
  if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
}

function add(tag: string): void {
  if (!T) return;
  const t = clean(tag);
  if (!t || t.startsWith('format:')) return;
  if (T.cur.length >= MAX_TAGS) { toast(`At most ${MAX_TAGS} tags`, 'err'); return; }
  if (!T.cur.some((x) => x.tag === t)) T.cur.push({ tag: t, auto: false });
  T.q = '';
  T.hi = 0;
  paint();
}

async function save(): Promise<void> {
  if (!T || T.busy) return;
  if (T.q.trim()) add(T.q);
  const te = T;
  const before = new Set(te.orig.filter((t) => !t.auto).map((t) => t.tag));
  const after = new Set(te.cur.filter((t) => !t.auto).map((t) => t.tag));
  const plus = [...after].filter((t) => !before.has(t)), minus = te.paths.length > 1 ? [] : [...before].filter((t) => !after.has(t));
  if (!plus.length && !minus.length) { closeFloat(); return; }
  te.busy = true;
  paint();
  try {
    for (const p of te.paths) await api.setTags(te.bid, p, plus, minus);
    toast(`Tags saved${te.paths.length > 1 ? ` on ${fmtN(te.paths.length)} items` : ''}`, 'ok');
    closeFloat();
    await hooks.reload();
  } catch (e) {
    te.busy = false;
    toast(`Could not save the tags: ${errText(e)}`, 'err');
    paint();
  }
}

function popEl(): HTMLElement {
  let el = $('dc-tagpop');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'dc-tagpop';
  el.className = 'dc-pop te hidden';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-label', 'Edit tags');
  document.body.appendChild(el);
  el.addEventListener('click', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const rm = t?.closest<HTMLElement>('[data-rm]'), ad = t?.closest<HTMLElement>('[data-add]'), b = t?.closest<HTMLElement>('[data-te]');
    if (rm && T) { const tag = rm.dataset['rm']; T.cur = T.cur.filter((x) => x.auto || x.tag !== tag); paint(); }
    else if (ad) add(ad.dataset['add'] ?? '');
    else if (b?.dataset['te'] === 'cancel') closeFloat();
    else if (b?.dataset['te'] === 'save') void save();
  });
  el.addEventListener('input', (e) => {
    if (!T || !(e.target instanceof HTMLInputElement)) return;
    const v = e.target.value;
    if (/[,;]/.test(v)) { for (const p of v.split(/[,;]/).slice(0, -1)) add(p); T.q = v.split(/[,;]/).pop() ?? ''; }
    else T.q = v;
    T.hi = 0;
    paint();
  });
  el.addEventListener('keydown', (e) => {
    if (!T || !(e.target instanceof HTMLInputElement)) return;
    const list = matches();
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); T.hi = (T.hi + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % Math.max(1, list.length); paint(); }
    else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void save(); }
    else if (e.key === 'Enter') { e.preventDefault(); const m = list[T.hi]; if (m && T.q) add(m.tag); else if (T.q) add(T.q); else void save(); }
    else if (e.key === 'Backspace' && !T.q) { const last = [...T.cur].reverse().find((x) => !x.auto); if (last) { T.cur = T.cur.filter((x) => x !== last); paint(); } }
  });
  return el;
}

/** Open the editor for `paths` of bucket `bid` (one path: edit its tags; several: add tags to each). */
export function openTagEditor(from: HTMLElement, bid: string, paths: string[]): void {
  if (!bid || !paths.length) return;
  const orig = paths.length === 1 ? tagsOf(bid, paths[0] ?? '') : [];
  T = { bid, paths, orig, cur: orig.map((t) => ({ ...t })), q: '', hi: 0, busy: false };
  const el = popEl();
  paint();
  floatPopover(el, from, () => { T = null; });
  el.querySelector<HTMLInputElement>('#dc-te-q')?.focus();   // paint() ran while it was still hidden
  if (!D.tags.length) void api.allTags().then((t) => { D.tags = t; paint(); }).catch(() => {});
}
