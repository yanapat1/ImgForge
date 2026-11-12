// The file view: double-clicking a text-like file (txt, yaml, json, .env, js, py, html, …) opens it over the centre
// pane. Structured | Text toggle: JSON and YAML show as a collapsible tree (type-coloured values, counts on closed
// nodes, inline edit of values, rename keys, add / remove entries, copy path; YAML comments and order are kept),
// .env / .ini / .properties as a key / value table (values masked until revealed); Text is CodeMirror (static/js/
// vendor/editor.js, loaded the first time a file opens). Save (Ctrl/Cmd+S) goes through PUT …/text with the mtime it
// was read at: someone else's change in between is a conflict (reload theirs / overwrite); a saved file can be
// undone from the toast. Unsaved changes are asked about before leaving. Files over 512 KB are parsed in a worker.
//
//   openFile(bid, path)   leaveFile() -> false when the user keeps editing   fileOpen()   peek(bid, path) (details)

import { errText, isStatus } from '../core/api.js';
import { copyText, esc, icon } from '../core/dom.js';
import { actionToast, toast } from '../core/toast.js';
import { confirmDialog, openDialog } from '../views/dialog.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { showRetag, undoOp } from './ops.js';
import { baseName, bucketOf, D, hooks } from './state.js';
import type { EditorHandle, EditorModule, Lang, Path, StructOp } from '../types-editor.js';

const MAX = 8 * 1024 * 1024;            // the server's read cap
const WORKER_AT = 512 * 1024;
const PAGE = 500;                       // tree children shown per "show more"
type Mode = 'struct' | 'text';
type Kv = { line: number; key: string; value: string; section: string; quote: string; prefix: string };

interface FileState {
  bid: string; path: string; name: string; lang: Lang;
  orig: string; cur: string; mtime: string | null; editable: boolean; reason: string; newline: string;
  mode: Mode; handle: EditorHandle | null;
  /** the structured view's parse of `cur` (null = not parsed yet / stale), its error */
  value: unknown; parsed: boolean; perr: string | null;
  open: Set<string>; more: Map<string, number>; reveal: Set<number>; saving: boolean;
}

let F: FileState | null = null;
let V: EditorModule | null = null;
let vendor: Promise<EditorModule> | null = null;

const VENDOR_URL = new URL('../vendor/editor.js', import.meta.url).href;
const WORKER_URL = new URL('../vendor/parse-worker.js', import.meta.url).href;

function loadVendor(): Promise<EditorModule> {
  vendor ??= (import(VENDOR_URL) as Promise<EditorModule>).then((m) => (V = m));
  return vendor;
}

const structLang = (l: Lang): 'json' | 'yaml' | 'kv' | null => (l === 'json' || l === 'yaml' ? l : l === 'env' || l === 'ini' ? 'kv' : null);
const pkey = (p: Path): string => JSON.stringify(p);
const dirty = (): boolean => !!F && F.cur !== F.orig;
const $f = (): HTMLElement | null => document.getElementById('bk-fview');

export const fileOpen = (): boolean => !!F;

/** Close the file view; false (and it stays open) when it has unsaved changes the user wants to keep. */
export async function leaveFile(): Promise<boolean> {
  if (!F) return true;
  if (dirty()) {
    const name = F.name;
    if (!(await confirmDialog({ title: `Leave ${name} without saving?`, text: 'Its unsaved changes are lost.', ok: 'Leave', danger: true }))) return false;
    if (!F) return true;                               // closed meanwhile
  }
  closeFile();
  return true;
}

/** Close the file view now, unsaved changes or not (callers ask first through leaveFile / fileDirty). */
export function closeFile(): void {
  if (!F) return;
  F.handle?.destroy();
  F = null;
  $f()?.remove();
  window.removeEventListener('beforeunload', onUnload);
}

export const fileDirty = (): boolean => dirty();

function onUnload(e: BeforeUnloadEvent): void { if (dirty()) { e.preventDefault(); e.returnValue = ''; } }

