# `static/css/` — stylesheets

The files, linked in this order by `index.html`. On a specificity tie the later file wins, so a file
may override earlier ones but never the other way round.

| File | Contents |
|---|---|
| `tokens.css` | `@font-face` plus **every design token**: surfaces, text, roles (accent, danger, highlight), type scale, 4px spacing grid, radii, shadows, z-layers, motion, layout sizes. `:root` holds the dark theme (the default) and `:root[data-theme="light"]` overrides the colours. |
| `base.css` | Reset, typography helpers (`.muted`, `.num`, `.hint`), duotone icons, scrollbars, and every form control: buttons (primary / ghost / icon), selects, sliders (`--fill`), checkboxes, segmented groups, toggle buttons. Also `.panel` and progress bars. |
| `layout.css` | Header, jobs bar, startup screen, the 4-column grid (`activity | left | splitter | main`), activity bar, splitter, class-stats side panel. |
| `components.css` | Gallery cards (badges, picked / outlier states, pick numbers), pairs, pager, chips, hover tip, full-image modal; avatars (`.avatar`, `.avatar-stack`, `.owner-chip`, colours `--av-1…8` in tokens.css), the user menu, the small dialog (`.ui-dlg`) and the share dialog. |
| `plot.css` | Control groups, Groups popover, plot area, **lasso layer**, pan / lasso cursors, Plotly modebar, stats grid. |
| `settings.css` | Settings dialog (also Account and Users & admin: `.acc-*`, `.adm-table`, `.audit-list`): nav, dataset / connection cards, tiles, step forms, folder browser, load-mode radio cards, theme choice, plus its own narrow-screen rules. |
| `assistant.css` | The ◆ AI assistant panel (`#ai-side`: header, context chips, chat, Markdown, verdict cards, composer + mic) and the small ◆ hooks elsewhere (filter bar *Describe…*, Evaluation *Explain*, gallery *Check labels*). `#ai-side` is shown only while `main[data-tab="ai"]`. |
| `handdrawn.css`, `glass.css`, `brutalist.css`, `terminal.css`, `soft.css` | The **looks** (`data-look` on `<html>`, Settings > Appearance > Style). Each look's tokens (font, radii, shadows, a dark and a light colour block) live in `tokens.css`; these files add the component details (borders, press states, inputs, dialogs). Brutalist = square, 2px ink borders, hard offset shadows; Terminal = monospace, hairlines, no shadows; Soft = clay, pill buttons, paired light/dark shadows (`--sf-dark` / `--sf-light`). A new look needs: token blocks, its CSS file linked before `palettes.css`, an entry in `LOOKS` (`js/core/theme.js`), a `styleChoice` in `js/settings/forms.js`, a `.swatch.<look>` in `settings.css` and its first-paint colours in the inline `<style>` of `index.html`. |
| `palettes.css` | Colour palettes (`data-palette` on `<html>`: dracula, nord, solarized, cbsafe, contrast, sepia, dusk, mono, paper), each with a dark and a light block of the same tokens. Loads after the looks, so it wins on a tie. Picked in Settings > Appearance > Palette. |
| `responsive.css` | Breakpoints (1280 / 1100 / 900 / 600 px) and `prefers-reduced-motion`. It loads last so it wins. |

## How the CSS is written

- **Tokens only.** No colour literal outside `tokens.css`, except a few theme-specific fixes marked
  with `:root:not([data-theme="light"])`. A new colour means a new token in **both** themes.
- Semantic token names (`--surface-2`, `--accent-soft`, `--danger-text`), not visual ones. The light
  theme redefines the same names, so components need no theme-specific rules.
- Small things are less round than big things (`--r-sm` 5 → `--r-lg` 12). Spacing comes from the 4px
  scale (`--s1`…`--s6`).
- One block per component, starting with a `/* ---- name */` comment. State classes are `.active`,
  `.picked`, `.sel`, `.locked`, `.off`, `.hidden` (the only `!important`, so it always hides).
- Element ids are used for one-off layout (`#plot-wrap`) and classes for anything reusable.

## Techniques

- **Contrast budget.** `--text` / `--text-muted` reach at least 5.4:1, `--text-faint` at least 4.5:1, and
  every class colour reaches at least 4.3:1 on `--plot-bg`. The light palette (`config.yaml ui.palette_light`) was
  derived by darkening each dark-theme hue in HLS until it reached 4.3:1 on the light plot background.
- **Inline SVG data-URIs as tokens** (`--chev`, `--tick`), with per-theme stroke colours, for the select
  arrow and checkbox tick. They need no extra requests.
- **Pan performance.** `#plot.panning` gets `will-change: transform` on the WebGL and SVG layers, and
  annotations fade during the drag (they cannot be translated with the plot).
- **Marching-ants lasso.** The outline animates its `stroke-dashoffset` while drawing, and
  `vector-effect: non-scaling-stroke` keeps the line crisp.
- `:has()` is avoided for state that must repaint reliably. The load-mode cards use a `.sel` class set
  from JS, because `:has(input:checked)` did not always repaint.
