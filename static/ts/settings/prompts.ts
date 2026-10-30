// Settings › Prompts & Skills: what the user adds to the ◆ assistants (server/routes/assets.py).
// Tabs: My instructions (kind prompt: appended to the built-in rules, never replacing them), My skills (kind skill:
// steps the agent loads with use_skill when the description / "when" matches), Plugins (declarative bundles of
// skills, prompts and HTTP tools: install from a .json / .zip or pasted JSON, enable, write-only secrets, remove with
// an inline confirm) and Web search (level 0–3 capped by the admins, ask before each query, engine + URL / key / cx,
// Test, special words never / private with bulk paste). Loaded when the section first renders; every save re-renders
// from the server's answer. Edit forms are read from the DOM on Save, so typing never re-renders.

import { api, errText, getJSON, sendJSON } from '../core/api.js';
import { esc, icon } from '../core/dom.js';
import type { AssetRole, LlmItem, LlmPlugin, SetAction, SetChange, StatusLine, WebView, WebWord } from '../types-settings.js';
import { renderSettings } from './dialog.js';
import { dd, rowAttr, statusHTML, tagc } from './forms.js';

type Tab = 'instr' | 'skills' | 'plugins' | 'web';
interface PromptsState {
  tab: Tab; items: LlmItem[] | null; plugins: LlmPlugin[] | null; web: WebView | null;
  loading: boolean; error: string | null; edit: string | null; confirm: string | null;
  status: StatusLine | null; plugStatus: StatusLine | null; webStatus: StatusLine | null; testStatus: StatusLine | null;
  paste: boolean; bulk: boolean; testResults: Array<{ title: string; url: string }>; engine: string | null;
}
const PR: PromptsState = { tab: 'instr', items: null, plugins: null, web: null, loading: false, error: null, edit: null, confirm: null,
  status: null, plugStatus: null, webStatus: null, testStatus: null, paste: false, bulk: false, testResults: [], engine: null };

const MAX_BODY = 20000;
const TABS: Array<[Tab, string, string]> = [['instr', 'My instructions', 'text'], ['skills', 'My skills', 'sparkle'],
  ['plugins', 'Plugins', 'plug'], ['web', 'Web search', 'globe']];
const ROLES: Array<[AssetRole, string]> = [['all', 'Both'], ['data', 'Data'], ['visual', 'Visual']];
const ROLE_LABEL: Record<AssetRole, string> = { all: 'Data + Visual', data: 'Data', visual: 'Visual' };
const LEVELS: Array<[string, string]> = [
  ['Off', 'No web search at all.'],
  ['Public only', 'General words only. Every internal name (buckets, folders, files, classes, hosts, people) and all special words are blocked.'],
  ['Technical', 'Also class names, tags, dataset formats and model names. Still blocked: bucket, folder and file names, paths, hosts, people, special words.'],
  ['Full', 'Anything except secrets and words marked never. Only here may the provider use its built-in search: it writes its own queries, which cannot be checked first.'],
];
const ENGINES: Array<[string, string]> = [['auto', 'Auto (SearXNG, else DuckDuckGo)'], ['searxng', 'SearXNG'], ['duckduckgo', 'DuckDuckGo'],
  ['brave', 'Brave Search'], ['tavily', 'Tavily'], ['serper', 'Serper (Google)'], ['google_cse', 'Google Custom Search']];
const KEYED = new Set(['brave', 'tavily', 'serper', 'google_cse']);

export async function loadPromptsData(): Promise<void> {
  PR.loading = true;
  const [i, p, w] = await Promise.allSettled([
    getJSON<{ items: LlmItem[] }>('api/llm/prompts'),
    getJSON<{ plugins: LlmPlugin[] }>('api/llm/plugins'),
    getJSON<WebView>('api/llm/web'),
  ]);
  PR.loading = false;
  PR.items = i.status === 'fulfilled' ? i.value.items : PR.items ?? [];
  PR.plugins = p.status === 'fulfilled' ? p.value.plugins : PR.plugins ?? [];
  PR.web = w.status === 'fulfilled' ? w.value : PR.web;
  PR.error = i.status === 'rejected' ? errText(i.reason) : null;
}