// ---------------------------------------------------------------- open
export async function openFile(bid: string, path: string): Promise<void> {
  if (!(await leaveFile())) return;
  const main = document.getElementById('bk-main');
  if (!main) return;
  const host = document.createElement('section');
  host.id = 'bk-fview';
  host.className = 'bk-fview';
  host.setAttribute('aria-label', `File ${baseName(path)}`);
  host.innerHTML = `<div class="bk-empty"><span class="spin" aria-hidden="true"></span>Opening ${esc(baseName(path))}…</div>`;
  main.appendChild(host);
  host.addEventListener('keydown', onKeys);
  host.addEventListener('click', onClick);
  host.addEventListener('dblclick', onDbl);
  window.addEventListener('beforeunload', onUnload);
  try {
    const [r, mod] = await Promise.all([api.readText(bid, path, MAX), loadVendor()]);
    if ($f() !== host) return;                          // closed / another file meanwhile
    const lang = mod.detectLang(baseName(path), r.text);
    const b = bucketOf(bid);
    const st = structLang(lang);
    F = {
      bid, path, name: baseName(path), lang, orig: r.text, cur: r.text, mtime: r.mtime_ns, newline: r.newline,
      editable: r.editable && !!b?.writable, reason: r.reason ?? (b?.writable ? '' : 'this bucket is read-only'),
      mode: st ? 'struct' : 'text', handle: null, value: null, parsed: false, perr: null,
      open: new Set([pkey([])]), more: new Map(), reveal: new Set(), saving: false,
    };
    paint();
    if (F.mode === 'struct') await parse();
    if (F && F.mode === 'struct' && F.perr && structLang(F.lang) !== 'kv') F.mode = 'text';   // broken JSON: fix it as text
    paint();
  } catch (e) {
    if ($f() === host) {
      host.innerHTML = `<div class="bk-empty">${icon('alert')}<b>Could not open ${esc(baseName(path))}</b><span class="muted">${esc(errText(e))}</span>` +
        `<button type="button" data-fx="close">${icon('x')}Close</button></div>`;
      F = null;
    }
  }
}

/** Parse `F.cur` for the structured view (in a worker when it is big). */
async function parse(): Promise<void> {
  const f = F;
  if (!f || !V) return;
  const sl = structLang(f.lang);
  if (sl === 'kv' || !sl) { f.parsed = true; f.perr = null; return; }
  const text = f.cur;
  let r;
  if (text.length > WORKER_AT && typeof Worker !== 'undefined') {
    r = await new Promise<ReturnType<EditorModule['structParse']>>((ok) => {
      const w = new Worker(WORKER_URL, { type: 'module' });
      w.onmessage = (e: MessageEvent<ReturnType<EditorModule['structParse']>>): void => { ok(e.data); w.terminate(); };
      w.onerror = (): void => { ok(V ? V.structParse(sl, text) : { ok: false, error: { msg: 'parser failed', line: 1, col: 1 } }); w.terminate(); };
      w.postMessage({ id: 1, lang: sl, text });
    });
  } else r = V.structParse(sl, text);
  if (F !== f || f.cur !== text) return;
  f.parsed = true;
  if (r.ok) { f.value = r.value; f.perr = null; } else f.perr = r.error.line ? `${r.error.msg} (line ${r.error.line}, column ${r.error.col})` : r.error.msg;
}

