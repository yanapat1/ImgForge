// The label page's panels: task tabs + class list (left), objects of this image + the virtual image strip (right),
// top bar, status bar, the class context menu, a small dialog, toasts. Every list uses one delegated listener on its
// container (data-act / data-id); every interpolated value goes through esc(). Renders are coalesced per frame
// with schedule(part).

import { $, $req, esc, icon } from '../core/dom.js';
import type { ToastKind } from '../core/toast.js';
import type { AskOptions, AskResult, ClassMatchMode, MediaPage, ProjectStatsRow } from '../types-label-ui.js';
import type { Ann, LabelTask, Media, PointerPos, TaskClass, TaskType, Tool } from '../types-label.js';
import { CLS, clsOf, indexClasses, invalidate, D, TASKS } from './canvas.js';
import { combosFor, prettyCombo } from './hotkeys.js';
import { openImportDialog } from './import.js';
import { openDuplicate, openManageClasses } from './session-actions.js';
import { HOLD_ACTIONS, L, TASK_TYPES } from './state.js';
import { asEl, closestEl } from './ui-util.js';

/** What app.ts gives the panels. */
export interface PanelHandlers {
  tool(): Tool | null;
  selectClass(classId: string, fromClick?: boolean): void;
  switchTask(taskId: string): void;
  goToId(mediaId: string): void;
  selectAnn(id: string, add: boolean): void;
  hoverAnn(id: string | null): void;
  reassign(id: string, classId: string): void;
  toggleHidden(id: string): void;
  deleteAnn(id: string): void;
  classesChanged(reload?: boolean): void;
  flushAll(): Promise<void>;
  overlaysChanged(): void;
}
const NO_HANDLERS: PanelHandlers = {
  tool: () => null, selectClass: () => {}, switchTask: () => {}, goToId: () => {}, selectAnn: () => {}, hoverAnn: () => {},
  reassign: () => {}, toggleHidden: () => {}, deleteAnn: () => {}, classesChanged: () => {}, flushAll: async () => {}, overlaysChanged: () => {},
};
let H: PanelHandlers = NO_HANDLERS;
export function initPanels(handlers: PanelHandlers): void { H = handlers; bind(); }

type PanelPart = 'tasks' | 'classes' | 'objects' | 'top' | 'status' | 'strip' | 'banner';
interface PanelState {
  collapsed: Set<string>; filter: string; menu: { el: HTMLElement; c: TaskClass | null } | null; recording: TaskClass | null;
  stats: Record<string, { id: string; done?: number | undefined }>; strip: string;
  // the image strip's filter (GET media: q / cls / cls_mode / min_ann / max_ann, relative to the active task)
  sq: string; scls: Set<string>; smode: ClassMatchMode; smin: string; smax: string;
}
const P: PanelState = { collapsed: new Set(), filter: '', menu: null, recording: null, stats: {}, strip: 'all',
  sq: '', scls: new Set(), smode: 'any', smin: '', smax: '' };
const okColor = (c: string | null | undefined): string => (c && /^#[0-9a-f]{6}$/i.test(c) ? c : '#888888');
const pid = (): string => encodeURIComponent(L.pid ?? '');
export const activeTask = (): LabelTask | null => (L.taskId === null ? null : TASKS.get(L.taskId) || null);
const taskTypeInfo = (t: LabelTask): { label: string; icon: string } => (TASK_TYPES as Partial<Record<TaskType, { label: string; icon: string }>>)[t.task_type] || { label: t.task_type, icon: 'tag' };
const detailOf = (data: { detail?: unknown }, fallback: string): string => (data.detail ? String(data.detail) : fallback);

interface ReqResult<T> { ok: boolean; status: number; data: Partial<T> & { detail?: unknown } }
async function req<T extends object = object>(method: string, path: string, body?: unknown): Promise<ReqResult<T>> {
  const r = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : null });
  const data: Partial<T> & { detail?: unknown } = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

// ---------------------------------------------------------------- scheduling
const parts = new Set<PanelPart>();
let raf = 0;
export function schedule(...ps: PanelPart[]): void {
  for (const p of ps) parts.add(p);
  if (!raf) raf = requestAnimationFrame(flushRender);
}
function flushRender(): void {
  raf = 0;
  const ps = new Set(parts);
  parts.clear();
  if (ps.has('tasks')) renderTasks();
  if (ps.has('classes')) renderClasses();
  if (ps.has('objects')) renderObjects();
  if (ps.has('top')) renderTop();
  if (ps.has('status')) renderStatus();
  if (ps.has('strip')) renderStrip();
  if (ps.has('banner')) renderBanner();
}
export const renderAll = (): void => schedule('tasks', 'classes', 'objects', 'top', 'status', 'strip', 'banner');

// ---------------------------------------------------------------- task tabs
export function setStats(projectRow: ProjectStatsRow | null | undefined): void {
  P.stats = {};
  for (const t of (projectRow && projectRow.tasks) || []) P.stats[t.id] = t;
  schedule('tasks');
}

function renderTasks(): void {
  const n = L.project ? L.project.n_media || L.ids.length : 0;
  $req('lb-tasks').innerHTML = ((L.project && L.project.tasks) || []).map((t, i) => {
    const st = P.stats[t.id];
    const pct = st && n ? Math.round(100 * (st.done || 0) / n) : null;
    const tt = taskTypeInfo(t);
    const key = combosFor(`task_${i + 1}`)[0];
    const on = t.id === L.taskId;
    return `<button class="lb-task${on ? ' active' : ''}" role="tab" aria-selected="${on}" data-task="${esc(t.id)}"
      title="${esc(`${tt.label}: ${t.name}${key ? ` (${prettyCombo(key)})` : ''}`)}">${icon(tt.icon)}<span class="nm">${esc(t.name)}</span>` +
      `${pct != null ? `<span class="pct num">${pct}%</span>` : ''}${key ? `<kbd>${esc(prettyCombo(key))}</kbd>` : ''}</button>`;
  }).join('');
}

// ---------------------------------------------------------------- class list
function countsOnImage(): Map<string, number> {
  const m = new Map<string, number>();
  for (const a of L.anns.values()) m.set(a.class_id, (m.get(a.class_id) || 0) + 1);
  return m;
}

export function classKey(c: TaskClass, i: number): string {
  if (c.hotkey) return c.hotkey;
  return combosFor(`class_${i + 1}`)[0] || '';       // [] past the last class key
}

function renderClasses(): void {
  const t = activeTask();
  const el = $req('lb-classes');
  if (!t) { el.innerHTML = '<div class="lb-empty">No task</div>'; return; }
  const cnt = countsOnImage();
  const f = P.filter.trim().toLowerCase();
  const tagged = t.task_type === 'classification';
  const rows = t.classes.map((c, i): [TaskClass, number] => [c, i]).filter(([c]) => !f || c.name.toLowerCase().includes(f));
  el.innerHTML = rows.map(([c, i]) => {
    const k = classKey(c, i);
    const alias = c.hotkey ? '' : combosFor(`class_${i + 1}`).slice(1).map(prettyCombo).join(', ');
    const n = cnt.get(c.id) || 0;
    const on = c.id === L.classId;
    return `<div class="lb-cls${on ? ' active' : ''}${tagged && n ? ' tagged' : ''}" role="option" aria-selected="${on}" tabindex="-1" data-cls="${esc(c.id)}"
      title="${esc(c.name)}${k ? ` · ${esc(prettyCombo(k))}${alias ? ` (also ${esc(alias)})` : ''}` : ''} · double-click to rename, right-click for more">
      <span class="sw" style="--c:${okColor(c.color)}"></span><span class="nm">${esc(c.name)}</span>` +
      `${tagged ? `<span class="lb-check">${n ? icon('check') : ''}</span>` : ''}` +
      `${k ? `<kbd>${esc(prettyCombo(k))}</kbd>` : ''}<span class="n num${n ? '' : ' zero'}">${n}</span></div>`;
  }).join('') || `<div class="lb-empty">${t.classes.length ? 'No class matches' : 'No classes yet: type a name above and press Enter'}</div>`;
}

