// "Label with model…" (bucket Edit, the details footer, the folder's Send menu): pick a model (Settings › Models, or
// a model server connection and one of its models; POST api/label/models/detect shows its classes, which become the
// session's classes), where to label (this bucket, or a copy into a new managed bucket: a streamed S3 bucket can
// only be copied), the confidence, and the output formats written into the bucket at the end (YOLO, COCO, VOC, LabelMe,
// CreateML, CSV, …: GET api/label/formats, filtered by the model's task, with a live preview of the folder tree).
// POST /api/dc/autolabel runs it as one job (copy → labeling session → label all → label files); progress shows in
// the activity strip.

import { errText, getJSON, postJSON } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { watchJobs } from './activity.js';
import { labelUrl } from '../core/nav.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { baseName, bucketOf, D, fmtKey, hooks } from './state.js';
import type { AutolabelBody, AutolabelDone } from '../types-datacenter.js';

interface Model { id: string; name?: string; task_type?: string; classes?: string[]; versions?: string[] }
interface Source { key: string; label: string; group: 'reg' | 'conn' }
interface LabelFormat { key: string; label: string; media: string[]; tasks: string[]; layout: string; note?: string }
interface AL {
  sources: Source[] | null; src: string; models: Model[] | null; model: string; version: string;
  status: { kind: 'busy' | 'ok' | 'err'; text: string } | null;
  target: 'inplace' | 'copy'; newName: string; conf: number; fmts: string[]; formats: LabelFormat[]; open: boolean; tok: number;
}

const LABELABLE = new Set(['object_detection', 'segmentation', 'keypoints', 'classification']);
const TT: Record<string, string> = { object_detection: 'detection', segmentation: 'segmentation', keypoints: 'pose', classification: 'classification' };

const card = (on: boolean, t: string, ico: string, title: string, txt: string, dis = false): string =>
  `<button type="button" class="dest-o${on ? ' on' : ''}" data-target="${t}" role="radio" aria-checked="${on}"${dis ? ' disabled' : ''}>` +
  `<span class="ri" aria-hidden="true"></span><span class="dt"><b>${title}</b><span>${txt}</span></span>${icon(ico)}</button>`;

const YOLOISH = new Set(['yolo', 'yolo_obb']);

/** The output format chips (only the formats that can write the model's task) and the tree they write. */
function formatsBlock(A: AL, task: string, where: string, loose: boolean): string {
  const fit = A.formats.filter((f) => f.media.includes('image') && f.tasks.includes(task));
  const on = A.fmts.filter((k) => fit.some((f) => f.key === k));
  const chips = fit.map((f) => `<button type="button" class="chip${on.includes(f.key) ? ' on' : ''}" data-fmt="${esc(f.key)}" aria-pressed="${on.includes(f.key)}"` +
    `${f.note ? ` title="${esc(f.note)}"` : ''}>${on.includes(f.key) ? icon('check') : ''}${esc(f.label)}</button>`).join('');
  const root = `${where.split('/').pop() || where}/`;
  const tree = on.length ? on.map((k) => {
    const f = fit.find((x) => x.key === k);
    return `<span class="faint"># ${esc(f?.label ?? k)}</span>\n${esc(root)}\n${esc(f?.layout ?? '')}`;
  }).join('\n\n') : '';
  return `<div class="m-lbl">Output format <span class="faint">· written into the bucket when done; pick none to keep the labels in the session only</span></div>` +
    `<div class="chips dc-al-fmts" role="group" aria-label="Output formats">${A.formats.length ? chips || '<span class="muted">no file format for this task</span>' : '<span class="spin sm"></span>'}</div>` +
    (tree ? `<div class="m-lbl">Folder structure <span class="faint">· preview</span></div><pre class="dc-al-tree">${tree}</pre>` : '') +
    (loose && on.some((k) => YOLOISH.has(k)) ? `<div class="note warn">${icon('alert')}<span>This folder has no <code>images/</code> folder: YOLO needs one, so its images are moved into <code>images/</code> (labels go in <code>labels/</code>). You can undo the move.</span></div>` : '');
}

