// Export a folder of a bucket: as a zip download (downloadZip: …/export/check first, then an anchor so the zip
// streams), or "Export to…" (openExportDialog, views/dialog.ts): a target card (Download zip / S3 · RustFS / FTP /
// SFTP), a saved connection of that kind, then for S3 a bucket (a combobox over the connection's buckets, with
// Create bucket "x") and a prefix, for FTP / SFTP a path, and Overwrite. The folder layout is kept: a file at
// data/image/im1.png of the bucket lands at <prefix>data/image/im1.png, and the live preview shows that key for the
// folder's first file. POST …/export starts a bk-export:<bid> job (the status bar shows it); a toast reports the
// result. The last target / connection / bucket / prefix / path is a user pref (PREF.exp).

import { errText, getJSON, postJSON } from '../core/api.js';
import { can, cannot, pref, rememberPref } from '../core/auth.js';
import { bindCombo, comboHTML } from '../core/combobox.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import { watchJobs } from './activity.js';
import { bucketOf, D, fmtN, fmtSize, PREF } from './state.js';
import type { ExportRemoteDone, ExportRemoteKind, ExportRemoteTarget, Listing } from '../types-bucket.js';
import type { ConnectionsPayload, ConnItem, ConnTestResult } from '../types-settings.js';

type Kind = 'zip' | ExportRemoteKind;
const KINDS: ReadonlyArray<readonly [Kind, string, string]> = [
  ['zip', 'Download zip', 'download'], ['s3', 'S3 · RustFS', 'cloud'], ['ftp', 'FTP', 'ftp'], ['sftp', 'SFTP', 'server'],
];
/** saved connections per target (an S3 connection saved before kinds existed has none) */
const CONN_KIND: Record<ExportRemoteKind, (c: ConnItem) => boolean> = {
  s3: (c) => !c.kind || c.kind === 's3', ftp: (c) => c.kind === 'ftp', sftp: (c) => c.kind === 'sftp',
};
const isKind = (v: unknown): v is Kind => KINDS.some(([k]) => k === v);

/** the pref: what the last export used */
interface Last { kind?: Kind; connection_id?: string; bucket?: string; prefix?: string; path?: string }

interface ExportState {
  bid: string; path: string; kind: Kind;
  conns: ConnItem[] | null; connection_id: string;
  bucket: string; prefix: string; rpath: string; overwrite: boolean;
  /** the S3 connection's buckets (null while loading), why they could not be read, and "Create bucket" was picked */
  buckets: string[] | null; bErr: string | null; create: boolean;
  /** bucket-relative path of the folder's first file (undefined: looking, null: the folder has no file) */
  sample: string | null | undefined;
}

/** Download folder `path` of a bucket as a zip. GET …/export streams it through an anchor (no blob in memory), so
 *  …/export/check runs first: a failure is a toast with the server's reason, not a broken download. */
