# `static/` — the page

Plain HTML + CSS + native ES modules, served as-is by `dataviz/server` (no bundler, no build step:
edit, reload). Plotly is the only library; the server serves the copy from the Python `plotly`
package at `/plotly.min.js`.

```
static/
├── index.html          markup only: header, activity bar, panels, plot, modal, settings dialog
├── css/                7 stylesheets, linked in cascade order        → css/README.md
├── js/                 ES modules; main.js is the entry              → js/README.md
└── assets/             icons.svg sprite, fonts                       → assets/README.md
```

## Load order (`index.html` `<head>`)

1. `js/theme-boot.js`, a **classic blocking** script: sets `data-theme` before the first paint, so the page
   never flashes the wrong theme.
2. An inline `<style>` with just the page background and icon size: painted even before the
   stylesheets arrive.
3. `css/tokens → base → layout → components → plot → settings → responsive`. They are separate
   `<link>`s (parallel downloads; `@import` would be serial), and later files win ties.
4. `js/main.js` as `type="module"` (deferred by default).
5. `plotly.min.js` (about 1.5 MB) **only on the visualizer screen** (`/s/<id>/`): `theme-boot.js` adds a
   `<link rel="preload">` for it, and `main.js` runs it before the first plot. The Sessions home never
   downloads it. Both find it from their own script URL, never from the page URL.

## Five screens, one page

`index.html` holds them all: the **sign-in page** (`#login`, path `/login`), the **Sessions home** (`#home`, `/`),
the **labeling page** (`#label`, `/?label=<pid>`), the **Data Center** (`#datacenter`, `/?data[=<bucket>&path=…]`) and the **visualizer** (everything marked `.viz-only`,
`/s/<session id>/`). `theme-boot.js` sets `html[data-screen=login|home|label|data|viz]` before the first paint, so the
other screens never flash, and sends an old `?session=<id>` link to `s/<id>/`. The server renders the same
`index.html` for `/login` and for `/s/<id>/` (there with `../../` in front of every asset URL); every link the
page builds goes through `js/core/nav.js` (`homeUrl`, `vizUrl`, `labelUrl`, `dataUrl`, `loginUrl`), so a proxy prefix
(`/user/x/proxy/5151/`) is kept. Every request goes through `js/core/api.js` `apiFetch` (header
`X-Requested-With: dataviz`; a 401 goes to the sign-in page with `next=`).

## Caching

The server sends every static file with `Cache-Control: no-cache`, so the browser revalidates each
file through its ETag (a 304 when unchanged) and never runs stale code after an edit. The `?v=`
query on the entry files is an extra nudge for big changes.

**Never import `main.js` from another module.** `index.html` loads it as `main.js?v=10`, and an
`import … from '../main.js'` is a *different URL*, which the browser treats as a second module
instance. That instance runs `boot()` again and binds every listener twice (this happened once: the
middle-drag moved the lasso twice as far). Shared functions live in `js/core` or `js/views`.

## Conventions

- Markup has ids for everything JS touches. JS finds elements with `$('id')` and never by class.
- Icons are `<svg class="ico"><use href="static/assets/icons.svg#i-name"/></svg>`, or `icon('name')`
  in JS.
- Colours exist only as CSS tokens. JS reads the ones Plotly needs from `css/tokens.css`
  (`js/core/theme.js` `readTheme`), so the plot always matches the chrome.
