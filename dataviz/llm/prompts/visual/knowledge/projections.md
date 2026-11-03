---
title: Projection methods (feature space → 2D map)
summary: each method of the Method dropdown, what it preserves, its parameters and their effect, speed, when to use
core: true
---
| id | keeps | parameters (default) | speed / notes |
|---|---|---|---|
| `umap` | local neighbourhoods + some global layout | `n_neighbors` 15, `min_dist` 0.1 | fast; the default |
| `pacmap` | global structure (how clusters relate) better than UMAP | `n_neighbors` 10 | fast |
| `localmap` | PaCMAP with sharper gaps between clusters | `n_neighbors` 10 | fast |
| `tsne` | local clusters only: gaps and sizes between clusters mean nothing | `perplexity` 30 | slow (~1 min / 10k boxes) |
| `pca` | the 2 directions of largest variance (linear; shows explained variance) | none | instant; honest distances, crowded |
| `svd`, `ica`, `fa` | linear variants (no centring / independent axes / per-dim noise) | none | instant |
| `kpca` | PCA in an RBF kernel space | `gamma` 0 (= 1 / dims) | fit on ≤ 3000 points |
| `isomap` | distances along the neighbour graph | `n_neighbors` 10 | fit on ≤ 3000 points |
| `lle` | each point as a mix of its neighbours | `n_neighbors` 10 | fit on ≤ 3000 points |
| `spectral` | Laplacian eigenmaps of the k-NN graph | `n_neighbors` 10 | fit on ≤ 5000 points |
| `mds` | pairwise distances | none | fit on ≤ 1500 points |
| `random`, `trees` | baselines (random matrix; random-trees hashing + SVD) | none | instant |
- `n_neighbors` larger = more global structure, fewer tiny islands; smaller = finer local detail.
  `min_dist` smaller = tighter clumps (good for seeing duplicates), larger = spread out (reading density).
  `perplexity` ≈ how many close neighbours each point considers (5-50; below the class size).
- Pick: start with `umap`; `pacmap` when you care how classes relate; `tsne` for clean local clusters; `pca` to
  check that a structure is real and not made by the method. Changing Features, Method or a parameter does not recompute: press **Generate** (the dot on the Projection
  button shows unapplied changes), so several settings can be changed before one projection job.
- Checks (Suspicious labels, Far from class, duplicates, cleanlab) are computed in the full feature space, never
  on the map: a dot can look fine on the map and still be flagged.