async function addClass(name: string): Promise<void> {
  const t = activeTask();
  name = name.trim();
  if (!t || !name) return;
  const ex = t.classes.find((c) => c.name.toLowerCase() === name.toLowerCase());
  if (ex) { H.selectClass(ex.id); return; }
  const r = await req<{ classes: TaskClass[] }>('POST', `api/label/projects/${pid()}/tasks/${encodeURIComponent(t.id)}/classes`, { names: [name] });
  if (!r.ok) { toast(detailOf(r.data, 'Could not add the class'), 'err'); return; }
  for (const c of r.data.classes || []) if (!t.classes.some((x) => x.id === c.id)) t.classes.push(c);
  indexClasses();
  const c = (r.data.classes || [])[0];
  if (c) H.selectClass(c.id);
  schedule('classes', 'objects');
}

async function patchClass(c: TaskClass, patch: Partial<Pick<TaskClass, 'name' | 'color' | 'hotkey'>>): Promise<boolean> {
  const r = await req<TaskClass>('PATCH', `api/label/projects/${pid()}/classes/${encodeURIComponent(c.id)}`, patch);
  if (!r.ok) { toast(detailOf(r.data, 'Could not update the class'), 'err'); return false; }
  Object.assign(c, r.data && r.data.id ? r.data : patch);
  indexClasses();
  invalidate(D.SHAPES | D.TAGS | D.UI);
  schedule('classes', 'objects', 'status', 'banner');
  return true;
}

