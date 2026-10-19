// Model-assisted labeling on the label page: the model menu in the top bar (robot button), the link dialog
// (connection → model → class mapping → confidence), the "label all images" dialog and its job progress in the
// button (count, ETA, Cancel), the auto-on-open toggle and "remove model labels" for every image.
// The single-image run and the merge of its result live in app.js (it owns the LRU entries and the autosave);
// this module only talks to the API and draws its own UI. Contract: dataviz/labeling/README.md "v2 contract".

import { apiFetch, errMessage, errText, getJSON } from '../core/api.js';
import { userKey } from '../core/auth.js';
import { $req, esc, fmtEta, icon } from '../core/dom.js';
import type { Job } from '../types.js';
import type { Ann, LabelTask, ModelLink } from '../types-label.js';
import { prettyCombo, combosFor } from './hotkeys.js';
import * as P from './panels.js';
import { L, TASK_TYPES } from './state.js';
import type { TaskTypeInfo } from './state.js';

// ---------------------------------------------------------------- types
/** What the page gives the model module (initModel). */
export interface ModelHandlers {
  task: () => LabelTask | null;
  labelThis: () => void;
  removeThis: () => void;
  taskUpdated: (t: LabelTask) => void;
  staleAnns: () => void;
  afterBulk: () => void;
}
/** POST predict/{mid} answer: the task's full live set after the run. */
export interface PredictResult { version: number; annotations: Ann[]; n_model: number; ms: number }
/** A model a server (or Settings › Models) offers (POST api/label/models/detect → models[]). */
export interface DetectedModel {
  id: string; name?: string; task_type?: string; geometry?: string; classes?: string[]; versions?: string[]; keypoints?: string[];
  [k: string]: unknown;
}
interface DetectPayload { protocol: string; models: DetectedModel[] }
/** A model server connection (GET api/connections, kind 'model'). */
interface Connection { id: string; name?: string; url?: string; kind: string; [k: string]: unknown }
/** A Settings › Models model (GET api/models). */
interface RegistryModel {
  id: string; name: string; task_type?: string; family?: string; format?: string; api?: { preset?: string; [k: string]: unknown };
  [k: string]: unknown;
}
interface ModelJob { id: number; done: number; total: number | null; status: string; pct?: number | null; eta?: number | null; cancelling?: boolean }
interface Modal { back: HTMLElement; el: HTMLElement; body: HTMLElement; foot: HTMLElement; close: () => void }
interface LinkStatus { kind: 'busy' | 'ok' | 'err'; text: string }
interface LinkState {
  t: LabelTask; link: ModelLink | null; conns: Connection[] | null; reg: RegistryModel[]; connId: string;
  nc: { name: string; url: string; api_key: string; timeout_s: string | number };
  detect: DetectPayload | null; status: LinkStatus | null; modelId: string | null; version: string | null; conf: number;
  auto: boolean; map: Record<string, string>; saving: boolean; tok: number;
}
interface LinkDialog extends Modal { D: LinkState }
interface JobSaved { id: number; key: string }
interface BulkResult { cancelled?: boolean; n_images?: number; n_shapes?: number; failed?: number }
interface ClassStat { n_model?: number; n_model_images?: number }

const M: { menu: HTMLElement | null; job: ModelJob | null; dlg: Modal | null; chip: number } = { menu: null, job: null, dlg: null, chip: 0 };
let H: ModelHandlers = { task: () => null, labelThis: () => {}, removeThis: () => {}, taskUpdated: () => {}, staleAnns: () => {}, afterBulk: () => {} };

const isRec = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null;
const must = <T extends Element>(root: ParentNode, sel: string): T => {
  const el = root.querySelector<T>(sel);
  if (!el) throw new Error(`missing ${sel}`);
  return el;
};

const pid = (): string => encodeURIComponent(String(L.pid));
const jobKey = (): string => userKey(`label-model-job-${L.pid}`);
export const linkOf = (t: LabelTask | null | undefined): ModelLink | null => (t && t.config && t.config.model && t.config.model.model_id ? t.config.model : null);
// a linked model runs only once activated in this server session (server-enforced: 409 otherwise)
export const isActive = (t: LabelTask | null | undefined): boolean => !!(t && L.project && (L.project.model_active || []).includes(t.id));
export const activateKey = (): string => { const c = combosFor('model_activate')[0]; return c ? prettyCombo(c) : 'the model menu'; };
const linkName = (link: ModelLink): string => link.name || link.model_id;

function setActiveLocal(tid: string, on: boolean): void {
  if (!L.project) return;
  const cur = new Set(L.project.model_active || []);
  if (on) cur.add(tid); else cur.delete(tid);
  L.project.model_active = [...cur];
}

export async function toggleActive(): Promise<void> {
  const t = H.task();
  if (!t) return;
  if (!linkOf(t)) { void openLinkDialog(); return; }
  const on = !isActive(t);
  try {
    await call('POST', `api/label/projects/${pid()}/tasks/${encodeURIComponent(t.id)}/model/activate`, { on });
    setActiveLocal(t.id, on);
    renderButton();
    const lk = linkOf(t);
    P.toast(on && lk ? `${linkName(lk)} is active: it labels images when you ask${lk.auto_on_open ? ', and each image you open' : ''}`
      : 'Model off: it labels nothing until you activate it again');
  } catch (e) { P.toast(`Could not ${on ? 'activate' : 'deactivate'} the model: ${errText(e)}`, 'err'); }
}

