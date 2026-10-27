// "Send to session" (img5, views/dialog.ts openDialog): the destination (Visualize: embed and explore / Label: a
// labeling project), the session name, the task type of a labeling project (label/state.ts TASK_TYPES), a warning
// when the data has no labels to bring along, and the selected folders / files with their counts. POST /api/dc/send
// creates the session (the data is referenced in place, never copied), then the page goes there.

import { errText } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import { labelUrl, vizUrl } from '../core/nav.js';
import { TASK_TYPES } from '../label/state.js';
import { openDialog } from '../views/dialog.js';
import * as api from './api.js';
import { baseName, bucketOf, D, fmtKey, fmtLabel, fmtN, fmtSize, isYolo, key } from './state.js';
import type { SendTarget } from '../types-datacenter.js';
import type { TaskType } from '../types-label.js';

/** label/state.ts task types → the send API's task_type */
const TASK_API: Record<TaskType, string> = { object_detection: 'detect', segmentation: 'segment', keypoints: 'pose', classification: 'classify' };
const TASK_SHORT: Record<TaskType, string> = { object_detection: 'Detect', segmentation: 'Segment', keypoints: 'Pose', classification: 'Classify' };
const FMT_TASK: Record<string, TaskType> = { 'yolo-detect': 'object_detection', 'yolo-obb': 'object_detection', 'yolo-seg': 'segmentation', 'yolo-pose': 'keypoints', classification: 'classification', coco: 'object_detection', voc: 'object_detection' };

const today = (): string => new Date().toLocaleDateString('en-US', { month: 'short', day: '2-digit' }).toLowerCase().replace(' ', '');

export async function openSend(bid: string, paths0: string[], target0: SendTarget = 'visualize'): Promise<void> {
  const b = bucketOf(bid);
  if (!b) return;
  let target = target0;
  const paths = paths0.length ? [...new Set(paths0)] : [''];
  const fmt = D.bid === bid && D.listing ? D.listing.format ?? b.format : b.format;
  let task: TaskType = FMT_TASK[fmtKey(fmt)] ?? 'object_detection';
  const first = paths[0] ?? '';
  const defName = `${b.name}${first ? ` ${baseName(first)}` : ''} · ${today()}`;
  const entryOf = (p: string): { dir: boolean; n: number | null; size: number | null } => {
    const e = D.listing?.entries.find((x) => x.path === p);
    if (e) return { dir: e.type === 'dir', n: e.type === 'dir' ? e.items ?? null : 1, size: e.type === 'dir' ? null : e.size };
    const s = D.stats.get(key(bid, p));
    if (s && typeof s === 'object') return { dir: true, n: s.images, size: s.size };
    if (p === '') return { dir: true, n: b.stats?.images ?? null, size: b.stats?.size ?? null };
    return { dir: true, n: null, size: null };
  };
  const rows = paths.map((p) => ({ p, ...entryOf(p) }));
  const totN = rows.reduce((a, r) => a + (r.n ?? 0), 0), totS = rows.reduce((a, r) => a + (r.size ?? 0), 0);
  const noLabels = !isYolo(fmt) && fmtKey(fmt) !== 'coco' && fmtKey(fmt) !== 'voc';
  const destCard = (t: SendTarget, ico: string, title: string, txt: string): string =>
    `<button type="button" class="dest-o${target === t ? ' on' : ''}" data-dest="${t}" role="radio" aria-checked="${target === t}">` +
    `<span class="ri" aria-hidden="true"></span><span class="dt"><b>${title}</b><span>${txt}</span></span>${icon(ico)}</button>`;
  const body = (): string =>
    `<div class="dc-send"><div class="m-lbl">Destination</div><div class="dest" role="radiogroup" aria-label="Destination">` +
    destCard('visualize', 'scatter', 'Visualize', 'Embed and explore: clusters, outliers, near-duplicates') +
    destCard('label', 'edit', 'Label', 'Draw, fix and review annotations with your team') + '</div>' +
    `<div class="f2"><label class="fld"><span>Session name</span><input type="text" class="txt" id="dc-send-name" value="${esc(defName)}" autocomplete="off"></label>` +
    `<div class="fld dc-task${target === 'label' ? '' : ' hidden'}"><span>Task type</span><div class="segmented" role="radiogroup" aria-label="Task type">` +
    (Object.keys(TASK_TYPES) as TaskType[]).map((k) => `<button type="button" class="view-btn${task === k ? ' active' : ''}" data-task="${k}" role="radio" aria-checked="${task === k}" title="${esc(TASK_TYPES[k].label)}">${icon(TASK_TYPES[k].icon)}${TASK_SHORT[k]}</button>`).join('') +
    '</div></div></div>' +
    (target === 'label' && noLabels ? `<div class="note warn">${icon('alert')}<span>This data is <b>${esc(fmtLabel(fmt))}</b>, so no existing labels will be imported: the session starts empty.</span></div>` : '') +
    `<div class="m-lbl">Selected data <span class="faint">· referenced in place, not copied</span></div>` +
    `<div class="seltable">${rows.map((r) => `<div class="r">${icon(r.dir ? 'folder' : 'file-img')}<span class="p">${esc(b.name)}${r.p ? `/${esc(r.p)}` : ''}</span>` +
      `<span class="num">${r.n != null ? `${fmtN(r.n)} img` : ''}</span><span class="num">${r.size != null ? fmtSize(r.size) : ''}</span></div>`).join('')}` +
    (rows.length > 1 ? `<div class="r tot"><span></span><span>${rows.length} items</span><span class="num">${fmtN(totN)} img</span><span class="num">${totS ? fmtSize(totS) : ''}</span></div>` : '') + '</div>' +
    `<p class="hint">${icon('lock')}Files stay in Data Center; moving them later updates the session.</p></div>`;
  const okText = (): string => (target === 'label' ? 'Create label session' : 'Create visualize session');
  await openDialog({
    title: 'Send to session', wide: true, ok: okText(), body: body(), focus: '#dc-send-name',
    bind: (root) => {
      const repaint = (): void => {
        const name = root.querySelector<HTMLInputElement>('#dc-send-name')?.value ?? defName;
        const host = root.querySelector('.dc-send');
        if (host) host.outerHTML = body();
        const i = root.querySelector<HTMLInputElement>('#dc-send-name');
        if (i) i.value = name;
        const ok = root.closest('.ui-dlg')?.querySelector('[data-dlg="ok"]') ?? root.parentElement?.querySelector('[data-dlg="ok"]');
        if (ok) ok.textContent = okText();
      };
      root.addEventListener('click', (e) => {
        const t = e.target instanceof Element ? e.target : null;
        const d = t?.closest<HTMLElement>('[data-dest]'), k = t?.closest<HTMLElement>('[data-task]');
        if (d) { target = d.dataset['dest'] === 'label' ? 'label' : 'visualize'; repaint(); }
        if (k) { task = (k.dataset['task'] as TaskType | undefined) ?? task; repaint(); }
      });
    },
    onOk: async (root) => {
      const name = root.querySelector<HTMLInputElement>('#dc-send-name')?.value.trim() || defName;
      try {
        const r = await api.send(bid, paths, target, name, TASK_API[task]);
        location.href = r.target === 'label' ? labelUrl(r.id) : vizUrl(r.id);
        return null;
      } catch (e) {
        return `Could not create the session: ${errText(e)}`;
      }
    },
  });
}
