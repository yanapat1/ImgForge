# `static/js/plot/` — the embedding plot

| File | What it does |
|---|---|
| `render.js` | `renderPlot()` builds every trace (dots by colour, bubbles, outlier ✕, highlight rings, pick / hover overlays) and calls `Plotly.react` once. Also `scheduleRender`, `updateHighlight`, `visibleIndices`, `hoverOf`, `pointInPoly`, `bubbleAt`. |
| `interactions.js` | All mouse input on the plot: the **lasso** (left-drag), **middle-drag** to move the lasso (or pan when there is none), **right-drag pan**, **wheel zoom** around the cursor. `plotAxes()` gives the geometry. |
| `marks.js` | Overlays that change often, without a full render: the hovered card's dot (animated), picked cards' dots, crosshairs, and **distance lines** between picks (numbered dots, labels with the feature-space distance). |
| `explain.js` | Why "Far from class" / "Suspicious labels" flag a dot: class-centre stars, lines from the hovered / picked dot to its centre or to its feature-space neighbours, and the on-the-map "Far from class" score. |
| `groups.js` | Density groups from `/api/groups`, the outlier test, selecting a group or an outlier set. |
| `render3d.ts` | The **3D cloud** (projection with `dims: 3`, `S.view.z` set): `renderPlot()` hands over to `renderPlot3d()` (per-colour `scatter3d` traces, same colouring / filter / hover as 2D). Camera presets + orthographic, turntable spin, wall shadows (`projection`), depth fog, drop lines, pick distance lines, 3D class centres, and `screenPoints()` for the Shift+drag selection (`interactions.ts enableLasso3d`). |

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
| **Explain feature-space checks on the 2D map** | `explain.ts` | Both checks are scored in the full feature space; UMAP / t-SNE distort distances, so flagged dots often looked close to their class. The centre star is the box with the lowest `outlier` (nearest the real centre); hover lines show the distance or the k nearest neighbours in their class colours. A plot hover only re-draws when the dot changes and unhover is ignored, so the update cannot loop with Plotly's hover. |
| **Zoom to shown classes + off-screen counters** | `fitToVisible`, `updateOffscreen` | Switching classes kept the old zoom, so a spread class lost its far dots off screen. A class change relayouts once to the bbox of the visible dots; after each draw one pass counts dots past every edge. |
| **3D cloud: Plotly's own camera, 2D gestures off** | `render3d.ts`, `plotAxes()` | In 3D `plotAxes()` returns null, so the lasso / pan / wheel handlers stand down and Plotly orbits. Shift+drag selects: on release each shown dot is projected once with gl-plot3d's matrices (`projection · view · model · xyz · dataScale`, as Plotly's 3D annotations do) and tested in screen pixels. While drawing, `mouseover` / `mouseenter` are swallowed too: Plotly's camera turns on those with a button held. |
| Camera kept across re-renders | `renderPlot3d` | `uirevision` keeps only mouse-made camera changes; presets and the spin use `relayout`, so each re-render of the same cloud passes the current camera back. |
| Fog by colour, legend apart | `applyFog`, legend traces | scatter3d has one opacity per trace, so depth fog (and the lasso fade) mix colours toward the background, quantised to 12 levels per colour. Legend entries are empty traces in the dots' `legendgroup`, so their swatch keeps the real colour. |
