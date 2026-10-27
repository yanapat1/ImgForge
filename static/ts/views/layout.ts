// Page layout: activity bar tabs, the draggable splitter, the bar above the plot (its popovers) and the class filter chips.

import { $, $req, esc, icon } from '../core/dom.js';
import { arrowNav, bindPopover } from '../core/popover.js';
import { meta, S } from '../core/state.js';
import { clsColor } from '../core/theme.js';
import { renderPlot, scheduleRender } from '../plot/render.js';
import { reapplyInspect, saveSettings } from './controls.js';
import { showAiTab } from './assistant.js';
import { EVAL_PLOTS, showEvalTab } from './eval.js';
import { closestOf } from './ev-util.js';
import { CMP_PLOTS, showCompareTab } from './compare.js';
import { MODEL_PLOTS, showModelTab } from './model.js';
import { renderClassTable, renderStats } from './stats.js';

// Plotly is loaded as a classic script by main.ts (window.Plotly)
declare const Plotly: typeof import('plotly.js');

const mainEl = (): HTMLElement => {
  const m = document.querySelector('main');
  if (!m) throw new Error('missing <main>');
  return m;
};

// The class chips are one line that scrolls sideways (the mouse wheel scrolls it too); the chevron at its end wraps
// them onto up to 3 lines instead. Kept as it was across rebuilds.
let cfOpen = false;

function syncClassFilterOpen(): void {
  const el = $req('class-filter'), b = el.querySelector<HTMLElement>('.cf-expand');
  el.classList.toggle('open', cfOpen);
  if (!b) return;
  b.setAttribute('aria-expanded', String(cfOpen));
  b.setAttribute('aria-label', cfOpen ? 'Show the class chips on one line' : 'Show every class chip');
  b.title = cfOpen ? 'One line' : 'Show every class';
}

export function buildClassFilter(): void {
  const el = $req('class-filter');
  const m = meta();
  el.innerHTML = m.classes.map((c, i) =>
    `<span class="chip" data-c="${i}" role="button" tabindex="0" aria-pressed="true" title="click: toggle · double-click: only this class">` +
    `<span class="dot" style="background:${clsColor(i)}"></span>${esc(c)} <span class="muted">${m.counts[i]}</span></span>`
  ).join('') + '<span class="chip chip-all" data-c="all" role="button" tabindex="0">show all</span>' +
    `<button type="button" class="icon-btn ghost cf-expand" aria-controls="class-filter">${icon('chev-d')}</button>`;
  syncClassFilterOpen();
  el.onwheel = (e) => {
    if (cfOpen || e.deltaX || e.ctrlKey || el.scrollWidth <= el.clientWidth) return;
    e.preventDefault();
    el.scrollLeft += e.deltaY * (e.deltaMode === 1 ? 16 : 1);
  };
  let clickTimer: ReturnType<typeof setTimeout> | undefined;
  el.onclick = (e) => {
    if (closestOf(e, '.cf-expand')) { cfOpen = !cfOpen; syncClassFilterOpen(); return; }
    const chip = closestOf(e, '.chip');
    if (!chip) return;
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => {
      if (chip.dataset.c === 'all') S.hidden.clear();
      else {
        const c = +(chip.dataset.c ?? NaN);
        if (S.hidden.has(c)) S.hidden.delete(c); else S.hidden.add(c);
      }
      syncClassFilter();
      scheduleRender();
      saveSettings();
      reapplyInspect();
    }, 220);
  };
  el.ondblclick = (e) => {
    const chip = closestOf(e, '.chip');
    if (!chip || chip.dataset.c === 'all') return;
    clearTimeout(clickTimer);
    S.hidden = new Set(meta().classes.map((_, i) => i).filter((i) => i !== +(chip.dataset.c ?? NaN)));
    syncClassFilter();
    renderPlot();
    saveSettings();
    reapplyInspect();
  };
}

export function syncClassFilter(): void {
  document.querySelectorAll<HTMLElement>('#class-filter .chip[data-c]').forEach((ch) => {
    if (ch.dataset.c === 'all') return;
    const off = S.hidden.has(+(ch.dataset.c ?? NaN));
    ch.classList.toggle('off', off);
    ch.setAttribute('aria-pressed', String(!off));
  });
}