async function deleteClass(c: TaskClass): Promise<void> {
  const t = activeTask();
  if (!t) return;
  const others = t.classes.filter((x) => x.id !== c.id);
  const url = (q: string): string => `api/label/projects/${pid()}/classes/${encodeURIComponent(c.id)}${q}`;
  const ok = await ask({ title: `Delete class “${c.name}”?`, html: '<p class="muted">Shapes of this class on other images are handled next if there are any.</p>',
    buttons: [{ id: 'yes', label: 'Delete', danger: true }, { id: 'no', label: 'Cancel' }] });
  if (!ok || ok.id !== 'yes') return;
  await H.flushAll();
  let r: ReqResult<object> = await req('DELETE', url(''));
  if (r.status === 409) {
    const res = await ask({
      title: `“${c.name}” is still used`,
      html: `<p>${esc(r.data.detail || 'Some annotations use this class.')}</p>` + (others.length
        ? `<label class="field lb-dlg-field">Reassign them to <select name="to">${others.map((o) => `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join('')}</select></label>` : ''),
      buttons: [...(others.length ? [{ id: 'reassign', label: 'Reassign', primary: true }] : []), { id: 'delete', label: 'Delete the annotations', danger: true }, { id: 'no', label: 'Cancel' }],
    });
    if (!res || res.id === 'no') return;
    r = await req('DELETE', url(res.id === 'reassign' ? `?reassign_to=${encodeURIComponent(res.values['to'] ?? '')}` : '?delete_annotations=1'));
  }
  if (!r.ok) { toast(detailOf(r.data, 'Could not delete the class'), 'err'); return; }
  t.classes = others;
  indexClasses();
  if (L.classId === c.id) L.classId = others[0]?.id ?? null;
  H.classesChanged(true);
}

// ---------------------------------------------------------------- manage classes (bulk delete) + the panel's ⋯ menu
// opens the shared dialog; after a delete the page drops the classes from L.project and reloads like a class delete
export function manageClasses(): void {
  if (!L.pid) return;
  void openManageClasses({
    pid: L.pid, project: L.project,
    beforeDelete: () => H.flushAll(),
    onDone: (res) => {
      const gone = new Set(res.class_ids || []);
      for (const t of (L.project && L.project.tasks) || []) t.classes = (t.classes || []).filter((c) => !gone.has(c.id));
      indexClasses();
      const t = activeTask();
      if (L.classId !== null && gone.has(L.classId)) L.classId = t?.classes[0]?.id ?? null;
      H.classesChanged(true);
      toast(`Deleted ${res.deleted != null ? res.deleted : gone.size} class${(res.deleted != null ? res.deleted : gone.size) === 1 ? '' : 'es'}` +
        `${res.annotations ? ` and ${res.annotations} label${res.annotations === 1 ? '' : 's'}` : ''}`);
    },
  });
}

// duplicate this session (the page's project menu can call it); the copy opens in a new tab when done
export function duplicateSession(): void {
  if (!L.pid) return;
  openDuplicate({ pid: L.pid, project: L.project, onDone: (p) => {
    if (p && p.id) toast(`Created “${p.name}”`);
    document.dispatchEvent(new CustomEvent('sessions-changed'));
  } });
}

// import existing annotations (YOLO, COCO, CSV, database) into a task; the page reloads to show them
export function importAnnotations(): void {
  const importPid = L.pid;
  if (!importPid) return;
  void Promise.resolve(H.flushAll()).catch(() => {}).then(() => openImportDialog({ pid: importPid, onDone: (res) => {
    if (res && res.annotations) { toast(`Imported ${res.annotations} label${res.annotations === 1 ? '' : 's'}: reloading`); setTimeout(() => location.reload(), 600); }
  } }));
}

function openPanelMenu(btn: HTMLElement): void {
  closeMenu();
  const m = document.createElement('div');
  m.id = 'lb-menu';
  m.className = 'lb-menu panel';
  m.setAttribute('role', 'menu');
  m.innerHTML = `<button role="menuitem" data-m="manage">${icon('layers')}Manage classes…</button>` +
    `<button role="menuitem" data-m="import">${icon('upload')}Import annotations…</button>`;
  document.body.appendChild(m);
  const r = btn.getBoundingClientRect(), mr = m.getBoundingClientRect();
  m.style.left = `${Math.max(8, Math.min(r.left, innerWidth - mr.width - 8))}px`;
  m.style.top = `${Math.min(r.bottom + 4, innerHeight - mr.height - 8)}px`;
  P.menu = { el: m, c: null };
  btn.setAttribute('aria-expanded', 'true');
  m.addEventListener('click', (e) => {
    const b = closestEl(e.target, '[data-m]');
    if (!b) return;
    closeMenu();
    if (b.dataset['m'] === 'manage') manageClasses();
    else if (b.dataset['m'] === 'import') importAnnotations();
  });
  m.querySelector('button')?.focus();
}

// ---------------------------------------------------------------- context menu (class: rename, colour, hotkey, delete)
function openMenu(c: TaskClass, x: number, y: number): void {
  closeMenu();
  const m = document.createElement('div');
  m.id = 'lb-menu';
  m.className = 'lb-menu panel';
  m.setAttribute('role', 'menu');
  m.innerHTML = `<div class="lb-menu-head"><span class="sw" style="--c:${okColor(c.color)}"></span>${esc(c.name)}</div>
    <button role="menuitem" data-m="rename">${icon('edit')}Rename</button>
    <label class="lb-menu-color" role="menuitem">${icon('circle')}Colour<input type="color" data-m="color" value="${okColor(c.color)}" aria-label="Class colour"></label>
    <button role="menuitem" data-m="hotkey">${icon('keyboard')}<span>${c.hotkey ? `Hotkey <kbd>${esc(prettyCombo(c.hotkey))}</kbd>` : 'Set hotkey…'}</span></button>
    ${c.hotkey ? `<button role="menuitem" data-m="nohotkey">${icon('x')}Clear hotkey</button>` : ''}
    <button role="menuitem" class="danger" data-m="delete">${icon('trash')}Delete…</button>
    <button role="menuitem" data-m="manage">${icon('layers')}Manage classes…</button>`;
  document.body.appendChild(m);
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.min(x, innerWidth - r.width - 8)}px`;
  m.style.top = `${Math.min(y, innerHeight - r.height - 8)}px`;
  P.menu = { el: m, c };
  m.addEventListener('click', (e) => {
    const b = closestEl(e.target, '[data-m]');
    if (!b || b.tagName === 'INPUT') return;
    const act = b.dataset['m'];
    if (act === 'rename') { closeMenu(); startRename(c.id); }
    else if (act === 'hotkey') { P.recording = c; const sp = b.querySelector('span'); if (sp) sp.textContent = 'Press a key combo… (Esc cancels)'; }
    else if (act === 'nohotkey') { closeMenu(); void patchClass(c, { hotkey: null }); }
    else if (act === 'delete') { closeMenu(); void deleteClass(c); }
    else if (act === 'manage') { closeMenu(); manageClasses(); }
  });
  m.addEventListener('change', (e) => { const t = e.target; if (t instanceof HTMLInputElement && t.dataset['m'] === 'color') { void patchClass(c, { color: t.value }); } });
  m.querySelector('button')?.focus();
}

export function closeMenu(): boolean {
  if (!P.menu) return false;
  const mb = $('lb-cls-more');
  if (mb) mb.setAttribute('aria-expanded', 'false');
  P.menu.el.remove();
  P.menu = null;
  P.recording = null;
  return true;
}

// a class hotkey is being recorded (hotkeys.js lets the key through to menuKey, past the hold key)
export const recording = (): boolean => !!P.recording;

// called first by the hotkey handler: the open menu / hotkey recording take the key
export function menuKey(combo: string, e: KeyboardEvent): boolean {
  if (P.recording) {
    if (combo === 'escape') { closeMenu(); return true; }
    if (/^(ctrl|alt|shift)$/.test(combo)) return true;
    const c = P.recording;
    closeMenu();
    // a hold key (area select) is matched by its bare key before class hotkeys: Q, Shift+Q… would never reach the class
    const bare = combo.replace(/^((ctrl|alt|shift)\+)+(?=.)/, '');
    const hold = [...HOLD_ACTIONS].find((h) => combosFor(h).some((x) => x && x.replace(/^((ctrl|alt|shift)\+)+(?=.)/, '') === bare));
    if (hold) { toast(`${prettyCombo(combo)} is taken by the area-select hold key (${prettyCombo(bare)}); pick another key`, 'err'); return true; }
    const t = activeTask();
    const clash = t && t.classes.find((x) => x.id !== c.id && x.hotkey === combo);
    if (clash) void patchClass(clash, { hotkey: null });
    void patchClass(c, { hotkey: combo });
    return true;
  }
  if (P.menu) {
    if (combo === 'escape') { closeMenu(); return true; }
    if (e.target instanceof Node && P.menu.el.contains(e.target)) return combo !== 'tab' && combo !== 'shift+tab' && combo !== 'enter' && combo !== 'space' ? true : false;
  }
  return false;
}

function startRename(cid: string): void {
  const row = $req('lb-classes').querySelector(`[data-cls="${cid}"]`);
  const c = CLS.get(cid) && activeTask()?.classes.find((x) => x.id === cid);
  if (!row || !c) return;
  const nm = row.querySelector('.nm');
  if (!nm) return;
  nm.innerHTML = `<input class="lb-rename" value="${esc(c.name)}" aria-label="Class name" maxlength="120">`;
  const inp = nm.querySelector('input');
  if (!inp) return;
  inp.focus(); inp.select();
  let done = false;
  const finish = (save: boolean): void => {
    if (done) return;
    done = true;
    const v = inp.value.trim();
    if (save && v && v !== c.name) void patchClass(c, { name: v }); else schedule('classes');
  };
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
  });
  inp.addEventListener('blur', () => finish(true));
}

// ---------------------------------------------------------------- objects of this image
const TYPE_ICON: Record<string, string> = { bbox: 'box', polygon: 'polygon', keypoints: 'keypoint', tag: 'tag' };

// Rows render in chunks: the first OBJ_CHUNK now, the rest when a group's sentinel scrolls near (an image can hold
// 2000 shapes; parsing + laying out all their rows at once is a >100 ms task). The pending rows are kept as
// [annotation, number] and turned into HTML only when they go in, so their sel / hov marks are current. Hover never
// renders rows; a selection that must scroll to an unrendered row renders up to it in idle slices of OBJ_SLICE rows.
const OBJ_CHUNK = 60, OBJ_SLICE = 40;
const objRest = new Map<string, Array<[Ann, number]>>();       // task id -> [[annotation, k]] not in the DOM yet
let objIO: IntersectionObserver | null = null, revealJob = 0;

function objRowHtml(a: Ann, k: number): string {
  const c = clsOf(a);
  const hid = L.hidden.has(a.id);
  const cls = `lb-obj${L.selected.has(a.id) ? ' sel' : ''}${a.id === L.hoverId ? ' hov' : ''}${hid ? ' off' : ''}`;
  const kp = a.geometry_type === 'keypoints' ? ` ${a.geometry.points.filter((q) => q[2]).length}/${a.geometry.points.length}` : '';
  let model = '';
  if (a.source === 'model') {
    const t = TASKS.get(a.task_id);
    const mname = t?.config.model?.model_id;
    const sc = typeof a.score === 'number' ? a.score.toFixed(2) : '';
    const tip = `from model${mname ? ` ${mname}` : ''}${sc ? `, score ${sc}` : ''}`;
    model = `<span class="lb-obj-model" role="img" aria-label="${esc(tip)}" title="${esc(tip)}"><span class="lb-robot">🤖</span>` +
      `${sc ? `<span class="num">${sc}</span>` : ''}</span>`;
  }
  return `<div class="${cls}" data-id="${esc(a.id)}"><span class="sw" style="--c:${okColor(c.color)}"></span>` +
    `<select class="lb-obj-cls" data-act="cls" aria-label="Class of this object"><option value="${esc(a.class_id)}">${esc(c.name)}</option></select>` +
    model + `<span class="lb-obj-t" title="${esc(a.geometry_type)}">${icon(TYPE_ICON[a.geometry_type] || 'circle')}<span class="num">${k}${kp}</span>${a.parent_id ? '<span class="lnk" title="linked to a box">↳</span>' : ''}</span>` +
    `<button class="icon-btn ghost" data-act="eye" aria-label="${hid ? 'Show' : 'Hide'} this object" title="${hid ? 'Show' : 'Hide'}">${icon(hid ? 'eye-off' : 'eye')}</button>` +
    `<button class="icon-btn ghost" data-act="del" aria-label="Delete this object" title="Delete">${icon('trash')}</button></div>`;
}

// append the next n pending rows of a group in front of its sentinel
function flushObjRows(tid: string, n = OBJ_CHUNK): void {
  const rest = objRest.get(tid);
  const more = $req('lb-objects').querySelector(`.lb-more[data-task="${tid}"]`);
  if (!rest || !more) return;
  let html = '';
  for (const [a, k] of rest.splice(0, n)) html += objRowHtml(a, k);
  more.insertAdjacentHTML('beforebegin', html);
  if (!rest.length) { objRest.delete(tid); if (objIO) objIO.unobserve(more); more.remove(); }
  else more.textContent = `${rest.length} more…`;
}

const idle = (f: () => void): void => { if ('requestIdleCallback' in window) window.requestIdleCallback(f, { timeout: 100 }); else setTimeout(f, 16); };

// render (in idle slices) the pending rows up to the first selected one, then mark it and scroll to it
function revealSelected(): void {
  const job = ++revealJob;
  const step = (): void => {
    if (job !== revealJob) return;                    // a newer selection / re-render took over
    let tid: string | null = null;
    for (const [t, rest] of objRest) if (rest.some(([a]) => L.selected.has(a.id))) { tid = t; break; }
    if (tid === null) { syncObjectMarks(true, true); return; }
    flushObjRows(tid, OBJ_SLICE);
    idle(step);
  };
  idle(step);
}

function renderObjects(): void {
  const el = $req('lb-objects');
  const byTask = new Map<string, Ann[]>();
  for (const a of L.anns.values()) {
    const arr = byTask.get(a.task_id);
    if (arr) arr.push(a); else byTask.set(a.task_id, [a]);
  }
  $req('lb-objects-n').textContent = L.anns.size ? String(L.anns.size) : '';
  const tasks = (L.project && L.project.tasks) || [];
  objRest.clear();
  revealJob++;
  let html = '';
  let budget = OBJ_CHUNK;
  for (const t of tasks) {
    const list = byTask.get(t.id) || [];
    if (!list.length && t.id !== L.taskId) continue;
    const open = t.id === L.taskId ? !P.collapsed.has(t.id) : P.collapsed.has('+' + t.id);
    html += `<div class="lb-og${t.id === L.taskId ? ' active' : ''}"><button class="lb-og-head" data-act="og" data-task="${esc(t.id)}" aria-expanded="${open}">` +
      `${icon(open ? 'chev-d' : 'chev-r')}<span class="nm">${esc(t.name)}</span><span class="n num">${list.length}</span></button>`;
    if (open) {
      if (!list.length) html += '<div class="lb-empty">Nothing drawn yet</div>';
      const idx = new Map<string, number>();
      const rest: Array<[Ann, number]> = [];
      for (const a of list) {
        const k = (idx.get(a.class_id) || 0) + 1;
        idx.set(a.class_id, k);
        if (budget > 0) { html += objRowHtml(a, k); budget--; } else rest.push([a, k]);
      }
      if (rest.length) {
        objRest.set(t.id, rest);
        html += `<div class="lb-more" data-task="${esc(t.id)}">${rest.length} more…</div>`;
      }
    }
    html += '</div>';
  }
  el.innerHTML = html || '<div class="lb-empty">No objects on this image</div>';
  if (objRest.size) {
    if (!objIO) {
      objIO = new IntersectionObserver((ents) => {
        for (const en of ents) {
          const tk = asEl(en.target)?.dataset['task'];
          if (en.isIntersecting && tk !== undefined) flushObjRows(tk);
        }
      }, { root: el, rootMargin: '400px 0px' });
    }
    for (const m of el.querySelectorAll('.lb-more')) objIO.observe(m);
  }
}

// cheap sync of hover / selection marks without re-rendering the list (rows not rendered yet get their marks when
// they go in); `scroll` brings the first selected row into view, rendering pending rows up to it in idle slices
export function syncObjectMarks(scroll?: boolean, revealed = false): void {
  const el = $req('lb-objects');
  for (const r of el.querySelectorAll('.lb-obj.sel, .lb-obj.hov')) { r.classList.remove('sel', 'hov'); }
  let first: Element | null = null;
  for (const id of L.selected) { const r = el.querySelector(`[data-id="${id}"]`); if (r) { r.classList.add('sel'); first = first || r; } }
  if (L.hoverId) { const r = el.querySelector(`[data-id="${L.hoverId}"]`); if (r) r.classList.add('hov'); }
  if (!scroll) return;
  if (first) { revealJob++; first.scrollIntoView({ block: 'nearest' }); } else if (L.selected.size && objRest.size && !revealed) revealSelected();
}

// ---------------------------------------------------------------- top bar + status bar + banner
const STATUS_TEXT: Record<string, string> = { todo: 'To do', in_progress: 'In progress', done: 'Done', skipped: 'Skipped' };

function renderTop(): void {
  $req('lb-name').textContent = L.project ? L.project.name : '';
  document.title = L.project ? `${L.project.name} · Labeling` : 'Labeling';
  const n = L.ids.length;
  $req('lb-total').textContent = n.toLocaleString();
  const j = $req<HTMLInputElement>('lb-jump');
  if (document.activeElement !== j) j.value = n ? String(L.idx + 1) : '';
  j.max = String(n);
  $req<HTMLButtonElement>('lb-prev').disabled = L.idx <= 0;
  $req<HTMLButtonElement>('lb-next').disabled = L.idx >= n - 1;
  const st: string = (L.media && L.taskId !== null && L.media.status[L.taskId]) || 'todo';
  const pill = $req('lb-pill');
  pill.className = `lb-pill st-${st}`;
  pill.textContent = L.media ? STATUS_TEXT[st] || st : '';
  const h = L.history;
  $req<HTMLButtonElement>('lb-undo').disabled = !h || !h.undo.length;
  $req<HTMLButtonElement>('lb-redo').disabled = !h || !h.redo.length;
  const ck = combosFor('commit')[0];
  $req('lb-commit-key').textContent = ck ? prettyCombo(ck) : '';
  $req('lb-commit').title = `Mark this image done${L.settings.commit_scope === 'all' ? ' for every task' : ' for the active task'}${ck ? ` (${prettyCombo(ck)})` : ''}`;
  const sk = combosFor('skip')[0];
  $req('lb-skip').title = `Skip this image${sk ? ` (${prettyCombo(sk)})` : ''}`;
  renderReview(st);
  $req('lb-undo').title = `Undo${combosFor('undo')[0] ? ` (${prettyCombo(combosFor('undo')[0])})` : ''}`;
  $req('lb-redo').title = `Redo${combosFor('redo')[0] ? ` (${prettyCombo(combosFor('redo')[0])})` : ''}`;
}

// the optional review controls: only when the session tracks review. The button is on (pressed) when the image
// is approved by hand in the active task, and is off-limits until the image is done.
function renderReview(st: string): void {
  const on = !!(L.project && L.project.review);
  const btn = $req<HTMLButtonElement>('lb-review');
  btn.classList.toggle('hidden', !on);
  $req('lb-review-all').classList.toggle('hidden', !on);
  const g = $req<HTMLElement & { disabled: boolean }>('lb-strip-rv');
  g.classList.toggle('hidden', !on);
  g.disabled = !on;
  if (!on) return;
  const rv: string = (L.media && L.taskId !== null && L.media.review?.[L.taskId]) || '';
  const done = st === 'done';
  btn.disabled = !L.media || (!done && !rv);
  btn.setAttribute('aria-pressed', String(rv === 'manual'));
  btn.classList.toggle('on', rv === 'manual');
  btn.classList.toggle('bulk', rv === 'bulk');
  const rk = combosFor('review')[0];
  const k = rk ? ` (${prettyCombo(rk)})` : '';
  btn.title = !done ? `Only done images can be reviewed${k}`
    : rv === 'manual' ? `Reviewed by hand: click to undo${k}`
      : rv === 'bulk' ? `Approved with Review all, not looked at: click to review it by hand${k}`
        : `Mark this image as reviewed${k}`;
}

const SAVE_TEXT: Record<string, [string, string]> = { saved: ['check', 'Saved'], saving: ['refresh', 'Saving…'], offline: ['alert', 'Offline: retrying'], conflict: ['alert', 'Conflict'] };
let lastXY = '';
export function renderPointer(p: PointerPos | null): void {
  let s = '';
  if (p && L.media) {
    const w = L.media.width || 0, h = L.media.height || 0;
    s = `x ${Math.round(p.nx * w)}  y ${Math.round(p.ny * h)}`;
  }
  if (s !== lastXY) { lastXY = s; $req('lb-xy').textContent = s; }
}

function renderStatus(): void {
  $req('lb-zoom').textContent = `${Math.round(L.view.scale * 100)}%`;
  const tool = H.tool();
  const hint = tool && tool.hint ? tool.hint() : '';
  $req('lb-tool').textContent = tool ? `${tool.name}${hint ? ` · ${hint}` : ''}` : '';
  let sc = $('lb-selcount');
  if (!sc) {
    sc = document.createElement('span');
    sc.id = 'lb-selcount';
    sc.className = 'lb-selcount num';
    sc.setAttribute('aria-live', 'polite');
    $req('lb-tool').after(sc);
  }
  const ns = L.selected.size;
  const txt = ns > 1 ? `${ns} selected` : ns === 1 ? '1 selected' : '';
  if (sc.textContent !== txt) sc.textContent = txt;
  const c = L.classId && CLS.get(L.classId);
  $req('lb-cls').innerHTML = c ? `<span class="sw" style="--c:${okColor(c.color)}"></span>${esc(c.name)}` : '<span class="muted">no class</span>';
  const [ic, tx] = SAVE_TEXT[L.saveState] || ['check', 'Saved'];
  const sv = $req('lb-save');
  sv.className = `lb-save s-${L.saveState}`;
  sv.innerHTML = `${icon(ic)}${tx}`;
}

function renderBanner() {
  const tags = [...L.anns.values()].filter((a) => a.geometry_type === 'tag');
  const el = $req('lb-banner');
  const t = activeTask();
  const show = tags.length || (t && t.task_type === 'classification');
  el.classList.toggle('hidden', !show);
  if (show) {
    el.innerHTML = tags.length ? tags.map((a) => {
      const c = clsOf(a);
      return `<span class="lb-tagchip${a.task_id === L.taskId ? '' : ' dim'}" style="--c:${okColor(c.color)}">${esc(c.name)}</span>`;
    }).join('') : '<span class="muted">No image tags yet: pick a class on the left</span>';
  }
  H.overlaysChanged();
}

// ---------------------------------------------------------------- image strip (virtual) + media pages
const PAGE = 500;
const RH = 52;
interface MediaList { key: string; total: number | null; pages: Map<number, Media[]>; loading: Map<number, Promise<Media[]>>; error: string }
const mkList = (key: string): MediaList => ({ key, total: null, pages: new Map(), loading: new Map(), error: '' });
const ALL = mkList('all');
let SL: MediaList = ALL;         // the strip's current list
let stripH = 0;
const rowEls = new Map<number, { el: HTMLElement; mid: string; sig: string }>();        // index -> {el, mid, sig}
export const idxOf = new Map<string, number>();  // media id -> index in L.ids

export function resetMedia(): void {
  ALL.pages.clear(); ALL.loading.clear(); ALL.total = L.ids.length;
  SL = SL === ALL ? ALL : mkList(SL.key);
  idxOf.clear();
  L.ids.forEach((id, i) => idxOf.set(id, i));
  clearStripRows();
  schedule('strip');
}

// the strip filter as a query string ('' = no filter: the 'all' list, whose index is the position)
function filterQuery(): string {
  const p = new URLSearchParams();
  if (P.strip.startsWith('review:')) p.set('review', P.strip.slice(7));      // the review filter (needs the active task)
  else if (P.strip !== 'all') p.set('status', P.strip);
  const q = P.sq.trim();
  if (q) p.set('q', q);
  if (P.scls.size) { p.set('cls', [...P.scls].join(',')); p.set('cls_mode', P.smode); }
  if (P.smin !== '') p.set('min_ann', P.smin);
  if (P.smax !== '') p.set('max_ann', P.smax);
  if (![...p.keys()].length) return '';
  if (L.taskId) p.set('task_id', L.taskId);
  return `&${p}`;
}
const listQuery = (list: MediaList): string => (list === ALL ? '' : list.key);

function loadPage(list: MediaList, pi: number): Promise<Media[]> {
  const have = list.pages.get(pi);
  if (have) return Promise.resolve(have);
  const inflight = list.loading.get(pi);
  if (inflight) return inflight;
  const pr: Promise<Media[]> = fetch(`api/label/projects/${pid()}/media?offset=${pi * PAGE}&limit=${PAGE}${listQuery(list)}`)
    .then((r): Promise<MediaPage> => (r.ok ? r.json() : r.json().catch(() => ({})).then((d: { detail?: unknown }) => {
      // a bad /regex/ (400): the strip says why instead of staying empty
      if (r.status === 400 && list !== ALL) { list.error = String(d.detail || 'Bad filter'); list.total = 0; if (list === SL) schedule('strip'); }
      return Promise.reject(new Error(String(d.detail || r.status)));
    })))
    .then((d): Media[] => {
      list.error = '';
      list.total = d.total;
      list.pages.set(pi, d.items || []);
      list.loading.delete(pi);
      if (list === SL) schedule('strip');
      return d.items || [];
    })
    .catch((e) => { list.loading.delete(pi); throw e; });
  list.loading.set(pi, pr);
  return pr;
}

const itemAt = (list: MediaList, i: number): Media | undefined => { const pg = list.pages.get(Math.floor(i / PAGE)); return pg && pg[i % PAGE]; };

export const mediaCached = (i: number | null | undefined): Media | null => (i == null ? null : itemAt(ALL, i) || null);

// the media row (position order) of index i, loading its page when needed
export async function mediaAt(i: number): Promise<Media | undefined> {
  return itemAt(ALL, i) || (await loadPage(ALL, Math.floor(i / PAGE)))[i % PAGE];
}

// a Media changed (status / counts): patch every cached copy and the visible strip rows
let mediaTimer = 0;
export function mediaUpdated(m: Media | null | undefined): void {
  if (!m) return;
  const i = idxOf.get(m.id);
  if (i != null) {
    const pg = ALL.pages.get(Math.floor(i / PAGE));
    const old = pg?.[i % PAGE];
    if (pg && old) pg[i % PAGE] = { ...old, status: { ...m.status }, n: { ...(m.n || {}) }, review: { ...(m.review || {}) } };
  }
  if (SL !== ALL) {
    // a filtered list may now be wrong: refetch it lazily
    clearTimeout(mediaTimer);
    mediaTimer = setTimeout(() => { SL = mkList(SL.key); clearStripRows(); schedule('strip'); }, 800);
  }
  schedule('strip', 'top', 'tasks');
}

// next index after `from` whose status (for every task in taskIds) is not done / skipped; -1 when none
export async function nextUnfinished(from: number, taskIds: string[]): Promise<number> {
  const n = L.ids.length;
  const open = (m: Media | null | undefined): boolean => !!m && taskIds.some((t) => { const s = m.status && m.status[t]; return s !== 'done' && s !== 'skipped'; });
  for (let k = 1; k < n; k++) {
    const i = (from + k) % n;
    let m: Media | null | undefined = i === L.idx ? L.media : itemAt(ALL, i);
    if (!m) { try { m = await mediaAt(i); } catch (e) { return -1; } }
    if (open(m)) return i;
  }
  return -1;
}

function clearStripRows(): void { for (const r of rowEls.values()) r.el.remove(); rowEls.clear(); }

// a /regex/ JavaScript cannot parse is refused here (no request); the server's Python re has the last word
function regexError(q: string): string {
  q = q.trim();
  if (q.length < 2 || q[0] !== '/' || q[q.length - 1] !== '/') return '';
  try { new RegExp(q.slice(1, -1), 'i'); return ''; } catch (e) { return `bad regular expression: ${e instanceof Error ? e.message : String(e)}`; }
}

export function setStripFilter(v?: string | null): void {
  if (v != null) P.strip = v;
  const key = filterQuery();
  SL = key ? mkList(key) : ALL;
  const re = regexError(P.sq);
  if (re) { SL.error = re; SL.total = 0; }
  clearStripRows();
  $req('lb-strip').scrollTop = 0;
  syncStripBadge();
  schedule('strip');
}
export const stripFilter = (): string => P.strip;
// the active task changed (its classes / counts are what the filter means) or the server rewrote statuses
export function refreshFilteredStrip(): void {
  const t = activeTask();
  const ids = new Set(t ? t.classes.map((c) => c.id) : []);
  for (const c of [...P.scls]) if (!ids.has(c)) P.scls.delete(c);
  if (!$req('lb-strip-pop').classList.contains('hidden')) renderStripPop();
  if (SL !== ALL || filterQuery()) setStripFilter();
}

// ---- the filter popover: class chips (any / all / none of them), object count range; the name box debounced
function renderStripPop(): void {
  const t = activeTask();
  const chips = t && t.classes.length ? t.classes.map((c) => {
    const on = P.scls.has(c.id);
    return `<button class="lb-chip${on ? ' on' : ''}" data-scls="${esc(c.id)}" aria-pressed="${on}"><span class="sw" style="--c:${okColor(c.color)}"></span>${esc(c.name)}</button>`;
  }).join('') : '<span class="muted">No classes in this task</span>';
  const mode = (v: ClassMatchMode, t2: string): string => `<option value="${v}"${P.smode === v ? ' selected' : ''}>${t2}</option>`;
  $req('lb-strip-pop').innerHTML =
    `<div class="lb-pop-row"><span>Has class</span><select data-sf="mode" aria-label="How the picked classes must match">${mode('any', 'any of')}${mode('all', 'all of')}${mode('none', 'none of')}</select>` +
    `<span class="grow"></span><button class="ghost lb-pop-clear" data-sf="clear">Clear filters</button></div>` +
    `<div class="lb-chips" role="group" aria-label="Classes">${chips}</div>` +
    `<div class="lb-pop-row"><span>Objects (${esc(t ? t.name : 'task')})</span><input type="number" min="0" step="1" data-sf="min" value="${esc(P.smin)}" placeholder="min" aria-label="At least this many objects">` +
    `<span>–</span><input type="number" min="0" step="1" data-sf="max" value="${esc(P.smax)}" placeholder="max" aria-label="At most this many objects"></div>`;
}

function syncStripBadge(): void {
  const pop = P.scls.size + (P.smin !== '' || P.smax !== '' ? 1 : 0);
  const b = $req('lb-strip-badge');
  b.textContent = pop ? String(pop) : '';
  b.classList.toggle('hidden', !pop);
  $req('lb-strip-more').classList.toggle('on', !!pop);
}

let sfTimer = 0;
const stripFilterSoon = (): void => { clearTimeout(sfTimer); sfTimer = setTimeout(() => setStripFilter(), 150); };

function bindStripFilter(): void {
  const q = $req<HTMLInputElement>('lb-strip-q');
  q.addEventListener('input', () => { P.sq = q.value; stripFilterSoon(); });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); if (q.value) { q.value = ''; P.sq = ''; setStripFilter(); } else q.blur(); }
    else if (e.key === 'Enter') { clearTimeout(sfTimer); setStripFilter(); }
  });
  const more = $req('lb-strip-more'), pop = $req('lb-strip-pop');
  more.addEventListener('click', () => {
    const open = pop.classList.contains('hidden');
    if (open) renderStripPop();
    pop.classList.toggle('hidden', !open);
    more.setAttribute('aria-expanded', String(open));
    stripH = 0;
    schedule('strip');
  });
  pop.addEventListener('click', (e) => {
    const c = closestEl(e.target, '[data-scls]');
    const id = c?.dataset['scls'];
    if (c && id !== undefined) {
      if (P.scls.has(id)) P.scls.delete(id); else P.scls.add(id);
      c.classList.toggle('on', P.scls.has(id));
      c.setAttribute('aria-pressed', String(P.scls.has(id)));
      stripFilterSoon();
      return;
    }
    if (closestEl(e.target, '[data-sf="clear"]')) {
      P.scls.clear(); P.smin = ''; P.smax = ''; P.smode = 'any'; P.sq = ''; q.value = '';
      renderStripPop();
      setStripFilter();
    }
  });
  pop.addEventListener('input', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement)) return;
    const k = t.dataset['sf'];
    if (k === 'min' || k === 'max') {
      const v = t.value === '' ? '' : String(Math.max(0, Math.floor(+t.value) || 0));
      if (k === 'min') P.smin = v; else P.smax = v;
      stripFilterSoon();
    }
  });
  pop.addEventListener('change', (e) => {
    const t = e.target;
    if (t instanceof HTMLSelectElement && t.dataset['sf'] === 'mode') {
      P.smode = t.value === 'all' || t.value === 'none' ? t.value : 'any';
      if (P.scls.size) stripFilterSoon();
    }
  });
}

