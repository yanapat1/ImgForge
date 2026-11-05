# ◆ Assistant right dock: design variants

Static mockups of the assistant moved to the right side of every page. Re-render them with
`.venv/bin/python design/assistant/_src/render.py`. The sources are `_src/variant{1,2,3}.html` plus the shared
`_src/mock.css` and `_src/mock.js`, and they use the app's own `static/css/tokens.css`.

Every variant shows the same five states:
- the compaction divider ("Context compacted · view summary")
- a run that needs approval (Ask mode) or a running job (Auto mode)
- a "Can't" card that says why and what to do instead
- the ◆ pulse on the tab and button the AI used
- the Ask / Auto / Plan switch

| Variant | Files | Idea |
|---|---|---|
| 1 · Push dock | `v1-*.png`, `v1-dark-desktop-data.png`, `v1-light-desktop-collapsed.png` | A full-height right column, 420 px wide, resizable from 320 to 640 px, that collapses to a 44 px ◆ rail. The page reflows beside it. Shown in Ask mode with an Approve card. |
| 2 · Floating sheet | `v2-*.png` | A card that floats over the page without reflowing it. It can be pinned as a dock. While the AI drives, a "driving" bar appears with Esc to stop. Shown in Auto mode: a job runs, then a Can't card. |
| 3 · Dock + task rail | `v3-*.png` | Variant 1 plus a Tasks strip with live plan steps, an "is driving" breadcrumb with Stop, and step timings. Shown in Auto mode with the finished answer. |

On narrow screens (below 900 px), every variant becomes a bottom sheet that fills the screen (`*-narrow.png`).
