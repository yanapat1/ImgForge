---
name: near-duplicates
description: find near-identical boxes / images, train-val leakage, and decide what to remove
when: duplicates, copies, split leakage, a suspiciously high val score
---
1. `issue_summary`: Curation counts for `dup_image` (same file bytes), `dup_box`, `split_leak` and `near_dup` if
   scanned. Not scanned → `open_tab` `curation` and ask the user to Scan dataset.
2. Visual copies: a DINOv2 feature finds them best (`dinov2reg-large` or `dinov2-base`); propose `set_projection`
   when another feature is on, then `show_issue` `dups` (pairs from different images, cosine ≥ 0.97).
3. On the map, `umap` with a small `min_dist` (0.0-0.05) makes duplicate pairs sit on top of each other.
4. Leakage: `split_leak` lists val / test images copied from train. Those inflate validation scores: the fix is to
   move or delete one side in the Bucket page (the Bucket page assistant does that), then re-index.
5. Duplicates inside one split are mostly harmless for training but waste labelling time; burst frames from video
   are a judgement call: keep a few per scene (cutting the video again with Skip near-duplicates or Smart in the
   Bucket's Extract frames gives fewer, more varied frames).
6. The user removes boxes with the pick bar (Delete, undoable) or Curation fixes; files are the Bucket page's.
