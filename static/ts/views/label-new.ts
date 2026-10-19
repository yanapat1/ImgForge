// Create / edit a labeling session: a modal wizard (#lw), loaded on demand by views/home.js.
// New: 1 Name · 2 Images (drop files / folders, a local folder, an S3 prefix, an FTP / SFTP path) · 3 Tasks · 4 Export
// default; Create = POST api/label/projects, then (dropped files) 3 parallel multipart uploads + finalize, then the
// import job's progress, then ./?label=<pid>. Edit: name, tasks (rename existing, add classes / tasks, reorder) and
// the export default, sent as small PATCH / POST calls.
// Same patterns as the Settings dialog: all state in W, one innerHTML render per change (focus is restored by
// data-fk), inputs write into W without a render, one delegated listener per event type on #lw.

import { api, errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, fmtEta, icon, sleep } from '../core/dom.js';
import { setBar, waitJob } from '../core/jobs.js';
import { attachImport, importHTML, importState, resultText, sourceError, startImport } from '../label/import.js';
import { TASK_TYPES } from '../label/state.js';
import { CONN_KINDS, connDefaults, connFormHTML, connKind, connUri, crumbsHTML, statusHTML, tagc } from '../settings/forms.js';
import type { ConnDraft, ConnItem, ConnKind, StatusLine } from '../types-settings.js';
import type { ImportCtx, ImportPreview, ImportState, ImportTask, JobStarted } from '../types-label-ui.js';
import type { TaskConfig, TaskType } from '../types-label.js';
import type { Job } from '../types.js';
import type { WizardOpts } from '../types-home.js';

const IMAGE_RE = /\.(jpe?g|png|bmp|webp|tiff?)$/i;
const ACCEPT = '.jpg,.jpeg,.png,.bmp,.webp,.tif,.tiff';
const UP = { files: 8, bytes: 16 * 1024 * 1024, parallel: 3, retries: 2 };
type StepId = 'name' | 'images' | 'tasks' | 'labels' | 'export';
type SrcTab = 'drop' | 'local' | 's3' | 'remote';
const STEP_TEXT: Record<StepId, string> = { name: 'Name', images: 'Images', tasks: 'Tasks', labels: 'Labels', export: 'Export' };
const SRC_TABS: ReadonlyArray<readonly [SrcTab, string, string]> = [['drop', 'upload', 'Drop files'], ['local', 'folder', 'Local folder'], ['s3', 'cloud', 'S3'], ['remote', 'plug', 'FTP / SFTP']];
const EXPORT_KINDS: ReadonlyArray<readonly [string, string]> = [['', 'None (choose when exporting)'], ['zip', 'Download a zip'], ['local', 'Local folder'], ['s3', 'S3 bucket'],
  ['ftp', 'FTP server'], ['sftp', 'SFTP server'], ['db', 'Database (PostgreSQL / SQL Server)']];
const CLOSE_DE = 0.06;                 // OKLab distance under which two class colours count as "too close"

// ---- wizard state (`type` aliases, not interfaces: write() views them as plain records for the data-f="a.b" paths)
/** A class chip. `shown` is the colour on screen (the chosen one, else an auto colour from the palette). */
type WClass = { key: string; id?: string | undefined; name: string; color: string | null; custom: boolean; shown: string; origColor?: string | null | undefined };
type WTask = {
  key: string; id?: string | undefined; name: string; origName?: string | undefined; task_type: TaskType; classes: WClass[];
  kpText: string; flipText: string; multi: boolean; locked: boolean; note: string; config?: TaskConfig | undefined;
};
/** GET api/fs/browse and POST api/connections/browse (S3 / FTP / SFTP). */
type WBrowse = { path: string; parent?: string | null | undefined; images?: number | null | undefined; folders: Array<{ name: string; images?: number | null | undefined }> };
type WBusy = { title: string; text: string; indeterminate: boolean; cancel?: boolean | undefined; closable?: boolean | undefined };
type WUpload = {
  xhrs: Set<XMLHttpRequest>; live: Map<Array<[string, File]>, number>; files: number; bytes: number; saved: number; skipped: number;
  t0: number; stop: boolean; frame: number;
};
type WExport = { kind: string; path: string; connId: string; bucket: string; prefix: string };
type WState = {
  mode: 'new' | 'edit'; pid: string | null; orig: OrigProject | null; steps: StepId[]; step: number; tried: Partial<Record<StepId, boolean>>;
  name: string; desc: string; review: boolean;
  impOn: boolean; imp: ImportState; taskIds: string[] | null; annDone: boolean;
  tab: SrcTab;
  drop: { files: Map<string, File>; bytes: number; ignored: number; scanning: number; sent: Set<string> };
  local: { path: string; browse: WBrowse | null; recursive: boolean; status: StatusLine | null };
  s3: { connId: string; buckets: string[] | null; bucket: string; prefix: string; browse: WBrowse | null; status: StatusLine | null };
  remote: { connId: string; conn: ConnDraft; browse: WBrowse | null; status: StatusLine | null };
  tasks: WTask[];
  exp: WExport;
  conns: ConnItem[]; busy: WBusy | null; error: string | null; created: boolean; jobId: number | null; jobFailed: boolean; srcKind: string;
  dirty: boolean; up: WUpload | null;
  names: string[];                       // the other label sessions' names, lower-cased
  focus: string | null;                  // data-fk to focus after the next render
  drag: string | null;                   // key of the task row being dragged
};
/** GET api/label/projects/{pid}: what the edit mode reads. */
interface OrigProject {
  id: string; name: string; description?: string; review?: boolean;
  export_default?: { kind?: string; path?: string; connection_id?: string; bucket?: string; prefix?: string } | null;
  tasks?: Array<{ id: string; name: string; task_type: TaskType; config?: TaskConfig; classes?: Array<{ id: string; name: string; color: string }> }>;
}

let W: WState | null = null;
let palette: string[] | null = null;   // GET api/label/colors?n=64, fetched once
let bound = false;
let returnFocus: Element | null = null;
let seq = 0;
const uid = (): string => `k${++seq}`;

/** The live wizard state; every handler runs only while the dialog is open (closeWizard clears W). */
function wz(): WState { if (!W) throw new Error('the wizard is not open'); return W; }

