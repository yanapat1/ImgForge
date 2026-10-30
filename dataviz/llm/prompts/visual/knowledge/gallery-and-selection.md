---
title: Selecting, the gallery and the mouse
summary: lasso / box / circle, pick bar, mouse and keyboard on the map and gallery, Fit, Find similar, tags
---
**On the map**
- Left-drag draws the selection with the chosen shape (lasso / box / circle buttons on the map; circle: Shift for
  round). The boxes inside go to the gallery. A click outside the drawn shape cancels it (back to the active check).
- Middle-drag moves the drawn shape (no shape: pans). Right-drag or a two-finger scroll pans. Wheel / pinch zooms
  around the cursor. **Fit** zooms so every shown dot is in view (`fit_view`).
- Click a dot: its box dialog. Shift / Alt+click: its whole density group. With Display › Draw bubbles, a click on
  a bubble selects that group. Hovering shows the crop.
**Gallery**
- Click a thumbnail: box dialog (Find similar = the 48 nearest in the current features; Boxes in this image; Its
  group; Edit). Ctrl / Cmd+click or dragging a marquee over thumbnails **picks** them (Shift / Ctrl adds); Esc unpicks.
- Picked boxes: the pick bar's **Change class** and **Delete** (the Delete key) edit the label files, with Undo;
  with Display › Distances between picked boxes, lines on the map show their feature distance.
- Breakdown chips narrow the selection to one class (or to outliers / in groups). Sort: suspicion, outlier,
  cleanlab quality, area, blur, brightness, class, image, model mistakes.
- Tag… saves the selection under a tag (Load tag… brings it back; filter by tags). CSV exports ids, classes, boxes
  and scores. Check labels sends up to 96 crops to the AI.
