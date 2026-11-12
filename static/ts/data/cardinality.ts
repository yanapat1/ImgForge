// How the rows of a join's two sides relate: 1:1, 1:N, N:1 or N:M. A side is "1" when its join column is unique on its
// own (a primary key, a UNIQUE constraint or index: TableInfo.unique), "N" otherwise. A count over the real data (Check
// with data, the Joins tab's counts) or the user's own choice wins over the keys. The builder draws the answer as
// crow's-foot ends on each join line; N:M lines are marked, since their rows multiply.

import { A, tableKey } from './state.js';
import type { Card, Join, JoinStats, MTable, Tab, TableInfo } from './state.js';

export interface CardInfo { card: Card; from: 'keys' | 'data' | 'you'; optA: boolean; optB: boolean }

/** How the ends are drawn (this browser): crow's feet + the label, the label only, or nothing. */
export type Ends = 'crow' | 'text' | 'off';
const ENDS_KEY = 'dataviz.data.ends';
export function endsMode(): Ends {
  try { const v = localStorage.getItem(ENDS_KEY); return v === 'text' || v === 'off' ? v : 'crow'; } catch { return 'crow'; }
}
export function setEndsMode(v: Ends): void { try { localStorage.setItem(ENDS_KEY, v); } catch { /* a convenience only */ } }
export const ENDS_NEXT: Record<Ends, Ends> = { crow: 'text', text: 'off', off: 'crow' };
export const ENDS_LABEL: Record<Ends, string> = { crow: "Ends: crow's foot", text: 'Ends: text only', off: 'Ends: off' };

const card = (a1: boolean, b1: boolean): Card => (a1 ? (b1 ? '1:1' : '1:N') : (b1 ? 'N:1' : 'N:M'));
export const statKey = (j: Join): string => `${j.a.join('.')}|${j.b.join('.')}`;

function infoOf(t: Tab, m: MTable | undefined): TableInfo | undefined {
  if (!m || !t.conn || m.kind !== 'table') return undefined;       // a subquery / CTE / remote part: no keys to read
  return A.info.get(tableKey(t.conn, m.database || t.db || '', m.schema, m.name));
}

/** The relation of join j, or null when it is not known (a side without table info, e.g. a subquery, not counted). */
export function cardOf(t: Tab, tables: MTable[], j: Join): CardInfo | null {
  const ta = tables.find((x) => x.id === j.a[0]), tb = tables.find((x) => x.id === j.b[0]);
  const ia = infoOf(t, ta), ib = infoOf(t, tb);
  const nullable = (i: TableInfo | undefined, c: string): boolean => !!i?.columns.find((x) => x.name === c)?.nullable;
  // the "zero" circle at A's end: a B row whose column is NULL meets no A row (and the other way round)
  const optA = nullable(ib, j.b[1]), optB = nullable(ia, j.a[1]);
  if (j.card) return { card: j.card, from: j.cardFrom === 'data' ? 'data' : 'you', optA, optB };
  const s: JoinStats | undefined = t.joinStats[statKey(j)];
  if (s && s.a_unique !== undefined && s.b_unique !== undefined) return { card: card(s.a_unique, s.b_unique), from: 'data', optA, optB };
  if (!ia?.unique || !ib?.unique) return null;
  return { card: card(ia.unique.includes(j.a[1]), ib.unique.includes(j.b[1])), from: 'keys', optA, optB };
}

/** Each side's mark: "1" or "N", and "0" in front when its column may be NULL (a row that matches nothing). */
export function sideMarks(c: CardInfo): [string, string] {
  const [a, b] = c.card.split(':') as [string, string];
  // the mark at A's end says how many A rows one B row meets, so it is the A part of the relation
  return [`${c.optA ? '0' : ''}${a === '1' ? '1' : 'N'}`, `${c.optB ? '0' : ''}${b === '1' ? '1' : 'N'}`];
}

export function cardTitle(c: CardInfo, a: string, b: string): string {
  const [x, y] = c.card.split(':');
  const many = (n: string | undefined, name: string): string => (n === '1' ? `one ${name} row` : `many ${name} rows`);
  const from = { keys: 'from the keys', data: 'counted in the data', you: 'set by you' }[c.from];
  const base = `${many(x, a)} ↔ ${many(y, b)} (${c.card}, ${from})`;
  return c.card === 'N:M' ? `${base}. Many-to-many: rows multiply. Join through a bridge table or aggregate first.` : base;
}

/** The SVG markers for the ends: one (bar), many (crow's foot), each with an optional "zero" circle; a warn set for N:M. */
export function markerDefs(): string {
  const shapes: Record<string, string> = {
    '1': 'M-7 -6V6',
    'N': 'M-13 0L0 -6M-13 0L0 0M-13 0L0 6',
    '01': 'M-7 -6V6M-15 0m-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0',
    '0N': 'M-13 0L0 -6M-13 0L0 0M-13 0L0 6M-17 0m-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0',
  };
  return `<defs>${Object.entries(shapes).flatMap(([k, d]) => ['', 'w'].map((w) =>
    `<marker id="dt-m${k}${w}" viewBox="-24 -8 26 16" refX="0" refY="0" markerWidth="26" markerHeight="16" markerUnits="userSpaceOnUse" orient="auto-start-reverse">` +
    `<path d="${d}" style="fill:var(--surface);stroke:var(${w ? '--warn' : '--accent'})" stroke-width="1.6"/></marker>`)).join('')}</defs>`;
}
