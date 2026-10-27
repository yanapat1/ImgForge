// The Curation view: scan the dataset for label / image problems (background job), review them in a
// virtualised list (only the rows on screen exist in the DOM), fix them one by one or a whole type at once,
// undo (toast + history), ignore. Fixes come back with a patch of the in-memory points (class changes,
// deleted boxes), applied to S.P so the plot, gallery and filter update without a reload.

import { errMessage, errText, getJSON, postJSON } from '../core/api.js';
import { $req, esc, icon } from '../core/dom.js';
import { setBar, waitJob } from '../core/jobs.js';
import { pts, S } from '../core/state.js';
import { clsColor, clsName } from '../core/theme.js';
import { invalidateHover, renderPlot } from '../plot/render.js';
import type { CurFix, CurIds, CurInfo, CurIssuesPage, CurItem, CurOp, CurPatch, CurResult, PredictResult } from '../types-views.js';
import { labelsChanged, SETTING_HOOKS } from './controls.js';
import { closestOf } from './ev-util.js';
import { onDataPatched, setIssueFilter, setIssueTypes } from './filter-bar.js';
import { clFixIds, renderGallery, setSelection, syncClFix } from './gallery.js';
import { switchTab } from './layout.js';

const ROW = 132;            // px per issue row (fixed: the list is virtualised)
const PAGE = 200;           // issues fetched per request
const OVERSCAN = 4;
const GROUPS: ReadonlyArray<readonly [string, string]> = [['labels', 'Labels'], ['images', 'Images'], ['splits', 'Splits'], ['dataset', 'Dataset'], ['embedding', 'Embedding'], ['model', 'Model']];

interface CurState {
  info: CurInfo | null;     // GET api/curation/summary
  type: string;             // '' = every type
  showIgnored: boolean;
  total: number;
  pages: Map<number, CurItem[] | Promise<CurItem[] | null>>;   // page number -> items (or a pending promise)
  active: number;           // index of the keyboard-focused row
  seq: number;              // bumps on every reload: late page answers of an older list are dropped
  lastUndo: string | null;
  ignored: string[];
  bound: boolean;
}
const C: CurState = {
  info: null,
  type: '',
  showIgnored: false,
  total: 0,
  pages: new Map(),
  active: 0,
  seq: 0,
  lastUndo: null,
  ignored: [],
  bound: false,
};

// ---------------------------------------------------------------- patch of the in-memory points

export function applyPatch(patch: CurPatch | null | undefined): void {
  if (!patch) return;
  const P = pts();
  const del = P.deleted ?? (P.deleted = new Uint8Array(P.cls.length));
  let n = 0;
  for (const [id, c] of Object.entries(patch.cls || {})) { P.cls[+id] = c; n++; }
  for (const id of patch.deleted || []) { del[id] = 1; n++; }
  for (const id of patch.restored || []) { del[id] = 0; n++; }
  // live feed: a box whose geometry changed keeps its dot (features are for the old crop) with a stale note
  if (patch.moved?.length) {
    const mv = P.moved ?? (P.moved = new Uint8Array(P.cls.length));
    for (const m of patch.moved) if (m.id >= 0 && m.id < mv.length) { mv[m.id] = 1; n++; }
  }
  if (!n) return;
  invalidateHover();
  labelsChanged();
  window.dispatchEvent(new CustomEvent('labels-patched'));    // views/compare.ts refreshes its diff
  if (patch.deleted && patch.deleted.length && S.sel.length) {
    S.sel = S.sel.filter((id) => !del[id]);
    S.highlight = S.highlight.filter((id) => !del[id]);
  }
  onDataPatched();            // filter: class bitsets / box counts, then the mask (which also hides deleted boxes)
  syncClFix();
  renderPlot();               // class colours / legend counts changed: one full render
  renderGallery();
}

// ---------------------------------------------------------------- summary + type list

