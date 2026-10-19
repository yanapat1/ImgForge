// Runs before the first paint (a classic blocking <script> in <head>): applies the saved theme
// (localStorage "theme" = system | light | dark, "look" = default | handdrawn | glass | brutalist | terminal | soft, "palette" = default | dracula | nord | ...)
// so the page never flashes the wrong colours; these are this browser's mirror of the signed-in user's prefs
// (core/auth.ts syncs them from /api/me/prefs once the app is loaded). Then it picks the screen (html[data-screen]):
// 'login' (path ends in /login), 'viz' (path /s/<session id>/), 'label' (?label=<pid>), else the Sessions 'home'.
// An old link ?session=<id> is sent to its s/<id>/ URL. Only the visualizer needs Plotly (~1.5 MB), so only it
// preloads it here, in parallel with the stylesheets. The UI font is preloaded on every screen but the labeling page
// (it paints its canvas first and fetches the font through the stylesheet: a preload there went unused).
// Plotly and the font are found from this script's own URL (static/js/theme-boot.js), never from the page URL:
// under /s/<id>/ a relative 'plotly.min.js' would point into the session's path.
// js/core/theme.js takes over once the app is loaded.
(function () {
  var m = 'system'; try { m = localStorage.getItem('theme') || 'system'; } catch (e) {}
  var look = 'default'; try { look = localStorage.getItem('look') || 'default'; } catch (e) {}
  var pal = 'default'; try { pal = localStorage.getItem('palette') || 'default'; } catch (e) {}
  var t = m === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : m;
  var html = document.documentElement;
  html.setAttribute('data-theme', t);
  html.setAttribute('data-look', look);
  html.setAttribute('data-palette', pal);
  var path = location.pathname;
  var old = /[?&]session=([^&]+)/.exec(location.search);
  if (old && old[1] && !/\/s\/[^/]+\/?$/.test(path) && !/\/login\/?$/.test(path)) {
    // the visualizer moved to s/<id>/ (the server redirects too; this covers a cached page)
    location.replace(path.replace(/[^/]*$/, '') + 's/' + old[1] + '/');
    return;
  }
  var login = /\/login\/?$/.test(path);
  var viz = !login && /\/s\/[^/]+\/$/.test(path);
  var label = !login && !viz && /[?&]label=[^&]/.test(location.search);
  html.setAttribute('data-screen', login ? 'login' : viz ? 'viz' : label ? 'label' : 'home');
  var me = document.currentScript instanceof HTMLScriptElement ? document.currentScript.src : '';
  var base = me || document.baseURI;
  var url = function (rel: string): string { try { return new URL(rel, base).href; } catch (e) { return rel; } };
  if (!label) {
    var f = document.createElement('link');
    f.rel = 'preload'; f.as = 'font'; f.type = 'font/woff2'; f.crossOrigin = 'anonymous';
    f.href = me ? url('../assets/fonts/atkinson-next.woff2') : url('static/assets/fonts/atkinson-next.woff2');
    document.head.appendChild(f);
  }
  if (viz) {
    var l = document.createElement('link');
    l.rel = 'preload'; l.as = 'script'; l.href = me ? url('../../plotly.min.js') : url('plotly.min.js');
    document.head.appendChild(l);
  }
})();
