// Settings › Users & admin (admins only; the nav button is hidden for everyone else and the server answers 403):
// the server settings (sign-up on / off, who can add connections: PUT api/admin/settings), the user table
// (GET api/admin/users: admin and active switches through PATCH api/admin/users/{id}, reset password, delete with
// their sessions moved to another user or deleted), a create-user form (POST api/admin/users) and the recent audit
// log (GET api/admin/audit?limit=100), and the tiers (GET / POST / PATCH / DELETE api/admin/tiers: named permission
// sets, every user in one, the default one for sign-ups). Loaded when the section opens; every change re-reads the list.

import { errText, getJSON, postJSON, sendJSON } from '../core/api.js';
import { avatarHTML } from '../core/avatar.js';
import { ME } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import { openDialog } from '../views/dialog.js';
import type { AdminSettings, AdminTier, AdminUser, AuditEvent, SetAction, SetChange, StatusLine, TierPerm } from '../types-settings.js';
import { renderSettings } from './dialog.js';
import { dd, rowAttr, statusHTML } from './forms.js';

const USERNAME = /^[a-z0-9._-]{3,32}$/;
const PW_MIN = 8;

interface Draft { username: string; display_name: string; email: string; password: string; is_admin: boolean; tier_id: string }
interface UsersState {
  users: AdminUser[] | null; settings: AdminSettings | null; audit: AuditEvent[] | null; tiers: AdminTier[] | null; perms: TierPerm[];
  loading: boolean; error: string | null; status: StatusLine | null; setStatus: StatusLine | null; tierStatus: StatusLine | null;
  createOpen: boolean; draft: Draft; createStatus: StatusLine | null;
}
const EMPTY_DRAFT: Draft = { username: '', display_name: '', email: '', password: '', is_admin: false, tier_id: '' };
const US: UsersState = { users: null, settings: null, audit: null, tiers: null, perms: [], loading: false, error: null, status: null,
  setStatus: null, tierStatus: null, createOpen: false, draft: { ...EMPTY_DRAFT }, createStatus: null };

/** Read users, settings and the audit log (each failing alone shows its own line). */
export async function loadUsersData(): Promise<void> {
  US.loading = true;
  const [u, s, a, t] = await Promise.allSettled([
    getJSON<{ users: AdminUser[] }>('api/admin/users'),
    getJSON<AdminSettings>('api/admin/settings'),
    getJSON<{ events: AuditEvent[] }>('api/admin/audit?limit=100'),
    getJSON<{ tiers: AdminTier[]; permissions: TierPerm[] }>('api/admin/tiers'),
  ]);
  US.loading = false;
  US.users = u.status === 'fulfilled' ? u.value.users : US.users;
  US.settings = s.status === 'fulfilled' ? s.value : US.settings;
  US.audit = a.status === 'fulfilled' ? a.value.events : US.audit;
  if (t.status === 'fulfilled') { US.tiers = t.value.tiers; US.perms = t.value.permissions; }
  US.error = u.status === 'rejected' ? errText(u.reason) : null;
}

function when(t: number | string | null | undefined): string {
  if (t == null || t === '') return '—';
  const ms = typeof t === 'number' ? t * 1000 : Date.parse(String(t));
  if (!Number.isFinite(ms)) return String(t);
  const d = new Date(ms);
  const s = (Date.now() - ms) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}
const fullTime = (t: number | string | null | undefined): string => {
  if (t == null || t === '') return '';
  const ms = typeof t === 'number' ? t * 1000 : Date.parse(String(t));
  return Number.isFinite(ms) ? new Date(ms).toLocaleString() : String(t);
};

function settingsHTML(): string {
  const st = US.settings;
  if (!st) return '';
  return '<div class="form-card"><div class="step-head">' + icon('shield') + 'Server</div>' +
    `<label class="check"><input type="checkbox" data-act-change="adm-signup" ${st.signup_enabled ? 'checked' : ''}> ` +
    '<span><b>Anyone can create an account</b><br><span class="muted">Off: only administrators add users (below).</span></span></label>' +
    '<label class="fld adm-conn">Who can add connections (S3, FTP, databases, model servers)' +
    `<select data-act-change="adm-conns"><option value="everyone"${st.who_can_add_connections === 'everyone' ? ' selected' : ''}>Everyone</option>` +
    `<option value="admins"${st.who_can_add_connections === 'admins' ? ' selected' : ''}>Administrators only</option></select></label>` +
    statusHTML(US.setStatus) + '</div>';
}

