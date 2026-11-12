// Uploads in the background: queueUpload returns at once and the page keeps working while the files go up. Tasks
// run one after another; inside one, LANES requests are in flight together (small files in batches of ≤ 8 to
// /upload, big files in chunks to /upload/part, the chunks of one file in parallel). No request is bigger than the
// chunk size (8 MB, halved down to 256 KB when a proxy such as JupyterHub's cuts or refuses one, then one lane
// fewer; the browser keeps the size that worked). A big file's upload id comes from bucket, folder, name, size and
// mtime, so uploading it again after a cut or a reload asks the server what it holds and sends only the gaps.
// Each task is a row at the top of the page (the activity strip, activity.ts): files, MB/s, bytes, time left and the
// finish clock, Cancel; an error keeps the row with Retry. Rows are updated in place (one frame per change), never
// the whole page, so nothing else repaints while bytes go up. Leaving the page asks first (the tab sends the bytes).

import { errText, postXHR, XRW } from '../core/api.js';
import type { XhrAnswer } from '../core/api.js';
import { can, cannot } from '../core/auth.js';
import { esc, fmtEta, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import * as api from './api.js';
import { showRetag } from './ops.js';
import { bucketOf, fmtN, fmtSize, hooks } from './state.js';
import type { UploadResult } from '../types-bucket.js';

const BATCH_FILES = 8, CHUNK_MAX = 8 << 20, CHUNK_MIN = 256 << 10, TRIES = 4, LANES = 3, CHUNK_KEY = 'bucket.upload.chunk';
const CUT = 'the connection was cut (a proxy in front of the app may limit the request size)';
const RATE_WINDOW = 10_000;

/** A failed request: `status` 0 when the connection was cut, -1 when it was cancelled. */
class UpErr extends Error {
  constructor(msg: string, readonly status: number) { super(msg); }
}

let chunk = ((): number => {
  try { const v = Number(localStorage.getItem(CHUNK_KEY)); return v >= CHUNK_MIN && v <= CHUNK_MAX ? v : CHUNK_MAX; } catch (e) { return CHUNK_MAX; }
})();
let lanes = LANES;

/** A smaller request size after a cut; at the smallest, one lane fewer (the proxy may be limiting connections). */
function shrink(): boolean {
  if (chunk <= CHUNK_MIN) { lanes = Math.max(1, lanes - 1); return false; }
  chunk = Math.max(CHUNK_MIN, chunk >> 1);
  try { localStorage.setItem(CHUNK_KEY, String(chunk)); } catch (e) { /* private window: this page only */ }
  return true;
}

const sleep = (ms: number): Promise<void> => new Promise((ok) => setTimeout(ok, ms));

/** After a failed request of `len` bytes: whether to send it again (a smaller chunk after a cut / 413, a wait after a
 * gateway error). Parallel requests cut together shrink the size once: one already smaller than `len` is just re-split. */
async function again(e: unknown, tries: number, len = 0): Promise<boolean> {
  const st = e instanceof UpErr ? e.status : -2;
  if ((st === 0 || st === 413) && len > chunk) return true;
  if ((st === 0 || st === 413) && shrink()) return true;
  if ((st === 0 || st === 413 || (st >= 502 && st <= 504)) && tries < TRIES) { await sleep(1000 * 2 ** tries); return true; }
  return false;
}

interface Big { f: File; id: string; name: string; ended: boolean }
type Item = { kind: 'batch'; files: File[]; bytes: number } | { kind: 'chunk'; big: Big; off: number; end: number };

interface Task {
  id: number; bid: string; path: string; files: File[]; bytes: number;
  done: number; inflight: Map<XMLHttpRequest, number>; xhrs: Set<XMLHttpRequest>;
  added: number; skipped: number; filesDone: number; retagged: UploadResult['retagged'];
  status: 'queued' | 'running' | 'error'; err: string | null; samples: Array<[number, number]>; stop: boolean;
}

const tasks: Task[] = [];
let ids = 0, running = false, frame = 0;
/** activity.ts sets it: repaint the strip (shown / hidden) when a row comes or goes */
export const upStrip = { changed: (): void => {} };

export const hasUploads = (): boolean => tasks.some((t) => t.status !== 'error');
/** rows in the strip (running, queued or failed uploads) */
export const uploadRows = (): number => tasks.length;
const sent = (t: Task): number => t.done + [...t.inflight.values()].reduce((a, n) => a + n, 0);

/** POST `body` (a multipart batch or a raw chunk) for task `t` -> the parsed JSON answer. */
async function send(t: Task, url: string, body: FormData | Blob): Promise<unknown> {
  let r: XhrAnswer, mine: XMLHttpRequest | null = null;      // this request: its bytes sent count in t.inflight
  try {
    r = await postXHR(url, body, {
      onProgress: (n) => { if (mine && t.inflight.has(mine)) t.inflight.set(mine, n); schedule(); },
      track: (x, live) => {
        if (live) { mine = x; t.xhrs.add(x); t.inflight.set(x, 0); } else { t.xhrs.delete(x); t.inflight.delete(x); schedule(); }
      },
    });
  } catch (e) {
    throw e instanceof Error && e.message === 'abort' ? new UpErr('cancelled', -1) : new UpErr(CUT, 0);
  }
  const j = r.json as { detail?: unknown } | null;
  if (r.status >= 200 && r.status < 300) return j;
  throw new UpErr(j?.detail ? String(j.detail) : r.status === 413 ? 'the request was too large for a proxy in front of the app' : `${r.status}`, r.status);
}

async function status(bid: string, path: string, name: string, id: string, total: number): Promise<{ ranges?: number[][]; done?: boolean } | null> {
  const r = await fetch(api.uploadPartUrl(bid, path, name, id, 0, total), { headers: { 'X-Requested-With': XRW }, credentials: 'same-origin' });
  if (!r.ok) throw new UpErr(r.status === 413 ? CUT : `${r.status}`, r.status);
  return (await r.json()) as { ranges?: number[][]; done?: boolean };
}

const nameOf = (f: File): string => f.webkitRelativePath || f.name;

/** 32 hex characters naming one file's upload: the same file into the same folder gets the same id. */
async function fileId(t: Task, f: File): Promise<string> {
  const s = `${t.bid}|${t.path}|${nameOf(f)}|${f.size}|${f.lastModified}`;
  try {
    const h = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)));
    return [...h.slice(0, 16)].map((b) => b.toString(16).padStart(2, '0')).join('');
  } catch (e) {                                             // no crypto.subtle (plain http): FNV-1a, four seeds
    return [0x811c9dc5, 0x01000193, 0x9e3779b9, 0x85ebca6b].map((seed) => {
      let h = seed >>> 0;
      for (let i = 0; i < s.length; i += 1) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193) >>> 0;
      return h.toString(16).padStart(8, '0');
    }).join('');
  }
}

