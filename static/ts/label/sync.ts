// Saving: autosave (400 ms after the last change) PUTs the full live set of each dirty (image, task) in the
// background; a failed PUT stays queued and is retried with backoff (offline indicator); a 409 (another tab saved
// first) is reported as a conflict for the page's banner. flushMid() resolves once an image has nothing unsaved,
// so commit / skip run strictly after the saves of that image. beforeunload sends what is left with keepalive.
// The payload is built at send time from the image's cache entry (latest state, never a stale snapshot).

import type { Ann, SaveResult, SaveState } from '../types-label.js';
import { L } from './state.js';

/** What the page keeps per cached image (the LRU entry): the live annotations and the versions the server gave. */
export interface SyncEntry { anns: Map<string, Ann>; versions: Record<string, number> }
export interface SyncConflict { mid: string; tid: string; version: number | undefined; annotations: Ann[] }
interface Pending { mid: string; tid: string }
export interface SyncHandlers {
  entry: (mid: string) => SyncEntry | null;
  saved: (mid: string, tid: string, res: SaveResult | null, err?: string) => void;
  conflict: (mid: string, tid: string) => void;
  state: (s: SaveState) => void;
  held: (mid?: string) => boolean;
}
/** The statuses answered by commit / skip / reopen. */
export interface StatusResult { status: Record<string, string> }

// a 409 / 4xx body: {detail, version?, annotations?}
interface ErrBody { detail?: unknown; version?: unknown; annotations?: unknown }
async function errBody(r: Response): Promise<ErrBody> {
  try {
    const d: unknown = await r.json();
    return typeof d === 'object' && d !== null ? d : {};
  } catch (e) { return {}; }
}

const DEBOUNCE = 400;
const pending = new Map<string, Pending>();      // 'mid|tid' -> {mid, tid}
const inflight = new Map<string, Promise<void>>();     // 'mid|tid' -> Promise
const conflicts = new Map<string, SyncConflict>();    // 'mid|tid' -> {mid, tid, version, annotations}
let timer: ReturnType<typeof setTimeout> | undefined, retryTimer: ReturnType<typeof setTimeout> | undefined, backoff = 0, failing = false;
// held(mid): the page holds back edits of (mid) or runs the model on it (its saves wait for the run);
// held() with no mid: some image has held-back edits (not yet queued here)
let H: SyncHandlers = { entry: () => null, saved: () => {}, conflict: () => {}, state: () => {}, held: () => false };

const key = (mid: string, tid: string): string => `${mid}|${tid}`;
const url = (mid: string): string => `api/label/projects/${encodeURIComponent(String(L.pid))}/annotations/${mid}`;

export function initSync(handlers: Partial<SyncHandlers>): void {
  H = { ...H, ...handlers };
  addEventListener('online', () => { if (failing) retryNow(); });
  addEventListener('beforeunload', unloadFlush);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush(); });
}

function updateState(): void {
  const s = conflicts.size ? 'conflict' : failing ? 'offline' : (pending.size || inflight.size) ? 'saving' : 'saved';
  if (s !== L.saveState) { L.saveState = s; H.state(s); }
}

// the annotations of (mid, tid) changed: save them soon
export function touch(mid: string, tids: Iterable<string>): void {
  for (const tid of tids) { const k = key(mid, tid); if (!conflicts.has(k)) pending.set(k, { mid, tid }); }
  clearTimeout(timer);
  if (!failing) timer = setTimeout(sendAll, DEBOUNCE);
  updateState();
}

export function flush(): void { clearTimeout(timer); if (!failing) sendAll(); }

export const busy = (mid: string): boolean => [...pending.values(), ...conflicts.values()].some((p) => p.mid === mid) ||
  [...inflight.keys()].some((k) => k.startsWith(mid + '|')) || !!H.held(mid);

function sendAll(): void { for (const k of [...pending.keys()]) send(k); }

function body(mid: string, tid: string): { e: SyncEntry; json: string } | null {
  const e = H.entry(mid);
  if (!e || !e.anns) return null;
  const anns: Ann[] = [];
  for (const a of e.anns.values()) if (a.task_id === tid) anns.push(a);
  return { e, json: JSON.stringify({ task_id: tid, base_version: e.versions[tid] || 0, annotations: anns }) };
}