// T is the caller's claim about the server payload
async function call<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  if (L.readOnly && method !== 'GET') throw new Error('View only: ask the owner for Labeler access');
  const init: RequestInit = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) init.body = JSON.stringify(body);
  const r = await apiFetch(path, init);
  const d: unknown = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = isRec(d) ? d.detail : undefined;
    throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `${path}: ${r.status}`);
  }
  return d as T;
}

// ---------------------------------------------------------------- API used by app.js
// runs the task's model on one image now: {version, annotations (the task's full live set), n_model, ms}
export const predict = (mid: string, tid: string, conf?: number | null): Promise<PredictResult> => call<PredictResult>('POST', `api/label/projects/${pid()}/predict/${encodeURIComponent(mid)}`,
  conf == null ? { task_id: tid } : { task_id: tid, conf });

// ---------------------------------------------------------------- init + top-bar button
export function initModel(handlers: Partial<ModelHandlers>): void {
  H = { ...H, ...handlers };
  const wrap = $req('lb-model');
  wrap.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest('button') : null;
    if (!b) return;
    if (b.id === 'lb-model-btn') { if (M.menu) closeModelMenu(); else openMenu(); }
    else if (b.id === 'lb-model-cancel') void cancelJob();
  });
  document.addEventListener('pointerdown', (e) => {
    if (M.menu && e.target instanceof Node && !M.menu.contains(e.target) && !$req('lb-model-btn').contains(e.target)) closeModelMenu();
  }, true);
  renderButton();
  // the tracked job is stored as {id, key}: job ids restart at 1 with the server, so a bare id could point at an
  // unrelated job after a restart; trackJob() drops it unless the server's job key matches (an old bare id is dropped)
  let saved: Partial<JobSaved> | null = null;
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(jobKey()) || 'null');
    if (isRec(raw)) saved = raw;
  } catch (e) { /* private mode, old format */ }
  if (saved && saved.id != null && saved.key) void trackJob(saved.id, saved.key, true);
  else { try { sessionStorage.removeItem(jobKey()); } catch (e) { /* private mode */ } }
}

const shortName = (id: unknown): string => { const s = String(id || ''); const p = s.split('/'); return p[p.length - 1] || s; };

export function renderButton(): void {
  const btn = document.getElementById('lb-model-btn');
  if (!btn) return;
  const t = H.task();
  const link = linkOf(t);
  const j = M.job;
  let jobHTML = '';
  if (j) {
    const pct = j.total ? Math.min(100, Math.round(j.pct || (100 * j.done) / j.total)) : null;
    const txt = j.cancelling ? 'Cancelling…' : j.total ? `${j.done || 0}/${j.total}${j.eta != null ? ` · ${fmtEta(j.eta).replace(' left', '')}` : ''}` : j.status === 'queued' ? 'queued' : 'starting…';
    jobHTML = `<span class="lb-mjob"><span class="bar${pct == null ? ' indeterminate' : ''}" role="progressbar" aria-label="Labeling all images"` +
      `${pct != null ? ` aria-valuenow="${pct}"` : ''}><span style="width:${pct != null ? pct : 30}%"></span></span><span class="num">${esc(txt)}</span></span>`;
  }
  const on = isActive(t);
  const label = link ? `${shortName(linkName(link))}${on ? '' : ' · off'}` : 'Connect a model…';
  btn.innerHTML = `${icon('robot')}${j ? '' : `<span class="nm">${esc(label)}</span>`}${jobHTML}${icon('chev-d')}`;
  btn.title = !link ? 'Connect a model to label images for you'
    : !on ? `Model: ${linkName(link)} · OFF: activate it (${activateKey()}) to let it label` : `Model: ${linkName(link)} · active${link.auto_on_open ? ' · auto-labels each image you open' : ''}`;
  btn.classList.toggle('linked', !!link);
  btn.classList.toggle('inactive', !!link && !on);
  btn.classList.toggle('auto', !!(link && link.auto_on_open && on));
  const cancel = $req<HTMLButtonElement>('lb-model-cancel');
  cancel.classList.toggle('hidden', !j);
  cancel.disabled = !!(j && j.cancelling);
}

// "Model labeling…" chip in the status bar while single-image requests run
export function setChip(n: number): void {
  M.chip = Math.max(0, n);
  const el = document.getElementById('lb-model-chip');
  if (el) el.classList.toggle('hidden', !M.chip);
}

// ---------------------------------------------------------------- menu
const kbd = (id: string): string => { const c = combosFor(id)[0]; return c ? `<kbd>${esc(prettyCombo(c))}</kbd>` : ''; };