// activity bar: each view has a left panel and a right panel
// (curation.js also syncs its two panels from the act-curation aria-selected attribute: same ids, same result)
// (ai: the ◆ assistant chat on the left, the plot stays on the right so you can lasso and ask)
const VIEWS: Record<string, string[]> = { ai: ['ai-side', 'tab-embed'], explore: ['gallery-panel', 'tab-embed'], compare: ['cmp-side', 'tab-embed'], stats: ['stats-side', 'tab-stats'], model: ['model-side', 'tab-model'], eval: ['eval-side', 'tab-eval'], curation: ['cur-side', 'tab-curation'] };

export let curTab = 'explore';

export function switchTab(tab: string): void {
  $req('hover-tip').classList.add('hidden');
  if (tab === curTab) {                         // clicking the active icon collapses / expands the left panel
    setCollapsed(S.collapsed === 'left' ? null : 'left');
  } else {
    if (S.collapsed) setCollapsed(null);        // another view: show both of its panels
    curTab = tab;
  }
  document.querySelectorAll<HTMLElement>('#activity .act[data-tab]').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === tab);
    b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  });
  // a panel shared by two views (the plot) stays shown when either is active
  const shown = new Set(VIEWS[tab] || []);
  for (const ids of Object.values(VIEWS)) ids.forEach((id) => { const el = $(id); if (el) el.classList.toggle('hidden', !shown.has(id)); });
  mainEl().dataset.tab = tab;   // css: #ai-side only on the ai tab (core/llm.js also toggles it)
  if (tab === 'stats') { renderClassTable(); renderStats(); }
  if (tab === 'model') void showModelTab();
  if (tab === 'eval') void showEvalTab();
  if (tab === 'ai') showAiTab();
  if (tab === 'compare') void showCompareTab();
  resizePlots();
}

export function resizePlots(): void {
  const ids = curTab === 'stats' ? ['stat-counts', 'stat-area', 'stat-confusion'] : curTab === 'model' ? MODEL_PLOTS : curTab === 'eval' ? EVAL_PLOTS
    : curTab === 'compare' ? ['plot', ...CMP_PLOTS] : ['plot'];
  for (const id of ids) { const el = $<Plotly.PlotlyHTMLElement>(id); if (el && el.data) Plotly.Plots.resize(el); }
}

const MIN_LEFT = 240, MIN_RIGHT = 360;          // the narrowest usable panels
const SNAP_LEFT = 140, SNAP_RIGHT = 200;        // dragged narrower than this: that side collapses

export function setLeftWidth(px: number): void {
  const main = mainEl();
  const max = Math.max(MIN_LEFT, main.clientWidth - MIN_RIGHT);
  S.leftW = Math.round(Math.max(MIN_LEFT, Math.min(max, +px)));
  main.style.setProperty('--left-w', `${S.leftW}px`);
}

// the width the left panel has when nothing is collapsed: the last one set, or the CSS default
function restoreLeftWidth(): void {
  const main = mainEl();
  if (S.leftW) setLeftWidth(S.leftW); else main.style.removeProperty('--left-w');
}

let resizeFrame = 0;
const resizeSoon = (): void => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(resizePlots); };

// side: 'left' | 'right' | null (both shown). S.leftW is kept, so reopening goes back to the last width.
export function setCollapsed(side: string | null, save = true): void {
  const main = mainEl();
  S.collapsed = side === 'left' || side === 'right' ? side : null;
  main.classList.toggle('left-collapsed', S.collapsed === 'left');
  main.classList.toggle('right-collapsed', S.collapsed === 'right');
  restoreLeftWidth();
  const sp = $('splitter');
  if (sp) sp.setAttribute('aria-valuetext', S.collapsed ? `${S.collapsed} panel collapsed` : 'both panels shown');
  resizeSoon();
  if (save) saveSettings();
}

