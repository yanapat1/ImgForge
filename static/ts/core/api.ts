// fetch wrappers for the JSON API (errors become Error(path: status body)).
// Give the payload type at the call site: `getJSON<Meta>('api/meta')` (default `unknown`, so narrow before use).
// Every request of the page goes through apiFetch: it adds the X-Requested-With header the server wants on every
// non-GET /api request (its CSRF guard) and sends the session cookie; a 401 (signed out, session expired) sends
// the page to the sign-in page once, with `next` = where it was.

import { loginUrl, onLoginPage } from './nav.js';

export const XRW = 'dataviz';
let leaving = false;

/** The session is gone: go to the sign-in page (once, however many requests got the 401), then come back here. */
export function onUnauthorized(): void {
  if (leaving || onLoginPage()) return;
  leaving = true;
  setTimeout(() => location.replace(loginUrl(location.pathname + location.search)), 50);
}

/** fetch with the app's header and cookie; never throws on a status (a 401 also starts the sign-in redirect). */
export async function apiFetch(path: string, opts: RequestInit = {}): Promise<Response> {
  const headers = new Headers(opts.headers);
  headers.set('X-Requested-With', XRW);
  const r = await fetch(path, { credentials: 'same-origin', ...opts, headers });
  if (r.status === 401) onUnauthorized();
  return r;
}

/** apiFetch that throws `Error("path: status body")` on a non-2xx answer; resolves to the Response. */
export async function api(path: string, opts?: RequestInit): Promise<Response> {
  const r = await apiFetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r;
}

/** True when an error made by api() carries this HTTP status. */
export const isStatus = (e: unknown, status: number): boolean => new RegExp(`: ${status} `).test(errMessage(e));

/** GET path, parse JSON as T (T is the caller's claim about the server payload; types.ts has the shapes). */
export const getJSON = <T = unknown>(path: string, signal?: AbortSignal): Promise<T> =>
  api(path, signal ? { signal } : undefined).then((r): Promise<T> => r.json());

/** POST body as JSON, parse the JSON answer as T. */
export const postJSON = <T = unknown>(path: string, body?: unknown): Promise<T> => sendJSON<T>('POST', path, body);

/** Any method with a JSON body (PUT / PATCH / DELETE), parse the JSON answer as T. */
export const sendJSON = <T = unknown>(method: string, path: string, body?: unknown): Promise<T> => api(path, {
  method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? null : JSON.stringify(body),
}).then((r): Promise<T> => r.json());

/** The message of anything that was thrown (Error.message, or the value itself as a string). */
export function errMessage(e: unknown): string {
  if (typeof e === 'object' && e !== null && 'message' in e && e.message) return String(e.message);
  return String(e);
}

/** The server's `detail` message from an Error made by api(), else the plain message. */
export function errText(e: unknown): string {
  const msg = errMessage(e);
  const m = msg.match(/\{"detail":"(.*)"\}$/);
  return m && m[1] !== undefined ? m[1].replace(/\\"/g, '"') : msg;
}

// Resolves once the server answers api/status: a refused connection or a 502/503/504 means it is starting or
// restarting, so poll with backoff (500 ms rising to 3 s). Any other status counts as up (the caller reports it).
// Gives up after `timeout` ms by resolving false; onWait() is called before each retry.
export async function waitForServer(onWait?: () => void, timeout = 120000): Promise<boolean> {
  const t0 = Date.now();
  let wait = 500;
  for (;;) {
    try {
      const r = await apiFetch('api/status', { cache: 'no-store' });
      if (![502, 503, 504].includes(r.status)) return true;
    } catch (e) { /* not reachable yet */ }
    if (Date.now() - t0 > timeout) return false;
    if (onWait) onWait();
    await new Promise((res) => setTimeout(res, wait));
    wait = Math.min(wait * 1.5, 3000);
  }
}

/** The answer of postXHR: any HTTP status (the caller decides), the body as text and parsed JSON (null if not JSON). */
export interface XhrAnswer { status: number; text: string; json: unknown }

/** POST with upload progress (fetch has none): the app header and cookie, a Blob sent as octet-stream, a 401 starts
 *  the sign-in redirect. Rejects with Error('network') when the connection fails and Error('abort') when `track`'s
 *  xhr was aborted. `track(xhr, true|false)` lets the caller keep the request to cancel it. Both uploaders use it. */
export function postXHR(url: string, body: FormData | Blob, o: { onProgress?: (loaded: number) => void; track?: (x: XMLHttpRequest, live: boolean) => void } = {}): Promise<XhrAnswer> {
  return new Promise((ok, fail) => {
    const x = new XMLHttpRequest();
    const end = (): void => o.track?.(x, false);
    x.open('POST', url);
    x.setRequestHeader('X-Requested-With', XRW);
    if (body instanceof Blob) x.setRequestHeader('Content-Type', 'application/octet-stream');
    x.withCredentials = true;
    if (o.onProgress) { const f = o.onProgress; x.upload.onprogress = (e) => f(e.loaded); }
    x.onload = () => {
      end();
      if (x.status === 401) onUnauthorized();
      let json: unknown = null;
      try { json = JSON.parse(x.responseText); } catch (e) { /* not JSON */ }
      ok({ status: x.status, text: x.responseText, json });
    };
    x.onerror = () => { end(); fail(new Error('network')); };
    x.onabort = () => { end(); fail(new Error('abort')); };
    o.track?.(x, true);
    x.send(body);
  });
}
