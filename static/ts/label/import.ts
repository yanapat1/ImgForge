// Import existing annotations into a labeling task: YOLO label folders, COCO JSON, CSV / TSV / text tables, a database.
// One form (state I, importHTML + attachImport) used in two places:
//   the new-session wizard (views/label-new.js, step "Labels"): parsed before the session exists, the classes found
//     are added to the chosen task, and the import job starts right after the image import (same job lane, FIFO);
//   openImportDialog({pid}) for a saved session (label page ⋯ menu, Sessions home card menu): preview with the match
//     against the session's images, a per-class mapping, then the job's progress and its result.
// API: POST api/label/annotations/upload | preview, POST api/label/projects/{pid}/import-annotations
// (dataviz/server/routes/label_import.py). Every interpolated value goes through esc().

import { errText, getJSON, postJSON } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import type { ConnectionsPayload } from '../types-settings.js';
import type { ImportConn, ImportCoords, ImportCtx, ImportFormat, ImportHandlers, ImportKind, ImportMode, ImportPreview, ImportResult, ImportSource,
  ImportState, ImportTask, ImportUpload, JobStarted } from '../types-label-ui.js';
import type { Job } from '../types.js';
import type { LabelProject } from '../types-label.js';
import { closestEl } from './ui-util.js';

type Choice<T extends string> = readonly [T, string];
const FORMATS: ReadonlyArray<Choice<ImportFormat>> = [['auto', 'Auto-detect'], ['yolo', 'YOLO txt'], ['coco', 'COCO JSON'], ['text', 'CSV / text'], ['db', 'Database']];
const KINDS: ReadonlyArray<readonly [ImportKind, string, string]> = [['upload', 'upload', 'Upload'], ['local', 'folder', 'Path on server'], ['s3', 'cloud', 'S3'], ['remote', 'plug', 'FTP / SFTP']];
const MODES: ReadonlyArray<Choice<ImportMode>> = [['merge', 'Add to what is there'], ['replace', 'Replace the labels of the images it covers'], ['skip_labeled', 'Only images with no labels yet']];
const fmt = (n: number | null | undefined): string => Number(n || 0).toLocaleString();
const kindOf = (c: ImportConn): string => c.kind || 's3';

export function importState(over: Partial<ImportState> = {}): ImportState {
  return { fmt: 'auto', kind: 'upload', taskKey: null, upload: null, uploading: false, path: '', connId: '', bucket: '', prefix: '',
    rpath: '', dbConn: '', dbMode: 'dataviz', sql: '', taskName: '', coords: 'auto', mode: 'merge', classMap: {},
    preview: null, status: null, busy: false, ...over };
}

// the source object the API takes ({kind: upload | local | s3 | ftp | sftp | db, ...})
export function sourceOf(I: ImportState, conns: ImportConn[]): ImportSource {
  if (I.fmt === 'db') return { kind: 'db', connection_id: I.dbConn, mode: I.dbMode, sql: I.sql, task_name: I.taskName.trim() || null };
  if (I.kind === 'upload') return { kind: 'upload', upload_id: I.upload?.upload_id };
  if (I.kind === 'local') return { kind: 'local', path: I.path.trim() };
  if (I.kind === 's3') return { kind: 's3', connection_id: I.connId, bucket: I.bucket.trim(), prefix: I.prefix.trim() };
  const c = conns.find((x) => x.id === I.connId);
  return { kind: c ? kindOf(c) : 'sftp', connection_id: I.connId, path: I.rpath.trim() };
}

export function sourceError(I: ImportState): string | null {
  if (I.fmt === 'db') return !I.dbConn ? 'Pick a database connection.' : I.dbMode === 'sql' && !I.sql.trim() ? 'Write the SELECT that returns the boxes.' : null;
  if (I.kind === 'upload') return I.uploading ? 'Still uploading…' : I.upload ? null : 'Choose the label files (or a .zip of them) to upload.';
  if (I.kind === 'local') return I.path.trim() ? null : 'Type the folder (or the file) on the server that holds the labels.';
  if (I.kind === 's3') return I.connId && I.bucket.trim() ? null : 'Pick an S3 connection and type the bucket.';
  return I.connId ? null : 'Pick an FTP / SFTP connection.';
}