async function loadSummary(): Promise<void> {
  try {
    C.info = await getJSON<CurInfo>('api/curation/summary');
  } catch (e) { $req('cur-sub').textContent = errText(e); return; }
  renderSide();
  setIssueTypes(C.info.ready ? C.info.summary : {});
}

function sevDot(sev: string): string { return `<span class="cur-sev sev-${sev}" title="${sev}"></span>`; }

function renderSide(): void {
  const info = C.info;
  if (!info) return;
  const n = Object.values(info.summary || {}).reduce((a, v) => a + v.n, 0);
  $req('cur-sub').textContent = info.ready ? `${n.toLocaleString()} issues · scanned in ${info.scan_s}s` : 'not scanned yet';
  $req('act-cur-badge').textContent = n > 999 ? '999+' : String(n);
  $req('act-cur-badge').classList.toggle('hidden', !info.ready || !n);
  $req('cur-scan').innerHTML = `${icon(info.ready ? 'refresh' : 'search')}${info.ready ? 'Scan again' : 'Scan dataset'}`;
  $req<HTMLButtonElement>('cur-scan').disabled = !!info.scanning;
  const mid = S.model && S.model.id;
  $req<HTMLInputElement>('cur-use-model').disabled = !mid;
  $req('cur-use-model').closest('label')?.classList.toggle('hidden', !mid);   // a model check without a model: hidden, not just greyed
  $req('cur-model-name').textContent = mid ? `(${S.model.list.find((m) => m.id === mid)?.name || mid})` : '(pick one in the Model view)';
  // banner: label files changed since the index (embeddings, crops and counts are from before)
  const banner = $req('cur-banner');
  if (info.n_fixes) {
    banner.innerHTML = `${icon('alert')}<span><b>${info.n_fixes.toLocaleString()} fix${info.n_fixes === 1 ? '' : 'es'}</b> since the index. ` +
      'Re-index to refresh crops, embeddings and counts.</span>' +
      `<button id="cur-reindex" class="ghost">${icon('refresh')}Re-index</button>`;
    $req('cur-reindex').onclick = () => { void reindex(); };
  }
  banner.classList.toggle('hidden', !info.n_fixes);
  if (!info.writable) {
    banner.classList.remove('hidden');
    banner.innerHTML = `${icon('alert')}<span>This source is read-only: issues can be reviewed, not fixed.</span>`;
  }
  // issue types, grouped
  const sum = info.summary || {};
  const all = `<button type="button" class="cur-type${C.type === '' ? ' active' : ''}" data-type="" aria-pressed="${C.type === ''}">` +
    `<span class="cur-tname">All issues</span><span class="num">${n.toLocaleString()}</span></button>`;
  let html = info.ready ? all : '<p class="hint">Scanning reads every label file and checks boxes, classes, overlaps, ' +
    'images and (when computed) the embedding scores. Nothing is changed until you fix something.</p>';
  for (const [g, title] of GROUPS) {
    const types = Object.entries(sum).filter(([, v]) => v.group === g);
    if (!types.length) continue;
    html += `<div class="cur-group"><div class="cur-gtitle">${title}</div>` + types.map(([t, v]) =>
      `<div class="cur-trow"><button type="button" class="cur-type${C.type === t ? ' active' : ''}" data-type="${t}" aria-pressed="${C.type === t}">` +
      `${sevDot(v.severity)}<span class="cur-tname">${esc(v.label)}</span><span class="num">${v.n.toLocaleString()}</span></button>` +
      `<button type="button" class="icon-btn ghost" data-sel="${t}" aria-label="Select all ${esc(v.label)} in the gallery" title="Select all in the gallery">${icon('image')}</button>` +
      (v.bulk && info.writable ? `<button type="button" class="icon-btn ghost" data-fixall="${t}" aria-label="Fix all ${esc(v.label)}" title="Fix all…">${icon('check')}</button>` : '') +
      '</div>').join('') + '</div>';
  }
  $req('cur-types').innerHTML = html;
  const hist = info.history || [];
  $req('cur-hist-n').textContent = hist.length ? `(${hist.length})` : '';
  $req('cur-hist').innerHTML = hist.length ? hist.map((h) =>
    `<li><div><b>${esc(h.summary || 'Fix')}</b><div class="hint">${new Date(h.time * 1000).toLocaleString()} · ${h.applied} change${h.applied === 1 ? '' : 's'} in ${h.files} file${h.files === 1 ? '' : 's'}</div></div>` +
    `<button type="button" class="ghost" data-undo="${esc(h.undo_id)}">${icon('undo')}Undo</button></li>`).join('')
    : '<li class="hint">No fixes yet.</li>';
  renderHead();
}

