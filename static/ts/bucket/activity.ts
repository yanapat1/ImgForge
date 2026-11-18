// The Bucket page's status bar (at the bottom): one line of small text for the task that matters most (an upload
// that failed, then a running upload or job, then a queued one): what it is, its stage, %, count, rate and time left,
// a thin bar along the top edge and Cancel / Retry. "+N more" opens the list of every task (the full rows, upwards).
// Jobs (label with a model, scan / load from S3, import from the web, export to S3 / FTP, video → frames, label
// import / label all) are polled from GET api/jobs (every second while one runs, every 5 s otherwise, never while the
// page is hidden); uploads from this tab (uploads.ts) are painted in place. With no task, the line shows the page's
// own work (busy.ts: "Listing …", "Loading previews · 18 left") or "Ready". Bucket cards show a "Labeling… 42%"
// badge for the job that writes into them (jobBadge). When a job ends the page reloads once.

import { errText, getJSON, postJSON } from '../core/api.js';
import { esc, fmtEta, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import type { Job, JobsPayload } from '../types.js';
import type { Action } from './app.js';
import { busyHook, busyText } from './busy.js';
import { hooks } from './state.js';
import { stripPart, uploadBadge, uploadLine, uploadRows, upStrip } from './uploads.js';
import type { StatusLine } from './uploads.js';

const MINE = /^(dc-(autolabel|scan|fetch|check|sync|import|reimport|convert|export|frames)|label-(import|predict)):/;
const VERB: Record<string, string> = { autolabel: 'Labeling', scan: 'Scanning', fetch: 'Downloading', check: 'Checking', sync: 'Loading', import: 'Importing', reimport: 'Updating', convert: 'Converting', export: 'Exporting', frames: 'Cutting' };

let active: Job[] = [];
let timer = 0;
let started = false;
let byBucket = new Map<string, Job>();

/** The bucket a job writes into: bk-autolabel:<bid>:<path>, bk-sync:<bid>, bk-import:<bid>, … */
const bucketOfJob = (j: Job): string | null => /^dc-[a-z]+:([^:]+)/.exec(j.key)?.[1] ?? null;
const verb = (j: Job): string => VERB[/^dc-([a-z]+):/.exec(j.key)?.[1] ?? ''] ?? 'Working';
const pct = (j: Job): string => (j.total ? `${Math.round(j.pct ?? 0)}%` : '…');
const LIST_MAX = 50;                             // rows in the open list (the rest: "+N more queued")
let listOpen = false;

/** "Labeling… 42%" on the card of a bucket a job is writing into, else "Uploading 42%" ('' when neither). */
export function jobBadge(bid: string): string {
  const j = byBucket.get(bid);
  if (!j) return uploadBadge(bid);
  return `<span class="bk-jobb" data-jb="${esc(bid)}" title="${esc(j.label)}"><span class="spin sm"></span>${esc(verb(j))} <b class="num">${pct(j)}</b></span>`;
}

function row(j: Job): string {
  const det = !!j.total;
  const w = det ? Math.min(100, j.pct ?? 0) : 30;
  const nums = det ? `${j.done.toLocaleString('en-US')}/${j.total.toLocaleString('en-US')}` : `${j.elapsed}s`;
  return `<div class="bk-act-row" data-jid="${j.id}">` +
    `<b class="bk-act-l" title="${esc(j.label)}">${esc(j.label)}</b>` +
    `<span class="muted bk-act-s">${esc(j.status === 'queued' ? 'queued' : j.stage || j.status)}</span>` +
    `<div class="bar${det ? '' : ' indeterminate'}" role="progressbar" aria-label="${esc(j.label)}"${det ? ` aria-valuenow="${Math.round(w)}"` : ''}><div style="width:${w}%"></div></div>` +
    `<span class="num">${esc(nums)}${j.extra ? ` · ${esc(j.extra)}` : ''}</span><span class="muted num">${esc(fmtEta(j.eta))}</span>` +
    `<button type="button" class="icon-btn ghost sm" data-act="cancel-job" data-jid="${j.id}" aria-label="Cancel ${esc(j.label)}" title="Cancel"${j.cancelled ? ' disabled' : ''}>${icon('x')}</button></div>`;
}

function patchRow(box: HTMLElement, j: Job): void {
  const r = box.querySelector<HTMLElement>(`.bk-act-row[data-jid="${j.id}"]`);
  if (!r) return;
  const det = !!j.total;
  const w = det ? Math.min(100, j.pct ?? 0) : 30;
  const set = (sel: string, text: string): void => { const el = r.querySelector(sel); if (el && el.textContent !== text) el.textContent = text; };
  set('.bk-act-s', j.status === 'queued' ? 'queued' : j.stage || j.status);
  const nums = det ? `${j.done.toLocaleString('en-US')}/${j.total.toLocaleString('en-US')}` : `${j.elapsed}s`;
  set(':scope > span.num:not(.muted)', `${nums}${j.extra ? ` · ${j.extra}` : ''}`);
  set(':scope > span.muted.num', fmtEta(j.eta));
  const fill = r.querySelector<HTMLElement>('.bar > div');
  if (fill) fill.style.width = `${w}%`;
  if (det) r.querySelector('.bar')?.setAttribute('aria-valuenow', String(Math.round(w)));
}

function paint(): void {
  const jobs = stripPart('bk-act-jobs');
  if (!jobs) return;
  const shown = active.slice(0, LIST_MAX);
  const key = shown.map((j) => `${j.id}:${j.total ? 1 : 0}:${j.cancelled ? 1 : 0}`).join() + `|${Math.max(0, active.length - LIST_MAX)}`;
  if (jobs.dataset['k'] !== key) {                 // other jobs (or a kind of bar) than drawn here: one rebuild
    jobs.dataset['k'] = key;
    jobs.innerHTML = shown.map(row).join('') +
      (active.length > LIST_MAX ? `<div class="bk-act-more muted">+${active.length - LIST_MAX} more queued</div>` : '');
  } else {
    for (const j of shown) patchRow(jobs, j);      // the same jobs: numbers and bar in place (hover / focus on Cancel stay)
  }
  // badges in place: no full repaint for a percentage
  for (const b of document.querySelectorAll<HTMLElement>('.bk-jobb[data-jb]')) {
    const j = byBucket.get(b.dataset['jb'] ?? '');
    const n = b.querySelector('b');
    if (j && n) n.textContent = pct(j);
  }
  paintLine();
}

function jobLine(j: Job): StatusLine {
  const det = !!j.total;
  const stage = j.status === 'queued' ? 'queued' : j.stage || j.status;
  const nums = det ? `${Math.round(j.pct ?? 0)}% · ${j.done.toLocaleString('en-US')}/${j.total.toLocaleString('en-US')}` : `${j.elapsed}s`;
  return {
    key: `j${j.id}:${j.cancelled ? 1 : 0}`, text: `${j.label}${stage ? ` · ${stage}` : ''}`, nums: `${nums}${j.extra ? ` · ${j.extra}` : ''}`,
    eta: fmtEta(j.eta), pct: det ? Math.min(100, j.pct ?? 0) : null, err: false, running: j.status !== 'queued',
    btn: `<button type="button" class="icon-btn ghost sm" data-act="cancel-job" data-jid="${j.id}" aria-label="Cancel ${esc(j.label)}" title="Cancel"${j.cancelled ? ' disabled' : ''}>${icon('x')}</button>`,
  };
}

let lineFrame = 0;
/** Repaint the status line on the next frame (uploads and page work call it many times a second: one paint). */
function lineSoon(): void {
  if (!lineFrame) lineFrame = requestAnimationFrame(() => { lineFrame = 0; paintLine(); });
}

/** The status line: the most important task in place (text nodes, bar width; the button only when the task changes). */
function paintLine(): void {
  const st = document.getElementById('bk-status');
  if (!st) return;
  const up = uploadLine(), total = uploadRows() + active.length;
  const job = active.find((j) => j.status !== 'queued') ?? active[0];
  const line: StatusLine | null = up && (up.err || up.running) ? up
    : job && job.status !== 'queued' ? jobLine(job) : up ?? (job ? jobLine(job) : null);
  const work = line ? null : busyText();
  const state = line ? (line.err ? 'err' : 'run') : work ? 'busy' : 'idle';
  const q = <T extends HTMLElement>(sel: string): T | null => st.querySelector<T>(sel);
  const set = (sel: string, text: string): void => { const el = q(sel); if (el && el.textContent !== text) el.textContent = text; };
  if (st.dataset['s'] !== state) {
    st.dataset['s'] = state;
    const ic = q('.bk-st-ic');
    if (ic) ic.innerHTML = state === 'err' ? icon('alert') : state === 'idle' ? '' : '<span class="spin sm"></span>';
  }
  const text = line ? line.text : work ?? 'Ready';
  set('.bk-st-t', text);
  q('.bk-st-t')?.setAttribute('title', text);
  set('.bk-st-n', line?.nums ?? '');
  set('.bk-st-eta', line?.eta ?? '');
  const bar = q('.bk-st-bar');
  if (bar) {
    const det = !!line && line.pct != null;
    bar.classList.toggle('indeterminate', state !== 'idle' && !det);
    const fill = bar.firstElementChild as HTMLElement | null;
    if (fill) fill.style.width = det ? `${(line?.pct ?? 0).toFixed(1)}%` : '';
  }
  const btn = q('.bk-st-btn');
  if (btn && btn.dataset['k'] !== (line?.key ?? '')) { btn.dataset['k'] = line?.key ?? ''; btn.innerHTML = line?.btn ?? ''; }
  const more = q<HTMLButtonElement>('.bk-st-more');
  if (total < 2) listOpen = false;
  if (more) {
    more.classList.toggle('hidden', total < 2);
    set('.bk-st-more', listOpen ? 'Hide the list' : `+${total - 1} more`);
    more.setAttribute('aria-expanded', String(listOpen));
  }
  document.getElementById('bk-st-list')?.classList.toggle('hidden', !listOpen);
  // screen readers: a short note when the task in front changes, not every percentage
  const sr = q('.bk-st-sr'), say = line ? `${total} task${total === 1 ? '' : 's'}: ${line.text}` : '';
  if (sr && sr.dataset['k'] !== (line?.key ?? '')) { sr.dataset['k'] = line?.key ?? ''; sr.textContent = say; }
}

/** The status bar's markup (app.ts puts it under the 3 panes). */
export const STATUS_BAR = '<footer id="bk-status" class="bk-status" data-s="idle">' +
  '<div id="bk-st-list" class="bk-st-list hidden" role="region" aria-label="Running tasks"></div>' +
  '<div class="bk-st-bar" aria-hidden="true"><div></div></div>' +
  '<div class="bk-st-line"><span class="bk-st-ic" aria-hidden="true"></span><span class="bk-st-t">Ready</span>' +
  '<span class="bk-st-n num"></span><span class="bk-st-eta num"></span>' +
  '<button type="button" class="ghost sm bk-st-more hidden" data-act="toggle-status" aria-controls="bk-st-list" aria-expanded="false"></button>' +
  '<span class="bk-st-btn"></span></div>' +
  '<span class="bk-st-sr visually-hidden" aria-live="polite"></span></footer>';

/** Esc or a click outside the bar closes the open list. */
export function bindStatus(): void {
  const close = (): void => { if (listOpen) { listOpen = false; paintLine(); } };
  document.addEventListener('pointerdown', (e) => {
    if (listOpen && e.target instanceof Node && !document.getElementById('bk-status')?.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && listOpen) { e.stopPropagation(); close(); } }, true);
}

let polling = false, again = false;              // one poll loop only: a watchJobs() during a request re-polls after it

async function poll(): Promise<void> {
  timer = 0;
  if (polling) { again = true; return; }
  if (!document.getElementById('bk-status')) { started = false; return; }    // left the page
  polling = true;
  let delay = 5000;
  if (!document.hidden) {
    try {
      const r = await getJSON<JobsPayload>('api/jobs');
      const prev = new Set(active.map((j) => j.id));
      const prevBids = [...byBucket.keys()].sort().join();
      active = r.active.filter((j) => MINE.test(j.key));
      byBucket = new Map(active.flatMap((j) => { const b = bucketOfJob(j); return b ? [[b, j] as const] : []; }));
      const ended = [...prev].filter((id) => !active.some((j) => j.id === id));
      if (ended.length) void hooks.reload();                 // something finished: counts, formats, new files
      else if ([...byBucket.keys()].sort().join() !== prevBids) hooks.render();   // a card gains / loses its badge
      paint();
      if (active.length) delay = 1000;
    } catch { /* server restarting: try again later */ }
  }
  polling = false;
  if (again) { again = false; delay = 0; }
  timer = window.setTimeout(() => { void poll(); }, delay);
}

upStrip.changed = () => { paint(); };
upStrip.tick = lineSoon;
busyHook.changed = lineSoon;

/** Start polling (once), or poll now: call right after a job was started so it shows at once. */
export function watchJobs(): void {
  if (started && timer) { clearTimeout(timer); timer = 0; }
  started = true;
  void poll();
}

export const ACTIVITY_ACTIONS: Record<string, Action> = {
  'toggle-status': () => { listOpen = !listOpen; paintLine(); },
  'cancel-job': (el) => {
    const id = el.dataset['jid'];
    if (!id) return;
    (el as HTMLButtonElement).disabled = true;
    postJSON(`api/jobs/${id}/cancel`).then(() => toast('Cancelling…')).catch((e: unknown) => toast(`Could not cancel: ${errText(e)}`, 'err'));
  },
};