const fmt = (n: unknown): string => Number(n).toLocaleString();
const isRec = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isTaskType = (t: string): t is TaskType => Object.prototype.hasOwnProperty.call(TASK_TYPES, t);
const isSrcTab = (t: unknown): t is SrcTab => SRC_TABS.some(([id]) => id === t);
const fmtBytes = (b: number): string => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(0, Math.round((b || 0) / 1e3))} kB`);

function newTask(type: TaskType = 'object_detection', name = ''): WTask {
  return { key: uid(), name, task_type: type, classes: [], kpText: '', flipText: '', multi: false, locked: false, note: '' };
}

function fresh(): WState {
  return {
    mode: 'new', pid: null, orig: null, steps: ['name', 'images', 'tasks', 'labels', 'export'], step: 0, tried: {}, name: '', desc: '', review: false,
    impOn: false, imp: importState(), taskIds: null, annDone: false,
    tab: 'drop',
    drop: { files: new Map(), bytes: 0, ignored: 0, scanning: 0, sent: new Set() },
    local: { path: '', browse: null, recursive: true, status: null },
    s3: { connId: '', buckets: null, bucket: '', prefix: '', browse: null, status: null },
    remote: { connId: '', conn: connDefaults('sftp'), browse: null, status: null },
    tasks: [newTask('object_detection', 'detect')],          // R1: the first task is object detection
    exp: { kind: '', path: '', connId: '', bucket: '', prefix: '' },
    conns: [], busy: null, error: null, created: false, jobId: null, jobFailed: false, srcKind: '', dirty: false, up: null,
    names: [],                                               // the other label sessions' names, lower-cased
    focus: null, drag: null,
  };
}

// ---------------------------------------------------------------- colours
// auto colours fill in project-wide order (task order, then class order), skipping every colour already taken by
// a saved or hand-picked class, so no two classes of the project share one
function fallbackPalette(n: number): string[] {
  return Array.from({ length: n }, (_, i) => {
    const h = (i * 137.508) % 360, l = 0.55 + 0.12 * ((i >> 1) % 2), s = 0.65;
    const f = (k: number): number => { const a = s * Math.min(l, 1 - l), t = (k + h / 30) % 12; return l - a * Math.max(-1, Math.min(t - 3, 9 - t, 1)); };
    return '#' + [f(0), f(8), f(4)].map((v) => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
  });
}

function assignColors(): void {
  const W = wz();
  const pal = palette || fallbackPalette(64);
  const used = new Set<string>();
  for (const t of W.tasks) for (const c of t.classes) if (c.color) used.add(c.color.toLowerCase());
  let k = 0;
  for (const t of W.tasks) {
    for (const c of t.classes) {
      if (c.color) { c.shown = c.color; continue; }
      while (k < pal.length && used.has((pal[k] ?? '').toLowerCase())) k++;
      c.shown = pal[k % pal.length] ?? '#888888';
      k++;
    }
  }
}

function oklab(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  const lin = (v0: number): number => { const v = v0 / 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const r = lin((n >> 16) & 255), g = lin((n >> 8) & 255), b = lin(n & 255);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s, 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s];
}

// the closest other class to a hand-picked colour, when it is too close to tell apart
function tooClose(c: WClass): string | null {
  const W = wz();
  const a = oklab(c.shown);
  let best: string | null = null, bd = CLOSE_DE;
  for (const t of W.tasks) {
    for (const o of t.classes) {
      if (o === c) continue;
      const b = oklab(o.shown);
      const d = Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
      if (d < bd) { bd = d; best = o.name; }
    }
  }
  return best;
}

// ---------------------------------------------------------------- validation
function findTask(key: string | undefined): WTask {
  const t = wz().tasks.find((x) => x.key === key);
  if (!t) throw new Error(`no task ${key}`);
  return t;
}

function lines(text: unknown): string[] { return String(text || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean); }

// flip pairs: one pair per line ("left_eye right_eye", "left_eye, right_eye" or "left_eye:right_eye") -> YOLO flip_idx
function flipIdx(t: WTask): { idx: number[]; bad: string[] } {
  const names = lines(t.kpText);
  const idx = names.map((_, i) => i);
  const bad: string[] = [];
  for (const ln of lines(t.flipText)) {
    const p = ln.split(/\s*(?:<->|[,;:=]|\s)\s*/).filter(Boolean);
    const i = names.indexOf(p[0] ?? ''), j = names.indexOf(p[1] ?? '');
    if (p.length !== 2 || i < 0 || j < 0) { bad.push(ln); continue; }
    idx[i] = j;
    idx[j] = i;
  }
  return { idx, bad };
}

function taskIssues(): { errs: string[]; warns: string[] } {
  const W = wz();
  const errs: string[] = [], warns: string[] = [];
  if (!W.tasks.length) errs.push('Add at least one task.');
  const seen = new Map<string, boolean>();
  W.tasks.forEach((t, i) => {
    const nm = t.name.trim();
    const label = nm ? `“${nm}”` : `Task ${i + 1}`;
    if (!nm) errs.push(`Task ${i + 1} needs a name.`);
    else if (seen.has(nm.toLowerCase())) errs.push(`Two tasks are called “${nm}”: task names must be unique.`);
    seen.set(nm.toLowerCase(), true);
    if (t.task_type === 'keypoints' && !t.locked) {
      if (!lines(t.kpText).length) errs.push(`${label} needs its point names (one per line).`);
      const { bad } = flipIdx(t);
      if (bad.length) errs.push(`${label}: flip pair${bad.length > 1 ? 's' : ''} ${bad.map((b) => `“${b}”`).join(', ')} must name two of its points.`);
    }
    if (!t.classes.length) warns.push(`${label} has no classes yet: add them now, or later on the labeling page.`);
  });
  return { errs, warns };
}

function stepErrors(id: StepId): string[] {
  const W = wz();
  if (id === 'name') {
    const n = W.name.trim(), f = n.toLowerCase();
    if (!n) return ['Give the session a name.'];
    // names are unique per list (case-insensitive); in edit mode the session's own name is fine; the server re-checks
    const own = W.orig && String(W.orig.name || '').trim().toLowerCase() === f;
    return !own && W.names.includes(f) ? [`A label session called “${n}” already exists.`] : [];
  }
  if (id === 'tasks') return taskIssues().errs;
  if (id === 'images') {
    if (W.tab === 'drop') return W.drop.scanning ? ['Still reading the dropped folders…'] : [];
    if (W.tab === 'local') return W.local.browse ? [] : ['Open the folder that holds the images.'];
    if (W.tab === 's3') return W.s3.bucket && W.s3.browse ? [] : ['Pick a connection and a bucket, then open the prefix that holds the images.'];
    if (W.tab === 'remote') return W.remote.browse ? [] : ['Connect, then open the folder that holds the images.'];
  }
  if (id === 'labels') {
    if (!W.impOn) return [];
    const e = sourceError(W.imp);
    if (e) return [e];
    return W.imp.preview ? [] : ['Preview the labels first: their classes are added to the task.'];
  }
  if (id === 'export') {
    const x = W.exp;
    if (!x.kind || x.kind === 'zip') return [];
    if (x.kind === 'local') return x.path.trim() ? [] : ['Type the folder the export goes to.'];
    if (!x.connId) return ['Pick a connection for the export.'];
    if (x.kind === 's3' && !x.bucket.trim()) return ['Type the bucket the export goes to.'];
  }
  return [];
}

// ---------------------------------------------------------------- render
function stepsHTML(): string {
  const W = wz();
  return W.steps.map((id, i) => {
    const st = i === W.step ? 'cur' : i < W.step ? 'done' : '';
    return `<li class="${st}"><button data-act="goto" data-i="${i}" ${i > W.step || W.busy || W.created ? 'disabled' : ''} ` +
      `${i === W.step ? 'aria-current="step"' : ''}><span class="lw-sn num">${i < W.step ? '✓' : i + 1}</span>${STEP_TEXT[id]}</button></li>`;
  }).join('');
}

function nameHTML(): string {
  const W = wz();
  return '<div class="lw-step">' +
    `<label class="fld lw-big">Session name<input type="text" data-f="name" data-fk="name" value="${esc(W.name)}" maxlength="120" ` +
    'placeholder="e.g. Street scenes, March batch" autocomplete="off"></label>' +
    `<label class="fld"><span>Description <span class="muted">(optional)</span></span><textarea rows="3" data-f="desc" data-fk="desc">${esc(W.desc)}</textarea></label>` +
    `<label class="check"><input type="checkbox" data-f="review" ${W.review ? 'checked' : ''}> Track review <span class="muted">(optional: a second bar on the card, a Review button, and Review all)</span></label>` +
    '<p class="sec-note">Enter goes to the next step · Esc closes</p></div>';
}

// ---- existing labels (label/import.js renders the form; its classes join the chosen task on preview)
function labelsHTML(): string {
  const W = wz();
  const on = W.impOn;
  const head = '<div class="segmented lw-kinds" role="radiogroup" aria-label="Existing labels">' +
    ([[false, 'No labels to import'], [true, 'Import existing labels']] as const).map(([v, l]) =>
      `<button type="button" role="radio" aria-checked="${on === v}" class="${on === v ? 'active' : ''}" data-act="imp-on" data-v="${v ? 1 : 0}">${l}</button>`).join('') + '</div>';
  if (!on) {
    return `<div class="lw-step">${head}<p class="sec-note">Already have boxes, polygons or tags for these images: YOLO label files, a COCO JSON, ` +
      'a CSV, or a database table? Import them now and they show up as labels you can check and fix. You can also import later from the session.</p></div>';
  }
  if (!W.imp.taskKey && W.tasks[0]) W.imp.taskKey = W.tasks[0].key;
  return `<div class="lw-step">${head}${importHTML(W.imp, importCtx())}` +
    '<p class="sec-note">Images are matched by path, then file name, then name without extension.</p></div>';
}

const importCtx = (): ImportCtx => { const W = wz(); return { conns: W.conns, tasks: W.tasks, wizard: true }; };

// ---- images
function folderList(list: WBrowse['folders'] | null | undefined, act: string, emptyText: string): string {
  if (!list) return '';
  if (!list.length) return `<div class="folders"><div class="none">${esc(emptyText)}</div></div>`;
  return '<div class="folders">' + list.map((f) => {
    const n = f.images;
    return `<button class="folder${n ? ' has-img' : ''}" data-act="${act}" data-name="${esc(f.name)}">${icon('folder')}<span class="name">${esc(f.name)}</span>` +
      (n != null ? tagc(`${fmt(n)}${n >= 10000 ? '+' : ''} image${n === 1 ? '' : 's'}`, n ? 'on' : '') : '') + `${icon('chev-r')}</button>`;
  }).join('') + '</div>';
}

function dropHTML(): string {
  const W = wz();
  const d = W.drop;
  const n = d.files.size;
  let sum = '';
  if (n) {
    const tops = new Set<string>();
    for (const rel of d.files.keys()) { const i = rel.indexOf('/'); tops.add(i < 0 ? '' : rel.slice(0, i)); }
    const folders = [...tops].filter(Boolean);
    sum = `<div class="lw-sum">${icon('image')}<span><b class="num">${fmt(n)}</b> image${n === 1 ? '' : 's'} · <b class="num">${fmtBytes(d.bytes)}</b>` +
      (folders.length ? ` · from ${folders.length === 1 ? `“${esc(folders[0])}”` : `${folders.length} folders`}` : '') +
      (d.ignored ? ` · <span class="muted">${fmt(d.ignored)} other file${d.ignored === 1 ? '' : 's'} skipped</span>` : '') +
      `</span><span class="grow"></span><button class="ghost" data-act="drop-clear">${icon('x')}Clear</button></div>`;
  }
  return `<div class="lw-drop${n ? ' has' : ''}" id="lw-drop">` +
    `<span class="lw-drop-ico">${icon('upload')}</span><b>Drop images or folders here</b>` +
    '<span class="muted">Folders are read with all their sub-folders; only images (jpg, png, bmp, webp, tif) are kept. Files are uploaded when you create the session.</span>' +
    `<div class="sec-actions"><button data-act="pick-files">${icon('image')}Choose files</button><button data-act="pick-folder">${icon('folder')}Choose folder</button></div>` +
    `<input type="file" id="lw-files" multiple accept="${ACCEPT}" hidden data-change="files">` +
    '<input type="file" id="lw-folder" webkitdirectory multiple hidden data-change="files"></div>' +
    (d.scanning ? `<div class="status-line busy"><span class="spin"></span><span id="lw-scan">Reading folders… ${fmt(d.scanning)} files</span></div>` : '') +
    sum + (!n && !d.scanning ? '<p class="sec-note">No images yet is fine too: the session starts empty and you can import later from its page.</p>' : '');
}

function localHTML(): string {
  const W = wz();
  const L = W.local, b = L.browse;
  const parts = b ? b.path.split('/').filter(Boolean) : [];
  const here = b && b.images != null ? `${fmt(b.images)} image${b.images === 1 ? '' : 's'} directly in this folder` : '';
  return `<div class="sec-actions"><input class="txt mono grow" type="text" data-f="local.path" data-fk="local-path" value="${esc(L.path)}" ` +
    'placeholder="/data/images" data-enter="local-go" aria-label="Folder path">' +
    `<button data-act="local-go">${icon('arrow-r')}Go</button>${b && b.parent ? `<button class="icon-btn" data-act="local-up" aria-label="Parent folder" title="Parent folder">${icon('up')}</button>` : ''}</div>` +
    (b ? `<div class="browser">${crumbsHTML('/', parts, 'local-crumb')}${folderList(b.folders, 'local-folder', 'no sub-folders here')}</div>` : '') +
    statusHTML(L.status || (b ? { kind: b.images || L.recursive ? 'ok' : '', text: `Selected: ${b.path}${here ? ` · ${here}` : ''}` } : null)) +
    `<label class="check"><input type="checkbox" data-f="local.recursive" ${L.recursive ? 'checked' : ''}> Include every sub-folder</label>` +
    '<p class="sec-note">Read in place: nothing is copied; only small thumbnails go to the workspace.</p>';
}

function connOptions(kinds: ConnKind[], cur: string, withNew: boolean): string {
  const W = wz();
  const list = W.conns.filter((c) => kinds.includes(connKind(c)));
  return `<option value="" ${cur ? '' : 'selected'} disabled>${list.length ? 'Pick a connection…' : 'No saved connection'}</option>` +
    list.map((c) => `<option value="${esc(c.id)}" ${c.id === cur ? 'selected' : ''}>${esc(c.name || connUri(c))} — ${esc(connUri(c))}</option>`).join('') +
    (withNew ? `<option value="new" ${cur === 'new' ? 'selected' : ''}>+ New connection…</option>` : '');
}

function s3HTML(): string {
  const W = wz();
  const S = W.s3, b = S.browse;
  const parts = (S.prefix || '').split('/').filter(Boolean);
  const none = !W.conns.some((c) => connKind(c) === 's3');
  return `<label class="fld">Connection<select data-f="s3.connId" data-change="s3-conn" data-fk="s3-conn">${connOptions(['s3'], S.connId, false)}</select></label>` +
    (none ? '<p class="sec-note">Add an S3 / RustFS connection in Settings › Connections first.</p>' : '') +
    (S.buckets ? '<div class="form-grid">' +
      `<label class="fld">Bucket<select data-f="s3.bucket" data-change="s3-bucket" data-fk="s3-bucket">${S.buckets.map((x) =>
        `<option ${x === S.bucket ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>` +
      `<label class="fld mono">Prefix (the folder of images)<input type="text" data-f="s3.prefix" data-fk="s3-prefix" value="${esc(S.prefix)}" ` +
      'placeholder="raw/2024-03" data-enter="s3-go"></label></div>' : '') +
    (S.bucket && b ? `<div class="browser">${crumbsHTML(S.bucket, parts, 's3-crumb')}${folderList(b.folders, 's3-folder', 'no sub-folders here')}</div>` : '') +
    statusHTML(S.status || (b ? { kind: 'ok', text: `Selected: s3://${S.bucket}/${S.prefix || ''}${b.images != null ? ` · ${fmt(b.images)} images here` : ''} (sub-folders included)` } : null));
}