/** Upload `files` into bucket `bid` folder `path` in the background (progress at the top of the page). */
export function queueUpload(bid: string, path: string, files: File[]): void {
  if (!files.length) return;
  if (!can('bucket.manage')) { toast(cannot('upload data'), 'err'); return; }
  tasks.push({ id: (ids += 1), bid, path, files, bytes: files.reduce((a, f) => a + f.size, 0) || 1, done: 0, inflight: new Map(), xhrs: new Set(),
    added: 0, skipped: 0, filesDone: 0, retagged: null, status: 'queued', err: null, samples: [], stop: false });
  addEventListener('beforeunload', onUnload);
  upStrip.changed();
  schedule();
  void pump();
}

function onUnload(e: BeforeUnloadEvent): void {
  if (!hasUploads()) return;
  e.preventDefault();
  e.returnValue = '';                                       // older browsers need it to ask
}

async function pump(): Promise<void> {
  if (running) return;
  running = true;
  try {
    for (let t = tasks.find((x) => x.status === 'queued'); t; t = tasks.find((x) => x.status === 'queued')) await run(t);
  } finally {
    running = false;
    if (!hasUploads()) removeEventListener('beforeunload', onUnload);
  }
}

async function run(t: Task): Promise<void> {
  t.status = 'running';
  lanes = LANES;
  const queue = [...t.files];
  const items: Item[] = [];
  const took = (r: Partial<UploadResult> | null, n: number): void => {
    const c = (v: number | unknown[] | undefined): number => (Array.isArray(v) ? v.length : Number(v) || 0);
    t.added += c(r?.added ?? n);
    t.skipped += c(r?.skipped);
    t.retagged = r?.retagged ?? t.retagged;
  };
  /** A big file: what the server already holds (a resume), then chunks over the gaps. */
  const plan = async (f: File): Promise<void> => {
    const big: Big = { f, id: await fileId(t, f), name: nameOf(f), ended: false };
    let st = null;
    for (let tries = 0; ; tries += 1) {
      try { st = await status(t.bid, t.path, big.name, big.id, f.size); break; } catch (e) { if (!(await again(e, tries))) throw e; }
    }
    if (st?.done) { took(st as Partial<UploadResult>, 0); t.done += f.size; t.filesDone += 1; return; }
    let at = 0;
    for (const [a = 0, b = 0] of [...(st?.ranges ?? []), [f.size, f.size]]) {
      for (let o = at; o < a; o += chunk) items.push({ kind: 'chunk', big, off: o, end: Math.min(a, o + chunk) });
      t.done += b - a;
      at = b;
    }
  };
  const next = async (): Promise<Item | null> => {
    for (;;) {
      const it = items.shift();
      if (it) return it;
      const f = queue[0];
      if (!f) return null;
      if (f.size > chunk) { queue.shift(); await plan(f); continue; }
      const files: File[] = [];
      let bytes = 0;
      for (let g = queue[0]; g && files.length < BATCH_FILES && g.size <= chunk && !(files.length && bytes + g.size > chunk); g = queue[0]) {
        files.push(g);
        bytes += g.size;
        queue.shift();
      }
      return { kind: 'batch', files, bytes };
    }
  };
  const sendItem = async (it: Item): Promise<void> => {
    for (let tries = 0; ; tries += 1) {
      try {
        if (it.kind === 'batch') {
          const fd = new FormData();
          for (const f of it.files) fd.append('files', f, nameOf(f));
          took(await send(t, api.uploadUrl(t.bid, t.path), fd) as Partial<UploadResult> | null, it.files.length);
          t.done += it.bytes;
          t.filesDone += it.files.length;
        } else {
          const { big, off, end } = it;
          const r = await send(t, api.uploadPartUrl(t.bid, t.path, big.name, big.id, off, big.f.size), big.f.slice(off, end)) as
            Partial<UploadResult> & { done?: boolean } | null;
          t.done += end - off;
          if (r?.done && !big.ended) { big.ended = true; took(r, 0); t.filesDone += 1; }
        }
        return;
      } catch (e) {
        if (t.stop) throw e;
        const len = it.kind === 'batch' ? it.bytes : it.end - it.off;
        if (!(await again(e, tries, len))) throw e;
        if (chunk < len) {                                  // smaller now: split it again
          if (it.kind === 'batch') queue.unshift(...it.files);
          else for (let o = it.end - chunk; o > it.off - chunk; o -= chunk) items.unshift({ ...it, off: Math.max(it.off, o), end: Math.min(it.end, o + chunk) });
          return;
        }
      }
    }
  };
  let active = 0, fatal: unknown = null;
  const lane = async (): Promise<void> => {
    active += 1;
    try {
      while (!t.stop && active <= lanes) {             // after a cut there are fewer lanes: the extra ones end
        const it = await next();
        if (!it) return;
        await sendItem(it);
      }
    } catch (e) {
      fatal ??= e;                                     // the first failure stops the other lanes too
      t.stop = true;
      for (const x of t.xhrs) x.abort();
    } finally { active -= 1; }
  };
  try {
    await Promise.all(Array.from({ length: LANES }, () => lane()));
    while (!t.stop && (queue.length || items.length)) await lane();   // every lane ended early: finish with one
    if (fatal) throw fatal;
    if (t.stop) throw new UpErr('cancelled', -1);
  } catch (e) {
    const cancelled = e instanceof UpErr && e.status === -1;
    if (cancelled) { drop(t); toast(`Upload cancelled after ${fmtN(t.added)} file${t.added === 1 ? '' : 's'}`); }
    else {
      t.status = 'error';
      t.err = `Upload stopped after ${fmtN(t.added)} file${t.added === 1 ? '' : 's'}: ${errText(e)}`;
      rebuild(t);
      upStrip.changed();
    }
    await hooks.reload();
    return;
  }
  drop(t);
  const where = `${bucketOf(t.bid)?.name ?? ''}/${t.path}`;
  if (t.retagged) showRetag(t.bid, t.retagged, null, `Uploaded ${fmtN(t.added)} file${t.added === 1 ? '' : 's'} to ${where}`);
  else toast(`Uploaded ${fmtN(t.added)} file${t.added === 1 ? '' : 's'} to ${where}${t.skipped ? ` (${fmtN(t.skipped)} skipped)` : ''}`, 'ok');
  await hooks.reload();
}

