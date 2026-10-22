// The full-image dialog: one box (prev / next, similar, same image, its group), one whole image (the image browser,
// background images too), or one model prediction with no label. The image itself is views/viewer.js (boxes as
// SVG: toggles, right-click copy, Edit mode).

import { getJSON } from '../core/api.js';
import { $req, esc, ICONS } from '../core/dom.js';
import { featureLabel, S } from '../core/state.js';
import { clsColor, clsName, ERR, errColor, THEME } from '../core/theme.js';
import type { CardRef, EditResult, ImgBoxes, PredInfo, SampleInfo } from '../types-views.js';
import { afterEdit } from './curation.js';
import { galleryOrder } from './gallery.js';
import { imageOrder, onImageEdited } from './image-grid.js';
import { markEdited, modelEval } from './model.js';
import { closeViewer, dirty, initViewer, refreshPreds, setBoxes, showImage, V } from './viewer.js';

export let modalId: number | null = null;
let predMode: { pid: number; list: number[] } | null = null;       // a model prediction with no label box (false positive) is shown
let imageMode: { name: string; list: string[] | null } | null = null;   // a whole image from the image browser
let cardNav: { list: CardRef[] } | null = null;     // the model tab's card grid prev / next walk
let sampleImage: string | null = null;              // the image of modalId

// meta table: numbers stay monospace, sentences (a [key, html, true] row) use the body font
type MetaRow = [key: string, html: string | number, prose?: boolean];
const metaHtml = (rows: MetaRow[]): string => rows.map(([k, v, prose]) => `<tr><td>${k}</td><td${prose ? ' class="prose"' : ''}>${v}</td></tr>`).join('');
const num = (v: unknown, d = 2): string => (typeof v === 'number' ? String(+v.toFixed(d)) : esc(v ?? '–'));

export function initModal(): void {
  initViewer({ onSaved });
}

function syncPredsBtn(): void {
  const M = S.model;
  const b = $req('m-preds');
  const ev = modelEval();
  b.classList.toggle('hidden', !M.ev);
  b.classList.toggle('active', M.showPreds);
  b.setAttribute('aria-pressed', String(M.showPreds));
  if (ev) b.title = `Draw ${ev.model.name}'s predictions (dashed) at confidence ≥ ${M.conf} (P)`;
}

type DialogKind = 'box' | 'image' | 'pred';

function setMode(kind: DialogKind): void {
  for (const id of ['m-sameimg', 'm-similar']) $req(id).classList.toggle('hidden', kind !== 'box');
  if (kind !== 'box') $req('m-group').classList.add('hidden');
  $req('modal-crop').classList.toggle('hidden', kind === 'image');
  $req('m-prev').setAttribute('aria-label', kind === 'image' ? 'Previous image' : 'Previous box');
  $req('m-next').setAttribute('aria-label', kind === 'image' ? 'Next image' : 'Next box');
}

export function toggleModalPreds(): void {
  S.model.showPreds = !S.model.showPreds;
  syncPredsBtn();
  void refreshPreds();
}

function openDialog(): void {
  if ($req('modal').classList.contains('hidden')) modalReturnFocus = document.activeElement;
  $req('hover-tip').classList.add('hidden');
  $req('modal').classList.remove('hidden');
  $req('modal').querySelector<HTMLElement>('.modal-box')?.focus();
}

// unsaved editor changes: ask before they are dropped (next / previous / close)
const mayLeave = (): boolean => !dirty() || confirm('Discard the unsaved box changes?');

