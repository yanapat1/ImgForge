---
name: choose-feature-and-method
description: pick the feature space and projection method (with parameters) for what the user wants to see
when: "which features / method should I use", the map looks like one blob or confetti, or a new dataset
---
1. `view_state` (what is on screen) and `list_features` (what exists, computed ✓, text search).
2. Match the goal to a feature:
   - label errors, class confusion, text search → a CLIP / SigLIP one (`openclip-h14`, else `siglip2-so400m`).
   - near-duplicates, look-alike sub-types, viewpoint → DINOv2 (`dinov2reg-large`, or `dinov2-base` if lighter).
   - why *my detector* confuses classes → `yolo:<run>` of that model.
   - lighting / camera / size bias → `color-hist`, `geometry` (and colour by `brightness`, `session`).
   Prefer a computed one; an uncomputed deep feature is a GPU job (minutes on a big set): say so.
3. Match the goal to a method (`list_methods` for parameters):
   - general look: `umap` (`n_neighbors` 15, `min_dist` 0.1). Tiny islands everywhere → raise `n_neighbors` to 30-50.
   - how classes relate: `pacmap` (or `localmap` for sharper gaps).
   - tight local clusters (duplicates, sub-types): `tsne` (`perplexity` 30; 10-20 on small classes) or `umap` with
     `min_dist` 0.0-0.05.
   - is this structure real? `pca` (honest, linear; read its explained variance).
4. Propose it with `set_projection` (feature, method, params) and one line of why; add `set_color_by` if colour helps
   (e.g. `suggested` for confusion).
5. After it is applied, `projection_quality`: below ~0.9 trustworthiness / continuity, suggest the next variant.