export function bindSplitter(): void {
  const sp = $req('splitter'), main = mainEl();
  let last = 0, timer: ReturnType<typeof setTimeout> | undefined;
  const throttledResize = (): void => {                // re-flowing a big WebGL plot is not free: at most every 100 ms
    const now = performance.now();
    clearTimeout(timer);
    if (now - last > 100) { last = now; resizePlots(); } else timer = setTimeout(resizePlots, 100);
  };
  sp.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || closestOf(e, '.splitter-open')) return;
    e.preventDefault();
    sp.setPointerCapture(e.pointerId);
    sp.classList.add('dragging');
    document.body.classList.add('resizing');
    const left = main.getBoundingClientRect().left + $req('activity').offsetWidth;   // where the left panel starts
    const avail = main.clientWidth - $req('activity').offsetWidth - sp.offsetWidth;  // left + right panels
    const x0 = e.clientX;
    let w: number | null = null;                 // the raw dragged width: may go below the minimums until release
    const move = (ev: PointerEvent): void => {
      if (w === null && Math.abs(ev.clientX - x0) < 3) return;   // a click, not a drag
      w = Math.round(Math.max(0, Math.min(avail, ev.clientX - left)));
      if (S.collapsed) {                         // dragging a collapsed side back out
        S.collapsed = null;
        main.classList.remove('left-collapsed', 'right-collapsed');
      }
      main.style.setProperty('--left-w', `${w}px`);
      throttledResize();
    };
    const up = (): void => {
      sp.removeEventListener('pointermove', move);
      sp.removeEventListener('pointerup', up);
      sp.removeEventListener('pointercancel', up);
      sp.classList.remove('dragging');
      document.body.classList.remove('resizing');
      clearTimeout(timer);
      if (w === null) return;
      if (w < SNAP_LEFT) setCollapsed('left');
      else if (avail - w < SNAP_RIGHT) setCollapsed('right');
      else { setLeftWidth(w); setCollapsed(null); }
    };
    sp.addEventListener('pointermove', move);
    sp.addEventListener('pointerup', up);
    sp.addEventListener('pointercancel', up);
  });
  $req('splitter-open').addEventListener('click', () => setCollapsed(null));
  sp.addEventListener('dblclick', (e) => {
    if (closestOf(e, '.splitter-open')) return;
    if (S.collapsed) { setCollapsed(null); return; }   // a collapsed side comes back at its last width
    main.style.removeProperty('--left-w');       // back to the CSS default width
    S.leftW = null;
    resizePlots();
    saveSettings();
  });
  sp.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    if (e.ctrlKey && e.altKey) return;           // the collapse hotkeys below
    e.preventDefault();
    if (S.collapsed) { setCollapsed(null); return; }
    const first = VIEWS[curTab]?.[0];
    const side = first ? $(first) : null;
    const base = S.leftW || (side && side.offsetWidth) || parseFloat(getComputedStyle(main).getPropertyValue('--left-w')) || MIN_LEFT;
    setLeftWidth(base + (e.key === 'ArrowLeft' ? -16 : 16));
    throttledResize();
    saveSettings();
  });
  // Ctrl+Alt+← collapses the left side, Ctrl+Alt+→ the right one; the same keys again bring it back
  document.addEventListener('keydown', (e) => {
    if (!e.ctrlKey || !e.altKey || e.shiftKey || e.metaKey || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    if (e.target instanceof Element && e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    if (!$req('modal').classList.contains('hidden') || window.innerWidth <= 900) return;
    e.preventDefault();
    const side = e.key === 'ArrowLeft' ? 'left' : 'right';
    setCollapsed(S.collapsed === side ? null : side);
  });
  window.addEventListener('resize', () => { if (S.leftW) setLeftWidth(S.leftW); });
}

export function updateGroupsBadge(): void {
  const n = ['g-bubbles', 'g-outliers', 'g-dim'].filter((id) => $req<HTMLInputElement>(id).checked).length;
  $req('groups-badge').textContent = String(n);
  $req('groups-badge').classList.toggle('hidden', !n);
}

export function bindGroupsPopover(): void {
  bindPopover('btn-groups', 'groups-pop');
  for (const id of ['g-bubbles', 'g-outliers', 'g-dim']) $req(id).addEventListener('change', updateGroupsBadge);
}

// ---- the bar above the plot: popover triggers show what is chosen inside them
const optText = (id: string): string => {
  const el = $<HTMLSelectElement>(id);
  const o = el && el.selectedOptions[0];
  return o ? (o.textContent ?? '').replace(/^[✓\s]+/, '').trim() : '';
};

/** Trigger labels: "feature · method" and the classes shown. */
export function updateBarSummary(): void {
  const map = [optText('feature'), optText('method')].filter(Boolean).join(' · ');
  // the trigger drops the feature's "(…)" detail: "Box geometry · PCA"; the title keeps it all
  $req('map-summary').textContent = map.replace(/\s*\([^)]*\)/g, '') || 'Projection';
  $req('map-btn').title = `Feature space and projection method${map ? `: ${map}` : ''}`;
  const mode = $req<HTMLSelectElement>('mode').value;
  const cls = mode === 'single' ? optText('class-a') : mode === 'compare' ? `${optText('class-a')} vs ${optText('class-b')}` : 'All classes';
  $req('cls-summary').textContent = cls;
  $req('cls-btn').setAttribute('aria-label', `Classes shown: ${cls}`);
}

