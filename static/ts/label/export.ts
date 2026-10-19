// Export dialog: openExportDialog(pid, project?) (the label page's top bar and the Sessions home cards). Task
// checkboxes, a target (zip download / local folder with a folder browser / S3 / FTP / SFTP / database, the remote
// ones from saved connections), options, a preview of the layout written, then the job's progress per task and a
// summary (+ the zip's download link). Renders from the X object; one delegated listener set on the dialog.
// v2: per-task class chips (all on by default, label counts from class-stats; the request's `classes` lists the
// kept ids of a task only when some are excluded, the server renumbers them 0..N-1), the choice is saved in the
// project's export_default, and "Re-export to <last target>" repeats it in one click (overwrite after a confirm).
// reExport(pid, project?) opens the dialog straight into that.

import { errText, getJSON, postJSON } from '../core/api.js';
import { userKey } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import type { Job } from '../types.js';
import type { LabelProject, LabelTask } from '../types-label.js';
import type { ClassStat, ClassStatsPayload, ExportBody, ExportBrowse, ExportDefault, ExportKind, ExportRun, ExportTarget, ImportConn, JobStarted } from '../types-label-ui.js';
import type { ConnectionsPayload, ConnTestResult } from '../types-settings.js';
import { closestEl } from './ui-util.js';

const KINDS: ReadonlyArray<readonly [ExportKind, string, string]> = [
  ['zip', 'Download zip', 'download'], ['local', 'Local folder', 'folder'], ['s3', 'S3', 'cloud'],
  ['ftp', 'FTP', 'ftp'], ['sftp', 'SFTP', 'server'], ['db', 'Database', 'db'],
];
const CONN_KIND: Partial<Record<ExportKind, (c: ImportConn) => boolean>> = {
  s3: (c) => !c.kind || c.kind === 's3', ftp: (c) => c.kind === 'ftp', sftp: (c) => c.kind === 'sftp',
  db: (c) => c.kind === 'postgres' || c.kind === 'mssql' };

/** The project as the dialog reads it (export_default is typed here). */
type ExportProject = LabelProject & { export_default?: ExportDefault };

interface ExportState {
  pid: string; project: ExportProject; conns: ImportConn[];
  tasks: Set<string>;                          // task ids to export
  classes: Map<string, Set<string>>;           // task id -> class ids to export
  counts: Record<string, ClassStat> | null;
  confirmRe: boolean;
  kind: ExportKind; path: string; connection_id: string; bucket: string; prefix: string;
  include_images: boolean; only_done: boolean; overwrite: boolean; create_views: boolean;
  browse: ExportBrowse | null; buckets: string[] | null;
  job: Partial<Job> | null; result: ExportRun | null; error: string | null; running: boolean;
  closed?: boolean;
}
type TextKey = 'path' | 'connection_id' | 'bucket' | 'prefix';
type FlagKey = 'include_images' | 'only_done' | 'overwrite' | 'create_views';
const isTextKey = (k: string | undefined): k is TextKey => k === 'path' || k === 'connection_id' || k === 'bucket' || k === 'prefix';
const isFlagKey = (k: string | undefined): k is FlagKey => k === 'include_images' || k === 'only_done' || k === 'overwrite' || k === 'create_views';
const kindOfValue = (v: string): ExportKind | undefined => KINDS.find((k) => k[0] === v)?.[0];

let X: ExportState | null = null;
let el: HTMLElement | null = null;
let returnFocus: Element | null = null;

/** The open dialog's state / element (the handlers only run while it is open). */
const cur = (): ExportState => { if (!X) throw new Error('The export dialog is closed'); return X; };
const dlg = (): HTMLElement => { if (!el) throw new Error('The export dialog is closed'); return el; };

const slug = (s: string | null | undefined): string => String(s || 'task').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'task';

