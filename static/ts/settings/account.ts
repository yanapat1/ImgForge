// Settings › Account (every user): the profile picture (PUT api/me/avatar multipart, previewed before it is sent;
// DELETE api/me/avatar), display name / username / email (PATCH api/me), the password (PUT api/me/password: the
// current one + the new one twice; the server signs out the other devices), "Sign out other devices"
// (POST api/auth/logout-others) and Sign out. Field values live in ACC (data-acc inputs), so a re-render of the
// dialog never loses what was typed.

import { api, errText, postJSON, sendJSON } from '../core/api.js';
import { avatarHTML } from '../core/avatar.js';
import { displayName, ME, setMe, signOut } from '../core/auth.js';
import type { Me } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import type { SetAction, StatusLine } from '../types-settings.js';
import { renderSettings } from './dialog.js';
import { statusHTML } from './forms.js';

const USERNAME = /^[a-z0-9._-]{3,32}$/;
const PW_MIN = 8;

interface AccState {
  name: string | null; username: string | null; email: string | null;
  cur: string; pw1: string; pw2: string;
  file: File | null; preview: string | null;
  avStatus: StatusLine | null; profStatus: StatusLine | null; pwStatus: StatusLine | null; devStatus: StatusLine | null;
}
// name / username / email: null = not edited (shows ME's value)
const ACC: AccState = { name: null, username: null, email: null, cur: '', pw1: '', pw2: '', file: null, preview: null,
  avStatus: null, profStatus: null, pwStatus: null, devStatus: null };

const me = (): Me => {
  if (!ME) throw new Error('not signed in');
  return ME;
};
const val = (k: 'name' | 'username' | 'email'): string => ACC[k] ?? (k === 'name' ? me().display_name : k === 'username' ? me().username : me().email ?? '');

/** data-acc inputs write here (the dialog's input listener). */
export function accInput(el: HTMLInputElement): void {
  const k = el.dataset['acc'];
  if (k === 'name' || k === 'username' || k === 'email') {
    if (k === 'username') {
      const lower = el.value.toLowerCase();
      if (lower !== el.value) { const at = el.selectionStart; el.value = lower; if (at !== null) el.setSelectionRange(at, at); }
      const ok = !el.value || USERNAME.test(el.value);
      el.setAttribute('aria-invalid', String(!ok));
    }
    ACC[k] = el.value;
  } else if (k === 'cur' || k === 'pw1' || k === 'pw2') ACC[k] = el.value;
}

/** A file picked in the avatar input: preview it (nothing is sent until Save). */
export function accFile(el: HTMLInputElement): void {
  const f = el.files && el.files[0];
  if (!f) return;
  if (ACC.preview) URL.revokeObjectURL(ACC.preview);
  if (!f.type.startsWith('image/')) { ACC.file = null; ACC.preview = null; ACC.avStatus = { kind: 'err', text: 'That is not an image file' }; renderSettings(); return; }
  if (f.size > 5 * 1024 * 1024) { ACC.file = null; ACC.preview = null; ACC.avStatus = { kind: 'err', text: 'The picture is larger than 5 MB' }; renderSettings(); return; }
  ACC.file = f;
  ACC.preview = URL.createObjectURL(f);
  ACC.avStatus = { kind: '', text: 'Preview: save to use it everywhere' };
  renderSettings();
}

const fld = (k: string, label: string, value: string, extra = ''): string => `<label class="fld">${label}` +
  `<input type="${k === 'email' ? 'email' : k.startsWith('pw') || k === 'cur' ? 'password' : 'text'}" data-acc="${k}" value="${esc(value)}" spellcheck="false" ${extra}></label>`;