// ---------------------------------------------------------------- paint
function bar(f: FileState): string {
  const b = bucketOf(f.bid);
  const parts = f.path.split('/');
  const crumbs = parts.slice(0, -1).map((p, i) => `<button type="button" class="lnk" data-fx="crumb" data-path="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</button><span class="sep">/</span>`).join('');
  const st = structLang(f.lang);
  const seg = st ? `<div class="segmented" role="tablist" aria-label="View">` +
    `<button type="button" role="tab" data-fx="mode" data-m="struct" aria-selected="${f.mode === 'struct'}" class="view-btn sm${f.mode === 'struct' ? ' active' : ''}">${icon(st === 'kv' ? 'list' : 'layers')}${st === 'kv' ? 'Table' : 'Tree'}</button>` +
    `<button type="button" role="tab" data-fx="mode" data-m="text" aria-selected="${f.mode === 'text'}" class="view-btn sm${f.mode === 'text' ? ' active' : ''}">${icon('text')}Text</button></div>` : '';
  const canFormat = (f.lang === 'json' || f.lang === 'yaml') && f.editable;
  return `<div class="bk-fbar"><button type="button" class="icon-btn ghost sm" data-fx="close" aria-label="Close the file" title="Close (back to the folder)">${icon('chev-l')}</button>` +
    `<span class="bk-fcrumb"><button type="button" class="lnk" data-fx="crumb" data-path="">${esc(b?.name ?? 'bucket')}</button><span class="sep">/</span>${crumbs}<b>${esc(f.name)}</b>` +
    `<i class="bk-dirty${dirty() ? ' on' : ''}" title="Unsaved changes" aria-label="${dirty() ? 'Unsaved changes' : ''}"></i></span>` +
    `<span class="bk-flang">${esc(LANG_LABEL[f.lang] ?? f.lang)}</span>` +
    (f.editable ? '' : `<span class="bk-ro" title="${esc(f.reason)}">${icon('lock')}Read-only${f.reason ? `: ${esc(f.reason)}` : ''}</span>`) +
    `<span class="grow"></span>${seg}` +
    (canFormat ? `<button type="button" class="ghost sm" data-fx="format" title="Pretty-print">${icon('sparkle')}Format</button>` : '') +
    (f.editable ? `<button type="button" class="ghost sm" data-fx="revert"${dirty() ? '' : ' disabled'}>${icon('undo')}Revert</button>` +
      `<button type="button" class="primary sm" data-fx="save"${dirty() && !f.saving ? '' : ' disabled'} title="Save (Ctrl+S)">${f.saving ? '<span class="spin sm"></span>' : icon('check')}Save</button>` : '') +
    '</div>';
}

const LANG_LABEL: Partial<Record<Lang, string>> = {
  json: 'JSON', yaml: 'YAML', env: '.env', ini: 'INI', toml: 'TOML', xml: 'XML', html: 'HTML', css: 'CSS', js: 'JavaScript',
  ts: 'TypeScript', python: 'Python', markdown: 'Markdown', sql: 'SQL', shell: 'Shell', dockerfile: 'Dockerfile', csv: 'CSV', text: 'Text',
};

function paint(): void {
  const host = $f();
  const f = F;
  if (!host || !f) return;
  let barEl = host.querySelector('.bk-fbar');
  if (!barEl) {
    host.innerHTML = `${bar(f)}<div class="bk-fbody"><div class="bk-fstruct" tabindex="-1"></div><div class="bk-ftext"></div></div>`;
    barEl = host.querySelector('.bk-fbar');
  } else barEl.outerHTML = bar(f);
  const sEl = host.querySelector<HTMLElement>('.bk-fstruct');
  const tEl = host.querySelector<HTMLElement>('.bk-ftext');
  if (!sEl || !tEl) return;
  sEl.hidden = f.mode !== 'struct';
  tEl.hidden = f.mode !== 'text';
  if (f.mode === 'text') { void ensureText(tEl); return; }
  const top = sEl.scrollTop;
  sEl.innerHTML = structHTML(f);
  sEl.scrollTop = top;
}

/** Repaint just the bar (dirty dot, Save state) while typing in the text editor. */
function paintBar(): void {
  const el = $f()?.querySelector('.bk-fbar');
  if (el && F) el.outerHTML = bar(F);
}

async function ensureText(tEl: HTMLElement): Promise<void> {
  const f = F;
  if (!f || !V) return;
  if (f.handle) { f.handle.setText(f.cur); return; }
  tEl.innerHTML = '';
  f.handle = await V.createEditor(tEl, {
    text: f.cur, lang: f.lang, readOnly: !f.editable,
    onChange: (t) => { if (F !== f) return; const was = dirty(); f.cur = t; f.parsed = false; if (was !== dirty()) paintBar(); },
    onSave: () => { void save(); },
  });
  if (F === f) f.handle.focus();
}

