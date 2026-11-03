---
title: Colour by (the Color dropdown)
summary: every colour-by option, what it shows and when it is available
---
`set_color_by` takes these values (the dropdown's own):
- `class` the label (class palette). `group` density group (Display bubbles). `suggested` kNN-suggested class:
  dots whose colour differs from `class` are suspicious. `session` the image's session / sub-folder.
- `suspicion` label suspicion 0-1 (k-NN). `outlier` distance to the class centre.
- `area` box area (log), `aspect` aspect ratio (log), `brightness`, `blur` sharpness (log): spot size / light
  clusters that a feature space picked up instead of the object.
- cleanlab (after Run label-quality check): `cl_issue`, `cl_quality`, `cl_suggested`, `cl_given_prob` P(given class),
  `cl_ood_feat`, `cl_ood_prob`.
- combined dataset (Compare): `origin` dataset A / B / … (the default there; same-in-both boxes grey), `diff` box
  diff, `origin_cls` dataset × class (A's and B's classes in different colours and shapes, legend "A · car").
- a model picked in the Model tab: `m_status` found / missed / wrong, `m_pred` predicted class, `m_score` confidence,
  `cl_swap`, `cl_badloc`, `cl_image_quality` (cleanlab on the model's predictions); in a combined dataset also
  `origin_pred` dataset × predicted class.
- one point per image (image-only data, or Full images points) with a model run in the Model tab: `ip_cls` the
  model's top class per image ("nothing found" below the confidence), `ip_score` its confidence; in a combined
  dataset `origin_ip` dataset × predicted class.
- Embeddings sessions (rows, not boxes) drop the box-geometry options and add metadata columns; image sessions drop
  `area` / `aspect`. Options whose data is missing are disabled.
- Tip: colour by `brightness` or `session` on `color-hist` / `dinov2-base` to see whether the map splits by lighting
  or camera rather than by class.
