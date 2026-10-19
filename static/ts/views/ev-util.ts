// Typed helpers for DOM event handlers (e.target is only an EventTarget).

/** The closest ancestor (or the target itself) of the event's target that matches `sel`; null when the target is no element. */
export function closestOf<T extends Element = HTMLElement>(e: Event, sel: string): T | null {
  return e.target instanceof Element ? e.target.closest<T>(sel) : null;
}

/** The event's target as an element (null when it is not one). */
export function targetEl<T extends Element = HTMLElement>(e: Event): T | null {
  return e.target instanceof Element ? (e.target as T) : null;
}