export async function openExportDialog(pid: string, project?: LabelProject | null): Promise<void> {
  closeExport();
  returnFocus = document.activeElement;
  const root = document.createElement('div');
  el = root;
  el.id = 'lbx';
  el.className = 'modal lbx-back';
  el.innerHTML = '<div class="panel lbx" role="dialog" aria-modal="true" aria-labelledby="lbx-title" tabindex="-1"><div class="lbx-body"><div class="status-line"><div class="spin"></div>Loading…</div></div></div>';
  document.body.appendChild(el);
  bind();
  root.querySelector<HTMLElement>('.lbx')?.focus();
  try {
    const [p, conns, stats] = await Promise.all([
      project ? Promise.resolve(project as ExportProject) : getJSON<ExportProject>(`api/label/projects/${encodeURIComponent(pid)}`),
      getJSON<ConnectionsPayload>('api/connections').catch((): ConnectionsPayload => ({ connections: [] })),
      getJSON<ClassStatsPayload>(`api/label/projects/${encodeURIComponent(pid)}/class-stats`).catch((): ClassStatsPayload => ({ stats: null })),
    ]);
    const d: ExportDefault = p.export_default || {};
    const tg: Partial<ExportTarget> = d.target || (d.kind ? d : {});
    const live = (p.tasks || []).map((t) => t.id);
    const picked = (d.task_ids || []).filter((t) => live.includes(t));
    X = {
      pid, project: p, conns: conns.connections || [], tasks: new Set(picked.length ? picked : live),
      classes: keptClasses(pid, p, d.classes), counts: (stats && stats.stats) || null, confirmRe: false,
      kind: tg.kind || 'zip', path: tg.path || '', connection_id: tg.connection_id || '', bucket: tg.bucket || '',
      prefix: tg.prefix || '', include_images: d.include_images !== false, only_done: !!d.only_done, overwrite: !!d.overwrite,
      create_views: tg.create_views !== false, browse: null, buckets: null, job: null, result: null, error: null, running: false,
    };
    render();
  } catch (e) {
    const body = root.querySelector('.lbx-body');
    if (body) body.innerHTML = `<div class="status-line err">${icon('alert')}${esc(errText(e))}</div>`;
  }
}

// The saved class choice is remembered as the classes EXCLUDED last time, so a class added since is exported by
// default. The server keeps only the kept ids (export_default.classes, the API contract), and a kept list cannot tell
// "excluded" from "added later", so at each export the page stores, in this browser's localStorage per session,
// {kept: <the classes option sent>, excluded: {task id: [class ids known then but not kept]}}. It is used only while
// its `kept` still equals the server's export_default.classes (no other tab / browser exported since); without a
// matching record (another browser, a duplicated session, private mode) the dialog falls back to the kept list.
type IdLists = Record<string, string[]>;
const exclKey = (pid: string): string => userKey(`label-export-excluded-${pid}`);
const isIdLists = (v: unknown): v is IdLists => !!v && typeof v === 'object' && !Array.isArray(v);
const sameKept = (a: IdLists | undefined, b: IdLists | undefined): boolean => {
  const ka = Object.keys(a || {}), kb = Object.keys(b || {});
  if (ka.length !== kb.length) return false;
  return ka.every((t) => { const x = a?.[t], y = b?.[t]; return Array.isArray(y) && Array.isArray(x) && [...x].sort().join() === [...y].sort().join(); });
};
function readExcluded(pid: string, saved: IdLists | undefined): IdLists | null {
  try {
    const r: unknown = JSON.parse(localStorage.getItem(exclKey(pid)) || 'null');
    if (r && typeof r === 'object' && 'excluded' in r && 'kept' in r && isIdLists(r.excluded) && isIdLists(r.kept) && sameKept(r.kept, saved || {})) return r.excluded;
  } catch (e) { /* private mode, bad JSON */ }
  return null;
}
function writeExcluded(pid: string, project: ExportProject, classes: IdLists): void {
  const excluded: IdLists = {};
  for (const t of project.tasks || []) {
    const kept = classes[t.id];
    if (!Array.isArray(kept)) continue;
    excluded[t.id] = (t.classes || []).map((c) => c.id).filter((id) => !kept.includes(id));
  }
  try { localStorage.setItem(exclKey(pid), JSON.stringify({ kept: classes, excluded })); } catch (e) { /* private mode */ }
}

// task id -> Set of the class ids to export: every current class minus the ones excluded last time (see above)
function keptClasses(pid: string, p: ExportProject, saved: IdLists | undefined): Map<string, Set<string>> {
  const excluded = readExcluded(pid, saved);
  const m = new Map<string, Set<string>>();
  for (const t of p.tasks || []) {
    const all = (t.classes || []).map((c) => c.id);
    let s: string[] | null = null;
    const sv = saved?.[t.id];
    if (excluded) { const ex = excluded[t.id] || []; s = all.filter((id) => !ex.includes(id)); }
    else if (Array.isArray(sv)) s = all.filter((id) => sv.includes(id));
    m.set(t.id, new Set(s && s.length ? s : all));
  }
  return m;
}

