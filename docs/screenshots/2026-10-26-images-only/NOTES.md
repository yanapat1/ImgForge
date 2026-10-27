# 2026-10-26 · images-only S3 prefix (Phase 1)

What was tested: a bucket prefix of plain images with **no labels**, shaped like `s3://motion-sample-po/WD06-sec`:
12 images directly in `WD06-sec/`, 8 in `cam1/`, 6 in `cam2/` and one `readme.txt`. It runs on a moto S3 server, with
a fresh PostgreSQL and the real `app.py`, driven by headless Chromium (`tests/test_image_unit_page.py`).
Re-run with `DATAVIZ_SCREENSHOTS=<new run folder> pytest tests/test_image_unit_page.py`.

Screenshots are never deleted: every run has its own folder.

| Run | Result | Notes |
|---|---|---|
| run-01 | passed | Its PNGs were cleared before the "do not delete" rule. What it showed: the map had not been computed yet (the default feature was *Box geometry*), cards said "1 box", the viewer said "boxes 0 · background". All three were fixed. |
| run-02 | passed | After those fixes. 03 caught the map while the "PCA 100 %" progress card was still up (the test waited too early). 04 showed the folder twice (folder row + class row). |
| run-03 | passed | 04 fixed: one folder row. 03 still caught the progress card: the test's wait passed before the card appeared. |
| run-04 | failed, no shots | The test read the status line of the bucket root before the prefix's own listing came back. This was a test race, not an app bug: the test now waits for the prefix's text. |
| run-05 | **passed** | All four steps as expected (below). |

## run-05

| # | Step | Expected | Seen | |
|---|---|---|---|---|
| 01 | New visualize session › S3, bucket `motion-sample-po`, prefix `WD06-sec`, Enter | The images are counted; the step is marked done | "Images found: 12 here · 14 in sub-folders (deeper folders are included too) · no labels". `cam1` shows "8 images" and `cam2` "6 images". Steps 1–3 have a ✓. `readme.txt` is not counted. | pass |
| 02 | Step 3 "What each point is" → tick "Sub-folder names are the classes" | "One point per image" is pre-selected; "One point per labelled box" is disabled because there are no labels | As expected. The box card is greyed with "Needs YOLO label files (images/ + labels/): none here.", and the checkbox lists "(cam1, cam2…)". | pass |
| 03 | Save & open → Compute embedding | One point per image, coloured by folder; no box tools | The top bar shows "26 images · 1 point per image · 3 folders". The map has 26 points (PCA of the colour histograms), with the legend "(top folder) 12 · cam1 8 · cam2 6". The filter bar shows "26 images" (no "boxes"), the cards have no "1 box" badge, and the Curation / Model / Evaluation tabs are hidden. | pass |
| 04 | Click the first image | The full image, no outline, an explanation instead of "label file / boxes" | The image `cam1/frame_000.jpg` at 320×240, no box drawn, and the rows "folder cam1" and "labels: none: one point per image. Label these images in a label session to get boxes." The Edit button is hidden. | pass |

Still to polish (not blocking): the gallery hint says "Lasso dots in the plot to see their box crops"; it should say "images"
in this unit.
