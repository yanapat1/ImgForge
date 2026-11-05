---
name: read-the-map
description: explain what the 2D map (or 3D cloud) does and does not show, with its quality numbers
when: "what does this cluster / gap / distance mean", a flagged dot looks fine on the map, or the map looks wrong
---
1. `view_state` and `projection_quality` for the map on screen.
2. Explain the numbers: trustworthiness (no false neighbours), continuity (no torn-apart neighbours), neighbours kept;
   > 0.9 good, < 0.8 the layout is largely the method's.
3. Rules of reading, by method: `umap` / `tsne` keep neighbourhoods, not distances (gaps and cluster sizes mean
   little, `tsne` especially); `pacmap` keeps relations between clusters better; `pca` keeps global distances but
   crowds.
4. A cluster: colour by `class`, then `session` / `brightness` / `area`: a cluster made by lighting, camera or box size
   is a bias of the feature, not of the classes. Try a second feature (CLIP vs DINOv2) to see whether it survives.
5. A flagged dot that sits inside its class: the checks are in the full feature space; turn on `suspicious` or `far`
   and hover it: the lines go to its real neighbours / class centre. `far2d` is the only check on map distances.
6. To check a structure is real: `set_projection` with `pca` or `pacmap` on the same feature, or raise `n_neighbors`.
7. Two clusters overlap on the 2D map: `set_projection {dims: 3}` and tell the user to rotate it (or use the XZ /
   YZ buttons, Shadows on the walls): a 3D cloud often pulls them apart; Shift+drag selects what is seen.
