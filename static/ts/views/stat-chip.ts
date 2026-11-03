// One statistical test result (dataviz/ml/stats.py) as a small chip: the test, its p value, the effect size and a
// plain verdict, with a tooltip that says what the test asks and how to read it. Shared by the Compare panels, the
// Evaluation tab's model vs model card and Image conditions › Model fit.

import { esc } from '../core/dom.js';
import type { StatTest } from '../types-views.js';

const fmtP = (p: number | null | undefined): string => {
  if (p == null) return '–';
  if (p < 0.001) return '< 0.001';
  return p < 0.01 ? p.toFixed(3) : p.toFixed(2);
};

const fmtE = (v: number | null | undefined): string => {
  if (v == null) return '–';
  const a = Math.abs(v);
  return a !== 0 && (a < 0.001 || a >= 1000) ? v.toExponential(1) : v.toFixed(a < 0.1 ? 3 : 2);
};

/** Verdict class: differs (accent), negligible (muted), no evidence (plain). */
const tone = (v: string): string => (v === 'differs' ? 'st-diff' : v === 'significant but negligible' ? 'st-small' : v === 'n/a' ? 'st-na' : 'st-same');

/** The chip's HTML; `ask` = the question this test answers for this visual (first line of the tooltip). */
export function statChip(t: StatTest, ask: string): string {
  const title = `${ask}\n\n${t.test}: p = ${fmtP(t.p)}${t.p_mw != null ? ` (Mann-Whitney p = ${fmtP(t.p_mw)})` : ''}` +
    `${t.df ? `, df ${t.df}` : ''} · ${t.effect_name} = ${fmtE(t.effect)}` +
    `${t.w1 != null ? ` · Wasserstein-1 = ${fmtE(t.w1)}` : ''}${t.perms ? ` · ${t.perms} permutations` : ''}` +
    ` · n = ${t.n_a.toLocaleString()} / ${t.n_b.toLocaleString()}` +
    '\n\np: how likely a difference this big is by chance when the two are really alike (below 0.05: unlikely). ' +
    'The effect size says how big the difference is: with many samples a tiny one is "significant" too.';
  return `<span class="stat-chip ${tone(t.verdict)}" title="${esc(title)}"><b>${esc(t.test)}</b>` +
    `<span class="num">p ${esc(fmtP(t.p))}</span><span class="num">${esc(t.effect_name)} ${esc(fmtE(t.effect))}</span>` +
    `<span class="st-v">${esc(t.verdict)}</span></span>`;
}
