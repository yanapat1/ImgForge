---
title: Model, Evaluation and Compare views
summary: run a detector, val / train split, confidence and IoU, leaderboard and findings, Compare A vs B, combined datasets
---
- Detectors are added in Settings › Models. **Model** (`model`): pick a model, Run on this dataset, Images val /
  train / all (val is honest; train looks too good), Confidence (best = the best F1), Match IoU 0.5 / 0.75 / 0.3.
  It shows KPIs, a per-class table (AP50, P, R, miss, FP; click a class to load its mistakes), Likely label errors
  (a confident model disagreeing with the label), confusion matrix, precision-recall, F1 vs confidence, confidence
  of right vs wrong, Where the model misses (by size, brightness, sharpness, aspect) and False positives (often a
  box nobody drew). Colour by `m_status` / `m_pred` / `m_score` then works on the map.
- **Evaluation** (`eval`): every model on the same images. Score at best F1 (fairest) or a fixed confidence;
  What matters (findings), Leaderboard (mAP50, mAP50-95, P, R, F1, ms / image, missed, wrong class, FP background),
  Compare model A vs B, Speed vs accuracy; Explain (AI); Export Report / CSV / JSON. `eval_summary` reads it.
- **Compare** (`compare`): only in a combined dataset (2+ sessions joined): pick members A and B, the diff (images:
  only in A / only in B / same / labels changed / pixels changed, matched by content hash then name; boxes: same /
  moved / reclassed / reclassed + moved / unmatched, matched at IoU ≥ 0.5), the shared map, boxes of B unlike anything in A, class
  table, stat histograms; Colour by `origin` / `diff`. A combined dataset opens coloured by `origin` (unless the user
  saved another colouring; always when there is one class, e.g. image-only members whose every image is class
  `image`, where class colours paint every dot alike). With **Shared boxes in grey** (on by default, after a run) the
  boxes that are the same in A and B form one grey "same in A and B" trace, so identical dots do not hide each other
  and the A / B colours show only what differs; B is drawn on top of A. The density layer uses A's and B's colours.
  Image-only members (one point per image): the IoU sliders and area / aspect histograms are hidden.
- Include model checks in Curation adds the model's disagreements to the scan.
