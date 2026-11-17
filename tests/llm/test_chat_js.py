"""static/ts/core/chat.ts run under node (skipped without node / esbuild): the pure reducer that folds the agent's
NDJSON events (dataviz/llm/engine/events.py) into the blocks the ◆ chat draws: thinking with its time, read tools
grouped and change tools as cards, a retry that discards the step's streamed text, confirm → sent → confirmed,
UI actions, plan proposals and the page-only x_* events; it never mutates its input. Also the approved-plan message
(the same text as planner.approved_message), slash-command parsing, the mode cycle and the History date groups."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAT_TS = ROOT / 'static' / 'ts' / 'core' / 'chat.ts'

SCRIPT = r"""
import assert from 'node:assert/strict';
import { reduce, newTurn, approvedMessage, parseCommand, nextMode, dateGroup, turnText, expandTrace, stepState } from './chat.mjs';

const freeze = (o) => { if (o && typeof o === 'object') { Object.freeze(o); for (const v of Object.values(o)) freeze(v); } return o; };
// every step runs on a deep-frozen turn: a mutation throws (ESM is strict mode)
const run = (evs, t0 = newTurn(0)) => evs.reduce((t, e) => reduce(freeze(t), e), t0);

let t = run([
  { type: 'meta', run_id: 'r1', model: 'qwen', provider: 'local', mode: 'ask', effort: 'medium', max_steps: 30, in_est: 3200, tools: 14 },
  { type: 'step', n: 1, max: 30 },
  { type: 'thinking', text: 'Let me ', _t: 100 },
  { type: 'thinking', text: 'look.', _t: 150 },
  { type: 'thinking_end', ms: 420 },
  { type: 'tool_start', id: 'c1', name: 'list_folder', args: { path: 'val' }, risk: 'read' },
  { type: 'tool_start', id: 'c2', name: 'get_stats', args: {}, risk: 'read' },
  { type: 'tool_end', id: 'c1', name: 'list_folder', ok: true, summary: '→ 12 entries', ms: 8, preview: { n: 12 } },
  { type: 'tool_end', id: 'c2', name: 'get_stats', ok: false, summary: '→ boom', ms: 3, preview: 'boom' },
  { type: 'text', text: 'Hello ' },
]);
assert.equal(t.meta.runId, 'r1');
assert.equal(t.meta.inEst, 3200, 'the activity dropdown shows the prompt estimate');
assert.equal(t.meta.tools, 14);
assert.deepEqual(t.step, { n: 1, max: 30 });
assert.equal(t.blocks[0].k, 'think');
assert.equal(t.blocks[0].text, 'Let me look.');
assert.equal(t.blocks[0].ms, 420);
assert.equal(t.blocks[1].k, 'tools');
assert.equal(t.blocks[1].read, true);
assert.equal(t.blocks[1].items.length, 2, 'consecutive reads share one group');
assert.equal(t.blocks[1].items[0].ok, true);
assert.match(t.blocks[1].items[0].preview, /"n": 12/);
assert.equal(t.blocks[1].items[1].ok, false);
assert.equal(t.blocks[2].k, 'text');

// a retry with discard drops the text streamed in that step, and keeps one retry notice
t = run([
  { type: 'text', text: 'world' },
  { type: 'retry', attempt: 1, max: 50, reason: 'timeout', wait_s: 1, discard: true, step: 1 },
  { type: 'retry', attempt: 2, max: 50, reason: 'timeout', wait_s: 2, discard: false, step: 1 },
], t);
assert.equal(t.blocks.filter((b) => b.k === 'text').length, 0);
assert.equal(t.blocks.filter((b) => b.k === 'retry').length, 1);
assert.equal(t.blocks.at(-1).attempt, 2);

