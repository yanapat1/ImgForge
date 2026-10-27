// Getting data in. The Import data dialog has three ways: a new managed bucket filled with dropped / chosen files or a
// zip (unpacked by the server), a link to a local folder (the GET /api/fs/browse folder browser, as in the Settings
// and the labeling wizard), or a link to an S3 bucket + prefix through one of the user's S3 connections (read-only).
// Also the folder's Upload button and files dropped on the folder view (browser.ts): uploadFiles sends them with XHRs
// (byte progress; ≤ 8 files / 16 MB per request, one after the other) into the open folder, the progress shown in
// the folder strip; the answer's `retagged` gets the format-change toast (ops.ts).

import { errText, getJSON, postJSON, XRW } from '../core/api.js';
import { can, cannot } from '../core/auth.js';
import { $, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { showRetag } from './ops.js';
import { bucketOf, D, fmtN, fmtSize, hooks } from './state.js';
import type { BrowseResult, ConnectionsPayload } from '../types-settings.js';
import type { BucketKind, CreateBucketBody, UploadResult } from '../types-datacenter.js';

const BATCH_FILES = 8, BATCH_BYTES = 16 << 20;

/** POST one batch as multipart `files`; onBytes(sent) while it goes. */
function sendBatch(url: string, files: File[], onBytes: (n: number) => void): Promise<UploadResult> {
  return new Promise((ok, fail) => {
    const fd = new FormData();
    for (const f of files) fd.append('files', f, f.webkitRelativePath || f.name);
    const x = new XMLHttpRequest();
    x.open('POST', url);
    x.setRequestHeader('X-Requested-With', XRW);
    x.withCredentials = true;
    x.upload.onprogress = (e) => onBytes(e.loaded);
    x.onload = () => {
      if (x.status >= 200 && x.status < 300) {
        try { ok(JSON.parse(x.responseText) as UploadResult); } catch (e) { ok({ added: files.length, skipped: 0, retagged: null }); }
        return;
      }
      let msg = `${x.status}`;
      try { const j = JSON.parse(x.responseText) as { detail?: unknown }; if (j.detail) msg = String(j.detail); } catch (e) { /* not JSON */ }
      fail(new Error(msg));
    };
    x.onerror = () => fail(new Error('the upload was interrupted'));
    x.send(fd);
  });
}

const count = (v: number | unknown[] | undefined): number => (Array.isArray(v) ? v.length : Number(v) || 0);

/** Upload files into bucket `bid` folder `path`; progress → onProgress(done bytes, total bytes), else the folder strip. */
export async function uploadFiles(bid: string, path: string, files: File[], onProgress?: (done: number, total: number) => void): Promise<UploadResult | null> {
  if (!files.length) return null;
  if (!can('data.manage')) { toast(cannot('upload data'), 'err'); return null; }
  const total = files.reduce((a, f) => a + f.size, 0) || 1;
  const batches: File[][] = [];
  let cur: File[] = [], bytes = 0;
  for (const f of files) {
    if (cur.length && (cur.length >= BATCH_FILES || bytes + f.size > BATCH_BYTES)) { batches.push(cur); cur = []; bytes = 0; }
    cur.push(f);
    bytes += f.size;
  }
  if (cur.length) batches.push(cur);
  const strip = !onProgress;
  const show = (done: number): void => {
    if (onProgress) { onProgress(done, total); return; }
    D.upload = { done, total, files: files.length, err: null };
    hooks.render();
  };
  let done = 0, added = 0, skipped = 0;
  let retagged: UploadResult['retagged'] = null;
  show(0);
  try {
    for (const b of batches) {
      const size = b.reduce((a, f) => a + f.size, 0);
      const r = await sendBatch(api.uploadUrl(bid, path), b, (n) => show(done + n));
      done += size;
      added += count(r.added);
      skipped += count(r.skipped);
      retagged = r.retagged ?? retagged;
      show(done);
    }
  } catch (e) {
    const err = `Upload stopped after ${fmtN(added)} file${added === 1 ? '' : 's'}: ${errText(e)}`;
    if (strip) { D.upload = { done, total, files: files.length, err }; hooks.render(); } else toast(err, 'err');
    await hooks.reload();
    return null;
  }
  if (strip) D.upload = null;
  const where = `${bucketOf(bid)?.name ?? ''}/${path}`;
  if (retagged) showRetag(bid, retagged, null, `Uploaded ${fmtN(added)} files to ${where}`);
  else toast(`Uploaded ${fmtN(added)} file${added === 1 ? '' : 's'} to ${where}${skipped ? ` (${fmtN(skipped)} skipped)` : ''}`, 'ok');
  await hooks.reload();
  return { added, skipped, retagged };
}

// ---------------------------------------------------------------- the Import data dialog

type Way = 'managed' | 'local' | 's3';
interface Imp {
  way: Way; files: File[]; name: string;
  browse: BrowseResult | null; path: string; berr: string | null;
  conns: Array<{ id: string; name: string }> | null; conn: string; s3buckets: string[] | null; s3bucket: string; prefix: string; s3err: string | null;
  progress: string;
}

const isZip = (f: File): boolean => /\.zip$/i.test(f.name);

function wayTab(I: Imp, w: Way, ico: string, label: string): string {
  return `<button type="button" role="tab" class="${I.way === w ? 'on' : ''}" aria-selected="${I.way === w}" data-way="${w}">${icon(ico)}${label}</button>`;
}

function body(I: Imp): string {
  let pane = '';
  if (I.way === 'managed') {
    const size = I.files.reduce((a, f) => a + f.size, 0);
    pane = `<div class="dc-dz" id="dc-dz" tabindex="0" role="button" aria-label="Choose files or a zip">${icon('upload')}<b>Drop images, folders or a .zip here</b>` +
      `<span class="muted">or <button type="button" class="linkish" data-pick="files">choose files</button> · <button type="button" class="linkish" data-pick="dir">a folder</button></span>` +
      (I.files.length ? `<span class="num">${fmtN(I.files.length)} file${I.files.length === 1 ? '' : 's'} · ${fmtSize(size)}${I.files.some(isZip) ? ' · zips are unpacked' : ''}</span>` : '') + '</div>' +
      '<p class="hint">The files are copied into the managed store (config <code>storage.data_root</code>). You can also create the bucket empty and upload later.</p>';
  } else if (I.way === 'local') {
    const b = I.browse;
    pane = `<div class="dc-fsb"><div class="dc-fsb-path">${icon('folder')}<code>${esc(b?.path ?? '…')}</code>` +
      (b?.parent != null ? `<button type="button" class="ghost sm" data-fs="${esc(b.parent)}">${icon('up')}Up</button>` : '') + '</div>' +
      `<div class="dc-fsb-list" role="list">${I.berr ? `<div class="status-line err">${icon('alert')}${esc(I.berr)}</div>` : !b ? '<span class="spin sm"></span>'
        : b.folders.length ? b.folders.map((f) => `<button type="button" class="pk-row" data-fs="${esc(`${b.path.replace(/\/$/, '')}/${f.name}`)}">${icon('folder')}<span class="nm">${esc(f.name)}</span>` +
          `${f.yolo ? '<span class="dc-fb sm" style="--fc:var(--accent)">YOLO</span>' : ''}${f.images ? `<span class="num faint">${fmtN(f.images)} img</span>` : ''}</button>`).join('') : '<span class="muted">No subfolders</span>'}</div></div>` +
      `<p class="hint">Linked folders are used in place (inside <code>storage.allowed_roots</code>); nothing is copied.${b?.images ? ` This folder holds ${fmtN(b.images)} images.` : ''}</p>`;
  } else {
    pane = I.conns === null ? '<span class="spin sm"></span>' : !I.conns.length
      ? `<div class="note info">${icon('plug')}<span>No S3 connection yet: add one in Settings › Connections, then link a bucket here.</span></div>`
      : `<div class="f2"><label class="fld"><span>Connection</span><select id="dc-s3conn">${I.conns.map((c) => `<option value="${esc(c.id)}"${c.id === I.conn ? ' selected' : ''}>${esc(c.name)}</option>`).join('')}</select></label>` +
        `<label class="fld"><span>Bucket</span><select id="dc-s3bucket"${I.s3buckets ? '' : ' disabled'}>${(I.s3buckets ?? []).map((x) => `<option${x === I.s3bucket ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select></label></div>` +
        `<label class="fld"><span>Prefix (optional)</span><input type="text" class="txt" id="dc-s3prefix" value="${esc(I.prefix)}" placeholder="datasets/traffic/" autocomplete="off"></label>` +
        (I.s3err ? `<div class="status-line err">${icon('alert')}${esc(I.s3err)}</div>` : '') +
        '<p class="hint">S3 links are read-only: list, view, detect, tag and send them; copy into a managed bucket to change files.</p>';
  }
  return `<div class="dc-imp"><div class="dc-ways" role="tablist" aria-label="Where the data comes from">${wayTab(I, 'managed', 'upload', 'Upload files')}${wayTab(I, 'local', 'folder', 'Local folder')}${wayTab(I, 's3', 'cloud', 'S3 bucket')}</div>` +
    `<label class="fld"><span>Bucket name</span><input type="text" class="txt" id="dc-imp-name" value="${esc(I.name)}" placeholder="e.g. traffic-cams-2026" autocomplete="off" spellcheck="false"></label>` +
    `<div class="dc-imp-pane">${pane}</div>${I.progress ? `<div class="dc-upline" role="status">${icon('upload')}<span>${esc(I.progress)}</span></div>` : ''}</div>`;
}

async function walk(items: DataTransferItemList): Promise<File[]> {
  const out: File[] = [];
  const entries = [...items].map((i) => i.webkitGetAsEntry?.()).filter((e): e is FileSystemEntry => !!e);
  const read = async (e: FileSystemEntry, pre: string): Promise<void> => {
    if (e.isFile) {
      const f = await new Promise<File>((ok, fail) => (e as FileSystemFileEntry).file(ok, fail));
      out.push(pre ? new File([f], `${pre}${f.name}`, { type: f.type, lastModified: f.lastModified }) : f);
    } else if (e.isDirectory) {
      const rd = (e as FileSystemDirectoryEntry).createReader();
      for (;;) {
        const batch = await new Promise<FileSystemEntry[]>((ok, fail) => rd.readEntries(ok, fail));
        if (!batch.length) break;
        for (const c of batch) await read(c, `${pre}${e.name}/`);
      }
    }
  };
  for (const e of entries) await read(e, '');
  return out;
}

/** The Import data dialog; way 'managed' (upload) by default, 'local' / 's3' for Link source. */
export async function openImport(way: Way = 'managed'): Promise<void> {
  if (!can('data.manage')) { toast(cannot('add data'), 'err'); return; }
  const I: Imp = { way, files: [], name: '', browse: null, path: '', berr: null, conns: null, conn: '', s3buckets: null, s3bucket: '', prefix: '', s3err: null, progress: '' };
  let root: HTMLElement | null = null;
  const paint = (): void => {
    const host = root?.querySelector('.dc-imp');
    if (!host) return;
    const name = root?.querySelector<HTMLInputElement>('#dc-imp-name')?.value;
    if (name !== undefined) I.name = name;
    host.outerHTML = body(I);
  };
  const browse = async (p: string): Promise<void> => {
    I.berr = null;
    try {
      I.browse = await getJSON<BrowseResult>(`api/fs/browse?images=1&path=${encodeURIComponent(p)}`);
      I.path = I.browse.path;
      if (!I.name) I.name = I.path.split('/').filter(Boolean).pop() ?? '';
    } catch (e) { I.berr = errText(e); }
    paint();
  };
  const s3buckets = async (): Promise<void> => {
    I.s3buckets = null; I.s3err = null;
    paint();
    try {
      const r = await postJSON<{ buckets: string[] }>('api/connections/test', { id: I.conn });
      I.s3buckets = r.buckets;
      if (!r.buckets.includes(I.s3bucket)) I.s3bucket = r.buckets[0] ?? '';
    } catch (e) { I.s3err = errText(e); }
    paint();
  };
  const loadConns = async (): Promise<void> => {
    try {
      const r = await getJSON<ConnectionsPayload>('api/connections');
      I.conns = r.connections.filter((c) => c.kind === 's3').map((c) => ({ id: c.id, name: c.name || c.endpoint || c.id }));
      I.conn = I.conns[0]?.id ?? '';
    } catch (e) { I.conns = []; }
    paint();
    if (I.conn) await s3buckets();
  };
  const switchWay = (w: Way): void => {
    I.way = w;
    paint();
    if (w === 'local' && !I.browse) void browse('');
    if (w === 's3' && I.conns === null) void loadConns();
  };
  await openDialog({
    title: way === 'managed' ? 'Import data' : 'Link source', wide: true, ok: 'Create bucket', body: body(I), focus: '#dc-imp-name',
    bind: (r) => {
      root = r;
      if (way === 'local') void browse('');
      if (way === 's3') void loadConns();
      const input = document.createElement('input');
      input.type = 'file';
      input.multiple = true;
      input.addEventListener('change', () => { I.files.push(...(input.files ? [...input.files] : [])); input.value = ''; paint(); });
      r.addEventListener('click', (e) => {
        const t = e.target instanceof Element ? e.target : null;
        const w = t?.closest<HTMLElement>('[data-way]'), fs = t?.closest<HTMLElement>('[data-fs]'), pk = t?.closest<HTMLElement>('[data-pick]');
        if (w) switchWay((w.dataset['way'] as Way | undefined) ?? 'managed');
        else if (fs) void browse(fs.dataset['fs'] ?? '');
        else if (pk) { input.toggleAttribute('webkitdirectory', pk.dataset['pick'] === 'dir'); input.click(); }
        else if (t?.closest('#dc-dz')) { input.removeAttribute('webkitdirectory'); input.click(); }
      });
      r.addEventListener('change', (e) => {
        const t = e.target;
        if (t instanceof HTMLSelectElement && t.id === 'dc-s3conn') { I.conn = t.value; void s3buckets(); }
        if (t instanceof HTMLSelectElement && t.id === 'dc-s3bucket') I.s3bucket = t.value;
      });
      r.addEventListener('input', (e) => { if (e.target instanceof HTMLInputElement && e.target.id === 'dc-s3prefix') I.prefix = e.target.value; });
      r.addEventListener('dragover', (e) => { if (I.way === 'managed') { e.preventDefault(); r.querySelector('#dc-dz')?.classList.add('over'); } });
      r.addEventListener('dragleave', () => r.querySelector('#dc-dz')?.classList.remove('over'));
      r.addEventListener('drop', (e) => {
        if (I.way !== 'managed' || !e.dataTransfer) return;
        e.preventDefault();
        const items = e.dataTransfer.items, plain = [...e.dataTransfer.files];
        void walk(items).then((fs) => { I.files.push(...(fs.length ? fs : plain)); if (!I.name && fs[0]) I.name = (fs[0].name.split('/')[0] ?? '').replace(/\.zip$/i, ''); paint(); });
      });
    },
    onOk: async (r) => {
      const name = r.querySelector<HTMLInputElement>('#dc-imp-name')?.value.trim() ?? '';
      if (!name) return 'Give the bucket a name';
      const kind: BucketKind = I.way;
      const req: CreateBucketBody = { name, kind };
      if (kind === 'local') { if (!I.path) return 'Pick a folder'; req.path = I.path; }
      if (kind === 's3') {
        if (!I.conn || !I.s3bucket) return 'Pick a connection and a bucket';
        req.connection_id = I.conn; req.s3_bucket = I.s3bucket; req.prefix = I.prefix.trim().replace(/^\/+/, '');
      }
      let b;
      try { b = await api.createBucket(req); } catch (e) { return `Could not create the bucket: ${errText(e)}`; }
      if (kind === 'managed' && I.files.length) {
        const res = await uploadFiles(b.id, '', I.files, (d, t) => {
          I.progress = `Uploading ${fmtN(I.files.length)} files… ${Math.round((100 * d) / t)}% (${fmtSize(d)} of ${fmtSize(t)})`;
          paint();
        });
        if (!res) { await hooks.reload(); hooks.navigate(b.id, ''); return null; }
      }
      await hooks.reload();
      hooks.navigate(b.id, '');
      return null;
    },
  });
}

/** The folder's Upload button: the hidden #dc-file input, into the open folder. */
export function bindUpload(): void {
  const input = $<HTMLInputElement>('dc-file');
  if (!input) return;
  input.addEventListener('change', () => {
    const files = input.files ? [...input.files] : [];
    input.value = '';
    if (D.bid && files.length) void uploadFiles(D.bid, D.path, files);
  });
}

export const UPLOAD_ACTIONS: Record<string, Action> = {
  import: () => { void openImport('managed'); },
  link: () => { void openImport('local'); },
  upload: () => { $<HTMLInputElement>('dc-file')?.click(); },
};
