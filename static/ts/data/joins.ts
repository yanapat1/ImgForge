// The Joins tab: what each join keeps and cuts, as a Venn drawn to scale. Counts come from the user's database, so they
// run only when asked. Circle areas follow the distinct join keys. "All tables" draws every table of the query at
// once: each pair overlaps by its real share of keys, and the filled middle of each circle is the part of that table
// the query really uses. A Venn cannot stay exact past two sets, so each join also has its own two-circle step with
// the row flow beside it.

import { errText } from '../core/api.js';
import { esc } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { api, tab } from './state.js';
import type { Join, JoinOverview, JoinStats, MTable, Model, Tab } from './state.js';

function lens(r1: number, r2: number, d: number): number {
  if (d >= r1 + r2) return 0;
  if (d <= Math.abs(r1 - r2)) return Math.PI * Math.min(r1, r2) ** 2;
  const a = r1 * r1 * Math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1));
  const b = r2 * r2 * Math.acos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2));
  return a + b - 0.5 * Math.sqrt((-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2));
}

/** The distance between two circles whose overlap has area `m` (bisection on the lens area). */
function distFor(r1: number, r2: number, m: number): number {
  let lo = Math.abs(r1 - r2) + 1e-6, hi = r1 + r2;
  for (let i = 0; i < 60; i++) { const mid = (lo + hi) / 2; if (lens(r1, r2, mid) > m) lo = mid; else hi = mid; }
  return (lo + hi) / 2;
}

const statOf = (t: Tab, j: Join): JoinStats | undefined => t.joinStats[`${j.a.join('.')}|${j.b.join('.')}`];

function venn(s: JoinStats, j: Join, a: string, b: string): string {
  const A = Math.max(s.a_keys, 1), B = Math.max(s.b_keys, 1), M = Math.min(s.matched_keys, A, B);
  const r1 = Math.sqrt(A / Math.PI), r2 = Math.sqrt(B / Math.PI);
  const d = distFor(r1, r2, M), w = 560, h = 232, k = Math.min(196 / (2 * Math.max(r1, r2)), (w - 190) / (r1 + r2 + d));
  const ra = r1 * k, rb = r2 * k, D = d * k, cx1 = (w - (ra + D + rb)) / 2 + ra, cx2 = cx1 + D, cy = h / 2;
  const x = D ? (D * D - rb * rb + ra * ra) / (2 * D) : 0, y = Math.sqrt(Math.max(0, ra * ra - x * x)), px = cx1 + x;
  const p1 = `${px} ${cy - y}`, p2 = `${px} ${cy + y}`, f = (v: boolean): number => (v ? 1 : 0);
  const lensP = `M${p1}A${ra} ${ra} 0 ${f(x < 0)} 1 ${p2}A${rb} ${rb} 0 ${f(D - x < 0)} 1 ${p1}Z`;
  const left = `M${p1}A${ra} ${ra} 0 ${f(x > 0)} 0 ${p2}A${rb} ${rb} 0 ${f(D - x < 0)} 1 ${p1}Z`;
  const right = `M${p1}A${ra} ${ra} 0 ${f(x < 0)} 1 ${p2}A${rb} ${rb} 0 ${f(D - x > 0)} 0 ${p1}Z`;
  const keepL = j.type === 'LEFT' || j.type === 'FULL', keepR = j.type === 'RIGHT' || j.type === 'FULL';
  const hatch = `url(#dt-ha)`, none = M < 1;
  return `<svg class="dt-venn" viewBox="0 0 ${w} ${h}" role="img" aria-label="Rows kept and cut by this join">
  <defs><pattern id="dt-ha" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="7" height="7" fill="var(--danger-soft)"/><line x1="0" y1="0" x2="0" y2="7" stroke="var(--danger)" stroke-width="1.6" opacity=".7"/></pattern></defs>
  ${none ? `<circle cx="${cx1}" cy="${cy}" r="${ra}" fill="${keepL ? 'var(--accent)' : hatch}" fill-opacity="${keepL ? 0.55 : 1}"/><circle cx="${cx2}" cy="${cy}" r="${rb}" fill="${keepR ? 'var(--highlight)' : hatch}" fill-opacity="${keepR ? 0.55 : 1}"/>`
    : `<path d="${left}" fill="${keepL ? 'var(--accent)' : hatch}" fill-opacity="${keepL ? 0.55 : 1}"/><path d="${right}" fill="${keepR ? 'var(--highlight)' : hatch}" fill-opacity="${keepR ? 0.55 : 1}"/><path d="${lensP}" fill="var(--err-tp)" fill-opacity=".65"/>`}
  <text class="vt" x="${cx1 - ra - 8}" y="${cy - 2}" text-anchor="end">${s.lost_a.toLocaleString()}</text><text class="vs" x="${cx1 - ra - 8}" y="${cy + 15}" text-anchor="end">rows</text>
  <text class="vt" x="${(cx2 - rb + cx1 + ra) / 2}" y="${cy - 2}" text-anchor="middle">${s.joined.toLocaleString()}</text><text class="vs" x="${(cx2 - rb + cx1 + ra) / 2}" y="${cy + 15}" text-anchor="middle">matched</text>
  <text class="vt" x="${cx2 + rb + 8}" y="${cy - 2}">${s.lost_b.toLocaleString()}</text><text class="vs" x="${cx2 + rb + 8}" y="${cy + 15}">rows</text>
  <text class="vs" x="${Math.max(30, cx1 - ra * 0.55)}" y="14" text-anchor="middle">${esc(a)}</text><text class="vs" x="${Math.min(w - 30, cx2 + rb * 0.55)}" y="14" text-anchor="middle">${esc(b)}</text></svg>`;
}