function openMenu(): void {
  closeModelMenu();
  const t = H.task();
  const link = linkOf(t);
  const m = document.createElement('div');
  m.className = 'lb-menu lb-mmenu panel';
  m.setAttribute('role', 'menu');
  m.setAttribute('aria-label', 'Model');
  const item = (a: string, ic: string, text: string, extra = '', attrs = ''): string => `<button role="menuitem" data-ma="${a}" ${attrs}>${icon(ic)}<span class="tx">${text}</span>${extra}</button>`;
  if (!t) m.innerHTML = '<div class="lb-menu-head">No task</div>';
  else if (!link) {
    m.innerHTML = `<div class="lb-menu-head">${icon('robot')}<span>No model for <b>${esc(t.name)}</b></span></div>` +
      '<p class="lb-mnote">Connect a model you host: it labels images for you and you fix what it got wrong.</p>' +
      item('link', 'plug', 'Connect a model…');
  } else {
    const busy = !!M.job;
    const on = isActive(t);
    m.innerHTML = `<div class="lb-menu-head">${icon('robot')}<span class="lb-mhead"><b>${esc(linkName(link))}</b>` +
      `<span class="muted">${esc(t.name)} · confidence ≥ ${esc((+(link.conf ?? 0) || 0.25).toFixed(2))}${link.registry_id ? ' · Settings › Models' : ''}</span></span></div>` +
      `<button role="menuitemcheckbox" aria-checked="${on}" data-ma="activate" class="lb-mact${on ? ' on' : ''}">${icon(on ? 'check' : 'play')}` +
      `<span class="tx">${on ? 'Active in this session (click to stop)' : 'Activate the model for this session'}</span>${kbd('model_activate')}</button>` +
      (on ? '' : '<p class="lb-mnote">Off: it labels nothing (not even auto-label) until you activate it.</p>') +
      '<div class="lb-msep" role="separator"></div>' +
      item('this', 'play', 'Label this image', kbd('model_this'), L.loading || !on ? 'disabled' : '') +
      item('all', 'layers', busy ? 'Labeling all images…' : 'Label all images…', '', busy || !on ? 'disabled' : '') +
      `<button role="menuitemcheckbox" aria-checked="${!!link.auto_on_open}" data-ma="auto">${icon(link.auto_on_open ? 'check' : 'circle')}` +
      '<span class="tx">Auto-label each image I open</span></button>' +
      '<div class="lb-msep" role="separator"></div>' +
      item('rm-this', 'x', 'Remove model labels from this image', kbd('model_remove'), L.loading ? 'disabled' : '') +
      `<button role="menuitem" class="danger" data-ma="rm-all" ${busy ? 'disabled' : ''}>${icon('trash')}<span class="tx">Remove model labels from all images…</span></button>` +
      '<div class="lb-msep" role="separator"></div>' +
      item('link', 'gear', 'Model settings…');
  }
  document.body.appendChild(m);
  const btn = $req('lb-model-btn');
  const r = btn.getBoundingClientRect();
  const mr = m.getBoundingClientRect();
  m.style.left = `${Math.max(8, Math.min(r.right - mr.width, innerWidth - mr.width - 8))}px`;
  m.style.top = `${Math.min(r.bottom + 4, innerHeight - mr.height - 8)}px`;
  M.menu = m;
  btn.setAttribute('aria-expanded', 'true');
  m.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLButtonElement>('[data-ma]') : null;
    if (!b || b.disabled) return;
    const a = b.dataset.ma;
    closeModelMenu(a !== 'link' && a !== 'all' && a !== 'rm-all');
    if (a === 'this') H.labelThis();
    else if (a === 'all') openLabelAll();
    else if (a === 'auto') void toggleAuto();
    else if (a === 'activate') void toggleActive();
    else if (a === 'rm-this') H.removeThis();
    else if (a === 'rm-all') void removeAll();
    else if (a === 'link') void openLinkDialog();
  });
  const first = m.querySelector<HTMLButtonElement>('button:not(:disabled)');
  if (first) first.focus();
}

export function closeModelMenu(refocus = true): boolean {
  if (!M.menu) return false;
  M.menu.remove();
  M.menu = null;
  const b = document.getElementById('lb-model-btn');
  if (b) { b.setAttribute('aria-expanded', 'false'); if (refocus) b.focus(); }
  return true;
}

// the hotkey handler asks first: an open menu takes Esc and the arrows
export function modelMenuKey(combo: string, e: KeyboardEvent): boolean {
  if (!M.menu) return false;
  if (combo === 'escape') { closeModelMenu(); return true; }
  if (combo === 'arrowdown' || combo === 'arrowup') {
    const items = [...M.menu.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
    const i = items.findIndex((x) => x === document.activeElement);
    const n = items.length;
    if (n) items[(i + (combo === 'arrowdown' ? 1 : -1) + n) % n]?.focus();
    return true;
  }
  if (e.target instanceof Node && M.menu.contains(e.target)) return !['tab', 'shift+tab', 'enter', 'space'].includes(combo);
  return false;
}

// ---------------------------------------------------------------- link save / auto toggle
async function putLink(tid: string, body: unknown): Promise<LabelTask> {
  const t = await call<LabelTask>('PUT', `api/label/projects/${pid()}/tasks/${encodeURIComponent(tid)}/model`, body);
  setActiveLocal(tid, false);                     // a saved link must be activated again (the server dropped it)
  H.taskUpdated(t);
  renderButton();
  return t;
}

async function toggleAuto(): Promise<void> {
  const t = H.task();
  const link = linkOf(t);
  if (!t || !link) return;
  const on = !link.auto_on_open;
  const wasActive = isActive(t);
  try {
    await putLink(t.id, { ...link, auto_on_open: on });
    if (wasActive) {                              // only the auto setting changed: keep it active
      await call('POST', `api/label/projects/${pid()}/tasks/${encodeURIComponent(t.id)}/model/activate`, { on: true });
      setActiveLocal(t.id, true);
      renderButton();
    }
    P.toast(on ? 'The model now labels each image you open (once per image)' : 'Auto-labeling off');
  } catch (e) { P.toast(`Could not save: ${errText(e)}`, 'err'); }
}

// ---------------------------------------------------------------- small modal shell (Esc / backdrop close, focus trap)
function modal(cls: string, title: string, onClose?: () => void): Modal {
  const back = document.createElement('div');
  back.className = 'modal lb-dlg-back';
  back.innerHTML = `<div class="panel lb-dlg ${cls}" role="dialog" aria-modal="true" aria-labelledby="lbm-title" tabindex="-1">` +
    `<div class="lbm-head"><h2 id="lbm-title">${icon('robot')}<span>${esc(title)}</span></h2>` +
    `<button class="icon-btn ghost" data-a="close" aria-label="Close">${icon('x')}</button></div><div class="lbm-body"></div><div class="lb-dlg-actions lbm-foot"></div></div>`;
  document.body.appendChild(back);
  const prev = document.activeElement;
  const close = (): void => { back.remove(); if (onClose) onClose(); if (prev instanceof HTMLElement && document.contains(prev)) prev.focus(); };
  back.addEventListener('pointerdown', (e) => { if (e.target === back) close(); });
  back.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    const f = [...back.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled)')].filter((x) => x.offsetParent);
    if (!f.length) return;
    const i = f.findIndex((x) => x === document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1]?.focus(); }
    else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0]?.focus(); }
  });
  return { back, el: must<HTMLElement>(back, '.lb-dlg'), body: must<HTMLElement>(back, '.lbm-body'), foot: must<HTMLElement>(back, '.lbm-foot'), close };
}

