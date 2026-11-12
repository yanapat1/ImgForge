// The frontend's entry point: the sign-in page (/login), Home (/, the overview), the Image page (?image: the visualize
// and label sessions) and the screens of each zone, each loaded on demand: Bucket (?bucket, js/bucket/), the labeling
// page (?label=<pid>, js/label/), Data (?data, js/data/) and the visualizer of one session (/s/<id>/, js/viz/boot.js). A screen whose zone this
// install leaves out is never imported, so its files need not exist. Loaded by index.html as <script type="module">.

import { initPageDock } from './views/dock-page.js';
import { errText, waitForServer } from './core/api.js';
import { hasImage, loadMe, zoneOn } from './core/auth.js';
import { bucketUrl, dataUrl, homeUrl, imageUrl, sessionIdFromPath } from './core/nav.js';
import { $, $req, esc, icon } from './core/dom.js';
import { showError, installCrashHandlers } from './core/errorpage.js';
import { S, UI } from './core/state.js';
import { applyThemePrefs, THEME } from './core/theme.js';
import { bindSettings } from './settings/dialog.js';
import { initLLM } from './core/llm.js';
import { renderImage } from './views/image.js';
import { moveLocalKeys } from './core/legacy-keys.js';
import { bindUserButton, mountUserMenu } from './views/usermenu.js';
import { bindKeyHelp } from './core/keys.js';


const SCREEN = document.documentElement.dataset['screen'];
const SESSION = SCREEN === 'viz' ? sessionIdFromPath() : null;
const LABEL = SCREEN === 'label' ? new URLSearchParams(location.search).get('label') : null;
const HOME = SCREEN === 'home';
const IMAGE = SCREEN === 'image';
const BUCKET = SCREEN === 'bucket';
const DATA = SCREEN === 'data';
const SHEET = SCREEN === 'ui';

// still loading after 10 s / 30 s: say so, then offer the way out (the overlay itself only appears after 300 ms)
function slowHints(): { stop: () => void } {
  const hint = (txt: string, actions?: boolean): void => {
    const el = $('startup-hint');
    if (el) el.innerHTML = txt + (actions ? ` <a href="${esc(imageUrl('viz'))}">${icon('chev-l')}All sessions</a>` : '');
  };
  const t = [setTimeout(() => hint('Still working… large datasets can take a while.'), 10000),
    setTimeout(() => hint('This is taking longer than usual.', true), 30000)];
  return { stop: () => { t.forEach(clearTimeout); hint(''); } };
}