const defaultTier = (): AdminTier | undefined => (US.tiers ?? []).find((t) => t.is_default);
const permLabel = (k: string): string => US.perms.find((p) => p.key === k)?.label ?? k;

/** A tier <select>: '' = the default tier (what a NULL tier_id means on the server). */
function tierSelect(value: string | null | undefined, attrs: string): string {
  const dflt = defaultTier();
  const opts = (US.tiers ?? []).filter((t) => !t.is_default).map((t) =>
    `<option value="${esc(t.id)}"${value === t.id ? ' selected' : ''}>${esc(t.name)}</option>`).join('');
  return `<select ${attrs}><option value=""${!value || value === dflt?.id ? ' selected' : ''}>${esc(dflt ? `${dflt.name} (default)` : 'Default')}</option>${opts}</select>`;
}

function tiersHTML(): string {
  if (!US.tiers) return '';
  const rows = US.tiers.map((t) => {
    const perms = t.permissions.length
      ? t.permissions.map((p) => `<span class="chip tier-perm" title="${esc(p)}">${esc(permLabel(p))}</span>`).join('')
      : '<span class="hint">nothing: opens only what is shared with them</span>';
    return `<li class="tier-row" data-tid="${esc(t.id)}"><div class="tier-head"><b>${esc(t.name)}</b>` +
      (t.is_default ? '<span class="tier-dflt">default</span>' : '') +
      `<span class="num muted">${t.n_users ?? 0} user${t.n_users === 1 ? '' : 's'}</span><span class="tier-acts">` +
      (t.is_default ? '' : '<button class="ghost" data-act="tier-default" title="New sign-ups and users without a tier get this one">Make default</button>') +
      `<button class="icon-btn ghost" data-act="tier-edit" aria-label="${esc(`Edit the tier ${t.name}`)}" title="Edit">${icon('edit')}</button>` +
      (t.is_default ? '' : `<button class="icon-btn ghost danger" data-act="tier-delete" aria-label="${esc(`Delete the tier ${t.name}`)}" title="Delete">${icon('trash')}</button>`) +
      '</span></div>' + (t.description ? `<p class="hint">${esc(t.description)}</p>` : '') + `<div class="chips">${perms}</div></li>`;
  }).join('');
  return '<div class="form-card"><div class="step-head">' + icon('users') + 'Tiers</div>' +
    '<p class="sec-note">What each user may add to the server. Administrators have every permission; what someone may do on a ' +
    'shared session still comes from its sharing (viewer / labeler).</p>' +
    `<ul class="tier-list">${rows}</ul><div class="sec-actions"><button class="primary" data-act="tier-new">${icon('plus')}New tier</button>` +
    `${statusHTML(US.tierStatus)}</div></div>`;
}

function tierDialog(t: AdminTier | null): void {
  const have = new Set(t ? t.permissions : []);
  const boxes = US.perms.map((p) => `<label class="check full"><input type="checkbox" data-perm="${esc(p.key)}" ${have.has(p.key) ? 'checked' : ''}> ` +
    `<span><b>${esc(p.label)}</b> <span class="hint">${esc(p.key)}</span></span></label>`).join('');
  void openDialog({
    title: t ? `Edit the tier ${t.name}` : 'New tier', ok: t ? 'Save' : 'Create tier',
    body: `<label class="fld">Name<input type="text" id="tier-name" maxlength="40" value="${esc(t?.name ?? '')}" autocomplete="off"></label>` +
      `<label class="fld">Description<input type="text" id="tier-desc" maxlength="300" value="${esc(t?.description ?? '')}" autocomplete="off"></label>` +
      `<fieldset class="tier-perms"><legend>Permissions</legend>${boxes}</fieldset>`,
    onOk: async (root) => {
      const name = root.querySelector<HTMLInputElement>('#tier-name')?.value.trim() ?? '';
      if (!name) return 'Give the tier a name';
      const permissions = [...root.querySelectorAll<HTMLInputElement>('[data-perm]')].filter((x) => x.checked).map((x) => x.dataset['perm'] ?? '');
      const body = { name, description: root.querySelector<HTMLInputElement>('#tier-desc')?.value.trim() ?? '', permissions };
      try {
        if (t) await sendJSON('PATCH', `api/admin/tiers/${encodeURIComponent(t.id)}`, body);
        else await postJSON('api/admin/tiers', body);
      } catch (e) { return errText(e); }
      US.tierStatus = { kind: 'ok', text: t ? `Saved ${name}` : `Created the tier ${name}` };
      await reload(null);
      return null;
    },
  });
}

