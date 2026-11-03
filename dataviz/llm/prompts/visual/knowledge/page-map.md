---
title: Where things are on the visualizer page
summary: activity bar views, the bar above the map (Projection, Color, Classes, Describe boxes, Find issues, Display), filter bar, gallery
core: true
---
- **Activity bar** (left icons; the active one collapses the left panel): ◆ AI assistant (`ai`), Explore (`explore`),
  Class stats (`stats`), Curation (`curation`), then the model views Model (`model`) and Evaluation (`eval`), and
  Compare (`compare`, only for a combined dataset: label diff A vs B, shared map, Image conditions with its Stress test). Settings is the gear at the bottom (Models, AI assistant,
  datasets, S3, appearance).
- **Explore** = gallery on the left, map on the right. The bar above the map, left to right:
  **Projection** popover (changing Features, Method or a parameter only marks the map changed (dot on the button);
  nothing is computed until **Generate** · Features, Method, the method's parameters, **Generate**, map quality line, Saved views,
  Import embeddings…) · **Color** dropdown · **Classes** popover (Show: All classes / One class / Compare 2 classes) ·
  **Describe boxes…** text search (`/` focuses it) · **Find issues** popover · **Display** popover.
- Under it: the **filter bar** (image name, Image has class, More filters, Images / Boxes scope, Show in gallery,
  presets, ◆ Describe… when the AI filter is on), then the **class chips** (click hides a class, double-click isolates).
- On the map: **Fit** button (top right), selection shapes **lasso / box / circle**, counters of dots past an edge
  (click to fit). "Compute embedding" appears when the chosen features / method are not computed yet; the jobs bar at
  the top shows progress.
- **Gallery** (left): title + count, class breakdown chips (click narrows), Sort, thumbnail Size, Tag…, Load tag…,
  Check labels (AI), CSV, Clear; the **pick bar** (Change class, Delete) appears when thumbnails are picked.
- A thumbnail click opens the box dialog: Find similar, Boxes in this image, Its group, Edit (fix / draw boxes, Save).
