// The error page: one full-screen card for anything fatal (session could not open, server gone, uncaught crash).
// Non-fatal problems use toast() instead.

import { $, $req, esc, icon } from './dom.js';
import { errMessage } from './api.js';
import { toast } from './toast.js';

export type ErrorKind = 'session' | 'network' | 'notfound' | 'server' | 'crash';

const COPY: Record<ErrorKind, [string, string | null]> = {
  session: ['Could not open this session', null],
  network: ['Cannot reach the server', 'It may be restarting or the connection dropped. This page retries on its own.'],
  notfound: ['Not found', 'This page or item does not exist (any more).'],
  server: ['The server hit a problem', 'The request failed on the server. Details are in its log.'],
  crash: ['Something went wrong', 'The page hit an unexpected error.'],
};

let retryTimer: ReturnType<typeof setTimeout> | undefined;

// showError({kind, title, message, detail, retry}): retry() replaces the default reload; on 'network' it is also
// tried every few seconds (backoff up to 15 s) and the page reloads once the server answers.
export interface ErrorPageOptions { kind?: ErrorKind; title?: string; message?: string; detail?: string; retry?: () => void }

export function showError({ kind = 'crash', title, message, detail = '', retry }: ErrorPageOptions = {}): void {
  const [t, m] = COPY[kind] || COPY.crash;
  $('startup')?.classList.add('hidden');
  $('errpage')?.remove();
  const el = document.createElement('div');
  el.id = 'errpage';
  el.className = 'errpage';
  el.innerHTML = `<div class="errpage-box panel" role="alert">
    <div class="errpage-icon">${icon('alert')}</div>
    <h2>${esc(title || t)}</h2>
    <div class="errpage-msg muted">${esc(message || m || '')}</div>
    ${detail ? `<details class="errpage-details"><summary>Technical details</summary><pre>${esc(detail)}</pre></details>` : ''}
    <div class="errpage-actions">
      <button id="err-retry" class="primary">${icon('refresh')}Try again</button>
      <button id="err-home">All sessions</button>
      ${detail ? '<button id="err-copy">Copy details</button>' : ''}
    </div>
    ${kind === 'network' ? '<div class="errpage-note muted" id="err-note">Retrying…</div>' : ''}
  </div>`;
  document.body.appendChild(el);
  $req('err-retry').onclick = () => (retry ? retry() : location.reload());
  $req('err-home').onclick = () => { location.href = './'; };
  const copy = $('err-copy');
  if (copy) copy.onclick = () => navigator.clipboard?.writeText(`${title || t}\n${message || m || ''}\n${detail}`)
    .then(() => { copy.textContent = 'Copied'; }, () => {});
  $req('err-retry').focus();
  clearTimeout(retryTimer);
  if (kind === 'network') pingUntilUp(2000);
}

function pingUntilUp(wait: number): void {
  retryTimer = setTimeout(async () => {
    try { if ((await fetch('api/status', { cache: 'no-store' })).ok) { location.reload(); return; } } catch (e) { /* still down */ }
    if ($('errpage')) pingUntilUp(Math.min(wait * 1.5, 15000));
  }, wait);
}

// uncaught errors after boot are not fatal (one broken handler should not hide the page): a toast, not the error page
export function installCrashHandlers(): void {
  const note = (e: unknown): void => toast(`Unexpected error: ${(e && errMessage(e)) || e}`, 'err');
  window.addEventListener('error', (ev) => { if (!ev.target || ev.target === window) note(ev.error || ev.message); });
  window.addEventListener('unhandledrejection', (ev) => note(ev.reason));
}
