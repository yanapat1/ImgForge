// Shapes of the shared ◆ chat (core/chat.ts): the agent events (dataviz/llm/engine/events.py), the catalogue
// (GET api/llm/agent/<role>/catalog), the chat sessions (api/llm/sessions*) and the transcript the reducer builds.

export type ChatRole = 'data' | 'visual';
export type ChatMode = 'ask' | 'auto' | 'plan';
export type ChatEffort = 'low' | 'medium' | 'high';
export type ThinkLevel = 'off' | 'low' | 'medium' | 'high';
export type WebLevel = 'off' | 'public' | 'technical' | 'full';

/** One NDJSON event, keyed `type` (events.py). Loose on purpose: the reducer narrows every field it reads.
 *  `_t` is stamped by the page when the line arrives (ms, performance.now()); `x_*` types are page-only. */
export interface ChatEvent { type: string; _t?: number; [k: string]: unknown }

// ------------------------------------------------------------------ catalogue
export interface CatalogTool { name: string; risk: string; domain: string; description: string }
export interface CatalogSkill { name: string; description: string; source: string }
export interface CatalogCommand { name: string; args: string; description: string }
export interface CatalogProvider { id: string; name: string; kind: string; model: string | null; connected: boolean; profile?: string }
/** What the chat remembers per role (llm_user_config.chat.<role>). */
export interface ChatPrefs {
  provider_id?: string | null;
  model?: string | null;
  mode?: ChatMode;
  effort?: ChatEffort;
  think?: ThinkLevel | null;
  web?: WebLevel;
}
export interface ChatCatalog {
  role: string;
  title: string;
  tools: CatalogTool[];
  skills: CatalogSkill[];
  commands: CatalogCommand[];
  providers: CatalogProvider[];
  active: string | null;
  chat: ChatPrefs;
  web: Record<string, unknown>;
  limits: { max_steps: number; max_retries: number };
}

// ------------------------------------------------------------------ sessions (api/llm/sessions)
export interface ChatSession {
  id: string;
  role: string;
  title: string;
  scope: Record<string, unknown> | null;
  model: string | null;
  mode: string | null;
  effort: string | null;
  pinned: boolean;
  n_messages: number;
  created_at: string;
  updated_at: string;
  last_opened_at: string | null;
}
export interface SessionRef { id: string; title: string; last_opened_at?: string | null }
export interface SessionList {
  sessions: ChatSession[];
  count: number;
  max: number;
  next_cursor: string | null;
  will_remove: SessionRef | null;
  all_pinned?: boolean;
}
export interface SessionMessage { seq: number; role: 'user' | 'assistant'; text: string; trace: ChatEvent[] | null; usage: Record<string, unknown> | null; created_at: string }
export interface SessionDetail { session: ChatSession; messages: SessionMessage[] }

// ------------------------------------------------------------------ the transcript (built by reduce())
export interface ToolItem {
  id: string;
  name: string;
  args: Record<string, unknown>;
  risk: string;
  summary: string;
  ok: boolean | null;          // null = running
  ms: number | null;
  preview: string;
  undoId: string | null;
  undone: boolean;
  url: string | null;
}
export interface ConfirmAction { id: string; tool: string; args: Record<string, unknown>; summary: string; risk: string }
export interface ConfirmResult { id: string; ok: boolean; summary: string; declined: boolean; undoId: string | null; undone: boolean }
export type ConfirmState = 'pending' | 'sent' | 'decided' | 'stopped' | 'timed out' | 'expired';
export interface UiAction { id: string; action: string; args: Record<string, unknown>; label: string; applied: boolean }

export type Block =
  | { k: 'think'; text: string; t0: number; ms: number | null }
  | { k: 'text'; text: string; step: number }
  | { k: 'tools'; read: boolean; items: ToolItem[] }
  | { k: 'search'; query: string; engine: string; results: Array<{ title: string; url: string }>; blocked: boolean; reasons: string[]; level: string | null }
  | { k: 'skill'; name: string }
  | { k: 'todo'; items: Array<{ text: string; state: string }> }
  | { k: 'confirm'; id: string; actions: ConfirmAction[]; state: ConfirmState; results: ConfirmResult[]; timeoutS: number; t0: number }
  | { k: 'ask'; question: string; options: string[]; answered: boolean }
  | { k: 'cannot'; reason: string; alternative: string | null }
  | { k: 'plan'; summary: string; steps: string[]; state: 'pending' | 'approved' }
  | ({ k: 'ui' } & UiAction)
  | { k: 'retry'; attempt: number; max: number; reason: string; waitS: number; t0: number }
  | { k: 'notice'; level: string; text: string };

export interface TurnMeta {
  runId: string | null; model: string | null; provider: string | null; mode: string | null; effort: string | null; maxSteps: number | null;
  /** the estimated prompt tokens and the number of tools the model was sent (meta.in_est / meta.tools) */
  inEst: number | null; tools: number | null; think: string | null; web: string | null;
}
/** One answer of the agent. */
export interface AiTurn {
  role: 'ai';
  status: 'streaming' | 'done' | 'error' | 'stopped';
  blocks: Block[];
  step: { n: number; max: number } | null;
  meta: TurnMeta | null;
  usage: { in: number; out: number } | null;
  ms: number | null;
  steps: number | null;
  err: string | null;
  t0: number;
  resumed: boolean;
  /** 'report': the footer also offers Download .md */
  kind: string | null;
}
