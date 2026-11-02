// The pure part of the ◆ chat (no DOM, no imports at run time): reduce(turn, event) folds the agent's NDJSON events
// into the blocks of one answer, plus the small helpers the renderer shares. Unit-tested under node
// (tests/test_chat_js.py, through core/chat.ts which re-exports it). Split out of core/chat.ts.

import type {
  AiTurn, Block, ChatEffort, ChatEvent, ChatMode, ConfirmAction, ConfirmResult, ConfirmState, ThinkLevel, ToolItem, UiAction, WebLevel,
} from '../types-chat.js';


export const isRec = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
export const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : v == null ? d : String(v));
export const strOrNull = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
export const num = (v: unknown, d = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : d);
export const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
export const recOf = (v: unknown): Record<string, unknown> => (isRec(v) ? v : {});
export const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

export const MODES: ChatMode[] = ['ask', 'auto', 'plan'];
export const EFFORTS: ChatEffort[] = ['low', 'medium', 'high'];
export const THINKS: Array<ThinkLevel | null> = [null, 'off', 'low', 'medium', 'high'];
export const WEBS: WebLevel[] = ['off', 'public', 'technical', 'full'];
export const isMode = (v: unknown): v is ChatMode => MODES.includes(v as ChatMode);
export const isEffort = (v: unknown): v is ChatEffort => EFFORTS.includes(v as ChatEffort);
export const isThink = (v: unknown): v is ThinkLevel => v === 'off' || v === 'low' || v === 'medium' || v === 'high';
export const isWeb = (v: unknown): v is WebLevel => WEBS.includes(v as WebLevel);

/** ask → auto → plan → ask (Shift+Tab, the mode chip). */
export const nextMode = (m: ChatMode): ChatMode => MODES[(MODES.indexOf(m) + 1) % MODES.length] ?? 'ask';
export const cycle = <T>(list: readonly T[], v: T): T => list[(list.indexOf(v) + 1) % list.length] ?? v;

/** A new, empty answer. `t0` is performance.now() when it started (0 in tests and resumed traces). */
export function newTurn(t0 = 0, kind: string | null = null): AiTurn {
  return { role: 'ai', status: 'streaming', blocks: [], step: null, meta: null, usage: null, ms: null, steps: null, err: null, t0, resumed: false, kind };
}

/** The user message an approved plan becomes (the same text as dataviz/llm/engine/planner.py approved_message). */
export function approvedMessage(summary: string, steps: string[], note = ''): string {
  const lines = [`Approved plan: ${summary}`.trim(), 'Carry it out now, step by step, with the real names you found:'];
  steps.forEach((s, k) => lines.push(`${k + 1}. ${s}`));
  if (note) lines.push(`Change to the plan: ${note}`);
  return lines.join('\n');
}

/** "/name rest" → {name, arg}; null for anything that is not a slash command. */
export function parseCommand(text: string): { name: string; arg: string } | null {
  const m = /^\/([a-z][\w-]*)(?:\s+([\s\S]*))?$/i.exec(text.trim());
  return m ? { name: (m[1] ?? '').toLowerCase(), arg: (m[2] ?? '').trim() } : null;
}

/** The History panel's date group of a timestamp. */
export function dateGroup(iso: string | null | undefined, now = new Date()): 'Today' | 'Yesterday' | 'Last 7 days' | 'Older' {
  const t = iso ? new Date(iso) : null;
  if (!t || Number.isNaN(t.getTime())) return 'Older';
  const day = (d: Date): number => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((day(now) - day(t)) / 86400000);
  return diff <= 0 ? 'Today' : diff === 1 ? 'Yesterday' : diff < 7 ? 'Last 7 days' : 'Older';
}

export function previewText(v: unknown): string {
  if (v == null || v === '') return '';
  if (typeof v === 'string') return v.slice(0, 6000);
  try { return JSON.stringify(v, null, 2).slice(0, 6000); } catch (e) { return String(v); }
}