// a change after a read: its own card; confirm -> sent -> confirmed; undo
t = run([
  { type: 'step', n: 2, max: 30 },
  { type: 'text', text: 'Moving.' },
  { type: 'confirm', confirm_id: 'k1', actions: [{ id: 'a1', tool: 'delete', args: { bucket: 'b', paths: ['x'] }, summary: 'delete x', risk: 'danger' }], timeout_s: 600 },
  { type: 'x_confirm_sent', confirm_id: 'k1' },
], t);
const conf = t.blocks.find((b) => b.k === 'confirm');
assert.equal(conf.state, 'sent');
assert.equal(conf.actions[0].risk, 'danger');
t = run([
  { type: 'tool_start', id: 'c3', name: 'delete', args: { bucket: 'b' }, risk: 'danger', summary: 'delete x' },
  { type: 'tool_end', id: 'c3', name: 'delete', ok: true, summary: 'deleted 1', ms: 5, risk: 'danger', undo_id: 'u9' },
  { type: 'confirmed', confirm_id: 'k1', status: 'decided', results: [{ id: 'a1', ok: true, summary: 'deleted 1', undo_id: 'u9' }] },
], t);
const card = t.blocks.filter((b) => b.k === 'tools').at(-1);
assert.equal(card.read, false, 'a change is a card of its own');
assert.equal(card.items[0].undoId, 'u9');
assert.equal(t.blocks.find((b) => b.k === 'confirm').state, 'decided');
assert.equal(t.blocks.find((b) => b.k === 'confirm').results[0].undoId, 'u9');
t = run([{ type: 'x_undone', undo_id: 'u9' }], t);
assert.equal(t.blocks.filter((b) => b.k === 'tools').at(-1).items[0].undone, true);
assert.equal(t.blocks.find((b) => b.k === 'confirm').results[0].undone, true);

// visualizer actions, plan, ask, todo, notices, done
t = run([
  { type: 'ui_action', id: 'u1', action: 'set_projection', args: { method: 'umap' }, label: 'Switch to UMAP', applied: false },
  { type: 'x_ui_applied', id: 'u1' },
  { type: 'todo', items: [{ text: 'a', state: 'done' }] },
  { type: 'todo', items: [{ text: 'a', state: 'done' }, { text: 'b', state: 'pending' }] },
  { type: 'skill', name: 'cleanup' }, { type: 'skill', name: 'cleanup' },
  { type: 'search', query: 'umap vs tsne', engine: 'searxng', results: [{ title: 'A', url: 'https://a.example' }, { title: 'no url' }] },
  { type: 'ask', question: 'Which split?', options: ['train', 'val'] },
  { type: 'x_answered' },
  { type: 'plan_proposal', summary: 'Tidy val', steps: ['make folder night', 'move 3 images'] },
  { type: 'ping' }, { type: 'session', session_id: 's1', title: 'x' },
  { type: 'done', usage: { in: 120, out: 30 }, ms: 900, steps: 2 },
], t);
const ui = t.blocks.find((b) => b.k === 'ui');
assert.equal(ui.applied, true);
assert.equal(t.blocks.filter((b) => b.k === 'todo').length, 1, 'one live checklist');
assert.equal(t.blocks.find((b) => b.k === 'todo').items.length, 2);
assert.equal(t.blocks.filter((b) => b.k === 'skill').length, 1);
assert.equal(t.blocks.find((b) => b.k === 'search').results.length, 1, 'a source without a url is dropped');
assert.equal(t.blocks.find((b) => b.k === 'ask').answered, true);
assert.equal(t.blocks.find((b) => b.k === 'plan').state, 'pending');
assert.equal(t.status, 'done');
assert.deepEqual(t.usage, { in: 120, out: 30 });
assert.match(turnText(t), /Moving\./);
assert.match(turnText(t), /Proposed plan: Tidy val/);

// a resumed trace: a pending confirm expires, unfinished thinking closes
let r = run([
  { type: 'thinking', text: 'hmm' },
  { type: 'confirm', confirm_id: 'k2', actions: [{ id: 'a1', tool: 'move', args: {}, summary: 'move', risk: 'change' }] },
  { type: 'x_expire' },
]);
assert.equal(r.blocks.find((b) => b.k === 'confirm').state, 'expired');
assert.equal(r.blocks[0].ms, 0);
assert.equal(r.status, 'done');
assert.equal(r.resumed, true);