// ---------------------------------------------------------------- link dialog
const typeInfo = (tt: string | undefined): TaskTypeInfo | undefined => Object.entries(TASK_TYPES).find(([k]) => k === tt)?.[1];
const fits = (m: DetectedModel, t: LabelTask): boolean => m.task_type === t.task_type || (!m.task_type && m.geometry === TASK_TYPES[t.task_type].geometry);
const TYPE_TEXT = (tt: string | undefined): string => typeInfo(tt)?.label || tt || '?';

export async function openLinkDialog(): Promise<void> {
  const t = H.task();
  if (!t) return;
  if (M.dlg) M.dlg.close();
  const link = linkOf(t);
  const D: LinkState = { t, link, conns: null, reg: [], connId: link ? (link.registry_id ? `reg:${link.registry_id}` : link.connection_id ?? '') : '',
    nc: { name: '', url: '', api_key: '', timeout_s: 60 },
    detect: null, status: null, modelId: link ? link.model_id : null, version: link ? link.version || null : null,
    conf: link ? +(link.conf ?? 0) || 0.25 : 0.25, auto: !!(link && link.auto_on_open), map: {}, saving: false, tok: 0 };
  const dl: LinkDialog = { ...modal('lbm', `Model for ${t.name}`, () => { M.dlg = null; }), D };
  M.dlg = dl;
  render(dl);
  dl.el.focus();
  try {
    const [d, r] = await Promise.all([getJSON<{ connections?: Connection[] }>('api/connections'),
      getJSON<{ models?: RegistryModel[] }>('api/models').catch((): { models?: RegistryModel[] } => ({ models: [] }))]);
    D.conns = (d.connections || []).filter((c) => c.kind === 'model');
    // the global models (Settings › Models: local weights and APIs, also used by the visualizer) that fit this task
    D.reg = (r.models || []).filter((m) => (m.task_type || 'object_detection') === t.task_type ||
      ((m.api || {}).preset === 'label'));
  } catch (e) { D.conns = []; D.status = { kind: 'err', text: `Could not list the connections: ${errText(e)}` }; }
  const conns = D.conns;
  const first = D.reg[0], firstConn = conns[0];
  const known = (id: string): boolean => (id && id.startsWith('reg:') ? D.reg.some((m) => `reg:${m.id}` === id) : conns.some((c) => c.id === id));
  if (!known(D.connId)) D.connId = first ? `reg:${first.id}` : firstConn ? firstConn.id : 'new';
  render(dl);
  if (D.connId !== 'new') void detect(dl);
  bindLink(dl);
}

function pickModel(D: LinkState): void {
  const ms = (D.detect && D.detect.models) || [];
  let m = ms.find((x) => x.id === D.modelId && fits(x, D.t));
  if (!m) m = ms.find((x) => fits(x, D.t));
  D.modelId = m ? m.id : null;
  const vs = m && m.versions;
  const lastV = vs && vs.length ? vs[vs.length - 1] : undefined;
  D.version = vs && lastV !== undefined ? (D.version !== null && vs.includes(D.version) ? D.version : lastV) : null;
  D.map = {};
  if (!m) return;
  const byName = new Map(D.t.classes.map((c) => [c.name.trim().toLowerCase(), c.id]));
  const ids = new Set(D.t.classes.map((c) => c.id));
  const old = D.link && D.link.model_id === m.id ? D.link.class_map || {} : {};
  for (const name of m.classes || []) {
    const o = old[name];
    if (o === 'ignore' || (o != null && ids.has(o))) D.map[name] = o;
    else D.map[name] = byName.get(String(name).trim().toLowerCase()) || 'new';
  }
}

