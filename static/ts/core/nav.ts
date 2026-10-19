// The page's URLs: the app root (any proxy prefix kept), the visualizer of a session (s/<id>/), the labeling page
// (?label=<pid>), the Sessions home and the sign-in page. Every navigation goes through these, never through a
// hard-coded './': under s/<id>/ a relative './' is the visualizer itself, not the home.
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

/** The visualize session id of a s/<id>/ page (null on the other screens). */
export function sessionIdFromPath(): string | null {
  const m = location.pathname.match(VIZ_PATH);
  if (!m || m[1] === undefined) return null;
  try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
}

export const homeUrl = (): string => appRoot();
export const vizUrl = (id: string): string => `${appRoot()}s/${encodeURIComponent(id)}/`;
export const labelUrl = (pid: string): string => `${appRoot()}?label=${encodeURIComponent(pid)}`;

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