function renderHead(): void {
  const info = C.info, sum = (info && info.summary) || {};
  const t = C.type ? sum[C.type] : undefined;
  $req('cur-title').textContent = C.showIgnored ? `Ignored${t ? ` · ${t.label}` : ''}` : t ? t.label : 'All issues';
  $req('cur-count').textContent = C.total ? C.total.toLocaleString() : '';
  const bulk = !!(t && t.bulk && info && info.writable && !C.showIgnored && C.total);
  $req<HTMLButtonElement>('cur-fixall').disabled = !bulk;
  $req('cur-fixall').title = bulk ? `Apply the first fix to all ${C.total} issues of this type` : 'Pick a fixable issue type';
  $req<HTMLButtonElement>('cur-plot').disabled = !info || !info.ready;
}

// ---------------------------------------------------------------- the virtual list

function resetList(): Promise<void> {
  C.seq++;
  C.pages = new Map();
  C.active = 0;
  C.total = 0;
  $req('cur-list').scrollTop = 0;
  return loadPage(0).then(() => renderRows());
}

function loadPage(p: number): Promise<CurItem[] | null> {
  const have = C.pages.get(p);
  if (have) return have instanceof Promise ? have : Promise.resolve(have);
  const seq = C.seq;
  const q = `api/curation/issues?type=${encodeURIComponent(C.type)}&offset=${p * PAGE}&limit=${PAGE}&ignored=${C.showIgnored ? 1 : 0}`;
  const pr = getJSON<CurIssuesPage>(q).then((r): CurItem[] | null => {
    if (seq !== C.seq) return null;
    C.pages.set(p, r.items);
    C.total = r.total;
    $req('cur-spacer').style.height = `${C.total * ROW}px`;
    renderHead();
    return r.items;
  }).catch((e: unknown): null => { C.pages.delete(p); if (!/ 409/.test(errMessage(e))) console.warn(e); return null; });
  C.pages.set(p, pr);
  return pr;
}

function itemAt(i: number): CurItem | null {
  const pg = C.pages.get(Math.floor(i / PAGE));
  return Array.isArray(pg) ? pg[i % PAGE] ?? null : null;
}

let rowsQueued = false;

function scheduleRows(): void {
  if (rowsQueued) return;
  rowsQueued = true;
  requestAnimationFrame(() => { rowsQueued = false; renderRows(); });
}

function renderRows(): void {
  const list = $req('cur-list'), info = C.info;
  const empty = !info || !info.ready || !C.total;
  $req('cur-empty').classList.toggle('hidden', !empty);
  list.classList.toggle('hidden', empty);
  if (empty) {
    $req('cur-empty').innerHTML = !info || !info.ready
      ? `${icon('search')}<h3>Find problems in the labels</h3><p class="muted">Scan the dataset (left) for boxes outside the image, ` +
        'duplicates, unknown classes, malformed lines, empty or broken images and more. Each issue can then be fixed in the label file, with undo.</p>'
      : `${icon('check')}<h3>${C.showIgnored ? 'Nothing ignored' : 'No issues here'}</h3>`;
    return;
  }
  const h = list.clientHeight || 600;
  const first = Math.max(0, Math.floor(list.scrollTop / ROW) - OVERSCAN);
  const last = Math.min(C.total - 1, Math.ceil((list.scrollTop + h) / ROW) + OVERSCAN);
  const need = new Set<number>();
  let html = '';
  for (let i = first; i <= last; i++) {
    const it = itemAt(i);
    if (!it) { need.add(Math.floor(i / PAGE)); html += `<div class="cur-row loading" style="top:${i * ROW}px" aria-hidden="true"></div>`; continue; }
    html += rowHTML(it, i);
  }
  $req('cur-spacer').innerHTML = html;
  $req('cur-spacer').style.height = `${C.total * ROW}px`;
  list.setAttribute('aria-activedescendant', itemAt(C.active) ? `cur-r-${C.active}` : '');
  for (const p of need) void loadPage(p).then((items) => { if (items) scheduleRows(); });
}

