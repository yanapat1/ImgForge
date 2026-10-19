// The sign-in page (/login, html[data-screen=login], loaded on demand by main.ts): a centred card with two tabs,
// Sign in (username or email + password, show / hide) and Create account (shown only when GET api/auth/config says
// sign-up is on: username checked live against ^[a-z0-9._-]{3,32}$ and lower-cased as typed, display name, email,
// password + confirm with a strength meter, at least password_min characters). Server `detail` errors show inline
// (role=alert). Success goes to `next` when it is a URL of this app (core/nav.ts safeNext), else the home.

import { apiFetch } from '../core/api.js';
import { $req, esc, icon, ICONS } from '../core/dom.js';
import { safeNext } from '../core/nav.js';

type Tab = 'in' | 'up';
interface AuthConfig { signup_enabled?: boolean; password_min?: number }
const USERNAME = /^[a-z0-9._-]{3,32}$/;
const LG = { tab: 'in' as Tab, signup: false, min: 10, busy: false };

/** The server's message from an error answer (FastAPI: {detail: "…"} or a validation list). */
async function failText(r: Response, fallback: string): Promise<string> {
  const d: unknown = await r.json().catch(() => null);
  if (d && typeof d === 'object' && 'detail' in d) {
    const det = d.detail;
    if (typeof det === 'string') return det;
    if (Array.isArray(det)) {
      const first: unknown = det[0];
      if (first && typeof first === 'object' && 'msg' in first) return String(first.msg);
    }
  }
  return r.status === 429 ? 'Too many attempts: wait a minute, then try again.' : `${fallback} (${r.status})`;
}

const pwField = (id: string, label: string, ac: string, extra = ''): string => `<label class="fld" for="${id}">${label}` +
  `<span class="pw-wrap"><input type="password" id="${id}" name="${id}" autocomplete="${ac}" required spellcheck="false" ${extra}>` +
  `<button type="button" class="icon-btn ghost pw-eye" data-eye="${id}" aria-pressed="false" aria-label="Show password" title="Show password">${icon('eye')}</button></span></label>`;

function markup(): string {
  return `<div class="login-wrap"><main class="login-card panel" aria-labelledby="login-title">
    <div class="login-brand"><svg class="brand-mark" viewBox="0 0 32 32" fill="none" stroke="currentColor" aria-hidden="true"><use href="${ICONS}#i-brand"/></svg>
      <h1 id="login-title">Dataset Visualizer</h1></div>
    <div class="segmented login-tabs single" role="tablist" aria-label="Sign in or create an account">
      <button type="button" role="tab" id="lg-tab-in" aria-controls="lg-in" aria-selected="true" data-tab="in">Sign in</button>
      <button type="button" role="tab" id="lg-tab-up" aria-controls="lg-up" aria-selected="false" tabindex="-1" data-tab="up" class="hidden">Create account</button>
    </div>
    <form id="lg-in" class="login-form" role="tabpanel" aria-labelledby="lg-tab-in" novalidate>
      <label class="fld" for="lg-login">Username or email<input type="text" id="lg-login" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
      ${pwField('lg-pw', 'Password', 'current-password')}
      <div id="lg-in-err" class="status-line err hidden" role="alert"></div>
      <button type="submit" class="primary block" id="lg-in-go">Sign in</button>
    </form>
    <form id="lg-up" class="login-form hidden" role="tabpanel" aria-labelledby="lg-tab-up" novalidate>
      <label class="fld" for="lg-user">Username<input type="text" id="lg-user" name="username" autocomplete="username" autocapitalize="none" spellcheck="false"
        maxlength="32" required aria-describedby="lg-user-help"><span id="lg-user-help" class="help">3–32 characters: a–z, 0–9, dot, dash, underscore</span></label>
      <label class="fld" for="lg-name">Display name<input type="text" id="lg-name" name="name" autocomplete="name" maxlength="80" required></label>
      <label class="fld" for="lg-email">Email<input type="email" id="lg-email" name="email" autocomplete="email" autocapitalize="none" spellcheck="false" required></label>
      ${pwField('lg-pw1', 'Password', 'new-password', 'aria-describedby="lg-meter-text"')}
      <div class="pw-meter" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
      <span id="lg-meter-text" class="help pw-meter-text"></span>
      ${pwField('lg-pw2', 'Confirm password', 'new-password')}
      <div id="lg-up-err" class="status-line err hidden" role="alert"></div>
      <button type="submit" class="primary block" id="lg-up-go">Create account</button>
    </form>
  </main></div>`;
}

