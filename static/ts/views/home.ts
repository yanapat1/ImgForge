// Sessions home: the first page. Two tabs (an ARIA tablist, the last one remembered in localStorage 'home-tab'),
// each its own grid, newest first, with its own "New … session" tile, empty state and load error line:
// - Visualize: a saved dataset (local folder, S3 prefix or a labeling task read live) opens the visualizer on it
//   (./?session=<id>); new / edit reuse the Settings dataset forms in solo mode; the open one is pinned first;
// - Label: a labeling session (GET api/label/projects) opens the labeling page (./?label=<pid>); new / edit open
//   the wizard (views/label-new.js, loaded on demand), export opens label/export.js; its ⋯ menu (#home-cardmenu)
//   has Visualize a task…, Duplicate…, Manage classes… (label/session-actions.js, loaded on demand) and Re-export.
// Find: a name search (#home-search) and a tag filter (#home-tagbar, AND) over the current tab; each session has up to
// 20 tags of its own (under its name, edited in the #home-tagedit popover: PUT api/datasets/{id}/tags or
// PATCH api/label/projects/{pid}).
// One innerHTML render per change, one delegated click listener on #home-body; no Plotly and no job polling here.

import { api, errText, getJSON, postJSON } from '../core/api.js';
import { toast } from '../core/toast.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { setTheme, THEME } from '../core/theme.js';
import { TASK_TYPES } from '../label/state.js';
import type { TaskTypeInfo } from '../label/state.js';
import { VIZ_TASKS } from '../settings/forms.js';
import { openSettings } from '../settings/dialog.js';
import type { Status } from '../types.js';
import type { HomeExportTarget, HomeProject, HomeSession, HomeTask, WizardOpts } from '../types-home.js';

// errs: what failed to load ({ds, lp}); the other list still shows
// tab: 'viz' | 'label', picked on the first render (the remembered one, else the kind that has sessions)
type TabId = 'viz' | 'label';
/** The tag editor popover's state. */
interface TagEdit { kind: TabId; id: string; from: HTMLElement; tags: string[]; err: string; busy: boolean }
interface HomeState {
  sessions: HomeSession[]; projects: HomeProject[];
  errs: { ds: string | null; lp: string | null };
  palette: string[] | null; paletteLight: string[] | null;
  opening: boolean; bound: boolean; tab: TabId | null;
  menuFrom: HTMLElement | null; flash: string | null; q: string; tagSel: Set<string>; raf: number; te: TagEdit | null;
}
const H: HomeState = { sessions: [], projects: [], errs: { ds: null, lp: null }, palette: null, paletteLight: null, opening: false, bound: false, tab: null,
  menuFrom: null, flash: null, q: '', tagSel: new Set(), raf: 0, te: null };
// q: the search text (lower-cased); tagSel: the selected filter tags (lower-cased); te: the open tag editor
const TABS: readonly TabId[] = ['viz', 'label'];
const isTab = (v: unknown): v is TabId => v === 'viz' || v === 'label';
type Item = { d: HomeSession; p?: undefined; pid?: undefined; t: number } | { p: HomeProject; pid: string; d?: undefined; t: number };
const strList = (v: unknown): string[] | null => (Array.isArray(v) && v.every((x) => typeof x === 'string') ? v.map(String) : null);
const dataOf = (el: Element, attr: 'id' | 'pid'): string => el.closest<HTMLElement>(`[data-${attr}]`)?.dataset[attr] ?? '';
const TAB_KEY = 'home-tab';
const NEW_TEXT: Record<TabId, string> = { viz: 'New visualize session', label: 'New label session' };
const MAX_CHIPS = 5;
const MAX_TAGS = 20, TAG_LEN = 32;               // the server's rules (dataviz/core/names.py clean_tags)

// ---- find: name contains the query, and the session has every selected tag
const fold = (s: unknown): string => String(s).toLowerCase();
const tagsOf = (x: { tags?: string[] | undefined } | null | undefined): string[] => (x && Array.isArray(x.tags) ? x.tags : []);
const filtering = (): boolean => !!(H.q || H.tagSel.size);
function matches(name: string, tags: string[]): boolean {
  if (H.q && !fold(name).includes(H.q)) return false;
  if (!H.tagSel.size) return true;
  const have = new Set(tags.map(fold));
  for (const t of H.tagSel) if (!have.has(t)) return false;
  return true;
}

// a session's tags, right under its name; a click toggles the tag in the filter
function tagsHTML(tags: string[]): string {
  if (!tags.length) return '';
  return '<div class="sc-tags">' + tags.map((t) => {
    const on = H.tagSel.has(fold(t));
    return `<button class="sc-tag${on ? ' on' : ''}" data-act="tagfilter" data-tag="${esc(t)}" aria-pressed="${on}" ` +
      `title="${on ? 'Stop filtering by' : 'Show only sessions tagged'} ${esc(t)}">${esc(t)}</button>`;
  }).join('') + '</div>';
}

const fmt = (n: unknown): string => Number(n).toLocaleString();