function body(A: AL, canInplace: boolean, where: string, loose: boolean): string {
  const m = A.models?.find((x) => x.id === A.model);
  const st = A.status;
  const srcSel = A.sources === null ? '<span class="spin sm"></span>' : !A.sources.length
    ? `<div class="note warn">${icon('plug')}<span>No model yet: add one in Settings › Models, or a model server in Settings › Connections.</span></div>`
    : `<label class="fld"><span>Model</span><select id="dc-al-src">` +
      ['reg', 'conn'].map((g) => {
        const os = (A.sources ?? []).filter((s) => s.group === g);
        return os.length ? `<optgroup label="${g === 'reg' ? 'Settings › Models' : 'Model servers'}">${os.map((s) => `<option value="${esc(s.key)}"${s.key === A.src ? ' selected' : ''}>${esc(s.label)}</option>`).join('')}</optgroup>` : '';
      }).join('') + '</select></label>';
  const many = A.src.startsWith('conn:') && (A.models?.length ?? 0) > 0;
  const models = many ? `<div class="f2"><label class="fld"><span>Server model</span><select id="dc-al-model">${(A.models ?? []).map((x) =>
    `<option value="${esc(x.id)}"${x.id === A.model ? ' selected' : ''}${LABELABLE.has(x.task_type ?? 'object_detection') ? '' : ' disabled'}>${esc(x.name || x.id)} · ${esc(TT[x.task_type ?? 'object_detection'] ?? x.task_type ?? '')}</option>`).join('')}</select></label>` +
    (m?.versions?.length ? `<label class="fld"><span>Version</span><select id="dc-al-ver">${m.versions.map((v) => `<option${v === A.version ? ' selected' : ''}>${esc(v)}</option>`).join('')}</select></label>` : '<span></span>') + '</div>' : '';
  const classes = m?.classes ?? [];
  return `<div class="dc-send dc-al">${srcSel}${models}` +
    (st ? `<div class="status-line ${st.kind}" role="status">${st.kind === 'busy' ? '<span class="spin"></span>' : icon(st.kind === 'ok' ? 'check' : 'alert')}<span>${esc(st.text)}</span></div>` : '') +
    (m ? `<div class="m-lbl">Classes <span class="faint">· from the model, set up automatically</span></div><div class="tags-wrap dc-al-classes">${classes.slice(0, 60).map((c) => `<span class="dc-tag">${esc(c)}</span>`).join('')}` +
      `${classes.length > 60 ? `<span class="faint">+${classes.length - 60} more</span>` : ''}${classes.length ? '' : '<span class="muted">none reported</span>'}</div>` : '') +
    `<div class="m-lbl">Where to label <span class="faint">· ${esc(where)}</span></div><div class="dest" role="radiogroup" aria-label="Where to label">` +
    card(A.target === 'inplace', 'inplace', 'edit', 'In this bucket', canInplace ? 'Label files are written into this bucket.' : 'This bucket is read-only (streamed from S3).', !canInplace) +
    card(A.target === 'copy', 'copy', 'copy', 'Copy to a new bucket', 'Copy the images into a new managed bucket and label the copy.') + '</div>' +
    (A.target === 'copy' ? `<label class="fld"><span>New bucket name</span><input type="text" class="txt" id="dc-al-new" value="${esc(A.newName)}" autocomplete="off" spellcheck="false"></label>` : '') +
    `<label class="fld"><span>Confidence <b class="num" id="dc-al-cv">${A.conf.toFixed(2)}</b></span><input type="range" id="dc-al-conf" min="0.05" max="0.95" step="0.05" value="${A.conf}"></label>` +
    formatsBlock(A, m?.task_type ?? 'object_detection', where, A.target === 'inplace' && loose) +
    `<label class="check"><input type="checkbox" id="dc-al-open"${A.open ? ' checked' : ''}> Open the labeling session to review when done</label>` +
    `<p class="hint">${icon('lock')}Every image is labeled in a new labeling session, so you can review and fix the boxes. Existing label files with the same names are replaced.</p></div>`;
}