function setTab(t: Tab, focus: boolean): void {
  LG.tab = t;
  for (const k of ['in', 'up'] as const) {
    const on = k === t;
    const b = $req(`lg-tab-${k}`);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
    $req(`lg-${k}`).classList.toggle('hidden', !on);
  }
  if (focus) $req(`lg-tab-${t}`).focus();
  document.title = `${t === 'in' ? 'Sign in' : 'Create account'} · Dataset Visualizer`;
}

function showErr(id: string, text: string): void {
  const el = $req(id);
  el.classList.toggle('hidden', !text);
  el.innerHTML = text ? `${icon('alert')}<span>${esc(text)}</span>` : '';
}

// 0 = too short, 1 weak … 4 strong: length past the minimum + character classes
function strength(pw: string): number {
  if (pw.length < LG.min) return 0;
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((re) => re.test(pw)).length;
  let s = 1 + (pw.length >= LG.min + 4 ? 1 : 0) + (classes >= 3 ? 1 : 0) + (pw.length >= 20 || (classes === 4 && pw.length >= LG.min + 2) ? 1 : 0);
  if (/^(.)\1+$/.test(pw)) s = 1;
  return Math.min(4, s);
}
const STRENGTH_TEXT = ['', 'Weak', 'Fair', 'Good', 'Strong'];

function paintMeter(): void {
  const pw = $req<HTMLInputElement>('lg-pw1').value;
  const s = strength(pw);
  const meter = document.querySelector<HTMLElement>('.pw-meter');
  if (meter) meter.dataset['s'] = String(pw ? s : -1);
  $req('lg-meter-text').textContent = !pw ? `At least ${LG.min} characters` : s === 0 ? `Too short: ${pw.length} of at least ${LG.min} characters` : `Strength: ${STRENGTH_TEXT[s]}`;
}

function paintUsername(): void {
  const inp = $req<HTMLInputElement>('lg-user');
  const v = inp.value;
  const ok = !v || USERNAME.test(v);
  inp.setAttribute('aria-invalid', String(!ok));
  const help = $req('lg-user-help');
  help.classList.toggle('bad', !ok);
  help.textContent = ok ? '3–32 characters: a–z, 0–9, dot, dash, underscore'
    : v.length < 3 ? `Too short: at least 3 characters` : 'Only a–z, 0–9, dot, dash and underscore';
}

async function signIn(): Promise<void> {
  const login = $req<HTMLInputElement>('lg-login').value.trim(), password = $req<HTMLInputElement>('lg-pw').value;
  if (!login || !password) { showErr('lg-in-err', !login ? 'Enter your username or email.' : 'Enter your password.'); $req(!login ? 'lg-login' : 'lg-pw').focus(); return; }
  await submit('lg-in', 'api/auth/login', { login, password }, 'Could not sign in');
}

async function signUp(): Promise<void> {
  const username = $req<HTMLInputElement>('lg-user').value.trim(), display_name = $req<HTMLInputElement>('lg-name').value.trim();
  const email = $req<HTMLInputElement>('lg-email').value.trim(), password = $req<HTMLInputElement>('lg-pw1').value, again = $req<HTMLInputElement>('lg-pw2').value;
  const bad = (field: string, text: string): void => { showErr('lg-up-err', text); $req(field).focus(); };
  if (!USERNAME.test(username)) return bad('lg-user', 'The username needs 3–32 characters: a–z, 0–9, dot, dash, underscore.');
  if (!display_name) return bad('lg-name', 'Enter a display name.');
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return bad('lg-email', 'Enter a valid email address.');
  if (password.length < LG.min) return bad('lg-pw1', `The password needs at least ${LG.min} characters.`);
  if (password !== again) return bad('lg-pw2', 'The two passwords differ.');
  await submit('lg-up', 'api/auth/signup', { username, email, display_name, password }, 'Could not create the account');
}