// ---------------------------------------------------------------- section
export function renderPromptsSec(): string {
  if (!PR.items && !PR.error) {
    if (!PR.loading) void loadPromptsData().then(renderSettings);
    return `<section>${statusHTML({ kind: 'busy', text: 'loading…' })}</section>`;
  }
  const tabs = '<div class="segmented pr-tabs" role="tablist" aria-label="Prompts and skills">' + TABS.map(([k, label, ico]) =>
    `<button class="view-btn${PR.tab === k ? ' active' : ''}" role="tab" aria-selected="${PR.tab === k}" data-act="pr-tab" data-v="${k}">${icon(ico)}${esc(label)}</button>`).join('') + '</div>';
  const body = PR.tab === 'plugins' ? pluginsHTML() : PR.tab === 'web' ? webHTML() : itemsHTML(PR.tab === 'skills' ? 'skill' : 'prompt');
  return '<section class="pr-sec">' + (PR.error ? statusHTML({ kind: 'err', text: PR.error }) : '') + tabs + body + '</section>';
}

// ---------------------------------------------------------------- my instructions / my skills
function roleSelect(v: AssetRole): string {
  return '<select data-pr-f="role" aria-label="Which assistant">' +
    ROLES.map(([k, l]) => `<option value="${k}"${v === k ? ' selected' : ''}>${esc(l)}</option>`).join('') + '</select>';
}

function formHTML(kind: LlmItem['kind'], it: LlmItem | null): string {
  const v = it ?? { id: '', kind, role: 'all' as AssetRole, name: '', description: '', when: '', body: '', enabled: true };
  const skill = kind === 'skill';
  return `<div class="form-card pr-form" data-pr-id="${esc(v.id)}"><div class="step-head">${icon(skill ? 'sparkle' : 'text')}` +
    `${it ? 'Edit' : 'New'} ${skill ? 'skill' : 'instruction'}</div>` +
    '<div class="pr-row">' +
    `<label class="fld pr-grow">Name<input type="text" data-pr-f="name" maxlength="120" value="${esc(v.name)}" spellcheck="false"></label>` +
    `<label class="fld">Assistant${roleSelect(v.role)}</label>` +
    `<label class="pr-on"><input type="checkbox" class="switch" data-pr-f="enabled" ${v.enabled ? 'checked' : ''} aria-label="Enabled">Enabled</label></div>` +
    (skill ? `<label class="fld">Description<span class="help">The agent picks skills by this line</span>` +
      `<input type="text" data-pr-f="description" maxlength="1000" value="${esc(v.description)}"></label>` +
      `<label class="fld">When<span class="help">Optional: when to use it, e.g. “the user asks to split a dataset”</span>` +
      `<input type="text" data-pr-f="when" maxlength="500" value="${esc(v.when)}"></label>` : '') +
    `<label class="fld mono">${skill ? 'Steps (Markdown)' : 'Instruction (Markdown)'}` +
    `<textarea data-pr-f="body" rows="${skill ? 10 : 6}" maxlength="${MAX_BODY}" spellcheck="true">${esc(v.body)}</textarea>` +
    `<output class="pr-count">${v.body.length} / ${MAX_BODY}</output></label>` +
    statusHTML(PR.status) +
    `<div class="form-foot"><button class="ghost" data-act="pr-cancel">Cancel</button><button class="primary" data-act="pr-save" data-kind="${kind}">Save</button></div></div>`;
}

function confirmHTML(key: string, what: string, act: string): string {
  return `<div class="pr-confirm" role="alert"><span>Delete ${esc(what)}? This cannot be undone.</span>` +
    `<button class="ghost" data-act="pr-confirm-no">Cancel</button><button class="danger" data-act="${act}" data-key="${esc(key)}">Delete</button></div>`;
}

