// The Joins tab: what each join keeps and cuts, as a Venn drawn to scale. Counts come from the user's database, so they
// run only when asked. Circle areas follow the distinct join keys; a Venn cannot stay exact past two sets, so several
// joins are shown one step at a time, with the row flow beside them.

import { esc } from '../core/dom.js';
import { api, tab } from './state.js';
import type { Join, JoinStats, MTable, Tab } from './state.js';

function lens(r1: number, r2: number, d: number): number {
  if (d >= r1 + r2) return 0;
  if (d <= Math.abs(r1 - r2)) return Math.PI * Math.min(r1, r2) ** 2;
  const a = r1 * r1 * Math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1));
  const b = r2 * r2 * Math.acos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2));
  return a + b - 0.5 * Math.sqrt((-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2));
}

function venn(s: JoinStats, j: Join, a: string, b: string): string {
  const A = Math.max(s.a_keys, 1), B = Math.max(s.b_keys, 1), M = Math.min(s.matched_keys, A, B);
  const r1 = Math.sqrt(A / Math.PI), r2 = Math.sqrt(B / Math.PI);
  let lo = Math.abs(r1 - r2) + 1e-6, hi = r1 + r2;
  for (let i = 0; i < 60; i++) { const m = (lo + hi) / 2; if (lens(r1, r2, m) > M) lo = m; else hi = m; }
  const d = (lo + hi) / 2, w = 560, h = 232, k = Math.min(196 / (2 * Math.max(r1, r2)), (w - 190) / (r1 + r2 + d));
  const ra = r1 * k, rb = r2 * k, D = d * k, cx1 = (w - (ra + D + rb)) / 2 + ra, cx2 = cx1 + D, cy = h / 2;
  const x = D ? (D * D - rb * rb + ra * ra) / (2 * D) : 0, y = Math.sqrt(Math.max(0, ra * ra - x * x)), px = cx1 + x;
  const p1 = `${px} ${cy - y}`, p2 = `${px} ${cy + y}`, f = (v: boolean): number => (v ? 1 : 0);
  const lensP = `M${p1}A${ra} ${ra} 0 ${f(x < 0)} 1 ${p2}A${rb} ${rb} 0 ${f(D - x < 0)} 1 ${p1}Z`;
  const left = `M${p1}A${ra} ${ra} 0 ${f(x > 0)} 0 ${p2}A${rb} ${rb} 0 ${f(D - x < 0)} 1 ${p1}Z`;
  const right = `M${p1}A${ra} ${ra} 0 ${f(x < 0)} 1 ${p2}A${rb} ${rb} 0 ${f(D - x > 0)} 0 ${p1}Z`;
  const keepL = j.type === 'LEFT' || j.type === 'FULL', keepR = j.type === 'RIGHT' || j.type === 'FULL';
  const hatch = `url(#an-ha)`, none = M < 1;
  return `<svg class="an-venn" viewBox="0 0 ${w} ${h}" role="img" aria-label="Rows kept and cut by this join">
  <defs><pattern id="an-ha" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="7" height="7" fill="var(--danger-soft)"/><line x1="0" y1="0" x2="0" y2="7" stroke="var(--danger)" stroke-width="1.6" opacity=".7"/></pattern></defs>
  ${none ? `<circle cx="${cx1}" cy="${cy}" r="${ra}" fill="${keepL ? 'var(--accent)' : hatch}" fill-opacity="${keepL ? 0.55 : 1}"/><circle cx="${cx2}" cy="${cy}" r="${rb}" fill="${keepR ? 'var(--highlight)' : hatch}" fill-opacity="${keepR ? 0.55 : 1}"/>`
    : `<path d="${left}" fill="${keepL ? 'var(--accent)' : hatch}" fill-opacity="${keepL ? 0.55 : 1}"/><path d="${right}" fill="${keepR ? 'var(--highlight)' : hatch}" fill-opacity="${keepR ? 0.55 : 1}"/><path d="${lensP}" fill="var(--err-tp)" fill-opacity=".65"/>`}
  <text class="vt" x="${cx1 - ra - 8}" y="${cy - 2}" text-anchor="end">${s.lost_a.toLocaleString()}</text><text class="vs" x="${cx1 - ra - 8}" y="${cy + 15}" text-anchor="end">rows</text>
  <text class="vt" x="${(cx2 - rb + cx1 + ra) / 2}" y="${cy - 2}" text-anchor="middle">${s.joined.toLocaleString()}</text><text class="vs" x="${(cx2 - rb + cx1 + ra) / 2}" y="${cy + 15}" text-anchor="middle">matched</text>
  <text class="vt" x="${cx2 + rb + 8}" y="${cy - 2}">${s.lost_b.toLocaleString()}</text><text class="vs" x="${cx2 + rb + 8}" y="${cy + 15}">rows</text>
  <text class="vs" x="${Math.max(30, cx1 - ra * 0.55)}" y="14" text-anchor="middle">${esc(a)}</text><text class="vs" x="${Math.min(w - 30, cx2 + rb * 0.55)}" y="14" text-anchor="middle">${esc(b)}</text></svg>`;
}

