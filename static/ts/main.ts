// Dataset Visualizer frontend: the sign-in page (/login), the Sessions home (/), the labeling page (?label=<pid>), the
// Data Center (?data, js/datacenter/) or the visualizer of one session (/s/<id>/): 2D projection plot (right) +
// lasso-selection gallery (left).
// Entry point: boot (the signed-in user first, then the screen; the startup screen until the session's index is
// ready), init (first render) and every event binding of the page. Loaded by index.html as <script type="module">.

import type { Caps, DatasetInfo, Job, Meta, Points, Status } from './types.js';
import type { WorkspacePayload } from './types-plot.js';
import { errText, getJSON, postJSON, waitForServer } from './core/api.js';
import { loadMe } from './core/auth.js';
import { dataUrl, homeUrl, pointsLayerFromPath, sessionIdFromPath, vizUrl } from './core/nav.js';
import { $, $req, esc, fmtEta, icon, sleep } from './core/dom.js';
import { showError, installCrashHandlers } from './core/errorpage.js';
import { pollJobs, setBar } from './core/jobs.js';
import { meta as metaOf, S, status, UI } from './core/state.js';
import { applyThemePrefs, THEME } from './core/theme.js';
import { plotAxes } from './plot/interactions.js';
import { bindSettings, openSettings } from './settings/dialog.js';
import { initLLM } from './core/llm.js';
import { applySettings, applyUiDefaults, buildParamInputs, loadView, refreshSavedViews, refreshTagList, renderFeatureSelect, syncLqControls, updateMethodControls, updateTextSearch, updateModeControls } from './views/controls.js';
import { renderGallery } from './views/gallery.js';
import { bindControlsToggle, buildClassFilter, switchTab, updateBarSummary, updateGroupsBadge } from './views/layout.js';
import { loadViewerPrefs } from './views/viewer.js';
import { renderHome } from './views/home.js';
import { bindMedia, isRows, metaCols, rowsPills, unit } from './views/preview.js';
import { bindUserButton, mountUserMenu } from './views/usermenu.js';
// filters + curation (WP-D)
import { initCuration } from './views/curation.js';
import { initCompare } from './views/compare.js';
import { startLive } from './core/live.js';
import { initFilterBar } from './views/filter-bar.js';
import { bindKeyHelp } from './core/keys.js';
import { bindEvents } from './views/explore-bind.js';

declare const Plotly: typeof import('plotly.js');

const SCREEN = document.documentElement.dataset['screen'];
const SESSION = SCREEN === 'viz' ? sessionIdFromPath() : null;
const LABEL = SCREEN === 'label' ? new URLSearchParams(location.search).get('label') : null;
const DATA = SCREEN === 'data';

// Plotly (~1.5 MB) only on the visualizer: theme-boot.js already preloads it, this runs it. Found from this module's
// URL (static/js/main.js -> /plotly.min.js), the same absolute URL as the preload, never from the s/<id>/ page URL.
function loadPlotly(): Promise<void> {
  if (window.Plotly) return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const s = document.createElement('script');
    s.src = new URL('../../plotly.min.js', import.meta.url).href;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('could not load plotly.min.js'));
    document.head.appendChild(s);
  });
}

// still loading after 10 s / 30 s: say so, then offer the way out (the overlay itself only appears after 300 ms)
function slowHints(): { stop: () => void } {
  const hint = (txt: string, actions?: boolean): void => {
    const el = $('startup-hint');
    if (el) el.innerHTML = txt + (actions ? ` <a href="${esc(homeUrl())}">${icon('chev-l')}All sessions</a>` : '');
  };
  const t = [setTimeout(() => hint('Still working… large datasets can take a while.'), 10000),
    setTimeout(() => hint('This is taking longer than usual.', true), 30000)];
  return { stop: () => { t.forEach(clearTimeout); hint(''); } };
}