/** Follow an autolabel job: progress (and Cancel) in the activity strip, which reloads the page when it ends; a toast
 * at the end, and the session opens when asked. The images move into images/ early on: reload then too. */
async function track(jobId: number, name: string, open: boolean): Promise<void> {
  watchJobs();
  let moved = false;
  try {
    const j = await waitJob(jobId, (x) => {
      if (!moved && /session|listing|labeling|^label/i.test(x.stage ?? '')) { moved = true; void hooks.reload(); }
    });
    const r = (j as { result?: AutolabelDone }).result;
    const n = r?.labeled?.n_images ?? 0, fs = Object.keys(r?.written?.formats ?? {});
    toast(`${name}: ${n.toLocaleString('en-US')} images labeled${fs.length ? `, written as ${fs.map((k) => k.toUpperCase().replace('_', '-')).join(', ')}` : ''}`, 'ok');
    if (open && r?.session) location.href = labelUrl(r.session.id);
  } catch (e) {
    toast(`${name} failed: ${errText(e)}`, 'err');
  }
}

export async function openAutolabel(bid: string, path: string): Promise<void> {
  const b = bucketOf(bid);
  if (!b) return;
  const canInplace = b.writable;
  const A: AL = { sources: null, src: '', models: null, model: '', version: '', status: null, target: canInplace ? 'inplace' : 'copy',
    newName: `${b.name}${path ? `-${baseName(path)}` : ''}-labeled`, conf: 0.25, fmts: ['yolo'], formats: [], open: false, tok: 0 };
  const where = `${b.name}${path ? `/${path}` : ''}`;
  const fmt = D.bid === bid && D.path === path && D.listing ? D.listing.format : !path ? b.format : null;
  const loose = fmtKey(fmt) === 'images';                // loose images: the job moves them into images/
  let root: HTMLElement | null = null;
  const paint = (): void => {
    const host = root?.querySelector('.dc-al');
    if (!host) return;
    const nn = root?.querySelector<HTMLInputElement>('#dc-al-new')?.value;
    if (nn !== undefined) A.newName = nn;
    host.outerHTML = body(A, canInplace, where, loose);
  };
  const detect = async (): Promise<void> => {
    const tok = (A.tok += 1);
    A.models = null; A.model = '';
    A.status = { kind: 'busy', text: A.src.startsWith('reg:') ? 'Loading the model to read its classes…' : 'Asking the server which models it has…' };
    paint();
    try {
      const req = A.src.startsWith('reg:') ? { registry_id: A.src.slice(4) } : { connection_id: A.src.slice(5) };
      const r = await postJSON<{ protocol: string; models: Model[] }>('api/label/models/detect', req);
      if (tok !== A.tok) return;
      A.models = r.models ?? [];
      const ok = A.models.find((x) => LABELABLE.has(x.task_type ?? 'object_detection'));
      A.model = ok?.id ?? '';
      A.version = ok?.versions?.[ok.versions.length - 1] ?? '';
      A.status = ok ? { kind: 'ok', text: `${ok.name || ok.id} · ${TT[ok.task_type ?? 'object_detection'] ?? ok.task_type} · ${(ok.classes ?? []).length} classes` }
        : { kind: 'err', text: 'This model cannot label images' };
    } catch (e) {
      if (tok !== A.tok) return;
      A.status = { kind: 'err', text: errText(e) };
    }
    paint();
  };
  const load = async (): Promise<void> => {
    try {
      const [c, m, f] = await Promise.all([
        getJSON<{ connections?: Array<{ id: string; name?: string; url?: string; kind: string }> }>('api/connections'),
        getJSON<{ models?: Array<{ id: string; name: string; task_type?: string; family?: string; api?: { preset?: string } }> }>('api/models').catch(() => ({ models: [] })),
        getJSON<{ formats: LabelFormat[]; default: string[] }>('api/label/formats?media_type=image').catch(() => ({ formats: [], default: ['yolo'] })),
      ]);
      A.formats = f.formats;
      A.fmts = f.default;
      A.sources = [
        ...(m.models ?? []).filter((x) => x.family !== 'hf' && (LABELABLE.has(x.task_type ?? 'object_detection') || x.api?.preset === 'label'))
          .map((x): Source => ({ key: `reg:${x.id}`, label: x.name, group: 'reg' })),
        ...(c.connections ?? []).filter((x) => x.kind === 'model').map((x): Source => ({ key: `conn:${x.id}`, label: x.name || x.url || x.id, group: 'conn' })),
      ];
    } catch (e) {
      A.sources = [];
      A.status = { kind: 'err', text: `Could not list the models: ${errText(e)}` };
    }
    A.src = A.sources[0]?.key ?? '';
    paint();
    if (A.src) await detect();
  };
  await openDialog({
    title: 'Label with model', wide: true, ok: 'Start labeling', body: body(A, canInplace, where, loose), focus: '#dc-al-src',
    bind: (r) => {
      root = r;
      void load();
      r.addEventListener('click', (e) => {
        const t = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-target]') : null;
        if (t && !t.hasAttribute('disabled')) { A.target = t.dataset['target'] === 'copy' ? 'copy' : 'inplace'; paint(); }
        const fc = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-fmt]') : null;
        const k = fc?.dataset['fmt'];
        if (k) { A.fmts = A.fmts.includes(k) ? A.fmts.filter((x) => x !== k) : [...A.fmts, k]; paint(); }
      });
      r.addEventListener('change', (e) => {
        const t = e.target;
        if (t instanceof HTMLSelectElement && t.id === 'dc-al-src') { A.src = t.value; void detect(); }
        else if (t instanceof HTMLSelectElement && t.id === 'dc-al-model') {
          A.model = t.value;
          const m = A.models?.find((x) => x.id === A.model);
          A.version = m?.versions?.[m.versions.length - 1] ?? '';
          A.status = m ? { kind: 'ok', text: `${m.name || m.id} · ${(m.classes ?? []).length} classes` } : null;
          paint();
        } else if (t instanceof HTMLSelectElement && t.id === 'dc-al-ver') A.version = t.value;
        else if (t instanceof HTMLInputElement && t.id === 'dc-al-open') A.open = t.checked;
      });
      r.addEventListener('input', (e) => {
        const t = e.target;
        if (t instanceof HTMLInputElement && t.id === 'dc-al-conf') {
          A.conf = Number(t.value);
          const v = r.querySelector('#dc-al-cv');
          if (v) v.textContent = A.conf.toFixed(2);
        }
      });
    },
    onOk: async (r) => {
      if (!A.src) return 'Pick a model';
      const m = A.models?.find((x) => x.id === A.model);
      if (!m) return A.status?.kind === 'busy' ? 'Wait for the model to load' : 'Pick a model that can label images';
      const newName = r.querySelector<HTMLInputElement>('#dc-al-new')?.value.trim() ?? A.newName;
      if (A.target === 'copy' && !newName) return 'Name the new bucket';
      const req: AutolabelBody = {
        bucket: bid, path, target: A.target, conf: A.conf,
        formats: A.fmts.filter((k) => A.formats.some((f) => f.key === k && f.tasks.includes(m.task_type ?? 'object_detection'))),
        model: A.src.startsWith('reg:') ? { registry_id: A.src.slice(4), name: m.name || m.id } : { connection_id: A.src.slice(5), model_id: m.id, version: A.version || null, name: m.name || m.id },
      };
      if (A.target === 'copy') req.new_name = newName;
      let res;
      try { res = await api.autolabel(req); } catch (e) { return `Could not start: ${errText(e)}`; }
      toast(`Labeling ${A.target === 'copy' ? newName : where} with ${m.name || m.id}…`);
      // the dialog closes now; the new bucket shows up and the job is followed in the background
      if (A.target === 'copy') void hooks.reload();
      if (res.job_id != null) void track(res.job_id, A.target === 'copy' ? newName : b.name, A.open);
      return null;
    },
  });
}

export const AUTOLABEL_ACTIONS: Record<string, Action> = {
  autolabel: (el) => {
    const bid = el.dataset['bid'] || D.bid || D.focus || '';
    const path = el.dataset['path'] ?? (D.bid === bid ? D.path : '');
    if (bid) void openAutolabel(bid, path);
  },
};
