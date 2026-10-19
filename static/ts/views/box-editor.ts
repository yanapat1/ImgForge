// Edit mode of the image viewer: select / move / resize boxes (8 handles), draw new ones, delete, change the class,
// accept a model prediction as a label, undo / redo; Save sends one op batch (set_box / set_class / delete / add)
// to POST api/edits, which writes the label file through the curation backup + undo path. Works on a copy of the
// viewer's boxes in normalised coordinates; the viewer (views/viewer.js) owns the SVG and calls render().

import { errMessage, postJSON } from '../core/api.js';
import { $req, esc } from '../core/dom.js';
import { S } from '../core/state.js';
import type { CurOp, EditorDraw, EditResult, ImagePred, ImgBox, ViewerState } from '../types-views.js';

const NS = 'http://www.w3.org/2000/svg';
const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'] as const;
type Handle = (typeof HANDLES)[number];
const MIN_PX = 3;                // a drawn box smaller than this (screen pixels) is a click, not a box
const EPS = 1e-6;

/** The working copy of one box (key = its line in the label file, null for a box added here). */
interface EBox { key: number | null; cls: number; cx: number; cy: number; w: number; h: number; sid: number | null }
type Tool = 'select' | 'draw' | 'region';
type Pt = [number, number];
type Drag =
  | { kind: 'resize'; h: string; before: string; box: EBox }
  | { kind: 'move'; x0: number; y0: number; before: string; box: EBox }
  | { kind: 'draw'; x0: number; y0: number; cur: Pt | null; sx: number; sy: number }
  | { kind: 'region'; x0: number; y0: number; cur: Pt | null; sx: number; sy: number };

interface EditorState {
  on: boolean; svg: SVGSVGElement | null; V: ViewerState | null; redraw: () => void; onSaved: (r: EditResult) => void;
  boxes: EBox[]; sel: number; tool: Tool; undo: string[]; redo: string[]; drag: Drag | null; saving: boolean; cls: number;
  multi: Set<number>;
}
const E: EditorState = { on: false, svg: null, V: null, redraw: () => {}, onSaved: () => {}, boxes: [], sel: -1, tool: 'select',
  undo: [], redo: [], drag: null, saving: false, cls: 0, multi: new Set() };

/** The viewer state the editor was initialised with. */
const view = (): ViewerState => { if (!E.V) throw new Error('box editor not initialised'); return E.V; };
const stage = (): SVGSVGElement => { if (!E.svg) throw new Error('box editor not initialised'); return E.svg; };
const selBox = (): EBox | undefined => E.boxes[E.sel];

const svgEl = (tag: string, attrs: Record<string, string | number>): SVGElement => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
};
const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));
const snap = (): string => JSON.stringify({ boxes: E.boxes, sel: E.sel });

export interface EditorInit { svg: SVGSVGElement; V: ViewerState; redraw: () => void; onSaved: (r: EditResult) => void }

export function initEditor({ svg, V, redraw, onSaved }: EditorInit): void {
  Object.assign(E, { svg, V, redraw, onSaved });
  svg.addEventListener('pointerdown', down);
  svg.addEventListener('pointermove', move);
  svg.addEventListener('pointerup', up);
  svg.addEventListener('pointercancel', () => { E.drag = null; E.redraw(); });
  $req('vw-t-select').onclick = () => setTool('select');
  $req('vw-t-draw').onclick = () => setTool('draw');
  $req('vw-t-region').onclick = () => setTool('region');
  $req('vw-del').onclick = () => removeSel();
  $req('vw-undo').onclick = () => history(-1);
  $req('vw-redo').onclick = () => history(1);
  $req('vw-save').onclick = () => { void save(); };
  $req('vw-discard').onclick = () => { load(); status('Changes dropped'); };
  $req<HTMLSelectElement>('vw-cls').onchange = () => {
    const c = +$req<HTMLSelectElement>('vw-cls').value;
    E.cls = c;
    if (E.multi.size) setMultiClass(c);
    else {
      const b = selBox();
      if (b && b.cls !== c) change(() => { b.cls = c; });
    }
  };
}

export const active = (): boolean => E.on;
export const liveBoxes = (): EBox[] => E.boxes;

// the working copy from the viewer's boxes (key = the box's line in the file: how a change maps to an op)
function load(): void {
  const V = view();
  E.boxes = V.boxes.map((b) => ({ key: b.line, cls: b.cls, cx: b.cx, cy: b.cy, w: b.w, h: b.h, sid: b.sid }));
  E.sel = E.boxes.findIndex((b) => b.sid != null && b.sid === V.focus);
  E.multi.clear();
  E.undo = [];
  E.redo = [];
  E.drag = null;
  syncBar();
  E.redraw();
}

