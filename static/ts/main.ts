// Dataset Visualizer frontend: the sign-in page (/login), the Sessions home (/), the labeling page (?label=<pid>) or
// the visualizer of one session (/s/<id>/): 2D projection plot (right) + lasso-selection gallery (left).
// Entry point: boot (the signed-in user first, then the screen; the startup screen until the session's index is
// ready), init (first render) and every event binding of the page. Loaded by index.html as <script type="module">.

import type { DatasetInfo, Job, Meta, Points, Sample, Status } from './types.js';
import type { DuplicatesPayload, SimilarPayload, TextSearchResult, WorkspacePayload } from './types-plot.js';
import { errText, getJSON, postJSON, waitForServer } from './core/api.js';
import { loadMe } from './core/auth.js';
import { homeUrl, sessionIdFromPath } from './core/nav.js';
import { $, $req, esc, fmtEta, icon, sleep, syncRanges } from './core/dom.js';
import { showError, installCrashHandlers } from './core/errorpage.js';
import { toast } from './core/toast.js';
import { pollJobs, setBar, waitJob } from './core/jobs.js';
import { featureLabel, meta as metaOf, pts, S, status, UI } from './core/state.js';
import { applyThemePrefs, clsName, plotBase, THEME } from './core/theme.js';
import { plotDiv } from './plot/access.js';
import { isOutlier, selectGroup, selectGroupOf } from './plot/groups.js';
import { clickOutsideLasso, enableLasso, enableMiddleDrag, enableSelectTools, enableRightPan, enableWheelZoom, lassoEndedAt, plotAxes, redrawLasso, touchGestureRecent } from './plot/interactions.js';
import { clearPicked, hoverDotId, OVERLAY, pickCard, showHoverDot, updateMarks } from './plot/marks.js';
import { bubbleAt, hoverOf, scheduleRender } from './plot/render.js';
import { bindSettings, openSettings } from './settings/dialog.js';
import { initLLM } from './core/llm.js';
import { applySettings, applyUiDefaults, applyView, buildParamInputs, clView, odView, exportCSV, loadView, openConfusion, reapplyInspect, markMethods, onGroupControls, refreshSavedViews, refreshTagList, renderFeatureSelect, runLabelQuality, saveSettings, SETTING_IDS, syncLqControls, toggleOff, topBy, updateMapDirty, updateMethodControls, updateTextSearch, updateModeControls, viewIdsVisible } from './views/controls.js';
import { checkLabelsAI } from './views/assistant.js';
import { narrowSelection, popSelection, renderGallery, setSelection } from './views/gallery.js';
import { showMissedObjects } from './views/model.js';
import { bindBar, bindControlsToggle, bindGroupsPopover, bindSplitter, buildClassFilter, switchTab, updateBarSummary, updateGroupsBadge } from './views/layout.js';
import { closeModal, initModal, modalId, openImage, openModal, stepModal, toggleModalPreds } from './views/modal.js';
import { imageOrder, setImagePage } from './views/image-grid.js';
import { loadViewerPrefs, viewerKey } from './views/viewer.js';
import { renderHome } from './views/home.js';
import { bindUserButton, mountUserMenu } from './views/usermenu.js';
// filters + curation (WP-D)
import { initCuration } from './views/curation.js';
import { initFilterBar } from './views/filter-bar.js';

declare const Plotly: typeof import('plotly.js');