function itemsHTML(kind: LlmItem['kind']): string {
  const items = (PR.items ?? []).filter((i) => i.kind === kind);
  const skill = kind === 'skill';
  const head = '<div class="sec-actions">' +
    `<button data-act="pr-new" ${PR.edit === 'new' ? 'disabled' : ''}>${icon('plus')}${skill ? 'Add skill' : 'Add instruction'}</button>` +
    `<span class="sec-note">${skill
      ? 'Steps the assistant loads when a request matches the description. Plugins can add more.'
      : 'Added after the built-in rules of the assistant you pick. They never lift a confirmation or a read-only limit.'}` +
    ` <span class="num">${items.length} / 100</span></span></div>`;
  const rows = items.map((it) => {
    if (PR.edit === it.id) return formHTML(kind, it);
    const preview = (skill ? it.description : it.body).replace(/\s+/g, ' ').slice(0, 160);
    return `<div class="pr-item${it.enabled ? '' : ' off'}" data-pr-id="${esc(it.id)}"><div class="pr-item-head">` +
      `<b>${esc(it.name)}</b>${tagc(ROLE_LABEL[it.role] ?? it.role)}<span class="grow"></span>` +
      `<input type="checkbox" class="switch" data-act-change="pr-enable" ${it.enabled ? 'checked' : ''} aria-label="${esc(`Enable ${it.name}`)}">` +
      `<button class="icon-btn ghost" data-act="pr-edit" aria-label="${esc(`Edit ${it.name}`)}" title="Edit">${icon('edit')}</button>` +
      `<button class="icon-btn ghost danger" data-act="pr-del" aria-label="${esc(`Delete ${it.name}`)}" title="Delete">${icon('trash')}</button></div>` +
      `<p class="pr-preview">${esc(preview)}${skill && it.when ? ` <span class="muted">· when: ${esc(it.when)}</span>` : ''}</p>` +
      (PR.confirm === `item:${it.id}` ? confirmHTML(`item:${it.id}`, `“${it.name}”`, 'pr-del-yes') : '') + '</div>';
  }).join('');
  const empty = !items.length && PR.edit !== 'new'
    ? `<p class="sec-note pr-empty">${skill ? 'No skills yet.' : 'No instructions yet. Example: “Always answer in Thai” or “Our train / val split is 80 / 20”.'}</p>` : '';
  return head + (PR.edit === 'new' ? formHTML(kind, null) : '') + (PR.edit !== 'new' && PR.edit === null ? statusHTML(PR.status) : '') + rows + empty;
}

// ---------------------------------------------------------------- plugins
const host = (u: string): string => { try { return new URL(u).host; } catch { return u; } };

function pluginHTML(p: LlmPlugin): string {
  const tools = p.tools.length ? '<ul class="pr-tools">' + p.tools.map((t) =>
    `<li><code>${esc(t.name)}</code>${tagc(t.risk, t.risk === 'danger' ? 'bad' : t.risk === 'change' ? 'warn' : '')}` +
    `<span class="muted">${esc(t.method)} ${esc(host(t.url))} · ${esc(t.roles.map((r) => r === 'data' ? 'Data' : 'Visual').join(' + '))}</span>` +
    `<span class="pr-tdesc">${esc(t.description)}</span></li>`).join('') + '</ul>' : '';
  const chips = [...p.skills.map((s) => `<span class="chip" title="${esc(s.description)}">${icon('sparkle')}${esc(s.name)}</span>`),
    ...p.prompts.map((s) => `<span class="chip">${icon('text')}${esc(s.name)}</span>`)].join('');
  const secrets = p.secrets.length ? '<div class="pr-secrets">' + p.secrets.map((s) =>
    `<label class="fld mono">${esc(s.name)}<input type="password" data-secret="${esc(s.name)}" autocomplete="off" ` +
    `placeholder="${s.set ? '•••••• saved: type to replace' : 'not set'}"></label>` +
    (s.set ? `<button class="ghost" data-act="pr-secret-clear" data-name="${esc(s.name)}">Clear</button>` : '')).join('') +
    '<button data-act="pr-secrets-save">Save secrets</button></div>' : '';
  return `<div class="form-card pr-plugin${p.enabled ? '' : ' off'}" data-plug="${esc(p.id)}"><div class="step-head">${icon('plug')}` +
    `<b>${esc(p.name)}</b><span class="muted num">v${esc(p.version)}</span><span class="grow"></span>` +
    `<input type="checkbox" class="switch" data-act-change="pr-plug-enable" ${p.enabled ? 'checked' : ''} aria-label="${esc(`Enable ${p.name}`)}">` +
    `<button class="icon-btn ghost danger" data-act="pr-plug-del" aria-label="${esc(`Remove ${p.name}`)}" title="Remove">${icon('trash')}</button></div>` +
    (p.description ? `<p class="sec-note">${esc(p.description)}</p>` : '') +
    (PR.confirm === `plug:${p.id}` ? confirmHTML(`plug:${p.id}`, `the plugin ${p.name} with its skills and prompts`, 'pr-plug-del-yes') : '') +
    tools + (chips ? `<div class="chips">${chips}</div>` : '') + secrets + '</div>';
}