function remoteHTML(): string {
  const W = wz();
  const R = W.remote, b = R.browse;
  const isNew = R.connId === 'new';
  const kinds = isNew ? '<div class="segmented lw-kinds" role="radiogroup" aria-label="Protocol">' + (['sftp', 'ftp'] as const).map((k) =>
    `<button role="radio" aria-checked="${connKind(R.conn) === k}" class="${connKind(R.conn) === k ? 'active' : ''}" data-act="remote-kind" data-kind="${k}">${CONN_KINDS[k].label}</button>`).join('') + '</div>' : '';
  const parts = b ? b.path.split('/').filter(Boolean) : [];
  return `<label class="fld">Connection<select data-f="remote.connId" data-change="remote-conn" data-fk="remote-conn">${connOptions(['ftp', 'sftp'], R.connId, true)}</select></label>` +
    (isNew ? `<div class="lw-inline-conn">${kinds}${connFormHTML(R.conn, null)}<p class="sec-note">Saved to Settings › Connections when the session is created.</p></div>` : '') +
    `<div class="sec-actions"><button data-act="remote-go">${icon('plug')}${b ? 'Reload' : 'Connect'}</button>` +
    (b && b.parent != null ? `<button class="icon-btn" data-act="remote-up" aria-label="Parent folder" title="Parent folder">${icon('up')}</button>` : '') + '</div>' +
    (b ? `<div class="browser">${crumbsHTML('/', parts, 'remote-crumb')}${folderList(b.folders, 'remote-folder', 'no sub-folders here')}</div>` : '') +
    statusHTML(R.status || (b ? { kind: 'ok', text: `Selected: ${b.path}${b.images != null ? ` · ${fmt(b.images)} images here` : ''} (sub-folders included)` } : null));
}

function imagesHTML(): string {
  const W = wz();
  const tabs = SRC_TABS.map(([id, ico, label]) => `<button role="tab" id="lw-tab-${id}" aria-selected="${W.tab === id}" aria-controls="lw-panel" ` +
    `tabindex="${W.tab === id ? 0 : -1}" class="${W.tab === id ? 'active' : ''}" data-act="tab" data-tab="${id}">${icon(ico)}${label}</button>`).join('');
  const panel = { drop: dropHTML, local: localHTML, s3: s3HTML, remote: remoteHTML }[W.tab]();
  return `<div class="lw-step"><div class="lw-tabs" role="tablist" aria-label="Where the images are">${tabs}</div>` +
    `<div id="lw-panel" class="lw-panel" role="tabpanel" aria-labelledby="lw-tab-${W.tab}">${panel}</div></div>`;
}

// ---- tasks
function chipHTML(c: WClass): string {
  const near = c.custom ? tooClose(c) : null;
  return `<span class="lw-chip${near ? ' warn' : ''}" role="listitem" data-ck="${c.key}">` +
    `<label class="lw-sw" style="--c:${esc(c.shown)}" title="Change the colour of ${esc(c.name)}">` +
    `<input type="color" value="${esc(c.shown)}" data-color="1" aria-label="Colour of ${esc(c.name)}"></label>` +
    `<span class="lw-cn">${esc(c.name)}</span>` +
    (near ? `<span class="lw-near" title="Too close to “${esc(near)}”: hard to tell apart">${icon('alert')}<span>close to ${esc(near)}</span></span>` : '') +
    (c.id ? '' : `<button class="lw-cx" data-act="c-rm" aria-label="Remove ${esc(c.name)}" title="Remove">${icon('x')}</button>`) + '</span>';
}

function taskHTML(t: WTask, i: number): string {
  const n = wz().tasks.length;
  const type = TASK_TYPES[t.task_type];
  const opts = Object.entries(TASK_TYPES).map(([k, v]) => `<option value="${k}" ${k === t.task_type ? 'selected' : ''}>${esc(v.label)}</option>`).join('');
  let extra = '';
  if (t.task_type === 'keypoints') {
    extra = '<div class="form-grid">' +
      `<label class="fld">Point names, one per line<textarea rows="4" data-tf="kpText" data-fk="kp-${t.key}" ${t.locked ? 'disabled' : ''} ` +
      `placeholder="nose&#10;left_eye&#10;right_eye" spellcheck="false">${esc(t.kpText)}</textarea></label>` +
      `<label class="fld">Flip pairs (optional), one pair per line<textarea rows="4" data-tf="flipText" data-fk="flip-${t.key}" ${t.locked ? 'disabled' : ''} ` +
      `placeholder="left_eye right_eye" spellcheck="false">${esc(t.flipText)}</textarea><span class="help">Swapped when an image is flipped for training</span></label></div>`;
  } else if (t.task_type === 'classification') {
    extra = `<label class="check"><input type="checkbox" data-tf="multi" ${t.multi ? 'checked' : ''}> Several classes per image (multi-label)</label>`;
  }
  return `<div class="lw-task" data-tk="${t.key}" role="listitem" aria-label="Task ${i + 1}">` +
    '<div class="lw-thead">' +
    `<span class="lw-grip" draggable="true" title="Drag to reorder" aria-hidden="true"></span>` +
    `<span class="lw-ttype" title="${esc(type.label)}">${icon(type.icon)}</span>` +
    `<select data-tf="task_type" data-change="task-type" aria-label="Task ${i + 1} type" ${t.locked ? 'disabled' : ''}>${opts}</select>` +
    `<input class="txt" type="text" data-tf="name" data-fk="tn-${t.key}" value="${esc(t.name)}" placeholder="task name" aria-label="Task ${i + 1} name" maxlength="60">` +
    '<span class="grow"></span>' +
    `<button class="icon-btn ghost" data-act="t-up" data-fk="up-${t.key}" ${i === 0 ? 'disabled' : ''} aria-label="Move task ${i + 1} up" title="Move up">${icon('up')}</button>` +
    `<button class="icon-btn ghost lw-down" data-act="t-down" data-fk="down-${t.key}" ${i === n - 1 ? 'disabled' : ''} aria-label="Move task ${i + 1} down" title="Move down">${icon('up')}</button>` +
    (t.locked ? '' : `<button class="icon-btn ghost danger" data-act="t-rm" ${n === 1 ? 'disabled' : ''} aria-label="Remove task ${i + 1}" title="Remove the task">${icon('trash')}</button>`) +
    '</div>' +
    `<div class="lw-classes" role="list" aria-label="Classes of ${esc(t.name || `task ${i + 1}`)}">${t.classes.map(chipHTML).join('')}` +
    `<input class="lw-cin" type="text" data-cin="1" data-fk="cin-${t.key}" placeholder="${t.classes.length ? 'Add a class…' : 'Type a class name + Enter, or paste a list (one per line)'}" ` +
    `aria-label="Add a class to ${esc(t.name || `task ${i + 1}`)}" autocomplete="off" spellcheck="false"></div>` +
    (t.note ? `<div class="lw-note">${esc(t.note)}</div>` : '') + extra + '</div>';
}

