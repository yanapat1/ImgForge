// Background jobs: the progress bar under the header (polled), waiting on one job, the plot's wait overlay.

import type { Job, JobsPayload } from '../types.js';
import { getJSON } from './api.js';
import { $req, fmtEta, sleep } from './dom.js';
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

let prevActive = 0;

export async function pollJobs(): Promise<void> {
  let delay = 2500;
  try {
    const { jobs, active } = await getJSON<JobsPayload>('api/jobs');
    renderJobsBar(jobs, active);
    if (active.length) delay = 600;
    if (prevActive && !active.length) void refreshStatus();   // something finished: update ✓ marks
    prevActive = active.length;
  } catch (e) { /* server restarting */ }
  setTimeout(pollJobs, delay);
}

function renderJobsBar(jobs: Job[], active: Job[]): void {
  const bar = $req('jobs-bar');
  const err = jobs.filter((j) => j.status === 'error').pop();
  if (active.length) {
    const j = active.find((x) => x.status === 'running') || active[0];
    if (!j) return;
    bar.classList.remove('hidden', 'error');
    $req('job-label').textContent = j.label;
    $req('job-stage').textContent = j.stage;
    setBar($req('job-fill'), j);
    $req('job-pct').textContent = (j.total ? `${j.done}/${j.total} (${Math.round(j.pct ?? 0)}%)` : `${j.elapsed}s`) + (j.extra ? ` · ${j.extra}` : '');
    $req('job-eta').textContent = fmtEta(j.eta);
    $req('job-queued').textContent = active.length > 1 ? `+${active.length - 1} queued` : '';
  } else if (err && err.id > lastErrorShown) {
    bar.classList.remove('hidden');
    bar.classList.add('error');
    $req('job-label').textContent = `${err.label} failed`;
    $req('job-stage').textContent = err.error;
    $req('job-pct').textContent = $req('job-eta').textContent = $req('job-queued').textContent = '';
    setTimeout(() => { lastErrorShown = Math.max(lastErrorShown, err.id); bar.classList.add('hidden'); }, 12000);
  } else if (!bar.classList.contains('error')) {
    bar.classList.add('hidden');
  }
}

/** Poll job `jid` every 500 ms until done (resolves to the job) or error (throws). */
export async function waitJob(jid: number | string, onProgress?: (j: Job) => void): Promise<Job> {
  for (;;) {
    const j = await getJSON<Job>(`api/jobs/${jid}`);
    if (onProgress) onProgress(j);
    if (j.status === 'done') return j;
    if (j.status === 'error') throw new Error(`${j.label}: ${j.error}`);
    await sleep(500);
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