export function joinsHtml(t: Tab): string {
  const m = t.model;
  if (!m?.joins.length) return '<div class="an-empty empty">Add a join in the Visual builder to see which rows it keeps and cuts.</div>';
  const i = Math.min(t.joinStep, m.joins.length - 1), j = m.joins[i] as Join;
  const name = (id: string): string => m.tables.find((x: MTable) => x.id === id)?.name ?? id;
  const a = name(j.a[0]), b = name(j.b[0]), s = t.joinStats[`${j.a.join('.')}|${j.b.join('.')}`];
  const steps = `<div class="an-steps">${m.joins.map((x, n) => `<button type="button" class="sm ${n === i ? 'primary' : ''}" data-act="join-step" data-i="${n}">${esc(x.type)}: ${esc(name(x.a[0]))} ⋈ ${esc(name(x.b[0]))}</button>`).join('')}</div>`;
  if (!s) return `<div class="an-pad">${steps}<p class="an-empty empty">This counts the rows on both sides in your database, so it runs only when you ask.</p><button type="button" class="primary" data-act="join-count" data-i="${i}">Count rows</button></div>`;
  const cut = (j.type === 'INNER' || j.type === 'RIGHT' ? s.lost_a : 0) + (j.type === 'INNER' || j.type === 'LEFT' ? s.lost_b : 0);
  const flow = Math.max(s.a_rows, s.kept, 1);
  return `<div class="an-pad">${steps}<div class="an-two"><div>${venn(s, j, a, b)}<div class="an-legend"><span><i class="ok"></i>kept, matched</span><span><i class="cut"></i>cut by this join</span><span class="faint">Circle size follows distinct join keys.</span></div></div>
  <div><div class="an-sum stat-strip"><div><b>${s.kept.toLocaleString()}</b>rows after this join</div><div><b>${cut.toLocaleString()}</b>rows cut</div></div>
  <table class="an-doc list-table"><tbody><tr><td>${esc(a)} rows with no match</td><td class="num">${s.lost_a.toLocaleString()}</td><td>${j.type === 'LEFT' || j.type === 'FULL' ? 'kept' : '<b>cut</b>'}</td></tr><tr><td>${esc(b)} rows with no match</td><td class="num">${s.lost_b.toLocaleString()}</td><td>${j.type === 'RIGHT' || j.type === 'FULL' ? 'kept' : '<b>cut</b>'}</td></tr></tbody></table>
  ${s.fan_out ? `<div class="an-hint notice"><span>One key matches several rows, so ${s.joined.toLocaleString()} rows come out. Check totals before you sum columns, because their values repeat.</span></div>` : ''}
  <div class="an-flow"><div class="an-fr"><span>${esc(a)}</span><div class="an-bar"><i style="width:${s.a_rows / flow * 100}%"></i></div><b class="num">${s.a_rows.toLocaleString()}</b></div><div class="an-fr"><span>${esc(j.type)} ${esc(b)}</span><div class="an-bar"><i style="width:${s.kept / flow * 100}%"></i></div><b class="num">${s.kept.toLocaleString()}</b></div></div></div></div></div>`;
}

export async function countJoin(i: number): Promise<void> {
  const t = tab(), m = t.model, j = m?.joins[i];
  if (!t.conn || !m || !j) return;
  t.joinStats[`${j.a.join('.')}|${j.b.join('.')}`] = await api.joinStats(t.conn, t.db, m, i);
}
