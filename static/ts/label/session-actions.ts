// Session-level dialogs shared by the label page (classes panel menu) and the Sessions home (card menu):
//   openManageClasses({pid, project?, beforeDelete?, onDone?}): every task's classes with their label counts
//     (GET class-stats), checkboxes, "Select unused", then a danger confirmation (type the session name when labels
//     would go; a simple confirm when none would) and POST classes/delete (all or nothing).
//   openDuplicate({pid, project?, onDone?}): a name prompt, POST duplicate, the job's progress, then onDone(newProject).
// One modal element per dialog (class "modal": the label page's hotkeys pause while it is open), one delegated
// listener set, every interpolated value through esc(), focus trapped and restored.

import { errText, getJSON, postJSON } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import type { ClassStat, ClassStatsPayload, DeleteClassesPayload, DuplicateJob, DuplicateResult, JobStarted, ManageClassesResult, ProjectTasks } from '../types-label-ui.js';
import type { LabelProject, LabelTask, TaskClass, TaskType } from '../types-label.js';
import { TASK_TYPES } from './state.js';
import { asEl, closestEl } from './ui-util.js';

const okColor = (c: string | null | undefined): string => (c && /^#[0-9a-f]{6}$/i.test(c) ? c : '#888888');
const fmt = (n: number | null | undefined): string => Number(n || 0).toLocaleString();
const plural = (n: number, w: string): string => `${fmt(n)} ${w}${n === 1 ? '' : /(s|sh|ch|x)$/.test(w) ? 'es' : 's'}`;

/** A modal shell: the backdrop element, the dialog box, and close(). */
export interface Shell { el: HTMLElement; box: HTMLElement; onClose: (() => void) | null; busy: boolean; close(): void }

// a modal shell: Esc / a click on the backdrop closes it unless busy
function shell(cls: string, label: string): Shell {
  const prev = document.activeElement;
  const el = document.createElement('div');
  el.className = 'modal lbm-back';
  el.innerHTML = `<div class="panel lbm ${cls}" role="dialog" aria-modal="true" aria-label="${esc(label)}" tabindex="-1"></div>`;
  document.body.appendChild(el);
  const box = el.firstElementChild as HTMLElement;
  let closed = false;
  const S: Shell = {
    el, box, onClose: null, busy: false,
    close: () => {
      if (closed) return;
      closed = true;
      el.remove();
      if (prev instanceof HTMLElement && document.contains(prev)) prev.focus();
      if (S.onClose) S.onClose();
    },
  };
  el.addEventListener('mousedown', (e) => { if (e.target === el && !S.busy) S.close(); });
  el.addEventListener('keydown', (e) => {
    e.stopPropagation();                         // the label page's hotkeys never see keys typed here
    if (e.key === 'Escape') { e.preventDefault(); if (!S.busy) S.close(); return; }
    if (e.key !== 'Tab') return;
    const f = [...box.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href]')].filter((x) => x.offsetParent);
    const first = f[0], last = f[f.length - 1];
    if (!first || !last) return;
    const i = f.indexOf(document.activeElement as HTMLElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); first.focus(); }
  });
  box.focus();
  return S;
}

// ================================================================ manage classes
export interface ManageClassesOpts {
  pid: string;
  project?: ProjectTasks | null | undefined;
  beforeDelete?: (() => unknown) | null | undefined;
  onDone?: ((res: ManageClassesResult) => void) | null | undefined;
}
type ManageStep = 'load' | 'pick' | 'confirm' | 'busy';
type ClassRow = TaskClass & { task: LabelTask };