function dotsOf(m: Media): string {
  return ((L.project && L.project.tasks) || []).map((t) => {
    const s: string = (m.status && m.status[t.id]) || 'todo';
    const rv = L.project?.review && s === 'done' && m.review && m.review[t.id];     // '' | manual | bulk
    return `<i class="dot st-${esc(s)}${rv ? ` rv-${esc(rv)}` : ''}" title="${esc(`${t.name}: ${STATUS_TEXT[s] || s}${rv ? (rv === 'bulk' ? ', approved with Review all' : ', reviewed') : ''}`)}"></i>`;
  }).join('');
}

function renderStrip(): void {
  const sc = $req('lb-strip');
  if (!sc || $req('lb-right').classList.contains('off')) return;
  const list = SL;
  let total: number = list.total ?? 0;
  if (list.total == null) { loadPage(list, 0).catch(() => {}); total = 0; }
  $req('lb-strip-sizer').style.height = `${total * RH}px`;
  const empty = $req('lb-strip-empty');
  empty.classList.toggle('hidden', !(list.total === 0));
  if (list.total === 0) {
    const t = list.error || 'No images match';
    if (empty.textContent !== t) empty.textContent = t;
    empty.classList.toggle('err', !!list.error);
  }
  const nEl = $req('lb-strip-n');
  const nt = list === ALL || list.total == null ? '' : `${list.total.toLocaleString()} / ${L.ids.length.toLocaleString()}`;
  if (nEl.textContent !== nt) nEl.textContent = nt;
  if (!stripH) stripH = sc.clientHeight;
  const top = sc.scrollTop;
  const i0 = Math.max(0, Math.floor(top / RH) - 4), i1 = Math.min(total, Math.ceil((top + stripH) / RH) + 4);
  for (let pi = Math.floor(i0 / PAGE); pi <= Math.floor(Math.max(i0, i1 - 1) / PAGE) && total; pi++) if (!list.pages.has(pi)) loadPage(list, pi).catch(() => {});
  for (const [i, r] of rowEls) if (i < i0 || i >= i1) { r.el.remove(); rowEls.delete(i); }
  const curMid = L.media && L.media.id;
  const sizer = $req('lb-strip-sizer');
  for (let i = i0; i < i1; i++) {
    const m = itemAt(list, i);
    const mid = m ? m.id : '';
    const sig = m ? `${mid === curMid ? 1 : 0}|${JSON.stringify(m.status || {})}` : '';
    const r = rowEls.get(i);
    if (r && r.mid === mid && r.sig === sig) continue;
    if (r && r.mid === mid && m) {                 // same image, new status / current mark: patch in place
      r.el.classList.toggle('cur', mid === curMid);
      r.el.setAttribute('aria-current', mid === curMid ? 'true' : 'false');
      const dots = r.el.querySelector('.lb-dots');
      if (dots) dots.innerHTML = dotsOf(m);
      r.sig = sig;
      continue;
    }
    if (r) r.el.remove();
    const el = document.createElement('div');
    el.className = `lb-row${mid && mid === curMid ? ' cur' : ''}`;
    el.style.transform = `translateY(${i * RH}px)`;
    if (m) {
      el.dataset.mid = mid;
      el.setAttribute('role', 'button');
      el.tabIndex = 0;
      el.setAttribute('aria-current', mid === curMid ? 'true' : 'false');
      const ix = idxOf.get(mid);
      const pos = ix !== undefined ? ix + 1 : '';
      el.innerHTML = `<img src="api/label/projects/${pid()}/thumb/${esc(mid)}" loading="lazy" decoding="async" alt="">` +
        `<div class="txt"><span class="fn" title="${esc(m.file_name)}">${esc(m.file_name)}</span><span class="lb-dots">${dotsOf(m)}</span></div><span class="pos num">${pos}</span>`;
    } else el.innerHTML = '<div class="lb-row-ph"></div>';
    sizer.appendChild(el);
    rowEls.set(i, { el, mid, sig });
  }
}

