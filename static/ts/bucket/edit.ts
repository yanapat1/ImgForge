// The bucket's Edit dialog (the ✎ on a card, a list row and the details header): its name, its tags, and for an S3
// link the load mode: Stream from S3 (read on every request, read-only) or Load into local (an editable copy under
// the managed store, filled by a pull job). A local copy can be re-synced (S3 replaces files of the same name, files
// only the copy has are kept). Any bucket exports as a zip or to S3 / FTP / SFTP (export.ts). Also Re-detect format,
// Label with model…, Delete / Unlink.

import { errText } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { watchJobs } from './activity.js';
import { openAutolabel } from './autolabel.js';
import { checkNow } from './localcopy.js';
import { downloadZip, openExportDialog } from './export.js';
import { deleteBucket } from './ops.js';
import { openTagEditor } from './tags.js';
import { bucketOf, D, fmtSize, hooks, SOURCE, tagChip, timeAgo } from './state.js';
import type { Bucket, S3Mode } from '../types-bucket.js';

const modeCard = (on: boolean, m: S3Mode, ico: string, title: string, txt: string): string =>
  `<button type="button" class="dest-o${on ? ' on' : ''}" data-mode="${m}" role="radio" aria-checked="${on}">` +
  `<span class="ri" aria-hidden="true"></span><span class="dt"><b>${title}</b><span>${txt}</span></span>${icon(ico)}</button>`;

/** The Stream / Load into local radio cards (also in the Link source dialog). */
export const modeCards = (mode: S3Mode): string =>
  `<div class="dest" role="radiogroup" aria-label="Load mode">` +
  modeCard(mode === 'stream', 'stream', 'cloud', 'Stream from S3', 'Read from S3 when needed. Nothing is stored here; read-only.') +
  modeCard(mode === 'local', 'local', 'hdd', 'Load into local', 'Download into the managed store: fast, editable, exportable. Re-sync any time.') +
  '</div>';

/** Follow a pull job: progress in a toast, then reload the buckets. */
export async function trackPull(jobId: number, name: string): Promise<void> {
  toast(`Loading ${name} from S3…`);
  watchJobs();                                     // progress shows in the status bar
  try {
    const j = await waitJob(jobId);
    const r = (j as { result?: { downloaded?: number; skipped?: number; bytes?: number } }).result;
    toast(`${name} is loaded locally${r?.downloaded != null ? ` · ${r.downloaded.toLocaleString('en-US')} files, ${fmtSize(r.bytes ?? 0)}` : ''}` +
      (r?.skipped ? ` · ${r.skipped.toLocaleString('en-US')} unchanged` : ''), 'ok');
  } catch (e) {
    toast(`Loading ${name} failed: ${errText(e)}`, 'err');
  }
}

function s3Sec(b: Bucket, mode: S3Mode): string {
  const pulled = b.synced_at ? `last synced ${esc(timeAgo(b.synced_at))}` : 'not downloaded yet';
  return `<section class="bk-edit-sec"><div class="m-lbl">Load mode</div>${modeCards(mode)}` +
    `<div class="bk-edit-src">${icon('cloud')}<code title="${esc(b.s3_root ?? '')}">${esc(b.s3_root ?? b.root)}</code></div>` +
    (mode === 'local'
      ? `<div class="bk-edit-row"><span class="muted">${b.local_ready ? `Local copy · ${pulled}` : 'The local copy is filled by a download job.'}</span><span class="grow"></span>` +
        `<button type="button" class="ghost sm" data-ed="sync"${b.mode === 'local' ? '' : ' disabled'}>${icon('refresh')}Re-sync from S3</button></div>` +
        '<p class="hint">Re-sync downloads what changed in S3 (unchanged files are skipped) and replaces those files; files and folders only the local copy has are kept.</p>'
      : `<p class="hint">${b.local_ready || b.synced_at ? 'Switching to stream keeps the downloaded copy on disk; loading again only refreshes it.' : 'Switch to Load into local to change files here.'}</p>` +
        (b.local_folders
          ? `<div class="bk-edit-row"><span class="muted">${icon('hdd')}${b.local_folders} folder${b.local_folders === 1 ? '' : 's'} downloaded (read-only copy)${b.local_outdated ? ` · <b>${b.local_outdated} outdated</b>` : ''}</span><span class="grow"></span>` +
            `<button type="button" class="ghost sm" data-ed="check">${icon('refresh')}Check for updates</button></div>`
          : '<p class="hint">Or pick folders in the bucket and choose Download to local: they stay readable while S3 is unreachable.</p>')) +
    '</section>';
}

function body(b: Bucket, name: string, mode: S3Mode): string {
  const user = b.tags.filter((t) => !t.auto);
  return `<div class="bk-edit">` +
    `<label class="fld"><span>Name</span><input type="text" class="txt" id="bk-ed-name" value="${esc(name)}" autocomplete="off" spellcheck="false"></label>` +
    `<section class="bk-edit-sec"><div class="m-lbl">Tags</div><div class="tags-wrap">${user.map((t) => tagChip(t.tag, false, 'on')).join('') || '<span class="muted">no tags</span>'}` +
    `<button type="button" class="bk-tag add" data-ed="tags">${icon('plus')}${user.length ? 'Edit' : 'Add tag'}</button></div></section>` +
    (b.kind === 's3' ? s3Sec(b, mode)
      : `<section class="bk-edit-sec"><div class="m-lbl">Source</div><div class="bk-edit-src box">${icon(SOURCE[b.kind]?.[0] ?? 'folder')}<code title="${esc(b.root)}">${esc(b.root)}</code></div></section>`) +
    `<section class="bk-edit-sec"><div class="m-lbl">Actions</div><div class="bk-edit-acts">${ACTS.map(actTile).join('')}</div>` +
    dangerRow(b) + '</section></div>';
}