function drop(t: Task): void {
  const i = tasks.indexOf(t);
  if (i >= 0) tasks.splice(i, 1);
  document.querySelector(`[data-upid="${t.id}"]`)?.remove();
  upStrip.changed();
}

// ---------------------------------------------------------------- the rows at the top

/** Bytes per second over the last RATE_WINDOW (null until there is enough to tell). */
function rate(t: Task): number | null {
  const now = performance.now(), b = sent(t);
  const last = t.samples[t.samples.length - 1];
  if (!last || now - last[0] > 250) t.samples.push([now, b]);
  while (t.samples.length > 2 && now - (t.samples[0]?.[0] ?? now) > RATE_WINDOW) t.samples.shift();
  const first = t.samples[0];
  if (!first || now - first[0] < 1500) return null;
  return (b - first[1]) / ((now - first[0]) / 1000);
}

const clock = (s: number): string => new Date(Date.now() + s * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const label = (t: Task): string => `Uploading ${fmtN(t.files.length)} file${t.files.length === 1 ? '' : 's'} to ${bucketOf(t.bid)?.name ?? 'bucket'}${t.path ? `/${t.path}` : ''}`;

function rowHtml(t: Task): string {
  const btns = t.status === 'error'
    ? `<span class="row"><button type="button" class="ghost sm" data-act="retry-upload" data-upid="${t.id}">${icon('refresh')}Retry</button>` +
      `<button type="button" class="icon-btn ghost sm" data-act="dismiss-upload" data-upid="${t.id}" aria-label="Dismiss" title="Dismiss">${icon('x')}</button></span>`
    : `<button type="button" class="icon-btn ghost sm" data-act="cancel-upload" data-upid="${t.id}" aria-label="Cancel the upload" title="Cancel">${icon('x')}</button>`;
  return `<div class="bk-act-row${t.status === 'error' ? ' err' : ''}" data-upid="${t.id}">` +
    `<b class="bk-act-l" title="${esc(label(t))}">${esc(label(t))}</b>` +
    `<span class="muted bk-act-s" data-u="s"></span>` +
    `<div class="bar" role="progressbar" aria-label="${esc(label(t))}"><div data-u="bar" style="width:0%"></div></div>` +
    `<span class="num" data-u="n"></span><span class="muted num" data-u="eta"></span>${btns}</div>`;
}

/** The strip's upload area (created on first use, above the jobs). */
export function stripPart(cls: 'bk-act-up' | 'bk-act-jobs'): HTMLElement | null {
  const el = document.getElementById('bk-activity');
  if (!el) return null;
  let p = el.querySelector<HTMLElement>(`:scope > .${cls}`);
  if (!p) {
    p = document.createElement('div');
    p.className = cls;
    if (cls === 'bk-act-up') el.prepend(p); else el.append(p);
  }
  return p;
}

function rebuild(t: Task): void {
  document.querySelector(`[data-upid="${t.id}"].bk-act-row`)?.remove();
  paint();
}

function schedule(): void {
  if (!frame) frame = requestAnimationFrame(() => { frame = 0; paint(); });
}

/** Update the rows in place: only their text and bar widths change. */
function paint(): void {
  const host = stripPart('bk-act-up');
  if (!host) return;
  for (const t of tasks) {
    let row = host.querySelector<HTMLElement>(`.bk-act-row[data-upid="${t.id}"]`);
    if (!row) { host.insertAdjacentHTML('beforeend', rowHtml(t)); row = host.lastElementChild as HTMLElement; }
    const set = (k: string, v: string): void => { const n = row?.querySelector<HTMLElement>(`[data-u="${k}"]`); if (n && n.textContent !== v) n.textContent = v; };
    const b = Math.min(sent(t), t.bytes), r = t.status === 'running' ? rate(t) : null;
    const pct = Math.min(100, (100 * b) / t.bytes);
    const bar = row.querySelector<HTMLElement>('[data-u="bar"]');
    if (bar) bar.style.width = `${pct.toFixed(1)}%`;
    if (t.status === 'error') { set('s', t.err ?? ''); set('eta', ''); }
    else set('s', t.status === 'queued' ? 'queued' : `${fmtN(t.filesDone)} of ${fmtN(t.files.length)} files${r ? ` · ${fmtSize(r)}/s` : ''}`);
    set('n', `${fmtSize(b)} / ${fmtSize(t.bytes)}`);
    if (t.status !== 'error') {
      const left = r ? (t.bytes - b) / r : null;
      set('eta', left == null ? '' : `${fmtEta(left)} · done ≈ ${clock(left)}`);
    }
    const badge = document.querySelector(`.bk-jobb[data-up="${CSS.escape(t.bid)}"] b`);
    if (badge) badge.textContent = `${Math.round(pct)}%`;
  }
}

/** "Uploading 42%" on the card of a bucket an upload is writing into ('' when none). */
export function uploadBadge(bid: string): string {
  const t = tasks.find((x) => x.bid === bid && x.status !== 'error');
  return t ? `<span class="bk-jobb" data-up="${esc(bid)}" title="${esc(label(t))}"><span class="spin sm"></span>Uploading <b class="num">${Math.round((100 * Math.min(sent(t), t.bytes)) / t.bytes)}%</b></span>` : '';
}

const taskOf = (el: HTMLElement): Task | undefined => tasks.find((t) => String(t.id) === el.dataset['upid']);

export const UPLOADS_ACTIONS: Record<string, (el: HTMLElement) => void> = {
  'cancel-upload': (el) => {
    const t = taskOf(el);
    if (!t) return;
    (el as HTMLButtonElement).disabled = true;
    if (t.status === 'queued') { drop(t); return; }
    t.stop = true;
    for (const x of t.xhrs) x.abort();
  },
  'retry-upload': (el) => {
    const t = taskOf(el);
    if (!t) return;
    drop(t);
    queueUpload(t.bid, t.path, t.files);                   // files already there are skipped, a big one resumes
  },
  'dismiss-upload': (el) => { const t = taskOf(el); if (t) drop(t); },
};