function pluginsHTML(): string {
  const install = '<div class="form-card"><div class="step-head">' + icon('upload') + 'Install a plugin</div>' +
    '<p class="sec-note">A plugin is a <code>plugin.json</code> (or a .zip with it and <code>skills/*/SKILL.md</code>): skills, ' +
    'instructions and HTTP tools named <code>plugin__tool</code>. No code runs; change and danger tools always ask you first.</p>' +
    '<div class="sec-actions"><label class="pr-file"><input type="file" accept=".json,.zip,application/json,application/zip" ' +
    `data-act-change="pr-plug-file">${icon('file')}Choose file…</label>` +
    `<button class="ghost" data-act="pr-paste">${PR.paste ? 'Hide JSON' : 'Paste JSON'}</button></div>` +
    (PR.paste ? '<label class="fld mono">Manifest JSON<textarea data-pr-f="manifest" rows="8" spellcheck="false" ' +
      'placeholder="{&quot;name&quot;: &quot;tracker&quot;, &quot;version&quot;: &quot;1.0&quot;, &quot;tools&quot;: [...]}"></textarea></label>' +
      '<div class="form-foot"><button class="primary" data-act="pr-plug-paste">Install</button></div>' : '') +
    statusHTML(PR.plugStatus) + '</div>';
  const list = (PR.plugins ?? []).map(pluginHTML).join('');
  return install + (list || '<p class="sec-note pr-empty">No plugins installed.</p>');
}

// ---------------------------------------------------------------- web search
function wordsHTML(words: WebWord[]): string {
  const chips = words.map((w, i) => `<span class="chip pr-word ${w.strength}" title="${w.strength === 'never' ? 'Never searched, at any level' : 'Blocked at Public and Technical'}">` +
    `${esc(w.text)}<span class="muted">${w.strength}</span>` +
    `<button class="icon-btn ghost" data-act="pr-word-del" data-i="${i}" aria-label="${esc(`Remove ${w.text}`)}">${icon('x')}</button></span>`).join('');
  const org = PR.web && PR.web.org_words ? `<p class="hint">${PR.web.org_words} organisation word${PR.web.org_words === 1 ? '' : 's'} set by your administrator also apply.</p>` : '';
  return '<div class="form-card"><div class="step-head">' + icon('lock') + 'Special words</div>' +
    '<p class="sec-note">Project names, customers, code names… a word, a phrase or a <code>/regex/</code>. ' +
    '<b>never</b>: blocked at every level, Full too. <b>private</b>: blocked at Public and Technical.</p>' +
    `<div class="chips pr-words">${chips}</div>` + (words.length ? '' : '<p class="hint">No personal words yet.</p>') + org +
    '<div class="sec-actions"><input type="text" class="txt" id="pr-word" data-enter="pr-word-add" placeholder="Add a word" aria-label="Special word" spellcheck="false">' +
    '<select id="pr-word-strength" aria-label="Strength"><option value="private">private</option><option value="never">never</option></select>' +
    '<button data-act="pr-word-add">Add</button>' +
    `<button class="ghost" data-act="pr-bulk">${PR.bulk ? 'Hide' : 'Paste many'}</button></div>` +
    (PR.bulk ? '<label class="fld mono">One per line<textarea id="pr-bulk" rows="6" spellcheck="false"></textarea></label>' +
      '<div class="form-foot"><button class="primary" data-act="pr-bulk-add">Add all</button></div>' : '') + '</div>';
}