async function detect(dl: LinkDialog): Promise<void> {
  const D = dl.D;
  D.detect = null;
  D.status = { kind: 'busy', text: 'Asking the server which models it has…' };
  render(dl);
  const tok = (D.tok = D.tok + 1);
  try {
    const body = D.connId === 'new' ? { connection: { url: D.nc.url.trim(), api_key: D.nc.api_key || undefined, timeout_s: +D.nc.timeout_s || 60 } }
      : D.connId.startsWith('reg:') ? { registry_id: D.connId.slice(4) } : { connection_id: D.connId };
    if ('registry_id' in body) D.status = { kind: 'busy', text: 'Loading the model to read its classes…' };
    render(dl);
    const r = await call<DetectPayload>('POST', 'api/label/models/detect', body);
    if (tok !== D.tok) return;
    D.detect = r;
    const n = (r.models || []).length, ok = (r.models || []).filter((m) => fits(m, D.t)).length;
    D.status = { kind: ok ? 'ok' : 'err', text: `${r.protocol === 'generic' ? 'Generic API (dataviz-label/1)' : r.protocol === 'serving' ? 'YOLO serving API' : r.protocol === 'registry' ? 'Settings › Models' : r.protocol} · ` +
      `${n} model${n === 1 ? '' : 's'}${ok < n ? `, ${ok} fit${ok === 1 ? 's' : ''} ${/^[aeiou]/i.test(TYPE_TEXT(D.t.task_type)) ? 'an' : 'a'} ${TYPE_TEXT(D.t.task_type).toLowerCase()} task` : ''}` };
    pickModel(D);
  } catch (e) {
    if (tok !== D.tok) return;
    D.status = { kind: 'err', text: errText(e) };
  }
  render(dl);
}

function statusLine(st: LinkStatus | null): string {
  if (!st) return '<div class="status-line"></div>';
  const ico = st.kind === 'busy' ? '<span class="spin"></span>' : st.kind === 'ok' ? icon('check') : st.kind === 'err' ? icon('alert') : '';
  return `<div class="status-line ${esc(st.kind)}" role="status">${ico}<span>${esc(st.text)}</span></div>`;
}

function render(dl: LinkDialog): void {
  const D = dl.D, t = D.t;
  const conns = D.conns;
  if (!conns) {
    dl.body.innerHTML = '<div class="status-line busy"><span class="spin"></span><span>loading…</span></div>';
    dl.foot.innerHTML = '';
    return;
  }
  const kindText = (m: RegistryModel): string => (m.format === 'api' ? `API · ${(m.api || {}).preset || 'generic'}` : `${m.family} · ${m.format}`);
  const regOpts = D.reg.map((m) => `<option value="reg:${esc(m.id)}" ${`reg:${m.id}` === D.connId ? 'selected' : ''}>${esc(m.name)} — ${esc(kindText(m))}</option>`).join('');
  const opts = (regOpts ? `<optgroup label="Settings › Models (shared with the visualizer)">${regOpts}</optgroup>` : '') +
    `<optgroup label="Model servers">${conns.map((c) => `<option value="${esc(c.id)}" ${c.id === D.connId ? 'selected' : ''}>${esc(c.name || c.url)} — ${esc(c.url || '')}</option>`).join('')}` +
    `<option value="new" ${D.connId === 'new' ? 'selected' : ''}>+ New model connection…</option></optgroup>`;
  const nc = D.nc;
  const newForm = D.connId !== 'new' ? '' : '<div class="lbm-grid">' +
    `<label class="lbm-fld">Name<input data-nc="name" value="${esc(nc.name)}" placeholder="e.g. GPU box detector" autocomplete="off"></label>` +
    `<label class="lbm-fld mono">Server URL<input data-nc="url" type="url" value="${esc(nc.url)}" placeholder="http://gpu-box:8000" autocomplete="off" spellcheck="false"></label>` +
    `<label class="lbm-fld mono">API key (optional)<input data-nc="api_key" type="password" value="${esc(nc.api_key)}" autocomplete="new-password" placeholder="sent as Authorization: Bearer"></label>` +
    `<label class="lbm-fld mono">Timeout (s)<input data-nc="timeout_s" inputmode="numeric" value="${esc(nc.timeout_s)}"></label></div>`;
  const s1 = `<section class="lbm-sec"><h3>1 · Model source</h3><label class="lbm-fld">Model<select data-d="conn">${opts}</select></label>${newForm}` +
    '<p class="lb-mnote">Local weights (.pt, ONNX, OpenVINO…) and model APIs are added in Settings › Models; they also run in the visualizer. ' +
    `A linked model does nothing until you activate it for the session. <button class="ghost lbm-sm" data-a="models">${icon('gear')}Settings › Models…</button></p>` +
    `<div class="lbm-row"><button data-a="test">${icon('plug')}Test</button>${statusLine(D.status)}</div></section>`;
  let s2 = '', s3 = '';
  const ms = (D.detect && D.detect.models) || [];
  if (D.detect) {
    s2 = '<section class="lbm-sec"><h3>2 · Model</h3><div class="lbm-models" role="radiogroup" aria-label="Model">' + (ms.length ? ms.map((m) => {
      const ok = fits(m, t);
      return `<label class="lbm-model${m.id === D.modelId ? ' sel' : ''}${ok ? '' : ' off'}"><input type="radio" name="lbm-model" value="${esc(m.id)}" data-d="model" ${m.id === D.modelId ? 'checked' : ''} ${ok ? '' : 'disabled'}>` +
        `<span class="nm"><b>${esc(m.name || m.id)}</b><span class="muted mono">${esc(m.id)}</span></span>` +
        `<span class="tt${ok ? '' : ' bad'}">${esc(TYPE_TEXT(m.task_type))}${ok ? '' : ' · does not fit this task'}</span>` +
        `<span class="muted">${(m.classes || []).length} class${(m.classes || []).length === 1 ? '' : 'es'}</span></label>`;
    }).join('') : '<p class="muted">The server lists no models.</p>') + '</div>';
    const m = ms.find((x) => x.id === D.modelId);
    if (m && m.versions && m.versions.length > 1) {
      s2 += `<label class="lbm-fld lbm-inline">Version<select data-d="version">${m.versions.map((v) => `<option ${v === D.version ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>`;
    }
    s2 += '</section>';
    if (m) {
      const vals = Object.values(D.map);
      const nNew = vals.filter((v) => v === 'new').length, nIgn = vals.filter((v) => v === 'ignore').length;
      const cls = t.classes;
      const rows = (m.classes || []).map((name, i) => {
        const v = D.map[name];
        const o = cls.map((c) => `<option value="${esc(c.id)}" ${String(v) === String(c.id) ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
        return `<tr><td class="mono">${esc(name)}</td><td>${icon('arrow-r')}</td><td><select data-d="map" data-i="${i}" aria-label="${esc(`Task class for the model class ${name}`)}" class="${v === 'new' ? 'new' : v === 'ignore' ? 'ign' : ''}">` +
          `${o ? `<optgroup label="Task classes">${o}</optgroup>` : ''}<option value="new" ${v === 'new' ? 'selected' : ''}>+ Add as new class</option>` +
          `<option value="ignore" ${v === 'ignore' ? 'selected' : ''}>Ignore (drop these)</option></select></td></tr>`;
      }).join('');
      s3 = `<section class="lbm-sec"><h3>3 · Classes <span class="muted">${vals.length - nNew - nIgn} matched · ${nNew} new · ${nIgn} ignored</span>` +
        `<span class="grow"></span><button class="ghost lbm-sm" data-a="all-new">Unmatched → new</button><button class="ghost lbm-sm" data-a="all-ign">Unmatched → ignore</button></h3>` +
        `<div class="lbm-map"><table><thead><tr><th>Model class</th><th></th><th>Task class</th></tr></thead><tbody>${rows}</tbody></table></div></section>` +
        '<section class="lbm-sec"><h3>4 · Options</h3>' +
        `<label class="lbm-fld lbm-inline">Minimum confidence<span class="lab-range"><input type="range" min="0.05" max="0.95" step="0.05" value="${esc(D.conf)}" data-d="conf">` +
        `<output class="num">${esc(D.conf.toFixed(2))}</output></span></label>` +
        `<label class="check"><input type="checkbox" data-d="auto" ${D.auto ? 'checked' : ''}> Auto-label each image I open (once per image)</label></section>`;
    }
  }
  dl.body.innerHTML = s1 + s2 + s3;
  const canSave = !!(D.detect && D.modelId) && !D.saving && (D.connId !== 'new' || nc.url.trim());
  dl.foot.innerHTML = (D.link ? `<button class="danger" data-a="unlink" ${D.saving ? 'disabled' : ''}>${icon('trash')}Unlink</button><span class="grow"></span>` : '') +
    `<button data-a="close">Cancel</button><button class="primary" data-a="save" ${canSave ? '' : 'disabled'}>${D.saving ? '<span class="spin"></span>' : icon('check')}Save</button>`;
}