export async function openModal(id: number, nav: { list: CardRef[] } | null = null): Promise<void> {
  if (!mayLeave()) return;
  openDialog();
  cardNav = nav;
  modalId = id;
  predMode = null;
  imageMode = null;
  setMode('box');
  syncPredsBtn();
  $req<HTMLImageElement>('modal-crop').src = `api/crop/${id}`;
  $req('m-group').classList.toggle('hidden', !S.groups);
  const image = S.P && S.P.image ? S.P.image[id] ?? null : null;
  sampleImage = image;
  const feature = S.viewReq ? S.viewReq.feature : '';
  const [s] = await Promise.all([
    getJSON<SampleInfo>(`api/sample/${id}?feature=${encodeURIComponent(feature)}`),
    image ? showImage(image, { focus: id }) : Promise.resolve(null),
  ]);
  if (modalId !== id) return;
  if (!image) { sampleImage = s.image; void showImage(s.image, { focus: id }); }
  $req('modal-title').innerHTML = `<span class="chip"><span class="dot" style="background:${clsColor(s.cls)}"></span>${esc(s.cls_name)}</span>` +
    `<span class="num">#${id}</span><span class="muted">${esc(s.image)}</span>`;
  const g = S.groups ? S.groups.of.get(id) : undefined;
  const rows: MetaRow[] = [['class', `${esc(s.cls_name)} (${s.cls})`]];
  if (s.suggested_name !== undefined) {
    rows.push(['kNN suggests', `${esc(s.suggested_name)} · ${Math.round((s.suspicion ?? 0) * 100)}% neighbours disagree`, true],
      ['class distance', num(s.outlier, 4)],
      ['features', esc(featureLabel(s.feature ?? null))]);
  }
  const lq = s.label_quality;
  if (lq) {
    const flag = lq.issue ? ` · <span class="txt-danger">label issue</span> (#${lq.rank + 1})` : '';
    const bars = lq.top.map((t) => `<span class="lq-p" title="out-of-fold probability"><span class="dot" style="background:${clsColor(t.cls)}"></span>` +
      `${esc(t.name)} <b class="num">${Math.round(t.p * 100)}%</b></span>`).join('');
    rows.push(['cleanlab', `quality ${num(lq.quality, 3)}${flag}`, true], ['k-fold probs', bars, true]);
    if (lq.ood_feat != null) rows.push(['out-of-distr.', num(lq.ood_feat, 3)]);
  }
  const st = S.P?.m_status?.[id];
  if (st !== undefined && st >= 0) rows.push(['model', modelRow(id), true]);
  if (g !== undefined) rows.push(['group', g === -1 ? '<span class="txt-danger">outlier</span> (fits no group)' : `group ${g}`, true]);
  rows.push(
    ['image', esc(s.image)],
    ['label file', s.box_idx == null ? `${esc(s.label)} · removed` : `${esc(s.label)} · line ${s.box_idx + 1}`],
    ['bbox (px)', s.bbox.join(', ')],
    ['box size', `${s.bbox[2] - s.bbox[0]}×${s.bbox[3] - s.bbox[1]} (${(s.area * 100).toFixed(2)}% of image)`],
    ['image size', `${s.img_w}×${s.img_h}`],
    ['aspect w/h', num(s.aspect)],
    ['brightness', num(s.brightness, 1)],
    ['sharpness', num(s.blur, 1)],
    ['session', esc(s.session)],
    ['other boxes', s.same_image.length],
  );
  $req('modal-meta').innerHTML = metaHtml(rows);
}

function modelRow(id: number): string {
  const P = S.P;
  const st = P?.m_status?.[id] ?? -1;
  const pred = P?.m_pred?.[id] ?? -1;
  const score = P?.m_score?.[id];
  return `<span style="color:${errColor(st)};font-weight:600">${ERR[st]?.label ?? ''}</span>` +
    (pred >= 0 ? ` · says ${esc(clsName(pred))}` : '') + (score ? ` · ${score.toFixed(2)}` : '') +
    ` <span class="muted">(${esc(modelEval()?.model.name)})</span>`;
}