function webHTML(): string {
  const v = PR.web;
  if (!v) return statusHTML({ kind: 'err', text: 'Web search settings are not available on this server.' });
  const w = v.web;
  const lvl = Math.min(w.level, v.max_level);
  const [name, desc] = LEVELS[lvl] ?? ['', ''];
  const stops = LEVELS.map(([n], i) => `<span class="${i === lvl ? 'on' : ''}${i > v.max_level ? ' off' : ''}">${esc(n)}</span>`).join('');
  const eng = PR.engine ?? w.engine;         // an engine picked but not saved yet survives other saves
  const level = '<div class="form-card"><div class="step-head">' + icon('globe') + 'How much may leave</div>' +
    `<div class="pr-level"><input type="range" min="0" max="3" step="1" value="${lvl}" data-pr-f="level" data-act-change="pr-web-level" ` +
    `aria-label="Web search level" aria-valuetext="${esc(name)}"><div class="pr-stops">${stops}</div></div>` +
    `<p class="pr-level-cap" id="pr-level-cap"><b>${esc(name)}</b> ${esc(desc)}</p>` +
    (v.max_level < 3 ? `<p class="hint">Your administrator allows up to <b>${esc(LEVELS[v.max_level]?.[0] ?? '')}</b>.</p>` : '') +
    '<p class="hint">The provider’s built-in search is used only at Full. At Public and Technical every query goes through the server’s filter first.</p>' +
    `<label class="llm-feat"><span class="llm-ftext"><b>Ask before each search</b><span>Each query shows as a card you can send, edit or skip.</span></span>` +
    `<span></span><input type="checkbox" class="switch" data-act-change="pr-web-ask" ${w.ask_before ? 'checked' : ''} aria-label="Ask before each search"></label>` +
    statusHTML(PR.webStatus) + '</div>';
  const engine = '<div class="form-card pr-engine"><div class="step-head">' + icon('search') + 'Search engine</div>' +
    '<label class="fld">Engine<select data-act-change="pr-web-engine" data-pr-f="engine">' +
    ENGINES.map(([k, l]) => `<option value="${k}"${eng === k ? ' selected' : ''}>${esc(l)}</option>`).join('') + '</select></label>' +
    (eng === 'searxng' ? `<label class="fld mono">SearXNG URL<input type="url" data-pr-f="url" value="${esc(w.url)}" placeholder="https://searx.example.org" spellcheck="false"></label>` : '') +
    (KEYED.has(eng) ? `<label class="fld mono">API key<input type="password" data-pr-f="api_key" autocomplete="off" placeholder="${v.has_key ? '•••••• saved: type to replace' : 'paste the key'}"></label>` : '') +
    (eng === 'google_cse' ? `<label class="fld mono">Search engine ID (cx)<input type="text" data-pr-f="cx" value="${esc(w.cx ?? '')}" spellcheck="false"></label>` : '') +
    '<div class="form-foot">' + (KEYED.has(eng) && v.has_key ? '<button class="ghost" data-act="pr-web-key-clear">Remove key</button>' : '') +
    '<span class="grow"></span><button class="ghost" data-act="pr-web-test">Test</button><button class="primary" data-act="pr-web-save">Save</button></div>' +
    statusHTML(PR.testStatus) +
    (PR.testResults.length ? '<ul class="pr-results">' + PR.testResults.slice(0, 5).map((r) =>
      `<li><b>${esc(r.title || r.url)}</b><span class="muted">${esc(r.url)}</span></li>`).join('') + '</ul>' : '') + '</div>';
  return level + engine + wordsHTML(w.words);
}

/** Live parts while typing: the character counter, the level caption. */
export function prInput(el: HTMLElement): void {
  const f = el.dataset['prF'];
  if (f === 'body' && el instanceof HTMLTextAreaElement) {
    const out = el.parentElement?.querySelector('.pr-count');
    if (out) out.textContent = `${el.value.length} / ${MAX_BODY}`;
  } else if (f === 'level' && el instanceof HTMLInputElement) {
    const i = Number(el.value);
    const [name, desc] = LEVELS[i] ?? ['', ''];
    const cap = document.getElementById('pr-level-cap');
    if (cap) cap.innerHTML = `<b>${esc(name)}</b> ${esc(desc)}`;
    el.setAttribute('aria-valuetext', name);
    el.parentElement?.querySelectorAll('.pr-stops span').forEach((s, k) => s.classList.toggle('on', k === i));
  }
}

// ---------------------------------------------------------------- actions
const fieldOf = (root: Element, k: string): HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null =>
  root.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`[data-pr-f="${k}"]`);