export function toolItem(ev: ChatEvent): ToolItem {
  return {
    id: str(ev['id']), name: str(ev['name'], '?'), args: recOf(ev['args']), risk: str(ev['risk'], 'read'),
    summary: str(ev['summary']), ok: null, ms: null, preview: '', undoId: null, undone: false, url: null,
  };
}

export function replaceAt<T>(list: readonly T[], i: number, v: T): T[] { const a = list.slice(); a[i] = v; return a; }

/** Apply f to the tool item `id` wherever it is; null when no block holds it. */
export function mapTool(blocks: readonly Block[], id: string, f: (it: ToolItem) => ToolItem): Block[] | null {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i];
    if (b?.k !== 'tools') continue;
    const j = b.items.findIndex((x) => x.id === id);
    if (j >= 0) return replaceAt(blocks, i, { ...b, items: replaceAt(b.items, j, f(b.items[j] as ToolItem)) });
  }
  return null;
}

export function mapBlocks(blocks: readonly Block[], f: (b: Block) => Block): Block[] {
  let changed = false;
  const out = blocks.map((b) => { const n = f(b); if (n !== b) changed = true; return n; });
  return changed ? out : (blocks as Block[]);
}

export function confirmResult(x: unknown): ConfirmResult {
  const r = recOf(x);
  return { id: str(r['id']), ok: r['ok'] !== false, summary: str(r['summary']), declined: !!r['declined'], undoId: strOrNull(r['undo_id']), undone: false };
}

