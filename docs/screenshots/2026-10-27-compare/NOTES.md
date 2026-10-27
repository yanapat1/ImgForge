# 2026-10-27 · Compare / Combine datasets

## Home & settings

Compare / Combine on the Sessions home and the Combined form of Settings (`views/home.ts`, `settings/forms.ts`,
`settings/dialog.ts`). Driven by `tests/test_union_home_page.py`: the real `app.py`, a fresh PostgreSQL database and
headless Chromium. The test sets up four local sessions (`ds`, Survey A/B/C, copies of the same small YOLO folder)
and one embeddings session (Speech vectors). Re-run with
`DATAVIZ_SCREENSHOTS=docs/screenshots/2026-10-27-compare pytest tests/test_union_home_page.py`.
The earlier runs' shots are kept in `home-run-01/` and `home-run-02/`; the shots at the top level come from the final run.

| Run | Result | Notes |
|---|---|---|
| home-run-01 | stopped by the test | Every card without a cover had a 24 px empty band above and below the hatched cover (see 50 and 51). Cause: the `.empty` rule in `components.css` (`margin: var(--s6) auto; max-width: 380px`) also matches `.sc-cover.empty`. This was already there before this feature. **Changed:** `home.css` now resets margin / max-width / padding on `.sc-cover.empty`. The test also expected the member chip text without a line break, which was wrong. |
| home-run-02 | all steps passed; failed at the end on console errors | `static/css/compare.css` (the other agent's file) did not exist yet, and the refused DELETE logs a 400. 53 / 62 (dark): the unchecked pick boxes were bright white boxes on the dark cards. 63 (400 px): the member names were cut to "Su…". **Changed:** `color-scheme: dark` for the pick box in the dark theme. On phones the member name now gets its own line under the alias and the buttons. The test waits 400 ms after a theme switch, because 53 was taken during the colour transition and made "Clear" look disabled. The expected 400 is filtered out of the console-error check. |
| final | **passed** | The shots below. |

| # | What I did | What I saw | What I changed because of it |
|---|---|---|---|
| 50 `50-home-cards-light` | Opened the home in the light theme | Five Visualize cards and the New tile. The covers now fill the top of the card with no empty band. The pick boxes are hidden until hover or focus. | The `.empty` margin fix (run-01). |
| 51 `51-home-pick-on-hover` | Hovered Survey A | Its pick box shows at the top-left of the cover, and the LOCAL badge slides right to make room for it. The other cards are unchanged. | — |
| 52 `52-home-two-picked-light` | Picked A with its box and B with Ctrl+click | Both cards have an accent ring and a ticked box. Once something is picked, every pick box is visible. The bar sticks to the bottom: "2 selected · A Survey A × · B Survey B × · Compare · Combine (2) · Clear". Ctrl+click did not open the card. The embeddings card's box is greyed out (disabled). | — |
| 53 `53-home-two-picked-dark` | The same, in the dark theme | The pick boxes are dark, not white. The bar has the accent border, and Compare is the primary button. | Dark `color-scheme` (run-02). |
| 54 `54-home-three-picked-dark` | Also picked C (the bar only) | "3 selected", A/B/C chips, Compare is disabled (tooltip: exactly 2), "Combine (3)" is enabled. | — |
| 55 `55-home-compare-opened` | Unpicked C, clicked Compare | The page went to `/s/<id>/#compare` and shows "Opening Survey A vs Survey B… member A (1/2): Survey A". `GET api/datasets` now has one union card with parts `[A: Survey A, B: Survey B]`, `uri union://<id>`. | — |
| 56 `56-home-combined-card-light` | Back to the home, hovered the combined card | The card has the badge "COMBINED" with the layers icon (amber), member chips "A Survey A" and "B Survey B" instead of a path, and a greyed, disabled pick box (tooltip: a combined dataset cannot be combined again). Picking A and B again and clicking Compare opened the **same** id (reuse): there is still only one union. | — |
| 57 `57-home-remove-member-refused` | Clicked Remove on Survey A and confirmed | An inline error under the heading: "Could not remove “Survey A”: used by the combined dataset “Survey A vs Survey B”: remove it there first". The card stays. Before this change the home showed a browser `alert()`. | Errors from Remove now go to the `#home-status` line (and to the error line above the Settings list). |
| 58 `58-settings-union-bad-alias-dark` | New session › Combined datasets, added A, B and C, typed "bad alias" as C's alias | Row 3 has a red border and the message "1 to 24 letters, digits, - or _" under the alias. The footer says "Alias of “Survey C”: …", and Save / Save & open are disabled. Row 1's number is filled in (it wins). The first row's ↑ and the last row's ↓ are disabled. The picker no longer lists the embeddings or combined sessions. | — |
| 59 `59-settings-union-three-light` | Changed the alias to "Cnew", moved C up twice, named it "Three surveys" | The order is now Cnew / A / B. The focus followed the moved row (row 2's ↑ is still focused after the move). The footer line is gone and Save is enabled. | — |
| 60 `60-home-three-member-card-light` | Saved (without opening) | The dialog closed and the home reloaded. The new card "Three surveys" has the chips "Cnew Survey C · A Survey A · B Survey B". The API has the parts in that order. The first union now has a mosaic and stats (50 images, 98 boxes) from its first open. Edit › Full edit… opened the form with the aliases Cnew, A, B in order. | — |
| 61 `61-home-narrow-picked-light` | 400 px wide, picked B and C | The cards are in one column and the pick boxes are always visible. The bar wraps: the chips on the first line, and Compare / Combine (2) / Clear sharing the second line. No horizontal scroll. | — |
| 62 `62-home-narrow-picked-dark` | The same, in the dark theme | Readable. The bar has the dark surface and border. Esc then cleared the selection and hid the bar. | — |
| 63 `63-settings-union-narrow-dark` | The Combined form at 400 px with two members | Each row shows the number, a wide alias field and ↑ ↓ ×, with "Survey A · local folder" on its own line underneath. The picker and Add wrap. | Phone layout of the rows (run-02). |

## Compare tab, map and side-by-side viewer

The visualizer side (`views/compare.ts`, `views/compare-viewer.ts`, `plot/render.ts`, `css/compare.css`). Driven by
`tests/test_compare_page.py`: the real `app.py`, a fresh PostgreSQL and headless Chromium at 1280×800. Two local
sessions with a known answer: 5 identical images, an identical image renamed in B, `edit.jpg` with one moved, one
reclassed and one added box in B, one image only in A and one only in B (other pixels). The union is created with
`POST /api/datasets {kind: 'union', reuse: true}` and opened at `/s/<union>/#compare`. Shots 01–12 at the top
level come from the final run. Earlier runs are kept in `run-01/` and `run-02/`. Re-run with
`DATAVIZ_TEST_CHROMIUM=<chrome> pytest tests/test_compare_page.py`.

| Run | Result | Notes |
|---|---|---|
| run-01 | stopped in the dark pass (test bug: `goto` to the same URL plus hash does not reload) | 04: the density cells were only small squares exactly on the dots, so the blur did nothing. Cause: `minCount = 1` hides every cell with no raw box. The heatmap trace also brought back tick marks on both map axes. 05: the confusion colour bar ran 0.6–1.4 for a single cell. 09: when zoomed, the box outlines in the viewer grew with the zoom (about 12 px wide at 6×), because CSS transforms scale `non-scaling-stroke`. 07: the density cells had **drifted off their dots** after the grid slider moved (64 → 96). **Changed:** `minCount = 0` (render.ts `densityZ`); `ticks: ''` on the map axes; `zmax` set on the confusion heatmap; stroke width `calc(2px / var(--zs))` set from the shared zoom (compare-viewer.ts `apply`, compare.css); the restyle in `applyFilterMask` now also sends `x0 / dx / y0 / dy` with `z` (**this was a real bug**). The test now calls `page.reload()`. |
| run-02 | **passed** | 10 (swipe after a zoom in side-by-side): the zoom carried over into the wider stacked pane and pointed at the image's right edge, so the left of the line showed only the white pane. 07: the identical-images thumbnails were still grey (lazy images not loaded when the shot was taken). **Changed:** a mode switch now goes back to fit (`setMode`). The test waits for the thumbnails. |
| final | **passed** | The shots below. |

| # | What I did | What I saw | What I changed because of it |
|---|---|---|---|
| 01 `01-compare-tab-summary` | Opened `/s/<union>/#compare`; the tab runs the comparison by itself | A · shop A vs B · shop B. Images: only A 1, only B 1, identical 6, labels differ 1, pixels differ 0, duplicates 0. Boxes: same 6, moved 1, class changed 1, removed 0, added 1, image only A 1 / B 1. All match the generated ground truth. Zero cards are dimmed. Each card has a left rule in its diff colour, and the image cards have a ⧉ side-by-side button. | none |
| 02 `02-compare-map-both-diff-colours` | Compute embedding (Box geometry · PCA), Colour = Diff | 19 dots (A 9 + B 10). Legend: same 12 grey, moved 2 amber, class changed 2 violet, added 1 green, image only A / B dark red / dark green. The grey "same" dots recede and the changes stand out. | none |
| 03 `03-compare-map-diff-only` | Show = Diff only | 7 dots (moved 2 + reclass 2 + added 1 + image only 2). The trace count is unchanged: the toggle is a `restyle` through `applyFilterMask`, not a re-render. The legend keeps every entry ("same (0)"). | none |
| 04 `04-compare-map-density` | Show = Both, tick Density difference | Blurred cells in A's blue (A denser) and B's orange (B denser) under the dots, transparent where the two match. No axis ticks. The legend under the sliders shows the A → B gradient and the novelty %. | run-01 fixes (minCount, ticks) |
| 05 `05-compare-tab-classes-hists` | Grid 96, open *Class changes* and *Box statistics* | Confusion: one cell, dog in A → cat in B (colour bar 0–1). Four step histograms (area, aspect, brightness, sharpness) in A blue / B orange, with KS in each title (red when KS > 0.2). | the confusion zmax (run-01) |
| 06 `06-explore-gallery-added` | Clicked the *Added* card | Explore opens with "Added: A vs B", 1 box: B's new dog in `edit.jpg`. | none |
| 07 `07-explore-images-identical` | Compare tab › *Identical* card | The image browser shows only those 6 images, titled "Identical: A vs B", with an "× All images" button. The density layer is still on its dots after the grid change. | the restyle fix (run-01); the test waits for the thumbnails (run-02) |
| 08 `08-viewer-side-by-side` | ⧉ on *Labels differ* | A/edit.jpg (2 boxes) and B/edit.jpg (3 boxes) side by side. The moved cat is amber on both sides, the reclassed dog → cat is violet, and B's added dog is green. | none |
| 09 `09-viewer-zoomed-synced-hover` | 3 wheel notches over pane A, then hovered A's moved box | Both panes are zoomed identically (the test asserts the two `style.transform` strings are equal). The hovered box and its partner in B are drawn thicker. The footer reads "A #5 cat ↔ B #14 cat · IoU 0.81 · moved". Outlines keep 2 px at any zoom. | the stroke-width fix (run-01) |
| 10 `10-viewer-swipe` | Swipe mode, mouse at 40% | B (left of the blue line: moved cat, added dog) over A (right: the dog that B calls a cat). Fitted. | fit on a mode switch (run-02) |
| 11 `11-dark-compare-tab-map` | Dark theme, reload, Colour = Diff, density on | Cards, chips and legend are readable on the dark surfaces. Brighter diff colours (amber `#ffc24b`, violet `#b98cff`, green `#6fe08a`). The density blobs stay faint enough to keep the dots visible. | none |
| 12 `12-dark-viewer-side-by-side` | ⧉ on *Labels differ* (dark) | Letterboxed images on the dark stage. The outlines and the black text on coloured tags keep their contrast. | none |

## End-to-end check by Claude: home → Compare → Promote → live map (80–90)

The real `app.py` on a fresh database, with two generated sites:
- Site A: 60 images.
- Site B: 45 of A's images (every 5th renamed `cam2_*`), with some boxes moved by +0.04, some reclassed and some extra boxes added, plus 20 images of its own (`night_*`).

The browser is headless Chromium, driven by a Playwright script (1400×900, light theme). The Playwright MCP server could not start, because it looks for Google Chrome at `/opt/google/chrome`.

| Shot | What I did | What I saw | What I changed because of it |
|---|---|---|---|
| 80 `80-e2e-home-two-datasets` | Signed in and opened the home page | Site A and Site B cards, each with a pick box. | none |
| 81 `81-e2e-home-both-picked` | Picked Site A, then Site B | The action bar shows the A and B chips; Compare and Combine (2) are enabled. | My script first picked cards by position. That broke once other cards existed (combined, live), so it now picks by name. |
| 82 `82-e2e-compare-tab` | Compare | The tab was ready 2.5 s after the click; members already indexed. The numbers match the ground truth:<br>• 15 only in A, 20 only in B<br>• 23 identical and 22 labels differ (all 9 renamed copies matched by content)<br>• 86 same, 7 class changed<br>• 11 moved plus 3 removed and 3 added: the generator moved 14 boxes, but 3 of them were very small, so the +0.04 shift left IoU < 0.5 and they count correctly as removed + added<br>The map was empty here: "Embedding not computed yet". | The script now presses Compute embedding first (82b). |
| 82b `82b-e2e-map-diff-colours` | Compute embedding, then Colour = Diff | All 299 boxes on one joint map, coloured by diff status. | none |
| 83 `83-e2e-diff-only` | Show = Diff only | Only changed boxes are drawn. The legend counts dots, so it shows both sides of each pair: moved 22 = 11 pairs × 2, class changed 14 = 7 × 2. The cards count pairs. | none (documented) |
| 84 `84-e2e-density` | Show = Both, density on | Density-difference heatmap with "A denser / B denser" legend and grid and blur sliders. | none |
| 85 `85-e2e-side-by-side` | ⧉ on *Labels differ* | A/frame_002 against B/frame_002. The person → bike reclass is violet on both sides; unchanged boxes are dashed grey. | none |
| 86 `86-e2e-side-by-side-next` | → key | Next image of the list (2 / 22). | none |
| 87 `87-e2e-promote-dialog` | Promote to labeling… | Name, dedup, label policy, priority (A, B), background images and live session options. | A second promote with the pre-filled name failed with "a label session called … already exists". It now takes the next free name, "… (2)"; `promote.py`, `test_promote.py::test_promote_twice_takes_a_free_name`. |
| 88 `88-e2e-promote-done` | Promote | Done in 2.9 s. The result dialog links to the labeling page and the live map. | none |
| 89 `89-e2e-live-map` | Opened the live map, then Compute embedding | Ready in 3.7 s. Donor reuse: the indexer decoded no image and computed no feature. | none |
| 90 `90-e2e-live-after-edit` | Through the labeling API (as the labeling page does): changed the class of one box on the first image and drew a new box | The class change was on the map 0.25 s after the save. The new box became an orange-ring provisional dot ("new boxes (1)") after 0.29 s, placed by its nearest neighbours in the shown projection. | Two copy and layout fixes:<br>• The banner said "re-index to place them in the plot" while the dot was already shown. It now says "shown as provisional dots (rings), re-index to place them exactly" (`image-grid.ts`).<br>• The "● live" badge was cut off at the right edge. It now goes right after the dataset pill (`core/live.ts`). |