function connSelect(field: string, conns: ImportConn[], kinds: string[], cur: string): string {
  const list = conns.filter((c) => kinds.includes(kindOf(c)));
  return `<select data-if="${field}">` + `<option value="" ${cur ? '' : 'selected'} disabled>${list.length ? 'Pick a connection…' : 'No saved connection (Settings › Connections)'}</option>` +
    list.map((c) => `<option value="${esc(c.id)}" ${c.id === cur ? 'selected' : ''}>${esc(c.name || c.host || c.endpoint || c.id)} · ${esc(kindOf(c))}</option>`).join('') + '</select>';
}

const seg = (field: string, opts: ReadonlyArray<Choice<string>>, cur: string): string => `<div class="segmented imp-seg" role="radiogroup">` + opts.map(([v, label]) =>
  `<button type="button" role="radio" aria-checked="${v === cur}" class="${v === cur ? 'active' : ''}" data-ia="set" data-k="${field}" data-v="${v}">${esc(label)}</button>`).join('') + '</div>';

// ctx: {conns, tasks: [{key, name, task_type, classes: [{name}]}], wizard: bool}
export function importHTML(I: ImportState, ctx: ImportCtx): string {
  const t = ctx.tasks.find((x) => x.key === I.taskKey) || ctx.tasks[0];
  const taskSel = ctx.tasks.length > 1 || !ctx.wizard
    ? `<label class="fld">Into the task<select data-if="taskKey">${ctx.tasks.map((x) => `<option value="${esc(x.key)}" ${x === t ? 'selected' : ''}>${esc(x.name)} · ${esc(x.task_type.replace('_', ' '))}</option>`).join('')}</select></label>` : '';
  let src = '';
  if (I.fmt === 'db') {
    src = `<label class="fld">Connection${connSelect('dbConn', ctx.conns, ['postgres', 'mssql'], I.dbConn)}</label>` +
      seg('dbMode', [['dataviz', 'Tables this app exported'], ['sql', 'My own SELECT']], I.dbMode) +
      (I.dbMode === 'sql'
        ? `<label class="fld mono">SELECT<textarea rows="4" data-if="sql" spellcheck="false" placeholder="SELECT file_name AS image, label AS class, xmin, ymin, xmax, ymax FROM boxes">${esc(I.sql)}</textarea></label>` +
          '<p class="sec-note">Columns are found by name: <b>image</b>, <b>class</b>, then x1 y1 x2 y2 (or xmin…), cx cy w h, or x y w h; optional <b>score</b>. Read only: one SELECT.</p>'
        : `<label class="fld">Exported task name <span class="muted">(optional: all tasks)</span><input type="text" data-if="taskName" value="${esc(I.taskName)}"></label>`);
  } else {
    const panels: Record<ImportKind, () => string> = {
      upload: () => `<div class="sec-actions"><button type="button" data-ia="pick">${icon('upload')}Choose files</button>` +
        `<button type="button" data-ia="pick-dir">${icon('folder')}Choose folder</button>` +
        '<input type="file" class="imp-files" multiple accept=".json,.csv,.tsv,.txt,.zip,.yaml,.yml" hidden>' +
        '<input type="file" class="imp-dir" webkitdirectory multiple hidden></div>' +
        (I.uploading ? '<div class="status-line busy"><span class="spin"></span>Uploading…</div>'
          : I.upload ? `<div class="status-line ok">${icon('check')}${fmt(I.upload.files)} file${I.upload.files === 1 ? '' : 's'} uploaded (${(I.upload.bytes / 1e6).toFixed(1)} MB)</div>` : '') +
        '<p class="sec-note">A COCO .json, a .csv, a YOLO <code>labels/</code> folder (with classes.txt or data.yaml) or a .zip of it.</p>',
      local: () => `<label class="fld mono">Folder or file on the server<input type="text" data-if="path" value="${esc(I.path)}" placeholder="/data/dataset/labels  or  /data/coco/instances.json" spellcheck="false"></label>`,
      s3: () => `<label class="fld">Connection${connSelect('connId', ctx.conns, ['s3'], I.connId)}</label><div class="form-grid">` +
        `<label class="fld">Bucket<input type="text" data-if="bucket" value="${esc(I.bucket)}"></label>` +
        `<label class="fld mono">Prefix (folder) or key of one file<input type="text" data-if="prefix" value="${esc(I.prefix)}" placeholder="dataset/labels/"></label></div>`,
      remote: () => `<label class="fld">Connection${connSelect('connId', ctx.conns, ['ftp', 'sftp'], I.connId)}</label>` +
        `<label class="fld mono">Folder or file<input type="text" data-if="rpath" value="${esc(I.rpath)}" placeholder="/exports/labels"></label>`,
    };
    const panel = panels[I.kind]();
    src = seg('kind', KINDS.map(([v, , l]): Choice<string> => [v, l]), I.kind) + `<div class="imp-src">${panel}</div>`;
  }
  const coords = I.fmt === 'text' || I.fmt === 'db' || I.fmt === 'auto'
    ? `<label class="fld">Box coordinates <select data-if="coords"><option value="auto" ${I.coords === 'auto' ? 'selected' : ''}>detect (values above 1.5 are pixels)</option>` +
      `<option value="norm" ${I.coords === 'norm' ? 'selected' : ''}>normalised 0–1</option><option value="px" ${I.coords === 'px' ? 'selected' : ''}>pixels</option></select></label>` : '';
  const err = sourceError(I);
  return `<div class="imp">${taskSel}<div class="fld"><span>Format</span>${seg('fmt', FORMATS, I.fmt)}</div>${src}${coords}` +
    `<div class="fld"><span>How</span>${seg('mode', MODES, I.mode)}</div>` +
    `<div class="sec-actions"><button type="button" data-ia="preview" ${err || I.busy ? 'disabled' : ''} title="${esc(err || 'Read the labels and show what an import would do')}">${icon('search')}Preview</button>` +
    (I.status ? `<span class="status-line ${I.status.kind}">${I.status.kind === 'busy' ? '<span class="spin"></span>' : ''}${esc(I.status.text)}</span>` : '') + '</div>' +
    previewHTML(I, t, ctx) + '</div>';
}

