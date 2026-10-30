---
name: curate-a-class
description: clean one class (or two that get mixed up): browse, find wrong / odd boxes, relabel or remove them
when: the user wants to check, clean or fix one class, or two classes are confused
---
1. `class_stats` for the class: boxes, images, size / light quantiles, how many look mislabelled and the class it is
   confused with.
2. Show it alone: `apply_filter` with that class (cls names) or Classes › One class; for a confused pair, Compare 2
   classes (a projection of only those two) and colour by `suggested`.
3. Browse problems: `show_issue` `suspicious` (wrong class), `far` (odd crops, bad boxes), `cl-issues` if cleanlab ran.
   Sort the gallery by suspicion or outlier.
4. `selection_view` on what the user selected to report classes, images and sizes of those boxes.
5. Fixes (the user applies them): pick bar › Change class / Delete; Edit in the box dialog for a misplaced box;
   Tag… "relabel" for later. Undo is in the toast and Curation › History.
6. If the two classes overlap a lot (Class confusion… in Find issues), suggest merging them or writing a clearer
   labelling rule, and say how many boxes it would affect.
