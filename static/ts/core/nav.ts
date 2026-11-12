// The page's URLs: the app root = Home (any proxy prefix kept), Bucket (?bucket[=<bucket>&path=…]), Image (the
// sessions: ?image[=viz|label]), Data (?data[=<session>]), the visualizer of a session (s/<id>/), the labeling page
// (?label=<pid>) and the sign-in page. Every navigation
// goes through these, never through a hard-coded './': under s/<id>/ a relative './' is the visualizer itself.
// No imports: theme-boot.ts repeats the two path rules (it is a classic script).

const VIZ_PATH = /\/s\/([^/]+)\/?$/;
const LOGIN_PATH = /\/login\/?$/;

/** The app root as an absolute path ending in '/', e.g. '/' or '/user/x/proxy/5151/'. */
export function appRoot(): string {
  const p = location.pathname;
  if (VIZ_PATH.test(p)) return p.replace(VIZ_PATH, '/');
  if (LOGIN_PATH.test(p)) return p.replace(LOGIN_PATH, '/');
  return p.replace(/[^/]*$/, '');
}

function vizSegment(): string | null {
  const m = location.pathname.match(VIZ_PATH);
  if (!m || m[1] === undefined) return null;
  try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
}

/** The visualize session id of a s/<id>/ (or s/<id>@images/) page (null on the other screens). */
export function sessionIdFromPath(): string | null {
  const seg = vizSegment();
  return seg === null ? null : seg.split('@')[0] ?? seg;
}

/** The point layer of the page: 'images' on s/<id>@images/ (a labelled session shown one point per image). */
export function pointsLayerFromPath(): 'images' | null {
  return vizSegment()?.endsWith('@images') ? 'images' : null;
}

/** Home: the overview of Bucket, Image and Data. */
export const homeUrl = (): string => appRoot();
/** Image: the visualize and label sessions, on one tab (default: the last one used) and with `new` its New dialog. */
export function imageUrl(tab?: 'viz' | 'label' | null, opts?: { new?: boolean }): string {
  return `${appRoot()}?image${tab ? `=${tab}` : ''}${opts?.new ? '&new=1' : ''}`;
}
export const vizUrl = (id: string, layer?: 'images' | null): string =>
  `${appRoot()}s/${encodeURIComponent(id)}${layer ? `@${layer}` : ''}/`;
export const labelUrl = (pid: string): string => `${appRoot()}?label=${encodeURIComponent(pid)}`;

/** Bucket: all buckets (no bucket), one bucket's root, or a folder inside it. */
export function bucketUrl(bucket?: string | null, path?: string | null): string {
  if (!bucket) return `${appRoot()}?bucket`;
  return `${appRoot()}?bucket=${encodeURIComponent(bucket)}${path ? `&path=${encodeURIComponent(path)}` : ''}`;
}

/** Data: the last session, or this one. */
export const dataUrl = (session?: string): string => `${appRoot()}?data${session ? `=${encodeURIComponent(session)}` : ''}`;
/** The UI sheet: every shared part of the page, drawn live (ui/sheet.ts). */
export const uiUrl = (): string => `${appRoot()}?ui`;
/** The sign-in page; `next` (a path of this app) is where it goes after signing in. */
export const loginUrl = (next?: string): string => `${appRoot()}login${next ? `?next=${encodeURIComponent(next)}` : ''}`;

export const onLoginPage = (): boolean => LOGIN_PATH.test(location.pathname);

/** A `next` value made safe: a URL of this app (same origin, under the app root), else the home. */
export function safeNext(next: string | null | undefined): string {
  if (!next) return homeUrl();
  try {
    const u = new URL(next, location.origin + appRoot());
    // a pathname like //evil.com would be read back as a protocol-relative URL by location.replace
    if (u.origin !== location.origin || /^\/[/\\]/.test(u.pathname) || !u.pathname.startsWith(appRoot())
        || LOGIN_PATH.test(u.pathname)) return homeUrl();
    return u.pathname + u.search + u.hash;
  } catch (e) { return homeUrl(); }
}