// ---------------------------------------------------------------- structured: tree (JSON / YAML)
const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const typeOf = (v: unknown): string => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);

function scalarHTML(v: unknown): string {
  const t = typeOf(v);
  const s = t === 'string' ? JSON.stringify(v) : String(v);
  return `<span class="v t-${t}">${esc(s.length > 400 ? `${s.slice(0, 400)}…` : s)}</span>`;
}

function rowHTML(f: FileState, k: string | number | null, v: unknown, path: Path, depth: number): string {
  const pk = pkey(path);
  const box = Array.isArray(v) || isObj(v);
  const open = box && f.open.has(pk);
  const n = Array.isArray(v) ? v.length : isObj(v) ? Object.keys(v).length : 0;
  const ed = f.editable;
  const keyEl = k === null ? '<span class="k root">root</span>'
    : typeof k === 'number' ? `<span class="k idx">${k}</span>`
      : `<span class="k"${ed ? ' data-fx="rename" title="Double-click to rename"' : ''}>${esc(k)}</span>`;
  const val = box ? `<span class="sum">${Array.isArray(v) ? `[${n}]` : `{${n}}`}${open ? '' : `<span class="faint"> ${n} item${n === 1 ? '' : 's'}</span>`}</span>`
    : `<span class="val"${ed ? ' data-fx="edit" title="Click to edit"' : ''}>${scalarHTML(v)}</span>`;
  const tools = `<span class="tools">` +
    (box && ed ? `<button type="button" class="icon-btn ghost xs" data-fx="add" aria-label="Add an entry" title="Add an entry">${icon('plus')}</button>` : '') +
    `<button type="button" class="icon-btn ghost xs" data-fx="copy-path" aria-label="Copy the path" title="Copy the path">${icon('copy')}</button>` +
    (k !== null && ed ? `<button type="button" class="icon-btn ghost xs danger-ghost" data-fx="del" aria-label="Remove" title="Remove">${icon('trash')}</button>` : '') + '</span>';
  let html = `<div class="bk-tr" role="treeitem" aria-level="${depth + 1}"${box ? ` aria-expanded="${open}"` : ''} style="--d:${depth}" data-p="${esc(pk)}">` +
    (box ? `<button type="button" class="tw${open ? ' open' : ''}" data-fx="toggle" aria-label="${open ? 'Collapse' : 'Expand'}">${icon('chev-r')}</button>` : '<span class="tw"></span>') +
    `${keyEl}<span class="colon">:</span>${val}${tools}</div>`;
  if (open) {
    const entries: Array<[string | number, unknown]> = Array.isArray(v) ? v.map((x, i) => [i, x]) : Object.entries(v as Record<string, unknown>);
    const shown = f.more.get(pk) ?? PAGE;
    html += entries.slice(0, shown).map(([ck, cv]) => rowHTML(f, ck, cv, [...path, ck], depth + 1)).join('');
    if (entries.length > shown) {
      html += `<div class="bk-tr more" style="--d:${depth + 1}" data-p="${esc(pk)}"><span class="tw"></span><button type="button" class="lnk" data-fx="more">Show ${Math.min(PAGE, entries.length - shown)} more of ${entries.length - shown}</button></div>`;
    }
  }
  return html;
}

function structHTML(f: FileState): string {
  const sl = structLang(f.lang);
  if (sl === 'kv') return kvHTML(f);
  if (!f.parsed) return '<div class="bk-empty"><span class="spin" aria-hidden="true"></span>Reading the structure…</div>';
  if (f.perr) {
    return `<div class="bk-empty">${icon('alert')}<b>Not valid ${esc(LANG_LABEL[f.lang] ?? '')}</b><span class="muted">${esc(f.perr)}</span>` +
      `<button type="button" data-fx="mode" data-m="text">${icon('text')}Fix it in Text</button></div>`;
  }
  return `<div class="bk-jtree" role="tree" aria-label="${esc(f.name)}">${rowHTML(f, null, f.value, [], 0)}</div>`;
}

