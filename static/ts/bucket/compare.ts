// Compare with cloud: every file of a downloaded S3 folder (or of a bucket loaded into local) against S3, from the
// server's comparison (dataviz/bucket/cloudiff.py, POST / GET …/compare). Cards count each status (a click
// filters), a search filters by path, and the list is virtual: a fixed row height, only the visible rows are in the
// DOM and pages of 200 come on scroll, so a folder of a million files scrolls as smoothly as one of ten. Select rows
// to get the cloud version back (a streamed link's per-folder copy), or click a path to open its folder.

import { errText } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { downloadPaths, streamed } from './localcopy.js';
import { bucketOf, D, fmtDate, fmtN, fmtSize, hooks, timeAgo } from './state.js';
import type { CompareResult, CompareRow, CompareStatus } from '../types-bucket.js';

const ROW = 30;                     // px: fixed, so the list positions rows without measuring them
const PAGE = 200;
const OVERSCAN = 8;
const ORDER: CompareStatus[] = ['added_local', 'deleted_local', 'edited_local', 'both', 'new_cloud', 'changed_cloud', 'removed_cloud', 'same'];
const LOCAL: CompareStatus[] = ['added_local', 'deleted_local', 'edited_local', 'both'];
const CLOUD: CompareStatus[] = ['new_cloud', 'changed_cloud', 'removed_cloud', 'both'];
const ICON: Record<CompareStatus, string> = {
  added_local: 'plus', deleted_local: 'trash', edited_local: 'edit', both: 'alert',
  new_cloud: 'cloud', changed_cloud: 'refresh', removed_cloud: 'x-c', same: 'check',
};
/** what "Get cloud version" does for a row: the S3 file comes back over the local one (none: S3 has no file) */
const RESTORABLE = new Set<CompareStatus>(['deleted_local', 'edited_local', 'both', 'new_cloud', 'changed_cloud']);
type Filter = 'local' | 'cloud' | 'diff' | 'all' | CompareStatus;
const FILTERS: Array<[Filter, string]> = [['local', 'Your changes'], ['cloud', 'Cloud changes'], ['diff', 'All differences'], ['all', 'Everything']];

function statusesOf(f: Filter): CompareStatus[] {
  if (f === 'local') return LOCAL;
  if (f === 'cloud') return CLOUD;
  if (f === 'diff') return ORDER.filter((s) => s !== 'same');
  if (f === 'all') return [];
  return [f];
}