async function boot(): Promise<void> {
  const hints = slowHints();
  // server starting or restarting: keep the loading page up (it fades in after 300 ms) instead of an error page
  const up = await waitForServer(() => {
    $req('startup-title').textContent = 'Waiting for the server…';
    $req('startup-stage').textContent = 'it may be starting or restarting; this page continues on its own';
  }, 120000, SCREEN === 'login' ? 'api/auth/config' : 'api/status');
  if (!up) { hints.stop(); showError({ kind: 'network' }); return; }
  if (SCREEN === 'login') {                    // signed out: only the sign-in card (no user, no settings)
    hints.stop();
    $req('startup').classList.add('hidden');
    await (await import('./views/login.js')).renderLogin();
    return;
  }
  // who is signed in, and their prefs (theme, labeling settings…): a 401 here is already on its way to /login
  if (!await loadMe()) { hints.stop(); return; }
  applyThemePrefs();
  if (!SESSION) { hints.stop(); $req('startup').classList.add('hidden'); }   // home and labeling paint their own UI
  else { $req('startup-title').textContent = 'Loading visualizer…'; $req('startup-stage').textContent = 'loading plot library and dataset'; }
  bindSettings();
  initLLM();                                   // ◆ AI features: hidden until the server says they are usable
  // the user menu: the home's header here, the visualizer's activity bar; the label page mounts its own (label/app.js)
  const homeActions = document.querySelector<HTMLElement>('#home .header-actions');
  if (!SESSION && !LABEL && !DATA && homeActions) mountUserMenu(homeActions);
  const navS = $<HTMLAnchorElement>('home-nav-sessions'), navD = $<HTMLAnchorElement>('home-nav-data');
  if (navS) navS.href = homeUrl();
  if (navD) navD.href = dataUrl();
  if (SESSION) {
    bindUserButton($req<HTMLButtonElement>('act-user'));
    $req<HTMLAnchorElement>('brand-home').href = homeUrl();
  }
  // the labeling page loads its own code on demand: the home and the visualizer never download it
  if (LABEL) { import('./label/app.js').then((m) => m.start(LABEL)).catch((e: unknown) => {
    $req('label').innerHTML = `<div class="status-line err">${icon('alert')}Could not load the labeling page: ${esc(errText(e))}</div>`;
  }); return; }
  // the Data Center loads its own code on demand too (it mounts its own header, user menu included)
  if (DATA) { import('./datacenter/app.js').then((m) => m.start()).catch((e: unknown) => {
    $req('datacenter').innerHTML = `<div class="status-line err">${icon('alert')}Could not load the Data Center: ${esc(errText(e))}</div>`;
  }); return; }
  if (!SESSION) { void renderHome(); return; }      // the first page: no Plotly, no job polling
  const plotly = loadPlotly();
  plotly.catch(() => {});                      // reported below, once the data is there
  let st: Status;
  try {
    st = await getJSON<Status>('api/status');
    // open the requested session unless it already is (a reload, or the settings' "Open" of the same one)
    if (!st.dataset || st.dataset.id !== SESSION) {
      $req('startup-title').textContent = 'Opening session…';
      await postJSON(`api/datasets/${encodeURIComponent(SESSION)}/open`, {});
      st = await getJSON<Status>('api/status');
    }
  } catch (e) { showStartupError(errText(e)); return; }
  void pollJobs();
  if (!st.ready) {
    $req('startup-title').textContent = `Opening ${(st.dataset && st.dataset.name) || 'session'}…`;
    $req('startup-stage').textContent = 'starting';
    while (!st.ready) {
      const indexJob = st.dataset ? st.dataset.index_job : undefined;
      const isIdx = (x: Job): boolean => x.id === indexJob;
      const j = st.active.find(isIdx) || st.jobs.find(isIdx);
      if (j) {
        if (j.status === 'error') { showStartupError(j.error ?? 'error'); return; }
        $req('startup-stage').textContent = j.stage;
        setBar($req('startup-fill'), j);
        $req('startup-pct').textContent = j.total ? `${j.done.toLocaleString()} / ${j.total.toLocaleString()}` +
          `${j.extra ? ` · ${j.extra}` : ''} · ${fmtEta(j.eta)}` : (j.extra || `${j.elapsed}s`);
      }
      await sleep(500);
      try { st = await getJSON<Status>('api/status'); $req('startup-title').textContent = `Opening ${(st.dataset && st.dataset.name) || 'session'}…`; } catch (e) {   // server restarting: keep waiting
        $req('startup-title').textContent = 'Waiting for the server…'; await sleep(1000);
      }
    }
    $req('startup-title').textContent = 'Loading visualizer…';
    $req('startup-stage').textContent = 'loading dataset';
  }
  try { await plotly; } catch (e) { showStartupError(errText(e)); return; }
  hints.stop();
  await init(st);
}

