// A session's tags inside an edit form (the settings dataset forms, the labeling wizard's name step): chips with ×,
// an input (Enter / comma adds, a pasted list splits on commas and newlines, Backspace on empty removes the last),
// suggestions from the other sessions' tags in a themed list (core/combobox.ts: a pick adds it), n / 20. The field repaints itself in place (the host form never has to
// re-render per keystroke); the host keeps a TagState in its form state and reads .tags when it saves.
// The client mirrors the server rules (dataviz/core/names.py clean_tags: trim, collapse spaces, ≤ 32 chars, ≤ 20,
// case-insensitive dedupe).

import { bindCombo } from './combobox.js';
import { esc, icon } from './dom.js';

export const MAX_TAGS = 20, TAG_LEN = 32;

/** draft: the text still in the box (kept across a host re-render); err: why the last add was refused. */
export interface TagState { tags: string[]; draft: string; err: string }
/** What bindTagFields needs per field: its state and the suggestions to offer. */
export interface TagFieldRef { st: TagState; suggest: readonly string[] }

export const tagState = (tags: readonly string[] | null | undefined): TagState => ({ tags: [...(tags || [])], draft: '', err: '' });
export const cleanTag = (t: unknown): string => String(t).trim().replace(/\s+/g, ' ');
const fold = (s: unknown): string => String(s).toLowerCase();
/** The same tags in the same order (what decides whether a save sends them). */
export const sameTags = (a: readonly string[], b: readonly string[] | null | undefined): boolean => a.join('\n') === (b || []).join('\n');

// the suggestions not on the field yet, one per spelling (case-insensitive), sorted
function suggestions(st: TagState, suggest: readonly string[]): string[] {
  const have = new Set(st.tags.map(fold));
  return [...new Map(suggest.filter((t) => !have.has(fold(t))).map((t) => [fold(t), t])).values()].sort((a, b) => a.localeCompare(b));
}

function inner(key: string, st: TagState): string {
  const full = st.tags.length >= MAX_TAGS;
  return `<span class="tagf-head"><span>Tags <span class="muted">(optional)</span></span><span class="tagf-n num${full ? ' full' : ''}">${st.tags.length} / ${MAX_TAGS}</span></span>` +
    `<span class="tagf-box" data-cbx="tagf-${esc(key)}">` + st.tags.map((t, i) => `<span class="tagf-chip"><span class="nm">${esc(t)}</span>` +
      `<button type="button" data-tagf-rm="${i}" aria-label="Remove the tag ${esc(t)}" title="Remove">${icon('x')}</button></span>`).join('') +
    `<input type="text" class="tagf-in" value="${esc(st.draft)}" autocomplete="off" spellcheck="false" aria-label="New tag" ` +
    `role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="cbx-list-tagf-${esc(key)}" ` +
    `placeholder="${full ? '' : st.tags.length ? 'Add a tag…' : 'Add a tag: Enter or comma adds it'}" ${full ? 'disabled' : ''}></span>` +
    (st.err ? `<span class="tagf-err" role="alert">${icon('alert')}<span>${esc(st.err)}</span></span>`
      : `<span class="tagf-note">${full ? `That is the limit: at most ${MAX_TAGS} tags. Remove one to add another.`
        : `Up to ${MAX_TAGS} tags of ${TAG_LEN} characters at most; a click on a tag on a card filters by it.`}</span>`);
}

/** The field: key names it inside its form (one form can only hold one field per key). */
export function tagFieldHTML(key: string, st: TagState, _suggest?: readonly string[]): string {
  return `<div class="fld tagf" data-tagf="${esc(key)}">${inner(key, st)}</div>`;
}

// add raw tags (typed / pasted) -> the refused ones worth fixing (too long); .over: some did not fit in 20
function add(st: TagState, raw: string[]): { bad: string[]; over: boolean } {
  const res = { bad: [] as string[], over: false };
  st.err = '';
  for (const r of raw) {
    const t = cleanTag(r);
    if (!t) continue;
    if (t.length > TAG_LEN) { st.err = `“${t.slice(0, TAG_LEN)}…” is too long: a tag has at most ${TAG_LEN} characters.`; res.bad.push(t); continue; }
    if (st.tags.some((x) => fold(x) === fold(t))) continue;
    if (st.tags.length >= MAX_TAGS) { st.err = `At most ${MAX_TAGS} tags: the rest were not added.`; res.over = true; break; }
    st.tags.push(t);
  }
  return res;
}

