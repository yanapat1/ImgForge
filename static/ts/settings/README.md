# `static/ts/settings/` — the Settings dialog

It opens from the gear at the bottom of the activity bar, or from the dataset name in the header. The labeling page opens it with `openSettings('labeling')`, and the Sessions home from its gear. The
sections that need an open visualizer session (Models, Plot, Workspace: `data-needs="session"` in `index.html`) are
hidden on the home and the labeling page. The Sessions home opens it in **solo mode** (`openSettings('datasets', { solo: true, view: 'add' | edit: id })`):
no section nav, just the new / edit session forms; Cancel steps back to the two tiles or closes, and a
save without opening closes it and fires `sessions-changed` so the home redraws its cards.

| File | What it does |
|---|---|
| `../types-settings.ts` | Types only: `SET` (`SetState`), the discriminated forms (`SetForm`, tag `form`), the API payloads and `LabSettings`. |
| `dialog.ts` | The shell: `openSettings(section)` / `closeSettings`, loading `/api/datasets` and `/api/connections`, `renderSettings()` (nav, title, body), every **action** (`SET_ACTIONS`, bound to `data-act` buttons) and **change handler** (`SET_CHANGES`), and `bindSettings()` (delegated listeners, focus trap, Esc). It also binds the header theme toggle. |
| `models.ts` | Settings › Models: the saved models, models found in the project (one-click add), and the add / edit form (name + family → format → server path / upload / API endpoint → options), with the required output structure for Custom models and generic APIs. Its actions are merged into `SET_ACTIONS`. |
| `forms.ts` | One renderer per section or form, each returning an HTML string from the `SET` state: datasets list, add-dataset tiles, the **3-step S3 form** (connection → bucket and prefix → load mode), the local-folder form, connections, appearance, plot, workspace. Also the shared pieces: `statusHTML`, `crumbsHTML`, `foldersHTML`, `tagc`, and the **connection kinds** (`CONN_KINDS`: s3, ftp, sftp, postgres, mssql; `connDefaults`, `connFormHTML`, `connUri`, `connTestText`), which the labeling wizard reuses for its inline FTP / SFTP form. |
| `labeling.ts` | Settings › **Labeling**: every `label/state.ts` `SETTINGS_DEFAULTS` option (crosshair, faded other tasks, box drawing mode, fill opacity, line width, name tags + their text, commit scope, auto-advance + where to) and the **hotkey editor**: every `HOTKEY_LABELS` action with 2 combo cells. A click records the next key press through a window capture listener bound before the label page's own (so the combo never also runs); combos come from `label/hotkeys.ts` `comboOf`, so they match the page exactly; Esc cancels, Backspace clears; a combo used elsewhere shows "also used by …" with Swap / Cancel; `BROWSER_RESERVED` combos get a warning icon; Reset per row and Reset all. Every change is saved at once: localStorage `label-settings`, the `label-settings-changed` event (detail = the full settings, so an open label page applies it live) and a debounced `PUT /api/label/settings` storing only the hotkeys that differ from `DEFAULT_HOTKEYS`. |
| `llm.ts` | Settings › **AI assistant** (on every screen): connect an LLM and pick the AI features. `loadLlmData()` reads `GET api/llm/config` into `SET.llm` (an error lands in `SET.llmError`, shown in the section; nothing throws). **Empty state**: a gradient ◆ (own gradient id `gem-grad-hero`), "Connect an LLM to unlock AI features" and the **provider tiles** (`PRESETS`: OpenAI, Claude, Gemini, vLLM, Ollama, OpenRouter, Custom OpenAI-compatible: kind, base URL, suggested models, key needed, "Get a key" link). **Form** (`SET.view = 'llm-form'`, fields `data-f="llm.*"`): ① endpoint & key (write-only key, `env:VAR_NAME` reads a server env var) ② model (free text + `<datalist>` filled by **Load models** → `POST api/llm/models`), **Vision** / **Voice** switches guessed from the model name until the user touches them (voice off for Claude), Advanced: box format, timeout, max output tokens, temperature ③ **Test connection** → `POST api/llm/test {form}`. Typing does not re-render: `llmInput()` (called from the dialog's input listener) updates the step locks and the guess in place. Save → `POST api/llm/providers`, and if the last test passed for the same values, `POST api/llm/test {id}` records it, so the provider is Connected at once. **List**: provider cards (Active radio → `PUT api/llm/config {active}`, Connected / Not tested / Failed tags, Test / Edit / Remove), the **feature switches** (grouped; only once the active provider is connected; a switch whose capability the model lacks is disabled with the reason; each toggle `PUT`s at once; Turn all on / All off) and the **token budget** slider (8k–100k, debounced `PUT`). Every change fires the window event `llm-changed` (core/llm.ts re-applies every `[data-llm]` element). |

## How it is written

- **Render from state, delegate events.** All UI state is in `SET` (`sec`, `view`, `form`, and the
  loaded lists). A renderer is a pure function of `SET`. Buttons carry `data-act="name"`, and one click
  listener dispatches to `SET_ACTIONS[name]`.
- **Inputs write into state without re-rendering.** `data-f="conn.endpoint"` fields update `SET.form` on
  `input`, so typing never loses focus. Only actions re-render (test, browse, save).
- **Each step unlocks the next.** Step 2 is locked until the connection test passes, and step 3 until
  the prefix is a YOLO folder (it has `images/` and `labels/`).
- **Connections have a kind** (s3 by default, ftp, sftp, postgres, mssql; see `dataviz/labeling/README.md`). A new
  connection starts with the kind picker; a saved one keeps its kind. The S3 form and the S3 dataset flow are unchanged.
- **Secrets are write-only.** The page sends a secret once. Afterwards it only sees `has_secret`, and an
  empty field on save means "keep the stored one". Saved connections are tested by `{id}`, so the secret
  never comes back.

## Techniques

- **Folder browsers** for S3 and local folders: a breadcrumb plus a folder list, with a **YOLO** badge
  computed on the server. The S3 check lists each subfolder's sub-prefixes in parallel.
- **Opening a session navigates to `./?session=<id>`.** The server has already swapped the state, and `boot()` shows
  the new dataset's sync and index progress (files, MB, MB/s, ETA) on the startup screen.
- **Sync progress in place.** The card's bar follows the sync job through `waitJob`.
- **Focus management.** Focus moves into the dialog when it opens, Tab and Shift+Tab stay inside, and
  focus returns to the element that opened it.