function send(k: string): void {
  if (inflight.has(k)) return;                  // re-sent when the one in flight returns
  const p = pending.get(k);
  pending.delete(k);
  const b = p && body(p.mid, p.tid);
  if (!b) {
    if (p && H.entry(p.mid)) pending.set(k, p);  // its annotations are being reloaded: send once they are back
    updateState();
    return;
  }
  const pr = fetch(url(p.mid), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: b.json })
    .then(async (r) => {
      if (r.status === 409) {
        const d = await errBody(r);
        pending.delete(k);
        conflicts.set(k, { mid: p.mid, tid: p.tid, version: typeof d.version === 'number' ? d.version : undefined,
          annotations: Array.isArray(d.annotations) ? d.annotations as Ann[] : [] });
        H.conflict(p.mid, p.tid);
        return;
      }
      if (!r.ok) {
        if (r.status >= 400 && r.status < 500) {   // rejected (validation): retrying cannot help, report it
          const d = await errBody(r);
          H.saved(p.mid, p.tid, null, d.detail ? String(d.detail) : `save failed (${r.status})`);
          return;
        }
        throw new Error(String(r.status));
      }
      const res = await r.json() as SaveResult;
      b.e.versions[p.tid] = res.version;
      failing = false; backoff = 0;
      try { H.saved(p.mid, p.tid, res); } catch (err) { console.error(err); }
    })
    .catch(() => {
      if (!pending.has(k)) pending.set(k, p);
      failing = true;
      scheduleRetry();
    })
    .finally(() => {
      inflight.delete(k);
      if (pending.has(k) && !failing) { clearTimeout(timer); timer = setTimeout(sendAll, 30); }
      updateState();
    });
  inflight.set(k, pr);
  updateState();
}

// one backoff step per failed round, not per failed key (5 dirty keys offline must not mean a 30 s wait)
function scheduleRetry(): void {
  if (retryTimer) return;
  backoff = Math.min(30000, backoff ? backoff * 2 : 1000);
  retryTimer = setTimeout(retryNow, backoff);
}

function retryNow(): void { clearTimeout(retryTimer); retryTimer = undefined; failing = false; sendAll(); updateState(); }

// resolves when (mid) has nothing pending or in flight (conflicts excluded: they wait for the user)
export async function flushMid(mid: string): Promise<void> {
  for (;;) {
    for (const [k, p] of pending) if (p.mid === mid && !failing) send(k);
    const fl = [...inflight].filter(([k]) => k.startsWith(mid + '|')).map(([, pr]) => pr);
    const left = [...pending.values()].some((p) => p.mid === mid);
    if (!fl.length && !left) return;
    if (fl.length) await Promise.all(fl);
    else await new Promise((r) => setTimeout(r, 250));      // offline: wait for the retry
  }
}

// commit / skip / reopen after the image's saves; returns {status:{tid: …}}
const chains = new Map<string, Promise<StatusResult>>();
export function postStatus(mid: string, kind: string, taskIds: string[]): Promise<StatusResult> {
  const prev = chains.get(mid) || Promise.resolve();
  const run = prev.catch(() => {}).then(() => flushMid(mid)).then(async () => {
    const r = await fetch(`api/label/projects/${encodeURIComponent(String(L.pid))}/${kind}/${mid}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_ids: taskIds }),
    });
    if (!r.ok) { const d = await errBody(r); throw new Error(d.detail ? String(d.detail) : `${kind} failed (${r.status})`); }
    return r.json() as Promise<StatusResult>;
  });
  chains.set(mid, run);
  run.finally(() => { if (chains.get(mid) === run) chains.delete(mid); }).catch(() => {});
  return run;
}

// conflicts: the server copy (reload) or overwrite it with ours (keep mine)
export const conflictsOf = (mid: string): SyncConflict[] => [...conflicts.values()].filter((c) => c.mid === mid);

export function resolveConflict(mid: string, keepMine: boolean): SyncConflict[] {
  const out: SyncConflict[] = [];
  for (const [k, c] of conflicts) {
    if (c.mid !== mid) continue;
    conflicts.delete(k);
    out.push(c);
    const e = H.entry(mid);
    if (e && c.version != null) e.versions[c.tid] = c.version;
    if (keepMine) pending.set(k, { mid, tid: c.tid });
  }
  if (keepMine) flush();
  updateState();
  return out;
}

export function dropMid(mid: string): void {
  for (const k of [...pending.keys()]) if (k.startsWith(mid + '|')) pending.delete(k);
  updateState();
}

// last chance: keepalive PUTs for what is queued, and the browser's "leave page?" prompt for anything that cannot
// be sent safely now: offline, an open conflict, or a key whose PUT is in flight (a keepalive PUT would carry the
// pre-response base_version and lose to it with a 409), or edits the page holds back while the model runs
function unloadFlush(ev: BeforeUnloadEvent): void {
  clearTimeout(timer);
  for (const [k, p] of pending) {
    if (failing || inflight.has(k)) continue;
    const b = body(p.mid, p.tid);
    if (!b) continue;
    try {
      void fetch(url(p.mid), { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: b.json });
      pending.delete(k);
    } catch (e) { /* body over the keepalive quota: keep it, the prompt below asks the user to stay */ }
  }
  if (pending.size || inflight.size || conflicts.size || H.held()) { ev.preventDefault(); ev.returnValue = ''; }
}

export const hasUnsaved = (): boolean => pending.size > 0 || inflight.size > 0 || !!H.held();