function deleteTier(t: AdminTier): void {
  const n = t.n_users ?? 0;
  void openDialog({
    title: `Delete the tier ${t.name}?`, ok: 'Delete tier', okClass: 'danger-btn',
    body: `<p class="muted">${n ? `Its ${n} user${n === 1 ? '' : 's'} move${n === 1 ? 's' : ''} to ${esc(defaultTier()?.name ?? 'the default tier')}.` : 'Nobody is in it.'}</p>`,
    onOk: async () => {
      try { await sendJSON('DELETE', `api/admin/tiers/${encodeURIComponent(t.id)}`); } catch (e) { return errText(e); }
      US.tierStatus = { kind: 'ok', text: `Deleted the tier ${t.name}` };
      await reload(null);
      return null;
    },
  });
}

const tierOf = (b: HTMLElement): AdminTier | undefined => (US.tiers ?? []).find((t) => t.id === rowAttr(b, '[data-tid]', 'tid'));

function rowHTML(u: AdminUser): string {
  const self = ME && ME.id === u.id;
  const sw = (field: 'is_admin' | 'is_active', on: boolean, label: string): string =>
    `<input type="checkbox" role="switch" class="adm-sw" data-act-change="adm-flag" data-field="${field}" ${on ? 'checked' : ''} ` +
    `aria-label="${esc(`${label}: ${u.name || u.username}`)}"${self && field === 'is_active' ? ' disabled title="You cannot deactivate yourself"' : ''}>`;
  return `<tr data-uid="${esc(u.id)}" class="${u.is_active ? '' : 'off'}">` +
    `<td><span class="adm-user">${avatarHTML(u, 28, { title: false })}<span class="adm-who"><b>${esc(u.name || u.username)}</b>` +
    `<span class="muted">@${esc(u.username)}${self ? ' · you' : ''}</span></span></span></td>` +
    `<td class="adm-email"><bdi>${esc(u.email)}</bdi></td>` +
    `<td>${tierSelect(u.tier_id, `data-act-change="adm-tier" aria-label="${esc(`Tier of ${u.username}`)}"` +
      (u.is_admin ? ' title="Administrators have every permission; the tier applies if they stop being one"' : ''))}</td>` +
    `<td class="c">${sw('is_admin', u.is_admin, 'Administrator')}</td><td class="c">${sw('is_active', u.is_active, 'Active')}</td>` +
    `<td title="${esc(fullTime(u.last_login))}">${esc(when(u.last_login))}</td><td class="num c">${u.n_sessions ?? 0}</td>` +
    `<td class="adm-acts"><button class="icon-btn ghost" data-act="adm-reset" aria-label="${esc(`Reset the password of ${u.username}`)}" title="Reset password">${icon('lock')}</button>` +
    (self ? '' : `<button class="icon-btn ghost danger" data-act="adm-delete" aria-label="${esc(`Delete ${u.username}`)}" title="Delete user">${icon('trash')}</button>`) +
    '</td></tr>';
}

