// Background jobs: the progress bar under the header (polled), waiting on one job, the plot's wait overlay.

import type { Job, JobsPayload } from '../types.js';
import { getJSON, isStatus } from './api.js';
import { $req, fmtEta } from './dom.js';
import { refreshStatus } from '../views/controls.js';

/** Progress bar fill: determinate when the job knows its total, else an indeterminate stripe. */
export function setBar(fillEl: HTMLElement, job: { total?: number | undefined; pct?: number | null | undefined } | null | undefined): void {
  const bar = fillEl.parentElement;
  if (!bar) return;
  const det = job && job.total;
  bar.classList.toggle('indeterminate', !det);
  fillEl.style.width = det ? `${Math.min(100, job.pct || 0)}%` : '30%';
  if (det) bar.setAttribute('aria-valuenow', String(Math.round(job.pct || 0)));
  else bar.removeAttribute('aria-valuenow');
}

let lastErrorShown = 0;
let errShowing = 0, hideTimer = 0;               // the error on the bar now, and its one 12 s hide timer

let prevActive = 0;

export async function pollJobs(): Promise<void> {
  let delay = 2500;
  try {
    const { jobs, active } = await getJSON<JobsPayload>('api/jobs');
    renderJobsBar(jobs, active);
    if (active.length) delay = 600;
    if (prevActive && !active.length) void refreshStatus();   // something finished: update ✓ marks
    prevActive = active.length;
  } catch (e) {
    if (isStatus(e, 401)) return;                // signed out: api() is taking the page to the sign-in page
    /* server restarting */
  }
  await pollPause(delay, undefined, 15000);
  void pollJobs();
}

function renderJobsBar(jobs: Job[], active: Job[]): void {
  const bar = $req('jobs-bar');
  const err = jobs.filter((j) => j.status === 'error').pop();
  if (active.length) {
    const j = active.find((x) => x.status === 'running') || active[0];
    if (!j) return;
    if (errShowing) { lastErrorShown = Math.max(lastErrorShown, errShowing); errShowing = 0; clearTimeout(hideTimer); }
    bar.classList.remove('hidden', 'error');
    $req('job-label').textContent = j.label;
    $req('job-stage').textContent = j.stage;
    setBar($req('job-fill'), j);
    $req('job-pct').textContent = (j.total ? `${j.done}/${j.total} (${Math.round(j.pct ?? 0)}%)` : `${j.elapsed}s`) + (j.extra ? ` · ${j.extra}` : '');
    $req('job-eta').textContent = fmtEta(j.eta);
    $req('job-queued').textContent = active.length > 1 ? `+${active.length - 1} queued` : '';
  } else if (err && err.id > lastErrorShown) {
    if (errShowing === err.id) return;            // already on the bar, its hide timer is running
    errShowing = err.id;
    bar.classList.remove('hidden');
    bar.classList.add('error');
    $req('job-label').textContent = `${err.label} failed`;
    $req('job-stage').textContent = err.error;
    $req('job-pct').textContent = $req('job-eta').textContent = $req('job-queued').textContent = '';
    clearTimeout(hideTimer);
    hideTimer = window.setTimeout(() => {
      lastErrorShown = Math.max(lastErrorShown, err.id);
      if (errShowing !== err.id) return;          // a job started since: the bar is not this error's any more
      errShowing = 0;
      bar.classList.add('hidden');
      bar.classList.remove('error');
    }, 12000);
  } else if (!bar.classList.contains('error')) {
    bar.classList.add('hidden');
  }
}

/** Wait `ms` between two polls. In a hidden tab the wait grows to `hiddenMs` (no polling a page nobody sees), and
 *  it ends early when the tab is shown again or `signal` aborts. Every job poller of the app waits through this. */
export function pollPause(ms: number, signal?: AbortSignal, hiddenMs = Math.max(ms * 6, 5000)): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) { resolve(); return; }
    const end = (): void => {
      clearTimeout(t);
      document.removeEventListener('visibilitychange', onVis);
      signal?.removeEventListener('abort', end);
      resolve();
    };
    const onVis = (): void => { if (!document.hidden) end(); };
    const t = setTimeout(end, document.hidden ? hiddenMs : ms);
    document.addEventListener('visibilitychange', onVis);
    signal?.addEventListener('abort', end);
  });
}

export interface WaitOpts<T> {
  onProgress?: (j: T) => void;
  /** ms between polls while the tab is visible (default 500) */
  every?: number;
  /** stops the wait: it rejects with an AbortError */
  signal?: AbortSignal;
}

/** Poll job `jid` until done (resolves to the job) or error (throws; a 401 too, so it never loops). The old form
 *  waitJob(jid, onProgress) still works. */
export async function waitJob<T extends Job = Job>(jid: number | string, opts: WaitOpts<T> | ((j: T) => void) = {}): Promise<T> {
  const o: WaitOpts<T> = typeof opts === 'function' ? { onProgress: opts } : opts;
  for (;;) {
    if (o.signal?.aborted) throw new DOMException('stopped', 'AbortError');
    const j = await getJSON<T>(`api/jobs/${encodeURIComponent(String(jid))}`);
    if (o.signal?.aborted) throw new DOMException('stopped', 'AbortError');
    o.onProgress?.(j);
    if (j.status === 'done') return j;
    if (j.status === 'error') throw new Error(`${j.label}: ${j.error}`);
    await pollPause(o.every ?? 500, o.signal);
  }
}

/** Show the plot's wait overlay for job `j` (a partial job is fine, e.g. a label before the first poll). */
export function showWait(j: Partial<Job>): void {
  $req('hover-tip').classList.add('hidden');
  $req('plot-wait').classList.remove('hidden');
  $req('wait-label').textContent = j.label || 'Computing…';
  $req('wait-stage').textContent = j.status === 'queued' ? 'queued (another job is running)' : j.stage ?? '';
  setBar($req('wait-fill'), j);
  $req('wait-pct').textContent = j.total ? `${j.done}/${j.total} · ${Math.round(j.pct ?? 0)}% ${fmtEta(j.eta)}` : `${j.elapsed || 0}s`;
}

export const hideWait = (): void => $req('plot-wait').classList.add('hidden');
