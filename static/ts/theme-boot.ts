// Runs before the first paint (a classic blocking <script> in <head>): applies the saved theme
// (localStorage "theme" = system | light | dark, "look" = default | handdrawn | glass | brutalist | terminal | soft, "palette" = default | dracula | nord | ...) so the page never flashes the wrong colours, and picks the
// screen: the Sessions home (no ?session=), the visualizer (?session=<id>) or the labeling page (?label=<id>). Only the visualizer needs
// Plotly (~1.5 MB), so only it preloads it here, in parallel with the stylesheets. The UI font is preloaded on the
// home and the visualizer (text on the first paint); the labeling page paints its canvas first and fetches the font
// through the stylesheet (a preload there went unused: a console warning).
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
  var viz = /[?&]session=[^&]/.test(location.search);
  var label = !viz && /[?&]label=[^&]/.test(location.search);
  html.setAttribute('data-screen', viz ? 'viz' : label ? 'label' : 'home');
  if (!label) {
    var f = document.createElement('link');
    f.rel = 'preload'; f.as = 'font'; f.type = 'font/woff2'; f.crossOrigin = 'anonymous';
    f.href = 'static/assets/fonts/atkinson-next.woff2';
    document.head.appendChild(f);
  }
  if (viz) {
    var l = document.createElement('link');
    l.rel = 'preload'; l.as = 'script'; l.href = 'plotly.min.js';
    document.head.appendChild(l);
  }
})();