// ---------------------------------------------------------------- All tables
const MAX_DRAWN = 12, W = 640, H = 380, MIN_R = 0.14;
/** what the overview was counted for: a change to the joins, the tables or the filter makes it stale */
const sigOf = (m: Model): string => JSON.stringify([m.joins, m.tables.map((x) => [x.id, x.database, x.schema, x.name, x.kind, x.sql ?? '']), m.parts?.where ?? '']);
const drawn = new Map<string, { key: string; html: string }>();      // tab id -> the last SVG (rebuilt only on a change)

interface Node { id: string; name: string; keys: number; rows: number | null; used: number | null; r: number; x: number; y: number }

/** Every table of the joins, from the pair counts (keys, rows) and the overview (keys the query uses). */
function nodesOf(m: Model, t: Tab, all: JoinOverview | null): Node[] {
  const name = (id: string): string => m.tables.find((x: MTable) => x.id === id)?.name ?? id;
  const out = new Map<string, Node>();
  for (const j of m.joins) {
    const s = statOf(t, j);
    for (const [side, id] of [['a', j.a[0]], ['b', j.b[0]]] as const) {
      if (out.has(id)) continue;                     // the first join of a table is its key (the server's too)
      const keys = s ? (side === 'a' ? s.a_keys : s.b_keys) : 0, rows = s ? (side === 'a' ? s.a_rows : s.b_rows) : null;
      const used = all ? all.used[id] ?? null : null;
      const k = Math.max(keys, used ?? 0, 1);
      out.set(id, { id, name: name(id), keys: k, rows, used, r: Math.sqrt(k / Math.PI), x: 0, y: 0 });
    }
  }
  const list = [...out.values()], big = Math.max(...list.map((n) => n.r));
  for (const n of list) n.r = Math.max(n.r, big * MIN_R);         // a 3-key table next to a million keys stays visible
  return list;
}

