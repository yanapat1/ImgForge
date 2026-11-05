---
title: Display options and density groups
summary: dot size, distances between picked boxes, class centres, gridlines, zoom to shown classes, bubbles, outliers; the 3D cloud's camera presets, spin, wall shadows, depth fog, drop lines, Shift+drag selection
---
Display popover (above the map):
- **Dots**: Size; Distances between picked boxes (lines with the feature-space distance); Class centres (stars,
  always on with Far from class / Suspicious labels); Gridlines; Zoom to shown classes (re-fit when the shown classes
  change).
- **Density groups** (HDBSCAN on the 2D map): Draw bubbles, Mark outliers (red cross on dots in no group), Dim grouped
  dots; Grouping per class (each bubble one class; a dot that fits no group of its own class is an outlier) or all
  points; Min size (default 15: the smallest group). **Select all outliers** loads them (`sel-outliers`).
- Groups are made on the map, so they depend on the projection: use them to browse clusters, not as proof.
- **3D cloud** (Dimensions = 3D cloud; Gridlines, Zoom to shown classes and Density groups are hidden there):
  - Mouse: drag = rotate, right-drag = pan, wheel = zoom, double-click = reset; **Shift+drag** = select with the
    lasso / box / circle tool (what is under the outline on screen, through the depth; the dots outside fade, like a
    2D lasso). Hover / click a dot as on the 2D map.
  - Toolbar over the plot: **3D** (isometric), **XY** / **XZ** / **YZ** (look along one axis: a flat view of the other
    two), **⟳** spin (turntable; speed = Display › Spin). Fit (⛶) = back to the isometric view, fitted to the shown dots.
  - Display › 3D cloud: **Shadows on the walls** (each wall shows the cloud's flat projection: three 2D views next to
    the 3D one), **Depth fog** (far dots fade: depth reads at a glance), **Drop lines to the floor** (from the hovered /
    picked dots, so their height reads; on by default), **Walls and grid**, **Orthographic** (no perspective: XY / XZ
    / YZ are then exact flat maps).
  - Picked boxes (ctrl+click 2+ cards) get numbered dots and distance lines labelled with the feature-space
    distance, as in 2D. Class centres are diamonds; with a Far / Suspicious check or Class centres on, the hovered /
    picked dots get lines to their class centre or nearest neighbours. "Far from class (on the map)" measures in 3D.
- Classes popover: All classes / One class / Compare 2 classes (projects only those classes: a clearer map of two
  look-alike classes). Class chips under the filter bar hide (click) or isolate (double-click) classes.