export function renderAccountSec(): string {
  if (!ME) return '<section><p class="sec-note">Not signed in.</p></section>';
  const u = me();
  const pic = ACC.preview
    ? `<span class="avatar acc-preview" style="--av-size:72px"><img src="${esc(ACC.preview)}" alt="Preview of the new picture" width="72" height="72"></span>`
    : avatarHTML(u, 72);
  const avatar = '<div class="form-card"><div class="step-head">' + icon('user') + 'Picture</div><div class="acc-avatar">' + pic +
    '<div class="acc-av-actions"><input type="file" id="acc-file" accept="image/png,image/jpeg,image/webp,image/gif" class="visually-hidden" data-acc-file="1" aria-label="Choose a picture">' +
    (ACC.file
      ? `<button class="primary" data-act="acc-av-save">${icon('check')}Save picture</button><button class="ghost" data-act="acc-av-cancel">Cancel</button>`
      : `<button data-act="acc-av-pick">${icon('upload')}Upload picture…</button>` +
        (u.avatar_ver > 0 ? `<button class="ghost" data-act="acc-av-remove">${icon('trash')}Remove</button>` : '')) +
    '<span class="hint">PNG, JPEG, WebP or GIF up to 5 MB; it is cropped to a square.</span></div></div>' +
    statusHTML(ACC.avStatus) + '</div>';
  const profile = '<div class="form-card"><div class="step-head">' + icon('edit') + 'Profile</div><div class="form-grid">' +
    fld('name', 'Display name', val('name'), 'autocomplete="name" maxlength="80" data-enter="acc-save"') +
    fld('username', 'Username', val('username'), 'autocomplete="username" autocapitalize="none" maxlength="32" aria-describedby="acc-user-help" data-enter="acc-save"') +
    fld('email', 'Email', val('email'), 'autocomplete="email" autocapitalize="none" data-enter="acc-save"') +
    '<span id="acc-user-help" class="hint full">Username: 3–32 characters, a–z, 0–9, dot, dash, underscore. Others find you by it when they share a session.</span></div>' +
    `<div class="sec-actions"><button class="primary" data-act="acc-save">${icon('check')}Save profile</button>${statusHTML(ACC.profStatus)}</div></div>`;
  const pw = '<div class="form-card"><div class="step-head">' + icon('lock') + 'Password</div><div class="form-grid">' +
    `<label class="fld full">Current password<input type="password" data-acc="cur" value="${esc(ACC.cur)}" autocomplete="current-password" data-enter="acc-pw"></label>` +
    `<label class="fld">New password<input type="password" data-acc="pw1" value="${esc(ACC.pw1)}" autocomplete="new-password" aria-describedby="acc-pw-help" data-enter="acc-pw"></label>` +
    `<label class="fld">Confirm the new password<input type="password" data-acc="pw2" value="${esc(ACC.pw2)}" autocomplete="new-password" data-enter="acc-pw"></label>` +
    `<span id="acc-pw-help" class="hint full">At least ${PW_MIN} characters. Changing it signs you out on your other devices.</span></div>` +
    `<div class="sec-actions"><button class="primary" data-act="acc-pw">${icon('lock')}Change password</button>${statusHTML(ACC.pwStatus)}</div></div>`;
  const devices = '<div class="form-card"><div class="step-head">' + icon('monitor') + 'Devices</div>' +
    `<p class="sec-note">Signed in as <b>${esc(displayName(u))}</b> (@${esc(u.username)})${u.is_admin ? ' · administrator' : u.tier ? ` · tier <b>${esc(u.tier.name)}</b>` : ''}.</p>` +
    `<div class="sec-actions"><button data-act="acc-others">${icon('logout')}Sign out other devices</button>` +
    `<button class="ghost" data-act="acc-signout">${icon('logout')}Sign out</button>${statusHTML(ACC.devStatus)}</div></div>`;
  return `<section class="acc-sec">${avatar}${profile}${pw}${devices}</section>`;
}

function clearAvatarDraft(): void {
  if (ACC.preview) URL.revokeObjectURL(ACC.preview);
  ACC.file = null;
  ACC.preview = null;
}

