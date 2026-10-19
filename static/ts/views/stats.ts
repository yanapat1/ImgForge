// Class stats tab: per-class table (left) and the three Plotly charts (right).

import { $req, esc } from '../core/dom.js';
import { featureLabel, meta, pts, S } from '../core/state.js';
import { clsColor, plotBase, SCALE, THEME } from '../core/theme.js';
import { setSelection } from './gallery.js';
import { switchTab } from './layout.js';

import type * as PL from 'plotly.js';

declare const Plotly: typeof import('plotly.js');

let statsDrawn = false;

// a new feature space or theme: draw the stats charts again next time they are shown
export function invalidateStats(): void { statsDrawn = false; }

export function renderStats(): void {
  if (statsDrawn) { ['stat-counts', 'stat-area', 'stat-confusion'].forEach((d) => Plotly.Plots.resize(d)); return; }
  const P = pts(), m = meta();
  $req('stats-idle').classList.toggle('hidden', !!P.suggested);
  const suggested = P.suggested;
  if (!suggested) return;
  statsDrawn = true;
  const C = m.classes.length;
  // a fresh object per axis: Plotly writes the detected axis type back into it, so a shared object would
  // turn the y axis into a category axis too
  const ax = (): Partial<PL.LayoutAxis> => ({ gridcolor: THEME.grid, linecolor: THEME.border, zerolinecolor: THEME.border, tickfont: { color: THEME.muted } });
  const dark = { ...plotBase(THEME.surface, 12), xaxis: ax(), yaxis: ax() };
  const cfg: Partial<PL.Config> = { displaylogo: false, responsive: true };

  void Plotly.react('stat-counts', [{
    type: 'bar', x: m.classes, y: m.counts, marker: { color: m.classes.map((_, i) => clsColor(i)) },
    hovertemplate: '%{x}: %{y} boxes<extra></extra>',
  }], { ...dark, margin: { t: 12, b: 100, l: 50, r: 10 } }, cfg);

  const byCls: number[][] = m.classes.map(() => []);
  P.id.forEach((id) => { byCls[P.cls[id] ?? 0]?.push((P.area[id] ?? 0) * 100); });
  void Plotly.react('stat-area', m.classes.map((c, i): Partial<PL.PlotData> => ({
    type: 'box', name: c, y: byCls[i] ?? [], marker: { color: clsColor(i), size: 3 }, boxpoints: 'outliers',
  })), { ...dark, showlegend: false,
    yaxis: { ...ax(), type: 'log' }, margin: { t: 12, b: 100, l: 50, r: 10 } }, cfg);

  const z: number[][] = m.classes.map(() => new Array<number>(C).fill(0));
  P.id.forEach((id) => {
    const row = z[P.cls[id] ?? 0], c = suggested[id] ?? 0;
    if (row) row[c] = (row[c] ?? 0) + 1;
  });
  const zn = z.map((row) => { const s = row.reduce((a, b) => a + b, 0) || 1; return row.map((v) => v / s); });
  void Plotly.react('stat-confusion', [{
    type: 'heatmap', z: zn, x: m.classes, y: m.classes, customdata: z, colorscale: SCALE.heat(THEME), zmin: 0, zmax: 1,
    colorbar: { thickness: 10, outlinewidth: 0, tickformat: '.0%', tickfont: { color: THEME.muted } },
    hovertemplate: 'labelled <b>%{y}</b><br>neighbours say <b>%{x}</b><br>%{customdata} boxes (%{z:.1%})<extra></extra>',
  }], { ...dark, xaxis: { ...ax(), title: { text: 'kNN-majority class', font: { color: THEME.muted } }, tickangle: -40 },
    yaxis: { ...ax(), autorange: 'reversed' }, margin: { t: 12, b: 120, l: 130, r: 10 } }, cfg);
  $req('stat-confusion-sub').textContent = `· ${featureLabel(S.scoresFeature)}`;
  const el = $req<PL.PlotlyHTMLElement>('stat-confusion');
  if (!el.dataset['bound']) {
    el.dataset['bound'] = '1';
    el.on('plotly_click', (ev) => {
      const pt = ev.points[0];
      if (!pt) return;
      const r = m.classes.indexOf(String(pt.y)), c = m.classes.indexOf(String(pt.x));
      const cur = pts();
      const ids = cur.id.filter((id) => cur.cls[id] === r && cur.suggested?.[id] === c);
      $req<HTMLSelectElement>('sort').value = 'suspicion';
      setSelection(ids, `labelled ${m.classes[r]}, neighbours say ${m.classes[c]}`);
      switchTab('explore');
    });
  }
}

// class stats left panel: one row per class, click = load that class into the gallery
export function renderClassTable(): void {
  const m = meta(), P = pts(), total = m.counts.reduce((a, b) => a + b, 0) || 1;
  const sus = new Array<number>(m.classes.length).fill(0);
  const { suggested, suspicion } = P;
  if (suggested && suspicion) {
    P.id.forEach((id) => {
      const c = P.cls[id] ?? 0;
      if (suggested[id] !== c && (suspicion[id] ?? 0) >= 0.5) sus[c] = (sus[c] ?? 0) + 1;
    });
  }
  const cnt = (i: number): number => m.counts[i] ?? 0;
  const maxN = Math.max(1, ...m.counts);
  const rows = $req<HTMLTableElement>('class-table').tBodies[0];
  if (rows) rows.innerHTML = m.classes.map((c, i): [string, number] => [c, i]).sort((a, b) => cnt(b[1]) - cnt(a[1])).map(([c, i]) =>
    `<tr data-c="${i}" tabindex="0" role="button" title="load all ${esc(c)} boxes into the gallery">` +
    `<td class="bar-cell"><span class="cname"><span class="dot" style="background:${clsColor(i)}"></span>${esc(c)}</span>` +
    `<span class="share" style="width:${(100 * cnt(i) / maxN).toFixed(1)}%;background:${clsColor(i)}"></span></td>` +
    `<td class="r">${cnt(i).toLocaleString()}</td><td class="r">${(100 * cnt(i) / total).toFixed(1)}</td>` +
    `<td class="r${sus[i] ? ' sus' : ''}">${P.suggested ? (sus[i] ?? 0).toLocaleString() : '–'}</td></tr>`).join('');
  $req('stats-side-sub').textContent = P.suggested ? `suspicious: ${featureLabel(S.scoresFeature)}` : '';
}
