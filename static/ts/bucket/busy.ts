// Short page work for the status bar at the bottom (activity.ts): "Listing s3://…", "Detecting the format…",
// "Loading previews · 18 left". busy(text, promise) counts the request while it runs; it shows only once it has run for
// SHOW_AFTER ms, so quick answers never flash. Background jobs and uploads take the line first; this shows when none run.

const SHOW_AFTER = 250;

interface Entry { text: string; t0: number }

const live = new Map<number, Entry>();
const previews = new Set<HTMLImageElement>();     // thumbnails still loading
let ids = 0, frame = 0;

/** activity.ts sets it: repaint the status line */
export const busyHook = { changed: (): void => {} };

function changed(): void {
  if (!frame) frame = requestAnimationFrame(() => { frame = 0; busyHook.changed(); });
}

/** Show `text` in the status bar while `p` runs (after SHOW_AFTER ms); resolves / rejects as `p` does. */
export function busy<T>(text: string, p: Promise<T>): Promise<T> {
  const id = (ids += 1);
  live.set(id, { text, t0: performance.now() });
  const timer = window.setTimeout(changed, SHOW_AFTER);
  const end = (): void => { clearTimeout(timer); live.delete(id); changed(); };
  p.then(end, end);
  return p;
}

/** A preview image was given its src: it counts as loading until it loads, fails or leaves the page. */
export function previewStarted(img: HTMLImageElement): void {
  if (img.complete) return;
  previews.add(img);
  const end = (): void => { previews.delete(img); changed(); };
  img.addEventListener('load', end, { once: true });
  img.addEventListener('error', end, { once: true });
  changed();
}

/** The page work to show now (the oldest shown request, plus how many more), or null. */
export function busyText(): string | null {
  const now = performance.now();
  const shown = [...live.values()].filter((e) => now - e.t0 >= SHOW_AFTER);
  const first = shown[0];
  const parts: string[] = [];
  if (first) parts.push(first.text + (shown.length > 1 ? ` (+${shown.length - 1})` : ''));
  for (const img of previews) if (!img.isConnected || img.complete) previews.delete(img);   // a replaced grid: gone
  if (previews.size) parts.push(`Loading previews · ${previews.size.toLocaleString('en-US')} left`);
  return parts.length ? parts.join(' · ') : null;
}