const NC_KEYS = ['name', 'url', 'api_key', 'timeout_s'] as const;
type NcKey = typeof NC_KEYS[number];
const ncKey = (k: string | undefined): NcKey | null => NC_KEYS.find((x) => x === k) ?? null;

function bindLink(dl: LinkDialog): void {
  const D = dl.D;
  dl.el.addEventListener('input', (e) => {
    const f = e.target;
    if (!(f instanceof HTMLInputElement)) return;
    const nk = ncKey(f.dataset.nc);
    if (nk) {
      D.nc[nk] = f.value;
      if (nk === 'url') { const sv = dl.foot.querySelector<HTMLButtonElement>('[data-a="save"]'); if (sv) sv.disabled = !(D.detect && D.modelId && f.value.trim()); }
    } else if (f.dataset.d === 'conf') {
      D.conf = +f.value;
      const out = f.parentElement && f.parentElement.querySelector('output');
      if (out) out.textContent = D.conf.toFixed(2);
    }
  });
  dl.el.addEventListener('change', (e) => {
    const f = e.target;
    if (!(f instanceof HTMLInputElement || f instanceof HTMLSelectElement)) return;
    const k = f.dataset.d;
    if (k === 'conn') {
      D.connId = f.value; D.detect = null; D.status = null;
      const lk = D.link;
      const same = lk && (lk.connection_id === D.connId || `reg:${lk.registry_id}` === D.connId);
      D.modelId = lk && same ? lk.model_id : null;
      render(dl);
      if (D.connId !== 'new') void detect(dl);
    }
    else if (k === 'model') { D.modelId = f.value; pickModel(D); render(dl); }
    else if (k === 'version') D.version = f.value;
    else if (k === 'map') {
      const m = D.detect && D.detect.models.find((x) => x.id === D.modelId);
      const name = m && m.classes && m.classes[+(f.dataset.i ?? -1)];
      if (name === undefined || name === null) return;
      const c = D.t.classes.find((x) => String(x.id) === f.value);
      D.map[name] = c ? c.id : f.value;
      render(dl);
      const sel = dl.body.querySelector<HTMLSelectElement>(`[data-d="map"][data-i="${+(f.dataset.i ?? -1)}"]`);
      if (sel) sel.focus();
    } else if (k === 'auto' && f instanceof HTMLInputElement) D.auto = f.checked;
  });
  dl.el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target instanceof HTMLElement && e.target.dataset.nc) { e.preventDefault(); void detect(dl); }
  });
  dl.el.addEventListener('click', async (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLButtonElement>('[data-a]') : null;
    if (!b || b.disabled) return;
    const a = b.dataset.a;
    if (a === 'close') dl.close();
    else if (a === 'models') { dl.close(); void import('../settings/dialog.js').then((m) => m.openSettings('models')); }
    else if (a === 'test') {
      if (D.connId === 'new' && !D.nc.url.trim()) { D.status = { kind: 'err', text: 'Enter the server URL first' }; render(dl); return; }
      void detect(dl);
    } else if (a === 'all-new' || a === 'all-ign') {
      const ids = new Set(D.t.classes.map((c) => c.id));
      for (const [k, v] of Object.entries(D.map)) if (!ids.has(v)) D.map[k] = a === 'all-new' ? 'new' : 'ignore';
      render(dl);
    } else if (a === 'save') void saveLink(dl);
    else if (a === 'unlink') {
      if (!confirm(`Unlink the model from “${D.t.name}”? The labels it made stay.`)) return;
      try { await putLink(D.t.id, null); dl.close(); P.toast('Model unlinked'); } catch (err) { D.status = { kind: 'err', text: errText(err) }; render(dl); }
    }
  });
}

