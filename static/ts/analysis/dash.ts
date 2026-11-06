// Dashboards: a blank board of widgets. Drag a field from the last result onto a chart, drag a widget to swap places,
// and pick its width. Numbers come from the saved result; a chart says so when its result has gone and needs a new run.

import { errText } from '../core/api.js';
import { $, $req, esc } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { promptDialog } from '../views/dialog.js';
import { currentResult } from './analyze.js';
import { api } from './state.js';
import type { Dashboard, Widget } from './state.js';

const KINDS: [Widget['kind'], string][] = [['bar', 'Bar'], ['line', 'Line'], ['table', 'Table'], ['kpi', 'KPI number'], ['text', 'Text box']];
const D = { boards: [] as Dashboard[], cur: 0, sel: '' };

const board = (): Dashboard | undefined => D.boards[D.cur];
let saveTimer: ReturnType<typeof setTimeout> | undefined;
function save(): void {
  const b = board();
  if (!b) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { void api.saveDashboard(b).catch((e: unknown) => toast(errText(e), 'err')); }, 500);
}

function chartSvg(w: Widget, labels: string[], values: number[]): string {
  const W = 420, H = 190, L = 52, B = 24, T = 10, R = 8, top = Math.max(...values, 0) * 1.1 || 1, low = Math.min(0, ...values);
  const iw = W - L - R, ih = H - T - B, y = (v: number): number => T + ih - ((v - low) / (top - low)) * ih, st = iw / Math.max(labels.length, 1);
  const ticks = [low, (low + top) / 2, top];
  const grid = ticks.map((t) => `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}" stroke="var(--border)"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="var(--text-faint)">${esc(Math.round(t).toLocaleString())}</text>`).join('');
  const every = Math.ceil(labels.length / 8);
  const lab = labels.map((v, i) => (i % every ? '' : `<text x="${L + st * i + st / 2}" y="${H - 7}" text-anchor="middle" font-size="11" fill="var(--text-muted)">${esc(v.slice(0, 12))}</text>`)).join('');
  const marks = w.kind === 'line'
    ? `<polyline points="${values.map((v, i) => `${L + st * i + st / 2},${y(v)}`).join(' ')}" fill="none" stroke="var(--accent)" stroke-width="2.4"/>${values.map((v, i) => `<circle cx="${L + st * i + st / 2}" cy="${y(v)}" r="3.2" fill="var(--accent)"><title>${esc(labels[i])}: ${esc(v.toLocaleString())}</title></circle>`).join('')}`
    : values.map((v, i) => `<rect x="${L + st * i + st * 0.15}" y="${Math.min(y(v), y(0))}" width="${st * 0.7}" height="${Math.abs(y(0) - y(v))}" rx="2" fill="var(--accent)"><title>${esc(labels[i])}: ${esc(v.toLocaleString())}</title></rect>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(w.title)}">${grid}${marks}${lab}</svg>`;
}

async function fill(w: Widget): Promise<void> {
  const el = document.querySelector<HTMLElement>(`.an-w[data-w="${CSS.escape(w.id)}"] .an-wb`);
  if (!el || w.kind === 'text') return;
  const r = currentResult();
  if (!r) { el.innerHTML = '<div class="faint">Run a query to fill this.</div>'; return; }
  if (w.kind === 'kpi') {
    if (!w.y) { el.innerHTML = `<div class="an-kpi">${r.rows.toLocaleString()}</div><div class="faint">rows</div>`; return; }
    try {
      const c = await api.chart(r.id, w.x || r.columns[0] || '', w.y, w.agg, 500);
      const v = w.agg === 'avg' ? c.values.reduce((a, b) => a + b, 0) / (c.values.length || 1) : c.values.reduce((a, b) => a + b, 0);
      el.innerHTML = `<div class="an-kpi">${esc(v.toLocaleString('en-US', { maximumFractionDigits: 2 }))}</div><div class="faint">${esc(w.agg)} of ${esc(w.y)}</div>`;
    } catch (e) { el.innerHTML = `<div class="faint">${esc(errText(e))}</div>`; }
    return;
  }
  if (!w.x) { el.innerHTML = '<div class="faint">Drag a field onto this chart.</div>'; return; }
  if (!r.columns.includes(w.x)) { el.innerHTML = `<div class="faint">${esc(w.x)} is not in the last result. Drag another field here.</div>`; return; }
  try {
    const c = await api.chart(r.id, w.x, w.y && r.columns.includes(w.y) ? w.y : null, w.agg);
    el.innerHTML = w.kind === 'table' ? `<table class="an-doc"><tbody>${c.labels.map((l, i) => `<tr><td>${esc(l)}</td><td class="num">${esc((c.values[i] ?? 0).toLocaleString('en-US', { maximumFractionDigits: 2 }))}</td></tr>`).join('')}</tbody></table>` : chartSvg(w, c.labels, c.values);
  } catch (e) { el.innerHTML = `<div class="faint">${esc(errText(e))}</div>`; }
}

export async function renderDash(reload = true): Promise<void> {
  const w = $req('an-work');
  if (reload) {
    D.boards = (await api.dashboards()).dashboards;
    if (!D.boards.length) D.boards = [{ id: crypto.randomUUID(), name: 'Dashboard', layout: [] }];
  }
  const b = board(), r = currentResult();
  if (!b) return;
  const sel = b.layout.find((x) => x.id === D.sel);
  w.innerHTML = `<div class="an-toolbar"><select class="an-sel" id="an-d-pick" aria-label="Dashboard">${D.boards.map((x, i) => `<option value="${i}" ${i === D.cur ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select><button type="button" class="an-btn sm" data-act="d-rename">Rename</button><button type="button" class="an-btn sm" data-act="d-new">New dashboard</button><span class="grow"></span><button type="button" class="an-btn" data-act="d-auto">Build from this result</button><button type="button" class="an-btn" data-act="d-clear">Clear</button></div>` +
    `<div class="an-dash"><div class="an-dgrid" id="an-dgrid">${b.layout.map((x) => `<article class="an-w${x.id === D.sel ? ' on' : ''}" draggable="true" data-w="${esc(x.id)}" style="grid-column:span ${x.span};grid-row:span ${x.kind === 'kpi' || x.kind === 'text' ? 2 : 4}"><div class="an-wh" data-act="d-sel" data-id="${esc(x.id)}"><span class="lbl">${esc(x.title)}</span><span class="grow"></span><button type="button" class="pb" data-act="d-rm" data-id="${esc(x.id)}" aria-label="Remove ${esc(x.title)}">×</button></div><div class="an-wb">${x.kind === 'text' ? `<textarea data-text="${esc(x.id)}" aria-label="Text box">${esc(x.text ?? '')}</textarea>` : '<div class="faint">Loading…</div>'}</div></article>`).join('') || '<div class="an-empty an-dempty">This space is blank. Add a tool on the right, or build a first draft from your last result.</div>'}</div>` +
    `<aside class="an-dside"><h4>Add to dashboard</h4><div class="an-tools">${KINDS.map(([k, n]) => `<button type="button" class="an-btn sm" data-act="d-add" data-k="${k}">${n}</button>`).join('')}</div><h4>Fields from your query</h4><div class="an-fields">${(r?.columns ?? []).map((c) => `<span class="an-fld" draggable="true" data-f="${esc(c)}">${esc(c)}</span>`).join('') || '<span class="faint">Run a query first.</span>'}</div>` +
    (sel && sel.kind !== 'text' ? `<h4>${esc(sel.title)}</h4><label class="an-well" data-well="x"><span class="faint">X</span><b>${esc(sel.x || 'Drop a field')}</b></label><label class="an-well" data-well="y"><span class="faint">Y</span><b>${esc(sel.y || 'Row count')}</b></label><label>Using<select class="an-sel" id="an-d-agg">${['sum', 'avg', 'min', 'max', 'count'].map((a) => `<option ${a === sel.agg ? 'selected' : ''}>${a}</option>`).join('')}</select></label><label>Width<select class="an-sel" id="an-d-span">${[[4, 'Small'], [6, 'Half'], [12, 'Full']].map(([v, n]) => `<option value="${v}" ${sel.span === v ? 'selected' : ''}>${n}</option>`).join('')}</select></label>` : '') + '</aside></div>';
  await Promise.all(b.layout.map(fill));
}

const mk = (kind: Widget['kind'], title: string, o: Partial<Widget> = {}): Widget => ({ id: crypto.randomUUID(), kind, title, x: '', y: '', agg: 'sum', span: kind === 'kpi' ? 3 : 6, ...o });

export const D_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  'd-add': (el) => {
    const b = board(), k = el.dataset['k'] as Widget['kind'], n = KINDS.find((x) => x[0] === k)?.[1] ?? k;
    if (!b) return undefined;
    const w = mk(k, n, k === 'text' ? { text: '' } : {});
    b.layout.push(w);
    D.sel = w.id;
    save();
    return renderDash(false);
  },
  'd-sel': (el) => { D.sel = el.dataset['id'] ?? ''; return renderDash(false); },
  'd-rm': (el) => { const b = board(); if (b) b.layout = b.layout.filter((x) => x.id !== el.dataset['id']); save(); return renderDash(false); },
  'd-clear': () => { const b = board(); if (b) b.layout = []; save(); return renderDash(false); },
  'd-new': async () => { const n = await promptDialog({ title: 'New dashboard', label: 'Name', value: 'Dashboard', ok: 'Create' }); if (n) { D.boards.push({ id: crypto.randomUUID(), name: n, layout: [] }); D.cur = D.boards.length - 1; save(); await renderDash(false); } },
  'd-rename': async () => { const b = board(); if (!b) return; const n = await promptDialog({ title: 'Rename dashboard', label: 'Name', value: b.name, ok: 'Rename' }); if (n) { b.name = n; save(); await renderDash(false); } },
  'd-auto': async () => {
    const b = board(), r = currentResult();
    if (!b || !r) { toast('Run a query first', 'err'); return; }
    const st = await api.stats(r.id), cols = Object.entries(st.columns);
    const num = cols.filter(([, c]) => c.numeric && c.distinct > 10).map(([k]) => k), cat = cols.filter(([, c]) => !c.numeric && c.distinct >= 2 && c.distinct <= 24).map(([k]) => k);
    const when = cols.filter(([, c]) => /DATE|TIMESTAMP/.test(c.type)).map(([k]) => k);
    b.layout = [mk('kpi', 'Rows'), ...num.slice(0, 2).map((n) => mk('kpi', `Sum of ${n}`, { y: n, x: r.columns[0] ?? '' })),
      ...(when[0] ? [mk('line', `By ${when[0]}`, { x: when[0], y: num[0] ?? '' })] : []),
      ...cat.slice(0, 2).map((c) => mk('bar', `By ${c}`, { x: c, y: num[0] ?? '' }))];
    save();
    await renderDash(false);
  },
};

/** The assistant's draft: added to the open board, or a new one. */
export function addCharts(charts: { kind: string; title: string; x?: string; y?: string; agg?: string }[]): void {
  if (!D.boards.length) D.boards = [{ id: crypto.randomUUID(), name: 'Dashboard', layout: [] }];
  const b = board();
  if (!b) return;
  for (const c of charts) b.layout.push(mk(c.kind as Widget['kind'], c.title, { x: c.x ?? '', y: c.y ?? '', agg: c.agg ?? 'sum' }));
  save();
}

export function dashInput(t: HTMLInputElement): boolean {
  const b = board();
  if (!b) return false;
  if (t.dataset['text']) { const w = b.layout.find((x) => x.id === t.dataset['text']); if (w) w.text = t.value; save(); return true; }
  const w = b.layout.find((x) => x.id === D.sel);
  if (t.id === 'an-d-pick') { D.cur = Number(t.value); D.sel = ''; void renderDash(false); return true; }
  if (t.id === 'an-d-agg' && w) { w.agg = t.value; save(); void renderDash(false); return true; }
  if (t.id === 'an-d-span' && w) { w.span = Number(t.value); save(); void renderDash(false); return true; }
  return false;
}

export function dashDrag(e: DragEvent): void {
  const t = e.target instanceof HTMLElement ? e.target : null;
  if (t?.dataset['f']) e.dataTransfer?.setData('text/an-field', t.dataset['f']);
  if (t?.dataset['w']) e.dataTransfer?.setData('text/an-widget', t.dataset['w']);
}

export function dashDrop(e: DragEvent): boolean {
  const b = board(), el = e.target instanceof Element ? e.target : null;
  if (!b || !el || !$('an-dgrid')) return false;
  const field = e.dataTransfer?.getData('text/an-field'), widget = e.dataTransfer?.getData('text/an-widget');
  const well = el.closest<HTMLElement>('[data-well]'), on = el.closest<HTMLElement>('.an-w');
  if (field && well) { const w = b.layout.find((x) => x.id === D.sel); if (w) w[well.dataset['well'] as 'x' | 'y'] = field; }
  else if (field && on) {
    const w = b.layout.find((x) => x.id === on.dataset['w']);
    if (w) { if (!w.x) w.x = field; else w.y = field; D.sel = w.id; }
  } else if (widget && on && on.dataset['w'] !== widget) {
    const i = b.layout.findIndex((x) => x.id === widget), k = b.layout.findIndex((x) => x.id === on.dataset['w']);
    const [p, q] = [b.layout[i], b.layout[k]];
    if (p && q) { b.layout[i] = q; b.layout[k] = p; }
  } else return false;
  e.preventDefault();
  save();
  void renderDash(false);
  return true;
}