export async function openManageClasses({ pid, project = null, beforeDelete = null, onDone = null }: ManageClassesOpts): Promise<void> {
  const S = shell('lbm-classes', 'Manage classes');
  const M: { step: ManageStep; project: ProjectTasks; stats: Record<string, ClassStat>; sel: Set<string>; typed: string; error: string | null; result: DeleteClassesPayload | null } =
    { step: 'load', project: project ?? { name: '', tasks: [] }, stats: {}, sel: new Set(), typed: '', error: null, result: null };
  const ep = (p: string): string => `api/label/projects/${encodeURIComponent(pid)}${p}`;
  const classes = (): ClassRow[] => (M.project.tasks || []).flatMap((t) => (t.classes || []).map((c) => ({ ...c, task: t })));
  const st = (cid: string): ClassStat => M.stats[cid] || { n_annotations: 0, n_images: 0 };
  const picked = () => classes().filter((c) => M.sel.has(c.id));
  const totals = () => {
    const p = picked();
    const n = p.reduce((s, c) => s + (st(c.id).n_annotations || 0), 0);
    const withLabels = p.filter((c) => st(c.id).n_annotations);
    let imgs = withLabels.reduce((s, c) => s + (st(c.id).n_images || 0), 0);
    if (M.project.n_media) imgs = Math.min(imgs, M.project.n_media);
    return { n, imgs, exact: withLabels.length <= 1, classes: p.length };
  };

  function head(title: string): string {
    return `<div class="lbm-head"><h2>${icon('tag')}${esc(title)}</h2>` +
      `<button class="icon-btn ghost" data-a="close" aria-label="Close" title="Close (Esc)"${M.step === 'busy' ? ' disabled' : ''}>${icon('x')}</button></div>`;
  }

  function pickHTML() {
    const tasks = M.project.tasks || [];
    const all = classes();
    const unused = all.filter((c) => !st(c.id).n_annotations).length;
    const T = totals();
    const rows = tasks.map((t) => {
      const tt = (TASK_TYPES as Partial<Record<TaskType, { label: string; icon: string }>>)[t.task_type] || { label: t.task_type, icon: 'tag' };
      const cs = t.classes || [];
      return `<section class="lbm-task"><h3>${icon(tt.icon)}<span>${esc(t.name)}</span><span class="hint">${esc(tt.label)} · ${plural(cs.length, 'class')}</span></h3>` +
        (cs.length ? `<div class="lbm-list">${cs.map((c) => {
          const s = st(c.id);
          return `<label class="check lbm-row${M.sel.has(c.id) ? ' on' : ''}"><input type="checkbox" data-cid="${esc(c.id)}"${M.sel.has(c.id) ? ' checked' : ''}>` +
            `<span class="sw" style="--c:${okColor(c.color)}"></span><span class="nm">${esc(c.name)}</span>` +
            `<span class="lbm-n num${s.n_annotations ? '' : ' zero'}">${s.n_annotations ? `${plural(s.n_annotations, 'label')} · ${plural(s.n_images, 'image')}` : 'unused'}</span></label>`;
        }).join('')}</div>` : '<p class="hint">No classes.</p>') + '</section>';
    }).join('') || '<p class="hint">This session has no tasks.</p>';
    return head(`Manage classes · ${M.project.name}`) +
      `<div class="lbm-tools"><button data-a="unused"${unused ? '' : ' disabled'}>${icon('check')}Select unused <span class="num">(${unused})</span></button>` +
      `<button data-a="none"${M.sel.size ? '' : ' disabled'}>Clear</button><span class="grow"></span>` +
      `<span class="hint num" aria-live="polite">${M.sel.size ? `${plural(T.classes, 'class')} · ${plural(T.n, 'label')}` : `${plural(all.length, 'class')}`}</span></div>` +
      `<div class="lbm-body">${rows}${M.error ? `<div class="status-line err">${icon('alert')}${esc(M.error)}</div>` : ''}</div>` +
      `<div class="lbm-foot"><span class="grow"></span><button data-a="close">Cancel</button>` +
      `<button class="danger-btn" data-a="review"${M.sel.size ? '' : ' disabled'}>${icon('trash')}Delete ${M.sel.size ? plural(M.sel.size, 'class') : 'classes'}…</button></div>`;
  }

  function confirmHTML() {
    const T = totals();
    const list = picked();
    const name = M.project.name || '';
    const needType = T.n > 0;
    const match = M.typed.trim() === name.trim();
    const items = `<ul class="lbm-affected">${list.map((c) => {
      const s = st(c.id);
      return `<li><span class="sw" style="--c:${okColor(c.color)}"></span><span class="nm">${esc(c.name)}</span><span class="hint">${esc(c.task.name)}</span>` +
        `<span class="num">${s.n_annotations ? `${plural(s.n_annotations, 'label')} · ${plural(s.n_images, 'image')}` : 'unused'}</span></li>`;
    }).join('')}</ul>`;
    const body = needType
      ? `<p class="lbm-warn" role="alert">${icon('alert')}<span>This deletes ${plural(T.classes, 'class')} and <b>${plural(T.n, 'label')} on ${T.exact ? '' : 'up to '}${plural(T.imgs, 'image')}</b>. It cannot be undone from this page.</span></p>${items}` +
        `<label class="lbm-type"><span>Type the session name <b>${esc(name)}</b> to confirm</span><input data-a="type" value="${esc(M.typed)}" autocomplete="off" spellcheck="false" aria-label="Session name"></label>`
      : `<p>Delete ${plural(T.classes, 'unused class')}? None of them has labels.</p>${items}`;
    const busy = M.step === 'busy';
    return head(needType ? 'Delete classes and their labels' : 'Delete unused classes') +
      `<div class="lbm-body">${body}${M.error ? `<div class="status-line err">${icon('alert')}${esc(M.error)}</div>` : ''}</div>` +
      `<div class="lbm-foot">${busy ? '<span class="spin"></span><span class="hint">Deleting…</span>' : ''}<span class="grow"></span>` +
      `<button data-a="back"${busy ? ' disabled' : ''}>Back</button>` +
      `<button class="danger-btn" data-a="delete"${busy || (needType && !match) ? ' disabled' : ''}>${icon('trash')}Delete</button></div>`;
  }

  function render(focus?: string): void {
    if (M.step === 'load') {
      S.box.innerHTML = head('Manage classes') + `<div class="lbm-body">${M.error ? `<div class="status-line err">${icon('alert')}${esc(M.error)}</div>`
        : '<div class="status-line"><div class="spin"></div>Loading…</div>'}</div>`;
      return;
    }
    S.box.innerHTML = M.step === 'pick' ? pickHTML() : confirmHTML();
    const f = focus ? S.box.querySelector<HTMLElement>(focus) : null;
    if (f) f.focus();
  }

  async function load(): Promise<void> {
    render();
    try {
      const [p, s] = await Promise.all([
        getJSON<LabelProject>(ep('')),           // fresh: the classes may have changed since the page loaded
        getJSON<ClassStatsPayload | null>(ep('/class-stats')),
      ]);
      M.project = p;
      M.stats = (s && s.stats) || {};
      M.step = 'pick';
      render('[data-cid], [data-a="close"]');
    } catch (e) {
      M.error = errText(e);
      render();
    }
  }

  async function doDelete(): Promise<void> {
    const ids = [...M.sel];
    M.step = 'busy'; M.error = null; S.busy = true;
    render();
    try {
      if (beforeDelete) await beforeDelete();
      M.result = await postJSON<DeleteClassesPayload>(ep('/classes/delete'), { class_ids: ids });
    } catch (e) {
      M.step = 'confirm'; S.busy = false; M.error = errText(e);
      render('[data-a="back"]');
      return;
    }
    S.busy = false;
    if (!M.result) return;
    const res: ManageClassesResult = { ...M.result, class_ids: ids };
    S.close();
    if (onDone) onDone(res);
  }

  const ACT: Record<string, () => void> = {
    close: () => S.close(),
    unused: () => { for (const c of classes()) if (!st(c.id).n_annotations) M.sel.add(c.id); render('[data-a="review"]'); },
    none: () => { M.sel.clear(); render('[data-a="unused"]'); },
    review: () => { if (!M.sel.size) return; M.step = 'confirm'; M.typed = ''; M.error = null; render('[data-a="type"], [data-a="delete"]'); },
    back: () => { M.step = 'pick'; M.error = null; render('[data-a="review"]'); },
    delete: () => doDelete(),
  };
  S.box.addEventListener('click', (e) => {
    const b = closestEl<HTMLButtonElement>(e.target, 'button[data-a]');
    const act = b && ACT[b.dataset['a'] ?? ''];
    if (b && !b.disabled && act) act();
  });
  S.box.addEventListener('change', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement)) return;
    const cid = t.dataset['cid'];
    if (!cid) return;
    if (t.checked) M.sel.add(cid); else M.sel.delete(cid);
    render(`[data-cid="${CSS.escape(cid)}"]`);
  });
  S.box.addEventListener('input', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement) || t.dataset['a'] !== 'type') return;
    M.typed = t.value;
    const del = S.box.querySelector<HTMLButtonElement>('[data-a="delete"]');
    if (del) del.disabled = M.typed.trim() !== (M.project.name || '').trim();
  });
  S.box.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && asEl(e.target)?.dataset['a'] === 'type') {
      e.preventDefault();
      const del = S.box.querySelector<HTMLButtonElement>('[data-a="delete"]');
      if (del && !del.disabled) void doDelete();
    }
  });
  await load();
}

