// The per-folder local copy of a streamed S3 link: Download to local (the selection), Update an outdated folder
// (new + changed files; "also remove files S3 no longer has" is opt-in), Check for updates, Remove local copy, and
// the badge every entry of such a bucket carries (localBadge): cloud = stream only, hdd = downloaded and the same as
// S3, amber = S3 changed it, new = only in S3 so far, gone = only here (S3 removed it). The copy is read-only: to
// edit, load the whole bucket into local (Edit › Load mode).

import { errText } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { watchJobs } from './activity.js';
import { bucketOf, D, fmtN, fmtSize, hooks, timeAgo } from './state.js';
import type { Bucket, Entry, LocalRow, LocalState } from '../types-datacenter.js';

/** An S3 link that streams (its local copy, if any, holds only the folders picked). */
export const streamed = (b: Bucket | undefined | null): boolean => !!b && b.kind === 's3' && !b.local_ready;

const META: Record<LocalState | 'stream', [string, string, string]> = {
  stream: ['cloud', 'stream', 'In S3 only: streamed when opened'],
  fresh: ['hdd', 'local', 'Downloaded: the same as S3'],
  outdated: ['refresh', 'outdated', 'Downloaded, but S3 changed it: Update to get the new version'],
  new: ['download', 'new', 'New in S3: not downloaded yet (Update)'],
  gone: ['x-c', 'not in S3', 'Only in the local copy: S3 no longer has it'],
  fetching: ['download', 'downloading', 'Downloading…'],
  error: ['alert', 'failed', 'The last download failed: Update to try again'],
  partial: ['hdd', 'part local', 'Some folders inside are downloaded'],
};

/** The badge of one entry of a streamed bucket ('' elsewhere). */
export function localBadge(e: Entry): string {
  if (!streamed(bucketOf(D.bid ?? ''))) return '';
  const st = e.local ?? 'stream';
  const [ico, label, title] = META[st];
  const spin = st === 'fetching' ? '<span class="spin sm" aria-hidden="true"></span>' : icon(ico);
  return `<span class="dc-loc ${st}" title="${esc(title)}" aria-label="${esc(title)}">${spin}<span class="t">${esc(label)}</span></span>`;
}

/** "12 new · 3 changed · 2 not in S3" of a downloaded folder's last check. */
export function diffText(r: LocalRow): string {
  const d = r.diff;
  if (!d) return '';
  if (d.error) return d.error;
  return [d.new ? `${fmtN(d.new)} new` : '', d.changed ? `${fmtN(d.changed)} changed` : '', d.removed ? `${fmtN(d.removed)} not in S3` : '']
    .filter(Boolean).join(' · ');
}

async function track(jobId: number | null, what: string): Promise<void> {
  watchJobs();                                      // progress + Cancel in the activity strip
  await hooks.reload();                             // the badges turn to "downloading" at once
  if (jobId == null) return;
  try {
    const j = await waitJob(jobId);
    const r = (j as { result?: { downloaded?: number; skipped?: number; removed?: number; cancelled?: boolean } }).result;
    if (r?.cancelled) toast(`${what}: cancelled`);
    else toast(`${what}: done${r?.downloaded != null ? ` · ${fmtN(r.downloaded)} downloaded` : ''}${r?.skipped ? ` · ${fmtN(r.skipped)} unchanged` : ''}` +
      `${r?.removed ? ` · ${fmtN(r.removed)} removed` : ''}`, 'ok');
  } catch (e) {
    toast(`${what} failed: ${errText(e)}`, 'err');
  }
  await hooks.reload();
}

/** Download `paths` (folders or files) of the open streamed bucket. */
export async function downloadPaths(bid: string, paths: string[], mirror = false): Promise<void> {
  if (!paths.length) return;
  try {
    const r = await api.fetchLocal(bid, paths, mirror);
    toast(`Downloading ${r.paths?.length === 1 ? (r.paths[0] || 'the bucket') : `${fmtN(r.paths?.length ?? paths.length)} items`} to local…`);
    await track(r.job_id, 'Download');
  } catch (e) { toast(`Could not download: ${errText(e)}`, 'err'); }
}

