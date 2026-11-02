# `static/js/core/` — foundations (no UI of their own)

| File | Main exports |
|---|---|
| `dom.js` | `$` (getElementById), `esc` (HTML escape), `icon(name)` + `ICONS` (sprite URL), `rgba`, `mixHex` (blend two colours), `fmtEta`, `sleep`, `syncRanges` (slider fill), `isTypingTarget` (global keys skip inputs). No app state. |
| `keys.js` | The **?** shortcut sheet: one list of each screen's keys (`registerKeys` adds the label page's mapped hotkeys), `bindKeyHelp` (main.ts). |
| `defer-delete.js` | `deferDelete({title, url, hide, restore, after})`: remove with Undo: the item goes at once, the DELETE is sent after 8 s (or with keepalive when the page closes). Sessions, labeling sessions, models, connections. |
| `format.js` | The one copy of the small formatters: `timeAgo` / `toMs` (Data Center re-exports them), `fmtCount`, `pct1`, and `saveFile(blob or text, name)` (every download). |
| `api.js` | `apiFetch` (every request: `X-Requested-With` header, cookie, 401 → sign-in page once), `api`, `getJSON`, `postJSON`, `sendJSON` (non-2xx → `Error("path: status body")`), `isStatus`, `errText` (the server's `detail` message from such an error), `postXHR` (a POST with upload progress: both uploaders use it). |
| `nav.js` | The page's URLs: `appRoot`, `homeUrl`, `vizUrl(id)` (`s/<id>/`), `labelUrl(pid)`, `dataUrl(bucket?, path?)` (`?data…`), `loginUrl(next)`, `sessionIdFromPath`, `safeNext`. No imports. |
| `auth.js` | The signed-in user (`loadMe`, `ME`, `setMe`, `isAdmin`), their prefs (`pref`, `setPrefs`, `rememberPref`), `userKey`, `signOut`. |
| `avatar.js` | `avatarHTML` (picture or initials on a colour hashed from the id), `avatarStackHTML` (overlapping, `+N`), `ownerChipHTML`. |
| `state.js` | `S` (the page state), `UI` (defaults from `config.yaml ui`), `featureLabel`. |
| `theme.js` | `THEME` (CSS tokens read for Plotly), `SCALE` (the current theme's colour scales), `CONTINUOUS` (colour-by fields), `plotBase`, `clsColor` / `clsName` (class colour from the theme's palette), `setTheme` / `themeMode`, `setPalette` / `paletteName` / `PALETTES` (named colour palettes; the colour-blind ones swap in the Okabe-Ito class and error colours). |
| `tag-field.js` | A session's **Tags** field in the edit forms (settings dataset forms, the labeling wizard): `tagState`, `tagFieldHTML`, `bindTagFields` (delegated, repaints itself in place), `commitTagDraft` (before a save), `sameTags`, `MAX_TAGS` / `TAG_LEN` (the server's `clean_tags` rules). Suggestions open in the `combobox.js` list (a pick adds the tag). Imports `dom.js`, `combobox.js`. |
| `combobox.js` | A themed text field + list in place of `<input list>` / `<datalist>` (whose popup the browser draws unthemed): `comboHTML(key, value, {placeholder, mono, disabled, attrs})` (`.cbx` input + chevron), `bindCombo(root, key, {options, onPick, onInput, create, empty})` (listeners delegated once on the document, capture: survives re-renders; filters with `<mark>`, ↑ / ↓ / Home / End / Enter / Tab / Escape, optional Create row; a pick fires the input's `input` event, then `onPick`). The list floats (`.cbx-list`, above dialogs) at the field's width, flips up. Used by the label export Bucket, Settings › AI Model, the tags field. Imports only `dom.js`. |
| `stream.js` | `streamNDJSON(path, body, onEvent, signal)`: the one NDJSON reader (POST through `apiFetch`, line splitting, a non-2xx answer throws like `api()`), `splitLines`, `isAbort`. Used by `chat.ts`. |
| `chat.js` | The shared ◆ chat of both assistants: `mountChat(root, {role, scope(), sessionScope, onUiAction, onChange, onDone, onUpdate, settings, extraChips, composerExtra, placeholder, emptyHTML, inputId})` → a handle (`send`, `stop`, `newChat`, `note`, `user`, `custom`, `archive`, `catalog`, `mode`, `pending`, …). Session header (title: click to rename; History; New; delete with an inline confirm), the transcript, the composer (chips model / mode / effort / think / web, Stop; Shift+Tab cycles ask → auto → plan, Esc stops or blurs; `/` opens the command list) and the History panel (search, This bucket / dataset · All, pinned + Today / Yesterday / Last 7 days / Older, rename / pin / delete with inline Yes / No, Select + Delete N, Delete all (+ include pinned), `n / max`, the "New chat will remove …" notice, an 8 s Undo toast → restore). `reduce(turn, event)` is **pure** (`tests/test_chat_js.py`): it folds the agent events (`dataviz/llm/engine/events.py`) into blocks: thinking (live, elapsed), text (Markdown, `md.ts`), consecutive read tools grouped, change / danger tool cards (risk badge, args + result folded, Undo from `undo_id` → `POST api/dc/buckets/<id>/undo`), web search + sources (blocked queries), skill, todo checklist, confirm card (checkboxes, danger in red, editable web query, Approve / Reject → `/api/llm/agent/confirm`), ask / cannot, plan proposal (Approve in ask / auto sends the `planner.approved_message` text; Edit), UI action (Apply / applied), retry (`discard` drops the step's text), notices; page-only `x_*` events mark what the page did. Rendering puts every step of an answer (thinking, read tools, web searches, skills, retries, info notices, the words said between tool calls) into **one activity dropdown** at the top of the answer: closed, its header says what the model is doing now (Connecting / Waiting for <model> / Thinking / Running <tool> / Writing the answer / Waiting for your OK, step n/max, a live timer) or, once done, "Worked 14 s · thought 11 s · 2 tools · 1 search"; open, a rail of same-shaped rows (each opens its thinking text, arguments + result or sources) and a stats line (model, step, tokens in / out, tok/s; estimated while streaming). Change / danger cards, confirm, ask, plan, UI actions and the checklist stay in the answer. A chip or command change flashes a short line above the composer box. A resumed chat re-renders its stored traces folded; a pending confirm shows "expired · ask again". The chips are remembered per role (`PUT api/llm/config {chat}`). Imports `api`, `dom`, `llm`, `md`, `popover`, `stream`, `toast` (none touches the DOM at import time, so the reducer runs under node). Styles: `css/chat.css`. |
| `jobs.js` | `pollJobs` (the bar under the header), `waitJob(id, {onProgress, every, signal})`, `pollPause` (the wait between two polls of any job poller: slows down in a hidden tab, wakes when it is shown), `showWait` / `hideWait` (the plot overlay), `setBar`. |

## How it is written

- `dom.js`, `nav.js` and `state.js` import nothing; `api.js` imports only `nav.js`: pure helpers, safe to import anywhere.
  `dom.js` `ICONS` is an absolute URL built from its own `import.meta.url`, so icons work under `s/<id>/`.
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
