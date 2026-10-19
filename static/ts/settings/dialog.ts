// Settings dialog shell: open / close, data loading, actions (data-act) and input binding.
// The Sessions home opens it in solo mode (no section nav): just the new / edit session forms.

import { api, errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon, syncRanges } from '../core/dom.js';
import { setWheelMode, wheelModeHTML } from '../core/gesture.js';
import { setBar, waitJob } from '../core/jobs.js';
import { setLook, setPalette, setTheme, THEME } from '../core/theme.js';
import { updateMarks } from '../plot/marks.js';
import type { BrowseResult, ConnectionsPayload, ConnTestResult, DatasetItem, DatasetsPayload, LabProject, LabelingForm, OpenSettingsOpts, S3Form,
  SectionId, SetAction, SetChange, SetState } from '../types-settings.js';
import { connDefaults, connEditable, connKind, connTestText, dd, fillWorkspaceSec, formOf, renderAppearanceSec, renderConnsSec, renderDatasetsSec, renderPlotSec,
  renderWorkspaceSec, rowAttr, SECTIONS, setDisabled, statusHTML, VIZ_TASKS } from './forms.js';
import { LAB_ACTIONS, labCancelRec, labInput, labKeydown, loadLabSettings, renderLabelingSec } from './labeling.js';
import { saveSettings } from '../views/controls.js';
import { loadModelsData, MODEL_ACTIONS, MODEL_CHANGES, renderModelsSec } from './models.js';
import { LLM_ACTIONS, LLM_CHANGES, llmInput, loadLlmData, renderLlmSec } from './llm.js';

// Datasets: local folders (read in place) and S3 / RustFS prefixes (stream or local cache), connections (S3, FTP,
// SFTP, PostgreSQL, SQL Server), the labeling page's options + hotkeys, the AI assistant (LLM providers + feature
// switches, settings/llm.js), appearance, plot options, workspace info.
// Everything renders from SET; clicks go through data-act. Sections marked data-needs="session" in index.html
// (models, plot, workspace) exist only on the visualizer screen: the home and the label page hide them.
export const SET: SetState = { sec: 'datasets', view: 'list', solo: false, datasets: [], conns: [], models: [], found: [], spec: null, active: null, llm: null, llmError: null, form: null, error: null };

let settingsReturnFocus: Element | null = null;

// opts (Sessions home): { solo: true, view: 'add' } = new session, { solo: true, edit: id } = edit one,
// { solo: true, view: 'labeling', project: pid } = visualize a task of that labeling session
const hasSession = () => document.documentElement.dataset.screen === 'viz';
// Models are global (also on the home and the label page: models/hub.py); Plot and Workspace need an open dataset
const NEEDS_SESSION = new Set<string>(['plot', 'workspace']);

const isSection = (x: string | undefined): x is SectionId => x !== undefined && x in SECTIONS;
const isRecord = (x: unknown): x is Record<string, unknown> => typeof x === 'object' && x !== null;
type FormControl = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
const isControl = (x: unknown): x is FormControl => x instanceof HTMLInputElement || x instanceof HTMLSelectElement || x instanceof HTMLTextAreaElement;

export async function openSettings(sec: SectionId = 'datasets', opts: OpenSettingsOpts = {}): Promise<void> {
  const box = $req('settings');
  if (box.classList.contains('hidden')) settingsReturnFocus = document.activeElement;
  if (NEEDS_SESSION.has(sec) && !hasSession()) sec = 'labeling';
  $req('hover-tip').classList.add('hidden');
  box.classList.remove('hidden');
  SET.solo = !!opts.solo;
  box.classList.toggle('solo', SET.solo);
  SET.sec = sec;
  SET.view = opts.view === 'labeling' ? 'add' : opts.view || 'list';
  SET.form = null;
  renderSettings();
  box.querySelector<HTMLElement>('.settings-box')?.focus();
  await loadSettingsData();
  if (opts.edit && SET.datasets.some((d) => d.id === opts.edit)) { editDataset(opts.edit); return; }
  if (opts.view === 'labeling') { void newLabelingForm(null, opts.project); return; }
  renderSettings();
}

