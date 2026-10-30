---
title: Map quality (how far to trust the 2D map)
summary: trustworthiness, continuity, neighbours kept: what they mean, good values, what to do when they are low
---
Every new projection is scored against its feature space (`projection_quality`; the Projection popover shows the
line "map quality (n pts, k=10): trustworthiness · continuity · neighbours kept"). Computed on a fixed random
subset of ≤ 2000 boxes, k = 10 neighbours.
- **trustworthiness**: are dots that sit together on the map really neighbours in the features? 1 = no false
  neighbours. Low → clusters on the map may mix things that are not similar.
- **continuity**: are real neighbours kept together on the map? 1 = none torn apart. Low → one real group is split
  into several islands.
- **neighbours kept** (knn_preserved): mean share of each box's 10 nearest neighbours that are also its 10 nearest
  on the map. Usually much lower than the other two (0.3-0.6 is normal for UMAP on 1000+ boxes).
- Rough reading of trustworthiness / continuity: > 0.95 very faithful, 0.90-0.95 good, 0.80-0.90 read distances
  with care, < 0.80 the layout is mostly the method's.
- Typical: `umap` and `tsne` score high on trustworthiness (local), `pca` high on continuity but crowded;
  `pacmap` balances both.
- Low numbers? Raise `n_neighbors` (UMAP / PaCMAP), try `pacmap`, or check a cluster on `pca`. Explained variance
  (PCA only) under ~20 % for the 2 axes means a linear 2D view hides most of the structure.
- Whatever the numbers: the checks never use map distances (except Far from class (on the map)).