function previewHTML(I: ImportState, t: ImportTask | undefined, ctx: ImportCtx): string {
  const p = I.preview;
  if (!p) return '';
  const sk = Object.entries(p.skipped || {}).map(([k, n]) => `${fmt(n)} ${k.replace(/_/g, ' ')}`).join(' · ');
  const match = p.matched != null
    ? `<b>${fmt(p.matched)}</b> of them match images of this session` + (p.unmatched_sample && p.unmatched_sample.length
      ? `<details><summary>${fmt(p.images - p.matched)} not found</summary><code>${p.unmatched_sample.map(esc).join('<br>')}</code></details>` : '')
    : 'matched to the images once they are imported';
  const opts = (t ? t.classes : []).map((c) => c.name);
  const rows = (p.classes || []).map((c) => {
    if (ctx.wizard) return `<li><span>${esc(c.name)}</span><span class="num muted">${fmt(c.n)}</span><span class="muted">${opts.some((o) => o.toLowerCase() === c.name.toLowerCase()) ? 'in the task' : 'added to the task'}</span></li>`;
    const cur = I.classMap[c.name] || (c.match ? `=${c.match}` : 'new');
    return `<li><span>${esc(c.name)}</span><span class="num muted">${fmt(c.n)}</span><select data-if="cmap" data-cls="${esc(c.name)}">` +
      `<option value="new" ${cur === 'new' ? 'selected' : ''}>→ new class</option><option value="ignore" ${cur === 'ignore' ? 'selected' : ''}>skip these</option>` +
      opts.map((o) => `<option value="=${esc(o)}" ${cur === `=${o}` ? 'selected' : ''}>→ ${esc(o)}</option>`).join('') + '</select></li>';
  }).join('');
  return `<div class="imp-preview"><p>${icon('check')}<b>${esc(p.format.toUpperCase())}</b>: <b>${fmt(p.records)}</b> shape${p.records === 1 ? '' : 's'} on <b>${fmt(p.images)}</b> image${p.images === 1 ? '' : 's'}; ${match}.</p>` +
    (sk ? `<p class="muted">Skipped: ${esc(sk)}</p>` : '') +
    (p.errors && p.errors.length ? `<details><summary>${fmt(p.errors.length)} problem line${p.errors.length === 1 ? '' : 's'}</summary><code>${p.errors.slice(0, 20).map(esc).join('<br>')}</code></details>` : '') +
    (rows ? `<ul class="imp-classes">${rows}</ul>` : '') + '</div>';
}

