// The one NDJSON reader of the page (the ◆ chats, core/chat.ts): POST a JSON body through apiFetch (the app's header
// and cookie), then call onEvent with every parsed line as it arrives. A non-2xx answer throws the same
// `Error("path: status body")` as core/api.ts api() (so errText() gives the server's detail); a line that is not
// JSON is skipped; the caller's AbortSignal stops the read (isAbort() tells that apart from a failure).

import { apiFetch } from './api.js';

/** True for the error an aborted fetch / read throws. */
export const isAbort = (e: unknown): boolean => typeof e === 'object' && e !== null && 'name' in e && (e as { name: unknown }).name === 'AbortError';

/** Split a growing buffer into complete lines: returns the lines and the unfinished rest. */
export function splitLines(buf: string): { lines: string[]; rest: string } {
  const parts = buf.split('\n');
  const rest = parts.pop() ?? '';
  return { lines: parts.map((s) => s.trim()).filter(Boolean), rest };
}

/** POST `body` to `path` and stream its NDJSON answer into onEvent (one parsed object per line). Resolves when the
 *  server closes the stream. */
export async function streamNDJSON(path: string, body: unknown, onEvent: (ev: unknown) => void, signal?: AbortSignal): Promise<void> {
  const init: RequestInit = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  if (signal) init.signal = signal;
  const r = await apiFetch(path, init);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  if (!r.body) throw new Error('the server sent no body');
  const rd = r.body.getReader(), dec = new TextDecoder();
  let buf = '';
  const emit = (line: string): void => {
    let v: unknown;
    try { v = JSON.parse(line); } catch (e) { return; }
    onEvent(v);
  };
  for (;;) {
    const { done, value } = await rd.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const s = splitLines(buf);
    buf = s.rest;
    for (const line of s.lines) emit(line);
  }
  buf += dec.decode();
  if (buf.trim()) emit(buf.trim());
}