function tasksHTML(): string {
  const W = wz();
  assignColors();
  const { errs, warns } = taskIssues();
  const shown = W.tried.tasks ? errs : [];
  return '<div class="lw-step">' +
    '<p class="sec-note">One session can have several tasks on the same images; each has its own classes and is exported to its own folder. ' +
    'Alt+1, Alt+2… switch tasks on the labeling page.</p>' +
    `<div class="lw-tasks" id="lw-tasks" role="list">${W.tasks.map(taskHTML).join('')}</div>` +
    `<div class="sec-actions"><button data-act="t-add">${icon('plus')}Add task</button></div>` +
    shown.map((e) => statusHTML({ kind: 'err', text: e })).join('') +
    warns.map((w) => `<div class="status-line warn">${icon('alert')}<span>${esc(w)}</span></div>`).join('') + '</div>';
}

// ---- export default
function exportHTML(): string {
  const W = wz();
  const x = W.exp;
  const opts = EXPORT_KINDS.map(([k, t]) => `<option value="${k}" ${k === x.kind ? 'selected' : ''}>${esc(t)}</option>`).join('');
  let fields = '';
  if (x.kind === 'local') {
    fields = `<label class="fld mono">Folder<input type="text" data-f="exp.path" data-fk="exp-path" value="${esc(x.path)}" placeholder="/data/exports/my-session"></label>`;
  } else if (['s3', 'ftp', 'sftp', 'db'].includes(x.kind)) {
    const kinds: ConnKind[] = x.kind === 'db' ? ['postgres', 'mssql'] : x.kind === 's3' ? ['s3'] : x.kind === 'ftp' ? ['ftp'] : ['sftp'];
    fields = `<label class="fld">Connection<select data-f="exp.connId" data-fk="exp-conn">${connOptions(kinds, x.connId, false)}</select></label>`;
    if (!W.conns.some((c) => kinds.includes(connKind(c)))) fields += '<p class="sec-note">No saved connection of this kind yet: add one in Settings › Connections.</p>';
    if (x.kind === 's3') {
      fields += '<div class="form-grid">' +
        `<label class="fld mono">Bucket<input type="text" data-f="exp.bucket" data-fk="exp-bucket" value="${esc(x.bucket)}"></label>` +
        `<label class="fld mono">Prefix<input type="text" data-f="exp.prefix" data-fk="exp-prefix" value="${esc(x.prefix)}" placeholder="exports/my-session"></label></div>`;
    } else if (x.kind !== 'db') {
      fields += `<label class="fld mono">Folder on the server<input type="text" data-f="exp.path" data-fk="exp-path" value="${esc(x.path)}" placeholder="/exports/my-session"></label>`;
    }
  }
  return '<div class="lw-step">' +
    `<label class="fld"><span>Default export target <span class="muted">(optional)</span></span><select data-f="exp.kind" data-change="exp-kind" data-fk="exp-kind">${opts}</select></label>` +
    fields +
    '<pre class="lw-tree" aria-label="Export layout">images/\n' + W.tasks.map((t) => `${esc(slug(t.name) || 'task')}/  labels/  classes.txt  data.yaml`).join('\n') + '\nmanifest.json</pre>' +
    '<p class="sec-note">Every export writes one shared images/ folder and one folder per task in YOLO format. The target can be changed in the export dialog.</p></div>';
}

const slug = (s: unknown): string => String(s || '').trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');

// ---- busy (creating, uploading, importing) / created-but-failed views
function busyHTML(b: WBusy): string {
  return `<div class="lw-busy" role="status" aria-live="polite"><div class="lw-busy-title"><span class="spin"></span><b id="lw-bt">${esc(b.title)}</b></div>` +
    `<div class="bar big${b.indeterminate ? ' indeterminate' : ''}" role="progressbar" aria-label="${esc(b.title)}"><div id="lw-bf"></div></div>` +
    `<div id="lw-bx" class="lw-busy-text num">${esc(b.text || '')}</div></div>`;
}

function stepBody(): string {
  const W = wz();
  if (W.busy) return busyHTML(W.busy);
  const id = W.steps[W.step];
  const body: Record<StepId, () => string> = { name: nameHTML, images: imagesHTML, tasks: tasksHTML, labels: labelsHTML, export: exportHTML };
  return id ? body[id]() : '';
}

function footHTML(): string {
  const W = wz();
  const err = W.error ? statusHTML({ kind: 'err', text: W.error }) : '';
  if (W.busy) {
    return `${err}<span class="grow"></span>` + (W.busy.cancel ? `<button class="ghost" data-act="up-cancel">${icon('x')}Cancel upload</button>` : '') +
      (W.busy.closable ? '<button class="ghost" data-act="close">Close (keeps running)</button>' : '');
  }
  if (W.created) {
    return `${err}<span class="grow"></span><button class="ghost" data-act="close">Close</button>` +
      `<button data-act="open-now">${icon('play')}Open the session</button><button class="primary" data-act="next">${icon('refresh')}Retry</button>`;
  }
  const last = W.step === W.steps.length - 1;
  const cur = W.steps[W.step] ?? 'name';
  const stepErr = W.tried[cur] && cur !== 'tasks' ? stepErrors(cur) : [];
  const nextText = !last ? `Next${icon('arrow-r')}` : W.mode === 'edit' ? `${icon('check')}Save changes` : `${icon('check')}Create session`;
  return (err || (stepErr[0] ? statusHTML({ kind: 'err', text: stepErr[0] }) : '')) + '<span class="grow"></span>' +
    (W.step > 0 ? `<button class="ghost" data-act="back">${icon('chev-l')}Back</button>` : '<button class="ghost" data-act="close">Cancel</button>') +
    `<button class="primary" data-act="next" ${W.drop.scanning ? 'disabled' : ''}>${nextText}</button>`;
}

function render(): void {
  if (!W) return;
  const a = document.activeElement;
  const fk = a instanceof HTMLElement ? a.dataset['fk'] : undefined;
  const selStart = fk && (a instanceof HTMLInputElement || a instanceof HTMLTextAreaElement) ? a.selectionStart : null;
  const body = $req('lw-body');
  const keepScroll = body.scrollTop;
  $req('lw-title').textContent = W.mode === 'edit' ? `Edit “${W.orig ? W.orig.name : ''}”` : 'New labeling session';
  $req('lw-steps').innerHTML = stepsHTML();
  body.innerHTML = stepBody();
  $req('lw-foot').innerHTML = footHTML();
  body.scrollTop = keepScroll;
  const want = W.focus || fk;
  W.focus = null;
  if (want) {
    const el = $req('lw').querySelector<HTMLElement>(`[data-fk="${CSS.escape(want)}"]`);
    if (el) {
      el.focus();
      if (selStart != null && want === fk && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) try { el.setSelectionRange(selStart, selStart); } catch (e) { /* not a text field */ }
    }
  }
}

// progress painted in place (no full render per upload / poll tick)
function paintBusy(title: string | null, pct: number | null, text: string): void {
  const t = $('lw-bt'), f = $('lw-bf'), x = $('lw-bx');
  if (!f) return;
  if (title && t) t.textContent = title;
  f.parentElement?.classList.toggle('indeterminate', pct == null);
  f.style.width = pct == null ? '30%' : `${Math.min(100, pct)}%`;
  if (x) x.textContent = text;
}

// ---------------------------------------------------------------- drop / pick files
function addFiles(pairs: Array<[string, File]>): void {
  const W = wz();
  const d = W.drop;
  for (const [rel, f] of pairs) {
    const base = rel.slice(rel.lastIndexOf('/') + 1);
    if (base.startsWith('.') || !IMAGE_RE.test(base)) { d.ignored++; continue; }
    if (d.files.has(rel)) continue;
    d.files.set(rel, f);
    d.bytes += f.size;
  }
  W.dirty = true;
}

// folders are walked with webkitGetAsEntry; readEntries returns at most 100 entries per call, so it is called until empty
const isFileEntry = (en: FileSystemEntry): en is FileSystemFileEntry => en.isFile;
const isDirEntry = (en: FileSystemEntry): en is FileSystemDirectoryEntry => en.isDirectory;