// class_map for the API: {source name: class id | 'new' | 'ignore'}
export function classMapOf(I: ImportState, task: ImportTask): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, v] of Object.entries(I.classMap)) {
    if (v === 'new' || v === 'ignore') out[name] = v;
    else if (v.startsWith('=')) {
      const c = (task.classes || []).find((x) => x.name === v.slice(1));
      if (c && c.id) out[name] = c.id;
    }
  }
  return out;
}

async function uploadFiles(I: ImportState, files: Iterable<File>, render: () => void): Promise<void> {
  const list = [...files].filter((f) => /\.(json|csv|tsv|txt|zip|ya?ml)$/i.test(f.name));
  if (!list.length) { I.status = { kind: 'err', text: 'None of those is a label file (.json, .csv, .txt, .zip, .yaml)' }; render(); return; }
  I.uploading = true;
  I.upload = null;
  I.preview = null;
  I.status = null;
  render();
  const fd = new FormData();
  for (const f of list) { fd.append('files', f, f.name); fd.append('rel_paths', f.webkitRelativePath || f.name); }
  try {
    const r = await fetch('api/label/annotations/upload', { method: 'POST', body: fd });
    const d: { detail?: unknown } & Partial<ImportUpload> = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `upload: ${r.status}`);
    I.upload = d as ImportUpload;
  } catch (e) { I.status = { kind: 'err', text: errText(e) }; }
  I.uploading = false;
  render();
}

// Narrow a job result / unknown value to an import result (null when it is not an object).
export function asImportResult(res: unknown): ImportResult | null {
  return res && typeof res === 'object' ? (res as ImportResult) : null;
}

/** The members of `opts` the DOM value `v` names, else undefined. */
function pick<T extends string>(opts: ReadonlyArray<readonly [T, ...string[]]>, v: string | undefined): T | undefined {
  return opts.find((o) => o[0] === v)?.[0];
}

type TextField = 'path' | 'connId' | 'bucket' | 'prefix' | 'rpath' | 'dbConn' | 'sql' | 'taskName';
const TEXT_FIELDS: readonly string[] = ['path', 'connId', 'bucket', 'prefix', 'rpath', 'dbConn', 'sql', 'taskName'];
const isTextField = (k: string): k is TextField => TEXT_FIELDS.includes(k);

/** Store the value of a form control (`data-if`) in the state. */
function setField(I: ImportState, k: string, v: string): void {
  if (isTextField(k)) I[k] = v;
  else if (k === 'taskKey') I.taskKey = v;
  else if (k === 'coords') { const c: ImportCoords | undefined = pick<ImportCoords>([['auto'], ['norm'], ['px']], v); if (c) I.coords = c; }
}

type FormEl = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
const isFormEl = (t: EventTarget | null): t is FormEl =>
  t instanceof HTMLInputElement || t instanceof HTMLSelectElement || t instanceof HTMLTextAreaElement;