// opening the session failed (folder gone, bucket gone, wrong credentials…): say why, offer the way back
function showStartupError(err: string): void {
  showError({ kind: /Failed to fetch|NetworkError|Load failed/.test(err) ? 'network' : 'session', message: err });
}

/** When the dataset changed after its cache was built. */
function staleBuilt(ds: Partial<DatasetInfo>): string {
  return ds.stale?.built ?? '';
}

async function init(st: Status): Promise<void> {
  const [meta, points] = await Promise.all([getJSON<Meta>('api/meta'), getJSON<Points>('api/points')]);
  $req('startup').classList.add('hidden');     // the loading panel is shown by index.html until the data is here
  S.meta = meta;
  S.P = points;
  S.status = st;
  // a viewer of a shared session: no box editor, no curation fixes, no tag writes (css/states.css .needs-labeler)
  document.documentElement.dataset['role'] = st.my_role || 'owner';
  // an embeddings session (rows + metadata): no boxes, images or models; css/states.css hides every .needs-boxes
  const caps = st.caps ?? { boxes: true, images: true, curation: true, models: true, meta: false };
  const imageUnit = caps.unit === 'image';         // one point per image: images, no labels to edit or score
  document.documentElement.classList.toggle('image-unit', imageUnit);
  document.documentElement.classList.toggle('no-boxes', !caps.boxes && !imageUnit);
  bindPointsLayer(caps);
  if (imageUnit) adaptToImages();
  else if (!caps.boxes) adaptToRows(points);
  loadViewerPrefs();
  const sessions = [...new Set(points.session)].sort();
  S.sessionIdx = new Map(sessions.map((s, i) => [s, i]));
  const pill = (v: string | number, k: string, cls = ''): string => `<span class="pill ${cls}"><b>${esc(v)}</b>${k}</span>`;
  const ds: Partial<DatasetInfo> = st.dataset || {};
  const src = ds.kind === 's3' ? (ds.mode === 'cache' ? 'S3 · cached' : 'S3 · stream') : ds.kind === 'labeling' ? 'labeling' : 'local';
  $req('ds-info').innerHTML = `<button id="ds-switch" class="pill name" title="${esc(meta.dataset_uri || '')}\nclick: datasets & sources">` +
    `${icon(ds.kind === 's3' ? 'cloud' : ds.kind === 'labeling' ? 'tag' : isRows() ? 'vectors' : 'folder')}<b>${esc(meta.dataset)}</b><span class="src">${isRows() ? 'embeddings' : src}</span></button>` +
    (isRows() ? rowsPills(Number(meta.n_samples), meta.classes.length)
      : imageUnit ? pill(Number(meta.n_images).toLocaleString(), 'images') + pill('1 point', 'per image', 'muted') +
        (meta.classes.length > 1 ? pill(meta.classes.length, 'folders') : '')
      : pill(Number(meta.n_images).toLocaleString(), 'images') + pill(Number(meta.n_samples).toLocaleString(), 'boxes') + pill(meta.classes.length, 'classes')) +
    (ds.stale ? `<button id="ds-refresh" class="pill stale" title="The dataset files changed after this cache was built (${esc(staleBuilt(ds))}). Click to re-index and recompute features for the new files.">${icon('refresh')}<b>Dataset changed</b>Update cache</button>` : '');
  if (ds.stale) {
    const refresh = $req<HTMLButtonElement>('ds-refresh');
    refresh.onclick = async () => {
      refresh.disabled = true;
      try { await postJSON(`api/datasets/${encodeURIComponent(String(ds.id))}/refresh`, {}); location.reload(); }
      catch (e) { refresh.disabled = false; refresh.title = errText(e); }
    };
  }

  const opts = meta.classes.map((c, i) => `<option value="${i}">${esc(c)} (${meta.counts[i]})</option>`).join('');
  $req('class-a').innerHTML = opts;
  $req('class-b').innerHTML = opts;
  if (meta.classes.length > 1) $req<HTMLSelectElement>('class-b').value = '1';
  // grouped by category (Linear / Non-linear / Other) in the server's order
  const byCat = new Map<string, string[]>();
  for (const [k, m] of Object.entries(st.methods)) {
    const cat = m.category || 'Other';
    byCat.set(cat, [...(byCat.get(cat) ?? []), `<option value="${k}" title="${esc(m.note || '')}">${m.label}</option>`]);
  }
  $req('method').innerHTML = [...byCat].map(([cat, opts]) => `<optgroup label="${esc(cat)}">${opts.join('')}</optgroup>`).join('');
  buildParamInputs();
  renderFeatureSelect(st.default_feature);
  // always start on the one startup view (config startup.feature + startup.method, all classes, default params)
  const start = st.startup || { feature: st.default_feature, method: 'umap' };
  $req<HTMLSelectElement>('feature').value = start.feature ?? '';
  updateTextSearch();
  if (st.methods[start.method]) $req<HTMLSelectElement>('method').value = start.method;

  buildClassFilter();
  bindEvents();
  // ---- filters + curation: register their remembered settings before applySettings, re-apply the curation
  // edits of this server session (deleted boxes) before the first render
  initFilterBar();
  if (caps.curation) await initCuration();      // an embeddings session has no label files to fix
  // ---- end filters + curation
  applyUiDefaults(st.ui || {});        // config defaults first, then the user's last-used settings on top
  let saved: Record<string, unknown> = {};
  try { saved = await getJSON<Record<string, unknown>>('api/settings'); applySettings(saved); } catch (e) { /* first run */ }
  updateGroupsBadge();
  updateModeControls();
  updateMethodControls();
  updateBarSummary();
  syncLqControls();
  bindControlsToggle();                // after the selects hold their values: the collapsed strip names them
  renderGallery();
  void refreshTagList();
  $req('ds-switch').onclick = () => { void openSettings('datasets'); };
  getJSON<WorkspacePayload>('api/workspace').then((w) => {
    $req('ds-info').insertAdjacentHTML('beforeend', `<span class="pill ws opt" tabindex="0">workspace <b>${esc(w.name)}</b></span>`);
    $req('ds-info').title = `${w.path}\ncreated ${w.created || '?'} · ${w.n_features} feature spaces · ${w.n_projections} saved views · ${w.size_mb} MB` +
      (w.others.length ? `\nother versions of this dataset: ${w.others.map((o) => o.name).join(', ')}` : '');
  }).catch(() => {});
  await refreshSavedViews();
  // a combined dataset: S.cmp, the Compare tab (opened right away by a #compare link from the Sessions home)
  if (meta.members && meta.members.length > 1) {
    initCompare(points, { colorSaved: 'color-by' in saved });
    if (location.hash === '#compare') switchTab('compare');
  }
  // nothing heavy runs until the user asks: a cached startup view loads at once, otherwise wait for "Compute embedding"
  if (st.startup && st.startup.cached) await loadView();
  else showIdle();
  startLive();                         // labels edited elsewhere (labeling page, promoted union) patch the map live
}