// workspace.json times are "YYYY-MM-DD HH:MM:SS" (server local time); `created` is epoch seconds
function timeAgo(t: string | number): string {
  const ms = typeof t === 'number' ? t * 1000 : Date.parse(String(t).replace(' ', 'T'));
  if (!Number.isFinite(ms)) return '';
  const s = (Date.now() - ms) / 1000;
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago`
    : s < 86400 * 60 ? `${Math.round(s / 86400)} days ago` : new Date(ms).toLocaleDateString();
}

function classColor(i: number): string {
  const p = (THEME.name === 'light' ? H.paletteLight : H.palette) || H.palette || [];
  return p.length ? p[i % p.length] ?? 'var(--text-faint)' : 'var(--text-faint)';
}

function sourceLabel(d: HomeSession): string {
  if (d.kind === 'labeling') return 'Labeling';
  if (d.kind !== 's3') return 'Local';
  return d.mode === 'cache' ? 'S3 · cached' : 'S3 · stream';
}

const KIND_ICON: Record<string, string> = { s3: 'cloud', labeling: 'tag', local: 'folder' };
const kindIcon = (d: HomeSession): string => KIND_ICON[d.kind] || 'folder';

function coverHTML(d: HomeSession): string {
  const st = d.stats || {};
  const tag = `<span class="sc-src${d.kind === 'labeling' ? ' lbl' : ''}">${icon(kindIcon(d))}${esc(sourceLabel(d))}</span>` +
    (d.active ? `<span class="sc-live">${icon('check')}Open now</span>` : '');
  if (!d.cover) {
    return `<div class="sc-cover empty">${tag}<div class="sc-glyph">${icon(d.kind === 's3' ? 'cloud' : d.kind === 'labeling' ? 'tag' : 'db')}` +
      '<span>Not indexed yet: the first open builds its crops</span></div></div>';
  }
  const v = encodeURIComponent(st.workspace || '');      // a new dataset version = a new workspace = fresh crops
  let imgs = '';
  for (let k = 0; k < 4; k++) {
    imgs += k < d.cover
      ? `<img src="api/cover/${encodeURIComponent(d.id)}/${k}?v=${v}" alt="" width="112" height="112" loading="lazy" decoding="async">`
      : '<span class="sc-tile"></span>';
  }
  return `<div class="sc-cover">${tag}<div class="sc-mosaic">${imgs}</div></div>`;
}

// the card's Edit button opens the tag editor popover; its "Full edit…" button reaches the settings form / wizard
const editBtn = (act: string, x: { name: string; tags?: string[] | undefined }, title: string): string => `<button class="icon-btn ghost" data-act="${act}" aria-haspopup="dialog" aria-expanded="false" ` +
  `aria-label="Edit ${esc(x.name)}" title="${title} (tags ${tagsOf(x).length} / ${MAX_TAGS})">${icon('edit')}</button>`;

function cardHTML(d: HomeSession): string {
  const st = d.stats || {};
  const stat = (v: number | null | undefined, label: string): string => (v == null ? '' : `<span><b class="num">${fmt(v)}</b>${label}</span>`);
  const stats = stat(st.n_images, 'images') + stat(st.n_boxes, 'boxes') + stat(st.n_classes, 'classes');
  const classes = st.classes || [];
  const more = (st.n_classes || classes.length) - Math.min(MAX_CHIPS, classes.length);
  const chips = classes.slice(0, MAX_CHIPS).map((c, i) =>
    `<span class="sc-chip"><i style="background:${esc(classColor(i))}"></i>${esc(c)}</span>`).join('') +
    (more > 0 ? `<span class="sc-chip more">+${more}</span>` : '');
  const problem = d.error || (d.exists === false ? 'folder not found' : '');
  const when = st.last_opened ? `opened ${timeAgo(st.last_opened)}` : d.created ? `created ${timeAgo(d.created)}` : '';
  // a labeling-backed session names its labeling session + task (the label:// uri means nothing to people)
  const lb = d.kind === 'labeling' ? d.labeling || {} : null;
  const where = lb ? (lb.project ? `${lb.project} › ${lb.task}${d.only_done ? ' · finished images only' : ''}` : 'labeling session') : d.uri || '';
  return `<article class="session-card${d.active ? ' active' : ''}${problem ? ' broken' : ''}" data-id="${esc(d.id)}">` +
    coverHTML(d) +
    '<div class="sc-body">' +
    `<h3 class="sc-title"><button class="sc-open" data-act="open" title="Open ${esc(d.name)}">${esc(d.name)}</button></h3>` +
    tagsHTML(tagsOf(d)) +
    `<div class="sc-uri${lb ? ' plain' : ''}" title="${esc(where)}"><bdi>${esc(where)}</bdi></div>` +
    (stats ? `<div class="sc-stats">${stats}</div>` : '<div class="sc-stats muted">Stats appear after the first open</div>') +
    (chips ? `<div class="sc-chips">${chips}</div>` : '') +
    '</div>' +
    '<footer class="sc-foot">' +
    (problem ? `<span class="sc-problem">${icon('alert')}${esc(problem)}</span>` : `<span class="sc-when">${esc(when)}</span>`) +
    '<span class="grow"></span>' +
    (lb && lb.project ? `<button class="icon-btn ghost" data-act="tolabel" aria-label="Open the labeling session of ${esc(d.name)}" title="Open the labeling session ${esc(lb.project)}">${icon('tag')}</button>` : '') +
    editBtn('tags', d, 'Edit tags or open the full edit') +
    `<button class="icon-btn ghost danger" data-act="remove" aria-label="Remove ${esc(d.name)}" title="Remove the session (no files are deleted)">${icon('trash')}</button>` +
    '</footer><div class="sc-busy" aria-hidden="true"><span class="spin"></span>Opening…</div></article>';
}

// ---- labeling session cards
const TASK_MAX = 3;
type StatusKey = 'done' | 'in_progress' | 'skipped' | 'todo';
const STATUS_TEXT: ReadonlyArray<[StatusKey, string]> = [['done', 'done'], ['in_progress', 'in progress'], ['skipped', 'skipped'], ['todo', 'to do']];
const isTaskType = (t: string): t is keyof typeof TASK_TYPES => Object.prototype.hasOwnProperty.call(TASK_TYPES, t);
const taskInfo = (t: string): TaskTypeInfo | undefined => (isTaskType(t) ? TASK_TYPES[t] : undefined);

interface LpSource { ico: string; label: string; uri: string; plain?: string }
function lpSource(p: HomeProject): LpSource {
  const s = p.source || {};
  if (s.kind === 'local') return { ico: 'folder', label: 'Local folder', uri: s.path || '' };
  if (s.kind === 's3') return { ico: 'cloud', label: 'S3', uri: `s3://${s.bucket || ''}/${s.prefix || ''}` };
  if (s.kind === 'ftp' || s.kind === 'sftp') return { ico: 'plug', label: s.kind.toUpperCase(), uri: s.path || '/' };
  return { ico: 'upload', label: 'Uploaded', uri: '', plain: 'Images uploaded from this browser' };
}

// the optional review layer: how many of a task's done images were approved by a person ('manual') or by Review
// all ('bulk'); the two never share a colour, so a bulk approval can be told apart and looked at again
function reviewState(t: HomeTask) {
  const done = t.done || 0, man = t.review_manual || 0, bulk = t.review_bulk || 0;
  return { done, man, bulk, left: Math.max(0, done - man - bulk), all: done > 0 && man + bulk >= done };
}

function taskHTML(t: HomeTask, total: number, review: boolean | undefined): string {
  const base = { done: t.done || 0, in_progress: t.in_progress || 0, skipped: t.skipped || 0 };
  const n: Record<StatusKey, number> = { ...base, todo: t.todo != null ? t.todo : Math.max(0, total - base.done - base.in_progress - base.skipped) };
  const sum = n.done + n.in_progress + n.skipped + n.todo || 1;
  const tip = `${t.name} · ${taskInfo(t.task_type)?.label || t.task_type}: ` +
    STATUS_TEXT.map(([k, label]) => `${fmt(n[k])} ${label}`).join(' · ');
  const seg = (k: StatusKey): string => (n[k] ? `<i class="${k}" style="width:${((100 * n[k]) / sum).toFixed(2)}%"></i>` : '');
  const rv = review ? reviewState(t) : null;
  const rseg = (k: string, v: number): string => (v ? `<i class="${k}" style="width:${((100 * v) / sum).toFixed(2)}%"></i>` : '');
  const rtip = rv ? `Review: ${fmt(rv.man)} by hand · ${fmt(rv.bulk)} by Review all · ${fmt(rv.left)} of ${fmt(rv.done)} done not reviewed` : '';
  return `<div class="lp-task" title="${esc(tip)}"><div class="lp-tname">${icon(taskInfo(t.task_type)?.icon || 'tag')}` +
    `<b>${esc(t.name)}</b><span class="lp-ncls">${fmt(t.n_classes || 0)} class${t.n_classes === 1 ? '' : 'es'}</span>` +
    `<span class="lp-pct num">${Math.round((100 * n.done) / sum)}%</span></div>` +
    `<div class="lp-bar" role="img" aria-label="${esc(tip)}">${seg('done')}${seg('in_progress')}${seg('skipped')}</div>` +
    (rv ? `<div class="lp-bar lp-review" role="img" title="${esc(rtip)}" aria-label="${esc(rtip)}">${rseg('manual', rv.man)}${rseg('bulk', rv.bulk)}</div>` : '') + '</div>';
}