// a saved chat reopened: the stored trace (sessions.compact_trace) replays to the same cards as the live answer
const saved = JSON.parse(process.env.SAVED_TRACE);
let o = run([...expandTrace(saved), { type: 'x_expire' }]);
assert.equal(o.blocks.find((b) => b.k === 'think').ms, 1200, 'the thinking time is kept');
const tl = o.blocks.filter((b) => b.k === 'tools').flatMap((b) => b.items);
assert.deepEqual(tl.map((i) => [i.name, i.ok]), [['list_folder', true], ['add_tags', true]], 'tool rows and change cards are back');
assert.equal(tl[1].undoId, 'u9', 'with their Undo');
assert.equal(tl[1].risk, 'change');
const cf = o.blocks.find((b) => b.k === 'confirm');
assert.equal(cf.state, 'decided', 'an approved confirmation is not shown as expired');
assert.equal(cf.results[0].ok, true);
assert.equal(o.blocks.find((b) => b.k === 'retry').attempt, 2);
assert.equal(expandTrace([{ type: 'confirm', confirm_id: 'k', actions: [], status: 'pending' }]).length, 1, 'an undecided one still expires');
assert.deepEqual(expandTrace([null, 'x', { type: 'tool', id: 'c', name: 'n' }]).map((e) => e.type), ['tool_start'], 'a call that never ended');

// checklist states: the todo tool's pending | active | done | skipped (and the older in_progress / completed)
assert.deepEqual(['pending', 'active', 'done', 'skipped', 'in_progress', 'completed', ''].map(stepState),
  ['todo', 'doing', 'done', 'skipped', 'doing', 'done', 'todo']);

// errors and stops
assert.equal(run([{ type: 'error', detail: 'stopped after 50 steps' }]).err, 'stopped after 50 steps');
assert.equal(run([{ type: 'x_stopped' }]).status, 'stopped');
assert.equal(run([{ type: 'x_empty' }]).err, 'The assistant sent an empty answer.');
const same = newTurn(0);
assert.equal(reduce(same, { type: 'nope' }), same, 'an unknown event changes nothing');

// helpers
assert.equal(approvedMessage('Tidy val', ['make folder night', 'move 3 images']),
  'Approved plan: Tidy val\nCarry it out now, step by step, with the real names you found:\n1. make folder night\n2. move 3 images');
assert.match(approvedMessage('s', ['a'], 'skip b'), /\nChange to the plan: skip b$/);
assert.deepEqual(parseCommand('/mode auto'), { name: 'mode', arg: 'auto' });
assert.deepEqual(parseCommand('/Model'), { name: 'model', arg: '' });
assert.deepEqual(parseCommand('/compact the model results'), { name: 'compact', arg: 'the model results' });