function boxesSVG(it: CurItem): string {
  if (!it.boxes || !it.boxes.length) return '';
  return '<svg class="cur-ov" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">' + it.boxes.map(([x1, y1, x2, y2], k) =>
    `<rect class="b${k}" x="${x1}" y="${y1}" width="${Math.max(0.002, x2 - x1)}" height="${Math.max(0.002, y2 - y1)}"/>`).join('') + '</svg>';
}

function fixButtons(it: CurItem, i: number): string {
  const out: string[] = [];
  if (C.info?.writable && !C.showIgnored) {
    it.fixes.forEach((f, k) => {
      if (f.op === 'set_class' && f.cls === undefined) {
        out.push(`<select data-i="${i}" data-setcls="${k}" aria-label="Change the class of this box"><option value="">Change class…</option>` +
          (S.meta?.classes ?? []).map((c, ci) => `<option value="${ci}">${esc(c)}</option>`).join('') + '</select>');
      } else {
        out.push(`<button type="button" class="${k === 0 ? 'primary' : ''}" data-i="${i}" data-fix="${k}">${esc(f.label)}</button>`);
      }
    });
  }
  if (it.box_ids && it.box_ids.length) out.push(`<button type="button" class="ghost" data-i="${i}" data-open="1" title="Show these boxes in the gallery">${icon('image')}Boxes</button>`);
  if (it.cls !== undefined) out.push(`<button type="button" class="ghost" data-i="${i}" data-cls="${it.cls}">${icon('image')}Show class</button>`);
  out.push(`<button type="button" class="ghost" data-i="${i}" data-ign="1" title="${C.showIgnored ? 'List it again' : 'This is fine: hide it (i)'}">${icon(C.showIgnored ? 'eye' : 'eye-off')}${C.showIgnored ? 'Un-ignore' : 'Ignore'}</button>`);
  return out.join('');
}

const typeLabel = (type: string): string => C.info?.types[type]?.label || type;

function rowHTML(it: CurItem, i: number): string {
  const thumb = it.image
    ? `<span class="cur-imgwrap"><img loading="lazy" decoding="async" src="api/curation/image?name=${encodeURIComponent(it.image)}&max_w=240" alt="">${boxesSVG(it)}</span>`
    : it.cls !== undefined ? `<span class="cur-clsdot" style="background:${clsColor(it.cls)}"></span>` : `<span class="cur-noimg">${icon('alert')}</span>`;
  const where = it.image || it.label || (it.cls !== undefined ? clsName(it.cls) ?? '' : '');
  return `<div id="cur-r-${i}" class="cur-row sev-${it.severity}${i === C.active ? ' active' : ''}" role="option" aria-selected="${i === C.active}" data-i="${i}" style="top:${i * ROW}px">` +
    `<div class="cur-thumb">${thumb}</div><div class="cur-body">` +
    `<div class="cur-l1">${sevDot(it.severity)}<b>${esc(typeLabel(it.type))}</b><span class="muted cur-where" title="${esc(where)}">${esc(where)}</span></div>` +
    `<div class="cur-detail">${esc(it.detail)}</div><div class="cur-fixes">${fixButtons(it, i)}</div></div></div>`;
}

function setActive(i: number, scroll = true): void {
  if (!C.total) return;
  C.active = Math.max(0, Math.min(C.total - 1, i));
  const list = $req('cur-list');
  if (scroll) {
    const top = C.active * ROW;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (top + ROW > list.scrollTop + list.clientHeight) list.scrollTop = top + ROW - list.clientHeight;
  }
  renderRows();
}

