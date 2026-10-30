"""static/ts/core/chat.ts run under node (skipped without node / esbuild): the pure reducer that folds the agent's
NDJSON events (dataviz/llm/engine/events.py) into the blocks the ◆ chat draws: thinking with its time, read tools
grouped and change tools as cards, a retry that discards the step's streamed text, confirm → sent → confirmed,
UI actions, plan proposals and the page-only x_* events; it never mutates its input. Also the approved-plan message
(the same text as planner.approved_message), slash-command parsing, the mode cycle and the History date groups."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHAT_TS = ROOT / 'static' / 'ts' / 'core' / 'chat.ts'

SCRIPT = r"""
import assert from 'node:assert/strict';
import { reduce, newTurn, approvedMessage, parseCommand, nextMode, dateGroup, turnText } from './chat.mjs';

const freeze = (o) => { if (o && typeof o === 'object') { Object.freeze(o); for (const v of Object.values(o)) freeze(v); } return o; };
// every step runs on a deep-frozen turn: a mutation throws (ESM is strict mode)
const run = (evs, t0 = newTurn(0)) => evs.reduce((t, e) => reduce(freeze(t), e), t0);

let t = run([
  { type: 'meta', run_id: 'r1', model: 'qwen', provider: 'local', mode: 'ask', effort: 'medium', max_steps: 30 },
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
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr + r.stdout
