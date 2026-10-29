// The Data Center's activity strip (under the header): the page's running jobs (label with model, load from S3,
// import from the web, export to S3 / FTP, label import / label all) with stage, a progress bar, rate / ETA and Cancel, polled from
// GET api/jobs (every second while one runs, every 5 s otherwise, never while the page is hidden). Bucket cards
// show a "Labeling… 42%" badge for the job that writes into them (jobBadge). When a job ends the page reloads once.

import { errText, getJSON, postJSON } from '../core/api.js';
import { esc, fmtEta, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import type { Job, JobsPayload } from '../types.js';
import type { Action } from './app.js';
import { hooks } from './state.js';

const MINE = /^(dc-(autolabel|sync|import|reimport|convert|export)|label-(import|predict)):/;
const VERB: Record<string, string> = { autolabel: 'Labeling', sync: 'Loading', import: 'Importing', reimport: 'Updating', convert: 'Converting', export: 'Exporting' };

let active: Job[] = [];
let timer = 0;
let started = false;
let byBucket = new Map<string, Job>();

/** The bucket a job writes into: dc-autolabel:<bid>:<path>, dc-sync:<bid>, dc-import:<bid>, … */
const bucketOfJob = (j: Job): string | null => /^dc-[a-z]+:([^:]+)/.exec(j.key)?.[1] ?? null;
const verb = (j: Job): string => VERB[/^dc-([a-z]+):/.exec(j.key)?.[1] ?? ''] ?? 'Working';
const pct = (j: Job): string => (j.total ? `${Math.round(j.pct ?? 0)}%` : '…');

/** "Labeling… 42%" on the card of a bucket a job is writing into ('' when none). */
export function jobBadge(bid: string): string {
  const j = byBucket.get(bid);
  return j ? `<span class="dc-jobb" data-jb="${esc(bid)}" title="${esc(j.label)}"><span class="spin sm"></span>${esc(verb(j))} <b class="num">${pct(j)}</b></span>` : '';
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

function paint(): void {
  const el = document.getElementById('dc-activity');
  if (!el) return;
  el.classList.toggle('hidden', !active.length);
  el.innerHTML = active.slice(0, 3).map(row).join('') +
    (active.length > 3 ? `<div class="dc-act-more muted">+${active.length - 3} more queued</div>` : '');
  // badges in place: no full repaint for a percentage
  for (const b of document.querySelectorAll<HTMLElement>('.dc-jobb[data-jb]')) {
    const j = byBucket.get(b.dataset['jb'] ?? '');
    const n = b.querySelector('b');
    if (j && n) n.textContent = pct(j);
  }
}

async function poll(): Promise<void> {
  timer = 0;
  if (!document.getElementById('dc-activity')) { started = false; return; }    // left the page
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
  timer = window.setTimeout(() => { void poll(); }, delay);
}

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