/** Fold one event into an answer. Pure: `t` is never mutated, a new object comes back when anything changed. */
export function reduce(t: AiTurn, ev: ChatEvent): AiTurn {
  const at = num(ev._t, t.t0);
  const b = t.blocks;
  const lb = b[b.length - 1];
  switch (ev.type) {
    case 'meta':
      return { ...t, meta: { runId: strOrNull(ev['run_id']), model: strOrNull(ev['model']), provider: strOrNull(ev['provider']),
        mode: strOrNull(ev['mode']), effort: strOrNull(ev['effort']), maxSteps: numOrNull(ev['max_steps']),
        inEst: numOrNull(ev['in_est']), tools: numOrNull(ev['tools']), think: strOrNull(ev['think']), web: strOrNull(ev['web']) } };
    case 'step':
      return { ...t, step: { n: num(ev['n'], 1), max: num(ev['max'], t.meta?.maxSteps ?? 0) } };
    case 'thinking': {
      const text = str(ev['text']);
      if (!text) return t;
      if (lb?.k === 'think' && lb.ms === null) return { ...t, blocks: replaceAt(b, b.length - 1, { ...lb, text: lb.text + text }) };
      return { ...t, blocks: [...b, { k: 'think', text, t0: at, ms: null }] };
    }
    case 'thinking_end': {
      for (let i = b.length - 1; i >= 0; i--) {
        const x = b[i];
        if (x?.k === 'think' && x.ms === null) return { ...t, blocks: replaceAt(b, i, { ...x, ms: num(ev['ms'], Math.max(0, at - x.t0)) }) };
      }
      return t;
    }
    case 'text': {
      const text = str(ev['text']);
      if (!text) return t;
      const step = t.step?.n ?? 0;
      if (lb?.k === 'text' && lb.step === step) return { ...t, blocks: replaceAt(b, b.length - 1, { ...lb, text: lb.text + text }) };
      return { ...t, blocks: [...b, { k: 'text', text, step }] };
    }
    case 'tool_start': {
      const item = toolItem(ev);
      const again = item.id ? mapTool(b, item.id, (x) => ({ ...x, args: item.args, risk: item.risk, summary: item.summary || x.summary, ok: null })) : null;
      if (again) return { ...t, blocks: again };
      const read = item.risk === 'read';
      if (read && lb?.k === 'tools' && lb.read) return { ...t, blocks: replaceAt(b, b.length - 1, { ...lb, items: [...lb.items, item] }) };
      return { ...t, blocks: [...b, { k: 'tools', read, items: [item] }] };
    }
    case 'tool_end': {
      const id = str(ev['id']);
      const done = (x: ToolItem): ToolItem => ({
        ...x, ok: ev['ok'] !== false, summary: str(ev['summary'], x.summary), ms: numOrNull(ev['ms']),
        preview: previewText(ev['preview']) || x.preview, undoId: strOrNull(ev['undo_id']), url: strOrNull(ev['url']),
        risk: typeof ev['risk'] === 'string' ? ev['risk'] : x.risk,
      });
      const next = mapTool(b, id, done);
      if (next) return { ...t, blocks: next };
      const item = done(toolItem(ev));                 // a trace without its start: show the finished call
      return { ...t, blocks: [...b, { k: 'tools', read: item.risk === 'read', items: [item] }] };
    }
    case 'skill': {
      const name = str(ev['name']);
      if (!name || (lb?.k === 'skill' && lb.name === name)) return t;
      return { ...t, blocks: [...b, { k: 'skill', name }] };
    }
    case 'search': {
      const results = arr(ev['results']).map(recOf).map((r) => ({ title: str(r['title']) || str(r['url']), url: str(r['url']) })).filter((r) => r.url);
      return { ...t, blocks: [...b, { k: 'search', query: str(ev['query']), engine: str(ev['engine']), results, blocked: !!ev['blocked'],
        reasons: arr(ev['reasons']).map((x) => str(x)).filter(Boolean), level: strOrNull(ev['level']) }] };
    }
    case 'todo': {
      const items = arr(ev['items']).map(recOf).map((r) => ({ text: str(r['text']), state: str(r['state'], 'pending') }));
      return { ...t, blocks: [...b.filter((x) => x.k !== 'todo'), { k: 'todo', items }] };   // one live checklist, at the bottom
    }
    case 'confirm': {
      const actions: ConfirmAction[] = arr(ev['actions']).map(recOf).map((a) => ({
        id: str(a['id']), tool: str(a['tool'], '?'), args: recOf(a['args']), summary: str(a['summary']), risk: str(a['risk'], 'change'),
      }));
      return { ...t, blocks: [...b, { k: 'confirm', id: str(ev['confirm_id']), actions, state: 'pending', results: [], timeoutS: num(ev['timeout_s'], 600), t0: at }] };
    }
    case 'confirmed': {
      const id = str(ev['confirm_id']);
      const st = str(ev['status'], 'decided');
      const state: ConfirmState = st === 'stopped' ? 'stopped' : st === 'timed out' || st === 'timeout' ? 'timed out' : 'decided';
      const results = arr(ev['results']).map(confirmResult);
      return { ...t, blocks: mapBlocks(b, (x) => (x.k === 'confirm' && x.id === id ? { ...x, state, results } : x)) };
    }
    case 'ask':
      return { ...t, blocks: [...b, { k: 'ask', question: str(ev['question']), options: arr(ev['options']).map((o) => str(o)).filter(Boolean), answered: false }] };
    case 'cannot':
      return { ...t, blocks: [...b, { k: 'cannot', reason: str(ev['reason']), alternative: strOrNull(ev['alternative']) }] };
    case 'plan_proposal':
      return { ...t, blocks: [...b, { k: 'plan', summary: str(ev['summary']), steps: arr(ev['steps']).map((s) => str(s)), state: 'pending' }] };
    case 'ui_action': {
      const ui: UiAction = { id: str(ev['id']), action: str(ev['action']), args: recOf(ev['args']), label: str(ev['label']) || str(ev['action']), applied: !!ev['applied'] };
      return { ...t, blocks: [...b, { k: 'ui', ...ui }] };
    }
    case 'retry': {
      const step = num(ev['step'], t.step?.n ?? -1);
      const kept = b.filter((x) => x.k !== 'retry' && !(ev['discard'] && x.k === 'text' && x.step === step));
      return { ...t, blocks: [...kept, { k: 'retry', attempt: num(ev['attempt'], 1), max: num(ev['max'], 50), reason: str(ev['reason']), waitS: num(ev['wait_s']), t0: at }] };
    }
    case 'notice': {
      const text = str(ev['text']);
      return text ? { ...t, blocks: [...b, { k: 'notice', level: str(ev['level'], 'info'), text }] } : t;
    }
    case 'error':
      return { ...t, status: 'error', err: str(ev['detail']) || 'The assistant failed.' };
    case 'done': {
      const u = recOf(ev['usage']);
      return { ...t, status: t.status === 'streaming' ? 'done' : t.status, usage: { in: num(u['in']), out: num(u['out']) }, ms: numOrNull(ev['ms']), steps: numOrNull(ev['steps']) };
    }
    // ---- page-only
    case 'x_stopped':
      return t.status === 'streaming' ? { ...t, status: 'stopped' } : t;
    case 'x_failed':
      return { ...t, status: 'error', err: str(ev['detail']) || 'The assistant failed.' };
    case 'x_empty':
      return t.status === 'streaming' ? (b.length ? { ...t, status: 'done' } : { ...t, status: 'error', err: 'The assistant sent an empty answer.' }) : t;
    case 'x_confirm_sent': {
      const id = str(ev['confirm_id']);
      return { ...t, blocks: mapBlocks(b, (x) => (x.k === 'confirm' && x.id === id && x.state === 'pending' ? { ...x, state: 'sent' } : x)) };
    }
    case 'x_ui_applied': {
      const id = str(ev['id']);
      return { ...t, blocks: mapBlocks(b, (x) => (x.k === 'ui' && x.id === id ? { ...x, applied: true } : x)) };
    }
    case 'x_answered':
      return { ...t, blocks: mapBlocks(b, (x) => (x.k === 'ask' && !x.answered ? { ...x, answered: true } : x)) };
    case 'x_plan_approved':
      return { ...t, blocks: mapBlocks(b, (x) => (x.k === 'plan' && x.state === 'pending' ? { ...x, state: 'approved' } : x)) };
    case 'x_undone': {
      const id = str(ev['undo_id']);
      return { ...t, blocks: mapBlocks(b, (x) => {
        if (x.k === 'tools' && x.items.some((i) => i.undoId === id)) return { ...x, items: x.items.map((i) => (i.undoId === id ? { ...i, undone: true } : i)) };
        if (x.k === 'confirm' && x.results.some((r) => r.undoId === id)) return { ...x, results: x.results.map((r) => (r.undoId === id ? { ...r, undone: true } : r)) };
        return x;
      }) };
    }
    case 'x_expire': {             // a resumed chat: nothing waits any more
      const blocks = mapBlocks(b, (x) => (x.k === 'confirm' && (x.state === 'pending' || x.state === 'sent') ? { ...x, state: 'expired' }
        : x.k === 'think' && x.ms === null ? { ...x, ms: 0 } : x));
      return { ...t, blocks, resumed: true, status: t.status === 'streaming' ? 'done' : t.status };
    }
    default:
      return t;                   // meta-level events (session, ping) and unknown types
  }
}

/** The answer's own words (its text blocks), for Copy, Export and the history sent to the model. */
export function turnText(t: AiTurn): string {
  const out: string[] = [];
  for (const b of t.blocks) {
    if (b.k === 'text') out.push(b.text);
    else if (b.k === 'ask') out.push(`${b.question}${b.options.length ? ` (${b.options.join(' / ')})` : ''}`);
    else if (b.k === 'cannot') out.push(`I can't do that: ${b.reason}${b.alternative ? ` Instead: ${b.alternative}` : ''}`);
    else if (b.k === 'plan') out.push(`Proposed plan: ${b.summary}\n${b.steps.map((s, i) => `${i + 1}. ${s}`).join('\n')}`);
  }
  return out.join('\n\n').trim();
}

// ================================================================ formatting
