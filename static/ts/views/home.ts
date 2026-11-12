// Home (/, html[data-screen=home]): the DataViz overview, design "A · Three doors". One card per page in the header's
// order (Bucket, Image, Data): its counts, its three most recent items and its main buttons; a card whose zone this
// install leaves out is not drawn. Below them the running jobs. Every card loads on its own (the requests run in
// parallel, a card that fails says why and retries alone) and paints with one innerHTML; the jobs strip polls
// GET api/jobs only while this page is visible (core/jobs.ts pollPause). Clicks go through one delegated listener.

import { getJSON, errText, isStatus } from '../core/api.js';
import { can, hasImage, zoneOn } from '../core/auth.js';
import { $req, esc, icon } from '../core/dom.js';
import { fmtCount, fmtSize, timeAgo, toMs } from '../core/format.js';
import { appHeader } from '../core/header.js';
import { pollPause, setBar } from '../core/jobs.js';
import { bucketUrl, dataUrl, imageUrl, labelUrl, vizUrl } from '../core/nav.js';
import { setTheme, THEME } from '../core/theme.js';
import { openSettings } from '../settings/dialog.js';
import { initPageDock } from './dock-page.js';
import { mountUserMenu } from './usermenu.js';
import type { Job, JobsPayload } from '../types.js';
import type { Bucket } from '../types-bucket.js';
import type { HomeProject, HomeSession } from '../types-image.js';

type DoorId = 'bucket' | 'image' | 'data';
interface Recent { name: string; href: string; kind: string; when: number }
interface DoorView { stats: string[]; recent: Recent[]; empty: string }
interface DataSessionRow { id: string; name: string; updated_at: string }

const RECENT = 3, JOBS_SHOWN = 3;

/** Newest first by `when` (ms); a missing time sorts last. */
const newest = (a: Recent, b: Recent): number => (b.when || 0) - (a.when || 0);
const ms = (t: number | string | null | undefined): number => { const v = toMs(t); return Number.isFinite(v) ? v : 0; };
const plural = (n: number, one: string, many = `${one}s`): string => `${fmtCount(n)} ${n === 1 ? one : many}`;

// ---------------------------------------------------------------- the three doors
interface Door {
  id: DoorId; title: string; icon: string; on: () => boolean; href: () => string; open: string;
  load: () => Promise<DoorView>;
  buttons: () => string;
}

const btn = (href: string, ico: string, text: string, primary = false): string =>
  `<a class="btn sm${primary ? ' primary' : ''}" href="${esc(href)}">${icon(ico)}${esc(text)}</a>`;