async function readDrop(dt: DataTransfer): Promise<void> {
  const W = wz();
  const entries: FileSystemEntry[] = [], loose: Array<[string, File]> = [];
  for (const it of dt.items || []) {                    // every item must be taken synchronously, before any await
    if (it.kind !== 'file') continue;
    const en = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
    if (en) entries.push(en);
    else { const f = it.getAsFile(); if (f) loose.push([f.name, f]); }
  }
  if (!entries.length && !loose.length) for (const f of dt.files || []) loose.push([f.name, f]);
  const out = loose;
  const d = W.drop;
  d.scanning = 1;
  render();
  const fileOf = (en: FileSystemFileEntry): Promise<File> => new Promise((res, rej) => en.file(res, rej));
  const readAll = async (dir: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> => {
    const reader = dir.createReader();
    const all: FileSystemEntry[] = [];
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) return all;
      all.push(...batch);
    }
  };
  const walk = async (en: FileSystemEntry): Promise<void> => {
    if (isFileEntry(en)) {
      if (en.name.startsWith('.') || !IMAGE_RE.test(en.name)) { d.ignored++; return; }
      out.push([en.fullPath.replace(/^\/+/, ''), await fileOf(en)]);
      if (out.length % 200 === 0) { d.scanning = out.length; const el = $('lw-scan'); if (el) el.textContent = `Reading folders… ${fmt(out.length)} files`; }
    } else if (isDirEntry(en)) {
      await Promise.all((await readAll(en)).map(walk));
    }
  };
  try {
    await Promise.all(entries.map(walk));
    addFiles(out);
  } catch (e) { W.error = `Could not read the dropped files: ${errText(e)}`; }
  d.scanning = 0;
  render();
}

// ---------------------------------------------------------------- browsers (local, S3, FTP / SFTP)
async function localBrowse(path: string | null | undefined): Promise<void> {
  const L = wz().local;
  L.status = { kind: 'busy', text: 'reading folder…' };
  render();
  try {
    const b = await getJSON<WBrowse>(`api/fs/browse?images=1&path=${encodeURIComponent(path || '')}`);
    L.browse = b;
    L.path = b.path;
    L.status = null;
  } catch (e) { L.status = { kind: 'err', text: errText(e) }; }
  render();
}

async function s3Test(): Promise<void> {
  const S = wz().s3;
  S.status = { kind: 'busy', text: 'connecting…' };
  S.buckets = S.browse = null;
  render();
  try {
    const r = await postJSON<{ buckets: string[] }>('api/connections/test', { id: S.connId });
    S.buckets = r.buckets;
    if (!r.buckets.includes(S.bucket)) { S.bucket = r.buckets[0] || ''; S.prefix = ''; }
    S.status = r.buckets.length ? null : { kind: 'err', text: 'This connection sees no buckets.' };
    if (S.bucket) { await s3Browse(); return; }
  } catch (e) { S.status = { kind: 'err', text: errText(e) }; }
  render();
}

async function s3Browse(): Promise<void> {
  const S = wz().s3;
  S.status = { kind: 'busy', text: `listing ${S.bucket}/${S.prefix || ''}…` };
  render();
  try {
    S.browse = await postJSON<WBrowse>('api/connections/browse', { connection: { id: S.connId }, bucket: S.bucket, prefix: S.prefix || '' });
    S.status = null;
  } catch (e) { S.browse = null; S.status = { kind: 'err', text: errText(e) }; }
  render();
}

const remoteConn = (): ConnDraft => { const R = wz().remote; return R.connId === 'new' ? { ...R.conn } : { id: R.connId }; };

async function remoteBrowse(path: string | null): Promise<void> {
  const R = wz().remote;
  if (!R.connId) { R.status = { kind: 'err', text: 'Pick a connection, or add a new one.' }; render(); return; }
  R.status = { kind: 'busy', text: 'connecting…' };
  render();
  try {
    R.browse = await postJSON<WBrowse>('api/connections/browse', { connection: remoteConn(), path: path == null ? (R.browse ? R.browse.path : '') : path });
    R.status = null;
  } catch (e) { R.status = { kind: 'err', text: errText(e) }; }
  render();
}

const joinPath = (a: unknown, b: string): string => `${String(a || '').replace(/\/+$/, '')}/${b}`;

// ---------------------------------------------------------------- tasks / classes edits
function addClasses(t: WTask, names: string[]): void {
  const W = wz();
  const have = new Set(t.classes.map((c) => c.name.toLowerCase()));
  let dup = 0;
  for (const raw of names) {
    const nm = raw.trim();
    if (!nm) continue;
    if (have.has(nm.toLowerCase())) { dup++; continue; }
    have.add(nm.toLowerCase());
    t.classes.push({ key: uid(), name: nm, color: null, custom: false, shown: '' });
  }
  t.note = dup ? `${dup} duplicate${dup === 1 ? '' : 's'} skipped (names are case-insensitive)` : '';
  W.dirty = true;
}

function moveTask(key: string | undefined, to: number): void {
  const W = wz();
  const i = W.tasks.findIndex((t) => t.key === key);
  if (i < 0 || to < 0 || to >= W.tasks.length || to === i) return;
  const [t] = W.tasks.splice(i, 1);
  if (!t) return;
  W.tasks.splice(to, 0, t);
  W.dirty = true;
}

// ---------------------------------------------------------------- create / save
function taskConfig(t: WTask): TaskConfig {
  if (t.task_type === 'keypoints') return { keypoints: lines(t.kpText), flip_idx: flipIdx(t).idx };
  if (t.task_type === 'classification') return { multi_label: !!t.multi };
  return {};
}

/** The export default sent to the server (project.export_default), null = none. */
type ExportBody = { kind: string; path?: string; connection_id?: string; bucket?: string; prefix?: string };
function exportDefault(): ExportBody | null {
  const x = wz().exp;
  if (!x.kind) return null;
  if (x.kind === 'zip') return { kind: 'zip' };
  if (x.kind === 'local') return { kind: 'local', path: x.path.trim() };
  if (x.kind === 's3') return { kind: 's3', connection_id: x.connId, bucket: x.bucket.trim(), prefix: x.prefix.trim() };
  if (x.kind === 'db') return { kind: 'db', connection_id: x.connId };
  return { kind: x.kind, connection_id: x.connId, path: x.path.trim() };
}

/** The `source` of POST api/label/projects. */
type SourceBody = { kind: string; path?: string; recursive?: boolean; connection_id?: string; bucket?: string; prefix?: string };
async function buildSource(): Promise<SourceBody> {
  const W = wz();
  if (W.tab === 'local') return { kind: 'local', path: W.local.browse?.path ?? '', recursive: !!W.local.recursive };
  if (W.tab === 's3') return { kind: 's3', connection_id: W.s3.connId, bucket: W.s3.bucket, prefix: W.s3.prefix || '' };
  if (W.tab === 'remote') {
    const R = W.remote;
    if (R.connId === 'new') {                            // the inline connection is saved now: the source needs its id
      const c = await postJSON<ConnItem>('api/connections', R.conn);
      R.connId = c.id;
      W.conns.push(c);
    }
    return { kind: connKind(W.conns.find((c) => c.id === R.connId) || R.conn), connection_id: R.connId, path: R.browse?.path ?? '' };
  }
  return { kind: 'upload' };
}

function setBusy(title: string, opts: Partial<WBusy> = {}): void {
  wz().busy = { title, text: '', indeterminate: true, ...opts };
  render();
}

// one multipart request; resolves {saved, skipped}; rejects (with .cancelled when aborted)
class CancelledError extends Error { readonly cancelled = true; }
type Batch = Array<[string, File]>;
function sendBatch(pid: string, batch: Batch, onProgress: (loaded: number) => void): Promise<{ saved?: number; skipped?: number }> {
  const W = wz();
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const [rel, f] of batch) { fd.append('files', f, f.name); fd.append('rel_paths', rel); }
    const xhr = new XMLHttpRequest();
    const up = W.up;
    up?.xhrs.add(xhr);
    const done = (): void => { up?.xhrs.delete(xhr); };
    xhr.open('POST', `api/label/projects/${encodeURIComponent(pid)}/upload`);
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      done();
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); } catch (e) { resolve({ saved: batch.length, skipped: 0 }); }
      } else reject(new Error(`upload: ${xhr.status} ${xhr.responseText}`));
    };
    xhr.onerror = () => { done(); reject(new Error('upload: network error')); };
    xhr.onabort = () => { done(); reject(new CancelledError('cancelled')); };
    xhr.send(fd);
  });
}