// one small line under the task bars: labeled every task? reviewed every task? (bulk-only review is marked)
function checklistHTML(p: HomeProject): string {
  const tasks = p.tasks || [];
  if (!p.review || !tasks.length) return '';
  const total = p.n_media || 0;
  const labeled = total > 0 && tasks.every((t) => (t.done || 0) + (t.skipped || 0) >= total);
  const rs = tasks.map(reviewState);
  const reviewed = rs.filter((r) => r.all).length;
  const anyBulk = rs.some((r) => r.bulk > 0);
  const item = (ok: boolean, text: string, title: string): string => `<span class="lp-chk${ok ? ' ok' : ''}" title="${esc(title)}">${icon(ok ? 'check' : 'circle')}${text}</span>`;
  return '<div class="lp-checklist">' +
    item(labeled, 'Labeled', 'Every image is done or skipped in every task') +
    item(reviewed === tasks.length, `Reviewed ${reviewed}/${tasks.length}`, 'Tasks whose done images are all reviewed') +
    (anyBulk ? `<span class="lp-chk bulk" title="Some images were approved with Review all, not one by one">${icon('alert')}bulk</span>` : '') +
    '</div>';
}

function labelCardHTML(p: HomeProject): string {
  const src = lpSource(p);
  const cover = p.cover || [];
  const badge = `<span class="sc-src lbl">${icon('tag')}Labeling</span><span class="sc-live lp-src">${icon(src.ico)}${esc(src.label)}</span>`;
  let mosaic: string;
  if (!cover.length) {
    mosaic = `<div class="sc-cover empty">${badge}<div class="sc-glyph">${icon('image')}<span>No images yet: open it to import some</span></div></div>`;
  } else {
    let imgs = '';
    for (let k = 0; k < 4; k++) {
      imgs += k < cover.length
        ? `<img src="api/label/projects/${encodeURIComponent(p.id)}/thumb/${encodeURIComponent(cover[k] ?? '')}" alt="" width="160" height="160" loading="lazy" decoding="async">`
        : '<span class="sc-tile"></span>';
    }
    mosaic = `<div class="sc-cover">${badge}<div class="sc-mosaic">${imgs}</div></div>`;
  }
  const tasks = p.tasks || [];
  const more = tasks.length - TASK_MAX;
  return `<article class="session-card label-card${H.flash === p.id ? ' flash' : ''}" data-pid="${esc(p.id)}">${mosaic}<div class="sc-body">` +
    `<h3 class="sc-title"><button class="sc-open" data-act="lopen" title="Open ${esc(p.name)}">${esc(p.name)}</button></h3>` +
    tagsHTML(tagsOf(p)) +
    (src.uri ? `<div class="sc-uri" title="${esc(src.uri)}"><bdi>${esc(src.uri)}</bdi></div>` : `<div class="sc-uri plain">${esc(src.plain ?? '')}</div>`) +
    `<div class="sc-stats"><span><b class="num">${fmt(p.n_media || 0)}</b>images</span><span><b class="num">${tasks.length}</b>task${tasks.length === 1 ? '' : 's'}</span></div>` +
    (tasks.length ? `<div class="lp-tasks">${tasks.slice(0, TASK_MAX).map((t) => taskHTML(t, p.n_media || 0, p.review)).join('')}` +
      (more > 0 ? `<span class="lp-more">+${more} more task${more === 1 ? '' : 's'}</span>` : '') + '</div>' : '') +
    checklistHTML(p) +
    '</div><footer class="sc-foot">' +
    `<span class="sc-when">${p.updated ? `updated ${esc(timeAgo(p.updated))}` : ''}</span><span class="grow"></span>` +
    `<button class="icon-btn ghost" data-act="lexport" aria-label="Export ${esc(p.name)}" title="Export the labels">${icon('download')}</button>` +
    editBtn('ltags', p, 'Edit tags, or open the full edit to rename and add tasks or classes') +
    `<button class="icon-btn ghost danger" data-act="lremove" aria-label="Remove ${esc(p.name)}" title="Remove the labeling session">${icon('trash')}</button>` +
    `<button class="icon-btn ghost lp-more" data-act="lmore" aria-haspopup="menu" aria-expanded="false" aria-label="More actions for ${esc(p.name)}" title="More: duplicate, manage classes, re-export"><span aria-hidden="true">⋯</span></button>` +
    '</footer><div class="sc-busy" aria-hidden="true"><span class="spin"></span>Opening…</div></article>';
}

// 0 sessions of both kinds (a fresh checkout): one welcome screen instead of two empty tabs
function emptyHTML(): string {
  const step = (n: number, title: string, text: string): string => `<li><span class="es-n num">${n}</span><div><b>${title}</b><span>${text}</span></div></li>`;
  return '<div class="home-empty">' +
    `<div class="es-mark"><svg class="brand-mark" viewBox="0 0 32 32" fill="none" stroke="currentColor" aria-hidden="true"><use href="static/assets/icons.svg#i-brand"/></svg></div>` +
    '<h2>No sessions yet</h2>' +
    '<p class="muted">Visualize a YOLO dataset (a folder on this machine or an S3 / RustFS prefix with images/ and labels/), or label a folder of images: boxes, polygons, keypoints or tags, one or more tasks at once.</p>' +
    `<div class="es-actions"><button class="primary big" data-act="new-viz">${icon('scatter')}Visualize a dataset</button>` +
    `<button class="big" data-act="new-label">${icon('tag')}Label images</button></div>` +
    '<ol class="es-steps">' +
    step(1, 'Pick the images', 'Local folders are read in place; S3 / FTP stream; dropped files are uploaded once.') +
    step(2, 'Explore or label', 'Every box becomes a dot to lasso, or draw boxes and polygons with the keyboard.') +
    step(3, 'Export', 'Labels go out per task in YOLO format, to a zip, a folder, S3, FTP or a database.') +
    '</ol></div>';
}

// most recently used first; the open visualize session first of all
function stamp(x: Item): number {
  if (x.p) return (x.p.updated || x.p.created || 0) * 1000;
  const st = x.d.stats || {};
  const t = st.last_opened ? Date.parse(String(st.last_opened).replace(' ', 'T')) : NaN;
  return Number.isFinite(t) ? t : (x.d.created || 0) * 1000;
}

function sorted(list: Item[]): Item[] {
  for (const x of list) x.t = stamp(x);
  return list.sort((a, b) => ((b.d && b.d.active) ? 1 : 0) - ((a.d && a.d.active) ? 1 : 0) || b.t - a.t);
}

