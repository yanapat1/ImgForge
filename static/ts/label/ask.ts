// The label page's small question dialog (ask) and its toast. Split out of label/panels.ts, which re-exports them.

import { esc } from '../core/dom.js';
import { toast as appToast } from '../core/toast.js';
import type { ToastKind } from '../core/toast.js';
import type { AskOptions, AskResult } from '../types-label-ui.js';
import { trapTab } from '../views/dialog.js';
import { closestEl } from './ui-util.js';

// ---------------------------------------------------------------- dialog + toast
let dlgSeq = 0;
export function ask({ title, html, buttons }: AskOptions): Promise<AskResult | null> {
  return new Promise((resolve) => {
    const tid = `lb-dlg-t${++dlgSeq}`;                  // unique: a dialog may open over another
    const back = document.createElement('div');
    back.className = 'modal lb-dlg-back';
    back.innerHTML = `<div class="panel lb-dlg" role="dialog" aria-modal="true" aria-labelledby="${tid}" tabindex="-1">
      <h2 id="${tid}">${esc(title)}</h2><div class="lb-dlg-body">${html || ''}</div>
      <div class="lb-dlg-actions">${buttons.map((b) => `<button data-b="${esc(b.id)}" class="${b.primary ? 'primary' : b.danger ? 'danger' : ''}">${esc(b.label)}</button>`).join('')}</div></div>`;
    document.body.appendChild(back);
    const prev = document.activeElement;
    const done = (v: AskResult | null): void => { back.remove(); if (prev instanceof HTMLElement && document.contains(prev)) prev.focus(); resolve(v); };
    back.addEventListener('click', (e) => {
      const b = closestEl(e.target, '[data-b]');
      if (b) {
        const values: Record<string, string> = {};
        for (const f of back.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[name]')) values[f.name] = f.value;
        done({ id: b.dataset['b'] ?? '', values });
      } else if (e.target === back) done(null);
    });
    const box = back.querySelector<HTMLElement>('.lb-dlg');
    back.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); done(null); return; }
      if (e.key === 'Enter' && e.target instanceof HTMLInputElement) {         // Enter in a field = the primary button
        e.preventDefault();
        back.querySelector<HTMLElement>('.lb-dlg-actions .primary')?.click();
        return;
      }
      if (box) trapTab(box, e);
    });
    (back.querySelector<HTMLElement>('select, .primary, button') || back.querySelector<HTMLElement>('.lb-dlg'))?.focus();
  });
}

/** The label page's messages use the app toast (core/toast.ts). */
export function toast(msg: string, kind: ToastKind = ''): void { appToast(msg, kind); }
