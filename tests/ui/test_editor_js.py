"""static/vendor-src/editor.ts run under node (skipped without node / esbuild / npm install): language detection by
name and content, JSON / YAML parse errors, structured edits that keep YAML comments, key order and numeric keys
(names: {0: cat}), the JSON file's indent, the header comment surviving a delete, and Format."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDITOR_TS = ROOT / 'static' / 'vendor-src' / 'editor.ts'

SCRIPT = r"""
import assert from 'node:assert/strict';
import * as V from './editor.mjs';

assert.equal(V.detectLang('.env', ''), 'env');
assert.equal(V.detectLang('.env.local', ''), 'env');
assert.equal(V.detectLang('data.yaml', ''), 'yaml');
assert.equal(V.detectLang('Dockerfile', 'FROM x'), 'dockerfile');
assert.equal(V.detectLang('noext', '{"a": 1}'), 'json');
assert.equal(V.detectLang('conf', 'a: 1\nb:\n  - x\n'), 'yaml');
assert.equal(V.detectLang('vars', 'A=1\nexport B=2\n'), 'env');
assert.equal(V.detectLang('notes.txt', 'just words'), 'text');

const y = '# dataset config\npath: .\ntrain: images/train  # training split\nnc: 2\nnames:\n  0: cat\n  1: dog\n';
let t = V.structEdit('yaml', y, { op: 'set', path: ['nc'], value: 3 });
assert.match(t, /^nc: 3$/m);
assert.match(t, /# training split/);
t = V.structEdit('yaml', t, { op: 'add', path: ['names'], key: '2', value: 'bird' });
assert.match(t, /^  2: bird$/m);                         // a number, like its siblings, not "2"
t = V.structEdit('yaml', t, { op: 'set', path: ['names', '0'], value: 'kitty' });
assert.match(t, /^  0: kitty$/m);
t = V.structEdit('yaml', t, { op: 'rename', path: ['train'], key: 'training' });
assert.ok(t.indexOf('training:') < t.indexOf('nc:'));     // keeps its place
assert.throws(() => V.structEdit('yaml', t, { op: 'rename', path: ['nc'], key: 'names' }), /already a key/);
t = V.structEdit('yaml', t, { op: 'delete', path: ['path'] });
assert.ok(t.startsWith('# dataset config') && !/^path:/m.test(t));
assert.deepEqual(V.structParse('yaml', t).value, { training: 'images/train', nc: 3, names: { 0: 'kitty', 1: 'dog', 2: 'bird' } });

const j = JSON.stringify({ a: 1, b: { c: [1, 2] }, d: 'x' }, null, 4) + '\n';
let k = V.structEdit('json', j, { op: 'set', path: ['b', 'c', 1], value: 9 });
assert.equal(k, JSON.stringify({ a: 1, b: { c: [1, 9] }, d: 'x' }, null, 4) + '\n');
k = V.structEdit('json', k, { op: 'rename', path: ['b'], key: 'bee' });
assert.deepEqual(Object.keys(JSON.parse(k)), ['a', 'bee', 'd']);
k = V.structEdit('json', k, { op: 'add', path: ['bee', 'c'], value: 3 });
k = V.structEdit('json', k, { op: 'delete', path: ['a'] });
assert.deepEqual(JSON.parse(k), { bee: { c: [1, 9, 3] }, d: 'x' });

assert.equal(V.structParse('json', '{"a": }').ok, false);
const ye = V.structParse('yaml', 'a: [1, 2\nb: 3');
assert.equal(ye.ok, false);
assert.ok(ye.error.line >= 1);
assert.equal(V.formatText('json', '{"a":[1,2]}'), '{\n  "a": [\n    1,\n    2\n  ]\n}\n');
console.log('ok');
"""


def test_editor_js(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.exists() or not (ROOT / 'node_modules' / 'yaml').is_dir():
        pytest.skip('run `npm install` to get esbuild and the editor libraries')
    b = subprocess.run([str(esbuild), str(EDITOR_TS), '--bundle', '--format=esm', '--platform=neutral',
                        '--main-fields=module,main', f'--outfile={tmp_path / "editor.mjs"}'],
                       capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert b.returncode == 0, b.stderr
    (tmp_path / 't.mjs').write_text(SCRIPT)
    r = subprocess.run([node, str(tmp_path / 't.mjs')], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr
