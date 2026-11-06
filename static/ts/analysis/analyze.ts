// Analyze: profile, statistic tests, data quality and pivot, all on the open tab's last result. Nothing here reaches the
// user's database: the saved result is read on the server.

import { errText } from '../core/api.js';
import { $, $req, esc } from '../core/dom.js';
import { api, tab } from './state.js';
import type { ResultInfo, TestResult } from './state.js';

const SHEETS = [['profile', 'Profile'], ['tests', 'Statistic tests'], ['quality', 'Data quality'], ['pivot', 'Pivot']] as const;
type Sheet = typeof SHEETS[number][0];
let sheet: Sheet = 'profile';
const TESTS: [string, string, string, string][] = [
  ['ttest', 'Do two groups have a different average?', 'Number', 'Group'],
  ['mannwhitney', 'The same, without assuming a bell curve', 'Number', 'Group'],
  ['anova', 'Do several groups have different averages?', 'Number', 'Group'],
  ['chi2', 'Are two categories related?', 'Category', 'Category'],
  ['pearson', 'Do two numbers move together?', 'Number', 'Number'],
  ['spearman', 'The same, by rank', 'Number', 'Number'],
  ['normal', 'Is this number bell-shaped?', 'Number', ''],
];

export const currentResult = (): ResultInfo | undefined => { const t = tab(), r = t.run?.results ?? []; return r[Math.min(t.result, r.length - 1)]; };

const hist = (counts: number[]): string => {
  const max = Math.max(...counts, 1);
  return `<div class="an-spark">${counts.map((c) => `<i style="height:${Math.max(1, c / max * 42)}px"></i>`).join('')}</div>`;
};

export async function renderAnalyze(): Promise<void> {
  const w = $req('an-work'), r = currentResult();
  w.innerHTML = `<div class="an-tabs">${SHEETS.map(([k, n]) => `<button type="button" class="an-tab ${sheet === k ? 'on' : ''}" data-act="sheet" data-k="${k}">${n}</button>`).join('')}<span class="grow"></span><span class="pill">${r ? `Using: ${esc(tab().name)} · ${r.rows.toLocaleString()} rows` : ''}</span></div><div class="an-scroll" id="an-sheet"><div class="an-empty">${r ? 'Loading…' : 'Run a query in the Query workspace first. This page works on its result.'}</div></div>`;
  if (!r) return;
  const el = $req('an-sheet');
  try {
    if (sheet === 'profile') el.innerHTML = await profile(r);
    else if (sheet === 'tests') el.innerHTML = await tests(r);
    else if (sheet === 'quality') el.innerHTML = await quality(r);
    else el.innerHTML = pivotForm(r);
  } catch (e) { el.innerHTML = `<div class="an-hint bad"><span>${esc(errText(e))}</span></div>`; }
}

async function profile(r: ResultInfo): Promise<string> {
  const p = await api.profile(r.id);
  return `<div class="an-cards">${p.columns.map((c) => `<div class="an-card"><h4>${esc(c.name)} <span class="faint">${esc(c.type.toLowerCase())}</span></h4>${hist(c.hist.counts)}<div class="num">${c.numeric && c.mean !== null ? `mean ${c.mean.toLocaleString('en-US', { maximumFractionDigits: 2 })} · ` : ''}${c.distinct.toLocaleString()} distinct</div><div class="faint">${esc(String(c.min ?? ''))} to ${esc(String(c.max ?? ''))}</div>${c.note ? `<div class="muted">${esc(c.note)}</div>` : ''}</div>`).join('')}</div>`;
}

const options = (r: ResultInfo, cur: string): string => r.columns.map((c) => `<option ${c === cur ? 'selected' : ''}>${esc(c)}</option>`).join('');
const form = { kind: 'ttest', a: '', b: '' };

async function tests(r: ResultInfo): Promise<string> {
  if (!form.a) form.a = r.columns[0] ?? '';
  if (!form.b) form.b = r.columns[1] ?? '';
  const s = await api.suggest(r.id), t = TESTS.find((x) => x[0] === form.kind) ?? TESTS[0] as typeof TESTS[0];
  return (s.length ? `<div class="an-suggest"><b>Fits your columns</b>${s.map((x, i) => `<button type="button" class="an-btn sm" data-act="test-pick" data-i="${i}" data-kind="${esc(x.kind)}" data-a="${esc(x.a)}" data-b="${esc(x.b ?? '')}">${esc(x.label)}</button>`).join('')}</div>` : '') +
    `<div class="an-form an-testform"><label>Question<select id="an-t-kind">${TESTS.map((x) => `<option value="${x[0]}" ${x[0] === form.kind ? 'selected' : ''}>${esc(x[1])}</option>`).join('')}</select></label><label>${t[2]}<select id="an-t-a">${options(r, form.a)}</select></label>${t[3] ? `<label>${t[3]}<select id="an-t-b">${options(r, form.b)}</select></label>` : ''}<button type="button" class="an-btn pri" data-act="test-run">Run test</button></div><div id="an-t-out"></div>`;
}

function testHtml(x: TestResult): string {
  return `<div class="an-card"><h4>${esc(x.test)}</h4><div class="an-sum"><div><b>${esc(x.stat.name)} = ${x.stat.value.toLocaleString('en-US', { maximumFractionDigits: 3 })}</b>test value</div><div><b>${x.p < 0.001 ? 'p < 0.001' : `p = ${x.p.toFixed(3)}`}</b>chance result</div><div><b>${esc(x.effect.value)}</b>${esc(x.effect.name)}${x.effect.label ? `, ${esc(x.effect.label)}` : ''}</div>${(x.groups ?? []).map((g) => `<div><b>${g.mean.toLocaleString('en-US', { maximumFractionDigits: 2 })}</b>${esc(g.name)}, ${g.n.toLocaleString()} rows</div>`).join('')}</div><div class="an-hint"><span>${esc(x.text)}</span></div></div>`;
}

async function quality(r: ResultInfo): Promise<string> {
  const q = await api.quality(r.id);
  return `<table class="an-doc"><thead><tr><th style="width:100%">Check</th><th>Result</th><th>Rows</th></tr></thead><tbody>${q.checks.map((c) => `<tr><td>${esc(c.rule)}</td><td><span class="an-tag ${c.ok ? '' : 'write'}">${c.ok ? 'Passed' : 'Failed'}</span></td><td class="num">${c.rows.toLocaleString()}</td></tr>`).join('')}</tbody></table>`;
}

function pivotForm(r: ResultInfo): string {
  return `<div class="an-form an-testform"><label>Rows<select id="an-p-row">${options(r, r.columns[0] ?? '')}</select></label><label>Columns<select id="an-p-col">${options(r, r.columns[1] ?? '')}</select></label><label>Value<select id="an-p-val"><option value="">Count of rows</option>${options(r, '')}</select></label><label>Using<select id="an-p-agg"><option>sum</option><option>avg</option><option>min</option><option>max</option></select></label><button type="button" class="an-btn pri" data-act="pivot-run">Build</button></div><div id="an-p-out"></div>`;
}

const val = (id: string): string => ($(id) as HTMLSelectElement | null)?.value ?? '';

export const A_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  sheet: (el) => { sheet = el.dataset['k'] as Sheet; return renderAnalyze(); },
  'test-pick': async (el) => {
    form.kind = el.dataset['kind'] ?? 'ttest';
    form.a = el.dataset['a'] ?? '';
    form.b = el.dataset['b'] ?? '';
    await renderAnalyze();
    await runTest();
  },
  'test-run': () => runTest(),
  'pivot-run': async () => {
    const r = currentResult();
    if (!r) return;
    const p = await api.pivot(r.id, val('an-p-row'), val('an-p-col'), val('an-p-val'), val('an-p-agg'));
    $req('an-p-out').innerHTML = `<div class="an-gridwrap"><table class="an-doc"><thead><tr><th></th>${p.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${p.rows.map((x) => `<tr><td><b>${esc(x.name)}</b></td>${x.cells.map((c) => `<td class="num">${c === null ? '' : esc(typeof c === 'number' ? c.toLocaleString('en-US', { maximumFractionDigits: 2 }) : c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  },
};

async function runTest(): Promise<void> {
  const r = currentResult();
  if (!r) return;
  form.kind = val('an-t-kind') || form.kind;
  form.a = val('an-t-a') || form.a;
  form.b = val('an-t-b') || form.b;
  const out = $req('an-t-out');
  try { out.innerHTML = testHtml(await api.test(r.id, form.kind, form.a, form.b)); } catch (e) { out.innerHTML = `<div class="an-hint bad"><span>${esc(errText(e))}</span></div>`; }
}

export function analyzeChange(t: HTMLElement): boolean {
  if (t.id !== 'an-t-kind') return false;
  form.kind = (t as HTMLSelectElement).value;
  void renderAnalyze();
  return true;
}
