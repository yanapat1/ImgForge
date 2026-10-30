---
title: Find issues (the checks and how they are scored)
summary: suspicious labels, far from class, on the map, near-duplicates, cleanlab label issues / quality / OOD, model checks
---
The Find issues popover (above the map) loads the flagged boxes into the gallery, worst first (up to 300, of the
classes shown). Turning a view on again turns it off. `show_issue` opens one; `issue_summary` gives the counts.
**Nearest neighbours** (k-NN in the current feature space, k = 10):
- `suspicious` Suspicious labels: score = share of a box's 10 nearest neighbours with another class (0-1); the
  majority neighbour class is the "kNN-suggested class". High = probably mislabelled or a look-alike class.
- `far` Far from class: distance to the class centre (cosine distance, or euclidean for `geometry`). Odd crops,
  wrong boxes, rare sub-types.
- `far2d` Far from class (on the map): distance to the class's 2D median ÷ the class's median distance. What the
  eye sees on this map, not the features.
- `dups` Near-duplicates: box pairs from different images with cosine ≥ 0.97 (spaces that are not cosine, like
  `hog` / `geometry`: the closest 0.5 % of pairs). Train / val copies inflate scores.
- `sel-outliers` (Display › Select all outliers): dots in no density group (HDBSCAN on the map, min size 15).
**cleanlab** (Run label-quality check first; classifier logistic / kNN / MLP; 5-fold out-of-fold probabilities on
the feature space, PCA to 256 dims; classes with < 5 boxes left out):
- `cl-issues` Label issues: labels cleanlab flags as wrong, most likely first (with its suggested class).
- `cl-low` Lowest label quality: every box by self-confidence, flagged or not. `cl-ood` Out-of-distribution.
- Class confusion… opens the confident joint (Class stats): which classes are mixed up, candidates to merge.
**Detection** (a model run in the Model tab): `cl-swap` Wrong class, `cl-badloc` Badly placed; Missed objects… lists
unlabelled objects the model finds.
- The neighbour checks need the features and scores of the current feature (Compute embedding). Results depend on
  the feature: confirm a suspicious class with a second feature (CLIP vs DINOv2) before mass-fixing.
- While `suspicious` / `far` / `far2d` is on, the map explains each dot (centre stars, lines): see `explanations`.
