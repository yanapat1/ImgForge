---
title: Recommended projections (recipes) and preprocessing
summary: Recipe menu, Preprocess options
core: false
---
- **Recipe** (top of the Projection popover, `list_projection_recipes`, `apply_recipe {id}`): named settings for one
  purpose, grouped by category: **Label errors** (Find mislabels, Classes that overlap), **Duplicates**
  (Near-duplicates), **Domain shift** (Camera / lighting shift, Objects without the camera bias), **Overview** (Class
  separation, Quick linear look), **Small objects** (Small-object issues, Leave out tiny or blurry boxes),
  **Orientation** (Orientation and pose, Ignore left / right), **Background bias** (Background bias, Object only),
  **Segmentation shape**. A recipe sets only what it names (a feature, the method + parameters, dimensions,
  preprocessing, the colour) and keeps the rest, then generates the map; ✓ = its map is computed already. One that
  cannot work on this session is greyed out with why (no CLIP model, no segmentation polygons, a single camera, a
  whole-image / combined session). Prefer `apply_recipe` when the user states a purpose; read `list_projection_recipes`
  for the exact ids and what each sets rather than guessing.
- **Preprocess** (collapsed section after the method's parameters; `set_projection prep`; "Default" = none, the
  summary next to the title says what is on):
  - **Image · before the features** (a feature variant, computed once per dataset version, then cached):
    context box | +5 % | +10 % (default, the index crop) | +25 % | +50 % | whole image (the wider ones read every
    image again: slow on a streamed S3 session); fit square (default) | letterbox (grey bars, keeps the shape) |
    stretch | centre crop; tone gray | clahe (evens lighting); mask polygon (grey outside the segmentation
    polygon: the object only; segmentation labels only); tta hflip (features of the crop and its mirror averaged).
  - **Features · before the method** (no new extraction): scale standardize | whiten (PCA whitening);
    pca dims the method works on (50 default; 0 off, 20, 100, 200); debias session | folder (each group's mean
    removed: clusters stop being cameras / folders); fuse {feature, weight}: a second feature space added with that
    share (e.g. geometry 0.3 to bring box size in); filter {min_px (shorter box side), min_blur (sharpness score),
    brightness [lo, hi]} with show hide (default: left out of the map) | grey (placed by their neighbours, drawn faint,
    not selectable).
- The map's line under Generate says "preprocessed: …" and how many boxes were left out. Saved views keep the
  preprocessing; picking one restores it. Scores (suspicion, outliers, cleanlab) stay those of the plain feature.
- Default keeps every earlier map valid; each preprocessing is its own cached map (Saved views lists them).
