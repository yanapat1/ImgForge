// Trackpad, touch and Safari gestures, shared by the plot (and the label canvas): which kind a wheel event
// is (mouse notch = zoom, two-finger trackpad scroll = pan, pinch = ctrl+wheel), two-finger touch pan / pinch,
// and Safari's own gesture events. The "Scroll wheel" choice (Settings › Appearance) is kept per browser.

/** The user's "Scroll wheel" choice. */
export type WheelMode = 'auto' | 'zoom' | 'pan';
/** What a wheel event means: a trackpad/Ctrl pinch, or (per the mode) zoom / pan. */
export type WheelKind = 'pinch' | 'zoom' | 'pan';

const WHEEL_KEY = 'wheel-mode';
export const WHEEL_MODES: ReadonlyArray<readonly [WheelMode, string, string]> = [['auto', 'Auto', 'mouse wheel zooms, two-finger trackpad scroll pans'],
  ['zoom', 'Zoom', 'every scroll zooms (pinch still zooms)'], ['pan', 'Pan', 'every scroll pans (pinch or Ctrl+wheel zooms)']];
const asMode = (m: unknown): WheelMode | null => WHEEL_MODES.find(([k]) => k === m)?.[0] ?? null;

let mode: WheelMode | null = null;                                 // cached: read once, not on every wheel event

export function wheelMode(): WheelMode {
  if (mode) return mode;
  mode = 'auto';
  try { mode = asMode(localStorage.getItem(WHEEL_KEY)) ?? 'auto'; } catch (e) { /* private window */ }
  return mode;
}

export function setWheelMode(m: string | null | undefined): void {
  mode = asMode(m) ?? 'auto';
  try { localStorage.setItem(WHEEL_KEY, mode); } catch (e) { /* private window */ }
}

window.addEventListener('storage', (e: StorageEvent) => { if (e.key === WHEEL_KEY) mode = null; });   // changed in another tab

// The Settings › Appearance block: three buttons, data-act="wheel" (settings/dialog.js runs the action)
export function wheelModeHTML(): string {
  const cur = wheelMode();
  return '<section><div class="form-card"><div class="step-head">Scroll wheel on the plot</div>' +
    '<div class="segmented" role="group" aria-label="Scroll wheel">' + WHEEL_MODES.map(([k, label, hint]) =>
      `<button class="view-btn${k === cur ? ' active' : ''}" data-act="wheel" data-mode="${k}" aria-pressed="${k === cur}" title="${hint}">${label}</button>`).join('') +
    '</div><p class="sec-note">Auto tells a mouse wheel (zoom) from a two-finger trackpad scroll (pan) by its steps. ' +
    'Pinch, or Ctrl+wheel, always zooms.</p></div></section>';
}

// A mouse notch is a big whole step on one axis (100 or 120 px, or that over devicePixelRatio). A trackpad sends
// small or fractional steps, often with deltaX. One stream of events (no 250 ms gap) keeps its kind, so a fast
// swipe's big momentum steps do not flip to zoom halfway.
let trackpad = false, lastAt = 0;

export function classifyWheel(e: WheelEvent): WheelKind {
  if (e.ctrlKey) return 'pinch';                 // trackpad pinch (Chrome, Firefox, Edge) or Ctrl+wheel
  const now = e.timeStamp || performance.now(), gap = now - lastAt;
  lastAt = now;
  if (e.deltaMode !== 0) trackpad = false;       // lines / pages: a mouse
  else {
    const dpr = window.devicePixelRatio || 1, ay = Math.abs(e.deltaY) * dpr;
    const whole = (v: number): boolean => Math.abs(v - Math.round(v)) < 0.01;
    const notch = e.deltaX === 0 && ay >= 99.5 && whole(ay);
    if (e.deltaX !== 0 || !whole(e.deltaY * dpr) || Math.abs(e.deltaY) < 40) trackpad = true;
    else if (notch && (gap > 250 || !trackpad)) trackpad = false;
  }
  const m = wheelMode();
  if (m !== 'auto') return m;
  return trackpad ? 'pan' : 'zoom';
}