export async function downloadZip(bid: string, path: string): Promise<void> {
  toast('Preparing zip…');
  try { await api.exportCheck(bid, path); } catch (e) { toast(`Could not export: ${errText(e)}`, 'err'); return; }
  const a = document.createElement('a');
  a.href = api.exportUrl(bid, path);
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Follow an export job: progress in the status bar, the result in a toast. */
async function trackExport(jobId: number, what: string): Promise<void> {
  toast(`Exporting ${what}…`);
  watchJobs();
  try {
    const j = await waitJob(jobId);
    doneToast((j as { result?: ExportRemoteDone | null }).result ?? null, what);
  } catch (e) {
    toast(`Exporting ${what} failed: ${errText(e)}`, 'err');
  }
}
const doneToast = (r: ExportRemoteDone | null, what: string): void =>
  toast(r ? `Exported ${fmtN(r.files)} files (${fmtSize(r.bytes)}) to ${r.uri}` : `${what} is exported`, 'ok');

/** The first file under a folder (the open listing when it is this folder, else a short ls, a few levels down). */
async function firstFile(bid: string, path: string, depth = 0): Promise<string | null> {
  const L: Listing = D.bid === bid && D.path === path && D.listing && !D.nameQ ? D.listing : await api.ls(bid, path, 0, 50);
  const f = L.entries.find((e) => e.type === 'file');
  if (f) return f.path;
  const d = L.entries.find((e) => e.type === 'dir');
  return d && depth < 4 ? firstFile(bid, d.path, depth + 1) : null;
}

/** 'a/b' → 'a/b/', '/a/' → 'a/', '' → '' (the server joins the same way) */
const asPrefix = (p: string): string => { const t = p.trim().replace(/^\/+/, '').replace(/\/+$/, ''); return t ? `${t}/` : ''; };
const connName = (c: ConnItem | undefined): string => (c ? c.name || c.host || c.endpoint || c.id : '');

export async function openExportDialog(bid: string, path: string): Promise<void> {
  const b = bucketOf(bid);
  if (!b) return;
  const last = (pref(PREF.exp) ?? {}) as Last;
  const manage = can('bucket.manage');
  const X: ExportState = {
    bid, path, kind: manage && isKind(last.kind) ? last.kind : 'zip',
    conns: null, connection_id: last.connection_id ?? '',
    bucket: last.bucket ?? '', prefix: last.prefix ?? '', rpath: last.path ?? '', overwrite: false,
    buckets: null, bErr: null, create: false, sample: undefined,
  };
  const what = `${b.name}${path ? `/${path}` : ''}`;
  const conns = (): ConnItem[] => (X.kind === 'zip' || !X.conns ? [] : X.conns.filter(CONN_KIND[X.kind]));
  const conn = (): ConnItem | undefined => conns().find((c) => c.id === X.connection_id);
  /** a typed bucket that is not on the connection is created (as if "Create bucket" was picked) */
  const willCreate = (): boolean => X.create || (!!X.bucket.trim() && X.buckets !== null && !X.bErr && !X.buckets.includes(X.bucket.trim()));
  // a connection of the target's kind: the remembered one when it fits, else the first
  const fitConn = (): void => { const cs = conns(); if (!cs.some((c) => c.id === X.connection_id)) X.connection_id = cs[0]?.id ?? ''; };

  const dest = (): string => {
    const file = X.sample ?? `${path ? `${path}/` : ''}…`;
    if (X.kind === 's3') return `s3://${X.bucket.trim() || '<bucket>'}/${asPrefix(X.prefix)}${file}`;
    const host = conn()?.host || connName(conn()) || '<connection>';
    return `${X.kind}://${host}/${asPrefix(X.rpath)}${file}`;
  };
  const destHTML = (): string => X.kind === 'zip'
    ? `<p class="hint">${icon('download')}A zip of <b>${esc(what)}</b> downloads in this browser; it streams, so a big folder takes a while.</p>`
    : `<div class="bk-exp-dest"><div class="m-lbl">Destination of the first file</div><code id="bk-exp-uri" title="${esc(dest())}">${esc(dest())}</code>` +
      `<p class="hint">The folder layout is kept under the ${X.kind === 's3' ? 'prefix' : 'path'}.` +
      `<span id="bk-exp-new">${X.kind === 's3' && willCreate() ? ` Bucket <b>${esc(X.bucket.trim())}</b> will be created.` : ''}</span></p></div>`;

  const targetHTML = (): string => {
    if (X.kind === 'zip') return '';
    if (X.conns === null) return '<span class="spin sm"></span>';
    const cs = conns();
    const label = KINDS.find(([k]) => k === X.kind)?.[1] ?? X.kind;
    if (!cs.length) return `<div class="note info">${icon('plug')}<span>No ${esc(label)} connection yet. <button type="button" class="lbx-link" data-ex="conns">Add a connection in Settings</button>, then export here.</span></div>`;
    const sel = `<label class="fld"><span>Connection</span><select id="bk-exp-conn">${cs.map((c) =>
      `<option value="${esc(c.id)}"${c.id === X.connection_id ? ' selected' : ''}>${esc(connName(c))}</option>`).join('')}</select></label>`;
    if (X.kind === 's3') {
      return `<div class="bk-exp-f">${sel}<div class="fld"><span>Bucket</span>` +
        `${comboHTML('bk-exp-bucket', X.bucket, { placeholder: 'Pick or type a bucket', mono: true, attrs: 'data-noenter="1"' })}</div></div>` +
        `<label class="fld mono"><span>Prefix (optional)</span><input type="text" class="txt mono" id="bk-exp-prefix" value="${esc(X.prefix)}" placeholder="dev/v1/new folder/" autocomplete="off" spellcheck="false"></label>` +
        (X.bErr ? `<div class="status-line err">${icon('alert')}Could not list the buckets: ${esc(X.bErr)}</div>` : '');
    }
    return `<div class="bk-exp-f">${sel}<label class="fld mono"><span>Path</span><input type="text" class="txt mono" id="bk-exp-path" value="${esc(X.rpath)}" placeholder="/exports/my-dataset" autocomplete="off" spellcheck="false"></label></div>`;
  };

  const body = (): string =>
    `<div class="bk-exp"><div class="m-lbl">Target</div><div class="lbx-kinds bk-exp-kinds" role="radiogroup" aria-label="Export target">${KINDS.map(([k, l, ic]) => {
      const off = k !== 'zip' && !manage;
      return `<label class="lbx-kind${X.kind === k ? ' sel' : ''}${off ? ' off' : ''}"${off ? ` title="${esc(cannot('export to a connection'))}"` : ''}>` +
        `<input type="radio" name="bk-exp-kind" value="${k}"${X.kind === k ? ' checked' : ''}${off ? ' disabled' : ''}>${icon(ic)}<span>${esc(l)}</span></label>`;
    }).join('')}</div>` +
    (manage ? '' : `<p class="hint">${icon('lock')}${esc(cannot('export to S3 / FTP'))}.</p>`) +
    `<div class="bk-exp-t">${targetHTML()}</div>` +
    (X.kind === 'zip' ? '' : `<label class="check"><input type="checkbox" id="bk-exp-over"${X.overwrite ? ' checked' : ''}> Overwrite a non-empty target</label>`) +
    destHTML() + '</div>';

  const okText = (): string => (X.kind === 'zip' ? 'Download zip' : 'Export');
  let root: HTMLElement | null = null;
  const bindBucket = (): void => {
    if (!root || X.kind !== 's3') return;
    bindCombo(root, 'bk-exp-bucket', {
      options: () => X.buckets ?? [],
      onPick: (v, created) => { X.bucket = v; X.create = created; preview(); },
      onInput: (v) => { X.bucket = v; X.create = false; preview(); },
      create: (q) => { const t = q.trim(); return t && !(X.buckets ?? []).includes(t) ? `Create bucket “${t}”` : null; },
      empty: () => (X.buckets === null ? 'Loading buckets…' : X.bErr ? 'The buckets could not be listed' : 'No bucket yet: type a name to create one'),
    });
  };
  const preview = (): void => {
    const u = root?.querySelector<HTMLElement>('#bk-exp-uri');
    if (u) { u.textContent = dest(); u.title = dest(); }
    const n = root?.querySelector<HTMLElement>('#bk-exp-new');
    if (n) n.innerHTML = X.kind === 's3' && willCreate() ? ` Bucket <b>${esc(X.bucket.trim())}</b> will be created.` : '';
  };
  const repaint = (): void => {
    const host = root?.querySelector('.bk-exp');
    if (!host || !root) return;
    const focused = document.activeElement instanceof HTMLElement && host.contains(document.activeElement) ? document.activeElement : null;
    const again = focused instanceof HTMLInputElement && focused.name === 'bk-exp-kind' ? `input[name="bk-exp-kind"][value="${X.kind}"]` : focused?.id ? `#${focused.id}` : null;
    host.outerHTML = body();
    bindBucket();
    if (again) root.querySelector<HTMLElement>(again)?.focus();
    const ok = root.closest('.ui-dlg')?.querySelector('[data-dlg="ok"]');
    if (ok) ok.textContent = okText();
  };
  const loadBuckets = async (): Promise<void> => {
    X.buckets = null; X.bErr = null;
    const id = X.connection_id;
    if (X.kind !== 's3' || !id) return;
    try {
      const r = await postJSON<ConnTestResult>('api/connections/test', { id });
      if (id !== X.connection_id) return;                       // another connection was picked meanwhile
      X.buckets = (r.buckets ?? []).map((x) => (typeof x === 'string' ? x : x.name));
    } catch (e) { if (id === X.connection_id) { X.buckets = []; X.bErr = errText(e); } }
    // only the parts that depend on the list: a full repaint would steal the focus from the bucket field
    const err = root?.querySelector('.bk-exp-t .status-line.err');
    if (X.bErr && !err) repaint(); else preview();
  };

  void getJSON<ConnectionsPayload>('api/connections').then((r) => r.connections ?? [], (): ConnItem[] => []).then((cs) => {
    X.conns = cs; fitConn(); repaint(); void loadBuckets();
  });
  void firstFile(bid, path).then((p) => { X.sample = p; }, () => { X.sample = null; }).then(preview);

  await openDialog({
    title: `Export ${what}`, wide: true, ok: okText(), body: body(),
    bind: (r) => {
      root = r;
      bindBucket();
      r.addEventListener('change', (e) => {
        const t = e.target;
        if (t instanceof HTMLInputElement && t.name === 'bk-exp-kind' && isKind(t.value)) {
          X.kind = t.value; X.create = false; fitConn(); repaint(); void loadBuckets();
        } else if (t instanceof HTMLSelectElement && t.id === 'bk-exp-conn') {
          X.connection_id = t.value; X.create = false; repaint(); void loadBuckets();
        } else if (t instanceof HTMLInputElement && t.id === 'bk-exp-over') X.overwrite = t.checked;
      });
      r.addEventListener('input', (e) => {
        const t = e.target;
        if (!(t instanceof HTMLInputElement)) return;
        if (t.id === 'bk-exp-prefix') X.prefix = t.value;
        else if (t.id === 'bk-exp-path') X.rpath = t.value;
        else return;
        preview();
      });
      r.addEventListener('click', (e) => {
        const a = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-ex="conns"]') : null;
        if (!a) return;
        r.closest('.ui-dlg')?.querySelector<HTMLElement>('[data-dlg="cancel"]')?.click();   // Settings opens instead
        void import('../settings/dialog.js').then((m) => m.openSettings('connections'));
      });
    },
    onOk: async () => {
      if (X.kind === 'zip') { void downloadZip(bid, path); return null; }
      if (!manage) return cannot('export to a connection');
      if (!X.connection_id) return 'Choose a connection';
      const target: ExportRemoteTarget = { kind: X.kind, connection_id: X.connection_id };
      if (X.kind === 's3') {
        if (!X.bucket.trim()) return 'Pick or type a bucket';
        target.bucket = X.bucket.trim(); target.prefix = asPrefix(X.prefix);
      } else target.path = X.rpath.trim();
      const create = X.kind === 's3' && willCreate();
      let res;
      try { res = await api.exportTo(bid, { path, target, create_bucket: create, overwrite: X.overwrite }); } catch (e) { return errText(e); }
      rememberPref(PREF.exp, { kind: X.kind, connection_id: X.connection_id, bucket: X.bucket.trim(), prefix: X.prefix.trim(), path: X.rpath.trim() } satisfies Last);
      if (res.job_id != null) void trackExport(res.job_id, what);
      else doneToast(res.result, what);
      return null;
    },
  });
}