// keep the current image's row in view (the 'all' list: index = position)
export function revealCurrent(): void {
  if (SL !== ALL || L.idx < 0) { schedule('strip'); return; }
  const sc = $req('lb-strip');
  if (!stripH) stripH = sc.clientHeight;
  const y = L.idx * RH;
  if (y < sc.scrollTop) sc.scrollTop = y;
  else if (y + RH > sc.scrollTop + stripH) sc.scrollTop = y + RH - stripH;
  schedule('strip');
}

// ---------------------------------------------------------------- dialog + toast
export function ask({ title, html, buttons }: AskOptions): Promise<AskResult | null> {
  return new Promise((resolve) => {
    const back = document.createElement('div');
    back.className = 'modal lb-dlg-back';
    back.innerHTML = `<div class="panel lb-dlg" role="dialog" aria-modal="true" aria-labelledby="lb-dlg-title" tabindex="-1">
      <h2 id="lb-dlg-title">${esc(title)}</h2><div class="lb-dlg-body">${html || ''}</div>
      <div class="lb-dlg-actions">${buttons.map((b) => `<button data-b="${esc(b.id)}" class="${b.primary ? 'primary' : b.danger ? 'danger' : ''}">${esc(b.label)}</button>`).join('')}</div></div>`;
    document.body.appendChild(back);
    const prev = document.activeElement;
    const done = (v: AskResult | null): void => { back.remove(); if (prev instanceof HTMLElement && document.contains(prev)) prev.focus(); resolve(v); };
    back.addEventListener('click', (e) => {
      const b = closestEl(e.target, '[data-b]');
      if (b) {
        const values: Record<string, string> = {};
        for (const f of back.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[name]')) values[f.name] = f.value;
        done({ id: b.dataset['b'] ?? '', values });
      } else if (e.target === back) done(null);
    });
    back.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } });
    (back.querySelector<HTMLElement>('select, .primary, button') || back.querySelector<HTMLElement>('.lb-dlg'))?.focus();
  });
}

