# `static/js/plot/` — the embedding plot

| File | What it does |
|---|---|
| `render.js` | `renderPlot()` builds every trace (dots by colour, bubbles, outlier ✕, highlight rings, pick / hover overlays) and calls `Plotly.react` once. Also `scheduleRender`, `updateHighlight`, `visibleIndices`, `hoverOf`, `pointInPoly`, `bubbleAt`. |
| `interactions.js` | All mouse input on the plot: the **lasso** (left-drag), **middle-drag** to move the lasso (or pan when there is none), **right-drag pan**, **wheel zoom** around the cursor. `plotAxes()` gives the geometry. |
| `marks.js` | Overlays that change often, without a full render: the hovered card's dot (animated), picked cards' dots, crosshairs, and **distance lines** between picks (numbered dots, labels with the feature-space distance). |
| `groups.js` | Density groups from `/api/groups`, the outlier test, selecting a group or an outlier set. |

## How it is written

- Plotly's own drag modes are off (`dragmode: false`), and so are its lasso, box, zoom and pan buttons.
  Every drag is handled here in **capture phase** on the plot element, so Plotly only sees plain hover
  and clicks.
- Coordinates: `px` are relative to the plot div, and the plot area starts at `xa._offset` /
  `ya._offset`. `xa.p2l(px − offset)` gives data coordinates, and `xa.l2p(x) + offset` goes back.
- Anything that must survive pan and zoom (the lasso) is stored in **data coordinates** and redrawn
  from them (`redrawLasso`, called on every `plotly_afterplot`).

## Techniques (and why)

| Technique | Where | Problem it solved |
|---|---|---|
| **Own SVG lasso** | `enableLasso` | Plotly's lasso recomputed the selection over every WebGL trace on each mouse move, so the line lagged behind the cursor. Now a move appends a point (at least 2 px apart) and one `<path>` updates per animation frame. The selection is computed **once** on release (bbox prefilter, then even-odd point-in-polygon, under 5 ms for 10k dots). Mouse moves are stopped from reaching Plotly while drawing. |
| **Middle-drag moves the lasso** | `enableMiddleDrag` | Plotly kept lasso outlines as editable selections; middle-dragging one re-selected continuously, and the browser's autoscroll ran on top, which froze the page. Now only a CSS `transform` moves the outline during the drag, and on release the offset is converted to data units and the selection runs once. The handler calls `preventDefault` (no autoscroll) and `stopPropagation` (Plotly never sees it). |
| **CSS-translate pan** | `startPan` | `Plotly.relayout` on every frame redrew everything. Now the WebGL canvas and SVG layer are translated with CSS while the mouse moves, and **one** relayout happens on release, or after 160 ms of rest so newly exposed edges fill in. Transforms are cleared only after the relayout resolves, so nothing flickers. |
| **Fade by colour, not alpha** | `renderPlot` (`outside lasso` trace) | UMAP stacks many dots on one pixel, so ten dots at alpha 0.1 add back up to a bright blob. Dots outside the lasso go into one legend-less trace drawn **first** (underneath), coloured `mixHex(classColour, plotBg, 0.82)` at full opacity. Measured: 2,880 bright pixels outside the lasso became 0. |
| Legend kept intact | class traces with `[null]` | A class with no dot inside the lasso keeps its legend entry. |
| Wheel zoom without Plotly | `enableWheelZoom` | Plotly's `scrollZoom` did not fire on this plot. Wheel deltas are accumulated and applied once per frame, zooming around the cursor. |
| Hover suppressed during drags | `plotly_hover` handler | Checks the `lassoing`, `panning` and `moving-lasso` classes on the plot element. |
| Click vs lasso | `lassoEndedAt` | The mouseup that ends a lasso is not treated as a click on a dot. |
| Distances fetched once per pick set | `distanceMarks` | Results are cached by `feature + ids`. A pending fetch re-renders the marks when it lands, and only if the picks haven't changed. |
