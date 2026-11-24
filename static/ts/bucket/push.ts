// Upload to S3: right-click › Upload to S3 on files / folders of the local copy of an S3 link (a downloaded folder, or
// a bucket loaded into local) sends them back to the same keys (POST …/push, job bk-push: bytes, ETA and Cancel in the
// status bar). Unchanged files are skipped. A file S3 changed since the download is a conflict: skipped and listed in
// the toast, with "Overwrite these N" (push again with overwrite) and "Download the S3 versions" (fetch them).

import { errText } from '../core/api.js';
import { esc } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { actionToast, toast } from '../core/toast.js';
import { watchJobs } from './activity.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { downloadPaths, streamed } from './localcopy.js';
import { bucketOf, curBucket, D, fmtN, fmtSize, hooks, selEntries } from './state.js';
import type { Entry, PushResult } from '../types-bucket.js';
import type { Job } from '../types.js';

const LOCAL = new Set(['fresh', 'outdated']);

/** Whether entries of the open folder can go up to S3 (an S3 link with a local copy holding them). */
export function canPush(es: Entry[]): boolean {
  const b = curBucket();
  if (!b || b.kind !== 's3' || !es.length || D.listing?.offline) return false;
  if (!streamed(b)) return true;                       // loaded into local as a whole
  return !!D.listing?.here || es.every((e) => LOCAL.has(e.local ?? ''));
}

export async function pushPaths(bid: string, paths: string[], overwrite: string[] = []): Promise<void> {
  if (!paths.length) return;
  const b = bucketOf(bid);
  let r: { job_id: string | number | null; result: PushResult | null };
  try { r = await api.push(bid, paths, overwrite); } catch (e) { toast(`Not uploaded: ${errText(e)}`, 'err'); return; }
  watchJobs();                                         // the status bar: bytes, ETA, Cancel
  let res = r.result;
  if (r.job_id != null) {
    try { res = ((await waitJob<Job & { result?: PushResult }>(r.job_id)).result ?? null); } catch (e) { toast(`Upload to S3 failed: ${errText(e)}`, 'err'); return; }
  }
  if (!res) return;
  const where = `s3://${b?.s3_bucket ?? ''}`;
  const parts = [`${fmtN(res.uploaded)} uploaded (${fmtSize(res.bytes)})`, res.unchanged ? `${fmtN(res.unchanged)} unchanged` : '',
    res.failed.length ? `${res.failed.length} failed` : '', res.deleted_here ? `${res.deleted_here} deleted here stay in S3` : ''].filter(Boolean).join(' · ');
  const conf = res.conflicts.map((c) => c.path);
  if (conf.length) {
    actionToast({
      kind: 'warn', timeout: 0,
      title: `${fmtN(conf.length)} not uploaded: changed in S3 since the download`,
      body: `${esc(parts)}.<ul class="bk-plist">${conf.slice(0, 5).map((p) => `<li><code>${esc(p)}</code></li>`).join('')}${conf.length > 5 ? `<li class="faint">and ${conf.length - 5} more</li>` : ''}</ul>`,
      actions: [{ label: `Overwrite these ${fmtN(conf.length)}`, primary: false, run: () => pushPaths(bid, conf, conf) },
        { label: 'Download the S3 versions', primary: false, run: () => downloadPaths(bid, conf) }],
    });
  } else if (res.failed.length) toast(`Upload to ${where}: ${parts}. ${res.failed[0]?.error ?? ''}`, 'err');
  else toast(`${res.cancelled ? 'Stopped' : 'Uploaded'} to ${where}: ${parts}`, res.cancelled ? 'err' : 'ok');
  await hooks.reload();
}

export const PUSH_ACTIONS: Record<string, Action> = {
  push: (el) => {
    const bid = el.dataset['bid'] || D.bid || '';
    const paths = el.dataset['paths'] !== undefined ? el.dataset['paths'].split('\n') : D.sel.size ? selEntries().map((e) => e.path) : [D.path];
    void pushPaths(bid, paths);
  },
};