interface TabDef {
  err: 'ds' | 'lp'; what: string; act: string; ico: string; tile: string; empty: [string, string];
  items: () => Item[]; of: (x: Item) => HomeSession | HomeProject; card: (x: Item) => string;
}
const TAB: Record<TabId, TabDef> = {
  viz: {
    err: 'ds', what: 'the visualize sessions', act: 'new-viz', ico: 'scatter',
    items: () => sorted(H.sessions.map((d): Item => ({ d, t: 0 }))),
    of: (x) => x.d ?? x.p,
    card: (x) => (x.d ? cardHTML(x.d) : ''),
    tile: 'A YOLO folder, an S3 / RustFS prefix, or a task of a labeling session',
    empty: ['No visualize sessions yet', 'Explore a YOLO dataset as a map of its boxes: lasso groups, find outliers, suspicious labels and near-duplicates. A labeling session can be visualized too, live.'],
  },
  label: {
    err: 'lp', what: 'the labeling sessions', act: 'new-label', ico: 'tag',
    items: () => sorted(H.projects.map((p): Item => ({ pid: p.id, p, t: 0 }))),
    of: (x) => x.p ?? x.d,
    card: (x) => (x.p ? labelCardHTML(x.p) : ''),
    tile: 'Drop or point at a folder of images, then draw boxes, polygons, keypoints or tags',
    empty: ['No label sessions yet', 'Label a folder of images for one or more tasks at once, then export YOLO labels or send them to a database.'],
  },
};

function errLine(key: 'ds' | 'lp', what: string): string {
  return H.errs[key] ? `<div class="status-line err">${icon('alert')}Could not load ${what}: ${esc(H.errs[key])}` +
    `<button data-act="reload">${icon('refresh')}Retry</button></div>` : '';
}

// list: the sessions that match the filter; total: all of the tab's sessions
function panelHTML(t: TabId, list: Item[], total: number): string {
  const T = TAB[t];
  const err = errLine(T.err, T.what);
  const empty = list.length || err ? ''
    : total ? `<div class="tab-empty filtered">${icon('search')}<div><b>No sessions match</b><span class="muted">` +
      `None of the ${fmt(total)} ${t === 'viz' ? 'visualize' : 'label'} session${total === 1 ? '' : 's'} ${filterText()}.</span></div>` +
      `<button data-act="clear-filter">${icon('x')}Clear filters</button></div>`
    : `<div class="tab-empty">${icon(T.ico)}<div><b>${T.empty[0]}</b><span class="muted">${T.empty[1]}</span></div></div>`;
  return (err ? `<div class="tab-note">${err}</div>` : '') + empty + list.map(T.card).join('') +
    `<button class="session-new" data-act="${T.act}"><span class="sn-plus">${icon('plus')}</span><b>${NEW_TEXT[t]}</b>` +
    `<span class="muted">${T.tile}</span></button>`;
}

function filterText(): string {
  const parts: string[] = [];
  if (H.q) parts.push(`has “${esc(H.q)}” in its name`);
  if (H.tagSel.size) parts.push(`has the tag${H.tagSel.size === 1 ? '' : 's'} ${[...H.tagSel].map((t) => `“${esc(t)}”`).join(' + ')}`);
  return parts.join(' and ');
}

// the tag bar: the current tab's tags with their counts, most used first (selected ones always stay, to be unselected)
function renderTagbar(): void {
  const bar = $req('home-tagbar');
  const seen = new Map<string, { name: string; n: number }>();
  for (const x of TAB[H.tab || 'viz'].items()) {
    for (const t of tagsOf(TAB[H.tab || 'viz'].of(x))) {
      const k = fold(t);
      let e = seen.get(k);
      if (!e) { e = { name: t, n: 0 }; seen.set(k, e); }
      e.n++;
    }
  }
  for (const k of H.tagSel) if (!seen.has(k)) seen.set(k, { name: k, n: 0 });
  const all = [...seen.entries()].sort((a, b) => b[1].n - a[1].n || a[0].localeCompare(b[0]));
  bar.classList.toggle('hidden', !all.length);
  bar.innerHTML = all.length ? `<span class="htb-label">${icon('tag')}Tags</span>` + all.map(([k, e]) => {
    const on = H.tagSel.has(k);
    return `<button class="htb-tag${on ? ' on' : ''}" data-act="tagfilter" data-tag="${esc(e.name)}" aria-pressed="${on}">` +
      `<span class="nm">${esc(e.name)}</span><span class="num">${e.n}</span></button>`;
  }).join('') + (H.tagSel.size ? `<button class="ghost htb-clear" data-act="tagclear">${icon('x')}Clear</button>` : '') : '';
}

// the grids, the tab counts and the tag bar (not the tabs / search box: typing keeps its focus)
function renderPanels(): void {
  closeTagEdit(false);
  closeCardMenu(false);
  const on = filtering();
  for (const t of TABS) {
    const all = TAB[t].items();
    const list = on ? all.filter((x) => { const o = TAB[t].of(x); return matches(o.name, tagsOf(o)); }) : all;
    $req(`home-n-${t}`).textContent = H.errs[TAB[t].err] ? '' : on ? `${list.length}/${all.length}` : String(all.length);   // no count while its list failed to load
    $req(`home-panel-${t}`).classList.remove('is-empty');
    $req(`home-panel-${t}`).innerHTML = panelHTML(t, list, all.length);
  }
  renderTagbar();
}

function readTab(): TabId | null {
  try { const t = localStorage.getItem(TAB_KEY); return isTab(t) ? t : null; } catch (e) { return null; }
}

// remember: a choice of the user (a click / arrow key), not the first render's pick
function setTab(t: TabId, focus: boolean, remember: boolean): void {
  H.tab = t;
  if (remember) {
    try { localStorage.setItem(TAB_KEY, t); } catch (e) { /* private window: the tab is just not remembered */ }
  }
  for (const k of TABS) {
    const on = k === t;
    const b = $req(`home-tab-${k}`);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
    $req(`home-panel-${k}`).classList.toggle('hidden', !on);
  }
  $req('home-new-text').textContent = NEW_TEXT[t];
  if (focus) $req(`home-tab-${t}`).focus();
  closeCardMenu(false);
  closeTagEdit(false);
  renderTagbar();
}

function render(): void {
  const lists: Record<TabId, Item[]> = { viz: TAB.viz.items(), label: TAB.label.items() };
  const failed = !!(H.errs.ds || H.errs.lp);
  const none = !lists.viz.length && !lists.label.length && !failed;
  $req('home-head').classList.toggle('hidden', none);
  $req('home-tabs').classList.toggle('hidden', none);
  $req('home-filter').classList.toggle('hidden', none);
  if (none) {
    closeTagEdit(false);
    $req('home-panel-viz').classList.remove('hidden');
    $req('home-panel-label').classList.add('hidden');
    $req('home-panel-viz').classList.add('is-empty');
    $req('home-panel-viz').innerHTML = emptyHTML();
    $req('home-panel-label').innerHTML = '';
    return;
  }
  if (!H.tab) {                                // the first paint: remembered tab, unless only the other kind has sessions
    const want = readTab() || 'viz';
    const other = want === 'viz' ? 'label' : 'viz';
    H.tab = !lists[want].length && lists[other].length ? other : want;
  }
  renderPanels();
  setTab(H.tab, false, false);
}

async function fetchSessions(): Promise<void> {
  try {
    const { datasets } = await getJSON<{ datasets: HomeSession[] }>('api/datasets');
    H.sessions = datasets;
    H.errs.ds = null;
  } catch (e) { H.errs.ds = errText(e); }
}

async function fetchProjects(): Promise<void> {
  try {
    const { projects } = await getJSON<{ projects: HomeProject[] }>('api/label/projects');
    H.projects = projects;
    H.errs.lp = null;
  } catch (e) { H.errs.lp = errText(e); }
}