// 3 requests in flight, each <= 8 files or 16 MB; a failed request is retried twice; what was sent is remembered
// in W.drop.sent so a retry continues with the rest
async function upload(pid: string): Promise<void> {
  const W = wz();
  const d = W.drop;
  const todo = [...d.files].filter(([rel]) => !d.sent.has(rel));
  if (!todo.length) return;
  const batches: Batch[] = [];
  let cur: Batch = [], size = 0;
  for (const it of todo) {
    if (cur.length && (cur.length >= UP.files || size + it[1].size > UP.bytes)) { batches.push(cur); cur = []; size = 0; }
    cur.push(it);
    size += it[1].size;
  }
  if (cur.length) batches.push(cur);
  const totalBytes = todo.reduce((s, [, f]) => s + f.size, 0);
  const up: WUpload = { xhrs: new Set(), live: new Map(), files: 0, bytes: 0, saved: 0, skipped: 0, t0: performance.now(), stop: false, frame: 0 };
  W.up = up;
  setBusy('Uploading images', { cancel: true, indeterminate: false });
  const paint = (): void => {
    up.frame = 0;
    let bytes = up.bytes;
    for (const v of up.live.values()) bytes += v;
    const secs = (performance.now() - up.t0) / 1000;
    const rate = secs > 0.5 ? bytes / secs : 0;
    paintBusy(null, (100 * bytes) / (totalBytes || 1), `${fmt(up.files)} / ${fmt(todo.length)} files · ${fmtBytes(bytes)} / ${fmtBytes(totalBytes)}` +
      (rate ? ` · ${fmtBytes(rate)}/s · ${fmtEta((totalBytes - bytes) / rate)}` : ''));
  };
  const schedule = (): void => { if (!up.frame) up.frame = requestAnimationFrame(paint); };
  paint();
  const queue = batches.slice();
  const worker = async (): Promise<void> => {
    while (queue.length && !up.stop) {
      const b = queue.shift();
      if (!b) break;
      const bsize = b.reduce((s, [, f]) => s + f.size, 0);
      for (let attempt = 0; ; attempt++) {
        try {
          const r = await sendBatch(pid, b, (loaded) => { up.live.set(b, Math.min(loaded, bsize)); schedule(); });
          up.live.delete(b);
          up.files += b.length;
          up.bytes += bsize;
          up.saved += r.saved || 0;
          up.skipped += r.skipped || 0;
          for (const [rel] of b) d.sent.add(rel);
          schedule();
          break;
        } catch (e) {
          up.live.delete(b);
          if (e instanceof CancelledError || up.stop || attempt >= UP.retries) { up.stop = true; throw e; }
          await sleep(700 * (attempt + 1));
        }
      }
    }
  };
  const res = await Promise.allSettled(Array.from({ length: Math.min(UP.parallel, batches.length) }, worker));
  if (up.frame) cancelAnimationFrame(up.frame);
  const bad = res.find((r) => r.status === 'rejected');
  if (bad) throw bad.reason;
}

async function watchImport(jid: number): Promise<void> {
  setBusy('Importing images', { closable: true });
  await waitJob(jid, (j) => {
    const f = $('lw-bf');
    if (f) setBar(f, j);
    const x = $('lw-bx');
    if (x) x.textContent = `${j.status === 'queued' ? 'queued (another job is running)' : j.stage || ''}` +
      (j.total ? ` · ${fmt(j.done)} / ${fmt(j.total)}${j.extra ? ` · ${j.extra}` : ''} ${fmtEta(j.eta)}` : j.extra ? ` · ${j.extra}` : '');
  });
}

async function watchJob(jid: number, title: string): Promise<Job> {
  setBusy(title, { closable: true });
  return waitJob(jid, (j) => {
    const f = $('lw-bf');
    if (f) setBar(f, j);
    const x = $('lw-bx');
    if (x) x.textContent = `${j.stage || j.status}${j.total ? ` · ${fmt(j.done)} / ${fmt(j.total)}` : ''}${j.extra ? ` · ${j.extra}` : ''}`;
  });
}

async function create(): Promise<void> {
  const W = wz();
  W.error = null;
  assignColors();
  let pid = W.pid;
  try {
    const source = W.created ? { kind: W.srcKind } : await buildSource();
    W.srcKind = source.kind;
    if (!pid) {
      setBusy('Creating the session');
      const body: { name: string; description: string; source: SourceBody; review: boolean; tasks: unknown[]; export_default?: ExportBody } = {
        name: W.name.trim(), description: W.desc.trim(), source, review: !!W.review,
        tasks: W.tasks.map((t) => ({ name: t.name.trim(), task_type: t.task_type, config: taskConfig(t), classes: t.classes.map((c) => ({ name: c.name, color: c.shown })) })) };
      const exp = exportDefault();
      if (exp) body.export_default = exp;
      const r = await postJSON<{ project: { id: string; tasks?: Array<{ id: string }> }; job_id: number | null }>('api/label/projects', body);
      pid = W.pid = r.project.id;
      W.taskIds = (r.project.tasks || []).map((t) => t.id);
      W.created = true;
      W.jobId = r.job_id;
      document.dispatchEvent(new CustomEvent('sessions-changed'));
    } else if (!W.jobId || W.jobFailed) W.jobId = null;
    if (source.kind === 'upload') {
      if (W.drop.files.size) await upload(pid);
      if (!W.jobId) {
        setBusy('Starting the import');
        W.jobId = (await postJSON<JobStarted>(`api/label/projects/${encodeURIComponent(pid)}/import`, {})).job_id;
      }
    } else if (!W.jobId) {
      W.jobId = (await postJSON<JobStarted>(`api/label/projects/${encodeURIComponent(pid)}/import`, {})).job_id;
    }
    W.jobFailed = false;
    if (W.jobId) {
      try { await watchImport(W.jobId); } catch (e) { W.jobFailed = true; throw e; }
    }
    if (W.impOn && !W.annDone) {                         // after the images: their names are what the labels match
      const k = W.tasks.findIndex((t) => t.key === W.imp.taskKey);
      const tid = W.taskIds && W.taskIds[Math.max(0, k)];
      if (tid) {
        setBusy('Importing the existing labels');
        const j = await watchJob(await startImport(pid, tid, W.imp, W.conns, null), 'Importing the existing labels');
        W.annDone = true;
        setBusy('Opening the session', { text: resultText(j.result) });
      }
    }
    W.dirty = false;
    setBusy('Opening the session');
    location.href = `./?label=${encodeURIComponent(pid)}`;
  } catch (e) {
    const d = W.drop;
    W.busy = null;
    W.error = e instanceof CancelledError ? `Upload cancelled: ${fmt(d.sent.size)} of ${fmt(d.files.size)} images are on the server. Retry continues with the rest.`
      : (W.created ? 'The session was created, but: ' : '') + errText(e);
    render();
  }
}

