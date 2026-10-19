# `static/js/` — the page logic (native ES modules)

```
js/
├── theme-boot.js      classic script, runs before the first paint (saved theme, login / home / label / viz screen, Plotly preload)
├── main.js            entry: boot → init → bindEvents; nothing imports it
├── core/              no UI: DOM helpers, API, state, theme, jobs   → core/README.md
├── plot/              the embedding plot and its mouse interactions → plot/README.md
├── views/             the Sessions home, the other panels and controls → views/README.md
└── settings/          the Settings dialog                          → settings/README.md
```

Dependencies mostly point from `main.js` down to `views`, `plot` and `settings`, and from those to
`core`. Two `core` modules call back up: a theme switch re-renders the views, and a finished job
refreshes the feature list. The imports are circular, which is safe here: they are only **function**
declarations, and nothing runs at import time except small constant setup.

## How the code is written

- **One mutable state object** `S` (`core/state.js`): the data, the current view, the selection and the
  picks. Page defaults from `config.yaml` live in `UI`. Modules read and write `S.x` and never keep
  private copies of shared data.
- **Exported `let` bindings are owned by their module.** ES modules let other modules read them live
  but never reassign them. `THEME` and `SCALE` are reassigned only in `core/theme.js`, `hoverDotId` only
  in `plot/marks.js`, and so on. To change one from outside, call the owner's function, for example
  `invalidateStats()`.
- Event handlers are bound once, in `main.js` `bindEvents()` or the module's own `bind*()`. Lists
  (gallery, chips, settings cards) use **event delegation**: one listener on the container plus
  `data-*` attributes. They never attach a listener per item.
- HTML is built with template strings, and **every** interpolated value goes through `esc()`.
- `window.dataviz = { S, UI, THEME, plotAxes }` is the single global, meant for the browser console
  and the Playwright tests.

## Rendering budget (the techniques that matter most)

| Technique | Where | Why |
|---|---|---|
| One WebGL trace per colour | `plot/render.js` | About 10k dots in about 16 `scattergl` traces, and all group bubbles in one trace per colour (null-separated polygons), instead of about 250. |
| `requestAnimationFrame` coalescing | `scheduleRender`, the lasso path, pan, wheel zoom | A burst of input produces at most one redraw per frame. |
| Restyle instead of re-render | `updateHighlight`, `updateMarks` | A selection or hover change touches 1–2 small traces, not the plot. |
| Hover text cache | `hoverOf` / `invalidateHover` | About 10k HTML strings are built once per (scores, groups) state. |
| Server payloads cached per key | `api/*` responses are pre-serialized | The page never asks twice for the same projection while it is open. |

Details are in each folder's README.

## Accounts and sharing

- `core/auth.ts` loads the signed-in user once (`loadMe`, `ME`, `isAdmin`) with their prefs (`pref`, `setPrefs`,
  `rememberPref`: theme / look / palette, `label_settings`, `homeTab`, `homeScope`, PUT `api/me/prefs` debounced).
  `userKey(k)` suffixes every browser-storage key that holds a user's data with the user id.
- `main.ts` boots in this order: wait for the server → on `/login` only `views/login.ts` → `loadMe()` → the screen.
- Roles (`my_role`: viewer < labeler < owner) only hide what the server would refuse anyway: the home cards' actions,
  the label page (`L.readOnly`: no tools, no saves, a "View only" banner), the visualizer (`html[data-role=viewer]`).