async function load(): Promise<void> {
  await Promise.all([fetchSessions(), fetchProjects()]);
  render();
}

function open(sel: string, url: string): void {
  if (H.opening) return;                       // a double click opens once
  H.opening = true;
  const card = document.querySelector(sel);
  if (card) card.classList.add('opening');
  document.body.classList.add('home-opening');
  location.href = url;
}

const openSession = (id: string): void => open(`.session-card[data-id="${CSS.escape(id)}"]`, `./?session=${encodeURIComponent(id)}`);
const openProject = (pid: string): void => open(`.session-card[data-pid="${CSS.escape(pid)}"]`, `./?label=${encodeURIComponent(pid)}`);

async function remove(id: string): Promise<void> {
  const d = H.sessions.find((x) => x.id === id);
  if (!d || !confirm(`Remove the session "${d.name}"?\n\nNo dataset files are deleted; its cached crops and features stay on disk.`)) return;
  try { await api(`api/datasets/${encodeURIComponent(id)}`, { method: 'DELETE' }); } catch (e) { alert(errText(e)); }
  await load();
}

// ---- small confirm dialog (#home-dlg): resolves {checked} on OK, null on cancel / Esc / backdrop
interface ConfirmOpts { title: string; text: string; check?: string; ok: string }
function confirmDlg({ title, text, check, ok }: ConfirmOpts): Promise<{ checked: boolean } | null> {
  const dlg = $req('home-dlg');
  const back = document.activeElement;
  const okBtn = $req('home-dlg-ok'), cancelBtn = $req('home-dlg-cancel'), cb = $req<HTMLInputElement>('home-dlg-cb');
  $req('home-dlg-title').textContent = title;
  $req('home-dlg-text').textContent = text;
  $req('home-dlg-check').classList.toggle('hidden', !check);
  cb.checked = false;
  $req('home-dlg-cb-text').textContent = check || '';
  okBtn.textContent = ok;
  dlg.classList.remove('hidden');
  cancelBtn.focus();
  return new Promise((resolve) => {
    const done = (v: { checked: boolean } | null): void => {
      dlg.classList.add('hidden');
      dlg.onkeydown = dlg.onmousedown = okBtn.onclick = cancelBtn.onclick = null;
      if (back instanceof HTMLElement && document.contains(back)) back.focus();
      resolve(v);
    };
    okBtn.onclick = () => done({ checked: cb.checked });
    cancelBtn.onclick = () => done(null);
    dlg.onmousedown = (e) => { if (e.target === dlg) done(null); };
    dlg.onkeydown = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); return; }
      if (e.key !== 'Tab') return;                // keep focus inside
      const f = [...dlg.querySelectorAll<HTMLElement>('button, input')].filter((x) => x.offsetParent);
      const i = f.findIndex((x) => x === document.activeElement);
      e.preventDefault();
      f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length]?.focus();
    };
  });
}

async function removeProject(pid: string): Promise<void> {
  const p = H.projects.find((x) => x.id === pid);
  if (!p) return;
  const r = await confirmDlg({ title: `Remove “${p.name}”?`, ok: 'Remove',
    text: 'The labeling session leaves this list. Without the box below, its uploaded images and annotations stay on disk (the workspace labeling/ folder); source folders and buckets are never touched.',
    check: 'Also delete the uploaded images and every annotation (cannot be undone)' });
  if (!r) return;
  try { await api(`api/label/projects/${encodeURIComponent(pid)}?delete_files=${r.checked ? 1 : 0}`, { method: 'DELETE' }); } catch (e) { alert(errText(e)); }
  await load();
}

// the label session names the wizard checks a new name against (minus the one being edited)
const labelNames = (pid: string | null): string[] => H.projects.filter((p) => p.id !== pid).map((p) => p.name);
const wizard = (opts: WizardOpts): Promise<void> => import('./label-new.js').then((m) => m.openWizard(opts))
  .catch((e: unknown) => { $req('home-status').innerHTML = `<div class="status-line err">${icon('alert')}Could not open the wizard: ${esc(errText(e))}</div>`; });

function exportProject(pid: string): void {
  import('../label/export.js').then((m) => m.openExportDialog(pid))
    .catch((e: unknown) => { $req('home-status').innerHTML = `<div class="status-line err">${icon('alert')}Could not open the export dialog: ${esc(errText(e))}</div>`; });
}

// ---- a labeling card's ⋯ menu: duplicate, manage classes, re-export (a popover built on demand)
const actions = () => import('../label/session-actions.js');
const failLine = (what: string, e: unknown): void => { $req('home-status').innerHTML = `<div class="status-line err">${icon('alert')}Could not open ${esc(what)}: ${esc(errText(e))}</div>`; };

function lastTargetText(p: HomeProject): string {
  const tg: HomeExportTarget | null | undefined = p.export_default && p.export_default.target;
  if (!tg || !tg.kind) return '';
  if (tg.kind === 'zip') return 'zip';
  if (tg.kind === 'local') return tg.path || 'local folder';
  if (tg.kind === 's3') return `s3://${tg.bucket || ''}/${tg.prefix || ''}`;
  return tg.kind === 'db' ? 'database' : `${tg.kind}:${tg.path || '/'}`;
}

function openCardMenu(from: HTMLElement): void {
  closeCardMenu(false);
  const pid = dataOf(from, 'pid');
  const p = H.projects.find((x) => x.id === pid);
  if (!p) return;
  let m = $('home-cardmenu');
  if (!m) {
    const menu = m = document.createElement('div');
    menu.id = 'home-cardmenu';
    menu.className = 'lb-menu panel home-cardmenu';
    menu.setAttribute('role', 'menu');
    document.body.appendChild(menu);
    menu.addEventListener('click', (e) => {
      const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-m]') : null;
      if (!b) return;
      const id = menu.dataset['pid'] ?? '';
      closeCardMenu(false);
      CARD_MENU[b.dataset['m'] ?? '']?.(id);
    });
    menu.addEventListener('keydown', (e) => {
      const its = [...menu.querySelectorAll<HTMLElement>('[role=menuitem]')];
      const i = its.findIndex((x) => x === document.activeElement);
      if (e.key === 'Escape') { e.preventDefault(); closeCardMenu(true); }
      else if (e.key === 'Tab') closeCardMenu(false);
      else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); its[(i + (e.key === 'ArrowDown' ? 1 : -1) + its.length) % its.length]?.focus(); }
    });
  }
  const menuEl = m;
  const reItem = (re: string): string => `<button role="menuitem" data-m="reexport" title="${esc(`Export again with the last choices to ${re}`)}">${icon('refresh')}<span class="nm">Re-export to ${esc(re)}</span></button>`;
  menuEl.dataset['pid'] = pid;
  const boxTasks = (p.tasks || []).filter((t) => VIZ_TASKS.has(t.task_type)).length;
  menuEl.innerHTML = (boxTasks ? `<button role="menuitem" data-m="viz" title="Explore the boxes of a task as a map: a visualize session reading this labeling session live">${icon('scatter')}Visualize a task…</button>` : '') +
    `<button role="menuitem" data-m="dup">${icon('layers')}Duplicate…</button>` +
    `<button role="menuitem" data-m="classes">${icon('tag')}Manage classes…</button>` +
    `<button role="menuitem" data-m="import">${icon('upload')}Import annotations…</button>` +
    (p.review ? `<button role="menuitem" data-m="review" title="Mark every done, not yet reviewed image as reviewed (shown apart from images you review one by one)">${icon('check')}Review all…</button>` : '') +
    (p.export_default !== undefined && lastTargetText(p) ? reItem(lastTargetText(p)) : '');
  if (p.export_default === undefined) {            // the list rows carry no export_default: read it once
    getJSON<Pick<HomeProject, 'export_default'>>(`api/label/projects/${encodeURIComponent(pid)}`).then((full) => {
      p.export_default = full.export_default || null;
      const re = lastTargetText(p);
      if (re && H.menuFrom === from && menuEl.dataset['pid'] === pid && !menuEl.querySelector('[data-m="reexport"]')) menuEl.insertAdjacentHTML('beforeend', reItem(re));
    }).catch(() => {});
  }
  placePopover(menuEl, from);
  H.menuFrom = from;
  from.setAttribute('aria-expanded', 'true');
  menuEl.querySelector<HTMLElement>('[role=menuitem]')?.focus();
}

