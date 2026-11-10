// The Data Center's text editor, bundled with its libraries (CodeMirror 6, yaml) into static/js/vendor/ by build.mjs
// (a second pass: bundle + splitting, so each language is its own chunk, loaded the first time a file of it opens).
// The page loads this module only when a file is opened (datacenter/editor.ts), so it costs nothing until then.
//
//   createEditor(host, opts) -> EditorHandle    a CodeMirror view themed from the page's CSS tokens (light / dark)
//   detectLang(name, text)  -> Lang             by extension / file name, then a sniff of the content
//   structParse(lang, text) -> {ok, value} | {ok: false, error: {msg, line, col}}     JSON / YAML
//   structEdit(lang, text, op) -> text          set / delete / rename / add on a path, keeping YAML comments and
//                                               order (yaml's Document API) and the JSON file's indent
//   formatText(lang, text)  -> text             pretty-print JSON (detected indent) / normalise YAML
//
// The shapes are in static/ts/types-editor.ts (the page's side of the contract).

import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from '@codemirror/autocomplete';
import { defaultKeymap, history, historyKeymap, indentWithTab, isolateHistory } from '@codemirror/commands';
import {
  bracketMatching, foldGutter, foldKeymap, HighlightStyle, indentOnInput, LanguageSupport, StreamLanguage, syntaxHighlighting, syntaxTree,
} from '@codemirror/language';
import type { StreamParser } from '@codemirror/language';
import { linter, lintGutter } from '@codemirror/lint';
import type { Diagnostic } from '@codemirror/lint';
import { highlightSelectionMatches, search, searchKeymap } from '@codemirror/search';
import { Compartment, EditorState } from '@codemirror/state';
import type { Extension } from '@codemirror/state';
import {
  drawSelection, EditorView, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers, rectangularSelection,
} from '@codemirror/view';
import { tags as t } from '@lezer/highlight';
import { isMap, isScalar, isSeq, parseDocument, Scalar } from 'yaml';
import type { Document, YAMLMap } from 'yaml';

export type Lang = 'json' | 'yaml' | 'env' | 'ini' | 'toml' | 'xml' | 'html' | 'css' | 'js' | 'ts' | 'python' | 'markdown' | 'sql'
  | 'shell' | 'dockerfile' | 'csv' | 'text';
export type Path = Array<string | number>;
export type StructOp =
  | { op: 'set'; path: Path; value: unknown }
  | { op: 'delete'; path: Path }
  | { op: 'rename'; path: Path; key: string }
  | { op: 'add'; path: Path; key?: string; value: unknown };
export interface EditorOpts {
  text: string; lang: Lang; readOnly?: boolean; onChange?: (text: string) => void; onSave?: () => void;
}
export interface EditorHandle {
  view: EditorView;
  getText(): string;
  /** replace the whole text (a structured edit, a revert); keeps the scroll position where it can */
  setText(text: string): void;
  setReadOnly(ro: boolean): void;
  focus(): void;
  destroy(): void;
}
export type ParseResult = { ok: true; value: unknown } | { ok: false; error: { msg: string; line: number; col: number } };

// ---------------------------------------------------------------- languages
const EXT: Record<string, Lang> = {
  json: 'json', jsonc: 'json', geojson: 'json', ipynb: 'json', yaml: 'yaml', yml: 'yaml', env: 'env', ini: 'ini', cfg: 'ini',
  conf: 'ini', properties: 'ini', toml: 'toml', xml: 'xml', svg: 'xml', html: 'html', htm: 'html', css: 'css', scss: 'css',
  js: 'js', mjs: 'js', cjs: 'js', jsx: 'js', ts: 'ts', tsx: 'ts', py: 'python', md: 'markdown', markdown: 'markdown',
  sql: 'sql', sh: 'shell', bash: 'shell', zsh: 'shell', csv: 'csv', tsv: 'csv', txt: 'text', log: 'text', names: 'text',
  dockerfile: 'dockerfile',
};

/** The language of a file: its name first (.env*, Dockerfile, extension), then a sniff of the first lines. */
export function detectLang(name: string, text: string): Lang {
  const low = name.toLowerCase();
  if (low === '.env' || low.startsWith('.env.') || low.endsWith('.env')) return 'env';
  if (low === 'dockerfile' || low === 'containerfile' || low.endsWith('.dockerfile')) return 'dockerfile';
  if (low === 'makefile') return 'shell';
  if (low.startsWith('.') && /^\.(prettierrc|eslintrc)/.test(low) && !low.includes('.', 1)) return sniff(text) ?? 'json';
  const ext = low.includes('.') ? low.slice(low.lastIndexOf('.') + 1) : '';
  const byExt = EXT[ext];
  if (byExt && byExt !== 'text') return byExt;
  return sniff(text) ?? byExt ?? 'text';
}

