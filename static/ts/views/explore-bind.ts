// The visualizer's page wiring (buttons, gallery, plot, modal keys), bound once by main.ts init(). Split out of
// main.ts, which keeps the boot / load sequence.

import type { Sample } from '../types.js';
import type { DuplicatesPayload, SimilarPayload, TextSearchResult } from '../types-plot.js';
import { errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, syncRanges } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { waitJob } from '../core/jobs.js';
import { featureLabel, pts, S, UI } from '../core/state.js';
import { clsName, plotBase, THEME } from '../core/theme.js';
import { plotDiv } from '../plot/access.js';
import { isOutlier, selectGroup, selectGroupOf } from '../plot/groups.js';
import { bind3dControls } from '../plot/render3d.js';
import { clickOutsideLasso, enableLasso, enableLasso3d, enableMiddleDrag, enableSelectTools, enableRightPan, enableWheelZoom, enableFit, lassoEndedAt, redrawLasso, touchGestureRecent, updateOffscreen } from '../plot/interactions.js';
import { clearPicked, hoverDotId, OVERLAY, pickCard, showHoverDot, updateMarks } from '../plot/marks.js';
import { bubbleAt, hoverOf, requestFit, scheduleRender, updateGrid } from '../plot/render.js';
import { far2dIds, setExplainDot } from '../plot/explain.js';
import { applyRecipe, applyView, bindFeatureImport, clView, odView, exportCSV, loadView, openConfusion, reapplyInspect, markMethods, onGroupControls, refreshTagList, runLabelQuality, saveSettings, SETTING_IDS, toggleOff, topBy, updateMapDirty, updateMethodControls, updateTextSearch, updateModeControls, viewIdsVisible } from './controls.js';
import { checkLabelsAI } from './assistant.js';
import { PREP_IDS, writePrep } from './prep.js';
import { dialogOpen, openDialog, trapTab } from './dialog.js';
import { backToInspect, enableGalleryMarquee, marqueeEndedAt, narrowSelection, popSelection, renderGallery, setSelection } from './gallery.js';
import { showMissedObjects } from './model.js';
import { bindBar, bindGroupsPopover, bindSplitter, switchTab } from './layout.js';
import { closeModal, initModal, modalId, openImage, openModal, stepModal, toggleModalPreds } from './modal.js';
import { imageOrder, setImagePage } from './image-grid.js';
import { viewerKey } from './viewer.js';
import { allRows, bindMedia, isRows, tipMedia, unit } from './preview.js';
import { fixToSuggested } from './curation.js';
import { initPickBar } from './pick-bar.js';

// plotly.min.js is a classic script (main.ts loads it): a value here, see globals.d.ts
declare const Plotly: typeof import('plotly.js');

const closestOf = (e: Event, sel: string): HTMLElement | null => (e.target instanceof Element ? e.target.closest<HTMLElement>(sel) : null);
/** `data-id` of a gallery card. */
const cardId = (card: HTMLElement): number => +(card.dataset.id ?? NaN);

