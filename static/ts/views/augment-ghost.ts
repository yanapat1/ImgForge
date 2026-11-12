// Augment › Show on the map: the recipe's augmented boxes as open diamonds on the Explore map. The server embeds their
// crops with the map's own feature and gives each its nearest session boxes with weights (routes/augment.py ghost);
// here each one is drawn at the weighted mean of those boxes' positions on the map now on screen, so it follows any
// projection of that feature. It shows where the augmentation pushes the data: next to its source class, into
// another class's cluster (a step that changes what an object looks like too much), or into empty space.

import { errText, getJSON, postJSON } from '../core/api.js';
import { $, esc } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { S } from '../core/state.js';
import { clsColor, clsName, THEME } from '../core/theme.js';
import { toast } from '../core/toast.js';
import { is3d, plotDiv, xyOf } from '../plot/access.js';
import { OVERLAY } from '../plot/marks.js';
import type { Job } from '../types.js';
import { AG, augName } from './augment.js';
import { switchTab } from './layout.js';

declare const Plotly: typeof import('plotly.js');

interface Ghosts { key: string; feature: string; n: number; cls: number[]; image: string[]; applied: string[][]; nbr: number[][]; w: number[][]; sim: number[] }

const NAME = 'aug-ghost';
OVERLAY.add(NAME);
let G: Ghosts | null = null, on = false, hooked = false, adding = false;

function positions(g: Ghosts): { x: number[]; y: number[] } {
  const x: number[] = [], y: number[] = [];
  g.nbr.forEach((ids, i) => {
    let sx = 0, sy = 0, sw = 0;
    ids.forEach((id, j) => {
      const [px, py] = xyOf(id), w = g.w[i]?.[j] ?? 0;
      if (Number.isFinite(px) && Number.isFinite(py)) { sx += px * w; sy += py * w; sw += w; }
    });
    x.push(sw ? sx / sw : NaN);
    y.push(sw ? sy / sw : NaN);
  });
  return { x, y };
}

function draw(): void {
  const gd = plotDiv();
  if (!gd.data || !G) return;
  const at = gd.data.findIndex((t) => t.name === NAME);
  if (!on || is3d() || !S.view || (S.view as { key?: string }).key?.split('|')[0] !== G.feature) {
    if (at >= 0) void Plotly.deleteTraces(gd, at);
    return;
  }
  const { x, y } = positions(G);
  const dot = Number(($('dot-size') as HTMLInputElement | null)?.value ?? 6);
  const text = G.applied.map((a, i) => `augmented ${esc(clsName(G?.cls[i] ?? 0) ?? '')} · ${a.map(augName).join(', ') || 'unchanged'} · nearest similarity ${(G?.sim[i] ?? 0).toFixed(2)}`);
  const trace = { type: 'scattergl', mode: 'markers', name: NAME, x, y, text, hoverinfo: 'text', showlegend: false,
    marker: { symbol: 'diamond-open', size: dot + 5, color: G.cls.map((c) => clsColor(c)), line: { width: 2 }, opacity: 0.95 } };
  if (at >= 0) void Plotly.restyle(gd, { x: [x], y: [y], text: [text], 'marker.color': [trace.marker.color] } as Plotly.Data, [at]);
  else { adding = true; void Plotly.addTraces(gd, trace as Plotly.Data).finally(() => { adding = false; }); }
  chip();
}

function chip(): void {
  const host = $('tab-embed');
  if (!host) return;
  let el = $('ag-ghost-chip');
  if (!on || !G) { el?.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.id = 'ag-ghost-chip';
    el.className = 'ag-ghost-chip';
    host.append(el);
    el.addEventListener('click', (e) => { if ((e.target as Element).closest('button')) hideGhosts(); });
  }
  el.innerHTML = `<span style="color:${THEME.accent}">◇</span> ${G.n} augmented boxes on the map <button type="button" class="sm">Hide</button>`;
}

export function hideGhosts(): void { on = false; draw(); chip(); }

/** Run the placement for the recipe (a job), then show the map with the diamonds. */
export async function showGhosts(): Promise<void> {
  const key = (S.view as { key?: string } | null)?.key;
  if (!key) { toast('Generate the map on Explore first: the augmented boxes are placed on it', 'err'); return; }
  if (is3d()) { toast('Augmented boxes are drawn on a 2D map: switch the projection to 2D', 'err'); return; }
  const st = $('ag-st');
  try {
    if (st) st.textContent = 'Placing the augmented boxes…';
    const r = await postJSON<{ ready: boolean; key: string; result?: Ghosts; job_id?: number }>('api/augment/ghost', { recipe: AG.recipe, feature: key.split('|')[0], n: 60, k: 1, seed: AG.seed });
    if (r.ready) G = r.result ?? null;
    else if (r.job_id !== undefined) {
      await waitJob<Job>(r.job_id, (j) => { if (st) st.textContent = j.stage ?? 'Working…'; });
      G = (await getJSON<{ result: Ghosts }>(`api/augment/coverage/result?key=${encodeURIComponent(r.key)}`)).result;
    }
  } catch (e) { toast(errText(e), 'err'); return; } finally { if (st) st.textContent = ''; }
  if (!G) return;
  on = true;
  switchTab('explore');
  const gd = plotDiv();
  if (!hooked && gd.on) {
    hooked = true;                         // the map is redrawn from scratch at times: put the diamonds back
    gd.on('plotly_afterplot', () => { if (on && !adding && gd.data && !gd.data.some((t) => t.name === NAME)) draw(); });
  }
  draw();
  toast(`${G.n} augmented boxes drawn as open diamonds, in their class colour`);
}