let toastTimer = 0;
export function toast(msg: string, kind: ToastKind = ''): void {
  const el = $req('lb-toast');
  el.className = `lb-toast show ${kind}`;
  el.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'lb-toast'; }, 3500);
}

// ---------------------------------------------------------------- panel sizes / collapse
const WKEY = 'label-panels';
interface PanelPrefs { left?: number; right?: number; leftOff?: boolean; rightOff?: boolean }
type Side = 'left' | 'right';
function loadPanels(): PanelPrefs {
  try {
    const o: unknown = JSON.parse(localStorage.getItem(WKEY) || '{}');
    return o && typeof o === 'object' ? o : {};
  } catch (e) { return {}; }
}
function savePanels(o: PanelPrefs): void { try { localStorage.setItem(WKEY, JSON.stringify(o)); } catch (e) { /* private mode */ } }

export function applyPanels(): void {
  const o = loadPanels();
  const app = $req('lb-app');
  if (o.left) app.style.setProperty('--lb-left-w', `${o.left}px`);
  if (o.right) app.style.setProperty('--lb-right-w', `${o.right}px`);
  setPanel('left', !o.leftOff, false);
  setPanel('right', !o.rightOff, false);
}

export function setPanel(side: Side, on: boolean, save = true): void {
  const app = $req('lb-app');
  app.classList.toggle(`no-${side}`, !on);
  $req(side === 'left' ? 'lb-left' : 'lb-right').classList.toggle('off', !on);
  $req(side === 'left' ? 'lb-show-l' : 'lb-show-r').classList.toggle('hidden', on);
  if (save) { const o = loadPanels(); o[side === 'left' ? 'leftOff' : 'rightOff'] = !on; savePanels(o); }
  stripH = 0;
  if (side === 'right' && on) schedule('strip');
  requestAnimationFrame(() => H.overlaysChanged());
}
export const togglePanel = (side: Side): void => setPanel(side, $req(side === 'left' ? 'lb-left' : 'lb-right').classList.contains('off'));