/** [data-ed, icon, title, hint] of the Actions tiles */
const ACTS: Array<[string, string, string, string]> = [
  ['export', 'download', 'Export (zip)', 'Download every file'],
  ['export-to', 'upload', 'Export to…', 'S3 · RustFS, FTP, SFTP'],
  ['redetect', 'refresh', 'Re-detect format', 'Check the layout again'],
  ['autolabel', 'robot', 'Label with model…', 'Auto-label every image'],
];
const actTile = ([ed, ico, title, hint]: [string, string, string, string]): string =>
  `<button type="button" class="bk-edit-act" data-ed="${ed}"><span class="ai">${icon(ico)}</span><span class="at"><b>${title}</b><span>${hint}</span></span></button>`;

/** Delete (a managed bucket) / Unlink (a linked source): apart from the other actions, in red */
function dangerRow(b: Bucket): string {
  const managed = b.kind === 'managed';
  return `<div class="bk-edit-danger"><span class="at"><b>${managed ? 'Delete this bucket' : 'Unlink this source'}</b>` +
    `<span>${managed ? 'Removes the bucket and its files from the managed store.' : 'Removes the link only: the source itself is not touched.'}</span></span>` +
    `<button type="button" class="sm danger-ghost" data-ed="delete">${icon('trash')}${managed ? 'Delete…' : 'Unlink…'}</button></div>`;
}

export async function openEditBucket(bid: string): Promise<void> {
  let b = bucketOf(bid);
  if (!b) return;
  let mode: S3Mode = b.mode ?? 'stream';
  let root: HTMLElement | null = null;
  const repaint = (): void => {
    const host = root?.querySelector('.bk-edit');
    if (!host || !b) return;
    const name = root?.querySelector<HTMLInputElement>('#bk-ed-name')?.value ?? b.name;
    host.outerHTML = body(b, name, mode);
  };
  const refresh = async (): Promise<void> => {
    await hooks.reload();
    b = bucketOf(bid) ?? b;
    repaint();
  };
  await openDialog({
    title: `Edit ${b.name}`, wide: true, ok: 'Save', body: body(b, b.name, mode), focus: '#bk-ed-name',
    bind: (r) => {
      root = r;
      r.addEventListener('click', (e) => {
        const t = e.target instanceof Element ? e.target : null;
        const m = t?.closest<HTMLElement>('[data-mode]');
        if (m) { mode = m.dataset['mode'] === 'local' ? 'local' : 'stream'; repaint(); return; }
        const a = t?.closest<HTMLElement>('[data-ed]');
        if (!a || !b) return;
        const what = a.dataset['ed'];
        if (what === 'export') void downloadZip(bid, '');
        else if (what === 'export-to') void openExportDialog(bid, '');           // replaces this dialog
        else if (what === 'tags') openTagEditor(a, bid, ['']);
        else if (what === 'redetect') {
          api.detect(bid, '', true).then(async (f) => { toast(`Format: ${f.label}`, 'ok'); D.retag.delete(bid); await refresh(); },
            (err: unknown) => toast(`Could not detect: ${errText(err)}`, 'err'));
        } else if (what === 'sync') {
          a.setAttribute('aria-busy', 'true');
          api.syncBucket(bid).then((s) => { if (s.job_id != null) void trackPull(s.job_id, b?.name ?? '').then(refresh); },
            (err: unknown) => { a.removeAttribute('aria-busy'); toast(`Could not re-sync: ${errText(err)}`, 'err'); });
        } else if (what === 'check') void checkNow(bid).then(refresh);
        else if (what === 'autolabel') void openAutolabel(bid, '');        // replaces this dialog
        else if (what === 'delete') void deleteBucket(bid);
      });
    },
    onOk: async (r) => {
      if (!b) return null;
      const name = r.querySelector<HTMLInputElement>('#bk-ed-name')?.value.trim() ?? '';
      if (!name) return 'Enter a name';
      if (/[/\\]/.test(name)) return 'A name cannot contain / or \\';
      const patch: { name?: string; mode?: S3Mode } = {};
      if (name !== b.name) patch.name = name;
      if (b.kind === 's3' && mode !== (b.mode ?? 'stream')) patch.mode = mode;
      if (!patch.name && !patch.mode) return null;
      let res: Bucket;
      try { res = await api.updateBucket(bid, patch); } catch (e) { return errText(e); }
      toast(patch.mode === 'local' ? `Saved · downloading ${res.name} into the managed store` : patch.mode === 'stream' ? `Saved · ${res.name} streams from S3` : 'Saved', 'ok');
      await hooks.reload();
      if (res.job_id != null) void trackPull(res.job_id, res.name);
      return null;
    },
  });
}

export const EDIT_ACTIONS: Record<string, Action> = {
  'edit-bucket': (el) => { void openEditBucket(el.dataset['bid'] || D.focus || D.bid || ''); },
};
