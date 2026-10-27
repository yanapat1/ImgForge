// The live change feed of a session whose labels change elsewhere (a labeling session, a promoted combined
// dataset): GET api/live long-polls (wait=25 s) for patches newer than the last generation seen. Class changes,
// deletions, restores and moves go through curation.ts applyPatch (the same path as a Curation fix), new boxes
// count in a badge until a re-index places them, a compare diff patch updates S.cmp. The loop runs only when
// caps.live, pauses while the tab is hidden (the request is aborted), backs off on errors (1 s doubling to 30 s)
// and stops for good when the server has no feed (404) or says live: false.

import type { CmpSummary, LivePlaced, Points } from '../types.js';
import type { CurPatch } from '../types-views.js';
import { apiFetch } from './api.js';
import { $, esc, sleep } from './dom.js';
import { S } from './state.js';
import { toast } from './toast.js';
import { updateLiveNew } from '../plot/render.js';
import { applyPatch } from '../views/curation.js';
import { setLivePending } from '../views/image-grid.js';
import { patchDiff } from '../views/compare.js';
import { modalImage } from '../views/modal.js';
import { dirty, reloadBoxes } from '../views/viewer.js';

/** One change set of GET api/live. */
interface LivePatch {
  gen: number;
  cls?: Record<string, number>; deleted?: number[]; restored?: number[];
  moved?: Array<{ id: number; bbox: number[] }>;
  new?: unknown[];
  /** new boxes embedded with the feature of `proj` and placed at (x, y) of that projection */
  placed?: LivePlaced[];
  diff?: Record<string, { status?: Record<string, number>; summary?: CmpSummary }>;
}
interface LiveFeed { gen: number; live: boolean; resync?: boolean; reindex?: boolean; patches: LivePatch[]; pending_new?: number }

const L = { running: false, gen: null as number | null, ctrl: null as AbortController | null, wake: null as (() => void) | null, pending: 0 };

/** Start the loop once (no-op unless the session has a live feed). */
export function startLive(): void {
  if (L.running || !S.status?.caps?.live) return;
  L.running = true;
  S.liveNew = { byAnn: new Map() };
  // another projection: ask again right away with the new proj= (the server re-places the pending boxes there)
  window.addEventListener('view-changed', () => L.ctrl?.abort());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) L.ctrl?.abort();          // no long poll from a background tab
    else { L.wake?.(); L.wake = null; }
  });
  void loop();
}

export function stopLive(): void { L.running = false; L.ctrl?.abort(); }

const visible = (): Promise<void> => new Promise((r) => { L.wake = r; });

async function loop(): Promise<void> {
  let backoff = 1000;
  while (L.running) {
    if (document.hidden) { await visible(); continue; }
    const ctrl = new AbortController();
    L.ctrl = ctrl;
    try {
      const qs = new URLSearchParams({ wait: '25', proj: S.view?.key ?? '' });
      if (L.gen !== null) qs.set('since', String(L.gen));
      const r = await apiFetch(`api/live?${qs}`, { signal: ctrl.signal, cache: 'no-store' });
      if (r.status === 404 || r.status === 401 || r.status === 403) { L.running = false; break; }   // no feed here
      if (!r.ok) throw new Error(`api/live: ${r.status}`);
      const f = await r.json() as LiveFeed;
      backoff = 1000;
      if (!f.live) { L.running = false; break; }
      const first = L.gen === null;
      L.gen = f.gen;
      if (!first) await apply(f);
      const pend = f.pending_new ?? L.pending;
      setBadge(pend);
      setLivePending(pend);          // the re-index banner counts the feed's pending boxes only (no double count)
    } catch (e) {
      if (ctrl.signal.aborted) continue;           // hidden: wait for the tab to come back
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 30000);
    }
  }
  setBadge(null);
  setLivePending(null);
}

async function apply(f: LiveFeed): Promise<void> {
  if (f.reindex) toast('Many labels changed since the index: re-index the session to update the map.', '');
  // every patch of this answer as one: one render, one filter run
  const all: CurPatch = { cls: {}, deleted: [], restored: [], moved: [] };
  const touched = new Set<number>();
  for (const p of f.patches) {
    Object.assign(all.cls ?? {}, p.cls ?? {});
    all.deleted?.push(...(p.deleted ?? []));
    all.restored?.push(...(p.restored ?? []));
    all.moved?.push(...(p.moved ?? []));
    for (const id of [...Object.keys(p.cls ?? {}).map(Number), ...(p.deleted ?? []), ...(p.restored ?? []), ...(p.moved ?? []).map((m) => m.id)]) touched.add(id);
  }
  if (f.resync) await resync(all);
  applyPatch(all);
  placeNew(f);
  for (const p of f.patches) for (const [key, d] of Object.entries(p.diff ?? {})) patchDiff(key, d.status, d.summary);
  // the open image changed: show the new boxes unless the editor holds unsaved ones
  const open = modalImage(), P = S.P;
  if (open && P && [...touched].some((id) => P.image[id] === open)) {
    if (dirty()) toast('This image was changed elsewhere: save or discard to see the new boxes.', '');
    else void reloadBoxes();
  }
}

// provisional dots: kept per annotation; only those placed in the shown projection are drawn (render.ts drops
// the others, and the server re-places every pending box when the proj= of the next poll changes)
function placeNew(f: LiveFeed): void {
  const ln = S.liveNew;
  if (!ln) return;
  let changed = false;
  if (f.reindex || f.resync) { ln.byAnn.clear(); changed = true; }
  const key = S.view?.key ?? '';
  for (const [ann, p] of ln.byAnn) if (p.proj !== key) { ln.byAnn.delete(ann); changed = true; }   // the view changed
  for (const p of f.patches) for (const x of p.placed ?? []) { ln.byAnn.set(x.ann, x); changed = true; }
  if (f.pending_new === 0 && ln.byAnn.size) { ln.byAnn.clear(); changed = true; }      // indexed: the real dots are there
  if (changed) updateLiveNew();
}

// the server dropped the patches in between (too many, or a restart): compare the classes and deletions in full
async function resync(into: CurPatch): Promise<void> {
  const P = S.P;
  if (!P) return;
  const r = await apiFetch('api/points', { cache: 'no-store' });
  if (!r.ok) return;
  const np = await r.json() as Points & { deleted?: number[] };
  const n = Math.min(np.cls.length, P.cls.length);
  for (let i = 0; i < n; i++) if (np.cls[i] !== P.cls[i] && into.cls) into.cls[String(i)] = np.cls[i] ?? 0;
}

function setBadge(nNew: number | null): void {
  const host = $('ds-info');
  if (!host) return;
  let b = $('live-badge');
  if (nNew === null) { b?.remove(); return; }
  L.pending = nNew;
  if (!b) {
    // right after the dataset pill: the long workspace pill is the one that runs out of room, never this
    const html = '<span id="live-badge" class="pill live" role="status" title="Label changes made elsewhere show up here as they happen"></span>';
    if (host.firstElementChild) host.firstElementChild.insertAdjacentHTML('afterend', html);
    else host.insertAdjacentHTML('beforeend', html);
    b = $('live-badge');
  }
  if (b) b.innerHTML = `<span class="live-dot" aria-hidden="true">●</span><b>live</b>${nNew ? ` · ${esc(nNew.toLocaleString())} new box${nNew === 1 ? '' : 'es'}` : ''}`;
}