function sniff(text: string): Lang | null {
  const s = text.trimStart();
  if (/^[[{]/.test(s)) { try { JSON.parse(s); return 'json'; } catch { /* not JSON */ } }
  const lines = s.split('\n', 40).filter((l) => l.trim() && !l.trimStart().startsWith('#'));
  if (!lines.length) return null;
  if (lines.every((l) => /^(export\s+)?[A-Za-z_][A-Za-z0-9_.]*\s*=/.test(l))) return 'env';
  if (lines.some((l) => /^\[[^\]]+\]\s*$/.test(l)) && lines.every((l) => /^\[[^\]]+\]\s*$|^[^=:]+[=:]/.test(l))) return 'ini';
  if (s.startsWith('<')) return /<html|<!doctype html/i.test(s.slice(0, 500)) ? 'html' : 'xml';
  if (lines.filter((l) => /^\s*(- |[\w"'./-][^:]*:(\s|$))/.test(l)).length >= Math.max(1, lines.length * 0.6)) return 'yaml';
  return null;
}

const legacy = (p: StreamParser<unknown>): LanguageSupport => new LanguageSupport(StreamLanguage.define(p));

/** The CodeMirror language of `lang`, each in its own chunk. */
async function language(lang: Lang): Promise<Extension> {
  switch (lang) {
    case 'json': return (await import('@codemirror/lang-json')).json();
    case 'yaml': return (await import('@codemirror/lang-yaml')).yaml();
    case 'xml': return (await import('@codemirror/lang-xml')).xml();
    case 'html': return (await import('@codemirror/lang-html')).html();
    case 'css': return (await import('@codemirror/lang-css')).css();
    case 'js': return (await import('@codemirror/lang-javascript')).javascript({ jsx: true });
    case 'ts': return (await import('@codemirror/lang-javascript')).javascript({ typescript: true, jsx: true });
    case 'python': return (await import('@codemirror/lang-python')).python();
    case 'markdown': return (await import('@codemirror/lang-markdown')).markdown();
    case 'sql': return (await import('@codemirror/lang-sql')).sql();
    case 'shell': return legacy((await import('@codemirror/legacy-modes/mode/shell')).shell as StreamParser<unknown>);
    case 'toml': return legacy((await import('@codemirror/legacy-modes/mode/toml')).toml as StreamParser<unknown>);
    case 'env': case 'ini': return legacy((await import('@codemirror/legacy-modes/mode/properties')).properties as StreamParser<unknown>);
    case 'dockerfile': return legacy((await import('@codemirror/legacy-modes/mode/dockerfile')).dockerFile as StreamParser<unknown>);
    default: return [];
  }
}

// ---------------------------------------------------------------- theme (CSS tokens: follows light / dark at once)
const theme = EditorView.theme({
  '&': { color: 'var(--text)', backgroundColor: 'var(--surface)', height: '100%', fontSize: '12.5px' },
  '.cm-scroller': { fontFamily: 'var(--mono)', lineHeight: '1.6' },
  '.cm-content': { caretColor: 'var(--accent)', padding: '6px 0' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--accent)' },
  '&.cm-focused': { outline: 'none' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': { backgroundColor: 'var(--syn-sel) !important' },
  '.cm-gutters': { backgroundColor: 'var(--surface-2)', color: 'var(--text-faint)', border: 'none', borderRight: '1px solid var(--border)' },
  '.cm-activeLine': { backgroundColor: 'var(--accent-soft)' },
  '.cm-activeLineGutter': { backgroundColor: 'var(--accent-soft)', color: 'var(--text-muted)' },
  '.cm-foldPlaceholder': { backgroundColor: 'var(--surface-3)', border: '1px solid var(--border)', color: 'var(--text-muted)' },
  '.cm-matchingBracket': { backgroundColor: 'var(--accent-soft)', outline: '1px solid var(--accent-line)' },
  '.cm-searchMatch': { backgroundColor: 'var(--highlight-soft)', outline: '1px solid var(--highlight-line)' },
  '.cm-selectionMatch': { backgroundColor: 'var(--accent-soft)' },
  '.cm-panels': { backgroundColor: 'var(--surface-2)', color: 'var(--text)', borderColor: 'var(--border)' },
  '.cm-panel input, .cm-panel button': { fontSize: '12px' },
  '.cm-tooltip': { backgroundColor: 'var(--surface-2)', border: '1px solid var(--border-strong)', color: 'var(--text)' },
  '.cm-diagnostic-error': { borderLeftColor: 'var(--danger)' },
  '.cm-lintRange-error': { backgroundImage: 'none', textDecoration: 'underline wavy var(--danger)' },
});
const highlight = HighlightStyle.define([
  { tag: [t.propertyName, t.attributeName, t.definition(t.propertyName)], color: 'var(--syn-key)' },
  { tag: [t.string, t.special(t.string), t.regexp], color: 'var(--syn-string)' },
  { tag: [t.number, t.integer, t.float], color: 'var(--syn-number)' },
  { tag: [t.bool, t.atom], color: 'var(--syn-bool)' },
  { tag: [t.null], color: 'var(--syn-null)' },
  { tag: [t.comment, t.lineComment, t.blockComment, t.docComment], color: 'var(--syn-comment)', fontStyle: 'italic' },
  { tag: [t.keyword, t.operatorKeyword, t.modifier, t.controlKeyword], color: 'var(--syn-keyword)' },
  { tag: [t.tagName, t.typeName, t.className], color: 'var(--syn-tag)' },
  { tag: [t.heading], color: 'var(--syn-key)', fontWeight: '700' },
  { tag: [t.link, t.url], color: 'var(--accent-text)', textDecoration: 'underline' },
  { tag: [t.invalid], color: 'var(--danger-text)' },
]);

// ---------------------------------------------------------------- lint: JSON / YAML parse errors
function lintFor(lang: Lang): Extension {
  if (lang !== 'json' && lang !== 'yaml') return [];
  return linter((view) => {
    const text = view.state.doc.toString();
    if (!text.trim()) return [];
    const r = structParse(lang, text);
    if (r.ok) return [];
    if (lang === 'json' && !r.error.line) {             // V8 gave no position: the grammar's first error node has it
      let at = -1;
      syntaxTree(view.state).iterate({ enter: (n) => { if (at < 0 && n.type.isError) at = n.from; return at < 0; } });
      const p = at < 0 ? view.state.doc.length : at;
      return [{ from: Math.max(0, p - 1), to: Math.min(view.state.doc.length, p + 1), severity: 'error', message: r.error.msg }];
    }
    const line = view.state.doc.line(Math.min(Math.max(1, r.error.line), view.state.doc.lines));
    const from = Math.min(line.from + Math.max(0, r.error.col - 1), line.to);
    const d: Diagnostic = { from, to: Math.min(line.to, from + 1) || from, severity: 'error', message: r.error.msg };
    return [d];
  }, { delay: 400 });
}

export async function createEditor(host: HTMLElement, o: EditorOpts): Promise<EditorHandle> {
  const ro = new Compartment();
  const lang = await language(o.lang);
  const save = (): boolean => { o.onSave?.(); return true; };
  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: o.text,
      extensions: [
        lineNumbers(), highlightActiveLineGutter(), foldGutter(), drawSelection(), history(), indentOnInput(), bracketMatching(),
        closeBrackets(), autocompletion(), rectangularSelection(), highlightActiveLine(), highlightSelectionMatches(), search({ top: true }),
        lintGutter(), lintFor(o.lang), lang, theme, syntaxHighlighting(highlight), EditorView.lineWrapping,
        keymap.of([{ key: 'Mod-s', run: save, preventDefault: true }, indentWithTab, ...closeBracketsKeymap, ...defaultKeymap,
          ...searchKeymap, ...historyKeymap, ...foldKeymap, ...completionKeymap]),
        ro.of([EditorState.readOnly.of(!!o.readOnly), EditorView.editable.of(!o.readOnly)]),
        EditorView.updateListener.of((u) => { if (u.docChanged) o.onChange?.(u.state.doc.toString()); }),
      ],
    }),
  });
  return {
    view,
    getText: () => view.state.doc.toString(),
    setText: (text) => {
      if (text === view.state.doc.toString()) return;
      const top = view.scrollDOM.scrollTop;
      // one undo step of its own: Ctrl+Z brings back exactly the text before (not merged with typing just before)
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, annotations: isolateHistory.of('full'), userEvent: 'input.replace' });
      view.scrollDOM.scrollTop = top;
    },
    setReadOnly: (b) => view.dispatch({ effects: ro.reconfigure([EditorState.readOnly.of(b), EditorView.editable.of(!b)]) }),
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}