export function bindEvents(): void {
  const plot = plotDiv();
  const tip = $req('hover-tip');

  void Plotly.newPlot('plot', [], { ...plotBase(THEME.plot),
    xaxis: { visible: false }, yaxis: { visible: false }, margin: { l: 0, r: 0, t: 0, b: 0 } },
  { displaylogo: false, responsive: true });
  plot.on('plotly_afterplot', () => { redrawLasso(); updateOffscreen(); });   // resize / relayout: keep the lasso on its dots
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
      // blank space (outside the lasso, if there is one): the picked / hovered target dots go
      const blank = !closestOf(e, '.modebar, .legend, .colorbar, #select-tools') && (!S.lasso || clickOutsideLasso(e));
      if (blank) { clearPicked(); setExplainDot(null); if (hoverDotId !== null) showHoverDot(null); }
      if (S.lasso && clickOutsideLasso(e)) backToInspect();
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
    setExplainDot(id);                          // a "far" / "suspicious" check on: lines say why (plot/explain.ts)
    const media = tip.querySelector('.tip-media'), label = tip.querySelector('.tip-label');
    if (media) media.innerHTML = tipMedia(id);
    if (label) label.innerHTML = hoverOf(id);
    const e = (ev.event as MouseEvent | undefined) ?? lastPointer;      // a 3D hover event carries no mouse event
    tip.style.left = `${Math.min(e.clientX + 16, window.innerWidth - 180)}px`;
    tip.style.top = `${Math.min(e.clientY + 16, window.innerHeight - 220)}px`;
    tip.classList.remove('hidden');
  });
  plot.on('plotly_unhover', () => tip.classList.add('hidden'));
  let lastPointer = { clientX: 0, clientY: 0 };
  plot.addEventListener('mousemove', (e) => { lastPointer = { clientX: e.clientX, clientY: e.clientY }; }, { passive: true });
  plot.addEventListener('mouseleave', () => tip.classList.add('hidden'));

  enableRightPan(plot);
  enableLasso(plot);
  enableLasso3d(plot);
  bind3dControls();
  enableSelectTools();
  enableMiddleDrag(plot);
  enableWheelZoom(plot);
  enableFit();
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
  // feature / method / parameters only mark the map as changed: Generate (#btn-apply) computes it, so several
  // settings can be changed before one (possibly long) projection job
  $req<HTMLSelectElement>('feature').onchange = () => { updateTextSearch(); updateMapDirty(); };
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
  $req<HTMLSelectElement>('method').onchange = () => { updateMethodControls(); updateMapDirty(); };
  $req<HTMLSelectElement>('proj-dims').onchange = () => { updateMapDirty(); };
  // Preprocess: like the method, a change only marks the map; Default puts every control back
  for (const id of PREP_IDS) $req(id).addEventListener(id.startsWith('prep-min') || id.startsWith('prep-br') || id === 'prep-fuse-w' ? 'input' : 'change', updateMapDirty);
  $req('prep-reset').onclick = () => { writePrep(null); updateMapDirty(); };
  // a recipe fills the fields it names and generates the map (the menu goes back to its placeholder)
  const recipe = $req<HTMLSelectElement>('recipe');
  recipe.onchange = () => { const id = recipe.value; recipe.value = ''; if (id) void applyRecipe(id); };
  $req('btn-apply').onclick = () => { void loadView(); };
  $req<HTMLSelectElement>('mode').onchange = () => { updateModeControls(); requestFit(); scheduleRender(); reapplyInspect(); };
  $req<HTMLSelectElement>('class-a').onchange = () => { if ($req<HTMLSelectElement>('mode').value !== 'all') { requestFit(); scheduleRender(); reapplyInspect(); } };
  $req<HTMLSelectElement>('class-b').onchange = () => { if ($req<HTMLSelectElement>('mode').value === 'compare') { requestFit(); scheduleRender(); reapplyInspect(); } };
  $req<HTMLSelectElement>('color-by').onchange = () => { void onGroupControls(); };
  $req<HTMLInputElement>('dot-size').oninput = scheduleRender;
  $req<HTMLInputElement>('show-dist').onchange = () => updateMarks();
  $req<HTMLInputElement>('show-grid').onchange = updateGrid;
  for (const id of ['g-bubbles', 'g-outliers', 'g-dim', 'g-mode', 'g-min']) $req(id).onchange = () => { void onGroupControls(); };
  bindGroupsPopover();
  bindBar();
  bindFeatureImport();
  $req('params').addEventListener('input', updateMapDirty);
  // Find issues › cleanlab
  $req('btn-lq-run').onclick = async () => { if (await runLabelQuality()) clView('cl-issues'); };
  $req('btn-cl-issues').onclick = () => clView('cl-issues');
  $req('btn-cl-low').onclick = () => clView('cl-low');
  $req('btn-cl-ood').onclick = () => clView('cl-ood');
  $req('btn-cl-confusion').onclick = () => openConfusion();
  $req('btn-cl-fix').onclick = () => { void fixToSuggested(); };
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
  $req('btn-outliers-2d').onclick = () => {
    if (toggleOff('far2d')) return;
    $req<HTMLSelectElement>('sort').value = 'none';
    setSelection(far2dIds().slice(0, UI.check_list_size), `Far from class on the map · ${$req<HTMLSelectElement>('method').selectedOptions[0]?.textContent ?? ''}`, { view: 'far2d' });
  };
  $req<HTMLInputElement>('show-centres').onchange = scheduleRender;
  let dupsSeq = 0;
  $req('btn-dups').onclick = async () => {
    if (toggleOff('dups')) return;
    const btn = $req<HTMLButtonElement>('btn-dups'), seq = ++dupsSeq;
    btn.disabled = true;                              // one search at a time; a stale answer is dropped below
    try {
      const vis = new Set(viewIdsVisible());
      const { pairs, cutoff } = await getJSON<DuplicatesPayload>(`api/duplicates?threshold=${UI.duplicate_threshold}&feature=${encodeURIComponent(String(S.scoresFeature))}`);
      if (seq !== dupsSeq) return;
      const keep = pairs.filter((p) => vis.has(p.a) && vis.has(p.b));
      const rule = cutoff?.by === 'rank'
        ? `closest ${+((cutoff.quantile ?? 0) * 100).toFixed(2)}%, distance ≤ ${(cutoff.distance ?? 0).toPrecision(3)}`
        : `cos ≥ ${UI.duplicate_threshold}`;
      setSelection([...new Set(keep.flatMap((p) => [p.a, p.b]))], `Near-duplicates (${rule}) · ${featureLabel(S.scoresFeature)}`,
        { kind: 'pairs', pairs: keep, view: 'dups' });
    } catch (e) { toast(errText(e), 'err'); } finally { if (seq === dupsSeq) btn.disabled = false; }
  };

  $req<HTMLSelectElement>('sort').onchange = () => { S.page = 0; renderGallery(); };
  const thumb = $req<HTMLInputElement>('thumb-size');
  thumb.oninput = () => $req('gallery').style.setProperty('--thumb', `${thumb.value}px`);
  $req('btn-clear').onclick = () => { setSelection([], 'No selection'); };
  $req('btn-export').onclick = () => { void exportCSV(); };
  $req('btn-ai-check').onclick = () => { void checkLabelsAI(); };      // ◆ hidden unless label_check is on (core/llm.js)
  $req('btn-tag').onclick = () => {
    if (!S.sel.length || document.documentElement.dataset['role'] === 'viewer') return;
    const ids = [...S.sel];                          // the selection the user saw when they opened the dialog
    void openDialog({
      title: `Tag ${ids.length} ${unit(ids.length)}`, ok: 'Tag', focus: '#tag-name',
      body: '<label class="fld"><span>Tag</span><input type="text" class="txt" id="tag-name" value="relabel" autocomplete="off" spellcheck="false"></label>',
      bind: (r) => r.querySelector<HTMLInputElement>('#tag-name')?.select(),
      onOk: async (r) => {
        const tag = r.querySelector<HTMLInputElement>('#tag-name')?.value.trim() ?? '';
        if (!tag) return 'Give the tag a name';
        await postJSON('api/tags', { tag, ids });      // a failure throws: the dialog stays open and shows it
        void refreshTagList();
        toast(`Tagged ${ids.length} ${unit(ids.length)} “${tag}”`);
        return null;
      },
    });
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
  enableGalleryMarquee();
  initPickBar();
  $req('gallery').onclick = (e) => {
    const card = closestOf(e, '.card');
    if (!card || Date.now() - marqueeEndedAt < 300) return;
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
    const browsing = isRows() && !S.sel.length;          // "All rows": the chips narrow every row
    narrowSelection((browsing ? allRows() : S.sel).filter((id) => pts().cls[id] === c),
      `${browsing ? 'All rows' : S.selTitle} › ${clsName(c)}`);
  };

  initModal();
  // an embeddings row's dialog: its neighbour cards open that row (their play buttons play, see preview.bindMedia)
  bindMedia($req('m-row-nbrs'));
  $req('m-row-nbrs').addEventListener('click', (e) => {
    const card = closestOf(e, '.card[data-id]');
    if (card) void openModal(cardId(card));
  });
  $req('m-close').onclick = () => closeModal();
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
      else if (S.lasso) backToInspect();
    }
  });
  // keyboard focus on a card shows its dot on the plot, like mouse hover
  $req('gallery').addEventListener('focusin', (e) => {
    const card = closestOf(e, '.card[data-id]');
    if (card && cardId(card) !== hoverDotId) showHoverDot(cardId(card));
  });
  document.addEventListener('keydown', (e) => {
    if ($req('modal').classList.contains('hidden')) return;
    if (dialogOpen()) return;                       // a question on top of the modal has the keys
    if (trapTab($req('modal'), e)) return;          // keep focus inside the dialog (fields of the edit mode too)
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