/** The Update dialog of an outdated downloaded folder: new + changed files, optionally drop what S3 lost. */
export async function openUpdate(bid: string, row: LocalRow): Promise<void> {
  const d = row.diff;
  const removed = d?.removed ?? 0;
  const name = row.path || 'the whole bucket';
  await openDialog({
    title: `Update ${name} from S3`, ok: 'Update',
    body: `<p>${d && !d.error ? `S3 changed since the download${row.checked_at ? ` (checked ${esc(timeAgo(row.checked_at))})` : ''}: <b>${esc(diffText(row) || 'nothing')}</b>.` : 'Downloads what S3 has now.'}</p>` +
      '<p class="hint">New and changed files are downloaded; unchanged ones are skipped.</p>' +
      (removed ? `<label class="check"><input type="checkbox" id="dc-upd-mirror"> Also remove the ${fmtN(removed)} local file${removed === 1 ? '' : 's'} S3 no longer has</label>` : ''),
    onOk: async (root) => {
      const mirror = !!root.querySelector<HTMLInputElement>('#dc-upd-mirror')?.checked;
      void downloadPaths(bid, [row.path], mirror);
      return null;
    },
  });
}

/** Remove the local copy of the selected downloaded folders (they stream again). */
export async function dropPaths(bid: string, paths: string[]): Promise<void> {
  const ok = await openDialog({
    title: 'Remove the local copy', ok: 'Remove', okClass: 'danger-btn',
    body: `<p>Delete the downloaded copy of ${paths.length === 1 ? `<b>${esc(paths[0] || 'the bucket')}</b>` : `${fmtN(paths.length)} items`}? ` +
      'S3 is not touched: they stream from S3 again.</p>',
    onOk: async () => {
      try { await api.dropLocal(bid, paths); } catch (e) { return errText(e); }
      return null;
    },
  });
  if (ok) { toast('Local copy removed', 'ok'); await hooks.reload(); }
}

export async function checkNow(bid: string): Promise<void> {
  try {
    const r = await api.checkLocal(bid);
    toast('Checking the downloaded folders against S3…');
    await track(r.job_id, 'Check');
  } catch (e) { toast(`Could not check: ${errText(e)}`, 'err'); }
}

/** The strip lines of a streamed bucket: offline banner, still listing, this folder's local copy. */
export function localStrip(): string {
  const L = D.listing, bid = D.bid ?? '';
  if (!L || !streamed(bucketOf(bid))) return '';
  const out: string[] = [];
  if (L.offline) {
    out.push(`<div class="dc-banner" role="status">${icon('warn-c')}<span><b>S3 is unreachable.</b> Showing only the downloaded folders` +
      `${L.local_folders ? ` (${fmtN(L.local_folders)})` : ''}; the rest comes back when S3 answers.</span><span class="grow"></span>` +
      `<button type="button" class="sm" data-act="retry-s3">${icon('refresh')}Retry S3</button></div>`);
  } else if (L.partial) {
    out.push(`<div class="dc-upline" role="status"><span class="spin sm" aria-hidden="true"></span><span>Listing from S3… <b class="num">${fmtN(L.loaded ?? L.total)}</b> so far</span></div>`);
  }
  const h = L.here;
  if (h) {
    const old = h.state === 'outdated' || h.state === 'error';
    const when = h.fetched_at ? `downloaded ${esc(timeAgo(h.fetched_at))}` : 'downloading';
    out.push(`<div class="dc-strip-row dc-localrow${old ? ' old' : ''}"><span class="lbl">${icon(old ? 'refresh' : 'hdd')}<b>${old ? 'S3 changed this folder' : 'Local copy'}</b></span>` +
      `<span class="muted">${old && diffText(h) ? `${esc(diffText(h))} · ` : ''}${when}${h.bytes ? ` · ${fmtSize(h.bytes)}` : ''}${h.path !== D.path ? ` · in ${esc(h.path || 'the whole bucket')}` : ''}</span><span class="grow"></span>` +
      (L.offline ? '' : `<button type="button" class="ghost sm" data-act="check-local">${icon('refresh')}Check for updates</button>` +
        `<button type="button" class="${old ? 'primary' : 'ghost'} sm" data-act="update-local">${icon('download')}Update</button>`) + '</div>');
  }
  return out.join('');
}

export const LOCAL_ACTIONS: Record<string, Action> = {
  'dl-local': () => { if (D.bid) void downloadPaths(D.bid, [...D.sel]); },
  'drop-local': () => { if (D.bid) void dropPaths(D.bid, [...D.sel]); },
  'update-local': () => { const h = D.listing?.here; if (D.bid && h) void openUpdate(D.bid, h); },
  'check-local': () => { if (D.bid) void checkNow(D.bid); },
};