// the Objects | Full images switch: each is its own address (s/<id>/, s/<id>@images/) with its own State on the
// server, so switching is a page load (caps, image-unit adaptions are decided once per load); both stay cached
function bindPointsLayer(caps: Caps): void {
  const box = $req('points-layer');
  if (!caps.points?.switch || !SESSION) return;
  const layer = pointsLayerFromPath();
  box.classList.remove('hidden');
  for (const b of box.querySelectorAll<HTMLButtonElement>('button[data-layer]')) {
    const mine = (b.dataset['layer'] || null) === layer;
    b.setAttribute('aria-pressed', String(mine));
    b.onclick = () => { if (!mine) location.href = vizUrl(SESSION, (b.dataset['layer'] || null) as 'images' | null) + location.search; };
  }
}

// an embeddings session: the controls that read box geometry / images go, its metadata columns come in
// one point per image (data/views.py ImageView): every "box" is the whole image, so box size and shape say nothing
function adaptToImages(): void {
  const colorBy = $req<HTMLSelectElement>('color-by');
  for (const v of ['area', 'aspect']) colorBy.querySelector(`option[value="${v}"]`)?.remove();
  colorBy.querySelector('optgroup[data-needs="model"]')?.remove();
  const sort = $req<HTMLSelectElement>('sort');
  for (const v of ['area-asc', 'area-desc', 'm-status', 'm-score']) sort.querySelector(`option[value="${v}"]`)?.remove();
  $req('stat-counts-h').textContent = 'Images per class';
}

