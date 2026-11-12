// What the ◆ assistant may change in each table (dataviz/data/aiaccess.py): Read only (the default), Edit rows,
// Change structure. Only the user sets it, in a table's right-click menu (› AI access); the tree marks raised tables.
// Kept per user on the server (GET / PUT api/data/ai-access); the server checks every change against it.

import { errText, getJSON, sendJSON } from '../core/api.js';
import { esc } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { confirmDialog } from '../views/dialog.js';
import { menuHead, menuItem } from './menu.js';

export type AiLevel = 'read' | 'rows' | 'schema';
interface Row { c: string; d: string; s: string; t: string; level: AiLevel }
interface Ref { c: string; d: string; s: string; t: string }

const LEVELS: Array<{ id: AiLevel; label: string; hint: string; ic: string }> = [
  { id: 'read', label: 'Read only', hint: 'SELECT, CAST, temp tables', ic: 'eye' },
  { id: 'rows', label: 'Edit rows', hint: 'INSERT, UPDATE, DELETE', ic: 'edit' },
  { id: 'schema', label: 'Change structure', hint: 'ALTER columns and types, TRUNCATE, DROP', ic: 'alert' },
];
const levels = new Map<string, AiLevel>();
const k = (f: Ref): string => [f.c, f.d.toLowerCase(), f.s.toLowerCase(), f.t.toLowerCase()].join('\u001f');

export const aiLevel = (f: Ref): AiLevel => levels.get(k(f)) ?? 'read';

/** Read the user's levels once per page; a failure leaves every table at the default (Read only). */
export async function loadAiAccess(): Promise<void> {
  try {
    const r = await getJSON<{ items: Row[] }>('api/data/ai-access');
    levels.clear();
    for (const x of r.items) levels.set(k(x), x.level);
  } catch { /* the assistant still only reads: the server holds the real levels */ }
}

/** The small mark after a table's name in the tree (nothing for Read only). */
export function aiBadge(f: Ref): string {
  const lv = aiLevel(f);
  if (lv === 'read') return '';
  const l = LEVELS.find((x) => x.id === lv);
  return `<span class="dt-aiacc ${lv}" title="◆ AI access: ${esc(l?.label ?? '')}">${lv === 'rows' ? '✎' : '⚙'}</span>`;
}

/** The "AI access" part of a table's right-click menu (picks come back as m = 'ai-access', g = the level). */
export function aiMenu(f: Ref): string {
  const cur = aiLevel(f);
  return menuHead('◆ AI access') + LEVELS.map((l) =>
    menuItem('ai-access', `${esc(l.label)} <span class="faint">${esc(l.hint)}</span>`, cur === l.id ? 'check' : l.ic, l.id, cur === l.id ? 'on' : '')).join('');
}

/** Set one table's level (Change structure asks first); `done` redraws the tree. */
export async function setAiLevel(f: Ref, level: string, done: () => void): Promise<void> {
  if (level !== 'read' && level !== 'rows' && level !== 'schema') return;
  if (level === aiLevel(f)) return;
  const name = f.s ? `${f.s}.${f.t}` : f.t;
  if (level === 'schema' && !await confirmDialog({
    title: `Let the assistant change the structure of ${name}?`,
    text: 'It may then propose ALTER TABLE (add, drop or rename columns, change a column type), TRUNCATE and DROP TABLE on this table. Each change still waits for your Approve.',
    ok: 'Allow structure changes', danger: true,
  })) return;
  try {
    await sendJSON('PUT', 'api/data/ai-access', { connection: f.c, database: f.d, schema: f.s, table: f.t, level });
    if (level === 'read') levels.delete(k(f)); else levels.set(k(f), level);
    done();
    toast(`◆ AI access of ${name}: ${LEVELS.find((x) => x.id === level)?.label ?? level}`);
  } catch (e) { toast(`AI access was not changed: ${errText(e)}`, 'err'); }
}
