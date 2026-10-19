// The labeling page's settings: localStorage 'label-settings' for the first paint, GET/PUT /api/label/settings
// as the source of truth, and the 'label-settings-changed' document event (Settings › Labeling dispatches it after
// saving, with detail = the full settings object).

import { api, getJSON } from '../core/api.js';
import type { LabelSettings } from '../types-label.js';
import { L, SETTINGS_DEFAULTS } from './state.js';

declare global {
  interface DocumentEventMap { 'label-settings-changed': CustomEvent<LabelSettings | null> }
}

type SettingsHandler = (next: LabelSettings, prev: LabelSettings) => void;

const KEY = 'label-settings';
let onChange: SettingsHandler = () => {};
let saveTimer: ReturnType<typeof setTimeout> | undefined;

// s: a stored / server copy (any JSON, possibly partial or null): defaults fill the gaps
function merged(s: unknown): LabelSettings {
  const p = (typeof s === 'object' && s !== null ? s : {}) as Partial<LabelSettings>;
  return { ...SETTINGS_DEFAULTS, ...p, hotkeys: { ...(p.hotkeys || {}) } };
}

function mirror(s: LabelSettings): void { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* private mode */ } }

function apply(s: unknown): void {
  const prev = L.settings;
  L.settings = merged(s);
  mirror(L.settings);
  onChange(L.settings, prev);
}

// sync: the mirrored copy (first paint); then the server copy replaces it when it arrives
export function initSettings(handler?: SettingsHandler): void {
  onChange = handler || onChange;
  try { L.settings = merged(JSON.parse(localStorage.getItem(KEY) || 'null') as unknown); } catch (e) { L.settings = merged(null); }
  document.addEventListener('label-settings-changed', (e) => { if (e.detail) apply(e.detail); });
  getJSON<unknown>('api/label/settings').then((s) => {
    if (JSON.stringify(merged(s)) !== JSON.stringify(L.settings)) apply(s);
  }).catch(() => { /* offline: keep the mirror */ });
}

// a change made on the page itself (H, T): applied now, saved debounced
export function setSettings(patch: Partial<LabelSettings>): void {
  apply({ ...L.settings, ...patch });
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api('api/label/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(L.settings) })
      .catch(() => { /* the mirror keeps it for this browser */ });
  }, 400);
}
