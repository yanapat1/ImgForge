// The share dialog of a session (both kinds: the ids are session ids), loaded on demand by the home: Private /
// Global (a segmented radiogroup with one line each), the owner, the members with a Viewer / Labeler select and a
// remove button, and a people search (GET api/users/search?q=, debounced 200 ms, from 2 characters, ↑ ↓ Enter Esc,
// the owner and current members left out). Save sends the whole list (PUT api/sessions/<id>/sharing), then the home
// reloads (`sessions-changed`). Nothing is sent before Save; errors show inline.

import { errText, getJSON, sendJSON } from '../core/api.js';
import { avatarHTML, userName } from '../core/avatar.js';
import type { AvatarUser } from '../core/avatar.js';
import { ME } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import type { SessionMember, SessionPerson, Visibility } from '../types-home.js';
import { openDialog } from './dialog.js';

interface Sharing { visibility: Visibility; owner: SessionPerson | null; members: SessionMember[] }
interface ShareState extends Sharing { results: SessionPerson[]; active: number; q: string; seq: number; timer: number; searching: boolean; searchErr: string }

const VIS: ReadonlyArray<[Visibility, string, string, string]> = [
  ['private', 'Private', 'lock', 'Only you and the people below'],
  ['global', 'Global', 'globe', 'Every user can view; people below can also label if Labeler'],
];

const person = (u: SessionPerson): AvatarUser => ({ id: u.id, name: u.name ?? null, username: u.username ?? null, avatar_ver: u.avatar_ver ?? 0 });

function visHTML(s: ShareState): string {
  return '<div class="segmented share-vis" role="radiogroup" aria-label="Who can open it">' + VIS.map(([v, label, ico]) =>
    `<button type="button" role="radio" data-vis="${v}" aria-checked="${s.visibility === v}" tabindex="${s.visibility === v ? 0 : -1}" ` +
    `class="view-btn${s.visibility === v ? ' active' : ''}">${icon(ico)}${label}</button>`).join('') + '</div>' +
    `<p class="share-vis-note muted" id="share-vis-note">${esc(VIS.find(([v]) => v === s.visibility)?.[3] ?? '')}</p>`;
}

function membersHTML(s: ShareState): string {
  const owner = s.owner ? `<li class="share-row owner">${avatarHTML(person(s.owner), 28, { title: false })}<span class="share-who"><b>${esc(userName(person(s.owner)))}</b>` +
    `<span class="muted">@${esc(s.owner.username ?? '')}${ME && s.owner.id === ME.id ? ' · you' : ''}</span></span><span class="share-role-fixed">Owner</span></li>` : '';
  const rows = s.members.map((m, i) => `<li class="share-row" data-i="${i}">${avatarHTML(person(m), 28, { title: false })}` +
    `<span class="share-who"><b>${esc(userName(person(m)))}</b><span class="muted">@${esc(m.username ?? '')}</span></span>` +
    `<select data-role-i="${i}" aria-label="${esc(`Role of ${userName(person(m))}`)}"><option value="viewer"${m.role === 'viewer' ? ' selected' : ''}>Viewer</option>` +
    `<option value="labeler"${m.role === 'labeler' ? ' selected' : ''}>Labeler</option></select>` +
    `<button type="button" class="icon-btn ghost danger" data-rm="${i}" aria-label="${esc(`Remove ${userName(person(m))}`)}" title="Remove">${icon('x')}</button></li>`).join('');
  return `<ul class="share-list" aria-label="People with access">${owner}${rows}</ul>` +
    (s.members.length ? '' : '<p class="hint share-empty">Nobody else yet: find people below.</p>');
}

function resultsHTML(s: ShareState): string {
  if (s.searchErr) return `<li class="share-res-note err" role="presentation">${esc(s.searchErr)}</li>`;
  if (s.q.length < 2) return '';
  if (s.searching && !s.results.length) return '<li class="share-res-note" role="presentation">Searching…</li>';
  if (!s.results.length) return '<li class="share-res-note" role="presentation">Nobody found</li>';
  return s.results.map((u, i) => `<li role="option" id="share-opt-${i}" data-pick="${i}" aria-selected="${i === s.active}" class="${i === s.active ? 'on' : ''}">` +
    `${avatarHTML(person(u), 24, { title: false })}<b>${esc(userName(person(u)))}</b><span class="muted">@${esc(u.username ?? '')}</span></li>`).join('');
}

/** Open the dialog for session `id` (its name in the title). */
export async function openShare({ id, name }: { id: string; name: string }): Promise<void> {
  const enc = encodeURIComponent(id);
  let s: ShareState;
  try {
    const r = await getJSON<Sharing>(`api/sessions/${enc}/sharing`);
    s = { visibility: r.visibility === 'global' ? 'global' : 'private', owner: r.owner, members: (r.members || []).map((m) => ({ ...m, role: m.role === 'labeler' ? 'labeler' : 'viewer' })),
      results: [], active: -1, q: '', seq: 0, timer: 0, searching: false, searchErr: '' };
  } catch (e) {
    await openDialog({ title: `Share “${name}”`, ok: 'Close', body: `<div class="status-line err">${icon('alert')}<span>${esc(errText(e))}</span></div>`, onOk: () => null });
    return;
  }
  const saved = await openDialog({
    title: `Share “${name}”`, ok: 'Save', wide: true, focus: `[data-vis="${s.visibility}"]`,
    body: `<div class="share">${'<div id="share-vis">' + visHTML(s) + '</div>'}` +
      '<h4 class="share-h">People</h4><div id="share-members">' + membersHTML(s) + '</div>' +
      '<div class="share-search"><label class="fld" for="share-q">Add people<span class="share-q-wrap">' + icon('search') +
      '<input type="text" id="share-q" data-noenter="1" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="share-results" ' +
      'placeholder="Name or username" autocomplete="off" spellcheck="false"></span></label>' +
      '<ul id="share-results" class="share-results panel hidden" role="listbox" aria-label="People found"></ul></div></div>',
    bind: (root) => bindShare(root, s),
    onOk: async () => {
      try {
        await sendJSON('PUT', `api/sessions/${enc}/sharing`, { visibility: s.visibility, members: s.members.map((m) => ({ user_id: m.id, role: m.role })) });
      } catch (e) { return errText(e); }
      return null;
    },
  });
  if (saved) document.dispatchEvent(new CustomEvent('sessions-changed'));
}