// Drag a splitter: the panel follows between MIN_W and MAX_W; dragged under COLLAPSE_AT it snaps closed (setPanel,
// the last open width stays in --lb-*-w and localStorage for the show button / Alt+[ ]). A closed panel keeps a thin
// splitter ghost: dragging it out (or a double-click) opens the panel again. Moves are applied once per frame.
const MIN_W = 200, MAX_W = 560, COLLAPSE_AT = 120;
function bindSplitter(id: string, side: Side): void {
  const sp = $req(id);
  const panel = $req(side === 'left' ? 'lb-left' : 'lb-right');
  const dir = side === 'left' ? 1 : -1;
  sp.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    sp.setPointerCapture(e.pointerId);
    const app = $req('lb-app');
    const start = e.clientX;
    let off = panel.classList.contains('off');
    const w0 = off ? 0 : panel.getBoundingClientRect().width;
    let w = parseFloat(app.style.getPropertyValue(`--lb-${side}-w`)) || Math.round(w0) || 280;
    const keepW = w;                               // the width a collapse leaves for reopening (not the clamped minimum)
    let x = start, raf = 0, moved = false;
    const apply = () => {
      raf = 0;
      const raw = w0 + dir * (x - start);
      const closed = raw < COLLAPSE_AT;
      if (closed !== off) {
        off = closed;
        if (closed) { w = keepW; app.style.setProperty(`--lb-${side}-w`, `${w}px`); }
        setPanel(side, !closed, false);
      }
      if (!closed) {
        const nw = Math.round(Math.min(MAX_W, Math.max(MIN_W, raw)));
        if (nw !== w || !moved) { w = nw; app.style.setProperty(`--lb-${side}-w`, `${w}px`); }
      }
      moved = true;
    };
    const move = (ev: PointerEvent): void => { x = ev.clientX; if (!raf) raf = requestAnimationFrame(apply); };
    const up = () => {
      sp.removeEventListener('pointermove', move);
      sp.removeEventListener('pointerup', up);
      sp.removeEventListener('pointercancel', up);
      if (raf) { cancelAnimationFrame(raf); apply(); }
      if (!moved) return;
      const o = loadPanels();
      o[side] = w;                                 // closed: the width it had before the drag (keepW)
      o[side === 'left' ? 'leftOff' : 'rightOff'] = off;
      savePanels(o);
      stripH = 0;
      schedule('strip');
    };
    sp.addEventListener('pointermove', move);
    sp.addEventListener('pointerup', up);
    sp.addEventListener('pointercancel', up);
  });
  sp.addEventListener('dblclick', () => { if (panel.classList.contains('off')) setPanel(side, true); });
  sp.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault(); e.stopPropagation();
    const d = (e.key === 'ArrowRight' ? 16 : -16) * dir;
    if (panel.classList.contains('off')) { if (d > 0) setPanel(side, true); return; }
    const raw = panel.getBoundingClientRect().width + d;
    if (raw < MIN_W) { setPanel(side, false); return; }      // past the minimum: the arrow closes the panel
    const w = Math.round(Math.min(MAX_W, raw));
    $req('lb-app').style.setProperty(`--lb-${side}-w`, `${w}px`);
    const o = loadPanels(); o[side] = w; savePanels(o);
  });
}