// the request's / export_default's `classes`: only the tasks with excluded classes
function classesOption(): IdLists {
  const X = cur();
  const out: IdLists = {};
  for (const t of X.project.tasks || []) {
    if (!X.tasks.has(t.id)) continue;
    const k = X.classes.get(t.id) || new Set();
    const all = t.classes || [];
    if (k.size < all.length) out[t.id] = all.filter((c) => k.has(c.id)).map((c) => c.id);
  }
  return out;
}

const lastTarget = (): ExportTarget | null => { const d = cur().project.export_default || {}; return d.target && d.target.kind ? d.target : null; };

function targetText(tg: ExportTarget | null): string {
  const X = cur();
  if (!tg) return '';
  const conn = X.conns.find((c) => c.id === tg.connection_id);
  const cn = conn ? conn.name || conn.host || conn.id : 'connection';
  if (tg.kind === 'zip') return 'a zip download';
  if (tg.kind === 'local') return tg.path || 'a local folder';
  if (tg.kind === 's3') return `s3://${tg.bucket || ''}/${tg.prefix || ''}`;
  if (tg.kind === 'db') return `${cn} (database)`;
  return `${tg.kind}://${cn}${tg.path || '/'}`;
}

// open the dialog and start "Re-export to <last target>" at once (the overwrite confirm still shows)
export async function reExport(pid: string, project?: LabelProject | null): Promise<void> {
  await openExportDialog(pid, project);
  if (X && lastTarget()) void ACT['reexport']?.(dlg());
}

export function closeExport(): void {
  if (!el) return;
  el.remove();
  el = null;
  if (X) X.closed = true;
  X = null;
  if (returnFocus instanceof HTMLElement && document.contains(returnFocus)) returnFocus.focus();
}

// ---------------------------------------------------------------- render
function connsOf(kind: ExportKind): ImportConn[] { const f = CONN_KIND[kind]; return f ? cur().conns.filter(f) : []; }

function targetHTML(): string {
  const X = cur();
  const k = X.kind;
  if (k === 'zip') return '<p class="hint">A zip file you download when the export finishes.</p>';
  if (k === 'local') {
    const b = X.browse;
    const parts = b ? b.path.split('/').filter(Boolean) : [];
    return `<label class="lbx-row"><span>Folder</span><input data-f="path" value="${esc(X.path)}" placeholder="/data/exports/my-session" spellcheck="false"></label>
      <div class="lbx-row"><span></span><button data-x="browse">${icon('folder')}Browse…</button>${b ? `<button data-x="pick">${icon('check')}Use this folder</button>` : ''}</div>
      ${b ? `<div class="browser"><div class="crumbs"><button data-x="crumb" data-to="/">/</button>${parts.map((p, i) => `<span class="sep">/</span><button data-x="crumb" data-to="/${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</button>`).join('')}</div>
        <div class="folders">${b.parent ? `<button class="folder" data-x="up">${icon('up')}<span class="name">..</span></button>` : ''}${(b.folders || []).map((f) =>
          `<button class="folder" data-x="open" data-name="${esc(f.name)}">${icon('folder')}<span class="name">${esc(f.name)}</span>${f.images ? `<span class="hint num">${f.images} img</span>` : ''}</button>`).join('') || '<div class="none">no sub-folders here</div>'}</div></div>` : ''}`;
  }
  const cs = connsOf(k);
  const sel = `<label class="lbx-row"><span>Connection</span><select data-f="connection_id" data-rerender="1"><option value="">Choose…</option>${cs.map((c) =>
    `<option value="${esc(c.id)}"${c.id === X.connection_id ? ' selected' : ''}>${esc(c.name || c.host || c.endpoint || c.id)}${c.kind && k === 'db' ? ` (${esc(c.kind)})` : ''}</option>`).join('')}</select></label>`;
  const none = cs.length ? '' : `<p class="hint">No saved ${esc(KINDS.find((x) => x[0] === k)?.[1] ?? k)} connection yet. <button class="lbx-link" data-x="conns">Add one in Settings › Connections</button>.</p>`;
  if (k === 's3') {
    return sel + none + `<label class="lbx-row"><span>Bucket</span><input data-f="bucket" list="lbx-buckets" value="${esc(X.bucket)}" spellcheck="false">
      <datalist id="lbx-buckets">${(X.buckets || []).map((b) => `<option value="${esc(b)}">`).join('')}</datalist></label>
      <label class="lbx-row"><span>Prefix</span><input data-f="prefix" value="${esc(X.prefix)}" placeholder="exports/my-session/" spellcheck="false"></label>`;
  }
  if (k === 'ftp' || k === 'sftp') {
    const b = X.browse;
    return sel + none + `<label class="lbx-row"><span>Folder</span><input data-f="path" value="${esc(X.path)}" placeholder="/exports/my-session" spellcheck="false"></label>
      ${X.connection_id ? `<div class="lbx-row"><span></span><button data-x="rbrowse">${icon('folder')}Browse…</button>${b ? `<button data-x="pick">${icon('check')}Use this folder</button>` : ''}</div>` : ''}
      ${b ? `<div class="browser"><div class="crumbs"><span class="num">${esc(b.path || '/')}</span></div><div class="folders">${b.parent != null ? `<button class="folder" data-x="rup">${icon('up')}<span class="name">..</span></button>` : ''}${(b.folders || []).map((f) =>
        `<button class="folder" data-x="ropen" data-name="${esc(f.name)}">${icon('folder')}<span class="name">${esc(f.name)}</span></button>`).join('') || '<div class="none">no sub-folders here</div>'}</div></div>` : ''}`;
  }
  return sel + none + `<label class="check"><input type="checkbox" data-f="create_views"${X.create_views ? ' checked' : ''}>Create one SQL view per task</label>`;
}