// ---- popovers (the ⋯ menu, the tag editor): shown under their button, right-aligned, flipped up when no room
function placePopover(m: HTMLElement, from: HTMLElement): void {
  m.classList.remove('hidden');
  const r = from.getBoundingClientRect();
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.right - w))}px`;
  m.style.top = `${r.bottom + 4 + h <= innerHeight - 8 ? r.bottom + 4 : Math.max(8, r.top - 4 - h)}px`;
}

// hide a popover, give its button back aria-expanded=false (and the focus on Esc)
function hidePopover(m: HTMLElement, from: HTMLElement | null, refocus: boolean): void {
  m.classList.add('hidden');
  if (!from) return;
  from.setAttribute('aria-expanded', 'false');
  if (refocus && document.contains(from)) from.focus();
}

function closeCardMenu(refocus: boolean): void {
  const m = $('home-cardmenu');
  if (!m || m.classList.contains('hidden')) return;
  const from = H.menuFrom;
  H.menuFrom = null;
  hidePopover(m, from, refocus);
}

// ---- tag editor (#home-tagedit): chips with ×, an input (Enter / comma adds, a pasted list splits on commas and
// newlines, Backspace on empty removes the last), suggestions from the tags of the same list, Save / Cancel.
// The client mirrors the server rules (trim, collapse spaces, ≤ 32 chars, ≤ 20, case-insensitive dedupe).
const cleanTag = (t: unknown): string => String(t).trim().replace(/\s+/g, ' ');

function teItem(te: TagEdit): HomeSession | HomeProject | undefined {
  return te.kind === 'viz' ? H.sessions.find((x) => x.id === te.id) : H.projects.find((x) => x.id === te.id);
}

function openTagEdit(from: HTMLElement, kind: TabId): void {
  const id = kind === 'viz' ? dataOf(from, 'id') : dataOf(from, 'pid');
  closeCardMenu(false);
  closeTagEdit(false);
  const x = kind === 'viz' ? H.sessions.find((d) => d.id === id) : H.projects.find((p) => p.id === id);
  if (!x) return;
  let m = $('home-tagedit');
  if (!m) {
    m = document.createElement('div');
    m.id = 'home-tagedit';
    m.className = 'panel home-tagedit hidden';
    m.setAttribute('role', 'dialog');
    document.body.appendChild(m);
    m.innerHTML = '<div class="te-head"><b id="home-tagedit-title"></b><span class="te-n num"></span></div>' +
      '<div class="te-box"><span class="te-chips"></span>' +
      `<input id="home-tagedit-in" type="text" list="home-tagedit-list" autocomplete="off" spellcheck="false" aria-label="New tag"></div>` +
      '<datalist id="home-tagedit-list"></datalist>' +
      '<div class="te-note hint"></div><div class="te-err status-line err hidden" role="alert"></div>' +
      `<div class="te-foot"><button class="ghost" data-te="add">${icon('plus')}Add</button>` +
      `<button class="ghost" data-te="full">${icon('edit')}Full edit…</button><span class="grow"></span>` +
      '<button class="ghost" data-te="cancel">Cancel</button><button class="primary" data-te="save">Save</button></div>';
    m.setAttribute('aria-labelledby', 'home-tagedit-title');
    bindTagEdit(m);
  }
  H.te = { kind, id, from, tags: tagsOf(x).slice(), err: '', busy: false };
  $req('home-tagedit-title').textContent = `Tags · ${x.name}`;
  const inp = m.querySelector<HTMLInputElement>('#home-tagedit-in');
  if (inp) inp.value = '';
  teRefresh();
  placePopover(m, from);
  from.setAttribute('aria-expanded', 'true');
  inp?.focus();
}

function closeTagEdit(refocus: boolean): void {
  const m = $('home-tagedit');
  if (!m || !H.te) return;
  const from = H.te.from;
  H.te = null;
  hidePopover(m, from, refocus);
}

// chips, counter, note, suggestions and the buttons' state from H.te
function teRefresh(): void {
  const te = H.te, m = $('home-tagedit');
  if (!te || !m) return;
  const full = te.tags.length >= MAX_TAGS;
  const q = <T extends HTMLElement = HTMLElement>(sel: string): T => {
    const el = m.querySelector<T>(sel);
    if (!el) throw new Error(`missing ${sel} in the tag editor`);
    return el;
  };
  q('.te-chips').innerHTML = te.tags.map((t, i) => `<span class="te-chip"><span class="nm">${esc(t)}</span>` +
    `<button data-te="rm" data-i="${i}" aria-label="Remove the tag ${esc(t)}" title="Remove">${icon('x')}</button></span>`).join('');
  q('.te-n').textContent = `${te.tags.length} / ${MAX_TAGS}`;
  q('.te-n').classList.toggle('full', full);
  const inp = q<HTMLInputElement>('#home-tagedit-in');
  inp.disabled = full || te.busy;
  inp.placeholder = full ? '' : te.tags.length ? 'Add a tag…' : 'Add a tag: Enter or comma adds it';
  q('.te-note').textContent = full ? `That is the limit: a session can have at most ${MAX_TAGS} tags. Remove one to add another.`
    : `Up to ${MAX_TAGS} tags of ${TAG_LEN} characters at most; a click on a tag on a card filters by it.`;
  q<HTMLButtonElement>('[data-te="add"]').disabled = full || te.busy;
  q<HTMLButtonElement>('[data-te="save"]').disabled = te.busy;
  q('[data-te="save"]').textContent = te.busy ? 'Saving…' : 'Save';
  const err = q('.te-err');
  err.classList.toggle('hidden', !te.err);
  err.innerHTML = te.err ? `${icon('alert')}<span>${esc(te.err)}</span>` : '';
  // suggestions: the tags already used in the same list, minus this session's
  const have = new Set(te.tags.map(fold)), sug = new Map<string, string>();
  for (const o of te.kind === 'viz' ? H.sessions : H.projects) {
    for (const t of tagsOf(o)) if (!have.has(fold(t)) && !sug.has(fold(t))) sug.set(fold(t), t);
  }
  q('#home-tagedit-list').innerHTML = [...sug.values()].sort((a, b) => a.localeCompare(b))
    .map((t) => `<option value="${esc(t)}"></option>`).join('');
  if (!m.classList.contains('hidden') && document.contains(te.from)) placePopover(m, te.from);   // it grew: keep it on screen
}

// add raw tags (typed / pasted) -> the refused ones that are worth fixing (too long), plus .over: some did not fit in 20
interface AddResult { bad: string[]; over: boolean }
function teAdd(raw: string[]): AddResult {
  const te = H.te;
  const res: AddResult = { bad: [], over: false };
  if (!te) return res;
  const bad = res.bad;
  te.err = '';
  for (const r of raw) {
    const t = cleanTag(r);
    if (!t) continue;
    if (t.length > TAG_LEN) { te.err = `“${t.slice(0, TAG_LEN)}…” is too long: a tag has at most ${TAG_LEN} characters.`; bad.push(t); continue; }
    if (te.tags.some((x) => fold(x) === fold(t))) continue;
    if (te.tags.length >= MAX_TAGS) { te.err = `A session can have at most ${MAX_TAGS} tags: the rest were not added.`; res.over = true; break; }
    te.tags.push(t);
  }
  teRefresh();
  return res;
}

// add the text of the box (Enter / comma / Add / Save / paste); a too-long tag stays in the box to be fixed.
// false when something was refused (Save then stops once, so the message is read)
function teCommit(extra = ''): boolean {
  const inp = $req<HTMLInputElement>('home-tagedit-in');
  const text = inp.value + extra;
  if (!text.trim()) { inp.value = ''; return true; }
  const { bad, over } = teAdd(text.split(/[,\n\r]+/));
  inp.value = bad.join(', ');
  return !bad.length && !over;
}

async function teSave(): Promise<void> {
  const te = H.te;
  if (!te || te.busy || !teCommit()) return;
  te.busy = true;
  te.err = '';
  teRefresh();
  const enc = encodeURIComponent(te.id);
  const req = te.kind === 'viz' ? { url: `api/datasets/${enc}/tags`, method: 'PUT' } : { url: `api/label/projects/${enc}`, method: 'PATCH' };
  try {
    const r = await api(req.url, { method: req.method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags: te.tags }) });
    const out: { tags?: unknown } = await r.json().catch(() => ({}));
    if (H.te !== te) return;
    const x = teItem(te);
    if (x) x.tags = strList(out.tags) ?? te.tags;      // the server's cleaned list
    const sel = te.kind === 'viz' ? `.session-card[data-id="${CSS.escape(te.id)}"] [data-act="tags"]`
      : `.session-card[data-pid="${CSS.escape(te.id)}"] [data-act="ltags"]`;
    closeTagEdit(false);
    renderPanels();
    const b = document.querySelector<HTMLElement>(sel);
    if (b) b.focus();
  } catch (e) {
    if (H.te !== te) return;
    te.busy = false;
    te.err = errText(e);
    teRefresh();
    $req('home-tagedit-in').focus();
  }
}

function bindTagEdit(m: HTMLElement): void {
  const inp = m.querySelector<HTMLInputElement>('#home-tagedit-in');
  if (!inp) return;
  m.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-te]') : null;
    if (!b || (b instanceof HTMLButtonElement && b.disabled) || !H.te) return;
    const a = b.dataset['te'];
    if (a === 'rm') { H.te.tags.splice(+(b.dataset['i'] ?? NaN), 1); H.te.err = ''; teRefresh(); inp.focus(); }
    else if (a === 'add') { teCommit(); inp.focus(); }
    else if (a === 'cancel') closeTagEdit(true);
    else if (a === 'full') {
      const { kind, id } = H.te;
      closeTagEdit(false);
      if (kind === 'viz') editSession(id); else editProject(id);
    }
    else if (a === 'save') void teSave();
  });
  inp.addEventListener('keydown', (e) => {
    if (!H.te) return;
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { void teSave(); return; }
      teCommit();
    } else if (e.key === 'Backspace' && !inp.value && H.te.tags.length && !H.te.busy) {
      e.preventDefault();
      H.te.tags.pop();
      H.te.err = '';
      teRefresh();
    }
  });
  // phone keyboards send no keydown for ','; a typed comma still splits
  inp.addEventListener('input', () => {
    if (!H.te || !/,/.test(inp.value)) return;
    const parts = inp.value.split(',');
    const rest = parts.pop() ?? '';
    const { bad, over } = teAdd(parts);
    inp.value = [...bad, over ? '' : rest].filter(Boolean).join(', ');
  });
  inp.addEventListener('paste', (e) => {
    const text = e.clipboardData?.getData('text') ?? '';
    if (!H.te || !/[,\n\r]/.test(text)) return;          // one tag: the normal paste
    e.preventDefault();
    teCommit(text);
  });
  m.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeTagEdit(true); return; }
    if (e.key !== 'Tab') return;                  // keep focus inside
    const f = [...m.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button, input')].filter((x) => !x.disabled && x.offsetParent);
    const i = f.findIndex((x) => x === document.activeElement);
    e.preventDefault();
    f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length]?.focus();
  });
}

function toggleTagFilter(b: HTMLElement): void {
  const k = fold(b.dataset['tag']);
  if (H.tagSel.has(k)) H.tagSel.delete(k); else H.tagSel.add(k);
  // the clicked button is rebuilt: put the focus back on its twin (same card or the tag bar)
  const card = b.closest<HTMLElement>('.session-card');
  const sel = card ? (card.dataset['pid'] ? `.session-card[data-pid="${CSS.escape(card.dataset['pid'])}"]` : `.session-card[data-id="${CSS.escape(card.dataset['id'] ?? '')}"]`) : '#home-tagbar';
  renderPanels();
  const scope = document.querySelector(sel);
  const twin = scope && [...scope.querySelectorAll<HTMLElement>('[data-act="tagfilter"]')].find((x) => fold(x.dataset['tag']) === k);
  (twin || $req('home-search')).focus();
}

function clearFilters(keepQuery: boolean): void {
  if (!keepQuery) { H.q = ''; $req<HTMLInputElement>('home-search').value = ''; }
  H.tagSel.clear();
  renderPanels();
  $req('home-search').focus();
}

async function showNewCard(p?: { id?: string } | null): Promise<void> {
  if (!p || !p.id) { await load(); return; }
  H.flash = p.id;
  await load();
  const card = document.querySelector(`.session-card[data-pid="${CSS.escape(p.id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  setTimeout(() => { H.flash = null; const c = document.querySelector('.session-card.flash'); if (c) c.classList.remove('flash'); }, 2400);
}