// ---------------------------------------------------------------- events (delegated, bound once)
function bind(): void {
  const ch = $req('lb-classes').parentElement?.querySelectorAll('.lb-head')[1];
  if (ch && !$('lb-cls-more')) {
    ch.insertAdjacentHTML('beforeend', '<span class="grow"></span><button id="lb-cls-more" class="icon-btn ghost lb-more-btn" aria-haspopup="menu" ' +
      'aria-expanded="false" aria-label="More class actions" title="More: manage classes"><span aria-hidden="true">⋯</span></button>');
    $('lb-cls-more')?.addEventListener('click', (e) => { if (P.menu) closeMenu(); else if (e.currentTarget instanceof HTMLElement) openPanelMenu(e.currentTarget); });
  }
  $req('lb-tasks').addEventListener('click', (e) => { const b = closestEl(e.target, '[data-task]'); if (b?.dataset['task']) H.switchTask(b.dataset['task']); });
  const cl = $req('lb-classes');
  cl.addEventListener('click', (e) => { const r = closestEl(e.target, '[data-cls]'); if (r?.dataset['cls'] && !closestEl(e.target, 'input')) H.selectClass(r.dataset['cls'], true); });
  cl.addEventListener('dblclick', (e) => { const r = closestEl(e.target, '[data-cls]'); if (r?.dataset['cls'] && !closestEl(e.target, 'input')) startRename(r.dataset['cls']); });
  cl.addEventListener('contextmenu', (e) => {
    const r = closestEl(e.target, '[data-cls]');
    if (!r) return;
    e.preventDefault();
    const c = activeTask()?.classes.find((x) => x.id === r.dataset['cls']);
    if (c) openMenu(c, e.clientX, e.clientY);
  });
  document.addEventListener('pointerdown', (e) => {
    if (P.menu && !(e.target instanceof Node && P.menu.el.contains(e.target)) && !closestEl(e.target, '#lb-cls-more')) closeMenu();
  }, true);

  const flt = $req<HTMLInputElement>('lb-filter');
  flt.addEventListener('input', () => { P.filter = flt.value; schedule('classes'); });
  flt.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const f = flt.value.trim().toLowerCase();
      const c = activeTask()?.classes.find((x) => x.name.toLowerCase().includes(f));
      if (c) { H.selectClass(c.id); flt.value = ''; P.filter = ''; schedule('classes'); flt.blur(); }
    } else if (e.key === 'Escape') { flt.value = ''; P.filter = ''; schedule('classes'); flt.blur(); e.stopPropagation(); }
  });
  const add = $req<HTMLInputElement>('lb-add');
  add.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey) { e.preventDefault(); const v = add.value; add.value = ''; void addClass(v); }
    else if (e.key === 'Escape') { add.value = ''; add.blur(); e.stopPropagation(); }
  });

  const ob = $req('lb-objects');
  ob.addEventListener('click', (e) => {
    const h = closestEl(e.target, '[data-act="og"]');
    if (h) {
      const tid = h.dataset['task'] ?? '';
      const k = tid === L.taskId ? tid : '+' + tid;
      if (P.collapsed.has(k)) P.collapsed.delete(k); else P.collapsed.add(k);
      schedule('objects');
      return;
    }
    const row = closestEl(e.target, '[data-id]');
    if (!row) return;
    const act = closestEl(e.target, '[data-act]');
    const id = row.dataset['id'] ?? '';
    if (act && act.dataset['act'] === 'eye') H.toggleHidden(id);
    else if (act && act.dataset['act'] === 'del') H.deleteAnn(id);
    else if (!act) H.selectAnn(id, e.shiftKey);
  });
  // the class select gets its options only when opened (500 rows × N options would be a lot of DOM)
  const fill = (sel: HTMLSelectElement): void => {
    if (sel.dataset['filled']) return;
    const a = L.anns.get(sel.closest<HTMLElement>('[data-id]')?.dataset['id'] ?? '');
    const t = a && TASKS.get(a.task_id);
    if (!a || !t) return;
    sel.innerHTML = t.classes.map((c) => `<option value="${esc(c.id)}"${c.id === a.class_id ? ' selected' : ''}>${esc(c.name)}</option>`).join('');
    sel.dataset['filled'] = '1';
  };
  ob.addEventListener('mousedown', (e) => { const t = e.target; if (t instanceof HTMLSelectElement && t.matches('select.lb-obj-cls')) fill(t); });
  ob.addEventListener('focusin', (e) => { const t = e.target; if (t instanceof HTMLSelectElement && t.matches('select.lb-obj-cls')) fill(t); });
  ob.addEventListener('change', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLSelectElement) || !t.matches('select.lb-obj-cls')) return;
    H.reassign(t.closest<HTMLElement>('[data-id]')?.dataset['id'] ?? '', t.value);
    t.blur();
  });
  ob.addEventListener('pointerover', (e) => { const r = closestEl(e.target, '[data-id]'); H.hoverAnn(r ? r.dataset['id'] ?? null : null); });
  ob.addEventListener('pointerleave', () => H.hoverAnn(null));

  const sc = $req('lb-strip');
  let sraf = 0;
  sc.addEventListener('scroll', () => { if (!sraf) sraf = requestAnimationFrame(() => { sraf = 0; renderStrip(); }); }, { passive: true });
  new ResizeObserver(() => { stripH = sc.clientHeight; schedule('strip'); }).observe(sc);
  sc.addEventListener('click', (e) => { const r = closestEl(e.target, '[data-mid]'); if (r?.dataset['mid']) H.goToId(r.dataset['mid']); });
  sc.addEventListener('keydown', (e) => { if (e.key === 'Enter') { const r = closestEl(e.target, '[data-mid]'); if (r?.dataset['mid']) H.goToId(r.dataset['mid']); } });
  $req('lb-strip-filter').addEventListener('change', (e) => {
    const t = e.target;
    if (t instanceof HTMLSelectElement) { setStripFilter(t.value); t.blur(); }
  });
  bindStripFilter();

  bindSplitter('lb-split-l', 'left');
  bindSplitter('lb-split-r', 'right');
  const toggles: Array<[string, Side, boolean]> = [['lb-hide-l', 'left', false], ['lb-hide-r', 'right', false], ['lb-show-l', 'left', true], ['lb-show-r', 'right', true]];
  for (const [id, side, on] of toggles) {
    $req(id).addEventListener('click', () => setPanel(side, on));
  }
}

