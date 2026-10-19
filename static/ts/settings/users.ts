// Settings › Users & admin (admins only; the nav button is hidden for everyone else and the server answers 403):
// the server settings (sign-up on / off, who can add connections: PUT api/admin/settings), the user table
// (GET api/admin/users: admin and active switches through PATCH api/admin/users/{id}, reset password, delete with
// their sessions moved to another user or deleted), a create-user form (POST api/admin/users) and the recent audit
// log (GET api/admin/audit?limit=100). Loaded when the section opens; every change re-reads the list.

import { errText, getJSON, postJSON, sendJSON } from '../core/api.js';
import { avatarHTML } from '../core/avatar.js';
import { ME } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import { openDialog } from '../views/dialog.js';
import type { AdminSettings, AdminUser, AuditEvent, SetAction, SetChange, StatusLine } from '../types-settings.js';
import { renderSettings } from './dialog.js';
import { dd, rowAttr, statusHTML } from './forms.js';

const USERNAME = /^[a-z0-9._-]{3,32}$/;
const PW_MIN = 10;

interface UsersState {
  users: AdminUser[] | null; settings: AdminSettings | null; audit: AuditEvent[] | null;
  loading: boolean; error: string | null; status: StatusLine | null; setStatus: StatusLine | null;
  createOpen: boolean; draft: { username: string; display_name: string; email: string; password: string; is_admin: boolean };
  createStatus: StatusLine | null;
}
const US: UsersState = { users: null, settings: null, audit: null, loading: false, error: null, status: null, setStatus: null, createOpen: false,
  draft: { username: '', display_name: '', email: '', password: '', is_admin: false }, createStatus: null };

/** Read users, settings and the audit log (each failing alone shows its own line). */
export async function loadUsersData(): Promise<void> {
  US.loading = true;
  const [u, s, a] = await Promise.allSettled([
    getJSON<{ users: AdminUser[] }>('api/admin/users'),
    getJSON<AdminSettings>('api/admin/settings'),
    getJSON<{ events: AuditEvent[] }>('api/admin/audit?limit=100'),
  ]);
  US.loading = false;
  US.users = u.status === 'fulfilled' ? u.value.users : US.users;
  US.settings = s.status === 'fulfilled' ? s.value : US.settings;
  US.audit = a.status === 'fulfilled' ? a.value.events : US.audit;
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

function rowHTML(u: AdminUser): string {
  const self = ME && ME.id === u.id;
  const sw = (field: 'is_admin' | 'is_active', on: boolean, label: string): string =>
    `<input type="checkbox" role="switch" class="adm-sw" data-act-change="adm-flag" data-field="${field}" ${on ? 'checked' : ''} ` +
    `aria-label="${esc(`${label}: ${u.name || u.username}`)}"${self && field === 'is_active' ? ' disabled title="You cannot deactivate yourself"' : ''}>`;
  return `<tr data-uid="${esc(u.id)}" class="${u.is_active ? '' : 'off'}">` +
    `<td><span class="adm-user">${avatarHTML(u, 28, { title: false })}<span class="adm-who"><b>${esc(u.name || u.username)}</b>` +
    `<span class="muted">@${esc(u.username)}${self ? ' · you' : ''}</span></span></span></td>` +
    `<td class="adm-email"><bdi>${esc(u.email)}</bdi></td>` +
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
    '<th scope="col" class="c">Admin</th><th scope="col" class="c">Active</th><th scope="col">Last sign-in</th><th scope="col" class="c">Sessions</th>' +
    `<th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead><tbody>${US.users.map(rowHTML).join('')}</tbody></table></div>` : '';
  return '<section class="adm-sec">' + (US.error ? statusHTML({ kind: 'err', text: US.error }) : '') + settingsHTML() +
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
      password: d.password, is_admin: d.is_admin });
    US.draft = { username: '', display_name: '', email: '', password: '', is_admin: false };
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
};

export const USERS_CHANGES: Record<string, SetChange> = {
  'adm-signup': (el) => { if (el instanceof HTMLInputElement) void saveSettings({ signup_enabled: el.checked }); },
  'adm-conns': (el) => {
    if (el instanceof HTMLSelectElement) void saveSettings({ who_can_add_connections: el.value === 'admins' ? 'admins' : 'everyone' });
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
