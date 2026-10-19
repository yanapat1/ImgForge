// Settings › AI assistant: connect an LLM (OpenAI, Claude, Gemini, vLLM, Ollama, OpenRouter, any OpenAI-compatible
// server) and choose which AI features the page shows. Nothing AI-related appears anywhere until a provider is saved,
// tested OK and a feature is switched on (core/llm.js applies that to every [data-llm] element).
// Views: no provider = hero + provider tiles; 'llm-add' = the tiles again; 'llm-form' = 3 steps (endpoint & key ·
// model · test); otherwise the provider cards, the feature switches and the token budget.
// Every save / test / switch fires the window event `llm-changed`, so the page re-applies live.

import { api, errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon, syncRanges } from '../core/dom.js';
import type { LlmConfig, LlmDraft, LlmForm, LlmPayload, LlmProvider, LlmTestResult, PresetKey, SetAction, SetChange, StatusLine } from '../types-settings.js';
import { SET, renderSettings } from './dialog.js';
import { dd, formOf, rowAttr, statusHTML, tagc } from './forms.js';

// presets: what a tile fills in. keyUrl = where to get a key; key = a key is needed; ph = key placeholder
interface Preset {
  label: string; sub: string; kind: string; base_url: string; model: string; models: string[]; mph?: string; key: boolean; ph: string;
  keyUrl?: string; ini: string; desc: string;
}
const PRESET_KEYS: PresetKey[] = ['openai', 'anthropic', 'gemini', 'vllm', 'ollama', 'openrouter', 'custom'];
const isPreset = (x: string): x is PresetKey => PRESET_KEYS.some((k) => k === x);
const PRESETS: Record<PresetKey, Preset> = {
  openai: { label: 'OpenAI', sub: 'ChatGPT', kind: 'openai', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini',
    models: ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini', 'gpt-4.1', 'gpt-5-mini', 'gpt-5'], key: true, ph: 'sk-…',
    keyUrl: 'https://platform.openai.com/api-keys', ini: 'O', desc: 'GPT-4o, GPT-4.1, GPT-5: images and audio.' },
  anthropic: { label: 'Claude', sub: 'Anthropic', kind: 'anthropic', base_url: 'https://api.anthropic.com', model: 'claude-sonnet-5-5',
    models: ['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5'], key: true, ph: 'sk-ant-…',
    keyUrl: 'https://console.anthropic.com/settings/keys', ini: 'C', desc: 'Sonnet, Opus, Haiku: strong at reading images; no audio input.' },
  gemini: { label: 'Gemini', sub: 'Google', kind: 'gemini', base_url: 'https://generativelanguage.googleapis.com/v1beta', model: 'gemini-2.5-flash',
    models: ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.5-flash-lite'], key: true, ph: 'AIza…',
    keyUrl: 'https://aistudio.google.com/apikey', ini: 'G', desc: 'Flash and Pro: images, audio and good box coordinates.' },
  vllm: { label: 'vLLM', sub: 'your GPU server', kind: 'openai_compat', base_url: 'http://localhost:8000/v1', model: '',
    models: [], mph: 'Qwen/Qwen3-VL-8B-Instruct', key: false, ph: 'optional', ini: 'v',
    desc: 'Qwen-VL, InternVL, Llama… served with an OpenAI-compatible API.' },
  ollama: { label: 'Ollama', sub: 'local models', kind: 'openai_compat', base_url: 'http://localhost:11434/v1', model: '',
    models: ['qwen2.5vl:7b', 'gemma3:12b', 'llava:13b', 'llama3.2-vision'], mph: 'qwen2.5vl:7b', key: false, ph: 'optional', ini: 'Ol',
    desc: 'Models running on this machine or on the LAN. Nothing leaves it.' },
  openrouter: { label: 'OpenRouter', sub: 'many providers', kind: 'openai_compat', base_url: 'https://openrouter.ai/api/v1', model: '',
    models: [], mph: 'google/gemini-2.5-flash', key: true, ph: 'sk-or-…', keyUrl: 'https://openrouter.ai/keys', ini: 'OR',
    desc: 'One key for hundreds of models from many providers.' },
  custom: { label: 'Custom', sub: 'OpenAI-compatible', kind: 'openai_compat', base_url: '', model: '', models: [], mph: 'model name',
    key: false, ph: 'optional', ini: '', desc: 'LM Studio, TGI, LiteLLM, a company gateway… any /v1/chat/completions server.' },
};