/** Walk the join tree from the first table: each table sits at the distance that gives its join's real overlap. */
function layout(m: Model, t: Tab, nodes: Node[]): void {
  const by = new Map(nodes.map((n) => [n.id, n]));
  const adj = new Map<string, Array<{ to: string; j: Join }>>();
  for (const j of m.joins) {
    adj.set(j.a[0], [...(adj.get(j.a[0]) ?? []), { to: j.b[0], j }]);
    adj.set(j.b[0], [...(adj.get(j.b[0]) ?? []), { to: j.a[0], j }]);
  }
  const placed = new Set<string>(), angle = new Map<string, number>();
  for (const root of nodes) {
    if (placed.has(root.id)) continue;               // another group of joins: beside the first one
    if (placed.size) { const right = Math.max(...nodes.filter((n) => placed.has(n.id)).map((n) => n.x + n.r)); root.x = right + root.r * 1.6; }
    placed.add(root.id);
    const queue = [root.id];
    while (queue.length) {
      const id = queue.shift() as string, p = by.get(id) as Node;
      const kids = (adj.get(id) ?? []).filter((e) => !placed.has(e.to) && by.has(e.to));
      const base = angle.get(id);
      kids.forEach((e, k) => {
        const c = by.get(e.to) as Node, s = statOf(t, e.j);
        const d = s ? distFor(p.r, c.r, Math.min(s.matched_keys, p.keys, c.keys)) : p.r + c.r;
        const a = base === undefined ? (2 * Math.PI * k) / kids.length : base + (kids.length === 1 ? 0 : (-Math.PI / 3) + (2 * Math.PI / 3) * (k / (kids.length - 1)));
        c.x = p.x + d * Math.cos(a); c.y = p.y + d * Math.sin(a);
        angle.set(c.id, a);
        placed.add(c.id);
        queue.push(c.id);
      });
    }
  }
  const joined = new Set(m.joins.flatMap((j) => [`${j.a[0]}|${j.b[0]}`, `${j.b[0]}|${j.a[0]}`]));
  for (let pass = 0; pass < 3; pass++) {             // tables with no join between them must not seem to share keys
    for (let i = 0; i < nodes.length; i++) for (let k = i + 1; k < nodes.length; k++) {
      const a = nodes[i] as Node, b = nodes[k] as Node;
      if (joined.has(`${a.id}|${b.id}`)) continue;
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1e-6, over = a.r + b.r - d;
      if (over <= 0) continue;
      const ux = dx / d, uy = dy / d;
      a.x -= ux * over / 2; a.y -= uy * over / 2; b.x += ux * over / 2; b.y += uy * over / 2;
    }
  }
}

/** An arc along a circle's outline from the top, clockwise, covering `f` of it (the share of keys the query uses). */
function gauge(cx: number, cy: number, r: number, f: number): string {
  if (f >= 0.999) return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" class="dt-vg"/>`;
  if (f <= 0) return '';
  const a = -Math.PI / 2 + 2 * Math.PI * f, x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
  return `<path d="M${cx} ${cy - r}A${r} ${r} 0 ${f > 0.5 ? 1 : 0} 1 ${x} ${y}" fill="none" class="dt-vg"/>`;
}

function allSvg(m: Model, nodes: Node[]): string {
  const minX = Math.min(...nodes.map((n) => n.x - n.r)), maxX = Math.max(...nodes.map((n) => n.x + n.r));
  const minY = Math.min(...nodes.map((n) => n.y - n.r)), maxY = Math.max(...nodes.map((n) => n.y + n.r));
  const padX = 60, top = 30, bottom = 24;
  const k = Math.min((W - 2 * padX) / Math.max(maxX - minX, 1e-6), (H - top - bottom) / Math.max(maxY - minY, 1e-6));
  const X = (x: number): number => padX + (x - minX) * k + ((W - 2 * padX) - (maxX - minX) * k) / 2;
  const Y = (y: number): number => top + (y - minY) * k + ((H - top - bottom) - (maxY - minY) * k) / 2;
  const by = new Map(nodes.map((n) => [n.id, n]));
  // translucent fills: where tables share keys the colour darkens; the outline arc is the share of keys the query
  // uses, the dashed rest of the outline the keys it leaves out
  const circles = nodes.map((n) => {
    const cx = X(n.x), cy = Y(n.y), r = n.r * k, f = n.used == null ? null : Math.min(1, n.used / n.keys);
    return `<circle cx="${cx}" cy="${cy}" r="${r}" class="dt-vc${f != null && f < 1 ? ' cut' : ''}"/>` + (f == null ? '' : gauge(cx, cy, r, f));
  }).join('');
  const boxes: Array<{ x: number; y: number; w: number }> = [];
  const labels = nodes.map((n) => {                  // the name above its circle, moved up until it overlaps no other
    const cx = X(n.x), r = n.r * k, w = n.name.length * 7 + 8;
    let y = Math.max(14, Y(n.y) - r - 6);
    for (let tries = 0; tries < 6 && boxes.some((b) => Math.abs(b.x - cx) < (b.w + w) / 2 && Math.abs(b.y - y) < 14); tries++) y = Math.max(14, y - 15);
    boxes.push({ x: cx, y, w });
    return `<text class="vs vn" x="${Math.min(W - w / 2, Math.max(w / 2, cx))}" y="${y}" text-anchor="middle">${esc(n.name)}</text>`;
  }).join('');
  const marks = m.joins.map((j, n) => {            // a numbered dot on each join: it opens that join's own step
    const a = by.get(j.a[0]), b = by.get(j.b[0]);
    if (!a || !b) return '';
    // on the line between the centres: where the overlap's chord crosses it, or the middle of the gap
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
    const at = d < a.r + b.r ? (d * d + a.r * a.r - b.r * b.r) / (2 * d) : a.r + (d - a.r - b.r) / 2;
    const f = Math.min(1, Math.max(0, at / d)), x = X(a.x + dx * f), y = Y(a.y + dy * f);
    return `<g class="dt-vj" data-act="join-step" data-i="${n}" role="button" tabindex="0" aria-label="${esc(`${j.type} join: ${a.name} and ${b.name}`)}"><title>${esc(`${j.type}: ${a.name} ⋈ ${b.name}`)}</title>` +
      `<circle cx="${x}" cy="${y}" r="10"/><text x="${x}" y="${y + 4}" text-anchor="middle" class="vs">${n + 1}</text></g>`;
  }).join('');
  return `<svg class="dt-venn dt-venn-all" viewBox="0 0 ${W} ${H}" role="img" aria-label="Every table of the query, sized by distinct join keys">${circles}${marks}${labels}</svg>`;
}

