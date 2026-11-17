// The Image page (?image[=viz|label]): the sessions. Two tabs, Visualize and Label (an ARIA tablist; ?image=<tab> opens one,
// else the last one remembered in the user's prefs 'imageTab'; &new=1 opens its New dialog),
// each its own grid, the ones I opened last first, with its own "New … session" tile, empty state and load error line:
// - Visualize: a saved dataset (local folder, S3 prefix or a labeling task read live) opens the visualizer on it
//   (s/<id>/, core/nav.ts vizUrl); new / edit reuse the Settings dataset forms in solo mode;
// - Label: a labeling session (GET api/label/projects) opens the labeling page (?label=<pid>); new / edit open
//   the wizard (views/label-new.js, loaded on demand), export opens label/export.js; its ⋯ menu (#image-cardmenu)
//   has Visualize a task…, Duplicate…, Manage classes… (label/session-actions.js, loaded on demand) and Re-export.
// Find: a name search (#image-search) and a tag filter (#image-tagbar, AND) over the current tab; each session has up to
// 20 tags of its own (under its name; the card's Edit opens the full edit form, whose Tags field saves them:
// core/tag-field.ts, PUT api/datasets/{id}/tags or PATCH api/label/projects/{pid}).
// Whose: a scope filter (#image-scope: All · Mine · Shared with me · Global · Everyone's for admins, prefs 'imageScope')
// passed to both lists as ?scope=. Each card shows its owner, the people it is shared with (views/share.ts edits
// them), a lock (private) or globe (global) badge, and only the actions my role on it allows (viewer < labeler < owner).
// Compare / Combine: Visualize cards can be picked (a checkbox shown on hover / focus, or Ctrl / Cmd + click; Esc clears);
// a bar at the bottom (#image-selbar) then offers Compare (exactly 2) and Combine (2–8). Both POST a combined dataset
// (kind 'union', reuse: the same members in the same order give back the one made before) and open its Compare tab
// (s/<id>/#compare). Combined and embeddings sessions cannot be picked; a combined card lists its members (alias + name).
// One innerHTML render per change, one delegated click listener on #image-body; no Plotly and no job polling here.

import { errText, getJSON, postJSON } from '../core/api.js';
import { toast } from '../core/toast.js';
import { $, $req, esc, icon, ICONS } from '../core/dom.js';
import { can as tierCan, cannot, isAdmin, pref, rememberPref } from '../core/auth.js';
import { avatarStackHTML, ownerChipHTML } from '../core/avatar.js';
import { labelUrl, vizUrl } from '../core/nav.js';
import { setTheme, THEME } from '../core/theme.js';
import { TASK_TYPES } from '../label/state.js';
import type { TaskTypeInfo } from '../label/state.js';
import { MODALITY_ICON, UNION_MAX, unionBlock, VIZ_TASKS } from '../settings/forms.js';
import { openSettings } from '../settings/dialog.js';
import type { Role, Status } from '../types.js';
import type { HomeExportTarget, HomeProject, HomeScope, HomeSession, HomeTask, SharedFields, WizardOpts } from '../types-image.js';
import { openDialog, paras } from './dialog.js';
import { timeAgo } from '../core/format.js';
import { deferDelete } from '../core/defer-delete.js';

// errs: what failed to load ({ds, lp}); the other list still shows
// tab: 'viz' | 'label', picked on the first render (the remembered one, else the kind that has sessions)
type TabId = 'viz' | 'label';
interface HomeState {
  sessions: HomeSession[]; projects: HomeProject[];
  errs: { ds: string | null; lp: string | null };
  palette: string[] | null; paletteLight: string[] | null;
  opening: boolean; bound: boolean; tab: TabId | null;
  menuFrom: HTMLElement | null; flash: string | null; q: string; tagSel: Set<string>; raf: number;
  scope: HomeScope; loadSeq: number;
  sel: string[]; selBusy: '' | 'compare' | 'combine'; selErr: string;
}
const H: HomeState = { sessions: [], projects: [], errs: { ds: null, lp: null }, palette: null, paletteLight: null, opening: false, bound: false, tab: null,
  menuFrom: null, flash: null, q: '', tagSel: new Set(), raf: 0, scope: 'all', loadSeq: 0,
  sel: [], selBusy: '', selErr: '' };
// q: the search text (lower-cased); tagSel: the selected filter tags (lower-cased)
// sel: the picked Visualize cards, in the order they were picked (= the members' order: A, B, …); selBusy: creating the
// combined dataset; selErr: why that failed (the server's 400 message)
const TABS: readonly TabId[] = ['viz', 'label'];
const isTab = (v: unknown): v is TabId => v === 'viz' || v === 'label';
type Item = { d: HomeSession; p?: undefined; pid?: undefined; t: number } | { p: HomeProject; pid: string; d?: undefined; t: number };
const strList = (v: unknown): string[] | null => (Array.isArray(v) && v.every((x) => typeof x === 'string') ? v.map(String) : null);
const dataOf = (el: Element, attr: 'id' | 'pid'): string => el.closest<HTMLElement>(`[data-${attr}]`)?.dataset[attr] ?? '';
const TAB_PREF = 'imageTab', SCOPE_PREF = 'imageScope';
const SCOPES: ReadonlyArray<[HomeScope, string, string]> = [
  ['all', 'All', 'Every session I can open'], ['mine', 'Mine', 'Sessions I created'],
  ['shared', 'Shared with me', 'Sessions someone shared with me'], ['global', 'Global', 'Sessions every user can open'],
  ['everyone', 'Everyone\'s', 'Every session on the server, private ones too (administrators)'],
];
const isScope = (v: unknown): v is HomeScope => SCOPES.some(([k]) => k === v) && (v !== 'everyone' || isAdmin());

// ---- my role on a session: what the card offers (the server checks every request again)
const RANK: Record<Role, number> = { viewer: 1, labeler: 2, owner: 3 };
const roleOf = (x: SharedFields): Role => x.my_role || 'owner';     // no role in the row: an older server, single user
const can = (x: SharedFields, need: Role): boolean => RANK[roleOf(x)] >= RANK[need];
const NEW_TEXT: Record<TabId, string> = { viz: 'Create session', label: 'Create session' };
const MAX_CHIPS = 5;

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

