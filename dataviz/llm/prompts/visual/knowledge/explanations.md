---
title: Explanations on the map (centre stars, neighbour lines)
summary: why a flagged dot looks fine on the map: class-centre stars, lines to the centre or to the nearest neighbours
---
While `suspicious`, `far` or `far2d` is on (or Display › Class centres is ticked):
- Each shown class gets a **centre star**. For `far` / `suspicious` it sits on the box nearest the class centre in
  the *features* (lowest outlier score); for `far2d` on the class's 2D median.
- Hover or pick a dot: **Far from class** draws a dashed line to its class centre with "distance to class centre x
  (in the features)"; **on the map** shows "n× the class's median distance (on the map)".
- **Suspicious labels** draws lines to its 10 nearest neighbours in the features, each coloured by that neighbour's
  class, and a label "7/10 nearest (features) are not cat · mostly dog". Lines that jump across the map show that the
  map tore real neighbours apart.
- Lines stay until another dot, a blank click, or a new selection. Up to 3 dots (suspicious) or 8 (far) at once.
- Use it to answer "why is this flagged when it sits inside its class?": the score is in the full feature space;
  the map is a 2D distortion (see `map-quality`).
