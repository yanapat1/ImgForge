---
name: compare-models
description: compare detectors on this dataset, explain their errors with the data, recommend one
when: the user asks which model is better, why a model fails, or to explain the Evaluation tab
---
1. `eval_summary`: the leaderboard (mAP50, mAP50-95, P, R, F1, ms per image, missed, wrong class, FP background),
   per-class AP of the best model and the findings. Nothing evaluated → `open_tab` `model` and explain Run on this
   dataset (Settings › Models adds a model).
2. Compare on val images (train scores look too good), each at its best F1 (the fairest), then mAP50-95 for box
   precision and ms / image for speed.
3. Explain the gaps with the data: `class_stats` (small / dark / blurry / rare classes vs per-class AP), the Model
   tab's Where the model misses chart, and the error columns (missed vs wrong class vs FP background).
4. Many FP background with high confidence are often unlabelled objects: suggest the Model tab's False positives
   and Missed objects…; wrong class pairs → `show_issue` `cl-swap` and colour by `m_status`.
5. Recommend: the model for the stated need (accuracy vs speed), and the data fixes that would help most (label
   errors first, then more examples of the weakest classes). Evaluation › Report exports it.
