---
name: compare-models
description: compare detectors on this dataset, explain their errors with the data, recommend one
when: which model is better, why a model fails, the Evaluation tab
---
1. `eval_summary`: the leaderboard (mAP50, mAP50-95, P, R, F1, ms per image, missed, wrong class, FP background),
   per-class AP of the best model and the findings. Nothing evaluated → `list_models`, then `run_eval_all` (or
   `run_model` for one; the user approves in ask mode). No models at all → Settings › Models adds one (`cannot_do`).
2. Compare on val images (train scores look too good), each at its best F1 (the fairest), then mAP50-95 for box
   precision and ms / image for speed.
3. Explain the gaps with the data: `class_stats` (small / dark / blurry / rare classes vs per-class AP), the Model
   tab's Where the model misses chart, and the error columns (missed vs wrong class vs FP background).
4. Many FP background with high confidence are often unlabelled objects: suggest the Model tab's False positives
   and Missed objects…; wrong class pairs → `show_issue` `cl-swap` and colour by `m_status`.
5. Recommend: the model for the stated need (accuracy vs speed), and the data fixes that would help most (label
   errors first, then more examples of the weakest classes). Evaluation › Report exports it.