function treeHTML(): string {
  const X = cur();
  const tasks = (X.project.tasks || []).filter((t) => X.tasks.has(t.id));
  const conn = X.conns.find((c) => c.id === X.connection_id);
  if (X.kind === 'db') {
    return `<pre class="lbx-tree">${esc(conn ? `${conn.kind || 'database'} · ${conn.name || conn.host || ''}` : 'database')}
├── ${esc((conn && conn.table_prefix) || 'lbl_')}projects / tasks / classes / media
├── ${esc((conn && conn.table_prefix) || 'lbl_')}annotations  (upserted, idempotent)
└── ${esc((conn && conn.table_prefix) || 'lbl_')}export_runs  one run per task${X.create_views ? `\n${tasks.map((t) => `    view ${esc(slug(t.name))}`).join('\n')}` : ''}</pre>`;
  }
  const root = X.kind === 'zip' ? `${slug(X.project.name)}.zip` : X.kind === 'local' ? X.path || '<folder>'
    : X.kind === 's3' ? `s3://${X.bucket || '<bucket>'}/${X.prefix || ''}` : `${X.kind}://${conn ? conn.name || conn.host : '<connection>'}${X.path || '/'}`;
  const n = X.project.n_media != null ? X.project.n_media : '';
  const lines: Array<[string, string]> = [];
  if (X.include_images) lines.push(['images/', `${n} images${X.only_done ? ' (done only)' : ''}`]);
  tasks.forEach((t) => {
    lines.push([`${slug(t.name)}/`, t.name]);
    const all = (t.classes || []).length, k = (X.classes.get(t.id) || new Set()).size;
    lines.push(['  labels/', 'one .txt per image'], ['  classes.txt', k < all ? `${k} of ${all} classes, ids 0..${Math.max(0, k - 1)}` : `${all} classes`], ['  data.yaml', '']);
  });
  lines.push(['manifest.json', '']);
  return `<pre class="lbx-tree">${esc(root)}\n${lines.map(([a, b], i) => {
    const sub = a.startsWith('  ');
    const last = i === lines.length - 1;
    const pre = sub ? `${'│   '}${lines[i + 1]?.[0].startsWith('  ') ? '├── ' : '└── '}` : last ? '└── ' : '├── ';
    return `${pre}${esc(a.trim())}${b ? `  <span class="hint">${esc(b)}</span>` : ''}`;
  }).join('\n')}</pre>`;
}

