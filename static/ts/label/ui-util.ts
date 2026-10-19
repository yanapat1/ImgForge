// Small typed DOM helpers shared by the label dialogs / panels (event targets are EventTarget, not Element).

/** `target.closest(sel)` for an event target that may be a non-element (text node, document, window). */
export const closestEl = <T extends Element = HTMLElement>(target: EventTarget | null, sel: string): T | null =>
  target instanceof Element ? target.closest<T>(sel) : null;

/** The target if it is an HTMLElement, else null. */
export const asEl = (target: EventTarget | null): HTMLElement | null => (target instanceof HTMLElement ? target : null);

/** Parse a number the user typed; NaN when empty. */
export const numOf = (v: string): number => (v === '' ? NaN : Number(v));
