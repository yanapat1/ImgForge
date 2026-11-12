// Small formatters and the file download used across the app (one copy each; bucket/state.ts re-exports the
// time ones for its modules). Times are epoch seconds or ms, or a workspace.json "YYYY-MM-DD HH:MM:SS" string.

/** Epoch ms of a time value; NaN when it is missing or unreadable. */
export function toMs(t: number | string | null | undefined): number {
  if (t == null || t === '') return NaN;
  if (typeof t === 'number') return t < 1e12 ? t * 1000 : t;
  return Date.parse(String(t).replace(' ', 'T'));
}

/** "just now", "12 min ago", "3 h ago", "yesterday", then a short date ("Oct 28"); '' for a missing time. */
export function timeAgo(t: number | string | null | undefined): string {
  const ms = toMs(t);
  if (!Number.isFinite(ms)) return '';
  const s = (Date.now() - ms) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return 'yesterday';
  return new Date(ms).toLocaleDateString('en-US', { month: 'short', day: '2-digit' });
}

/** A byte size: "812 B", "4.2 MB", "1.4 TB"; '—' when missing. */
export function fmtSize(b: number | null | undefined): string {
  if (b == null || !Number.isFinite(b)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = b, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v >= 100 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}

/** A count with thousands separators; a missing value counts as 0. */
export const fmtCount = (v: number | null | undefined): string => Number(v || 0).toLocaleString();

/** A 0–1 ratio as a percent with one decimal, without the % sign; '–' when missing. */
export const pct1 = (v: number | null | undefined): string => (v == null ? '–' : (v * 100).toFixed(1));

/** Give the user a file: a Blob or text under `name`. The object URL lives a few seconds (some browsers start the
 *  download after click() returns, so revoking at once can cancel it). */
export function saveFile(data: Blob | string, name: string, type = 'text/plain'): void {
  const blob = typeof data === 'string' ? new Blob([data], { type }) : data;
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