// the people of a session (owner chip, member stack) and when I opened it, at the start of the card's footer; the
// time is short ("26 h ago", the full "opened 26 h ago" as its tooltip) and never cut: the owner's name gives way
function peopleHTML(x: SharedFields, when: string): string {
  const members = (x.members || []).filter((m) => !x.owner || m.id !== x.owner.id);
  const short = when.replace(/^(opened|created|updated) /, '');
  return `<span class="sc-people">${ownerChipHTML(x.owner)}${avatarStackHTML(members)}` +
    `${when ? `<span class="sc-when" title="${esc(when)}">${esc(short)}</span>` : ''}</span>`;
}

// private (lock) or global (globe): next to the source badge on the cover
function visBadge(x: SharedFields): string {
  if (!x.visibility) return '';
  const glob = x.visibility === 'global';
  const n = (x.members || []).length;
  const tip = glob ? 'Global: every user can open it' : n ? `Private: you and ${n} ${n === 1 ? 'person' : 'people'}` : 'Private: only you';
  return `<span class="sc-vis${glob ? ' glob' : ''}" title="${esc(tip)}" role="img" aria-label="${esc(tip)}">${icon(glob ? 'globe' : 'lock')}</span>`;
}

const shareBtn = (x: SharedFields & { name: string }, kind: 'viz' | 'label'): string => (can(x, 'owner')
  ? `<button class="icon-btn ghost" data-act="${kind === 'viz' ? 'share' : 'lshare'}" aria-haspopup="dialog" aria-label="Share ${esc(x.name)}" title="Share: who can open it, who can label">${icon('share')}</button>` : '');

function openedText(x: SharedFields, fallback: string): string {
  return x.opened_at ? `opened ${timeAgo(x.opened_at)}` : fallback;
}


function classColor(i: number): string {
  const p = (THEME.name === 'light' ? H.paletteLight : H.palette) || H.palette || [];
  return p.length ? p[i % p.length] ?? 'var(--text-faint)' : 'var(--text-faint)';
}

function sourceLabel(d: HomeSession): string {
  if (d.kind === 'labeling') return 'Labeling';
  if (d.kind === 'embeddings') return 'Embeddings';
  if (d.kind === 'union') return 'Combined';
  if (d.kind !== 's3') return 'Local';
  return d.mode === 'cache' ? 'S3 · cached' : 'S3 · stream';
}

const KIND_ICON: Record<string, string> = { s3: 'cloud', labeling: 'tag', local: 'folder', embeddings: 'vectors', union: 'layers' };
// an embeddings session's cover: the glyph of what its rows are (audio, video, text...) when there are no images
const modalityGlyph = (d: HomeSession): string => {
  const m = d.modality || [];
  const main = ['video', 'audio', 'text'].find((x) => m.includes(x));      // the richest preview names the session
  return main ? MODALITY_ICON[main] ?? 'vectors' : 'vectors';
};
const modalityHTML = (d: HomeSession): string => (d.kind === 'embeddings' && d.modality?.length
  ? `<span class="sc-modality">${d.modality.map((m) => `<span title="rows carry ${esc(m)} previews">${icon(MODALITY_ICON[m] || 'vectors')}${esc(m)}</span>`).join('')}</span>` : '');
const kindIcon = (d: HomeSession): string => KIND_ICON[d.kind] || 'folder';