// ================================================================ duplicate
export interface DuplicateOpts {
  pid: string;
  project?: { name: string } | null | undefined;
  onDone?: ((created: DuplicateResult | null) => void) | null | undefined;
}

export function openDuplicate({ pid, project = null, onDone = null }: DuplicateOpts): Shell {
  const S = shell('lbm-dup', 'Duplicate session');
  const D: { name: string; job: Partial<DuplicateJob> | null; error: string | null; busy: boolean; project: { name: string } | null } =
    { name: project ? `${project.name} (copy)` : '', job: null, error: null, busy: false, project };
  const ep = (p: string): string => `api/label/projects/${encodeURIComponent(pid)}${p}`;

  function render(focus?: string): void {
    const j: Partial<DuplicateJob> = D.job || {};
    const pct = j.total ? Math.round(j.pct || 0) : null;
    S.box.innerHTML = `<div class="lbm-head"><h2>${icon('layers')}Duplicate${D.project ? ` · ${esc(D.project.name)}` : ''}</h2>` +
      `<button class="icon-btn ghost" data-a="close" aria-label="Close" title="Close (Esc)"${D.busy ? ' disabled' : ''}>${icon('x')}</button></div>` +
      '<div class="lbm-body"><p class="hint">A new session with the same images, tasks, classes, settings, labels and progress. Every id is new, ' +
      'so both can be exported to one database. Images from a folder, S3 or FTP are shared, uploaded ones are linked or copied.</p>' +
      `<label class="lbm-type"><span>Name of the copy</span><input data-a="name" value="${esc(D.name)}" maxlength="120" autocomplete="off"${D.busy ? ' disabled' : ''}></label>` +
      (D.busy ? `<div class="status-line"><div class="spin"></div>${esc(j.stage || j.status || 'starting')}${j.total ? ` · <span class="num">${fmt(j.done)}/${fmt(j.total)}</span>` : ''}</div>` +
        `<div class="bar big${pct == null ? ' indeterminate' : ''}" role="progressbar" aria-label="Duplicate progress"${pct != null ? ` aria-valuenow="${pct}"` : ''}><div style="width:${pct != null ? pct : 30}%"></div></div>` : '') +
      (D.error ? `<div class="status-line err">${icon('alert')}${esc(D.error)}</div>` : '') + '</div>' +
      `<div class="lbm-foot"><span class="grow"></span><button data-a="close"${D.busy ? ' disabled' : ''}>Cancel</button>` +
      `<button class="primary" data-a="go"${D.busy || !D.name.trim() ? ' disabled' : ''}>${icon('layers')}Duplicate</button></div>`;
    const f = focus ? S.box.querySelector<HTMLElement>(focus) : null;
    if (f) { f.focus(); if (f instanceof HTMLInputElement) f.select(); }
  }

  async function go(): Promise<void> {
    if (D.busy || !D.name.trim()) return;
    D.busy = true; S.busy = true; D.error = null; D.job = { stage: 'starting' };
    render();
    try {
      const r = await postJSON<JobStarted>(ep('/duplicate'), { name: D.name.trim() });
      let j: DuplicateJob;
      for (;;) {
        j = await getJSON<DuplicateJob>(`api/jobs/${encodeURIComponent(r.job_id)}`);
        if (j.status === 'error') throw new Error(j.error || 'duplicate failed');
        if (j.status === 'done') break;
        D.job = j;
        render();
        await new Promise((ok) => setTimeout(ok, 400));
      }
      D.busy = false; S.busy = false;
      S.close();
      if (onDone) onDone(j.result || null);
    } catch (e) {
      D.busy = false; S.busy = false; D.job = null; D.error = errText(e);
      render('[data-a="name"]');
    }
  }

  S.box.addEventListener('click', (e) => {
    const b = closestEl<HTMLButtonElement>(e.target, 'button[data-a]');
    if (!b || b.disabled) return;
    if (b.dataset['a'] === 'close') S.close(); else if (b.dataset['a'] === 'go') void go();
  });
  S.box.addEventListener('input', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement) || t.dataset['a'] !== 'name') return;
    D.name = t.value;
    const g = S.box.querySelector<HTMLButtonElement>('[data-a="go"]');
    if (g) g.disabled = !D.name.trim();
  });
  S.box.addEventListener('keydown', (e) => { if (e.key === 'Enter' && asEl(e.target)?.dataset['a'] === 'name') { e.preventDefault(); void go(); } });
  render('[data-a="name"]');
  if (!project) {
    getJSON<LabelProject>(ep('')).then((p) => { D.project = p; if (!D.name) D.name = `${p.name} (copy)`; if (!D.busy) render('[data-a="name"]'); }).catch(() => {});
  }
  return S;
}
