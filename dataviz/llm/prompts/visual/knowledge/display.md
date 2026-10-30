---
title: Display options and density groups
summary: dot size, distances between picked boxes, class centres, gridlines, zoom to shown classes, bubbles, outliers
---
Display popover (above the map):
- **Dots**: Size; Distances between picked boxes (lines with the feature-space distance); Class centres (stars,
  always on with Far from class / Suspicious labels); Gridlines; Zoom to shown classes (re-fit when the shown classes
  change).
- **Density groups** (HDBSCAN on the 2D map): Draw bubbles, Mark outliers (red cross on dots in no group), Dim grouped
  dots; Grouping per class (each bubble one class; a dot that fits no group of its own class is an outlier) or all
  points; Min size (default 15: the smallest group). **Select all outliers** loads them (`sel-outliers`).
- Groups are made on the map, so they depend on the projection: use them to browse clusters, not as proof.
- Classes popover: All classes / One class / Compare 2 classes (projects only those classes: a clearer map of two
  look-alike classes). Class chips under the filter bar hide (click) or isolate (double-click) classes.