// problem: a broken session's short reason, a badge over the cover
function coverHTML(d: HomeSession, problem = ''): string {
  const st = d.stats || {};
  const tag = (problem ? `<span class="sc-problem">${icon('alert')}${esc(problem)}</span>` : '') + `<span class="sc-src${d.kind === 'labeling' ? ' lbl' : d.kind === 'union' ? ' uni' : ''}">${icon(kindIcon(d))}${esc(sourceLabel(d))}</span>` +
    (d.loaded ? `<span class="sc-live" title="Loaded on the server: it opens at once">${icon('check')}Loaded</span>` : '') + visBadge(d);
  if (!d.cover) {
    const emb = d.kind === 'embeddings';
    const note = d.kind === 'union' ? 'Not combined yet: the first open joins its members (nothing is copied)'
      : !emb ? 'Not indexed yet: the first open builds its crops'
      : d.stats?.n_boxes ? `${fmt(d.stats.n_boxes)} rows · ${(d.modality || ['vector']).join(' · ')}` : 'Not read yet: the first open reads its vectors';
    return `<div class="sc-cover empty${emb ? ' emb' : ''}">${tag}<div class="sc-glyph">${icon(emb ? modalityGlyph(d) : d.kind === 's3' || d.kind === 'labeling' || d.kind === 'union' ? kindIcon(d) : 'db')}` +
      `<span>${esc(note)}</span></div></div>`;
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

// the card's Edit button opens the full edit (the settings form / the wizard), tags included
const editBtn = (act: string, x: { name: string }, title: string): string => `<button class="icon-btn ghost" data-act="${act}" aria-haspopup="dialog" ` +
  `aria-label="Edit ${esc(x.name)}" title="${title}">${icon('edit')}</button>`;

// the pick box of a Visualize card (top-left of its cover); a session that cannot be a member gets a disabled one + why
function pickHTML(d: HomeSession): string {
  const why = unionBlock(d);
  const on = H.sel.includes(d.id);
  const tip = why || (on ? 'Selected: Compare or Combine it below (Ctrl / Cmd + click toggles too)' : 'Select to compare or combine with other sessions (Ctrl / Cmd + click)');
  return `<label class="sc-pick${why ? ' off' : ''}" title="${esc(tip)}"><input type="checkbox" data-act="pick" ${on ? 'checked' : ''} ${why ? 'disabled' : ''} ` +
    `aria-label="${esc(why ? `${d.name}: ${why}` : `Select ${d.name} to compare or combine`)}"></label>`;
}

// a combined dataset's members, in priority order: alias + name
function partsHTML(d: HomeSession): string {
  const parts = d.parts || [];
  if (!parts.length) return '<div class="sc-uri plain">no members</div>';
  return '<div class="sc-parts">' + parts.map((p) => `<span class="sc-part" title="${esc(`${p.alias}: ${p.name || p.id}`)}">` +
    `<b>${esc(p.alias)}</b><span class="nm">${esc(p.name || p.id)}</span></span>`).join('') + '</div>';
}

function cardHTML(d: HomeSession): string {
  const st = d.stats || {};
  // label: [one, many] ("1 class", "16 classes")
  const stat = (v: number | null | undefined, [one, many]: [string, string]): string => (v == null ? '' : `<span><b class="num">${fmt(v)}</b>${v === 1 ? one : many}</span>`);
  const stats = d.kind === 'embeddings' ? stat(st.n_boxes, ['row', 'rows']) + stat(st.n_classes, ['class', 'classes'])
    : stat(st.n_images, ['image', 'images']) + stat(st.n_boxes, ['box', 'boxes']) + stat(st.n_classes, ['class', 'classes']);
  const classes = st.classes || [];
  const more = (st.n_classes || classes.length) - Math.min(MAX_CHIPS, classes.length);
  const chips = classes.slice(0, MAX_CHIPS).map((c, i) =>
    `<span class="sc-chip"><i style="background:${esc(classColor(i))}"></i>${esc(c)}</span>`).join('') +
    (more > 0 ? `<span class="sc-chip more">+${more}</span>` : '');
  const problem = d.error || (d.exists === false ? 'folder not found' : '');
  // a broken session: what is wrong on its (greyed) cover, one line in the body (why + Fix…, the full text as its
  // tooltip), so it is no taller than a healthy card and does not stretch its grid row
  const why = d.error || 'Moved, renamed or deleted';
  const tip = d.error ? `Cannot open: ${d.error}` : 'Folder not found: it was moved, renamed or deleted. Edit the session to point it at the new place, or remove it.';
  const alert = !problem ? '' : `<div class="sc-alert" role="note" title="${esc(tip)}"><span class="why">${esc(why)}</span>` +
    (can(d, 'owner') ? `<button class="sc-fix" data-act="edit" title="Edit the session: point it at the new place">Fix…</button>` : '') +
    `<span class="visually-hidden">${esc(tip)}</span></div>`;
  const when = openedText(d, d.created ? `created ${timeAgo(d.created)}` : '');
  // a labeling-backed session names its labeling session + task (the label:// uri means nothing to people)
  const lb = d.kind === 'labeling' ? d.labeling || {} : null;
  const where = lb ? (lb.project ? `${lb.project} › ${lb.task}${d.only_done ? ' · finished images only' : ''}` : 'labeling session') : d.uri || '';
  return `<article class="session-card${d.loaded ? ' active' : ''}${problem ? ' broken' : ''}${H.sel.includes(d.id) ? ' picked' : ''}" data-id="${esc(d.id)}" data-role="${roleOf(d)}">` +
    pickHTML(d) + coverHTML(d, problem ? (d.error ? 'Cannot open' : 'Folder not found') : '') +
    '<div class="sc-body">' +
    `<h3 class="sc-title"><button class="sc-open" data-act="open" title="Open ${esc(d.name)}">${esc(d.name)}</button></h3>` +
    tagsHTML(tagsOf(d)) +
    (d.kind === 'union' ? partsHTML(d) : `<div class="sc-uri${lb ? ' plain' : ''}" title="${esc(where)}"><bdi>${esc(where)}</bdi></div>`) + alert + modalityHTML(d) +
    (stats ? `<div class="sc-stats">${stats}</div>` : '<div class="sc-stats muted">Stats appear after the first open</div>') +
    (chips ? `<div class="sc-chips">${chips}</div>` : '') +
    '</div>' +
    '<footer class="sc-foot">' +
    peopleHTML(d, when) +
    (lb && lb.project ? `<button class="icon-btn ghost" data-act="tolabel" aria-label="Open the labeling session of ${esc(d.name)}" title="Open the labeling session ${esc(lb.project)}">${icon('tag')}</button>` : '') +
    shareBtn(d, 'viz') +
    (can(d, 'owner') ? editBtn('edit', d, 'Edit: name, source and tags') +
      `<button class="icon-btn ghost danger" data-act="remove" aria-label="Remove ${esc(d.name)}" title="Remove the session (no files are deleted)">${icon('trash')}</button>` : '') +
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
  // model predictions are a part of in_progress: drawn and counted apart from the work people did by hand
  const auto = Math.min(t.auto || 0, n.in_progress);
  const doing = n.in_progress - auto;
  const pct = (v: number): number => Math.round((100 * v) / sum);
  const tip = `${t.name} · ${taskInfo(t.task_type)?.label || t.task_type}: ` +
    STATUS_TEXT.map(([k, label]) => `${fmt(k === 'in_progress' ? doing : n[k])} ${label}`).join(' · ') +
    (auto ? ` · ${fmt(auto)} predicted by a model` : '');
  const w = (v: number): string => `${((100 * v) / sum).toFixed(2)}%`;
  const seg = (k: string, v: number): string => (v ? `<i class="${k}" style="width:${w(v)}"></i>` : '');
  const rv = review ? reviewState(t) : null;
  const rtip = rv ? `Review: ${fmt(rv.man)} by hand · ${fmt(rv.bulk)} by Review all · ${fmt(rv.left)} of ${fmt(rv.done)} done not reviewed` : '';
  return `<div class="lp-task" title="${esc(tip)}"><div class="lp-tname">${icon(taskInfo(t.task_type)?.icon || 'tag')}` +
    `<b>${esc(t.name)}</b><span class="lp-ncls">${fmt(t.n_classes || 0)} class${t.n_classes === 1 ? '' : 'es'}</span>` +
    `<span class="lp-pcts">${auto ? `<span class="lp-auto num" title="${fmt(auto)} images predicted by a model, not committed yet">${icon('robot')}${pct(auto)}%</span>` : ''}` +
    `<span class="lp-pct num" title="${fmt(n.done)} images done (committed)">${pct(n.done)}%</span></span></div>` +
    `<div class="lp-bar" role="img" aria-label="${esc(tip)}">${seg('done', n.done)}${seg('auto', auto)}${seg('in_progress', doing)}${seg('skipped', n.skipped)}</div>` +
    (rv ? `<div class="lp-bar lp-review" role="img" title="${esc(rtip)}" aria-label="${esc(rtip)}">${seg('manual', rv.man)}${seg('bulk', rv.bulk)}</div>` : '') + '</div>';
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
  const badge = `<span class="sc-src lbl">${icon('tag')}Labeling</span><span class="sc-live lp-src">${icon(src.ico)}${esc(src.label)}</span>${visBadge(p)}`;
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
  return `<article class="session-card label-card${H.flash === p.id ? ' flash' : ''}" data-pid="${esc(p.id)}" data-role="${roleOf(p)}">${mosaic}<div class="sc-body">` +
    `<h3 class="sc-title"><button class="sc-open" data-act="lopen" title="Open ${esc(p.name)}">${esc(p.name)}</button></h3>` +
    tagsHTML(tagsOf(p)) +
    (src.uri ? `<div class="sc-uri" title="${esc(src.uri)}"><bdi>${esc(src.uri)}</bdi></div>` : `<div class="sc-uri plain">${esc(src.plain ?? '')}</div>`) +
    `<div class="sc-stats"><span><b class="num">${fmt(p.n_media || 0)}</b>images</span><span><b class="num">${tasks.length}</b>task${tasks.length === 1 ? '' : 's'}</span></div>` +
    (tasks.length ? `<div class="lp-tasks">${tasks.slice(0, TASK_MAX).map((t) => taskHTML(t, p.n_media || 0, p.review)).join('')}` +
      (more > 0 ? `<span class="lp-more">+${more} more task${more === 1 ? '' : 's'}</span>` : '') + '</div>' : '') +
    checklistHTML(p) +
    '</div><footer class="sc-foot">' +
    peopleHTML(p, openedText(p, p.updated ? `updated ${timeAgo(p.updated)}` : '')) +
    (can(p, 'labeler') ? `<button class="icon-btn ghost" data-act="lexport" aria-label="Export ${esc(p.name)}" title="Export the labels">${icon('download')}</button>` : '') +
    shareBtn(p, 'label') +
    (can(p, 'owner') ? editBtn('ledit', p, 'Edit: name, tags, tasks, classes and export') +
      `<button class="icon-btn ghost danger" data-act="lremove" aria-label="Remove ${esc(p.name)}" title="Remove the labeling session">${icon('trash')}</button>` : '') +
    `<button class="icon-btn ghost lp-more" data-act="lmore" aria-haspopup="menu" aria-expanded="false" aria-label="More actions for ${esc(p.name)}" title="More: duplicate, manage classes, re-export"><span aria-hidden="true">⋯</span></button>` +
    '</footer><div class="sc-busy" aria-hidden="true"><span class="spin"></span>Opening…</div></article>';
}

// 0 sessions of both kinds (a fresh checkout): one welcome screen instead of two empty tabs
function emptyHTML(): string {
  const step = (n: number, title: string, text: string): string => `<li><span class="es-n num">${n}</span><div><b>${title}</b><span>${text}</span></div></li>`;
  return '<div class="image-empty">' +
    `<div class="es-mark"><svg class="brand-mark" viewBox="0 0 32 32" fill="none" stroke="currentColor" aria-hidden="true"><use href="${ICONS}#i-brand"/></svg></div>` +
    '<h2>No sessions yet</h2>' +
    '<p class="muted">Visualize a folder of images, with or without YOLO labels (on this machine or an S3 / RustFS prefix), or a folder of your own embeddings, or label a folder of images: boxes, polygons, keypoints or tags, one or more tasks at once.</p>' +
    `<div class="es-actions"><button class="primary big" data-act="new">${icon('plus')}Create session</button></div>` +
    '<ol class="es-steps">' +
    step(1, 'Pick the images', 'Local folders are read in place; S3 / FTP stream; dropped files are uploaded once.') +
    step(2, 'Explore or label', 'Every box becomes a dot to lasso, or draw boxes and polygons with the keyboard.') +
    step(3, 'Export', 'Labels go out per task in YOLO format, to a zip, a folder, S3, FTP or a database.') +
    '</ol></div>';
}

// the ones I opened last first (opened_at), then the most recently changed
function stamp(x: Item): number {
  if (x.p) return (x.p.updated || x.p.created || 0) * 1000;
  const st = x.d.stats || {};
  const t = st.last_opened ? Date.parse(String(st.last_opened).replace(' ', 'T')) : NaN;
  return Number.isFinite(t) ? t : (x.d.created || 0) * 1000;
}
const openedAt = (x: Item): number => (x.p ? x.p.opened_at : x.d.opened_at) || 0;

function sorted(list: Item[]): Item[] {
  for (const x of list) x.t = stamp(x);
  return list.sort((a, b) => openedAt(b) - openedAt(a) || b.t - a.t);
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
    tile: 'From a folder of a bucket: Visualize, Label, or both linked (the default)',
    empty: ['No visualize sessions yet', 'Explore a YOLO dataset as a map of its boxes: lasso groups, find outliers, suspicious labels and near-duplicates. A labeling session can be visualized too, live.'],
  },
  label: {
    err: 'lp', what: 'the labeling sessions', act: 'new-label', ico: 'tag',
    items: () => sorted(H.projects.map((p): Item => ({ pid: p.id, p, t: 0 }))),
    of: (x) => x.p ?? x.d,
    card: (x) => (x.p ? labelCardHTML(x.p) : ''),
    tile: 'From a folder of a bucket: Label, Visualize, or both linked (the default)',
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
  const scoped = H.scope !== 'all' ? SCOPES.find(([k]) => k === H.scope) : undefined;
  const empty = list.length || err ? ''
    : !total && scoped ? `<div class="tab-empty filtered">${icon(H.scope === 'global' ? 'globe' : 'users')}<div><b>Nothing in “${esc(scoped[1])}”</b>` +
      `<span class="muted">No ${t === 'viz' ? 'visualize' : 'label'} sessions here: ${esc(scoped[2].toLowerCase())}.</span></div>` +
      `<button data-act="scope-all">${icon('x')}Show all</button></div>`
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
  const bar = $req('image-tagbar');
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

// the tab counts for both tabs, the grid of the tab on screen (the hidden one is drawn when it is opened: a search
// keystroke builds one grid, not two) and the tag bar (not the tabs / search box: typing keeps its focus)
const stalePanels = new Set<TabId>();

function paintPanel(t: TabId, list: Item[], total: number): void {
  stalePanels.delete(t);
  $req(`image-panel-${t}`).classList.remove('is-empty');
  $req(`image-panel-${t}`).innerHTML = panelHTML(t, list, total);
}

function listOf(t: TabId): { list: Item[]; total: number } {
  const all = TAB[t].items();
  const list = filtering() ? all.filter((x) => { const o = TAB[t].of(x); return matches(o.name, tagsOf(o)); }) : all;
  return { list, total: all.length };
}

function renderPanels(): void {
  closeCardMenu(false);
  const on = filtering();
  for (const t of TABS) {
    const { list, total } = listOf(t);
    $req(`image-n-${t}`).textContent = H.errs[TAB[t].err] ? '' : on ? `${list.length}/${total}` : String(total);   // no count while its list failed to load
    if (t === (H.tab || 'viz')) paintPanel(t, list, total); else stalePanels.add(t);
  }
  renderTagbar();
  renderSelBar();
}

function readTab(): TabId | null {
  const t = pref(TAB_PREF);
  return isTab(t) ? t : null;
}

/** ?image=<tab> of the URL (Home's Visualize / Label buttons), else null. */
function urlTab(): TabId | null {
  const t = new URLSearchParams(location.search).get('image');
  return isTab(t) ? t : null;
}

// remember: a choice of the user (a click / arrow key), not the first render's pick
function setTab(t: TabId, focus: boolean, remember: boolean, bars = true): void {
  H.tab = t;
  if (stalePanels.has(t)) { const { list, total } = listOf(t); paintPanel(t, list, total); }
  if (remember) rememberPref(TAB_PREF, t);
  for (const k of TABS) {
    const on = k === t;
    const b = $req(`image-tab-${k}`);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
    $req(`image-panel-${k}`).classList.toggle('hidden', !on);
  }
  $req('image-new-text').textContent = NEW_TEXT[t];
  if (focus) $req(`image-tab-${t}`).focus();
  closeCardMenu(false);
  if (!bars) return;
  renderTagbar();
  renderSelBar();
}

function render(): void {
  const lists: Record<TabId, Item[]> = { viz: TAB.viz.items(), label: TAB.label.items() };
  const failed = !!(H.errs.ds || H.errs.lp);
  const none = !lists.viz.length && !lists.label.length && !failed && H.scope === 'all';
  $req('image-head').classList.toggle('hidden', none);
  $req('image-tabs').classList.toggle('hidden', none);
  $req('image-filter').classList.toggle('hidden', none);
  if (none) {
    $req('image-panel-viz').classList.remove('hidden');
    $req('image-panel-label').classList.add('hidden');
    $req('image-panel-viz').classList.add('is-empty');
    $req('image-panel-viz').innerHTML = emptyHTML();
    $req('image-panel-label').innerHTML = '';
    return;
  }
  if (!H.tab) {                                // the first paint: the URL's tab, else the remembered one, unless only the other kind has sessions
    const asked = urlTab();
    const want = asked || readTab() || 'viz';
    const other = want === 'viz' ? 'label' : 'viz';
    H.tab = !asked && !lists[want].length && lists[other].length ? other : want;
  }
  renderPanels();
  setTab(H.tab, false, false, false);            // the tag bar and selection bar were just drawn by renderPanels
}

const scopeQ = (): string => `?scope=${encodeURIComponent(H.scope)}`;

async function fetchSessions(): Promise<void> {
  try {
    const { datasets } = await getJSON<{ datasets: HomeSession[] }>(`api/datasets${scopeQ()}`);
    H.sessions = datasets;
    H.sel = H.sel.filter((id) => !!pickable(id));          // gone, or not visible in this scope any more
    H.errs.ds = null;
  } catch (e) { H.errs.ds = errText(e); }
}

async function fetchProjects(): Promise<void> {
  try {
    const { projects } = await getJSON<{ projects: HomeProject[] }>(`api/label/projects${scopeQ()}`);
    H.projects = projects;
    H.errs.lp = null;
  } catch (e) { H.errs.lp = errText(e); }
}

// a newer load (scope switched meanwhile) wins: an older answer is not painted over it
async function load(): Promise<void> {
  const seq = ++H.loadSeq;
  await Promise.all([fetchSessions(), fetchProjects()]);
  if (seq === H.loadSeq) render();
}

// ---- the scope filter (#image-scope, a radiogroup in #image-filter)
function renderScope(): void {
  const box = $req('image-scope');
  box.innerHTML = SCOPES.filter(([k]) => k !== 'everyone' || isAdmin()).map(([k, label, tip]) =>
    `<button type="button" role="radio" data-scope="${k}" aria-checked="${H.scope === k}" tabindex="${H.scope === k ? 0 : -1}" ` +
    `class="${H.scope === k ? 'view-btn active' : 'view-btn'}" title="${esc(tip)}">${esc(label)}</button>`).join('');
}

function setScope(s: HomeScope, focus: boolean): void {
  if (s === H.scope) return;
  H.scope = s;
  rememberPref(SCOPE_PREF, s);
  renderScope();
  if (focus) $req('image-scope').querySelector<HTMLElement>(`[data-scope="${s}"]`)?.focus();
  void load();
}

function open(sel: string, url: string): void {
  if (H.opening) return;                       // a double click opens once
  H.opening = true;
  const card = document.querySelector(sel);
  if (card) card.classList.add('opening');
  document.body.classList.add('image-opening');
  location.href = url;
}

const openSession = (id: string): void => open(openSel(id), vizUrl(id));
const openProject = (pid: string): void => open(`.session-card[data-pid="${CSS.escape(pid)}"]`, labelUrl(pid));

const openSel = (id: string): string => `.session-card[data-id="${CSS.escape(id)}"]`;

// ---- Compare / Combine: the picked cards (H.sel) and the bar under the grid (#image-selbar)
const pickable = (id: string): HomeSession | undefined => H.sessions.find((d) => d.id === id && !unionBlock(d));

function setPicked(id: string, on: boolean): void {
  const d = H.sessions.find((x) => x.id === id);
  if (!d) return;
  const why = unionBlock(d);
  if (on && why) { toast(why); on = false; }
  H.sel = H.sel.filter((x) => x !== id);
  if (on) H.sel.push(id);
  H.selErr = '';
  const card = document.querySelector(openSel(id));           // in place: no re-render, the focus stays
  card?.classList.toggle('picked', on);
  const cb = card?.querySelector<HTMLInputElement>('[data-act="pick"]');
  if (cb) cb.checked = on;
  renderSelBar();
}

function clearPicks(): void {
  for (const id of [...H.sel]) setPicked(id, false);
}

function renderSelBar(): void {
  const bar = $('image-selbar');
  if (!bar) return;
  const show = H.tab === 'viz' && H.sel.length > 0;
  bar.classList.toggle('hidden', !show);
  $('image-panel-viz')?.classList.toggle('selecting', H.sel.length > 0);
  if (!show) { bar.innerHTML = ''; return; }
  const n = H.sel.length;
  const items = H.sel.map((id) => H.sessions.find((d) => d.id === id)).filter((d): d is HomeSession => !!d);
  const busy = H.selBusy;
  const cmpTip = n === 2 ? 'Open the two side by side: what was added, removed, moved or relabelled' : 'Compare needs exactly 2 sessions; Combine joins more';
  const cmbTip = n < 2 ? 'Select at least 2 sessions' : n > UNION_MAX ? `At most ${UNION_MAX} sessions can be combined` : `Explore the ${n} sessions as one map (in this order: the first one wins)`;
  bar.innerHTML = `<span class="hs-n"><b class="num">${n}</b> selected</span>` +
    '<span class="hs-list">' + items.map((d, i) => `<span class="hs-item"><b>${esc(String.fromCharCode(65 + i))}</b><span class="nm" title="${esc(d.name)}">${esc(d.name)}</span>` +
      `<button class="icon-btn ghost" data-act="unpick" data-id="${esc(d.id)}" aria-label="Unselect ${esc(d.name)}" title="Unselect">${icon('x')}</button></span>`).join('') + '</span>' +
    (H.selErr ? `<span class="status-line err hs-err" role="alert">${icon('alert')}<span>${esc(H.selErr)}</span></span>` : '') +
    '<span class="hs-acts">' +
    `<button class="primary" data-act="sel-compare" title="${esc(cmpTip)}" ${n === 2 && !busy ? '' : 'disabled'}>${busy === 'compare' ? '<span class="spin"></span>' : icon('layers')}Compare</button>` +
    `<button data-act="sel-combine" title="${esc(cmbTip)}" ${n >= 2 && n <= UNION_MAX && !busy ? '' : 'disabled'}>${busy === 'combine' ? '<span class="spin"></span>' : icon('plus')}Combine (${n})</button>` +
    `<button class="ghost" data-act="sel-clear" title="Unselect all (Esc)" ${busy ? 'disabled' : ''}>Clear</button></span>`;
}

// POST the combined dataset (the one made before over the same members, if any), then open its Compare tab
async function combine(mode: 'compare' | 'combine'): Promise<void> {
  const items = H.sel.map(pickable).filter((d): d is HomeSession => !!d);
  if (H.selBusy || items.length < 2 || items.length > UNION_MAX || (mode === 'compare' && items.length !== 2)) return;
  const names = items.map((d) => d.name);
  H.selBusy = mode;
  H.selErr = '';
  renderSelBar();
  try {
    const u = await postJSON<{ id: string }>('api/datasets', { kind: 'union', reuse: true, name: names.join(mode === 'compare' ? ' vs ' : ' + '),
      parts: items.map((d) => ({ id: d.id })) });
    H.opening = true;
    document.body.classList.add('image-opening');
    location.href = `${vizUrl(u.id)}#compare`;
  } catch (e) {
    H.selBusy = '';
    H.selErr = errText(e);
    renderSelBar();
  }
}

// no dataset file is touched by a removal, so no question first: it goes at once with Undo (core/defer-delete.ts)
function remove(id: string): void {
  const d = H.sessions.find((x) => x.id === id);
  if (!d) return;
  deferDelete({
    title: `Removed the session “${d.name}”`, url: `api/datasets/${encodeURIComponent(id)}`,
    hide: () => { H.sessions = H.sessions.filter((x) => x.id !== id); render(); },
    restore: load, after: load,
  });
}

// ---- a removal question on the shared dialog (views/dialog.ts): resolves {checked} on OK, null when dismissed
interface ConfirmOpts { title: string; text: string; check?: string; ok: string }
async function confirmDlg({ title, text, check, ok }: ConfirmOpts): Promise<{ checked: boolean } | null> {
  let checked = false;
  const done = await openDialog({
    title, ok, okClass: 'danger-btn', focus: '.form-foot [data-dlg="cancel"]',
    body: `<div class="ui-dlg-text">${paras(text)}</div>` +
      (check ? `<label class="check"><input type="checkbox" id="image-dlg-cb"> <span>${esc(check)}</span></label>` : ''),
    onOk: (r) => { checked = !!r.querySelector<HTMLInputElement>('#image-dlg-cb')?.checked; return null; },
  });
  return done ? { checked } : null;
}

async function removeProject(pid: string): Promise<void> {
  const p = H.projects.find((x) => x.id === pid);
  if (!p) return;
  const r = await confirmDlg({ title: `Remove “${p.name}”?`, ok: 'Remove',
    text: 'The labeling session leaves this list. Without the box below, its uploaded images and annotations stay on disk (the workspace labeling/ folder); source folders and buckets are never touched.',
    check: 'Also delete the uploaded images and every annotation (cannot be undone)' });
  if (!r) return;
  deferDelete({
    title: `Removed “${p.name}”${r.checked ? ' and its files' : ''}`, url: `api/label/projects/${encodeURIComponent(pid)}?delete_files=${r.checked ? 1 : 0}`,
    hide: () => { H.projects = H.projects.filter((x) => x.id !== pid); render(); },
    restore: load, after: load,
  });
}

// the label session names the wizard checks a new name against (minus the one being edited)
const labelNames = (pid: string | null): string[] => H.projects.filter((p) => p.id !== pid).map((p) => p.name);
const wizard = (opts: WizardOpts): Promise<void> => import('./label-new.js').then((m) => m.openWizard(opts))
  .catch((e: unknown) => { $req('image-status').innerHTML = `<div class="status-line err">${icon('alert')}Could not open the wizard: ${esc(errText(e))}</div>`; });

function exportProject(pid: string): void {
  import('../label/export.js').then((m) => m.openExportDialog(pid))
    .catch((e: unknown) => { $req('image-status').innerHTML = `<div class="status-line err">${icon('alert')}Could not open the export dialog: ${esc(errText(e))}</div>`; });
}

// ---- a labeling card's ⋯ menu: duplicate, manage classes, re-export (a popover built on demand)
const actions = () => import('../label/session-actions.js');
const failLine = (what: string, e: unknown, verb = 'Could not open'): void => { $req('image-status').innerHTML = `<div class="status-line err">${icon('alert')}${esc(verb)} ${esc(what)}: ${esc(errText(e))}</div>`; };

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
  let m = $('image-cardmenu');
  if (!m) {
    const menu = m = document.createElement('div');
    menu.id = 'image-cardmenu';
    menu.className = 'lb-menu panel image-cardmenu';
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
  const lab = can(p, 'labeler'), own = can(p, 'owner');
  menuEl.innerHTML = (boxTasks ? `<button role="menuitem" data-m="viz" title="Explore the boxes of a task as a map: a visualize session reading this labeling session live">${icon('scatter')}Visualize a task…</button>` : '') +
    (own ? `<button role="menuitem" data-m="share">${icon('share')}Share…</button>` : '') +
    (lab ? `<button role="menuitem" data-m="dup">${icon('layers')}Duplicate…</button>` : '') +
    (own ? `<button role="menuitem" data-m="classes">${icon('tag')}Manage classes…</button>` : '') +
    (lab ? `<button role="menuitem" data-m="import">${icon('upload')}Import annotations…</button>` : '') +
    (lab && p.review ? `<button role="menuitem" data-m="review" title="Mark every done, not yet reviewed image as reviewed (shown apart from images you review one by one)">${icon('check')}Review all…</button>` : '') +
    (own && p.export_default !== undefined && lastTargetText(p) ? reItem(lastTargetText(p)) : '');
  if (!menuEl.querySelector('[role=menuitem]')) menuEl.innerHTML = '<div class="lb-menu-head muted">View only: nothing more to do here</div>';
  if (p.export_default === undefined && own) {            // the list rows carry no export_default: read it once
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

// ---- popovers (the ⋯ menu): shown under their button, right-aligned, flipped up when no room
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
  const m = $('image-cardmenu');
  if (!m || m.classList.contains('hidden')) return;
  const from = H.menuFrom;
  H.menuFrom = null;
  hidePopover(m, from, refocus);
}

function toggleTagFilter(b: HTMLElement): void {
  const k = fold(b.dataset['tag']);
  if (H.tagSel.has(k)) H.tagSel.delete(k); else H.tagSel.add(k);
  // the clicked button is rebuilt: put the focus back on its twin (same card or the tag bar)
  const card = b.closest<HTMLElement>('.session-card');
  const sel = card ? (card.dataset['pid'] ? `.session-card[data-pid="${CSS.escape(card.dataset['pid'])}"]` : `.session-card[data-id="${CSS.escape(card.dataset['id'] ?? '')}"]`) : '#image-tagbar';
  renderPanels();
  const scope = document.querySelector(sel);
  const twin = scope && [...scope.querySelectorAll<HTMLElement>('[data-act="tagfilter"]')].find((x) => fold(x.dataset['tag']) === k);
  (twin || $req('image-search')).focus();
}

function clearFilters(keepQuery: boolean): void {
  if (!keepQuery) { H.q = ''; $req<HTMLInputElement>('image-search').value = ''; }
  H.tagSel.clear();
  renderPanels();
  $req('image-search').focus();
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

// the share dialog (views/share.ts, loaded on demand); both kinds of cards: the ids are session ids
const share = (id: string, name: string): Promise<void> => import('./share.js').then((m) => m.openShare({ id, name }))
  .catch((e: unknown) => failLine('the share dialog', e));

const CARD_MENU: Record<string, (pid: string) => unknown> = {
  viz: visualizeTask,
  share: (pid) => share(pid, H.projects.find((x) => x.id === pid)?.name ?? ''),
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
const editProject = (pid: string): void => { void wizard({ edit: pid, names: labelNames(pid), tagSuggest: H.projects.flatMap(tagsOf) }); };

/** Create session: Visualize, Label or both (linked) from a folder of a bucket; or why the user's tier cannot. */
function createSession(): void {
  if (!tierCan('dataset.create') && !tierCan('label.create')) { toast(cannot('create sessions'), 'err'); return; }
  void import('./session-new.js').then((m) => m.openCreateSession())
    .catch((e: unknown) => toast(`Could not open Create session: ${errText(e)}`, 'err'));
}

const ACTIONS: Record<string, (b: HTMLElement) => unknown> = {
  open: (b) => openSession(dataOf(b, 'id')),
  edit: (b) => editSession(dataOf(b, 'id')),
  remove: (b) => remove(dataOf(b, 'id')),
  lopen: (b) => openProject(dataOf(b, 'pid')),
  lexport: (b) => exportProject(dataOf(b, 'pid')),
  ledit: (b) => editProject(dataOf(b, 'pid')),
  tagfilter: (b) => toggleTagFilter(b),
  tagclear: () => clearFilters(true),
  'clear-filter': () => clearFilters(false),
  lremove: (b) => removeProject(dataOf(b, 'pid')),
  lmore: (b) => { if (H.menuFrom === b) closeCardMenu(true); else openCardMenu(b); },
  tolabel: (b) => {
    const d = H.sessions.find((x) => x.id === dataOf(b, 'id'));
    if (d && d.project_id) open(`.session-card[data-id="${CSS.escape(d.id)}"]`, labelUrl(d.project_id));
  },
  // one Create session for both kinds, from a bucket folder (views/session-new.ts); the tab it was opened on is the
  // side that is surely ticked, the folder's format decides the other
  new: () => createSession(),
  'new-viz': () => createSession(),
  'new-label': () => createSession(),
  reload: () => load(),
  share: (b) => { const id = dataOf(b, 'id'); return share(id, H.sessions.find((x) => x.id === id)?.name ?? ''); },
  lshare: (b) => { const pid = dataOf(b, 'pid'); return share(pid, H.projects.find((x) => x.id === pid)?.name ?? ''); },
  'scope-all': () => setScope('all', false),
  pick: (b) => setPicked(dataOf(b, 'id'), b instanceof HTMLInputElement && b.checked),
  unpick: (b) => { setPicked(dataOf(b, 'id'), false); $('image-selbar')?.querySelector<HTMLElement>('[data-act="unpick"], [data-act="sel-clear"]')?.focus(); },
  'sel-compare': () => combine('compare'),
  'sel-combine': () => combine('combine'),
  'sel-clear': () => {
    const first = H.sel[0];
    const back = first ? document.querySelector<HTMLElement>(`${openSel(first)} [data-act="pick"]`) : null;
    clearPicks();
    back?.focus();                                 // the bar is gone: back to the first picked card
  },
};

function bind(): void {
  if (H.bound) return;
  H.bound = true;
  const onClick = (e: MouseEvent): void => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    const b = t.closest<HTMLElement>('[data-act]');
    // Ctrl / Cmd + click on a Visualize card (or its title) picks it instead of opening it
    const vcard = t.closest<HTMLElement>('.session-card[data-id]');
    if ((e.ctrlKey || e.metaKey) && vcard && (!b || b.dataset['act'] === 'open')) {
      e.preventDefault();
      const id = vcard.dataset['id'] ?? '';
      setPicked(id, !H.sel.includes(id));
      return;
    }
    const act = b ? ACTIONS[b.dataset['act'] ?? ''] : undefined;
    if (b && !(b instanceof HTMLButtonElement && b.disabled) && act) { act(b); return; }
    const tab = t.closest<HTMLElement>('[role=tab]');
    const tabId = tab?.dataset['tab'];
    if (tab && isTab(tabId)) { setTab(tabId, false, true); return; }
    // anywhere else on a card opens it (the title button is the keyboard / screen-reader way in)
    const card = t.closest<HTMLElement>('.session-card');
    if (!card || t.closest('button, a, label, input')) return;
    if (card.dataset['pid']) openProject(card.dataset['pid']);
    else openSession(card.dataset['id'] ?? '');
  };
  const body = $req('image-body');
  const selbar = document.createElement('div');           // the Compare / Combine bar: sticks to the bottom of the scroll
  selbar.id = 'image-selbar';
  selbar.className = 'image-selbar hidden';
  selbar.setAttribute('role', 'region');
  selbar.setAttribute('aria-label', 'Selected sessions');
  body.appendChild(selbar);
  body.addEventListener('click', onClick);
  body.addEventListener('keydown', (e) => {               // Esc unselects (after the search box / popovers had theirs)
    if (e.key !== 'Escape' || e.defaultPrevented || !H.sel.length || H.selBusy) return;
    e.preventDefault();
    clearPicks();
  });
  $req('image-status').addEventListener('click', onClick);
  $req('image-new').addEventListener('click', onClick);
  // the scope radiogroup: click, or arrows / Home / End (roving tabindex, the choice follows the focus)
  const scope = $req('image-scope');
  scope.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-scope]') : null;
    if (b && isScope(b.dataset['scope'])) setScope(b.dataset['scope'], false);
  });
  scope.addEventListener('keydown', (e) => {
    const keys = [...scope.querySelectorAll<HTMLElement>('[data-scope]')].map((b) => b.dataset['scope']).filter(isScope);
    const i = keys.indexOf(H.scope);
    const moves: Record<string, number> = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: keys.length - 1 };
    const to = moves[e.key];
    if (to === undefined || !keys.length) return;
    e.preventDefault();
    const next = keys[(to + keys.length) % keys.length];
    if (next) setScope(next, true);
  });
  // the tablist: arrows / Home / End move between the two tabs and show them (automatic activation)
  $req('image-tabs').addEventListener('keydown', (e) => {
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
  $req('image-theme').onclick = () => { setTheme(THEME.name === 'dark' ? 'light' : 'dark'); render(); };
  document.addEventListener('mousedown', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    if (H.menuFrom && !t.closest('#image-cardmenu') && !H.menuFrom.contains(t)) closeCardMenu(false);
  });
  const follow = (): void => closeCardMenu(false);
  window.addEventListener('resize', follow);
  body.closest('.image-scroll')?.addEventListener('scroll', follow, { passive: true });
  // the name search: one re-render per frame, the box keeps its focus; Esc clears it
  const search = $req<HTMLInputElement>('image-search');
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
    H.selBusy = '';
    document.body.classList.remove('image-opening');
    void load();
  });
}

export async function renderImage(): Promise<void> {
  document.title = 'Image · DataViz';
  const saved = pref(SCOPE_PREF);
  if (isScope(saved)) H.scope = saved;
  renderScope();
  bind();
  // class colours for the chips: the same palettes as the plot (config.yaml ui.palette / palette_light)
  const status = getJSON<Status>('api/status').then((st) => {
    H.palette = strList(st.ui && st.ui['palette']);
    H.paletteLight = strList(st.ui && st.ui['palette_light']);
  }).catch(() => {});
  await Promise.all([fetchSessions(), fetchProjects(), status]);
  render();                                    // one paint, with the chips already coloured
  const q = new URLSearchParams(location.search);
  if (q.get('new') === '1') {                  // Home's Visualize / Label: the New dialog of that tab, once
    q.delete('new');
    history.replaceState(history.state, '', `${location.pathname}?${q.toString().replace(/=(?=&|$)/g, '')}${location.hash}`);
    const tab = urlTab();
    void ACTIONS[tab === 'label' ? 'new-label' : tab === 'viz' ? 'new-viz' : 'new']?.(document.body);
  }
}