// one whole image (the image browser): all its boxes, nothing ringed; list = the order prev / next walk
export async function openImage(name: string, list: string[] | null = null): Promise<void> {
  if (!mayLeave()) return;
  openDialog();
  cardNav = null;
  modalId = null;
  predMode = null;
  imageMode = { name, list };
  sampleImage = name;
  setMode('image');
  syncPredsBtn();
  $req('modal-title').innerHTML = `<span class="chip"><svg class="ico" aria-hidden="true"><use href="${ICONS}#i-image"/></svg>image</span><span class="muted">${esc(name)}</span>`;
  $req('modal-meta').innerHTML = '';
  const b = await showImage(name);
  if (!imageMode || imageMode.name !== name || !b) return;
  renderImageMeta(b);
}

type BoxesInfo = Pick<ImgBoxes, 'boxes' | 'img_w' | 'img_h' | 'label' | 'writable'>;

function renderImageMeta(b: BoxesInfo): void {
  const counts = new Map<number, number>();
  for (const x of b.boxes) counts.set(x.cls, (counts.get(x.cls) || 0) + 1);
  const pending = b.boxes.filter((x) => x.sid == null).length;
  const rows: MetaRow[] = [
    ['image', esc(V.name)],
    ['image size', `${b.img_w}×${b.img_h}`],
    ['label file', `${esc(b.label)}${b.boxes.length ? '' : ' · none yet (background image)'}`],
    ['boxes', b.boxes.length ? `${b.boxes.length}${pending ? ` <span class="muted">(${pending} not in the plot until a re-index)</span>` : ''}` : '0 · background'],
    ...[...counts.entries()].sort((a, c) => c[1] - a[1]).map(([c, n]): MetaRow => [
      `<span class="dot" style="background:${clsColor(c)}"></span>`, `${esc(S.meta?.classes[c] ?? `class ${c}`)} · ${n}`]),
    ['editing', b.writable ? 'Edit (E) fixes boxes or draws missing ones' : 'read-only source', true],
  ];
  $req('modal-meta').innerHTML = metaHtml(rows);
}

// a prediction that matches no label (no box id): the editable image with labels solid + predictions dashed, this one ringed
export async function openPredModal(pid: number, list: number[] = [pid], nav: { list: CardRef[] } | null = null): Promise<void> {
  if (!mayLeave()) return;
  openDialog();
  cardNav = nav;
  const M = S.model;
  modalId = null;
  imageMode = null;
  predMode = { pid, list };
  M.showPreds = true;
  setMode('pred');
  syncPredsBtn();
  const base = `api/models/${encodeURIComponent(String(M.id))}/pred/${pid}`;
  $req<HTMLImageElement>('modal-crop').src = `${base}/crop`;
  $req('modal-meta').innerHTML = '';
  const p = await getJSON<PredInfo>(base);
  if (!predMode || predMode.pid !== pid) return;
  sampleImage = p.image;
  $req('modal-title').innerHTML = `<span class="chip"><span class="dot" style="background:${p.cls >= 0 ? clsColor(p.cls) : THEME.ungrouped}"></span>` +
    `${esc(p.cls_name || 'unknown class')}</span><span class="num">${p.score.toFixed(2)}</span><span class="muted">${esc(p.image)}</span>`;
  const b = await showImage(p.image, { focusPid: pid });
  if (!predMode || predMode.pid !== pid || !b) return;
  renderPredMeta(p, b, pid);
}

const PRED_KINDS: Record<string, string> = { fp_bg: 'no label there: missing label, or a false alarm', fp_dup: 'second box on an already found object', fp_loc: 'right class, badly placed', tp: 'matches a label', wrong_cls: 'on a label of another class' };