export function setActive(on: boolean): void {
  E.on = on;
  $req('vw-editbar').classList.toggle('hidden', !on);
  $req('vw').classList.toggle('editing', on);
  if (on) {
    const cls = S.meta ? S.meta.classes : [];
    $req('vw-cls').innerHTML = cls.map((n, i) => `<option value="${i}">${i < 9 ? `${i + 1} · ` : ''}${esc(n)}</option>`).join('');
    load();
    status(view().boxes.length ? 'Click a box to select it · N to draw · click a dashed prediction to add it as a label' : 'No boxes yet: press N and drag to draw one');
  } else {
    E.boxes = [];
    E.drag = null;
  }
}

export function reset(): void {
  if (E.on) setActive(false);
  E.boxes = [];
  E.undo = [];
  E.redo = [];
  E.drag = null;
  E.saving = false;
}

// ---------------------------------------------------------------- changes
function ops(): CurOp[] {
  const out: CurOp[] = [];
  const by = new Map(E.boxes.filter((b) => b.key != null).map((b): [number, EBox] => [b.key as number, b]));
  for (const o of view().boxes) {
    const b = by.get(o.line);
    if (!b) { out.push({ op: 'delete', line: o.line }); continue; }
    if (b.cls !== o.cls) out.push({ op: 'set_class', line: o.line, cls: b.cls });
    if (Math.abs(b.cx - o.cx) > EPS || Math.abs(b.cy - o.cy) > EPS || Math.abs(b.w - o.w) > EPS || Math.abs(b.h - o.h) > EPS) {
      out.push({ op: 'set_box', line: o.line, cx: b.cx, cy: b.cy, w: b.w, h: b.h });
    }
  }
  for (const b of E.boxes) if (b.key == null) out.push({ op: 'add', cls: b.cls, cx: b.cx, cy: b.cy, w: b.w, h: b.h });
  return out;
}

export const dirty = (): boolean => E.on && ops().length > 0;

function change(fn: () => void): void {
  const before = snap();
  fn();
  if (snap() === before) return;
  E.undo.push(before);
  if (E.undo.length > 200) E.undo.shift();
  E.redo = [];
  syncBar();
  E.redraw();
}

function history(d: number): void {
  const from = d < 0 ? E.undo : E.redo, to = d < 0 ? E.redo : E.undo;
  const last = from.pop();
  if (last === undefined) return;
  to.push(snap());
  const s: { boxes: EBox[]; sel: number } = JSON.parse(last);
  E.multi.clear();
  E.boxes = s.boxes;
  E.sel = s.sel;
  syncBar();
  E.redraw();
}

// every box picked with the Region tool takes the class in one undo step
function setMultiClass(c: number): void {
  const idx = [...E.multi].filter((i) => E.boxes[i] && E.boxes[i]?.cls !== c);
  if (!idx.length) return;
  change(() => { for (const i of idx) { const b = E.boxes[i]; if (b) b.cls = c; } });
  status(`${idx.length} box${idx.length === 1 ? '' : 'es'} set to ${S.meta?.classes[c] ?? c} (unsaved)`);
}

function removeSel(): void {
  if (E.multi.size) {
    const rm = E.multi;
    E.multi = new Set();
    change(() => { E.boxes = E.boxes.filter((_, i) => !rm.has(i)); E.sel = -1; });
    return;
  }
  if (E.sel < 0) return;
  change(() => { E.boxes.splice(E.sel, 1); E.sel = -1; });
}

function setTool(t: Tool): void {
  E.tool = t;
  for (const [id, k] of [['vw-t-select', 'select'], ['vw-t-draw', 'draw'], ['vw-t-region', 'region']] as const) {
    $req(id).classList.toggle('active', t === k);
    $req(id).setAttribute('aria-pressed', String(t === k));
  }
  $req('vw').classList.toggle('drawing', t === 'draw' || t === 'region');
  if (t !== 'region' && E.multi.size) { E.multi.clear(); syncBar(); E.redraw(); }
}