function allHtml(m: Model, t: Tab, steps: string): string {
  const all = t.joinAll && t.joinAll.sig === sigOf(m) ? t.joinAll.data : null;
  const counted = m.joins.every((j) => statOf(t, j));
  if (!all || !counted) {
    return `<div class="dt-pad">${steps}<p class="dt-empty empty">This counts the rows of every table in your database (one query per join, and one for the whole query), so it runs only when you ask.</p>` +
      '<button type="button" class="primary" data-act="join-count-all">Count all</button></div>';
  }
  const nodes = nodesOf(m, t, all).slice(0, MAX_DRAWN);
  const key = JSON.stringify([sigOf(m), all, m.joins.map((j) => statOf(t, j))]);
  let hit = drawn.get(t.id);
  if (!hit || hit.key !== key) {
    layout(m, t, nodes);
    hit = { key, html: allSvg(m, nodes) };
    drawn.set(t.id, hit);
  }
  const every = nodesOf(m, t, all);
  const rows = every.map((n) => `<tr><td>${esc(n.name)}</td><td class="num">${n.rows == null ? '' : n.rows.toLocaleString()}</td><td class="num">${n.keys.toLocaleString()}</td>` +
    `<td class="num">${n.used == null ? '' : n.used.toLocaleString()}</td><td class="num">${n.used == null ? '' : `${Math.round((n.used / n.keys) * 100)}%`}</td></tr>`).join('');
  return `<div class="dt-pad">${steps}<div class="dt-two"><div>${hit.html}<div class="dt-legend"><span><i class="use"></i>outline: share of its keys the query uses</span><span><i class="cut"></i>the keys it leaves out</span>` +
    `<span class="faint">Sizes follow distinct join keys (small tables are drawn bigger to stay visible). Each pair overlaps by its real share; where three circles meet is not to scale.${every.length > MAX_DRAWN ? ` The first ${MAX_DRAWN} tables are drawn.` : ''} Click a number to see that join.</span></div></div>` +
    `<div><div class="dt-sum stat-strip"><div><b>${all.rows.toLocaleString()}</b>rows the query keeps${m.parts?.where ? ' (with its filter)' : ''}</div><div><b>${every.length}</b>tables</div></div>` +
    `<table class="dt-doc list-table"><thead><tr><th>Table</th><th class="num">Rows</th><th class="num">Keys</th><th class="num">Used</th><th class="num">%</th></tr></thead><tbody>${rows}</tbody></table></div></div></div>`;
}

