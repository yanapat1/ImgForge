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
- **After fixing: Export dataset / Send to Label** (header buttons, shown when the session reads a bucket folder and
  the user owns it). Fixes saved here are in the label files, so both carry them.
  - **Export dataset…**: pick a label format the system writes (YOLO, YOLO OBB, COCO, VOC, LabelMe, CreateML, CSV,
    image folders; what the task allows) and a bucket + folder (default: the session's own bucket,
    `<folder>/exports/<name>-<format>`; it must be new or empty). Images are hard-linked next to the labels; a
    streamed S3 bucket gets it in S3 at that prefix (images copied inside S3, nothing downloaded). A job: the bar
    under the header shows progress + ETA; the toast opens it in Bucket. The assistant: `export_dataset {format,
    bucket?, path?}`. Reads YOLO detect / segment / pose and COCO folders.
  - **Send to Label…**: a labeling project on the **same folder** (images in place, the folder's labels imported),
    name + task; the toast opens it. The assistant: `send_to_label {name?, task_type?}`. Label edits stay in the
    project until exported from Label. A combined session: Promote to labeling instead.
  - The Bucket page marks every label file fixed here (an amber dot, counts on its parent folders), so the user
    sees there what changed; Upload to S3 sends a downloaded folder's fixes back.
  - A session on a **streamed S3** folder never writes S3: its labels/ was downloaded when the session was made, so
    fixes land in that local copy (Bucket marks them; right-click › Upload to S3 sends them). An older S3 session
    without local labels cannot be fixed (Curation is hidden): create it again from Bucket.