// ---------------------------------------------------------------- actions

async function scan(): Promise<void> {
  const useModel = $req<HTMLInputElement>('cur-use-model').checked && S.model && S.model.id;
  const body = { force: true, feature: S.scoresFeature, model: useModel ? S.model.id : null,
    conf: S.model && S.model.conf, iou: S.model && S.model.iou };
  $req<HTMLButtonElement>('cur-scan').disabled = true;
  const prog = $req('cur-prog');
  prog.classList.remove('hidden');
  try {
    const r = await postJSON<PredictResult>('api/curation/scan', body);
    if (!r.ready) {
      await waitJob(r.job_id, (j) => {
        const fill = prog.querySelector<HTMLElement>('.bar > div');
        if (fill) setBar(fill, j);
        const label = prog.querySelector('span');
        if (label) label.textContent = j.total ? `${j.stage} · ${j.done.toLocaleString()} / ${j.total.toLocaleString()}` : j.stage;
      });
    }
  } catch (e) { toast(`Scan failed: ${errText(e)}`); }
  prog.classList.add('hidden');
  await loadSummary();
  await resetList();
}

const errList = (errs: Array<{ file: string; error: string }>): string => errs.slice(0, 3).map((e) => `${e.file} (${e.error})`).join(', ') + (errs.length > 3 ? ', …' : '');

function fixItems(it: CurItem, ops: CurOp[]): Array<{ label: string | null; image: string | null; expect_sha1: string | null; ops: CurOp[] }> {
  return [{ label: it.label, image: it.image, expect_sha1: it.sha1, ops }];
}

async function applyFix(it: CurItem, ops: CurOp[], text: string): Promise<void> {
  try {
    const r = await postJSON<CurResult>('api/curation/fix', { items: fixItems(it, ops), summary: text });
    afterFix(r, text);
  } catch (e) { toast(`Fix failed: ${errText(e)}`); }
}

function afterFix(r: CurResult, text: string): void {
  applyPatch(r.patch);
  if (r.undo_id) { C.lastUndo = r.undo_id; toast(`${text} · ${r.applied} change${r.applied === 1 ? '' : 's'}`, r.undo_id); }
  if (r.conflicts && r.conflicts.length) {
    toast(`${r.conflicts.length} file${r.conflicts.length === 1 ? '' : 's'} changed on disk since the scan (${r.conflicts.slice(0, 3).join(', ')}): scan again`);
  }
  // files that failed (bad op, write error): the rest of the batch went through and stays undoable
  if (r.errors && r.errors.length) toast(`${r.errors.length} file${r.errors.length === 1 ? '' : 's'} not fixed: ${errList(r.errors)}`, r.undo_id || undefined);
  const keep = C.active;
  void loadSummary().then(() => resetList()).then(() => setActive(keep, false));
}

// the image editor's save (views/viewer.js): same patch, toast + undo and banner as a fix made here
export function afterEdit(r: CurResult, text: string): void {
  applyPatch(r.patch);
  if (r.undo_id) { C.lastUndo = r.undo_id; toast(text, r.undo_id); }
  if (C.info) void loadSummary();
}

export const showToast = (text: string): void => toast(text);

/** Gallery › "Change N to cleanlab's class": one fix batch (backup + undo) over the shown cleanlab issues. */
export async function fixToSuggested(): Promise<void> {
  const ids = clFixIds();
  if (!ids.length || !S.lq) return;
  if (!confirm(`Change ${ids.length} label${ids.length === 1 ? '' : 's'} to the class cleanlab suggests?\n\n` +
    'The label files are edited (a backup is kept): undo from the message that follows or in Curation › History.')) return;
  try {
    const r = await postJSON<CurResult>('api/curation/fix-boxes', { ids, op: 'suggested', feature: S.lq.feature });
    afterFix(r, `Changed to cleanlab's class`);
    if (C.info) void loadSummary();
  } catch (e) { toast(`Fix failed: ${errText(e)}`); }
}