async function saveLink(dl: LinkDialog): Promise<void> {
  const D = dl.D;
  D.saving = true;
  render(dl);
  try {
    let cid = D.connId;
    const nNewReg = Object.values(D.map).filter((v) => v === 'new').length;
    if (cid.startsWith('reg:')) {
      const rid = cid.slice(4);
      await putLink(D.t.id, { registry_id: rid, model_id: rid, conf: D.conf, auto_on_open: D.auto, class_map: { ...D.map } });
      dl.close();
      P.toast(`Model linked${nNewReg ? ` · ${nNewReg} new class${nNewReg === 1 ? '' : 'es'} added` : ''}. Activate it (${activateKey()}) to let it label.`);
      return;
    }
    if (cid === 'new') {
      const nc = D.nc;
      const c = await call<{ id?: string }>('POST', 'api/connections', { kind: 'model', name: nc.name.trim() || nc.url.trim(), url: nc.url.trim(),
        api_key: nc.api_key || '', timeout_s: +nc.timeout_s || 60 });
      if (!c.id) throw new Error('the connection was not saved');
      cid = c.id;
    }
    const nNew = Object.values(D.map).filter((v) => v === 'new').length;
    await putLink(D.t.id, { connection_id: cid, model_id: D.modelId, version: D.version || undefined, conf: D.conf,
      auto_on_open: D.auto, class_map: { ...D.map } });
    dl.close();
    P.toast(`Model linked${nNew ? ` · ${nNew} new class${nNew === 1 ? '' : 'es'} added` : ''}. Activate it (${activateKey()}), then ${prettyCombo(combosFor('model_this')[0] || 'm')} labels this image.`);
  } catch (e) {
    D.saving = false;
    D.status = { kind: 'err', text: `Not saved: ${errText(e)}` };
    render(dl);
  }
}

// ---------------------------------------------------------------- label all images (job)
const SCOPES: Array<[string, string, string]> = [
  ['unlabeled', 'Images with no labels yet', 'in this task'],
  ['not_done', 'Every image not marked done', 'its old model labels are replaced; your own labels stay'],
  ['all', 'Every image, done ones too', 'its old model labels are replaced; your own labels stay'],
];

function openLabelAll(): void {
  const t = H.task();
  const link = linkOf(t);
  if (!t || !link || M.job) return;
  const dl = modal('lbm lbm-all', 'Label all images');
  let conf = +(link.conf ?? 0) || 0.25;
  dl.body.innerHTML = `<p>The model <b>${esc(link.model_id)}</b> labels the images of <b>${esc(t.name)}</b> in the background. ` +
    'You can keep labeling meanwhile; the progress shows in the model button.</p>' +
    '<fieldset class="lbm-scope"><legend>Which images</legend>' + SCOPES.map(([v, a, b], i) =>
      `<label class="radio-row"><input type="radio" name="lbm-scope" value="${v}" ${i === 0 ? 'checked' : ''}><span><b>${esc(a)}</b><span class="muted">${esc(b)}</span></span></label>`).join('') +
    '</fieldset>' +
    `<label class="lbm-fld lbm-inline">Minimum confidence<span class="lab-range"><input type="range" min="0.05" max="0.95" step="0.05" value="${esc(conf)}" data-d="conf">` +
    `<output class="num">${esc(conf.toFixed(2))}</output></span></label><div class="lbm-err"></div>`;
  dl.foot.innerHTML = `<button data-a="close">Cancel</button><button class="primary" data-a="start">${icon('play')}Start</button>`;
  dl.el.addEventListener('input', (e) => {
    const f = e.target;
    if (!(f instanceof HTMLInputElement) || f.dataset.d !== 'conf') return;
    conf = +f.value;
    const out = f.parentElement && f.parentElement.querySelector('output');
    if (out) out.textContent = conf.toFixed(2);
  });
  dl.el.addEventListener('click', async (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLButtonElement>('[data-a]') : null;
    if (!b || b.disabled) return;
    if (b.dataset.a === 'close') { dl.close(); return; }
    b.disabled = true;
    const scope = must<HTMLInputElement>(dl.el, 'input[name="lbm-scope"]:checked').value;
    try {
      const r = await call<{ job_id: number }>('POST', `api/label/projects/${pid()}/predict-all`, { task_id: t.id, scope, conf });
      dl.close();
      void trackJob(r.job_id, predictJobKey(t.id));
    } catch (err) {
      b.disabled = false;
      must(dl.el, '.lbm-err').innerHTML = statusLine({ kind: 'err', text: errText(err) });
    }
  });
  must<HTMLButtonElement>(dl.el, '[data-a="start"]').focus();
}

