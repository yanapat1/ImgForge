---
title: Features (what each box is turned into)
summary: every extractor in the Features dropdown, what it captures, which to pick, text search needs CLIP / SigLIP
core: true
---
Each labelled box is cropped (10 % margin, 224 px) and turned into a vector; the map, the k-NN checks, duplicates and
cleanlab all work in that space. Computed once per dataset as a background job (GPU), then cached. `list_features`
gives the session's real list (ids, dims, computed ✓, text search).
| id | kind | captures | pick it for |
|---|---|---|---|
| `openclip-h14` | CLIP ViT-H/14, 1024-d | meaning / concept, robust to pose and colour; best class separation here | default; label errors, class confusion, text search |
| `siglip2-so400m` | SigLIP 2, 1152-d | like CLIP, strongest open encoder | second opinion to CLIP; text search |
| `clip-b32` | CLIP ViT-B/32, 512-d | coarse concepts, fast | quick look, text search on a big set |
| `dinov2reg-large` | DINOv2 + registers, 1024-d | visual similarity: shape, texture, parts; fine-grained | near-duplicates, sub-types inside a class, look-alike classes |
| `dinov2-base` / `dinov2-small` / `dinov2-large` | DINOv2, 768 / 384 / 1024-d | same, smaller = faster | the same, on a smaller GPU budget |
| `yolo:<run>` | your trained YOLO's backbone | what *your detector* sees | why the model confuses two classes |
| `color-hist` | HSV histogram | colour and lighting only | lighting / colour bias between classes or sessions |
| `hog` | edges and shape | outline, no meaning | shape outliers, cheap baseline |
| `geometry` | box size, position, aspect, brightness, blur (euclidean) | label geometry, not pixels | size / position bias, odd boxes |
- **CLIP / SigLIP vs DINOv2**: CLIP groups by *what it is* (a red and a blue car together); DINOv2 by *how it looks*
  (two near-identical photos together, a car seen from the side apart from one from the front). Disagreements
  between them are informative.
- **Text search** (Describe boxes…, `search_boxes_by_text`, the filter's text field) works only with `clip` /
  `siglip` features (`openclip-h14`, `siglip2-so400m`, `clip-b32`).
- Your own models (Settings › Models) and imported embeddings (Projection › Import embeddings…) join the list.