function createHTML(): string {
  if (!US.createOpen) return `<div class="sec-actions"><button class="primary" data-act="adm-new">${icon('plus')}New user</button></div>`;
  const d = US.draft;
  return '<div class="form-card"><div class="step-head">' + icon('user') + 'New user</div><div class="form-grid">' +
    `<label class="fld">Username<input type="text" data-adm="username" data-enter="adm-create" value="${esc(d.username)}" autocomplete="off" autocapitalize="none" maxlength="32" spellcheck="false"></label>` +
    `<label class="fld">Display name<input type="text" data-adm="display_name" data-enter="adm-create" value="${esc(d.display_name)}" autocomplete="off" maxlength="80"></label>` +
    `<label class="fld">Email<input type="email" data-adm="email" data-enter="adm-create" value="${esc(d.email)}" autocomplete="off" autocapitalize="none" spellcheck="false"></label>` +
    `<label class="fld">Password<input type="password" data-adm="password" data-enter="adm-create" value="${esc(d.password)}" autocomplete="new-password" aria-describedby="adm-pw-help"></label>` +
    (US.tiers ? `<label class="fld">Tier${tierSelect(d.tier_id, 'data-act-change="adm-draft-tier"')}</label>` : '') +
    `<label class="check full"><input type="checkbox" data-adm="is_admin" ${d.is_admin ? 'checked' : ''}> Administrator</label>` +
    `<span id="adm-pw-help" class="hint full">At least ${PW_MIN} characters; tell the person to change it under Account.</span></div>` +
    `<div class="sec-actions"><button class="primary" data-act="adm-create">${icon('check')}Create user</button>` +
    `<button class="ghost" data-act="adm-new-cancel">Cancel</button>${statusHTML(US.createStatus)}</div></div>`;
}

function auditHTML(): string {
  if (!US.audit) return '';
  const rows = US.audit.map((e) => `<li><span class="au-at" title="${esc(fullTime(e.at))}">${esc(when(e.at))}</span>` +
    `<b>${esc(e.user ? `@${e.user.username}` : 'system')}</b><span class="au-act">${esc(e.action)}</span>` +
    `<span class="au-target muted">${esc(e.target ?? '')}</span>${e.ip ? `<span class="au-ip hint">${esc(e.ip)}</span>` : ''}</li>`).join('');
  return '<h3 class="sec-sub">Recent activity</h3>' + (rows ? `<ol class="audit-list">${rows}</ol>` : '<p class="sec-note">Nothing yet.</p>');
}

export function renderUsersSec(): string {
  if (!ME || !ME.is_admin) return '<section><p class="sec-note">Only administrators manage users.</p></section>';
  if (!US.users && !US.error) {
    if (!US.loading) void loadUsersData().then(renderSettings);
    return `<section>${statusHTML({ kind: 'busy', text: 'loading users…' })}</section>`;
  }
  const table = US.users ? '<div class="adm-table-wrap"><table class="adm-table"><thead><tr><th scope="col">User</th><th scope="col">Email</th>' +
    '<th scope="col">Tier</th><th scope="col" class="c">Admin</th><th scope="col" class="c">Active</th><th scope="col">Last sign-in</th><th scope="col" class="c">Sessions</th>' +
    `<th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead><tbody>${US.users.map(rowHTML).join('')}</tbody></table></div>` : '';
  return '<section class="adm-sec">' + (US.error ? statusHTML({ kind: 'err', text: US.error }) : '') + settingsHTML() + tiersHTML() +
    `<h3 class="sec-sub">Users${US.users ? ` <span class="num muted">${US.users.length}</span>` : ''}</h3>` + createHTML() + statusHTML(US.status) + table +
    auditHTML() + '</section>';
}

/** data-adm inputs of the new-user form write into the draft. */
export function admInput(el: HTMLInputElement): void {
  const k = el.dataset['adm'];
  if (k === 'is_admin') US.draft.is_admin = el.checked;
  else if (k === 'username') {
    const lower = el.value.toLowerCase();
    if (lower !== el.value) { const at = el.selectionStart; el.value = lower; if (at !== null) el.setSelectionRange(at, at); }
    el.setAttribute('aria-invalid', String(!!el.value && !USERNAME.test(el.value)));
    US.draft.username = el.value;
  } else if (k === 'display_name' || k === 'email' || k === 'password') US.draft[k] = el.value;
}

