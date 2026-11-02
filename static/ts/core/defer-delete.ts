// Delete with Undo: the item leaves the list at once and an action toast offers Undo for a few seconds; the DELETE is
// sent only when that time is up (or right away, with keepalive, when the page is closed first). Used for removing
// sessions, labeling sessions, models and connections, which had no way back.

import { api, errText, XRW } from './api.js';
import { actionToast, toast } from './toast.js';

const WAIT = 8000;
const pending = new Map<string, { timer: ReturnType<typeof setTimeout>; url: string }>();
let bound = false;

// the page is closing: what is still waiting is sent now (the user saw it go and did not undo)
function flushOnHide(): void {
  for (const [k, p] of pending) {
    clearTimeout(p.timer);
    void fetch(p.url, { method: 'DELETE', keepalive: true, credentials: 'same-origin', headers: { 'X-Requested-With': XRW } }).catch(() => {});
    pending.delete(k);
  }
}

export interface DeferDelete {
  /** the toast text, e.g. Removed “cats” */
  title: string;
  /** the DELETE request */
  url: string;
  /** take the item out of the page now */
  hide: () => void;
  /** Undo (or a failed DELETE): show it again */
  restore: () => void | Promise<void>;
  /** after the server deleted it (reload lists, notify other views) */
  after?: () => void | Promise<void>;
}

export function deferDelete(o: DeferDelete): void {
  if (!bound) { bound = true; addEventListener('pagehide', flushOnHide); }
  const prev = pending.get(o.url);
  if (prev) clearTimeout(prev.timer);
  o.hide();
  const timer = setTimeout(() => {
    pending.delete(o.url);
    api(o.url, { method: 'DELETE' })
      .then(() => o.after?.())
      .catch(async (e: unknown) => { toast(`${o.title} failed: ${errText(e)}`, 'err'); await o.restore(); });
  }, WAIT);
  pending.set(o.url, { timer, url: o.url });
  actionToast({
    title: o.title, kind: 'ok', timeout: WAIT,
    actions: [{ label: 'Undo', icon: 'undo', run: async () => {
      const p = pending.get(o.url);
      if (!p) return;                                // already sent
      clearTimeout(p.timer);
      pending.delete(o.url);
      await o.restore();
    } }],
  });
}
