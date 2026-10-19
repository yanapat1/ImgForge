// fetch wrappers for the JSON API (errors become Error(path: status body)).
// Give the payload type at the call site: `getJSON<Meta>('api/meta')` (default `unknown`, so narrow before use).

/** fetch that throws `Error("path: status body")` on a non-2xx answer; resolves to the Response. */
export async function api(path: string, opts?: RequestInit): Promise<Response> {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r;
}

/** GET path, parse JSON as T (T is the caller's claim about the server payload; types.ts has the shapes). */
export const getJSON = <T = unknown>(path: string): Promise<T> => api(path).then((r): Promise<T> => r.json());

/** POST body as JSON, parse the JSON answer as T. */
export const postJSON = <T = unknown>(path: string, body?: unknown): Promise<T> => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
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
      const r = await fetch('api/status', { cache: 'no-store' });
      if (![502, 503, 504].includes(r.status)) return true;
    } catch (e) { /* not reachable yet */ }
    if (Date.now() - t0 > timeout) return false;
    if (onWait) onWait();
    await new Promise((res) => setTimeout(res, wait));
    wait = Math.min(wait * 1.5, 3000);
  }
}