// h: {get: () => I, render, ctx: () => ctx, onPreview?(preview)}; listeners on `root` (delegated, once)
export function attachImport(root: HTMLElement, h: ImportHandlers): void {
  root.addEventListener('click', (e) => {
    const b = closestEl<HTMLButtonElement>(e.target, '[data-ia]');
    if (!b || b.disabled) return;
    const I = h.get();
    if (!I) return;
    const a = b.dataset['ia'];
    if (a === 'set') {
      const k = b.dataset['k'], v = b.dataset['v'];
      if (k === 'fmt') { const x = pick(FORMATS, v); if (x) I.fmt = x; }
      else if (k === 'kind') { const x = pick(KINDS, v); if (x) I.kind = x; }
      else if (k === 'mode') { const x = pick(MODES, v); if (x) I.mode = x; }
      else if (k === 'dbMode') { const x = pick<ImportState['dbMode']>([['dataviz'], ['sql']], v); if (x) I.dbMode = x; }
      if (k !== 'mode') I.preview = null;
      I.status = null;
      h.render();
    } else if (a === 'pick') root.querySelector<HTMLInputElement>('.imp-files')?.click();
    else if (a === 'pick-dir') root.querySelector<HTMLInputElement>('.imp-dir')?.click();
    else if (a === 'preview') void preview(I, h);
  });
  root.addEventListener('change', (e) => {
    const el = e.target;
    const I = h.get();
    if (!I || !isFormEl(el)) return;
    if (el instanceof HTMLInputElement && (el.classList.contains('imp-files') || el.classList.contains('imp-dir'))) {
      if (el.files) void uploadFiles(I, el.files, h.render);
      el.value = '';
      return;
    }
    const f = el.dataset['if'];
    if (!f) return;
    if (f === 'cmap') { const c = el.dataset['cls']; if (c !== undefined) I.classMap[c] = el.value; return; }
    setField(I, f, el.value);
    if (['taskKey', 'dbConn', 'connId', 'coords'].includes(f)) { I.preview = null; h.render(); }
  });
  root.addEventListener('input', (e) => {
    const el = e.target, I = h.get();
    if (!I || !isFormEl(el) || !el.dataset['if'] || el.tagName === 'SELECT') return;
    setField(I, el.dataset['if'], el.value);
    I.preview = null;
    const b = root.querySelector<HTMLButtonElement>('[data-ia="preview"]');          // no re-render per key: only the button follows
    if (b) b.disabled = !!sourceError(I);
  });
}

async function preview(I: ImportState, h: ImportHandlers): Promise<void> {
  const ctx = h.ctx();
  const t = ctx.tasks.find((x) => x.key === I.taskKey) || ctx.tasks[0];
  if (!t) { I.status = { kind: 'err', text: 'There is no task to import into' }; h.render(); return; }
  I.busy = true;
  I.status = { kind: 'busy', text: 'reading the labels…' };
  h.render();
  try {
    const body: Record<string, unknown> = { format: I.fmt, source: sourceOf(I, ctx.conns), coords: I.coords, task_type: t.task_type,
      n_kpt: (t.kpText || '').split('\n').filter((s) => s.trim()).length };
    if (ctx.pid && t.id) { body['project_id'] = ctx.pid; body['task_id'] = t.id; }
    I.preview = await postJSON<ImportPreview>('api/label/annotations/preview', body);
    I.status = null;
    if (h.onPreview) h.onPreview(I.preview, t);
  } catch (e) {
    I.preview = null;
    I.status = { kind: 'err', text: errText(e) };
  }
  I.busy = false;
  h.render();
}

// start the job for a saved session; resolves the job id
export async function startImport(pid: string, taskId: string | undefined, I: ImportState, conns: ImportConn[], task: ImportTask | null): Promise<number> {
  const r = await postJSON<JobStarted>(`api/label/projects/${encodeURIComponent(pid)}/import-annotations`, {
    task_id: taskId, format: I.fmt, source: sourceOf(I, conns), mode: I.mode, coords: I.coords,
    class_map: task ? classMapOf(I, task) : {} });
  return r.job_id;
}

export function resultText(result: unknown): string {
  const res = asImportResult(result);
  if (!res) return 'Done.';
  const sk = Object.entries(res.skipped || {}).map(([k, n]) => `${fmt(n)} ${k.replace(/_/g, ' ')}`).join(', ');
  return `Imported ${fmt(res.annotations)} shape${res.annotations === 1 ? '' : 's'} on ${fmt(res.images)} image${res.images === 1 ? '' : 's'}` +
    (res.classes_added && res.classes_added.length ? ` · new classes: ${res.classes_added.join(', ')}` : '') +
    (res.unmatched ? ` · ${fmt(res.unmatched)} image name${res.unmatched === 1 ? '' : 's'} not in the session` : '') +
    (sk ? ` · skipped: ${sk}` : '');
}

// ================================================================ the dialog for a saved session
export interface ImportDialogOpts { pid: string; onDone?: ((res: ImportResult) => void) | null | undefined }

