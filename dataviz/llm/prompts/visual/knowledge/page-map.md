---
title: Where things are on the visualizer page
summary: activity bar views, the bar above the map (Projection, Color, Classes, Describe boxes, Find issues, Display), filter bar, gallery
core: true
---
- **◆ AI assistant** (you): the dock on the right of every page (the ◆ on its collapsed rail, its collapse button or Ctrl+. open / collapse it; see assistant).
- **Activity bar** (left icons; the active one collapses the left panel): Explore (`explore`),
  Class stats (`stats`), Curation (`curation`), Augment (`aug`: Albumentations recipes previewed on the images), then the model views Model (`model`) and Evaluation (`eval`), and
  Compare (`compare`, combined datasets only: label diff A vs B, shared map, Image conditions, Stress test).
  Settings: the gear at the bottom.
- **Explore** = gallery on the left, map on the right. The bar above the map, left to right:
  **Projection** popover (a change only marks the map (dot on the button) until **Generate** · **Points: Objects |
  Full images** (see image-points) · **Recipe** · Features, Method, its parameters, **Preprocess** (see
  projection-recipes), **Generate**, map quality line, Saved views, Import embeddings…) · **Color** dropdown · **Classes** popover (Show: All classes / One class / Compare 2 classes) ·
  **Describe boxes…** text search (`/` focuses it) · **Find issues** popover · **Display** popover.
- Under it: the **filter bar** (image name, Image has class, More filters, Images / Boxes scope, Show in gallery,
  presets, ◆ Describe… when the AI filter is on), then the **class chips** (click hides a class, double-click isolates).
- On the map: **Fit** (top right), selection **lasso / box / circle**, counters of dots past an edge. "Compute
  embedding" shows when the map is not computed yet; the jobs bar shows progress.
- **Gallery** (left): class chips (click narrows), Sort, Size, Tag…, Load tag…, Check labels (AI), CSV, Clear;
  the **pick bar** (Change class, Delete) when thumbnails are picked.
- A thumbnail click opens the box dialog: Find similar, Boxes in this image, Its group, Edit (fix / draw boxes, Save).