const userOf = (b: HTMLElement): AdminUser | undefined => (US.users ?? []).find((u) => u.id === rowAttr(b, '[data-uid]', 'uid'));

async function reload(status: StatusLine | null): Promise<void> {
  await loadUsersData();
  US.status = status;
  renderSettings();
}

async function createUser(): Promise<void> {
  const d = US.draft;
  const bad = (text: string): void => { US.createStatus = { kind: 'err', text }; renderSettings(); };
  if (!USERNAME.test(d.username)) return bad('The username needs 3–32 characters: a–z, 0–9, dot, dash, underscore');
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(d.email.trim())) return bad('Enter a valid email address');
  if (d.password.length < PW_MIN) return bad(`The password needs at least ${PW_MIN} characters`);
  US.createStatus = { kind: 'busy', text: 'creating…' };
  renderSettings();
  try {
    await postJSON('api/admin/users', { username: d.username, email: d.email.trim(), display_name: d.display_name.trim() || d.username,
      password: d.password, is_admin: d.is_admin, tier_id: d.tier_id });
    US.draft = { ...EMPTY_DRAFT };
    US.createOpen = false;
    US.createStatus = null;
    await reload({ kind: 'ok', text: `Created @${d.username}` });
  } catch (e) { bad(errText(e)); }
}

function resetPassword(u: AdminUser): void {
  void openDialog({
    title: `Reset the password of @${u.username}`, ok: 'Set password',
    body: `<p class="muted">Their other sessions are signed out. Tell them the new password; they can change it under Account.</p>` +
      `<label class="fld">New password<input type="password" id="adm-rp1" autocomplete="new-password"></label>` +
      `<label class="fld">Confirm<input type="password" id="adm-rp2" autocomplete="new-password"></label>`,
    onOk: async (root) => {
      const p1 = root.querySelector<HTMLInputElement>('#adm-rp1')?.value ?? '', p2 = root.querySelector<HTMLInputElement>('#adm-rp2')?.value ?? '';
      if (p1.length < PW_MIN) return `The password needs at least ${PW_MIN} characters`;
      if (p1 !== p2) return 'The two passwords differ';
      try { await sendJSON('PATCH', `api/admin/users/${encodeURIComponent(u.id)}`, { password: p1 }); } catch (e) { return errText(e); }
      US.status = { kind: 'ok', text: `New password set for @${u.username}` };
      renderSettings();
      return null;
    },
  });
}

function deleteUser(u: AdminUser): void {
  const others = (US.users ?? []).filter((x) => x.id !== u.id && x.is_active);
  const n = u.n_sessions ?? 0;
  const opts = others.map((x) => `<option value="${esc(x.id)}"${ME && x.id === ME.id ? ' selected' : ''}>${esc(x.name || x.username)} (@${esc(x.username)})</option>`).join('');
  void openDialog({
    title: `Delete @${u.username}?`, ok: 'Delete user', okClass: 'danger-btn',
    body: `<p class="muted">${esc(u.name || u.username)} can no longer sign in. ${n ? `They own ${n} session${n === 1 ? '' : 's'}.` : 'They own no sessions.'}</p>` +
      (n ? `<label class="fld">Their sessions<select id="adm-transfer">${opts}<option value="">Delete their sessions</option></select></label>` +
        '<label class="check hidden" id="adm-del-ok-row"><input type="checkbox" id="adm-del-ok"> <span>Yes, delete their sessions and everything in them (cannot be undone)</span></label>' : ''),
    bind: (root) => {
      const sel = root.querySelector<HTMLSelectElement>('#adm-transfer');
      sel?.addEventListener('change', () => root.querySelector('#adm-del-ok-row')?.classList.toggle('hidden', sel.value !== ''));
    },
    onOk: async (root) => {
      const sel = root.querySelector<HTMLSelectElement>('#adm-transfer');
      const to = sel ? sel.value : '';
      if (sel && !to && !root.querySelector<HTMLInputElement>('#adm-del-ok')?.checked) return 'Tick the box to confirm deleting their sessions';
      try {
        const r = await sendJSON<{ transferred?: number; deleted?: number }>('DELETE', `api/admin/users/${encodeURIComponent(u.id)}?transfer_to=${encodeURIComponent(to)}`);
        const parts = [r.transferred ? `${r.transferred} session${r.transferred === 1 ? '' : 's'} moved` : '', r.deleted ? `${r.deleted} deleted` : ''].filter(Boolean);
        await reload({ kind: 'ok', text: `Deleted @${u.username}${parts.length ? ` (${parts.join(', ')})` : ''}` });
      } catch (e) { return errText(e); }
      return null;
    },
  });
}