function closeSettings(): void {
  $req('settings').classList.add('hidden');
  $req('settings').classList.remove('solo');
  SET.solo = false;
  if (settingsReturnFocus instanceof HTMLElement && document.contains(settingsReturnFocus)) settingsReturnFocus.focus();
  settingsReturnFocus = null;
}

async function loadSettingsData(): Promise<void> {
  // the models need an open session; the labeling options load on their own (one failing hides nothing else)
  const extra: Promise<void>[] = [loadModelsData().catch(() => {}), loadLabSettings(), loadLlmData()];
  try {
    const [d, c] = await Promise.all([getJSON<DatasetsPayload>('api/datasets'), getJSON<ConnectionsPayload>('api/connections'), ...extra]);
    SET.datasets = d.datasets;
    SET.active = d.active;
    SET.conns = c.connections;
    SET.error = null;
  } catch (e) { SET.error = errText(e); }
}

const RENDERERS: Record<SectionId, () => string> = {
  datasets: renderDatasetsSec, connections: renderConnsSec, labeling: renderLabelingSec, models: renderModelsSec, llm: renderLlmSec,
  appearance: () => renderAppearanceSec() + wheelModeHTML(), plot: renderPlotSec, workspace: renderWorkspaceSec,
};

export function renderSettings(): void {
  const viz = hasSession();
  document.querySelectorAll<HTMLElement>('.snav').forEach((b) => {
    b.classList.toggle('active', b.dataset.sec === SET.sec);
    b.classList.toggle('hidden', !viz && NEEDS_SESSION.has(b.dataset.sec ?? ''));
  });
  const count = (id: string, n: number) => { const el = $(id); if (el) el.textContent = String(n || ''); };
  count('snav-nds', SET.datasets.length);
  count('snav-ncs', SET.conns.length);
  count('snav-nms', SET.models.length);
  count('snav-nllm', SET.llm ? SET.llm.providers.length : 0);
  const [title, sub] = SET.solo ? [SET.form && 'id' in SET.form && SET.form.id ? 'Edit session' : 'New session',
    'One YOLO dataset: a folder on this machine, an S3 / RustFS prefix, or a task of a labeling session'] : SECTIONS[SET.sec];
  $req('sec-title').innerHTML = `${esc(title)}<span class="sub">${esc(sub)}</span>`;
  const body = $req('settings-body');
  body.innerHTML = (SET.error ? `<div class="status-line err">${icon('alert')}${esc(SET.error)}</div>` : '') + RENDERERS[SET.sec]();
  if (SET.sec === 'workspace') void fillWorkspaceSec();
  syncRanges();
}

// ---- actions
// the visualizer screen of that session: boot() opens it on the server and shows its sync / index progress
function openDataset(id: string): void {
  location.href = `./?session=${encodeURIComponent(id)}`;
}

// saved without opening: the Sessions home re-renders its cards; the full dialog goes back to its list
async function savedDataset(): Promise<void> {
  if (SET.solo) {
    closeSettings();
    document.dispatchEvent(new CustomEvent('sessions-changed'));
    return;
  }
  await loadSettingsData();
  back();
}

function editDataset(id: string): void {
  const d = SET.datasets.find((x) => x.id === id);
  if (!d) return;
  if (d.kind === 's3') newS3Form(d);
  else if (d.kind === 'labeling') void newLabelingForm(d);
  else { SET.view = 'local'; SET.sec = 'datasets'; SET.form = { form: 'local', id: d.id, path: d.path, name: d.name }; renderSettings(); void localBrowse(d.path); }
}