/** Open the comparison of `path` of bucket `bid` (compared now when it never was). */
export async function openCompare(bid: string, path: string): Promise<void> {
  const b = bucketOf(bid);
  const canGet = streamed(b);
  const S = {
    filter: 'local' as Filter, q: '', matched: 0, gen: 0, sum: null as CompareResult | null, running: false,
    pages: new Map<number, CompareRow[] | 'loading'>(), sel: new Set<string>(), qTimer: 0, frame: 0,
  };
  let root: HTMLElement | null = null;
  const $ = <T extends HTMLElement>(sel: string): T | null => root?.querySelector<T>(sel) ?? null;

  const statusLabel = (s: CompareStatus): string => S.sum?.labels?.[s] ?? s;
  const side = (x: CompareRow['local']): string => (x ? `${fmtSize(x.size)} · ${esc(fmtDate(x.mtime))}` : '<span class="faint">—</span>');

  function rowHTML(r: CompareRow, i: number): string {
    const on = S.sel.has(r.path);
    const ok = canGet && RESTORABLE.has(r.status);
    return `<div class="bk-cmp-row s-${r.status}${on ? ' on' : ''}" role="row" style="top:${i * ROW}px" data-path="${esc(r.path)}">` +
      `<span role="cell"><input type="checkbox" data-cmp="pick" aria-label="Select ${esc(r.path)}"${on ? ' checked' : ''}${ok ? '' : ' disabled'}></span>` +
      `<span role="cell" class="bk-cmp-st s-${r.status}" title="${esc(statusLabel(r.status))}${r.verified === 'match' ? ' (same bytes)' : r.verified === 'differ' ? ' (bytes differ)' : ''}">${icon(ICON[r.status])}<span>${esc(statusLabel(r.status))}</span></span>` +
      `<span role="cell" class="bk-cmp-p mono"><button type="button" class="link" data-cmp="open" title="Open its folder">${esc(r.path)}</button></span>` +
      `<span role="cell" class="bk-cmp-s num">${side(r.local)}</span><span role="cell" class="bk-cmp-s num">${side(r.cloud)}</span></div>`;
  }

  /** draw the rows in view (+ a few), ask for the pages they need */
  function paint(): void {
    S.frame = 0;
    const box = $('.bk-cmp-list'), inner = $('.bk-cmp-inner');
    if (!box || !inner) return;
    inner.style.height = `${S.matched * ROW}px`;
    const first = Math.max(0, Math.floor(box.scrollTop / ROW) - OVERSCAN);
    const last = Math.min(S.matched, Math.ceil((box.scrollTop + box.clientHeight) / ROW) + OVERSCAN);
    let h = '';
    for (let i = first; i < last; i++) {
      const pg = S.pages.get(Math.floor(i / PAGE));
      if (pg === undefined) void loadPage(Math.floor(i / PAGE));
      const r = Array.isArray(pg) ? pg[i % PAGE] : undefined;
      h += r ? rowHTML(r, i) : `<div class="bk-cmp-row ghost" style="top:${i * ROW}px"><span></span><span class="faint">…</span></div>`;
    }
    inner.innerHTML = h;
    const empty = $('.bk-cmp-empty');
    if (empty) empty.hidden = S.matched > 0 || !S.sum?.ready;
  }
  const schedule = (): void => { if (!S.frame) S.frame = requestAnimationFrame(paint); };

  async function loadPage(n: number): Promise<void> {
    if (S.pages.has(n)) return;
    S.pages.set(n, 'loading');
    const gen = S.gen;
    try {
      const r = await api.compareResult(bid, path, statusesOf(S.filter), S.q, n * PAGE, PAGE);
      if (gen !== S.gen) return;                     // the filter changed meanwhile
      S.pages.set(n, r.rows ?? []);
      if (r.matched !== S.matched) { S.matched = r.matched ?? 0; renderHead(); }
      schedule();
    } catch (e) {
      S.pages.delete(n);
      if (gen === S.gen) toast(`Could not read the comparison: ${errText(e)}`, 'err');
    }
  }

  function renderHead(): void {
    const s = S.sum;
    const cards = $('.bk-cmp-cards'), meta = $('.bk-cmp-meta'), tabs = $('.bk-cmp-tabs');
    if (!cards || !meta || !tabs) return;
    const c = s?.counts;
    cards.innerHTML = ORDER.map((k) => `<button type="button" class="bk-cmp-card s-${k}${S.filter === k ? ' on' : ''}" data-cmp="card" data-s="${k}" aria-pressed="${S.filter === k}">` +
      `${icon(ICON[k])}<b class="num">${c ? fmtN(c[k]) : '…'}</b><span>${esc(statusLabel(k))}</span></button>`).join('');
    tabs.innerHTML = FILTERS.map(([f, l]) => `<button type="button" role="tab" class="${S.filter === f ? 'on' : ''}" aria-selected="${S.filter === f}" data-cmp="tab" data-f="${f}">${esc(l)}</button>`).join('');
    meta.innerHTML = S.running
      ? `<span class="spin sm" aria-hidden="true"></span><span>Comparing with S3…</span>`
      : s?.ready ? `<span>${fmtN(s.total)} files · compared ${esc(timeAgo(s.finished_at ?? 0))}${s.verify ? ' · contents verified' : ''}${s.complete === false ? ' · <b>stopped early</b>' : ''}` +
        ` · <b class="num">${fmtN(S.matched)}</b> shown</span>` : '<span class="faint">Not compared yet</span>';
    const sel = $<HTMLButtonElement>('[data-cmp="get"]');
    if (sel) { sel.disabled = !S.sel.size || S.running; sel.innerHTML = `${icon('download')}Get cloud version${S.sel.size ? ` (${fmtN(S.sel.size)})` : ''}`; }
  }

  /** a new filter / search: the pages start over */
  function reset(): void {
    S.gen++;
    S.pages.clear();
    S.matched = 0;
    const box = $('.bk-cmp-list');
    if (box) box.scrollTop = 0;
    void loadPage(0).then(() => { renderHead(); schedule(); });
    renderHead();
  }

  async function load(): Promise<void> {
    const r = await api.compareResult(bid, path, statusesOf(S.filter), S.q, 0, PAGE);
    S.sum = r;
    S.running = !!r.running;
    if (r.ready) {
      S.gen++;
      S.pages = new Map([[0, r.rows ?? []]]);
      S.matched = r.matched ?? 0;
    }
    renderHead();
    schedule();
    return r.ready || r.running ? undefined : run(false);
  }

  async function run(verify: boolean): Promise<void> {
    S.running = true;
    renderHead();
    try {
      const j = await api.compareCloud(bid, path, verify);
      if (j.job_id != null) {
        const done = await waitJob(j.job_id);
        if ((done as { status?: string }).status === 'error') throw new Error((done as { error?: string }).error || 'the comparison failed');
      }
    } catch (e) {
      toast(`Could not compare: ${errText(e)}`, 'err');
    }
    S.running = false;
    if (root?.isConnected) await load().catch((e: unknown) => toast(`Could not read the comparison: ${errText(e)}`, 'err'));
  }

  const body =
    `<p class="hint">Your local copy of <b class="mono">${esc(path || b?.name || 'the bucket')}</b> against S3. A file's time says who changed it: ` +
    'after the download here (yours), or in S3 since. <i>Verify contents</i> also compares the bytes (slower: it reads every file).</p>' +
    '<div class="bk-cmp-cards" role="group" aria-label="Files per status"></div>' +
    '<div class="bk-cmp-bar"><div class="bk-ways bk-cmp-tabs" role="tablist" aria-label="Show"></div><span class="grow"></span>' +
    '<input type="search" class="txt sm" data-cmp="q" placeholder="Filter by path" aria-label="Filter by path"></div>' +
    '<div class="bk-cmp-head" role="row"><span></span><span>Status</span><span>Path</span><span>Local</span><span>Cloud (S3)</span></div>' +
    '<div class="bk-cmp-list" role="table" aria-label="Files compared with S3" tabindex="0"><div class="bk-cmp-inner"></div>' +
    '<p class="bk-cmp-empty faint" hidden>No file here: nothing differs in this view.</p></div>' +
    '<div class="bk-cmp-foot"><span class="bk-cmp-meta" aria-live="polite"></span><span class="grow"></span>' +
    `<label class="check sm"><input type="checkbox" data-cmp="verify"> Verify contents</label>` +
    `<button type="button" class="ghost sm" data-cmp="again">${icon('refresh')}Compare again</button>` +
    (canGet ? `<button type="button" class="primary sm" data-cmp="get" disabled>${icon('download')}Get cloud version</button>` : '') + '</div>';

  await openDialog({
    title: 'Compare with cloud', wide: true, noCancel: true, ok: 'Close', body,
    bind: (r) => {
      root = r;
      r.closest('.ui-dlg')?.classList.add('bk-cmp-dlg');
      const box = r.querySelector<HTMLElement>('.bk-cmp-list');
      box?.addEventListener('scroll', schedule, { passive: true });
      r.addEventListener('input', (e) => {
        const t = e.target as HTMLInputElement;
        if (t.dataset['cmp'] !== 'q') return;
        clearTimeout(S.qTimer);
        S.qTimer = window.setTimeout(() => { S.q = t.value.trim(); reset(); }, 250);
      });
      r.addEventListener('change', (e) => {
        const t = e.target as HTMLInputElement;
        if (t.dataset['cmp'] !== 'pick') return;
        const p = t.closest<HTMLElement>('.bk-cmp-row')?.dataset['path'];
        if (!p) return;
        if (t.checked) S.sel.add(p); else S.sel.delete(p);
        t.closest('.bk-cmp-row')?.classList.toggle('on', t.checked);
        renderHead();
      });
      r.addEventListener('click', (e) => {
        const el = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-cmp]') : null;
        if (!el || (el instanceof HTMLButtonElement && el.disabled)) return;
        switch (el.dataset['cmp']) {
          case 'card': { const s = el.dataset['s'] as CompareStatus; S.filter = S.filter === s ? 'diff' : s; reset(); break; }
          case 'tab': S.filter = (el.dataset['f'] ?? 'local') as Filter; reset(); break;
          case 'again': void run(!!r.querySelector<HTMLInputElement>('[data-cmp="verify"]')?.checked); break;
          case 'get': {
            const paths = [...S.sel];
            S.sel.clear();
            renderHead();
            schedule();
            void downloadPaths(bid, paths).then(() => run(false));
            break;
          }
          case 'open': {
            const p = el.closest<HTMLElement>('.bk-cmp-row')?.dataset['path'] ?? '';
            const dir = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';
            r.closest('.ui-dlg')?.querySelector<HTMLElement>('[data-dlg="ok"]')?.click();
            hooks.navigate(bid, dir, true);
            break;
          }
        }
      });
      void load().catch((e: unknown) => toast(`Could not read the comparison: ${errText(e)}`, 'err'));
    },
    onOk: () => null,
  });
  root = null;
}

export const COMPARE_ACTIONS: Record<string, Action> = {
  'compare-cloud': (el) => { if (D.bid) void openCompare(D.bid, el.dataset['path'] ?? D.path); },
};