const pathOf = (el: Element): Path | null => {
  const r = el.closest<HTMLElement>('[data-p]');
  try { return r ? (JSON.parse(r.dataset['p'] ?? '') as Path) : null; } catch { return null; }
};

const valueAt = (root: unknown, p: Path): unknown => p.reduce<unknown>((v, k) => (v as Record<string | number, unknown> | null)?.[k], root);

/** Apply a structured edit to the text, then re-parse and repaint (the text editor follows). */
async function apply(op: StructOp): Promise<void> {
  const f = F;
  if (!f || !V) return;
  const sl = structLang(f.lang);
  if (sl !== 'json' && sl !== 'yaml') return;
  try {
    f.cur = V.structEdit(sl, f.cur, op);
  } catch (e) { toast(errText(e), 'err'); return; }
  f.handle?.setText(f.cur);
  await parse();
  paint();
}

/** "42" stays a number, "true" a boolean, "null" null when the old value had that type; anything else is text. */
function coerce(input: string, old: unknown): unknown {
  const s = input.trim();
  if (typeof old === 'number' && s !== '' && Number.isFinite(Number(s))) return Number(s);
  if (typeof old === 'boolean' && (s === 'true' || s === 'false')) return s === 'true';
  if (old === null && s === 'null') return null;
  if (typeof old !== 'string') {
    try { return JSON.parse(s) as unknown; } catch { /* plain text */ }
  }
  return input;
}

/** A typed value from the Add dialog: JSON when it parses ({}, [], 3, true, "x"), else text. */
function literal(s: string): unknown {
  try { return JSON.parse(s) as unknown; } catch { return s; }
}

function inlineEdit(span: HTMLElement, initial: string, done: (v: string) => void): void {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'txt bk-tin';
  input.value = initial;
  input.spellcheck = false;
  span.replaceChildren(input);
  input.focus();
  input.select();
  let finished = false;
  const end = (ok: boolean): void => {
    if (finished) return;
    finished = true;
    if (ok && input.value !== initial) done(input.value); else paint();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); end(true); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); end(false); }
  });
  input.addEventListener('blur', () => end(true));
}

function addDialog(path: Path, parent: unknown): void {
  const list = Array.isArray(parent);
  void openDialog({
    title: list ? 'Add an item' : 'Add a key', ok: 'Add', focus: list ? '#bk-av' : '#bk-ak',
    body: `<div class="bk-send">${list ? '' : '<label class="fld"><span>Key</span><input type="text" class="txt" id="bk-ak" autocomplete="off" spellcheck="false"></label>'}` +
      `<label class="fld"><span>Value</span><input type="text" class="txt" id="bk-av" autocomplete="off" spellcheck="false" placeholder='text, 42, true, null, {} or []'></label>` +
      '<p class="hint">A value that reads as JSON (a number, true / false, null, {} or []) keeps that type; anything else is text.</p></div>',
    onOk: async (r) => {
      const k = r.querySelector<HTMLInputElement>('#bk-ak')?.value.trim() ?? '';
      const v = r.querySelector<HTMLInputElement>('#bk-av')?.value ?? '';
      if (!list && !k) return 'Give the new entry a key';
      if (!list && isObj(parent) && k in parent) return `“${k}” is already a key here`;
      if (F) F.open.add(pkey(path));
      await apply(list ? { op: 'add', path, value: literal(v) } : { op: 'add', path, key: k, value: literal(v) });
      return null;
    },
  });
}

// ---------------------------------------------------------------- structured: key / value (.env, .ini, .properties)
const SECRETISH = /pass|secret|token|key|auth|cred|private/i;

