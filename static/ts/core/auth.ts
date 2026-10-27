// The signed-in user (GET api/auth/me, read once per page) and their preferences (/api/me/prefs: theme, look,
// palette, the labeling settings + hotkeys, the home's tab and scope filter). userKey(k) gives every browser-storage
// key that holds a user's data the user id as a suffix, so two people sharing a browser never see each other's.

import { apiFetch, errText, getJSON, isStatus, sendJSON } from './api.js';
import { loginUrl } from './nav.js';

export interface Me {
  id: string; username: string; email?: string; display_name: string; is_admin: boolean; avatar_ver: number;
  /** the user's tier (dataviz/auth/tiers.py) and what it lets them add; admins have every permission */
  tier?: { id: string; name: string } | null; perms?: string[];
}
export type Prefs = Record<string, unknown>;
interface MePayload { user: Me; prefs?: Prefs | null }

/** The signed-in user; null before loadMe() resolves (and on the sign-in page). */
export let ME: Me | null = null;
let PREFS: Prefs = {};
let loading: Promise<Me | null> | null = null;

const isRecord = (x: unknown): x is Prefs => typeof x === 'object' && x !== null && !Array.isArray(x);

/** Read the user once (later calls share the same answer); null when signed out (the 401 already redirects);
 *  any other failure throws. */
export function loadMe(): Promise<Me | null> {
  if (!loading) {
    loading = getJSON<MePayload>('api/auth/me').then((r) => {
      ME = r.user;
      PREFS = isRecord(r.prefs) ? { ...r.prefs } : {};
      return ME;
    }).catch((e: unknown) => {
      if (isStatus(e, 401)) return null;             // signed out: apiFetch is on its way to the sign-in page
      loading = null;                                // anything else: the caller reports it, a later call retries
      throw e;
    });
  }
  return loading;
}

/** Replace the cached user (after an account edit or an avatar upload) and tell the page (`me-changed`). */
export function setMe(u: Me): void {
  ME = u;
  document.dispatchEvent(new CustomEvent('me-changed'));
}

export const isAdmin = (): boolean => !!ME && !!ME.is_admin;
/** Whether the user's tier grants `perm` (e.g. 'dataset.create'); the server checks it again. Unknown (an older
 *  server without tiers): allowed. */
export const can = (perm: string): boolean => !!ME && (ME.is_admin || !ME.perms || ME.perms.includes(perm));
/** The toast when a tier lacks a permission. */
export const cannot = (what: string): string => `Your tier${ME?.tier ? ` (${ME.tier.name})` : ''} cannot ${what}: ask an administrator`;
export const userKey = (k: string): string => `${k}:${ME ? ME.id : 'anon'}`;
export const displayName = (u: { display_name?: string | null; name?: string | null; username?: string | null } | null): string =>
  (u && (u.display_name || u.name || u.username)) || '';

// ---- preferences: read from the copy loaded with the user; writes are merged and sent debounced (one PUT for a burst)
export const pref = (k: string): unknown => PREFS[k];

let pendingPrefs: Prefs = {};
let prefTimer: ReturnType<typeof setTimeout> | undefined;
let prefWaiters: Array<{ ok: () => void; fail: (e: Error) => void }> = [];

/** Set top-level pref keys now (locally) and on the server after `delay` ms; resolves once they are saved. */
export function setPrefs(patch: Prefs, delay = 400): Promise<void> {
  Object.assign(PREFS, patch);
  Object.assign(pendingPrefs, patch);
  if (!ME) return Promise.resolve();                 // signed out: nothing to save to
  clearTimeout(prefTimer);
  prefTimer = setTimeout(() => { void sendPrefs(); }, delay);
  return new Promise((ok, fail) => { prefWaiters.push({ ok, fail }); });
}

/** Fire-and-forget setPrefs for things that are just remembered (a failed save keeps them for this page). */
export function rememberPref(k: string, v: unknown): void { setPrefs({ [k]: v }).catch(() => {}); }

async function sendPrefs(): Promise<void> {
  const body = pendingPrefs, waiters = prefWaiters;
  pendingPrefs = {};
  prefWaiters = [];
  try {
    await sendJSON('PUT', 'api/me/prefs', { prefs: body });
    waiters.forEach((w) => w.ok());
  } catch (e) {
    for (const [k, v] of Object.entries(body)) if (!(k in pendingPrefs)) pendingPrefs[k] = v;   // retried with the next write
    const err = new Error(errText(e));
    waiters.forEach((w) => w.fail(err));
  }
}

// unsent prefs when the page goes away: one keepalive PUT
window.addEventListener('pagehide', () => {
  if (!ME || !Object.keys(pendingPrefs).length) return;
  clearTimeout(prefTimer);
  void apiFetch('api/me/prefs', { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prefs: pendingPrefs }) })
    .catch(() => {});
  pendingPrefs = {};
});

/** Sign out here: POST api/auth/logout, then the sign-in page. */
export async function signOut(): Promise<void> {
  try { await apiFetch('api/auth/logout', { method: 'POST' }); } catch (e) { /* the cookie goes anyway on the next 401 */ }
  location.replace(loginUrl());
}