async function boot(): Promise<void> {
  const hints = slowHints();
  // server starting or restarting: keep the loading page up (it fades in after 300 ms) instead of an error page
  const up = await waitForServer(() => {
    $req('startup-title').textContent = 'Waiting for the server…';
    $req('startup-stage').textContent = 'it may be starting or restarting; this page continues on its own';
  }, 120000, SCREEN === 'login' ? 'api/auth/config' : 'api/status');
  if (!up) { hints.stop(); showError({ kind: 'network' }); return; }
  if (SCREEN === 'login') {                    // signed out: only the sign-in card (no user, no settings)
    hints.stop();
    $req('startup').classList.add('hidden');
    await (await import('./views/login.js')).renderLogin();
    return;
  }
  // who is signed in, and their prefs (theme, labeling settings…): a 401 here is already on its way to /login
  if (!await loadMe()) { hints.stop(); return; }
  applyThemePrefs();
  if (!SESSION) { hints.stop(); $req('startup').classList.add('hidden'); }   // home and labeling paint their own UI
  else { $req('startup-title').textContent = 'Loading visualizer…'; $req('startup-stage').textContent = 'loading plot library and dataset'; }
  bindSettings();
  initLLM();                                   // ◆ AI features: hidden until the server says they are usable
  // the user menu: the Image page's header here, the visualizer's activity bar; the other pages mount their own
  const imageActions = document.querySelector<HTMLElement>('#image .header-actions');
  if (IMAGE && imageActions) mountUserMenu(imageActions);
  // the Image page's header links (index.html): hrefs through core/nav.js (a proxy prefix is kept), a missing zone's link goes
  for (const [id, href, on] of [['image-nav-bucket', bucketUrl(), zoneOn('bucket')], ['image-nav-image', imageUrl(), true],
    ['image-nav-data', dataUrl(), zoneOn('data')], ['image-brand', homeUrl(), true]] as const) {
    const a = $<HTMLAnchorElement>(id);
    if (a && on) a.href = href; else a?.remove();
  }
  if (SESSION) {
    bindUserButton($req<HTMLButtonElement>('act-user'));
    $req<HTMLAnchorElement>('brand-home').href = imageUrl('viz');
  }
  // the labeling page loads its own code on demand: the home and the visualizer never download it
  if (LABEL) initPageDock('label', LABEL);      // ◆ the right-side dock (the Bucket assistant)
  else if (IMAGE) initPageDock('image');
  if (LABEL) { import('./label/app.js').then((m) => m.start(LABEL)).catch((e: unknown) => {
    $req('label').innerHTML = `<div class="status-line err">${icon('alert')}Could not load the labeling page: ${esc(errText(e))}</div>`;
  }); return; }
  // the Bucket page loads its own code on demand too (it mounts its own header, user menu included)
  if (BUCKET) { import('./bucket/app.js').then((m) => m.start()).catch((e: unknown) => {
    $req('bucket').innerHTML = `<div class="status-line err">${icon('alert')}Could not load the Bucket page: ${esc(errText(e))}</div>`;
  }); return; }
  if (DATA) { import('./data/app.js').then((m) => m.start()).catch((e: unknown) => {
    $req('data').innerHTML = `<div class="status-line err">${icon('alert')}Could not load Data: ${esc(errText(e))}</div>`;
  }); return; }
  if (SHEET) { import('./ui/sheet.js').then((m) => m.start()).catch((e: unknown) => {
    $req('ui-sheet').innerHTML = `<div class="status-line err">${icon('alert')}Could not load the UI sheet: ${esc(errText(e))}</div>`;
  }); return; }
  if (HOME) { import('./views/home.js').then((m) => m.renderHome()).catch((e: unknown) => {
    $req('home').innerHTML = `<div class="status-line err">${icon('alert')}Could not load Home: ${esc(errText(e))}</div>`;
  }); return; }
  if (IMAGE && !hasImage()) { location.replace(homeUrl()); return; }   // no visualize or label zone: nothing to list
  if (IMAGE) { void renderImage(); return; }      // no Plotly, no job polling
  if (!SESSION) return;
  let viz: typeof import('./viz/boot.js');
  try { viz = await import('./viz/boot.js'); }      // the visualizer's code: only this screen downloads it
  catch (e) { hints.stop(); showError({ kind: 'session', message: `The visualizer could not load: ${errText(e)}` }); return; }
  await viz.startViz(SESSION, hints);
}

// modules keep everything private: this one handle is for the browser console and the UI tests (viz/boot.ts adds plotAxes)
window.dataviz = { S, UI, get THEME() { return THEME; } };

moveLocalKeys();                                   // this browser's settings under their old names
installCrashHandlers();
bindKeyHelp();                                     // "?" opens the shortcut sheet on every screen
// "Skip to the main content" (the first Tab stop on every screen): focus the visible main region
document.addEventListener('click', (e) => {
  if (!(e.target instanceof Element) || !e.target.closest('[data-skip]')) return;
  e.preventDefault();
  const main = [...document.querySelectorAll<HTMLElement>('main, [role="main"]')].find((m) => m.getClientRects().length > 0);
  if (!main) return;
  if (!main.hasAttribute('tabindex')) main.tabIndex = -1;
  main.focus();
});
boot().catch((e: unknown) => showError({ kind: 'crash', message: errText(e), detail: e instanceof Error && e.stack ? e.stack : String(e) }));