function kvParse(text: string, env: boolean): Kv[] {
  const out: Kv[] = [];
  let section = '';
  text.split('\n').forEach((raw, i) => {
    const line = raw.replace(/\r$/, '');
    const s = line.trim();
    if (!s || s.startsWith('#') || s.startsWith(';')) return;
    const sec = /^\[([^\]]+)\]$/.exec(s);
    if (sec) { section = sec[1] ?? ''; return; }
    const m = env ? /^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*)$/.exec(line) : /^(\s*)([^=:]+?)\s*[=:]\s*(.*)$/.exec(line);
    if (!m) return;
    let v = m[3] ?? '';
    let quote = '';
    const q = /^(['"])(.*)\1\s*(#.*)?$/.exec(v);
    if (q) { quote = q[1] ?? ''; v = q[2] ?? ''; } else if (env) v = v.replace(/\s+#.*$/, '');
    out.push({ line: i, key: m[2] ?? '', value: v, section, quote, prefix: m[1] ?? '' });
  });
  return out;
}

function kvHTML(f: FileState): string {
  const env = f.lang === 'env';
  const rows = kvParse(f.cur, env);
  const ed = f.editable;
  let sec: string | null = null;
  const body = rows.map((r) => {
    const head = !env && r.section !== sec ? `<tr class="sec"><th colspan="4">[${esc(r.section || 'no section')}]</th></tr>` : '';
    sec = r.section;
    const masked = (env || SECRETISH.test(r.key)) && !f.reveal.has(r.line);
    const val = masked ? `<span class="mask" aria-label="hidden value">${'•'.repeat(Math.min(12, Math.max(6, r.value.length)))}</span>` : `<span class="v t-string">${esc(r.value)}</span>`;
    return `${head}<tr data-l="${r.line}"><td class="k mono">${esc(r.key)}</td>` +
      `<td class="val mono"${ed && !masked ? ' data-fx="kv-edit" title="Click to edit"' : ''}>${val || '<span class="faint">empty</span>'}</td>` +
      `<td class="w"><button type="button" class="icon-btn ghost xs" data-fx="kv-reveal" aria-label="${masked ? 'Show' : 'Hide'} the value" title="${masked ? 'Show' : 'Hide'}">${icon(masked ? 'eye' : 'eye-off')}</button></td>` +
      `<td class="w">${ed ? `<button type="button" class="icon-btn ghost xs danger-ghost" data-fx="kv-del" aria-label="Remove ${esc(r.key)}" title="Remove">${icon('trash')}</button>` : ''}</td></tr>`;
  }).join('');
  return `<div class="bk-kv"><table><thead><tr><th>Key</th><th>Value</th><th class="w"></th><th class="w"></th></tr></thead><tbody>${body ||
    '<tr><td colspan="4" class="muted">No keys yet.</td></tr>'}</tbody></table>` +
    (ed ? `<button type="button" class="ghost sm" data-fx="kv-add">${icon('plus')}Add a key</button>` : '') +
    (env ? '<p class="hint">Values are hidden until you show them. Comments and blank lines stay as they are.</p>' : '') + '</div>';
}

function kvSetLine(line: number, next: string | null): void {
  const f = F;
  if (!f) return;
  const nl = f.newline === '\r\n' ? '\r\n' : '\n';
  const lines = f.cur.split(/\r?\n/);
  if (next === null) lines.splice(line, 1); else lines[line] = next;
  f.cur = lines.join(nl);
  f.handle?.setText(f.cur);
  paint();
}

const kvQuote = (v: string, q: string): string => (q ? `${q}${v}${q}` : /[\s#"']/.test(v) ? JSON.stringify(v) : v);

// ---------------------------------------------------------------- save / revert / format
async function save(force = false): Promise<void> {
  const f = F;
  if (!f || !f.editable || f.saving || (!dirty() && !force)) return;
  f.saving = true;
  paintBar();
  const text = f.cur;
  try {
    const r = await api.writeText(f.bid, f.path, text, force ? null : f.mtime);
    if (F !== f) return;
    f.orig = text; f.mtime = r.mtime_ns; f.saving = false;
    paintBar();
    const undo = r.undo_id;
    if (r.retagged) showRetag(f.bid, r.retagged, undo, `Saved ${f.name}`);
    else {
      actionToast({ kind: 'ok', title: `Saved ${f.name}`, actions: undo ? [{ label: 'Undo', icon: 'undo', primary: false, run: async () => {
        await undoOp(f.bid, undo);
        if (F === f) await reloadFile();
      } }] : [] });
    }
    void hooks.reload();
  } catch (e) {
    f.saving = false;
    paintBar();
    if (isStatus(e, 409)) { conflict(); return; }
    toast(`Not saved: ${errText(e)}`, 'err');
  }
}

function conflict(): void {
  const f = F;
  if (!f) return;
  let choice: 'theirs' | 'mine' = 'theirs';
  void openDialog({
    title: `${f.name} changed on disk`, ok: 'Continue', focus: 'input[name="bk-cf"]',
    body: '<div class="bk-send"><p>Someone (or something) changed this file after you opened it.</p>' +
      '<label class="check"><input type="radio" name="bk-cf" value="theirs" checked> <b>Reload theirs</b> <span class="faint">· your edits are dropped</span></label>' +
      '<label class="check"><input type="radio" name="bk-cf" value="mine"> <b>Overwrite with mine</b> <span class="faint">· their change goes to the trash (Undo brings it back)</span></label></div>',
    bind: (r) => r.addEventListener('change', (e) => { if (e.target instanceof HTMLInputElement) choice = e.target.value === 'mine' ? 'mine' : 'theirs'; }),
    onOk: async () => {
      if (choice === 'mine') await save(true); else await reloadFile();
      return null;
    },
  });
}

async function reloadFile(): Promise<void> {
  const f = F;
  if (!f) return;
  try {
    const r = await api.readText(f.bid, f.path, MAX);
    if (F !== f) return;
    f.orig = r.text; f.cur = r.text; f.mtime = r.mtime_ns; f.parsed = false;
    f.handle?.setText(r.text);
    await parse();
    paint();
  } catch (e) { toast(`Could not reload: ${errText(e)}`, 'err'); }
}

// ---------------------------------------------------------------- events
function onKeys(e: KeyboardEvent): void {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); void save(); }
}

function onClick(e: MouseEvent): void {
  const el = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-fx]') : null;
  const f = F;
  if (!el) return;
  const fx = el.dataset['fx'] ?? '';
  if (fx === 'close') { void leaveFile().then((ok) => { if (ok) hooks.render(); }); return; }
  if (!f) return;
  if (fx === 'crumb') { const p = el.dataset['path'] ?? ''; void leaveFile().then((ok) => { if (ok) hooks.navigate(f.bid, p); }); return; }
  if (fx === 'mode') {
    const m: Mode = el.dataset['m'] === 'text' ? 'text' : 'struct';
    if (m === f.mode) return;
    f.mode = m;
    if (m === 'struct' && !f.parsed) { paint(); void parse().then(paint); return; }
    paint();
    return;
  }
  if (fx === 'save') { void save(); return; }
  if (fx === 'revert') { f.cur = f.orig; f.handle?.setText(f.cur); f.parsed = false; void parse().then(paint); return; }
  if (fx === 'format') {
    try { f.cur = V ? V.formatText(f.lang, f.cur) : f.cur; } catch (err) { toast(`Cannot format: ${errText(err)}`, 'err'); return; }
    f.handle?.setText(f.cur); f.parsed = false; void parse().then(paint);
    return;
  }
  const p = pathOf(el);
  if (fx === 'toggle' && p) { const k = pkey(p); if (f.open.has(k)) f.open.delete(k); else f.open.add(k); paint(); return; }
  if (fx === 'more' && p) { const k = pkey(p); f.more.set(k, (f.more.get(k) ?? PAGE) + PAGE); paint(); return; }
  if (fx === 'copy-path' && p) {
    const s = p.length ? p.map((k, i) => (typeof k === 'number' ? `[${k}]` : /^[A-Za-z_$][\w$]*$/.test(k) ? `${i ? '.' : ''}${k}` : `[${JSON.stringify(k)}]`)).join('') : '(root)';
    void copyText(s).then((ok) => toast(ok ? `Copied ${s}` : s));
    return;
  }
  if (fx === 'del' && p) { void apply({ op: 'delete', path: p }); return; }
  if (fx === 'add' && p) { addDialog(p, valueAt(f.value, p)); return; }
  if (fx === 'edit' && p) {
    const old = valueAt(f.value, p);
    inlineEdit(el, typeof old === 'string' ? old : JSON.stringify(old), (v) => { void apply({ op: 'set', path: p, value: coerce(v, old) }); });
    return;
  }
  const line = Number(el.closest<HTMLElement>('[data-l]')?.dataset['l'] ?? -1);
  const row = kvParse(f.cur, f.lang === 'env').find((r) => r.line === line);
  if (fx === 'kv-reveal' && row) { if (f.reveal.has(line)) f.reveal.delete(line); else f.reveal.add(line); paint(); return; }
  if (fx === 'kv-del' && row) { kvSetLine(line, null); return; }
  if (fx === 'kv-edit' && row) {
    inlineEdit(el, row.value, (v) => kvSetLine(line, `${row.prefix}${row.key}${f.lang === 'env' ? '=' : ' = '}${kvQuote(v, row.quote)}`));
    return;
  }
  if (fx === 'kv-add') {
    void openDialog({
      title: 'Add a key', ok: 'Add', focus: '#bk-ak',
      body: '<div class="bk-send"><label class="fld"><span>Key</span><input type="text" class="txt mono" id="bk-ak" autocomplete="off" spellcheck="false"></label>' +
        '<label class="fld"><span>Value</span><input type="text" class="txt mono" id="bk-av" autocomplete="off" spellcheck="false"></label></div>',
      onOk: (r) => {
        const k = r.querySelector<HTMLInputElement>('#bk-ak')?.value.trim() ?? '';
        const v = r.querySelector<HTMLInputElement>('#bk-av')?.value ?? '';
        if (!k) return 'Give the key a name';
        if (f.lang === 'env' && !/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(k)) return 'A .env key is letters, digits and _ (not starting with a digit)';
        const nl = f.newline === '\r\n' ? '\r\n' : '\n';
        f.cur = `${f.cur.replace(/\s*$/, '')}${f.cur.trim() ? nl : ''}${k}${f.lang === 'env' ? '=' : ' = '}${kvQuote(v, '')}${nl}`;
        f.handle?.setText(f.cur);
        paint();
        return null;
      },
    });
  }
}