// called after every undo (the viewer reloads the boxes of the image it shows)
export const UNDO_HOOKS: Array<(r: CurResult) => void> = [];

async function undo(id: string | undefined): Promise<void> {
  if (!id) return;
  try {
    const r = await postJSON<CurResult>('api/curation/undo', { undo_id: id });
    applyPatch(r.patch);
    for (const f of UNDO_HOOKS) f(r);
    if (C.lastUndo === id) C.lastUndo = null;
    const nc = (r.conflicts || []).length, errs = r.errors || [], ne = errs.length;
    toast(nc || ne ? `Undone, except ${nc ? `${nc} file(s) changed since the fix` : ''}${nc && ne ? ' and ' : ''}${ne ? `${ne} file(s) that failed: ${errList(errs)} (undo again to retry)` : ''}`
      : r.stale ? 'Undone: the label files are back as they were. Re-index to see those boxes in the plot' : 'Undone: the label files are back as they were');
  } catch (e) { toast(`Undo failed: ${errText(e)}`); }
  const keep = C.active;
  await loadSummary();
  await resetList();
  setActive(keep, false);
}

async function ignore(it: CurItem): Promise<void> {
  try {
    const r = await postJSON<{ ignored: string[] }>('api/curation/ignore', { ids: [it.id], ignore: !C.showIgnored });
    C.ignored = r.ignored;
  } catch (e) { toast(errText(e)); return; }
  const keep = C.active;
  await loadSummary();
  await resetList();
  setActive(keep, false);
}

let toastTimer: ReturnType<typeof setTimeout> | undefined;

function toast(text: string, undoId: string | null | undefined = null): void {
  const el = $req('cur-toast');
  $req('cur-toast-text').textContent = text;
  $req('cur-toast-undo').classList.toggle('hidden', !undoId);
  $req('cur-toast-undo').dataset.undo = undoId || '';
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), undoId ? 12000 : 7000);
}

async function selectType(t: string): Promise<void> {
  try {
    const r = await getJSON<CurIds>(`api/curation/ids?types=${encodeURIComponent(t)}`);
    const P = pts();
    const ids = new Set(r.boxes);
    if (r.images.length) {
      const imgs = new Set(r.images);
      for (let i = 0; i < P.image.length; i++) if (imgs.has(P.image[i] ?? '')) ids.add(i);
    }
    const del = P.deleted;
    const sel = [...ids].filter((i) => !del || !del[i]);
    const label = C.info?.summary[t]?.label || t;
    switchTab('explore');
    setSelection(sel, `Issue: ${label}`);
    if (!sel.length) toast('No indexed boxes for these issues (e.g. images without boxes)');
  } catch (e) { toast(errText(e)); }
}