export function joinsHtml(t: Tab): string {
  const m = t.model;
  if (!m?.joins.length) return '<div class="dt-empty empty">Add a join in the Visual builder to see which rows it keeps and cuts.</div>';
  const name = (id: string): string => m.tables.find((x: MTable) => x.id === id)?.name ?? id;
  const multi = m.joins.length > 1;
  const i = t.joinStep < 0 && multi ? -1 : Math.max(0, Math.min(t.joinStep, m.joins.length - 1));
  const steps = `<div class="dt-steps">${multi ? `<button type="button" class="sm ${i === -1 ? 'primary' : ''}" data-act="join-step" data-i="-1">All tables</button>` : ''}` +
    `${m.joins.map((x, n) => `<button type="button" class="sm ${n === i ? 'primary' : ''}" data-act="join-step" data-i="${n}">${multi ? `${n + 1}. ` : ''}${esc(x.type)}: ${esc(name(x.a[0]))} ⋈ ${esc(name(x.b[0]))}</button>`).join('')}</div>`;
  if (i === -1) return allHtml(m, t, steps);
  const j = m.joins[i] as Join;
  const a = name(j.a[0]), b = name(j.b[0]), s = statOf(t, j);
  if (!s) return `<div class="dt-pad">${steps}<p class="dt-empty empty">This counts the rows on both sides in your database, so it runs only when you ask.</p><button type="button" class="primary" data-act="join-count" data-i="${i}">Count rows</button></div>`;
  const cut = (j.type === 'INNER' || j.type === 'RIGHT' ? s.lost_a : 0) + (j.type === 'INNER' || j.type === 'LEFT' ? s.lost_b : 0);
  const flow = Math.max(s.a_rows, s.kept, 1);
  return `<div class="dt-pad">${steps}<div class="dt-two"><div>${venn(s, j, a, b)}<div class="dt-legend"><span><i class="ok"></i>kept, matched</span><span><i class="cut"></i>cut by this join</span><span class="faint">Circle size follows distinct join keys.</span></div></div>
  <div><div class="dt-sum stat-strip"><div><b>${s.kept.toLocaleString()}</b>rows after this join</div><div><b>${cut.toLocaleString()}</b>rows cut</div></div>
  <table class="dt-doc list-table"><tbody><tr><td>${esc(a)} rows with no match</td><td class="num">${s.lost_a.toLocaleString()}</td><td>${j.type === 'LEFT' || j.type === 'FULL' ? 'kept' : '<b>cut</b>'}</td></tr><tr><td>${esc(b)} rows with no match</td><td class="num">${s.lost_b.toLocaleString()}</td><td>${j.type === 'RIGHT' || j.type === 'FULL' ? 'kept' : '<b>cut</b>'}</td></tr></tbody></table>
  ${s.fan_out ? `<div class="dt-hint notice"><span>One key matches several rows, so ${s.joined.toLocaleString()} rows come out. Check totals before you sum columns, because their values repeat.</span></div>` : ''}
  <div class="dt-flow"><div class="dt-fr"><span>${esc(a)}</span><div class="dt-bar"><i style="width:${s.a_rows / flow * 100}%"></i></div><b class="num">${s.a_rows.toLocaleString()}</b></div><div class="dt-fr"><span>${esc(j.type)} ${esc(b)}</span><div class="dt-bar"><i style="width:${s.kept / flow * 100}%"></i></div><b class="num">${s.kept.toLocaleString()}</b></div></div></div></div></div>`;
}

export async function countJoin(i: number): Promise<void> {
  const t = tab(), m = t.model, j = m?.joins[i];
  if (!t.conn || !m || !j) return;
  t.joinStats[`${j.a.join('.')}|${j.b.join('.')}`] = await api.joinStats(t.conn, t.db, m, i);
}

/** Count all: the whole query once, and each join not counted yet, at the same time. */
export async function countAll(): Promise<void> {
  const t = tab(), m = t.model, conn = t.conn;
  if (!conn || !m?.joins.length) return;
  const sig = sigOf(m);
  try {
    const [all] = await Promise.all([
      api.joinOverview(conn, t.db, m),
      ...m.joins.map((j, i) => (statOf(t, j) ? null : api.joinStats(conn, t.db, m, i).then((s) => { t.joinStats[`${j.a.join('.')}|${j.b.join('.')}`] = s; }))),
    ]);
    t.joinAll = { sig, data: all };
  } catch (e) { toast(errText(e), 'err'); }
}