function onDbl(e: MouseEvent): void {
  const el = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-fx="rename"]') : null;
  const p = el ? pathOf(el) : null;
  if (!el || !p || !F) return;
  const old = String(p[p.length - 1] ?? '');
  inlineEdit(el, old, (k) => { if (k.trim()) void apply({ op: 'rename', path: p, key: k.trim() }); });
}

// ---------------------------------------------------------------- details: a short preview of one selected file
const peeks = new Map<string, string>();

/** Fill the details pane's #bk-peek with the file's first 30 lines (cached per file + mtime). */
export async function peek(bid: string, path: string, mtime: string | number | null): Promise<void> {
  const k = `${bid}\u0000${path}\u0000${mtime ?? ''}`;
  let txt = peeks.get(k);
  if (txt === undefined) {
    try {
      const r = await api.readText(bid, path, 8192);
      txt = r.text.split('\n').slice(0, 30).join('\n') + (r.truncated || r.text.split('\n').length > 30 ? '\n…' : '');
    } catch (e) { txt = `Cannot preview: ${errText(e)}`; }
    if (peeks.size > 50) peeks.clear();
    peeks.set(k, txt);
  }
  const el = document.getElementById('bk-peek');
  if (el && el.dataset['k'] === k) el.textContent = txt;
}

export const peekKey = (bid: string, path: string, mtime: string | number | null): string => `${bid}\u0000${path}\u0000${mtime ?? ''}`;

/** Kinds of file that open in the editor (double-click / Open editor). */
export const TEXTY = new Set(['text', 'code', 'yaml', 'json', 'xml', 'label']);

export const EDITOR_ACTIONS: Record<string, Action> = {
  'open-file': (el) => {
    const bid = el.dataset['bid'] || D.bid || '';
    const p = el.dataset['path'] ?? '';
    if (bid && p) void openFile(bid, p);
  },
};