async function confirmFixAll(t: string): Promise<void> {
  const v = C.info?.summary[t];
  if (!v) return;
  let preview: CurItem[] = [];
  try { preview = (await getJSON<CurIssuesPage>(`api/curation/issues?type=${encodeURIComponent(t)}&limit=5`)).items; } catch (e) { /* the count is enough */ }
  const first: CurFix | undefined = preview[0]?.fixes[0];
  $req('cur-dlg-title').textContent = `Fix all: ${v.label}`;
  $req('cur-dlg-text').textContent = `${first ? `“${first.label}”` : 'The first fix'} on ${v.n.toLocaleString()} issue${v.n === 1 ? '' : 's'}. ` +
    'Every changed label file is backed up first; one Undo restores them all.';
  $req('cur-dlg-list').innerHTML = preview.map((it) => `<li><b>${esc(it.image || it.label)}</b> <span class="muted">${esc(it.detail)}</span></li>`).join('') +
    (v.n > preview.length ? `<li class="muted">… and ${(v.n - preview.length).toLocaleString()} more</li>` : '');
  $req('cur-dlg-ok').textContent = `Fix ${v.n.toLocaleString()}`;
  const dlg = $req('cur-dlg');
  const prevFocus = document.activeElement;
  dlg.classList.remove('hidden');
  $req('cur-dlg-ok').focus();
  const ok = await new Promise<boolean>((resolve) => {
    const done = (v2: boolean): void => { dlg.classList.add('hidden'); dlg.onkeydown = null; resolve(v2); };
    $req('cur-dlg-ok').onclick = () => done(true);
    $req('cur-dlg-cancel').onclick = () => done(false);
    dlg.onclick = (e) => { if (e.target === dlg) done(false); };
    dlg.onkeydown = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); done(false); }
      if (e.key === 'Tab') {                    // keep focus in the dialog
        const cancel = $req('cur-dlg-cancel'), okBtn = $req('cur-dlg-ok');
        if (document.activeElement === okBtn && !e.shiftKey) { e.preventDefault(); cancel.focus(); } else if (document.activeElement === cancel && e.shiftKey) { e.preventDefault(); okBtn.focus(); }
      }
    };
  });
  if (prevFocus instanceof HTMLElement) prevFocus.focus();
  if (!ok) return;
  try {
    const r = await postJSON<CurResult>('api/curation/fix', { type: t });
    afterFix(r, `Fix all · ${v.label}`);
  } catch (e) { toast(`Fix failed: ${errText(e)}`); }
}

async function reindex(): Promise<void> {
  const ds = S.status && S.status.dataset;
  if (!ds || !ds.id) return;
  try {
    await postJSON(`api/datasets/${encodeURIComponent(ds.id)}/open`, {});
    location.reload();
  } catch (e) { toast(`Re-index failed: ${errText(e)}`); }
}

/** The fix button's op: the fix without its label. */
function opOf(f: CurFix): CurOp {
  const { label: _label, ...op } = f;
  return op;
}

function onListClick(e: Event): void {
  const el = closestOf(e, '[data-i]');
  if (!el) return;
  const i = +(el.dataset.i ?? NaN), it = itemAt(i);
  if (!it) return;
  const P = pts();
  if (el.dataset.fix !== undefined) {
    const f = it.fixes[+el.dataset.fix];
    if (f) void applyFix(it, [opOf(f)], `${typeLabel(it.type)}: ${f.label}`);
  } else if (el.dataset.ign) void ignore(it);
  else if (el.dataset.open) { switchTab('explore'); setSelection(it.box_ids.filter((id) => !P.deleted?.[id]), `${it.detail}`); }
  else if (el.dataset.cls !== undefined) {
    const c = +el.dataset.cls;
    switchTab('explore');
    setSelection(P.id.filter((id) => P.cls[id] === c && !P.deleted?.[id]), `Class: ${clsName(c)}`);
  } else if (el.classList.contains('cur-row')) { C.active = i; renderRows(); }
}

function onListChange(e: Event): void {
  const el = e.target;
  if (!(el instanceof HTMLSelectElement) || el.dataset.setcls === undefined || el.value === '') return;
  const it = itemAt(+(el.dataset.i ?? NaN));
  if (!it) return;
  const f = it.fixes[+el.dataset.setcls];
  if (!f) return;
  const cls = +el.value;
  const op: CurOp = f.line === undefined ? { op: 'set_class', cls } : { op: 'set_class', line: f.line, cls };
  void applyFix(it, [op], `Change class to ${clsName(cls)}`);
}