async function submit(form: 'lg-in' | 'lg-up', path: string, body: Record<string, string>, fail: string): Promise<void> {
  if (LG.busy) return;
  LG.busy = true;
  const go = $req<HTMLButtonElement>(`${form}-go`);
  const label = go.textContent ?? '';
  go.disabled = true;
  go.innerHTML = `<span class="spin" aria-hidden="true"></span>${esc(label)}`;
  showErr(`${form}-err`, '');
  try {
    const r = await apiFetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await failText(r, fail));
    location.replace(safeNext(new URLSearchParams(location.search).get('next')));
    return;
  } catch (e) {
    showErr(`${form}-err`, e instanceof Error ? e.message : String(e));
    const pw = $req<HTMLInputElement>(form === 'lg-in' ? 'lg-pw' : 'lg-pw1');
    if (form === 'lg-in') { pw.value = ''; pw.focus(); }
  }
  LG.busy = false;
  go.disabled = false;
  go.textContent = label;
}

function bind(): void {
  const root = $req('login');
  root.addEventListener('click', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const tab = t?.closest<HTMLElement>('[role=tab]');
    if (tab) { setTab(tab.dataset['tab'] === 'up' ? 'up' : 'in', false); $req(tab.dataset['tab'] === 'up' ? 'lg-user' : 'lg-login').focus(); return; }
    const eye = t?.closest<HTMLButtonElement>('[data-eye]');
    if (eye) {
      const inp = $req<HTMLInputElement>(eye.dataset['eye'] ?? '');
      const show = inp.type === 'password';
      inp.type = show ? 'text' : 'password';
      eye.setAttribute('aria-pressed', String(show));
      const text = show ? 'Hide password' : 'Show password';
      eye.setAttribute('aria-label', text);
      eye.title = text;
      eye.innerHTML = icon(show ? 'eye-off' : 'eye');
    }
  });
  root.querySelector('[role=tablist]')?.addEventListener('keydown', (e) => {
    if (!(e instanceof KeyboardEvent) || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key) || !LG.signup) return;
    e.preventDefault();
    setTab(e.key === 'Home' ? 'in' : e.key === 'End' ? 'up' : LG.tab === 'in' ? 'up' : 'in', true);
  });
  $req('lg-in').addEventListener('submit', (e) => { e.preventDefault(); void signIn(); });
  $req('lg-up').addEventListener('submit', (e) => { e.preventDefault(); void signUp(); });
  const user = $req<HTMLInputElement>('lg-user');
  user.addEventListener('input', () => {
    const lower = user.value.toLowerCase();
    if (lower !== user.value) {                    // lower-case as typed, keeping the caret
      const at = user.selectionStart;
      user.value = lower;
      if (at !== null) user.setSelectionRange(at, at);
    }
    paintUsername();
  });
  $req('lg-pw1').addEventListener('input', paintMeter);
}

export async function renderLogin(): Promise<void> {
  document.title = 'Sign in · Dataset Visualizer';
  const root = $req('login');
  root.innerHTML = markup();
  bind();
  setTab('in', false);
  $req('lg-login').focus();
  try {
    const r = await apiFetch('api/auth/config', { cache: 'no-store' });
    const cfg: AuthConfig = r.ok ? await r.json() : {};
    LG.signup = !!cfg.signup_enabled;
    if (typeof cfg.password_min === 'number' && cfg.password_min > 0) LG.min = cfg.password_min;
  } catch (e) { /* no config: sign in only */ }
  $req('lg-tab-up').classList.toggle('hidden', !LG.signup);
  $req('login').querySelector('.login-tabs')?.classList.toggle('single', !LG.signup);
  paintMeter();
  if (LG.signup && new URLSearchParams(location.search).get('signup') === '1') setTab('up', false);
}
