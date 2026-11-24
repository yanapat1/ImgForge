// The Visualize header's dataset actions, for a session that reads a bucket folder (local, downloaded or streamed S3):
//  - Export dataset…  its labels (with every fix saved here: they are in the files) in a label format the system
//    writes (YOLO, COCO, VOC, LabelMe, CreateML, CSV, image folders…) into a folder of a bucket: by default the
//    session's own bucket, <folder>/exports/<name>-<format>; a streamed S3 bucket gets it in S3 itself.
//    POST api/bucket/session-folder/<id>/export -> a job (the bar under the header shows its progress and ETA).
//  - Send to Label…   a labeling project on the SAME folder (images in place, the labels imported):
//    POST api/bucket/session-folder/<id>/label.
// GET api/bucket/session-folder/<id> says whether the session reads a bucket folder at all (else no buttons).

import { errText, getJSON, postJSON } from '../core/api.js';
import { zoneOn } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { bucketUrl, labelUrl } from '../core/nav.js';
import { actionToast, toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import type { Job } from '../types.js';

interface FolderInfo {
  bucket: string; bucket_name: string; path: string; session: string | null; format: string; label: string;
  task_type: string | null; formats: Array<{ key: string; label: string }>; dest: { bucket: string; path: string; name: string };
  streamed: boolean; can_export: boolean; can_label: boolean; note: string | null;
}
interface BucketRow { id: string; name: string; kind: string; writable: boolean; local_ready?: boolean }
interface ExportResult { bucket: string; bucket_name: string; path: string; format: string; labels?: number; images_linked?: number; uploaded?: { uri: string; files: number } }

const TASKS: Array<[string, string]> = [['detect', 'Detect (boxes)'], ['segment', 'Segment (polygons)'], ['pose', 'Pose (keypoints)'], ['obb', 'Oriented boxes'], ['classify', 'Classify']];

let INFO: FolderInfo | null = null;
let DID = '';

/** Add the buttons to the header when this session reads a bucket folder (and the user owns it). */
export async function initDatasetActions(did: string, kind: string | undefined, role: string): Promise<void> {
  if (!did || role !== 'owner' || (kind !== 'local' && kind !== 's3') || !zoneOn('bucket')) return;
  try { INFO = await getJSON<FolderInfo>(`api/bucket/session-folder/${encodeURIComponent(did)}`); } catch (e) { return; }   // not in a bucket
  DID = did;
  const host = document.querySelector('header.app-header.viz-only .header-actions');
  if (!host || host.querySelector('#btn-ds-export')) return;
  const btn = (id: string, ico: string, text: string, title: string, dis = ''): string =>
    `<button type="button" id="${id}" class="ghost sm"${dis ? ` disabled title="${esc(dis)}"` : ` title="${esc(title)}"`}>${icon(ico)}<span class="bk-wide-txt">${esc(text)}</span></button>`;
  host.insertAdjacentHTML('afterbegin',
    btn('btn-ds-export', 'download', 'Export dataset', `Write this dataset in another label format into a bucket (${INFO.bucket_name}/${INFO.path})`, INFO.can_export ? '' : INFO.note ?? 'nothing to export') +
    (zoneOn('labeling') ? btn('btn-ds-label', 'edit', 'Send to Label', 'Make a labeling project on the same folder (images in place, the labels imported)', INFO.can_label ? '' : 'this folder cannot be labelled') : ''));
  host.querySelector<HTMLButtonElement>('#btn-ds-export')?.addEventListener('click', () => { void openExport(); });
  host.querySelector<HTMLButtonElement>('#btn-ds-label')?.addEventListener('click', () => { void openToLabel(); });
}

async function openExport(): Promise<void> {
  const info = INFO;
  if (!info) return;
  let buckets: BucketRow[] = [];
  try {
    const r = await getJSON<BucketRow[] | { buckets: BucketRow[] }>('api/bucket/buckets');
    buckets = (Array.isArray(r) ? r : r.buckets).filter((b) => b.writable || b.kind === 's3');
  } catch (e) { /* the session's own bucket is still offered */ }
  if (!buckets.some((b) => b.id === info.dest.bucket)) buckets.unshift({ id: info.dest.bucket, name: info.bucket_name, kind: info.streamed ? 's3' : 'local', writable: !info.streamed });
  const first = info.formats.find((f) => f.key === 'coco') ?? info.formats[0];
  let fmt = first?.key ?? 'yolo';
  let edited = false;
  const destFor = (f: string): string => `${info.dest.path}/${info.dest.name}-${f}`;
  await openDialog({
    title: 'Export dataset', ok: 'Export', wide: true,
    body: `<p class="muted">From <code>${esc(info.bucket_name)}/${esc(info.path)}</code> (${esc(info.label)}). Fixes saved in Visualize are in the files, so they are exported too.</p>` +
      `<div class="fld"><span>Format</span><div class="chips" role="radiogroup" id="ex-fmts">${info.formats.map((f) =>
        `<button type="button" class="chip${f.key === fmt ? ' on' : ''}" role="radio" aria-checked="${f.key === fmt}" data-fmt="${esc(f.key)}">${esc(f.label)}</button>`).join('')}</div></div>` +
      `<label class="fld"><span>Bucket</span><select id="ex-bucket">${buckets.map((b) =>
        `<option value="${esc(b.id)}"${b.id === info.dest.bucket ? ' selected' : ''}>${esc(b.name)}${b.kind === 's3' && !b.local_ready ? ' (S3)' : ''}</option>`).join('')}</select></label>` +
      `<label class="fld"><span>Folder (created; must be new or empty)</span><input type="text" class="txt" id="ex-path" value="${esc(destFor(fmt))}" autocomplete="off" spellcheck="false"></label>` +
      `<p class="hint" id="ex-hint">${info.streamed ? 'A streamed S3 bucket: the dataset is written to S3 at this prefix (images copied inside S3).' : 'Images are linked next to the labels (no extra space on the same disk).'}</p>`,
    bind: (root) => {
      const path = root.querySelector<HTMLInputElement>('#ex-path');
      path?.addEventListener('input', () => { edited = true; });
      root.querySelector('#ex-fmts')?.addEventListener('click', (e) => {
        const b = (e.target as Element).closest<HTMLElement>('[data-fmt]');
        if (!b) return;
        fmt = b.dataset['fmt'] ?? fmt;
        root.querySelectorAll<HTMLElement>('#ex-fmts [data-fmt]').forEach((x) => { const on = x === b; x.classList.toggle('on', on); x.setAttribute('aria-checked', String(on)); });
        if (path && !edited) path.value = destFor(fmt);
      });
      root.querySelector<HTMLSelectElement>('#ex-bucket')?.addEventListener('change', (e) => {
        const b = buckets.find((x) => x.id === (e.target as HTMLSelectElement).value);
        const hint = root.querySelector('#ex-hint');
        if (hint) hint.textContent = b?.kind === 's3' && !b.local_ready ? 'A streamed S3 bucket: written to S3 at this prefix (inside a downloaded folder: to its local copy).' : 'Images are linked next to the labels.';
        if (path && !edited && b && b.id !== info.dest.bucket) path.value = `${info.dest.name}-${fmt}`;
      });
    },
    onOk: async (root) => {
      const to = root.querySelector<HTMLSelectElement>('#ex-bucket')?.value ?? info.dest.bucket;
      const p = (root.querySelector<HTMLInputElement>('#ex-path')?.value ?? '').trim().replace(/^\/+|\/+$/g, '');
      if (!p) return 'Name the folder to write into';
      let r: { job_id: number | string; bucket: string; bucket_name: string; path: string };
      try { r = await postJSON(`api/bucket/session-folder/${encodeURIComponent(DID)}/export`, { format: fmt, to_bucket: to, to_path: p }); } catch (e) { return errText(e); }
      void track(r);
      return null;
    },
  });
}

async function track(r: { job_id: number | string; bucket: string; bucket_name: string; path: string }): Promise<void> {
  toast(`Exporting to ${r.bucket_name}/${r.path}: the bar under the header shows the progress`);
  try {
    const j = await waitJob<Job & { result?: ExportResult }>(r.job_id, { every: 1000 });
    const res = j.result;
    actionToast({
      kind: 'ok', timeout: 15000, title: `Exported ${res?.format ?? ''} to ${r.bucket_name}/${r.path}`,
      body: esc([res?.labels != null ? `${res.labels} label files` : '', res?.images_linked ? `${res.images_linked} images` : '',
        res?.uploaded ? `in ${res.uploaded.uri}` : ''].filter(Boolean).join(' · ')),
      actions: [{ label: 'Open in Bucket', icon: 'folder', run: () => { window.open(bucketUrl(r.bucket, r.path), '_blank', 'noopener'); } }],
    });
  } catch (e) { toast(`Export failed: ${errText(e)}`, 'err'); }
}

async function openToLabel(): Promise<void> {
  const info = INFO;
  if (!info) return;
  const tt = info.task_type ?? 'detect';
  await openDialog({
    title: 'Send to Label', ok: 'Create the Label session',
    body: `<p class="muted">A labeling project on the same folder <code>${esc(info.bucket_name)}/${esc(info.path)}</code>: the images stay where they are, the folder's labels are imported into it.</p>` +
      `<label class="fld"><span>Name</span><input type="text" class="txt" id="tl-name" value="${esc(`${info.session ?? info.dest.name} · label`)}" autocomplete="off"></label>` +
      `<label class="fld"><span>Task</span><select id="tl-task">${TASKS.map(([k, l]) => `<option value="${k}"${k === tt ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>`,
    onOk: async (root) => {
      const name = root.querySelector<HTMLInputElement>('#tl-name')?.value.trim() ?? '';
      const task = root.querySelector<HTMLSelectElement>('#tl-task')?.value ?? tt;
      let r: { id: string; url: string; name: string; job_id?: number | string | null; import_job_id?: number | string | null; import_error?: string };
      try { r = await postJSON(`api/bucket/session-folder/${encodeURIComponent(DID)}/label`, { name, task_type: task }); } catch (e) { return errText(e); }
      void (async () => {
        toast(`Making “${r.name}”: reading the images${r.import_job_id ? ' and labels' : ''}…`);
        try {
          if (r.job_id != null) await waitJob(r.job_id, { every: 1000 });
          if (r.import_job_id != null) await waitJob(r.import_job_id, { every: 1000 });
        } catch (e) { toast(`“${r.name}” was made, but: ${errText(e)}`, 'err'); }
        actionToast({ kind: r.import_error ? 'warn' : 'ok', timeout: 15000, title: `Label session “${r.name}” is ready`,
          ...(r.import_error ? { body: esc(`its labels were not imported: ${r.import_error}`) } : {}),
          actions: [{ label: 'Open it', icon: 'edit', run: () => { window.open(labelUrl(r.id), '_blank', 'noopener'); } }] });
      })();
      return null;
    },
  });
}
