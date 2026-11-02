// The Data Center's activity strip (under the header): the page's running jobs (label with model, scan / load from S3,
// import from the web, export to S3 / FTP, video → frames, label import / label all) with stage, a progress bar, rate / ETA and Cancel, polled from
// GET api/jobs (every second while one runs, every 5 s otherwise, never while the page is hidden). Bucket cards
// show a "Labeling… 42%" badge for the job that writes into them (jobBadge). When a job ends the page reloads once.
// Uploads from this tab (uploads.ts) have their rows above the jobs, painted there in place.

import { errText, getJSON, postJSON } from '../core/api.js';
import { esc, fmtEta, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import type { Job, JobsPayload } from '../types.js';
import type { Action } from './app.js';
import { hooks } from './state.js';
import { stripPart, uploadBadge, uploadRows, upStrip } from './uploads.js';

const MINE = /^(dc-(autolabel|scan|fetch|check|sync|import|reimport|convert|export|frames)|label-(import|predict)):/;
const VERB: Record<string, string> = { autolabel: 'Labeling', scan: 'Scanning', fetch: 'Downloading', check: 'Checking', sync: 'Loading', import: 'Importing', reimport: 'Updating', convert: 'Converting', export: 'Exporting', frames: 'Cutting' };

let active: Job[] = [];
let timer = 0;
let started = false;
let byBucket = new Map<string, Job>();

/** The bucket a job writes into: dc-autolabel:<bid>:<path>, dc-sync:<bid>, dc-import:<bid>, … */
const bucketOfJob = (j: Job): string | null => /^dc-[a-z]+:([^:]+)/.exec(j.key)?.[1] ?? null;
const verb = (j: Job): string => VERB[/^dc-([a-z]+):/.exec(j.key)?.[1] ?? ''] ?? 'Working';
const pct = (j: Job): string => (j.total ? `${Math.round(j.pct ?? 0)}%` : '…');

/** "Labeling… 42%" on the card of a bucket a job is writing into, else "Uploading 42%" ('' when neither). */
export function jobBadge(bid: string): string {
  const j = byBucket.get(bid);
  if (!j) return uploadBadge(bid);
  return `<span class="dc-jobb" data-jb="${esc(bid)}" title="${esc(j.label)}"><span class="spin sm"></span>${esc(verb(j))} <b class="num">${pct(j)}</b></span>`;
}

function row(j: Job): string {
  const det = !!j.total;
  const w = det ? Math.min(100, j.pct ?? 0) : 30;
  const nums = det ? `${j.done.toLocaleString('en-US')}/${j.total.toLocaleString('en-US')}` : `${j.elapsed}s`;
  return `<div class="dc-act-row" data-jid="${j.id}">` +
    `<b class="dc-act-l" title="${esc(j.label)}">${esc(j.label)}</b>` +
    `<span class="muted dc-act-s">${esc(j.status === 'queued' ? 'queued' : j.stage || j.status)}</span>` +
    `<div class="bar${det ? '' : ' indeterminate'}" role="progressbar" aria-label="${esc(j.label)}"${det ? ` aria-valuenow="${Math.round(w)}"` : ''}><div style="width:${w}%"></div></div>` +
    `<span class="num">${esc(nums)}${j.extra ? ` · ${esc(j.extra)}` : ''}</span><span class="muted num">${esc(fmtEta(j.eta))}</span>` +
    `<button type="button" class="icon-btn ghost sm" data-act="cancel-job" data-jid="${j.id}" aria-label="Cancel ${esc(j.label)}" title="Cancel"${j.cancelled ? ' disabled' : ''}>${icon('x')}</button></div>`;
}

function patchRow(box: HTMLElement, j: Job): void {
  const r = box.querySelector<HTMLElement>(`.dc-act-row[data-jid="${j.id}"]`);
  if (!r) return;
  const det = !!j.total;
  const w = det ? Math.min(100, j.pct ?? 0) : 30;
  const set = (sel: string, text: string): void => { const el = r.querySelector(sel); if (el && el.textContent !== text) el.textContent = text; };
  set('.dc-act-s', j.status === 'queued' ? 'queued' : j.stage || j.status);
  const nums = det ? `${j.done.toLocaleString('en-US')}/${j.total.toLocaleString('en-US')}` : `${j.elapsed}s`;
  set(':scope > span.num:not(.muted)', `${nums}${j.extra ? ` · ${j.extra}` : ''}`);
  set(':scope > span.muted.num', fmtEta(j.eta));
  const fill = r.querySelector<HTMLElement>('.bar > div');
  if (fill) fill.style.width = `${w}%`;
  if (det) r.querySelector('.bar')?.setAttribute('aria-valuenow', String(Math.round(w)));
}

function paint(): void {
  const el = document.getElementById('dc-activity'), jobs = stripPart('dc-act-jobs');
  if (!el || !jobs) return;
  el.classList.toggle('hidden', !active.length && !uploadRows());
  const shown = active.slice(0, 3);
  const key = shown.map((j) => `${j.id}:${j.total ? 1 : 0}:${j.cancelled ? 1 : 0}`).join() + `|${Math.max(0, active.length - 3)}`;
  if (jobs.dataset['k'] !== key) {                 // other jobs (or a kind of bar) than drawn here: one rebuild
    jobs.dataset['k'] = key;
    jobs.innerHTML = shown.map(row).join('') +
      (active.length > 3 ? `<div class="dc-act-more muted">+${active.length - 3} more queued</div>` : '');
  } else {
    for (const j of shown) patchRow(jobs, j);      // the same jobs: numbers and bar in place (hover / focus on Cancel stay)
  }
  // badges in place: no full repaint for a percentage
  for (const b of document.querySelectorAll<HTMLElement>('.dc-jobb[data-jb]')) {
    const j = byBucket.get(b.dataset['jb'] ?? '');
    const n = b.querySelector('b');
    if (j && n) n.textContent = pct(j);
  }
}

let polling = false, again = false;              // one poll loop only: a watchJobs() during a request re-polls after it

async function poll(): Promise<void> {
  timer = 0;
  if (polling) { again = true; return; }
  if (!document.getElementById('dc-activity')) { started = false; return; }    // left the page
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

/** Start polling (once), or poll now: call right after a job was started so it shows at once. */
export function watchJobs(): void {
  if (started && timer) { clearTimeout(timer); timer = 0; }
  started = true;
  void poll();
}

export const ACTIVITY_ACTIONS: Record<string, Action> = {
  'cancel-job': (el) => {
    const id = el.dataset['jid'];
    if (!id) return;
    (el as HTMLButtonElement).disabled = true;
    postJSON(`api/jobs/${id}/cancel`).then(() => toast('Cancelling…')).catch((e: unknown) => toast(`Could not cancel: ${errText(e)}`, 'err'));
  },
};