const DOORS: Door[] = [
  {
    id: 'bucket', title: 'Bucket', icon: 'db', on: () => zoneOn('bucket'), href: () => bucketUrl(), open: 'Open Bucket',
    async load() {
      // one request: the counts are the list's own (GET api/bucket/summary would list the buckets a second time)
      const got = await getJSON<Bucket[] | { buckets: Bucket[] }>('api/bucket/buckets');
      const list = Array.isArray(got) ? got : got.buckets ?? [];
      const s3 = list.filter((b) => b.kind === 's3').length, linked = list.filter((b) => b.kind === 'local').length;
      const size = list.reduce((n, b) => n + Number(b.stats?.size || 0), 0);
      return {
        stats: [plural(list.length, 'bucket'), `${fmtSize(size)}${s3 + linked ? ` · ${s3} S3 · ${linked} linked` : ''}`],
        recent: list.map((b) => ({ name: b.name, href: bucketUrl(b.id), kind: b.kind === 's3' ? 'S3' : b.kind, when: ms(b.updated_at ?? b.created_at) }))
          .sort(newest).slice(0, RECENT),
        empty: 'No buckets yet. Add a folder, an upload or an S3 link.',
      };
    },
    buttons: () => (can('bucket.manage') ? btn(`${bucketUrl()}&add=1`, 'plus', 'Add data', true) : ''),
  },
  {
    id: 'image', title: 'Image', icon: 'layers', on: hasImage, href: () => imageUrl(), open: 'Open Image',
    async load() {
      const viz = zoneOn('visualizer'), lab = zoneOn('labeling');
      const [ds, lp] = await Promise.all([
        viz ? getJSON<{ datasets: HomeSession[] }>('api/datasets?scope=all').then((r) => r.datasets) : Promise.resolve([]),
        lab ? getJSON<{ projects: HomeProject[] }>('api/label/projects?scope=all').then((r) => r.projects) : Promise.resolve([])]);
      const recent: Recent[] = [
        ...ds.map((d) => ({ name: d.name, href: vizUrl(d.id), kind: 'Visualize', when: ms(d.opened_at ?? d.created) })),
        ...lp.map((p) => ({ name: p.name, href: labelUrl(p.id), kind: 'Label', when: ms(p.opened_at ?? p.updated ?? p.created) }))];
      return {
        stats: [...(viz ? [plural(ds.length, 'visualize session')] : []), ...(lab ? [plural(lp.length, 'label session')] : [])],
        recent: recent.sort(newest).slice(0, RECENT),
        empty: 'No sessions yet. Visualize a dataset or start labeling.',
      };
    },
    buttons: () => (zoneOn('visualizer') && can('dataset.create') ? btn(imageUrl('viz', { new: true }), 'scatter', 'Visualize', true) : '') +
      (zoneOn('labeling') && can('label.create') ? btn(imageUrl('label', { new: true }), 'tag', 'Label') : ''),
  },
  {
    id: 'data', title: 'Data', icon: 'search', on: () => zoneOn('data'), href: () => dataUrl(), open: 'Open Data',
    async load() {
      if (!can('data.use')) return { stats: [], recent: [], empty: 'Your account has no access to Data (ask an admin).' };
      const { sessions } = await getJSON<{ sessions: DataSessionRow[] }>('api/data/sessions');
      return {
        stats: [plural(sessions.length, 'workspace')],
        recent: sessions.map((s) => ({ name: s.name, href: dataUrl(s.id), kind: 'Workspace', when: ms(s.updated_at) }))
          .sort(newest).slice(0, RECENT),
        empty: 'No workspaces yet. Query one of your databases.',
      };
    },
    buttons: () => (can('data.use') ? btn(dataUrl(), 'plus', 'New query', true) : ''),
  },
];

function doorShell(d: Door): string {
  return `<section class="door" id="door-${d.id}" aria-labelledby="door-${d.id}-h" aria-busy="true">` +
    `<header class="door-head"><span class="door-ico" aria-hidden="true">${icon(d.icon)}</span><h2 id="door-${d.id}-h">${esc(d.title)}</h2></header>` +
    `<div class="door-body" id="door-${d.id}-body">${skeleton()}</div>` +
    `<footer class="door-foot"><div class="door-btns">${d.buttons()}</div>` +
    `<a class="door-open" href="${esc(d.href())}">${esc(d.open)}${icon('arrow-r')}</a></footer></section>`;
}

const skeleton = (): string => '<div class="door-stats"><span class="door-skel w60"></span><span class="door-skel w40"></span></div>' +
  `<ul class="door-recent">${'<li><span class="door-skel w80"></span></li>'.repeat(RECENT)}</ul>`;

function doorBody(v: DoorView): string {
  const stats = `<div class="door-stats">${v.stats.map((s, i) => `<span class="${i ? 'muted' : 'door-num'}">${esc(s)}</span>`).join('')}</div>`;
  if (!v.recent.length) return `${stats}<p class="door-empty muted">${esc(v.empty)}</p>`;
  return `${stats}<ul class="door-recent" aria-label="Recent">${v.recent.map((r) =>
    `<li><a href="${esc(r.href)}"><span class="lbl">${esc(r.name)}</span><span class="door-meta faint">${esc(r.kind)}` +
    `${r.when ? ` · ${esc(timeAgo(r.when))}` : ''}</span></a></li>`).join('')}</ul>`;
}