export async function openImportDialog({ pid, onDone = null }: ImportDialogOpts): Promise<void> {
  const prev = document.activeElement;
  const el = document.createElement('div');
  el.className = 'modal lbm-back';
  el.innerHTML = '<div class="panel lbm lbm-import" role="dialog" aria-modal="true" aria-label="Import annotations" tabindex="-1"></div>';
  document.body.appendChild(el);
  const box = el.firstElementChild as HTMLElement;
  const D: { I: ImportState; project: LabelProject | null; conns: ImportConn[]; job: { text: string } | null; result: ImportResult | null; error: string | null } =
    { I: importState(), project: null, conns: [], job: null, result: null, error: null };
  const close = (): void => {
    if (D.job) return;
    el.remove();
    if (prev instanceof HTMLElement && document.contains(prev)) prev.focus();
    if (D.result && onDone) onDone(D.result);
  };
  const tasks = (): ImportTask[] => (D.project ? D.project.tasks : []).map((t) => ({ ...t, key: t.id }));
  const ctx = (): ImportCtx => ({ conns: D.conns, tasks: tasks(), wizard: false, pid });
  const render = (): void => {
    const head = `<div class="lbm-head"><h2>${icon('upload')}Import annotations</h2><button class="icon-btn ghost" data-x="close" aria-label="Close" ${D.job ? 'disabled' : ''}>${icon('x')}</button></div>`;
    let body: string;
    if (!D.project) body = D.error ? `<div class="status-line err">${esc(D.error)}</div>` : '<div class="status-line busy"><span class="spin"></span>Loading…</div>';
    else if (D.job) body = `<div class="status-line busy"><span class="spin"></span><span class="imp-job">${esc(D.job.text || 'importing…')}</span></div>`;
    else if (D.result) body = `<div class="status-line ok">${icon('check')}${esc(resultText(D.result))}</div>` +
      (D.result.unmatched_sample && D.result.unmatched_sample.length ? `<details><summary>Image names not found</summary><code>${D.result.unmatched_sample.map(esc).join('<br>')}</code></details>` : '');
    else body = importHTML(D.I, ctx()) + (D.error ? `<div class="status-line err">${esc(D.error)}</div>` : '');
    const ready = D.project && !D.job && !D.result && !sourceError(D.I);
    const foot = D.result ? `<button class="primary" data-x="close">${icon('check')}Done</button>`
      : `<button class="ghost" data-x="close" ${D.job ? 'disabled' : ''}>Cancel</button><button class="primary" data-x="go" ${ready ? '' : 'disabled'}>${icon('upload')}Import</button>`;
    box.innerHTML = `${head}<div class="lbm-body">${body}</div><div class="lbm-foot"><span class="grow"></span>${foot}</div>`;
  };
  attachImport(box, { get: () => D.I, render, ctx });
  box.addEventListener('click', async (e) => {
    const b = closestEl<HTMLButtonElement>(e.target, '[data-x]');
    if (!b || b.disabled) return;
    if (b.dataset['x'] === 'close') { close(); return; }
    const t = tasks().find((x) => x.key === D.I.taskKey) || tasks()[0];
    D.error = null;
    try {
      const jid = await startImport(pid, t?.id, D.I, D.conns, t ?? null);
      D.job = { text: 'queued…' };
      render();
      const j = await waitJob(jid, (s: Job) => {
        const x = box.querySelector('.imp-job');
        if (x) x.textContent = `${s.stage || s.status}${s.total ? ` · ${fmt(s.done)} / ${fmt(s.total)}` : ''}${s.extra ? ` · ${s.extra}` : ''}`;
      });
      D.result = asImportResult(j && j.result) || {};
    } catch (err) { D.error = errText(err); }
    D.job = null;
    render();
  });
  el.addEventListener('mousedown', (e) => { if (e.target === el) close(); });
  el.addEventListener('keydown', (e) => {
    e.stopPropagation();                      // the label page's hotkeys never see keys typed here
    if (e.key === 'Escape') { e.preventDefault(); close(); }
  });
  render();
  box.focus();
  try {
    const [p, c] = await Promise.all([getJSON<LabelProject>(`api/label/projects/${encodeURIComponent(pid)}`), getJSON<ConnectionsPayload>('api/connections')]);
    D.project = p;
    D.conns = c.connections || [];
    D.I.taskKey = p.tasks[0]?.id ?? null;
  } catch (e) { D.error = errText(e); }
  render();
}
