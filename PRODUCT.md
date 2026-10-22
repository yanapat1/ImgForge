# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Primary: a solo ML / computer-vision researcher curating their own YOLO datasets before and between training runs. They explore embeddings, look for bad labels, train/val leaks, outliers and near-duplicates, compare models, and fix boxes. The multi-user features (accounts, private/global sessions, Viewer/Labeler roles) are for occasional collaborators, not a team workflow.

## Product Purpose
Dataset Visualizer is a self-hosted, FiftyOne-style explorer for YOLO datasets. Every bounding box becomes a dot in a 2D projection (UMAP / PaCMAP / t-SNE / PCA) of a chosen feature space (OpenCLIP, SigLIP 2, DINOv2, own YOLO weights, colour histogram, HOG, box geometry). The researcher lassos dots or describes them in words to see their crops, runs curation checks, and corrects labels. Success: dataset problems are found and fixed in the same sitting, and the next training run starts from cleaner data.

## Positioning
Find and fix in one place. The loop goes embed → spot the problem on the plot (suspicious labels, leaks, outliers, duplicates, model disagreement) → fix the box right there with undo and conflict checks, on local disk or S3 / RustFS. There is no export/import round-trip to a separate labeling tool.

## Operating Context
- Long working sessions on large monitors, moving between the plot, crop galleries, the image viewer / box editor, Curation, Evaluation and Model tabs, and the label page.
- Data lives on local folders, S3 / RustFS buckets, FTP/SFTP and databases. Models come from local weights or API model servers. An optional ◆ AI assistant uses the user's own LLM keys.
- Slow work (features, projections, scans, predictions) always runs as a job with progress shown in the page and the terminal.
- Runs on the user's own GPU box via Docker (PostgreSQL + pgvector). It may be served behind a proxy prefix.

## Capabilities and Constraints
- Screens: sign-in, Sessions home, visualizer (`/s/<id>/`), labeling page. All share one `index.html`.
- Front end: strict TypeScript in `static/ts` built with esbuild. Plain CSS in cascade-ordered files. Plotly is the only UI library. Colours exist only as CSS tokens, and the plot reads them from the CSS.
- Existing fact, not a confirmed commitment: light/dark themes, six selectable styles (Instrument, Editorial, Blueprint, Brutalist, Terminal, Soft) and six colour palettes (Default, Colour-blind safe, High contrast, Fjord, Ember, Orchid).
- Terminology: session, visualize vs label session, owner / Viewer / Labeler, connection, curation scan, fix / undo, job.

## Brand Commitments
- Name: "Dataset Visualizer". The README says "FiftyOne-style", which compares the product but does not borrow FiftyOne's identity.
- Engineering voice from the README conventions: comments say why, performance claims are measured, nothing slow hangs a request.

## Evidence on Hand
- README.md, the per-folder READMEs, `examples/`, and two screenshots in the repo root (`Screenshot 2026-10-19 191514.png`, `image.png`).
- No users, testimonials, benchmarks for marketing, or pricing exist. Do not make them up.

## Product Principles
1. **The fix sits next to the finding.** Every insight surface should lead straight to the correcting action, with undo.
2. **Density over ceremony.** Built for hours of use by one expert. Show more data and use fewer modal steps, with low eye strain.
3. **Nothing hangs.** Slow work is a visible job with progress and ETA.
4. **The user's data, the user's machine.** Self-hosted, with the user's own connections, models and keys. Secrets are never exposed.

## Accessibility & Inclusion
- Class colours must stay distinguishable for colour-blind users, as well as meeting contrast against the plot (the current palettes are ≥ 4.3:1 per theme in `config.yaml`).
- UI text and layout must handle Thai and other non-English text, now or later: the Atkinson Hyperlegible fonts don't cover Thai, so a fallback is needed, along with line height for tall scripts and text expansion. `index.html` is currently `lang="en"`.
- Comfortable for long sessions: steady contrast, no gratuitous motion.
