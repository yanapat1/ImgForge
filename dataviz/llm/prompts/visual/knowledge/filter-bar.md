---
title: The filter bar (images and boxes)
summary: name match, Image has class, More filters (count, size, light, tags, scores, issues, text), scope, presets, the spec apply_filter takes
---
Above the map. Filtered-out dots vanish from the map and the gallery; the count updates live; Show in gallery loads
the result. Presets (bookmark icon) save a filter. Chips list the active parts (Clear all).
- **Name**: contains (`substr`), wildcard (`glob`, e.g. cam1*), regex, related (`fuzzy`, shares words / numbers); NOT.
- **Image has class**: any / all / none of the chosen classes.
- **More filters**: boxes per image, box area % of the image, aspect (w / h), brightness 0-255 (dark < 60), blur
  (laplacian variance, < 50 blurry), sessions, tags, model outcome, suspicion / outlier minimum, cleanlab quality
  maximum and "flagged only", curation issue types, text (CLIP / SigLIP).
- **Scope**: Images (a matching image keeps all its boxes) or Boxes (only the matching boxes).
- **◆ Describe…** (AI filter) turns a sentence into a filter; Undo returns to the previous one.
`apply_filter {spec}` uses the same shape: `scope` image|box, `name` {mode, q, neg}, `cls` {mode any|all|none, ids or
names}, `nbox` / `area` / `aspect` / `brightness` / `blur` as [min, max] with null for no limit, `tags`, `issues`,
`suspicion_min`, `outlier_min`, `cl_quality_max`, `cl_issue`, `clip` {q, k}. Unknown classes, tags and issue types
are dropped. Example: dark small dogs → {"scope": "box", "cls": {"names": ["dog"]}, "brightness": [null, 60],
"area": [null, 1]}.