const valOf = (root: Element, k: string): string => fieldOf(root, k)?.value ?? '';
const itemOf = (b: HTMLElement): LlmItem | undefined => (PR.items ?? []).find((i) => i.id === rowAttr(b, '[data-pr-id]', 'prId'));
const plugOf = (b: HTMLElement): LlmPlugin | undefined => (PR.plugins ?? []).find((p) => p.id === rowAttr(b, '[data-plug]', 'plug'));
const isRole = (x: string): x is AssetRole => x === 'all' || x === 'data' || x === 'visual';

async function saveWeb(patch: Record<string, unknown>, okText = 'Saved'): Promise<void> {
  PR.webStatus = { kind: 'busy', text: 'saving…' };
  try {
    PR.web = await sendJSON<WebView>('PUT', 'api/llm/web', patch);
    PR.webStatus = { kind: 'ok', text: okText };
  } catch (e) { PR.webStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

async function addWords(texts: string[], strength: WebWord['strength']): Promise<void> {
  if (!PR.web) return;
  const words = [...PR.web.web.words];
  let n = 0;
  for (const raw of texts) {
    const t = raw.trim();
    if (t && !words.some((w) => w.text.toLowerCase() === t.toLowerCase())) { words.push({ text: t.slice(0, 200), strength }); n += 1; }
  }
  if (!n) { PR.webStatus = { kind: '', text: 'Nothing new to add' }; renderSettings(); return; }
  PR.bulk = false;
  await saveWeb({ words }, `Added ${n} word${n === 1 ? '' : 's'}`);
}

async function install(send: () => Promise<Response>): Promise<void> {
  PR.plugStatus = { kind: 'busy', text: 'installing…' };
  renderSettings();
  try {
    const r = await send();
    const { plugin } = await r.json() as { plugin: LlmPlugin };
    const list = (PR.plugins ?? []).filter((p) => p.id !== plugin.id);
    PR.plugins = [...list, plugin].sort((a, b) => a.name.localeCompare(b.name));
    PR.paste = false;
    PR.plugStatus = { kind: 'ok', text: `Installed ${plugin.name} ${plugin.version}: ${plugin.tools.length} tools, ${plugin.skills.length} skills` +
      (plugin.secrets.some((s) => !s.set) ? '. Set its secrets below.' : '') };
  } catch (e) { PR.plugStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

async function putPlugin(p: LlmPlugin, body: Record<string, unknown>, okText: string): Promise<void> {
  try {
    const { plugin } = await sendJSON<{ plugin: LlmPlugin }>('PUT', `api/llm/plugins/${encodeURIComponent(p.id)}`, body);
    PR.plugins = (PR.plugins ?? []).map((x) => x.id === plugin.id ? plugin : x);
    PR.plugStatus = { kind: 'ok', text: okText };
  } catch (e) { PR.plugStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

export const PROMPTS_ACTIONS: Record<string, SetAction> = {
  'pr-tab': (b) => {
    const v = dd(b, 'v');
    if (v === 'instr' || v === 'skills' || v === 'plugins' || v === 'web') PR.tab = v;
    PR.edit = null; PR.confirm = null; PR.status = null;
    renderSettings();
  },
  'pr-new': () => { PR.edit = 'new'; PR.status = null; renderSettings(); document.querySelector<HTMLElement>('.pr-form [data-pr-f="name"]')?.focus(); },
  'pr-edit': (b) => { const it = itemOf(b); if (!it) return; PR.edit = it.id; PR.status = null; renderSettings(); },
  'pr-cancel': () => { PR.edit = null; PR.status = null; renderSettings(); },
  'pr-save': async (b) => {
    const form = b.closest('.pr-form');
    if (!form) return;
    const kind = dd(b, 'kind') === 'skill' ? 'skill' : 'prompt';
    const role = valOf(form, 'role');
    const en = fieldOf(form, 'enabled');
    const body: Record<string, unknown> = { kind, role: isRole(role) ? role : 'all', name: valOf(form, 'name'), body: valOf(form, 'body'),
      enabled: en instanceof HTMLInputElement ? en.checked : true };
    if (kind === 'skill') { body['description'] = valOf(form, 'description'); body['when'] = valOf(form, 'when'); }
    const id = rowAttr(b, '[data-pr-id]', 'prId');
    try {
      const { item } = await sendJSON<{ item: LlmItem }>(id ? 'PUT' : 'POST', id ? `api/llm/prompts/${encodeURIComponent(id)}` : 'api/llm/prompts', body);
      PR.items = id ? (PR.items ?? []).map((x) => x.id === item.id ? item : x) : [...(PR.items ?? []), item];
      PR.edit = null;
      PR.status = { kind: 'ok', text: `Saved “${item.name}”` };
      renderSettings();
    } catch (e) {
      const st = form.querySelector('.status-line');
      if (st) st.outerHTML = statusHTML({ kind: 'err', text: errText(e) });
    }
  },
  'pr-del': (b) => { const it = itemOf(b); if (!it) return; PR.confirm = `item:${it.id}`; renderSettings(); },
  'pr-confirm-no': () => { PR.confirm = null; renderSettings(); },
  'pr-del-yes': async (b) => {
    const id = dd(b, 'key').replace(/^item:/, '');
    PR.confirm = null;
    try {
      await api(`api/llm/prompts/${encodeURIComponent(id)}`, { method: 'DELETE' });
      PR.items = (PR.items ?? []).filter((x) => x.id !== id);
      PR.status = { kind: 'ok', text: 'Deleted' };
    } catch (e) { PR.status = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  // plugins
  'pr-paste': () => { PR.paste = !PR.paste; renderSettings(); },
  'pr-plug-paste': (b) => {
    const text = valOf(b.closest('.form-card') ?? document.body, 'manifest').trim();
    if (!text) { PR.plugStatus = { kind: 'err', text: 'Paste the manifest JSON first' }; renderSettings(); return; }
    return install(() => api('api/llm/plugins', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ manifest: text }) }));
  },
  'pr-plug-del': (b) => { const p = plugOf(b); if (!p) return; PR.confirm = `plug:${p.id}`; renderSettings(); },
  'pr-plug-del-yes': async (b) => {
    const id = dd(b, 'key').replace(/^plug:/, '');
    PR.confirm = null;
    try {
      await api(`api/llm/plugins/${encodeURIComponent(id)}`, { method: 'DELETE' });
      PR.plugins = (PR.plugins ?? []).filter((x) => x.id !== id);
      PR.plugStatus = { kind: 'ok', text: 'Removed' };
    } catch (e) { PR.plugStatus = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  'pr-secrets-save': (b) => {
    const p = plugOf(b);
    const card = b.closest('[data-plug]');
    if (!p || !card) return;
    const secrets: Record<string, string> = {};
    card.querySelectorAll<HTMLInputElement>('input[data-secret]').forEach((i) => { if (i.value.trim()) secrets[dd(i, 'secret')] = i.value.trim(); });
    if (!Object.keys(secrets).length) { PR.plugStatus = { kind: '', text: 'Type a secret to save (blank keeps the saved one)' }; renderSettings(); return; }
    return putPlugin(p, { secrets }, `Saved ${Object.keys(secrets).length} secret${Object.keys(secrets).length === 1 ? '' : 's'} of ${p.name}`);
  },
  'pr-secret-clear': (b) => { const p = plugOf(b); if (p) return putPlugin(p, { secrets: { [dd(b, 'name')]: '' } }, `Cleared ${dd(b, 'name')}`); },
  // web search
  'pr-web-save': async (b) => {
    const card = b.closest('.pr-engine');
    if (!card || !PR.web) return;
    const key = valOf(card, 'api_key').trim();
    if (key) {
      try { await sendJSON('PUT', 'api/llm/web/key', { api_key: key }); } catch (e) { PR.testStatus = { kind: 'err', text: errText(e) }; renderSettings(); return; }
    }
    const patch: Record<string, unknown> = { engine: valOf(card, 'engine') || PR.web.web.engine };
    if (fieldOf(card, 'url')) patch['url'] = valOf(card, 'url').trim();
    if (fieldOf(card, 'cx')) patch['cx'] = valOf(card, 'cx').trim();
    PR.testStatus = null; PR.engine = null;
    await saveWeb(patch, key ? 'Saved (key stored on the server)' : 'Saved');
  },
  'pr-web-key-clear': async () => {
    try { await sendJSON('PUT', 'api/llm/web/key', { api_key: '' }); if (PR.web) PR.web.has_key = false; PR.testStatus = { kind: 'ok', text: 'Key removed' }; }
    catch (e) { PR.testStatus = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  'pr-web-test': async () => {
    PR.testStatus = { kind: 'busy', text: 'searching…' };
    PR.testResults = [];
    renderSettings();
    try {
      const r = await sendJSON<{ ok?: boolean; error?: string; engine?: string; ms?: number; results?: Array<{ title?: string; url?: string }> }>('POST', 'api/llm/web/test', {});
      const res = (r.results ?? []).map((x) => ({ title: String(x.title ?? ''), url: String(x.url ?? '') }));
      PR.testResults = res;
      PR.testStatus = r.ok === false ? { kind: 'err', text: r.error || 'the search failed' }
        : { kind: 'ok', text: `Works${r.engine ? ` (${r.engine})` : ''}: ${res.length} result${res.length === 1 ? '' : 's'}${r.ms ? ` in ${r.ms} ms` : ''}` };
    } catch (e) { PR.testStatus = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  'pr-word-add': () => {
    const inp = document.getElementById('pr-word');
    const st = document.getElementById('pr-word-strength');
    if (!(inp instanceof HTMLInputElement)) return;
    return addWords([inp.value], st instanceof HTMLSelectElement && st.value === 'never' ? 'never' : 'private');
  },
  'pr-word-del': (b) => {
    if (!PR.web) return;
    const i = Number(dd(b, 'i'));
    return saveWeb({ words: PR.web.web.words.filter((_, k) => k !== i) }, 'Removed');
  },
  'pr-bulk': () => { PR.bulk = !PR.bulk; renderSettings(); },
  'pr-bulk-add': () => {
    const ta = document.getElementById('pr-bulk');
    const st = document.getElementById('pr-word-strength');
    if (!(ta instanceof HTMLTextAreaElement)) return;
    return addWords(ta.value.split(/\r?\n/), st instanceof HTMLSelectElement && st.value === 'never' ? 'never' : 'private');
  },
};

export const PROMPTS_CHANGES: Record<string, SetChange> = {
  'pr-enable': async (el) => {
    const it = itemOf(el);
    if (!it || !(el instanceof HTMLInputElement)) return;
    try {
      const { item } = await sendJSON<{ item: LlmItem }>('PUT', `api/llm/prompts/${encodeURIComponent(it.id)}`, { enabled: el.checked });
      PR.items = (PR.items ?? []).map((x) => x.id === item.id ? item : x);
      PR.status = null;
    } catch (e) { PR.status = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  'pr-plug-enable': (el) => {
    const p = plugOf(el);
    if (p && el instanceof HTMLInputElement) return putPlugin(p, { enabled: el.checked }, `${p.name} ${el.checked ? 'enabled' : 'disabled'}`);
  },
  'pr-plug-file': (el) => {
    const f = el instanceof HTMLInputElement && el.files ? el.files[0] : undefined;
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { PR.plugStatus = { kind: 'err', text: 'The plugin file is larger than 2 MB' }; renderSettings(); return; }
    const fd = new FormData();
    fd.append('file', f, f.name);
    return install(() => api('api/llm/plugins', { method: 'POST', body: fd }));
  },
  'pr-web-level': (el) => {
    if (!(el instanceof HTMLInputElement) || !PR.web) return;
    const want = Number(el.value);
    const lvl = Math.min(want, PR.web.max_level);
    return saveWeb({ level: lvl }, want > lvl ? `Capped at ${LEVELS[lvl]?.[0] ?? lvl} by your administrator` : `Web search: ${LEVELS[lvl]?.[0] ?? lvl}`);
  },
  'pr-web-ask': (el) => { if (el instanceof HTMLInputElement) return saveWeb({ ask_before: el.checked }, el.checked ? 'Each search will ask first' : 'Searches run without asking'); },
  'pr-web-engine': (el) => {
    if (!(el instanceof HTMLSelectElement) || !PR.web) return;
    PR.engine = el.value;           // shows the fields this engine needs; Save stores it
    PR.testStatus = null; PR.testResults = [];
    renderSettings();
  },
};