const BOX_FORMATS: Array<[string, string]> = [['norm1000_yxyx', 'Gemini style [ymin,xmin,ymax,xmax] 0–1000'], ['norm1000_xyxy', '0–1000 [x1,y1,x2,y2] (Qwen3-VL, most)'],
  ['pixel_xyxy', 'pixels (Qwen2.5-VL)']];
const VISION_RE = /gpt-4o|gpt-4\.1|gpt-5|o3|o4|claude|gemini|vl|vision|llava|qwen.*vl|pixtral|gemma-3|llama-3\.2.*vision|internvl|minicpm-v/i;
const AUDIO_RE = /gemini|audio|gpt-4o.*audio|qwen.*audio|ultravox|omni|phi-4-multimodal/i;
const NO_AUDIO = 'Claude does not accept audio';

// the feature switches, grouped as on the page; panel = only reachable from the ◆ panel (needs `assistant`)
const FEATS: Record<string, [title: string, desc: string, panel?: boolean]> = {
  assistant: ['AI panel', 'The ◆ in the left bar: a chat next to the plot, so you can lasso and ask in one place.'],
  dataset_qa: ['Dataset Q&A', 'Answers from a compact dataset summary: classes, counts, box sizes, image quality.', true],
  explain_eval: ['Explain evaluation', 'Reads the leaderboard and findings and explains them; adds Explain to the Evaluation tab.'],
  ask_selection: ['Ask about the selection', 'Sends the lasso-selected boxes as one numbered crop mosaic.', true],
  label_check: ['Label check', 'Looks at box crops and flags labels that seem wrong, with a suggested class.'],
  report: ['Dataset report', 'Writes a Markdown report of the dataset, curation and evaluation.', true],
  nl_filter: ['Describe a filter', 'Type “small cars at night” in the filter bar and get the matching filter.'],
  auto_tag: ['Auto-tag images', 'Tags images (night, rain, crowded…) as ai:* tags you can filter on.', true],
  prelabel: ['Pre-label this image', 'Label page: one click draws suggested boxes for you to accept or reject.'],
  label_assist: ['Label assistant', 'Label page: ask “how do I label the hands here?” and get an answer plus boxes.'],
  voice: ['Voice commands', 'Hold the mic to ask a question or drive the page by voice.'],
};
const GROUPS: Array<[string, string[]]> = [['Visualizer', ['assistant', 'dataset_qa', 'explain_eval', 'ask_selection', 'label_check', 'report']],
  ['Search & tags', ['nl_filter', 'auto_tag']], ['Labeling', ['prelabel', 'label_assist']], ['Voice', ['voice']]];
const NEEDS: Record<string, 'vision' | 'audio'> = { ask_selection: 'vision', label_check: 'vision', auto_tag: 'vision', prelabel: 'vision', label_assist: 'vision', voice: 'audio' };

export async function loadLlmData(): Promise<void> {
  try {
    SET.llm = await getJSON<LlmConfig>('api/llm/config');
    SET.llmError = null;
  } catch (e) { SET.llm = null; SET.llmError = errText(e); }
}