function syncBar(): void {
  const n = E.on ? ops().length : 0;
  $req<HTMLButtonElement>('vw-undo').disabled = !E.undo.length;
  $req<HTMLButtonElement>('vw-redo').disabled = !E.redo.length;
  $req<HTMLButtonElement>('vw-del').disabled = E.sel < 0 && !E.multi.size;
  $req<HTMLButtonElement>('vw-save').disabled = !n || E.saving;
  $req<HTMLButtonElement>('vw-discard').disabled = !n || E.saving;
  if (E.multi.size) status(`${E.multi.size} box${E.multi.size === 1 ? '' : 'es'} in the region: pick a class to relabel them (Esc clears)`);
  const cur = selBox();
  if (cur) $req<HTMLSelectElement>('vw-cls').value = String(cur.cls);
  else $req<HTMLSelectElement>('vw-cls').value = String(E.cls);
  if (n) status(`${n} unsaved change${n === 1 ? '' : 's'}`);
}

function status(t: string): void { $req('vw-edit-st').textContent = t; }

// ---------------------------------------------------------------- drawing (called by the viewer)
export function render(k: number, { names, color, nameOf, tag }: EditorDraw): SVGElement {
  const g = svgEl('g', { class: 'vw-edit' });
  const { W, H } = view();
  const tags: SVGElement[] = [];      // appended after every box, so no outline crosses a tag
  E.boxes.forEach((b, i) => {
    const x = (b.cx - b.w / 2) * W, y = (b.cy - b.h / 2) * H, w = b.w * W, h = b.h * H;
    const r = svgEl('rect', { x, y, width: w, height: h, stroke: color(b.cls), 'data-i': i,
      class: `vw-box vw-ebox${i === E.sel ? ' sel' : ''}${E.multi.has(i) ? ' multi' : ''}${b.key == null ? ' new' : ''}` });
    g.append(r);
    if (names) tags.push(...tag(x, y, `${b.key == null ? '+ ' : ''}${nameOf(b.cls)}`, color(b.cls), k));
  });
  g.append(...tags);
  const sb = selBox();
  if (sb) {
    const s = 9 * k;
    const x1 = (sb.cx - sb.w / 2) * W, y1 = (sb.cy - sb.h / 2) * H, x2 = x1 + sb.w * W, y2 = y1 + sb.h * H;
    const at: Record<Handle, Pt> = { nw: [x1, y1], n: [(x1 + x2) / 2, y1], ne: [x2, y1], e: [x2, (y1 + y2) / 2], se: [x2, y2], s: [(x1 + x2) / 2, y2],
      sw: [x1, y2], w: [x1, (y1 + y2) / 2] };
    for (const hd of HANDLES) {
      g.append(svgEl('rect', { x: at[hd][0] - s / 2, y: at[hd][1] - s / 2, width: s, height: s, 'data-h': hd, class: `vw-handle h-${hd}` }));
    }
  }
  const d = E.drag;
  if (d && d.kind === 'region' && d.cur) {
    g.append(svgEl('rect', { x: Math.min(d.x0, d.cur[0]) * W, y: Math.min(d.y0, d.cur[1]) * H, width: Math.abs(d.cur[0] - d.x0) * W,
      height: Math.abs(d.cur[1] - d.y0) * H, class: 'vw-box vw-region' }));
  }
  if (d && d.kind === 'draw' && d.cur) {
    g.append(svgEl('rect', { x: Math.min(d.x0, d.cur[0]) * W, y: Math.min(d.y0, d.cur[1]) * H, width: Math.abs(d.cur[0] - d.x0) * W,
      height: Math.abs(d.cur[1] - d.y0) * H, stroke: color(E.cls), class: 'vw-box vw-drawing' }));
  }
  return g;
}

// ---------------------------------------------------------------- pointer
function toNorm(e: PointerEvent): Pt {
  const m = stage().getScreenCTM();
  if (!m) return [0, 0];
  const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
  const V = view();
  return [clamp01(p.x / V.W), clamp01(p.y / V.H)];
}

function down(e: PointerEvent): void {
  if (!E.on || e.button !== 0) return;
  const ds = e.target instanceof SVGElement || e.target instanceof HTMLElement ? e.target.dataset : undefined;
  const [x, y] = toNorm(e);
  stage().setPointerCapture(e.pointerId);
  e.preventDefault();
  const cur = selBox();
  if (ds && ds.h && cur) {
    E.drag = { kind: 'resize', h: ds.h, before: snap(), box: { ...cur } };
    return;
  }
  if (E.tool === 'region') { E.multi.clear(); E.sel = -1; E.drag = { kind: 'region', x0: x, y0: y, cur: null, sx: e.clientX, sy: e.clientY }; syncBar(); return; }
  if (E.tool === 'draw') { E.drag = { kind: 'draw', x0: x, y0: y, cur: null, sx: e.clientX, sy: e.clientY }; return; }
  if (ds && ds.pid !== undefined) {           // a dashed prediction: add it as a label
    const p = view().preds.find((q) => String(q.pid) === ds.pid);
    if (p) accept(p);
    return;
  }
  if (ds && ds.i !== undefined) {
    E.sel = +ds.i;
    const b = selBox();
    if (b) E.drag = { kind: 'move', x0: x, y0: y, before: snap(), box: { ...b } };
    syncBar();
    E.redraw();
    return;
  }
  E.sel = -1;
  syncBar();
  E.redraw();
}

