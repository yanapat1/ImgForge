# UI sheet

The live UI sheet is a page of the app: open **`?ui`** (Settings › Appearance › Open the UI sheet). It draws every
shared part with the real stylesheets, in any theme, look and palette (the switches at its top only preview; they do
not change your settings). Code: `static/ts/ui/sheet.ts`; styles of the parts: `static/css/kit.css` plus the shared
rules in `base.css`, `layout.css`, `components.css` and `settings.css`.

## Rules

1. A screen is built from the parts on the sheet. Its own stylesheet (e.g. `analysis.css`, `datacenter.css`) lays the
   page out (grid, panes, sizes of its areas); it does not restyle buttons, inputs, tabs, pills or tables.
2. A part a screen needs that is not on the sheet is added to `kit.css` and to the sheet first, then used.
3. Every page header is `appHeader()` (`static/ts/core/header.ts`): the Dataset Visualizer brand, the page links,
   settings and theme. The home's copy in `index.html` has the same markup.
4. Colours, sizes, spacing, radii and shadows are tokens (`tokens.css`); nothing is written by hand.
5. Behaviour hooks (`data-act`, ids) stay separate from look (classes), so a restyle never breaks a test or a handler.

## Parts

Page header · activity bar · side panel · buttons (default, primary, ghost, danger, sm, icon, icon xs) · segmented
choice · inputs (select, text, number, range, check, labelled field) · tabs (page and panel size) · toolbar · pills,
chips, badges, count badge · feedback (status line, hint line, notice, dialog, toast, Undo toast) · tables (class table,
list table, data grid) · totals strip, tiles, empty state · foundations (type scale, spacing, radius, shadow, colour).