// the visualize form for a labeling task, prefilled with this labeling session (settings/forms.js)
const visualizeTask = (pid: string): void => { setTab('viz', false, true); void openSettings('datasets', { solo: true, view: 'labeling', project: pid }); };

// Review all: mark every done, not yet reviewed image of every task as 'bulk' after a confirm that gives the count
let reviewing = false;                // one Review all at a time: a second click while it runs does nothing
async function reviewAll(pid: string): Promise<void> {
  const p = H.projects.find((x) => x.id === pid);
  if (!p || reviewing) return;
  const left = (p.tasks || []).reduce((s, t) => s + reviewState(t).left, 0);
  if (!left) { toast('Nothing to review: every done image is reviewed already'); return; }
  const r = await confirmDlg({ title: 'Review all', ok: `Review ${fmt(left)}`,
    text: `Mark ${fmt(left)} done image${left === 1 ? '' : 's'} as reviewed in "${p.name}"? They show as a separate colour (bulk) from the ones you review one by one, and any later edit puts an image back to not reviewed.` });
  if (!r) return;
  reviewing = true;
  try {
    await postJSON(`api/label/projects/${encodeURIComponent(pid)}/review`, { task_ids: (p.tasks || []).map((t) => t.id), mode: 'bulk' });
    await load();
  } catch (e) { failLine('Review all', e); } finally { reviewing = false; }
}