// Safari (macOS trackpad, iOS) pinches through its own gesture events, not ctrl+wheel
let safariGesture = false;
export const isGesturing = (): boolean => safariGesture;

// Two touch pointers: centroid delta -> onPan(dx, dy), distance ratio -> onPinch(scale, cx, cy) (scale > 1: fingers
// spread). One finger is left alone (the plot's lasso). onRelease runs when the last finger of a two-finger
// gesture (or a Safari gesture) lifts: the taps the browser may still send after it are not clicks. Returns an
// unbind function.
/** Callbacks of bindTouch (all but onPan / onPinch optional). */
export interface TouchHandlers {
  onStart?: () => void;
  onPan: (dx: number, dy: number) => void;
  onPinch: (scale: number, cx: number, cy: number) => void;
  onEnd?: () => void;
  onRelease?: () => void;
}
/** Safari's non-standard gesture event. */
interface SafariGestureEvent extends UIEvent { scale: number; clientX: number; clientY: number }

export function bindTouch(el: HTMLElement, { onStart, onPan, onPinch, onEnd, onRelease }: TouchHandlers): () => void {
  const pts = new Map<number, { x: number; y: number }>();
  let prev: { cx: number; cy: number; d: number } | null = null;                               // { cx, cy, d } of the last two-finger frame
  let multi = false;                             // a two-finger gesture ran since the first finger went down
  const two = (): { cx: number; cy: number; d: number } => {
    const [a, b] = [...pts.values()] as [{ x: number; y: number }, { x: number; y: number }];
    return { cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2, d: Math.hypot(a.x - b.x, a.y - b.y) };
  };
  const down = (e: PointerEvent): void => {
    if (e.pointerType !== 'touch') return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) { prev = two(); multi = true; if (onStart) onStart(); }
  };
  const move = (e: PointerEvent): void => {
    const p = pts.get(e.pointerId);
    if (!p) return;
    p.x = e.clientX; p.y = e.clientY;
    if (pts.size !== 2 || !prev) return;
    e.preventDefault();
    e.stopPropagation();
    const cur = two();
    if (cur.cx !== prev.cx || cur.cy !== prev.cy) onPan(cur.cx - prev.cx, cur.cy - prev.cy);
    if (prev.d > 0 && cur.d > 0 && cur.d !== prev.d) onPinch(cur.d / prev.d, cur.cx, cur.cy);
    prev = cur;
  };
  const up = (e: PointerEvent): void => {
    if (!pts.delete(e.pointerId)) return;
    if (prev && pts.size < 2) { prev = null; if (onEnd) onEnd(); }
    if (multi && !pts.size) { multi = false; if (onRelease) onRelease(); }
  };
  let gScale = 1;
  const gStart = (e: Event): void => { e.preventDefault(); if (pts.size) return; safariGesture = true; gScale = 1; if (onStart) onStart(); };
  const gChange = (ev: Event): void => {
    const e = ev as SafariGestureEvent;
    e.preventDefault();
    if (!safariGesture || !e.scale) return;
    onPinch(e.scale / gScale, e.clientX, e.clientY);
    gScale = e.scale;
  };
  const gEnd = (e: Event): void => {
    e.preventDefault();
    if (!safariGesture) return;
    safariGesture = false;
    if (onEnd) onEnd();
    if (onRelease) onRelease();
  };
  const opt = { passive: false, capture: true };
  el.addEventListener('pointerdown', down, true);
  el.addEventListener('pointermove', move, opt);
  el.addEventListener('pointerup', up, true);
  el.addEventListener('pointercancel', up, true);
  el.addEventListener('gesturestart', gStart, opt);
  el.addEventListener('gesturechange', gChange, opt);
  el.addEventListener('gestureend', gEnd, opt);
  return () => {
    el.removeEventListener('pointerdown', down, true);
    el.removeEventListener('pointermove', move, opt);
    el.removeEventListener('pointerup', up, true);
    el.removeEventListener('pointercancel', up, true);
    el.removeEventListener('gesturestart', gStart, opt);
    el.removeEventListener('gesturechange', gChange, opt);
    el.removeEventListener('gestureend', gEnd, opt);
  };
}