// compaction: the start block is replaced by the done one (with the summary), never duplicated
let ct = run([{ type: 'compact', state: 'start', auto: true }]);
assert.equal(ct.blocks.length, 1);
assert.equal(ct.blocks[0].state, 'start');
ct = run([{ type: 'compact', state: 'done', auto: true, summary: '## Goal\n- find bad trucks', before: 9000, after: 700, turns: 36, covered: 36 }], ct);
assert.equal(ct.blocks.length, 1);
assert.deepEqual([ct.blocks[0].state, ct.blocks[0].turns, ct.blocks[0].after, ct.blocks[0].covered], ['done', 36, 700, 36]);
assert.match(ct.blocks[0].summary, /bad trucks/);
assert.equal(turnText(ct), '', 'the summary is not part of the answer text');
// the context ring: each context event replaces the last, done keeps the last step's (not the summed usage)
let cx = run([{ type: 'meta', run_id: 'r9' }, { type: 'context', used: 3000, limit: 100000, exact: false }]);
assert.deepEqual(cx.ctx, { used: 3000, limit: 100000 });
cx = run([{ type: 'context', used: 5200, limit: 100000, exact: true }, { type: 'done', usage: { in: 8200, out: 40 }, ctx: 5200, limit: 100000 }], cx);
assert.deepEqual([cx.ctx, cx.usage.in], [{ used: 5200, limit: 100000 }, 8200]);
assert.equal(run([{ type: 'done', usage: { in: 1, out: 1 } }], cx).ctx.used, 5200, 'an old done without ctx keeps it');
assert.equal(run([{ type: 'compact', state: 'done', turns: 3 }], cx).ctx, null, 'compacted: unknown until the next call');
assert.equal(run([{ type: 'context', used: 'x', limit: 0 }]).ctx, null);
assert.equal(parseCommand('hello /mode'), null);
assert.deepEqual(['ask', 'auto', 'plan'].map(nextMode), ['auto', 'plan', 'ask']);
const now = new Date(2026, 9, 29, 12);
assert.equal(dateGroup(new Date(2026, 9, 29, 1).toISOString(), now), 'Today');
assert.equal(dateGroup(new Date(2026, 9, 28, 23).toISOString(), now), 'Yesterday');
assert.equal(dateGroup(new Date(2026, 9, 24).toISOString(), now), 'Last 7 days');
assert.equal(dateGroup(new Date(2026, 8, 1).toISOString(), now), 'Older');
assert.equal(dateGroup(null, now), 'Older');
console.log('ok');
"""


def _saved_trace():
    """One answer's live events as the session store keeps them (a read, a confirmed change, thinking, retries)."""
    from dataviz.llm import sessions
    return sessions.compact_trace([
        {'type': 'meta', 'model': 'm', 'mode': 'ask', 'effort': 'medium', 'run_id': 'r'}, {'type': 'step', 'n': 1, 'max': 30},
        {'type': 'thinking', 'text': 'hmm '}, {'type': 'thinking', 'text': 'ok'}, {'type': 'thinking_end', 'ms': 1200},
        {'type': 'tool_start', 'id': 'c1', 'name': 'list_folder', 'args': {'bucket': 'b'}, 'risk': 'read'},
        {'type': 'tool_end', 'id': 'c1', 'name': 'list_folder', 'ok': True, 'summary': '→ 3 entries', 'ms': 12, 'preview': '{}'},
        {'type': 'confirm', 'confirm_id': 'k1', 'timeout_s': 600, 'actions': [
            {'id': 'a1', 'call_id': 'c2', 'tool': 'add_tags', 'args': {'bucket': 'b'}, 'summary': 'Tag b', 'risk': 'change'}]},
        {'type': 'tool_start', 'id': 'c2', 'name': 'add_tags', 'args': {'bucket': 'b'}, 'risk': 'change', 'summary': 'Tag b'},
        {'type': 'tool_end', 'id': 'c2', 'name': 'add_tags', 'ok': True, 'summary': '→ tagged', 'ms': 30, 'risk': 'change', 'undo_id': 'u9'},
        {'type': 'confirmed', 'confirm_id': 'k1', 'status': 'decided',
         'results': [{'id': 'a1', 'ok': True, 'summary': '→ tagged', 'undo_id': 'u9'}]},
        {'type': 'retry', 'attempt': 1, 'max': 50, 'reason': '429', 'wait_s': 1, 'discard': False, 'step': 2},
        {'type': 'retry', 'attempt': 2, 'max': 50, 'reason': '429', 'wait_s': 2, 'discard': False, 'step': 2},
        {'type': 'text', 'text': 'Done.'}, {'type': 'done', 'usage': {'in': 10, 'out': 2}, 'ms': 900, 'steps': 2}])


def test_chat_reducer_js(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.exists():
        pytest.skip('run `npm install` to get esbuild')
    # bundled: chat.ts imports dom / api / md / popover / toast / stream; none of them touch the DOM at import time
    b = subprocess.run([str(esbuild), str(CHAT_TS), '--bundle', '--format=esm', '--platform=neutral',
                        f'--outfile={tmp_path / "chat.mjs"}'], capture_output=True, text=True, timeout=60)
    assert b.returncode == 0, b.stderr
    (tmp_path / 't.mjs').write_text(SCRIPT)
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=60,
                       env={**os.environ, 'SAVED_TRACE': json.dumps(_saved_trace())})
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr + r.stdout