async function loadDoor(d: Door): Promise<void> {
  const body = $req(`door-${d.id}-body`), card = $req(`door-${d.id}`);
  card.setAttribute('aria-busy', 'true');
  try {
    body.innerHTML = doorBody(await d.load());
  } catch (e) {
    if (isStatus(e, 401)) return;                // signed out: api() is taking the page to the sign-in page
    body.innerHTML = `<div class="status-line err">${icon('alert')}<span>Could not load: ${esc(errText(e))}</span>` +
      `<button type="button" class="sm" data-act="retry" data-door="${d.id}">Retry</button></div>`;
  } finally { card.removeAttribute('aria-busy'); }
}

// ---------------------------------------------------------------- running jobs
function jobsHTML(active: Job[]): string {
  const shown = active.slice(0, JOBS_SHOWN);
  return `<span class="hj-title">${icon('refresh')}Running</span>` + shown.map((j) =>
    `<span class="hj-job" title="${esc(j.stage || j.label)}"><span class="lbl">${esc(j.label)}</span>` +
    `<span class="bar"><i data-job="${j.id}"></i></span><span class="num faint">${j.pct == null ? '…' : `${Math.round(j.pct)}%`}</span></span>`).join('') +
    (active.length > shown.length ? `<span class="faint">+${active.length - shown.length} more</span>` : '');
}

async function pollHomeJobs(signal: AbortSignal): Promise<void> {
  const el = $req('home-jobs');
  while (!signal.aborted) {
    let busy = false;
    try {
      const { active } = await getJSON<JobsPayload>('api/jobs');
      busy = active.length > 0;
      el.classList.toggle('hidden', !busy);
      if (busy) {
        el.innerHTML = jobsHTML(active);
        for (const j of active.slice(0, JOBS_SHOWN)) {
          const fill = el.querySelector<HTMLElement>(`[data-job="${j.id}"]`);
          if (fill) setBar(fill, j);
        }
      }
    } catch (e) {
      if (isStatus(e, 401)) return;
    }
    await pollPause(busy ? 1000 : 5000, signal);
  }
}

// ---------------------------------------------------------------- the page
function greeting(): string {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

const ACTIONS: Record<string, (el: HTMLElement) => void> = {
  settings: () => { void openSettings('datasets'); },
  theme: () => { setTheme(THEME.name === 'dark' ? 'light' : 'dark'); },
  retry: (el) => { const d = DOORS.find((x) => x.id === el.dataset['door']); if (d) void loadDoor(d); },
};

export function renderHome(): void {
  document.title = 'DataViz';
  const root = $req('home');
  const doors = DOORS.filter((d) => d.on());
  root.innerHTML = appHeader({ page: 'home', actionsId: 'home-actions' }) +
    '<div class="landing-scroll" role="main" tabindex="-1"><div class="landing-wrap">' +
    `<div class="landing-hello"><h2>${greeting()}.</h2><p class="muted">Pick up where you left off.</p></div>` +
    (doors.length ? `<div class="doors">${doors.map(doorShell).join('')}</div>`
      : '<p class="status-line">No parts of DataViz are installed here.</p>') +
    '<div id="home-jobs" class="home-jobs hidden" role="status" aria-live="polite"></div></div></div>';
  mountUserMenu($req('home-actions'));
  root.addEventListener('click', (e) => {
    const el = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-act]') : null;
    const fn = el && ACTIONS[el.dataset['act'] ?? ''];
    if (el && fn) { e.preventDefault(); fn(el); }
  });
  initPageDock('home');
  for (const d of doors) void loadDoor(d);          // in parallel: each card paints when its own data arrives
  const stop = new AbortController();
  window.addEventListener('pagehide', () => stop.abort(), { once: true });
  void pollHomeJobs(stop.signal);
}
