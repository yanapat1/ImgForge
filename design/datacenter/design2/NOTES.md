# Design 2: Bucket board

**Concept:** The first screen is a grid of bucket cards that looks like the Sessions home, so Data and Sessions read as siblings. Each card shows a 4-image strip, the source (Managed / Local link / S3 link), a colour-coded format badge with the detected structure in mono under it, counts, tags, and "Used by N sessions". Opening a card drills into a file manager with three parts: a breadcrumb header, a folder tree that also shows the format check, and a files pane. The assistant is a 404px right-hand panel that you open from "Assistant" in the header.

- **img1:** The board, filtered by the tag `cv-team` (8 of 9 buckets; a footer line names the hidden one). It also has the format segmented control with counts, search, sort, grid/list toggle, Import data and Link source.
- **img2:** `traffic-cams-2026 / images / train` as thumbnails, with label-count and "no label" badges. 6 tiles are selected, which brings up the floating bar (Move · Copy · Tag · Delete · Send to…). The tree shows the format check (images/, labels/ mirror, data.yaml, 24 images without .txt) and which sessions use the bucket.
- **img3:** The same folder in "Names only" (name, label status, size, dimensions, captured, last change). The folder-tag popover is open with existing tags, suggestions for "dus", "create tag", and the locked auto tag `format:yolo`.
- **img4:** The assistant panel open over the board. It shows: two read-only tool calls (badged READ, ran automatically), then a 4-step plan with Confirm / Cancel; a clarifying question with choice chips that show counts; and an "I can't do that here" card with Why / Instead and an action to take instead.
- **img5:** After `labels/` was moved out: the card badge is now dashed "Other", with "YOLO detect" struck through and a warning line. A toast shows YOLO detect → Other with the reason and "Undo move". The "Send to session" dialog is open: selected folders summary, an "add more data" link, Visualize vs Label, new vs existing session, name, task type, and pre-labels.
- **img6 / img7:** img1 and img2 in the light theme.

**Strengths**
- The badge is the most prominent thing on each card. The mono structure hint under it explains why the format was detected, and the tree's format check and the toast reuse the same wording.
- Familiar: reuses session-card patterns (source pill, tag chips, picked ring) and the app tokens, chips and ◆ assistant look.
- The assistant's safety model is visible at a glance: READ tags on tools, a plan card with checkboxes, a single Confirm, and "undoable".
- The dialog is placed so the changed card stays readable behind a lighter scrim.

**Weaknesses**
- Cards are roomy: only 8 buckets fit above the fold at 1600px. Teams with 50+ buckets will want the list view (shown only as a toggle).
- Format colours have to use the existing error palette, so YOLO seg and Embeddings (purple and lavender) are close and need their icons to tell them apart.
- The folder tree takes 264px in the bucket view, and the tag popover covers part of the list.
- With the assistant open, the board drops to 3 columns, and the tag and format filters are cut down to "+N more" / "More".