function renderPredMeta(p: PredInfo, b: BoxesInfo, pid: number): void {
  const ev = modelEval();
  const fp = ev ? ev.fps.find((f) => f.pid === pid) || (ev.issues || []).find((x) => x.pid === pid) : undefined;
  const verdict = fp ? PRED_KINDS[fp.kind] || fp.kind : null;
  const [x1, y1, x2, y2] = [Math.round(p.box[0]), Math.round(p.box[1]), Math.round(p.box[2]), Math.round(p.box[3])] as const;
  const rows: MetaRow[] = [['model prediction', `${esc(p.cls_name || '?')} · ${p.score.toFixed(2)} <span class="muted">(${esc(ev?.model.name)})</span>`, true]];
  if (verdict) rows.push(['verdict', esc(verdict), true]);
  rows.push(
    ['image', esc(p.image)],
    ['label file', `${esc(b.label)}${b.boxes.length ? '' : ' · none yet (background image)'}`],
    ['bbox (px)', `${x1}, ${y1}, ${x2}, ${y2}`],
    ['box size', `${x2 - x1}×${y2 - y1} (${(((x2 - x1) * (y2 - y1)) / ((b.img_w * b.img_h) || 1) * 100).toFixed(2)}% of image)`],
    ['image size', `${b.img_w}×${b.img_h}`],
    ['labels in image', b.boxes.length],
    ['editing', b.writable ? 'Edit (E) to draw the missing label or fix a box' : 'read-only source', true],
  );
  $req('modal-meta').innerHTML = metaHtml(rows);
}

// the editor saved: viewer boxes, the plot's in-memory points, the Curation banner / toast, the browser tile
function onSaved(r: EditResult): void {
  setBoxes(r);
  const n = r.applied || 0;
  afterEdit(r, `Saved ${n} change${n === 1 ? '' : 's'} to ${r.label}`);
  onImageEdited(V.name ?? '', r.status);
  if (n > 0) markEdited(V.name ?? '');
  if (imageMode && imageMode.name === V.name) renderImageMeta(r);
  if (predMode) {
    const pid = predMode.pid;
    void getJSON<PredInfo>(`api/models/${encodeURIComponent(String(S.model.id))}/pred/${pid}`).then((p) => { if (predMode) renderPredMeta(p, r, pid); });
  }
}

let modalReturnFocus: Element | null = null;

export function closeModal(): void {
  if (!mayLeave()) return;
  closeViewer();
  $req('modal').classList.add('hidden');
  modalId = null;
  predMode = null;
  imageMode = null;
  cardNav = null;
  sampleImage = null;
  if (modalReturnFocus instanceof HTMLElement && document.contains(modalReturnFocus)) modalReturnFocus.focus();
  modalReturnFocus = null;
}

// open one card of the model tab's grids; `list` (all cards, in order) is what prev / next walk
export function openCard(item: CardRef, list: CardRef[]): void {
  const nav = { list };
  if (item.sid != null) void openModal(item.sid, nav);
  else void openPredModal(item.pid ?? -1, list.filter((x) => x.sid == null).map((x) => x.pid ?? -1), nav);
}

export function stepModal(d: number): void {
  if (cardNav && cardNav.list.length > 1) {
    const { list } = cardNav;
    const cur = predMode ? list.findIndex((x) => x.pid === predMode?.pid && x.sid == null) : list.findIndex((x) => x.sid === modalId);
    const next = cur >= 0 ? list[(cur + d + list.length) % list.length] : undefined;
    if (next) openCard(next, list);
    return;
  }
  if (predMode) {
    const { list } = predMode, i = list.indexOf(predMode.pid);
    const next = list[(i + d + list.length) % list.length];
    if (list.length > 1 && next !== undefined) void openPredModal(next, list);
    return;
  }
  if (imageMode) {
    const list = imageMode.list || imageOrder();
    const i = list.indexOf(imageMode.name);
    const next = list[(i + d + list.length) % list.length];
    if (i >= 0 && list.length > 1 && next !== undefined) void openImage(next, imageMode.list);
    return;
  }
  const order = galleryOrder();
  const i = modalId === null ? -1 : order.indexOf(modalId);
  const next = order[(i + d + order.length) % order.length];
  if (i < 0 || !order.length || next === undefined) return;
  void openModal(next);
}

export const modalImage = (): string | null => sampleImage;