// ---------------------------------------------------------------- structured JSON / YAML
function lineCol(text: string, pos: number): { line: number; col: number } {
  const before = text.slice(0, Math.max(0, pos));
  const nl = before.lastIndexOf('\n');
  return { line: before.split('\n').length, col: pos - nl };
}

export function structParse(lang: 'json' | 'yaml', text: string): ParseResult {
  if (lang === 'json') {
    try { return { ok: true, value: JSON.parse(text) as unknown }; } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      const lc = /line (\d+) column (\d+)/.exec(msg);
      const pos = /position (\d+)/.exec(msg);
      const at = lc ? { line: Number(lc[1]), col: Number(lc[2]) } : pos ? lineCol(text, Number(pos[1])) : { line: 0, col: 0 };
      return { ok: false, error: { msg: msg.replace(/^JSON\.parse: /, ''), ...at } };
    }
  }
  const doc = parseDocument(text, { prettyErrors: false, keepSourceTokens: false });
  const err = doc.errors[0];
  if (err) {
    const at = err.linePos?.[0] ?? lineCol(text, err.pos[0]);
    return { ok: false, error: { msg: err.message.split('\n')[0] ?? err.message, line: at.line, col: at.col } };
  }
  return { ok: true, value: doc.toJS({ maxAliasCount: 1000 }) as unknown };
}