const SCREEN = document.documentElement.dataset['screen'];
const SESSION = SCREEN === 'viz' ? sessionIdFromPath() : null;
const LABEL = SCREEN === 'label' ? new URLSearchParams(location.search).get('label') : null;

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
  });
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
  if (!SESSION && !LABEL && homeActions) mountUserMenu(homeActions);
  if (SESSION) {
    bindUserButton($req<HTMLButtonElement>('act-user'));
    $req<HTMLAnchorElement>('brand-home').href = homeUrl();
  }
  // the labeling page loads its own code on demand: the home and the visualizer never download it
  if (LABEL) { import('./label/app.js').then((m) => m.start(LABEL)).catch((e: unknown) => {
    $req('label').innerHTML = `<div class="status-line err">${icon('alert')}Could not load the labeling page: ${esc(errText(e))}</div>`;
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
  loadViewerPrefs();
  const sessions = [...new Set(points.session)].sort();
  S.sessionIdx = new Map(sessions.map((s, i) => [s, i]));
  const pill = (v: string | number, k: string, cls = ''): string => `<span class="pill ${cls}"><b>${esc(v)}</b>${k}</span>`;
  const ds: Partial<DatasetInfo> = st.dataset || {};
  const src = ds.kind === 's3' ? (ds.mode === 'cache' ? 'S3 · cached' : 'S3 · stream') : ds.kind === 'labeling' ? 'labeling' : 'local';
  $req('ds-info').innerHTML = `<button id="ds-switch" class="pill name" title="${esc(meta.dataset_uri || '')}\nclick: datasets & sources">` +
    `${icon(ds.kind === 's3' ? 'cloud' : ds.kind === 'labeling' ? 'tag' : 'folder')}<b>${esc(meta.dataset)}</b><span class="src">${src}</span></button>` +
    pill(Number(meta.n_images).toLocaleString(), 'images') +
    pill(Number(meta.n_samples).toLocaleString(), 'boxes') + pill(meta.classes.length, 'classes') +
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
  $req('method').innerHTML = Object.entries(st.methods).map(([k, m]) => `<option value="${k}" title="${esc(m.note || '')}">${m.label}</option>`).join('');
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
  await initCuration();
  // ---- end filters + curation
  applyUiDefaults(st.ui || {});        // config defaults first, then the user's last-used settings on top
  try { applySettings(await getJSON('api/settings')); } catch (e) { /* first run */ }
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
  // nothing heavy runs until the user asks: a cached startup view loads at once, otherwise wait for "Compute embedding"
  if (st.startup && st.startup.cached) await loadView();
  else showIdle();
}

function showIdle(): void {
  const key = $req<HTMLSelectElement>('feature').value;
  const f = (status().features ?? []).find((x) => x.key === key);
  $req('idle-note').textContent = `${f ? f.label : key} features + ${$req<HTMLSelectElement>('method').selectedOptions[0]?.textContent} ` +
    `for ${Number(metaOf().n_samples).toLocaleString()} boxes` + (f && f.cached ? ' (features cached, only the projection runs)' : '');
  $req('plot-idle').classList.remove('hidden');
  $req('btn-compute').onclick = () => { void loadView(); };
  $req('btn-stats-compute').onclick = () => {
    document.querySelector<HTMLElement>('[data-tab="explore"]')?.click();
    $req('btn-compute').click();
  };
}

/** The element an event came from that matches `sel` (itself or an ancestor), or null. */
const closestOf = (e: Event, sel: string): HTMLElement | null => (e.target instanceof Element ? e.target.closest<HTMLElement>(sel) : null);
/** `data-id` of a gallery card. */
const cardId = (card: HTMLElement): number => +(card.dataset.id ?? NaN);

function bindEvents(): void {
  const plot = plotDiv();
  const tip = $req('hover-tip');

  void Plotly.newPlot('plot', [], { ...plotBase(THEME.plot),
    xaxis: { visible: false }, yaxis: { visible: false }, margin: { l: 0, r: 0, t: 0, b: 0 } },
  { displaylogo: false, responsive: true });
  plot.on('plotly_afterplot', redrawLasso);     // resize / relayout: keep the lasso on its dots
  plot.on('plotly_click', (ev) => {
    if (Date.now() - lassoEndedAt < 400 || touchGestureRecent()) return;  // the end of a lasso / pinch is not a click
    const clicked = ev.points || [];
    const dotPt = clicked.find((q) => !OVERLAY.has(q.data.name) && typeof q.customdata === 'number');
    if (!dotPt || typeof dotPt.customdata !== 'number') return;
    lastDotClick = Date.now();
    const e = ev.event;
    if (e && (e.shiftKey || e.altKey) && selectGroupOf(dotPt.customdata)) return;   // shift+click: whole group
    void openModal(dotPt.customdata);
  });
  // a plain click (no drag) that did not hit a dot: outside the drawn lasso / box / circle it cancels it, then
  // it selects the bubble under the mouse
  let lastDotClick = 0, downAt: { x: number; y: number } | null = null;
  plot.addEventListener('mousedown', (e) => { if (e.button === 0) downAt = { x: e.clientX, y: e.clientY }; }, true);
  plot.addEventListener('click', (e) => {
    if (!downAt || Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > 4) return;
    setTimeout(() => {
      if (Date.now() - lastDotClick < 400 || Date.now() - lassoEndedAt < 400 || touchGestureRecent()) return;
      if (clickOutsideLasso(e)) setSelection([], 'No selection');
      if (!S.groups || !$req<HTMLInputElement>('g-bubbles').checked) return;
      const gid = bubbleAt(e.clientX, e.clientY);
      if (gid !== null) selectGroup(gid);
    }, 0);
  });
  plot.on('plotly_hover', (ev) => {
    if (plot.classList.contains('lassoing') || plot.classList.contains('panning') || plot.classList.contains('moving-lasso')) return;
    const p = ev.points && ev.points[0];
    if (!p || OVERLAY.has(p.data.name) || typeof p.customdata !== 'number') return;
    const id = p.customdata;
    const img = tip.querySelector('img'), label = tip.querySelector('div');
    if (img) img.src = `api/crop/${id}`;
    if (label) label.innerHTML = hoverOf(id);
    const e = ev.event;
    tip.style.left = `${Math.min(e.clientX + 16, window.innerWidth - 180)}px`;
    tip.style.top = `${Math.min(e.clientY + 16, window.innerHeight - 220)}px`;
    tip.classList.remove('hidden');
  });
  plot.on('plotly_unhover', () => tip.classList.add('hidden'));
  plot.addEventListener('mouseleave', () => tip.classList.add('hidden'));

  enableRightPan(plot);
  enableLasso(plot);
  enableSelectTools();
  enableMiddleDrag(plot);
  enableWheelZoom(plot);
  document.addEventListener('change', (e) => { if (e.target instanceof HTMLElement && SETTING_IDS.includes(e.target.id)) { saveSettings(); markMethods(); } });
  document.addEventListener('input', (e) => {
    const t = e.target;
    if (t instanceof HTMLInputElement && t.type === 'range') syncRanges();
    if (t instanceof HTMLElement && SETTING_IDS.includes(t.id)) saveSettings();
  });
  const savedViews = $req<HTMLSelectElement>('saved-views');
  savedViews.onchange = () => {
    const v = S.views && S.views[+savedViews.value];
    savedViews.value = '';
    if (v) applyView(v);
  };
  $req<HTMLSelectElement>('feature').onchange = () => { updateTextSearch(); void loadView(); };
  $req<HTMLFormElement>('text-search').onsubmit = async (e) => {
    e.preventDefault();
    const q = $req<HTMLInputElement>('text-q').value.trim(), feature = $req<HTMLSelectElement>('feature').value;
    if (!q) return;
    $req<HTMLButtonElement>('text-go').disabled = true;
    try {
      const body = { query: q, feature, k: Number(UI.text_search_k) || 200 };
      let r = await postJSON<TextSearchResult>('api/text-search', body);
      if (!r.ready) {
        await waitJob(r.job_id);
        r = await postJSON<TextSearchResult>('api/text-search', body);
      }
      if (!r.ready) throw new Error('text search is not ready');
      $req<HTMLSelectElement>('sort').value = 'none';
      setSelection(r.id, `Text: "${q}" · ${featureLabel(feature)}`);
    } catch (err) { toast(errText(err), 'err'); } finally { updateTextSearch(); }
  };
  $req<HTMLSelectElement>('method').onchange = () => { updateMethodControls(); void loadView(); };
  $req('btn-apply').onclick = () => { void loadView(); };
  $req<HTMLSelectElement>('mode').onchange = () => { updateModeControls(); scheduleRender(); reapplyInspect(); };
  $req<HTMLSelectElement>('class-a').onchange = () => { if ($req<HTMLSelectElement>('mode').value !== 'all') { scheduleRender(); reapplyInspect(); } };
  $req<HTMLSelectElement>('class-b').onchange = () => { if ($req<HTMLSelectElement>('mode').value === 'compare') { scheduleRender(); reapplyInspect(); } };
  $req<HTMLSelectElement>('color-by').onchange = () => { void onGroupControls(); };
  $req<HTMLInputElement>('dot-size').oninput = scheduleRender;
  $req<HTMLInputElement>('show-dist').onchange = () => updateMarks();
  for (const id of ['g-bubbles', 'g-outliers', 'g-dim', 'g-mode', 'g-min']) $req(id).onchange = () => { void onGroupControls(); };
  bindGroupsPopover();
  bindBar();
  $req('params').addEventListener('input', updateMapDirty);
  // Find issues › cleanlab
  $req('btn-lq-run').onclick = async () => { if (await runLabelQuality()) clView('cl-issues'); };
  $req('btn-cl-issues').onclick = () => clView('cl-issues');
  $req('btn-cl-low').onclick = () => clView('cl-low');
  $req('btn-cl-ood').onclick = () => clView('cl-ood');
  $req('btn-cl-confusion').onclick = () => openConfusion();
  $req('btn-cl-swap').onclick = () => odView('cl-swap');
  $req('btn-cl-badloc').onclick = () => odView('cl-badloc');
  $req('btn-cl-missed').onclick = () => { void showMissedObjects(); };
  $req('btn-sel-outliers').onclick = async () => {
    if (toggleOff('sel-outliers')) return;
    if (!S.groups) { $req<HTMLInputElement>('g-outliers').checked = true; await onGroupControls(); }
    const ids = viewIdsVisible().filter(isOutlier);
    $req<HTMLSelectElement>('sort').value = 'outlier';
    setSelection(ids, `Outliers (no group, min size ${$req<HTMLInputElement>('g-min').value})`, { view: 'sel-outliers' });
  };

  $req('btn-suspicious').onclick = () => topBy('suspicion', UI.check_list_size, 'Suspicious labels', 'suspicious');
  $req('btn-outliers').onclick = () => topBy('outlier', UI.check_list_size, 'Far from class centre', 'far');
  $req('btn-dups').onclick = async () => {
    if (toggleOff('dups')) return;
    try {
      const vis = new Set(viewIdsVisible());
      const { pairs } = await getJSON<DuplicatesPayload>(`api/duplicates?threshold=${UI.duplicate_threshold}&feature=${encodeURIComponent(String(S.scoresFeature))}`);
      const keep = pairs.filter((p) => vis.has(p.a) && vis.has(p.b));
      setSelection([...new Set(keep.flatMap((p) => [p.a, p.b]))], `Near-duplicates (cos ≥ ${UI.duplicate_threshold}) · ${featureLabel(S.scoresFeature)}`,
        { kind: 'pairs', pairs: keep, view: 'dups' });
    } catch (e) { toast(errText(e), 'err'); }
  };

  $req<HTMLSelectElement>('sort').onchange = () => { S.page = 0; renderGallery(); };
  const thumb = $req<HTMLInputElement>('thumb-size');
  thumb.oninput = () => $req('gallery').style.setProperty('--thumb', `${thumb.value}px`);
  $req('btn-clear').onclick = () => { setSelection([], 'No selection'); };
  $req('btn-export').onclick = () => { void exportCSV(); };
  $req('btn-ai-check').onclick = () => { void checkLabelsAI(); };      // ◆ hidden unless label_check is on (core/llm.js)
  $req('btn-tag').onclick = async () => {
    if (!S.sel.length || document.documentElement.dataset['role'] === 'viewer') return;
    const tag = prompt(`Tag ${S.sel.length} boxes as:`, 'relabel');
    if (!tag) return;
    await postJSON('api/tags', { tag, ids: S.sel });
    void refreshTagList();
  };
  const tagList = $req<HTMLSelectElement>('tag-list');
  tagList.onchange = () => {
    const t = tagList.value, ids = S.tags[t];
    if (t && ids) setSelection(ids.slice(), `Tag: ${t}`);
    tagList.value = '';
  };

  $req('gallery').addEventListener('mouseover', (e) => {
    const card = closestOf(e, '.card[data-id]');
    if (card && cardId(card) !== hoverDotId) showHoverDot(cardId(card));   // gaps between cards keep the dot
  });
  $req('gallery').addEventListener('mouseleave', () => { if (hoverDotId !== null) showHoverDot(null); });
  // click = details, ctrl/cmd+click = pick it (add / remove; its dot is pinned on the map)
  $req('gallery').onclick = (e) => {
    const card = closestOf(e, '.card');
    if (!card) return;
    if (card.dataset.img !== undefined) { void openImage(card.dataset.img, imageOrder()); return; }   // image browser tile
    if (e.ctrlKey || e.metaKey) pickCard(cardId(card), true);
    else void openModal(cardId(card));
  };
  $req('pager').onclick = (e) => {
    const b = closestOf(e, 'button[data-p], button[data-ip]');
    if (b && b.dataset.ip !== undefined) setImagePage(+b.dataset.ip);
    else if (b) { S.page = +(b.dataset.p ?? NaN); renderGallery(); }
  };
  $req('breakdown').onclick = (e) => {
    const chip = closestOf(e, '.chip');
    if (chip && chip.dataset.undo) { popSelection(); return; }
    if (!chip || S.selKind === 'pairs') return;
    if (chip.dataset.f) {
      const keepOut = chip.dataset.f === 'out';
      narrowSelection(S.sel.filter((id) => isOutlier(id) === keepOut), `${S.selTitle} › ${keepOut ? 'outliers' : 'in groups'}`);
      return;
    }
    const c = +(chip.dataset.c ?? NaN);
    narrowSelection(S.sel.filter((id) => pts().cls[id] === c), `${S.selTitle} › ${clsName(c)}`);
  };

  initModal();
  $req('m-close').onclick = closeModal;
  $req('m-preds').onclick = toggleModalPreds;
  $req('m-group').onclick = () => { const id = modalId; closeModal(); if (id !== null) selectGroupOf(id); };
  $req('modal').onclick = (e) => { if (e.target === $('modal')) closeModal(); };
  $req('m-prev').onclick = () => stepModal(-1);
  $req('m-next').onclick = () => stepModal(1);
  $req('m-similar').onclick = async () => {
    const id = modalId;
    if (id === null) return;
    try {
      const r = await getJSON<SimilarPayload>(`api/similar/${id}?k=${UI.similar_k}&feature=${encodeURIComponent(String(S.scoresFeature))}`);
      closeModal();
      $req<HTMLSelectElement>('sort').value = 'none';
      setSelection([id, ...r.id], `Similar to #${id} (${clsName(pts().cls[id] ?? -1)}) · ${featureLabel(S.scoresFeature)}`);
    } catch (e) { toast(errText(e), 'err'); }
  };
  $req('m-sameimg').onclick = async () => {
    const id = modalId;
    if (id === null) return;
    const s = await getJSON<Sample>(`api/sample/${id}`);
    closeModal();
    $req<HTMLSelectElement>('sort').value = 'class';
    setSelection([id, ...s.same_image], `Boxes in ${s.image}`);
  };
  // chips and cards are role="button" spans/divs: Enter / Space click them like real buttons;
  // on a card Space picks it (ctrl+Space adds) and Enter opens the details
  document.addEventListener('keydown', (e) => {
    const t = e.target instanceof HTMLElement ? e.target : null;
    if (t && (e.key === 'Enter' || e.key === ' ') && t.getAttribute('role') === 'button' && t.tagName !== 'BUTTON') {
      e.preventDefault();
      if (t.classList.contains('card') && t.dataset.img !== undefined) { void openImage(t.dataset.img, imageOrder()); return; }
      if (t.classList.contains('card')) {
        if (e.key === 'Enter') void openModal(cardId(t));
        else pickCard(cardId(t), e.ctrlKey || e.metaKey);
        return;
      }
      t.click();
    }
    if (e.key === 'Escape' && $req('modal').classList.contains('hidden') && $req('settings').classList.contains('hidden') &&
        !t?.closest('.popover')) {
      if (S.picked.length) clearPicked();
      else if (S.lasso) setSelection([], 'No selection');
    }
  });
  // keyboard focus on a card shows its dot on the plot, like mouse hover
  $req('gallery').addEventListener('focusin', (e) => {
    const card = closestOf(e, '.card[data-id]');
    if (card && cardId(card) !== hoverDotId) showHoverDot(cardId(card));
  });
  document.addEventListener('keydown', (e) => {
    if ($req('modal').classList.contains('hidden')) return;
    if (e.key === 'Tab') {                          // keep focus inside the dialog
      const f = [...$req('modal').querySelectorAll<HTMLElement>('button:not(.hidden):not(:disabled)')];
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || !$req('modal').contains(document.activeElement))) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      return;
    }
    if (!$req('vw-menu').classList.contains('hidden')) return;      // the right-click menu has the keys
    if (viewerKey(e)) return;                                      // toggles, Edit mode (box keys, Ctrl+S / Z)
    if (e.key === 'Escape') closeModal();
    if (e.key === 'ArrowLeft') stepModal(-1);
    if (e.key === 'ArrowRight') stepModal(1);
  });

  document.querySelectorAll<HTMLElement>('#activity .act[data-tab]').forEach((b) => { b.onclick = () => { if (b.dataset.tab !== undefined) switchTab(b.dataset.tab); }; });
  bindSplitter();
  $req('class-table').addEventListener('click', (e) => {
    const row = closestOf(e, 'tr[data-c]');
    if (!row) return;
    const c = +(row.dataset.c ?? NaN);
    const P = pts();
    $req<HTMLSelectElement>('sort').value = P.suspicion ? 'suspicion' : 'none';
    setSelection(P.id.filter((id) => P.cls[id] === c), `Class: ${clsName(c)}`);
    switchTab('explore');
  });
}

// modules keep everything private: this one handle is for the browser console and the UI tests
window.dataviz = { S, UI, plotAxes, get THEME() { return THEME; } };

installCrashHandlers();
boot().catch((e: unknown) => showError({ kind: 'crash', message: errText(e), detail: e instanceof Error && e.stack ? e.stack : String(e) }));