/** Add the text still in the box (call it before a save); false when something was refused (the field shows why). */
export function commitTagDraft(st: TagState, extra = ''): boolean {
  const text = st.draft + extra;
  if (!text.trim()) { st.draft = ''; return true; }
  const { bad, over } = add(st, text.split(/[,\n\r]+/));
  st.draft = bad.join(', ');
  return !bad.length && !over;
}

// repaint one field in place, the focus back in its input
function paint(el: HTMLElement, ref: TagFieldRef, focus: boolean): void {
  el.innerHTML = inner(el.dataset['tagf'] ?? '', ref.st);
  const inp = el.querySelector<HTMLInputElement>('.tagf-in');
  if (focus && inp && !inp.disabled) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
}

/** One delegated set of listeners on the host dialog (capture: an Enter in the box never reaches the host's own Enter). */
export function bindTagFields(root: HTMLElement, find: (key: string) => TagFieldRef | null, onChange?: () => void): void {
  const at = (t: EventTarget | null): { el: HTMLElement; ref: TagFieldRef } | null => {
    const el = t instanceof Element ? t.closest<HTMLElement>('[data-tagf]') : null;
    const ref = el ? find(el.dataset['tagf'] ?? '') : null;
    return el && ref ? { el, ref } : null;
  };
  const changed = (el: HTMLElement, ref: TagFieldRef): void => { paint(el, ref, true); onChange?.(); };
  // the suggestion list of field `key`: bound when its input takes the focus, from a window capture listener so it
  // runs before the combobox's own document listeners (they open the list on that same focusin). A pick adds the tag;
  // Enter / Escape / arrows while the list is open stay with the list, a comma or Ctrl+Enter still come here.
  const fieldEl = (key: string): HTMLElement | undefined => [...root.querySelectorAll<HTMLElement>('[data-tagf]')].find((x) => x.dataset['tagf'] === key);
  window.addEventListener('focusin', (e) => {
    const f = e.target instanceof HTMLInputElement && e.target.classList.contains('tagf-in') && root.contains(e.target) ? at(e.target) : null;
    const key = f?.el.dataset['tagf'];
    if (!f || key == null) return;
    bindCombo(root, `tagf-${key}`, {
      options: () => { const r = find(key); return r ? suggestions(r.st, r.suggest) : []; },
      empty: () => '',
      onPick: (v) => {
        const r = find(key), el = fieldEl(key);
        if (!r || !el) return;
        r.st.draft = v;
        commitTagDraft(r.st);
        changed(el, r);
      },
    });
  }, true);
  root.addEventListener('click', (e) => {
    const f = at(e.target);
    if (!f) return;
    const rm = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-tagf-rm]') : null;
    if (rm) { e.stopPropagation(); f.ref.st.tags.splice(+(rm.dataset['tagfRm'] ?? NaN), 1); f.ref.st.err = ''; changed(f.el, f.ref); return; }
    if (e.target instanceof Element && e.target.closest('.tagf-box')) f.el.querySelector<HTMLInputElement>('.tagf-in')?.focus();
  }, true);
  root.addEventListener('keydown', (e) => {
    const f = at(e.target);
    if (!f || !(e.target instanceof HTMLInputElement) || e.isComposing) return;
    const { st } = f.ref;
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { st.draft = e.target.value; commitTagDraft(st); return; }   // the host's save
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault(); e.stopPropagation();
      st.draft = e.target.value;
      commitTagDraft(st);
      changed(f.el, f.ref);
    } else if (e.key === 'Backspace' && !e.target.value && st.tags.length) {
      e.preventDefault(); e.stopPropagation();
      st.tags.pop(); st.err = '';
      changed(f.el, f.ref);
    }
  }, true);
  // phone keyboards send no keydown for ','; a typed comma still splits
  root.addEventListener('input', (e) => {
    const f = at(e.target);
    if (!f || !(e.target instanceof HTMLInputElement)) return;
    e.stopPropagation();
    const { st } = f.ref;
    st.draft = e.target.value;
    if (!st.draft.includes(',')) return;
    const parts = st.draft.split(',');
    const rest = parts.pop() ?? '';
    const { bad, over } = add(st, parts);
    st.draft = [...bad, over ? '' : rest].filter(Boolean).join(', ');
    changed(f.el, f.ref);
  }, true);
  root.addEventListener('paste', (e) => {
    const f = at(e.target);
    const text = e.clipboardData?.getData('text') ?? '';
    if (!f || !(e.target instanceof HTMLInputElement) || !/[,\n\r]/.test(text)) return;     // one tag: the normal paste
    e.preventDefault(); e.stopPropagation();
    f.ref.st.draft = e.target.value;
    commitTagDraft(f.ref.st, text);
    changed(f.el, f.ref);
  }, true);
}
