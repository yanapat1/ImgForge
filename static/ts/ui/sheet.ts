// The UI sheet (?ui): every shared part of the page, drawn with the real stylesheets and the same helpers the screens
// use (core/header.ts, icon(), openDialog, toast), so it cannot drift from the app. A screen is built from these parts;
// a new part goes into css/kit.css and onto this page first (design/ui-sheet/README.md). The switches at the top only
// preview a theme, look or palette on this page; they do not change the user's settings.

import { $req, esc, icon } from '../core/dom.js';
import { appHeader } from '../core/header.js';
import { LOOKS, PALETTES } from '../core/theme.js';
import { actionToast, toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import { mountUserMenu } from '../views/usermenu.js';

interface Part { id: string; title: string; use: string; classes: string; demo: string }

const LOOK_LABEL: Record<string, string> = { default: 'Instrument', editorial: 'Editorial', blueprint: 'Blueprint', brutalist: 'Brutalist', terminal: 'Terminal', soft: 'Soft' };
const code = (s: string): string => s.split(' ').map((c) => `<code>${esc(c)}</code>`).join(' ');

const PARTS: Part[] = [
  { id: 'header', title: 'Page header', classes: '.app-header .brand .hdr-nav .header-actions',
    use: 'Top of every page. The brand goes home, the links switch pages (the current one is underlined), settings and theme sit on the right. Built by appHeader() in core/header.ts.',
    demo: `<div class="ui-frame">${appHeader({ page: 'analysis', actionsId: 'ui-hact' })}</div>` },
  { id: 'activity', title: 'Activity bar', classes: '.activity-bar .act .act.active .act-sep .act-bottom',
    use: 'The icon column on the left of a work page: one icon per view, the active one filled with a bar at its edge. Groups are split by .act-sep; settings or a panel toggle sit at the bottom.',
    demo: `<nav class="activity-bar ui-rail" aria-label="Example views"><button class="act active" title="Query">${icon('search')}</button><button class="act" title="Catalog">${icon('db')}</button>` +
      `<button class="act" title="Analyze">${icon('bars')}</button><div class="act-sep" role="separator"><span>more</span></div><button class="act" title="History">${icon('list')}</button>` +
      `<button class="act act-bottom" title="Settings">${icon('gear')}</button></nav>` },
  { id: 'side', title: 'Side panel', classes: '.left-panel .side-head .side-scroll',
    use: 'The panel next to the activity bar: a title row, then a list that scrolls by itself.',
    demo: `<section class="left-panel ui-side"><div class="side-head"><h2>Objects</h2><span class="muted hint">3 connections</span></div>` +
      `<div class="side-scroll"><div class="check">${icon('db')} sales · public</div><div class="check">${icon('db')} test2 · dbo</div><div class="check">${icon('folder')} finance-exports</div></div></section>` },
  { id: 'buttons', title: 'Buttons', classes: 'button button.primary button.ghost button.danger-btn button.sm a.btn .icon-btn .icon-btn.xs',
    use: 'One primary action per area. Ghost for quiet actions in toolbars and headers, danger for removing things (always behind a confirm). .sm inside dense toolbars, .icon-btn when an icon says it all (with a title).',
    demo: `<div class="ui-row"><button class="primary">${icon('play')}Run</button><button>${icon('refresh')}Refresh</button><button class="ghost">Cancel</button><button class="danger-btn">${icon('trash')}Delete</button><button disabled>Disabled</button></div>` +
      `<div class="ui-row"><button class="sm primary">${icon('play')}Run</button><button class="sm">Copy</button><button class="icon-btn" title="Settings">${icon('gear')}</button><button class="icon-btn ghost" title="Close">${icon('x')}</button><button class="icon-btn ghost xs" title="Rename">${icon('edit')}</button><a class="btn sm" href="#ui-buttons">${icon('download')}Export (a link)</a></div>` },
  { id: 'segmented', title: 'Segmented choice', classes: '.segmented .view-btn .view-btn.active',
    use: 'A few exclusive options shown at once (a mode, a layout). More than five: use a select.',
    demo: `<div class="segmented"><button class="view-btn active">Standard</button><button class="view-btn">Table on top</button><button class="view-btn">Table only</button></div>` },
  { id: 'inputs', title: 'Inputs', classes: 'select input.txt input.txt.sm input[type=number] input[type=range] .check .fld .help .form-stack',
    use: 'Labelled fields in forms (.fld: label above, help below; .form-stack lays a dialog form out). input.txt for free text on its own (.sm in a dense toolbar); a .check row for a yes / no.',
    demo: `<div class="ui-row"><select aria-label="Connection"><option>sales (PostgreSQL)</option><option>test2 (SQL Server)</option></select><input class="txt" placeholder="Find table or column" aria-label="Find"><input class="txt sm" placeholder="Filter these rows" aria-label="Filter"><input type="number" value="20" aria-label="Number"><input type="range" aria-label="Range"></div>` +
      `<div class="ui-grid2"><label class="fld">Name<input type="text" value="Orders by customer"><span class="help">Shown on the tab and in History.</span></label><label class="check"><input type="checkbox" checked> Let AI read sample rows</label></div>` +
      `<div class="form-stack" style="max-width:420px"><label>Column<select><option>total</option></select></label><label>Cast to<input type="text" value="int"></label><label class="check"><input type="checkbox"> Use TRY_CAST</label></div>` },
  { id: 'tabs', title: 'Tabs', classes: '.tabbar [role=tab][aria-selected] .tabbar.sm',
    use: 'Pages of one area. .tabbar on a page (the Sessions home); .tabbar.sm inside a panel (query tabs, Results | Joins | Cost), with small actions at its end.',
    demo: `<div class="tabbar" role="tablist"><button role="tab" aria-selected="true">${icon('scatter')}Visualize<span class="count-badge">4</span></button><button role="tab" aria-selected="false">${icon('tag')}Label<span class="count-badge">2</span></button></div>` +
      `<div class="tabbar sm" role="tablist"><button role="tab" aria-selected="true">Results</button><button role="tab" aria-selected="false">Joins</button><button role="tab" aria-selected="false">Cost</button><span class="grow"></span><span class="pill"><b>1.24 s</b>ran in</span></div>` },
  { id: 'toolbar', title: 'Toolbar', classes: '.toolbar .grow .sep',
    use: 'A row of controls above a pane: the main action first, then choices, then status at the end.',
    demo: `<div class="toolbar"><button class="primary sm">${icon('play')}Run</button><button class="sm">${icon('stop')}Stop</button><span class="sep"></span><select aria-label="Connection"><option>sales</option></select><span class="badge ok">PG</span><span class="grow"></span><span class="hintline">${icon('lock')}Read-only guard on</span></div>` },
  { id: 'labels', title: 'Pills, chips, badges', classes: '.pill .pill.name .pill.warn .chip .chip.on .count-badge .badge .badge.accent|ok|warn|danger|alt .badge.dashed .badge.solid.read|temp|write',
    use: 'Pill: a fact with a number (rows, time). Chip: a filter you can toggle. Badge: a source or kind in a few letters, readable without colour. Count badge: a count next to a title.',
    demo: `<div class="ui-row"><span class="pill name"><b>Q3 customer orders</b></span><span class="pill"><b>12,480</b>rows</span><span class="pill warn">Out of date</span><span class="chip on">${icon('check')}All classes</span><span class="chip">person</span><span class="count-badge">42</span></div>` +
      `<div class="ui-row"><span class="badge accent">MS</span><span class="badge ok">PG</span><span class="badge alt">MG</span><span class="badge warn dashed">TEMP</span><span class="badge solid">read</span><span class="badge solid temp">temp</span><span class="badge solid write">change</span></div>` },
  { id: 'feedback', title: 'Feedback', classes: '.status-line .status-line.ok|err .hintline .notice .notice.bad|info openDialog() toast() actionToast()',
    use: 'Status line: the result of a check, in place. Notice: something to act on, with its action at the end. Dialog: a decision or a form. Toast: a short "done" or "failed"; an action toast offers Undo.',
    demo: `<div class="status-line ok">${icon('check-c')}Connected · PostgreSQL 16</div><div class="status-line err">${icon('alert')}Could not reach the server</div>` +
      `<div class="notice"><span>The query changed. These results are from the last run.</span><button class="sm primary">Run to update</button></div><div class="notice bad"><span><b>Run failed.</b> relation "orders2" does not exist</span></div>` +
      `<div class="ui-row"><button class="sm" data-ui="dialog">Open a dialog</button><button class="sm" data-ui="toast">Show a toast</button><button class="sm" data-ui="undo">Show an Undo toast</button></div>` },
  { id: 'tables', title: 'Tables', classes: '.class-table .list-table .data-grid td.n td.null',
    use: 'class-table: a short clickable list with numbers (class stats). list-table: rows of text (descriptions, history). data-grid: long results with fixed, resizable columns and a sticky header.',
    demo: `<table class="class-table"><thead><tr><th>class</th><th class="r">boxes</th><th class="r">share</th></tr></thead><tbody><tr><td>person</td><td class="r">1,204</td><td class="r">48%</td></tr><tr><td>car</td><td class="r">812</td><td class="r">32%</td></tr></tbody></table>` +
      `<div class="ui-scroll"><table class="data-grid" style="width:520px"><colgroup><col style="width:110px"><col style="width:170px"><col style="width:120px"><col style="width:120px"></colgroup><thead><tr><th>id</th><th>name</th><th>total</th><th>note</th></tr></thead>` +
      `<tbody><tr><td class="n">100231</td><td>Siam Parts</td><td class="n">1,519.73</td><td class="null">NULL</td></tr><tr><td class="n">100238</td><td>Mekong Foods</td><td class="n">1,076.96</td><td>Rush delivery</td></tr><tr class="cur"><td class="n">100245</td><td class="cur">Lanna Print</td><td class="n">1,268.35</td><td></td></tr></tbody></table></div>` },
  { id: 'tiles', title: 'Totals, mini cards, empty state', classes: '.stat-strip .mini-card .mini-grid .sec-title .sec-label .empty',
    use: 'A strip of totals above a result; mini cards for a grid of small reports (a column profile, a dashboard item); an empty state that says what to do next.',
    demo: `<div class="stat-strip"><div><b>12,480</b>rows</div><div><b>8</b>columns</div><div><b>2.1%</b>empty cells</div><div><b>0</b>duplicate rows</div></div>` +
      `<div class="mini-grid"><div class="mini-card"><h4>total <span class="badge">numeric</span></h4><span class="muted">mean 765.75 · median 690.10</span></div><div class="mini-card"><h4>status <span class="badge">text</span></h4><span class="muted">4 values, no empty cells</span></div></div>` +
      `<div class="empty"><h3>No results yet</h3><p>Write a query or drag a table onto the builder, then press Run.</p></div>` },
];

function foundations(): string {
  const fs = ['fs-2xs', 'fs-xs', 'fs-sm', 'fs', 'fs-lg', 'fs-xl'].map((k) => `<div class="ui-fs" style="font-size:var(--${k})"><code>--${k}</code> The quick brown fox · ข้อมูลภาษาไทย</div>`).join('');
  const sp = [1, 2, 3, 4, 5, 6].map((k) => `<div class="ui-sp"><code>--s${k}</code><i style="width:var(--s${k})"></i></div>`).join('');
  const r = ['r-sm', 'r', 'r-md', 'r-lg'].map((k) => `<div class="ui-r" style="border-radius:var(--${k})"><code>--${k}</code></div>`).join('');
  const sh = ['shadow-1', 'shadow-2', 'shadow-3'].map((k) => `<div class="ui-r" style="box-shadow:var(--${k})"><code>--${k}</code></div>`).join('');
  const tok = ['bg', 'surface', 'surface-2', 'surface-3', 'border', 'border-strong', 'text', 'text-muted', 'text-faint', 'accent', 'accent-soft', 'accent-text',
    'ok', 'ok-text', 'highlight', 'danger', 'danger-text', 'err-fp'].map((k) => `<div class="ui-sw"><i style="background:var(--${k})"></i><code>--${k}</code><span class="ui-val" data-tok="${k}"></span></div>`).join('');
  return `<section class="ui-part" id="ui-foundations"><h2 class="sec-title">Foundations</h2><p class="muted">Tokens from tokens.css. A screen never writes a colour, size or radius by hand.</p>` +
    `<h3 class="sec-label">Type scale</h3>${fs}<h3 class="sec-label">Spacing</h3>${sp}<h3 class="sec-label">Radius and shadow</h3><div class="ui-row">${r}${sh}</div>` +
    `<h3 class="sec-label">Colour</h3><div class="ui-swatches">${tok}</div></section>`;
}

function fillValues(): void {
  const cs = getComputedStyle(document.documentElement);
  document.querySelectorAll<HTMLElement>('#ui-sheet .ui-val').forEach((el) => { el.textContent = cs.getPropertyValue(`--${el.dataset['tok'] ?? ''}`).trim(); });
}

function preview(attr: 'theme' | 'look' | 'palette', v: string): void {
  document.documentElement.dataset[attr] = v;           // this page only: the user's own settings stay as they are
  fillValues();
}

export function start(): void {
  const root = $req('ui-sheet');
  const d = document.documentElement.dataset;
  const opt = (v: string, label: string, cur: string | undefined): string => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(label)}</option>`;
  root.innerHTML = appHeader({ page: 'none', actionsId: 'ui-hactions' }).replace('<nav class="hdr-nav"', '<span class="pill name"><b>UI sheet</b></span><nav class="hdr-nav"') +
    `<div class="ui-body"><nav class="ui-toc" aria-label="Parts">${[...PARTS.map((p) => [p.id, p.title]), ['foundations', 'Foundations']].map(([id, t]) => `<a href="#ui-${id}">${esc(t ?? '')}</a>`).join('')}</nav>` +
    `<main class="ui-main"><div class="toolbar ui-switch"><b>Preview</b><select id="ui-theme" aria-label="Theme">${opt('dark', 'Dark', d['theme'])}${opt('light', 'Light', d['theme'])}</select>` +
    `<select id="ui-look" aria-label="Look">${LOOKS.map((l) => opt(l, LOOK_LABEL[l] ?? l, d['look'])).join('')}</select>` +
    `<select id="ui-palette" aria-label="Palette">${PALETTES.map((p) => opt(p.id, p.label, d['palette'])).join('')}</select>` +
    '<span class="grow"></span><span class="hintline">Preview only: your own settings do not change.</span></div>' +
    '<p class="muted ui-intro">Every part a screen is built from, drawn with the real stylesheets. A screen uses these; a part it needs that is not here is added to css/kit.css and to this page first.</p>' +
    PARTS.map((p) => `<section class="ui-part" id="ui-${p.id}"><h2 class="sec-title">${esc(p.title)}</h2><p class="muted">${esc(p.use)}</p><div class="ui-classes">${code(p.classes)}</div><div class="ui-demo">${p.demo}</div></section>`).join('') +
    foundations() + '</main></div>';
  mountUserMenu($req('ui-hactions'));
  fillValues();
  root.addEventListener('change', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLSelectElement)) return;
    if (t.id === 'ui-theme') preview('theme', t.value);
    if (t.id === 'ui-look') preview('look', t.value);
    if (t.id === 'ui-palette') preview('palette', t.value);
  });
  root.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-ui], [data-act]') : null;
    if (!b) return;
    if (b.dataset['act'] === 'settings' || b.dataset['act'] === 'theme') { e.preventDefault(); toast('In a real page this opens Settings or switches the theme.'); return; }
    const k = b.dataset['ui'];
    if (k === 'dialog') void openDialog({ title: 'Remove this connection?', ok: 'Remove', okClass: 'danger-btn', onOk: () => null, body: '<div class="ui-dlg-text"><p>Sessions that use it stop working until you pick another one.</p></div>' });
    if (k === 'toast') toast('Query finished in 1.24 s', 'ok');
    if (k === 'undo') actionToast({ title: 'Removed the session “Q3 orders”', kind: 'ok', actions: [{ label: 'Undo', icon: 'undo', run: () => toast('Restored') }] });
  });
}