async function s3Browse(): Promise<void> {
  const f = formOf('s3');
  f.locStatus = { kind: 'busy', text: `listing ${f.bucket}/${f.prefix || ''}…` };
  renderSettings();
  try {
    f.browse = await postJSON<BrowseResult>('api/connections/browse', { connection: connPayload(f), bucket: f.bucket, prefix: f.prefix || '' });
    f.locStatus = null;
  } catch (e) { f.browse = null; f.locStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

function connPayload(f: S3Form): { id: string } | S3Form['conn'] {
  return f.connId && f.connId !== 'new' ? { id: f.connId } : { ...f.conn };
}

async function testConn(): Promise<void> {
  const f = SET.form;
  if (f === null || (f.form !== 's3' && f.form !== 'conn')) throw new Error('no connection form is open');
  f.connStatus = { kind: 'busy', text: 'connecting…' };
  renderSettings();
  try {
    const r = await postJSON<ConnTestResult>('api/connections/test', f.form === 'conn' ? f.conn : connPayload(f));
    const buckets = (r.buckets ?? []).map((x) => (typeof x === 'string' ? x : x.name));
    f.connStatus = { kind: 'ok', text: f.form === 'conn' && connKind(f.conn) !== 's3' ? connTestText(r)
      : `connected in ${r.ms} ms · ${buckets.length} bucket${buckets.length === 1 ? '' : 's'}` };
    if (f.form === 's3') {
      f.buckets = buckets;
      if (f.bucket === undefined || !buckets.includes(f.bucket)) f.bucket = buckets[0] || '';
      renderSettings();
      if (f.bucket) await s3Browse();
      return;
    }
  } catch (e) { f.connStatus = { kind: 'err', text: errText(e) }; if (f.form === 's3') { f.buckets = null; f.browse = null; } }
  renderSettings();
}

async function localBrowse(path: string | null | undefined): Promise<void> {
  const f = formOf('local');
  f.status = { kind: 'busy', text: 'reading folder…' };
  renderSettings();
  try {
    f.browse = await getJSON<BrowseResult>(`api/fs/browse?path=${encodeURIComponent(path || '')}`);
    f.path = f.browse.path;
    f.status = null;
  } catch (e) { f.status = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

function newS3Form(d: DatasetItem | null): void {
  SET.view = 's3';
  SET.sec = 'datasets';
  const f: S3Form = { form: 's3', id: d?.id, connId: d ? (d.connection_id ?? 'new') : (SET.conns[0]?.id ?? 'new'),
    conn: { endpoint: '', region: '', verify_ssl: true }, bucket: d?.bucket, prefix: d ? d.prefix : '',
    mode: d ? d.mode : 'stream', name: d ? d.name : '' };
  SET.form = f;
  renderSettings();
  if (f.connId !== 'new') void testConn();
}

// labeling-backed session: the labeling sessions (cards: tasks with types and done counts) are listed once per form
async function newLabelingForm(d: DatasetItem | null, pid?: string): Promise<void> {
  SET.view = 'labeling';
  SET.sec = 'datasets';
  const f: LabelingForm = { form: 'labeling', id: d?.id, project_id: d ? (d.project_id ?? '') : pid || '', task_id: d ? (d.task_id ?? '') : '',
    only_done: !!(d && d.only_done), name: d ? d.name : '', direct: !!pid, projects: null };
  SET.form = f;
  renderSettings();
  try {
    const { projects } = await getJSON<{ projects: LabProject[] }>('api/label/projects');
    if (SET.form !== f) return;                   // the user moved on meanwhile
    f.projects = projects;
    const first = projects.find((p) => (p.tasks || []).some((t) => VIZ_TASKS.has(t.task_type))) ?? projects[0];
    if (!f.project_id && first) f.project_id = first.id;
    if (!f.task_id) pickLabelingTask(f);
  } catch (e) { f.status = { kind: 'err', text: errText(e) }; }
  if (SET.form === f) renderSettings();
}

// the first task of the picked session that has boxes (classification tasks cannot be visualized)
function pickLabelingTask(f: LabelingForm): void {
  const p = (f.projects || []).find((x) => x.id === f.project_id);
  const t = p && (p.tasks || []).find((x) => VIZ_TASKS.has(x.task_type));
  f.task_id = t ? t.id : '';
}

function back(): void {
  if (SET.solo) {                            // solo: a new-session form steps back to the two tiles, else close
    const f = SET.form;
    if (SET.view !== 'add' && !(f && 'id' in f && (f.id || ('direct' in f && f.direct)))) { SET.view = 'add'; SET.form = null; renderSettings(); } else closeSettings();
    return;
  }
  SET.view = 'list'; SET.form = null; renderSettings();
}

// save-s3 / save-local / save-labeling: post the dataset, then open it or go back to the list
async function saveDataset(form: { saveStatus?: S3Form['saveStatus'] }, busyText: string, body: () => Promise<{ id: string }>, open: boolean): Promise<void> {
  form.saveStatus = { kind: 'busy', text: busyText };
  renderSettings();
  try {
    const d = await body();
    if (open) { form.saveStatus = { kind: 'busy', text: 'opening…' }; renderSettings(); openDataset(d.id); return; }
    await savedDataset();
  } catch (e) { form.saveStatus = { kind: 'err', text: errText(e) }; renderSettings(); }
}

const SET_ACTIONS: Record<string, SetAction> = {
  add: () => { SET.view = 'add'; renderSettings(); },
  back,
  'new-s3': () => newS3Form(null),
  'new-local': () => { SET.view = 'local'; SET.form = { form: 'local', path: '', name: '' }; renderSettings(); void localBrowse(''); },
  'new-labeling': () => newLabelingForm(null),
  'test-conn': testConn,
  'go-prefix': () => { if (formOf('s3').bucket) void s3Browse(); },
  's3-crumb': (b) => { formOf('s3').prefix = dd(b, 'to'); void s3Browse(); },
  's3-folder': (b) => { const f = formOf('s3'); f.prefix = [f.prefix, dd(b, 'name')].filter(Boolean).join('/'); void s3Browse(); },
  'go-path': () => localBrowse(formOf('local').path),
  'up-path': () => localBrowse(formOf('local').browse?.parent),
  'local-crumb': (b) => localBrowse('/' + dd(b, 'to')),
  'local-folder': (b) => {
    const br = formOf('local').browse;
    if (br) void localBrowse(`${br.path.replace(/\/$/, '')}/${dd(b, 'name')}`);
  },
  'save-s3': (b) => {
    const f = formOf('s3');
    return saveDataset(f, 'saving…', async () => {
      let cid = f.connId;
      if (cid === 'new') cid = (await postJSON<{ id: string }>('api/connections', f.conn)).id;
      return postJSON<{ id: string }>('api/datasets', { id: f.id, kind: 's3', connection_id: cid, bucket: f.bucket, prefix: f.prefix || '',
        mode: f.mode, name: f.name });
    }, !!b.dataset.open);
  },
  'save-local': (b) => {
    const f = formOf('local');
    return saveDataset(f, 'saving…', () => postJSON<{ id: string }>('api/datasets', { id: f.id, kind: 'local', path: f.browse?.path, name: f.name }), !!b.dataset.open);
  },
  'save-labeling': (b) => {
    const f = formOf('labeling');
    return saveDataset(f, 'checking the labeling task…', () => postJSON<{ id: string }>('api/datasets', { id: f.id, kind: 'labeling', project_id: f.project_id,
      task_id: f.task_id, only_done: !!f.only_done, name: f.name }), !!b.dataset.open);
  },
  'open-ds': (b) => { setDisabled(b, true); openDataset(rowAttr(b, '[data-id]', 'id')); },
  'sync-ds': async (b) => {
    const id = rowAttr(b, '[data-id]', 'id');
    const el = $req(`dsp-${id}`);
    setDisabled(b, true);
    try {
      const { job_id: jid } = await postJSON<{ job_id: number }>(`api/datasets/${encodeURIComponent(id)}/sync`, {});
      el.innerHTML = '<div class="bar"><div></div></div><span class="num"></span>';
      await waitJob(jid, (j) => {
        const box = $(`dsp-${id}`);
        const bar = box && box.querySelector<HTMLElement>('.bar > div');
        if (!box || !bar) return;
        setBar(bar, j);
        const num = box.querySelector('.num');
        if (num) num.textContent = j.total ? `${j.stage} ${j.done}/${j.total} ${j.extra || ''}` : j.stage;
      });
      await loadSettingsData();
      renderSettings();
    } catch (e) { el.innerHTML = statusHTML({ kind: 'err', text: errText(e) }); setDisabled(b, false); }
  },
  'edit-ds': (b) => editDataset(rowAttr(b, '[data-id]', 'id')),
  'remove-ds': async (b) => {
    const d = SET.datasets.find((x) => x.id === rowAttr(b, '[data-id]', 'id'));
    if (!d) return;
    if (!confirm(`Remove the session "${d.name}"?\n\nNo dataset files are deleted.`)) return;
    try { await api(`api/datasets/${encodeURIComponent(d.id)}`, { method: 'DELETE' }); } catch (e) { alert(errText(e)); }
    await loadSettingsData();
    renderSettings();
  },
  'new-conn': () => { SET.view = 'conn'; SET.form = { form: 'conn', conn: connDefaults('s3') }; renderSettings(); },
  'conn-kind': (b) => {
    const name = formOf('conn').conn.name;
    SET.form = { form: 'conn', conn: { ...connDefaults(dd(b, 'kind')), name: name || '' } };
    renderSettings();
    const el = document.querySelector<HTMLElement>(`.kind-opt[data-kind="${dd(b, 'kind')}"]`);
    if (el) el.focus();
  },
  'edit-conn': (b) => {
    const c = SET.conns.find((x) => x.id === rowAttr(b, '[data-cid]', 'cid'));
    if (!c) return;
    SET.view = 'conn';
    SET.form = { form: 'conn', conn: connEditable(c) };
    renderSettings();
  },
  'save-conn': async () => {
    const f = formOf('conn');
    try { await postJSON('api/connections', f.conn); } catch (e) { f.connStatus = { kind: 'err', text: errText(e) }; renderSettings(); return; }
    await loadSettingsData();
    back();
  },
  'test-saved': async (b) => {
    const id = rowAttr(b, '[data-cid]', 'cid');
    const el = $req(`cst-${id}`);
    el.innerHTML = statusHTML({ kind: 'busy', text: 'connecting…' });
    try {
      const r = await postJSON<ConnTestResult>('api/connections/test', { id });
      el.innerHTML = statusHTML({ kind: 'ok', text: connTestText(r) });
    } catch (e) { el.innerHTML = statusHTML({ kind: 'err', text: errText(e) }); }
  },
  'remove-conn': async (b) => {
    const c = SET.conns.find((x) => x.id === rowAttr(b, '[data-cid]', 'cid'));
    if (!c) return;
    if (!confirm(`Delete the connection "${c.name}"? Its stored keys are removed from the server.`)) return;
    try { await api(`api/connections/${encodeURIComponent(c.id)}`, { method: 'DELETE' }); } catch (e) { alert(errText(e)); }
    await loadSettingsData();
    renderSettings();
  },
  look: (b) => { setLook(dd(b, 'look')); renderSettings(); },
  palette: (b) => { setPalette(dd(b, 'palette')); renderSettings(); },
  theme: (b) => { setTheme(dd(b, 'mode')); renderSettings(); },
  wheel: (b) => { setWheelMode(dd(b, 'mode')); renderSettings(); },
  ...MODEL_ACTIONS,
  ...LAB_ACTIONS,
  ...LLM_ACTIONS,
};

const SET_CHANGES: Record<string, SetChange> = {
  'conn-pick': () => { const f = formOf('s3'); f.buckets = null; f.browse = null; f.connStatus = null; renderSettings(); if (f.connId !== 'new') void testConn(); },
  'bucket-pick': () => { formOf('s3').prefix = ''; void s3Browse(); },
  'lds-project': () => { pickLabelingTask(formOf('labeling')); renderSettings(); },
  'lds-task': () => renderSettings(),
  'mode-pick': () => document.querySelectorAll('.radio-card').forEach((c) => c.classList.toggle('sel', !!c.querySelector<HTMLInputElement>('input')?.checked)),
  'dist-toggle': (el) => { if (el instanceof HTMLInputElement) $req<HTMLInputElement>('show-dist').checked = el.checked; updateMarks(); saveSettings(); },
  ...MODEL_CHANGES,
  ...LLM_CHANGES,
};

export function bindSettings(): void {
  const body = $req('settings-body');
  // inputs write straight into the form state (no re-render per keystroke, so focus stays put)
  const write = (el: FormControl): void => {
    const path = el.dataset.f;
    const form: Record<string, unknown> | null = SET.form;
    if (!path || !form) return;
    const isInput = el instanceof HTMLInputElement;
    const val = isInput && el.type === 'checkbox' ? el.checked : el.dataset.type === 'int' ? (el.value.trim() === '' ? null : Number(el.value)) : el.value;
    if (isInput && el.type === 'radio' && !el.checked) return;
    if (path.startsWith('conn.')) {
      const conn = form.conn;
      if (isRecord(conn)) conn[path.slice(5)] = val;
    } else if (path.includes('.')) {                 // nested, e.g. model.api.mapping.box
      const keys = path.split('.');
      let o: Record<string, unknown> = form;
      for (const k of keys.slice(0, -1)) {
        const next = o[k];
        if (isRecord(next)) o = next;
        else { const fresh: Record<string, unknown> = {}; o[k] = fresh; o = fresh; }
      }
      const last = keys[keys.length - 1];
      if (last !== undefined) o[last] = val;
    } else form[path] = val;
  };
  body.addEventListener('input', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.dataset.lab) { if (t instanceof HTMLInputElement || t instanceof HTMLSelectElement) labInput(t, false); return; }
    if (isControl(t)) write(t);
    if (SET.sec === 'llm') llmInput(t);       // live step locks, capability guess, token budget
  });
  body.addEventListener('change', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.dataset.lab) { if (t instanceof HTMLInputElement || t instanceof HTMLSelectElement) labInput(t, true); return; }
    if (isControl(t)) write(t);
    const act = t.dataset.actChange;
    const fn = act ? SET_CHANGES[act] : undefined;
    if (fn) void fn(t);
  });
  body.addEventListener('keydown', (e) => {
    const t = e.target;
    if (e.key !== 'Enter' || !(t instanceof HTMLElement) || !t.dataset.enter) return;
    e.preventDefault();
    if (isControl(t)) write(t);
    void SET_ACTIONS[t.dataset.enter]?.(t);
  });
  body.addEventListener('click', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    labCancelRec(t);
    const b = t && t.closest<HTMLElement>('[data-act]');
    const fn = b && b.dataset.act ? SET_ACTIONS[b.dataset.act] : undefined;
    if (b && fn && !((b instanceof HTMLButtonElement || b instanceof HTMLInputElement) && b.disabled)) void fn(b, e);
  });
  document.querySelectorAll<HTMLElement>('.snav').forEach((b) => {
    b.onclick = () => {
      const sec = b.dataset.sec;
      if (!isSection(sec)) return;
      SET.sec = sec; SET.view = 'list'; SET.form = null; labCancelRec(null); renderSettings();
    };
  });
  // the hotkey editor records the next key press: a window capture listener, bound here (main.js boot) before the
  // label page binds its own hotkeys, so a recorded combo never also runs its action
  window.addEventListener('keydown', (e) => { if (!$req('settings').classList.contains('hidden')) labKeydown(e); }, true);
  $req('settings-close').onclick = closeSettings;
  $req('settings').addEventListener('mousedown', (e) => { if (e.target === $req('settings')) closeSettings(); });
  $req('settings').addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); closeSettings(); return; }
    if (e.key !== 'Tab') return;                  // keep focus inside the dialog
    const f = [...$req('settings').querySelectorAll<HTMLElement>('button:not(:disabled), input, select, textarea')].filter((x) => x.offsetParent);
    const first = f[0], last = f[f.length - 1];
    if (!first || !last) return;
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  $req('btn-settings').onclick = () => openSettings('datasets');
  const homeBtn = $('home-settings');
  if (homeBtn) homeBtn.onclick = () => openSettings('datasets');
  $req('btn-theme').onclick = () => setTheme(THEME.name === 'dark' ? 'light' : 'dark');
}
