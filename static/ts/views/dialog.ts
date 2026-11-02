// A small modal dialog built on demand (one at a time, on top of anything, the Settings dialog too): a title, any
// body HTML, Cancel + one action button. The action runs `onOk(root)`: it returns an error text to keep the dialog
// open with that text shown (role=alert), or null to close. Esc / Cancel / the backdrop close it; Tab stays inside;
// the focus goes back to where it was. Used by the share dialog and the account / users settings.

import { esc, icon } from '../core/dom.js';

export interface DialogOpts {
  title: string;
  /** body HTML (escaped by the caller) */
  body: string;
  ok: string;
  /** 'primary' (default) or 'danger-btn' */
  okClass?: string;
  wide?: boolean;
  /** wires the body (inputs, live checks); runs once the dialog is in the page */
  bind?: (root: HTMLElement) => void;
  /** the action: an error text keeps the dialog open, null closes it */
  onOk: (root: HTMLElement) => Promise<string | null> | string | null;
  /** the element to focus first (a selector inside the body), else the first field */
  focus?: string;
  /** true while the form holds input worth keeping: a click on the backdrop then does not close the dialog */
  dirty?: (root: HTMLElement) => boolean;
}

let current: { close: (ok: boolean) => void } | null = null;

/** True while one of these dialogs is open (global keys stand back). */
export const dialogOpen = (): boolean => current !== null;

export const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]';

export const focusables = (root: HTMLElement): HTMLElement[] =>
  [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((x) => x.offsetParent !== null);

/** Keep Tab / Shift+Tab inside `box` (every modal of the app uses this one trap). Returns true when it handled the key. */
export function trapTab(box: HTMLElement, e: KeyboardEvent): boolean {
  if (e.key !== 'Tab') return false;
  const f = focusables(box);
  e.preventDefault();
  e.stopPropagation();
  if (!f.length) { box.focus(); return true; }
  const i = f.findIndex((x) => x === document.activeElement);
  f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length]?.focus();
  return true;
}

/** Plain text as dialog paragraphs: a blank line starts a new one. */
export const paras = (text: string): string => text.split(/\n\s*\n/).map((t) => `<p>${esc(t.trim())}</p>`).join('');

/** Show the dialog; resolves true after a successful action, false when it was dismissed. */
export function openDialog(o: DialogOpts): Promise<boolean> {
  current?.close(false);
  const back = document.activeElement;
  const wrap = document.createElement('div');
  wrap.className = 'modal ui-dlg-wrap';
  const id = `ui-dlg-${Date.now().toString(36)}`;
  wrap.innerHTML = `<div class="ui-dlg panel${o.wide ? ' wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="${id}-t" tabindex="-1">` +
    `<header class="ui-dlg-head"><h3 id="${id}-t">${esc(o.title)}</h3>` +
    `<button type="button" class="icon-btn ghost" data-dlg="cancel" aria-label="Close" title="Close (Esc)">${icon('x')}</button></header>` +
    `<div class="ui-dlg-body">${o.body}</div>` +
    '<div class="ui-dlg-err status-line err hidden" role="alert"></div>' +
    `<div class="form-foot"><button type="button" class="ghost" data-dlg="cancel">Cancel</button>` +
    `<button type="button" class="${o.okClass || 'primary'}" data-dlg="ok">${esc(o.ok)}</button></div></div>`;
  document.body.appendChild(wrap);
  const box = wrap.querySelector<HTMLElement>('.ui-dlg');
  const okBtn = wrap.querySelector<HTMLButtonElement>('[data-dlg="ok"]');
  const errEl = wrap.querySelector<HTMLElement>('.ui-dlg-err');
  if (!box || !okBtn || !errEl) throw new Error('dialog markup');
  return new Promise((resolve) => {
    let busy = false;
    const close = (ok: boolean): void => {
      if (current?.close !== close) return;
      current = null;
      wrap.remove();
      if (back instanceof HTMLElement && document.contains(back)) back.focus();
      resolve(ok);
    };
    current = { close };
    const run = async (): Promise<void> => {
      if (busy) return;
      busy = true;
      okBtn.disabled = true;
      errEl.classList.add('hidden');
      let err: string | null;
      try { err = await o.onOk(box); } catch (e) { err = e instanceof Error ? e.message : String(e); }
      busy = false;
      okBtn.disabled = false;
      if (err === null) { close(true); return; }
      errEl.innerHTML = `${icon('alert')}<span>${esc(err)}</span>`;
      errEl.classList.remove('hidden');
    };
    wrap.addEventListener('click', (e) => {
      const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-dlg]') : null;
      if (b?.dataset['dlg'] === 'cancel') close(false);
      else if (b?.dataset['dlg'] === 'ok') void run();
    });
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap && !o.dirty?.(box)) close(false); });
    wrap.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); return; }
      if (e.key === 'Enter' && e.target instanceof HTMLInputElement && e.target.type !== 'checkbox' && !e.target.dataset['noenter']) {
        e.preventDefault(); void run(); return;
      }
      trapTab(box, e);
    });
    o.bind?.(box);
    const first = (o.focus && box.querySelector<HTMLElement>(o.focus)) || focusables(box).find((x) => !x.matches('[data-dlg="cancel"]'));
    (first || box).focus();
  });
}

/** A yes / no question in the app's dialog (replaces window.confirm). `text` is plain text, blank lines split it. */
export function confirmDialog(o: { title: string; text?: string; ok: string; danger?: boolean }): Promise<boolean> {
  return openDialog({ title: o.title, body: o.text ? `<div class="ui-dlg-text">${paras(o.text)}</div>` : '', ok: o.ok,
    okClass: o.danger ? 'danger-btn' : 'primary', onOk: () => null, focus: o.danger ? '.form-foot [data-dlg="cancel"]' : '[data-dlg="ok"]' });
}

/** One text field (replaces window.prompt): resolves to the trimmed text, or null when dismissed. `check` returns an
 *  error text to keep the dialog open. */
export async function promptDialog(o: { title: string; label: string; value?: string; ok: string; hint?: string;
  check?: (v: string) => string | null }): Promise<string | null> {
  let out: string | null = null;
  const done = await openDialog({
    title: o.title, ok: o.ok, focus: '#ui-dlg-in',
    body: `<label class="fld"><span>${esc(o.label)}</span><input type="text" class="txt" id="ui-dlg-in" value="${esc(o.value ?? '')}" autocomplete="off" spellcheck="false"></label>` +
      (o.hint ? `<p class="hint">${esc(o.hint)}</p>` : ''),
    bind: (r) => r.querySelector<HTMLInputElement>('#ui-dlg-in')?.select(),
    onOk: (r) => {
      const v = r.querySelector<HTMLInputElement>('#ui-dlg-in')?.value.trim() ?? '';
      const err = v ? o.check?.(v) ?? null : `Enter ${o.label.toLowerCase()}`;
      if (!err) out = v;
      return err;
    },
  });
  return done ? out : null;
}
