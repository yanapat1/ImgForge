// A small message at the bottom of the page for non-fatal problems (the label page has its own, in label/panels.js).

import { $ } from './dom.js';

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
