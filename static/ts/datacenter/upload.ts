// Getting data in. The Import data dialog has three ways: a new managed bucket filled with dropped / chosen files or a
// zip (unpacked by the server), a link to a local folder (the GET /api/fs/browse folder browser, as in the Settings
// and the labeling wizard), or a link to an S3 bucket + prefix through one of the user's S3 connections, streamed (read-only)
// or loaded into local (a pull job; edit.ts trackPull follows it).
// Also the folder's Upload button and files dropped on the folder view (browser.ts). Every upload goes to
// uploads.ts queueUpload: it runs in the background with its progress at the top of the page.

import { errText, getJSON, postJSON } from '../core/api.js';
import { can, cannot } from '../core/auth.js';
import { $, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { watchJobs } from './activity.js';
import { modeCards, trackPull } from './edit.js';
import { openSettings } from '../settings/dialog.js';
import { D, fmtN, fmtSize, hooks } from './state.js';
import { queueUpload } from './uploads.js';
import type { BrowseResult, ConnectionsPayload } from '../types-settings.js';
import type { BucketKind, CreateBucketBody, S3Mode, SourceInfo, WebSource } from '../types-datacenter.js';

// ---------------------------------------------------------------- the Import data dialog

type Way = 'managed' | 'local' | 's3' | 'web';
/** "From the web": the sources (GET api/dc/sources), the picked one, its ref, token, options and the inspect card */
interface Web {
  sources: WebSource[] | null; src: string; ref: string; conn: string; opts: Record<string, string | boolean>;
  info: SourceInfo | null; busy: boolean; err: string | null; extract: boolean; keep: boolean; nested: boolean;
  tok: number; named: boolean;
}
interface Imp {
  way: Way; files: File[]; name: string; web: Web;
  browse: BrowseResult | null; path: string; berr: string | null;
  conns: Array<{ id: string; name: string }> | null; conn: string; s3buckets: string[] | null; s3bucket: string; prefix: string; s3err: string | null;
  mode: S3Mode;
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
  } else if (I.way === 'web') {
    pane = webPane(I.web);
  } else {
    pane = I.conns === null ? '<span class="spin sm"></span>' : !I.conns.length
      ? `<div class="note info">${icon('plug')}<span>No S3 connection yet: add one in Settings › Connections, then link a bucket here.</span></div>`
      : `<div class="f2"><label class="fld"><span>Connection</span><select id="dc-s3conn">${I.conns.map((c) => `<option value="${esc(c.id)}"${c.id === I.conn ? ' selected' : ''}>${esc(c.name)}</option>`).join('')}</select></label>` +
        `<label class="fld"><span>Bucket</span><select id="dc-s3bucket"${I.s3buckets ? '' : ' disabled'}>${(I.s3buckets ?? []).map((x) => `<option${x === I.s3bucket ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select></label></div>` +
        `<label class="fld"><span>Prefix (optional)</span><input type="text" class="txt" id="dc-s3prefix" value="${esc(I.prefix)}" placeholder="datasets/traffic/" autocomplete="off"></label>` +
        (I.s3err ? `<div class="status-line err">${icon('alert')}${esc(I.s3err)}</div>` : '') +
        `<div class="m-lbl">Load mode</div>${modeCards(I.mode)}` +
        `<p class="hint">${I.mode === 'stream' ? 'A streamed link is read-only: list, view, detect, tag and send it. Switch to Load into local later in Edit.'
          : 'The bucket is downloaded into the managed store, then you can change, label and export it. Re-sync from S3 any time (Edit).'}</p>`;
  }
  return `<div class="dc-imp"><div class="dc-ways" role="tablist" aria-label="Where the data comes from">${wayTab(I, 'managed', 'upload', 'Upload files')}${wayTab(I, 'local', 'folder', 'Local folder')}${wayTab(I, 's3', 'cloud', 'S3 bucket')}${wayTab(I, 'web', 'globe', 'From the web')}</div>` +
    `<label class="fld"><span>Bucket name</span><input type="text" class="txt" id="dc-imp-name" value="${esc(I.name)}" placeholder="e.g. traffic-cams-2026" autocomplete="off" spellcheck="false"></label>` +
    `<div class="dc-imp-pane">${pane}</div></div>`;
}

const SRC_ICON: Record<string, string> = { kaggle: 'download', huggingface: 'sparkle', roboflow: 'target', url: 'link', zenodo: 'db', openml: 'grid' };

function webPane(W: Web): string {
  if (W.sources === null) return W.err ? `<div class="status-line err">${icon('alert')}${esc(W.err)}</div>` : '<span class="spin sm"></span>';
  const S = W.sources.find((x) => x.key === W.src);
  const grid = `<div class="dc-srcs" role="radiogroup" aria-label="Source">${W.sources.map((x) =>
    `<button type="button" role="radio" aria-checked="${x.key === W.src}" class="dc-src${x.key === W.src ? ' on' : ''}" data-src="${esc(x.key)}">${icon(SRC_ICON[x.key] ?? 'globe')}<b>${esc(x.label)}</b></button>`).join('')}</div>`;
  if (!S) return grid;
  const conns = S.connections;
  const tokenRow = S.needs ? `<div class="f2"><label class="fld"><span>Token${S.token === 'optional' ? ' <span class="faint">(optional)</span>' : ''}</span>` +
    (conns.length ? `<select id="dc-web-conn">${S.token === 'optional' ? `<option value=""${W.conn ? '' : ' selected'}>None</option>` : ''}${conns.map((c) =>
      `<option value="${esc(c.id)}"${c.id === W.conn ? ' selected' : ''}>${esc(c.name)}</option>`).join('')}</select>`
      : `<span class="muted">${S.token === 'required' ? `${esc(S.label)} needs your token.` : 'None saved.'}</span>`) + '</label>' +
    `<span class="fld"><span>&nbsp;</span><button type="button" class="ghost sm" data-web="add-token">${icon('plus')}Add a ${esc(S.label)} token…</button></span></div>` : '';
  const fields = S.fields.map((f) => {
    const v = W.opts[f.key] ?? (f.default as string | boolean | undefined) ?? '';
    if (f.type === 'select') {
      return `<label class="fld"><span>${esc(f.label)}</span><select data-wopt="${esc(f.key)}">${(f.options ?? []).map(([k, l]) => `<option value="${esc(k)}"${k === v ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>`;
    }
    if (f.type === 'bool') return `<label class="check"><input type="checkbox" data-wopt="${esc(f.key)}"${v ? ' checked' : ''}> ${esc(f.label)}</label>`;
    return `<label class="fld"><span>${esc(f.label)}</span><input type="text" class="txt" data-wopt="${esc(f.key)}" value="${esc(String(v))}" placeholder="${esc(f.placeholder ?? '')}" autocomplete="off" spellcheck="false"></label>`;
  }).join('');
  const i = W.info;
  const card = W.busy ? `<div class="dc-src-card"><span class="spin sm"></span>Looking it up…</div>`
    : W.err ? `<div class="status-line err">${icon('alert')}${esc(W.err)}</div>`
      : i ? `<div class="dc-src-card"><b>${esc(i.title)}</b>` +
        `<span class="muted num">${[i.size ? fmtSize(i.size) : '', i.files ? `${fmtN(i.files)} file${i.files === 1 ? '' : 's'}` : '', i.rows ? `${fmtN(i.rows)} rows` : '',
          i.license ? esc(i.license) : '', i.version != null ? `version ${esc(String(i.version))}` : ''].filter(Boolean).join(' · ')}</span>` +
        (i.sample?.length ? `<span class="faint dc-src-files">${i.sample.slice(0, 6).map(esc).join(' · ')}${(i.files ?? 0) > 6 ? ' …' : ''}</span>` : '') +
        (i.warning ? `<span class="status-line warn">${icon('alert')}${esc(i.warning)}</span>` : '') +
        (i.url ? `<a href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">${icon('link')}Open the page</a>` : '') + '</div>' : '';
  return grid +
    `<label class="fld"><span>${esc(S.label)} dataset</span><input type="text" class="txt mono" id="dc-web-ref" value="${esc(W.ref)}" placeholder="${esc(S.examples[0] ?? '')}" autocomplete="off" spellcheck="false"></label>` +
    `<p class="hint">${esc(S.help)}${S.examples.length > 1 ? ` E.g. <code>${S.examples.slice(1).map(esc).join('</code>, <code>')}</code>` : ''}</p>` +
    tokenRow + (fields ? `<div class="f2">${fields}</div>` : '') + card +
    `<div class="dc-src-opts"><label class="check"><input type="checkbox" data-wflag="extract"${W.extract ? ' checked' : ''}> Unpack archives (zip, tar.gz, …)</label>` +
    `<label class="check"><input type="checkbox" data-wflag="nested"${W.nested ? ' checked' : ''}${W.extract ? '' : ' disabled'}> Also unpack archives inside</label>` +
    `<label class="check"><input type="checkbox" data-wflag="keep"${W.keep ? ' checked' : ''}${W.extract ? '' : ' disabled'}> Keep the downloaded archive</label></div>` +
    '<p class="hint">The download runs in the background (progress at the top of the page); the format is detected when it is done.</p>';
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
  const I: Imp = { way, files: [], name: '', web: { sources: null, src: 'kaggle', ref: '', conn: '', opts: {}, info: null, busy: false, err: null,
    extract: true, keep: false, nested: false, tok: 0, named: false }, browse: null, path: '', berr: null, conns: null, conn: '', s3buckets: null, s3bucket: '', prefix: '', s3err: null, mode: 'stream' };
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
  const W = I.web;
  const pickSource = (key: string): void => {
    W.src = key; W.info = null; W.err = null; W.opts = {};
    const S = W.sources?.find((x) => x.key === key);
    W.conn = S?.connections[0]?.id ?? '';
    paint();
    if (W.ref.trim()) inspectSoon(0);
  };
  const loadSources = async (): Promise<void> => {
    try {
      W.sources = (await api.sources()).sources;
      W.err = null;
      pickSource(W.sources.some((x) => x.key === W.src) ? W.src : W.sources[0]?.key ?? '');
    } catch (e) { W.sources = null; W.err = errText(e); paint(); }
  };
  let inspT: ReturnType<typeof setTimeout> | undefined;
  const inspectSoon = (ms = 400): void => {
    clearTimeout(inspT);
    inspT = setTimeout(() => { void inspect(); }, ms);
  };
  const inspect = async (): Promise<void> => {
    const tok = (W.tok += 1);
    if (!W.ref.trim()) { W.info = null; W.err = null; W.busy = false; paint(); return; }
    W.busy = true; W.err = null;
    paint();
    try {
      const info = await api.inspectSource(W.src, W.ref.trim(), W.conn || null, W.opts);
      if (tok !== W.tok) return;
      W.info = info;
      if (!W.named || !I.name) { I.name = info.name; W.named = false; }
    } catch (e) {
      if (tok !== W.tok) return;
      W.info = null; W.err = errText(e);
    }
    W.busy = false;
    paint();
  };
  const switchWay = (w: Way): void => {
    I.way = w;
    paint();
    if (w === 'local' && !I.browse) void browse('');
    if (w === 's3' && I.conns === null) void loadConns();
    if (w === 'web' && W.sources === null) void loadSources();
  };
  await openDialog({
    title: way === 'managed' || way === 'web' ? 'Import data' : 'Link source', wide: true, ok: way === 'web' ? 'Import' : 'Create bucket', body: body(I), focus: '#dc-imp-name',
    bind: (r) => {
      root = r;
      if (way === 'local') void browse('');
      if (way === 's3') void loadConns();
      if (way === 'web') void loadSources();
      const input = document.createElement('input');
      input.type = 'file';
      input.multiple = true;
      input.addEventListener('change', () => { I.files.push(...(input.files ? [...input.files] : [])); input.value = ''; paint(); });
      r.addEventListener('click', (e) => {
        const t = e.target instanceof Element ? e.target : null;
        const w = t?.closest<HTMLElement>('[data-way]'), fs = t?.closest<HTMLElement>('[data-fs]'), pk = t?.closest<HTMLElement>('[data-pick]');
        const sc = t?.closest<HTMLElement>('[data-src]'), wb = t?.closest<HTMLElement>('[data-web]');
        if (sc) { pickSource(sc.dataset['src'] ?? ''); return; }
        if (wb?.dataset['web'] === 'add-token') {
          // the token form lives in Settings › Connections; the source list is read again when it closes
          void openSettings('connections');
          const box = document.getElementById('settings');
          if (box) {
            const mo = new MutationObserver(() => { if (box.classList.contains('hidden')) { mo.disconnect(); void loadSources(); } });
            mo.observe(box, { attributes: true, attributeFilter: ['class'] });
          }
          return;
        }
        const md = t?.closest<HTMLElement>('[data-mode]');
        if (md) { I.mode = md.dataset['mode'] === 'local' ? 'local' : 'stream'; paint(); }
        else if (w) switchWay((w.dataset['way'] as Way | undefined) ?? 'managed');
        else if (fs) void browse(fs.dataset['fs'] ?? '');
        else if (pk) { input.toggleAttribute('webkitdirectory', pk.dataset['pick'] === 'dir'); input.click(); }
        else if (t?.closest('#dc-dz')) { input.removeAttribute('webkitdirectory'); input.click(); }
      });
      r.addEventListener('change', (e) => {
        const t = e.target;
        if (t instanceof HTMLSelectElement && t.id === 'dc-s3conn') { I.conn = t.value; void s3buckets(); }
        if (t instanceof HTMLSelectElement && t.id === 'dc-s3bucket') I.s3bucket = t.value;
        if (t instanceof HTMLSelectElement && t.id === 'dc-web-conn') { W.conn = t.value; inspectSoon(0); }
        if ((t instanceof HTMLSelectElement || t instanceof HTMLInputElement) && t.dataset['wopt']) {
          W.opts[t.dataset['wopt']] = t instanceof HTMLInputElement && t.type === 'checkbox' ? t.checked : t.value;
          inspectSoon(0);
        }
        if (t instanceof HTMLInputElement && t.dataset['wflag']) {
          const k = t.dataset['wflag'];
          if (k === 'extract') W.extract = t.checked; else if (k === 'nested') W.nested = t.checked; else if (k === 'keep') W.keep = t.checked;
          paint();
        }
      });
      r.addEventListener('input', (e) => {
        const t = e.target;
        if (!(t instanceof HTMLInputElement)) return;
        if (t.id === 'dc-s3prefix') I.prefix = t.value;
        if (t.id === 'dc-web-ref') { W.ref = t.value; inspectSoon(); }
        if (t.id === 'dc-imp-name' && I.way === 'web') W.named = !!t.value.trim();
      });
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
      if (I.way === 'web') {
        if (!W.ref.trim()) return 'Paste what to download';
        const S = W.sources?.find((x) => x.key === W.src);
        if (S?.token === 'required' && !W.conn) return `Add a ${S.label} token first (Settings › Connections)`;
        let res;
        try {
          res = await api.importSource({ source: W.src, ref: W.ref.trim(), ...(name ? { name } : {}), connection_id: W.conn || null,
            opts: { ...W.opts, extract: W.extract, nested: W.nested, keep_archive: W.keep } });
        } catch (e) { return `Could not start: ${errText(e)}`; }
        toast(`Downloading ${W.ref.trim()} from ${S?.label ?? W.src}…`);
        watchJobs();
        await hooks.reload();
        hooks.navigate(res.bucket, '');
        return null;
      }
      if (!name) return 'Give the bucket a name';
      const kind: BucketKind = I.way;
      const req: CreateBucketBody = { name, kind };
      if (kind === 'local') { if (!I.path) return 'Pick a folder'; req.path = I.path; }
      if (kind === 's3') {
        if (!I.conn || !I.s3bucket) return 'Pick a connection and a bucket';
        req.connection_id = I.conn; req.s3_bucket = I.s3bucket; req.prefix = I.prefix.trim().replace(/^\/+/, ''); req.mode = I.mode;
      }
      let b;
      try { b = await api.createBucket(req); } catch (e) { return `Could not create the bucket: ${errText(e)}`; }
      if (kind === 'managed' && I.files.length) {
        queueUpload(b.id, '', I.files);
        toast(`Uploading ${fmtN(I.files.length)} file${I.files.length === 1 ? '' : 's'} in the background: progress at the top of the page`);
      }
      await hooks.reload();
      hooks.navigate(b.id, '');
      if (b.job_id != null) void trackPull(b.job_id, b.name);
      if (b.scan_job_id != null) { toast(`Linked ${b.name} · scanning S3…`); watchJobs(); }   // count + Cancel in the strip
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
    if (D.bid && files.length) queueUpload(D.bid, D.path, files);
  });
}

export const UPLOAD_ACTIONS: Record<string, Action> = {
  import: () => { void openImport('managed'); },
  link: () => { void openImport('local'); },
  'import-web': () => { void openImport('web'); },
  upload: () => { $<HTMLInputElement>('dc-file')?.click(); },
};
