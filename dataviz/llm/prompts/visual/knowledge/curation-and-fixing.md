---
title: Curation and fixing labels
summary: Scan dataset, issue types, Fix / Fix all, change class, delete boxes, edit boxes, tags, undo and history
---
**Curation** view (`curation`): Scan dataset (a job, ~1 s for 20k images; Include model checks adds the model's
findings). Issue types by group, each with a count (`issue_summary` lists them):
- labels: `bad_line` malformed line, `bad_class` unknown class id, `degenerate` empty / tiny box, `conflict_box`
  same box two classes, `oob` box outside the image, `dup_box` duplicate box, `extreme_aspect`.
- images: `unreadable_image`, `missing_image` (label without image), `dup_image` same file bytes, `empty_label`
  background image. splits: `split_leak` a val / test image copied from train. dataset: `rare_class`,
  `class_imbalance`.
- embedding (need scores / cleanlab of a feature): `likely_mislabel`, `outlier`, `near_dup`, `cl_label_issue`,
  `cl_ood`. model: `model_wrong_cls`, `model_missing_label`, `model_missed`, `cl_swap`, `cl_badloc`, `cl_overlooked`.
- Each row: Fix (delete, dedupe, drop the line, set the class, clamp to the image) or Ignore. **Fix all…** fixes a
  bulk-fixable type at once. Show on plot filters the map to those boxes.
**Every fix edits the label files**, keeps a backup and can be undone: Undo in the toast, or Curation › History
(last 50 batches). Other ways to fix:
- Gallery pick bar: Change class / Delete on the picked boxes. "Change to cleanlab's class" on a cleanlab view.
- Box dialog › Edit: move / resize, Draw (N) a missing box, Region (R) relabels several, Save (Ctrl+S).
- Tag… marks boxes for later (e.g. "relabel") without changing labels.
You (the assistant) cannot fix labels yourself: point to the control, or open the view with `open_tab` /
`show_issue`, and let the user apply the fix.