function onKey(e: KeyboardEvent): void {
  if ($req('tab-curation').classList.contains('hidden') || !$req('cur-dlg').classList.contains('hidden')) return;
  const t = e.target instanceof Element ? e.target : null;
  if (t && (t.closest('input, select, textarea, [contenteditable]') || t.closest('.modal:not(.hidden)'))) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const it = itemAt(C.active);
  const viewer = document.documentElement.dataset['role'] === 'viewer';   // a viewer browses the issues, never fixes them
  if (viewer && (e.key === 'f' || e.key === 'i' || e.key === 'u' || e.key === 'Enter')) return;
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); setActive(C.active + 1); }
  else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); setActive(C.active - 1); }
  else if ((e.key === 'f' || (e.key === 'Enter' && t?.id === 'cur-list')) && it && it.fixes.length && C.info?.writable && !C.showIgnored) {
    const f = it.fixes.find((x) => x.op !== 'set_class' || x.cls !== undefined);
    if (!f) return;
    e.preventDefault();
    void applyFix(it, [opOf(f)], `${typeLabel(it.type)}: ${f.label}`);
  } else if (e.key === 'i' && it) { e.preventDefault(); void ignore(it); }
  else if (e.key === 'u' && C.lastUndo) { e.preventDefault(); void undo(C.lastUndo); }
}

// the view is shown / hidden by the activity bar (views/layout.js switchTab): follow its aria-selected
function syncVisible(): void {
  const on = $req('act-curation').getAttribute('aria-selected') === 'true';
  $req('cur-side').classList.toggle('hidden', !on);
  $req('tab-curation').classList.toggle('hidden', !on);
  if (on) {
    void loadSummary().then(() => { if (C.info && C.info.ready && !C.pages.size) void resetList(); else renderRows(); });
  }
}

export async function initCuration(): Promise<void> {
  SETTING_HOOKS.push({
    collect: (out) => { out.curation_ignored = C.ignored; },
    apply: (st) => { C.ignored = Array.isArray(st.curation_ignored) ? st.curation_ignored.filter((x): x is string => typeof x === 'string') : []; },
  });
  // edits made before a page reload (same server session, index not rebuilt): deleted boxes stay hidden
  try {
    const p = await getJSON<CurPatch>('api/curation/patch');
    if ((p.deleted && p.deleted.length) || Object.keys(p.cls || {}).length) {
      const P = pts();
      const del = P.deleted = new Uint8Array(P.cls.length);
      for (const id of p.deleted ?? []) del[id] = 1;
      for (const [id, c] of Object.entries(p.cls ?? {})) P.cls[+id] = c;
      onDataPatched();
    }
  } catch (e) { /* not ready */ }
  if (C.bound) return;
  C.bound = true;
  new MutationObserver(syncVisible).observe($req('act-curation'), { attributes: true, attributeFilter: ['aria-selected'] });
  $req('cur-scan').onclick = () => { void scan(); };
  $req('cur-types').addEventListener('click', (e) => {
    const b = closestOf<HTMLButtonElement>(e, 'button');
    if (!b) return;
    if (b.dataset.type !== undefined) { C.type = b.dataset.type; renderSide(); void resetList(); $req('cur-list').focus(); }
    else if (b.dataset.sel) void selectType(b.dataset.sel);
    else if (b.dataset.fixall) void confirmFixAll(b.dataset.fixall);
  });
  $req('cur-hist').addEventListener('click', (e) => { const b = closestOf(e, '[data-undo]'); if (b) void undo(b.dataset.undo); });
  const showIgn = $req<HTMLInputElement>('cur-show-ign');
  showIgn.onchange = () => { C.showIgnored = showIgn.checked; renderHead(); void resetList(); };
  $req('cur-fixall').onclick = () => { if (C.type) void confirmFixAll(C.type); };
  $req('cur-plot').onclick = () => {
    const types = C.type ? [C.type] : Object.keys((C.info && C.info.summary) || {});
    switchTab('explore');
    setIssueFilter(types);
  };
  const list = $req('cur-list');
  list.addEventListener('scroll', scheduleRows, { passive: true });
  list.addEventListener('click', onListClick);
  list.addEventListener('change', onListChange);
  new ResizeObserver(scheduleRows).observe(list);
  document.addEventListener('keydown', onKey);
  $req('cur-toast-undo').onclick = () => { $req('cur-toast').classList.add('hidden'); void undo($req('cur-toast-undo').dataset.undo); };
  $req('cur-toast-x').onclick = () => $req('cur-toast').classList.add('hidden');
  void loadSummary();
}