function move(e: PointerEvent): void {
  const d = E.drag;
  if (!d) return;
  const [x, y] = toNorm(e);
  if (d.kind === 'draw' || d.kind === 'region') { d.cur = [x, y]; E.redraw(); return; }
  const b = selBox(), o = d.box;
  if (!b) return;
  if (d.kind === 'move') {
    const hw = o.w / 2, hh = o.h / 2;
    b.cx = Math.min(1 - hw, Math.max(hw, o.cx + x - d.x0));
    b.cy = Math.min(1 - hh, Math.max(hh, o.cy + y - d.y0));
  } else {
    let x1 = o.cx - o.w / 2, y1 = o.cy - o.h / 2, x2 = o.cx + o.w / 2, y2 = o.cy + o.h / 2;
    if (d.h.includes('w')) x1 = x;
    if (d.h.includes('e')) x2 = x;
    if (d.h.includes('n')) y1 = y;
    if (d.h.includes('s')) y2 = y;
    const V = view();
    const minw = (MIN_PX * scaleK()) / V.W, minh = (MIN_PX * scaleK()) / V.H;
    if (Math.abs(x2 - x1) < minw || Math.abs(y2 - y1) < minh) return;
    b.cx = (x1 + x2) / 2; b.cy = (y1 + y2) / 2; b.w = Math.abs(x2 - x1); b.h = Math.abs(y2 - y1);
  }
  E.redraw();
}

function up(e: PointerEvent): void {
  const d = E.drag;
  E.drag = null;
  if (!d) return;
  if (d.kind === 'region') {
    if (d.cur && Math.abs(e.clientX - d.sx) >= MIN_PX && Math.abs(e.clientY - d.sy) >= MIN_PX) {
      const [x1, x2] = [Math.min(d.x0, d.cur[0]), Math.max(d.x0, d.cur[0])], [y1, y2] = [Math.min(d.y0, d.cur[1]), Math.max(d.y0, d.cur[1])];
      E.boxes.forEach((b, i) => { if (b.cx >= x1 && b.cx <= x2 && b.cy >= y1 && b.cy <= y2) E.multi.add(i); });
      if (!E.multi.size) status('No box centre inside that region');
    }
    syncBar();
    E.redraw();
    return;
  }
  if (d.kind === 'draw') {
    if (d.cur && Math.abs(e.clientX - d.sx) >= MIN_PX && Math.abs(e.clientY - d.sy) >= MIN_PX) {
      const [x1, x2] = [Math.min(d.x0, d.cur[0]), Math.max(d.x0, d.cur[0])], [y1, y2] = [Math.min(d.y0, d.cur[1]), Math.max(d.y0, d.cur[1])];
      change(() => {
        E.boxes.push({ key: null, cls: E.cls, cx: (x1 + x2) / 2, cy: (y1 + y2) / 2, w: x2 - x1, h: y2 - y1, sid: null });
        E.sel = E.boxes.length - 1;
      });
    } else E.redraw();
    return;
  }
  // a move / resize changes the box in place: record its undo step now (if it moved at all)
  if (snap() !== d.before) {
    E.undo.push(d.before);
    E.redo = [];
  }
  syncBar();
  E.redraw();
}

function scaleK(): number {
  const w = $req('modal-img').getBoundingClientRect().width;       // on-screen width, zoom included
  return w ? view().W / w : 1;
}

function accept(p: ImagePred): void {
  if (p.cls < 0) { status('This prediction is of a class the dataset does not have'); return; }
  const [x1, y1, x2, y2] = p.box, { W, H } = view();
  change(() => {
    E.boxes.push({ key: null, cls: p.cls, cx: clamp01((x1 + x2) / 2 / W), cy: clamp01((y1 + y2) / 2 / H),
      w: Math.min(1, (x2 - x1) / W), h: Math.min(1, (y2 - y1) / H), sid: null });
    E.sel = E.boxes.length - 1;
  });
  status('Prediction added as a label (unsaved)');
}