function adaptToRows(points: Points): void {
  // modules that read box geometry (filters, stats) see zeros instead of missing columns; their controls are hidden
  const zeros = (): number[] => new Array<number>(points.id.length).fill(0);
  points.area = points.area ?? zeros();
  points.aspect = points.aspect ?? zeros();
  points.brightness = points.brightness ?? zeros();
  points.blur = points.blur ?? zeros();
  const cols = metaCols();
  const colorBy = $req<HTMLSelectElement>('color-by');
  for (const v of ['session', 'area', 'aspect', 'brightness', 'blur']) colorBy.querySelector(`option[value="${v}"]`)?.remove();
  colorBy.querySelector('optgroup[data-needs="model"]')?.remove();
  const usable = cols.filter(([, t]) => t !== 'text');
  if (usable.length) {
    colorBy.insertAdjacentHTML('beforeend', '<optgroup label="Columns">' + usable.map(([c, t]) =>
      `<option value="meta:${esc(c)}">${esc(c)}${t === 'num' ? ' (scale)' : ''}</option>`).join('') + '</optgroup>');
  }
  const sort = $req<HTMLSelectElement>('sort');
  for (const v of ['area-asc', 'area-desc', 'blur', 'dark', 'bright', 'm-status', 'm-score']) sort.querySelector(`option[value="${v}"]`)?.remove();
  const img = sort.querySelector<HTMLOptionElement>('option[value="image"]');
  if (img) img.textContent = 'row id';
  const nums = cols.filter(([, t]) => t === 'num');
  if (nums.length) sort.insertAdjacentHTML('beforeend', nums.map(([c]) => `<option value="meta:${esc(c)}">${esc(c)} ↓</option>`).join(''));
  $req('stat-counts-h').textContent = 'Rows per class';
  const q = $req<HTMLInputElement>('fb-q');
  q.placeholder = 'Filter by row id…';
  q.setAttribute('aria-label', 'Filter by row id');
  bindMedia($req('gallery'));
}

function showIdle(): void {
  const key = $req<HTMLSelectElement>('feature').value;
  const f = (status().features ?? []).find((x) => x.key === key);
  $req('idle-note').textContent = `${f ? f.label : key} ${isRows() ? 'vectors' : 'features'} + ${$req<HTMLSelectElement>('method').selectedOptions[0]?.textContent} ` +
    `for ${Number(metaOf().n_samples).toLocaleString()} ${unit(2)}` + (f && f.cached ? ' (features cached, only the projection runs)' : '');
  $req('plot-idle').classList.remove('hidden');
  $req('btn-compute').onclick = () => { void loadView(); };
  $req('btn-stats-compute').onclick = () => {
    document.querySelector<HTMLElement>('[data-tab="explore"]')?.click();
    $req('btn-compute').click();
  };
}

/** The element an event came from that matches `sel` (itself or an ancestor), or null. */

// modules keep everything private: this one handle is for the browser console and the UI tests
window.dataviz = { S, UI, plotAxes, get THEME() { return THEME; } };

installCrashHandlers();
bindKeyHelp();                                     // "?" opens the shortcut sheet on every screen
// "Skip to the main content" (the first Tab stop on every screen): focus the visible main region
document.addEventListener('click', (e) => {
  if (!(e.target instanceof Element) || !e.target.closest('[data-skip]')) return;
  e.preventDefault();
  const main = [...document.querySelectorAll<HTMLElement>('main, [role="main"]')].find((m) => m.getClientRects().length > 0);
  if (!main) return;
  if (!main.hasAttribute('tabindex')) main.tabIndex = -1;
  main.focus();
});
boot().catch((e: unknown) => showError({ kind: 'crash', message: errText(e), detail: e instanceof Error && e.stack ? e.stack : String(e) }));
