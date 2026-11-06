// The Data Analysis page's state and its calls to /api/analysis/*. One query tab = one SQL text with its own connection,
// database and last run; tabs are saved with the session, results stay on the server until the session closes.

import { getJSON, postJSON, sendJSON } from '../core/api.js';

export interface Source { id: string; name: string; kind: 'postgres' | 'mssql' | 'mongo'; host: string; database: string }
export interface TableRef { schema: string; name: string; kind: string }
export interface Column { name: string; type: string; nullable: boolean; key: string; description: string; from?: string; present?: number }
export interface TableInfo {
  columns: Column[]; description: string; rows: number; sampled?: number;
  keys: { column: string; schema: string; table: string; to: string }[];
}
export interface Statement { sql: string; kind: 'read' | 'temp' | 'write'; op: string; tables: string[]; reason: string }
export interface ResultInfo { id: string; rows: number; columns: string[]; truncated: boolean; ms: number; statement: string; stopped?: boolean }
export interface RunState { id: string; status: 'running' | 'done' | 'error' | 'stopped'; ms: number; statement: number; total: number; results: ResultInfo[]; error: string | null }
export interface Page { columns: string[]; rows: unknown[][]; total: number; offset: number }
export interface ColStats { type: string; nulls: number; null_pct: number; distinct: number; min: unknown; max: unknown; mean: number | null; numeric: boolean }
export interface Stats { columns: Record<string, ColStats>; duplicates: number }
export interface HistoryRow { id: number; connection: string; database: string; name: string; sql: string; kind: string; status: string; rows: number | null; ms: number | null; error: string | null; at: string }
export interface Script { id: string; name: string; position: number; connection: string | null; database: string | null; body: string }
export interface TempTable { name: string; connection: string; database: string; source: string; source_name: string }
export interface PlanNode { label: string; kind: string; cost: number; rows: number | null; pct: number }
export interface Plan { nodes: PlanNode[]; total: number; estimated: boolean }

export interface Tab {
  id: string; name: string; sql: string; conn: string | null; db: string | null;
  run: RunState | null; result: number; rt: 'results' | 'joins' | 'cost' | 'messages'; plan: Plan | null; dirty: boolean;
}
export type Workspace = 'query' | 'catalog' | 'scripts' | 'analyze' | 'dash' | 'history';

export const A = {
  sid: '', name: '', ws: 'query' as Workspace,
  sources: [] as Source[], tabs: [] as Tab[], active: 0, temps: [] as TempTable[],
  open: new Set<string>(), dbs: new Map<string, string[]>(), tables: new Map<string, TableRef[]>(), info: new Map<string, TableInfo>(),
  version: new Map<string, string>(), filter: '', explorer: true,
  catalog: null as { conn: string; db: string; schema: string; table: string } | null,
  history: [] as HistoryRow[], scripts: [] as Script[], script: 0,
};

const P = 'api/analysis';
const qs = (o: Record<string, string | number | null | undefined>): string =>
  Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&');

export const tab = (): Tab => A.tabs[A.active] as Tab;
export const sourceOf = (id: string | null): Source | undefined => A.sources.find((s) => s.id === id);
export const tableKey = (conn: string, db: string, schema: string, table: string): string => `${conn}|${db}|${schema}|${table}`;
export const newTab = (name: string, sql = '', conn: string | null = null, db: string | null = null): Tab =>
  ({ id: crypto.randomUUID(), name, sql, conn, db, run: null, result: 0, rt: 'results', plan: null, dirty: false });

export const api = {
  sources: () => getJSON<{ sources: Source[] }>(`${P}/sources`),
  sessions: () => getJSON<{ sessions: { id: string; name: string; state: { tabs?: Partial<Tab>[]; active?: number } }[] }>(`${P}/sessions`),
  newSession: (name: string) => postJSON<{ id: string; name: string; state: Record<string, unknown> }>(`${P}/sessions`, { name }),
  saveSession: (state: unknown) => sendJSON('PATCH', `${P}/sessions/${A.sid}`, { state }),
  heartbeat: () => postJSON<{ temps: TempTable[] }>(`${P}/sessions/${A.sid}/heartbeat`, {}),
  closeSession: () => postJSON(`${P}/sessions/${A.sid}/close`, {}),
  databases: (conn: string, refresh = false) => getJSON<{ databases: string[]; version: string }>(`${P}/tree?${qs({ connection: conn, refresh: refresh ? 1 : 0 })}`),
  tables: (conn: string, db: string, refresh = false) => getJSON<{ tables: TableRef[] }>(`${P}/tree?${qs({ connection: conn, database: db, refresh: refresh ? 1 : 0 })}`),
  describe: (conn: string, db: string, schema: string, table: string) => getJSON<TableInfo>(`${P}/describe?${qs({ connection: conn, database: db, schema, table })}`),
  saveDoc: (b: { connection: string; database: string; schema: string; table: string; column: string; description: string }) => sendJSON('PUT', `${P}/docs`, b),
  check: (conn: string, db: string | null, sql: string) => postJSON<{ statements: Statement[]; confirm: string | null }>(`${P}/check`, { session: A.sid, connection: conn, database: db, sql }),
  run: (conn: string, db: string | null, sql: string, confirm: string | null, name: string) => postJSON<RunState>(`${P}/run`, { session: A.sid, connection: conn, database: db, sql, confirm, name }),
  runState: (id: string) => getJSON<RunState>(`${P}/run/${id}`),
  stop: (id: string) => postJSON<RunState>(`${P}/run/${id}/stop`, {}),
  explain: (conn: string, db: string | null, sql: string) => postJSON<Plan>(`${P}/explain`, { session: A.sid, connection: conn, database: db, sql }),
  page: (rid: string, offset: number, limit: number, sort: string | null, desc: boolean, q: string) =>
    getJSON<Page>(`${P}/results/${rid}/page?${qs({ offset, limit, sort, desc: desc ? 1 : 0, q })}`),
  stats: (rid: string) => getJSON<Stats>(`${P}/results/${rid}/stats`),
  histogram: (rid: string, column: string, bins = 12) => getJSON<{ kind: string; counts: number[]; labels?: string[]; lo?: number; hi?: number }>(`${P}/results/${rid}/histogram?${qs({ column, bins })}`),
  exportUrl: (rid: string) => `${P}/results/${rid}/export`,
  history: () => getJSON<{ history: HistoryRow[] }>(`${P}/sessions/${A.sid}/history`),
  scripts: () => getJSON<{ scripts: Script[] }>(`${P}/sessions/${A.sid}/scripts`),
  saveScript: (s: Partial<Script>) => sendJSON<{ id: string }>('PUT', `${P}/sessions/${A.sid}/scripts`, s),
  dropScript: (id: string) => sendJSON('DELETE', `${P}/sessions/${A.sid}/scripts/${id}`),
};
