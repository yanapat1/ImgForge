// Catalog: one table's columns, keys and descriptions. A description the user writes stays in this app, so the source
// database is not changed; the assistant reads these, so better descriptions give better answers.

import { errText } from '../core/api.js';
import { $, $req, esc } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { A, api, tableKey } from './state.js';

export async function openCatalog(conn: string, db: string, schema: string, table: string): Promise<void> {
  A.catalog = { conn, db, schema, table };
  const w = $req('an-work');
  w.innerHTML = '<div class="an-empty empty">Loading…</div>';
  try {
    const info = await api.describe(conn, db, schema, table);
    A.info.set(tableKey(conn, db, schema, table), info);
  } catch (e) { w.innerHTML = `<div class="an-empty empty">${esc(errText(e))}</div>`; return; }
  renderCatalog();
}

export function renderCatalog(): void {
  const w = $req('an-work'), c = A.catalog;
  if (!c) { w.innerHTML = '<div class="an-empty empty">Click a table in the object explorer to see its columns and descriptions.</div>'; return; }
  const info = A.info.get(tableKey(c.conn, c.db, c.schema, c.table));
  if (!info) { void openCatalog(c.conn, c.db, c.schema, c.table); return; }
  const missing = info.columns.filter((x) => !x.description).length;
  const from: Record<string, string> = { database: 'Database', user: 'You', ai: 'AI, check it' };
  w.innerHTML = `<div class="an-scroll"><h2 class="sec-title">${esc(c.schema ? `${c.schema}.${c.table}` : c.table)}</h2><div class="faint">${esc(c.db)} · about ${info.rows.toLocaleString()} rows · ${info.columns.length} columns${info.sampled ? ` · fields sampled from ${info.sampled} documents` : ''} · <span id="an-catmiss">${missLabel(missing)}</span></div>` +
    `<div class="an-toolbar toolbar an-plain"><button type="button" class="sm" data-act="cat-query">Open in Query</button><span class="faint" title="Tab or Enter: next description · Shift+Enter or ↑: previous · Ctrl+Enter: next empty one · Esc: undo the edit">Tab / Enter moves to the next description</span><span class="grow"></span><span id="an-catstat" class="faint" aria-live="polite"></span></div>` +
    `<label class="an-tdesc">Table description<input id="an-tdoc" data-col="" data-saved="${esc(info.description)}" value="${esc(info.description)}" placeholder="What is this table for?"></label>` +
    `<table class="an-doc list-table"><thead><tr><th>Column</th><th>Type</th><th>Key</th><th style="width:100%">Description, also read by the AI</th><th>From</th></tr></thead><tbody>${info.columns.map((x) => `<tr><td class="an-mono">${esc(x.name)}</td><td class="faint an-mono">${esc(x.type)}</td><td class="an-key">${esc(x.key)}</td><td><input class="an-desc${x.description ? '' : ' miss'}" data-col="${esc(x.name)}" data-saved="${esc(x.description)}" aria-label="Description of ${esc(x.name)}" value="${esc(x.description)}" placeholder="Add a description"></td><td class="faint">${esc(from[x.from ?? ''] ?? '')}</td></tr>`).join('')}</tbody></table>` +
    (info.keys.length ? `<h3 class="sec-label">Relationships</h3><table class="an-doc list-table"><tbody>${info.keys.map((k) => { const one = info.unique?.includes(k.column); return `<tr><td class="an-mono">${esc(k.column)}</td><td>→</td><td class="an-mono">${esc(k.schema ? `${k.schema}.` : '')}${esc(k.table)}.${esc(k.to)}</td><td class="faint" title="${one ? 'Each row of this table matches at most one row there, and the other way round' : 'Many rows of this table can point to the same row there'}">${info.unique ? (one ? '1 : 1' : 'many : 1') : ''}</td></tr>`; }).join('')}</tbody></table>` : '') + '</div>';
}

const missLabel = (n: number): string => n ? `${n} without a description` : 'all described';

export function catalogChange(t: HTMLInputElement): boolean {
  if (!t.dataset || t.dataset['col'] === undefined || !$('an-work')?.contains(t)) return false;
  const c = A.catalog;
  if (!c) return false;
  const column = t.dataset['col'] ?? '', description = t.value;
  if (description === (t.dataset['saved'] ?? '')) return true;          // nothing changed: no save
  const stat = $('an-catstat');
  if (stat) stat.textContent = 'Saving…';
  // the page is updated in place (no re-render), so the focus Tab / Enter moved to the next description stays there
  void api.saveDoc({ connection: c.conn, database: c.db, schema: c.schema, table: c.table, column, description }).then(() => {
    t.dataset['saved'] = description;
    const info = A.info.get(tableKey(c.conn, c.db, c.schema, c.table));
    if (info) {
      if (!column) info.description = description;
      const col = info.columns.find((x) => x.name === column);
      if (col) { col.description = description; col.from = description ? 'user' : ''; }
      const m = $('an-catmiss');
      if (m) m.textContent = missLabel(info.columns.filter((x) => !x.description).length);
    }
    if (column) {
      t.classList.toggle('miss', !description);
      const from = t.closest('tr')?.lastElementChild;
      if (from) from.textContent = description ? 'You' : '';
    }
    if (stat) stat.textContent = `Saved${column ? ` · ${column}` : ''}. The AI will use it.`;
  }).catch((e: unknown) => { if (stat) stat.textContent = ''; toast(errText(e), 'err'); });
  return true;
}

/** Keys in the Catalog's description boxes: Enter / ↓ next, Shift+Enter / ↑ previous, Ctrl+Enter next empty, Esc undo. */
export function catalogKey(e: KeyboardEvent): boolean {
  const t = e.target;
  const w = $('an-work');
  if (!(t instanceof HTMLInputElement) || !w?.contains(t) || !(t.id === 'an-tdoc' || t.classList.contains('an-desc'))) return false;
  const list = [...w.querySelectorAll<HTMLInputElement>('#an-tdoc, input.an-desc')];
  const i = list.indexOf(t);
  const go = (j: number): void => { const n = list[j]; if (n) { n.focus(); n.select(); } };
  if (e.key === 'Escape') { t.value = t.dataset['saved'] ?? ''; t.blur(); }
  else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    const next = list.slice(i + 1).find((x) => !x.value) ?? list.slice(0, i).find((x) => !x.value);
    if (next) go(list.indexOf(next)); else t.blur();
  } else if (e.key === 'Enter' || e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    const up = e.key === 'ArrowUp' || e.key === 'Enter' && e.shiftKey;
    const j = i + (up ? -1 : 1);
    if (j >= 0 && j < list.length) go(j); else t.blur();          // past the last one: just save it
  } else return false;
  e.preventDefault();
  e.stopPropagation();
  return true;
}