function bindShare(root: HTMLElement, s: ShareState): void {
  const q = root.querySelector<HTMLInputElement>('#share-q');
  const list = root.querySelector<HTMLElement>('#share-results');
  const visBox = root.querySelector<HTMLElement>('#share-vis');
  const memBox = root.querySelector<HTMLElement>('#share-members');
  if (!q || !list || !visBox || !memBox) return;
  const paintVis = (focus: boolean): void => {
    visBox.innerHTML = visHTML(s);
    if (focus) visBox.querySelector<HTMLElement>(`[data-vis="${s.visibility}"]`)?.focus();
  };
  const paintMembers = (): void => { memBox.innerHTML = membersHTML(s); };
  const paintResults = (): void => {
    const open = s.q.length >= 2;
    list.innerHTML = resultsHTML(s);
    list.classList.toggle('hidden', !open);
    q.setAttribute('aria-expanded', String(open && s.results.length > 0));
    if (s.active >= 0 && s.results[s.active]) q.setAttribute('aria-activedescendant', `share-opt-${s.active}`);
    else q.removeAttribute('aria-activedescendant');
    list.querySelector<HTMLElement>('li.on')?.scrollIntoView({ block: 'nearest' });
  };
  const exclude = (): Set<string> => new Set([...(s.owner ? [s.owner.id] : []), ...s.members.map((m) => m.id)]);
  const search = async (): Promise<void> => {
    const seq = ++s.seq;
    s.searching = true;
    s.searchErr = '';
    paintResults();
    try {
      const r = await getJSON<{ users: SessionPerson[] }>(`api/users/search?q=${encodeURIComponent(s.q)}`);
      if (seq !== s.seq) return;
      const skip = exclude();
      s.results = (r.users || []).filter((u) => !skip.has(u.id));
      s.active = s.results.length ? 0 : -1;
    } catch (e) {
      if (seq !== s.seq) return;
      s.results = [];
      s.searchErr = errText(e);
    }
    s.searching = false;
    paintResults();
  };
  const pick = (i: number): void => {
    const u = s.results[i];
    if (!u) return;
    s.members.push({ ...u, role: 'viewer' });
    s.q = '';
    s.results = [];
    s.active = -1;
    q.value = '';
    paintMembers();
    paintResults();
    q.focus();
  };
  q.addEventListener('input', () => {
    s.q = q.value.trim();
    clearTimeout(s.timer);
    if (s.q.length < 2) { s.seq++; s.results = []; s.active = -1; s.searchErr = ''; paintResults(); return; }
    s.timer = setTimeout(() => { void search(); }, 200);
  });
  q.addEventListener('keydown', (e) => {
    const n = s.results.length;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!n) return;
      e.preventDefault();
      s.active = (s.active + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
      paintResults();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (s.active >= 0) pick(s.active);
    } else if (e.key === 'Escape' && (s.q || !list.classList.contains('hidden'))) {
      e.preventDefault();
      e.stopPropagation();                         // closes the list, not the dialog
      s.q = ''; q.value = ''; s.results = []; s.active = -1; s.seq++;
      paintResults();
    }
  });
  q.addEventListener('blur', () => { setTimeout(() => { if (!root.contains(document.activeElement) || document.activeElement !== q) list.classList.add('hidden'); }, 150); });
  q.addEventListener('focus', () => { if (s.q.length >= 2) paintResults(); });
  list.addEventListener('mousedown', (e) => {
    const li = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-pick]') : null;
    if (!li) return;
    e.preventDefault();                            // keep the focus in the box
    pick(Number(li.dataset['pick']));
  });
  visBox.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-vis]') : null;
    const v = b?.dataset['vis'];
    if (v === 'private' || v === 'global') { s.visibility = v; paintVis(true); }
  });
  visBox.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault();
    s.visibility = e.key === 'Home' ? 'private' : e.key === 'End' ? 'global' : s.visibility === 'private' ? 'global' : 'private';
    paintVis(true);
  });
  memBox.addEventListener('change', (e) => {
    const sel = e.target instanceof HTMLSelectElement ? e.target : null;
    const m = sel ? s.members[Number(sel.dataset['roleI'])] : undefined;
    if (sel && m) m.role = sel.value === 'labeler' ? 'labeler' : 'viewer';
  });
  memBox.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-rm]') : null;
    if (!b) return;
    const i = Number(b.dataset['rm']);
    s.members.splice(i, 1);
    paintMembers();
    (memBox.querySelector<HTMLElement>(`[data-rm="${Math.min(i, s.members.length - 1)}"]`) ?? q).focus();
  });
}
