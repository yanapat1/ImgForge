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

export interface MTable {
  id: string; schema: string; name: string; database: string; alias: string; kind: 'table' | 'sub' | 'cte' | 'remote';
  sql?: string; raw?: string; cols?: string[]; show: boolean; sel: string[]; star: boolean;
  cast: Record<string, { type: string; safe: boolean }>; x: number; y: number;
}
export interface Join { a: [string, string]; b: [string, string]; type: 'INNER' | 'LEFT' | 'RIGHT' | 'FULL' }
export interface Calc { alias: string; t: string; kind: string; expr: string; spec?: unknown }
export interface Parts { where?: string; group?: string; having?: string; order?: string; top?: number; distinct?: boolean; parsed?: boolean }
export interface Model { tables: MTable[]; joins: Join[]; calc: Calc[]; parts: Parts; locked: string[]; adv: string; cte?: { name: string; sql: string }[] }
export interface JoinStats { a_rows: number; b_rows: number; matched_a: number; matched_b: number; lost_a: number; lost_b: number; a_keys: number; b_keys: number; matched_keys: number; joined: number; kept: number; fan_out: boolean }
export interface Parsed extends Model { error?: string; statement?: string; loose?: string[]; output?: string[] }

export interface Profile { rows: number; columns: { name: string; type: string; numeric: boolean; nulls: number; null_pct: number; distinct: number; min: unknown; max: unknown; mean: number | null; note: string; hist: { counts: number[] } }[] }
export interface TestResult { test: string; stat: { name: string; value: number; df?: number }; p: number; effect: { name: string; value: number; label: string }; groups?: { name: string; n: number; mean: number }[]; text: string }
export interface Widget { id: string; kind: 'bar' | 'line' | 'table' | 'kpi' | 'text'; title: string; x: string; y: string; agg: string; span: number; text?: string }
export interface Dashboard { id: string; name: string; layout: Widget[] }

export interface Tab {
  id: string; name: string; sql: string; conn: string | null; db: string | null;
  run: RunState | null; result: number; rt: 'results' | 'joins' | 'cost' | 'messages'; plan: Plan | null; dirty: boolean;
  model: Model | null; typed: boolean; notDrawn: boolean; undo: string[]; redo: string[]; view: { z: number; x: number; y: number };
  joinStep: number; joinStats: Record<string, JoinStats>;
}
export interface Layout { flip: boolean; h: number; w: number; max: '' | 'sql' | 'viz' | 'res'; swap: boolean }
export type Workspace = 'query' | 'catalog' | 'scripts' | 'analyze' | 'dash' | 'history';

export const A = {
  sid: '', name: '', ws: 'query' as Workspace,
  sources: [] as Source[], tabs: [] as Tab[], active: 0, temps: [] as TempTable[],
  open: new Set<string>(), dbs: new Map<string, string[]>(), tables: new Map<string, TableRef[]>(), info: new Map<string, TableInfo>(),
  version: new Map<string, string>(), filter: '', explorer: true,
  catalog: null as { conn: string; db: string; schema: string; table: string } | null,
  history: [] as HistoryRow[], scripts: [] as Script[], script: 0,
  aiRows: false,
  layout: { flip: false, h: 46, w: 42, max: '', swap: false } as Layout,
};

const P = 'api/analysis';
const qs = (o: Record<string, string | number | null | undefined>): string =>
  Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&');

export const tab = (): Tab => A.tabs[A.active] as Tab;
export const sourceOf = (id: string | null): Source | undefined => A.sources.find((s) => s.id === id);
export const tableKey = (conn: string, db: string, schema: string, table: string): string => `${conn}|${db}|${schema}|${table}`;
export const newTab = (name: string, sql = '', conn: string | null = null, db: string | null = null): Tab =>
  ({ id: crypto.randomUUID(), name, sql, conn, db, run: null, result: 0, rt: 'results', plan: null, dirty: false, model: null, typed: !!sql, notDrawn: false, undo: [], redo: [], view: { z: 1, x: 0, y: 0 }, joinStep: 0, joinStats: {} });

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
  joinStats: (connection: string, database: string | null, model: Model, index: number) => postJSON<JoinStats>(`${P}/joins`, { connection, database, model, index }),
  parseModel: (connection: string, sql: string) => postJSON<Parsed>(`${P}/model/parse`, { connection, sql }),
  generate: async (model: Model, dialect: string) => (await postJSON<{ sql: string }>(`${P}/model/generate`, { model, dialect })).sql,
  explain: (conn: string, db: string | null, sql: string) => postJSON<Plan>(`${P}/explain`, { session: A.sid, connection: conn, database: db, sql }),
  page: (rid: string, offset: number, limit: number, sort: string | null, desc: boolean, q: string) =>
    getJSON<Page>(`${P}/results/${rid}/page?${qs({ offset, limit, sort, desc: desc ? 1 : 0, q })}`),
  stats: (rid: string) => getJSON<Stats>(`${P}/results/${rid}/stats`),
  histogram: (rid: string, column: string, bins = 12) => getJSON<{ kind: string; counts: number[]; labels?: string[]; lo?: number; hi?: number }>(`${P}/results/${rid}/histogram?${qs({ column, bins })}`),
  exportUrl: (rid: string) => `${P}/results/${rid}/export`,
  profile: (rid: string) => getJSON<Profile>(`${P}/results/${rid}/profile`),
  test: (rid: string, kind: string, a: string, b: string) => postJSON<TestResult>(`${P}/results/${rid}/test`, { kind, a, b }),
  suggest: async (rid: string) => (await getJSON<{ tests: { kind: string; a: string; b: string | null; label: string }[] }>(`${P}/results/${rid}/suggest`)).tests,
  quality: (rid: string) => getJSON<{ rows: number; checks: { rule: string; ok: boolean; rows: number }[] }>(`${P}/results/${rid}/quality`),
  pivot: (rid: string, row: string, col: string, value: string, agg: string) => getJSON<{ columns: string[]; rows: { name: string; cells: (number | string | null)[] }[] }>(`${P}/results/${rid}/pivot?${qs({ row, col, value, agg })}`),
  chart: (rid: string, x: string, y: string | null, agg: string, limit = 30) => getJSON<{ labels: string[]; values: number[] }>(`${P}/results/${rid}/chart?${qs({ x, y, agg, limit })}`),
  dashboards: () => getJSON<{ dashboards: Dashboard[] }>(`${P}/sessions/${A.sid}/dashboards`),
  saveDashboard: (d: Dashboard) => sendJSON('PUT', `${P}/sessions/${A.sid}/dashboards`, d),
  history: () => getJSON<{ history: HistoryRow[] }>(`${P}/sessions/${A.sid}/history`),
  scripts: () => getJSON<{ scripts: Script[] }>(`${P}/sessions/${A.sid}/scripts`),
  saveScript: (s: Partial<Script>) => sendJSON<{ id: string }>('PUT', `${P}/sessions/${A.sid}/scripts`, s),
  dropScript: (id: string) => sendJSON('DELETE', `${P}/sessions/${A.sid}/scripts/${id}`),
};