function progressHTML(): string {
  const X = cur();
  if (!X.job && !X.result && !X.error) return '';
  const j: Partial<Job> = X.job || {};
  const tasks = (X.project.tasks || []).filter((t) => X.tasks.has(t.id));
  const stage = String(j.stage || '').toLowerCase();
  let curI = tasks.findIndex((t) => stage.includes(t.name.toLowerCase()) || stage.includes(slug(t.name)));
  if (X.result) curI = tasks.length;
  const pct = j.total ? Math.round(j.pct || 0) : null;
  let html = '<div class="lbx-progress">';
  if (X.running) {
    html += `<div class="status-line"><div class="spin"></div>${esc(j.stage || j.status || 'starting')}${j.total ? ` · <span class="num">${j.done}/${j.total}</span>` : ''}</div>
      <div class="bar big${pct == null ? ' indeterminate' : ''}" role="progressbar" aria-label="Export progress"${pct != null ? ` aria-valuenow="${pct}"` : ''}><div style="width:${pct != null ? pct : 30}%"></div></div>`;
  }
  html += `<ul class="lbx-tasks">${tasks.map((t, i) => {
    const st = X.error && i >= Math.max(0, curI) ? 'err' : i < curI || X.result ? 'done' : i === curI ? 'run' : 'wait';
    const sum = X.result?.summary?.tasks?.[t.id];
    return `<li class="lbx-t ${st}">${icon(st === 'done' ? 'check' : st === 'err' ? 'alert' : st === 'run' ? 'refresh' : 'circle')}<span>${esc(t.name)}</span>` +
      `${sum ? `<span class="hint num">${esc(Object.entries(sum).map(([k, v]) => `${k.replace(/^n_/, '')} ${v}`).join(' · '))}</span>` : ''}</li>`;
  }).join('')}</ul>`;
  if (X.error) html += `<div class="status-line err">${icon('alert')}${esc(X.error)}</div>`;
  if (X.result) {
    const s = X.result.summary || {};
    const flat = Object.entries(s).filter(([, v]) => v == null || typeof v !== 'object');
    html += `<div class="status-line ok">${icon('check')}Export finished</div>` +
      (flat.length ? `<dl class="lbx-sum">${flat.map(([k, v]) => `<dt>${esc(k.replace(/_/g, ' '))}</dt><dd class="num">${esc(k === 'ms' && typeof v === 'number' ? `${(v / 1000).toFixed(1)} s` : k === 'bytes' && typeof v === 'number' ? fmtBytes(v) : v)}</dd>`).join('')}</dl>` : '');
    const dl = X.result.download || (X.kind === 'zip' ? `api/label/exports/${encodeURIComponent(X.result.run_id ?? '')}/download` : '');
    if (dl) html += `<a class="lbx-dl" href="${esc(dl)}" download>${icon('download')}Download the zip</a>`;
  }
  return html + '</div>';
}

