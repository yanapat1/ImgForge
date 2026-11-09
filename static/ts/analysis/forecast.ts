// Forecast: a measure of the last result over time, with its likely range, how well it tested and the unusual steps.
// The server does the work on the saved result (dataviz/analysis/forecast.py); this module draws the sheet in Analyze and
// the chart that the dashboard's forecast widget reuses.

import { errText } from '../core/api.js';
import { $, $req, esc } from '../core/dom.js';
import { api } from './state.js';
import type { Forecast, ResultInfo } from './state.js';

const GRAINS = [['auto', 'Pick for me'], ['hour', 'Hour'], ['day', 'Day'], ['week', 'Week'], ['month', 'Month'], ['quarter', 'Quarter'], ['year', 'Year']] as const;
const AGGS = ['sum', 'avg', 'count', 'min', 'max'] as const;
const F = { rid: '', date: '', value: '', agg: 'sum', grain: 'auto', horizon: '', group: '', out: null as Forecast | null, err: '' };

const num = (v: number): string => v.toLocaleString('en-US', { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 });
const short = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const when = (t: string, grain: string): string => grain === 'hour' ? t.slice(0, 13) + ':00' : grain === 'month' || grain === 'quarter' ? t.slice(0, 7) : grain === 'year' ? t.slice(0, 4) : t.slice(0, 10);

