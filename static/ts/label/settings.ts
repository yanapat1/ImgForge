// The labeling page's settings: the user's prefs (/api/me/prefs key label_settings, core/auth.ts) as the source of
// truth, mirrored in localStorage 'label-settings:<user id>', and the 'label-settings-changed' document event
// (Settings › Labeling dispatches it after saving, with detail = the full settings object).

import { pref, rememberPref, userKey } from '../core/auth.js';
import type { LabelSettings } from '../types-label.js';
import { L, SETTINGS_DEFAULTS } from './state.js';

declare global {
  interface DocumentEventMap { 'label-settings-changed': CustomEvent<LabelSettings | null> }
}

type SettingsHandler = (next: LabelSettings, prev: LabelSettings) => void;

export const LABEL_PREF = 'label_settings';
const key = (): string => userKey('label-settings');
let onChange: SettingsHandler = () => {};

// s: a stored / server copy (any JSON, possibly partial or null): defaults fill the gaps
function merged(s: unknown): LabelSettings {
  const p = (typeof s === 'object' && s !== null ? s : {}) as Partial<LabelSettings>;
  return { ...SETTINGS_DEFAULTS, ...p, hotkeys: { ...(p.hotkeys || {}) } };
}

function mirror(s: LabelSettings): void { try { localStorage.setItem(key(), JSON.stringify(s)); } catch (e) { /* private mode */ } }

function apply(s: unknown): void {
  const prev = L.settings;
  L.settings = merged(s);
  mirror(L.settings);
  onChange(L.settings, prev);
}

// the prefs read with the user (main.ts boot awaits loadMe); the mirror when they have none yet / are unreachable
export function initSettings(handler?: SettingsHandler): void {
  onChange = handler || onChange;
  const saved = pref(LABEL_PREF);
  if (saved && typeof saved === 'object') { L.settings = merged(saved); mirror(L.settings); }
  else { try { L.settings = merged(JSON.parse(localStorage.getItem(key()) || 'null') as unknown); } catch (e) { L.settings = merged(null); } }
  document.addEventListener('label-settings-changed', (e) => { if (e.detail) apply(e.detail); });
}

// a change made on the page itself (H, T): applied now, saved debounced
export function setSettings(patch: Partial<LabelSettings>): void {
  apply({ ...L.settings, ...patch });
  rememberPref(LABEL_PREF, L.settings);               // debounced in auth.ts; the mirror keeps it if it fails
}
