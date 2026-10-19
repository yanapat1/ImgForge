# `static/js/core/` — foundations (no UI of their own)

| File | Main exports |
|---|---|
| `dom.js` | `$` (getElementById), `esc` (HTML escape), `icon(name)` + `ICONS` (sprite URL), `rgba`, `mixHex` (blend two colours), `fmtEta`, `sleep`, `syncRanges` (slider fill). No app state. |
| `api.js` | `api`, `getJSON`, `postJSON` (non-2xx → `Error("path: status body")`), `errText` (the server's `detail` message from such an error). |
| `state.js` | `S` (the page state), `UI` (defaults from `config.yaml ui`), `featureLabel`. |
| `theme.js` | `THEME` (CSS tokens read for Plotly), `SCALE` (the current theme's colour scales), `CONTINUOUS` (colour-by fields), `plotBase`, `clsColor` / `clsName` (class colour from the theme's palette), `setTheme` / `themeMode`, `setPalette` / `paletteName` / `PALETTES` (named colour palettes; the colour-blind ones swap in the Okabe-Ito class and error colours). |
| `jobs.js` | `pollJobs` (the bar under the header), `waitJob`, `showWait` / `hideWait` (the plot overlay), `setBar`. |

## How it is written

- `dom.js`, `api.js` and `state.js` import nothing: pure helpers, safe to import anywhere.
- `theme.js` and `jobs.js` are the two exceptions that reach *up*: a theme switch re-renders the views,
  and a finished job refreshes the feature list (`views/controls.js`). That is fine for function
  imports; keep anything that runs at import time out of them.
- `S` is a plain object. Its fields are documented where it is declared. Add a field there before
  using it anywhere else.

## Techniques

- **Theme switch without a reload.** `applyTheme()` sets `data-theme`, re-reads the tokens into `THEME`,
  swaps `SCALE`, picks the light or dark class palette, then re-renders the plot, gallery, class chips,
  stats and the settings dialog. A `matchMedia` listener follows the OS when the mode is `system`.
- **Colour scales keep contrast.** The low end of every scale stays at least 3:1 against the plot
  background in both themes, so no dot disappears.
- **Adaptive polling.** `pollJobs` checks every 600 ms while a job runs and every 2.5 s when idle.
  When the last job finishes it refreshes `/api/status`, which updates the ✓ marks.
- **ETA and rate text.** These come from the server (`eta`, `extra`). The page only formats them, so
  the terminal and the page always agree.