/** The dot on the projection trigger: method parameters changed since the view was computed (Re-project applies). */
export function setMapDirty(on: boolean): void {
  $req('map-dirty').classList.toggle('hidden', !on);
}

export function bindBar(): void {
  bindPopover('map-btn', 'map-pop');
  bindPopover('cls-btn', 'cls-pop');
  const issues = bindPopover('issues-btn', 'issues-pop');
  arrowNav('issues-pop');
  // picking a check closes the menu (the gallery shows the result); the run button keeps it open for its progress
  $req('issues-pop').addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest('button.menu-item') : null;
    if (b && !(b as HTMLButtonElement).disabled) issues.close();
  });
  for (const id of ['feature', 'method', 'mode', 'class-a', 'class-b']) $req(id).addEventListener('change', updateBarSummary);
  // "/" focuses the text search (not while typing somewhere)
  document.addEventListener('keydown', (e) => {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement ||
        (t instanceof HTMLElement && t.isContentEditable)) return;
    const q = $req<HTMLInputElement>('text-q');
    if (q.disabled || q.offsetParent === null) return;
    e.preventDefault();
    q.focus();
  });
}

/** The Find issues trigger is highlighted while one of its checks fills the gallery. */
export function syncIssuesBtn(): void {
  const on = !!$req('issues-pop').querySelector('.view-btn.active');
  $req('issues-btn').classList.toggle('on', on);
}

// ---- hide / show the bar above the plot (remembered per browser)
const CTL_KEY = 'controlsCollapsed';

function ctlSummary(): string {
  const txt = (id: string): string => { const el = $<HTMLSelectElement>(id); const o = el && el.selectedOptions[0]; return o ? (o.textContent ?? '').replace(/^[✓\s]+/, '').trim() : ''; };
  return [txt('feature'), txt('method'), txt('mode')].filter(Boolean).map((t) => `<b>${esc(t)}</b>`).join(' · ');
}

export function setControlsCollapsed(on: boolean): void {
  const c = $req('controls'), btn = $req('ctl-toggle');
  c.classList.toggle('collapsed', on);
  btn.setAttribute('aria-expanded', String(!on));
  btn.title = on ? 'Show controls' : 'Hide controls (more room for the plot)';
  btn.setAttribute('aria-label', btn.title);
  if (on) $req('ctl-mini').innerHTML = `${ctlSummary()} <span class="muted">· show controls</span>`;
  try { localStorage.setItem(CTL_KEY, on ? '1' : ''); } catch (e) { /* private window */ }
  resizePlots();
}

export function bindControlsToggle(): void {
  $req('ctl-toggle').onclick = () => setControlsCollapsed(!$req('controls').classList.contains('collapsed'));
  $req('ctl-mini').onclick = () => setControlsCollapsed(false);
  let on = false;
  try { on = localStorage.getItem(CTL_KEY) === '1'; } catch (e) { /* private window */ }
  if (on) setControlsCollapsed(true);
}
