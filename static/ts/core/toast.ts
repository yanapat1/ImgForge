// A small message at the bottom of the page for non-fatal problems (the label page has its own, in label/panels.js),
// and actionToast: a toast with buttons (Undo) used by the Data Center (datacenter/ops.ts).

import { $, esc, ICONS } from './dom.js';

export type ToastKind = '' | 'err' | 'ok' | (string & {});

let timer: ReturnType<typeof setTimeout> | undefined;
export function toast(msg: string, kind: ToastKind = ''): void {
  let el = $<HTMLDivElement>('app-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'app-toast';
    el.setAttribute('role', 'status');
    document.body.appendChild(el);
  }
  el.className = `app-toast show ${kind}`;
  el.textContent = msg;
  clearTimeout(timer);
  timer = setTimeout(() => { el.className = 'app-toast'; }, kind === 'err' ? 6000 : 3500);
}

/** An action toast (bottom left, one at a time): a title, an optional body (HTML, escaped by the caller), action
 *  buttons (e.g. Undo) and a close button; a thin bar shows the time left. `warn` gets the amber edge (format changes). */
export interface ActionToastOpts {
  title: string;
  body?: string;
  kind?: '' | 'warn' | 'err' | 'ok';
  actions?: Array<{ label: string; icon?: string; primary?: boolean; run: () => void | Promise<void> }>;
  /** ms before it closes by itself (default 9000; 0 = stays) */
  timeout?: number;
}
let actTimer: ReturnType<typeof setTimeout> | undefined;
export function actionToast(o: ActionToastOpts): () => void {
  let el = $<HTMLDivElement>('app-toast-act');
  if (!el) {
    el = document.createElement('div');
    el.id = 'app-toast-act';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  const box = el;
  const ms = o.timeout ?? 9000;
  const ic = (n: string, cls = ''): string => `<svg class="ico${cls ? ` ${cls}` : ''}" aria-hidden="true"><use href="${ICONS}#i-${n}"/></svg>`;
  const kind = o.kind || '';
  box.className = `act-toast show ${kind}`;
  box.innerHTML = `${ic(kind === 'ok' ? 'check-c' : kind === 'err' ? 'x-c' : 'alert', 't')}` +
    `<div class="at-body"><b>${esc(o.title)}</b>${o.body ? `<span>${o.body}</span>` : ''}` +
    (ms ? `<div class="at-timer"><i style="animation-duration:${ms}ms"></i></div>` : '') + '</div>' +
    `<div class="at-acts">${(o.actions || []).map((a, i) => `<button type="button" class="sm${a.primary === false ? '' : ' primary'}" data-i="${i}">${a.icon ? ic(a.icon) : ''}${esc(a.label)}</button>`).join('')}` +
    `<button type="button" class="icon-btn ghost sm" data-close aria-label="Close">${ic('x')}</button></div>`;
  const close = (): void => { clearTimeout(actTimer); box.className = 'act-toast'; box.innerHTML = ''; };
  box.onclick = (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('button') : null;
    if (!b) return;
    if (b.hasAttribute('data-close')) { close(); return; }
    const a = (o.actions || [])[Number(b.dataset['i'])];
    close();
    if (a) void Promise.resolve(a.run()).catch(() => {});
  };
  clearTimeout(actTimer);
  if (ms) actTimer = setTimeout(close, ms);
  return close;
}