/** History, then the forecast as a dashed line in its 80% and 95% bands; unusual steps as dots. */
export function forecastSvg(f: Forecast, compact = false): string {
  const W = compact ? 420 : 760, H = compact ? 190 : 280, L = 52, R = 10, T = 10, B = 24;
  const keep = Math.min(f.history.length, Math.max(f.horizon * 4, compact ? 40 : 90));
  const hist = f.history.slice(-keep), fc = f.forecast, n = hist.length + fc.length;
  const vals = [...hist.map((p) => p.v), ...fc.map((p) => p.lo95), ...fc.map((p) => p.hi95)];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.06;
  lo -= pad; hi += pad;
  const iw = W - L - R, ih = H - T - B;
  const x = (i: number): number => L + (n > 1 ? i * iw / (n - 1) : iw / 2), y = (v: number): number => T + ih - (v - lo) / (hi - lo) * ih;
  const last = hist.length - 1, at = (i: number): string => `${x(i).toFixed(1)},${y(i <= last ? (hist[i]?.v ?? 0) : (fc[i - last - 1]?.yhat ?? 0)).toFixed(1)}`;
  const band = (k: '80' | '95'): string => {
    const up = fc.map((p, i) => `${x(last + 1 + i).toFixed(1)},${y(p[`hi${k}`]).toFixed(1)}`);
    const dn = fc.map((p, i) => `${x(last + 1 + i).toFixed(1)},${y(p[`lo${k}`]).toFixed(1)}`).reverse();
    return `<polygon points="${at(last)} ${up.join(' ')} ${dn.join(' ')}" fill="var(--accent)" fill-opacity="${k === '95' ? 0.12 : 0.22}" stroke="none"/>`;
  };
  const ticks = [0, 1, 2, 3].map((k) => lo + (hi - lo) * k / 3);
  const grid = ticks.map((t) => `<line x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}" stroke="var(--border)"/><text x="${L - 6}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--text-faint)">${esc(short.format(t))}</text>`).join('');
  const every = Math.max(1, Math.ceil(n / (compact ? 5 : 8)));
  const labels = Array.from({ length: n }, (_, i) => i).filter((i) => i % every === 0).map((i) => {
    const t = i <= last ? hist[i]?.t : fc[i - last - 1]?.t;
    return `<text x="${x(i).toFixed(1)}" y="${H - 7}" text-anchor="middle" font-size="11" fill="var(--text-muted)">${esc(when(t ?? '', f.grain))}</text>`;
  }).join('');
  const pos = new Map(hist.map((p, i) => [p.t, i]));
  const odd = f.anomalies.map((a) => { const i = pos.get(a.t); return i === undefined ? '' : `<circle cx="${x(i).toFixed(1)}" cy="${y(a.value).toFixed(1)}" r="4" fill="var(--danger)"><title>${esc(when(a.t, f.grain))}: ${esc(num(a.value))}, usually about ${esc(num(a.expected))}</title></circle>`; }).join('');
  const past = hist.map((_, i) => at(i)).join(' '), ahead = [last, ...fc.map((_, i) => last + 1 + i)].map(at).join(' ');
  const dots = fc.map((p, i) => `<circle cx="${x(last + 1 + i).toFixed(1)}" cy="${y(p.yhat).toFixed(1)}" r="${compact ? 2 : 2.6}" fill="var(--accent)"><title>${esc(when(p.t, f.grain))}: about ${esc(num(p.yhat))} (likely ${esc(num(p.lo80))} to ${esc(num(p.hi80))})</title></circle>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(f.text)}" style="width:100%;height:auto;display:block">${grid}${band('95')}${band('80')}` +
    `<line x1="${x(last).toFixed(1)}" x2="${x(last).toFixed(1)}" y1="${T}" y2="${T + ih}" stroke="var(--text-faint)" stroke-dasharray="3 3"/>` +
    `<polyline points="${past}" fill="none" stroke="var(--text-muted)" stroke-width="1.8"/><polyline points="${ahead}" fill="none" stroke="var(--accent)" stroke-width="2.4" stroke-dasharray="6 4"/>${dots}${odd}${labels}</svg>`;
}

function outHtml(f: Forecast): string {
  const first = f.forecast[0], what = f.value ? `${f.agg} of ${f.value}` : 'rows', vs = f.backtest.vs_baseline;
  const sumUp = !f.value || f.agg === 'sum' || f.agg === 'count';
  const total = sumUp ? f.forecast.reduce((a, p) => a + p.yhat, 0) : (f.forecast[f.forecast.length - 1]?.yhat ?? 0);
  const strip = first ? `<div class="an-sum stat-strip"><div><b>${esc(num(first.yhat))}</b>next ${esc(f.grain)}, likely ${esc(num(first.lo80))} to ${esc(num(first.hi80))}</div>` +
    `<div><b>${esc(num(total))}</b>${sumUp ? `over ${f.horizon} ${esc(f.grain)}s` : `in ${f.horizon} ${esc(f.grain)}s`}</div>` +
    `<div><b>${vs === null ? 'n/a' : `${vs > 0 ? '+' : ''}${Math.round(vs * 100)}%`}</b>accuracy vs ${esc(f.backtest.baseline.toLowerCase())}</div>` +
    `<div><b>${esc(f.model.name)}</b>model${f.season ? `, pattern every ${f.season} ${esc(f.grain)}s` : ''}</div></div>` : '';
  const warn = f.warnings.length ? `<div class="an-hint notice"><span><b>Read with care</b><br>${f.warnings.map(esc).join('<br>')}</span></div>` : '';
  const odd = f.anomalies.length ? `<h4>Unusual ${esc(f.grain)}s</h4><table class="an-doc list-table"><thead><tr><th>When</th><th>Value</th><th>Usually about</th><th style="width:100%">How far</th></tr></thead><tbody>${f.anomalies.map((a) => `<tr><td class="nowrap">${esc(when(a.t, f.grain))}</td><td class="num">${esc(num(a.value))}</td><td class="num">${esc(num(a.expected))}</td><td>${a.direction === 'high' ? 'higher' : 'lower'} than usual (z ${a.z})</td></tr>`).join('')}</tbody></table>` : '';
  const groups = f.groups?.length ? `<h4>By ${esc(f.group ?? '')}: expected change</h4><table class="an-doc list-table"><thead><tr><th style="width:100%">${esc(f.group ?? '')}</th><th>Next ${esc(f.grain)}</th><th>Average ahead</th><th>Average lately</th><th>Change</th></tr></thead><tbody>${f.groups.map((g) => `<tr><td>${esc(g.name)}${g.note ? ` <span class="faint">${esc(g.note)}</span>` : ''}</td><td class="num">${g.next === null ? '' : esc(num(g.next))}</td><td class="num">${g.ahead === null ? '' : esc(num(g.ahead))}</td><td class="num">${g.recent === null ? '' : esc(num(g.recent))}</td><td class="num">${g.change_pct === null ? '' : `${g.change_pct > 0 ? '+' : ''}${g.change_pct}%`}</td></tr>`).join('')}</tbody></table>` : '';
  const table = `<details><summary>The forecast in numbers</summary><table class="an-doc list-table"><thead><tr><th>When</th><th>Forecast</th><th>Likely (80%)</th><th style="width:100%">Possible (95%)</th></tr></thead><tbody>${f.forecast.map((p) => `<tr><td class="nowrap">${esc(when(p.t, f.grain))}</td><td class="num">${esc(num(p.yhat))}</td><td class="num nowrap">${esc(num(p.lo80))} to ${esc(num(p.hi80))}</td><td class="num">${esc(num(p.lo95))} to ${esc(num(p.hi95))}</td></tr>`).join('')}</tbody></table></details>`;
  return `<div class="an-card mini-card an-fc"><h4>${esc(what)} by ${esc(f.grain)}, ${f.horizon} ahead</h4>${strip}${forecastSvg(f)}<div class="an-hint notice"><span>${esc(f.text)}</span></div>${warn}${odd}${groups}${table}</div>`;
}

const opts = (list: [string, string][], cur: string): string => list.map(([v, n]) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(n)}</option>`).join('');

export async function forecastSheet(r: ResultInfo): Promise<string> {
  const st = (await api.stats(r.id)).columns, cols = r.columns;
  const dates = cols.filter((c) => /DATE|TIMESTAMP/.test(st[c]?.type ?? '')), nums = cols.filter((c) => st[c]?.numeric);
  const cats = cols.filter((c) => !st[c]?.numeric && !dates.includes(c) && (st[c]?.distinct ?? 0) >= 2 && (st[c]?.distinct ?? 0) <= 200);
  if (F.rid !== r.id) {
    F.rid = r.id;
    F.out = null;
    F.err = '';
    if (!cols.includes(F.date)) F.date = dates[0] ?? '';
    if (!cols.includes(F.value)) F.value = nums.find((c) => c !== F.date) ?? '';
    if (!cols.includes(F.group)) F.group = '';
  }
  const dateList: [string, string][] = [...dates, ...cols.filter((c) => !dates.includes(c))].map((c) => [c, c]);
  const none = !dates.length ? `<div class="an-hint notice"><span>This result has no date column. Add one to the query (an order date, a created time …) to forecast, or pick a column that holds dates as text.</span></div>` : '';
  return `${none}<div class="an-form form-stack an-testform"><label>Date<select id="an-f-date">${opts(dateList, F.date || (dates[0] ?? ''))}</select></label>` +
    `<label>Value<select id="an-f-val">${opts([['', 'Count of rows'], ...nums.map((c): [string, string] => [c, c])], F.value)}</select></label>` +
    `<label>Per step<select id="an-f-agg">${opts(AGGS.map((a): [string, string] => [a, a]), F.agg)}</select></label>` +
    `<label>Step<select id="an-f-grain">${opts(GRAINS.map(([a, b]): [string, string] => [a, b]), F.grain)}</select></label>` +
    `<label>Steps ahead<input id="an-f-h" type="number" min="1" max="120" placeholder="auto" value="${esc(F.horizon)}"></label>` +
    `<label>One per<select id="an-f-group">${opts([['', 'No split'], ...cats.map((c): [string, string] => [c, c])], F.group)}</select></label>` +
    `<button type="button" class="primary" data-act="fc-run">Forecast</button></div>` +
    `<div id="an-f-out">${F.err ? `<div class="an-hint notice bad"><span>${esc(F.err)}</span></div>` : F.out ? outHtml(F.out) : '<div class="faint">Pick the date and the number to follow over time, then Forecast. The last part of the history is held back to test how good the forecast is.</div>'}</div>`;
}

const val = (id: string): string => ($(id) as HTMLInputElement | HTMLSelectElement | null)?.value ?? '';

export const F_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  'fc-run': async (el) => {
    if (!F.rid) return;
    Object.assign(F, { date: val('an-f-date'), value: val('an-f-val'), agg: val('an-f-agg') || 'sum', grain: val('an-f-grain') || 'auto', horizon: val('an-f-h'), group: val('an-f-group') });
    const out = $req('an-f-out'), btn = el as HTMLButtonElement;
    btn.disabled = true;
    out.innerHTML = '<div class="faint">Testing models on your history…</div>';
    try {
      F.out = await api.forecast(F.rid, { date: F.date, value: F.value, agg: F.agg, grain: F.grain, horizon: F.horizon ? Number(F.horizon) : null, group: F.group });
      F.err = '';
      out.innerHTML = outHtml(F.out);
    } catch (e) {
      F.out = null;
      F.err = errText(e);
      out.innerHTML = `<div class="an-hint notice bad"><span>${esc(F.err)}</span></div>`;
    } finally { btn.disabled = false; }
  },
};