STREAM_TS = ROOT / 'static' / 'ts' / 'core' / 'stream.ts'
STREAM_SCRIPT = r"""
import assert from 'node:assert/strict';
import { streamNDJSON } from './stream.mjs';

// a body that arrives in two chunks, the first ending mid-line: events are handed on as their lines complete
let seen = null;
globalThis.fetch = async (path, init) => {
  seen = { path, init };
  const enc = new TextEncoder();
  const body = new ReadableStream({ start(c) { c.enqueue(enc.encode('{"type":"meta"}\n{"type":"st')); c.enqueue(enc.encode('ep","n":1}\nnot json\n{"type":"done"}')); c.close(); } });
  return new Response(body, { status: 200 });
};
const evs = [];
await streamNDJSON('api/llm/agent/bucket/chat', { text: 'hi' }, (v) => evs.push(v.type));
assert.deepEqual(evs, ['meta', 'step', 'done']);
const h = seen.init.headers;
// JupyterHub's jupyter-server-proxy streams only when Accept is exactly text/event-stream (else it buffers it all)
assert.equal(h.get('Accept'), 'text/event-stream');
assert.equal(h.get('Content-Type'), 'application/json');
assert.equal(h.get('X-Requested-With'), 'dataviz');
assert.equal(seen.init.method, 'POST');
console.log('ok');
"""


def test_stream_ndjson_js(tmp_path):
    """streamNDJSON asks the proxy to stream (Accept: text/event-stream) and hands on each line as it completes."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.exists():
        pytest.skip('run `npm install` to get esbuild')
    b = subprocess.run([str(esbuild), str(STREAM_TS), '--bundle', '--format=esm', '--platform=neutral',
                        f'--outfile={tmp_path / "stream.mjs"}'], capture_output=True, text=True, timeout=60)
    assert b.returncode == 0, b.stderr
    (tmp_path / 't.mjs').write_text(STREAM_SCRIPT)
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr + r.stdout


DIFF_TS = ROOT / 'static' / 'ts' / 'core' / 'linediff.ts'
DIFF_SCRIPT = r"""
import assert from 'node:assert/strict';
import { diffLines } from './linediff.mjs';

const sides = (ops) => [ops.filter((o) => o.t !== '+').map((o) => o.s), ops.filter((o) => o.t !== '-').map((o) => o.s)];
assert.deepEqual(diffLines(['a', 'b'], ['a', 'b']), [{ t: ' ', s: 'a' }, { t: ' ', s: 'b' }], 'identical');
assert.deepEqual(diffLines([], ['x']), [{ t: '+', s: 'x' }], 'insert into empty');
assert.deepEqual(diffLines(['SELECT *', 'FROM t', 'LIMIT 10'], ['SELECT TOP 10 *', 'FROM t']),
  [{ t: '-', s: 'SELECT *' }, { t: '+', s: 'SELECT TOP 10 *' }, { t: ' ', s: 'FROM t' }, { t: '-', s: 'LIMIT 10' }]);
// random texts: both sides always come back, and the edit count is minimal for a known case
let seed = 7;
const rnd = (n) => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; };
for (let i = 0; i < 2000; i++) {
  const mk = () => Array.from({ length: rnd(14) }, () => 'abcd'[rnd(4)]);
  const a = mk(), b = mk(), [x, y] = sides(diffLines(a, b));
  assert.deepEqual(x, a); assert.deepEqual(y, b);
}
assert.equal(diffLines(['a', 'b', 'c', 'd'], ['a', 'x', 'c', 'd']).filter((o) => o.t !== ' ').length, 2);
assert.equal(diffLines(['a', 'b', 'c'], ['x', 'y', 'z'], 2), null, 'gives up past maxD');
console.log('ok');
"""


def test_line_diff_js(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.exists():
        pytest.skip('run `npm install` to get esbuild')
    b = subprocess.run([str(esbuild), str(DIFF_TS), '--format=esm', '--platform=neutral',
                        f'--outfile={tmp_path / "linediff.mjs"}'], capture_output=True, text=True, timeout=60)
    assert b.returncode == 0, b.stderr
    (tmp_path / 't.mjs').write_text(DIFF_SCRIPT)
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr + r.stdout