// ---------------------------------------------------------------- keys (true = handled)
export function key(e: KeyboardEvent): boolean {
  const k = e.key, mod = e.ctrlKey || e.metaKey;
  if (mod && k.toLowerCase() === 's') { e.preventDefault(); void save(); return true; }
  if (mod && k.toLowerCase() === 'z') { e.preventDefault(); history(e.shiftKey ? 1 : -1); return true; }
  if (mod && k.toLowerCase() === 'y') { e.preventDefault(); history(1); return true; }
  if (mod || e.altKey) return false;
  if (k === 'Delete' || k === 'Backspace') { if (E.sel >= 0 || E.multi.size) { e.preventDefault(); removeSel(); return true; } return false; }
  if (k === 'n' || k === 'N') { setTool('draw'); return true; }
  if (k === 'v' || k === 'V') { setTool('select'); return true; }
  if (k === 'r' || k === 'R') { setTool('region'); return true; }
  if (k === 'Escape') {
    if (E.drag) { E.drag = null; E.redraw(); return true; }
    if (E.multi.size) { E.multi.clear(); syncBar(); E.redraw(); return true; }
    if (E.tool !== 'select') { setTool('select'); return true; }
    if (E.sel >= 0) { E.sel = -1; syncBar(); E.redraw(); return true; }
    return false;
  }
  if (/^[1-9]$/.test(k) && S.meta && +k - 1 < S.meta.classes.length) {
    const c = +k - 1;
    E.cls = c;
    const b = selBox();
    if (E.multi.size) { setMultiClass(c); syncBar(); }
    else if (b) change(() => { b.cls = c; });
    else syncBar();
    return true;
  }
  const nb = selBox();
  if (nb && k.startsWith('Arrow')) {        // nudge: 1 px, Shift 10 px
    e.preventDefault();
    const step = (e.shiftKey ? 10 : 1);
    const V = view();
    const dx = (k === 'ArrowLeft' ? -step : k === 'ArrowRight' ? step : 0) / V.W;
    const dy = (k === 'ArrowUp' ? -step : k === 'ArrowDown' ? step : 0) / V.H;
    change(() => {
      nb.cx = Math.min(1 - nb.w / 2, Math.max(nb.w / 2, nb.cx + dx));
      nb.cy = Math.min(1 - nb.h / 2, Math.max(nb.h / 2, nb.cy + dy));
    });
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- save
/** The server's 409 detail ({boxes, sha1}) when the label file changed under the editor. */
function conflictOf(err: unknown): { boxes: ImgBox[]; sha1: string } | null {
  const m = errMessage(err).match(/: 409 (\{.*\})$/s);
  if (!m || m[1] === undefined) return null;
  try {
    const parsed: unknown = JSON.parse(m[1]);
    const d: unknown = typeof parsed === 'object' && parsed !== null && 'detail' in parsed ? parsed.detail : null;
    if (typeof d === 'object' && d !== null && 'boxes' in d && 'sha1' in d && Array.isArray(d.boxes) && typeof d.sha1 === 'string') {
      return { boxes: d.boxes as ImgBox[], sha1: d.sha1 };
    }
    return null;
  } catch (e) { return null; }
}

export async function save(): Promise<void> {
  if (!E.on || E.saving) return;
  const list = ops();
  if (!list.length) { status('Nothing to save'); return; }
  E.saving = true;
  syncBar();
  status('Saving…');
  const V = view();
  try {
    const r = await postJSON<EditResult>('api/edits', { image: V.name, expect_sha1: V.sha1, ops: list });
    E.saving = false;
    E.onSaved(r);                      // the viewer takes the new boxes (V.boxes), then the working copy restarts
    load();
    const nNew = list.filter((o) => o.op === 'add').length;
    status(`Saved ${list.length} change${list.length === 1 ? '' : 's'}${nNew ? ` · ${nNew} new box${nNew === 1 ? '' : 'es'} get a dot after a re-index` : ''}`);
  } catch (err) {
    E.saving = false;
    const c = conflictOf(err);
    if (c) {
      // the file changed elsewhere: take its boxes, keep the boxes drawn here as new ones on top of it
      const added = E.boxes.filter((b) => b.key == null);
      V.boxes = c.boxes;
      V.sha1 = c.sha1;
      load();
      if (added.length) change(() => { E.boxes.push(...added); });
      status(`The label file changed on disk: reloaded.${added.length ? ` Your ${added.length} new box${added.length === 1 ? ' is' : 'es are'} kept (unsaved);` : ''} changes to existing boxes were dropped.`);
      E.redraw();
      return;
    }
    syncBar();
    const m = errMessage(err).match(/\{"detail":"(.*)"\}$/);
    status(`Not saved: ${m ? m[1] : errMessage(err)}`);
  }
}
