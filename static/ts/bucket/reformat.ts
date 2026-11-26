// Re-format to… (right-click › Export): write a folder again in another label format as a new folder of the bucket
// "<bucket> · formats" (POST api/bucket/reformat, a job). The formats offered are the folder's own (session check
// convert_to); a streamed S3 folder read for the first time is listed first (the check's scan job). Create session's
// Format step starts the same job (startReformat).

import { errText, getJSON, postJSON } from '../core/api.js';
import { esc } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import { watchJobs } from './activity.js';

interface Check { format: string; label?: string; note: string; convert_to: string[]; streamed: boolean;
  scanning?: boolean; job_id?: number | null }

/** Start a re-format job of folder `path` of bucket `bid` to `to` (the toast says where it lands) -> started. */
export async function startReformat(bid: string, path: string, to: string): Promise<boolean> {
  try {
    const x = await postJSON<{ bucket_name: string; path: string }>('api/bucket/reformat', { bucket: bid, path, to });
    toast(`Re-format started: it will be in ${x.bucket_name}/${x.path} (the Bucket page shows the job)`, 'ok');
    return true;
  } catch (e) {
    toast(`Could not re-format: ${errText(e)}`, 'err');
    return false;
  }
}

const check = (bid: string, path: string): Promise<Check> =>
  getJSON<Check>(`api/bucket/buckets/${encodeURIComponent(bid)}/session-check?path=${encodeURIComponent(path)}`);

/** The Re-format dialog of one folder: its format, the formats it can be written as, Re-format. */
export async function openReformat(bid: string, path: string): Promise<void> {
  let c: Check;
  try {
    c = await check(bid, path);
    for (let i = 0; c.scanning && c.job_id != null && i < 3; i++) {
      toast('Listing the folder in S3…');
      await waitJob(c.job_id);
      c = await check(bid, path);
    }
  } catch (e) {
    toast(`Could not read the folder: ${errText(e)}`, 'err');
    return;
  }
  const name = path || 'the bucket';
  const opts = c.convert_to;
  await openDialog({
    title: `Re-format ${name}`, ok: opts.length ? 'Re-format' : 'Close', noCancel: !opts.length,
    body: `<p><b>${esc(c.label ?? c.format)}</b></p>` +
      (opts.length
        ? `<p class="hint">Writes a new folder in the bucket “… · formats”; this folder is not changed${c.streamed ? ' (a streamed folder: labels only, the images stay in S3)' : ''}.</p>` +
          `<div class="seg" role="radiogroup">${opts.map((x, i) => `<label class="check"><input type="radio" name="bk-rf" value="${esc(x)}"${i ? '' : ' checked'}> ${esc(x.toUpperCase())}</label>`).join('')}</div>`
        : `<p class="hint">Nothing to re-format: ${esc(c.note)}</p>`),
    onOk: (root) => {
      const to = root.querySelector<HTMLInputElement>('input[name="bk-rf"]:checked')?.value;
      if (to) void startReformat(bid, path, to).then((ok) => { if (ok) watchJobs(); });   // the Bucket status bar
      return null;
    },
  });
}