// the server's job key of a label-all run (routes/labeling.py predict_all)
const predictJobKey = (tid: string): string => `label-predict:${L.pid}:${tid}`;

// a done job's public result: {cancelled?, n_images?, n_shapes?, failed?}
const bulkResult = (r: unknown): BulkResult => (isRec(r) ? r : {});

async function trackJob(jid: number, key: string, resumed = false): Promise<void> {
  if (M.job && M.job.id === jid) return;
  const mj: ModelJob = { id: jid, done: 0, total: null, status: 'queued' };
  M.job = mj;
  try { sessionStorage.setItem(jobKey(), JSON.stringify({ id: jid, key })); } catch (e) { /* private mode */ }
  renderButton();
  let lastDone = -1, lastStale = performance.now();
  for (;;) {
    let j: Job;
    try { j = await getJSON<Job>(`api/jobs/${encodeURIComponent(jid)}`); } catch (e) {
      if (/: 404 /.test(errMessage(e))) { endJob(); if (!resumed) P.toast('The labeling job is gone (server restarted?)', 'err'); return; }
      await new Promise((r) => setTimeout(r, 2000));
      continue;
    }
    if (!M.job || M.job.id !== jid) return;
    if (j.key !== key) { endJob(); return; }      // the id now names another job (the server restarted): not ours
    Object.assign(M.job, { done: j.done, total: j.total, pct: j.pct, eta: j.eta, status: j.status });
    renderButton();
    // labels land on images the page may hold in its LRU: drop their annotations every few seconds (bitmaps stay)
    if (j.done !== lastDone && performance.now() - lastStale > 4000) { H.staleAnns(); lastStale = performance.now(); }
    lastDone = j.done;
    if (j.status === 'done' || j.status === 'error') {
      endJob();
      if (j.status === 'error') P.toast(`Labeling all images failed: ${j.error || 'error'}`, 'err');
      else {
        const r = bulkResult(j.result);
        P.toast(`${r.cancelled ? 'Stopped: ' : ''}the model labeled ${r.n_images ?? '?'} image${r.n_images === 1 ? '' : 's'} (${r.n_shapes ?? '?'} shapes)` +
          `${r.failed ? ` · ${r.failed} failed` : ''}`, r.failed ? 'warn' : '');
      }
      H.afterBulk();
      return;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
}

function endJob(): void {
  M.job = null;
  try { sessionStorage.removeItem(jobKey()); } catch (e) { /* private mode */ }
  renderButton();
}

async function cancelJob(): Promise<void> {
  if (!M.job || M.job.cancelling) return;
  M.job.cancelling = true;
  renderButton();
  try { await call('POST', `api/jobs/${encodeURIComponent(M.job.id)}/cancel`); }
  catch (e) { if (M.job) M.job.cancelling = false; renderButton(); P.toast(`Could not cancel: ${errText(e)}`, 'err'); }
}

export const jobRunning = (): boolean => !!M.job;

// ---------------------------------------------------------------- remove model labels from every image
async function removeAll(): Promise<void> {
  const t = H.task();
  if (!t) return;
  const n = L.ids.length;
  // the count: class-stats `n_model` / `n_model_images` per class when the server gives them, else the image count
  let what = `Every label the model made in <b>${esc(t.name)}</b> is deleted, on all ${n.toLocaleString()} image${n === 1 ? '' : 's'}.`;
  try {
    const st = (await getJSON<{ stats?: Record<string, ClassStat> }>(`api/label/projects/${pid()}/class-stats`)).stats || {};
    const rows = t.classes.map((c) => st[c.id]).filter((r): r is ClassStat => !!r && r.n_model != null);
    if (rows.length) {
      const nm = rows.reduce((a, r) => a + (r.n_model ?? 0), 0);
      if (!nm) { P.toast(`No model labels in ${t.name}`); return; }
      const ni = rows.some((r) => r.n_model_images != null) ? Math.max(...rows.map((r) => r.n_model_images || 0)) : null;
      what = `<b>${nm.toLocaleString()} model label${nm === 1 ? '' : 's'}</b> in <b>${esc(t.name)}</b>${ni != null ? ` (on at least ${ni.toLocaleString()} image${ni === 1 ? '' : 's'})` : ''} are deleted.`;
    }
  } catch (e) { /* no stats: the generic text */ }
  const res = await P.ask({
    title: 'Remove model labels from all images?',
    html: `<p>${what} Labels you drew or edited stay. This cannot be undone from this page.</p>`,
    buttons: [{ id: 'yes', label: 'Remove model labels', danger: true }, { id: 'no', label: 'Cancel' }],
  });
  if (!res || res.id !== 'yes') return;
  try {
    const r = await call<{ removed: number; images: number }>('POST', `api/label/projects/${pid()}/remove-model-labels`, { task_id: t.id });
    P.toast(`Removed ${r.removed} model label${r.removed === 1 ? '' : 's'} on ${r.images} image${r.images === 1 ? '' : 's'}`);
    H.afterBulk();
  } catch (e) { P.toast(`Could not remove: ${errText(e)}`, 'err'); }
}