/** The indent unit a JSON text uses ('  ' when it is minified or unknown, so a format expands it readably). */
function jsonIndent(text: string): string | number {
  const m = /^[[{]\s*\n([ \t]+)\S/.exec(text.trimStart());
  return m?.[1] ?? 2;
}

function getAt(root: unknown, path: Path): unknown {
  let v = root;
  for (const k of path) v = (v as Record<string | number, unknown> | null)?.[k];
  return v;
}

function jsonEdit(text: string, op: StructOp): string {
  const root = JSON.parse(text) as unknown;
  const parentPath = op.op === 'add' ? op.path : op.path.slice(0, -1);
  const last = op.path[op.path.length - 1];
  const parent = getAt(root, parentPath);
  let out = root;
  if (op.op === 'set' && !op.path.length) out = op.value;
  else if (op.op === 'set') (parent as Record<string | number, unknown>)[last as string] = op.value;
  else if (op.op === 'delete') {
    if (Array.isArray(parent)) parent.splice(Number(last), 1); else delete (parent as Record<string, unknown>)[String(last)];
  } else if (op.op === 'rename') {
    const o = parent as Record<string, unknown>;
    if (op.key in o && op.key !== last) throw new Error(`“${op.key}” is already a key here`);
    // rebuild to keep the key's position
    const next: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(o)) next[k === last ? op.key : k] = v;
    for (const k of Object.keys(o)) delete o[k];
    Object.assign(o, next);
  } else if (op.op === 'add') {
    if (Array.isArray(parent)) parent.push(op.value);
    else if (parent && typeof parent === 'object') {
      const k = op.key ?? '';
      if (!k) throw new Error('give the new entry a key');
      if (k in parent) throw new Error(`“${k}” is already a key here`);
      (parent as Record<string, unknown>)[k] = op.value;
    } else throw new Error('only an object or a list can get a new entry');
  }
  const nl = text.includes('\r\n') ? '\r\n' : '\n';
  const s = JSON.stringify(out, null, jsonIndent(text));
  return (nl === '\n' ? s : s.replace(/\n/g, nl)) + (/\n\s*$/.test(text) ? nl : '');
}

/** The tree's path uses the keys as the JS value shows them ('0' for `0: cat`); the YAML nodes may hold numbers (or
 *  booleans): map each step to the node's own key so getIn / setIn find it. */
function nodePath(doc: Document, path: Path): Path {
  const out: Path = [];
  let node: unknown = doc.contents;
  for (const seg of path) {
    let k: string | number = seg;
    if (isMap(node)) {
      const pair = node.items.find((p) => String(isScalar(p.key) ? p.key.value : p.key) === String(seg));
      const kv = pair && isScalar(pair.key) ? pair.key.value : undefined;
      if (typeof kv === 'number' || typeof kv === 'string') k = kv;
      node = pair?.value;
    } else if (isSeq(node)) {
      k = Number(seg);
      node = node.items[k];
    } else node = undefined;
    out.push(k);
  }
  return out;
}

/** A new key in a map whose keys are all numbers stays a number when it reads as one (names: {0: cat, 1: dog}). */
function newKey(m: YAMLMap, key: string): string | number {
  const nums = m.items.length > 0 && m.items.every((p) => isScalar(p.key) && typeof p.key.value === 'number');
  return nums && /^-?\d+$/.test(key) ? Number(key) : key;
}

function yamlEdit(text: string, op0: StructOp): string {
  const doc: Document = parseDocument(text);
  if (doc.errors.length) throw new Error(doc.errors[0]?.message ?? 'YAML error');
  const op = { ...op0, path: nodePath(doc, op0.path) } as StructOp;
  if (op.op === 'set') {
    if (!op.path.length) doc.contents = doc.createNode(op.value) as typeof doc.contents;
    else {
      const old = doc.getIn(op.path, true);
      if (isScalar(old) && (op.value === null || typeof op.value !== 'object')) old.value = op.value;   // keeps comments / style
      else doc.setIn(op.path, doc.createNode(op.value));
    }
  } else if (op.op === 'delete') {
    // a comment above the removed entry (often the file's header) moves to the next one instead of going with it
    const parent = op.path.length > 1 ? doc.getIn(op.path.slice(0, -1), true) : doc.contents;
    const last = op.path[op.path.length - 1];
    if (isMap(parent)) {
      const i = parent.items.findIndex((p) => (isScalar(p.key) ? p.key.value : p.key) === last);
      const cur = parent.items[i], next = parent.items[i + 1];
      const c = cur && isScalar(cur.key) ? cur.key.commentBefore : undefined;
      if (c) {
        if (next && isScalar(next.key)) next.key.commentBefore = next.key.commentBefore ? `${c}\n${next.key.commentBefore}` : c;
        else if (parent === doc.contents) doc.commentBefore = doc.commentBefore ? `${doc.commentBefore}\n${c}` : c;
      }
    }
    doc.deleteIn(op.path);
  }
  else if (op.op === 'rename') {
    const parent = op.path.length > 1 ? doc.getIn(op.path.slice(0, -1), true) : doc.contents;
    if (!isMap(parent)) throw new Error('only a key of a mapping can be renamed');
    const last = op.path[op.path.length - 1];
    const m = parent as YAMLMap;
    if (m.items.some((p) => String(isScalar(p.key) ? p.key.value : p.key) === op.key)) throw new Error(`“${op.key}” is already a key here`);
    const pair = m.items.find((p) => (isScalar(p.key) ? p.key.value : p.key) === last);
    if (!pair) throw new Error(`no key “${String(last)}”`);
    const nk = newKey(m, op.key);
    if (isScalar(pair.key)) pair.key.value = nk; else pair.key = new Scalar(nk);
  } else if (op.op === 'add') {
    const parent = op.path.length ? doc.getIn(op.path, true) : doc.contents;
    if (isSeq(parent)) parent.add(doc.createNode(op.value));
    else if (isMap(parent)) {
      const k = op.key ?? '';
      if (!k) throw new Error('give the new entry a key');
      const nk = newKey(parent, k);
      if (parent.has(nk)) throw new Error(`“${k}” is already a key here`);
      parent.set(nk, doc.createNode(op.value));
    } else if (parent == null && !op.path.length) {
      doc.contents = doc.createNode(op.key ? { [op.key]: op.value } : [op.value]) as typeof doc.contents;
    } else throw new Error('only a mapping or a list can get a new entry');
  }
  return doc.toString({ lineWidth: 0 });
}

/** Apply one structured edit to the text of a JSON / YAML file -> the new text. Throws on a bad path / key. */
export function structEdit(lang: 'json' | 'yaml', text: string, op: StructOp): string {
  return lang === 'json' ? jsonEdit(text, op) : yamlEdit(text, op);
}

/** Pretty-print: JSON with its indent (2 spaces for a minified file), YAML through the yaml printer (comments kept). */
export function formatText(lang: Lang, text: string): string {
  if (lang === 'json') {
    const ind = jsonIndent(text);
    return JSON.stringify(JSON.parse(text), null, ind) + '\n';
  }
  if (lang === 'yaml') {
    const doc = parseDocument(text);
    if (doc.errors.length) throw new Error(doc.errors[0]?.message ?? 'YAML error');
    return doc.toString({ lineWidth: 0 });
  }
  return text;
}
