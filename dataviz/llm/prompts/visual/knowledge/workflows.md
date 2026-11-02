---
title: Common workflows (short)
summary: first look at a dataset, clean labels, find duplicates / leakage, explain a model's errors, new camera / lighting, prepare for training
---
1. **First look**: `dataset_summary` → class balance, tiny / dark / blurry boxes; Class stats for the kNN confusion
   matrix; `umap` on `openclip-h14`, colour by `class`, then by `session` / `brightness` for capture bias.
2. **Clean labels**: Curation › Scan dataset and fix the error-severity types; `suspicious` + `cl-issues` on two features
   (CLIP and DINOv2); fix the boxes flagged by both first; re-run the check (skill `find-label-errors`).
3. **Duplicates / leakage**: `dups` on `dinov2reg-large` (visual copies), Curation `split_leak` and `dup_image`
   (skill `near-duplicates`).
4. **Two classes mixed up**: Classes › Compare 2 classes, Class confusion…, colour by `suggested`; merge or relabel
   (skill `curate-a-class`).
5. **Model errors**: Model tab on val, Likely label errors and False positives first (often label mistakes), then
   Evaluation to compare models (skill `compare-models`).
6. **Map looks odd**: `projection_quality`, try `pacmap` or more `n_neighbors`, check on `pca` (skill `read-the-map`).
7. **New camera / lighting**: combine old (A) and new (B) data, Compare › Image conditions › Analyse, `compare_shift`
   for the verdict, drifting metrics and model fit (skill `check-camera-shift`).
