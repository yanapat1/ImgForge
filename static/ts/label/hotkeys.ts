// Hotkeys: one capture-phase keydown listener on window. A key press becomes a combo string (state.js format:
// ctrl+alt+shift+key, KeyboardEvent.code for digits / arrows / named keys / punctuation, meta = ctrl), looked up in the merged
// bindings (DEFAULT_HOTKEYS + settings.hotkeys). Order: modal open -> nothing; typing in a field -> only escape and
// ctrl+enter; then the page's pre() (open menu, the active tool, per-class hotkeys), then the action table.
// An action returning false is "not handled": the browser default runs (Enter on a focused button, Tab focus).

import { HOLD_ACTIONS, L, settingsHotkeys } from './state.js';

const CODE_NAMES: Record<string, string> = {
  Enter: 'enter', NumpadEnter: 'enter', Escape: 'escape', Tab: 'tab', Backspace: 'backspace', Delete: 'delete',
  Space: 'space', ArrowLeft: 'arrowleft', ArrowRight: 'arrowright', ArrowUp: 'arrowup', ArrowDown: 'arrowdown',
  Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown', BracketLeft: '[', BracketRight: ']',
  Minus: '-', Equal: '=', NumpadAdd: '+', NumpadSubtract: '-',
  // punctuation by physical key: shift+; is 'shift+;' (e.key would say ':'), the same on every layout
  Backquote: '`', Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/', Backslash: '\\',
};

/** What comboOf reads from a KeyboardEvent (the hold-key lookup passes just code + key). */
export type KeyLike = Pick<KeyboardEvent, 'code' | 'key'> & Partial<Pick<KeyboardEvent, 'ctrlKey' | 'metaKey' | 'altKey' | 'shiftKey'>>;
/** An action: false = not handled (the browser default runs). */
export type HotkeyAction = (e: KeyboardEvent, combo: string) => boolean | void;
export interface HotkeyHandlers {
  actions: Record<string, HotkeyAction>;
  pre: (combo: string, e: KeyboardEvent) => boolean;
  recording: () => boolean;
}

export function comboOf(e: KeyLike): string | null {
  const c = e.code || '';
  let k: string | undefined;
  if (/^Digit\d$/.test(c)) k = c.charAt(5);
  else if (/^Numpad\d$/.test(c)) k = c.charAt(6);
  else if (CODE_NAMES[c] !== undefined) k = CODE_NAMES[c];
  else if (/^[a-z]$/i.test(e.key)) k = e.key.toLowerCase();
  else if (/^Key[A-Z]$/.test(c)) k = c.charAt(3).toLowerCase();          // non-latin layouts, macOS Alt+letter
  else if (/^F\d{1,2}$/.test(e.key)) k = e.key.toLowerCase();
  else if (e.key && e.key.length === 1 && e.key !== ' ') k = e.key.toLowerCase();
  else return null;                                                  // a modifier alone, dead keys
  return `${e.ctrlKey || e.metaKey ? 'ctrl+' : ''}${e.altKey ? 'alt+' : ''}${e.shiftKey ? 'shift+' : ''}${k}`;
}

const MAC = /Mac|iPhone|iPad/.test(navigator.platform || '');
const KEY_TEXT: Record<string, string> = { arrowright: '→', arrowleft: '←', arrowup: '↑', arrowdown: '↓', enter: '↵', escape: 'Esc', backspace: '⌫',
  delete: 'Del', space: 'Space', tab: 'Tab', pageup: 'PgUp', pagedown: 'PgDn', home: 'Home', end: 'End' };
export function prettyCombo(c: string | null | undefined): string {
  if (!c) return '';
  return c.split('+').map((p) => p === 'ctrl' ? (MAC ? '⌘' : 'Ctrl') : p === 'alt' ? (MAC ? '⌥' : 'Alt') : p === 'shift' ? '⇧'
    : KEY_TEXT[p] || p.toUpperCase()).join(MAC ? '' : '+');
}

const THROTTLED = new Set(['next', 'prev', 'next_unlabeled']);
const THROTTLE_MS = 60;
let map = new Map<string, string>();               // combo -> action id
let H: HotkeyHandlers = { actions: {}, pre: () => false, recording: () => false };   // recording: a hotkey recorder takes every key
let lastNav = 0;
export const keys = { space: false, area: false };   // hold keys currently down

export function rebuildHotkeys(): void {
  map = new Map();
  for (const [id, combos] of Object.entries(settingsHotkeys(L.settings))) {
    for (const c of combos || []) if (c && !map.has(c)) map.set(c, id);
  }
}

export const combosFor = (id: string): string[] => settingsHotkeys(L.settings)[id] || [];
export const actionOf = (combo: string): string | undefined => map.get(combo);

const isField = (el: EventTarget | null): boolean => el instanceof HTMLElement && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
const isButton = (el: EventTarget | null): boolean => el instanceof Element && (el.tagName === 'BUTTON' || el.tagName === 'A' || el.getAttribute('role') === 'button');
const modalOpen = (): boolean => !!document.querySelector('.modal:not(.hidden)');

function onKeyDown(e: KeyboardEvent): void {
  if (e.isComposing || modalOpen()) return;
  const combo = comboOf(e);
  if (!combo) return;
  const t = e.target;
  if (combo === 'space' && !isField(t)) {
    if (isButton(t)) return;
    keys.space = true;
    e.preventDefault();
    return;
  }
  if (isField(t) && combo !== 'escape' && combo !== 'ctrl+enter') return;
  // hold key (area_select): matched by its bare key, so Shift / Ctrl may already be down (Shift+Q = touching mode),
  // unless the exact combo is bound to another action (e.g. shift+q): that action wins and the hold key does not engage
  const bare = comboOf({ code: e.code, key: e.key });
  const exact = map.get(combo);
  if (bare && !H.recording() && HOLD_ACTIONS.has(map.get(bare) ?? '') && !(exact && !HOLD_ACTIONS.has(exact))) {
    if (map.get(bare) === 'area_select') keys.area = true;
    e.preventDefault();
    return;
  }
  if (H.pre(combo, e) === true) { e.preventDefault(); e.stopPropagation(); return; }
  const id = map.get(combo);
  const act = id ? H.actions[id] : undefined;
  if (!id || !act) return;
  if (isButton(t) && (combo === 'enter' || combo === 'space')) return;
  if (THROTTLED.has(id)) {
    const now = performance.now();
    e.preventDefault();
    if (now - lastNav < THROTTLE_MS) return;
    lastNav = now;
  }
  if (act(e, combo) === false) return;
  e.preventDefault();
  e.stopPropagation();
}

export function bindHotkeys(handlers: Partial<HotkeyHandlers>): void {
  H = { ...H, ...handlers };
  rebuildHotkeys();
  addEventListener('keydown', onKeyDown, true);
  addEventListener('keyup', (e) => {
    if (e.code === 'Space') keys.space = false;
    // a hold key is released by its bare key, whatever modifiers are down now (Q, then Shift, then let go of Q)
    const bare = comboOf({ code: e.code, key: e.key });
    if (bare && map.get(bare) === 'area_select') keys.area = false;
  }, true);
  addEventListener('blur', () => { keys.space = false; keys.area = false; });
}