const CARD_MENU: Record<string, (pid: string) => unknown> = {
  viz: visualizeTask,
  dup: (pid) => actions().then((m) => m.openDuplicate({ pid, project: H.projects.find((x) => x.id === pid) ?? null, onDone: showNewCard }))
    .catch((e: unknown) => failLine('the duplicate dialog', e)),
  classes: (pid) => actions().then((m) => m.openManageClasses({ pid, onDone: () => load() }))
    .catch((e: unknown) => failLine('the classes dialog', e)),
  reexport: (pid) => import('../label/export.js').then((m) => m.reExport(pid)).catch((e: unknown) => failLine('the export dialog', e)),
  review: reviewAll,
  import: (pid) => import('../label/import.js').then((m) => m.openImportDialog({ pid, onDone: () => load() }))
    .catch((e: unknown) => failLine('the import dialog', e)),
};

const editSession = (id: string): void => { void openSettings('datasets', { solo: true, edit: id }); };
const editProject = (pid: string): void => { void wizard({ edit: pid, names: labelNames(pid) }); };

const ACTIONS: Record<string, (b: HTMLElement) => unknown> = {
  open: (b) => openSession(dataOf(b, 'id')),
  edit: (b) => editSession(dataOf(b, 'id')),
  remove: (b) => remove(dataOf(b, 'id')),
  lopen: (b) => openProject(dataOf(b, 'pid')),
  lexport: (b) => exportProject(dataOf(b, 'pid')),
  ledit: (b) => editProject(dataOf(b, 'pid')),
  tags: (b) => { if (H.te && H.te.from === b) closeTagEdit(true); else openTagEdit(b, 'viz'); },
  ltags: (b) => { if (H.te && H.te.from === b) closeTagEdit(true); else openTagEdit(b, 'label'); },
  tagfilter: (b) => toggleTagFilter(b),
  tagclear: () => clearFilters(true),
  'clear-filter': () => clearFilters(false),
  lremove: (b) => removeProject(dataOf(b, 'pid')),
  lmore: (b) => { if (H.menuFrom === b) closeCardMenu(true); else openCardMenu(b); },
  tolabel: (b) => {
    const d = H.sessions.find((x) => x.id === dataOf(b, 'id'));
    if (d && d.project_id) open(`.session-card[data-id="${CSS.escape(d.id)}"]`, `./?label=${encodeURIComponent(d.project_id)}`);
  },
  new: () => (H.tab === 'label' ? wizard({ names: labelNames(null) }) : openSettings('datasets', { solo: true, view: 'add' })),
  'new-viz': () => openSettings('datasets', { solo: true, view: 'add' }),
  'new-label': () => wizard({ names: labelNames(null) }),
  reload: () => load(),
};

function bind(): void {
  if (H.bound) return;
  H.bound = true;
  const onClick = (e: MouseEvent): void => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    const b = t.closest<HTMLElement>('[data-act]');
    const act = b ? ACTIONS[b.dataset['act'] ?? ''] : undefined;
    if (b && !(b instanceof HTMLButtonElement && b.disabled) && act) { act(b); return; }
    const tab = t.closest<HTMLElement>('[role=tab]');
    const tabId = tab?.dataset['tab'];
    if (tab && isTab(tabId)) { setTab(tabId, false, true); return; }
    // anywhere else on a card opens it (the title button is the keyboard / screen-reader way in)
    const card = t.closest<HTMLElement>('.session-card');
    if (!card || t.closest('button, a')) return;
    if (card.dataset['pid']) openProject(card.dataset['pid']);
    else openSession(card.dataset['id'] ?? '');
  };
  const body = $req('home-body');
  body.addEventListener('click', onClick);
  $req('home-status').addEventListener('click', onClick);
  $req('home-new').addEventListener('click', onClick);
  // the tablist: arrows / Home / End move between the two tabs and show them (automatic activation)
  $req('home-tabs').addEventListener('keydown', (e) => {
    const i = H.tab ? TABS.indexOf(H.tab) : -1;
    const moves: Record<string, number> = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 };
    const to = moves[e.key];
    if (to === undefined) return;
    e.preventDefault();
    const next = TABS[(to + TABS.length) % TABS.length];
    if (next) setTab(next, true, true);
  });
  // a crop that is gone (workspace cleaned up) leaves a plain tile, not a broken-image icon; error doesn't bubble
  body.addEventListener('error', (e) => { if (e.target instanceof HTMLImageElement) e.target.classList.add('gone'); }, true);
  $req('home-theme').onclick = () => { setTheme(THEME.name === 'dark' ? 'light' : 'dark'); render(); };
  document.addEventListener('mousedown', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    if (H.menuFrom && !t.closest('#home-cardmenu') && !H.menuFrom.contains(t)) closeCardMenu(false);
    if (H.te && !t.closest('#home-tagedit') && !H.te.from.contains(t)) closeTagEdit(false);
  });
  // the tag editor follows its button (a phone keyboard opening resizes the window: it must not close it)
  const follow = (): void => { closeCardMenu(false); if (H.te && document.contains(H.te.from)) placePopover($req('home-tagedit'), H.te.from); };
  window.addEventListener('resize', follow);
  body.closest('.home-scroll')?.addEventListener('scroll', follow, { passive: true });
  // the name search: one re-render per frame, the box keeps its focus; Esc clears it
  const search = $req<HTMLInputElement>('home-search');
  search.addEventListener('input', () => {
    H.q = search.value.trim().toLowerCase();
    if (!H.raf) H.raf = requestAnimationFrame(() => { H.raf = 0; renderPanels(); });
  });
  search.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !search.value) return;
    e.preventDefault();
    search.value = '';
    H.q = '';
    renderPanels();
  });
  document.addEventListener('sessions-changed', () => { void load(); });
  // back / forward cache: a card left in "Opening…" must be usable again
  window.addEventListener('pageshow', (e) => {
    if (!e.persisted) return;
    H.opening = false;
    document.body.classList.remove('home-opening');
    void load();
  });
}

export async function renderHome(): Promise<void> {
  document.title = 'Sessions · Dataset Visualizer';
  bind();
  // class colours for the chips: the same palettes as the plot (config.yaml ui.palette / palette_light)
  const status = getJSON<Status>('api/status').then((st) => {
    H.palette = strList(st.ui && st.ui['palette']);
    H.paletteLight = strList(st.ui && st.ui['palette_light']);
  }).catch(() => {});
  await Promise.all([fetchSessions(), fetchProjects(), status]);
  render();                                    // one paint, with the chips already coloured
}