async function saveEdit(): Promise<void> {
  const W = wz();
  const pid = W.pid, P = W.orig;
  if (pid === null || !P) return;
  const enc = encodeURIComponent(pid);
  const req = async <T = unknown>(path: string, method: string, body: unknown): Promise<T> => {
    const r = await api(`api/label/projects/${enc}${path}`, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const out: T = await r.json();
    return out;
  };
  W.error = null;
  assignColors();
  setBusy('Saving');
  try {
    const patch: { name?: string; description?: string; review?: boolean; export_default?: ExportBody | null } = {};
    if (W.name.trim() !== P.name) patch.name = W.name.trim();
    if (W.desc.trim() !== (P.description || '')) patch.description = W.desc.trim();
    if (!!W.review !== !!P.review) patch.review = !!W.review;
    const exp = exportDefault();
    if (JSON.stringify(exp) !== JSON.stringify(P.export_default || null)) patch.export_default = exp;
    if (Object.keys(patch).length) await req('', 'PATCH', patch);
    for (const t of W.tasks) {
      if (!t.id) {
        const nt = await req<{ id: string }>('/tasks', 'POST', { name: t.name.trim(), task_type: t.task_type, config: taskConfig(t),
          classes: t.classes.map((c) => ({ name: c.name, color: c.shown })) });
        t.id = nt.id;
        continue;
      }
      const tp: { name?: string; config?: TaskConfig } = {};
      if (t.name.trim() !== t.origName) tp.name = t.name.trim();
      if (t.task_type === 'classification' && !!t.multi !== !!(t.config || {}).multi_label) tp.config = { ...(t.config || {}), multi_label: !!t.multi };
      if (Object.keys(tp).length) await req(`/tasks/${encodeURIComponent(t.id)}`, 'PATCH', tp);
      const added = t.classes.filter((c) => !c.id);
      if (added.length) {
        const r = await req<{ classes?: Array<{ id: string; color: string }> }>(`/tasks/${encodeURIComponent(t.id)}/classes`, 'POST', { classes: added.map((c) => ({ name: c.name, color: c.shown })) });
        (r.classes || []).forEach((c, i) => { const a = added[i]; if (a) { a.id = c.id; a.color = a.origColor = c.color; } });
      }
      for (const c of t.classes) {
        if (c.id && c.color && c.origColor && c.color !== c.origColor) { await req(`/classes/${encodeURIComponent(c.id)}`, 'PATCH', { color: c.color }); c.origColor = c.color; }
      }
    }
    const order = W.tasks.map((t) => t.id);
    if (order.join() !== (P.tasks || []).map((t) => t.id).join()) await req('/tasks/reorder', 'POST', { ids: order });
    W.dirty = false;
    document.dispatchEvent(new CustomEvent('sessions-changed'));
    closeWizard(true);
  } catch (e) {
    W.busy = null;
    W.error = errText(e);
    render();
  }
}

// ---------------------------------------------------------------- navigation
function next(): void {
  const W = wz();
  if (W.busy) return;
  if (W.created) { void create(); return; }
  const id = W.steps[W.step];
  if (!id) return;
  W.tried[id] = true;
  W.error = null;
  if (stepErrors(id).length) { render(); return; }
  if (W.step < W.steps.length - 1) {
    W.step++;
    const nextId = W.steps[W.step];
    const first = W.tasks[0];
    const focus: Record<StepId, string | null> = { name: 'name', images: null, tasks: first ? `cin-${first.key}` : null, labels: null, export: 'exp-kind' };
    W.focus = nextId ? focus[nextId] : null;
    render();
    if (!W.focus && nextId === 'images') { const t = $(`lw-tab-${W.tab}`); if (t) t.focus(); }
    return;
  }
  // every step must be valid before creating
  const bad = W.steps.findIndex((s) => stepErrors(s).length);
  if (bad >= 0) { W.step = bad; const bs = W.steps[bad]; if (bs) W.tried[bs] = true; render(); return; }
  if (W.mode === 'edit') void saveEdit(); else void create();
}

type Act = (b: HTMLElement) => void;
const dataTk = (b: Element): string | undefined => b.closest<HTMLElement>('[data-tk]')?.dataset['tk'];

const WACT: Record<string, Act> = {
  next,
  back: () => { const W = wz(); if (W.step > 0) { W.step--; W.error = null; render(); } },
  goto: (b) => { const W = wz(); W.step = Number(b.dataset['i']); W.error = null; render(); },
  close: () => closeWizard(false),
  'open-now': () => { location.href = `./?label=${encodeURIComponent(wz().pid ?? '')}`; },
  'imp-on': (b) => { const W = wz(); W.impOn = b.dataset['v'] === '1'; W.error = null; render(); },
  tab: (b) => {
    const W = wz();
    const tab = b.dataset['tab'];
    if (!isSrcTab(tab)) return;
    W.tab = tab;
    W.error = null;
    render();
    const t = $(`lw-tab-${W.tab}`);
    if (t) t.focus();
    onTab();
  },
  'pick-files': () => $req('lw-files').click(),
  'pick-folder': () => $req('lw-folder').click(),
  'drop-clear': () => { const W = wz(); W.drop.files.clear(); W.drop.bytes = 0; W.drop.ignored = 0; render(); },
  'local-go': () => { void localBrowse(wz().local.path); },
  'local-up': () => { void localBrowse(wz().local.browse?.parent); },
  'local-crumb': (b) => { void localBrowse('/' + (b.dataset['to'] ?? '')); },
  'local-folder': (b) => { void localBrowse(joinPath(wz().local.browse?.path, b.dataset['name'] ?? '')); },
  's3-go': () => { if (wz().s3.bucket) void s3Browse(); },
  's3-crumb': (b) => { wz().s3.prefix = b.dataset['to'] ?? ''; void s3Browse(); },
  's3-folder': (b) => { const S = wz().s3; S.prefix = [S.prefix, b.dataset['name']].filter(Boolean).join('/'); void s3Browse(); },
  'remote-kind': (b) => {
    const W = wz();
    const old = W.remote.conn;
    W.remote.conn = { ...connDefaults(b.dataset['kind'] ?? ''), name: old.name, host: old.host, username: old.username, base_path: old.base_path };
    W.remote.browse = null;
    render();
  },
  'remote-go': () => { void remoteBrowse(null); },
  'remote-up': () => { void remoteBrowse(wz().remote.browse?.parent ?? null); },
  'remote-crumb': (b) => { void remoteBrowse('/' + (b.dataset['to'] ?? '')); },
  'remote-folder': (b) => { void remoteBrowse(joinPath(wz().remote.browse?.path, b.dataset['name'] ?? '')); },
  't-add': () => {
    const W = wz();
    const used = new Set(W.tasks.map((t) => t.name.toLowerCase()));
    let n = 2;
    const name = 'task';
    while (used.has(`${name}${n}`)) n++;
    const t = newTask('object_detection', W.tasks.length ? `${name}${n}` : 'detect');
    W.tasks.push(t);
    W.dirty = true;
    W.focus = `tn-${t.key}`;
    render();
  },
  't-rm': (b) => {
    const W = wz();
    const t = findTask(dataTk(b));
    if (t.classes.length && !confirm(`Remove the task “${t.name}” and its ${t.classes.length} classes?`)) return;
    W.tasks.splice(W.tasks.indexOf(t), 1);
    render();
  },
  't-up': (b) => { const W = wz(); const k = dataTk(b); moveTask(k, W.tasks.findIndex((t) => t.key === k) - 1); render(); },
  't-down': (b) => { const W = wz(); const k = dataTk(b); moveTask(k, W.tasks.findIndex((t) => t.key === k) + 1); render(); },
  'c-rm': (b) => {
    const W = wz();
    const t = findTask(dataTk(b));
    const ck = b.closest<HTMLElement>('[data-ck]')?.dataset['ck'];
    t.classes = t.classes.filter((c) => c.key !== ck);
    W.focus = `cin-${t.key}`;
    render();
  },
  'up-cancel': () => {
    const W = wz();
    if (!W.up) return;
    W.up.stop = true;
    for (const x of [...W.up.xhrs]) x.abort();
  },
};

type Chg = (el: HTMLElement) => void;
const WCHG: Record<string, Chg> = {
  files: (el) => {
    if (!(el instanceof HTMLInputElement)) return;
    addFiles([...(el.files ?? [])].map((f): [string, File] => [f.webkitRelativePath || f.name, f]));
    el.value = '';
    render();
  },
  's3-conn': () => { if (wz().s3.connId) void s3Test(); },
  's3-bucket': () => { wz().s3.prefix = ''; void s3Browse(); },
  'remote-conn': () => { const W = wz(); W.remote.browse = null; W.remote.status = null; render(); if (W.remote.connId !== 'new') void remoteBrowse(''); },
  'exp-kind': () => { wz().exp.connId = ''; render(); },
  'task-type': () => render(),
};

// the first visit of a tab loads what it shows
function onTab(): void {
  const W = wz();
  if (W.tab === 'local' && !W.local.browse && !W.local.status) void localBrowse(W.local.path);
  if (W.tab === 's3' && !W.s3.connId) {
    const c = W.conns.find((x) => connKind(x) === 's3');
    if (c) { W.s3.connId = c.id; void s3Test(); }
  }
  if (W.tab === 'remote' && !W.remote.connId) {
    const c = W.conns.find((x) => { const k = connKind(x); return k === 'ftp' || k === 'sftp'; });
    W.remote.connId = c ? c.id : 'new';
    render();
    if (c) void remoteBrowse('');
  }
}

// ---------------------------------------------------------------- input binding
type FormEl = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
const isFormEl = (el: unknown): el is FormEl => el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement;
const isCheck = (el: FormEl): el is HTMLInputElement => el instanceof HTMLInputElement && el.type === 'checkbox';

function write(el: EventTarget | null): void {
  if (!W || !isFormEl(el)) return;
  const tkEl = el.closest<HTMLElement>('[data-tk]');
  const tf = el.dataset['tf'];
  if (tf && tkEl) {
    const t = findTask(tkEl.dataset['tk']);
    if (tf === 'multi') t.multi = isCheck(el) ? el.checked : false;
    else if (tf === 'name') t.name = el.value;
    else if (tf === 'kpText') t.kpText = el.value;
    else if (tf === 'flipText') t.flipText = el.value;
    else if (tf === 'task_type' && isTaskType(el.value)) t.task_type = el.value;
    W.dirty = true;
    return;
  }
  const path = el.dataset['f'];
  if (!path) return;
  let val: string | boolean | number | null = isCheck(el) ? el.checked : el.value;
  if (el.dataset['type'] === 'int') val = el.value.trim() === '' ? null : Number(el.value);
  const keys = path.split('.');
  let o: Record<string, unknown> = keys[0] === 'conn' ? W.remote.conn : W;
  for (const k of keys.slice(keys[0] === 'conn' ? 1 : 0, -1)) {
    const nxt = o[k];
    if (!isRec(nxt)) return;
    o = nxt;
  }
  const last = keys[keys.length - 1];
  if (last !== undefined) o[last] = val;
  W.dirty = true;
}

function onKeydown(e: KeyboardEvent): void {
  if (!W) return;
  const tg = e.target;
  if (!(tg instanceof HTMLElement)) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    if (!W.busy || W.busy.closable) closeWizard(false);
    return;
  }
  if (e.key === 'Tab') {                                // keep focus inside the dialog
    const box = $req('lw-box');
    const f = [...box.querySelectorAll<HTMLElement>('button:not(:disabled), input:not([type=file]):not(:disabled), select:not(:disabled), textarea:not(:disabled)')]
      .filter((x) => x.offsetParent);
    const first = f[0], last = f[f.length - 1];
    if (!first || !last) return;
    if (e.shiftKey && (document.activeElement === first || document.activeElement === box)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    return;
  }
  // tabs: arrows move between them
  if (tg.getAttribute('role') === 'tab' && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
    e.preventDefault();
    const curTab = W.tab;
    const i = SRC_TABS.findIndex(([id]) => id === curTab);
    const to = SRC_TABS[(i + (e.key === 'ArrowRight' ? 1 : -1) + SRC_TABS.length) % SRC_TABS.length];
    if (to) W.tab = to[0];
    const tabId = W.tab;
    render();
    $(`lw-tab-${tabId}`)?.focus();
    onTab();
    return;
  }
  if (tg.dataset['cin'] && tg instanceof HTMLInputElement) {   // the class entry: Enter adds, Backspace on empty removes the last new one
    const t = findTask(dataTk(tg));
    if (e.key === 'Enter' && !e.isComposing && !(e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (tg.value.trim()) { addClasses(t, [tg.value]); tg.value = ''; W.focus = `cin-${t.key}`; render(); }
      return;
    }
    if (e.key === 'Backspace' && !tg.value) {
      const lastNew = [...t.classes].reverse().find((c) => !c.id);
      if (lastNew) { e.preventDefault(); t.classes.splice(t.classes.indexOf(lastNew), 1); W.focus = `cin-${t.key}`; render(); }
      return;
    }
  }
  if (e.key !== 'Enter' || e.isComposing || e.shiftKey || e.altKey) return;
  if (tg.tagName === 'TEXTAREA' && !(e.ctrlKey || e.metaKey)) return;
  if (tg.tagName === 'BUTTON' || (tg instanceof HTMLInputElement && ['checkbox', 'radio', 'file', 'color'].includes(tg.type))) return;
  e.preventDefault();
  write(tg);
  const enter = tg.dataset['enter'];
  if (enter && !(e.ctrlKey || e.metaKey)) WACT[enter]?.(tg);
  else next();
}

function onPaste(e: ClipboardEvent): void {
  const tg = e.target;
  if (!W || !(tg instanceof HTMLInputElement) || !tg.dataset['cin']) return;
  const text = e.clipboardData?.getData('text') ?? '';
  if (!/[\r\n]/.test(text)) return;                     // one line: a normal paste into the field
  e.preventDefault();
  const t = findTask(dataTk(tg));
  addClasses(t, (tg.value + text).split(/\r?\n/));
  W.focus = `cin-${t.key}`;
  render();
}

function bind(): void {
  if (bound) return;
  bound = true;
  const box = $req('lw');
  box.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-act]') : null;
    const act = b ? WACT[b.dataset['act'] ?? ''] : undefined;
    if (b && !(b instanceof HTMLButtonElement && b.disabled) && act) act(b);
  });
  box.addEventListener('input', (e) => {
    const t = e.target;
    if (t instanceof HTMLElement && t.dataset['color']) {   // live swatch while the picker is open; the render waits for change
      const sw = t.closest<HTMLElement>('.lw-sw');
      if (sw && t instanceof HTMLInputElement) sw.style.setProperty('--c', t.value);
      return;
    }
    write(t);
  });
  box.addEventListener('change', (e) => {
    const el = e.target;
    if (!(el instanceof HTMLElement)) return;
    if (el.dataset['color'] && el instanceof HTMLInputElement) {
      const t = findTask(dataTk(el));
      const ck = el.closest<HTMLElement>('[data-ck]')?.dataset['ck'];
      const c = t.classes.find((x) => x.key === ck);
      if (!c) return;
      c.color = el.value;
      c.custom = true;
      wz().dirty = true;
      render();
      return;
    }
    const chg = el.dataset['change'];
    if (chg !== 'files') write(el);
    if (chg) WCHG[chg]?.(el);
  });
  box.addEventListener('keydown', onKeydown);
  box.addEventListener('paste', onPaste);
  attachImport(box, {
    get: () => (W && W.steps[W.step] === 'labels' ? W.imp : null), render, ctx: importCtx,
    onPreview: (p: ImportPreview, t: ImportTask | undefined) => {   // the classes the labels use join the task (same names are kept)
      if (!t) return;
      const names = (p.classes || []).map((c) => c.name).filter((n) => !n.startsWith('#'));
      if (names.length) { addClasses(findTask(t.key), names); wz().dirty = true; }
    },
  });
  box.addEventListener('mousedown', (e) => { if (e.target === box && (!W || !W.busy || W.busy.closable)) closeWizard(false); });
  $req('lw-close').onclick = () => { if (!W || !W.busy || W.busy.closable) closeWizard(false); };
  // files dropped anywhere on the dialog never navigate away; on the drop zone they are added
  box.addEventListener('dragover', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (W && W.drag) {                                 // a task row being reordered
      const row = t?.closest<HTMLElement>('.lw-task');
      e.preventDefault();
      for (const r of box.querySelectorAll('.lw-task.drop-before, .lw-task.drop-after')) r.classList.remove('drop-before', 'drop-after');
      if (row && row.dataset['tk'] !== W.drag) {
        const r = row.getBoundingClientRect();
        row.classList.add(e.clientY < r.top + r.height / 2 ? 'drop-before' : 'drop-after');
      }
      return;
    }
    e.preventDefault();
    const zone = t?.closest('.lw-drop') ?? null;
    if (e.dataTransfer) e.dataTransfer.dropEffect = zone ? 'copy' : 'none';
    for (const z of box.querySelectorAll('.lw-drop.over')) if (z !== zone) z.classList.remove('over');
    if (zone) zone.classList.add('over');
  });
  box.addEventListener('dragleave', (e) => {
    const z = e.target instanceof Element ? e.target.closest('.lw-drop') : null;
    if (z && !(e.relatedTarget instanceof Node && z.contains(e.relatedTarget))) z.classList.remove('over');
  });
  box.addEventListener('drop', (e) => {
    e.preventDefault();
    const t = e.target instanceof Element ? e.target : null;
    if (W && W.drag) {
      const row = t?.closest<HTMLElement>('.lw-task');
      const key = W.drag;
      W.drag = null;
      if (row && row.dataset['tk'] !== key) {
        const r = row.getBoundingClientRect();
        const from = W.tasks.findIndex((x) => x.key === key);
        let to = W.tasks.findIndex((x) => x.key === row.dataset['tk']) + (e.clientY < r.top + r.height / 2 ? 0 : 1);
        if (from < to) to--;
        moveTask(key, to);
      }
      render();
      return;
    }
    const zone = t?.closest('.lw-drop');
    if (zone && e.dataTransfer) { zone.classList.remove('over'); void readDrop(e.dataTransfer); }
  });
  box.addEventListener('dragstart', (e) => {
    const grip = e.target instanceof Element ? e.target.closest('.lw-grip') : null;
    if (!grip || !W) return;
    const row = grip.closest<HTMLElement>('.lw-task');
    if (!row) return;
    W.drag = row.dataset['tk'] ?? null;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/x-lw-task', W.drag ?? '');
      e.dataTransfer.setDragImage(row, 24, 20);
    }
    row.classList.add('dragging');
  });
  box.addEventListener('dragend', () => {
    if (!W) return;
    if (W.drag) { W.drag = null; render(); }
  });
}