async function saveAvatar(): Promise<void> {
  if (!ACC.file) return;
  ACC.avStatus = { kind: 'busy', text: 'uploading…' };
  renderSettings();
  const fd = new FormData();
  fd.append('file', ACC.file, ACC.file.name);
  try {
    const r = await api('api/me/avatar', { method: 'PUT', body: fd });
    const { user } = await r.json() as { user: Me };
    clearAvatarDraft();
    setMe(user);
    ACC.avStatus = { kind: 'ok', text: 'Picture saved' };
  } catch (e) { ACC.avStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

async function removeAvatar(): Promise<void> {
  ACC.avStatus = { kind: 'busy', text: 'removing…' };
  renderSettings();
  try {
    const { user } = await sendJSON<{ user: Me }>('DELETE', 'api/me/avatar');
    setMe(user);
    ACC.avStatus = { kind: 'ok', text: 'Picture removed: your initials show instead' };
  } catch (e) { ACC.avStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

async function saveProfile(): Promise<void> {
  const u = me();
  const body: Record<string, string> = {};
  const name = val('name').trim(), username = val('username').trim(), email = val('email').trim();
  if (!name) { ACC.profStatus = { kind: 'err', text: 'The display name cannot be empty' }; renderSettings(); return; }
  if (!USERNAME.test(username)) { ACC.profStatus = { kind: 'err', text: 'The username needs 3–32 characters: a–z, 0–9, dot, dash, underscore' }; renderSettings(); return; }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { ACC.profStatus = { kind: 'err', text: 'Enter a valid email address' }; renderSettings(); return; }
  if (name !== u.display_name) body['display_name'] = name;
  if (username !== u.username) body['username'] = username;
  if (email !== (u.email ?? '')) body['email'] = email;
  if (!Object.keys(body).length) { ACC.profStatus = { kind: 'ok', text: 'Nothing changed' }; renderSettings(); return; }
  ACC.profStatus = { kind: 'busy', text: 'saving…' };
  renderSettings();
  try {
    const { user } = await sendJSON<{ user: Me }>('PATCH', 'api/me', body);
    ACC.name = ACC.username = ACC.email = null;
    setMe(user);
    ACC.profStatus = { kind: 'ok', text: 'Profile saved' };
  } catch (e) { ACC.profStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

async function changePassword(): Promise<void> {
  const bad = (text: string): void => { ACC.pwStatus = { kind: 'err', text }; renderSettings(); };
  if (!ACC.cur) return bad('Enter your current password');
  if (ACC.pw1.length < PW_MIN) return bad(`The new password needs at least ${PW_MIN} characters`);
  if (ACC.pw1 !== ACC.pw2) return bad('The two new passwords differ');
  ACC.pwStatus = { kind: 'busy', text: 'changing…' };
  renderSettings();
  try {
    await sendJSON('PUT', 'api/me/password', { current: ACC.cur, password: ACC.pw1 });
    ACC.cur = ACC.pw1 = ACC.pw2 = '';
    ACC.pwStatus = { kind: 'ok', text: 'Password changed; your other devices were signed out' };
  } catch (e) { ACC.pwStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

async function signOutOthers(): Promise<void> {
  ACC.devStatus = { kind: 'busy', text: 'signing out…' };
  renderSettings();
  try {
    const r = await postJSON<{ n?: number }>('api/auth/logout-others', {});
    const n = r.n ?? 0;
    ACC.devStatus = { kind: 'ok', text: n ? `Signed out ${n} other session${n === 1 ? '' : 's'}` : 'No other device was signed in' };
  } catch (e) { ACC.devStatus = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

export const ACCOUNT_ACTIONS: Record<string, SetAction> = {
  'acc-av-pick': () => { document.getElementById('acc-file')?.click(); },
  'acc-av-cancel': () => { clearAvatarDraft(); ACC.avStatus = null; renderSettings(); },
  'acc-av-save': saveAvatar,
  'acc-av-remove': removeAvatar,
  'acc-save': saveProfile,
  'acc-pw': changePassword,
  'acc-others': signOutOthers,
  'acc-signout': () => signOut(),
};