async function saveSettings(patch: Partial<AdminSettings>): Promise<void> {
  US.setStatus = { kind: 'busy', text: 'saving…' };
  renderSettings();
  try {
    US.settings = await sendJSON<AdminSettings>('PUT', 'api/admin/settings', patch);
    US.setStatus = { kind: 'ok', text: 'Saved' };
  } catch (e) { US.setStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

export const USERS_ACTIONS: Record<string, SetAction> = {
  'adm-new': () => { US.createOpen = true; US.createStatus = null; renderSettings(); document.querySelector<HTMLElement>('[data-adm="username"]')?.focus(); },
  'adm-new-cancel': () => { US.createOpen = false; renderSettings(); },
  'adm-create': createUser,
  'adm-reset': (b) => { const u = userOf(b); if (u) resetPassword(u); },
  'adm-delete': (b) => { const u = userOf(b); if (u) deleteUser(u); },
  'tier-new': () => tierDialog(null),
  'tier-edit': (b) => { const t = tierOf(b); if (t) tierDialog(t); },
  'tier-delete': (b) => { const t = tierOf(b); if (t) deleteTier(t); },
  'tier-default': async (b) => {
    const t = tierOf(b);
    if (!t) return;
    try {
      await sendJSON('PATCH', `api/admin/tiers/${encodeURIComponent(t.id)}`, { is_default: true });
      US.tierStatus = { kind: 'ok', text: `${t.name} is now the default tier` };
    } catch (e) { US.tierStatus = { kind: 'err', text: errText(e) }; }
    await reload(null);
  },
};

export const USERS_CHANGES: Record<string, SetChange> = {
  'adm-signup': (el) => { if (el instanceof HTMLInputElement) void saveSettings({ signup_enabled: el.checked }); },
  'adm-conns': (el) => {
    if (el instanceof HTMLSelectElement) void saveSettings({ who_can_add_connections: el.value === 'admins' ? 'admins' : 'everyone' });
  },
  'adm-draft-tier': (el) => { if (el instanceof HTMLSelectElement) US.draft.tier_id = el.value; },
  'adm-tier': async (el) => {
    const u = el instanceof HTMLSelectElement ? userOf(el) : undefined;
    if (!u || !(el instanceof HTMLSelectElement)) return;
    const name = el.selectedOptions[0]?.textContent ?? '';
    el.disabled = true;
    try {
      await sendJSON('PATCH', `api/admin/users/${encodeURIComponent(u.id)}`, { tier_id: el.value });
      await reload({ kind: 'ok', text: `@${u.username}: now in ${name}` });
    } catch (e) { await reload({ kind: 'err', text: errText(e) }); }
  },
  'adm-flag': async (el) => {
    const u = el instanceof HTMLInputElement ? userOf(el) : undefined;
    if (!u || !(el instanceof HTMLInputElement)) return;
    const field = dd(el, 'field');
    const on = el.checked;
    el.disabled = true;
    try {
      await sendJSON('PATCH', `api/admin/users/${encodeURIComponent(u.id)}`, { [field]: on });
      await reload({ kind: 'ok', text: `@${u.username}: ${field === 'is_admin' ? (on ? 'now an administrator' : 'no longer an administrator') : on ? 'active again' : 'deactivated (signed out everywhere)'}` });
    } catch (e) {
      el.checked = !on;
      el.disabled = false;
      US.status = { kind: 'err', text: errText(e) };
      renderSettings();
    }
  },
};
