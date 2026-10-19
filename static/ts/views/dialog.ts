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
}

let current: { close: (ok: boolean) => void } | null = null;

const focusables = (root: HTMLElement): HTMLElement[] => [...root.querySelectorAll<HTMLElement>(
  'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')].filter((x) => x.offsetParent !== null);

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
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) close(false); });
    wrap.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); return; }
      if (e.key === 'Enter' && e.target instanceof HTMLInputElement && e.target.type !== 'checkbox' && !e.target.dataset['noenter']) {
        e.preventDefault(); void run(); return;
      }
      if (e.key !== 'Tab') return;
      const f = focusables(box);
      const i = f.findIndex((x) => x === document.activeElement);
      e.preventDefault();
      e.stopPropagation();
      f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length]?.focus();
    });
    o.bind?.(box);
    const first = (o.focus && box.querySelector<HTMLElement>(o.focus)) || focusables(box).find((x) => !x.matches('[data-dlg="cancel"]'));
    (first || box).focus();
  });
}