const changed = (): boolean => window.dispatchEvent(new Event('llm-changed'));
const put = (body: unknown): Promise<LlmConfig> => api('api/llm/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  .then((r): Promise<LlmConfig> => r.json());

/** The loaded config (the section only offers actions once it is loaded). */
function cfg(): LlmConfig {
  if (!SET.llm) throw new Error('the AI assistant config is not loaded');
  return SET.llm;
}

function presetOf(p: LlmProvider): PresetKey {
  const hit = PRESET_KEYS.find((k) => { const v = PRESETS[k]; return v.kind === p.kind && !!v.base_url && v.base_url === (p.base_url || '').replace(/\/$/, ''); });
  if (hit) return hit;
  return p.kind === 'openai_compat' ? 'custom' : isPreset(p.kind) ? p.kind : 'custom';
}

// the gradient diamond (own gradient id: the activity-bar copy uses #gem-grad)
const gemSVG = (cls: string, id: string): string => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1">` +
  '<stop offset="0" style="stop-color:var(--gem-1)"/><stop offset=".55" style="stop-color:var(--gem-2)"/><stop offset="1" style="stop-color:var(--gem-3)"/></linearGradient></defs>' +
  `<path class="gem-body" style="fill:url(#${id});stroke:url(#${id})" d="M6.5 4h10l4.5 5.5L11.5 21 2 9.5z"/>` +
  `<path class="gem-facet" style="stroke:url(#${id})" d="M2 9.5h19M9.3 4 7.2 9.5l4.3 11.5 4.3-11.5L13.7 4M7.2 9.5 11.5 4l4.3 5.5"/>` +
  '<path class="gem-spark" d="M20 1.5v4M18 3.5h4"/></svg>';

const logo = (k: PresetKey): string => `<span class="llm-logo llm-${k}">${PRESETS[k].ini ? esc(PRESETS[k].ini) : icon('plug')}</span>`;

// ---------------------------------------------------------------- section
export function renderLlmSec(): string {
  const llm = SET.llm;
  if (!llm) {
    return `<section>${statusHTML(SET.llmError ? { kind: 'err', text: `The AI assistant is not available on this server: ${SET.llmError}` } : { kind: 'busy', text: 'loading…' })}</section>`;
  }
  if (SET.view === 'llm-form' && SET.form && SET.form.form === 'llm') return renderForm(SET.form);
  const provs = llm.providers || [];
  if (!provs.length) {
    return '<section class="llm-sec"><div class="llm-hero">' + gemSVG('llm-hero-gem', 'gem-grad-hero') +
      '<h4>Connect an LLM to unlock AI features</h4>' +
      '<p>Nothing AI-related appears anywhere in the app until a model is connected and a feature is switched on.</p></div>' +
      tilesHTML() + '</section>';
  }
  if (SET.view === 'llm-add') {
    return `<section class="llm-sec"><h4 class="llm-h">Pick a provider</h4>${tilesHTML()}` +
      '<div class="form-foot"><button class="ghost" data-act="back">Cancel</button></div></section>';
  }
  return '<section class="llm-sec">' +
    `<div class="sec-actions"><button data-act="llm-add">${icon('plus')}Add provider</button>` +
    '<span class="sec-note">The active provider answers every AI feature. Keys stay on the server.</span></div>' +
    provs.map((p) => cardHTML(p, llm.active)).join('') + featuresHTML(llm) + budgetHTML(llm) + '</section>';
}

function tilesHTML(): string {
  return '<div class="llm-tiles" role="list">' + PRESET_KEYS.map((k) => {
    const p = PRESETS[k];
    return `<button class="tile llm-tile" role="listitem" data-act="llm-new" data-preset="${k}">` +
    `<span class="llm-tile-head">${logo(k)}<b>${esc(p.label)}</b><small>${esc(p.sub)}</small></span>` +
    `<span>${esc(p.desc)}</span><span class="ds-meta">${p.key ? tagc('API key') : tagc('no key needed', 'ok')}` +
    `${p.kind === 'openai_compat' && k !== 'openrouter' ? tagc('self-hosted') : ''}</span></button>`;
  }).join('') + '</div>';
}

function cardHTML(p: LlmProvider, activeId: string | null): string {
  const active = p.id === activeId;
  const k = presetOf(p);
  const tags: string[] = [];
  if (p.verified_at) tags.push(tagc('Connected', 'ok', 'check'));
  else if (p.last_error) tags.push(`<span class="tagc bad" title="${esc(p.last_error)}">${icon('alert')}Failed</span>`);
  else tags.push(tagc('Not tested'));
  if (p.vision) tags.push(tagc('vision', 'on', 'eye'));
  if (p.audio) tags.push(tagc('voice', 'on', 'mic'));
  if (p.key_env) tags.push(tagc(`key from ${p.api_key_masked}`, '', 'check'));
  else if (p.has_key) tags.push(tagc('key stored', '', 'check'));
  const v = p.verify && p.verified_at ? ` · ${p.verify.ms} ms` : '';
  return `<div class="ds-card llm-card${active ? ' active' : ''}" data-lid="${esc(p.id)}">` +
    `<label class="llm-radio" title="Use this provider for every AI feature"><input type="radio" name="llm-active" value="${esc(p.id)}" ` +
    `data-act-change="llm-active" ${active ? 'checked' : ''} aria-label="Active provider: ${esc(p.name)}"></label>` +
    `<div class="ds-main"><div class="ds-title">${logo(k)}${esc(p.name)}${active ? tagc('active', 'on') : ''}</div>` +
    `<div class="ds-uri" title="${esc(p.base_url)}">${esc(p.model)} · ${esc(p.base_url)}${esc(v)}</div><div class="ds-meta">${tags.join('')}</div></div>` +
    `<div class="ds-actions"><button data-act="llm-test-saved" title="Send a tiny request (and an image / a sound if ticked)">${icon('plug')}Test</button>` +
    `<button class="icon-btn ghost" data-act="llm-edit" aria-label="Edit" title="Edit">${icon('edit')}</button>` +
    `<button class="icon-btn ghost" data-act="llm-remove" aria-label="Remove" title="Remove (its key is deleted from the server)">${icon('trash')}</button>` +
    `</div><div class="ds-progress" id="llmst-${esc(p.id)}"></div></div>`;
}

const activeProv = (llm: LlmConfig): LlmProvider | null => (llm.providers || []).find((p) => p.id === llm.active) ?? null;

// why a feature cannot be switched on with the active model ('' = it can)
function lacks(k: string, p: LlmProvider): string {
  const need = NEEDS[k];
  if (need === 'vision' && !p.vision) return 'needs a vision model: tick Vision on the provider (and test again)';
  if (need === 'audio' && !p.audio) return p.kind === 'anthropic' ? `${NO_AUDIO}: use Gemini or GPT-4o audio for voice` : 'needs a model that accepts audio: tick Voice on the provider';
  return '';
}

function featuresHTML(llm: LlmConfig): string {
  const p = activeProv(llm);
  if (!p || !p.verified_at) {
    return '<div class="form-card llm-feats"><div class="step-head">AI features</div>' +
      `<p class="sec-note llm-locked">${icon('alert')}Test the connection to unlock the switches.</p>` +
      (p ? `<div class="sec-actions"><button class="primary" data-act="llm-test-saved" data-id="${esc(p.id)}">${icon('plug')}Test ${esc(p.name)}</button></div>` : '') +
      '</div>';
  }
  const sw = llm.features || {};
  const avail = Object.keys(FEATS).filter((k) => !lacks(k, p));
  const anyOn = Object.keys(FEATS).some((k) => sw[k]);
  const groups = GROUPS.map(([g, keys]) => `<div class="llm-group"><div class="llm-ghead">${esc(g)}</div>` + keys.map((k) => {
    const [title, desc, panel] = FEATS[k] ?? [k, ''];
    const why = lacks(k, p);
    const need = NEEDS[k];
    const badges = (need ? tagc(`needs ${need === 'audio' ? 'voice' : 'vision'}`, why ? 'bad' : '') : '') +
      (panel && sw[k] && !sw.assistant ? tagc('turn on the AI panel', 'warn') : '');
    return `<label class="llm-feat${why ? ' off' : ''}"><span class="llm-ftext"><b>${esc(title)}</b><span>${esc(desc)}</span>` +
      `${why ? `<span class="llm-why">${esc(why)}</span>` : ''}</span><span class="llm-badges">${badges}</span>` +
      `<input type="checkbox" class="switch" data-llm-feat="${k}" data-act-change="llm-feat" ${sw[k] && !why ? 'checked' : ''} ${why ? 'disabled' : ''} ` +
      `aria-label="${esc(title)}"></label>`;
  }).join('') + '</div>').join('');
  return `<div class="form-card llm-feats"><div class="step-head">${icon('sparkle')}AI features<span class="grow"></span>` +
    `<button class="ghost" data-act="llm-all-on" ${avail.every((k) => sw[k]) ? 'disabled' : ''}>Turn all on</button>` +
    `<button class="ghost" data-act="llm-all-off" ${anyOn ? '' : 'disabled'}>All off</button></div>` +
    '<p class="sec-note">Each switch applies at once. Features of the ◆ panel (Q&amp;A, selection, report, auto-tag) also need <b>AI panel</b> on.</p>' +
    groups + '</div>';
}

const kTok = (v: number): string => `≤ ${Math.round(v / 1000)}k tokens per request · typical 2–15k`;

function budgetHTML(llm: LlmConfig): string {
  const v = (llm.budget && llm.budget.max_input_tokens) || 100000;
  return '<div class="form-card"><div class="step-head">Token budget</div>' +
    '<div class="lab-row"><span>Largest request sent to the model<span class="muted">older chat turns and images are dropped first; 100k is the hard cap</span></span>' +
    `<span class="lab-range"><input type="range" min="8000" max="100000" step="4000" value="${esc(v)}" data-llm-budget aria-label="Token budget">` +
    `<output id="llm-budget-out">${esc(kTok(v))}</output></span></div></div>`;
}

// ---------------------------------------------------------------- form (3 steps)
function blank(key: PresetKey): LlmDraft {
  const p = PRESETS[key];
  return { name: '', kind: p.kind, base_url: p.base_url, model: p.model, api_key: '', vision: false, audio: false,
    box_format: p.kind === 'gemini' ? 'norm1000_yxyx' : 'norm1000_xyxy', timeout_s: 120, max_output_tokens: 2048, temperature: 0.2 };
}

function guessCaps(f: LlmForm): void {
  const m = f.llm.model || '';
  f.llm.vision = VISION_RE.test(m);
  f.llm.audio = f.llm.kind !== 'anthropic' && AUDIO_RE.test(m);
}

function openForm(key: PresetKey, p: LlmProvider | null): void {
  SET.sec = 'llm';
  SET.view = 'llm-form';
  let llm: LlmDraft = blank(key);
  if (p) {
    // the server-side status fields are not part of the form
    const { verified_at, verify, last_error, has_key, key_env, api_key_masked, ...fields } = p;
    llm = { ...llm, ...fields, api_key: '' };
  }
  const f: LlmForm = { form: 'llm', llm, preset: key, hasKey: !!(p && p.has_key), keyMasked: p ? (p.api_key_masked ?? '') : '', capsTouched: !!p, adv: false,
    models: null, modelsStatus: null, testStatus: null, lastTest: null, saveStatus: null };
  SET.form = f;
  if (!p) guessCaps(f);
  renderSettings();
}

// values a test depends on: a test is only "for this form" while they are unchanged
const sig = (p: LlmDraft): string => JSON.stringify([p.kind, (p.base_url || '').trim(), (p.model || '').trim(), p.api_key || '', !!p.vision, !!p.audio]);

function steps(f: LlmForm): { s1: boolean; s2: boolean; s3: boolean } {
  const p = f.llm, pr = PRESETS[f.preset];
  const s1 = /^https?:\/\/\S+$/.test((p.base_url || '').trim()) && (!pr.key || !!(p.api_key || '').trim() || f.hasKey);
  const s2 = s1 && !!(p.model || '').trim();
  const s3 = s2 && !!(f.lastTest && f.lastTest.ok && f.lastTest.sig === sig(p));
  return { s1, s2, s3 };
}

const card = (id: string, n: number, title: string, inner: string, done: boolean, locked: boolean): string =>
  `<div class="form-card${done ? ' done' : ''}${locked ? ' locked' : ''}" id="${id}"><div class="step-head"><span class="step-n">${done ? '✓' : n}</span>${title}</div>${inner}</div>`;

interface FldOpts { ph?: string; help?: string; mono?: boolean; type?: string; extra?: string; cls?: string }
const fld = (label: string, path: string, val: unknown, { ph = '', help = '', mono = false, type = 'text', extra = '', cls = '' }: FldOpts = {}): string =>
  `<label class="fld${mono ? ' mono' : ''}${cls ? ` ${cls}` : ''}">${label}<input type="${type}" data-f="${path}" value="${esc(val == null ? '' : val)}" placeholder="${esc(ph)}" ${extra}>` +
  `${help ? `<span class="help">${help}</span>` : ''}</label>`;

function payload(f: LlmForm): LlmPayload {
  const trim = (v: unknown): string => String(v == null ? '' : v).trim();
  const { api_key, ...rest } = f.llm;
  const p: LlmPayload = { ...rest, name: trim(rest.name), base_url: trim(rest.base_url), model: trim(rest.model) };
  const key = trim(api_key);
  if (key) p.api_key = key;                     // blank = keep the stored key
  if (p.kind === 'anthropic') p.audio = false;
  return p;
}

function renderForm(f: LlmForm): string {
  const p = f.llm, pr = PRESETS[f.preset];
  const st = steps(f);
  const editing = !!p.id;
  const keyHelp = (pr.keyUrl ? `<a class="btn-link" href="${esc(pr.keyUrl)}" target="_blank" rel="noopener noreferrer">Get a key ↗</a> · ` : '') +
    'or write <code>env:VAR_NAME</code> to read it from the server’s environment' +
    (f.hasKey ? ` · stored: <code>${esc(f.keyMasked || '••••')}</code>` : '');
  const s1 = `<div class="llm-preset">${logo(f.preset)}<b>${esc(pr.label)}</b><span class="muted">${esc(pr.sub)}</span>` +
    (editing ? '' : `<button class="ghost llm-change" data-act="llm-add">Change</button>`) + '</div>' +
    '<div class="form-grid">' +
    fld('Name', 'llm.name', p.name, { ph: `e.g. ${pr.label}${pr.model ? ` · ${pr.model}` : ''}` }) +
    fld('Base URL', 'llm.base_url', p.base_url, { mono: true, type: 'url', ph: 'https://…/v1', extra: 'spellcheck="false"' }) +
    fld(`API key${pr.key ? '' : ' (optional)'}`, 'llm.api_key', p.api_key, { mono: true, type: 'password', cls: 'full',
      ph: f.hasKey ? 'unchanged (stored on the server)' : pr.ph, extra: 'autocomplete="new-password" spellcheck="false"', help: keyHelp }) +
    '</div><p class="sec-note">The key is kept on the server in an owner-only file and is never sent back to the page.</p>';
  const list = f.models || pr.models;
  const audioOff = p.kind === 'anthropic';
  const s2 = '<div class="sec-actions llm-model-row">' +
    `<label class="fld mono" style="flex:1">Model<input type="text" data-f="llm.model" list="llm-models" value="${esc(p.model || '')}" ` +
    `placeholder="${esc(pr.mph || pr.model || 'model name')}" spellcheck="false" autocomplete="off"></label>` +
    `<button data-act="llm-load-models" ${st.s1 ? '' : 'disabled'} title="Ask the server which models it offers">${icon('search')}Load models</button></div>` +
    `<datalist id="llm-models">${list.map((m) => `<option value="${esc(m)}"></option>`).join('')}</datalist>` +
    statusHTML(f.modelsStatus) +
    '<div class="llm-caps">' +
    `<label class="llm-cap"><input type="checkbox" class="switch" data-f="llm.vision" data-act-change="llm-cap" ${p.vision ? 'checked' : ''}>` +
    `<span><b>${icon('eye')}Vision</b><span>reads images: selection, label check, auto-tag, label page</span></span></label>` +
    `<label class="llm-cap${audioOff ? ' off' : ''}"><input type="checkbox" class="switch" data-f="llm.audio" data-act-change="llm-cap" ${p.audio && !audioOff ? 'checked' : ''} ${audioOff ? 'disabled' : ''}>` +
    `<span><b>${icon('mic')}Voice</b><span>${audioOff ? esc(NO_AUDIO) : 'hears audio: voice commands'}</span></span></label></div>` +
    `<p class="sec-note">${f.capsTouched ? 'Set by you.' : 'Guessed from the model name; change them if wrong.'} The test checks both.</p>` +
    `<details class="llm-adv"${f.adv ? ' open' : ''}><summary data-act="llm-adv">Advanced</summary><div class="form-grid">` +
    `<label class="fld">Box format (label page)<select data-f="llm.box_format">${BOX_FORMATS.map(([v, l]) =>
      `<option value="${v}" ${v === p.box_format ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>` +
    '<span class="help">how this model writes box coordinates</span></label>' +
    fld('Timeout (s)', 'llm.timeout_s', p.timeout_s, { type: 'number', extra: 'min="5" max="600" step="5"' }) +
    fld('Max output tokens', 'llm.max_output_tokens', p.max_output_tokens, { type: 'number', extra: 'min="64" max="32768" step="64"' }) +
    fld('Temperature', 'llm.temperature', p.temperature, { type: 'number', extra: 'min="0" max="2" step="0.1"', help: 'low = factual, repeatable answers' }) +
    '</div></details>';
  const s3 = `<div class="sec-actions"><button class="primary" data-act="llm-test-form" ${st.s2 ? '' : 'disabled'}>${icon('plug')}Test connection</button>` +
    `${statusHTML(f.testStatus || { kind: '', text: 'Sends a tiny request (plus a small image / sound if Vision / Voice are on).' })}</div>`;
  return '<section class="llm-sec">' +
    card('llm-s1', 1, 'Endpoint &amp; key', s1, st.s1, false) +
    card('llm-s2', 2, 'Model', s2, st.s2, !st.s1) +
    card('llm-s3', 3, 'Test connection', s3, st.s3, !st.s2) +
    `<div class="form-foot">${statusHTML(f.saveStatus)}<span class="grow"></span><button class="ghost" data-act="back">Cancel</button>` +
    `<button class="primary" id="llm-save" data-act="llm-save" ${st.s2 ? '' : 'disabled'}>${icon('check')}${editing ? 'Save' : 'Add provider'}</button></div></section>`;
}

// typing in the form: no re-render (focus stays); re-check the step locks and the capability guess in place
export function llmInput(el: HTMLElement): void {
  if (el.matches('[data-llm-budget]')) { if (el instanceof HTMLInputElement) budgetInput(el); return; }
  const f = SET.form;
  if (SET.view !== 'llm-form' || !f || f.form !== 'llm') return;
  if (el.dataset.f === 'llm.model' && !f.capsTouched) {
    guessCaps(f);
    const box = $req('settings-body');
    const v = box.querySelector<HTMLInputElement>('[data-f="llm.vision"]'), a = box.querySelector<HTMLInputElement>('[data-f="llm.audio"]');
    if (v) v.checked = f.llm.vision;
    if (a && !a.disabled) a.checked = f.llm.audio;
  }
  const st = steps(f);
  const stepCards: Array<[string, boolean, boolean, number]> = [['llm-s1', st.s1, false, 1], ['llm-s2', st.s2, !st.s1, 2], ['llm-s3', st.s3, !st.s2, 3]];
  stepCards.forEach(([id, done, locked, n]) => {
    const c = $(id);
    if (!c) return;
    c.classList.toggle('done', done);
    c.classList.toggle('locked', locked);
    const num = c.querySelector('.step-n');
    if (num) num.textContent = done ? '✓' : String(n);
  });
  const set = (sel: string, on: boolean) => { const b = $req('settings-body').querySelector<HTMLButtonElement>(sel); if (b) b.disabled = !on; };
  set('[data-act="llm-load-models"]', st.s1);
  set('[data-act="llm-test-form"]', st.s2);
  set('#llm-save', st.s2);
}

let budgetTimer = 0;
function budgetInput(el: HTMLInputElement): void {
  const v = +el.value;
  const out = $('llm-budget-out');
  if (out) out.textContent = kTok(v);
  syncRanges();
  clearTimeout(budgetTimer);
  budgetTimer = setTimeout(async () => {
    try { SET.llm = await put({ budget: { max_input_tokens: v } }); changed(); } catch (e) { if (out) out.textContent = errText(e); }
  }, 400);
}

const mark = (on: boolean | undefined, ok: boolean | undefined): string => (on ? (ok ? '✓' : '✗') : '—');
const testText = (r: LlmTestResult, p: { vision?: boolean | undefined; audio?: boolean | undefined }): string => `${r.ms} ms · text ${mark(true, r.text_ok)} · vision ${mark(p.vision, r.vision_ok)} · voice ${mark(p.audio, r.audio_ok)}` +
  (r.model ? ` · ${r.model}` : '');

async function testSaved(id: string): Promise<void> {
  const el = () => $(`llmst-${id}`);
  const busy = el();
  if (busy) busy.innerHTML = statusHTML({ kind: 'busy', text: 'testing…' });
  let st: StatusLine;
  try {
    const r = await postJSON<LlmTestResult>('api/llm/test', { id });
    const p = (cfg().providers || []).find((x) => x.id === id) ?? {};
    st = r.ok ? { kind: 'ok', text: testText(r, p) } : { kind: 'err', text: r.error || 'test failed' };
  } catch (e) { st = { kind: 'err', text: errText(e) }; }
  await loadLlmData();
  changed();
  renderSettings();
  const done = el();
  if (done) done.innerHTML = statusHTML(st);
}

async function setFeatures(features: Record<string, boolean>, focusKey?: string): Promise<void> {
  try { SET.llm = await put({ features }); } catch (e) { SET.llmError = errText(e); alert(errText(e)); await loadLlmData(); }
  changed();
  renderSettings();
  if (focusKey) { const el = $req('settings-body').querySelector<HTMLElement>(`[data-llm-feat="${focusKey}"]`); if (el) el.focus(); }
}

// ---------------------------------------------------------------- actions (merged into SET_ACTIONS / SET_CHANGES)
export const LLM_ACTIONS: Record<string, SetAction> = {
  'llm-add': () => { SET.view = 'llm-add'; SET.form = null; renderSettings(); },
  'llm-new': (b) => { const k = dd(b, 'preset'); if (isPreset(k)) openForm(k, null); },
  'llm-edit': (b) => {
    const p = cfg().providers.find((x) => x.id === rowAttr(b, '[data-lid]', 'lid'));
    if (p) openForm(presetOf(p), p);
  },
  'llm-adv': (b) => { formOf('llm').adv = !(b.parentElement instanceof HTMLDetailsElement && b.parentElement.open); },
  'llm-load-models': async () => {
    const f = formOf('llm');
    f.modelsStatus = { kind: 'busy', text: 'asking the server for its models…' };
    renderSettings();
    try {
      const r = await postJSON<{ models?: string[] }>('api/llm/models', { form: payload(f) });
      const models = r.models || [];
      f.models = models;
      f.modelsStatus = { kind: 'ok', text: `${models.length} model${models.length === 1 ? '' : 's'}: pick one in the Model field` };
    } catch (e) { f.modelsStatus = { kind: 'err', text: errText(e) }; }
    if (SET.form === f) renderSettings();
  },
  'llm-test-form': async () => {
    const f = formOf('llm'), s = sig(f.llm);
    f.testStatus = { kind: 'busy', text: 'testing…' };
    renderSettings();
    try {
      const r = await postJSON<LlmTestResult>('api/llm/test', { form: payload(f) });
      f.lastTest = { ok: !!r.ok, sig: s };
      f.testStatus = r.ok ? { kind: 'ok', text: testText(r, f.llm) } : { kind: 'err', text: r.error || 'test failed' };
    } catch (e) { f.lastTest = null; f.testStatus = { kind: 'err', text: errText(e) }; }
    if (SET.form === f) renderSettings();
  },
  'llm-save': async () => {
    const f = formOf('llm');
    const tested = !!(f.lastTest && f.lastTest.ok && f.lastTest.sig === sig(f.llm));
    f.saveStatus = { kind: 'busy', text: 'saving…' };
    renderSettings();
    let saved: { provider?: { id: string } };
    try { saved = await postJSON<{ provider?: { id: string } }>('api/llm/providers', payload(f)); } catch (e) { f.saveStatus = { kind: 'err', text: errText(e) }; renderSettings(); return; }
    // tested OK with these exact values: record it now, so the provider is Connected without a second click
    if (tested && saved.provider) { try { await postJSON('api/llm/test', { id: saved.provider.id }); } catch (e) { /* shown as Not tested */ } }
    await loadLlmData();
    changed();
    SET.view = 'list';
    SET.form = null;
    renderSettings();
  },
  'llm-test-saved': (b) => testSaved(dd(b, 'id') || rowAttr(b, '[data-lid]', 'lid')),
  'llm-remove': async (b) => {
    const p = cfg().providers.find((x) => x.id === rowAttr(b, '[data-lid]', 'lid'));
    if (!p || !confirm(`Remove the provider "${p.name}"?\n\nIts API key is deleted from the server.`)) return;
    try { SET.llm = await (await api(`api/llm/providers/${encodeURIComponent(p.id)}`, { method: 'DELETE' })).json(); } catch (e) { alert(errText(e)); await loadLlmData(); }
    changed();
    renderSettings();
  },
  'llm-all-on': () => {
    const p = activeProv(cfg());
    if (p) void setFeatures(Object.fromEntries(Object.keys(FEATS).filter((k) => !lacks(k, p)).map((k) => [k, true])));
  },
  'llm-all-off': () => setFeatures(Object.fromEntries(Object.keys(FEATS).map((k) => [k, false]))),
};

export const LLM_CHANGES: Record<string, SetChange> = {
  'llm-cap': () => {
    formOf('llm').capsTouched = true;
    const model = $req('settings-body').querySelector<HTMLElement>('[data-f="llm.model"]');
    if (model) llmInput(model);
  },
  'llm-feat': (el) => setFeatures({ [dd(el, 'llmFeat')]: el instanceof HTMLInputElement && el.checked }, dd(el, 'llmFeat')),
  'llm-active': async (el) => {
    try { SET.llm = await put({ active: el instanceof HTMLInputElement ? el.value : '' }); } catch (e) { alert(errText(e)); await loadLlmData(); }
    changed();
    renderSettings();
  },
};
