// Catalog: one table's columns, keys and descriptions. A description the user writes stays in this app, so the source
// database is not changed; the assistant reads these, so better descriptions give better answers.

import { errText } from '../core/api.js';
import { $, $req, esc } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { A, api, tableKey } from './state.js';

export async function openCatalog(conn: string, db: string, schema: string, table: string): Promise<void> {
  A.catalog = { conn, db, schema, table };
  const w = $req('an-work');
  w.innerHTML = '<div class="an-empty">Loading…</div>';
  try {
    const info = await api.describe(conn, db, schema, table);
    A.info.set(tableKey(conn, db, schema, table), info);
  } catch (e) { w.innerHTML = `<div class="an-empty">${esc(errText(e))}</div>`; return; }
  renderCatalog();
}

export function renderCatalog(): void {
  const w = $req('an-work'), c = A.catalog;
  if (!c) { w.innerHTML = '<div class="an-empty">Click a table in the object explorer to see its columns and descriptions.</div>'; return; }
  const info = A.info.get(tableKey(c.conn, c.db, c.schema, c.table));
  if (!info) { void openCatalog(c.conn, c.db, c.schema, c.table); return; }
  const missing = info.columns.filter((x) => !x.description).length;
  const from: Record<string, string> = { database: 'Database', user: 'You', ai: 'AI, check it' };
  w.innerHTML = `<div class="an-scroll"><h2 class="an-h2">${esc(c.schema ? `${c.schema}.${c.table}` : c.table)}</h2><div class="faint">${esc(c.db)} · about ${info.rows.toLocaleString()} rows · ${info.columns.length} columns${info.sampled ? ` · fields sampled from ${info.sampled} documents` : ''} · ${missing ? `${missing} without a description` : 'all described'}</div>` +
    `<div class="an-toolbar an-plain"><button type="button" class="an-btn sm" data-act="cat-query">Open in Query</button></div>` +
    `<label class="an-tdesc">Table description<input id="an-tdoc" data-col="" value="${esc(info.description)}" placeholder="What is this table for?"></label>` +
    `<table class="an-doc"><thead><tr><th>Column</th><th>Type</th><th>Key</th><th style="width:100%">Description, also read by the AI</th><th>From</th></tr></thead><tbody>${info.columns.map((x) => `<tr><td class="an-mono">${esc(x.name)}</td><td class="faint an-mono">${esc(x.type)}</td><td class="an-key">${esc(x.key)}</td><td><input class="an-desc${x.description ? '' : ' miss'}" data-col="${esc(x.name)}" aria-label="Description of ${esc(x.name)}" value="${esc(x.description)}" placeholder="Add a description"></td><td class="faint">${esc(from[x.from ?? ''] ?? '')}</td></tr>`).join('')}</tbody></table>` +
    (info.keys.length ? `<h3 class="an-h3">Relationships</h3><table class="an-doc"><tbody>${info.keys.map((k) => `<tr><td class="an-mono">${esc(k.column)}</td><td>→</td><td class="an-mono">${esc(k.schema ? `${k.schema}.` : '')}${esc(k.table)}.${esc(k.to)}</td></tr>`).join('')}</tbody></table>` : '') + '</div>';
}

export function catalogChange(t: HTMLInputElement): boolean {
  if (!t.dataset || t.dataset['col'] === undefined || !$('an-work')?.contains(t)) return false;
  const c = A.catalog;
  if (!c) return false;
  const column = t.dataset['col'] ?? '', description = t.value;
  void api.saveDoc({ connection: c.conn, database: c.db, schema: c.schema, table: c.table, column, description }).then(() => {
    const info = A.info.get(tableKey(c.conn, c.db, c.schema, c.table));
    if (info) {
      if (!column) info.description = description;
      const col = info.columns.find((x) => x.name === column);
      if (col) { col.description = description; col.from = description ? 'user' : ''; }
    }
    renderCatalog();
    toast('Description saved. The AI will use it.');
  }).catch((e: unknown) => toast(errText(e), 'err'));
  return true;
}