const fmtBytes = (b: number): string => (b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} kB`);

const okColor = (c: string | null | undefined): string => (c && /^#[0-9a-f]{6}$/i.test(c) ? c : '#888888');

function chipsHTML(t: LabelTask): string {
  const X = cur();
  const cs = t.classes || [];
  if (!X.tasks.has(t.id) || !cs.length) return '';
  const k = X.classes.get(t.id) || new Set();
  const n = (id: string): number | null => (X.counts ? X.counts[id]?.n_annotations || 0 : null);
  return `<div class="lbx-cls" role="group" aria-label="${esc(`Classes of ${t.name} to export`)}">` +
    `<span class="lbx-cls-tools"><button class="lbx-link" data-x="call" data-t="${esc(t.id)}"${k.size === cs.length ? ' disabled' : ''}>all</button>` +
    `<span class="hint">/</span><button class="lbx-link" data-x="cnone" data-t="${esc(t.id)}"${k.size ? '' : ' disabled'}>none</button>` +
    `<span class="hint num">${k.size}/${cs.length}${k.size < cs.length && k.size ? ' · ids renumbered 0..' + (k.size - 1) : ''}</span></span>` +
    cs.map((c) => {
      const on = k.has(c.id);
      const cnt = n(c.id);
      return `<button class="lbx-chip${on ? ' on' : ''}" data-x="chip" data-t="${esc(t.id)}" data-c="${esc(c.id)}" aria-pressed="${on}" ` +
        `title="${esc(`${c.name}${cnt != null ? `: ${cnt} label${cnt === 1 ? '' : 's'}` : ''} (click to ${on ? 'exclude' : 'include'})`)}">` +
        `<span class="sw" style="--c:${okColor(c.color)}"></span><span class="nm">${esc(c.name)}</span>${cnt != null ? `<span class="num">${cnt}</span>` : ''}</button>`;
    }).join('') + '</div>';
}

function reHTML(): string {
  const X = cur();
  const tg = lastTarget();
  if (!tg) return '';
  if (X.confirmRe) {
    return `<div class="lbx-re confirm" role="alert">${icon('alert')}<span>Re-export to <b>${esc(targetText(tg))}</b> with the last choices. ` +
      'Files already there are overwritten.</span><span class="grow"></span><button data-x="recancel">Cancel</button>' +
      `<button class="primary" data-x="reyes">${icon('refresh')}Overwrite and export</button></div>`;
  }
  return `<div class="lbx-re">${icon('refresh')}<span>Last export: <b>${esc(targetText(tg))}</b></span><span class="grow"></span>` +
    `<button class="primary" data-x="reexport">Re-export to ${esc(tg.kind === 'zip' ? 'zip' : targetText(tg))}</button></div>`;
}

function render(): void {
  if (el && X) renderDialog(el, X);
}

function renderDialog(el_: HTMLElement, X: ExportState): void {
  const tasks = X.project.tasks || [];
  const busy = X.running;
  const lbx = el_.querySelector('.lbx');
  if (lbx) lbx.innerHTML = `
    <div class="lbx-head"><h2 id="lbx-title">${icon('download')}Export · ${esc(X.project.name)}</h2>
      <button class="icon-btn ghost" data-x="close" aria-label="Close" title="Close (Esc)">${icon('x')}</button></div>
    <div class="lbx-body"><fieldset ${busy ? 'disabled' : ''}>
      ${reHTML()}
      <section><h3>Tasks and classes</h3><div class="lbx-checks">${tasks.map((t) => `<label class="check"><input type="checkbox" data-task="${esc(t.id)}"${X.tasks.has(t.id) ? ' checked' : ''}>${esc(t.name)} <span class="hint">${esc(t.task_type.replace(/_/g, ' '))}</span></label>${chipsHTML(t)}`).join('') || '<p class="hint">This session has no tasks.</p>'}</div></section>
      <section><h3>Target</h3><div class="lbx-kinds" role="radiogroup" aria-label="Export target">${KINDS.map(([k, label, ic]) =>
        `<label class="lbx-kind${X.kind === k ? ' sel' : ''}"><input type="radio" name="lbx-kind" value="${k}"${X.kind === k ? ' checked' : ''}>${icon(ic)}<span>${esc(label)}</span></label>`).join('')}</div>
        <div class="lbx-target">${targetHTML()}</div></section>
      <section><h3>Options</h3><div class="lbx-checks">
        <label class="check"><input type="checkbox" data-f="include_images"${X.include_images ? ' checked' : ''}>Include the images (one shared <code>images/</code> folder)</label>
        <label class="check"><input type="checkbox" data-f="only_done"${X.only_done ? ' checked' : ''}>Only committed (done) images</label>
        <label class="check"><input type="checkbox" data-f="overwrite"${X.overwrite ? ' checked' : ''}>Overwrite a non-empty target</label></div></section>
      <section><h3>Layout</h3>${treeHTML()}</section></fieldset>
      ${progressHTML()}
    </div>
    <div class="lbx-foot"><span class="grow"></span><button data-x="close">${X.result ? 'Close' : busy ? 'Hide' : 'Cancel'}</button>
      ${X.result ? '' : `<button class="primary" data-x="start"${busy || !X.tasks.size ? ' disabled' : ''}>${icon('play')}Export</button>`}</div>`;
}

// ---------------------------------------------------------------- actions
function check(): string | null {
  const X = cur();
  if (!X.tasks.size) return 'Pick at least one task';
  for (const t of X.project.tasks || []) {
    if (X.tasks.has(t.id) && (t.classes || []).length && !(X.classes.get(t.id) || new Set()).size) return `Pick at least one class of ${t.name} (or untick the task)`;
  }
  if (X.kind === 'local' && !X.path.trim()) return 'Choose a folder';
  if (['s3', 'ftp', 'sftp', 'db'].includes(X.kind) && !X.connection_id) return 'Choose a connection';
  if (X.kind === 's3' && !X.bucket.trim()) return 'Enter a bucket';
  return null;
}

async function start(): Promise<void> {
  const X = cur();
  const bad = check();
  if (bad) { X.error = bad; X.job = null; render(); return; }
  const target: ExportTarget = { kind: X.kind };
  if (X.kind === 'local' || X.kind === 'ftp' || X.kind === 'sftp') target.path = X.path.trim();
  if (X.kind !== 'zip' && X.kind !== 'local') target.connection_id = X.connection_id;
  if (X.kind === 's3') { target.bucket = X.bucket.trim(); target.prefix = X.prefix.trim(); }
  if (X.kind === 'db') target.create_views = X.create_views;
  X.error = null; X.result = null; X.running = true; X.job = { stage: 'starting' };
  render();
  const me = X;
  try {
    const classes = classesOption();
    const body: ExportBody = { target, task_ids: [...X.tasks], classes, include_images: X.include_images, only_done: X.only_done, overwrite: X.overwrite };
    const r = await postJSON<JobStarted & { run_id: string }>(`api/label/projects/${encodeURIComponent(X.pid)}/export`, body);
    saveDefault(me, body);
    writeExcluded(me.pid, me.project, classes);
    for (;;) {
      if (me.closed) return;
      const j = await getJSON<Job>(`api/jobs/${r.job_id}`);
      me.job = j;
      if (j.status === 'error') throw new Error(j.error || 'export failed');
      if (j.status === 'done') break;
      render();
      await new Promise((ok) => setTimeout(ok, 500));
    }
    const run = await getJSON<ExportRun>(`api/label/exports/${encodeURIComponent(r.run_id)}`);
    run.run_id = run.run_id || r.run_id;
    me.result = run;
    if (run.status === 'error') throw new Error((run.summary && run.summary.error) || 'export failed');
  } catch (e) {
    me.error = errText(e);
    me.result = null;
  }
  me.running = false;
  if (X === me) render();
}

// the server saves this export as the project's export_default (the re-export repeats it); mirror it on the page's
// project object so "Re-export to …" shows without a reload
function saveDefault(me: ExportState, body: ExportBody): void {
  me.project.export_default = { ...body.target, target: body.target, task_ids: body.task_ids, classes: body.classes,
    include_images: body.include_images, only_done: body.only_done };
  document.dispatchEvent(new CustomEvent('sessions-changed'));
}

function applyDefault(): void {
  const X = cur();
  const d: ExportDefault = X.project.export_default || {};
  const tg: Partial<ExportTarget> = d.target || {};
  const live = (X.project.tasks || []).map((t) => t.id);
  const picked = (d.task_ids || []).filter((t) => live.includes(t));
  Object.assign(X, { kind: tg.kind || 'zip', path: tg.path || '', connection_id: tg.connection_id || '', bucket: tg.bucket || '',
    prefix: tg.prefix || '', create_views: tg.create_views !== false, include_images: d.include_images !== false, only_done: !!d.only_done,
    tasks: new Set(picked.length ? picked : live), classes: keptClasses(X.pid, X.project, d.classes), browse: null });
}

async function browseLocal(path: string | null | undefined): Promise<void> {
  const X = cur();
  try {
    X.browse = await getJSON<ExportBrowse>(`api/fs/browse?path=${encodeURIComponent(path || '')}&images=1`);
    X.error = null;
  } catch (e) { X.error = errText(e); }
  render();
}

async function browseRemote(path: string | null | undefined): Promise<void> {
  const X = cur();
  try {
    X.browse = await postJSON<ExportBrowse>('api/connections/browse', { connection: { id: X.connection_id }, path: path || '' });
    X.error = null;
  } catch (e) { X.error = errText(e); }
  render();
}

async function loadBuckets(): Promise<void> {
  const X = cur();
  X.buckets = null;
  try {
    const r = await postJSON<ConnTestResult>('api/connections/test', { id: X.connection_id });
    X.buckets = (r.buckets || []).map((b) => (typeof b === 'string' ? b : b.name));
  } catch (e) { X.buckets = []; }
  render();
}

function toggleChip(b: HTMLElement): void {
  const X = cur();
  const tid = b.dataset['t'], cid = b.dataset['c'];
  const k = tid === undefined ? undefined : X.classes.get(tid);
  if (!k || cid === undefined) return;
  if (k.has(cid)) k.delete(cid); else k.add(cid);
  render();
  dlg().querySelector<HTMLElement>(`[data-x="chip"][data-c="${CSS.escape(cid)}"]`)?.focus();
}

const ACT: Record<string, (b: HTMLElement) => void | Promise<void>> = {
  close: () => closeExport(),
  start: () => start(),
  chip: toggleChip,
  call: (b) => { const X = cur(); const t = (X.project.tasks || []).find((x) => x.id === b.dataset['t']); if (t) X.classes.set(t.id, new Set(t.classes.map((c) => c.id))); render(); },
  cnone: (b) => { const t = b.dataset['t']; if (t !== undefined) cur().classes.set(t, new Set()); render(); },
  reexport: () => {
    const X = cur();
    if (!lastTarget() || X.running) return;
    applyDefault();
    if (X.kind === 'zip') { X.overwrite = false; void start(); return; }
    X.confirmRe = true; render();
    dlg().querySelector<HTMLElement>('[data-x="reyes"]')?.focus();
  },
  reyes: () => { const X = cur(); X.confirmRe = false; applyDefault(); X.overwrite = true; void start(); },
  recancel: () => { cur().confirmRe = false; render(); },
  browse: () => browseLocal(cur().path || ''),
  crumb: (b) => browseLocal(b.dataset['to']),
  up: () => browseLocal(cur().browse?.parent),
  open: (b) => { const br = cur().browse; if (br) return browseLocal(`${br.path.replace(/\/$/, '')}/${b.dataset['name'] ?? ''}`); },
  pick: () => { const X = cur(); if (X.browse) { X.path = X.browse.path; X.browse = null; render(); } },
  rbrowse: () => browseRemote(cur().path || ''),
  rup: () => browseRemote(cur().browse?.parent),
  ropen: (b) => { const br = cur().browse; if (br) return browseRemote(`${(br.path || '').replace(/\/$/, '')}/${b.dataset['name'] ?? ''}`); },
  conns: async () => { closeExport(); const m = await import('../settings/dialog.js'); void m.openSettings('connections'); },
};

function bind(): void {
  const root = dlg();
  root.addEventListener('click', (e) => {
    if (e.target === root) { closeExport(); return; }
    const b = closestEl<HTMLButtonElement>(e.target, '[data-x]');
    const act = b && ACT[b.dataset['x'] ?? ''];
    if (b && !b.disabled && act && X) { e.preventDefault(); void act(b); }
  });
  root.addEventListener('input', (e) => {
    const t = e.target;
    if (!X || !(t instanceof HTMLInputElement || t instanceof HTMLSelectElement) || t.type === 'checkbox') return;
    const f = t.dataset['f'];
    if (isTextKey(f)) X[f] = t.value;
    else return;
    const tree = root.querySelector('.lbx-tree');
    if (tree) tree.outerHTML = treeHTML();
  });
  root.addEventListener('change', (e) => {
    if (!X) return;
    const t = e.target;
    if (!(t instanceof HTMLInputElement || t instanceof HTMLSelectElement)) return;
    const f = t.dataset['f'];
    const task = t.dataset['task'];
    if (t.name === 'lbx-kind') {
      const k = kindOfValue(t.value);
      if (!k) return;
      X.kind = k; X.browse = null; X.error = null;
      { const x = X; if (!connsOf(x.kind).some((c) => c.id === x.connection_id)) x.connection_id = connsOf(x.kind)[0]?.id || ''; }
      if (X.kind === 's3' && X.connection_id) void loadBuckets();
    } else if (task && t instanceof HTMLInputElement) {
      if (t.checked) X.tasks.add(task); else X.tasks.delete(task);
    } else if (f) {
      // a text field's value is already in X (input event): re-rendering on its change (blur) would replace the
      // button being clicked between mousedown and click, and the click would be lost
      if (t instanceof HTMLInputElement && t.type !== 'checkbox') return;
      if (t instanceof HTMLInputElement && t.type === 'checkbox') { if (isFlagKey(f)) X[f] = t.checked; }
      else if (isTextKey(f)) X[f] = t.value;
      if (f === 'connection_id') { X.browse = null; if (X.kind === 's3' && X.connection_id) void loadBuckets(); }
    } else return;
    render();
    const again = t.name === 'lbx-kind' ? root.querySelector<HTMLElement>(`input[name="lbx-kind"][value="${X.kind}"]`)
      : task ? root.querySelector<HTMLElement>(`[data-task="${task}"]`) : root.querySelector<HTMLElement>(`[data-f="${f}"]`);
    if (again) again.focus();
  });
  root.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); closeExport(); return; }
    if (e.key !== 'Tab') return;
    const f = [...root.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]')].filter((x) => x.offsetParent);
    const first = f[0], last = f[f.length - 1];
    if (!first || !last) return;
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
}