// ---------------------------------------------------------------- open / close
function closeWizard(force: boolean): void {
  if (!W) return;
  if (!force && W.dirty && !W.created && !confirm(W.mode === 'edit' ? 'Discard your changes?' : 'Discard this new labeling session?')) return;
  if (W.up) { W.up.stop = true; for (const x of [...W.up.xhrs]) x.abort(); }
  const created = W.created;
  $req('lw').classList.add('hidden');
  W = null;
  if (created) document.dispatchEvent(new CustomEvent('sessions-changed'));
  if (returnFocus instanceof HTMLElement && document.contains(returnFocus)) returnFocus.focus();
  returnFocus = null;
}

type OrigTask = NonNullable<OrigProject['tasks']>[number];
function taskFromProject(t: OrigTask): WTask {
  const cfg: TaskConfig = t.config || {};
  const kp = cfg.keypoints || [];
  const flip = (cfg.flip_idx || []).map((j, i) => (j > i ? `${kp[i]} ${kp[j]}` : '')).filter(Boolean);
  return { key: uid(), id: t.id, name: t.name, origName: t.name, task_type: t.task_type, config: cfg, locked: true, note: '',
    multi: !!cfg.multi_label, kpText: kp.join('\n'), flipText: flip.join('\n'),
    classes: (t.classes || []).map((c): WClass => ({ key: uid(), id: c.id, name: c.name, color: c.color, shown: c.color, origColor: c.color, custom: false })) };
}

// opts: {} = new session, {edit: pid} = rename / add tasks and classes / export default of a saved one;
// opts.names: the other label sessions' names (the name step flags a taken one before the request)
export async function openWizard(opts: WizardOpts = {}): Promise<void> {
  bind();
  returnFocus = document.activeElement;
  const w = fresh();
  W = w;
  w.names = (opts.names || []).map((n) => String(n).trim().toLowerCase());
  $req('lw').classList.remove('hidden');
  const need: Array<Promise<void>> = [
    palette ? Promise.resolve() : getJSON<{ colors: string[] }>('api/label/colors?n=64').then((r) => { palette = r.colors; }).catch(() => {}),
    getJSON<{ connections: ConnItem[] }>('api/connections').then((r) => { if (W) W.conns = r.connections; }).catch(() => {})];
  if (opts.edit) {
    w.mode = 'edit';
    w.pid = opts.edit;
    w.steps = ['name', 'tasks', 'export'];
    setBusy('Loading the session');
    try {
      const [P] = await Promise.all([getJSON<OrigProject>(`api/label/projects/${encodeURIComponent(opts.edit)}`), ...need]);
      if (!W) return;
      W.orig = P;
      W.name = P.name;
      W.desc = P.description || '';
      W.review = !!P.review;
      W.tasks = (P.tasks || []).map(taskFromProject);
      const x = P.export_default || {};
      W.exp = { kind: x.kind || '', path: x.path || '', connId: x.connection_id || '', bucket: x.bucket || '', prefix: x.prefix || '' };
      W.busy = null;
    } catch (e) {
      if (!W) return;
      W.busy = null;
      W.error = `Could not load the session: ${errText(e)}`;
    }
  } else {
    await Promise.all(need);
    if (!W) return;
  }
  W.focus = 'name';
  render();
}
